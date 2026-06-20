#!/usr/bin/env python3
"""
Test multiple definitions of "post-peak slowdown" on Frankfurt morning range.

Goal: find a definition that is:
  1. Selective  — fires on < 100% of days (not trivially universal)
  2. Predictive — days that fire have higher EQ reach than days that don't
  3. Early      — fires while price is still above EQ (in the premium zone)

Definitions tested:
  A. No new extreme: N consecutive 5m candles fail to make a new high/low
  B. Stall run: N consecutive stall candles (body≤X×ATR OR range≤X×ATR)
  C. Compression run: each candle range ≤ prev range (shrinking volatility)
  D. Range collapse: avg range of last N candles < X×ATR
  E. First lower high: candle makes lower high than the peak (structural)
  F. Combo: 2+ stall candles + first meaningful counter-direction body after
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
LOOKFWD_H = 5   # hours to look forward after peak for EQ hit


def ts_for(date, hour, minute, tz):
    from datetime import datetime as _dt
    return int(_dt(date.year, date.month, date.day, hour, minute, tzinfo=tz).timestamp())


def median(lst):
    if not lst: return float("nan")
    s = sorted(lst); n = len(s)
    return s[n//2] if n % 2 else (s[n//2-1] + s[n//2]) / 2


def pct_str(a, b):
    return f"{a/b*100:.0f}%" if b else "n/a"


# ── Slowdown detectors ────────────────────────────────────────────────────────

def detect_no_new_extreme(candles, am_dir, n):
    """
    First index where N consecutive candles all fail to make a new extreme.
    For bullish expansion: no new high for N candles after peak.
    Returns (idx_of_Nth_candle, price_at_that_point) or None.
    """
    run = 0
    prev_extreme = candles[0]["high"] if am_dir == "bullish" else candles[0]["low"]
    for i, c in enumerate(candles):
        new_extreme = (am_dir == "bullish" and c["high"] > prev_extreme) or \
                      (am_dir == "bearish" and c["low"]  < prev_extreme)
        if new_extreme:
            prev_extreme = c["high"] if am_dir == "bullish" else c["low"]
            run = 0
        else:
            run += 1
            if run >= n:
                price = c["close"]
                return i, price
    return None


def detect_stall_run(candles, atr, body_mult, range_mult, n):
    """First index where N consecutive stall candles appear."""
    run_start = None
    run_len   = 0
    for i, c in enumerate(candles):
        prev = candles[i-1] if i > 0 else None
        body = abs(c["close"] - c["open"])
        rng  = c["high"] - c["low"]
        inside = prev is not None and c["high"] <= prev["high"] and c["low"] >= prev["low"]
        is_stall = body <= body_mult * atr or rng <= range_mult * atr or inside
        if is_stall:
            if run_start is None: run_start = i
            run_len += 1
            if run_len >= n:
                return i, candles[i]["close"]
        else:
            run_start = None
            run_len   = 0
    return None


def detect_compression_run(candles, n):
    """
    First index where N consecutive candles each have range ≤ previous candle range.
    (Shrinking bar sizes = losing momentum.)
    """
    run = 0
    for i in range(1, len(candles)):
        cur_rng  = candles[i]["high"]   - candles[i]["low"]
        prev_rng = candles[i-1]["high"] - candles[i-1]["low"]
        if cur_rng < prev_rng:
            run += 1
            if run >= n - 1:   # n-1 because we need n candles total
                return i, candles[i]["close"]
        else:
            run = 0
    return None


def detect_range_collapse(candles, atr, window, threshold):
    """
    First index where average range of last `window` candles < threshold × ATR.
    """
    for i in range(window - 1, len(candles)):
        w = candles[i - window + 1: i + 1]
        avg_rng = sum(c["high"] - c["low"] for c in w) / window
        if avg_rng < threshold * atr:
            return i, candles[i]["close"]
    return None


def detect_lower_high(candles, am_dir):
    """
    For bullish expansion: first candle after peak (idx>0) whose HIGH < peak candle high.
    For bearish expansion: first candle whose LOW > peak candle low.
    This is the first structural signal that expansion has stopped.
    """
    if not candles:
        return None
    peak_ext = candles[0]["high"] if am_dir == "bullish" else candles[0]["low"]
    for i in range(1, len(candles)):
        c = candles[i]
        is_lower = (am_dir == "bullish" and c["high"] < peak_ext) or \
                   (am_dir == "bearish" and c["low"]  > peak_ext)
        if is_lower:
            return i, c["close"]
    return None


def detect_stall_then_push(candles, atr, stall_n, push_body_mult):
    """
    N stall candles followed by a meaningful counter-direction push.
    For bullish expansion: N stalls then a candle with bearish body > push_body_mult×ATR.
    """
    stall_count = 0
    for i, c in enumerate(candles):
        prev  = candles[i-1] if i > 0 else None
        body  = abs(c["close"] - c["open"])
        rng   = c["high"] - c["low"]
        inside = prev is not None and c["high"] <= prev["high"] and c["low"] >= prev["low"]
        is_stall = body <= 0.35 * atr or rng <= 0.55 * atr or inside

        if is_stall:
            stall_count += 1
        else:
            if stall_count >= stall_n:
                # This candle is after a stall run — is it a meaningful push?
                # "meaningful" = body in counter direction > push_body_mult×ATR
                # We don't know direction here, so just check any big body
                if body > push_body_mult * atr:
                    return i, c["close"]
            stall_count = 0
    return None


def reached_eq_after(candles_after, entry_price, am_dir, am_eq, am_range, near_pct=0.10):
    eq_hit = near_hit = False
    max_ret = 0.0
    for c in candles_after:
        if am_dir == "bullish":
            fav = (entry_price - c["low"]) / am_range if am_range else 0
            if c["low"] <= am_eq:                          eq_hit  = True
            if c["low"] <= am_eq + near_pct * am_range:   near_hit = True
        else:
            fav = (c["high"] - entry_price) / am_range if am_range else 0
            if c["high"] >= am_eq:                         eq_hit  = True
            if c["high"] >= am_eq - near_pct * am_range:  near_hit = True
        max_ret = max(max_ret, fav)
    return eq_hit, near_hit, max_ret


def price_in_premium(price, am_dir, am_eq):
    """Is price still above EQ (for bullish expansion fade)?"""
    return (am_dir == "bullish" and price > am_eq) or \
           (am_dir == "bearish" and price < am_eq)


# ── Main ──────────────────────────────────────────────────────────────────────

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

    days = []   # one entry per trading day

    for trade_date in trading_dates:
        am_start = ts_for(trade_date, 9,  0,  IL_TZ)
        am_end   = ts_for(trade_date, 12, 30, IL_TZ)
        fwd_end  = am_end + LOOKFWD_H * 3600

        am_5m  = [c for c in all_5m if am_start  <= c["timestamp"] <= am_end]
        fwd_5m = [c for c in all_5m if am_start  <= c["timestamp"] <= fwd_end]
        if len(am_5m) < 10:
            continue

        am_high = max(c["high"] for c in am_5m)
        am_low  = min(c["low"]  for c in am_5m)
        am_range = am_high - am_low
        if am_range < 20:
            continue
        am_eq   = (am_high + am_low) / 2
        am_open = am_5m[0]["open"]
        dist_up   = am_high - am_open
        dist_down = am_open - am_low
        am_dir = "bullish" if dist_up >= dist_down else "bearish"

        if am_dir == "bullish":
            peak_c = max(am_5m, key=lambda c: c["high"])
        else:
            peak_c = min(am_5m, key=lambda c: c["low"])
        peak_ts  = peak_c["timestamp"]
        peak_idt = datetime.fromtimestamp(peak_ts, tz=IL_TZ)

        pre_15m = sorted([c for c in all_15m if c["timestamp"] < am_start],
                         key=lambda c: c["timestamp"])[-16:]
        atr_15m = analyzer.calculate_atr(list(reversed(pre_15m))) or am_range * 0.15 or 50.0
        atr_5m  = analyzer.calculate_atr(list(reversed(am_5m))) or atr_15m

        # Post-peak candles (session + lookahead)
        post_peak = [c for c in fwd_5m if c["timestamp"] > peak_ts]
        if len(post_peak) < 5:
            continue

        # Baseline: does price reach EQ at all within lookahead?
        eq_base, neq_base, _ = reached_eq_after(post_peak, peak_c["high"] if am_dir=="bullish" else peak_c["low"], am_dir, am_eq, am_range)

        days.append({
            "date":      trade_date,
            "am_dir":    am_dir,
            "atr_15m":   atr_15m,
            "atr_5m":    atr_5m,
            "am_range":  am_range,
            "am_range_atr": am_range / atr_15m,
            "am_eq":     am_eq,
            "peak_ts":   peak_ts,
            "peak_idt":  peak_idt.strftime("%H:%M"),
            "post_peak": post_peak,
            "eq_base":   eq_base,
            "neq_base":  neq_base,
        })

    n = len(days)
    db.close()

    print(f"\nDAX post-peak slowdown definition comparison  ({n} trading days)")
    print(f"Lookahead: {LOOKFWD_H}h after peak  |  Scan: {SCAN_DAYS} days\n")
    print(f"Baseline: {sum(d['eq_base'] for d in days)}/{n} days reach EQ "
          f"({pct_str(sum(d['eq_base'] for d in days), n)})\n")

    # ── Build results table for each definition ───────────────────────────────
    definitions = []

    # A. No new extreme for N candles
    for n_ext in [3, 5, 7, 10]:
        fired = []
        for d in days:
            r = detect_no_new_extreme(d["post_peak"], d["am_dir"], n_ext)
            if r:
                idx, price = r
                in_prem = price_in_premium(price, d["am_dir"], d["am_eq"])
                fwd = d["post_peak"][idx:]
                eq, neq, mr = reached_eq_after(fwd, price, d["am_dir"], d["am_eq"], d["am_range"])
                fired.append({"eq": eq, "neq": neq, "mr": mr, "in_prem": in_prem,
                              "min_from_peak": idx * 5, "date": d["date"]})
        definitions.append((f"A: No new extreme ≥{n_ext} candles", fired, n))

    # B. Stall run with tighter ATR multipliers
    for (body_m, range_m, run_n) in [(0.35, 0.55, 3), (0.35, 0.55, 4),
                                      (0.5, 0.7, 2),  (0.5, 0.7, 3)]:
        fired = []
        for d in days:
            r = detect_stall_run(d["post_peak"], d["atr_5m"], body_m, range_m, run_n)
            if r:
                idx, price = r
                in_prem = price_in_premium(price, d["am_dir"], d["am_eq"])
                fwd = d["post_peak"][idx:]
                eq, neq, mr = reached_eq_after(fwd, price, d["am_dir"], d["am_eq"], d["am_range"])
                fired.append({"eq": eq, "neq": neq, "mr": mr, "in_prem": in_prem,
                              "min_from_peak": idx * 5, "date": d["date"]})
        definitions.append((f"B: Stall run ≥{run_n} (body≤{body_m}×ATR, rng≤{range_m}×ATR)", fired, n))

    # C. Compression run
    for comp_n in [3, 4, 5]:
        fired = []
        for d in days:
            r = detect_compression_run(d["post_peak"], comp_n)
            if r:
                idx, price = r
                in_prem = price_in_premium(price, d["am_dir"], d["am_eq"])
                fwd = d["post_peak"][idx:]
                eq, neq, mr = reached_eq_after(fwd, price, d["am_dir"], d["am_eq"], d["am_range"])
                fired.append({"eq": eq, "neq": neq, "mr": mr, "in_prem": in_prem,
                              "min_from_peak": idx * 5, "date": d["date"]})
        definitions.append((f"C: Compression run ≥{comp_n} shrinking bars", fired, n))

    # D. Range collapse
    for (win, thresh) in [(3, 0.5), (4, 0.5), (5, 0.5), (3, 0.4)]:
        fired = []
        for d in days:
            r = detect_range_collapse(d["post_peak"], d["atr_5m"], win, thresh)
            if r:
                idx, price = r
                in_prem = price_in_premium(price, d["am_dir"], d["am_eq"])
                fwd = d["post_peak"][idx:]
                eq, neq, mr = reached_eq_after(fwd, price, d["am_dir"], d["am_eq"], d["am_range"])
                fired.append({"eq": eq, "neq": neq, "mr": mr, "in_prem": in_prem,
                              "min_from_peak": idx * 5, "date": d["date"]})
        definitions.append((f"D: Avg range <{thresh}×ATR over {win} candles", fired, n))

    # E. First lower high / higher low
    fired = []
    for d in days:
        r = detect_lower_high(d["post_peak"], d["am_dir"])
        if r:
            idx, price = r
            in_prem = price_in_premium(price, d["am_dir"], d["am_eq"])
            fwd = d["post_peak"][idx:]
            eq, neq, mr = reached_eq_after(fwd, price, d["am_dir"], d["am_eq"], d["am_range"])
            fired.append({"eq": eq, "neq": neq, "mr": mr, "in_prem": in_prem,
                          "min_from_peak": idx * 5, "date": d["date"]})
    definitions.append(("E: First lower high after peak", fired, n))

    # F. Stall then push
    for (stall_n, push_m) in [(2, 0.5), (2, 0.7), (3, 0.5)]:
        fired = []
        for d in days:
            r = detect_stall_then_push(d["post_peak"], d["atr_5m"], stall_n, push_m)
            if r:
                idx, price = r
                in_prem = price_in_premium(price, d["am_dir"], d["am_eq"])
                fwd = d["post_peak"][idx:]
                eq, neq, mr = reached_eq_after(fwd, price, d["am_dir"], d["am_eq"], d["am_range"])
                fired.append({"eq": eq, "neq": neq, "mr": mr, "in_prem": in_prem,
                              "min_from_peak": idx * 5, "date": d["date"]})
        definitions.append((f"F: ≥{stall_n} stalls then push >>{push_m}×ATR body", fired, n))

    # ── Print summary table ───────────────────────────────────────────────────
    print(f"  {'Definition':48s}  {'Fire':>5s}  {'%fire':>5s}  {'%EQ':>5s}  {'%nEQ':>5s}  "
          f"{'%prem':>5s}  {'medR':>5s}  {'lag':>5s}")
    print(f"  {'-'*48}  {'─'*5}  {'─'*5}  {'─'*5}  {'─'*5}  {'─'*5}  {'─'*5}  {'─'*5}")
    for label, fired, total in definitions:
        if not fired:
            print(f"  {label:48s}  {'0':>5s}  {'0%':>5s}  {'—':>5s}  {'—':>5s}  {'—':>5s}  {'—':>5s}  {'—':>5s}")
            continue
        nf   = len(fired)
        eq_r = sum(1 for f in fired if f["eq"])
        neq_r= sum(1 for f in fired if f["neq"])
        prem = sum(1 for f in fired if f["in_prem"])
        mrs  = [f["mr"] * 100 for f in fired]
        lags = [f["min_from_peak"] for f in fired]
        print(f"  {label:48s}  {nf:>5}  {pct_str(nf,total):>5s}  "
              f"{pct_str(eq_r,nf):>5s}  {pct_str(neq_r,nf):>5s}  "
              f"{pct_str(prem,nf):>5s}  {median(mrs):>4.0f}%  {median(lags):>4.0f}m")

    # ── Deep dive on the most selective / useful definitions ──────────────────
    print(f"\n{'═'*70}")
    print("DEEP DIVE — per-day for the most selective definitions")
    print("═" * 70)

    # Pick definitions that fire on <70% of days and have decent EQ reach
    interesting = [(lbl, fired) for lbl, fired, tot in definitions
                   if 0 < len(fired) < 0.70 * n and
                   sum(1 for f in fired if f["eq"]) / len(fired) >= 0.70]

    for label, fired in interesting[:3]:   # top 3 most interesting
        nf   = len(fired)
        eq_r = sum(1 for f in fired if f["eq"])
        neq_r= sum(1 for f in fired if f["neq"])
        prem = sum(1 for f in fired if f["in_prem"])
        mrs  = [f["mr"] * 100 for f in fired]
        print(f"\n  {label}")
        print(f"  Fires: {nf}/{n}  EQ reach: {eq_r}/{nf} ({pct_str(eq_r,nf)})  "
              f"In premium: {prem}/{nf} ({pct_str(prem,nf)})  "
              f"Median retrace: {median(mrs):.0f}%")
        # Show days it does NOT fire
        fired_dates = {f["date"] for f in fired}
        no_fire = [d for d in days if d["date"] not in fired_dates]
        if no_fire:
            nf_eq = sum(1 for d in no_fire if d["eq_base"])
            print(f"  Days without signal: {len(no_fire)}  EQ reach anyway: {nf_eq}/{len(no_fire)} ({pct_str(nf_eq,len(no_fire))})")


if __name__ == "__main__":
    main()
