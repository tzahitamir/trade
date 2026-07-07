"""
GER40 M5 candle loader — reads from MT5 EA file bridge (GER40_M5_export.mq5).

Returns candles as dicts: {'ts': datetime(UTC), 'open', 'high', 'low', 'close'}
"""

from __future__ import annotations

import csv
import glob
from datetime import datetime, timezone
from pathlib import Path

_MT5_COMMON_GLOB = "/mnt/c/Users/*/AppData/Roaming/MetaQuotes/Terminal/Common/Files"
CSV_FILENAME = "GER40_M5.csv"
HB_FILENAME  = "GER40_M5_heartbeat.txt"


def _common_dir() -> Path | None:
    matches = glob.glob(_MT5_COMMON_GLOB)
    return Path(matches[0]) if matches else None


def is_available() -> bool:
    d = _common_dir()
    return d is not None and (d / CSV_FILENAME).exists()


def load_mt5_candles() -> list[dict]:
    d = _common_dir()
    if d is None:
        raise FileNotFoundError("MT5 Common/Files directory not found.")
    path = d / CSV_FILENAME
    if not path.exists():
        raise FileNotFoundError(f"MT5 file not found: {path}")

    candles = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                dt = datetime.strptime(
                    row["datetime_utc"], "%Y.%m.%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
                total_min   = dt.hour * 60 + dt.minute
                rounded_min = round(total_min / 5) * 5
                dt = dt.replace(
                    hour=rounded_min // 60 % 24,
                    minute=rounded_min % 60,
                    second=0, microsecond=0,
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
