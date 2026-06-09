"""Tests for LogMonitor — timezone regression and stale-error suppression."""
import time
from datetime import datetime, timedelta
from pathlib import Path
from alerts.log_monitor import LogMonitor


def _write_log(path: Path, lines: list[str]):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def test_old_errors_not_reported_after_restart(tmp_path):
    """Errors that occurred before LogMonitor was created must not trigger alerts."""
    log = tmp_path / "trade.log"
    old_time = datetime.now() - timedelta(minutes=30)
    _write_log(log, [
        f"{_ts(old_time)} [ERROR] Failed to fetch EURUSD 5m: 429 rate limit",
        f"{_ts(old_time)} [ERROR] Failed to fetch NZDUSD 15m: 429 rate limit",
    ])

    notifier = None
    monitor = LogMonitor(log, notifier)
    alerts = []
    monitor._send_alert = lambda msg: alerts.append(msg)
    monitor.scan()

    assert alerts == [], "Old errors should not be replayed on first scan"


def test_new_errors_are_reported(tmp_path):
    """Errors written after LogMonitor starts must trigger an alert."""
    log = tmp_path / "trade.log"
    log.write_text("", encoding="utf-8")

    monitor = LogMonitor(log, None)
    monitor._send_alert = lambda msg: alerts.append(msg)
    alerts = []

    # Write new error after monitor created
    future = datetime.now() + timedelta(seconds=2)
    _write_log(log, [f"{_ts(future)} [ERROR] Failed to fetch EURUSD 15m: timeout"])

    monitor.scan()
    assert len(alerts) == 1
    assert "EURUSD" in alerts[0]


def test_no_duplicate_alert_on_second_scan(tmp_path):
    """Same error line must not fire again on subsequent scans."""
    log = tmp_path / "trade.log"
    future = datetime.now() + timedelta(seconds=2)
    _write_log(log, [f"{_ts(future)} [ERROR] some error"])

    monitor = LogMonitor(log, None)
    alerts = []
    monitor._send_alert = lambda msg: alerts.append(msg)

    monitor.scan()  # first scan — should alert
    monitor.scan()  # second scan — same line, must not alert again

    assert len(alerts) == 1


def test_multiple_errors_batched_into_one_alert(tmp_path):
    """Multiple new errors in one scan window produce a single Telegram message."""
    log = tmp_path / "trade.log"
    future = datetime.now() + timedelta(seconds=2)
    lines = [
        f"{_ts(future)} [ERROR] error A",
        f"{_ts(future)} [ERROR] error B",
        f"{_ts(future)} [ERROR] error C",
    ]
    _write_log(log, lines)

    monitor = LogMonitor(log, None)
    alerts = []
    monitor._send_alert = lambda msg: alerts.append(msg)
    monitor.scan()

    assert len(alerts) == 1
    assert "error A" in alerts[0]
    assert "error B" in alerts[0]


def test_info_lines_not_reported(tmp_path):
    """INFO log lines must never trigger an alert."""
    log = tmp_path / "trade.log"
    future = datetime.now() + timedelta(seconds=2)
    _write_log(log, [
        f"{_ts(future)} [INFO] Scheduler started",
        f"{_ts(future)} [INFO] Fetch: 15m | used 10/800",
    ])

    monitor = LogMonitor(log, None)
    alerts = []
    monitor._send_alert = lambda msg: alerts.append(msg)
    monitor.scan()

    assert alerts == []
