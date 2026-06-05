import argparse
import logging
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import List, Optional
import time

from apscheduler.schedulers.blocking import BlockingScheduler

from config.settings import Settings
from data.fx_fetcher import FXFetcher
from db.local_db import LocalDB
from alerts.alert_manager import AlertManager
from alerts.log_monitor import LogMonitor

FETCH_CHECK_SECOND = 10
INITIAL_LOOKBACK_HOURS = 720  # 30 days
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


def run_bos_experiment(settings: Settings, db: LocalDB, alert_manager: AlertManager) -> None:
    """
    CLI dev mode: scans 15m historical data for BOS events in the last 48h.

    For each unique BOS:
      - Checks the 4h bias (HTF) at the moment of the BOS using swing high/low
      - Generates a chart with BOS + sweep arrows, HTF bias label, and 10 lookahead candles
      - Saves chart to data/charts/{alert_id}.png
      - Telegram is DISABLED in dev mode

    Prints a per-pair count summary at the end.
    """
    from datetime import timedelta
    timeframe = "15m"
    htf = "4h"
    cutoff_ts = int((datetime.now(timezone.utc) - timedelta(hours=48)).timestamp())
    dev_mode = getattr(settings, "dev_mode", True)

    logging.info("Scanning 15m BOS (last 48h) — MTF bias from 4h — Telegram OFF")
    logging.info("Cutoff: %s UTC", datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"))

    pair_counts: dict = {}

    for symbol in settings.fx_pairs:
        candles_desc = db.query_recent(symbol, timeframe, limit=5000)
        if not candles_desc:
            logging.info("No %s data for %s, skipping", timeframe, symbol)
            pair_counts[symbol] = 0
            continue

        # Pre-fetch 4h candles for HTF bias (newest-first)
        candles_4h = db.query_recent(symbol, htf, limit=500)

        n = len(candles_desc)
        count = 0
        seen_levels: set = set()

        for k in range(50, n):
            bos_ts = candles_desc[n - 1 - k]["timestamp"]
            if bos_ts < cutoff_ts:
                continue

            window = candles_desc[n - 1 - k:]  # newest-first, k+1 candles
            lookahead = candles_desc[max(0, n - k - 11) : n - 1 - k] if dev_mode else None

            # HTF bias: 4h swing state at the exact moment of the BOS
            htf_bias = alert_manager.analyzer.get_htf_bias(candles_4h, bos_ts) if candles_4h else None

            alerts = alert_manager.evaluate(
                symbol, timeframe, window,
                lookahead_candles=lookahead,
                htf_bias=htf_bias,
            )

            for alert in alerts:
                ev = alert["event"]
                key = (ev["direction"], round(ev["broken_level"], 5))
                if key in seen_levels:
                    continue
                seen_levels.add(key)
                count += 1

                ts_str = datetime.fromtimestamp(ev["breakout_ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                sweep = ev.get("liquidity_sweep") or {}
                sweep_str = ""
                if sweep:
                    sweep_str = "  sweep@" + datetime.fromtimestamp(
                        sweep["timestamp"], tz=timezone.utc
                    ).strftime("%H:%M")
                logging.info(
                    "  [%s] %s BOS  id=%s  level=%.5f  str=%.2f  4H=%-8s  ts=%s%s",
                    symbol, ev["direction"].upper(), alert["alert_id"],
                    ev["broken_level"], ev["break_strength"],
                    (htf_bias or "?").upper(), ts_str, sweep_str,
                )
                # Telegram disabled in dev mode — charts are saved to data/charts/

        pair_counts[symbol] = count

    logging.info("")
    logging.info("=== 15m BOS count (last 48h, with liquidity sweep) ===")
    for sym, cnt in pair_counts.items():
        logging.info("  %-10s %d", sym, cnt)
    logging.info("  %-10s %d", "TOTAL", sum(pair_counts.values()))


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
