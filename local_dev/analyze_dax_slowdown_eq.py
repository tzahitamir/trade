#!/usr/bin/env python3
"""
Post-peak 5m slowdown → equilibrium analysis.

Question: after the Frankfurt morning expansion peaks, if 2+ consecutive
5m stall candles appear (slow profit-taking), does price still reach
morning equilibrium?

No 15m BOS constraint — pure morning range (09:00–12:30 H/L).

Stall candle: small body (≤0.35×ATR) OR compressed range (≤0.55×ATR)
              OR inside bar.
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

# Stall thresholds
STALL_BODY_ATR  = 0.35
STALL_RANGE_ATR = 0.55

# How many consecutive stalls = "slowdown"
MIN_STALL_RUN = 2

# Look-forward window after slowdown: 4 hours
LOOKFWD_HOURS = 4

# Near-EQ = within this % of morning range from EQ
NEAR_EQ_PCT = 0.10   # ±10% = "near EQ"


def ts_for(date, hour, minute, tz):
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


def is_stall(c, prev, atr):
    body  = abs(c["close"] - c["open"])
    rng   = c["high"] - c["low"]
    inside = (prev is not None
              and c["high"] <= prev["high"]
              and c["low"]  >= prev["low"])
    return body <= STALL_BODY_ATR * atr or rng <= STALL_RANGE_ATR * atr or inside


def find_first_stall_run(candles, atr, min_run=2):
    """Return (start_idx, run_length) of first run of ≥ min_run consecutive stalls, or None."""
    run_start = None
    run_len   = 0
    for i, c in enumerate(candles):
        prev = candles[i-1] if i > 0 else None
        if is_stall(c, prev, atr):
            if run_start is None:
                run_start = i
            run_len += 1
            if run_len >= min_run:
                return run_start, run_len
        else:
            run_start = None
            run_len   = 0
    return None


def max_retrace_after(candles_after, entry_price, am_dir, am_range):
    """Max favorable retrace (toward origin) as % of morning range, from entry_price."""
    best = 0.0
    for c in candles_after:
        if am_dir == "bullish":
            fav = (entry_price - c["low"]) / am_range if am_range else 0
        else:
            fav = (c["high"] - entry_price) / am_range if am_range else 0
        best = max(best, fav)
    return best


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

    all_5m  = list(reversed(db.query_recent("DAX", "5m",  limit=8000)))
    all_15m = list(reversed(db.query_recent("DAX", "15m", limit=2000)))

    records = []

    for trade_date in trading_dates:
        am_start = ts_for(trade_date, 9,  0,  IL_TZ)
        am_end   = ts_for(trade_date, 12, 30, IL_TZ)
        fwd_end  = am_end + LOOKFWD_HOURS * 3600

        am_5m  = [c for c in all_5m if am_start  <= c["timestamp"] <= am_end]
        fwd_5m = [c for c in all_5m if am_start  <= c["timestamp"] <= fwd_end]

        if len(am_5m) < 10:
            continue

        # Morning structure (pure range)
        am_high  = max(c["high"] for c in am_5m)
        am_low   = min(c["low"]  for c in am_5m)
        am_range = am_high - am_low
        am_eq    = (am_high + am_low) / 2
        am_open  = am_5m[0]["open"]

        dist_up   = am_high - am_open
        dist_down = am_open - am_low
        am_dir    = "bullish" if dist_up >= dist_down else "bearish"

        # Peak candle
        if am_dir == "bullish":
            peak_c = max(am_5m, key=lambda c: c["high"])
        else:
            peak_c = min(am_5m, key=lambda c: c["low"])
        peak_ts  = peak_c["timestamp"]
        peak_idt = datetime.fromtimestamp(peak_ts, tz=IL_TZ)
        peak_min = (peak_ts - am_start) / 60

        # ATR from 15m pre-session
        pre_15m = sorted([c for c in all_15m if c["timestamp"] < am_start],
                         key=lambda c: c["timestamp"])[-16:]
        atr_15m = analyzer.calculate_atr(list(reversed(pre_15m))) or am_range * 0.15 or 50.0

        # True 5m ATR — calculate from actual morning 5m candles
        atr_5m = analyzer.calculate_atr(list(reversed(am_5m))) or atr_15m

        # 5m candles AFTER peak (still within session + lookahead)
        post_peak = [c for c in fwd_5m if c["timestamp"] > peak_ts]
        if not post_peak:
            continue

        # Find first stall run
        result = find_first_stall_run(post_peak, atr_5m, MIN_STALL_RUN)

        has_slowdown = result is not None
        slowdown_start_idx = result[0] if result else None
        slowdown_run_len   = result[1] if result else None

        if has_slowdown:
            slowdown_candle  = post_peak[slowdown_start_idx]
            slowdown_ts      = slowdown_candle["timestamp"]
            slowdown_idt     = datetime.fromtimestamp(slowdown_ts, tz=IL_TZ)
            slowdown_min_from_peak = (slowdown_ts - peak_ts) / 60
            slowdown_price   = slowdown_candle["close"]

            # How deep into range is the slowdown start?
            if am_dir == "bullish":
                slowdown_pct = (slowdown_price - am_low) / am_range if am_range else 0
            else:
                slowdown_pct = (am_high - slowdown_price) / am_range if am_range else 0
            # 100% = at peak, 50% = EQ, 0% = at origin

            # Forward candles from slowdown
            fwd_from_slow = [c for c in post_peak if c["timestamp"] >= slowdown_ts]

            # Max retrace from the SLOWDOWN price (entry proxy)
            max_ret_from_slow = max_retrace_after(fwd_from_slow, slowdown_price, am_dir, am_range)

            # Did it reach EQ and near-EQ from slowdown price?
            reached_eq    = False
            reached_near_eq = False
            for c in fwd_from_slow:
                if am_dir == "bullish":
                    if c["low"] <= am_eq:
                        reached_eq = True
                    if c["low"] <= am_eq + NEAR_EQ_PCT * am_range:
                        reached_near_eq = True
                else:
                    if c["high"] >= am_eq:
                        reached_eq = True
                    if c["high"] >= am_eq - NEAR_EQ_PCT * am_range:
                        reached_near_eq = True
        else:
            slowdown_idt = None
            slowdown_min_from_peak = None
            slowdown_pct = None
            max_ret_from_slow = None
            reached_eq    = False
            reached_near_eq = False
            slowdown_run_len = None

        # Baseline: did price reach EQ at all (from any point after peak)?
        reached_eq_baseline = False
        max_ret_baseline = max_retrace_after(post_peak, peak_c["high"] if am_dir == "bullish" else peak_c["low"], am_dir, am_range)
        for c in post_peak:
            if am_dir == "bullish" and c["low"] <= am_eq:
                reached_eq_baseline = True
            elif am_dir == "bearish" and c["high"] >= am_eq:
                reached_eq_baseline = True

        records.append({
            "date":                 trade_date,
            "am_dir":               am_dir,
            "am_range_atr":         am_range / atr_15m,
            "peak_idt":             peak_idt.strftime("%H:%M"),
            "peak_min":             peak_min,
            "has_slowdown":         has_slowdown,
            "slowdown_run":         slowdown_run_len,
            "slowdown_min":         slowdown_min_from_peak,
            "slowdown_idt":         slowdown_idt.strftime("%H:%M") if slowdown_idt else "-",
            "slowdown_pct":         slowdown_pct,   # % toward peak (100=peak, 50=EQ)
            "max_ret_from_slow":    max_ret_from_slow,
            "reached_eq":           reached_eq,
            "reached_near_eq":      reached_near_eq,
            "reached_eq_baseline":  reached_eq_baseline,
            "max_ret_baseline":     max_ret_baseline,
            "atr_15m":              atr_15m,
        })

    n = len(records)
    db.close()

    with_sd = [r for r in records if r["has_slowdown"]]
    no_sd   = [r for r in records if not r["has_slowdown"]]

    print(f"\nDAX post-peak slowdown → EQ analysis  ({n} trading days, {SCAN_DAYS}-day scan)")
    print(f"Stall: body≤{STALL_BODY_ATR}×ATR OR range≤{STALL_RANGE_ATR}×ATR OR inside bar")
    print(f"Slowdown = {MIN_STALL_RUN}+ consecutive stalls after peak")
    print(f"Look-forward: {LOOKFWD_HOURS}h after peak\n")

    # ── 1. How often does the slowdown appear? ────────────────────────────────
    print("═" * 70)
    print("1. HOW OFTEN DOES THE POST-PEAK SLOWDOWN APPEAR?")
    print("═" * 70)
    print(f"  Days with {MIN_STALL_RUN}+ consecutive stalls after peak: "
          f"{len(with_sd)}/{n} ({pct_str(len(with_sd), n)})")
    print(f"  Days without slowdown: {len(no_sd)}/{n} ({pct_str(len(no_sd), n)})")

    if with_sd:
        sd_mins = [r["slowdown_min"] for r in with_sd if r["slowdown_min"] is not None]
        sd_runs = [r["slowdown_run"] for r in with_sd if r["slowdown_run"] is not None]
        print(f"\n  Slowdown timing (minutes after peak):")
        print(f"    Range:  {min(sd_mins):.0f}–{max(sd_mins):.0f} min")
        print(f"    Median: {median(sd_mins):.0f} min after peak")
        print(f"  Consecutive stall run length:")
        print(f"    Range:  {min(sd_runs):.0f}–{max(sd_runs):.0f} candles")
        print(f"    Median: {median(sd_runs):.0f} candles")
        for run in [2, 3, 4, 5]:
            cnt = sum(1 for r in with_sd if (r["slowdown_run"] or 0) >= run)
            print(f"    ≥{run} consecutive stalls: {cnt}/{len(with_sd)} ({pct_str(cnt, len(with_sd))})")

    # ── 2. EQ reach after slowdown ────────────────────────────────────────────
    print(f"\n{'═'*70}")
    print("2. DOES PRICE REACH EQ AFTER THE SLOWDOWN?")
    print(f"   EQ = 50% midpoint of morning range  |  Near-EQ = within ±{NEAR_EQ_PCT*100:.0f}% of range")
    print("═" * 70)

    n_eq_slow  = sum(1 for r in with_sd if r["reached_eq"])
    n_neq_slow = sum(1 for r in with_sd if r["reached_near_eq"])
    n_eq_base  = sum(1 for r in records if r["reached_eq_baseline"])

    print(f"  With slowdown    → reaches EQ:      {n_eq_slow}/{len(with_sd)} ({pct_str(n_eq_slow, len(with_sd))})")
    print(f"  With slowdown    → reaches near-EQ: {n_neq_slow}/{len(with_sd)} ({pct_str(n_neq_slow, len(with_sd))})")
    print(f"  ALL days baseline → reaches EQ:     {n_eq_base}/{n} ({pct_str(n_eq_base, n)})")
    if no_sd:
        n_eq_no_sd = sum(1 for r in no_sd if r["reached_eq_baseline"])
        print(f"  No slowdown      → reaches EQ:      {n_eq_no_sd}/{len(no_sd)} ({pct_str(n_eq_no_sd, len(no_sd))})")

    print(f"\n  Max retrace after slowdown (% of morning range from slowdown price):")
    mr = [r["max_ret_from_slow"]*100 for r in with_sd if r["max_ret_from_slow"] is not None]
    print(f"    Median: {median(mr):.1f}%   Mean: {mean(mr):.1f}%   Min: {min(mr):.1f}%   Max: {max(mr):.1f}%")

    print(f"\n  Cumulative: how many slowdown days reach each retrace level (from slowdown price)?")
    for lvl in [10, 20, 30, 40, 50, 60, 70, 80, 100]:
        cnt = sum(1 for r in with_sd if (r["max_ret_from_slow"] or 0)*100 >= lvl)
        eq_note = " ← EQ" if lvl == 50 else ""
        print(f"    ≥{lvl:3d}%  {cnt:>3}/{len(with_sd)} ({pct_str(cnt, len(with_sd))}){eq_note}")

    # ── 3. Slowdown depth: where is price when slowdown starts? ──────────────
    print(f"\n{'═'*70}")
    print("3. WHERE IS PRICE WHEN SLOWDOWN STARTS? (% from origin toward peak)")
    print("   100% = at morning peak  |  50% = EQ  |  0% = at origin")
    print("═" * 70)
    sd_pcts = [r["slowdown_pct"]*100 for r in with_sd if r["slowdown_pct"] is not None]
    print(f"  Median: {median(sd_pcts):.1f}%   Range: {min(sd_pcts):.1f}–{max(sd_pcts):.1f}%")

    for lo, hi in [(60, 80), (80, 95), (95, 110), (110, 150)]:
        g = [r for r in with_sd if r["slowdown_pct"] and lo <= r["slowdown_pct"]*100 < hi]
        if not g: continue
        eq_g = sum(1 for r in g if r["reached_eq"])
        mr_g = [r["max_ret_from_slow"]*100 for r in g if r["max_ret_from_slow"]]
        print(f"    Slowdown at {lo}–{hi}% toward peak  n={len(g):>2}  "
              f"EQ reach: {eq_g}/{len(g)} ({pct_str(eq_g,len(g))})  "
              f"median retrace: {median(mr_g):.1f}%")

    # ── 4. Morning range size vs slowdown EQ reach ────────────────────────────
    print(f"\n{'═'*70}")
    print("4. MORNING RANGE SIZE vs EQ REACH AFTER SLOWDOWN")
    print("═" * 70)
    for lo, hi in [(0, 3), (3, 5), (5, 8), (8, 99)]:
        g = [r for r in with_sd if lo <= r["am_range_atr"] < hi]
        if not g: continue
        eq_g = sum(1 for r in g if r["reached_eq"])
        mr_g = [r["max_ret_from_slow"]*100 for r in g if r["max_ret_from_slow"]]
        print(f"  Range {lo}–{hi if hi<99 else '∞'}×ATR  n={len(g):>2}  "
              f"EQ reach: {eq_g}/{len(g)} ({pct_str(eq_g,len(g))})  "
              f"median retrace: {median(mr_g):.1f}%")

    # ── 5. Per-day detail ─────────────────────────────────────────────────────
    print(f"\n{'═'*70}")
    print("5. PER-DAY DETAIL")
    print("═" * 70)
    print(f"  {'Date':12s} {'Dir':4s} {'RngATR':6s} {'PkIDT':5s}  "
          f"{'SlowIDT':7s} {'Run':3s}  {'SlowPct':7s}  {'MaxRet':6s}  {'EQ':3s}  {'NearEQ':6s}")
    for r in sorted(records, key=lambda x: x["date"]):
        eq_m  = "YES" if r["reached_eq"]      else "no"
        neq_m = "YES"  if r["reached_near_eq"] else "no"
        if r["has_slowdown"]:
            print(f"  {str(r['date']):12s} {r['am_dir'][:4]:4s} {r['am_range_atr']:6.1f} "
                  f"{r['peak_idt']:>5s}  "
                  f"{r['slowdown_idt']:>7s} {r['slowdown_run']:3d}  "
                  f"{r['slowdown_pct']*100:6.1f}%  "
                  f"{r['max_ret_from_slow']*100:6.1f}%  "
                  f"{eq_m:3s}  {neq_m:6s}")
        else:
            print(f"  {str(r['date']):12s} {r['am_dir'][:4]:4s} {r['am_range_atr']:6.1f} "
                  f"{r['peak_idt']:>5s}  "
                  f"{'no slowdown':>30s}  "
                  f"{r['max_ret_baseline']*100:6.1f}%  "
                  f"{'YES' if r['reached_eq_baseline'] else 'no':3s}")


if __name__ == "__main__":
    main()
