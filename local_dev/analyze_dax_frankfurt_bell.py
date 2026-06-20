#!/usr/bin/env python3
"""
Frankfurt-before-the-bell strategy analysis.

Hypothesis (user description):
  After Frankfurt/Xetra closes (15:30 UTC = 17:30 CEST = 18:30 IDT), the NY session
  can push the DAX CFD significantly away from that closing price.
  During the Asian session (overnight), price stays far from the Frankfurt close.
  At 7:00 AM IDT (04:00 UTC), when the gap is large, price tends to drift back
  toward the previous Frankfurt close during the 7:00–9:30 IDT window (04:00–06:30 UTC),
  before the regular Frankfurt open (09:00 CEST = 07:00 UTC = 10:00 IDT).
  The move loses control around 9:30 IDT (06:30 UTC).
  Avoid being in the trade during 9:50–10:10 IDT (06:50–07:10 UTC = Frankfurt open).

DATA:
  GER40 5m from broker (IDT = UTC+3 server time), imported as symbol=GER40 timeframe=5m.
  Covers 22:05 UTC (01:05 IDT) onward — full overnight + Xetra session.

Key UTC times (summer, CEST = UTC+2, IDT = UTC+3):
  15:25 UTC → last 5m candle before Frankfurt close (open 15:25, closes 15:30)
  04:00 UTC → entry check (07:00 IDT)
  06:30 UTC → window end / exit (09:30 IDT)
  07:00 UTC → Frankfurt open (09:00 CEST = 10:00 IDT) — avoid until 07:10 UTC
"""

import sys, statistics
from pathlib import Path
from datetime import datetime, timezone, timedelta, date

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from db.local_db import LocalDB

DB_PATH = str(Path(__file__).resolve().parents[1] / "src" / "data" / "trade.db")
SYMBOL  = "GER40"
TF      = "5m"

# Frankfurt close: last candle OPEN at or before 15:25 UTC (candle closes at 15:30)
FKFT_CLOSE_H = 15
FKFT_CLOSE_M = 25

# Entry: first candle at 04:00 UTC (= 07:00 IDT)
ENTRY_H = 4
ENTRY_M = 0

# Window end: 06:30 UTC (= 09:30 IDT)
WINDOW_END_H = 6
WINDOW_END_M = 30

# Minimum gap in ATR units for a day to be "far from close"
REPORT_MIN_GAP_ATR = 0.3


def ts_utc(d: date, hour: int, minute: int) -> int:
    return int(datetime(d.year, d.month, d.day, hour, minute, tzinfo=timezone.utc).timestamp())


def median(lst):
    if not lst: return float("nan")
    s = sorted(lst); n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def mean(lst):
    return statistics.mean(lst) if lst else float("nan")


def pct_str(a, b):
    return f"{a / b * 100:.0f}%" if b else "n/a"


def simple_atr(candles: list, n: int = 14) -> float:
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
    all_5m = list(reversed(raw))   # chronological

    if not all_5m:
        print(f"No {SYMBOL} {TF} data found in {DB_PATH}")
        return

    first_dt = datetime.fromtimestamp(all_5m[0]["timestamp"], timezone.utc)
    last_dt  = datetime.fromtimestamp(all_5m[-1]["timestamp"], timezone.utc)
    print(f"{SYMBOL} {TF}: {len(all_5m)} candles  "
          f"{first_dt.strftime('%Y-%m-%d %H:%M UTC')} → {last_dt.strftime('%Y-%m-%d %H:%M UTC')}")

    # Group candles by UTC date
    by_date: dict = {}
    for c in all_5m:
        d = datetime.fromtimestamp(c["timestamp"], timezone.utc).date()
        by_date.setdefault(d, []).append(c)

    trading_dates = sorted(d for d in by_date if d.weekday() < 5)

    records = []

    for i, trade_date in enumerate(trading_dates):
        if i == 0:
            continue  # need previous day for Frankfurt close

        prev_date = trading_dates[i - 1]

        # ── Previous day Frankfurt close ──────────────────────────────────────
        close_cutoff = ts_utc(prev_date, FKFT_CLOSE_H, FKFT_CLOSE_M)
        prev_candles = [c for c in by_date.get(prev_date, [])
                        if c["timestamp"] <= close_cutoff]
        if not prev_candles:
            continue
        fkft_close_price = prev_candles[-1]["close"]

        # ── Entry candle at 04:00 UTC ─────────────────────────────────────────
        entry_ts     = ts_utc(trade_date, ENTRY_H, ENTRY_M)
        window_end   = ts_utc(trade_date, WINDOW_END_H, WINDOW_END_M)

        # Candles from 04:00 to 06:30 UTC (inclusive of entry candle)
        window_candles = [c for c in by_date.get(trade_date, [])
                          if entry_ts <= c["timestamp"] <= window_end]
        if not window_candles:
            continue

        entry_price = window_candles[0]["open"]

        # ── Gap metrics ───────────────────────────────────────────────────────
        gap_pts   = entry_price - fkft_close_price
        atr       = simple_atr(prev_candles, n=20)
        if atr < 1:
            continue
        gap_atr   = abs(gap_pts) / atr
        trade_dir = "bearish" if gap_pts > 0 else "bullish"

        # ── Gap fill analysis ─────────────────────────────────────────────────
        gap_filled      = False
        fill_ts         = None
        fill_min        = None
        max_retrace_pts = 0.0

        for c in window_candles:
            if trade_dir == "bearish":
                retrace = entry_price - c["low"]
                if c["low"] <= fkft_close_price and not gap_filled:
                    gap_filled = True
                    fill_ts    = c["timestamp"]
                    fill_min   = (fill_ts - entry_ts) / 60
            else:
                retrace = c["high"] - entry_price
                if c["high"] >= fkft_close_price and not gap_filled:
                    gap_filled = True
                    fill_ts    = c["timestamp"]
                    fill_min   = (fill_ts - entry_ts) / 60
            max_retrace_pts = max(max_retrace_pts, retrace)

        gap_fill_pct = max_retrace_pts / abs(gap_pts) if abs(gap_pts) > 0 else 0.0

        # ── Worst-case move against us (SL distance) ──────────────────────────
        if trade_dir == "bearish":
            worst = max(c["high"] for c in window_candles)
        else:
            worst = min(c["low"] for c in window_candles)
        sl_dist = abs(entry_price - worst)
        rr      = abs(gap_pts) / sl_dist if sl_dist > 0 else 0.0

        # ── Price progression every 30 min ───────────────────────────────────
        progress_30 = {}  # minutes_offset → retrace_pct at that point
        for offset_min in [30, 60, 90, 120, 150]:
            t_check = entry_ts + offset_min * 60
            reach_candles = [c for c in window_candles if c["timestamp"] <= t_check]
            if not reach_candles:
                continue
            if trade_dir == "bearish":
                best = min(c["low"] for c in reach_candles)
                moved = entry_price - best
            else:
                best = max(c["high"] for c in reach_candles)
                moved = best - entry_price
            progress_30[offset_min] = moved / abs(gap_pts) if abs(gap_pts) > 0 else 0

        records.append({
            "date":             trade_date,
            "fkft_close":       fkft_close_price,
            "entry_price":      entry_price,
            "gap_pts":          gap_pts,
            "gap_atr":          gap_atr,
            "trade_dir":        trade_dir,
            "gap_filled":       gap_filled,
            "fill_min":         fill_min,
            "max_retrace_pts":  max_retrace_pts,
            "gap_fill_pct":     gap_fill_pct,
            "atr":              atr,
            "rr":               rr,
            "sl_dist":          sl_dist,
            "n_window":         len(window_candles),
            "progress_30":      progress_30,
        })

    n = len(records)
    print(f"\n{'═' * 70}")
    print(f"Frankfurt-before-the-bell  ({n} trading days, Apr–Jun 2026)")
    print(f"Entry = open of 04:00 UTC candle (07:00 IDT)")
    print(f"Target = previous Frankfurt close (15:30 UTC = 18:30 IDT)")
    print(f"Window = 04:00–06:30 UTC (07:00–09:30 IDT)")
    print(f"{'═' * 70}")

    # ── 1. Gap size at 04:00 UTC ──────────────────────────────────────────────
    print(f"\n1.  GAP SIZE AT 04:00 UTC vs prev Frankfurt close")
    all_gaps = [r["gap_atr"] for r in records]
    print(f"    All {n} days: median {median(all_gaps):.2f}×ATR  mean {mean(all_gaps):.2f}×ATR")
    print()
    print(f"    {'Range':>14s}  {'N':>4s}  {'Fill rate':>9s}  {'Med retrace%':>12s}")
    for lo, hi in [(0, 0.2), (0.2, 0.3), (0.3, 0.5), (0.5, 0.75), (0.75, 1.0), (1.0, 2.0), (2.0, 9)]:
        g = [r for r in records if lo <= r["gap_atr"] < hi]
        if not g:
            continue
        nf  = sum(1 for r in g if r["gap_filled"])
        mr  = median([r["gap_fill_pct"] * 100 for r in g])
        lbl = f"{lo:.2f}–{hi:.2f}×ATR" if hi < 9 else f"≥{lo:.2f}×ATR"
        print(f"    {lbl:>14s}  {len(g):>4d}  {pct_str(nf, len(g)):>9s}  {mr:>11.0f}%")

    # ── 2. Full-gap fill rate by threshold ────────────────────────────────────
    print(f"\n2.  FULL-GAP FILL RATE vs MINIMUM GAP THRESHOLD")
    print(f"    {'Min gap':>8s}  {'N':>4s}  {'Filled':>6s}  {'Fill%':>6s}  "
          f"{'Med retrace%':>12s}  {'Med fill min':>12s}")
    for thr in [0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0]:
        g = [r for r in records if r["gap_atr"] >= thr]
        if not g:
            continue
        nf  = sum(1 for r in g if r["gap_filled"])
        mr  = median([r["gap_fill_pct"] * 100 for r in g])
        ft  = median([r["fill_min"] for r in g if r["fill_min"] is not None])
        print(f"    {thr:>7.2f}x  {len(g):>4d}  {nf:>6d}  {pct_str(nf, len(g)):>6s}  "
              f"{mr:>11.0f}%  {ft:>11.0f}m")

    # ── 3. Partial fill / retrace depth (≥ threshold days) ───────────────────
    far = [r for r in records if r["gap_atr"] >= REPORT_MIN_GAP_ATR]
    print(f"\n3.  PARTIAL FILL — how far toward Frankfurt close?")
    print(f"    (gap ≥ {REPORT_MIN_GAP_ATR}×ATR, n={len(far)} days)")
    for pct_t in [25, 50, 75, 90, 100]:
        nh = sum(1 for r in far if r["gap_fill_pct"] * 100 >= pct_t)
        print(f"    ≥{pct_t:3d}% of gap:  {nh}/{len(far)}  ({pct_str(nh, len(far))})")

    # ── 4. Progress timeline (how much fills at each 30-min checkpoint) ───────
    print(f"\n4.  PROGRESS TIMELINE — median retrace % reached by each checkpoint")
    print(f"    (far days, n={len(far)})")
    print(f"    {'Checkpoint':>15s}  {'IDT time':>10s}  {'Med retrace%':>12s}  {'≥50% days':>10s}")
    for offset in [30, 60, 90, 120, 150]:
        vals = [r["progress_30"][offset] * 100 for r in far if offset in r["progress_30"]]
        if not vals:
            continue
        utc_h = 4 + offset // 60; utc_m = offset % 60
        idt_h = 7 + offset // 60; idt_m = offset % 60
        n50  = sum(1 for v in vals if v >= 50)
        print(f"    +{offset:>3d}min ({utc_h:02d}:{utc_m:02d} UTC)  "
              f"({idt_h:02d}:{idt_m:02d} IDT)  "
              f"{median(vals):>11.0f}%  {n50}/{len(vals)} ({pct_str(n50, len(vals))})")

    # ── 5. Fill timing ────────────────────────────────────────────────────────
    filled_far = [r for r in far if r["gap_filled"]]
    print(f"\n5.  FILL TIMING (far days that fully fill, n={len(filled_far)})")
    if filled_far:
        fmins = [r["fill_min"] for r in filled_far]
        print(f"    Median fill time: {median(fmins):.0f} min after entry  "
              f"→ ~{int(4 + median(fmins) // 60):02d}:{int(median(fmins) % 60):02d} UTC  "
              f"/ ~{int(7 + median(fmins) // 60):02d}:{int(median(fmins) % 60):02d} IDT")
        print()
        for lo, hi in [(0, 30), (30, 60), (60, 90), (90, 120), (120, 150), (150, 160)]:
            nt  = sum(1 for m in fmins if lo <= m < hi)
            utc_lo_h, utc_lo_m = divmod(4 * 60 + lo, 60)
            utc_hi_h, utc_hi_m = divmod(4 * 60 + hi, 60)
            idt_lo_h, idt_lo_m = utc_lo_h + 3, utc_lo_m
            idt_hi_h, idt_hi_m = utc_hi_h + 3, utc_hi_m
            print(f"    {utc_lo_h:02d}:{utc_lo_m:02d}–{utc_hi_h:02d}:{utc_hi_m:02d} UTC  "
                  f"({idt_lo_h:02d}:{idt_lo_m:02d}–{idt_hi_h:02d}:{idt_hi_m:02d} IDT):  "
                  f"{nt}/{len(fmins)}  ({pct_str(nt, len(fmins))})")

    # ── 6. Direction split ────────────────────────────────────────────────────
    print(f"\n6.  DIRECTION SPLIT  (gap ≥ {REPORT_MIN_GAP_ATR}×ATR)")
    for td, label in [
        ("bullish", "Price at 07:00 IDT BELOW Frankfurt close → buy toward close"),
        ("bearish", "Price at 07:00 IDT ABOVE Frankfurt close → sell toward close"),
    ]:
        g = [r for r in far if r["trade_dir"] == td]
        if not g:
            continue
        nf  = sum(1 for r in g if r["gap_filled"])
        mr  = median([r["gap_fill_pct"] * 100 for r in g])
        print(f"    {label}")
        print(f"      n={len(g)}  filled={pct_str(nf, len(g))}  median retrace={mr:.0f}%")

    # ── 7. Day-of-week ────────────────────────────────────────────────────────
    print(f"\n7.  DAY-OF-WEEK  (gap ≥ {REPORT_MIN_GAP_ATR}×ATR)")
    for dow, nm in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri"]):
        g = [r for r in far if r["date"].weekday() == dow]
        if not g:
            continue
        nf = sum(1 for r in g if r["gap_filled"])
        mr = median([r["gap_fill_pct"] * 100 for r in g])
        print(f"    {nm}: n={len(g):>2}  filled={pct_str(nf, len(g)):>4}  "
              f"median retrace={mr:.0f}%")

    # ── 8. Monday gate: skip Mondays? ─────────────────────────────────────────
    non_mon = [r for r in far if r["date"].weekday() != 0]
    print(f"\n8.  MONDAY GATE — skip Mondays (weekend gaps often huge / don't fill)")
    print(f"    All far days n={len(far)}  fill={pct_str(sum(1 for r in far if r['gap_filled']), len(far))}")
    print(f"    Non-Monday   n={len(non_mon)}  fill={pct_str(sum(1 for r in non_mon if r['gap_filled']), len(non_mon))}")

    # ── 9. Per-day detail ─────────────────────────────────────────────────────
    print(f"\n9.  PER-DAY DETAIL  (gap ≥ {REPORT_MIN_GAP_ATR}×ATR)")
    print(f"    {'Date':12s}  {'DoW':3s}  {'Dir':4s}  {'Gap':>6s}  {'GapATR':>6s}  "
          f"{'Retrace%':>8s}  {'FillMin':>7s}  {'Filled':>6s}  {'RR':>4s}")
    for r in records:
        if r["gap_atr"] < REPORT_MIN_GAP_ATR:
            continue
        dow_str    = ["Mon","Tue","Wed","Thu","Fri"][r["date"].weekday()]
        fill_str   = f"{r['fill_min']:6.0f}" if r["fill_min"] is not None else "    --"
        filled_str = "YES" if r["gap_filled"] else "no"
        print(f"    {str(r['date']):12s}  {dow_str}  {r['trade_dir'][:4]:4s}  "
              f"{r['gap_pts']:+6.0f}  {r['gap_atr']:6.2f}x  "
              f"{r['gap_fill_pct'] * 100:8.0f}%  {fill_str:>7s}  "
              f"{filled_str:>6s}  {r['rr']:4.1f}")

    db.close()


if __name__ == "__main__":
    main()
