"""
Backfill XAUUSD 5m candles as far back as possible.

Fetches 5000-candle chunks (≈17 days each) going backward from the oldest
candle currently in the DB. Stops when the API returns no new candles or
the daily budget is exhausted.

Run from src/:  ../.venv/bin/python backfill_5m_xauusd.py
"""
import sys
import os
import time
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from config.settings import Settings
from data.fx_fetcher import FXFetcher
from db.local_db import LocalDB

# ── Config ────────────────────────────────────────────────────────────────────
SYMBOL    = "XAUUSD"
TIMEFRAME = "5m"
CHUNK     = 5000          # max per Twelve Data request
DELAY_S   = 12            # seconds between requests (free-tier rate limit: 8/min)
TARGET_DAYS = 120         # how far back to try (days)

settings = Settings.load_from_yaml(str(Path(__file__).parents[1] / "config.yaml"))
db       = LocalDB(settings.db_path)
fetcher  = FXFetcher(settings)

conn = sqlite3.connect(settings.db_path)

def get_oldest_ts() -> int:
    row = conn.execute(
        "SELECT MIN(timestamp) FROM fx_candles WHERE symbol=? AND timeframe=?",
        (SYMBOL, TIMEFRAME),
    ).fetchone()
    return row[0] if row and row[0] else int(datetime.now(timezone.utc).timestamp())

def count_candles() -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM fx_candles WHERE symbol=? AND timeframe=?",
        (SYMBOL, TIMEFRAME),
    ).fetchone()[0]

def api_used() -> int:
    row = conn.execute(
        "SELECT calls_used FROM api_daily_usage WHERE date=date('now')",
    ).fetchone()
    return row[0] if row else 0

# ── Main loop ─────────────────────────────────────────────────────────────────
target_oldest = datetime.now(timezone.utc) - timedelta(days=TARGET_DAYS)
target_ts     = int(target_oldest.timestamp())

print(f"XAUUSD 5m backfill — target: back to {target_oldest.strftime('%Y-%m-%d')} UTC")
print(f"API budget used today: {api_used()} / 800")
print(f"Starting candle count: {count_candles()}")
print()

total_inserted = 0
call_n = 0

while True:
    oldest_ts = get_oldest_ts()
    oldest_dt = datetime.fromtimestamp(oldest_ts, tz=timezone.utc)

    if oldest_ts <= target_ts:
        print(f"Reached target date {target_oldest.strftime('%Y-%m-%d')} — done.")
        break

    used = api_used()
    if used >= 800:
        print(f"API daily limit reached ({used}/800) — stopping.")
        break

    # Fetch chunk ending just before our oldest candle
    end_date_str = oldest_dt.strftime("%Y-%m-%d %H:%M:%S")
    call_n += 1
    print(f"[{call_n}] Fetching {CHUNK} candles ending {end_date_str} UTC "
          f"(budget: {used}/800) ...", end=" ", flush=True)

    try:
        candles = fetcher.fetch_historical(SYMBOL, TIMEFRAME, limit=CHUNK, end_date=end_date_str)
    except Exception as exc:
        print(f"ERROR: {exc}")
        break

    db.increment_api_calls(1)

    if not candles:
        print("no data returned — stopping.")
        break

    # Filter out candles already in DB (INSERT OR IGNORE handles dups, but count new ones)
    existing_oldest = oldest_ts
    new_ones = [c for c in candles if c["timestamp"] < existing_oldest]

    if not new_ones:
        print("no new candles before current oldest — stopping.")
        break

    db.insert_candles(SYMBOL, TIMEFRAME, candles)  # INSERT OR IGNORE handles dups

    new_oldest = min(c["timestamp"] for c in new_ones)
    new_oldest_dt = datetime.fromtimestamp(new_oldest, tz=timezone.utc)
    print(f"inserted {len(new_ones)} new | oldest now {new_oldest_dt.strftime('%Y-%m-%d %H:%M')} UTC")
    total_inserted += len(new_ones)

    if call_n < 50:  # safety cap
        time.sleep(DELAY_S)
    else:
        print("Safety cap of 50 calls reached — stopping.")
        break

# ── Summary ───────────────────────────────────────────────────────────────────
final_count = count_candles()
final_oldest_ts = get_oldest_ts()
final_oldest_dt = datetime.fromtimestamp(final_oldest_ts, tz=timezone.utc)
span_days = (datetime.now(timezone.utc).timestamp() - final_oldest_ts) / 86400

print()
print(f"Done. {call_n} API calls used, {total_inserted} candles inserted.")
print(f"Total 5m XAUUSD candles in DB: {final_count}")
print(f"Coverage: {final_oldest_dt.strftime('%Y-%m-%d')} → today  ({span_days:.0f} days)")
print(f"API budget now: {api_used()} / 800")

conn.close()
db.close()
