#!/usr/bin/env python3
"""
Frankfurt-before-the-bell — focused analysis on gap < 2×ATR days.

Three questions:
  1. Within gap < 2×ATR, what separates full-fill days from partial-fill days?
  2. Does a liquidity sweep of the Asian session range predict the gap fill?
  3. What is the R:R if you enter AFTER the sweep rather than at the open?

Key times (UTC, summer CEST/IDT):
  22:05 UTC → Asian session opens (01:05 IDT, after daily CFD reset)
  03:55 UTC → Last Asian candle before entry (06:55 IDT)
  04:00 UTC → Entry / analysis start (07:00 IDT)
  06:30 UTC → Window end / exit (09:30 IDT)
  15:25 UTC → Frankfurt close (prev day, 18:25 IDT open → 18:30 IDT close)

Asian session range = high/low of 22:05–03:55 UTC candles.

Liquidity sweep = price temporarily breaks the Asian session extreme
  on the OPPOSITE side from the Frankfurt close, then reverses back.
  (Bullish setup: price sweeps Asian low first, then heads up toward Frankfurt close.)
  (Bearish setup: price sweeps Asian high first, then heads down toward Frankfurt close.)
"""

import sys, statistics
from pathlib import Path
from datetime import datetime, timezone, timedelta, date

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from db.local_db import LocalDB

DB_PATH = str(Path(__file__).resolve().parents[1] / "src" / "data" / "trade.db")
SYMBOL  = "GER40"
TF      = "5m"

MAX_GAP_ATR   = 2.0   # only trade days with gap < 2×ATR
MIN_GAP_ATR   = 0.3

ENTRY_H, ENTRY_M     = 4,  0    # 07:00 IDT
WINDOW_END_H, WINDOW_END_M = 6, 30   # 09:30 IDT
FKFT_CLOSE_H, FKFT_CLOSE_M = 15, 25  # last 5m before Frankfurt close


def ts_utc(d: date, h: int, m: int) -> int:
    return int(datetime(d.year, d.month, d.day, h, m, tzinfo=timezone.utc).timestamp())


def median(lst):
    if not lst: return float("nan")
    s = sorted(lst); n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def pct_str(a, b):
    return f"{a / b * 100:.0f}%" if b else "n/a"


def simple_atr(candles, n=20):
    tail = candles[-n:] if len(candles) >= n else candles
    if len(tail) < 2:
        return 0.0
    trs = []
    for i in range(1, len(tail)):
        c, p = tail[i], tail[i - 1]
        trs.append(max(c["high"] - c["low"],
                       abs(c["high"] - p["close"]),
                       abs(c["low"] - p["close"])))
    return sum(trs) / len(trs)


def main():
    db = LocalDB(DB_PATH)
    raw = db.query_recent(SYMBOL, TF, limit=80000)
    all_5m = list(reversed(raw))

    by_date: dict = {}
    for c in all_5m:
        d = datetime.fromtimestamp(c["timestamp"], timezone.utc).date()
        by_date.setdefault(d, []).append(c)

    trading_dates = sorted(d for d in by_date if d.weekday() < 5)
    records = []

    for i, trade_date in enumerate(trading_dates):
        if i == 0:
            continue

        prev_date = trading_dates[i - 1]

        # ── Frankfurt close (previous day) ────────────────────────────────────
        close_cutoff = ts_utc(prev_date, FKFT_CLOSE_H, FKFT_CLOSE_M)
        prev_candles = [c for c in by_date.get(prev_date, []) if c["timestamp"] <= close_cutoff]
        if not prev_candles:
            continue
        fkft_close = prev_candles[-1]["close"]

        # ── ATR from previous Xetra session ──────────────────────────────────
        atr = simple_atr(prev_candles, n=20)
        if atr < 1:
            continue

        # ── Asian session range (22:05 UTC prev day → 03:55 UTC today) ───────
        asian_start = ts_utc(prev_date, 22,  5)
        asian_end   = ts_utc(trade_date, 3, 55)
        asian_candles = [c for c in all_5m
                         if asian_start <= c["timestamp"] <= asian_end]
        if len(asian_candles) < 5:
            continue
        asian_high = max(c["high"]  for c in asian_candles)
        asian_low  = min(c["low"]   for c in asian_candles)
        asian_mid  = (asian_high + asian_low) / 2

        # ── Entry and window ──────────────────────────────────────────────────
        entry_ts   = ts_utc(trade_date, ENTRY_H, ENTRY_M)
        window_end = ts_utc(trade_date, WINDOW_END_H, WINDOW_END_M)
        window     = [c for c in by_date.get(trade_date, [])
                      if entry_ts <= c["timestamp"] <= window_end]
        if not window:
            continue

        entry_price = window[0]["open"]
        gap_pts     = entry_price - fkft_close
        gap_atr     = abs(gap_pts) / atr
        if not (MIN_GAP_ATR <= gap_atr < MAX_GAP_ATR):
            continue

        trade_dir = "bearish" if gap_pts > 0 else "bullish"
        bullish   = trade_dir == "bullish"

        # ── Gap fill tracking ─────────────────────────────────────────────────
        gap_filled      = False
        fill_ts         = None
        fill_min        = None
        max_retrace_pts = 0.0

        for c in window:
            retrace = (c["high"] - entry_price) if bullish else (entry_price - c["low"])
            max_retrace_pts = max(max_retrace_pts, retrace)
            if bullish and c["high"] >= fkft_close and not gap_filled:
                gap_filled = True; fill_ts = c["timestamp"]
                fill_min   = (fill_ts - entry_ts) / 60
            elif not bullish and c["low"] <= fkft_close and not gap_filled:
                gap_filled = True; fill_ts = c["timestamp"]
                fill_min   = (fill_ts - entry_ts) / 60

        gap_fill_pct = max_retrace_pts / abs(gap_pts) if abs(gap_pts) > 0 else 0

        # ── Asian midpoint hit ────────────────────────────────────────────────
        # (first structure target between entry and Frankfurt close)
        asian_mid_hit = False
        for c in window:
            if bullish  and c["high"] >= asian_mid: asian_mid_hit = True; break
            if not bullish and c["low"] <= asian_mid:  asian_mid_hit = True; break

        # ── Liquidity sweep detection ─────────────────────────────────────────
        # Bullish: price sweeps BELOW Asian low before heading toward Frankfurt close
        # Bearish: price sweeps ABOVE Asian high before heading toward Frankfurt close
        sweep_detected  = False
        sweep_ts        = None
        sweep_min       = None
        sweep_extreme   = None   # low of the sweep (bull) or high (bear)
        post_sweep_fill = False  # did gap fill AFTER the sweep?

        for j, c in enumerate(window):
            if sweep_detected:
                # already swept — track if gap fills from here
                if bullish  and c["high"] >= fkft_close: post_sweep_fill = True; break
                if not bullish and c["low"]  <= fkft_close: post_sweep_fill = True; break
            else:
                if bullish and c["low"] < asian_low:
                    sweep_detected = True
                    sweep_ts       = c["timestamp"]
                    sweep_min      = (sweep_ts - entry_ts) / 60
                    sweep_extreme  = min(c["low"] for c in window[:j + 1])
                elif not bullish and c["high"] > asian_high:
                    sweep_detected = True
                    sweep_ts       = c["timestamp"]
                    sweep_min      = (sweep_ts - entry_ts) / 60
                    sweep_extreme  = max(c["high"] for c in window[:j + 1])

        # R:R if entering at sweep extreme (with SL just beyond the sweep)
        rr_sweep = None
        if sweep_detected and sweep_extreme is not None:
            sweep_entry = sweep_extreme
            gap_reward  = abs(fkft_close - sweep_entry)
            # SL buffer: 0.25×ATR beyond the sweep
            sl_buffer   = 0.25 * atr
            if gap_reward > 0:
                rr_sweep = gap_reward / sl_buffer

        # R:R at window open (TP = fkft_close, SL = worst move in window)
        if bullish:
            worst = min(c["low"] for c in window)
        else:
            worst = max(c["high"] for c in window)
        sl_dist = abs(entry_price - worst)
        rr_open = abs(gap_pts) / sl_dist if sl_dist > 0 else 0

        # ── Sweep predictor features ──────────────────────────────────────────
        # How far (in ATR) does price need to move to reach the Asian sweep extreme?
        # Bullish: sweeps Asian LOW → dist = (entry - asian_low) / atr
        # Bearish: sweeps Asian HIGH → dist = (asian_high - entry) / atr
        # Negative = price already beyond the extreme at entry (rare)
        if bullish:
            dist_to_sweep_atr = (entry_price - asian_low)  / atr
        else:
            dist_to_sweep_atr = (asian_high  - entry_price) / atr

        asian_range_atr = (asian_high - asian_low) / atr

        # Where is entry within the Asian range?
        # 0% = at the sweep extreme, 100% = at the far extreme (toward Frankfurt close)
        if asian_range_atr > 0:
            if bullish:
                entry_pct_asian = (entry_price - asian_low)  / (asian_high - asian_low) * 100
            else:
                entry_pct_asian = (asian_high - entry_price) / (asian_high - asian_low) * 100
        else:
            entry_pct_asian = 50.0

        records.append({
            "date":               trade_date,
            "trade_dir":          trade_dir,
            "gap_pts":            gap_pts,
            "gap_atr":            gap_atr,
            "fkft_close":         fkft_close,
            "entry_price":        entry_price,
            "asian_high":         asian_high,
            "asian_low":          asian_low,
            "asian_mid":          asian_mid,
            "asian_range_atr":    asian_range_atr,
            "dist_to_sweep_atr":  dist_to_sweep_atr,
            "entry_pct_asian":    entry_pct_asian,
            "gap_filled":         gap_filled,
            "fill_min":           fill_min,
            "gap_fill_pct":       gap_fill_pct,
            "asian_mid_hit":      asian_mid_hit,
            "sweep":              sweep_detected,
            "sweep_min":          sweep_min,
            "post_sweep_fill":    post_sweep_fill,
            "rr_sweep":           rr_sweep,
            "rr_open":            rr_open,
            "atr":                atr,
        })

    n = len(records)
    print(f"\n{'═' * 72}")
    print(f"Frankfurt-before-the-bell  —  gap 0.3–2×ATR only  ({n} days)")
    print(f"Entry 04:00 UTC (07:00 IDT)  |  Window ends 06:30 UTC (09:30 IDT)")
    print(f"Asian session range = 22:05–03:55 UTC (01:05–06:55 IDT)")
    print(f"{'═' * 72}")

    # ── 1. Gap fill by gap-size band ──────────────────────────────────────────
    print(f"\n1.  GAP FILL RATES BY GAP SIZE")
    print(f"    {'Band':>14s}  {'N':>4s}  {'FullFill':>8s}  {'≥50%fill':>8s}  {'MedRetrace':>10s}")
    for lo, hi in [(0.3, 0.5), (0.5, 0.75), (0.75, 1.0), (1.0, 1.5), (1.5, 2.0)]:
        g = [r for r in records if lo <= r["gap_atr"] < hi]
        if not g: continue
        nf   = sum(1 for r in g if r["gap_filled"])
        n50  = sum(1 for r in g if r["gap_fill_pct"] >= 0.5)
        med  = median([r["gap_fill_pct"] * 100 for r in g])
        lbl  = f"{lo:.2f}–{hi:.2f}×ATR"
        print(f"    {lbl:>14s}  {len(g):>4d}  {pct_str(nf,len(g)):>8s}  "
              f"{pct_str(n50,len(g)):>8s}  {med:>9.0f}%")

    # ── 2. Liquidity sweep stats ───────────────────────────────────────────────
    sweep_days    = [r for r in records if r["sweep"]]
    no_sweep_days = [r for r in records if not r["sweep"]]
    print(f"\n2.  LIQUIDITY SWEEP OF ASIAN SESSION RANGE")
    print(f"    (sweep = price briefly breaks Asian high/low away from Frankfurt close")
    print(f"    before reversing toward it)")
    print()
    print(f"    Days with sweep:    {len(sweep_days)}/{n}  ({pct_str(len(sweep_days), n)})")
    print(f"    Days without sweep: {len(no_sweep_days)}/{n}  ({pct_str(len(no_sweep_days), n)})")

    if sweep_days:
        smins = [r["sweep_min"] for r in sweep_days if r["sweep_min"] is not None]
        print(f"\n    Sweep timing (minutes after 04:00 UTC / 07:00 IDT):")
        print(f"    Median: {median(smins):.0f} min  "
              f"→ {int(4 + median(smins)//60):02d}:{int(median(smins)%60):02d} UTC  "
              f"/ {int(7 + median(smins)//60):02d}:{int(median(smins)%60):02d} IDT")
        for lo, hi in [(0, 30), (30, 60), (60, 90), (90, 120), (120, 150)]:
            cnt = sum(1 for m in smins if lo <= m < hi)
            idt_lo = f"{7 + lo//60:02d}:{lo%60:02d}"
            idt_hi = f"{7 + hi//60:02d}:{hi%60:02d}"
            print(f"    {idt_lo}–{idt_hi} IDT:  {cnt}/{len(smins)}  ({pct_str(cnt, len(smins))})")

    # ── 3. Sweep vs no-sweep: full-fill rate comparison ───────────────────────
    print(f"\n3.  SWEEP vs NO-SWEEP — gap fill comparison")
    print(f"    {'Group':30s}  {'N':>4s}  {'FullFill':>8s}  {'≥50%':>6s}  {'MedRetrace':>10s}")
    for lbl, g in [
        ("WITH Asian sweep",      sweep_days),
        ("WITHOUT Asian sweep",   no_sweep_days),
    ]:
        if not g: continue
        nf  = sum(1 for r in g if r["gap_filled"])
        n50 = sum(1 for r in g if r["gap_fill_pct"] >= 0.5)
        med = median([r["gap_fill_pct"] * 100 for r in g])
        print(f"    {lbl:30s}  {len(g):>4d}  {pct_str(nf, len(g)):>8s}  "
              f"{pct_str(n50, len(g)):>6s}  {med:>9.0f}%")

    # Post-sweep fill specifically
    psf = sum(1 for r in sweep_days if r["post_sweep_fill"])
    print(f"\n    Post-sweep fill (gap fills AFTER the sweep): "
          f"{psf}/{len(sweep_days)}  ({pct_str(psf, len(sweep_days))})")

    # ── 4. Asian midpoint as partial target ───────────────────────────────────
    print(f"\n4.  ASIAN SESSION MIDPOINT as intermediate target")
    print(f"    (midpoint = halfway between Asian high and low — first structure target)")
    mid_hit = sum(1 for r in records if r["asian_mid_hit"])
    print(f"    Hit Asian midpoint in window: {mid_hit}/{n}  ({pct_str(mid_hit, n)})")
    # Of those that hit mid: how many go on to full fill?
    mid_then_full = sum(1 for r in records if r["asian_mid_hit"] and r["gap_filled"])
    mid_no_full   = sum(1 for r in records if r["asian_mid_hit"] and not r["gap_filled"])
    print(f"      → also fully fill gap:  {mid_then_full}/{mid_hit}  ({pct_str(mid_then_full, mid_hit)})")
    print(f"      → stop at midpoint:     {mid_no_full}/{mid_hit}  ({pct_str(mid_no_full, mid_hit)})")
    no_mid = [r for r in records if not r["asian_mid_hit"]]
    print(f"    Didn't reach midpoint: {len(no_mid)}/{n}  "
          f"(full fill from those: {sum(1 for r in no_mid if r['gap_filled'])}/{len(no_mid)})")

    # ── 5. Sweep entry R:R vs open entry R:R ─────────────────────────────────
    print(f"\n5.  R:R COMPARISON")
    rr_open_all  = [r["rr_open"] for r in records]
    rr_sweep_all = [r["rr_sweep"] for r in sweep_days if r["rr_sweep"] is not None]
    print(f"    Entry at 07:00 IDT open (TP=fkft_close, SL=window worst):")
    print(f"      Median R:R: {median(rr_open_all):.1f}  (n={len(rr_open_all)})")
    if rr_sweep_all:
        print(f"    Entry at Asian sweep extreme (TP=fkft_close, SL=sweep+0.25×ATR buffer):")
        print(f"      Median R:R: {median(rr_sweep_all):.1f}  (n={len(rr_sweep_all)})")

    # ── 6. Distinguishing full-fill vs partial: feature table ────────────────
    full_fill  = [r for r in records if r["gap_filled"]]
    part_fill  = [r for r in records if not r["gap_filled"]]
    print(f"\n6.  WHAT SEPARATES FULL-FILL DAYS FROM PARTIAL-FILL DAYS?")
    print(f"    {'Feature':35s}  {'Full fill (n={})'.format(len(full_fill)):>18s}  "
          f"{'Partial (n={})'.format(len(part_fill)):>18s}")

    def pct_feature(group, key):
        vals = [r[key] for r in group if r[key] is not None]
        return f"{sum(vals)/len(vals)*100:.0f}%" if vals else "n/a"

    def med_feature(group, key):
        vals = [r[key] for r in group if r[key] is not None and isinstance(r[key], (int, float))]
        return f"{median(vals):.2f}" if vals else "n/a"

    rows = [
        ("Median gap size (×ATR)",      "gap_atr",         "median"),
        ("Bullish direction (%)",        "bullish_flag",    "pct"),
        ("Had Asian sweep (%)",          "sweep",           "pct"),
        ("Post-sweep filled (%)",        "post_sweep_fill", "pct"),
        ("Asian midpoint hit (%)",       "asian_mid_hit",   "pct"),
    ]

    # Add derived fields
    for r in records:
        r["bullish_flag"] = 1 if r["trade_dir"] == "bullish" else 0

    print(f"    {'Median gap (×ATR)':35s}  "
          f"  {median([r['gap_atr'] for r in full_fill]):>17.2f}  "
          f"  {median([r['gap_atr'] for r in part_fill]):>17.2f}")
    print(f"    {'Bullish direction':35s}  "
          f"  {pct_str(sum(r['trade_dir']=='bullish' for r in full_fill), len(full_fill)):>17s}  "
          f"  {pct_str(sum(r['trade_dir']=='bullish' for r in part_fill), len(part_fill)):>17s}")
    print(f"    {'Had Asian session sweep':35s}  "
          f"  {pct_str(sum(r['sweep'] for r in full_fill), len(full_fill)):>17s}  "
          f"  {pct_str(sum(r['sweep'] for r in part_fill), len(part_fill)):>17s}")
    print(f"    {'Hit Asian session midpoint':35s}  "
          f"  {pct_str(sum(r['asian_mid_hit'] for r in full_fill), len(full_fill)):>17s}  "
          f"  {pct_str(sum(r['asian_mid_hit'] for r in part_fill), len(part_fill)):>17s}")
    print(f"    {'Tues or Thu':35s}  "
          f"  {pct_str(sum(r['date'].weekday() in (1,3) for r in full_fill), len(full_fill)):>17s}  "
          f"  {pct_str(sum(r['date'].weekday() in (1,3) for r in part_fill), len(part_fill)):>17s}")

    # ── 7. Combined gate: gap < 1×ATR AND bullish AND (Tue or Thu) ───────────
    print(f"\n7.  COMBINED FILTERS — stacking the best signals")
    combos = [
        ("Gap < 1×ATR",                         lambda r: r["gap_atr"] < 1.0),
        ("Gap < 1×ATR + bullish",                lambda r: r["gap_atr"] < 1.0 and r["trade_dir"] == "bullish"),
        ("Gap < 1×ATR + Tue/Thu",                lambda r: r["gap_atr"] < 1.0 and r["date"].weekday() in (1, 3)),
        ("Gap < 1×ATR + sweep",                  lambda r: r["gap_atr"] < 1.0 and r["sweep"]),
        ("Gap 1–2×ATR + sweep",                  lambda r: 1.0 <= r["gap_atr"] < 2.0 and r["sweep"]),
        ("Gap 1–2×ATR + sweep + Tue/Thu",        lambda r: 1.0 <= r["gap_atr"] < 2.0 and r["sweep"] and r["date"].weekday() in (1, 3)),
        ("Any gap + sweep + Tue/Thu",             lambda r: r["sweep"] and r["date"].weekday() in (1, 3)),
    ]
    print(f"    {'Filter':40s}  {'N':>4s}  {'FullFill':>8s}  {'≥50%':>6s}  {'MedRetrace':>10s}")
    for lbl, fn in combos:
        g = [r for r in records if fn(r)]
        if not g: continue
        nf  = sum(1 for r in g if r["gap_filled"])
        n50 = sum(1 for r in g if r["gap_fill_pct"] >= 0.5)
        med = median([r["gap_fill_pct"] * 100 for r in g])
        print(f"    {lbl:40s}  {len(g):>4d}  {pct_str(nf,len(g)):>8s}  "
              f"{pct_str(n50,len(g)):>6s}  {med:>9.0f}%")

    # ── 8. SWEEP PREDICTION ───────────────────────────────────────────────────
    print(f"\n{'═' * 72}")
    print(f"SWEEP PREDICTION — what at 07:00 IDT predicts whether a sweep happens?")
    print(f"{'═' * 72}")

    def sweep_rate(group):
        if not group: return "n/a", 0
        r = sum(1 for x in group if x["sweep"])
        return pct_str(r, len(group)), len(group)

    # A. Distance from entry to Asian sweep extreme
    print(f"\nA.  Distance from 07:00 IDT price to Asian sweep extreme (in ATR)")
    print(f"    (bullish: dist to Asian LOW  |  bearish: dist to Asian HIGH)")
    print(f"    {'Band':>16s}  {'N':>4s}  {'Sweep rate':>10s}  {'Fill rate (no sweep)':>20s}")
    for lo, hi in [(-1, 0.1), (0.1, 0.3), (0.3, 0.5), (0.5, 0.75), (0.75, 1.0), (1.0, 2.0), (2.0, 9)]:
        g = [r for r in records if lo <= r["dist_to_sweep_atr"] < hi]
        if not g: continue
        sw_rate, _ = sweep_rate(g)
        no_sw  = [r for r in g if not r["sweep"]]
        fill_r = pct_str(sum(1 for r in no_sw if r["gap_filled"]), len(no_sw)) if no_sw else "n/a"
        lbl = f"{'<0 (beyond)' if hi==0.1 and lo<0 else f'{lo:.2f}–{hi:.2f}' if hi<9 else f'≥{lo:.2f}'}×ATR"
        print(f"    {lbl:>16s}  {len(g):>4d}  {sw_rate:>10s}  {fill_r:>20s}")

    # B. Asian range size
    print(f"\nB.  Asian session range size (High − Low, in ATR)")
    print(f"    {'Band':>12s}  {'N':>4s}  {'Sweep rate':>10s}  {'Med dist-to-extreme':>20s}")
    for lo, hi in [(0, 1), (1, 2), (2, 3), (3, 5), (5, 9)]:
        g = [r for r in records if lo <= r["asian_range_atr"] < hi]
        if not g: continue
        sw_rate, _ = sweep_rate(g)
        med_dist = median([r["dist_to_sweep_atr"] for r in g])
        lbl = f"{lo}–{hi}×ATR" if hi < 9 else f"≥{lo}×ATR"
        print(f"    {lbl:>12s}  {len(g):>4d}  {sw_rate:>10s}  {med_dist:>19.2f}x")

    # C. Where is entry within Asian range
    print(f"\nC.  Entry position within Asian range at 07:00 IDT")
    print(f"    0% = price sitting on Asian extreme (sweep level)")
    print(f"    100% = price at far side of range (away from sweep level)")
    print(f"    {'Band':>14s}  {'N':>4s}  {'Sweep rate':>10s}  {'Fill (no sweep)':>15s}")
    for lo, hi in [(0, 20), (20, 40), (40, 60), (60, 80), (80, 100), (100, 200)]:
        g = [r for r in records if lo <= r["entry_pct_asian"] < hi]
        if not g: continue
        sw_rate, _ = sweep_rate(g)
        no_sw  = [r for r in g if not r["sweep"]]
        fill_r = pct_str(sum(1 for r in no_sw if r["gap_filled"]), len(no_sw)) if no_sw else "n/a"
        lbl = f"{lo}–{hi}%" if hi <= 100 else f">{lo}% (outside)"
        print(f"    {lbl:>14s}  {len(g):>4d}  {sw_rate:>10s}  {fill_r:>15s}")

    # D. Direction
    print(f"\nD.  Direction")
    for td in ("bullish", "bearish"):
        g = [r for r in records if r["trade_dir"] == td]
        sr, _ = sweep_rate(g)
        no_sw = [r for r in g if not r["sweep"]]
        fr = pct_str(sum(1 for r in no_sw if r["gap_filled"]), len(no_sw)) if no_sw else "n/a"
        print(f"    {td:8s}: n={len(g):>2}  sweep rate={sr:>4s}  fill (no sweep)={fr}")

    # E. Day of week
    print(f"\nE.  Day of week")
    for dow, nm in enumerate(["Mon","Tue","Wed","Thu","Fri"]):
        g = [r for r in records if r["date"].weekday() == dow]
        if not g: continue
        sr, _ = sweep_rate(g)
        no_sw = [r for r in g if not r["sweep"]]
        fr = pct_str(sum(1 for r in no_sw if r["gap_filled"]), len(no_sw)) if no_sw else "n/a"
        print(f"    {nm}: n={len(g):>2}  sweep rate={sr:>4s}  fill (no sweep)={fr}")

    # F. Practical prediction rule
    print(f"\nF.  PRACTICAL PREDICTION RULE")
    print(f"    Based on distance-to-sweep-extreme at 07:00 IDT:\n")
    # Threshold A: likely sweep (dist < 0.3×ATR)
    likely_sweep   = [r for r in records if r["dist_to_sweep_atr"] < 0.3]
    unlikely_sweep = [r for r in records if r["dist_to_sweep_atr"] >= 0.3]
    ls_sw = sum(1 for r in likely_sweep if r["sweep"])
    us_sw = sum(1 for r in unlikely_sweep if r["sweep"])
    ls_fill_ns = [r for r in likely_sweep   if not r["sweep"] and r["gap_filled"]]
    us_fill_ns = [r for r in unlikely_sweep if not r["sweep"] and r["gap_filled"]]

    print(f"    dist < 0.3×ATR (price near Asian extreme):")
    print(f"      n={len(likely_sweep)}  sweep rate: {pct_str(ls_sw, len(likely_sweep))}  "
          f"fill if no sweep: {pct_str(len(ls_fill_ns), len([r for r in likely_sweep if not r['sweep']]) or 1)}")
    print(f"    dist ≥ 0.3×ATR (price away from extreme):")
    print(f"      n={len(unlikely_sweep)}  sweep rate: {pct_str(us_sw, len(unlikely_sweep))}  "
          f"fill if no sweep: {pct_str(len(us_fill_ns), len([r for r in unlikely_sweep if not r['sweep']]) or 1)}")

    print(f"\n    → DECISION TREE at 07:00 IDT:")
    print(f"      dist_to_extreme < 0.3×ATR  → SWEEP LIKELY")
    print(f"        wait for the sweep, then enter at extreme for Setup B (8.5×R:R)")
    print(f"      dist_to_extreme ≥ 0.3×ATR  → SWEEP UNLIKELY")
    print(f"        enter immediately for Setup A (clean gap fill, ~72% fill rate)")

    # ── 9. Per-day detail ─────────────────────────────────────────────────────
    print(f"\n9.  PER-DAY DETAIL")
    print(f"    {'Date':12s}  {'DoW':3s}  {'Dir':4s}  {'GapATR':>6s}  {'DistSwp':>7s}  "
          f"{'Sweep':5s}  {'SwpMin':>6s}  {'MidHit':>6s}  {'Ret%':>5s}  {'Fill':>4s}  {'FMin':>4s}")
    for r in records:
        dow    = ["Mon","Tue","Wed","Thu","Fri"][r["date"].weekday()]
        swp    = "YES" if r["sweep"]         else "no"
        mid    = "YES" if r["asian_mid_hit"] else "no"
        fil    = "YES" if r["gap_filled"]    else "no"
        smin   = f"{r['sweep_min']:5.0f}" if r["sweep_min"] is not None else "   --"
        fmin   = f"{r['fill_min']:4.0f}"  if r["fill_min"]  is not None else "  --"
        print(f"    {str(r['date']):12s}  {dow}  {r['trade_dir'][:4]:4s}  "
              f"{r['gap_atr']:6.2f}x  {r['dist_to_sweep_atr']:6.2f}x  "
              f"{swp:5s}  {smin:>6s}  {mid:>6s}  "
              f"{r['gap_fill_pct']*100:5.0f}%  {fil:>4s}  {fmin:>4s}")

    db.close()


if __name__ == "__main__":
    main()
