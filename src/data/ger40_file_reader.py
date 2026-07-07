"""
Reads GER40 5m candles from the CSV file written by the MT5 EA.

File location (Windows): %AppData%/MetaQuotes/Terminal/Common/Files/GER40_M5.csv
File location (WSL):     /mnt/c/Users/Administrator/AppData/Roaming/MetaQuotes/
                           Terminal/Common/Files/GER40_M5.csv

Returns candles as dicts matching the DB schema:
  {"timestamp": int_utc, "open": float, "high": float, "low": float,
   "close": float, "volume": int}

Raises FileNotFoundError if the file doesn't exist.
Raises StaleFeedError   if the heartbeat is too old (MT5 not running).
"""

from __future__ import annotations

import csv
import glob
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path


# ── Paths ─────────────────────────────────────────────────────────────────────

# MT5 Common\Files is shared across all terminals on the machine
_MT5_COMMON_GLOB = (
    "/mnt/c/Users/*/AppData/Roaming/MetaQuotes/Terminal/Common/Files"
)

CSV_FILENAME = "GER40_M5.csv"
HB_FILENAME  = "GER40_heartbeat.txt"

# How old (seconds) a heartbeat can be before we consider MT5 down.
# During pre-market (03:00–07:00 UTC) we use a tight threshold.
STALE_THRESH_SEC = 10 * 60   # 10 minutes


class StaleFeedError(RuntimeError):
    """Raised when the MT5 EA heartbeat file is too old."""


def _find_common_files_dir() -> Path | None:
    matches = glob.glob(_MT5_COMMON_GLOB)
    if matches:
        return Path(matches[0])
    return None


def get_file_paths() -> tuple[Path, Path] | tuple[None, None]:
    """Return (csv_path, heartbeat_path) or (None, None) if not found."""
    d = _find_common_files_dir()
    if d is None:
        return None, None
    return d / CSV_FILENAME, d / HB_FILENAME


def read_heartbeat(hb_path: Path) -> datetime | None:
    """Parse the UTC timestamp written by the EA. Returns None if missing."""
    try:
        text = hb_path.read_text().strip()
        # Format: "2026.06.18 04:05:30"  (MQL5 TimeToString format)
        return datetime.strptime(text, "%Y.%m.%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except Exception:
        return None


def check_staleness(hb_path: Path | None, now_utc: datetime | None = None) -> None:
    """
    Raise StaleFeedError if the heartbeat file is missing or too old.
    Only called during the pre-market window (03:00–07:00 UTC).
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    # Only enforce during the window we care about
    h = now_utc.hour
    in_window = 3 <= h < 7

    if not in_window:
        return  # outside window, don't care

    if hb_path is None or not hb_path.exists():
        raise StaleFeedError(
            "MT5 EA heartbeat file not found — is MT5 running with GER40_M5_export EA?"
        )

    last_update = read_heartbeat(hb_path)
    if last_update is None:
        raise StaleFeedError("MT5 EA heartbeat file unreadable.")

    age = (now_utc - last_update).total_seconds()
    if age > STALE_THRESH_SEC:
        mins = int(age // 60)
        raise StaleFeedError(
            f"MT5 EA last updated {mins}m ago — EA may be paused or MT5 crashed."
        )


def read_candles(limit: int = 350) -> list[dict]:
    """
    Read up to `limit` candles from the EA-written CSV.
    Returns list sorted oldest→newest, same schema as DB query_recent.
    Raises FileNotFoundError or StaleFeedError on problems.
    """
    csv_path, hb_path = get_file_paths()

    if csv_path is None or not csv_path.exists():
        raise FileNotFoundError(
            f"GER40 live file not found. Expected: {_MT5_COMMON_GLOB}/{CSV_FILENAME}\n"
            "Attach GER40_M5_export EA to the GER40 M5 chart in MT5."
        )

    candles = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                dt = datetime.strptime(
                    row["datetime_utc"], "%Y.%m.%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
                candles.append(
                    {
                        "timestamp": int(dt.timestamp()),
                        "open":   float(row["open"]),
                        "high":   float(row["high"]),
                        "low":    float(row["low"]),
                        "close":  float(row["close"]),
                        "volume": int(row["tick_volume"]),
                    }
                )
            except (KeyError, ValueError):
                continue

    candles.sort(key=lambda c: c["timestamp"])
    return candles[-limit:] if len(candles) > limit else candles
