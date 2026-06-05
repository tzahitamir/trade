import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .telegram_notifier import TelegramNotifier


class LogMonitor:
    def __init__(self, log_file: Path, notifier: Optional[TelegramNotifier] = None):
        self.log_file = log_file
        self.notifier = notifier
        self.last_check = datetime.now(timezone.utc)

    def scan(self) -> None:
        lines = self._read_recent_lines()
        issues = []
        now = datetime.now(timezone.utc)

        for line in lines:
            timestamp = self._parse_timestamp(line)
            if not timestamp or timestamp <= self.last_check:
                continue
            if "ERROR" in line or "CRITICAL" in line or "[trade] fetch error" in line:
                issues.append(line.strip())

        self.last_check = now

        if issues:
            alert_text = "[trade] log monitor detected issues:\n" + "\n".join(issues[:10])
            self._send_alert(alert_text)

    def _read_recent_lines(self) -> List[str]:
        log_files = sorted(
            self.log_file.parent.glob(self.log_file.name + "*"),
            key=lambda path: path.stat().st_mtime,
        )

        lines: List[str] = []
        for path in log_files[-3:]:
            if path.is_file():
                try:
                    content = path.read_text(encoding="utf-8", errors="ignore")
                    lines.extend(content.splitlines())
                except OSError:
                    logging.warning("Unable to read log file %s", path)
        return lines[-1000:]

    def _parse_timestamp(self, line: str) -> Optional[datetime]:
        try:
            timestamp_str = line.split(" [", 1)[0]
            return datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            return None

    def _send_alert(self, message: str) -> None:
        if self.notifier:
            self.notifier.send_message(message)
        else:
            logging.warning("Log monitor detected issues but no notifier is configured.")
