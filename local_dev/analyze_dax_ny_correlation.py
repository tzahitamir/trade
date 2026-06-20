#!/usr/bin/env python3
"""
DAX morning expansion vs afternoon (14:00–17:00 IDT) correlation.

Hypothesis: the Frankfurt expansion direction (15m BOS) may be continued or
reversed by early NY flow starting ~14:00 IDT. If NY reverses the Frankfurt
direction, SERPE wins. If NY continues Frankfurt direction, SERPE loses.

Analysis:
  1. For every trading day, measure Frankfurt morning direction (15m BOS).
  2. Measure afternoon price movement (14:00–17:00 IDT) — direction + magnitude.
  3. Cross: does afternoon direction align with or oppose the morning expansion?
  4. On SERPE signal days: does afternoon alignment predict WIN/LOSS?
  5. Also check: did the 5m counter-BOS fire in the 13:45–15:00 window?
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

PM_START_HOUR   = 14   # 14:00 IDT — early NY flow
PM_END_HOUR     = 17   # 17:00 IDT


def median(lst):
    if not lst: return float("nan")
    s = sorted(lst); n = len(s)
    return s[n//2] if n % 2 else (s[n//2-1] + s[n//2]) / 2


def mean(lst):
    return statistics.mean(lst) if lst else float("nan")


def pct_str(a, b):
    return f"{a/b*100:.0f}%" if b else "n/a"


def main():
    db       = LocalDB(DB_PATH)
    analyzer = SMCAnalyzer()
    import pytz
    IL_TZ   = pytz.timezone("Asia/Jerusalem")
    cutoff  = int((datetime.now(timezone.utc) - timedelta(days=SCAN_DAYS)).timestamp())

    seen_dates = set()
    for c in db.query_recent("DAX", "15m", limit=2000):
        dt = datetime.fromtimestamp(c["timestamp"], tz=IL_TZ).date()
        if dt.weekday() < 5 and c["timestamp"] >= cutoff:
            seen_dates.add(dt)
    trading_dates = sorted(seen_dates)

    all_15m = list(reversed(db.query_recent("DAX", "15m", limit=2000)))
    all_5m  = list(reversed(db.query_recent("DAX", "5m",  limit=8000)))

    records_signal = []   # days with resolved SERPE signal
    records_all    = []   # all days with morning expansion + afternoon data

    for trade_date in trading_dates:
        start_ts, end_ts = _dax_session_window(trade_date)
        lookahead_end = end_ts + 6 * 3600

        sess_15m = sorted([c for c in all_15m if start_ts <= c["timestamp"] <= end_ts],
                          key=lambda c: c["timestamp"])
        if len(sess_15m) < 3:
            continue

        pre_15m  = sorted([c for c in all_15m if c["timestamp"] < start_ts],
                          key=lambda c: c["timestamp"])[-16:]
        day_5m   = sorted([c for c in all_5m  if start_ts <= c["timestamp"] <= lookahead_end],
                          key=lambda c: c["timestamp"])

        # ── Morning: 15m expansion direction ─────────────────────────────────
        exp = analyzer.detect_dax_expansion_state(
            sess_15m,
            params={"symbol": "DAX", "min_expansion_atr": 1.0},
            candles_15m_presession=pre_15m,
        )
        if not exp:
            continue

        atr_15m = analyzer.calculate_atr(list(reversed(pre_15m))) or 1.0
        atr_5m  = analyzer.calculate_atr(
            list(reversed([c for c in day_5m[:60]]))
        ) or atr_15m * 0.33

        expansion_dir = exp["expansion_dir"]   # bullish = morning went up
        peak_ts       = exp["peak_ts"]
        origin        = exp["origin"]
        peak          = exp["peak"]
        expansion_range = exp["expansion_range"]
        eq_level      = exp["eq_level"]

        peak_idt = datetime.fromtimestamp(peak_ts, tz=IL_TZ)

        # ── Afternoon: 14:00–17:00 IDT 5m candles ────────────────────────────
        pm_start_ts = int(datetime(
            trade_date.year, trade_date.month, trade_date.day,
            PM_START_HOUR, 0, tzinfo=IL_TZ).timestamp())
        pm_end_ts = int(datetime(
            trade_date.year, trade_date.month, trade_date.day,
            PM_END_HOUR, 0, tzinfo=IL_TZ).timestamp())

        pm_candles = [c for c in day_5m
                      if pm_start_ts <= c["timestamp"] <= pm_end_ts]
        if len(pm_candles) < 3:
            continue

        pm_open  = pm_candles[0]["open"]
        pm_close = pm_candles[-1]["close"]
        pm_move  = pm_close - pm_open
        pm_dir   = "bullish" if pm_move > 0 else "bearish"
        pm_move_atr = pm_move / atr_15m   # ATR-normalised afternoon move

        # Does afternoon oppose morning? (SERPE premise = afternoon reverses expansion)
        pm_vs_exp = "REVERSAL" if pm_dir != expansion_dir else "CONTINUATION"

        # ── Where was price at 14:00 relative to expansion levels? ───────────
        if expansion_dir == "bullish":
            pm_open_pct = (pm_open - origin) / expansion_range if expansion_range else 0
        else:
            pm_open_pct = (origin - pm_open) / expansion_range if expansion_range else 0
        # 0% = at origin, 50% = at EQ, 100% = at peak. Negative = past origin.

        # ── Max favorable retrace between 14:00–17:00 (toward origin) ────────
        ct_bullish = (expansion_dir == "bearish")   # CT = long for bearish expansion
        pm_max_retrace = 0.0
        for c in pm_candles:
            if ct_bullish:
                fav = (c["high"] - peak) / expansion_range if expansion_range else 0
            else:
                fav = (peak - c["low"])  / expansion_range if expansion_range else 0
            pm_max_retrace = max(pm_max_retrace, fav)

        # ── SERPE signal on this day? ─────────────────────────────────────────
        sigs = analyzer.detect_dax_session_setup(
            sess_15m, day_5m,
            params={"tp_pct": 0.5, "sl_atr_mult": 0.5,
                    "min_expansion_atr": 1.0, "entry_zone_min_pct": 0.5,
                    "swing_lookback_5m": 12, "min_break_str_5m": 0.3,
                    "symbol": "DAX"},
            candles_15m_presession=pre_15m,
        )

        sig_outcome = None
        sig_eff_r   = None
        entry_ts    = None
        entry_idt_s = None
        entry_in_pm = False

        if sigs:
            sig     = sigs[0]
            post_5m = [c for c in day_5m if c["timestamp"] > sig["breakout_ts"]]
            outcome, eff_r = _evaluate_dax_outcome(sig, post_5m)
            if outcome != "OPEN":
                sig_outcome = outcome
                sig_eff_r   = eff_r
                entry_ts    = sig["breakout_ts"]
                entry_idt   = datetime.fromtimestamp(entry_ts, tz=IL_TZ)
                entry_idt_s = entry_idt.strftime("%H:%M")
                # Did the entry fire in the 13:45–15:15 IDT window?
                entry_in_pm = (13 * 60 + 45) <= (entry_idt.hour * 60 + entry_idt.minute) <= (15 * 60 + 15)

        base = {
            "date":           trade_date,
            "exp_dir":        expansion_dir,
            "peak_idt":       peak_idt.strftime("%H:%M"),
            "peak_min":       (peak_ts - start_ts) / 60,
            "pm_dir":         pm_dir,
            "pm_vs_exp":      pm_vs_exp,
            "pm_move_atr":    pm_move_atr,
            "pm_open_pct":    pm_open_pct * 100,
            "pm_max_retrace": pm_max_retrace * 100,
            "late_peak":      (peak_idt.hour, peak_idt.minute) >= (11, 45),
        }
        records_all.append(base)

        if sig_outcome:
            records_signal.append({
                **base,
                "outcome":       sig_outcome,
                "eff_r":         sig_eff_r,
                "entry_idt":     entry_idt_s,
                "entry_in_pm":   entry_in_pm,
            })

    n_all  = len(records_all)
    n_sig  = len(records_signal)
    db.close()

    print(f"\nDAX morning expansion → afternoon correlation")
    print(f"Signal days: {n_sig} resolved  |  All days with expansion + PM data: {n_all}")
    print(f"Afternoon window: {PM_START_HOUR}:00–{PM_END_HOUR}:00 IDT\n")

    # ── 1. All days: does afternoon direction align with or oppose morning? ───
    print("═" * 70)
    print("1. ALL DAYS WITH MORNING EXPANSION: afternoon direction vs expansion")
    print("═" * 70)
    n_rev  = sum(1 for r in records_all if r["pm_vs_exp"] == "REVERSAL")
    n_cont = sum(1 for r in records_all if r["pm_vs_exp"] == "CONTINUATION")
    print(f"  Afternoon REVERSES morning expansion : {n_rev}/{n_all} ({pct_str(n_rev, n_all)})")
    print(f"  Afternoon CONTINUES morning expansion: {n_cont}/{n_all} ({pct_str(n_cont, n_all)})")

    rev_move  = [r["pm_move_atr"] for r in records_all if r["pm_vs_exp"] == "REVERSAL"]
    cont_move = [r["pm_move_atr"] for r in records_all if r["pm_vs_exp"] == "CONTINUATION"]
    print(f"\n  Afternoon move magnitude (ATR, signed = toward/away origin):")
    print(f"    REVERSAL days   median abs move: {abs(median(rev_move)):.2f}×ATR  "
          f"(range {min(abs(m) for m in rev_move):.1f}–{max(abs(m) for m in rev_move):.1f})")
    print(f"    CONTINUATION    median abs move: {abs(median(cont_move)):.2f}×ATR  "
          f"(range {min(abs(m) for m in cont_move):.1f}–{max(abs(m) for m in cont_move):.1f})")

    # ── 2. Late vs early peaks: does afternoon direction shift? ──────────────
    print(f"\n{'═'*70}")
    print("2. EARLY PEAK (<11:45) vs LATE PEAK (≥11:45): does afternoon differ?")
    print("═" * 70)
    for early_late, grp_all in [("Early peak  (<11:45)", [r for r in records_all if not r["late_peak"]]),
                                 ("Late peak   (≥11:45)", [r for r in records_all if r["late_peak"]])]:
        n_g = len(grp_all)
        rev_g  = sum(1 for r in grp_all if r["pm_vs_exp"] == "REVERSAL")
        cont_g = sum(1 for r in grp_all if r["pm_vs_exp"] == "CONTINUATION")
        pm_g   = [r["pm_move_atr"] for r in grp_all]
        print(f"  {early_late}:  n={n_g}")
        print(f"    PM reverses expansion : {rev_g}/{n_g} ({pct_str(rev_g, n_g)})")
        print(f"    PM continues expansion: {cont_g}/{n_g} ({pct_str(cont_g, n_g)})")
        print(f"    Median PM move         {median(pm_g):+.2f}×ATR  "
              f"({'toward origin' if median(pm_g) < 0 else 'away from origin'} for bullish, "
              f"sign depends on expansion dir)")
        print()

    # ── 3. SERPE signal days: WIN vs LOSS vs afternoon direction ─────────────
    print("═" * 70)
    print("3. SERPE SIGNAL DAYS: does afternoon REVERSAL vs CONTINUATION predict WIN?")
    print("═" * 70)
    for pm_type, grp in [("PM REVERSAL (afternoon against expansion)", [r for r in records_signal if r["pm_vs_exp"] == "REVERSAL"]),
                         ("PM CONTINUATION (afternoon with expansion)", [r for r in records_signal if r["pm_vs_exp"] == "CONTINUATION"])]:
        n_g = len(grp)
        wins  = [r for r in grp if r["outcome"] == "WIN"]
        losses= [r for r in grp if r["outcome"] == "LOSS"]
        wr = len(wins) / n_g if n_g else 0
        rrs = [r["eff_r"] for r in grp]
        ev  = wr * mean(rrs) - (1 - wr) if rrs else 0
        print(f"  {pm_type}")
        print(f"    n={n_g}  WIN={len(wins)}  LOSS={len(losses)}  WR={wr*100:.1f}%  EV={ev:+.2f}R")
        for r in sorted(grp, key=lambda x: x["date"]):
            marker = " ★" if r["outcome"] == "WIN" else "  "
            print(f"      {r['date']}  {r['outcome']:4s}  "
                  f"peak={r['peak_idt']}  entry={r['entry_idt']}  "
                  f"pm_move={r['pm_move_atr']:+.1f}ATR{marker}")
        print()

    # ── 4. Price at 14:00 relative to expansion ───────────────────────────────
    print("═" * 70)
    print("4. WHERE IS PRICE AT 14:00 IDT RELATIVE TO EXPANSION LEVELS?")
    print("   0% = at origin | 50% = at EQ | 100% = at peak | >100% = past peak")
    print("═" * 70)
    pm_opens_all = [r["pm_open_pct"] for r in records_all]
    pm_opens_sig = [r["pm_open_pct"] for r in records_signal]
    pm_opens_win = [r["pm_open_pct"] for r in records_signal if r["outcome"] == "WIN"]
    pm_opens_los = [r["pm_open_pct"] for r in records_signal if r["outcome"] == "LOSS"]
    print(f"  {'':25s}  {'All days':>8s}  {'Signal':>8s}  {'WIN':>8s}  {'LOSS':>8s}")
    print(f"  {'Median price @ 14:00 (%)':25s}  "
          f"{median(pm_opens_all):>8.1f}%  {median(pm_opens_sig):>8.1f}%  "
          f"{median(pm_opens_win):>8.1f}%  {median(pm_opens_los):>8.1f}%")

    print(f"\n  On signal days — where is price at 14:00?")
    for lo, hi in [(-50, 0), (0, 25), (25, 50), (50, 75), (75, 100), (100, 150)]:
        g = [r for r in records_signal if lo <= r["pm_open_pct"] < hi]
        wins_g = [r for r in g if r["outcome"] == "WIN"]
        wr = len(wins_g)/len(g) if g else 0
        rrs = [r["eff_r"] for r in g]
        ev  = wr * mean(rrs) - (1 - wr) if rrs else 0
        label_map = {
            (-50, 0):   "Below origin (<0%)",
            (0, 25):    "Near origin (0–25%)",
            (25, 50):   "Mid-lower (25–50%)",
            (50, 75):   "Mid-upper (50–75%)",
            (75, 100):  "Near peak  (75–100%)",
            (100, 150): "Past peak  (>100%)",
        }
        print(f"    {label_map[(lo,hi)]:25s}  n={len(g):>2}  WR={wr*100:5.1f}%  EV={ev:+.2f}R")

    # ── 5. Max PM retrace on signal days ─────────────────────────────────────
    print(f"\n{'═'*70}")
    print("5. PM WINDOW (14:00–17:00): max retrace toward origin on signal days")
    print("═" * 70)
    mr_win = [r["pm_max_retrace"] for r in records_signal if r["outcome"] == "WIN"]
    mr_los = [r["pm_max_retrace"] for r in records_signal if r["outcome"] == "LOSS"]
    print(f"  {'':35s}  {'WIN':>6s}  {'LOSS':>6s}")
    print(f"  {'Median PM max retrace (% of exp)':35s}  "
          f"{median(mr_win):6.1f}%  {median(mr_los):6.1f}%")

    for lvl in [25, 40, 50, 60, 75]:
        win_r  = sum(1 for r in records_signal if r["outcome"] == "WIN"  and r["pm_max_retrace"] >= lvl)
        loss_r = sum(1 for r in records_signal if r["outcome"] == "LOSS" and r["pm_max_retrace"] >= lvl)
        print(f"  PM reaches ≥{lvl:2d}%  WIN: {win_r}/{len(mr_win)}  "
              f"LOSS: {loss_r}/{len(mr_los)}")

    # ── 6. Per-signal: full picture ───────────────────────────────────────────
    print(f"\n{'═'*70}")
    print("6. PER-SIGNAL DETAIL")
    print("═" * 70)
    print(f"  {'Date':12s} {'Out':4s} {'Exp':5s} {'Pk IDT':6s} {'Entry':6s}  "
          f"{'PM@14:00':8s}  {'PM dir':4s}  {'PM move':8s}  {'Align':12s}")
    for r in sorted(records_signal, key=lambda x: x["date"]):
        late = "*" if r["late_peak"] else " "
        print(f"  {str(r['date']):12s} {r['outcome']:4s} {r['exp_dir'][:4]:4s} "
              f"{r['peak_idt']}{late:1s}  {r['entry_idt']:6s}  "
              f"{r['pm_open_pct']:+7.1f}%  "
              f"{'↑' if r['pm_dir']=='bullish' else '↓':4s}  "
              f"{r['pm_move_atr']:+7.2f}ATR  "
              f"{r['pm_vs_exp']:12s}")


if __name__ == "__main__":
    main()
