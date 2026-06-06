import argparse
import logging
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
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

FETCH_CHECK_SECOND = 10
INITIAL_LOOKBACK_HOURS = 8760  # 365 days
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
    Staggered fetch schedule to stay within Twelve Data free tier (8 API calls/minute).
    Each timeframe fetches at a different minute to avoid rate limit hits.
    
    Schedule (repeats every 15 minutes):
    - Minute 2, 17, 32, 47: 5m (4x/hour, 3 calls)
    - Minute 4, 19, 34, 49: 15m (4x/hour, 3 calls)
    - Minute 7, 22, 37, 52: 30m (4x/hour, 3 calls)
    - Minute 9, 24, 39, 54: 1h (4x/hour, 3 calls)
    - Minute 11, 26, 41, 56: 4h (4x/hour, 3 calls)
    
    Max calls per minute: 3 (always safe)
    """
    timeframe = timeframe.lower()
    minute = now.minute

    if timeframe in {"5m", "5min"}:
        return minute % 15 == 2
    if timeframe in {"15m", "15min"}:
        return minute % 15 == 4
    if timeframe in {"30m", "30min"}:
        return minute % 30 == 7
    if timeframe in {"1h", "h1", "60min"}:
        return minute % 15 == 9
    if timeframe in {"4h", "h4"}:
        return minute % 15 == 11
    return False


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


def process_symbol_timeframe(
    symbol: str,
    timeframe: str,
    fetcher: FXFetcher,
    db: LocalDB,
    alert_manager: AlertManager,
) -> None:
    logging.info("Fetching %s %s", symbol, timeframe)
    latest_timestamp = db.get_latest_timestamp(symbol, timeframe)
    fetch_limit = get_fetch_limit(timeframe, latest_timestamp)
    candles = fetcher.fetch_historical(symbol, timeframe, limit=fetch_limit)
    validate_candles(symbol, timeframe, candles)

    if latest_timestamp is not None:
        new_candles = filter_new_candles(candles, latest_timestamp)
    else:
        new_candles = filter_last_hours(candles, timeframe)

    if not new_candles:
        logging.info("No new candles for %s %s", symbol, timeframe)
        return

    db.insert_candles(symbol, timeframe, new_candles)
    logging.info("Inserted %d new candles for %s %s", len(new_candles), symbol, timeframe)

    alerts = alert_manager.evaluate(symbol, timeframe, new_candles)
    for alert in alerts:
        text = alert_manager.format_alert(alert)
        logging.info(text)
        alert_manager.send_alert(text)


def fetch_job(settings: Settings, fetcher: FXFetcher, db: LocalDB, alert_manager: AlertManager) -> None:
    now = datetime.now(timezone.utc)
    timeframes = get_timeframes_to_fetch(settings.timeframes, now)
    if not timeframes:
        logging.debug("No scheduled timeframes at %s", now)
        return

    logging.info("Running scheduled fetch for timeframes: %s", ", ".join(timeframes))
    for symbol in settings.fx_pairs:
        for timeframe in timeframes:
            try:
                process_symbol_timeframe(symbol, timeframe, fetcher, db, alert_manager)
                # Delay to stay within API rate limits (8/min). 
                # 7s ensures 9+ calls are spread across > 1 minute.
                time.sleep(7)
            except Exception as exc:
                message = f"Failed to fetch {symbol} {timeframe}: {exc}"
                logging.exception(message)
                alert_manager.send_fetch_error(symbol, timeframe, str(exc))


def _ensure_pair_data(symbol: str, timeframe: str, fetcher: "FXFetcher", db: LocalDB) -> None:
    """Fetch up to 365 days of historical data for a pair/timeframe, paginating if needed."""
    target_days = 365
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

    logging.info("Fetching 365-day history for %s %s (paginating)...", symbol, timeframe)
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
    symbol, settings, param_set_id, cutoff_ts, timeframe, htf, active_params, scan_run_id, strategy = args

    from db.local_db import LocalDB
    from alerts.alert_manager import AlertManager
    from analysis.confluence_detector import detect_confluences, find_trigger_candle

    db = LocalDB(settings.db_path)
    alert_manager = AlertManager(settings, db=db)
    dev_mode = settings.dev_mode

    candles_desc = db.query_recent(symbol, timeframe, limit=_candle_limit(timeframe))
    if not candles_desc:
        db.close()
        return symbol, 0, [], [], [], []

    candles_4h = db.query_recent(symbol, htf, limit=_candle_limit(htf))
    n = len(candles_desc)
    count = 0
    seen_raw: set = set()    # dedup for raw_signal storage
    seen_active: set = set() # dedup for chart rendering (active_params)
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
        lookahead = candles_desc[max(0, n - k - 51) : n - 1 - k] if dev_mode else None

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
        lookahead_chron = list(reversed(lookahead)) if lookahead else []
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
                outcome_raw = AlertManager.evaluate_bos_outcome(
                    window, ev, lookahead_chron, candles_4h, trigger_ts_val
                )
                raw_signal_records.append({
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "breakout_ts": ev["breakout_ts"],
                    "alert_id": AlertManager._generate_alert_id(symbol, ev["breakout_ts"]),
                    "direction": ev["direction"],
                    "broken_level": ev["broken_level"],
                    "break_strength": ev.get("break_strength", 0.0),
                    "htf_bias": htf_bias,
                    "confluences": json.dumps(confluences),
                    "outcome": outcome_raw,
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

            alert = alert_manager.render_alert(
                symbol, timeframe, ev, window, lookahead, htf_bias, confluences,
                trigger_ts=trigger_ts_val, candles_4h=candles_4h,
                param_set_id=param_set_id, skip_db=True,
            )

            trade_results.append({
                "alert_id": alert["alert_id"],
                "outcome": alert.get("outcome", "OPEN"),
                "confluences": alert.get("confluences", []),
                "symbol": symbol,
                "hour": bos_dt.hour,
                "month": bos_dt.strftime("%Y-%m"),
                "break_strength": ev.get("break_strength", 0.0),
                "htf_bias": htf_bias,
                "bos_direction": ev["direction"],
            })
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

            sweep = ev.get("liquidity_sweep") or {}
            sweep_str = ("  sweep@" + datetime.fromtimestamp(sweep["timestamp"], tz=timezone.utc).strftime("%H:%M")) if sweep else ""
            log_lines.append(
                f"  [{symbol}] {ev['direction'].upper()} BOS  id={alert['alert_id']}"
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

    result = []
    for r in raw_signals:
        if r["break_strength"] < min_str:
            continue
        confs = json.loads(r["confluences"]) if isinstance(r["confluences"], str) else r["confluences"]
        if req_brt and "BRT" not in confs:
            continue
        if req_sweep and not r.get("has_liquidity_sweep"):
            continue
        if len(confs) < min_conf:
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
    return "+".join(parts)


def _raw_to_trade_result(r: dict) -> dict:
    confs = r["confluences"] if isinstance(r["confluences"], list) else json.loads(r["confluences"])
    return {
        "alert_id":     r["alert_id"],
        "outcome":      r["outcome"],
        "confluences":  confs,
        "symbol":       r["symbol"],
        "hour":         r["hour"],
        "month":        r["month"],
        "break_strength": r["break_strength"],
        "htf_bias":     r.get("htf_bias"),
        "bos_direction": r["direction"],
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


def _run_param_sweep(raw_signals: list, db: LocalDB) -> tuple:
    """Evaluate all PARAM_SWEEP_SETS against raw_signals; print comparison table; store in DB."""
    all_symbols = sorted({r["symbol"] for r in raw_signals})
    logging.info("")
    logging.info("=== Parameter Sweep (%d sets × %d pairs) ===", len(PARAM_SWEEP_SETS), len(all_symbols))
    hdr_pairs = "  ".join(f"{s[:6]:>6}" for s in all_symbols)
    logging.info("  %-30s  %s  %s  %s", "Params", hdr_pairs, "OVERAL", "EV1:2")
    logging.info("  " + "-" * 102)

    all_pair_stats: dict = {}
    pset_ids: list = []

    for pset in PARAM_SWEEP_SETS:
        pset_id = db.get_or_create_param_set(pset)
        pset_ids.append(pset_id)

        filtered = _apply_param_filter(raw_signals, pset)
        trades   = [_raw_to_trade_result(r) for r in filtered]

        for sym in all_symbols:
            sym_trades = [t for t in trades if t["symbol"] == sym]
            s = _compute_stats(sym_trades)
            all_pair_stats[(pset_id, sym)] = s
            db.insert_scan_stats(pset_id, sym, s["total"], s["wins"], s["losses"], s["open"], json.dumps(s))

        ov = _compute_stats(trades)
        all_pair_stats[(pset_id, "ALL")] = ov
        db.insert_scan_stats(pset_id, "ALL", ov["total"], ov["wins"], ov["losses"], ov["open"], json.dumps(ov))

        lbl       = _pset_label(pset)
        pair_cols = "  ".join(f"{all_pair_stats[(pset_id, s)]['win_rate']*100:>5.1f}%" for s in all_symbols)
        ev_12     = ov.get("expected_value", {}).get("1:2", 0)
        logging.info("  v%-3d %-25s  %s  %5.1f%%  %+.3fR",
                     pset_id, lbl, pair_cols, ov["win_rate"] * 100, ev_12)

    logging.info("")
    logging.info("Best param set per pair (min 20 resolved trades):")
    for sym in all_symbols:
        candidates = [
            (pid, pset) for pid, pset in zip(pset_ids, PARAM_SWEEP_SETS)
            if all_pair_stats.get((pid, sym), {}).get("resolved", 0) >= 20
        ]
        if not candidates:
            logging.info("  %-10s  (no param set with ≥20 trades)", sym)
            continue
        best_pid, best_pset = max(candidates, key=lambda x: all_pair_stats[(x[0], sym)]["win_rate"])
        s = all_pair_stats[(best_pid, sym)]
        logging.info("  %-10s  v%-3d %-25s  WR: %5.1f%%  Trades: %d  EV 1:2: %+.3fR",
                     sym, best_pid, _pset_label(best_pset), s["win_rate"] * 100, s["resolved"],
                     s.get("expected_value", {}).get("1:2", 0))

    return all_pair_stats, all_symbols, pset_ids


def _send_sweep_summary(
    alert_manager: AlertManager,
    raw_signals: list,
    all_pair_stats: dict,
    all_symbols: list,
    pset_ids: list,
) -> None:
    """Send a concise Telegram summary of the sweep results."""
    try:
        pset_ev = []
        for pid, pset in zip(pset_ids, PARAM_SWEEP_SETS):
            ov = all_pair_stats.get((pid, "ALL"), {})
            ev = ov.get("expected_value", {}).get("1:2", -999)
            wr = ov.get("win_rate", 0)
            n  = ov.get("resolved", 0)
            if n >= 20:
                pset_ev.append((pid, pset, ev, wr, n))
        pset_ev.sort(key=lambda x: x[2], reverse=True)

        lines = [
            f"BOS Param Sweep — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
            f"Signals: {len(raw_signals):,}  Sets: {len(PARAM_SWEEP_SETS)}",
            "",
            "Top 5 by EV (1:2 R:R):",
        ]
        for i, (pid, pset, ev, wr, n) in enumerate(pset_ev[:5], 1):
            lines.append(f"  {i}. v{pid} {_pset_label(pset)}: WR={wr*100:.1f}% EV={ev:+.3f}R (n={n})")

        lines += ["", "Best per pair (>=20 trades):"]
        for sym in all_symbols:
            candidates = [
                (pid, pset) for pid, pset in zip(pset_ids, PARAM_SWEEP_SETS)
                if all_pair_stats.get((pid, sym), {}).get("resolved", 0) >= 20
            ]
            if not candidates:
                lines.append(f"  {sym}: (no qualifying set)")
                continue
            best_pid, best_pset = max(candidates, key=lambda x: all_pair_stats[(x[0], sym)]["win_rate"])
            s = all_pair_stats[(best_pid, sym)]
            lines.append(f"  {sym}: {_pset_label(best_pset)} WR={s['win_rate']*100:.1f}% (n={s['resolved']})")

        alert_manager.notifier.send_message("\n".join(lines))
        logging.info("Sweep summary sent to Telegram")
    except Exception as exc:
        logging.warning("Failed to send sweep summary: %s", exc)


def run_nightly_sweep(db: LocalDB, alert_manager: AlertManager) -> None:
    """Re-run the param sweep on the latest raw_signals in DB (no rescan, takes seconds)."""
    scan_run_id, raw_signals = db.get_latest_raw_signals(strategy="BOS15m")
    if not raw_signals:
        logging.error("No BOS15m raw signals in DB. Run --experiment-bos first.")
        return
    logging.info("Nightly sweep: %d raw signals from scan_run_id=%d", len(raw_signals), scan_run_id)
    all_pair_stats, all_symbols, pset_ids = _run_param_sweep(raw_signals, db)
    _send_sweep_summary(alert_manager, raw_signals, all_pair_stats, all_symbols, pset_ids)


# ──────────────────────────── FVG DOJI STRATEGY ────────────────────────────

_FVG_SWEEP_BASE = {
    "fvg_lookback": 20,
    "max_doji_body_pct": 0.35,
    "min_fvg_size_atr": 0.3,
    "min_retrace_pct": 0.5,
    "scan_days": 365,
}

FVG_PARAM_SWEEP_SETS = [
    # vary FVG size threshold
    {**_FVG_SWEEP_BASE, "min_fvg_size_atr": 0.3,  "min_retrace_pct": 0.50, "max_doji_body_pct": 0.35},
    {**_FVG_SWEEP_BASE, "min_fvg_size_atr": 0.5,  "min_retrace_pct": 0.50, "max_doji_body_pct": 0.35},
    {**_FVG_SWEEP_BASE, "min_fvg_size_atr": 0.7,  "min_retrace_pct": 0.50, "max_doji_body_pct": 0.35},
    {**_FVG_SWEEP_BASE, "min_fvg_size_atr": 1.0,  "min_retrace_pct": 0.50, "max_doji_body_pct": 0.35},
    # vary retrace depth
    {**_FVG_SWEEP_BASE, "min_fvg_size_atr": 0.5,  "min_retrace_pct": 0.65, "max_doji_body_pct": 0.35},
    {**_FVG_SWEEP_BASE, "min_fvg_size_atr": 0.5,  "min_retrace_pct": 0.80, "max_doji_body_pct": 0.35},
    # vary doji body strictness
    {**_FVG_SWEEP_BASE, "min_fvg_size_atr": 0.5,  "min_retrace_pct": 0.50, "max_doji_body_pct": 0.20},
    {**_FVG_SWEEP_BASE, "min_fvg_size_atr": 0.5,  "min_retrace_pct": 0.50, "max_doji_body_pct": 0.25},
    # combined strict setups
    {**_FVG_SWEEP_BASE, "min_fvg_size_atr": 0.7,  "min_retrace_pct": 0.65, "max_doji_body_pct": 0.25},
    {**_FVG_SWEEP_BASE, "min_fvg_size_atr": 1.0,  "min_retrace_pct": 0.80, "max_doji_body_pct": 0.20},
]


def _fvg_pset_label(pset: dict) -> str:
    return f"fvg{pset['min_fvg_size_atr']}+r{pset['min_retrace_pct']}+b{pset['max_doji_body_pct']}"


def _apply_fvg_param_filter(raw_signals: list, params: dict) -> list:
    min_size    = params.get("min_fvg_size_atr", 0.3)
    min_retrace = params.get("min_retrace_pct", 0.5)
    max_body    = params.get("max_doji_body_pct", 0.35)
    return [
        r for r in raw_signals
        if (r.get("fvg_size_atr") or 0) >= min_size
        and (r.get("retrace_depth") or 0) >= min_retrace
        and (r.get("doji_body_pct") or 1) <= max_body
    ]


def _run_fvg_param_sweep(raw_signals: list, db: LocalDB) -> tuple:
    """Evaluate FVG_PARAM_SWEEP_SETS against FVG raw signals; print comparison; store in DB."""
    all_symbols = sorted({r["symbol"] for r in raw_signals})
    logging.info("")
    logging.info("=== FVG Param Sweep (%d sets × %d pairs) ===", len(FVG_PARAM_SWEEP_SETS), len(all_symbols))
    hdr = "  ".join(f"{s[:6]:>6}" for s in all_symbols)
    logging.info("  %-32s  %s  %s  %s", "Params", hdr, "OVERAL", "EV1:2")
    logging.info("  " + "-" * 102)

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
        logging.info("  v%-3d %-27s  %s  %5.1f%%  %+.3fR",
                     pset_id, lbl, pair_cols, ov["win_rate"] * 100, ev_12)

    logging.info("")
    logging.info("Best FVG param set per pair (min 20 resolved trades):")
    for sym in all_symbols:
        candidates = [
            (pid, pset) for pid, pset in zip(pset_ids, FVG_PARAM_SWEEP_SETS)
            if all_pair_stats.get((pid, sym), {}).get("resolved", 0) >= 20
        ]
        if not candidates:
            logging.info("  %-10s  (no param set with ≥20 trades)", sym)
            continue
        best_pid, best_pset = max(candidates, key=lambda x: all_pair_stats[(x[0], sym)]["win_rate"])
        s = all_pair_stats[(best_pid, sym)]
        logging.info("  %-10s  v%-3d %-27s  WR: %5.1f%%  Trades: %d  EV 1:2: %+.3fR",
                     sym, best_pid, _fvg_pset_label(best_pset), s["win_rate"] * 100, s["resolved"],
                     s.get("expected_value", {}).get("1:2", 0))
    return all_pair_stats, all_symbols, pset_ids


def _fvg_alert_id(symbol: str, timestamp: int) -> str:
    dt   = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    pair = symbol.lower().replace("/", "")
    return f"fvg_{dt.minute:02d}-{dt.hour:02d}-{dt.day:02d}-{dt.month:02d}-{dt.year}-{pair}"


def _scan_one_symbol_fvg(args: tuple) -> tuple:
    """Worker: scan 30m candles for FVG doji setups. Returns raw signal records."""
    symbol, settings, param_set_id, cutoff_ts, scan_run_id = args

    from db.local_db import LocalDB
    from analysis.smc_analyzer import SMCAnalyzer
    from alerts.alert_manager import AlertManager

    db       = LocalDB(settings.db_path)
    analyzer = SMCAnalyzer()
    dev_mode = settings.dev_mode
    timeframe, htf = "30m", "4h"

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
                "fvg_size_atr":     ev.get("fvg_size_atr"),
                "retrace_depth":    ev.get("retrace_depth"),
                "doji_body_pct":    ev.get("doji_body_pct"),
                "strategy":         "FVG30m",
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


def run_fvg_experiment(settings: Settings, db: LocalDB, alert_manager: AlertManager) -> None:
    """Scan 365 days of 30m candles for FVG doji setups; run 10-set param sweep."""
    from datetime import timedelta
    cutoff_ts  = int((datetime.now(timezone.utc) - timedelta(days=365)).timestamp())
    timeframe, htf = "30m", "4h"

    logging.info("Scanning 30m FVG doji (last 365 days) — HTF bias from 4h")
    logging.info("Cutoff: %s UTC", datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"))

    base_params = {**_FVG_SWEEP_BASE, "min_fvg_size_atr": 0.0, "min_retrace_pct": 0.0, "max_doji_body_pct": 1.0}
    param_set_id = db.get_or_create_param_set(base_params)

    try:
        fetcher = FXFetcher(settings)
        for symbol in settings.fx_pairs:
            _ensure_pair_data(symbol, timeframe, fetcher, db)
            _ensure_pair_data(symbol, htf, fetcher, db)
    except Exception as exc:
        logging.warning("Auto-fetch unavailable: %s", exc)

    scan_run_id = int(datetime.now(timezone.utc).timestamp())
    n_workers   = len(settings.fx_pairs)
    worker_args = [
        (symbol, settings, param_set_id, cutoff_ts, scan_run_id)
        for symbol in settings.fx_pairs
    ]
    logging.info("Launching %d FVG workers...", n_workers)

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
    logging.info("=== 30m FVG doji count (last 365 days) ===")
    for sym, cnt in pair_counts.items():
        logging.info("  %-10s %d", sym, cnt)
    logging.info("  %-10s %d", "TOTAL", sum(pair_counts.values()))

    overall = _compute_stats(all_trade_results)
    ev = overall.get("expected_value", {})
    logging.info("")
    logging.info("=== FVG Scan Stats ===")
    logging.info("  Resolved: %d  Wins: %d  Losses: %d  Open: %d",
                 overall["resolved"], overall["wins"], overall["losses"], overall["open"])
    logging.info("  Win rate: %.1f%%  |  EV 1:2: %+.3fR", overall["win_rate"] * 100, ev.get("1:2", 0))

    if all_raw_signals:
        _run_fvg_param_sweep(all_raw_signals, db)


def run_bos_experiment(
    settings: Settings,
    db: LocalDB,
    alert_manager: AlertManager,
    timeframe: str = "15m",
    htf: str = "4h",
) -> None:
    """Scan historical BOS events on the given timeframe / HTF pair."""
    from datetime import timedelta
    strategy = f"BOS{timeframe}"
    cutoff_ts = int((datetime.now(timezone.utc) - timedelta(days=365)).timestamp())

    logging.info("Scanning %s BOS (last 365 days) — MTF bias from %s", timeframe.upper(), htf.upper())
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

    # Auto-fetch 365 days of data for pairs that are missing or stale
    try:
        fetcher = FXFetcher(settings)
        for symbol in settings.fx_pairs:
            _ensure_pair_data(symbol, timeframe, fetcher, db)
            _ensure_pair_data(symbol, htf, fetcher, db)
    except Exception as exc:
        logging.warning("Auto-fetch unavailable (no API key?): %s", exc)

    pair_counts: dict = {}
    all_trade_results: list = []
    all_raw_signals: list = []

    # Run all pairs in parallel — workers render charts and return results,
    # main process handles all DB writes to avoid SQLite contention.
    scan_run_id = int(datetime.now(timezone.utc).timestamp())
    n_workers = len(settings.fx_pairs)
    worker_args = [
        (symbol, settings, param_set_id, cutoff_ts, timeframe, htf, active_params, scan_run_id, strategy)
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
    logging.info("=== 15m BOS count (last 365 days) ===")
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

    all_pair_stats, all_symbols, pset_ids = _run_param_sweep(all_raw_signals, db)
    _send_sweep_summary(alert_manager, all_raw_signals, all_pair_stats, all_symbols, pset_ids)


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

    return log_file


def check_logs_job(log_monitor: LogMonitor) -> None:
    try:
        log_monitor.scan()
    except Exception:
        logging.exception("Log monitor failed")


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

    if args.experiment_bos:
        run_bos_experiment(settings, db, alert_manager)
        db.close()
        return

    if args.nightly:
        run_nightly_sweep(db, alert_manager)
        db.close()
        return

    if args.experiment_bos_4h:
        run_bos_experiment(settings, db, alert_manager, timeframe="4h", htf="1d")
        db.close()
        return

    if args.experiment_fvg:
        run_fvg_experiment(settings, db, alert_manager)
        db.close()
        return

    # FXFetcher and LogMonitor are only needed for the scheduler (service mode)
    fetcher = FXFetcher(settings)
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

    logging.info("Starting continuous fetch scheduler. Fetch check runs every minute at second %d.", FETCH_CHECK_SECOND)
    logging.info("Log rotation enabled: 12h interval, 4 backups (~48h retention)")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logging.info("Scheduler shut down.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
