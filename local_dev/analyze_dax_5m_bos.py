#!/usr/bin/env python3
"""
DAX 5m counter-BOS behaviour analysis.

Questions answered:
  1. When does the 5m counter-BOS fire? (minutes after open, after peak)
  2. What % of the expansion range does price actually retrace toward origin?
  3. WR / EV waterfall: how does outcome change as TP is set at 30%–100% of expansion?
  4. Does entry distance from peak (entry quality) predict outcome?
  5. How does the 5m expansion (mini-BOS range) compare to the 15m expansion?
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
SCAN_DAYS = 75
BASE_SL_ATR  = 0.5
BASE_TP_PCT  = 0.5


def median(lst):
    if not lst: return float("nan")
    s = sorted(lst); n = len(s)
    return s[n//2] if n % 2 else (s[n//2-1] + s[n//2]) / 2


def mean(lst):
    return statistics.mean(lst) if lst else float("nan")


def pct(a, b):
    return f"{a/b*100:.0f}%" if b else "n/a"


def main():
    db       = LocalDB(DB_PATH)
    analyzer = SMCAnalyzer()
    import pytz
    IL_TZ   = pytz.timezone("Asia/Jerusalem")
    cutoff  = int((datetime.now(timezone.utc) - timedelta(days=SCAN_DAYS)).timestamp())

    # Collect trading dates
    seen_dates = set()
    for c in db.query_recent("DAX", "15m", limit=2000):
        dt = datetime.fromtimestamp(c["timestamp"], tz=IL_TZ).date()
        if dt.weekday() < 5 and c["timestamp"] >= cutoff:
            seen_dates.add(dt)
    trading_dates = sorted(seen_dates)

    all_15m = list(reversed(db.query_recent("DAX", "15m", limit=2000)))
    all_5m  = list(reversed(db.query_recent("DAX", "5m",  limit=6000)))

    TP_LEVELS = [0.30, 0.40, 0.50, 0.55, 0.60, 0.70, 0.80, 0.90, 1.00]

    records         = []
    no_signal_days  = 0
    open_days       = 0

    for trade_date in trading_dates:
        start_ts, end_ts = _dax_session_window(trade_date)

        sess_15m = sorted([c for c in all_15m if start_ts <= c["timestamp"] <= end_ts],
                          key=lambda c: c["timestamp"])
        if len(sess_15m) < 3:
            continue

        pre_15m = sorted([c for c in all_15m if c["timestamp"] < start_ts],
                         key=lambda c: c["timestamp"])[-16:]
        lookahead_end = end_ts + 6 * 3600   # same as main stats scan: 6h post-session
        day_5m  = sorted([c for c in all_5m  if start_ts <= c["timestamp"] <= lookahead_end],
                         key=lambda c: c["timestamp"])

        # ── Step 1: get 15m expansion state (origin, peak) ───────────────────
        exp = analyzer.detect_dax_expansion_state(
            sess_15m,
            params={"symbol": "DAX", "min_expansion_atr": 1.0},
            candles_15m_presession=pre_15m,
        )
        if not exp:
            no_signal_days += 1
            continue

        # ── Step 2: get 5m counter-BOS signal ────────────────────────────────
        sigs = analyzer.detect_dax_session_setup(
            sess_15m, day_5m,
            params={"tp_pct": BASE_TP_PCT, "sl_atr_mult": BASE_SL_ATR,
                    "min_expansion_atr": 1.0, "entry_zone_min_pct": 0.5,
                    "swing_lookback_5m": 12, "min_break_str_5m": 0.3,
                    "symbol": "DAX"},
            candles_15m_presession=pre_15m,
        )
        if not sigs:
            no_signal_days += 1
            continue
        sig = sigs[0]

        entry_ts = sig["breakout_ts"]
        post_entry = [c for c in day_5m if c["timestamp"] > entry_ts]

        # Need at least a few post-entry candles to evaluate
        baseline_outcome, baseline_eff_r = _evaluate_dax_outcome(sig, post_entry)
        if baseline_outcome == "OPEN":
            open_days += 1
            continue

        # ── Timing ────────────────────────────────────────────────────────────
        peak_ts    = exp["peak_ts"]
        bos_ts     = exp["bos_15m_ts"]
        entry_min_from_open = (entry_ts - start_ts) / 60
        entry_min_from_peak = (entry_ts - peak_ts)  / 60
        bos_min             = (bos_ts   - start_ts) / 60
        peak_min            = (peak_ts  - start_ts) / 60

        entry_idt  = datetime.fromtimestamp(entry_ts, tz=IL_TZ).strftime("%H:%M")
        peak_idt   = datetime.fromtimestamp(peak_ts,  tz=IL_TZ).strftime("%H:%M")
        bos_idt    = datetime.fromtimestamp(bos_ts,   tz=IL_TZ).strftime("%H:%M")

        # ── Expansion geometry ────────────────────────────────────────────────
        peak           = exp["peak"]
        origin         = exp["origin"]
        expansion_range = exp["expansion_range"]
        ct_bullish     = (exp["expansion_dir"] == "bearish")   # CT = fade bearish exp → go LONG
        entry_price    = sig["entry"]
        sl_price       = sig["sl"]
        risk           = abs(entry_price - sl_price) or 1.0

        # Entry "retrace depth" from peak: how far into the pullback is the entry?
        # 0% = entry at peak, 50% = entry at EQ50, 100% = entry at origin
        if ct_bullish:
            entry_retrace_pct = (entry_price - peak) / expansion_range if expansion_range else 0
        else:
            entry_retrace_pct = (peak - entry_price) / expansion_range if expansion_range else 0

        # ATR for normalisation
        atr = analyzer.calculate_atr(list(reversed(pre_15m))) or 1.0

        # ── Max favorable retracement (pure price, no SL) ────────────────────
        max_retrace = 0.0
        for c in post_entry:
            if ct_bullish:
                fav = (c["high"] - peak) / expansion_range if expansion_range else 0
            else:
                fav = (peak - c["low"])  / expansion_range if expansion_range else 0
            max_retrace = max(max_retrace, fav)

        # ── TP waterfall at each level (with fixed SL) ───────────────────────
        tp_results = {}
        for tp_pct in TP_LEVELS:
            if ct_bullish:
                tp_price = peak + tp_pct * expansion_range
            else:
                tp_price = peak - tp_pct * expansion_range
            mod_sig = {**sig, "tp": tp_price, "risk": risk}
            outcome, eff_r = _evaluate_dax_outcome(mod_sig, post_entry)
            tp_results[tp_pct] = (outcome, eff_r if eff_r is not None else 0.0)

        records.append({
            "date":                trade_date,
            "outcome":             baseline_outcome,
            "eff_r":               baseline_eff_r,
            "bos_min":             bos_min,
            "peak_min":            peak_min,
            "entry_min":           entry_min_from_open,
            "lag_min":             entry_min_from_peak,   # how long after peak
            "bos_idt":             bos_idt,
            "peak_idt":            peak_idt,
            "entry_idt":           entry_idt,
            "exp_range":           expansion_range,
            "exp_range_atr":       expansion_range / atr,
            "entry_retrace_pct":   entry_retrace_pct,
            "max_retrace":         max_retrace,           # % of expansion range reached
            "tp_results":          tp_results,
            "ct_bullish":          ct_bullish,
            "atr":                 atr,
        })

    n = len(records)
    db.close()

    wins   = [r for r in records if r["outcome"] == "WIN"]
    losses = [r for r in records if r["outcome"] == "LOSS"]

    print(f"\nDAX 5m counter-BOS analysis  ({n} resolved signals, "
          f"{no_signal_days} days no signal, {open_days} days still open)")
    print(f"Scan: {SCAN_DAYS} days  |  Session: 09:00–12:30 IDT  |  baseline TP={BASE_TP_PCT} SL_atr={BASE_SL_ATR}\n")

    # ── 1. 5m BOS timing ─────────────────────────────────────────────────────
    print("═" * 70)
    print("1. WHEN DOES THE 5m COUNTER-BOS FIRE?")
    print("═" * 70)
    entry_mins = [r["entry_min"] for r in records]
    lag_mins   = [r["lag_min"]   for r in records]
    peak_mins  = [r["peak_min"]  for r in records]

    print(f"\n  After open (09:00 IDT):")
    print(f"    range   {min(entry_mins):.0f}–{max(entry_mins):.0f} min")
    print(f"    median  {median(entry_mins):.0f} min  "
          f"({9+median(entry_mins)/60:.2f} → "
          f"~{9+int(median(entry_mins)/60)}:{int(median(entry_mins))%60:02d} IDT)")
    for cut in [60, 90, 120, 150, 180]:
        cnt = sum(1 for r in records if r["entry_min"] <= cut)
        h = 9 + cut // 60; m = cut % 60
        print(f"    within {cut:3d} min ({h}:{m:02d} IDT): {cnt}/{n} ({pct(cnt, n)})")

    print(f"\n  After 15m expansion peak:")
    print(f"    range   {min(lag_mins):.0f}–{max(lag_mins):.0f} min")
    print(f"    median  {median(lag_mins):.0f} min after peak")
    for cut in [15, 30, 45, 60, 90]:
        cnt = sum(1 for r in records if r["lag_min"] <= cut)
        print(f"    within {cut:3d} min after peak: {cnt}/{n} ({pct(cnt, n)})")

    # ── 2. Entry depth vs outcome ─────────────────────────────────────────────
    print(f"\n{'═'*70}")
    print("2. WHERE DOES ENTRY FIRE RELATIVE TO PEAK? (entry retrace %)")
    print("   0% = entry at peak  |  50% = entry at EQ  |  100% = entry at origin")
    print("═" * 70)
    depths = [r["entry_retrace_pct"] * 100 for r in records]
    w_dep  = [r["entry_retrace_pct"] * 100 for r in wins]
    l_dep  = [r["entry_retrace_pct"] * 100 for r in losses]
    print(f"  {'':30s}  {'All':>6s}  {'WIN':>6s}  {'LOSS':>6s}")
    print(f"  {'Entry depth (% of exp range)':30s}  "
          f"{median(depths):6.1f}%  {median(w_dep):6.1f}%  {median(l_dep):6.1f}%")
    print(f"  {'Min entry depth':30s}  {min(depths):6.1f}%  "
          f"{min(w_dep) if w_dep else 0:6.1f}%  {min(l_dep) if l_dep else 0:6.1f}%")
    print(f"  {'Max entry depth':30s}  {max(depths):6.1f}%  "
          f"{max(w_dep) if w_dep else 0:6.1f}%  {max(l_dep) if l_dep else 0:6.1f}%")

    print(f"\n  Depth buckets (where in the expansion zone does entry fire):")
    for lo, hi in [(0, 30), (30, 50), (50, 70), (70, 100)]:
        bucket = [r for r in records if lo <= r["entry_retrace_pct"] * 100 < hi]
        bw = [r for r in bucket if r["outcome"] == "WIN"]
        wr = len(bw) / len(bucket) if bucket else 0
        rrs = [r["eff_r"] for r in bucket]
        ev = wr * mean(rrs) - (1 - wr) if rrs else 0
        print(f"    entry {lo:2d}–{hi:2d}% into expansion  n={len(bucket):>2}  "
              f"WR={wr*100:5.1f}%  EV={ev:+.2f}R")

    # ── 3. Max retracement toward origin ─────────────────────────────────────
    print(f"\n{'═'*70}")
    print("3. HOW FAR DOES PRICE RETRACE TOWARD ORIGIN? (max favorable, no SL)")
    print("   Measured from 15m expansion peak  |  100% = full origin retest")
    print("═" * 70)
    mr_all = [r["max_retrace"] * 100 for r in records]
    mr_win = [r["max_retrace"] * 100 for r in wins]
    mr_los = [r["max_retrace"] * 100 for r in losses]
    print(f"  {'':30s}  {'All':>6s}  {'WIN':>6s}  {'LOSS':>6s}")
    print(f"  {'Median max retrace':30s}  "
          f"{median(mr_all):6.1f}%  {median(mr_win):6.1f}%  {median(mr_los):6.1f}%")
    print(f"  {'Mean max retrace':30s}  "
          f"{mean(mr_all):6.1f}%  {mean(mr_win):6.1f}%  {mean(mr_los):6.1f}%")
    print(f"  {'Min max retrace':30s}  "
          f"{min(mr_all):6.1f}%  {min(mr_win) if mr_win else 0:6.1f}%  "
          f"{min(mr_los) if mr_los else 0:6.1f}%")

    print(f"\n  Cumulative: how many signals reach each retracement level?")
    print(f"  {'Level':>8s}  {'N reached':>10s}  {'% of all':>8s}  "
          f"{'N wins':>7s}  {'WR of reached':>13s}")
    for lvl in [20, 30, 40, 50, 55, 60, 70, 80, 90, 100]:
        reached = [r for r in records if r["max_retrace"] * 100 >= lvl]
        rw = [r for r in reached if r["outcome"] == "WIN"]
        wr = len(rw) / len(reached) if reached else 0
        marker = " ←TP50" if lvl == 50 else (" ←TP60" if lvl == 60 else "")
        print(f"  {lvl:>7}%  {len(reached):>10}  {pct(len(reached), n):>8s}  "
              f"{len(rw):>7}  {wr*100:>12.1f}%{marker}")

    # ── 4. TP waterfall: WR / EV at each TP level ───────────────────────────
    print(f"\n{'═'*70}")
    print(f"4. TP WATERFALL — WR + EV AT EACH TP LEVEL  (SL_atr={BASE_SL_ATR} fixed)")
    print("═" * 70)
    print(f"  {'TP level':>9s}  {'n':>4s}  {'n_win':>6s}  {'WR':>7s}  "
          f"{'EV':>8s}  {'R_win':>7s}")
    for tp_pct in TP_LEVELS:
        resolved = [(r, r["tp_results"][tp_pct]) for r in records
                    if r["tp_results"][tp_pct][0] in ("WIN", "LOSS")]
        if not resolved: continue
        n_tp   = len(resolved)
        n_win  = sum(1 for _, (o, _) in resolved if o == "WIN")
        wr     = n_win / n_tp
        r_wins = [er for _, (o, er) in resolved if o == "WIN"]
        r_mean = mean(r_wins) if r_wins else 0
        ev     = wr * r_mean - (1 - wr) * 1.0
        marker = " ◄ current" if abs(tp_pct - BASE_TP_PCT) < 0.01 else (
                 " ★ best?"  if tp_pct == 0.60 else "")
        print(f"  {'tp'+str(tp_pct):>9s}  {n_tp:>4}  {n_win:>6}  {wr*100:>6.1f}%  "
              f"{ev:>+8.3f}R  {r_mean:>7.2f}R{marker}")

    # ── 5. Timing vs outcome ──────────────────────────────────────────────────
    print(f"\n{'═'*70}")
    print("5. DOES ENTRY TIMING (lag from peak) PREDICT OUTCOME?")
    print("═" * 70)
    for cut in [15, 30, 45, 60, 90]:
        early = [r for r in records if r["lag_min"] <= cut]
        late  = [r for r in records if r["lag_min"] >  cut]
        for lbl, grp in [(f"≤{cut:2d}min after peak", early),
                         (f"> {cut:2d}min after peak", late)]:
            if not grp: continue
            gw = [r for r in grp if r["outcome"] == "WIN"]
            wr = len(gw) / len(grp)
            rrs = [r["eff_r"] for r in grp]
            ev  = wr * mean(rrs) - (1 - wr) if rrs else 0
            print(f"  {lbl:25s}  n={len(grp):>2}  WR={wr*100:5.1f}%  EV={ev:+.2f}R")
        print()

    # ── 6. Per-signal detail ──────────────────────────────────────────────────
    print("═" * 70)
    print("6. PER-SIGNAL DETAIL")
    print("═" * 70)
    hdr = (f"  {'Date':12s} {'Out':4s} {'EffR':5s}  "
           f"{'BOS':>5s} {'Pk':>5s} {'5mBOS':>6s}  "
           f"{'Lag':>5s}  {'Edep':>5s}  {'MaxR':>5s}  {'ExpAtr':>6s}")
    print(hdr)
    print(f"  {'────':12s} {'───':4s} {'────':5s}  "
          f"{'IDT':>5s} {'IDT':>5s} {'IDT':>6s}  "
          f"{'min':>5s}  {'%':>5s}  {'%':>5s}  {'×ATR':>6s}")
    for r in sorted(records, key=lambda x: x["date"]):
        print(f"  {str(r['date']):12s} {r['outcome']:4s} {r['eff_r']:5.2f}  "
              f"{r['bos_idt']:>5s} {r['peak_idt']:>5s} {r['entry_idt']:>6s}  "
              f"{r['lag_min']:5.0f}  "
              f"{r['entry_retrace_pct']*100:5.1f}%  "
              f"{r['max_retrace']*100:5.1f}%  "
              f"{r['exp_range_atr']:6.1f}")


if __name__ == "__main__":
    main()
