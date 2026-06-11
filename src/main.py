import argparse
import logging
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler, RotatingFileHandler
from pathlib import Path
from typing import List, Optional
import json
import time

from apscheduler.schedulers.blocking import BlockingScheduler

from config.settings import Settings
from data.fx_fetcher import FXFetcher
from analysis.confluence_detector import detect_confluences, find_trigger_candle
from db.local_db import LocalDB
from alerts.alert_manager import AlertManager
from alerts.log_monitor import LogMonitor


class SizeAndAgeRotatingHandler(RotatingFileHandler):
    """Rotate the log when it hits maxBytes OR when the file is older than max_age_seconds."""
    def __init__(self, filename: str, max_age_seconds: int = 48 * 3600, **kwargs):
        super().__init__(filename, **kwargs)
        self._max_age = max_age_seconds

    def shouldRollover(self, record) -> bool:
        if super().shouldRollover(record):
            return True
        try:
            age = time.time() - os.path.getmtime(self.baseFilename)
            return age > self._max_age
        except OSError:
            return False


# Module-level debug logger — writes to logs/debug.log, does not propagate to root logger.
_dlog = logging.getLogger("trade.debug")


FETCH_CHECK_SECOND = 10
INITIAL_LOOKBACK_HOURS = 8760  # 365 days

API_DAILY_LIMIT = 800

# Fetch order: highest EV alert pairs first; USDJPY last (no live alerts, drop first under pressure)
FETCH_PRIORITY = ["NZDUSD", "EURJPY", "EURUSD", "USDCHF", "USDCAD", "XAUUSD", "USDJPY"]

# Tracks pairs whose last fetch returned stale data (0 new candles when one was expected).
# fetch_job retries only these pairs each minute until the fresh candle arrives.
# Key: (symbol, timeframe), Value: retry attempt number (1, 2, 3 …)
_stale_retry: dict = {}
_STALE_MAX_RETRIES = 4  # give up after 4 extra attempts (~4 min total from candle close)

# Pairs with 5m-early BOS detection and their active UTC session windows.
# XAUUSD runs full session; FX pairs restricted to London+early-NY to stay within budget.
_5M_PAIRS_SESSION: dict = {
    "XAUUSD": (7, 21),   # 07:00–21:00 UTC  (14h × 12 = 168 calls/day)
    "NZDUSD": (8, 15),   # 08:00–15:00 UTC  ( 7h × 12 =  84 calls/day)
    "EURUSD": (8, 15),   # 08:00–15:00 UTC  ( 7h × 12 =  84 calls/day)
}
TIMEFRAME_INTERVAL_MINUTES = {
    "5m": 5,
    "5min": 5,
    "15m": 15,
    "15min": 15,
    "30m": 30,
    "30min": 30,
    "1h": 60,
    "h1": 60,
    "60min": 60,
    "4h": 240,
    "h4": 240,
    "1d": 1440,
    "1day": 1440,
}


def should_fetch_timeframe(timeframe: str, now: datetime) -> bool:
    """
    Fetch schedule — 15m, 30m (NY window), 4h, and 5m (_5M_PAIRS_SESSION, filtered in fetch_job).
    Covers active FX trading hours; skips Sat/Sun.
    Budget is enforced separately in fetch_job via the API daily counter.

    Daily estimate on a full weekday (7 pairs):
      15m: 4/hr × 14h (07-21 UTC) × 7 = 392 calls
      4h:  6/day × 7                   =  42 calls
      30m: 2/hr × 4h (12-16 UTC) × 1  =   8 calls  (EURUSD LIQ only — fetch_job filter)
      5m:  12/hr × 14h (07-21) × 1    = 168 calls  (XAUUSD)
           12/hr × 7h  (08-15) × 2    = 168 calls  (NZDUSD + EURUSD)
      Total ≈ 778/day  (under 800 free-tier limit)
    """
    timeframe = timeframe.lower()
    minute  = now.minute
    weekday = now.weekday()  # 0=Mon … 6=Sun

    if weekday >= 5:  # Saturday / Sunday — markets closed
        return False

    if timeframe in {"15m", "15min"}:
        # Session-gated: 07:00–21:00 UTC (14h covers London + NY; trims thin Asian hours)
        return 7 <= now.hour < 21 and minute % 15 == 1
    if timeframe in {"5m", "5min"}:
        # Broadest window — per-pair session gates enforced in fetch_job via _5M_PAIRS_SESSION
        return 7 <= now.hour < 21 and minute % 5 == 1
    if timeframe in {"4h", "h4"}:  # once per 4-hour bar close
        return minute == 0 and now.hour % 4 == 0
    if timeframe in {"30m", "30min"}:  # NY session window for LIQ sweep alerts
        return 12 <= now.hour <= 15 and minute % 30 == 4

    return False  # 1h, 1d not used in production


def get_timeframes_to_fetch(timeframes: List[str], now: datetime) -> List[str]:
    return [timeframe for timeframe in timeframes if should_fetch_timeframe(timeframe, now)]


def filter_new_candles(candles: List[dict], last_timestamp: int) -> List[dict]:
    return [candle for candle in candles if candle["timestamp"] > last_timestamp]


def filter_last_hours(candles: List[dict], timeframe: str, hours: int = INITIAL_LOOKBACK_HOURS) -> List[dict]:
    interval_minutes = TIMEFRAME_INTERVAL_MINUTES.get(timeframe.lower())
    if interval_minutes is None:
        return candles
    cutoff_seconds = datetime.now(timezone.utc).timestamp() - hours * 3600
    return [candle for candle in candles if candle["timestamp"] >= cutoff_seconds]


def get_fetch_limit(timeframe: str, latest_timestamp: Optional[int]) -> int:
    interval_minutes = TIMEFRAME_INTERVAL_MINUTES.get(timeframe.lower(), 5)
    if latest_timestamp is None:
        return int(INITIAL_LOOKBACK_HOURS * 60 / interval_minutes) + 10
    return 200


def validate_candles(symbol: str, timeframe: str, candles: List[dict]) -> None:
    if not candles:
        raise ValueError("No candles returned")

    required_fields = {"timestamp", "open", "high", "low", "close"}
    previous_ts: Optional[int] = None
    for index, candle in enumerate(candles):
        missing = required_fields - candle.keys()
        if missing:
            raise ValueError(f"Candle missing fields: {missing}")

        timestamp = candle["timestamp"]
        if not isinstance(timestamp, int):
            raise ValueError("Candle timestamp must be an integer")

        if previous_ts is not None and timestamp <= previous_ts:
            raise ValueError("Candle timestamps are not strictly increasing")

        previous_ts = timestamp

    latest_ts = candles[-1]["timestamp"]
    interval_minutes = TIMEFRAME_INTERVAL_MINUTES.get(timeframe.lower())
    if interval_minutes is None:
        return

    age_seconds = datetime.now(timezone.utc).timestamp() - latest_ts
    if age_seconds > interval_minutes * 120:
        raise ValueError(
            f"Latest candle is stale ({age_seconds} seconds old) for timeframe {timeframe}"
        )


_5M_ATR_MIN_DIST = 0.2   # 5m close must be >= this × ATR5m past the broken level

def _process_symbol_5m(
    symbol: str,
    new_candles: list,
    db: LocalDB,
    alert_manager: AlertManager,
) -> None:
    """5m-early entry signal: detects 15m structural breaks ~5-15 min early.

    Works for any pair in _5M_PAIRS_SESSION. Strategy:
      1. Identify the current 15m period from the latest 5m candle.
      2. Aggregate all 5m candles in that period into a synthetic 15m candle whose
         close equals the latest 5m close.  The synthetic candle carries the same
         timestamp as the real 15m candle will have when it closes (period_end).
      3. Prepend the synthetic candle to the closed 15m context and run the same
         BOS detector + gold-param filters used for real 15m alerts.
      4. Apply the 0.2×ATR5m distance filter — the close must be meaningfully past
         the structural level (backtest: NZDUSD 0%, EURUSD 1%, XAUUSD 2% stop rate).
      5. Insert the trade monitor using the same alert_id format as the real 15m
         alert would generate (both use period_end as timestamp).  When the real
         15m candle eventually closes, insert_monitor returns False → no duplicate.
    """
    if not new_candles:
        return

    latest_5m = new_candles[-1]
    ts_5m = latest_5m["timestamp"]

    _bar_hour = datetime.fromtimestamp(ts_5m, tz=timezone.utc).hour
    start_h, end_h = _5M_PAIRS_SESSION.get(symbol, (7, 21))
    if not (start_h <= _bar_hour < end_h):
        return

    # Determine 15m period boundaries
    _PERIOD = 900
    period_start = (ts_5m // _PERIOD) * _PERIOD
    period_end   = period_start + _PERIOD   # this will be the real 15m candle's timestamp

    # All 5m candles in the current 15m period (already inserted in DB)
    period_5m = [
        c for c in db.query_candles_after(symbol, "5m", period_start - 1, limit=4)
        if c["timestamp"] < period_end
    ]
    if not period_5m:
        return

    # Build synthetic 15m candle representing the in-progress period
    synthetic = {
        "timestamp": period_end,
        "open":   period_5m[0]["open"],
        "high":   max(c["high"] for c in period_5m),
        "low":    min(c["low"]  for c in period_5m),
        "close":  period_5m[-1]["close"],
        "volume": 0,
    }

    # 15m context: closed candles + synthetic prepended (newest-first)
    candles_15m = db.query_recent(symbol, "15m", limit=200)
    candles_15m = [c for c in candles_15m if c["timestamp"] != period_end]
    context_15m = [synthetic] + candles_15m
    if len(context_15m) < 60:
        return

    # 5m ATR for the distance filter
    atr_5m_ctx = db.query_recent(symbol, "5m", limit=20)
    atr_5m = alert_manager.analyzer.calculate_atr(atr_5m_ctx) or 1.0

    # HTF bias from 4h
    candles_4h = db.query_recent(symbol, "4h", limit=500)
    htf_bias = alert_manager.analyzer.get_htf_bias(candles_4h, ts_5m) if candles_4h else None

    # Gold params (same as BOS15m)
    gold_map = db.get_gold_params("BOS15m", symbol)
    gold = gold_map.get(symbol)
    gold_params: dict = {}
    if gold:
        gold_params = db.get_param_set_by_id(gold["param_set_id"])

    # Run BOS detection — only the synthetic candle (at period_end) can trigger
    alerts = alert_manager.evaluate_production(
        symbol, "15m", context_15m,
        candles_4h=candles_4h,
        htf_bias=htf_bias,
        gold_params=gold_params,
        min_breakout_ts=period_end,
    )
    _dlog.info("[BOS5e] %s | synthetic_close=%.2f period=%s | %d alert(s)",
               symbol, synthetic["close"],
               datetime.fromtimestamp(period_end, tz=timezone.utc).strftime("%H:%M"),
               len(alerts))

    for alert in alerts:
        ev           = alert["event"]
        broken_level = ev.get("broken_level", 0)
        dist_usd     = abs(synthetic["close"] - broken_level)
        dist_atr     = dist_usd / atr_5m

        # Distance filter: 5m close must be >= 0.2×ATR5m past the level
        if dist_atr < _5M_ATR_MIN_DIST:
            _dlog.info("[BOS5e] FILTERED | %s | dist=%.2f (%.2f×ATR < %.1f×) — skip",
                       symbol, dist_usd, dist_atr, _5M_ATR_MIN_DIST)
            continue

        alert["current_price"] = latest_5m["close"]
        alert["timeframe"]    = "5m-early"   # label it clearly in the Telegram message

        # insert_monitor uses the same alert_id the real 15m will generate
        # → real 15m alert is auto-suppressed once 5m-early fires
        try:
            inserted = db.insert_monitor(
                alert_id=alert["alert_id"],
                symbol=symbol,
                direction=ev.get("direction", "bullish"),
                entry=alert["entry"],
                sl=alert["sl"],
                tp=alert["tp"],
                breakout_ts=ev.get("breakout_ts", 0),
            )
        except Exception:
            logging.exception("Failed to insert 5m-early monitor for %s", alert.get("alert_id"))
            inserted = False

        if not inserted:
            _dlog.info("[BOS5e] DEDUP | %s | id=%s already fired", symbol, alert.get("alert_id", "?"))
            continue

        text = alert_manager.format_production_alert(alert)
        _dlog.info("[BOS5e] ALERT | %s | dir=%s entry=%.2f sl=%.2f tp=%.2f "
                   "dist=%.2f (%.2fx ATR) | id=%s",
                   symbol, ev.get("direction", "?"),
                   alert.get("entry", 0), alert.get("sl", 0), alert.get("tp", 0),
                   dist_usd, dist_atr, alert.get("alert_id", "?"))
        logging.info(text)
        alert_manager.send_alert({
            "message":    text,
            "image_path": alert.get("image_path"),
            "alert_id":   alert.get("alert_id", ""),
        })


def process_symbol_timeframe(
    symbol: str,
    timeframe: str,
    fetcher: FXFetcher,
    db: LocalDB,
    alert_manager: AlertManager,
) -> int:
    """Fetch latest candles for symbol/timeframe, store, run BOS detection.
    Returns number of new candles inserted (0 = API returned stale data)."""
    latest_timestamp = db.get_latest_timestamp(symbol, timeframe)
    latest_str = (datetime.fromtimestamp(latest_timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                  if latest_timestamp else "none")
    _dlog.info("[FETCH] %s %s | latest_in_db=%s UTC | fetching...", symbol, timeframe, latest_str)
    logging.info("Fetching %s %s", symbol, timeframe)

    fetch_limit = get_fetch_limit(timeframe, latest_timestamp)
    candles = fetcher.fetch_historical(symbol, timeframe, limit=fetch_limit)
    validate_candles(symbol, timeframe, candles)

    if latest_timestamp is not None:
        new_candles = filter_new_candles(candles, latest_timestamp)
    else:
        new_candles = filter_last_hours(candles, timeframe)

    if not new_candles:
        _dlog.info("[FETCH] %s %s | NO_NEW_CANDLES (stale) | latest_in_db=%s UTC",
                   symbol, timeframe, latest_str)
        logging.info("No new candles for %s %s (stale)", symbol, timeframe)
        return 0

    db.insert_candles(symbol, timeframe, new_candles)
    new_latest = datetime.fromtimestamp(new_candles[-1]["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    _dlog.info("[FETCH] %s %s | inserted=%d new candles | new_latest=%s UTC",
               symbol, timeframe, len(new_candles), new_latest)
    logging.info("Inserted %d new candles for %s %s", len(new_candles), symbol, timeframe)

    if not alert_manager.settings.should_alert(symbol):
        _dlog.info("[FETCH] %s %s | alerts_suppressed (not in alert_pairs)", symbol, timeframe)
        logging.debug("Alerts suppressed for %s (not in alert_pairs)", symbol)
        return len(new_candles)

    if timeframe == "30m":
        process_symbol_liq(symbol, db, alert_manager)
        return len(new_candles)

    if timeframe in ("5m", "5min"):
        if symbol in _5M_PAIRS_SESSION:
            _process_symbol_5m(symbol, new_candles, db, alert_manager)
        return len(new_candles)

    if timeframe != "15m":
        return len(new_candles)

    # Session gate: BOS alerts are only reliable during London + NY (07:00–21:00 UTC).
    # Asian-session breakouts have lower liquidity and high false-break rates.
    _bar_hour = datetime.fromtimestamp(new_candles[-1]["timestamp"], tz=timezone.utc).hour
    if not (7 <= _bar_hour < 21):
        _dlog.info("[BOS] %s 15m | OUTSIDE_SESSION (hour=%d UTC) | skip", symbol, _bar_hour)
        return len(new_candles)

    # Load gold params for this symbol/strategy to filter signals
    gold_map = db.get_gold_params("BOS15m", symbol)
    gold = gold_map.get(symbol)
    gold_params: dict = {}
    if gold:
        gold_params = db.get_param_set_by_id(gold["param_set_id"])

    min_str  = gold_params.get("min_break_strength", "?")
    htf_req  = gold_params.get("htf_aligned_only", False)
    sl_mode  = gold_params.get("sl_mode", "swing")
    _dlog.info("[BOS] %s 15m | checking_signals | gold_params: str>=%s htf_only=%s sl=%s",
               symbol, min_str, htf_req, sl_mode)

    # Fetch 4h candles for HTF bias
    candles_4h = db.query_recent(symbol, "4h", limit=500)

    # Use the most-recently inserted candle's timestamp as the BOS window boundary.
    # new_candles is oldest-first (from the API); new_candles[-1] is the newest.
    newest_new_ts = new_candles[-1]["timestamp"]
    oldest_new_ts = new_candles[0]["timestamp"]

    htf_bias = None
    if candles_4h:
        htf_bias = alert_manager.analyzer.get_htf_bias(candles_4h, newest_new_ts)
    _dlog.info("[BOS] %s 15m | htf_bias=%s | new_candles=%d | 4h_candles=%d",
               symbol, htf_bias or "none", len(new_candles), len(candles_4h))

    # Pass full recent context (200 candles, newest-first) so BOS detector has
    # enough prior candles for swing detection. Filter results to events whose
    # breakout_ts falls within the newly-inserted candle range only, so we don't
    # re-fire on historical setups from prior ticks.
    context_candles = db.query_recent(symbol, timeframe, limit=200)
    _dlog.info("[BOS] %s 15m | context_candles=%d | new_window=[%s … %s]",
               symbol, len(context_candles),
               datetime.fromtimestamp(oldest_new_ts, tz=timezone.utc).strftime("%H:%M"),
               datetime.fromtimestamp(newest_new_ts, tz=timezone.utc).strftime("%H:%M"))

    alerts = alert_manager.evaluate_production(
        symbol, timeframe, context_candles, candles_4h=candles_4h,
        htf_bias=htf_bias, gold_params=gold_params,
        min_breakout_ts=oldest_new_ts,
    )
    _dlog.info("[BOS] %s 15m | evaluate_production → %d alert(s) in new window",
               symbol, len(alerts))

    current_price = new_candles[-1]["close"] if new_candles else None
    for alert in alerts:
        alert["current_price"] = current_price
        # Insert monitor first — INSERT OR IGNORE returns False if already registered.
        # This is the dedup gate: if the same BOS breakout already fired, skip re-sending.
        try:
            ev = alert["event"]
            inserted = db.insert_monitor(
                alert_id=alert["alert_id"],
                symbol=symbol,
                direction=ev.get("direction", "bullish"),
                entry=alert["entry"],
                sl=alert["sl"],
                tp=alert["tp"],
                breakout_ts=ev.get("breakout_ts", 0),
            )
        except Exception:
            logging.exception("Failed to insert trade monitor for %s", alert.get("alert_id"))
            inserted = False

        if not inserted:
            _dlog.info("[BOS] DEDUP | %s 15m | id=%s already fired — skipped",
                       symbol, alert.get("alert_id", "?"))
            continue

        text = alert_manager.format_production_alert(alert)
        _dlog.info("[BOS] ALERT | %s 15m | dir=%s entry=%.5f sl=%.5f tp=%.5f curr=%.5f | id=%s",
                   symbol, alert.get("event", {}).get("direction", "?"),
                   alert.get("entry", 0), alert.get("sl", 0), alert.get("tp", 0),
                   current_price or 0, alert.get("alert_id", "?"))
        logging.info(text)
        alert_manager.send_alert({
            "message":    text,
            "image_path": alert.get("image_path"),
            "alert_id":   alert.get("alert_id", ""),
        })

    return len(new_candles)


# ── LIQ sweep live alerts ──────────────────────────────────────────────────────

_LIQ_LIVE_PAIRS = {"EURUSD"}  # USDCAD muted until stats improve

# Optimal params: PDH/PDL + NY open + HTF aligned + sweep≥0.2 ATR + entry_0.7atr SL + RR 3.0
_LIQ_LIVE_DETECT_PARAMS = {
    "use_pdh_pdl":   True,
    "use_eq_pools":  False,
    "kill_zones_utc": [(13, 16)],  # NY open 13-15 UTC
    "min_sweep_atr": 0.0,          # filter applied manually after detection
}
_LIQ_LIVE_MIN_SWEEP_ATR = 0.2
_LIQ_LIVE_SL_ATR        = 0.7
_LIQ_LIVE_RR            = 3.0


def _format_liq_alert(symbol: str, ev: dict, entry: float, sl: float, tp: float,
                      htf_bias: str, atr: float) -> str:
    direction  = ev["direction"].upper()
    pool_type  = ev["pool_type"]
    pool_level = ev["pool_level"]
    sweep_atr  = ev.get("sweep_size_atr", 0.0)
    wick_pct   = ev.get("rejection_wick_pct", 0.0)
    risk       = abs(entry - sl)
    ts_str     = datetime.fromtimestamp(ev["breakout_ts"], tz=timezone.utc).strftime("%H:%M %d-%b")
    return (
        f"LIQ SWEEP {direction} {symbol} @ {entry:.5f}"
        f" | {pool_type}={pool_level:.5f}"
        f" | SL:{sl:.5f} (entry±0.7ATR)"
        f" | TP:{tp:.5f} (3R={risk * _LIQ_LIVE_RR:.5f})"
        f" | sweep={sweep_atr:.2f}ATR  wick={wick_pct:.0%}"
        f" | 4H:{(htf_bias or '?').upper()}"
        f" | {ts_str} UTC"
    )


def process_symbol_liq(
    symbol: str,
    db: "LocalDB",
    alert_manager: "AlertManager",
) -> None:
    """Check for live LIQ sweep alerts on 30m after a new candle is inserted."""
    if symbol not in _LIQ_LIVE_PAIRS:
        _dlog.debug("[LIQ] %s 30m | not in live_pairs (%s) | skip", symbol, _LIQ_LIVE_PAIRS)
        return
    if not alert_manager.settings.should_alert(symbol):
        _dlog.debug("[LIQ] %s 30m | alerts_suppressed | skip", symbol)
        return

    now_hour = datetime.now(timezone.utc).hour
    in_ny = now_hour in (13, 14, 15)
    _dlog.info("[LIQ] %s 30m | hour=%d UTC | in_ny_window=%s", symbol, now_hour, in_ny)
    if not in_ny:
        return

    recent = db.query_recent(symbol, "30m", limit=100)
    if not recent or len(recent) < 30:
        _dlog.warning("[LIQ] %s 30m | insufficient candles (%d) | skip", symbol, len(recent) if recent else 0)
        return

    latest_str = datetime.fromtimestamp(recent[0]["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    _dlog.info("[LIQ] %s 30m | loaded %d candles | latest=%s UTC", symbol, len(recent), latest_str)

    candles_4h = db.query_recent(symbol, "4h", limit=500)
    htf_bias = None
    if candles_4h:
        htf_bias = alert_manager.analyzer.get_htf_bias(candles_4h, recent[0]["timestamp"])
    _dlog.info("[LIQ] %s 30m | htf_bias=%s", symbol, htf_bias or "none")

    det_slice = recent[:50]
    _dlog.info("[LIQ] %s 30m | detect_liquidity_sweep on %d candles | params: PDH/PDL kill_zone=%s min_sweep_atr=%.1f",
               symbol, len(det_slice), _LIQ_LIVE_DETECT_PARAMS.get("kill_zones_utc"), _LIQ_LIVE_MIN_SWEEP_ATR)

    liq_events = alert_manager.analyzer.detect_liquidity_sweep(
        det_slice, params={**_LIQ_LIVE_DETECT_PARAMS, "symbol": symbol, "timeframe": "30m"}
    )
    _dlog.info("[LIQ] %s 30m | raw_events=%d", symbol, len(liq_events) if liq_events else 0)

    if not liq_events:
        _dlog.info("[LIQ] %s 30m | no_sweep_detected", symbol)
        return

    atr = alert_manager.analyzer.calculate_atr(det_slice) or 1e-6

    for ev in liq_events:
        pool_type   = ev["pool_type"]
        direction   = ev["direction"]
        sweep_atr   = ev.get("sweep_size_atr", 0.0)
        wick_pct    = ev.get("rejection_wick_pct", 0.0)

        pool_ok   = pool_type in ("PDH", "PDL")
        sweep_ok  = sweep_atr >= _LIQ_LIVE_MIN_SWEEP_ATR
        htf_ok    = htf_bias == direction

        _dlog.info("[LIQ] %s 30m | event pool=%s dir=%s sweep=%.2fATR wick=%.0f%% | "
                   "pool_ok=%s sweep_ok=%s htf_ok=%s",
                   symbol, pool_type, direction, sweep_atr, wick_pct * 100,
                   pool_ok, sweep_ok, htf_ok)

        if not pool_ok:
            _dlog.info("[LIQ] %s 30m | FILTERED pool_type=%s not PDH/PDL", symbol, pool_type)
            continue
        if not sweep_ok:
            _dlog.info("[LIQ] %s 30m | FILTERED sweep=%.2fATR < %.1f min", symbol, sweep_atr, _LIQ_LIVE_MIN_SWEEP_ATR)
            continue
        if not htf_ok:
            _dlog.info("[LIQ] %s 30m | FILTERED htf_misaligned bias=%s dir=%s", symbol, htf_bias, direction)
            continue

        bullish = direction == "bullish"
        entry   = det_slice[0]["close"]
        sl      = (entry - _LIQ_LIVE_SL_ATR * atr) if bullish else (entry + _LIQ_LIVE_SL_ATR * atr)
        risk    = abs(entry - sl) or atr * 0.5
        tp      = (entry + _LIQ_LIVE_RR * risk) if bullish else (entry - _LIQ_LIVE_RR * risk)

        alert_id = _liq_alert_id(symbol, ev["breakout_ts"], ev["pool_type"])
        message  = _format_liq_alert(symbol, ev, entry, sl, tp, htf_bias, atr)

        # Dedup: insert_alert raises on duplicate alert_id (UNIQUE constraint)
        try:
            db.insert_alert(symbol, "30m", ev["breakout_ts"], "LIQ", message,
                            None, "{}", alert_id=alert_id)
        except Exception:
            _dlog.info("[LIQ] DEDUP | %s 30m | id=%s already_sent suppressed", symbol, alert_id)
            logging.debug("LIQ duplicate suppressed: %s", alert_id)
            continue

        _dlog.info("[LIQ] ALERT | %s 30m | %s dir=%s entry=%.5f sl=%.5f tp=%.5f | id=%s",
                   symbol, pool_type, direction, entry, sl, tp, alert_id)
        logging.info("LIQ ALERT: %s", message)
        alert_manager.send_alert(message)

        try:
            db.insert_monitor(
                alert_id=alert_id,
                symbol=symbol,
                direction=ev["direction"],
                entry=entry,
                sl=sl,
                tp=tp,
                breakout_ts=ev["breakout_ts"],
            )
        except Exception:
            logging.exception("Failed to insert LIQ monitor for %s", alert_id)


def momentum_monitor_job(db: LocalDB, alert_manager: AlertManager) -> None:
    """Check open trade monitors every 15 minutes.

    - candle 4+ with <50% progress toward TP → send stall warning (once per trade)
    - SL hit → send 🔴 notification and close monitor
    - TP hit → send ✅ notification and close monitor
    - >50 candles old → expire silently
    """
    monitors = db.get_open_monitors()
    _dlog.info("[MONITOR] START | open_monitors=%d", len(monitors))
    if not monitors:
        return

    closed = 0
    for m in monitors:
        symbol    = m["symbol"]
        direction = m["direction"]
        entry     = m["entry"]
        sl        = m["sl"]
        tp        = m["tp"]
        b_ts      = m["breakout_ts"]
        bullish   = direction == "bullish"

        candles = db.query_candles_after(symbol, "15m", b_ts, limit=60)
        if not candles:
            _dlog.info("[MONITOR] %s %s | no_candles_after_entry yet | id=%s", symbol, direction, m["alert_id"])
            continue

        n = len(candles)
        _dlog.info("[MONITOR] %s %s | entry=%.5f sl=%.5f tp=%.5f | candles_since=%d | id=%s",
                   symbol, direction, entry, sl, tp, n, m["alert_id"])

        # expire stale monitors
        if n > 50:
            _dlog.info("[MONITOR] EXPIRE | %s %s | %d candles no_resolution | id=%s",
                       symbol, direction, n, m["alert_id"])
            db.update_monitor(m["alert_id"], status="expired")
            closed += 1
            continue

        # check SL/TP
        sl_hit = any(c["low"] <= sl for c in candles) if bullish else any(c["high"] >= sl for c in candles)
        tp_hit = any(c["high"] >= tp for c in candles) if bullish else any(c["low"] <= tp for c in candles)

        # compute current progress for log
        if bullish:
            best_price = max(c["high"] for c in candles)
            progress = (best_price - entry) / (tp - entry) if (tp - entry) > 0 else 1.0
        else:
            best_price = min(c["low"] for c in candles)
            progress = (entry - best_price) / (entry - tp) if (entry - tp) > 0 else 1.0
        _dlog.info("[MONITOR] %s %s | best_price=%.5f progress=%.0f%% toward_tp",
                   symbol, direction, best_price, progress * 100)

        if tp_hit and not m["notified_close"]:
            _dlog.info("[MONITOR] TP_HIT | %s %s | entry=%.5f tp=%.5f | id=%s",
                       symbol, direction, entry, tp, m["alert_id"])
            alert_manager.send_alert(
                f"✅ TP HIT: {symbol} {direction.upper()} | entry {entry:.5f} → TP {tp:.5f} | {m['alert_id']}"
            )
            db.update_monitor(m["alert_id"], status="tp_hit", notified_close=1)
            closed += 1
            continue

        if sl_hit and not m["notified_close"]:
            _dlog.info("[MONITOR] SL_HIT | %s %s | entry=%.5f sl=%.5f | id=%s",
                       symbol, direction, entry, sl, m["alert_id"])
            db.update_monitor(m["alert_id"], status="sl_hit", notified_close=1)
            closed += 1
            continue

        # stall check: after 4 candles, progress < 50% of TP distance
        if n >= 4 and not m["notified_stall"]:
            first4 = candles[:4]
            if bullish:
                best      = max(c["high"] for c in first4)
                progress  = (best - entry) / (tp - entry) if (tp - entry) > 0 else 1.0
            else:
                best      = min(c["low"] for c in first4)
                progress  = (entry - best) / (entry - tp) if (entry - tp) > 0 else 1.0

            if progress < 0.5:
                _dlog.info("[MONITOR] STALL | %s %s | progress=%.0f%% after_4_candles | id=%s",
                           symbol, direction, progress * 100, m["alert_id"])
                alert_manager.send_alert(
                    f"⚠️ Momentum stalling: {symbol} {direction.upper()} — "
                    f"{progress:.0%} toward TP after 4 candles | {m['alert_id']}"
                )
                db.update_monitor(m["alert_id"], notified_stall=1)

        # reversal check: price has closed back past entry — trade invalidated
        if not m.get("notified_reversal"):
            last_close = candles[-1]["close"]
            reversed_past_entry = (last_close < entry) if bullish else (last_close > entry)
            if reversed_past_entry:
                action = "BUY" if bullish else "SELL"
                _dlog.info("[MONITOR] REVERSAL | %s %s | entry=%.5f last_close=%.5f | id=%s",
                           symbol, direction, entry, last_close, m["alert_id"])
                alert_manager.send_alert(
                    f"🚨 Early failure: {symbol} {action} reversed past entry "
                    f"({last_close:.5f} vs entry {entry:.5f}) — consider closing | {m['alert_id']}"
                )
                db.update_monitor(m["alert_id"], notified_reversal=1)

    _dlog.info("[MONITOR] END | closed=%d remaining=%d", closed, len(monitors) - closed)


def fetch_job(settings: Settings, fetcher: FXFetcher, db: LocalDB, alert_manager: AlertManager) -> None:
    global _stale_retry
    now = datetime.now(timezone.utc)
    timeframes = get_timeframes_to_fetch(settings.timeframes, now)

    # Ordered pair list (priority order)
    configured = set(settings.fx_pairs)
    ordered = [p for p in FETCH_PRIORITY if p in configured]
    ordered += [p for p in settings.fx_pairs if p not in set(FETCH_PRIORITY)]

    # Decide what to fetch this minute
    if timeframes:
        # Scheduled tick: fetch all pairs + reset stale tracking for these timeframes.
        # 5m: only pairs in _5M_PAIRS_SESSION within their session window (budget control).
        # 30m: only _LIQ_LIVE_PAIRS — others have no LIQ sweep alert consumer (saves 48 calls/day).
        pairs_to_fetch = [
            (sym, tf) for sym in ordered for tf in timeframes
            if not (tf in ("5m", "5min") and (
                sym not in _5M_PAIRS_SESSION or
                not (_5M_PAIRS_SESSION[sym][0] <= now.hour < _5M_PAIRS_SESSION[sym][1])
            ))
            and not (tf in ("30m", "30min") and sym not in _LIQ_LIVE_PAIRS)
        ]
        for sym, tf in pairs_to_fetch:
            _stale_retry.pop((sym, tf), None)
        mode = "SCHEDULED"
    elif _stale_retry:
        # Non-scheduled tick: retry only stale pairs (zero extra API cost for fresh pairs)
        pairs_to_fetch = list(_stale_retry.keys())
        mode = "STALE_RETRY"
    else:
        _dlog.debug("[FETCH] SKIP | %s | no timeframes scheduled and no stale pairs",
                    now.strftime("%H:%M:%S"))
        return

    calls_today = db.get_api_calls_today()
    if calls_today >= API_DAILY_LIMIT:
        _dlog.warning("[FETCH] BUDGET_EXHAUSTED | api_used=%d/%d | skipping",
                      calls_today, API_DAILY_LIMIT)
        return

    if mode == "STALE_RETRY":
        _dlog.info("[FETCH] STALE_RETRY | %s | retrying %d stale pair(s): %s",
                   now.strftime("%H:%M:%S"), len(pairs_to_fetch),
                   ", ".join(f"{s} {tf}" for s, tf in pairs_to_fetch))
    else:
        _dlog.info("[FETCH] START | timeframes=%s | api_used=%d/%d | pairs=%s",
                   ",".join(timeframes), calls_today, API_DAILY_LIMIT, ",".join(ordered))
        logging.info("Fetch: %s | used %d/%d API calls today",
                     ", ".join(timeframes), calls_today, API_DAILY_LIMIT)

    for symbol, timeframe in pairs_to_fetch:
        remaining = API_DAILY_LIMIT - db.get_api_calls_today()
        if remaining <= 0:
            if not db.api_limit_already_alerted():
                alert_manager.send_alert(
                    f"⚠️ [trade] Daily API limit reached ({API_DAILY_LIMIT} calls). "
                    f"Data fetch paused until UTC midnight."
                )
                db.mark_api_limit_alerted()
            _dlog.warning("[FETCH] BUDGET_HIT | api_used=%d/%d | stopping", db.get_api_calls_today(), API_DAILY_LIMIT)
            break

        try:
            n_new = process_symbol_timeframe(symbol, timeframe, fetcher, db, alert_manager)
            db.increment_api_calls(1)
            if n_new == 0:
                attempt = _stale_retry.get((symbol, timeframe), 0) + 1
                if attempt <= _STALE_MAX_RETRIES:
                    _stale_retry[(symbol, timeframe)] = attempt
                    _dlog.info("[FETCH] STALE | %s %s | will retry (attempt %d/%d)",
                               symbol, timeframe, attempt, _STALE_MAX_RETRIES)
                else:
                    _stale_retry.pop((symbol, timeframe), None)
                    _dlog.warning("[FETCH] STALE_GAVE_UP | %s %s | no new candle after %d retries",
                                  symbol, timeframe, _STALE_MAX_RETRIES)
            else:
                _stale_retry.pop((symbol, timeframe), None)
            time.sleep(8)
        except Exception as exc:
            _dlog.error("[FETCH] ERROR | %s %s | %s", symbol, timeframe, exc)
            logging.exception("Failed to fetch %s %s: %s", symbol, timeframe, exc)
            alert_manager.send_fetch_error(symbol, timeframe, str(exc))
            _stale_retry.pop((symbol, timeframe), None)

    _dlog.info("[FETCH] END | mode=%s | api_used_now=%d/%d | stale_pending=%d",
               mode, db.get_api_calls_today(), API_DAILY_LIMIT, len(_stale_retry))


def _ensure_pair_data(symbol: str, timeframe: str, fetcher: "FXFetcher", db: LocalDB, target_days: int = 365) -> None:
    """Fetch up to target_days of historical data for a pair/timeframe, paginating if needed."""
    target_cutoff = datetime.now(timezone.utc).timestamp() - target_days * 24 * 3600
    earliest = db.get_earliest_timestamp(symbol, timeframe)
    if earliest is not None and earliest <= target_cutoff:
        return  # already have full history

    interval_minutes = TIMEFRAME_INTERVAL_MINUTES.get(timeframe.lower(), 15)
    candles_per_page = 5000
    total_stored = 0

    # Paginate backwards: fetch from the earliest we have (or now), going back in time
    end_dt = None
    if earliest is not None:
        # start fetching before what we already have
        end_dt = datetime.fromtimestamp(earliest - interval_minutes * 60, tz=timezone.utc)
    else:
        end_dt = datetime.now(timezone.utc)

    logging.info("Fetching %d-day history for %s %s (paginating)...", target_days, symbol, timeframe)
    try:
        while end_dt.timestamp() > target_cutoff:
            end_date_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")
            candles = fetcher.fetch_historical(symbol, timeframe, limit=candles_per_page, end_date=end_date_str)
            if not candles:
                break
            db.insert_candles(symbol, timeframe, candles)
            total_stored += len(candles)
            oldest_ts = candles[0]["timestamp"]
            if oldest_ts <= target_cutoff:
                break  # reached the target start date
            # move end_date to just before the oldest candle we got
            end_dt = datetime.fromtimestamp(oldest_ts - interval_minutes * 60, tz=timezone.utc)
            time.sleep(8)  # stay within Twelve Data rate limit (8 calls/min)
        logging.info("  → stored %d candles for %s %s", total_stored, symbol, timeframe)
    except Exception as exc:
        logging.warning("  → failed to fetch %s %s: %s", symbol, timeframe, exc)


def _candle_limit(timeframe: str, days: int = 365) -> int:
    """How many candles cover `days` of data for a given timeframe."""
    interval = TIMEFRAME_INTERVAL_MINUTES.get(timeframe.lower(), 15)
    return int(days * 24 * 60 / interval) + 200


def _hour_to_session(hour: int) -> str:
    if 7 <= hour < 12:  return "london"
    if 12 <= hour < 17: return "ny"
    return "other"


def _scan_one_symbol(args: tuple) -> tuple:
    """
    Worker function executed in a subprocess.
    Detects ALL BOS events (no break_strength filter) and stores raw signals.
    Renders charts only for events passing active_params.
    All DB writes are deferred to the main process.
    """
    symbol, settings, param_set_id, cutoff_ts, timeframe, htf, active_params, scan_run_id, strategy, scan_days = args

    from db.local_db import LocalDB
    from alerts.alert_manager import AlertManager
    from analysis.confluence_detector import detect_confluences, find_trigger_candle

    db = LocalDB(settings.db_path)
    alert_manager = AlertManager(settings, db=db)
    dev_mode = settings.dev_mode

    candles_desc = db.query_recent(symbol, timeframe, limit=_candle_limit(timeframe, days=scan_days))
    if not candles_desc:
        db.close()
        return symbol, 0, [], [], [], []

    candles_4h = db.query_recent(symbol, htf, limit=_candle_limit(htf, days=scan_days))
    gold_map = db.get_gold_params(strategy)
    gold_wr  = gold_map.get(symbol, {}).get("win_rate")
    n = len(candles_desc)
    count = 0
    seen_raw: set = set()      # dedup for raw_signal storage
    seen_active: set = set()   # dedup for active_params counting / chart rendering
    outcome_cache: dict = {}   # key → (outcome_swing, outcome_bl, outcome_bc)
    trade_results = []
    alert_records = []
    log_lines = []
    raw_signal_records = []

    active_min_str = active_params.get("min_break_strength", 0.7)
    active_req_brt = active_params.get("require_brt_confluence", True)

    for k in range(50, n):
        bos_ts = candles_desc[n - 1 - k]["timestamp"]
        if bos_ts < cutoff_ts:
            continue

        window = candles_desc[n - 1 - k:]
        # Always compute lookahead for confluence detection and outcome evaluation.
        # Chart renderer only gets it in dev_mode to hide future candles from the chart.
        lookahead = candles_desc[max(0, n - k - 51) : n - 1 - k]

        # Detect with no break_strength filter to capture all potential signals
        bos_events = alert_manager.analyzer.detect_bos(
            window, params={
                "symbol": symbol, "timeframe": timeframe,
                "min_break_strength": 0.0,
                "require_liquidity_sweep": False,
            }
        )
        if not bos_events:
            continue

        htf_bias = alert_manager.analyzer.get_htf_bias(candles_4h, bos_ts) if candles_4h else None
        pre_bos_chron = list(reversed(window))
        lookahead_chron = list(reversed(lookahead))
        atr = alert_manager.analyzer.calculate_atr(window) or 0.0

        for ev in bos_events:
            key = (ev["direction"], round(ev["broken_level"], 5))

            # Always apply false-break filter
            if lookahead_chron:
                broken = ev.get("broken_level", 0.0)
                bullish_bos = ev["direction"] == "bullish"
                if any(
                    (bullish_bos and c["close"] < broken) or
                    (not bullish_bos and c["close"] > broken)
                    for c in lookahead_chron[:5]
                ):
                    continue

            confluences = detect_confluences(ev, lookahead_chron, pre_bos_chron, candles_4h, atr)
            if not confluences:
                continue

            trigger_idx = find_trigger_candle(ev, lookahead_chron, pre_bos_chron, atr)
            trigger_ts_val = lookahead_chron[trigger_idx]["timestamp"] if trigger_idx is not None else None
            bos_dt = datetime.fromtimestamp(ev["breakout_ts"], tz=timezone.utc)

            # Store raw signal (first occurrence per level across the full scan)
            if key not in seen_raw:
                seen_raw.add(key)
                outcome_swing, eff_r_swing = AlertManager.evaluate_bos_outcome(
                    window, ev, lookahead_chron, candles_4h, trigger_ts_val, sl_mode="swing"
                )
                outcome_bl, eff_r_bl = AlertManager.evaluate_bos_outcome(
                    window, ev, lookahead_chron, candles_4h, trigger_ts_val, sl_mode="broken_level"
                )
                outcome_bc, eff_r_bc = AlertManager.evaluate_bos_outcome(
                    window, ev, lookahead_chron, candles_4h, trigger_ts_val, sl_mode="break_candle"
                )
                outcome_cache[key] = (outcome_swing, outcome_bl, outcome_bc)
                raw_signal_records.append({
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "breakout_ts": ev["breakout_ts"],
                    "alert_id": AlertManager._generate_alert_id(symbol, ev["breakout_ts"], prefix=strategy.lower()),
                    "direction": ev["direction"],
                    "broken_level": ev["broken_level"],
                    "break_strength": ev.get("break_strength", 0.0),
                    "htf_bias": htf_bias,
                    "confluences": json.dumps(confluences),
                    "outcome": outcome_swing,
                    "hour": bos_dt.hour,
                    "month": bos_dt.strftime("%Y-%m"),
                    "has_liquidity_sweep": 1 if ev.get("liquidity_sweep") else 0,
                    "swing_age_candles": ev.get("swing_age_candles"),
                    "swing_test_count": ev.get("swing_test_count"),
                    "break_body_pct": ev.get("break_body_pct"),
                    "session": _hour_to_session(bos_dt.hour),
                    "dow": bos_dt.weekday(),
                    "strategy": strategy,
                    "scan_run_id": scan_run_id,
                    "outcome_broken_level": outcome_bl,
                    "outcome_break_candle": outcome_bc,
                    "eff_r_broken_level": eff_r_bl,
                    "eff_r_break_candle": eff_r_bc,
                })

            # Apply active_params filter for chart rendering (separate dedup)
            if key in seen_active:
                continue
            if ev.get("break_strength", 0.0) < active_min_str:
                continue
            if active_req_brt and "BRT" not in confluences:
                continue

            seen_active.add(key)
            count += 1

            # Build trade result from cached outcomes — avoids expensive chart I/O for historical signals.
            active_sl_mode = active_params.get("sl_mode", "swing")
            cached = outcome_cache.get(key, ("OPEN", "OPEN", "OPEN"))
            active_outcome = {
                "swing": cached[0], "broken_level": cached[1], "break_candle": cached[2],
            }.get(active_sl_mode, cached[0])
            alert_id = AlertManager._generate_alert_id(symbol, ev["breakout_ts"], prefix=strategy.lower())

            trade_results.append({
                "alert_id": alert_id,
                "outcome": active_outcome,
                "confluences": confluences,
                "symbol": symbol,
                "hour": bos_dt.hour,
                "month": bos_dt.strftime("%Y-%m"),
                "break_strength": ev.get("break_strength", 0.0),
                "htf_bias": htf_bias,
                "bos_direction": ev["direction"],
            })

            # Render chart only for recent signals (last 7 days) — skip for bulk historical scan
            if ev["breakout_ts"] >= time.time() - 7 * 86400:
                try:
                    alert = alert_manager.render_alert(
                        symbol, timeframe, ev, window, lookahead if dev_mode else None, htf_bias, confluences,
                        trigger_ts=trigger_ts_val, candles_4h=candles_4h,
                        param_set_id=param_set_id, skip_db=True,
                        strategy=strategy.lower(), gold_wr=gold_wr,
                    )
                    alert_records.append({
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "ts": ev.get("breakout_ts", 0),
                        "message": alert["message"],
                        "image_path": alert.get("image_path"),
                        "params_used": json.dumps(ev.get("params_used", {})),
                        "alert_id": alert["alert_id"],
                        "param_set_id": param_set_id,
                    })
                except Exception as exc:
                    log_lines.append(f"  [{symbol}] Chart render failed for {alert_id}: {exc}")

            sweep = ev.get("liquidity_sweep") or {}
            sweep_str = ("  sweep@" + datetime.fromtimestamp(sweep["timestamp"], tz=timezone.utc).strftime("%H:%M")) if sweep else ""
            log_lines.append(
                f"  [{symbol}] {ev['direction'].upper()} BOS  id={alert_id}"
                f"  level={ev['broken_level']:.5f}  str={ev['break_strength']:.2f}"
                f"  4H={( htf_bias or '?').upper():<8}  ts={bos_dt.strftime('%Y-%m-%d %H:%M')}{sweep_str}"
                f"  conf={','.join(confluences)}"
            )

    db.close()
    return symbol, count, trade_results, alert_records, log_lines, raw_signal_records


_SWEEP_BASE = {
    "min_break_distance_atr_mult": 0.3,
    "min_atr_pct": 0.0003,
    "min_swing_age_candles": 5,
    "swing_lookback": 20,
    "lookahead_candles": 50,
    "scan_days": 365,
    "min_conf_count": 1,
    "htf_aligned_only": False,
    "session": "all",
    "exclude_pairs": [],
    "sl_mode": "swing",
    # item-90 loss-reduction filters
    "max_confluences": None,   # None = no cap; 2 or 3 to limit crowded setups
    "exclude_weekend": False,  # True = skip Sat/Sun signals
    "dead_hours_utc": [],      # e.g. [18,19,20,21] to skip late-NY wind-down
    "max_break_strength": None, # None = no cap; 2.0 to exclude exhaustion candles
    "exclude_london_open": False, # True = skip 08:00-08:15 UTC (London open first 2 bars)
    "exclude_ny_open": False,     # True = skip 13:30-13:45 UTC (NY open first 2 bars)
}

PARAM_SWEEP_SETS = [
    # ── Tier 1: base / reference ──────────────────────────────────────────────
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": True,  "require_liquidity_sweep": False},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": True,  "require_liquidity_sweep": True},
    # ── Tier 2: break-strength variants ──────────────────────────────────────
    {**_SWEEP_BASE, "min_break_strength": 0.5,  "require_brt_confluence": False, "require_liquidity_sweep": False},
    {**_SWEEP_BASE, "min_break_strength": 1.0,  "require_brt_confluence": False, "require_liquidity_sweep": False},
    {**_SWEEP_BASE, "min_break_strength": 1.5,  "require_brt_confluence": False, "require_liquidity_sweep": False},
    {**_SWEEP_BASE, "min_break_strength": 2.0,  "require_brt_confluence": False, "require_liquidity_sweep": False},
    # ── Tier 3: HTF alignment filter ─────────────────────────────────────────
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "htf_aligned_only": True},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": True,  "require_liquidity_sweep": False, "htf_aligned_only": True},
    {**_SWEEP_BASE, "min_break_strength": 1.0,  "require_brt_confluence": False, "require_liquidity_sweep": False, "htf_aligned_only": True},
    # ── Tier 4: session filter ────────────────────────────────────────────────
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "session": "active"},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "session": "london"},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "session": "ny"},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "htf_aligned_only": True, "session": "active"},
    # ── Tier 5: pair exclusion ────────────────────────────────────────────────
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "exclude_pairs": ["GBPJPY"]},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "exclude_pairs": ["GBPJPY", "EURGBP"]},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "htf_aligned_only": True, "exclude_pairs": ["GBPJPY"]},
    # ── Tier 6: swing age ────────────────────────────────────────────────────
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "min_swing_age_candles": 10},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "min_swing_age_candles": 20},
    # ── Tier 7: confluence count ─────────────────────────────────────────────
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "min_conf_count": 2},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "min_conf_count": 3},
    # ── Tier 8: swing test count (level tested before break) ─────────────────
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "min_swing_test_count": 1},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "min_swing_test_count": 2},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "min_swing_test_count": 3},
    # ── Tier 9: break candle body quality ────────────────────────────────────
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "min_break_body_pct": 0.4},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "min_break_body_pct": 0.6},
    # ── Tier 10: combined signal-quality combos ───────────────────────────────
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "htf_aligned_only": True, "session": "active"},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "htf_aligned_only": True, "min_swing_test_count": 1},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "htf_aligned_only": True, "min_break_body_pct": 0.4},
    {**_SWEEP_BASE, "min_break_strength": 1.0,  "require_brt_confluence": False, "require_liquidity_sweep": False, "htf_aligned_only": True, "min_swing_test_count": 1},
    {**_SWEEP_BASE, "min_break_strength": 1.0,  "require_brt_confluence": False, "require_liquidity_sweep": False, "htf_aligned_only": True, "min_break_body_pct": 0.4},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "htf_aligned_only": True, "session": "active", "exclude_pairs": ["GBPJPY"]},
    {**_SWEEP_BASE, "min_break_strength": 1.0,  "require_brt_confluence": False, "require_liquidity_sweep": False, "htf_aligned_only": True, "session": "active"},
    # ── Tier 11: SL mode comparison (same signal quality, vary exit logic) ────
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "sl_mode": "broken_level"},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "sl_mode": "break_candle"},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "htf_aligned_only": True, "sl_mode": "broken_level"},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "htf_aligned_only": True, "sl_mode": "break_candle"},
    {**_SWEEP_BASE, "min_break_strength": 1.0,  "require_brt_confluence": False, "require_liquidity_sweep": False, "htf_aligned_only": True, "sl_mode": "broken_level"},
    {**_SWEEP_BASE, "min_break_strength": 1.0,  "require_brt_confluence": False, "require_liquidity_sweep": False, "htf_aligned_only": True, "sl_mode": "break_candle"},
    # ── Tier 12: liquidity sweep required — used for XAU/USD gold selection ──────
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": True},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": True, "htf_aligned_only": True},
    {**_SWEEP_BASE, "min_break_strength": 1.0,  "require_brt_confluence": False, "require_liquidity_sweep": True},
    {**_SWEEP_BASE, "min_break_strength": 1.0,  "require_brt_confluence": False, "require_liquidity_sweep": True, "htf_aligned_only": True},
    {**_SWEEP_BASE, "min_break_strength": 1.5,  "require_brt_confluence": False, "require_liquidity_sweep": True},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": True, "sl_mode": "broken_level"},
    {**_SWEEP_BASE, "min_break_strength": 1.0,  "require_brt_confluence": False, "require_liquidity_sweep": True, "sl_mode": "broken_level"},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": True, "htf_aligned_only": True, "sl_mode": "broken_level"},
    # ── Tier 13: max-confluence cap (item 90 — counterintuitive: more confs → lower WR) ─
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "max_confluences": 3},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "max_confluences": 2},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "htf_aligned_only": True, "max_confluences": 3},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "htf_aligned_only": True, "max_confluences": 2},
    # ── Tier 14: weekend + dead-hours exclusion ───────────────────────────────
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "exclude_weekend": True},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "dead_hours_utc": [18, 19, 20, 21]},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "exclude_weekend": True, "dead_hours_utc": [18, 19, 20, 21]},
    # ── Tier 15: all loss-reduction filters combined (FX) ────────────────────
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "htf_aligned_only": True, "max_confluences": 3, "exclude_weekend": True, "dead_hours_utc": [18, 19, 20, 21], "max_break_strength": 2.0},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "htf_aligned_only": True, "max_confluences": 3, "exclude_weekend": True, "dead_hours_utc": [18, 19, 20, 21], "max_break_strength": 2.0, "sl_mode": "broken_level"},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "htf_aligned_only": True, "max_confluences": 2, "exclude_weekend": True, "dead_hours_utc": [18, 19, 20, 21], "max_break_strength": 2.0},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "htf_aligned_only": True, "max_confluences": 2, "exclude_weekend": True, "dead_hours_utc": [18, 19, 20, 21], "max_break_strength": 2.0, "sl_mode": "broken_level"},
    # ── Tier 16: all loss-reduction filters combined + sweep-required (XAUUSD) ─
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": True,  "htf_aligned_only": True, "max_confluences": 3, "exclude_weekend": True, "dead_hours_utc": [18, 19, 20, 21], "max_break_strength": 2.0, "sl_mode": "broken_level"},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": True,  "htf_aligned_only": True, "max_confluences": 2, "exclude_weekend": True, "dead_hours_utc": [18, 19, 20, 21], "max_break_strength": 2.0, "sl_mode": "broken_level"},
    # ── Tier 17: open-window exclusion (London 08:00-08:15 / NY 13:30-13:45) ──
    # Standalone open filters
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "exclude_london_open": True},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "exclude_ny_open": True},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "exclude_london_open": True, "exclude_ny_open": True},
    # Combined with full gold stack (htf+mx2+nowe+nodh+mxs2+bl)
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "htf_aligned_only": True, "max_confluences": 2, "exclude_weekend": True, "dead_hours_utc": [18, 19, 20, 21], "max_break_strength": 2.0, "sl_mode": "broken_level", "exclude_london_open": True},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "htf_aligned_only": True, "max_confluences": 2, "exclude_weekend": True, "dead_hours_utc": [18, 19, 20, 21], "max_break_strength": 2.0, "sl_mode": "broken_level", "exclude_ny_open": True},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": False, "htf_aligned_only": True, "max_confluences": 2, "exclude_weekend": True, "dead_hours_utc": [18, 19, 20, 21], "max_break_strength": 2.0, "sl_mode": "broken_level", "exclude_london_open": True, "exclude_ny_open": True},
    # With sweep required (XAUUSD)
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": True,  "htf_aligned_only": True, "max_confluences": 2, "exclude_weekend": True, "dead_hours_utc": [18, 19, 20, 21], "max_break_strength": 2.0, "sl_mode": "broken_level", "exclude_london_open": True},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": True,  "htf_aligned_only": True, "max_confluences": 2, "exclude_weekend": True, "dead_hours_utc": [18, 19, 20, 21], "max_break_strength": 2.0, "sl_mode": "broken_level", "exclude_ny_open": True},
    {**_SWEEP_BASE, "min_break_strength": 0.7,  "require_brt_confluence": False, "require_liquidity_sweep": True,  "htf_aligned_only": True, "max_confluences": 2, "exclude_weekend": True, "dead_hours_utc": [18, 19, 20, 21], "max_break_strength": 2.0, "sl_mode": "broken_level", "exclude_london_open": True, "exclude_ny_open": True},
]

# Symbols for which gold params must come from sweep-required param sets only.
SWEEP_REQUIRED_SYMBOLS: frozenset = frozenset({"XAUUSD"})


# ── 4h-specific parameter sweep ───────────────────────────────────────────────
# 4h candles span 4 hours each; swing_lookback=10 means 40h of context for swing
# detection (vs 15m where 10 candles = 2.5h). SL mode is the primary variable
# to test since 4h swing SLs are very wide and tight exits can dramatically lift R.
_SWEEP_BASE_4H = {
    "min_break_distance_atr_mult": 0.3,
    "min_atr_pct": 0.0003,
    "min_swing_age_candles": 3,
    "swing_lookback": 10,
    "lookahead_candles": 50,
    "require_brt_confluence": False,
    "require_liquidity_sweep": False,
    "scan_days": 730,
    "min_conf_count": 1,
    "htf_aligned_only": False,
    "session": "all",
    "exclude_pairs": [],
    "sl_mode": "swing",
}

PARAM_SWEEP_SETS_4H = [
    # ── Tier 1: swing lookback variants (baseline SL) ────────────────────────
    {**_SWEEP_BASE_4H, "min_break_strength": 0.7,  "swing_lookback": 5},
    {**_SWEEP_BASE_4H, "min_break_strength": 0.7,  "swing_lookback": 10},
    {**_SWEEP_BASE_4H, "min_break_strength": 0.7,  "swing_lookback": 15},
    {**_SWEEP_BASE_4H, "min_break_strength": 0.7,  "swing_lookback": 20},
    # ── Tier 2: break-strength variants ──────────────────────────────────────
    {**_SWEEP_BASE_4H, "min_break_strength": 0.5},
    {**_SWEEP_BASE_4H, "min_break_strength": 1.0},
    {**_SWEEP_BASE_4H, "min_break_strength": 1.5},
    {**_SWEEP_BASE_4H, "min_break_strength": 2.0},
    # ── Tier 3: HTF alignment filter ─────────────────────────────────────────
    {**_SWEEP_BASE_4H, "min_break_strength": 0.7,  "htf_aligned_only": True},
    {**_SWEEP_BASE_4H, "min_break_strength": 1.0,  "htf_aligned_only": True},
    # ── Tier 4: SL mode — broken_level (tighter than swing) ──────────────────
    {**_SWEEP_BASE_4H, "min_break_strength": 0.7,  "sl_mode": "broken_level"},
    {**_SWEEP_BASE_4H, "min_break_strength": 0.7,  "sl_mode": "broken_level", "swing_lookback": 10},
    {**_SWEEP_BASE_4H, "min_break_strength": 1.0,  "sl_mode": "broken_level"},
    {**_SWEEP_BASE_4H, "min_break_strength": 0.7,  "sl_mode": "broken_level", "htf_aligned_only": True},
    {**_SWEEP_BASE_4H, "min_break_strength": 1.0,  "sl_mode": "broken_level", "htf_aligned_only": True},
    # ── Tier 5: SL mode — break_candle (tightest) ────────────────────────────
    {**_SWEEP_BASE_4H, "min_break_strength": 0.7,  "sl_mode": "break_candle"},
    {**_SWEEP_BASE_4H, "min_break_strength": 0.7,  "sl_mode": "break_candle", "swing_lookback": 10},
    {**_SWEEP_BASE_4H, "min_break_strength": 1.0,  "sl_mode": "break_candle"},
    {**_SWEEP_BASE_4H, "min_break_strength": 0.7,  "sl_mode": "break_candle", "htf_aligned_only": True},
    {**_SWEEP_BASE_4H, "min_break_strength": 1.0,  "sl_mode": "break_candle", "htf_aligned_only": True},
    # ── Tier 6: swing age and confluence count ────────────────────────────────
    {**_SWEEP_BASE_4H, "min_break_strength": 0.7,  "min_swing_age_candles": 5},
    {**_SWEEP_BASE_4H, "min_break_strength": 0.7,  "min_swing_age_candles": 5,  "htf_aligned_only": True},
    {**_SWEEP_BASE_4H, "min_break_strength": 0.7,  "min_conf_count": 2},
    # ── Tier 7: best combos of str + htf + sl_mode ───────────────────────────
    {**_SWEEP_BASE_4H, "min_break_strength": 1.0,  "htf_aligned_only": True, "sl_mode": "broken_level"},
    {**_SWEEP_BASE_4H, "min_break_strength": 1.0,  "htf_aligned_only": True, "sl_mode": "break_candle"},
    {**_SWEEP_BASE_4H, "min_break_strength": 0.7,  "htf_aligned_only": True, "sl_mode": "broken_level", "min_conf_count": 2},
]


def _apply_param_filter(raw_signals: list, params: dict) -> list:
    min_str            = params.get("min_break_strength", 0.7)
    req_brt            = params.get("require_brt_confluence", True)
    req_sweep          = params.get("require_liquidity_sweep", False)
    min_conf           = params.get("min_conf_count", 1)
    htf_aligned_only   = params.get("htf_aligned_only", False)
    session_filter     = params.get("session", "all")
    exclude_pairs      = set(params.get("exclude_pairs", []))
    min_swing_age      = params.get("min_swing_age_candles", 5)
    min_swing_tests    = params.get("min_swing_test_count", 0)
    min_break_body     = params.get("min_break_body_pct", 0.0)
    # item-90 loss-reduction filters
    max_conf_cap       = params.get("max_confluences")     # None = no cap
    excl_weekend       = params.get("exclude_weekend", False)
    dead_hours         = set(params.get("dead_hours_utc", []))
    max_str_cap        = params.get("max_break_strength")  # None = no cap
    excl_london_open   = params.get("exclude_london_open", False)
    excl_ny_open       = params.get("exclude_ny_open", False)

    result = []
    for r in raw_signals:
        if r["break_strength"] < min_str:
            continue
        if max_str_cap is not None and r["break_strength"] >= max_str_cap:
            continue
        confs = json.loads(r["confluences"]) if isinstance(r["confluences"], str) else r["confluences"]
        if req_brt and "BRT" not in confs:
            continue
        if req_sweep and not r.get("has_liquidity_sweep"):
            continue
        if len(confs) < min_conf:
            continue
        if max_conf_cap is not None and len(confs) > max_conf_cap:
            continue
        if htf_aligned_only and (not r.get("htf_bias") or r["htf_bias"] != r["direction"]):
            continue
        if session_filter != "all":
            sig_session = r.get("session") or _hour_to_session(r.get("hour", 0))
            if session_filter == "active" and sig_session == "other":
                continue
            elif session_filter in ("london", "ny") and sig_session != session_filter:
                continue
        if r["symbol"] in exclude_pairs:
            continue
        swing_age = r.get("swing_age_candles")
        if swing_age is not None and swing_age < min_swing_age:
            continue
        swing_tests = r.get("swing_test_count")
        if min_swing_tests > 0 and (swing_tests is None or swing_tests < min_swing_tests):
            continue
        break_body = r.get("break_body_pct")
        if min_break_body > 0 and (break_body is None or break_body < min_break_body):
            continue
        if excl_weekend and r.get("dow", 0) in (5, 6):
            continue
        if dead_hours and r.get("hour") in dead_hours:
            continue
        if excl_london_open:
            _ts = r.get("breakout_ts", 0)
            if r.get("hour") == 8 and (_ts % 3600) < 1800:  # 08:00 or 08:15 UTC
                continue
        if excl_ny_open:
            _ts = r.get("breakout_ts", 0)
            if r.get("hour") == 13 and (_ts % 3600) >= 1800:  # 13:30 or 13:45 UTC
                continue
        result.append({**r, "confluences": confs})
    return result


def _pset_label(pset: dict) -> str:
    parts = [f"str{pset['min_break_strength']}"]
    if not pset.get("require_brt_confluence", True):
        parts.append("nobrt")
    if pset.get("require_liquidity_sweep"):
        parts.append("swp")
    if pset.get("htf_aligned_only"):
        parts.append("htf")
    sess = pset.get("session", "all")
    if sess != "all":
        parts.append("actv" if sess == "active" else sess[:3])
    excl = pset.get("exclude_pairs", [])
    if excl:
        parts.append("no" + "+".join(p[:3].lower() for p in excl))
    age = pset.get("min_swing_age_candles", 5)
    if age > 5:
        parts.append(f"age{age}")
    nc = pset.get("min_conf_count", 1)
    if nc > 1:
        parts.append(f"{nc}c")
    st = pset.get("min_swing_test_count", 0)
    if st > 0:
        parts.append(f"tst{st}")
    bb = pset.get("min_break_body_pct", 0.0)
    if bb > 0:
        parts.append(f"body{int(bb*100)}")
    mc = pset.get("max_confluences")
    if mc is not None:
        parts.append(f"mx{mc}")
    if pset.get("exclude_weekend"):
        parts.append("nowe")
    dh = pset.get("dead_hours_utc", [])
    if dh:
        parts.append("nodh")
    ms = pset.get("max_break_strength")
    if ms is not None:
        parts.append(f"mxs{ms:.0f}")
    if pset.get("exclude_london_open"):
        parts.append("nolo")
    if pset.get("exclude_ny_open"):
        parts.append("nony")
    slm = pset.get("sl_mode", "swing")
    if slm != "swing":
        parts.append("bl" if slm == "broken_level" else "bc")
    return "+".join(parts)


def _raw_to_trade_result(r: dict, sl_mode: str = "swing") -> dict:
    confs = r["confluences"] if isinstance(r["confluences"], list) else json.loads(r["confluences"])
    if sl_mode == "broken_level":
        outcome = r.get("outcome_broken_level") or r["outcome"]
        r_win = r.get("eff_r_broken_level") or 2.0
    elif sl_mode == "break_candle":
        outcome = r.get("outcome_break_candle") or r["outcome"]
        r_win = r.get("eff_r_break_candle") or 2.0
    else:
        outcome = r["outcome"]
        r_win = 2.0
    return {
        "alert_id":       r["alert_id"],
        "outcome":        outcome or "OPEN",
        "r_win":          r_win,
        "confluences":    confs,
        "symbol":         r["symbol"],
        "hour":           r["hour"],
        "month":          r["month"],
        "break_strength": r["break_strength"],
        "htf_bias":       r.get("htf_bias"),
        "bos_direction":  r["direction"],
    }


def _compute_stats(trade_results: list) -> dict:
    """Return a JSON-serializable stats dict from a list of trade result dicts."""
    from collections import Counter

    def is_win(oc): return oc in ("WIN", "HTF WIN")
    def is_loss(oc): return oc in ("LOSS", "HTF LOSS")

    resolved = [r for r in trade_results if r["outcome"] != "OPEN"]
    n_res = len(resolved)
    n_wins = sum(1 for r in resolved if is_win(r["outcome"]))
    n_losses = n_res - n_wins
    wr = n_wins / n_res if n_res else 0.0
    lr = 1.0 - wr

    r_wins = [r.get("r_win", 2.0) for r in resolved if is_win(r["outcome"]) and r.get("r_win") is not None]
    avg_r_win = round(sum(r_wins) / len(r_wins), 3) if r_wins else 2.0
    ev_var = round(avg_r_win * wr - lr, 4)

    def _count_group(items, key_fn):
        data = {}
        for r in items:
            k = str(key_fn(r))
            if k not in data:
                data[k] = {"wins": 0, "total": 0, "wr": None}
            data[k]["total"] += 1
            if is_win(r["outcome"]):
                data[k]["wins"] += 1
        for entry in data.values():
            w, t = entry["wins"], entry["total"]
            entry["wr"] = round(w / t, 4) if t else None
        return data

    bkt_data = {}
    for lo, hi, lbl in [(0.7, 1.0, "0.7-1.0"), (1.0, 1.5, "1.0-1.5"), (1.5, 2.0, "1.5-2.0"), (2.0, 9999.0, "2.0+")]:
        grp = [r for r in resolved if lo <= r["break_strength"] < hi]
        w = sum(1 for r in grp if is_win(r["outcome"]))
        t = len(grp)
        bkt_data[lbl] = {"wins": w, "total": t, "wr": round(w / t, 4) if t else None}

    htf_data = {k: {"wins": 0, "total": 0, "wr": None} for k in ("aligned", "counter")}
    for r in resolved:
        if r["htf_bias"] is None:
            continue
        key = "aligned" if r["bos_direction"] == r["htf_bias"] else "counter"
        htf_data[key]["total"] += 1
        if is_win(r["outcome"]):
            htf_data[key]["wins"] += 1
    for entry in htf_data.values():
        w, t = entry["wins"], entry["total"]
        entry["wr"] = round(w / t, 4) if t else None

    conf_data = {}
    for c in ("CONF_CANDLE", "BRT", "OB_RETRACE", "FVG", "HTF_LEVEL"):
        w_c = sum(1 for r in resolved if is_win(r["outcome"]) and c in r["confluences"])
        l_c = sum(1 for r in resolved if is_loss(r["outcome"]) and c in r["confluences"])
        conf_data[c] = {
            "win_pct":  round(w_c / n_wins * 100, 1) if n_wins else None,
            "loss_pct": round(l_c / n_losses * 100, 1) if n_losses else None,
        }

    all_oc = Counter(r["outcome"] for r in trade_results)
    return {
        "total":    len(trade_results),
        "wins":     n_wins,
        "losses":   n_losses,
        "open":     all_oc.get("OPEN", 0),
        "resolved": n_res,
        "win_rate": round(wr, 4),
        "avg_r_win": avg_r_win,
        "ev_variable_r": ev_var,
        "expected_value": {
            "1:2":   round(wr * 2 - lr, 4),
            "1:1.5": round(wr * 1.5 - lr, 4),
            "1:3":   round(wr * 3 - lr, 4),
        } if n_res else {},
        "break_strength":   bkt_data,
        "confluence_count": _count_group(resolved, lambda r: len(r["confluences"])),
        "htf_alignment":    htf_data,
        "hour_of_day":      _count_group(resolved, lambda r: r["hour"]),
        "monthly":          _count_group(resolved, lambda r: r["month"]),
        "confluence_types": conf_data,
    }


def _run_param_sweep(raw_signals: list, db: LocalDB,
                     strategy: str = "BOS15m", update_gold: bool = False,
                     param_sets: list = None) -> tuple:
    """Evaluate param_sets (defaults to PARAM_SWEEP_SETS) against raw_signals."""
    if param_sets is None:
        param_sets = PARAM_SWEEP_SETS
    all_symbols = sorted({r["symbol"] for r in raw_signals})
    logging.info("")
    logging.info("=== Parameter Sweep (%d sets × %d pairs) ===", len(param_sets), len(all_symbols))
    hdr_pairs = "  ".join(f"{s[:6]:>6}" for s in all_symbols)
    logging.info("  %-30s  %s  %s  %s", "Params", hdr_pairs, "OVERAL", "EV1:2")
    logging.info("  " + "-" * 102)

    all_pair_stats: dict = {}
    pset_ids: list = []

    for pset in param_sets:
        pset_id = db.get_or_create_param_set(pset)
        pset_ids.append(pset_id)
        sl_mode = pset.get("sl_mode", "swing")

        filtered = _apply_param_filter(raw_signals, pset)
        trades   = [_raw_to_trade_result(r, sl_mode=sl_mode) for r in filtered]

        for sym in all_symbols:
            sym_trades = [t for t in trades if t["symbol"] == sym]
            s = _compute_stats(sym_trades)
            all_pair_stats[(pset_id, sym)] = s
            db.insert_scan_stats(pset_id, sym, s["total"], s["wins"], s["losses"], s["open"], json.dumps(s))

        ov = _compute_stats(trades)
        all_pair_stats[(pset_id, "ALL")] = ov
        db.insert_scan_stats(pset_id, "ALL", ov["total"], ov["wins"], ov["losses"], ov["open"], json.dumps(ov))

        lbl      = _pset_label(pset)
        pair_cols = "  ".join(f"{all_pair_stats[(pset_id, s)]['win_rate']*100:>5.1f}%" for s in all_symbols)
        ev_disp  = ov.get("ev_variable_r", ov.get("expected_value", {}).get("1:2", 0))
        avg_r    = ov.get("avg_r_win", 2.0)
        r_tag    = f"avgR:{avg_r:.2f}" if sl_mode != "swing" else ""
        logging.info("  v%-3d %-25s  %s  %5.1f%%  %s%+.3fR",
                     pset_id, lbl, pair_cols, ov["win_rate"] * 100,
                     f"{r_tag}  " if r_tag else "", ev_disp)

    logging.info("")
    logging.info("Best param set per pair (min 20 resolved trades):")
    gold_map = db.get_gold_params(strategy)
    for sym in all_symbols:
        candidates = [
            (pid, pset) for pid, pset in zip(pset_ids, param_sets)
            if all_pair_stats.get((pid, sym), {}).get("resolved", 0) >= 20
        ]
        if sym in SWEEP_REQUIRED_SYMBOLS:
            sweep_cands = [(pid, pset) for pid, pset in candidates
                           if pset.get("require_liquidity_sweep", False)]
            if sweep_cands:
                candidates = sweep_cands
            else:
                logging.info("  %-10s  (no sweep-required set with ≥20 trades)", sym)
        if not candidates:
            logging.info("  %-10s  (no param set with ≥20 trades)", sym)
            continue
        best_pid, best_pset = max(candidates, key=lambda x: all_pair_stats[(x[0], sym)].get("ev_variable_r", 0))
        s = all_pair_stats[(best_pid, sym)]
        ev_var = s.get("ev_variable_r", s.get("expected_value", {}).get("1:2", 0))
        avg_r  = s.get("avg_r_win", 2.0)
        gold = gold_map.get(sym)
        if gold:
            delta_wr = s["win_rate"] - gold["win_rate"]
            delta_ev = ev_var - gold["ev_1_2"]
            if update_gold:
                db.upsert_gold_params(strategy, sym, best_pid, s["win_rate"], ev_var, s["resolved"])
                gold_note = f"  [Δgold: {delta_wr*100:+.1f}pp WR  {delta_ev:+.3f}R EV] ← GOLD UPDATED"
            elif best_pid != gold["param_set_id"]:
                gold_note = f"  [Δgold: {delta_wr*100:+.1f}pp WR  {delta_ev:+.3f}R EV] ⚡ SUGGESTED"
            else:
                gold_note = f"  [Δgold: {delta_wr*100:+.1f}pp WR  {delta_ev:+.3f}R EV] ✓ unchanged"
        else:
            if update_gold:
                db.upsert_gold_params(strategy, sym, best_pid, s["win_rate"], ev_var, s["resolved"])
                gold_note = " ← GOLD SET"
            else:
                gold_note = " ⚡ SUGGESTED (no gold yet — run --update-gold to apply)"
        logging.info("  %-10s  v%-3d %-25s  WR: %5.1f%%  avgR: %.2f  Trades: %d  EV: %+.3fR%s",
                     sym, best_pid, _pset_label(best_pset), s["win_rate"] * 100, avg_r,
                     s["resolved"], ev_var, gold_note)

    return all_pair_stats, all_symbols, pset_ids


def _send_sweep_summary(
    alert_manager: AlertManager,
    raw_signals: list,
    all_pair_stats: dict,
    all_symbols: list,
    pset_ids: list,
    param_sets: list = None,
    strategy: str = "BOS15m",
) -> None:
    """Send a concise Telegram summary of the sweep results."""
    if param_sets is None:
        param_sets = PARAM_SWEEP_SETS
    try:
        gold_map = alert_manager.db.get_gold_params(strategy) if hasattr(alert_manager, "db") else {}

        pset_ev = []
        for pid, pset in zip(pset_ids, param_sets):
            ov = all_pair_stats.get((pid, "ALL"), {})
            ev = ov.get("ev_variable_r", ov.get("expected_value", {}).get("1:2", -999))
            wr = ov.get("win_rate", 0)
            n  = ov.get("resolved", 0)
            if n >= 20:
                pset_ev.append((pid, pset, ev, wr, n))
        pset_ev.sort(key=lambda x: x[2], reverse=True)

        lines = [
            f"📊 BOS Param Sweep — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
            f"Signals: {len(raw_signals):,}  Sets: {len(param_sets)}",
            "",
            "Top 5 by EV:",
        ]
        for i, (pid, pset, ev, wr, n) in enumerate(pset_ev[:5], 1):
            lines.append(f"  {i}. v{pid} {_pset_label(pset)}: WR={wr*100:.1f}% EV={ev:+.3f}R (n={n})")

        lines += ["", "Best per pair (>=20 trades):"]
        suggestions = []
        for sym in all_symbols:
            candidates = [
                (pid, pset) for pid, pset in zip(pset_ids, param_sets)
                if all_pair_stats.get((pid, sym), {}).get("resolved", 0) >= 20
            ]
            if not candidates:
                lines.append(f"  {sym}: (no qualifying set)")
                continue
            best_pid, best_pset = max(candidates, key=lambda x: all_pair_stats[(x[0], sym)].get("ev_variable_r", 0))
            s = all_pair_stats[(best_pid, sym)]
            gold = gold_map.get(sym)
            if gold and best_pid != gold["param_set_id"]:
                delta_wr = (s["win_rate"] - gold["win_rate"]) * 100
                delta_ev = s.get("ev_variable_r", 0) - gold["ev_1_2"]
                change_tag = " ⚡"
                suggestions.append(
                    f"  {sym}: v{best_pid} {_pset_label(best_pset)} "
                    f"({delta_wr:+.1f}pp WR  {delta_ev:+.3f}R EV)"
                )
            else:
                change_tag = ""
            lines.append(f"  {sym}: {_pset_label(best_pset)} WR={s['win_rate']*100:.1f}% (n={s['resolved']}){change_tag}")

        if suggestions:
            lines += [
                "",
                "⚡ Suggested gold param changes:",
            ] + suggestions + [
                "",
                "To apply: cd src && python -m main --experiment-bos --update-gold",
            ]
        else:
            lines.append("\n✅ All gold params unchanged — no action needed.")

        alert_manager.notifier.send_message("\n".join(lines))
        logging.info("Sweep summary sent to Telegram")

        # Send most recent chart per pair so setups are visible without needing local disk access
        charts_dir = alert_manager.charts_dir
        for sym in all_symbols:
            matches = sorted(
                charts_dir.glob(f"bos15m-*-{sym.lower()}-*.png"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not matches:
                continue
            best_cands = [
                (pid, pset) for pid, pset in zip(pset_ids, param_sets)
                if all_pair_stats.get((pid, sym), {}).get("resolved", 0) >= 20
            ]
            if best_cands:
                best_pid, best_pset = max(
                    best_cands, key=lambda x: all_pair_stats[(x[0], sym)].get("ev_variable_r", 0)
                )
                s = all_pair_stats[(best_pid, sym)]
                caption = (
                    f"{sym} — {_pset_label(best_pset)}\n"
                    f"WR={s['win_rate']*100:.1f}%  "
                    f"avgR={s.get('avg_r_win', 0):.2f}  "
                    f"EV={s.get('ev_variable_r', 0):+.3f}R  "
                    f"n={s['resolved']}"
                )
            else:
                caption = f"{sym} — most recent BOS signal"
            try:
                alert_manager.send_alert({"message": caption, "image_path": str(matches[0]), "alert_id": ""})
            except Exception as chart_exc:
                logging.warning("Failed to send chart for %s: %s", sym, chart_exc)
    except Exception as exc:
        logging.warning("Failed to send sweep summary: %s", exc)


def run_loss_analysis(db: LocalDB, alert_manager: AlertManager,
                      strategy: str = "BOS15m", timeframe: str = "15m") -> None:
    """Analyse what predicts losing trades and whether early-exit rules improve EV."""
    import sqlite3 as _sq, bisect as _bs, statistics as _st

    conn = _sq.connect(db.db_path)
    conn.row_factory = _sq.Row
    cur = conn.cursor()

    row = cur.execute(
        "SELECT scan_run_id FROM raw_signals WHERE strategy=? "
        "GROUP BY scan_run_id ORDER BY COUNT(*) DESC LIMIT 1", (strategy,)
    ).fetchone()
    if not row:
        logging.error("No raw signals for strategy=%s — run --experiment-bos first", strategy)
        return
    scan_run_id = row["scan_run_id"]

    run_date = datetime.fromtimestamp(scan_run_id, tz=timezone.utc).strftime("%Y-%m-%d")
    lines: list = [f"=== Loss Pattern Analysis ({strategy}) — {run_date} ===", ""]

    def _section(title: str, rows, label_fn, min_n: int = 10) -> None:
        lines.append(title)
        for r in rows:
            n, w = r["n"], r["wins"]
            if n < min_n:
                continue
            lines.append(f"  {label_fn(r):<18} WR={w/n*100:5.1f}%  n={n:4d}")
        lines.append("")

    DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    q, s = (scan_run_id, strategy)

    # ── 1. Day of week ─────────────────────────────────────────────────
    _section("Day of week:", cur.execute(
        "SELECT dow, COUNT(*) n, SUM(CASE WHEN outcome LIKE '%WIN%' THEN 1 ELSE 0 END) wins "
        "FROM raw_signals WHERE scan_run_id=? AND strategy=? AND outcome!='OPEN' "
        "GROUP BY dow ORDER BY dow", (q, s)).fetchall(),
        lambda r: DOW[r["dow"]] if r["dow"] is not None and 0 <= r["dow"] <= 6 else "?")

    # ── 2. Session ─────────────────────────────────────────────────────
    _section("Session:", cur.execute(
        "SELECT COALESCE(session,'?') session, COUNT(*) n, "
        "SUM(CASE WHEN outcome LIKE '%WIN%' THEN 1 ELSE 0 END) wins "
        "FROM raw_signals WHERE scan_run_id=? AND strategy=? AND outcome!='OPEN' "
        "GROUP BY session", (q, s)).fetchall(),
        lambda r: r["session"])

    # ── 3. Hour of day ─────────────────────────────────────────────────
    h_rows = cur.execute(
        "SELECT hour, COUNT(*) n, SUM(CASE WHEN outcome LIKE '%WIN%' THEN 1 ELSE 0 END) wins "
        "FROM raw_signals WHERE scan_run_id=? AND strategy=? AND outcome!='OPEN' "
        "GROUP BY hour ORDER BY hour", (q, s)).fetchall()
    qual = [r for r in h_rows if r["n"] >= 20]
    lines.append("Best hours (UTC):  " + "  ".join(
        f"{r['hour']:02d}h={r['wins']/r['n']*100:.0f}%(n={r['n']})"
        for r in sorted(qual, key=lambda r: r["wins"]/r["n"], reverse=True)[:5]))
    lines.append("Worst hours (UTC): " + "  ".join(
        f"{r['hour']:02d}h={r['wins']/r['n']*100:.0f}%(n={r['n']})"
        for r in sorted(qual, key=lambda r: r["wins"]/r["n"])[:5]))
    lines.append("")

    # ── 4. Break strength ─────────────────────────────────────────────
    _section("Break strength:", cur.execute(
        "SELECT CASE WHEN break_strength<1.0 THEN '0.7-1.0' "
        "           WHEN break_strength<1.5 THEN '1.0-1.5' "
        "           WHEN break_strength<2.0 THEN '1.5-2.0' "
        "           ELSE '2.0+' END bucket, "
        "COUNT(*) n, SUM(CASE WHEN outcome LIKE '%WIN%' THEN 1 ELSE 0 END) wins "
        "FROM raw_signals WHERE scan_run_id=? AND strategy=? AND outcome!='OPEN' "
        "GROUP BY bucket ORDER BY MIN(break_strength)", (q, s)).fetchall(),
        lambda r: "str " + r["bucket"])

    # ── 5. HTF alignment ──────────────────────────────────────────────
    _section("HTF alignment:", cur.execute(
        "SELECT CASE WHEN (direction='bullish' AND htf_bias='bullish') "
        "                 OR (direction='bearish' AND htf_bias='bearish') "
        "            THEN 'aligned' ELSE 'counter' END align, "
        "COUNT(*) n, SUM(CASE WHEN outcome LIKE '%WIN%' THEN 1 ELSE 0 END) wins "
        "FROM raw_signals WHERE scan_run_id=? AND strategy=? AND outcome!='OPEN' "
        "AND htf_bias IS NOT NULL GROUP BY align", (q, s)).fetchall(),
        lambda r: r["align"])

    # ── 6. Confluence count ───────────────────────────────────────────
    _section("Confluence count:", cur.execute(
        "SELECT json_array_length(confluences) n_conf, COUNT(*) n, "
        "SUM(CASE WHEN outcome LIKE '%WIN%' THEN 1 ELSE 0 END) wins "
        "FROM raw_signals WHERE scan_run_id=? AND strategy=? AND outcome!='OPEN' "
        "GROUP BY n_conf ORDER BY n_conf", (q, s)).fetchall(),
        lambda r: f"confs={r['n_conf']}")

    # ── 7. Liquidity sweep ────────────────────────────────────────────
    _section("Liquidity sweep:", cur.execute(
        "SELECT has_liquidity_sweep, COUNT(*) n, "
        "SUM(CASE WHEN outcome LIKE '%WIN%' THEN 1 ELSE 0 END) wins "
        "FROM raw_signals WHERE scan_run_id=? AND strategy=? AND outcome!='OPEN' "
        "GROUP BY has_liquidity_sweep", (q, s)).fetchall(),
        lambda r: "sweep" if r["has_liquidity_sweep"] else "no_sweep")

    # ── 8. Monthly trend ──────────────────────────────────────────────
    lines.append("Monthly trend:")
    for r in cur.execute(
        "SELECT month, COUNT(*) n, SUM(CASE WHEN outcome LIKE '%WIN%' THEN 1 ELSE 0 END) wins "
        "FROM raw_signals WHERE scan_run_id=? AND strategy=? AND outcome!='OPEN' "
        "GROUP BY month ORDER BY month", (q, s)).fetchall():
        wr = r["wins"] / r["n"] * 100
        bar = "▲" if wr >= 35 else ("▼" if wr < 25 else "─")
        lines.append(f"  {r['month']}  WR={wr:5.1f}%  n={r['n']:3d}  {bar}")
    lines.append("")

    # ── 9. Candle-by-candle momentum analysis ────────────────────────
    logging.info("Loading candle data for momentum analysis...")
    sig_rows = cur.execute(
        "SELECT breakout_ts, symbol, direction, broken_level, outcome "
        "FROM raw_signals WHERE scan_run_id=? AND strategy=? AND outcome!='OPEN' "
        "ORDER BY symbol, breakout_ts", (q, s)).fetchall()

    # Load all candles into parallel arrays (one pass per pair)
    pair_ts: dict = {}; pair_hi: dict = {}; pair_lo: dict = {}
    for sym in set(r["symbol"] for r in sig_rows):
        rows_c = cur.execute(
            "SELECT timestamp, high, low FROM fx_candles "
            "WHERE symbol=? AND timeframe=? ORDER BY timestamp ASC", (sym, timeframe)).fetchall()
        pair_ts[sym] = [c["timestamp"] for c in rows_c]
        pair_hi[sym] = [c["high"] for c in rows_c]
        pair_lo[sym] = [c["low"] for c in rows_c]

    logging.info("Candles loaded. Running momentum analysis over %d signals...", len(sig_rows))

    CHKPTS = [1, 2, 3, 4, 5, 6, 8, 10]
    win_fav: dict = {k: [] for k in CHKPTS}
    loss_fav: dict = {k: [] for k in CHKPTS}
    fav6_records: list = []       # (fav6_atr, is_win)
    time_to_1r: list = []         # candles to reach 1×ATR from entry for WIN signals

    for sig in sig_rows:
        sym, ts, drn, entry = sig["symbol"], sig["breakout_ts"], sig["direction"], sig["broken_level"]
        is_win = "WIN" in sig["outcome"]
        ts_list = pair_ts.get(sym, [])
        if not ts_list:
            continue

        idx = _bs.bisect_right(ts_list, ts)

        # ATR from prior 14 candles
        p0 = max(0, idx - 14)
        ph, pl = pair_hi[sym][p0:idx], pair_lo[sym][p0:idx]
        atr = _st.mean(h - l for h, l in zip(ph, pl)) if len(ph) >= 2 else entry * 0.001
        if atr <= 0:
            atr = entry * 0.001

        fh, fl = pair_hi[sym][idx:idx+20], pair_lo[sym][idx:idx+20]
        if not fh:
            continue

        bullish = (drn == "bullish")

        for k in CHKPTS:
            if k > len(fh):
                continue
            wh, wl = fh[:k], fl[:k]
            fav = ((max(wh) - entry) if bullish else (entry - min(wl))) / atr
            (win_fav if is_win else loss_fav)[k].append(fav)

        if len(fh) >= 6:
            fav6 = ((max(fh[:6]) - entry) if bullish else (entry - min(fl[:6]))) / atr
            fav6_records.append((fav6, is_win))

        # Time to reach 1×ATR favorable (approx 1R with broken_level SL)
        if is_win:
            target = (entry + atr) if bullish else (entry - atr)
            for k, (h, l) in enumerate(zip(fh, fl)):
                hit = (h >= target) if bullish else (l <= target)
                if hit:
                    time_to_1r.append(k + 1)
                    break

    lines.append("Momentum: max favorable move in first N candles (ATR units)")
    lines.append(f"  {'k':>3}  {'WIN':>7}  {'LOSS':>8}  {'diff':>6}")
    for k in CHKPTS:
        wl, ll = win_fav[k], loss_fav[k]
        if not wl or not ll:
            continue
        wa, la = _st.mean(wl), _st.mean(ll)
        lines.append(f"  {k:3d}  {wa:7.2f}  {la:8.2f}  {wa-la:+6.2f}")
    lines.append("")

    # ── 10. Stall test: quartile analysis at k=6 ─────────────────────
    if fav6_records:
        fav6_sorted = sorted(fav6_records, key=lambda x: x[0])
        n4 = len(fav6_sorted) // 4
        quartiles = [("Q1 stalled ", fav6_sorted[:n4]),
                     ("Q2         ", fav6_sorted[n4:2*n4]),
                     ("Q3         ", fav6_sorted[2*n4:3*n4]),
                     ("Q4 strong  ", fav6_sorted[3*n4:])]
        lines.append("WR by momentum quartile at candle 6  (Q1=stalled, Q4=strong):")
        for label, qd in quartiles:
            if not qd:
                continue
            qw = sum(1 for _, w in qd if w)
            avg_fav = _st.mean(f for f, _ in qd)
            lines.append(f"  {label}  WR={qw/len(qd)*100:5.1f}%  n={len(qd):4d}  avg_move={avg_fav:.2f}ATR")
        lines.append("")

        # EV simulation: exit Q1 stalled trades at 0R (breakeven) instead of waiting
        active = fav6_sorted[n4:]
        q1 = fav6_sorted[:n4]
        ev_current = _st.mean((2.0 if w else -1.0) for _, w in fav6_records)
        ev_exit_q1 = sum((2.0 if w else -1.0) for _, w in active) / len(fav6_records)
        delta = ev_exit_q1 - ev_current
        lines.append("Simulated EV: if stalled Q1 trades are closed at 0R (breakeven):")
        lines.append(f"  Current EV:   {ev_current:+.3f}R/trade")
        lines.append(f"  Simulated EV: {ev_exit_q1:+.3f}R/trade  (delta {delta:+.3f}R  "
                     f"{'↑ BETTER' if delta > 0 else '↓ WORSE'})")
        lines.append("")

    # ── 11. Time to 1R for WIN signals ────────────────────────────────
    if time_to_1r:
        lines.append(f"WIN signals: candles to reach 1R favorable (n={len(time_to_1r)}):")
        for threshold in [2, 3, 4, 6, 8, 10]:
            pct = sum(1 for x in time_to_1r if x <= threshold) / len(time_to_1r) * 100
            lines.append(f"  ≤{threshold:2d} candles: {pct:5.1f}%")
        median_t = sorted(time_to_1r)[len(time_to_1r) // 2]
        lines.append(f"  Median: {median_t} candles")
        lines.append("")

    conn.close()

    for line in lines:
        logging.info(line)

    try:
        # Telegram has a 4096-char message limit — split if needed
        chunk, chunks = [], []
        for line in lines:
            chunk.append(line)
            if sum(len(l)+1 for l in chunk) > 3800:
                chunks.append("\n".join(chunk))
                chunk = []
        if chunk:
            chunks.append("\n".join(chunk))
        for part in chunks:
            alert_manager.notifier.send_message(part)
        logging.info("Loss analysis sent to Telegram (%d message(s))", len(chunks))
    except Exception as exc:
        logging.warning("Failed to send loss analysis: %s", exc)


def run_nightly_sweep(db: LocalDB, alert_manager: AlertManager) -> None:
    """Re-run the param sweep on the latest raw_signals in DB (no rescan, takes seconds)."""
    scan_run_id, raw_signals = db.get_latest_raw_signals(strategy="BOS15m")
    if not raw_signals:
        logging.error("No BOS15m raw signals in DB. Run --experiment-bos first.")
        return
    logging.info("Nightly sweep: %d raw signals from scan_run_id=%d", len(raw_signals), scan_run_id)
    all_pair_stats, all_symbols, pset_ids = _run_param_sweep(raw_signals, db)
    _send_sweep_summary(alert_manager, raw_signals, all_pair_stats, all_symbols, pset_ids)


def run_decay_check(db: LocalDB, alert_manager: AlertManager,
                    min_live_trades: int = 5, decay_threshold: float = 0.15) -> None:
    """Compare live WR (from trade_monitors) against gold historical WR per pair.

    Sends a Telegram report flagging any pair where live WR has dropped more than
    decay_threshold (default 15pp) below the historical gold WR.
    Requires at least min_live_trades closed trades per pair to report.
    """
    gold_map = db.get_gold_params("BOS15m")

    conn = db._get_conn()
    rows = conn.execute(
        "SELECT symbol, status, COUNT(*) FROM trade_monitors "
        "WHERE status IN ('tp_hit','sl_hit') GROUP BY symbol, status"
    ).fetchall()

    # Aggregate live counts per symbol
    live: dict = {}
    for symbol, status, count in rows:
        if symbol not in live:
            live[symbol] = {"tp_hit": 0, "sl_hit": 0}
        live[symbol][status] = count

    if not live:
        logging.info("Decay check: no closed live trades in trade_monitors yet.")
        return

    lines = ["📊 *Weekly Decay Report* — live WR vs gold historical WR\n"]
    any_flagged = False

    for symbol in sorted(live):
        wins   = live[symbol].get("tp_hit", 0)
        losses = live[symbol].get("sl_hit", 0)
        n      = wins + losses
        if n < min_live_trades:
            lines.append(f"  {symbol}: n={n} (below min {min_live_trades}, skipping)")
            continue

        live_wr = wins / n
        gold     = gold_map.get(symbol)
        gold_wr  = gold["win_rate"] if gold else None

        if gold_wr is None:
            lines.append(f"  {symbol}: live WR={live_wr*100:.1f}% (n={n}) — no gold baseline")
            continue

        delta = live_wr - gold_wr
        if delta <= -decay_threshold:
            flag = "🔴 DEGRADED"
            any_flagged = True
        elif delta <= -decay_threshold / 2:
            flag = "🟡 watch"
        else:
            flag = "🟢 ok"

        lines.append(
            f"  {flag} {symbol}: live {live_wr*100:.1f}% vs gold {gold_wr*100:.1f}% "
            f"(Δ{delta*100:+.1f}pp, n={n})"
        )

    if any_flagged:
        lines.append("\n⚠️ Degraded pairs may need manual review. Run --experiment-bos --update-gold to re-evaluate.")
    else:
        lines.append("\n✅ All pairs within tolerance.")

    msg = "\n".join(lines)
    logging.info("Decay check:\n%s", msg)
    if alert_manager.notifier:
        alert_manager.notifier.send_message(msg)
    return live


def _build_weekly_markdown(
    report_dt: "datetime",
    raw_signals: list,
    all_pair_stats: dict,
    all_symbols: list,
    pset_ids: list,
    param_sets: list,
    gold_map: dict,
    live_stats: dict,
    decay_threshold: float = 0.15,
) -> str:
    week_str   = report_dt.strftime("%Y-%m-%d")
    next_week  = (report_dt + __import__("datetime").timedelta(days=7)).strftime("%Y-%m-%d")

    lines = [
        f"# Weekly Report — {week_str}",
        "",
        f"> Generated automatically on {report_dt.strftime('%Y-%m-%d %H:%M UTC')}  ",
        f"> Signals scanned: {len(raw_signals):,}  |  Param sets: {len(param_sets)}",
        "",
        "---",
        "",
        "## Current Gold Params",
        "",
        "| Pair | Param Set | WR | EV (var-R) | Trades |",
        "|------|-----------|----|------------|--------|",
    ]
    for sym in sorted(gold_map):
        g = gold_map[sym]
        pset = db_pset = None
        try:
            from db.local_db import LocalDB as _LDB  # already imported at module level
        except Exception:
            pass
        lbl = f"v{g['param_set_id']}"
        lines.append(f"| {sym} | {lbl} | {g['win_rate']*100:.1f}% | {g['ev_1_2']:+.3f}R | {g.get('resolved','?')} |")

    # Sweep suggestions
    suggestions = []
    for sym in all_symbols:
        candidates = [
            (pid, pset) for pid, pset in zip(pset_ids, param_sets)
            if all_pair_stats.get((pid, sym), {}).get("resolved", 0) >= 20
        ]
        if not candidates:
            continue
        best_pid, best_pset = max(candidates, key=lambda x: all_pair_stats[(x[0], sym)].get("ev_variable_r", 0))
        s = all_pair_stats[(best_pid, sym)]
        gold = gold_map.get(sym)
        if gold and best_pid != gold["param_set_id"]:
            delta_wr = (s["win_rate"] - gold["win_rate"]) * 100
            delta_ev = s.get("ev_variable_r", 0) - gold["ev_1_2"]
            suggestions.append((sym, best_pid, best_pset, s, delta_wr, delta_ev))

    lines += [
        "",
        "## Param Sweep — Suggested Changes",
        "",
    ]
    if suggestions:
        lines += [
            "| Pair | Suggested Set | Δ WR | Δ EV | New WR | Trades |",
            "|------|---------------|------|------|--------|--------|",
        ]
        for sym, best_pid, best_pset, s, delta_wr, delta_ev in suggestions:
            lbl = _pset_label(best_pset)
            lines.append(
                f"| {sym} | v{best_pid} `{lbl}` | {delta_wr:+.1f}pp | {delta_ev:+.3f}R"
                f" | {s['win_rate']*100:.1f}% | {s['resolved']} |"
            )
        lines += [
            "",
            "**To apply:**",
            "```",
            "cd /home/tzahi/repo/trade/src && python -m main --experiment-bos --update-gold",
            "```",
        ]
    else:
        lines.append("_No changes suggested — all gold params are optimal._")

    # Decay check
    lines += [
        "",
        "## Live Decay Check",
        "",
        "| Pair | Live WR | Gold WR | Δ | Status |",
        "|------|---------|---------|---|--------|",
    ]
    if live_stats:
        for sym in sorted(live_stats):
            w = live_stats[sym].get("tp_hit", 0)
            l = live_stats[sym].get("sl_hit", 0)
            n = w + l
            if n < 5:
                lines.append(f"| {sym} | — | — | — | n={n} (too few) |")
                continue
            live_wr = w / n
            gold = gold_map.get(sym)
            gold_wr = gold["win_rate"] if gold else None
            if gold_wr is None:
                lines.append(f"| {sym} | {live_wr*100:.1f}% | — | — | no baseline |")
                continue
            delta = live_wr - gold_wr
            flag = "🔴 DEGRADED" if delta <= -decay_threshold else ("🟡 watch" if delta <= -decay_threshold/2 else "🟢 ok")
            lines.append(f"| {sym} | {live_wr*100:.1f}% | {gold_wr*100:.1f}% | {delta*100:+.1f}pp | {flag} |")
    else:
        lines.append("| — | — | — | — | No closed live trades yet |")

    # Outcome section for manual fill-in
    lines += [
        "",
        "---",
        "",
        f"## Outcome — Week of {next_week}",
        "",
        "_Fill in after the week plays out. Compare against suggestions above._",
        "",
        "| Pair | Direction | Entry | Result | Notes |",
        "|------|-----------|-------|--------|-------|",
    ]
    for sym in all_symbols:
        lines.append(f"| {sym} | | | | |")

    lines += [
        "",
        "**Were the suggestions correct?** _(delete as applicable)_",
        "",
        "- [ ] Applied suggested gold param changes",
        "- [ ] Rejected suggestions — reason: _____",
        "- [ ] No changes needed",
    ]

    return "\n".join(lines) + "\n"


def run_weekly_report(settings: "Settings", db: LocalDB,
                      alert_manager: AlertManager) -> None:
    """Full weekly job: rescan BOS experiment (no gold update) + decay check + save markdown report."""
    import subprocess
    from pathlib import Path as _Path

    logging.info("=== Weekly report: running full BOS experiment (no gold update) ===")
    result = run_bos_experiment(settings, db, alert_manager, update_gold=False)
    logging.info("=== Weekly report: running decay check ===")
    live_stats = run_decay_check(db, alert_manager) or {}

    if result is None:
        logging.warning("Weekly report: no sweep data returned — skipping markdown save")
        return

    all_pair_stats, all_symbols, pset_ids, raw_signals, param_sets = result
    gold_map = db.get_gold_params("BOS15m")
    report_dt = datetime.now(timezone.utc)

    md = _build_weekly_markdown(
        report_dt, raw_signals, all_pair_stats, all_symbols,
        pset_ids, param_sets, gold_map, live_stats,
    )

    repo_root = _Path(__file__).parents[1]
    reports_dir = repo_root / "reports"
    reports_dir.mkdir(exist_ok=True)
    report_path = reports_dir / f"weekly_{report_dt.strftime('%Y-%m-%d')}.md"
    report_path.write_text(md, encoding="utf-8")
    logging.info("Weekly report saved: %s", report_path)

    # Auto-commit the report file into the repo
    try:
        subprocess.run(["git", "-C", str(repo_root), "add", str(report_path)], check=True)
        subprocess.run(
            ["git", "-C", str(repo_root), "commit", "-m",
             f"Weekly report {report_dt.strftime('%Y-%m-%d')}: sweep findings + suggestions"],
            check=True,
        )
        subprocess.run(["git", "-C", str(repo_root), "push"], check=True)
        logging.info("Weekly report committed and pushed.")
    except subprocess.CalledProcessError as exc:
        logging.warning("Weekly report git commit failed: %s", exc)


# ──────────────────────────── FVG DOJI STRATEGY ────────────────────────────

_FVG_SWEEP_BASE = {
    "fvg_lookback":           20,
    "max_bars_after_fvg":     8,
    "max_doji_body_pct":      0.20,
    "min_rejection_wick_pct": 0.75,
    "min_momentum_body_pct":  0.60,
    "min_fvg_size_atr":       0.3,
    "min_retrace_pct":        0.50,
    "scan_days":              365,
}

FVG_PARAM_SWEEP_SETS = [
    # ── wick ≥ 75% baseline ──
    {**_FVG_SWEEP_BASE},
    {**_FVG_SWEEP_BASE, "htf_aligned_only": True},
    {**_FVG_SWEEP_BASE, "min_fvg_size_atr": 0.5},
    {**_FVG_SWEEP_BASE, "min_fvg_size_atr": 0.5, "htf_aligned_only": True},
    {**_FVG_SWEEP_BASE, "min_retrace_pct": 0.70},
    {**_FVG_SWEEP_BASE, "min_retrace_pct": 0.70, "htf_aligned_only": True},
    {**_FVG_SWEEP_BASE, "min_fvg_size_atr": 0.5, "min_retrace_pct": 0.70},
    {**_FVG_SWEEP_BASE, "min_fvg_size_atr": 0.5, "min_retrace_pct": 0.70, "htf_aligned_only": True},
    # ── wick ≥ 80% ──
    {**_FVG_SWEEP_BASE, "min_rejection_wick_pct": 0.80},
    {**_FVG_SWEEP_BASE, "min_rejection_wick_pct": 0.80, "htf_aligned_only": True},
    {**_FVG_SWEEP_BASE, "min_rejection_wick_pct": 0.80, "min_fvg_size_atr": 0.5},
    {**_FVG_SWEEP_BASE, "min_rejection_wick_pct": 0.80, "min_fvg_size_atr": 0.5, "htf_aligned_only": True},
    {**_FVG_SWEEP_BASE, "min_rejection_wick_pct": 0.80, "min_retrace_pct": 0.70},
    {**_FVG_SWEEP_BASE, "min_rejection_wick_pct": 0.80, "min_retrace_pct": 0.70, "htf_aligned_only": True},
    # ── wick ≥ 85% ──
    {**_FVG_SWEEP_BASE, "min_rejection_wick_pct": 0.85},
    {**_FVG_SWEEP_BASE, "min_rejection_wick_pct": 0.85, "htf_aligned_only": True},
    {**_FVG_SWEEP_BASE, "min_rejection_wick_pct": 0.85, "min_fvg_size_atr": 0.5},
    {**_FVG_SWEEP_BASE, "min_rejection_wick_pct": 0.85, "min_retrace_pct": 0.70},
    # ── wick ≥ 90% (very rare / very high bar) ──
    {**_FVG_SWEEP_BASE, "min_rejection_wick_pct": 0.90},
    {**_FVG_SWEEP_BASE, "min_rejection_wick_pct": 0.90, "htf_aligned_only": True},
]


def _fvg_pset_label(pset: dict) -> str:
    lbl = f"fvg{pset['min_fvg_size_atr']}+r{pset['min_retrace_pct']}+b{pset['max_doji_body_pct']}+w{pset['min_rejection_wick_pct']}"
    if pset.get("htf_aligned_only"):
        lbl += "+htf"
    return lbl


def _apply_fvg_param_filter(raw_signals: list, params: dict) -> list:
    min_size    = params.get("min_fvg_size_atr", 0.3)
    min_retrace = params.get("min_retrace_pct", 0.5)
    max_body    = params.get("max_doji_body_pct", 0.35)
    min_wick    = params.get("min_rejection_wick_pct", 0.0)
    htf_only    = params.get("htf_aligned_only", False)
    result = []
    for r in raw_signals:
        if (r.get("fvg_size_atr") or 0) < min_size:
            continue
        if (r.get("retrace_depth") or 0) < min_retrace:
            continue
        if (r.get("doji_body_pct") or 1) > max_body:
            continue
        if min_wick > 0 and (r.get("rejection_wick_pct") or 0) < min_wick:
            continue
        if htf_only:
            htf = (r.get("htf_bias") or "").lower()
            direction = (r.get("direction") or "").lower()
            if not htf or htf != direction:
                continue
        result.append(r)
    return result


def _run_fvg_param_sweep(raw_signals: list, db: LocalDB,
                         strategy: str = "FVG30m", update_gold: bool = False) -> tuple:
    """Evaluate FVG_PARAM_SWEEP_SETS against FVG raw signals; print comparison; store in DB."""
    all_symbols = sorted({r["symbol"] for r in raw_signals})
    logging.info("")
    logging.info("=== FVG Param Sweep (%d sets × %d pairs) ===", len(FVG_PARAM_SWEEP_SETS), len(all_symbols))
    hdr = "  ".join(f"{s[:6]:>6}" for s in all_symbols)
    logging.info("  %-36s  %s  %s  %s", "Params", hdr, "OVERAL", "EV1:2")
    logging.info("  " + "-" * 106)

    all_pair_stats: dict = {}
    pset_ids: list = []

    for pset in FVG_PARAM_SWEEP_SETS:
        pset_id = db.get_or_create_param_set(pset)
        pset_ids.append(pset_id)
        filtered = _apply_fvg_param_filter(raw_signals, pset)
        trades   = [_raw_to_trade_result(r) for r in filtered]

        for sym in all_symbols:
            s = _compute_stats([t for t in trades if t["symbol"] == sym])
            all_pair_stats[(pset_id, sym)] = s
            db.insert_scan_stats(pset_id, sym, s["total"], s["wins"], s["losses"], s["open"], json.dumps(s))

        ov = _compute_stats(trades)
        all_pair_stats[(pset_id, "ALL")] = ov
        db.insert_scan_stats(pset_id, "ALL", ov["total"], ov["wins"], ov["losses"], ov["open"], json.dumps(ov))

        lbl       = _fvg_pset_label(pset)
        pair_cols = "  ".join(f"{all_pair_stats[(pset_id, s)]['win_rate']*100:>5.1f}%" for s in all_symbols)
        ev_12     = ov.get("expected_value", {}).get("1:2", 0)
        logging.info("  v%-3d %-31s  %s  %5.1f%%  %+.3fR",
                     pset_id, lbl, pair_cols, ov["win_rate"] * 100, ev_12)

    logging.info("")
    logging.info("Best FVG param set per pair (min 8 resolved trades):")
    gold_map = db.get_gold_params(strategy)
    for sym in all_symbols:
        candidates = [
            (pid, pset) for pid, pset in zip(pset_ids, FVG_PARAM_SWEEP_SETS)
            if all_pair_stats.get((pid, sym), {}).get("resolved", 0) >= 8
        ]
        if not candidates:
            logging.info("  %-10s  (no param set with ≥8 trades)", sym)
            continue
        best_pid, best_pset = max(candidates, key=lambda x: all_pair_stats[(x[0], sym)]["win_rate"])
        s = all_pair_stats[(best_pid, sym)]
        ev_12 = s.get("expected_value", {}).get("1:2", 0)
        gold = gold_map.get(sym)
        if gold:
            delta_wr = s["win_rate"] - gold["win_rate"]
            delta_ev = ev_12 - gold["ev_1_2"]
            if update_gold:
                db.upsert_gold_params(strategy, sym, best_pid, s["win_rate"], ev_12, s["resolved"])
                gold_note = f"  [Δgold: {delta_wr*100:+.1f}pp WR  {delta_ev:+.3f}R EV] ← GOLD UPDATED"
            elif best_pid != gold["param_set_id"]:
                gold_note = f"  [Δgold: {delta_wr*100:+.1f}pp WR  {delta_ev:+.3f}R EV] ⚡ SUGGESTED"
            else:
                gold_note = f"  [Δgold: {delta_wr*100:+.1f}pp WR  {delta_ev:+.3f}R EV] ✓ unchanged"
        else:
            if update_gold:
                db.upsert_gold_params(strategy, sym, best_pid, s["win_rate"], ev_12, s["resolved"])
                gold_note = " ← GOLD SET"
            else:
                gold_note = " ⚡ SUGGESTED (no gold yet — run --update-gold to apply)"
        logging.info("  %-10s  v%-3d %-31s  WR: %5.1f%%  Trades: %d  EV 1:2: %+.3fR%s",
                     sym, best_pid, _fvg_pset_label(best_pset), s["win_rate"] * 100, s["resolved"],
                     ev_12, gold_note)
    return all_pair_stats, all_symbols, pset_ids


# ──────────────────────────────── DAX STRATEGY ────────────────────────────────

try:
    from zoneinfo import ZoneInfo as _ZI
    _ISRAEL_TZ = _ZI("Asia/Jerusalem")
except Exception:
    from datetime import timezone as _tz, timedelta as _td
    _ISRAEL_TZ = _tz(_td(hours=3))   # fallback: IDT (summer, UTC+3)


def _dax_session_window(trade_date) -> tuple:
    """Return (start_ts_utc, end_ts_utc) for 09:00-12:30 Israel time on trade_date."""
    from datetime import datetime as _dt
    start = _dt(trade_date.year, trade_date.month, trade_date.day, 9,  0,  tzinfo=_ISRAEL_TZ)
    end   = _dt(trade_date.year, trade_date.month, trade_date.day, 12, 30, tzinfo=_ISRAEL_TZ)
    return int(start.timestamp()), int(end.timestamp())


_dax_alerted_dates: set = set()


def _format_dax_alert(sig: dict) -> str:
    """Format a DAX counter-trend signal for Telegram."""
    ct_dir   = sig["direction"].upper()        # direction of our trade
    exp_dir  = sig.get("expansion_dir", "").upper()  # direction of the expansion we fade
    entry    = sig["entry"]
    sl       = sig["sl"]
    tp       = sig["tp"]
    eq       = sig.get("eq_level")
    risk     = abs(entry - sl)
    r        = round(abs(tp - entry) / risk, 1) if risk else 0.0
    ep_pct   = sig.get("entry_pct_from_origin", 0) * 100
    eq_str   = f"\nEQ: {eq:.0f}" if eq else ""
    return (
        f"[DAX] COUNTER-TREND {ct_dir} (fades {exp_dir} expansion)\n"
        f"Entry: {entry:.0f}  SL: {sl:.0f}  TP: {tp:.0f}\n"
        f"R: 1:{r}  Entry zone: {ep_pct:.0f}% from origin{eq_str}"
    )


def dax_data_job(db: "LocalDB") -> None:
    """Fetch and store DAX 15m + 5m candles from Yahoo Finance.

    Runs every 5 min during full Frankfurt hours (09:00-17:30 IDT / 07:00-15:30 UTC)
    so post-session data is available for any future invalidation logic.
    """
    from datetime import time as _time
    from data.yahoo_fetcher import fetch_yahoo

    now_il = datetime.now(_ISRAEL_TZ)
    if now_il.weekday() >= 5:
        return
    t = now_il.time()
    # Frankfurt closes 17:30 CEST = 18:30 IDT; fetch until 18:45 to capture the close bar
    if not (_time(9, 0) <= t <= _time(18, 45)):
        return

    _dlog.info("[DAX_DATA] START | time_il=%s | fetching 15m+5m from Yahoo", now_il.strftime("%H:%M IDT"))
    try:
        candles_15m = fetch_yahoo("DAX", "15m", days=7)
        candles_5m  = fetch_yahoo("DAX", "5m",  days=5)
    except Exception as exc:
        _dlog.error("[DAX_DATA] ERROR | Yahoo fetch failed: %s", exc)
        logging.error("DAX data job: Yahoo fetch failed: %s", exc)
        return

    if candles_15m:
        db.insert_candles("DAX", "15m", candles_15m)
    if candles_5m:
        db.insert_candles("DAX", "5m", candles_5m)
    _dlog.info("[DAX_DATA] stored %d 15m + %d 5m candles",
               len(candles_15m or []), len(candles_5m or []))
    logging.debug("DAX data job: stored %d 15m + %d 5m candles", len(candles_15m or []), len(candles_5m or []))


def dax_session_job(settings: "Settings", db: "LocalDB", alert_manager: "AlertManager") -> None:
    """Check for DAX counter-trend setup; runs every 5 min during Frankfurt session."""
    from datetime import date as _date
    from analysis.smc_analyzer import SMCAnalyzer

    now_il  = datetime.now(_ISRAEL_TZ)
    today   = now_il.date()

    # Skip weekends
    if today.weekday() >= 5:
        return

    # Only within session window 09:00-12:30 Israel time
    t = now_il.time()
    from datetime import time as _time
    if not (_time(9, 0) <= t <= _time(12, 30)):
        return

    _dlog.info("[DAX_SESSION] %s | within_session | checking for setup", now_il.strftime("%H:%M IDT"))

    # One alert per session day
    if today in _dax_alerted_dates:
        _dlog.info("[DAX_SESSION] already_alerted today | skip")
        return

    # Load gold params
    gold_map = db.get_gold_params("DAX")
    gold = gold_map.get("DAX") or gold_map.get("dax")
    if not gold:
        _dlog.warning("[DAX_SESSION] no gold params | run --experiment-dax --update-gold")
        logging.warning("DAX session job: no gold params — run --experiment-dax --update-gold first")
        return
    gold_params = db.get_param_set_by_id(gold["param_set_id"])

    # Read candles from DB (kept fresh by dax_data_job every 5 min)
    start_ts, end_ts = _dax_session_window(today)
    candles_15m = db.query_recent("DAX", "15m", limit=200)
    candles_5m  = db.query_recent("DAX", "5m",  limit=500)

    if not candles_15m or not candles_5m:
        _dlog.warning("[DAX_SESSION] no candles in DB | 15m=%d 5m=%d",
                      len(candles_15m or []), len(candles_5m or []))
        return

    sess_15m = [c for c in candles_15m if start_ts <= c["timestamp"] <= end_ts]
    if len(sess_15m) < 3:
        _dlog.info("[DAX_SESSION] insufficient session candles (%d<3) | waiting", len(sess_15m))
        return

    # Last 16 pre-session 15m candles for context
    pre_15m = sorted(
        [c for c in candles_15m if c["timestamp"] < start_ts],
        key=lambda c: c["timestamp"],
    )[-16:]

    # All 5m candles for today
    day_5m = sorted(
        [c for c in candles_5m if c["timestamp"] >= start_ts],
        key=lambda c: c["timestamp"],
    )

    _dlog.info("[DAX_SESSION] detect_setup | sess_15m=%d day_5m=%d pre_15m=%d | param_set_id=%s",
               len(sess_15m), len(day_5m), len(pre_15m), gold.get("param_set_id", "?"))

    analyzer = SMCAnalyzer()
    try:
        sigs = analyzer.detect_dax_session_setup(
            sess_15m, day_5m,
            params={**gold_params, "symbol": "DAX"},
            candles_15m_presession=pre_15m,
        )
    except Exception as exc:
        _dlog.error("[DAX_SESSION] detection_failed | %s", exc)
        logging.error("DAX session job: detection failed: %s", exc)
        return

    if not sigs:
        _dlog.info("[DAX_SESSION] no_setup_found")
        return

    sig = sigs[0]
    _dax_alerted_dates.add(today)

    _dlog.info("[DAX_SESSION] SIGNAL | dir=%s entry=%.0f sl=%.0f tp=%.0f | alerts_enabled=%s",
               sig.get("direction", "?"), sig.get("entry", 0), sig.get("sl", 0), sig.get("tp", 0),
               getattr(settings, "dax_alerts_enabled", False))

    msg = _format_dax_alert(sig)
    logging.info("DAX SIGNAL: %s", msg)
    try:
        chart_path = alert_manager.render_dax_alert(sig, day_5m, outcome="OPEN")
    except Exception as exc:
        logging.warning("DAX chart render failed: %s", exc)
        chart_path = None
    if getattr(settings, "dax_alerts_enabled", False):
        alert_manager.send_alert({"message": msg, "image_path": chart_path, "alert_id": sig.get("signal_id", "dax")})
    else:
        _dlog.info("[DAX_SESSION] alert_suppressed (dax_alerts_enabled=false) | chart=%s", chart_path)
        logging.info("DAX alert suppressed (dax_alerts_enabled=false) — chart saved to %s", chart_path)


def _evaluate_dax_outcome(signal: dict, candles_5m_after_entry: list) -> tuple:
    """Return (outcome, eff_r) for a DAX signal. outcome = WIN/LOSS/OPEN."""
    tp      = signal["tp"]
    sl      = signal["sl"]
    entry   = signal["entry"]
    bullish = signal["direction"] == "bullish"
    risk    = signal["risk"] or abs(tp - sl) or 1.0
    outcome = "OPEN"
    for bar in candles_5m_after_entry:
        if bullish:
            if bar["low"]  <= sl: outcome = "LOSS"; break
            if bar["high"] >= tp: outcome = "WIN";  break
        else:
            if bar["high"] >= sl: outcome = "LOSS"; break
            if bar["low"]  <= tp: outcome = "WIN";  break
    if "WIN" in outcome:
        eff_r = round(abs(tp - entry) / risk, 2)
    elif "LOSS" in outcome:
        eff_r = 1.0
    else:
        eff_r = None
    return outcome, eff_r


_DAX_SWEEP_BASE = {
    "tp_pct":             0.5,   # TP at 50% of expansion = eq_level (mean reversion)
    "sl_atr_mult":        0.5,   # SL buffer beyond signal candle extreme
    "min_expansion_atr":  1.0,   # minimum expansion size in ATR multiples
    "entry_zone_min_pct": 0.5,   # signal must fire at ≥ 50% from origin (at/above eq)
    "swing_lookback_5m":  12,
    "min_break_str_5m":   0.3,
    # loss-reduction filters (from analysis 2026-06-07)
    "exclude_dow":        [],    # e.g. [0,4] to skip Mon+Fri
    "bearish_only":       False, # True = only fade bearish expansions (→ LONG)
}

DAX_PARAM_SWEEP_SETS = [
    # baseline
    {**_DAX_SWEEP_BASE},
    # vary TP depth (distance from origin to target)
    {**_DAX_SWEEP_BASE, "tp_pct": 0.3},   # ambitious — further into retracement
    {**_DAX_SWEEP_BASE, "tp_pct": 0.4},
    {**_DAX_SWEEP_BASE, "tp_pct": 0.6},   # conservative — barely past eq
    # vary SL tightness
    {**_DAX_SWEEP_BASE, "sl_atr_mult": 0.3},
    {**_DAX_SWEEP_BASE, "sl_atr_mult": 0.7},
    {**_DAX_SWEEP_BASE, "sl_atr_mult": 1.0},
    {**_DAX_SWEEP_BASE, "sl_atr_mult": 1.5},
    # entry zone — how deep into premium zone must signal fire
    {**_DAX_SWEEP_BASE, "entry_zone_min_pct": 0.6},
    {**_DAX_SWEEP_BASE, "entry_zone_min_pct": 0.7},
    {**_DAX_SWEEP_BASE, "entry_zone_min_pct": 0.8},
    # expansion size filter
    {**_DAX_SWEEP_BASE, "min_expansion_atr": 1.5},
    {**_DAX_SWEEP_BASE, "min_expansion_atr": 2.0},
    # combined candidates — tp0.5 variants
    {**_DAX_SWEEP_BASE, "tp_pct": 0.4, "sl_atr_mult": 0.3},
    {**_DAX_SWEEP_BASE, "tp_pct": 0.4, "entry_zone_min_pct": 0.6},
    {**_DAX_SWEEP_BASE, "tp_pct": 0.5, "sl_atr_mult": 0.3, "entry_zone_min_pct": 0.6},
    {**_DAX_SWEEP_BASE, "tp_pct": 0.4, "sl_atr_mult": 0.3, "entry_zone_min_pct": 0.6},
    # combined candidates — tp0.6 variants (winner tier)
    {**_DAX_SWEEP_BASE, "tp_pct": 0.6, "sl_atr_mult": 0.7},
    {**_DAX_SWEEP_BASE, "tp_pct": 0.6, "sl_atr_mult": 1.0},
    {**_DAX_SWEEP_BASE, "tp_pct": 0.6, "entry_zone_min_pct": 0.6},
    {**_DAX_SWEEP_BASE, "tp_pct": 0.6, "entry_zone_min_pct": 0.7},
    {**_DAX_SWEEP_BASE, "tp_pct": 0.6, "entry_zone_min_pct": 0.8},
    {**_DAX_SWEEP_BASE, "tp_pct": 0.6, "sl_atr_mult": 0.7, "entry_zone_min_pct": 0.7},
    # ── Tier: day-of-week filter (Mon=0, Fri=4 excluded) ─────────────────
    {**_DAX_SWEEP_BASE, "exclude_dow": [0, 4]},
    {**_DAX_SWEEP_BASE, "exclude_dow": [0]},
    {**_DAX_SWEEP_BASE, "exclude_dow": [4]},
    # ── Tier: bearish-only (fade bearish expansions → LONG only) ─────────
    {**_DAX_SWEEP_BASE, "bearish_only": True},
    {**_DAX_SWEEP_BASE, "bearish_only": True, "tp_pct": 0.6},
    {**_DAX_SWEEP_BASE, "bearish_only": True, "entry_zone_min_pct": 0.7},
    # ── Tier: combined — no Mon/Fri + bearish only ────────────────────────
    {**_DAX_SWEEP_BASE, "exclude_dow": [0, 4], "bearish_only": True},
    {**_DAX_SWEEP_BASE, "exclude_dow": [0, 4], "bearish_only": True, "tp_pct": 0.6},
    {**_DAX_SWEEP_BASE, "exclude_dow": [0, 4], "bearish_only": True, "tp_pct": 0.6, "entry_zone_min_pct": 0.7},
    {**_DAX_SWEEP_BASE, "exclude_dow": [0, 4], "bearish_only": True, "tp_pct": 0.6, "sl_atr_mult": 0.7},
    {**_DAX_SWEEP_BASE, "exclude_dow": [0, 4], "bearish_only": True, "tp_pct": 0.6, "sl_atr_mult": 0.7, "entry_zone_min_pct": 0.7},
]


def _dax_pset_label(pset: dict) -> str:
    parts = [f"tp{pset['tp_pct']}"]
    if pset.get("sl_atr_mult", 0.5) != 0.5:
        parts.append(f"sl{pset['sl_atr_mult']}")
    if pset.get("entry_zone_min_pct", 0.5) != 0.5:
        parts.append(f"ez{pset['entry_zone_min_pct']}")
    if pset.get("min_expansion_atr", 1.0) != 1.0:
        parts.append(f"exp{pset['min_expansion_atr']}")
    if pset.get("min_break_str_5m", 0.3) != 0.3:
        parts.append(f"str{pset['min_break_str_5m']}")
    if pset.get("swing_lookback_5m", 12) != 12:
        parts.append(f"lb{pset['swing_lookback_5m']}")
    dow = pset.get("exclude_dow", [])
    if dow:
        _day = {0:"mo",1:"tu",2:"we",3:"th",4:"fr"}
        parts.append("no" + "".join(_day[d] for d in sorted(dow)))
    if pset.get("bearish_only"):
        parts.append("bearonly")
    return "+".join(parts)


def run_dax_experiment(
    settings: Settings,
    db: LocalDB,
    alert_manager: AlertManager,
    scan_days: int = 365,
    update_gold: bool = False,
) -> None:
    """Scan DE40 for DAX Frankfurt open session setups; run param sweep."""
    from datetime import timedelta, date as _date
    from analysis.smc_analyzer import SMCAnalyzer

    symbol = "DAX"
    strategy = "DAX"
    cutoff_ts = int((datetime.now(timezone.utc) - timedelta(days=scan_days)).timestamp())

    logging.info("Scanning DAX session setups via Yahoo Finance (last %d days)", scan_days)
    logging.info("Session window: 09:00-12:30 Israel time  |  Cutoff: %s UTC",
                 datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).strftime("%Y-%m-%d"))

    # Fetch via Yahoo Finance and store in DB
    from data.yahoo_fetcher import fetch_yahoo
    for tf in ("15m", "5m"):
        existing = db.query_recent(symbol, tf, limit=1)
        if existing:
            age_hours = (datetime.now(timezone.utc).timestamp() - existing[0]["timestamp"]) / 3600
            if age_hours < 24:
                logging.info("DAX %s: using cached data (%.0fh old)", tf, age_hours)
                continue
        logging.info("Fetching DAX %s from Yahoo Finance...", tf)
        try:
            candles = fetch_yahoo(symbol, tf, days=scan_days)
            db.insert_candles(symbol, tf, candles)
            logging.info("  → stored %d %s candles", len(candles), tf)
        except Exception as exc:
            logging.error("Failed to fetch DAX %s from Yahoo: %s", tf, exc)

    candles_15m_desc = db.query_recent(symbol, "15m", limit=_candle_limit("15m", days=scan_days))
    candles_5m_desc  = db.query_recent(symbol, "5m",  limit=_candle_limit("5m",  days=scan_days))

    if not candles_15m_desc or not candles_5m_desc:
        logging.error("No DAX data in DB after fetch attempt.")
        return

    # Convert to chronological (oldest→newest) and filter to scan period
    c15m = [c for c in reversed(candles_15m_desc) if c["timestamp"] >= cutoff_ts]
    c5m  = [c for c in reversed(candles_5m_desc)  if c["timestamp"] >= cutoff_ts]
    logging.info("Loaded %d 15m and %d 5m candles", len(c15m), len(c5m))

    # Get unique trading dates
    trading_dates = sorted(set(
        datetime.fromtimestamp(c["timestamp"], tz=timezone.utc).date()
        for c in c15m
    ))
    logging.info("Trading dates to scan: %d", len(trading_dates))

    scan_run_id = int(datetime.now(timezone.utc).timestamp())
    analyzer    = SMCAnalyzer()
    all_raw: list = []

    # Pre-collect per-session data once; reused by base scan and all sweep iterations
    sessions_data: list = []
    for trade_date in trading_dates:
        if trade_date.weekday() >= 5:
            continue
        start_ts, end_ts = _dax_session_window(trade_date)
        lookahead_end    = end_ts + 6 * 3600
        sess_15m = [c for c in c15m if start_ts <= c["timestamp"] <= end_ts]
        day_5m   = [c for c in c5m  if start_ts <= c["timestamp"] <= lookahead_end]
        pre_15m  = [c for c in c15m if c["timestamp"] < start_ts][-16:]
        if len(sess_15m) >= 3:
            sessions_data.append((start_ts, end_ts, sess_15m, day_5m, pre_15m))

    # Base scan using _DAX_SWEEP_BASE params
    base_params = {**_DAX_SWEEP_BASE, "symbol": symbol}
    base_wins: list = []
    base_losses: int = 0
    base_opens: int  = 0

    for start_ts, end_ts, sess_15m, day_5m, pre_15m in sessions_data:
        signals = analyzer.detect_dax_session_setup(
            sess_15m, day_5m, params=base_params,
            candles_15m_presession=pre_15m,
        )
        for sig in signals:
            entry_ts      = sig["breakout_ts"]
            post_entry_5m = [c for c in day_5m if c["timestamp"] > entry_ts]
            outcome, eff_r = _evaluate_dax_outcome(sig, post_entry_5m)

            bos_dt   = datetime.fromtimestamp(entry_ts, tz=timezone.utc)
            alert_id = f"dax-{bos_dt.minute:02d}-{bos_dt.hour:02d}-{bos_dt.day:02d}-{bos_dt.month:02d}-{bos_dt.year}"

            if outcome == "WIN":
                base_wins.append(eff_r or 2.0)
            elif outcome == "LOSS":
                base_losses += 1
            else:
                base_opens += 1

            all_raw.append({
                "symbol":                symbol,
                "timeframe":             "15m",
                "breakout_ts":           entry_ts,
                "alert_id":              alert_id,
                "direction":             sig["direction"],
                "broken_level":          sig["eq_level"],
                "break_strength":        sig["retrace_depth_pct"],
                "htf_bias":              None,
                "confluences":           json.dumps([]),
                "outcome":               outcome,
                "hour":                  bos_dt.hour,
                "month":                 bos_dt.strftime("%Y-%m"),
                "has_liquidity_sweep":   0,
                "swing_age_candles":     None,
                "session":               "frankfurt",
                "dow":                   bos_dt.weekday(),
                "strategy":              strategy,
                "scan_run_id":           scan_run_id,
                "fvg_size_atr":          sig["expansion_range"],
                "retrace_depth":         sig["retrace_depth_pct"],
                "doji_body_pct":         None,
                "outcome_broken_level":  None,
                "outcome_break_candle":  None,
                "eff_r_broken_level":    None,
                "eff_r_break_candle":    None,
            })

    logging.info("DAX raw signals: %d", len(all_raw))
    if not all_raw:
        logging.warning("No DAX counter-trend setups found — check session window or expansion params")
        return

    try:
        db.insert_raw_signals(scan_run_id, all_raw)
    except Exception as exc:
        logging.warning("Failed to store DAX raw signals: %s", exc)

    # Base stats using actual variable-R
    b_res = len(base_wins) + base_losses
    b_wr  = len(base_wins) / b_res if b_res else 0
    b_avg_r = sum(base_wins) / len(base_wins) if base_wins else 2.0
    b_ev  = round(b_avg_r * b_wr - 1.0 * (1 - b_wr), 4) if b_res else -1.0
    logging.info("")
    logging.info("=== DAX Raw Scan Stats (base params) ===")
    logging.info("  Total: %d  Resolved: %d  WR: %.1f%%  avgR: %.2f  EV: %+.3fR",
                 len(all_raw), b_res, b_wr * 100, b_avg_r, b_ev)
    logging.info("  Wins: %d  Losses: %d  Open: %d", len(base_wins), base_losses, base_opens)

    # Param sweep — re-run detector for each pset to get correct SL/TP/outcome
    logging.info("")
    logging.info("=== DAX Param Sweep (%d sets) ===", len(DAX_PARAM_SWEEP_SETS))
    logging.info("  %-35s  %5s  %5s  %5s  %5s", "Params", "WR%", "avgR", "EV", "n")
    logging.info("  " + "-" * 60)

    all_pset_stats: dict = {}
    pset_ids_dax: list = []
    gold_map = db.get_gold_params(strategy)

    for pset in DAX_PARAM_SWEEP_SETS:
        pset_id = db.get_or_create_param_set(pset)
        pset_ids_dax.append(pset_id)

        pset_params = {**pset, "symbol": symbol}
        p_wins: list = []; p_losses = 0; p_opens = 0

        excl_dow    = set(pset.get("exclude_dow", []))
        bearish_only = pset.get("bearish_only", False)

        for start_ts, end_ts, sess_15m, day_5m, pre_15m in sessions_data:
            sess_dow = datetime.fromtimestamp(start_ts, tz=timezone.utc).weekday()
            if sess_dow in excl_dow:
                continue
            sigs = analyzer.detect_dax_session_setup(
                sess_15m, day_5m, params=pset_params,
                candles_15m_presession=pre_15m,
            )
            for sig in sigs:
                if bearish_only and sig.get("direction") != "bearish":
                    continue
                post_5m = [c for c in day_5m if c["timestamp"] > sig["breakout_ts"]]
                outcome, eff_r = _evaluate_dax_outcome(sig, post_5m)
                if outcome == "WIN":
                    p_wins.append(eff_r or 2.0)
                elif outcome == "LOSS":
                    p_losses += 1
                else:
                    p_opens += 1

        p_res   = len(p_wins) + p_losses
        p_wr    = len(p_wins) / p_res if p_res else 0
        p_avg_r = sum(p_wins) / len(p_wins) if p_wins else 2.0
        p_ev    = round(p_avg_r * p_wr - 1.0 * (1 - p_wr), 4) if p_res else -1.0
        s = {
            "total": len(p_wins) + p_losses + p_opens,
            "wins": len(p_wins), "losses": p_losses, "open": p_opens,
            "resolved": p_res, "win_rate": p_wr,
            "avg_r_win": p_avg_r, "ev_variable_r": p_ev,
        }
        all_pset_stats[pset_id] = s
        db.insert_scan_stats(pset_id, symbol, s["total"], s["wins"], s["losses"], s["open"], json.dumps(s))

        logging.info("  v%-3d %-31s  %4.1f%%  %4.2f  %+.3fR  n=%d",
                     pset_id, _dax_pset_label(pset), p_wr * 100, p_avg_r, p_ev, p_res)

    # Best param set
    best_pid = max(pset_ids_dax,
                   key=lambda pid: all_pset_stats[pid].get("ev_variable_r", -999))
    best_pset = DAX_PARAM_SWEEP_SETS[pset_ids_dax.index(best_pid)]
    bs = all_pset_stats[best_pid]
    ev_best = bs.get("ev_variable_r", 0)
    gold = gold_map.get(symbol)

    if gold:
        delta_wr = bs["win_rate"] - gold["win_rate"]
        delta_ev = ev_best - gold["ev_1_2"]
        gold_note = f"  [Δgold: {delta_wr*100:+.1f}pp WR  {delta_ev:+.3f}R EV]"
        if update_gold:
            db.upsert_gold_params(strategy, symbol, best_pid, bs["win_rate"], ev_best, bs["resolved"])
            gold_note += " ← GOLD UPDATED"
    else:
        db.upsert_gold_params(strategy, symbol, best_pid, bs["win_rate"], ev_best, bs["resolved"])
        gold_note = " ← GOLD SET"

    logging.info("")
    logging.info("Best DAX params: v%-3d %-31s  WR: %.1f%%  avgR: %.2f  Trades: %d  EV: %+.3fR%s",
                 best_pid, _dax_pset_label(best_pset),
                 bs["win_rate"] * 100, bs.get("avg_r_win", 2.0), bs["resolved"], ev_best, gold_note)

    # Render charts for all signals under gold (best) params
    logging.info("")
    logging.info("Rendering DAX charts with gold params (v%d)...", best_pid)
    gold_params_chart = {**best_pset, "symbol": symbol}
    chart_count = 0
    for start_ts, end_ts, sess_15m, day_5m, pre_15m in sessions_data:
        sigs = analyzer.detect_dax_session_setup(
            sess_15m, day_5m, params=gold_params_chart,
            candles_15m_presession=pre_15m,
        )
        for sig in sigs:
            post_5m = [c for c in day_5m if c["timestamp"] > sig["breakout_ts"]]
            outcome, _ = _evaluate_dax_outcome(sig, post_5m)
            try:
                alert_manager.render_dax_alert(sig, day_5m, outcome=outcome)
                chart_count += 1
            except Exception as exc:
                logging.warning("DAX chart render failed: %s", exc)
    logging.info("DAX charts saved: %d  →  %s", chart_count, str(alert_manager.charts_dir))


def _fvg_alert_id(symbol: str, timestamp: int) -> str:
    dt   = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    pair = symbol.lower().replace("/", "")
    return f"fvg_{dt.minute:02d}-{dt.hour:02d}-{dt.day:02d}-{dt.month:02d}-{dt.year}-{pair}"


# ── Liquidity Sweep strategy ──────────────────────────────────────────────────

_LIQ_SWEEP_BASE = {
    "eq_tolerance_atr":       0.20,
    "swing_lookback":         30,
    "min_swing_strength":     2,
    "min_sweep_atr":          0.0,
    "min_rejection_wick_pct": 0.0,
    "use_pdh_pdl":            True,
    "use_eq_pools":           True,
    "sl_buffer_atr":          0.1,
    "rr":                     2.0,
    "scan_days":              365,
}

_LIQ_LDN  = [7, 8, 9]       # London open kill zone hours (UTC)
_LIQ_NY   = [13, 14, 15]    # NY open kill zone hours (UTC)
_LIQ_BOTH = _LIQ_LDN + _LIQ_NY

LIQ_PARAM_SWEEP_SETS = [
    # ── Tier 0: baseline ──────────────────────────────────────────────────
    {**_LIQ_SWEEP_BASE},
    {**_LIQ_SWEEP_BASE, "htf_aligned_only": True},
    # ── Tier 1: kill zone gate ────────────────────────────────────────────
    {**_LIQ_SWEEP_BASE, "kill_zone_hours": _LIQ_LDN},
    {**_LIQ_SWEEP_BASE, "kill_zone_hours": _LIQ_NY},
    {**_LIQ_SWEEP_BASE, "kill_zone_hours": _LIQ_BOTH},
    {**_LIQ_SWEEP_BASE, "kill_zone_hours": _LIQ_LDN,  "htf_aligned_only": True},
    {**_LIQ_SWEEP_BASE, "kill_zone_hours": _LIQ_NY,   "htf_aligned_only": True},
    {**_LIQ_SWEEP_BASE, "kill_zone_hours": _LIQ_BOTH, "htf_aligned_only": True},
    # ── Tier 2: touch count (pool quality) ────────────────────────────────
    {**_LIQ_SWEEP_BASE, "min_pool_touches": 3},
    {**_LIQ_SWEEP_BASE, "min_pool_touches": 3, "htf_aligned_only": True},
    {**_LIQ_SWEEP_BASE, "min_pool_touches": 3, "kill_zone_hours": _LIQ_BOTH},
    {**_LIQ_SWEEP_BASE, "min_pool_touches": 3, "kill_zone_hours": _LIQ_BOTH, "htf_aligned_only": True},
    # ── Tier 3: pool freshness ────────────────────────────────────────────
    {**_LIQ_SWEEP_BASE, "max_pool_age_bars": 20},
    {**_LIQ_SWEEP_BASE, "max_pool_age_bars": 10},
    {**_LIQ_SWEEP_BASE, "max_pool_age_bars": 20, "htf_aligned_only": True},
    {**_LIQ_SWEEP_BASE, "max_pool_age_bars": 20, "kill_zone_hours": _LIQ_BOTH},
    # ── Tier 4: combined best candidates ─────────────────────────────────
    {**_LIQ_SWEEP_BASE, "min_pool_touches": 3, "max_pool_age_bars": 20, "kill_zone_hours": _LIQ_BOTH},
    {**_LIQ_SWEEP_BASE, "min_pool_touches": 3, "max_pool_age_bars": 20, "kill_zone_hours": _LIQ_BOTH, "htf_aligned_only": True},
    {**_LIQ_SWEEP_BASE, "min_pool_touches": 3, "max_pool_age_bars": 20, "kill_zone_hours": _LIQ_LDN,  "htf_aligned_only": True},
    {**_LIQ_SWEEP_BASE, "min_pool_touches": 3, "max_pool_age_bars": 20, "kill_zone_hours": _LIQ_NY,   "htf_aligned_only": True},
]


def _liq_pset_label(pset: dict) -> str:
    parts = []
    sw = pset.get("min_sweep_atr", 0.0)
    if sw > 0:
        parts.append(f"sw{sw}")
    wk = pset.get("min_rejection_wick_pct", 0.0)
    if wk > 0:
        parts.append(f"w{wk}")
    pt = pset.get("pool_types")
    if pt:
        parts.append("+".join(pt))
    mt = pset.get("min_pool_touches", 0)
    if mt > 2:
        parts.append(f"t{mt}")
    age = pset.get("max_pool_age_bars")
    if age is not None:
        parts.append(f"age{age}")
    kz = pset.get("kill_zone_hours")
    if kz:
        if set(kz) == set(_LIQ_LDN):
            parts.append("ldn")
        elif set(kz) == set(_LIQ_NY):
            parts.append("ny")
        else:
            parts.append("kz")
    if pset.get("htf_aligned_only"):
        parts.append("htf")
    return "+".join(parts) if parts else "all"


def _apply_liq_param_filter(raw_signals: list, params: dict) -> list:
    min_sweep   = params.get("min_sweep_atr", 0.0)
    min_wick    = params.get("min_rejection_wick_pct", 0.0)
    pool_types  = params.get("pool_types")
    htf_only    = params.get("htf_aligned_only", False)
    min_touches = params.get("min_pool_touches", 0)
    max_age     = params.get("max_pool_age_bars")
    kz_hours    = params.get("kill_zone_hours")
    result = []
    for r in raw_signals:
        if (r.get("fvg_size_atr") or 0) < min_sweep:
            continue
        if min_wick > 0 and (r.get("rejection_wick_pct") or 0) < min_wick:
            continue
        if pool_types and r.get("pool_type") not in pool_types:
            continue
        if min_touches > 2 and (r.get("swing_test_count") or 0) < min_touches:
            continue
        if max_age is not None and (r.get("swing_age_candles") or 9999) > max_age:
            continue
        if kz_hours is not None and r.get("hour") not in kz_hours:
            continue
        if htf_only:
            htf = (r.get("htf_bias") or "").lower()
            direction = (r.get("direction") or "").lower()
            if not htf or htf != direction:
                continue
        result.append(r)
    return result


def _run_liq_param_sweep(raw_signals: list, db: LocalDB, strategy: str,
                         update_gold: bool = False) -> None:
    logging.info("")
    logging.info("=== LIQ Param Sweep (%d sets) ===", len(LIQ_PARAM_SWEEP_SETS))
    logging.info("  %-38s  %5s  %5s  %6s", "Params", "n", "WR%", "EV 1:2")
    logging.info("  " + "-" * 62)

    pset_ids   = []
    pset_stats = {}

    for pset in LIQ_PARAM_SWEEP_SETS:
        pset_id = db.get_or_create_param_set(pset)
        pset_ids.append(pset_id)

        filtered = _apply_liq_param_filter(raw_signals, pset)
        trades   = [{"alert_id": r["alert_id"], "outcome": r["outcome"],
                     "confluences": [], "symbol": r["symbol"],
                     "hour": r["hour"], "month": r["month"],
                     "break_strength": r.get("fvg_size_atr", 0),
                     "r_win": 2.0,
                     "htf_bias": r.get("htf_bias"),
                     "bos_direction": r["direction"]}
                    for r in filtered]

        stats       = _compute_stats(trades)
        ev_12       = stats.get("expected_value", {}).get("1:2", 0)
        wr          = stats.get("win_rate", 0)
        n           = stats.get("resolved", 0)
        pset_stats[pset_id] = stats
        label = _liq_pset_label(pset)
        logging.info("  %-38s  %4d  %4.1f%%  %+.3fR", label, n, wr * 100, ev_12)

    if not pset_ids:
        return

    best_pid = max(pset_ids,
                   key=lambda pid: pset_stats[pid].get("expected_value", {}).get("1:2", -999))
    best_s   = pset_stats[best_pid]
    best_ev  = best_s.get("expected_value", {}).get("1:2", 0)
    best_label = _liq_pset_label(LIQ_PARAM_SWEEP_SETS[pset_ids.index(best_pid)])
    logging.info("")
    logging.info("Best LIQ params: v%-3d %-38s  WR: %.1f%%  n: %d  EV: %+.3fR",
                 best_pid, best_label, best_s["win_rate"] * 100, best_s["resolved"], best_ev)


def _liq_alert_id(symbol: str, timestamp: int, pool_type: str) -> str:
    dt   = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    pair = symbol.lower().replace("/", "")
    return f"liq_{pool_type.lower()}_{dt.minute:02d}-{dt.hour:02d}-{dt.day:02d}-{dt.month:02d}-{dt.year}-{pair}"


def _scan_one_symbol_liq(args: tuple) -> tuple:
    """Worker: scan liquidity sweep setups on given timeframe. Returns raw signal records."""
    symbol, settings, param_set_id, cutoff_ts, scan_run_id, timeframe, htf = args

    from db.local_db import LocalDB
    from analysis.smc_analyzer import SMCAnalyzer
    from alerts.alert_manager import AlertManager

    db       = LocalDB(settings.db_path)
    analyzer = SMCAnalyzer()
    dev_mode = settings.dev_mode

    candles_desc = db.query_recent(symbol, timeframe, limit=_candle_limit(timeframe))
    if not candles_desc:
        db.close()
        return symbol, 0, [], [], []

    candles_htf = db.query_recent(symbol, htf, limit=_candle_limit(htf))
    n = len(candles_desc)
    seen: set = set()
    trade_results, log_lines, raw_signal_records = [], [], []

    for k in range(50, n):
        bos_ts = candles_desc[n - 1 - k]["timestamp"]
        if bos_ts < cutoff_ts:
            continue

        idx       = n - 1 - k
        det_slice = candles_desc[idx : idx + 50]
        lookahead = candles_desc[max(0, idx - 51) : idx] if dev_mode else None

        liq_events = analyzer.detect_liquidity_sweep(
            det_slice, params={"symbol": symbol, "timeframe": timeframe}
        )
        if not liq_events:
            continue

        htf_bias        = analyzer.get_htf_bias(candles_htf, bos_ts) if candles_htf else None
        lookahead_chron = list(reversed(lookahead)) if lookahead else []
        sig_dt          = datetime.fromtimestamp(bos_ts, tz=timezone.utc)

        for ev in liq_events:
            key = (ev["direction"], ev["pool_type"], round(ev["pool_level"], 5))
            if key in seen:
                continue
            seen.add(key)

            outcome  = AlertManager.evaluate_liq_outcome(
                det_slice, ev, lookahead_chron, candles_htf
            )
            alert_id = _liq_alert_id(symbol, ev["breakout_ts"], ev["pool_type"])

            raw_signal_records.append({
                "symbol":              symbol,
                "timeframe":           timeframe,
                "breakout_ts":         ev["breakout_ts"],
                "alert_id":            alert_id,
                "direction":           ev["direction"],
                "broken_level":        ev["pool_level"],
                "break_strength":      ev.get("sweep_size_atr", 0.0),
                "htf_bias":            htf_bias,
                "confluences":         json.dumps([]),
                "outcome":             outcome,
                "hour":                sig_dt.hour,
                "month":               sig_dt.strftime("%Y-%m"),
                "has_liquidity_sweep": 1,
                "swing_age_candles":   None,
                "session":             _hour_to_session(sig_dt.hour),
                "dow":                 sig_dt.weekday(),
                "fvg_size_atr":        ev.get("sweep_size_atr"),
                "retrace_depth":       None,
                "doji_body_pct":       None,
                "rejection_wick_pct":  ev.get("rejection_wick_pct"),
                "pool_type":           ev["pool_type"],
                "swing_test_count":    ev.get("pool_touch_count"),
                "swing_age_candles":   ev.get("pool_age_bars"),
                "strategy":            f"LIQ{timeframe}",
                "scan_run_id":         scan_run_id,
            })
            trade_results.append({
                "alert_id":       alert_id,
                "outcome":        outcome,
                "confluences":    [],
                "r_win":          2.0,
                "symbol":         symbol,
                "hour":           sig_dt.hour,
                "month":          sig_dt.strftime("%Y-%m"),
                "break_strength": ev.get("sweep_size_atr", 0.0),
                "htf_bias":       htf_bias,
                "bos_direction":  ev["direction"],
            })
            log_lines.append(
                f"  [{symbol}] LIQ {ev['direction'].upper()}"
                f"  {ev['pool_type']}@{ev['pool_level']:.5f}"
                f"  sweep={ev['sweep_size_atr']:.2f}ATR  wick={ev['rejection_wick_pct']:.0%}"
                f"  4H={( htf_bias or '?').upper():<8}  ts={sig_dt.strftime('%Y-%m-%d %H:%M')}"
            )

    db.close()
    return symbol, len(raw_signal_records), trade_results, log_lines, raw_signal_records


def run_liq_experiment(settings: Settings, db: LocalDB, alert_manager: AlertManager,
                       update_gold: bool = False, timeframe: str = "30m") -> None:
    """Scan liquidity sweep setups on given timeframe; run param sweep."""
    from datetime import timedelta
    htf       = "4h"
    strategy  = f"LIQ{timeframe}"
    cutoff_ts = int((datetime.now(timezone.utc) - timedelta(days=365)).timestamp())

    logging.info("Scanning %s Liquidity Sweep (last 365 days) — HTF bias from %s", timeframe, htf)
    logging.info("Cutoff: %s UTC", datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"))

    param_set_id = db.get_or_create_param_set(_LIQ_SWEEP_BASE)
    scan_run_id  = int(datetime.now(timezone.utc).timestamp())
    n_workers    = min(len(settings.fx_pairs), max(1, (os.cpu_count() or 4) // 2))
    worker_args  = [
        (symbol, settings, param_set_id, cutoff_ts, scan_run_id, timeframe, htf)
        for symbol in settings.fx_pairs
    ]
    logging.info("Launching %d LIQ workers (%s)...", n_workers, timeframe)

    pair_counts: dict = {}
    all_trade_results: list = []
    all_raw_signals:   list = []

    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        futures = {executor.submit(_scan_one_symbol_liq, args): args[0] for args in worker_args}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                sym, count, trade_results, log_lines, raw_signal_recs = future.result()
            except Exception as exc:
                logging.error("LIQ worker for %s failed: %s", symbol, exc)
                pair_counts[symbol] = 0
                continue
            pair_counts[sym] = count
            all_trade_results.extend(trade_results)
            all_raw_signals.extend(raw_signal_recs)
            for line in log_lines:
                logging.info(line)

    try:
        db.insert_raw_signals(scan_run_id, all_raw_signals)
        logging.info("LIQ raw signals stored: %d (scan_run_id=%d)", len(all_raw_signals), scan_run_id)
    except Exception as exc:
        logging.warning("Failed to store LIQ raw signals: %s", exc)

    logging.info("")
    logging.info("=== %s Liquidity Sweep count (last 365 days) ===", timeframe)
    for sym, cnt in pair_counts.items():
        logging.info("  %-10s %d", sym, cnt)
    logging.info("  %-10s %d", "TOTAL", sum(pair_counts.values()))

    overall = _compute_stats(all_trade_results)
    ev = overall.get("expected_value", {})
    logging.info("")
    logging.info("=== %s LIQ Scan Stats ===", timeframe)
    logging.info("  Resolved: %d  Wins: %d  Losses: %d  Open: %d",
                 overall["resolved"], overall["wins"], overall["losses"], overall["open"])
    logging.info("  Win rate: %.1f%%  |  EV 1:2: %+.3fR", overall["win_rate"] * 100, ev.get("1:2", 0))

    if all_raw_signals:
        _run_liq_param_sweep(all_raw_signals, db, strategy=strategy, update_gold=update_gold)


def _scan_one_symbol_fvg(args: tuple) -> tuple:
    """Worker: scan FVG doji setups on given timeframe. Returns raw signal records."""
    symbol, settings, param_set_id, cutoff_ts, scan_run_id, timeframe, htf = args

    from db.local_db import LocalDB
    from analysis.smc_analyzer import SMCAnalyzer
    from alerts.alert_manager import AlertManager

    db       = LocalDB(settings.db_path)
    analyzer = SMCAnalyzer()
    dev_mode = settings.dev_mode

    candles_desc = db.query_recent(symbol, timeframe, limit=_candle_limit(timeframe))
    if not candles_desc:
        db.close()
        return symbol, 0, [], [], []

    candles_htf = db.query_recent(symbol, htf, limit=_candle_limit(htf))
    n = len(candles_desc)
    seen: set = set()
    trade_results, log_lines, raw_signal_records = [], [], []

    for k in range(50, n):
        bos_ts = candles_desc[n - 1 - k]["timestamp"]
        if bos_ts < cutoff_ts:
            continue

        # Pass only 50-candle slice for detection: avoids O(n²) from ATR/reverse on full window.
        # FVG lookback=20 + ATR period=14 fit easily in 50 candles.
        idx       = n - 1 - k
        det_slice = candles_desc[idx : idx + 50]
        lookahead = candles_desc[max(0, idx - 51) : idx] if dev_mode else None

        fvg_events = analyzer.detect_fvg_doji(
            det_slice, params={"symbol": symbol, "timeframe": timeframe,
                               "min_fvg_size_atr": 0.0, "min_retrace_pct": 0.0,
                               "max_doji_body_pct": 1.0}
        )
        if not fvg_events:
            continue

        htf_bias        = analyzer.get_htf_bias(candles_htf, bos_ts) if candles_htf else None
        lookahead_chron = list(reversed(lookahead)) if lookahead else []
        sig_dt          = datetime.fromtimestamp(bos_ts, tz=timezone.utc)

        for ev in fvg_events:
            key = (ev["direction"], round(ev["fvg_low"], 5), round(ev["fvg_high"], 5))
            if key in seen:
                continue
            seen.add(key)

            outcome  = AlertManager.evaluate_fvg_outcome(det_slice, ev, lookahead_chron, candles_htf)
            alert_id = _fvg_alert_id(symbol, ev.get("breakout_ts", bos_ts))

            raw_signal_records.append({
                "symbol":           symbol,
                "timeframe":        timeframe,
                "breakout_ts":      ev["breakout_ts"],
                "alert_id":         alert_id,
                "direction":        ev["direction"],
                "broken_level":     (ev["fvg_low"] + ev["fvg_high"]) / 2,
                "break_strength":   ev.get("fvg_size_atr", 0.0),
                "htf_bias":         htf_bias,
                "confluences":      json.dumps([]),
                "outcome":          outcome,
                "hour":             sig_dt.hour,
                "month":            sig_dt.strftime("%Y-%m"),
                "has_liquidity_sweep": 0,
                "swing_age_candles": None,
                "session":          _hour_to_session(sig_dt.hour),
                "dow":              sig_dt.weekday(),
                "fvg_size_atr":        ev.get("fvg_size_atr"),
                "retrace_depth":       ev.get("retrace_depth"),
                "doji_body_pct":       ev.get("doji_body_pct"),
                "rejection_wick_pct":  ev.get("rejection_wick_pct"),
                "strategy":            f"FVG{timeframe}",
                "scan_run_id":      scan_run_id,
            })
            trade_results.append({
                "alert_id":       alert_id,
                "outcome":        outcome,
                "confluences":    [],
                "symbol":         symbol,
                "hour":           sig_dt.hour,
                "month":          sig_dt.strftime("%Y-%m"),
                "break_strength": ev.get("fvg_size_atr", 0.0),
                "htf_bias":       htf_bias,
                "bos_direction":  ev["direction"],
            })
            log_lines.append(
                f"  [{symbol}] FVG DOJI {ev['direction'].upper()}"
                f"  zone=[{ev['fvg_low']:.5f}-{ev['fvg_high']:.5f}]"
                f"  size={ev['fvg_size_atr']:.2f}ATR  retrace={ev['retrace_depth']:.0%}"
                f"  4H={( htf_bias or '?').upper():<8}  ts={sig_dt.strftime('%Y-%m-%d %H:%M')}"
            )

    db.close()
    return symbol, len(raw_signal_records), trade_results, log_lines, raw_signal_records


def run_fvg_experiment(settings: Settings, db: LocalDB, alert_manager: AlertManager,
                       update_gold: bool = False, timeframe: str = "30m") -> None:
    """Scan FVG doji setups on given timeframe; run param sweep."""
    from datetime import timedelta
    htf       = "4h"
    strategy  = f"FVG{timeframe}"
    cutoff_ts = int((datetime.now(timezone.utc) - timedelta(days=365)).timestamp())

    logging.info("Scanning %s FVG doji (last 365 days) — HTF bias from %s", timeframe, htf)
    logging.info("Cutoff: %s UTC", datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"))

    # Loose base: catch all signals; param sweep applies filters on stored values
    base_params = {**_FVG_SWEEP_BASE, "min_fvg_size_atr": 0.0, "min_retrace_pct": 0.0,
                   "max_doji_body_pct": 1.0, "min_rejection_wick_pct": 0.0}
    param_set_id = db.get_or_create_param_set(base_params)

    try:
        fetcher = FXFetcher(settings)
        for symbol in settings.fx_pairs:
            _ensure_pair_data(symbol, timeframe, fetcher, db)
            _ensure_pair_data(symbol, htf, fetcher, db)
    except Exception as exc:
        logging.warning("Auto-fetch unavailable: %s", exc)

    scan_run_id = int(datetime.now(timezone.utc).timestamp())
    n_workers   = min(len(settings.fx_pairs), max(1, (os.cpu_count() or 4) // 2))
    worker_args = [
        (symbol, settings, param_set_id, cutoff_ts, scan_run_id, timeframe, htf)
        for symbol in settings.fx_pairs
    ]
    logging.info("Launching %d FVG workers (%s)...", n_workers, timeframe)

    pair_counts: dict = {}
    all_trade_results: list = []
    all_raw_signals:   list = []

    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        futures = {executor.submit(_scan_one_symbol_fvg, args): args[0] for args in worker_args}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                sym, count, trade_results, log_lines, raw_signal_recs = future.result()
            except Exception as exc:
                logging.error("FVG worker for %s failed: %s", symbol, exc)
                pair_counts[symbol] = 0
                continue
            pair_counts[sym] = count
            all_trade_results.extend(trade_results)
            all_raw_signals.extend(raw_signal_recs)
            for line in log_lines:
                logging.info(line)

    try:
        db.insert_raw_signals(scan_run_id, all_raw_signals)
        logging.info("FVG raw signals stored: %d (scan_run_id=%d)", len(all_raw_signals), scan_run_id)
    except Exception as exc:
        logging.warning("Failed to store FVG raw signals: %s", exc)

    logging.info("")
    logging.info("=== %s FVG doji count (last 365 days) ===", timeframe)
    for sym, cnt in pair_counts.items():
        logging.info("  %-10s %d", sym, cnt)
    logging.info("  %-10s %d", "TOTAL", sum(pair_counts.values()))

    overall = _compute_stats(all_trade_results)
    ev = overall.get("expected_value", {})
    logging.info("")
    logging.info("=== %s FVG Scan Stats ===", timeframe)
    logging.info("  Resolved: %d  Wins: %d  Losses: %d  Open: %d",
                 overall["resolved"], overall["wins"], overall["losses"], overall["open"])
    logging.info("  Win rate: %.1f%%  |  EV 1:2: %+.3fR", overall["win_rate"] * 100, ev.get("1:2", 0))

    if all_raw_signals:
        _run_fvg_param_sweep(all_raw_signals, db, strategy=strategy, update_gold=update_gold)


def run_bos_experiment(
    settings: Settings,
    db: LocalDB,
    alert_manager: AlertManager,
    timeframe: str = "15m",
    htf: str = "4h",
    update_gold: bool = False,
    scan_days: int = 365,
) -> None:
    """Scan historical BOS events on the given timeframe / HTF pair."""
    from datetime import timedelta
    strategy = f"BOS{timeframe}"
    cutoff_ts = int((datetime.now(timezone.utc) - timedelta(days=scan_days)).timestamp())

    logging.info("Scanning %s BOS (last %d days) — MTF bias from %s", timeframe.upper(), scan_days, htf.upper())
    logging.info("Cutoff: %s UTC", datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"))

    active_params = {
        "min_break_strength": 0.7,
        "min_break_distance_atr_mult": 0.3,
        "min_atr_pct": 0.0003,
        "min_swing_age_candles": 5,
        "swing_lookback": 20,
        "require_liquidity_sweep": False,
        "require_brt_confluence": False,
        "lookahead_candles": 50,
        "scan_days": 365,
    }
    param_set_id = db.get_or_create_param_set(active_params)
    logging.info("Parameter set v%d: %s", param_set_id, active_params)

    # Auto-fetch required history for all pairs
    try:
        fetcher = FXFetcher(settings)
        for symbol in settings.fx_pairs:
            _ensure_pair_data(symbol, timeframe, fetcher, db, target_days=scan_days)
            _ensure_pair_data(symbol, htf, fetcher, db, target_days=scan_days)
    except Exception as exc:
        logging.warning("Auto-fetch unavailable (no API key?): %s", exc)

    pair_counts: dict = {}
    all_trade_results: list = []
    all_raw_signals: list = []

    # Run all pairs in parallel — workers render charts and return results,
    # main process handles all DB writes to avoid SQLite contention.
    scan_run_id = int(datetime.now(timezone.utc).timestamp())
    n_workers = min(len(settings.fx_pairs), max(1, (os.cpu_count() or 4) // 2))
    worker_args = [
        (symbol, settings, param_set_id, cutoff_ts, timeframe, htf, active_params, scan_run_id, strategy, scan_days)
        for symbol in settings.fx_pairs
    ]
    logging.info("Launching %d parallel workers (one per pair)...", n_workers)

    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        futures = {executor.submit(_scan_one_symbol, args): args[0] for args in worker_args}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                sym, count, trade_results, alert_records, log_lines, raw_signal_recs = future.result()
            except Exception as exc:
                logging.error("Worker for %s failed: %s", symbol, exc)
                pair_counts[symbol] = 0
                continue

            pair_counts[sym] = count
            all_trade_results.extend(trade_results)
            all_raw_signals.extend(raw_signal_recs)

            for line in log_lines:
                logging.info(line)

            # Write DB records for this pair sequentially (safe, no contention)
            for rec in alert_records:
                try:
                    db.insert_alert(
                        rec["symbol"], rec["timeframe"], rec["ts"], "BOS",
                        rec["message"], rec["image_path"], rec["params_used"],
                        alert_id=rec["alert_id"], param_set_id=rec["param_set_id"],
                    )
                except Exception as exc:
                    logging.warning("DB insert failed for %s: %s", rec["alert_id"], exc)

    # Store raw signals for later param sweep queries
    try:
        db.insert_raw_signals(scan_run_id, all_raw_signals)
        logging.info("Raw signals stored: %d (scan_run_id=%d)", len(all_raw_signals), scan_run_id)
    except Exception as exc:
        logging.warning("Failed to store raw signals: %s", exc)

    logging.info("")
    logging.info("=== %s BOS count (last %d days) ===", timeframe.upper(), scan_days)
    for sym, cnt in pair_counts.items():
        logging.info("  %-10s %d", sym, cnt)
    logging.info("  %-10s %d", "TOTAL", sum(pair_counts.values()))

    # Compute per-pair and overall stats, store in DB
    symbols_in_results = sorted({r["symbol"] for r in all_trade_results})
    per_sym_stats: dict = {}
    for sym in symbols_in_results:
        sym_trades = [r for r in all_trade_results if r["symbol"] == sym]
        s = _compute_stats(sym_trades)
        per_sym_stats[sym] = s
        db.insert_scan_stats(param_set_id, sym, s["total"], s["wins"], s["losses"], s["open"], json.dumps(s))

    overall = _compute_stats(all_trade_results)
    db.insert_scan_stats(param_set_id, "ALL",
                         overall["total"], overall["wins"], overall["losses"], overall["open"],
                         json.dumps(overall))
    logging.info("Stats saved to DB (param_set_id=%d, %d symbols + overall)", param_set_id, len(symbols_in_results))

    # Console summary
    ev = overall.get("expected_value", {})
    logging.info("")
    logging.info("=== Scan Stats — param_set v%d ===", param_set_id)
    logging.info("  Resolved: %d  Wins: %d  Losses: %d  Open: %d",
                 overall["resolved"], overall["wins"], overall["losses"], overall["open"])
    logging.info("  Win rate: %.1f%%  |  EV 1:2 R:R: %+.3fR  |  EV 1:3 R:R: %+.3fR",
                 overall["win_rate"] * 100, ev.get("1:2", 0), ev.get("1:3", 0))
    logging.info("")
    logging.info("  %-10s  %5s  %6s  %6s", "Pair", "Wins", "Resol", "WR%")
    for sym, s in per_sym_stats.items():
        logging.info("  %-10s  %5d  %6d  %5.1f%%",
                     sym, s["wins"], s["resolved"], s["win_rate"] * 100)

    # Parameter sweep: evaluate all sets against the raw signals collected this run
    if not all_raw_signals:
        logging.warning("No raw signals collected — skipping param sweep")
        return

    sweep_sets = PARAM_SWEEP_SETS_4H if timeframe == "4h" else PARAM_SWEEP_SETS
    all_pair_stats, all_symbols, pset_ids = _run_param_sweep(
        all_raw_signals, db, strategy=strategy, update_gold=update_gold,
        param_sets=sweep_sets)
    _send_sweep_summary(alert_manager, all_raw_signals, all_pair_stats, all_symbols, pset_ids,
                        param_sets=sweep_sets, strategy=strategy)
    return all_pair_stats, all_symbols, pset_ids, all_raw_signals, sweep_sets


def scheduler_heartbeat_job() -> None:
    """Log a heartbeat every 30 minutes so the debug log confirms the scheduler is alive."""
    _dlog.info("[SCHEDULER] heartbeat | alive | %s UTC",
               datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))


def daily_report_job(settings: Settings, db: LocalDB, alert_manager: AlertManager) -> None:
    """Send daily Telegram summary at 21:00 IDT (18:00 UTC)."""
    _dlog.info("[DAILY_REPORT] generating")
    try:
        now = datetime.now(timezone.utc)
        is_weekend = now.weekday() >= 5

        freshness = db.get_data_freshness()
        freshness_map = {(r["symbol"], r["timeframe"]): r for r in freshness}

        all_pairs = list(settings.fx_pairs)
        if "DAX" not in all_pairs:
            all_pairs.append("DAX")
        report_tfs = ["15m", "4h", "30m"]

        lines = [f"📊 Daily Report — {now.strftime('%a %d %b %Y')} (UTC)"]
        lines.append("")
        lines.append("─── Data Freshness ───")

        stale_pairs: list = []
        for symbol in all_pairs:
            parts = []
            for tf in report_tfs:
                row = freshness_map.get((symbol, tf))
                if row is None:
                    parts.append(f"{tf}:–")
                    continue
                ts = row["latest_ts"]
                age_h = (now.timestamp() - ts) / 3600
                dt_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M")
                # threshold = 1.5× the bar interval so normal gaps don't false-flag
                _stale_thresh = {"15m": 0.5, "30m": 1.0, "4h": 6.0,
                                 "5m": 0.25, "1d": 30.0}.get(tf, 2.0)
                # 30m only fetches during the NY window (12-16 UTC); outside that range
                # the data will always look "stale" — suppress the flag.
                _in_30m_window = 12 <= now.hour < 16
                _skip_stale = tf == "30m" and not _in_30m_window
                stale = not is_weekend and not _skip_stale and age_h > _stale_thresh
                flag = " ⚠" if stale else " ✓"
                if stale:
                    stale_pairs.append(f"{symbol}/{tf}")
                parts.append(f"{tf}:{dt_str}{flag}")
            lines.append(f"{symbol:<8} {' │ '.join(parts)}")

        calls = db.get_api_calls_today()
        pct = calls / API_DAILY_LIMIT * 100
        lines += ["", "─── API Budget ───",
                  f"Calls today: {calls}/{API_DAILY_LIMIT} ({pct:.0f}%)"]

        monitors = db.get_open_monitors()
        lines += ["", f"─── Open Monitors ({len(monitors)}) ───"]
        if monitors:
            for m in monitors:
                n = len(db.query_candles_after(m["symbol"], "15m", m["breakout_ts"], limit=60))
                lines.append(
                    f"  {m['symbol']} {m['direction'].upper():<5} "
                    f"entry={m['entry']:.5f} TP={m['tp']:.5f} ({n} bars)"
                )
        else:
            lines.append("  (none)")

        today_alerts = db.get_today_alerts()
        lines += ["", f"─── Today's Alerts ({len(today_alerts)}) ───"]
        if today_alerts:
            for a in today_alerts[:10]:
                ts_str = datetime.fromtimestamp(a["ts"], tz=timezone.utc).strftime("%H:%M")
                lines.append(f"  {ts_str} {a['symbol']} {a['timeframe']} [{a['type']}]")
        else:
            lines.append("  (none)")

        live_stats = db.get_live_monitor_stats()
        if live_stats:
            lines += ["", "─── Live Monitor WR (all time) ───"]
            for symbol, s in sorted(live_stats.items()):
                wins  = s["tp_hit"]
                total = wins + s["sl_hit"]
                wr    = wins / total * 100 if total else 0
                lines.append(f"  {symbol}: {wins}W/{s['sl_hit']}L — WR {wr:.1f}% (n={total})")

        if stale_pairs:
            lines += ["", f"⚠ Stale data: {', '.join(stale_pairs)}"]

        message = "\n".join(lines)
        alert_manager.notifier.send_message(message)
        logging.info("Daily report sent")
        _dlog.info("[DAILY_REPORT] sent | pairs=%d monitors=%d today_alerts=%d stale=%d",
                   len(all_pairs), len(monitors), len(today_alerts), len(stale_pairs))
    except Exception:
        logging.exception("Daily report job failed")
        _dlog.error("[DAILY_REPORT] FAILED")


def setup_logging(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "trade.log"
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    file_handler = TimedRotatingFileHandler(
        filename=str(log_file),
        when="h",
        interval=12,
        backupCount=4,
        utc=True,
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers = []
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # ── Debug log: verbose checkpoint file, rotates at 10 MB or 48 h ──
    debug_file = log_dir / "debug.log"
    debug_handler = SizeAndAgeRotatingHandler(
        str(debug_file),
        max_age_seconds=48 * 3600,
        maxBytes=10 * 1024 * 1024,
        backupCount=2,
    )
    _UtcFmt = type(
        "_UtcFmt", (logging.Formatter,),
        {"converter": staticmethod(__import__("time").gmtime)},
    )
    debug_handler.setFormatter(
        _UtcFmt(
            "%(asctime)s.%(msecs)03d UTC | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    debug_logger = logging.getLogger("trade.debug")
    debug_logger.setLevel(logging.DEBUG)
    debug_logger.handlers = []
    debug_logger.addHandler(debug_handler)
    debug_logger.propagate = False  # don't bleed into root logger / trade.log

    return log_file


def check_logs_job(log_monitor: LogMonitor) -> None:
    try:
        log_monitor.scan()
    except Exception:
        logging.exception("Log monitor failed")


def run_gap_check(
    settings: Settings,
    fetcher: "FXFetcher",
    db: LocalDB,
    dry_run: bool = False,
) -> None:
    """
    Scan DB for missing candle ranges across all configured pairs + timeframes,
    then backfill any real gaps (non-weekend) from the Twelve Data API.

    DAX is excluded because it is fetched via Yahoo Finance, not TwelveData.
    Skips backfill (dry-run only) if the daily API budget is exhausted.
    """
    from data.gap_detector import check_and_backfill

    calls_today = db.get_api_calls_today()
    budget_left = API_DAILY_LIMIT - calls_today
    if budget_left <= 0 and not dry_run:
        logging.warning("Gap check: no API credits remaining today (%d/%d) — skipping backfill",
                        calls_today, API_DAILY_LIMIT)
        return

    # Only pairs served by the TwelveData fetcher (exclude DAX / Yahoo symbols)
    # Use FETCH_PRIORITY order so best pairs get backfilled first if budget is tight
    configured = set(p for p in settings.fx_pairs if p.upper() not in ("DAX",))
    pairs = [p for p in FETCH_PRIORITY if p in configured]
    pairs += [p for p in configured if p not in set(FETCH_PRIORITY)]
    timeframes = ["15m", "4h"]  # only production timeframes need gap coverage

    total_gaps     = 0
    total_inserted = 0
    api_calls_made = 0

    # Only inspect the last 14 days — ancient gaps are either known market
    # closures or permanently unfillable (API has no data). Retrying them
    # wastes API credits on every startup without ever inserting anything.
    since_ts = int(datetime.now(timezone.utc).timestamp()) - 14 * 24 * 3600

    _dlog.info("[GAP_CHECK] START | %d pairs × %d timeframes | budget=%d/%d used",
               len(pairs), len(timeframes), calls_today, API_DAILY_LIMIT)
    logging.info("=== Gap check: %d pairs × %d timeframes (budget: %d/%d used) ===",
                 len(pairs), len(timeframes), calls_today, API_DAILY_LIMIT)
    for symbol in pairs:
        for tf in timeframes:
            if not dry_run and (API_DAILY_LIMIT - db.get_api_calls_today()) <= 0:
                _dlog.warning("[GAP_CHECK] BUDGET_EXHAUSTED mid-run | stopping at %s %s", symbol, tf)
                logging.warning("Gap check: budget exhausted mid-run — stopping at %s %s", symbol, tf)
                break
            try:
                gaps, inserted = check_and_backfill(
                    db, fetcher, symbol, tf, since_ts=since_ts, dry_run=dry_run
                )
                _dlog.info("[GAP_CHECK] %s %s | gaps=%d inserted=%d", symbol, tf, gaps, inserted)
                total_gaps     += gaps
                total_inserted += inserted
                if not dry_run and gaps > 0:
                    # Each gap triggers at least one fetch_historical call
                    db.increment_api_calls(gaps)
                    api_calls_made += gaps
            except Exception as exc:
                _dlog.error("[GAP_CHECK] ERROR | %s %s | %s", symbol, tf, exc)
                logging.warning("Gap check failed for %s %s: %s", symbol, tf, exc)

    if dry_run:
        _dlog.info("[GAP_CHECK] END (dry-run) | total_gaps=%d", total_gaps)
        logging.info("Gap check (dry-run) complete — %d gap(s) found", total_gaps)
    else:
        _dlog.info("[GAP_CHECK] END | total_gaps=%d total_inserted=%d api_calls=%d",
                   total_gaps, total_inserted, api_calls_made)
        logging.info(
            "Gap check complete — %d gap(s) found, %d candles inserted, %d API calls used",
            total_gaps, total_inserted, api_calls_made,
        )


def gap_check_job(settings: Settings, fetcher: "FXFetcher", db: LocalDB) -> None:
    """Scheduled wrapper for run_gap_check — runs once daily at 06:00 UTC."""
    try:
        run_gap_check(settings, fetcher, db)
    except Exception:
        logging.exception("Scheduled gap check failed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="trade FX data fetcher and alert runner")
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="Send a test Telegram alert and exit",
    )
    parser.add_argument(
        "--experiment-bos",
        action="store_true",
        help="Scan existing 15m DB data for the first 3 BOS occurrences and send each as a Telegram image",
    )
    parser.add_argument(
        "--nightly",
        action="store_true",
        help="Re-run param sweep on latest DB raw_signals (no rescan); send Telegram summary",
    )
    parser.add_argument(
        "--experiment-bos-4h",
        action="store_true",
        help="Same as --experiment-bos but on 4h timeframe with 1d HTF bias",
    )
    parser.add_argument(
        "--experiment-fvg",
        action="store_true",
        help="Scan 30m data for FVG + deep retrace + doji rejection setups",
    )
    parser.add_argument(
        "--experiment-fvg15",
        action="store_true",
        help="Same as --experiment-fvg but on 15m timeframe",
    )
    parser.add_argument(
        "--experiment-liq30",
        action="store_true",
        help="Scan 30m data for liquidity sweep (EQH/EQL/PDH/PDL) + pin-bar rejection setups",
    )
    parser.add_argument(
        "--experiment-liq15",
        action="store_true",
        help="Same as --experiment-liq30 but on 15m timeframe",
    )
    parser.add_argument(
        "--experiment-dax",
        action="store_true",
        help="Scan DE40 for DAX Frankfurt open session setups (09:00-12:30 Israel time)",
    )
    parser.add_argument(
        "--update-gold",
        action="store_true",
        help="Apply the best param set found in this scan as the new gold params (manual approval step — never runs automatically)",
    )
    parser.add_argument(
        "--scan-days",
        type=int,
        default=None,
        help="Override number of days to scan (default: 365 for 15m, 730 for 4h)",
    )
    parser.add_argument(
        "--check-gaps",
        action="store_true",
        help="Detect and backfill missing candle ranges across all pairs and timeframes, then exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --check-gaps: report gaps without fetching or inserting anything",
    )
    parser.add_argument(
        "--analyze-losses",
        action="store_true",
        help="Analyse loss patterns from latest raw_signals: DoW, session, momentum fade, EV simulation",
    )
    parser.add_argument(
        "--weekly-report",
        action="store_true",
        help="Full weekly job: rescan BOS experiment (no gold update) + live decay check per pair",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = Settings.load_from_yaml(str(Path(__file__).parents[1] / "config.yaml"))
    log_file = setup_logging(Path(__file__).parents[1] / "logs")
    db = LocalDB(settings.db_path)
    # Pass the existing DB instance to AlertManager to avoid locking issues
    alert_manager = AlertManager(settings, db=db)

    if args.test_telegram:
        alert_manager.send_test_alert()
        db.close()
        return

    if args.analyze_losses:
        run_loss_analysis(db, alert_manager)
        db.close()
        return

    if args.weekly_report:
        run_weekly_report(settings, db, alert_manager)
        db.close()
        return

    if args.experiment_bos:
        days = args.scan_days or 365
        run_bos_experiment(settings, db, alert_manager, update_gold=args.update_gold, scan_days=days)
        db.close()
        return

    if args.nightly:
        run_nightly_sweep(db, alert_manager)
        db.close()
        return

    if args.experiment_bos_4h:
        days = args.scan_days or 730
        run_bos_experiment(settings, db, alert_manager, timeframe="4h", htf="1d",
                           update_gold=args.update_gold, scan_days=days)
        db.close()
        return

    if args.experiment_fvg:
        run_fvg_experiment(settings, db, alert_manager, update_gold=args.update_gold, timeframe="30m")
        db.close()
        return

    if args.experiment_fvg15:
        run_fvg_experiment(settings, db, alert_manager, update_gold=args.update_gold, timeframe="15m")
        db.close()
        return

    if args.experiment_liq30:
        run_liq_experiment(settings, db, alert_manager, update_gold=args.update_gold, timeframe="30m")
        db.close()
        return

    if args.experiment_liq15:
        run_liq_experiment(settings, db, alert_manager, update_gold=args.update_gold, timeframe="15m")
        db.close()
        return

    if args.experiment_dax:
        days = args.scan_days or 365
        run_dax_experiment(settings, db, alert_manager, scan_days=days, update_gold=args.update_gold)
        db.close()
        return

    # FXFetcher is needed for both --check-gaps and service mode
    fetcher = FXFetcher(settings)

    if args.check_gaps:
        run_gap_check(settings, fetcher, db, dry_run=args.dry_run)
        db.close()
        return

    log_monitor = LogMonitor(log_file, alert_manager.notifier)

    scheduler = BlockingScheduler()
    scheduler.add_job(
        fetch_job,
        trigger="cron",
        second=FETCH_CHECK_SECOND,
        args=[settings, fetcher, db, alert_manager],
        max_instances=1,
        id="trade_fetch_job",
    )
    scheduler.add_job(
        check_logs_job,
        trigger="interval",
        minutes=10,
        args=[log_monitor],
        max_instances=1,
        id="trade_log_monitor_job",
    )
    scheduler.add_job(
        dax_data_job,
        trigger="cron",
        minute="*/5",
        second=0,
        args=[db],
        max_instances=1,
        id="trade_dax_data_job",
    )
    scheduler.add_job(
        dax_session_job,
        trigger="cron",
        minute="*/5",
        second=30,
        args=[settings, db, alert_manager],
        max_instances=1,
        id="trade_dax_session_job",
    )
    scheduler.add_job(
        gap_check_job,
        trigger="cron",
        hour=6,
        minute=0,
        args=[settings, fetcher, db],
        max_instances=1,
        id="trade_gap_check_job",
    )
    scheduler.add_job(
        momentum_monitor_job,
        trigger="interval",
        minutes=15,
        args=[db, alert_manager],
        max_instances=1,
        id="trade_momentum_monitor_job",
    )
    scheduler.add_job(
        daily_report_job,
        trigger="cron",
        hour=18,
        minute=0,
        args=[settings, db, alert_manager],
        max_instances=1,
        id="trade_daily_report_job",
    )
    scheduler.add_job(
        scheduler_heartbeat_job,
        trigger="interval",
        minutes=30,
        max_instances=1,
        id="trade_heartbeat_job",
    )

    logging.info("Starting continuous fetch scheduler. Fetch check runs every minute at second %d.", FETCH_CHECK_SECOND)
    logging.info("DAX session job runs every 5 min; alerts during 09:00-12:30 Israel time (Frankfurt open)")
    logging.info("Gap check job runs daily at 06:00 UTC")
    logging.info("Daily report job runs at 21:00 IDT (18:00 UTC)")
    logging.info("Log rotation enabled: 12h interval, 4 backups (~48h retention)")
    logging.info("Debug log: logs/debug.log (rotates at 10 MB or 48 h)")

    _dlog.info("[STARTUP] trade service starting | pairs=%d alert_pairs=%s dev_mode=%s",
               len(settings.fx_pairs),
               getattr(settings, "alert_pairs", settings.fx_pairs),
               getattr(settings, "dev_mode", False))
    _dlog.info("[STARTUP] liq_live_pairs=%s | bos_timeframes=%s", _LIQ_LIVE_PAIRS, settings.timeframes)
    _dlog.info("[STARTUP] scheduler jobs: fetch(1min) monitor(15min) dax_data(5min) "
               "dax_session(5min) gap_check(06:00UTC) daily_report(18:00UTC) heartbeat(30min)")

    # Run gap check once at startup so any downtime gaps are recovered immediately
    logging.info("Running startup gap check...")
    _dlog.info("[STARTUP] running startup gap check...")
    try:
        run_gap_check(settings, fetcher, db)
    except Exception:
        logging.exception("Startup gap check failed (non-fatal)")
        _dlog.error("[STARTUP] gap check failed (non-fatal)")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logging.info("Scheduler shut down.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
