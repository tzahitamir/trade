"""
Gap detection and backfill for fx_candles.

Scans the DB for contiguous ranges of missing candles between the earliest
and latest stored timestamps for each (symbol, timeframe) pair. Detected
gaps are backfilled from the Twelve Data API in chunks, respecting rate limits.

Weekend / market-closure gaps are identified by heuristic and excluded:
  a gap is treated as expected if it starts on Friday evening or later
  (weekday >= 4) and its duration is ≤ WEEKEND_THRESHOLD_H hours.

Usage:
    from data.gap_detector import detect_gaps, backfill_gaps

    gaps = detect_gaps(db, "EURUSD", "15m")
    inserted = backfill_gaps(db, fetcher, "EURUSD", "15m", gaps)
"""

import logging
import time
from datetime import datetime, timezone
from typing import List, Optional, Tuple


# Max candles per Twelve Data request (API ceiling is 5000; leave a safety margin)
CHUNK_CANDLES = 4800

# Gaps that start on Fri/Sat/Sun and span ≤ this many hours are market-closure gaps
WEEKEND_THRESHOLD_H = 80

# Seconds between API requests to stay within Twelve Data free tier (8 calls/min)
RATE_LIMIT_SLEEP = 8

# A consecutive-pair span must exceed this multiple of the candle interval to be a gap
MIN_GAP_INTERVALS = 2.5

# (month, day) tuples where Forex markets are universally closed
FOREX_HOLIDAYS: set = {
    (12, 25),  # Christmas Day
    (1,  1),   # New Year's Day
}

INTERVAL_SECONDS: dict = {
    "5m":   300, "5min":  300,
    "15m":  900, "15min": 900,
    "30m": 1800, "30min": 1800,
    "1h":  3600, "h1":   3600, "60min": 3600,
    "4h": 14400, "h4":  14400,
    "1d": 86400, "1day": 86400,
}


def _interval_sec(timeframe: str) -> int:
    return INTERVAL_SECONDS.get(timeframe.lower(), 900)


def _is_expected_gap(t1: int, t2: int) -> bool:
    """
    Return True if the gap [t1, t2] is an expected market closure.

    Covers two cases:
      1. Weekend gap — starts Friday evening or later, ≤ 80 hours duration.
      2. Holiday gap — the gap window overlaps a known Forex market holiday.
    """
    from datetime import timedelta
    dt_start   = datetime.fromtimestamp(t1, tz=timezone.utc)
    dt_end     = datetime.fromtimestamp(t2, tz=timezone.utc)
    duration_h = (t2 - t1) / 3600

    # Weekend: starts Fri (4), Sat (5), or Sun (6) and is short enough
    if dt_start.weekday() >= 4 and duration_h <= WEEKEND_THRESHOLD_H:
        return True

    # Holiday: gap window contains at least one known market holiday
    if duration_h <= 72:
        cur = dt_start.date()
        end = dt_end.date()
        while cur <= end:
            if (cur.month, cur.day) in FOREX_HOLIDAYS:
                return True
            cur += timedelta(days=1)

    return False


def detect_gaps(
    db,
    symbol: str,
    timeframe: str,
    since_ts: Optional[int] = None,
) -> List[Tuple[int, int]]:
    """
    Return a sorted list of (gap_start_ts, gap_end_ts) for missing candle ranges.

    gap_start_ts — timestamp of the first candle that should exist but is absent.
    gap_end_ts   — timestamp of the last candle that should exist before the
                   next present candle.

    Parameters
    ----------
    db          : LocalDB instance
    symbol      : e.g. "EURUSD"
    timeframe   : e.g. "15m"
    since_ts    : only inspect data at or after this UNIX timestamp; defaults
                  to 365 days back so very old gaps are ignored.
    """
    interval = _interval_sec(timeframe)
    threshold = interval * MIN_GAP_INTERVALS

    if since_ts is None:
        since_ts = int(datetime.now(timezone.utc).timestamp()) - 365 * 24 * 3600

    timestamps = db.get_all_timestamps(symbol, timeframe, since_ts=since_ts)
    if len(timestamps) < 2:
        return []

    gaps: List[Tuple[int, int]] = []
    for i in range(len(timestamps) - 1):
        t1, t2 = timestamps[i], timestamps[i + 1]
        if (t2 - t1) <= threshold:
            continue
        if _is_expected_gap(t1, t2):
            continue
        gap_start = t1 + interval
        gap_end   = t2 - interval
        if gap_start <= gap_end:
            gaps.append((gap_start, gap_end))

    return gaps


def backfill_gaps(
    db,
    fetcher,
    symbol: str,
    timeframe: str,
    gaps: List[Tuple[int, int]],
) -> int:
    """
    Fetch and store candles for every detected gap.

    Large gaps are filled in CHUNK_CANDLES-sized requests walking backwards
    through the missing window, with RATE_LIMIT_SLEEP seconds between calls.

    Returns total number of candles inserted across all gaps.
    """
    if not gaps:
        return 0

    interval   = _interval_sec(timeframe)
    total      = 0

    for gap_start, gap_end in gaps:
        needed = max(1, (gap_end - gap_start) // interval + 1)
        logging.info(
            "Gap  %s %s  %s → %s  (%d candles missing)",
            symbol, timeframe,
            datetime.fromtimestamp(gap_start, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            datetime.fromtimestamp(gap_end,   tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            needed,
        )

        gap_total = 0
        # Walk backwards through the gap: start at gap_end and move left
        fetch_end_ts  = gap_end + interval   # anchor: include the first present candle
        is_first_call = True

        while fetch_end_ts > gap_start:
            remaining    = max(10, (fetch_end_ts - gap_start) // interval + 10)
            chunk        = min(CHUNK_CANDLES, int(remaining))
            end_date_str = datetime.fromtimestamp(
                fetch_end_ts, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S")

            try:
                candles = fetcher.fetch_historical(
                    symbol, timeframe, limit=chunk, end_date=end_date_str
                )
            except Exception as exc:
                logging.warning(
                    "Backfill fetch failed  %s %s  end=%s: %s",
                    symbol, timeframe, end_date_str, exc,
                )
                break

            if not candles:
                break

            # Filter to only candles inside the gap range
            in_range = [c for c in candles if gap_start <= c["timestamp"] <= gap_end]
            if in_range:
                db.insert_candles(symbol, timeframe, in_range)
                gap_total += len(in_range)

            oldest_ts = min(c["timestamp"] for c in candles)
            if oldest_ts <= gap_start:
                break  # entire gap is now covered

            fetch_end_ts  = oldest_ts - interval
            is_first_call = False
            time.sleep(RATE_LIMIT_SLEEP)

        total += gap_total
        logging.info("  → inserted %d candles", gap_total)

    return total


def check_and_backfill(
    db,
    fetcher,
    symbol: str,
    timeframe: str,
    since_ts: Optional[int] = None,
    dry_run: bool = False,
) -> Tuple[int, int]:
    """
    Convenience wrapper: detect then backfill in one call.

    Returns (gaps_found, candles_inserted).
    If dry_run=True, report gaps but do not fetch or insert anything.
    """
    gaps = detect_gaps(db, symbol, timeframe, since_ts=since_ts)
    if not gaps:
        logging.debug("No gaps found for %s %s", symbol, timeframe)
        return 0, 0

    logging.info("Found %d gap(s) for %s %s", len(gaps), symbol, timeframe)

    if dry_run:
        for gs, ge in gaps:
            needed = max(1, (ge - gs) // _interval_sec(timeframe) + 1)
            logging.info(
                "  [DRY] %s → %s  (%d candles)",
                datetime.fromtimestamp(gs, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                datetime.fromtimestamp(ge, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                needed,
            )
        return len(gaps), 0

    inserted = backfill_gaps(db, fetcher, symbol, timeframe, gaps)
    return len(gaps), inserted
