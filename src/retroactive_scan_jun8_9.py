"""
Retroactive BOS15m scan for June 8-9 2026.

Walks every 15m bar close from June 8 00:00 UTC through the latest candle,
applies gold params (str>=0.7, htf_only=True, sl=broken_level) for each
alert pair, evaluates TP/SL outcome using subsequent candles, and saves
charts + a text summary to data/charts/prod/.

Run from src/: ../.venv/bin/python retroactive_scan_jun8_9.py
"""

import sys
import os
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from pathlib import Path as _Path
from db.local_db import LocalDB
from alerts.alert_manager import AlertManager
from config.settings import Settings

# ── config ─────────────────────────────────────────────────────────────────────
PAIRS = ["NZDUSD", "EURJPY", "EURUSD", "USDCHF", "USDCAD", "XAUUSD"]
TF = "15m"
CONTEXT_LIMIT = 250       # candles passed to detect_bos for swing context
T_START = int(datetime(2026, 6, 8, 0, 0, 0, tzinfo=timezone.utc).timestamp())

settings = Settings.load_from_yaml(str(_Path(__file__).parents[1] / "config.yaml"))
db = LocalDB(settings.db_path)
am = AlertManager(settings, db=db)
prod_dir = am.prod_charts_dir
prod_dir.mkdir(parents=True, exist_ok=True)

results = []          # list of dicts — one per unique BOS event (first detection)
seen_levels: set = set()   # (symbol, direction, rounded broken_level) dedup key

for symbol in PAIRS:
    print(f"\n{'='*60}")
    print(f"Scanning {symbol} ...")

    # Full candle history newest-first (need context before June 8)
    all_candles = db.query_recent(symbol, TF, limit=5000)
    candles_4h  = db.query_recent(symbol, "4h", limit=500)
    if not all_candles:
        print(f"  {symbol}: no candles — skip")
        continue

    # Gold params for this pair
    gold_map = db.get_gold_params("BOS15m", symbol)
    gold_entry = gold_map.get(symbol)
    if not gold_entry:
        print(f"  {symbol}: no gold params — skip")
        continue
    gold_params = db.get_param_set_by_id(gold_entry["param_set_id"])
    print(f"  gold params: str>={gold_params.get('min_break_strength','?')} "
          f"htf_only={gold_params.get('htf_aligned_only','?')} "
          f"sl={gold_params.get('sl_mode','?')}")

    # Chronological list of June 8+ bar timestamps (oldest-first)
    june_bars = sorted(
        c["timestamp"] for c in all_candles if c["timestamp"] >= T_START
    )
    print(f"  bars to scan: {len(june_bars)}  "
          f"({datetime.fromtimestamp(june_bars[0], tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} → "
          f"{datetime.fromtimestamp(june_bars[-1], tz=timezone.utc).strftime('%Y-%m-%d %H:%M')})")

    # Build a lookup dict: timestamp → candle for fast slicing
    candle_by_ts = {c["timestamp"]: c for c in all_candles}
    all_ts_sorted = sorted(candle_by_ts)  # oldest-first

    for bar_ts in june_bars:
        # Context: up to CONTEXT_LIMIT candles ending at (and including) bar_ts, newest-first
        context_ts = [t for t in all_ts_sorted if t <= bar_ts][-CONTEXT_LIMIT:]
        context = [candle_by_ts[t] for t in reversed(context_ts)]  # newest-first

        if len(context) < 60:
            continue

        # Session gate: same filter as live service (07:00–21:00 UTC)
        bar_hour = datetime.fromtimestamp(bar_ts, tz=timezone.utc).hour
        if not (7 <= bar_hour < 21):
            continue

        # HTF bias at this bar
        htf_bias = am.analyzer.get_htf_bias(candles_4h, bar_ts)

        # evaluate_production with min_breakout_ts=bar_ts → only exact-bar breakouts
        alerts = am.evaluate_production(
            symbol, TF, context,
            candles_4h=candles_4h,
            htf_bias=htf_bias,
            gold_params=gold_params,
            min_breakout_ts=bar_ts,
        )

        if not alerts:
            continue

        # Lookahead: candles strictly after bar_ts (chronological)
        lookahead_chron = [candle_by_ts[t] for t in all_ts_sorted if t > bar_ts]

        for alert in alerts:
            ev = alert["event"]
            alert_id = alert["alert_id"]
            direction = ev.get("direction", "?")
            broken_level = ev.get("broken_level", 0)
            entry = alert.get("entry", 0)
            sl    = alert.get("sl", 0)
            tp    = alert.get("tp", 0)
            r     = alert.get("r_ratio")

            bar_dt = datetime.fromtimestamp(bar_ts, tz=timezone.utc)

            # Dedup: each BOS level should only appear once (first detection)
            dedup_key = (symbol, direction, round(broken_level, 5))
            if dedup_key in seen_levels:
                # Clean up the chart evaluate_production already rendered
                if alert.get("image_path") and Path(alert["image_path"]).exists():
                    Path(alert["image_path"]).unlink()
                continue
            seen_levels.add(dedup_key)

            # Evaluate outcome using subsequent candles
            outcome, eff_r = AlertManager.evaluate_bos_outcome(
                context, ev, lookahead_chron, candles_4h,
                trigger_ts=None, sl_mode=gold_params.get("sl_mode", "broken_level"),
            )

            print(f"  ALERT {bar_dt.strftime('%Y-%m-%d %H:%M')} | {direction:8s} | "
                  f"entry={entry:.5f} sl={sl:.5f} tp={tp:.5f} | "
                  f"outcome={outcome:10s} eff_r={eff_r}")

            # Rename the chart file evaluate_production already created, embedding outcome
            existing_chart = alert.get("image_path")
            if existing_chart and Path(existing_chart).exists():
                outcome_tag = outcome.replace(" ", "_").upper()
                stem = Path(existing_chart).stem
                new_name = f"{stem}-{outcome_tag}.png"
                new_path = prod_dir / new_name
                Path(existing_chart).rename(new_path)
                chart_saved = str(new_path)
            else:
                chart_saved = None

            results.append({
                "symbol":    symbol,
                "bar_dt":    bar_dt.strftime("%Y-%m-%d %H:%M UTC"),
                "direction": direction,
                "entry":     entry,
                "sl":        sl,
                "tp":        tp,
                "r_ratio":   r,
                "htf_bias":  htf_bias,
                "outcome":   outcome,
                "eff_r":     eff_r,
                "alert_id":  alert_id,
                "chart":     chart_saved,
            })

# ── Summary ────────────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"TOTAL ALERTS FIRED: {len(results)}")

if results:
    wins   = [r for r in results if "WIN" in r["outcome"]]
    losses = [r for r in results if "LOSS" in r["outcome"]]
    opens  = [r for r in results if r["outcome"] == "OPEN"]
    wr = len(wins) / (len(wins) + len(losses)) * 100 if (wins or losses) else 0

    print(f"  WIN: {len(wins)}   LOSS: {len(losses)}   OPEN: {len(opens)}")
    print(f"  Win rate (resolved): {wr:.0f}%")
    print()

    # Per-pair breakdown
    by_pair = {}
    for r in results:
        by_pair.setdefault(r["symbol"], []).append(r)
    for sym, rs in by_pair.items():
        w = sum(1 for r in rs if "WIN" in r["outcome"])
        l = sum(1 for r in rs if "LOSS" in r["outcome"])
        print(f"  {sym}: {len(rs)} alert(s)  {w}W/{l}L")

    # Detailed table
    print()
    print(f"{'Symbol':<8} {'Bar (UTC)':<18} {'Dir':<8} {'Entry':>10} {'SL':>10} {'TP':>10} {'Outcome':<12} {'effR'}")
    print("-" * 90)
    for r in sorted(results, key=lambda x: x["bar_dt"]):
        print(f"{r['symbol']:<8} {r['bar_dt']:<18} {r['direction']:<8} "
              f"{r['entry']:>10.5f} {r['sl']:>10.5f} {r['tp']:>10.5f} "
              f"{r['outcome']:<12} {r['eff_r'] if r['eff_r'] is not None else '?'}")

    # Save summary file to prod dir
    summary_path = prod_dir / "summary_jun8_9.txt"
    with open(summary_path, "w") as f:
        f.write(f"BOS15m Retroactive Scan — June 8-9 2026\n")
        f.write(f"Pairs: {', '.join(PAIRS)}\n")
        f.write(f"Gold params: str>=0.7, htf_only=True, sl=broken_level\n\n")
        f.write(f"Total alerts: {len(results)}\n")
        f.write(f"WIN: {len(wins)}  LOSS: {len(losses)}  OPEN: {len(opens)}\n")
        f.write(f"Win rate (resolved): {wr:.0f}%\n\n")
        f.write(f"{'Symbol':<8} {'Bar (UTC)':<18} {'Dir':<8} {'Entry':>10} {'SL':>10} {'TP':>10} {'Outcome':<12} {'effR'}\n")
        f.write("-" * 90 + "\n")
        for r in sorted(results, key=lambda x: x["bar_dt"]):
            f.write(f"{r['symbol']:<8} {r['bar_dt']:<18} {r['direction']:<8} "
                    f"{r['entry']:>10.5f} {r['sl']:>10.5f} {r['tp']:>10.5f} "
                    f"{r['outcome']:<12} {r['eff_r'] if r['eff_r'] is not None else '?'}\n")
    print(f"\nSummary saved to: {summary_path}")
else:
    print("No alerts fired for June 8-9 with current gold params.")

db.close()
