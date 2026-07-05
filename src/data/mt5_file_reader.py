"""
Generic MT5 EA file reader.

Reads OHLCV candles from a CSV file written by GER40_M5_export.mq5 (or any
compatible EA attached to a different chart with matching input parameters).

Usage:
    from data.mt5_file_reader import read_mt5_candles, check_mt5_staleness

    candles = read_mt5_candles("TSLA_M5.csv")          # 5m bars, oldest→newest
    check_mt5_staleness("TSLA_heartbeat.txt")           # raises StaleFeedError if down

File location (Windows): %AppData%/MetaQuotes/Terminal/Common/Files/<CsvFileName>
File location (WSL):     /mnt/c/Users/*/AppData/Roaming/MetaQuotes/Terminal/Common/Files/
"""

from __future__ import annotations

import csv
import glob
from datetime import datetime, timezone
from pathlib import Path

_MT5_COMMON_GLOB = "/mnt/c/Users/*/AppData/Roaming/MetaQuotes/Terminal/Common/Files"
_STALE_THRESH_SEC = 10 * 60   # 10 minutes


class StaleFeedError(RuntimeError):
    """Raised when the MT5 EA heartbeat file is missing or too old."""


def _common_files_dir() -> Path | None:
    matches = glob.glob(_MT5_COMMON_GLOB)
    return Path(matches[0]) if matches else None


def _resolve(filename: str) -> Path | None:
    d = _common_files_dir()
    return (d / filename) if d else None


def check_mt5_staleness(hb_filename: str, now_utc: datetime | None = None) -> None:
    """Raise StaleFeedError if heartbeat file is missing or older than 10 minutes."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    path = _resolve(hb_filename)
    if path is None or not path.exists():
        raise StaleFeedError(f"MT5 heartbeat not found: {hb_filename}")
    try:
        text = path.read_text().strip()
        last = datetime.strptime(text, "%Y.%m.%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        raise StaleFeedError(f"MT5 heartbeat unreadable: {hb_filename}")
    age = (now_utc - last).total_seconds()
    if age > _STALE_THRESH_SEC:
        raise StaleFeedError(f"MT5 feed stale ({int(age//60)}m ago): {hb_filename}")


def read_mt5_candles(csv_filename: str, limit: int = 500) -> list[dict]:
    """
    Read up to `limit` candles from an EA-written CSV file.
    Returns list sorted oldest→newest, schema: {timestamp, open, high, low, close, volume}.
    Raises FileNotFoundError if the file doesn't exist.
    """
    path = _resolve(csv_filename)
    if path is None or not path.exists():
        raise FileNotFoundError(
            f"MT5 file not found: {csv_filename}\n"
            f"Attach the export EA to the chart and set CsvFileName={csv_filename}"
        )
    candles = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                dt = datetime.strptime(row["datetime_utc"], "%Y.%m.%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
                candles.append({
                    "timestamp": int(dt.timestamp()),
                    "open":   float(row["open"]),
                    "high":   float(row["high"]),
                    "low":    float(row["low"]),
                    "close":  float(row["close"]),
                    "volume": int(row["tick_volume"]),
                })
            except (KeyError, ValueError):
                continue
    candles.sort(key=lambda c: c["timestamp"])
    return candles[-limit:] if len(candles) > limit else candles


def resample_to_15m(candles_5m: list[dict]) -> list[dict]:
    """Aggregate 5m bars into 15m bars. Input and output: oldest→newest."""
    out, i = [], 0
    while i < len(candles_5m):
        ts0 = candles_5m[i]["timestamp"]
        al  = (ts0 // 900) * 900
        g   = [c for c in candles_5m[i:i + 3] if c["timestamp"] < al + 900]
        if not g:
            i += 1
            continue
        out.append({
            "timestamp": al,
            "open":   g[0]["open"],
            "high":   max(c["high"] for c in g),
            "low":    min(c["low"]  for c in g),
            "close":  g[-1]["close"],
            "volume": sum(c["volume"] for c in g),
        })
        i += len(g)
    return out
