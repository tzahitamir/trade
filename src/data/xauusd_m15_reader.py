"""
XAUUSD candle loaders — reads from MT5 EA file bridge.

EAs write to:
  Windows: %AppData%/MetaQuotes/Terminal/Common/Files/
  WSL:     /mnt/c/Users/*/AppData/Roaming/MetaQuotes/Terminal/Common/Files/

  XAUUSD_M15_export.mq5  →  XAUUSD_M15.csv   (15m bars, ~4+ years)
  XAUUSD_H4_export.mq5   →  XAUUSD_H4.csv    (H4 bars, ~10 years)

Returns candles as dicts: {'ts': datetime(UTC), 'open', 'high', 'low', 'close'}
"""

from __future__ import annotations

import csv
import glob
import os
from datetime import datetime, timezone
from pathlib import Path

_MT5_COMMON_GLOBS = [
    "/mnt/c/Users/*/AppData/Roaming/MetaQuotes/Terminal/Common/Files",
    os.path.expanduser("~/.wine/drive_c/users/*/AppData/Roaming/MetaQuotes/Terminal/Common/Files"),
]
CSV_FILENAME = "XAUUSD_M15.csv"
HB_FILENAME  = "XAUUSD_M15_heartbeat.txt"


def _common_dir() -> Path | None:
    for pattern in _MT5_COMMON_GLOBS:
        matches = glob.glob(pattern)
        if matches:
            return Path(matches[0])
    return None


def is_available() -> bool:
    d = _common_dir()
    if d is None:
        return False
    return (d / CSV_FILENAME).exists()


def load_mt5_candles() -> list[dict]:
    """
    Load all XAUUSD M15 candles from the MT5 EA CSV.
    Returns list sorted oldest→newest.
    Raises FileNotFoundError if the file doesn't exist yet.
    """
    d = _common_dir()
    if d is None:
        raise FileNotFoundError(
            "MT5 Common/Files directory not found.\n"
            "Is MT5 installed on Windows and accessible from WSL?"
        )

    path = d / CSV_FILENAME
    if not path.exists():
        raise FileNotFoundError(
            f"MT5 file not found: {path}\n"
            "Attach XAUUSD_M15_export.mq5 to the XAUUSD M15 chart in MT5."
        )

    from datetime import timedelta
    candles = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                dt = datetime.strptime(
                    row["datetime_utc"], "%Y.%m.%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
                # Broker clock drift can shift timestamps by a fixed number of
                # seconds (e.g. :41 instead of :00). Round to nearest 15m boundary.
                total_min = dt.hour * 60 + dt.minute
                rounded_min = round(total_min / 15) * 15
                dt = dt.replace(
                    hour=rounded_min // 60 % 24,
                    minute=rounded_min % 60,
                    second=0, microsecond=0
                )
                candles.append({
                    "ts":    dt,
                    "open":  float(row["open"]),
                    "high":  float(row["high"]),
                    "low":   float(row["low"]),
                    "close": float(row["close"]),
                })
            except (KeyError, ValueError):
                continue

    candles.sort(key=lambda c: c["ts"])
    return candles


def load_h4_candles() -> list[dict]:
    """
    Load XAUUSD H4 candles from XAUUSD_H4.csv written by XAUUSD_H4_export.mq5.
    Returns list sorted oldest→newest.
    Raises FileNotFoundError if the file doesn't exist.
    """
    d = _common_dir()
    if d is None:
        raise FileNotFoundError("MT5 Common/Files directory not found.")

    path = d / "XAUUSD_H4.csv"
    if not path.exists():
        raise FileNotFoundError(
            f"MT5 H4 file not found: {path}\n"
            "Attach XAUUSD_H4_export.mq5 to a XAUUSD chart in MT5."
        )

    candles = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                dt = datetime.strptime(
                    row["datetime_utc"], "%Y.%m.%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
                # Round to nearest 4h boundary (same broker-drift correction as M15)
                total_h = dt.hour + dt.minute / 60
                rounded_h = round(total_h / 4) * 4
                dt = dt.replace(
                    hour=rounded_h % 24,
                    minute=0, second=0, microsecond=0
                )
                candles.append({
                    "ts":    dt,
                    "open":  float(row["open"]),
                    "high":  float(row["high"]),
                    "low":   float(row["low"]),
                    "close": float(row["close"]),
                })
            except (KeyError, ValueError):
                continue

    candles.sort(key=lambda c: c["ts"])
    return candles


def h4_available() -> bool:
    d = _common_dir()
    return d is not None and (d / "XAUUSD_H4.csv").exists()


def check_heartbeat(max_stale_minutes: int = 10) -> tuple[bool, str]:
    """
    Check if the EA is still running.
    Returns (ok: bool, message: str).
    """
    d = _common_dir()
    if d is None:
        return False, "MT5 Common/Files not found"

    hb_path = d / HB_FILENAME
    if not hb_path.exists():
        return False, f"{HB_FILENAME} missing — EA not attached?"

    try:
        text = hb_path.read_text().strip()
        last = datetime.strptime(text, "%Y.%m.%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return False, "Heartbeat file unreadable"

    now = datetime.now(timezone.utc)
    age_min = (now - last).total_seconds() / 60
    if age_min > max_stale_minutes:
        return False, f"Heartbeat {age_min:.0f}m old — EA paused or MT5 closed?"

    return True, f"EA live (heartbeat {age_min:.1f}m ago)"
