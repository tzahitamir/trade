#!/usr/bin/env python3
"""
Frankfurt morning range vs afternoon behaviour — no SERPE constraints.

Pure price structure question:
  1. What was the Frankfurt morning range + direction? (09:00–12:30 IDT)
  2. Does afternoon (14:00–17:00 IDT) retrace toward morning equilibrium?
  3. How often? How deep? Is there a dominant pattern?
  4. Can 5m weakness in the 12:30–14:00 transition window predict it?

No 15m BOS requirement. No SERPE. Just raw price.
"""
import sys, statistics, logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from db.local_db import LocalDB
from analysis.smc_analyzer import SMCAnalyzer
from main import _dax_session_window

logging.basicConfig(level=logging.WARNING)

DB_PATH   = str(Path(__file__).resolve().parents[1] / "src" / "data" / "trade.db")
SCAN_DAYS = 75

AM_START_H, AM_START_M = 9,  0    # Frankfurt open
AM_END_H,   AM_END_M   = 12, 30   # Frankfurt close
TRANS_END_H = 14                   # transition window end
PM_START_H  = 14                   # early NY
PM_END_H    = 17


def ts_for(date, hour, minute, tz):
    import pytz
    from datetime import datetime as _dt
    return int(_dt(date.year, date.month, date.day, hour, minute, tzinfo=tz).timestamp())


def median(lst):
    if not lst: return float("nan")
    s = sorted(lst); n = len(s)
    return s[n//2] if n % 2 else (s[n//2-1] + s[n//2]) / 2


def mean(lst):
    return statistics.mean(lst) if lst else float("nan")


def pct_str(a, b):
    return f"{a/b*100:.0f}%" if b else "n/a"


def _stall_candle(c, prev, atr):
    """True if c is a 5m stall: small body OR compressed range OR inside bar."""
    body  = abs(c["close"] - c["open"])
    rng   = c["high"] - c["low"]
    inside = (prev is not None
              and c["high"] <= prev["high"]
              and c["low"]  >= prev["low"])
    return body <= 0.35 * atr or rng <= 0.55 * atr or inside


def _count_stalls(candles_5m, atr):
    count = 0
    for i, c in enumerate(candles_5m):
        prev = candles_5m[i-1] if i > 0 else None
        if _stall_candle(c, prev, atr):
            count += 1
    return count


def _momentum_candle(c, direction, atr):
    """Candle pushing in `direction` with meaningful body."""
    body = abs(c["close"] - c["open"])
    if direction == "bullish" and c["close"] > c["open"] and body > 0.3 * atr:
        return True
    if direction == "bearish" and c["close"] < c["open"] and body > 0.3 * atr:
        return True
    return False


def main():
    db       = LocalDB(DB_PATH)
    analyzer = SMCAnalyzer()
    import pytz
    IL_TZ   = pytz.timezone("Asia/Jerusalem")
    cutoff  = int((datetime.now(timezone.utc) - timedelta(days=SCAN_DAYS)).timestamp())

    seen_dates = set()
    for c in db.query_recent("DAX", "5m", limit=8000):
        dt = datetime.fromtimestamp(c["timestamp"], tz=IL_TZ).date()
        if dt.weekday() < 5 and c["timestamp"] >= cutoff:
            seen_dates.add(dt)
    trading_dates = sorted(seen_dates)

    all_5m  = list(reversed(db.query_recent("DAX", "5m", limit=8000)))
    all_15m = list(reversed(db.query_recent("DAX", "15m", limit=2000)))

    records = []

    for trade_date in trading_dates:
        am_start = ts_for(trade_date, AM_START_H, AM_START_M, IL_TZ)
        am_end   = ts_for(trade_date, AM_END_H,   AM_END_M,   IL_TZ)
        trans_end= ts_for(trade_date, TRANS_END_H, 0, IL_TZ)
        pm_start = ts_for(trade_date, PM_START_H,  0, IL_TZ)
        pm_end   = ts_for(trade_date, PM_END_H,    0, IL_TZ)

        am_5m    = [c for c in all_5m if am_start    <= c["timestamp"] <= am_end]
        trans_5m = [c for c in all_5m if am_end      <  c["timestamp"] <= trans_end]
        pm_5m    = [c for c in all_5m if pm_start    <= c["timestamp"] <= pm_end]

        if len(am_5m) < 10 or len(pm_5m) < 6:
            continue

        # ── Morning structure ─────────────────────────────────────────────────
        am_open   = am_5m[0]["open"]
        am_close  = am_5m[-1]["close"]
        am_high   = max(c["high"] for c in am_5m)
        am_low    = min(c["low"]  for c in am_5m)
        am_range  = am_high - am_low
        am_eq     = (am_high + am_low) / 2   # morning midpoint = equilibrium
        am_net    = am_close - am_open

        # Morning direction: which extreme is further from open?
        dist_up   = am_high - am_open
        dist_down = am_open - am_low
        am_dir    = "bullish" if dist_up >= dist_down else "bearish"

        # Peak of the morning move (whichever extreme is more extreme relative to open)
        am_peak   = am_high if am_dir == "bullish" else am_low
        am_origin = am_low  if am_dir == "bullish" else am_high  # opposite extreme

        # When did morning peak form?
        if am_dir == "bullish":
            peak_candle = max(am_5m, key=lambda c: c["high"])
        else:
            peak_candle = min(am_5m, key=lambda c: c["low"])
        peak_ts  = peak_candle["timestamp"]
        peak_idt = datetime.fromtimestamp(peak_ts, tz=IL_TZ)
        peak_min = (peak_ts - am_start) / 60

        # ATR from 15m pre-session
        pre_15m = sorted([c for c in all_15m if c["timestamp"] < am_start],
                         key=lambda c: c["timestamp"])[-16:]
        atr = analyzer.calculate_atr(list(reversed(pre_15m))) or am_range * 0.15 or 50.0

        # Morning strength: how far did it go relative to ATR?
        am_range_atr = am_range / atr
        am_net_atr   = abs(am_net) / atr

        # ── Transition window (12:30–14:00) ──────────────────────────────────
        trans_stalls = 0
        trans_mom    = 0   # momentum candles in expansion direction
        if trans_5m:
            trans_stalls = _count_stalls(trans_5m, atr * 0.33)  # ATR in 5m ≈ ATR15m/3
            for i, c in enumerate(trans_5m):
                if _momentum_candle(c, am_dir, atr * 0.33):
                    trans_mom += 1

        # Any new high/low after Frankfurt close (expansion continuing into transition)?
        new_extreme_trans = False
        if am_dir == "bullish":
            new_extreme_trans = any(c["high"] > am_high for c in trans_5m)
        else:
            new_extreme_trans = any(c["low"] < am_low for c in trans_5m)

        # Price at PM start (14:00) relative to morning range
        # 0% = at origin (opposite extreme from expansion)
        # 50% = at equilibrium (midpoint)
        # 100% = at expansion peak
        pm_open_price = pm_5m[0]["open"]
        if am_range > 0:
            if am_dir == "bullish":
                pm_open_pct = (pm_open_price - am_low) / am_range   # 0=low, 1=high
            else:
                pm_open_pct = (am_high - pm_open_price) / am_range  # 0=high, 1=low
        else:
            pm_open_pct = 0.5

        # ── Afternoon (14:00–17:00) ───────────────────────────────────────────
        pm_close_price = pm_5m[-1]["close"]
        pm_net         = pm_close_price - pm_5m[0]["open"]
        pm_dir         = "bullish" if pm_net > 0 else "bearish"

        # Does afternoon oppose morning expansion? (mean reversion)
        pm_vs_am = "REVERSAL" if pm_dir != am_dir else "CONTINUATION"

        # How far did price retrace toward morning equilibrium (midpoint)?
        # Positive = moved toward eq, negative = moved away
        if am_dir == "bullish":
            # Eq is below current (we want price to fall toward am_eq)
            retraced = pm_open_price - pm_close_price   # positive if price fell
        else:
            # Eq is above current (we want price to rise toward am_eq)
            retraced = pm_close_price - pm_open_price

        retraced_pct = retraced / am_range if am_range else 0   # % of morning range

        # Did price reach equilibrium at any point during PM?
        pm_reached_eq   = False
        pm_max_retrace  = 0.0
        for c in pm_5m:
            if am_dir == "bullish":
                fav = (pm_open_price - c["low"]) / am_range if am_range else 0
            else:
                fav = (c["high"] - pm_open_price) / am_range if am_range else 0
            pm_max_retrace = max(pm_max_retrace, fav)
            if am_dir == "bullish" and c["low"] <= am_eq:
                pm_reached_eq = True
            elif am_dir == "bearish" and c["high"] >= am_eq:
                pm_reached_eq = True

        # 5m weakness in transition window: stall rate
        trans_candle_n = len(trans_5m)
        stall_rate = trans_stalls / trans_candle_n if trans_candle_n else 0

        # Did the transition window itself start reversing? (first clear reversal candle)
        first_rev_min = None   # minutes after 12:30 when first reversal candle appears
        if trans_5m:
            for i, c in enumerate(trans_5m):
                prev = trans_5m[i-1] if i > 0 else None
                # reversal candle: moves against am_dir with meaningful body
                if _momentum_candle(c, ("bearish" if am_dir == "bullish" else "bullish"), atr * 0.33):
                    first_rev_min = (c["timestamp"] - am_end) / 60
                    break

        records.append({
            "date":             trade_date,
            "am_dir":           am_dir,
            "am_range_atr":     am_range_atr,
            "am_net_atr":       am_net_atr,
            "peak_idt":         peak_idt.strftime("%H:%M"),
            "peak_min":         peak_min,
            "pm_open_pct":      pm_open_pct * 100,
            "pm_vs_am":         pm_vs_am,
            "pm_net_atr":       abs(pm_net) / atr,
            "pm_max_retrace":   pm_max_retrace * 100,
            "pm_reached_eq":    pm_reached_eq,
            "retraced_pct":     retraced_pct * 100,
            "trans_stalls":     trans_stalls,
            "trans_mom":        trans_mom,
            "trans_candles":    trans_candle_n,
            "stall_rate":       stall_rate,
            "new_extreme_trans":new_extreme_trans,
            "first_rev_min":    first_rev_min,
            "atr":              atr,
            "am_range":         am_range,
            "am_eq":            am_eq,
        })

    n = len(records)
    db.close()

    rev   = [r for r in records if r["pm_vs_am"] == "REVERSAL"]
    cont  = [r for r in records if r["pm_vs_am"] == "CONTINUATION"]

    print(f"\nDAX Frankfurt morning → afternoon analysis  ({n} trading days)")
    print(f"No SERPE constraints. Pure price.\n")

    # ── 1. Base rates ─────────────────────────────────────────────────────────
    n_rev = len(rev); n_cont = len(cont)
    print("═" * 70)
    print("1. BASE RATE: does afternoon (14:00–17:00) reverse the morning move?")
    print("═" * 70)
    print(f"  Afternoon REVERSES morning direction : {n_rev}/{n} ({pct_str(n_rev, n)})")
    print(f"  Afternoon CONTINUES morning direction: {n_cont}/{n} ({pct_str(n_cont, n)})")

    n_eq  = sum(1 for r in records if r["pm_reached_eq"])
    print(f"\n  Afternoon reaches morning EQ (50% midpoint): {n_eq}/{n} ({pct_str(n_eq, n)})")

    print(f"\n  Max PM retrace toward EQ (% of morning range):")
    mr_all  = [r["pm_max_retrace"] for r in records]
    mr_rev  = [r["pm_max_retrace"] for r in rev]
    mr_cont = [r["pm_max_retrace"] for r in cont]
    print(f"  {'':25s}  {'All':>6s}  {'Reversal':>8s}  {'Continuation':>12s}")
    print(f"  {'Median max retrace':25s}  "
          f"{median(mr_all):6.1f}%  {median(mr_rev):8.1f}%  {median(mr_cont):12.1f}%")
    print(f"  {'Mean max retrace':25s}  "
          f"{mean(mr_all):6.1f}%  {mean(mr_rev):8.1f}%  {mean(mr_cont):12.1f}%")

    print(f"\n  Cumulative: how many days reach each retrace level?")
    print(f"  {'Level':>7s}  {'N':>4s}  {'% days':>7s}  {'Reversal d':>10s}  {'Cont days':>10s}")
    for lvl in [10, 20, 30, 40, 50, 60, 70, 80, 100]:
        nr = sum(1 for r in records if r["pm_max_retrace"] >= lvl)
        nr_rev  = sum(1 for r in rev  if r["pm_max_retrace"] >= lvl)
        nr_cont = sum(1 for r in cont if r["pm_max_retrace"] >= lvl)
        mark = " ← EQ" if lvl == 50 else ""
        print(f"  {lvl:>6}%  {nr:>4}  {pct_str(nr, n):>7s}  "
              f"{nr_rev:>4}/{n_rev:<4} ({pct_str(nr_rev, n_rev):>4})  "
              f"{nr_cont:>4}/{n_cont:<4} ({pct_str(nr_cont, n_cont):>4}){mark}")

    # ── 2. Morning range size vs afternoon retrace ────────────────────────────
    print(f"\n{'═'*70}")
    print("2. MORNING RANGE SIZE vs AFTERNOON RETRACE")
    print("   Does a bigger morning move get a bigger retrace?")
    print("═" * 70)
    for lo, hi in [(0, 3), (3, 5), (5, 8), (8, 99)]:
        g = [r for r in records if lo <= r["am_range_atr"] < hi]
        if not g: continue
        eq_g = sum(1 for r in g if r["pm_reached_eq"])
        mr_g = [r["pm_max_retrace"] for r in g]
        print(f"  Morning range {lo}–{hi if hi < 99 else '∞'}×ATR  "
              f"n={len(g):>2}  reaches EQ: {eq_g}/{len(g)} ({pct_str(eq_g, len(g))})  "
              f"median retrace: {median(mr_g):.1f}%")

    # ── 3. Peak timing vs afternoon retrace ───────────────────────────────────
    print(f"\n{'═'*70}")
    print("3. MORNING PEAK TIMING vs AFTERNOON RETRACE")
    print("   Does a later peak mean less time to reverse → less retrace?")
    print("═" * 70)
    for lo, hi, label in [(0, 90, "≤90min (≤10:30)"), (90, 150, "90–150min (10:30–11:30)"),
                           (150, 195, "150–195min (11:30–12:15)"), (195, 999, ">195min (>12:15)")]:
        g = [r for r in records if lo <= r["peak_min"] < hi]
        if not g: continue
        eq_g = sum(1 for r in g if r["pm_reached_eq"])
        mr_g = [r["pm_max_retrace"] for r in g]
        rev_g = sum(1 for r in g if r["pm_vs_am"] == "REVERSAL")
        print(f"  Peak {label:28s}  n={len(g):>2}  "
              f"EQ reach: {eq_g}/{len(g)} ({pct_str(eq_g, len(g))})  "
              f"median retrace: {median(mr_g):.1f}%  "
              f"PM reversal: {pct_str(rev_g, len(g))}")

    # ── 4. Where is price at 14:00 within the morning range? ─────────────────
    print(f"\n{'═'*70}")
    print("4. PRICE AT 14:00 IDT — position within morning range")
    print("   0% = at origin extreme  |  50% = morning EQ  |  100% = morning peak")
    print("═" * 70)
    pm_pcts = [r["pm_open_pct"] for r in records]
    print(f"  Median position at 14:00: {median(pm_pcts):.1f}% toward peak")
    print()

    for lo, hi in [(-20, 0), (0, 25), (25, 50), (50, 75), (75, 100), (100, 150)]:
        g = [r for r in records if lo <= r["pm_open_pct"] < hi]
        if not g: continue
        eq_g = sum(1 for r in g if r["pm_reached_eq"])
        mr_g = [r["pm_max_retrace"] for r in g]
        rev_g = sum(1 for r in g if r["pm_vs_am"] == "REVERSAL")
        labels = {(-20,0):"Below origin (<0%)",  (0,25):"Near origin (0–25%)",
                  (25,50):"Between (25–50%)",    (50,75):"Near EQ (50–75%)",
                  (75,100):"Near peak (75–100%)",(100,150):"Past peak (>100%)"}
        print(f"  {labels[(lo,hi)]:25s}  n={len(g):>2}  "
              f"EQ reach: {eq_g}/{len(g)} ({pct_str(eq_g, len(g))})  "
              f"median retrace: {median(mr_g):.1f}%  "
              f"PM reversal rate: {pct_str(rev_g, len(g))}")

    # ── 5. Transition window 5m weakness ─────────────────────────────────────
    print(f"\n{'═'*70}")
    print("5. 5m WEAKNESS IN TRANSITION WINDOW (12:30–14:00 IDT)")
    print("   Stall candles: small body, compressed range, or inside bar")
    print("═" * 70)

    # Compare stall rate on reversal vs continuation days
    stall_rev  = [r["stall_rate"] for r in rev  if r["trans_candles"] >= 3]
    stall_cont = [r["stall_rate"] for r in cont if r["trans_candles"] >= 3]
    mr_rev_eq  = [r for r in rev  if r["trans_candles"] >= 3]
    mr_cont_eq = [r for r in cont if r["trans_candles"] >= 3]

    print(f"  Stall rate in 12:30–14:00 window:")
    print(f"    Reversal days  (afternoon reverses morning):  median {median(stall_rev)*100:.0f}%  n={len(stall_rev)}")
    print(f"    Continuation   (afternoon continues morning): median {median(stall_cont)*100:.0f}%  n={len(stall_cont)}")

    # Gate: high stall rate (≥50% of transition candles are stalls)
    print(f"\n  Gate — transition stall rate ≥ 50% (weak consolidation after morning move):")
    high_stall = [r for r in records if r["trans_candles"] >= 3 and r["stall_rate"] >= 0.5]
    low_stall  = [r for r in records if r["trans_candles"] >= 3 and r["stall_rate"] <  0.5]
    for lbl, g in [("High stall (≥50%)", high_stall), ("Low stall (<50%)", low_stall)]:
        eq_g  = sum(1 for r in g if r["pm_reached_eq"])
        rev_g = sum(1 for r in g if r["pm_vs_am"] == "REVERSAL")
        mr_g  = [r["pm_max_retrace"] for r in g]
        print(f"    {lbl:20s}  n={len(g):>2}  EQ reach: {eq_g}/{len(g)} ({pct_str(eq_g, len(g))})  "
              f"PM reversal: {pct_str(rev_g, len(g))}  median retrace: {median(mr_g):.1f}%")

    # New extreme in transition (expansion still running after close)?
    print(f"\n  New extreme after 12:30 (expansion continuing into transition):")
    ext_yes = [r for r in records if r["new_extreme_trans"]]
    ext_no  = [r for r in records if not r["new_extreme_trans"]]
    for lbl, g in [("New high/low post-close", ext_yes), ("No new extreme post-close", ext_no)]:
        if not g: continue
        eq_g  = sum(1 for r in g if r["pm_reached_eq"])
        rev_g = sum(1 for r in g if r["pm_vs_am"] == "REVERSAL")
        mr_g  = [r["pm_max_retrace"] for r in g]
        print(f"    {lbl:30s}  n={len(g):>2}  EQ reach: {pct_str(eq_g, len(g))}  "
              f"PM reversal: {pct_str(rev_g, len(g))}  median retrace: {median(mr_g):.1f}%")

    # When does first reversal candle appear in transition?
    first_rev_mins = [r["first_rev_min"] for r in records if r["first_rev_min"] is not None]
    no_rev_trans   = sum(1 for r in records if r["first_rev_min"] is None and r["trans_candles"] >= 3)
    print(f"\n  First reversal-direction candle in 12:30–14:00:")
    print(f"    Appears in transition:   {len(first_rev_mins)}/{n} ({pct_str(len(first_rev_mins), n)})")
    print(f"    No reversal candle seen: {no_rev_trans} days (momentum holding all the way to 14:00)")
    if first_rev_mins:
        print(f"    Range: {min(first_rev_mins):.0f}–{max(first_rev_mins):.0f} min after 12:30")
        print(f"    Median: {median(first_rev_mins):.0f} min after 12:30  "
              f"(~{12+int(median(first_rev_mins)+30)//60}:{(int(median(first_rev_mins)+30))%60:02d} IDT)")

    print(f"\n  Does early reversal candle in transition predict PM reversal?")
    early_rev  = [r for r in records if r["first_rev_min"] is not None and r["first_rev_min"] <= 45]
    late_rev   = [r for r in records if r["first_rev_min"] is not None and r["first_rev_min"] >  45]
    no_rev_t   = [r for r in records if r["first_rev_min"] is None and r["trans_candles"] >= 3]
    for lbl, g in [("1st rev candle ≤45min after 12:30 (~13:15)", early_rev),
                   ("1st rev candle >45min after 12:30", late_rev),
                   ("No reversal candle before 14:00", no_rev_t)]:
        if not g: continue
        eq_g  = sum(1 for r in g if r["pm_reached_eq"])
        rev_g = sum(1 for r in g if r["pm_vs_am"] == "REVERSAL")
        mr_g  = [r["pm_max_retrace"] for r in g]
        print(f"    {lbl:45s}  n={len(g):>2}  EQ reach: {pct_str(eq_g, len(g))}  "
              f"PM reversal: {pct_str(rev_g, len(g))}  retrace: {median(mr_g):.1f}%")

    # ── 6. Per-day detail ─────────────────────────────────────────────────────
    print(f"\n{'═'*70}")
    print("6. PER-DAY DETAIL")
    print("═" * 70)
    print(f"  {'Date':12s} {'AmDir':5s} {'RngATR':6s} {'PkIDT':5s}  "
          f"{'@14h%':6s}  {'MaxR%':5s}  {'EQhit':5s}  {'PMvAM':12s}  {'StallR':6s}  {'NewExt':6s}")
    for r in sorted(records, key=lambda x: x["date"]):
        eq_mark = "YES" if r["pm_reached_eq"] else "no"
        ne_mark = "YES" if r["new_extreme_trans"] else "-"
        print(f"  {str(r['date']):12s} {r['am_dir'][:4]:5s} {r['am_range_atr']:6.1f} "
              f"{r['peak_idt']:>5s}  "
              f"{r['pm_open_pct']:+6.1f}%  {r['pm_max_retrace']:5.1f}%  "
              f"{eq_mark:5s}  {r['pm_vs_am']:12s}  "
              f"{r['stall_rate']*100:5.0f}%   {ne_mark:5s}")


if __name__ == "__main__":
    main()
