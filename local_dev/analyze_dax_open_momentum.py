#!/usr/bin/env python3
"""
DAX open momentum timing analysis.

For each session day with a SERPE signal, measures:
  1. When the first 15m BOS fires (direction confirmed)
  2. Expansion size at 30/45/60/90 min snapshots
  3. Whether early expansion size predicts WIN/LOSS
"""
import sys, statistics, logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from db.local_db import LocalDB
from analysis.smc_analyzer import SMCAnalyzer
from main import _dax_session_window, _evaluate_dax_outcome

logging.basicConfig(level=logging.WARNING)

DB_PATH   = str(Path(__file__).resolve().parents[1] / "src" / "data" / "trade.db")
SCAN_DAYS = 70


def median(lst):
    if not lst: return float("nan")
    s = sorted(lst); n = len(s)
    return s[n//2] if n%2 else (s[n//2-1]+s[n//2])/2


def report(label, sigs, base_n):
    n = len(sigs)
    if n == 0:
        print(f"    {label:50s}  n=0")
        return
    wins = sum(1 for s in sigs if s["outcome"] == "WIN")
    rrs  = [s["eff_r"] for s in sigs if s["outcome"] in ("WIN","LOSS")]
    wr   = wins/n
    ev   = (wins/n)*statistics.mean(rrs) - (1-wins/n) if rrs else float("nan")
    cov  = n/base_n*100
    print(f"    {label:50s}  n={n:>2} ({cov:3.0f}%)  WR={wr*100:5.1f}%  EV={ev:+.2f}R")


def main():
    db       = LocalDB(DB_PATH)
    analyzer = SMCAnalyzer()
    import pytz
    IL_TZ    = pytz.timezone("Asia/Jerusalem")
    cutoff   = int((datetime.now(timezone.utc) - timedelta(days=SCAN_DAYS)).timestamp())

    seen_dates = set()
    for c in db.query_recent("DAX", "15m", limit=2000):
        dt = datetime.fromtimestamp(c["timestamp"], tz=IL_TZ).date()
        if dt.weekday() < 5 and c["timestamp"] >= cutoff:
            seen_dates.add(dt)
    trading_dates = sorted(seen_dates)

    all_15m = list(reversed(db.query_recent("DAX", "15m", limit=2000)))
    all_5m  = list(reversed(db.query_recent("DAX", "5m",  limit=6000)))

    records = []
    no_signal_days = 0

    for trade_date in trading_dates:
        start_ts, end_ts = _dax_session_window(trade_date)

        sess_15m = sorted([c for c in all_15m if start_ts <= c["timestamp"] <= end_ts],
                          key=lambda c: c["timestamp"])
        if len(sess_15m) < 3:
            continue

        pre_15m = sorted([c for c in all_15m if c["timestamp"] < start_ts],
                         key=lambda c: c["timestamp"])[-16:]
        day_5m  = sorted([c for c in all_5m  if c["timestamp"] >= start_ts],
                         key=lambda c: c["timestamp"])

        # Get expansion state (bos + peak)
        exp = analyzer.detect_dax_expansion_state(
            sess_15m,
            params={"symbol": "DAX", "min_expansion_atr": 1.0},
            candles_15m_presession=pre_15m,
        )

        # Get full signal
        sigs = analyzer.detect_dax_session_setup(
            sess_15m, day_5m,
            params={"tp_pct": 0.5, "sl_atr_mult": 0.5,
                    "min_expansion_atr": 1.0, "entry_zone_min_pct": 0.5,
                    "swing_lookback_5m": 12, "min_break_str_5m": 0.3,
                    "symbol": "DAX"},
            candles_15m_presession=pre_15m,
        )

        if not sigs:
            no_signal_days += 1
            continue
        sig = sigs[0]

        outcome, eff_r = _evaluate_dax_outcome(sig, [c for c in day_5m if c["timestamp"] > sig["breakout_ts"]])
        if outcome == "OPEN":
            continue

        if not exp:
            continue

        bos_ts  = exp["bos_15m_ts"]
        peak_ts = exp["peak_ts"]
        bos_min  = (bos_ts  - start_ts) / 60
        peak_min = (peak_ts - start_ts) / 60
        bos_idt  = datetime.fromtimestamp(bos_ts,  tz=IL_TZ).strftime("%H:%M")
        peak_idt = datetime.fromtimestamp(peak_ts, tz=IL_TZ).strftime("%H:%M")

        exp_range = exp["expansion_range"]

        # Expansion range at time snapshots (using 15m candles up to that point)
        def exp_at_t(minutes):
            snap_ts = start_ts + minutes * 60
            candles = [c for c in sess_15m if c["timestamp"] <= snap_ts]
            if not candles:
                return 0.0
            # Only measure from session start to this point
            if exp["expansion_dir"] == "bullish":
                lo = min(c["low"]  for c in candles)
                hi = max(c["high"] for c in candles)
                return hi - lo
            else:
                lo = min(c["low"]  for c in candles)
                hi = max(c["high"] for c in candles)
                return hi - lo

        snap30  = exp_at_t(30)
        snap45  = exp_at_t(45)
        snap60  = exp_at_t(60)
        snap90  = exp_at_t(90)

        # ATR for normalisation
        atr = analyzer.calculate_atr(list(reversed(pre_15m))) or 1.0

        records.append({
            "date": trade_date, "outcome": outcome, "eff_r": eff_r,
            "bos_min": bos_min, "bos_idt": bos_idt,
            "peak_min": peak_min, "peak_idt": peak_idt,
            "exp_range": exp_range, "atr": atr,
            "exp_atr": exp_range / atr,
            "snap30": snap30, "snap45": snap45,
            "snap60": snap60, "snap90": snap90,
            "snap30_atr": snap30/atr, "snap45_atr": snap45/atr,
            "snap60_atr": snap60/atr, "snap90_atr": snap90/atr,
        })

    n = len(records)
    db.close()

    print(f"DAX open momentum analysis  ({n} resolved signals, {no_signal_days} days no signal)")
    print(f"Session: 09:00–12:30 IDT  |  Scan: {SCAN_DAYS} days\n")

    # ── 1. When does the 15m BOS fire? ───────────────────────────────────────
    bos_mins = sorted(r["bos_min"] for r in records)
    print("── When does the first 15m BOS fire? (direction confirmed) ─────────────")
    print(f"  Range:  {min(bos_mins):.0f}–{max(bos_mins):.0f} min after open "
          f"({9+min(bos_mins)/60:.2f}–{9+max(bos_mins)/60:.2f} IDT)")
    print(f"  Median: {median(bos_mins):.0f} min  ({9+median(bos_mins)/60:.2f} IDT)")
    for cutoff in [15, 30, 45, 60]:
        cnt = sum(1 for r in records if r["bos_min"] <= cutoff)
        idt = f"{9+cutoff//60}:{cutoff%60:02d}"
        print(f"  BOS within {cutoff:2d} min ({idt} IDT): {cnt}/{n} days ({cnt/n*100:.0f}%)")
    print()

    # ── 2. BOS timing vs outcome ──────────────────────────────────────────────
    print("── First 15m BOS timing vs outcome ──────────────────────────────────────")
    for cutoff in [15, 30, 45, 60]:
        idt = f"{9+cutoff//60}:{cutoff%60:02d}"
        report(f"BOS ≤ {cutoff:2d} min / {idt} IDT (fast direction)",
               [r for r in records if r["bos_min"] <= cutoff], n)
        report(f"BOS >  {cutoff:2d} min / {idt} IDT (slow direction)",
               [r for r in records if r["bos_min"] >  cutoff], n)
        print()

    # ── 3. Expansion at time snapshots ───────────────────────────────────────
    print("── Expansion size at time snapshots (×ATR) ──────────────────────────────")
    print(f"  {'':30s} {'All':>6s}  {'WIN':>6s}  {'LOSS':>6s}")
    wins  = [r for r in records if r["outcome"]=="WIN"]
    losses= [r for r in records if r["outcome"]=="LOSS"]
    for key, label in [("snap30_atr","09:30 IDT (+30min)"),
                       ("snap45_atr","09:45 IDT (+45min)"),
                       ("snap60_atr","10:00 IDT (+60min)"),
                       ("snap90_atr","10:30 IDT (+90min)"),
                       ("exp_atr",   "Final expansion")]:
        all_v = [r[key] for r in records]
        w_v   = [r[key] for r in wins]
        l_v   = [r[key] for r in losses]
        print(f"  {label:30s}  "
              f"{median(all_v):6.2f}   {median(w_v):6.2f}   {median(l_v):6.2f}")
    print()

    # ── 4. Early expansion size vs outcome ───────────────────────────────────
    print("── Early expansion size at +30min vs outcome ───────────────────────────")
    for cutoff in [3.0, 5.0, 7.0, 10.0]:
        report(f"  30min expansion ≤ {cutoff:.0f}×ATR (weak open)",
               [r for r in records if r["snap30_atr"] <= cutoff], n)
        report(f"  30min expansion >  {cutoff:.0f}×ATR (strong open)",
               [r for r in records if r["snap30_atr"] >  cutoff], n)
        print()

    print("── Early expansion size at +45min vs outcome ───────────────────────────")
    for cutoff in [5.0, 7.0, 10.0]:
        report(f"  45min expansion ≤ {cutoff:.0f}×ATR",
               [r for r in records if r["snap45_atr"] <= cutoff], n)
        report(f"  45min expansion >  {cutoff:.0f}×ATR",
               [r for r in records if r["snap45_atr"] >  cutoff], n)
        print()

    # ── 5. Per-signal detail ──────────────────────────────────────────────────
    print("── Per-signal detail ────────────────────────────────────────────────────")
    print(f"  {'Date':12s} {'Out':5s} {'EffR':5s}  {'BOS':>5s}  {'Pk':>5s}  "
          f"{'30m':>5s}  {'45m':>5s}  {'60m':>5s}  {'90m':>5s}  {'Final':>5s}  (×ATR)")
    print(f"  {'────':12s} {'───':5s} {'────':5s}  {'min':>5s}  {'min':>5s}  "
          f"{'ATR':>5s}  {'ATR':>5s}  {'ATR':>5s}  {'ATR':>5s}  {'ATR':>5s}")
    for r in sorted(records, key=lambda x: x["date"]):
        print(f"  {str(r['date']):12s} {r['outcome']:5s} {r['eff_r']:5.2f}"
              f"  {r['bos_min']:5.0f}  {r['peak_min']:5.0f}"
              f"  {r['snap30_atr']:5.1f}  {r['snap45_atr']:5.1f}"
              f"  {r['snap60_atr']:5.1f}  {r['snap90_atr']:5.1f}"
              f"  {r['exp_atr']:5.1f}")


if __name__ == "__main__":
    main()
