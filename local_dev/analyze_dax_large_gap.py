#!/usr/bin/env python3
"""
analyze_dax_large_gap.py

For days where the overnight gap (at 04:00 UTC / 07:00 IDT) is LARGE (> 2×ATR),
analyze what happens during the Frankfurt session (07:00–12:30 UTC = 10:00–15:30 IDT).

Questions:
  1. Does Frankfurt session continue toward the gap fill?
  2. How much of the gap gets filled during Frankfurt hours?
  3. Does large gap direction predict Frankfurt session direction?
  4. Correlation with DAX SERPE (counter-trend setup that aligns with gap fill).
"""

import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta, date

DB_PATH = str(Path(__file__).resolve().parents[1] / "src" / "data" / "trade.db")
SYMBOL  = "GER40"
TF      = "5m"


def ts_utc(d: date, h: int, m: int) -> int:
    return int(datetime(d.year, d.month, d.day, h, m, tzinfo=timezone.utc).timestamp())

def pct(n, d):
    return f"{n/d*100:.0f}%" if d else "n/a"

def median(lst):
    if not lst: return 0
    s = sorted(lst)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n//2 - 1] + s[n//2]) / 2

def load_candles():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT timestamp, open, high, low, close FROM fx_candles "
        "WHERE symbol=? AND timeframe=? ORDER BY timestamp",
        (SYMBOL, TF)
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def simple_atr(candles, n=20):
    tail = candles[-n:] if len(candles) >= n else candles
    if len(tail) < 2: return 0.0
    trs = []
    for i in range(1, len(tail)):
        c, p = tail[i], tail[i-1]
        trs.append(max(c["high"]-c["low"], abs(c["high"]-p["close"]), abs(c["low"]-p["close"])))
    return sum(trs) / len(trs)

def prev_trading_day(d):
    d -= timedelta(days=1)
    while d.weekday() >= 5: d -= timedelta(days=1)
    return d


def main():
    all5m = load_candles()

    by_date: dict[date, list] = {}
    for c in all5m:
        d = datetime.fromtimestamp(c["timestamp"], timezone.utc).date()
        by_date.setdefault(d, []).append(c)

    trading_days = sorted(d for d in by_date if d.weekday() < 5 and len(by_date[d]) >= 20)
    records = []

    for tday in trading_days:
        prev = prev_trading_day(tday)
        if prev not in by_date: continue

        # Previous day Xetra close + ATR
        prev_xetra = [c for c in by_date[prev]
                      if ts_utc(prev,7,0) <= c["timestamp"] <= ts_utc(prev,15,25)]
        if len(prev_xetra) < 20: continue
        fkft_close = prev_xetra[-1]["close"]
        atr        = simple_atr(prev_xetra, n=20)
        if atr < 1: continue

        # Price at 04:00 UTC (07:00 IDT)
        entry_c = [c for c in by_date[tday] if c["timestamp"] >= ts_utc(tday,4,0)]
        if not entry_c: continue
        entry_price = entry_c[0]["open"]

        gap_atr = abs(entry_price - fkft_close) / atr
        if gap_atr < 2.0: continue

        bullish = entry_price < fkft_close  # price below close → expects UP

        # Frankfurt session candles 07:05–12:30 UTC (10:05–15:30 IDT)
        fk_c = [c for c in by_date[tday]
                if ts_utc(tday,7,5) <= c["timestamp"] <= ts_utc(tday,12,30)]
        if len(fk_c) < 5: continue

        fk_open = fk_c[0]["open"]
        fk_high = max(c["high"] for c in fk_c)
        fk_low  = min(c["low"]  for c in fk_c)
        fk_close_bar = fk_c[-1]["close"]

        # How much of the gap did Frankfurt cover?
        gap_total = abs(fkft_close - entry_price)
        if bullish:
            fk_fill_pct   = min((fk_high - entry_price) / gap_total, 2.0)
            gap_filled_fk = fk_high >= fkft_close
            partial_50    = fk_high >= entry_price + gap_total * 0.5
            partial_25    = fk_high >= entry_price + gap_total * 0.25
            fk_toward     = fk_high - fk_open   # how far UP
            fk_away       = fk_open - fk_low    # how far DOWN (wrong way)
            net_move      = fk_close_bar - fk_open   # positive = moved UP = toward fill
        else:
            fk_fill_pct   = min((entry_price - fk_low) / gap_total, 2.0)
            gap_filled_fk = fk_low <= fkft_close
            partial_50    = fk_low <= entry_price - gap_total * 0.5
            partial_25    = fk_low <= entry_price - gap_total * 0.25
            fk_toward     = fk_open - fk_low    # how far DOWN
            fk_away       = fk_high - fk_open   # how far UP (wrong way)
            net_move      = fk_open - fk_close_bar   # positive = moved DOWN = toward fill

        fk_bias_toward = fk_toward > fk_away

        records.append({
            "date": tday, "fkft_close": fkft_close, "entry_price": entry_price,
            "gap_atr": gap_atr, "atr": atr, "bullish": bullish,
            "fk_high": fk_high, "fk_low": fk_low, "fk_open": fk_open,
            "fk_toward_atr": fk_toward / atr, "fk_away_atr": fk_away / atr,
            "net_move_atr":  net_move  / atr,
            "fk_bias_toward": fk_bias_toward,
            "fk_fill_pct": fk_fill_pct, "gap_filled_fk": gap_filled_fk,
            "partial_50": partial_50, "partial_25": partial_25,
        })

    n = len(records)
    bull = [r for r in records if r["bullish"]]
    bear = [r for r in records if not r["bullish"]]
    toward = [r for r in records if r["fk_bias_toward"]]

    print(f"\n{'═'*68}")
    print(f"DAX LARGE GAP (>2×ATR) — FRANKFURT SESSION ANALYSIS")
    print(f"Period: {records[0]['date']} → {records[-1]['date']}")
    print(f"{'═'*68}")

    # 1. Overview
    print(f"\n1.  OVERVIEW  (n={n} large-gap days)")
    print(f"    Bullish gap (price below close → expects UP): {len(bull)}  ({pct(len(bull),n)})")
    print(f"    Bearish gap (price above close → expects DOWN): {len(bear)}  ({pct(len(bear),n)})")
    print(f"    Median gap: {median([r['gap_atr'] for r in records]):.2f}×ATR  "
          f"Max: {max(r['gap_atr'] for r in records):.2f}×ATR")
    print(f"\n    Distribution:")
    for lo, hi in [(2,3),(3,4),(4,6),(6,99)]:
        g = [r for r in records if lo <= r["gap_atr"] < hi]
        lbl = f"{lo}–{hi}" if hi<99 else f"≥{lo}"
        print(f"      {lbl}×ATR: {len(g):>3d} days  ({pct(len(g),n)})")

    # 2. Frankfurt session direction
    print(f"\n2.  FRANKFURT SESSION — DOES IT MOVE TOWARD THE GAP FILL?")
    print(f"    Session high > session open (for bulls) / low < open (for bears)")
    print(f"    Toward gap fill: {len(toward)}/{n} = {pct(len(toward),n)}")
    for grp, lbl in [(bull,"Bullish gap"),(bear,"Bearish gap")]:
        t = [r for r in grp if r["fk_bias_toward"]]
        print(f"      {lbl}: {pct(len(t),len(grp))} ({len(t)}/{len(grp)})")

    # Net move
    print(f"\n    Net session move (open→close, positive = toward gap fill):")
    print(f"    Median net move: {median([r['net_move_atr'] for r in records]):+.2f}×ATR")
    pos_net = [r for r in records if r["net_move_atr"] > 0]
    print(f"    Closed in fill direction: {pct(len(pos_net),n)} ({len(pos_net)}/{n})")

    # 3. Fill rates during Frankfurt
    print(f"\n3.  GAP FILL DURING FRANKFURT SESSION (10:00–15:30 IDT)")
    full = [r for r in records if r["gap_filled_fk"]]
    p50  = [r for r in records if r["partial_50"]]
    p25  = [r for r in records if r["partial_25"]]
    print(f"    25%+ of gap filled: {pct(len(p25),n):>4s}  ({len(p25)}/{n})")
    print(f"    50%+ of gap filled: {pct(len(p50),n):>4s}  ({len(p50)}/{n})")
    print(f"    Full fill (100%):   {pct(len(full),n):>4s}  ({len(full)}/{n})")
    print(f"    Median fill: {median([r['fk_fill_pct']*100 for r in records]):.0f}% of gap")

    print(f"\n    By direction:")
    for grp, lbl in [(bull,"Bullish"),(bear,"Bearish")]:
        gf = [r for r in grp if r["gap_filled_fk"]]
        gp = [r for r in grp if r["partial_50"]]
        med = median([r["fk_fill_pct"]*100 for r in grp])
        print(f"      {lbl}: 50%={pct(len(gp),len(grp))}  full={pct(len(gf),len(grp))}  med={med:.0f}%")

    print(f"\n    By gap size:")
    for lo, hi in [(2,3),(3,4),(4,6),(6,99)]:
        g = [r for r in records if lo <= r["gap_atr"] < hi]
        if not g: continue
        gf  = [r for r in g if r["gap_filled_fk"]]
        gp5 = [r for r in g if r["partial_50"]]
        med = median([r["fk_fill_pct"]*100 for r in g])
        lbl = f"{lo}–{hi}" if hi<99 else f"≥{lo}"
        print(f"      {lbl}×ATR: n={len(g):>2}  50%={pct(len(gp5),len(g)):>4s}  "
              f"full={pct(len(gf),len(g)):>4s}  med fill={med:.0f}%")

    # 4. Frankfurt range size
    print(f"\n4.  HOW FAR DID FRANKFURT MOVE IN EACH DIRECTION?")
    print(f"    Median move TOWARD gap fill: {median([r['fk_toward_atr'] for r in records]):.2f}×ATR")
    print(f"    Median move AWAY from fill:  {median([r['fk_away_atr']   for r in records]):.2f}×ATR")
    for grp, lbl in [(bull,"Bullish"),(bear,"Bearish")]:
        mt = median([r["fk_toward_atr"] for r in grp])
        ma = median([r["fk_away_atr"]   for r in grp])
        print(f"      {lbl}: toward={mt:.2f}×  away={ma:.2f}×ATR")

    # 5. Pre-Frankfurt narrowing (07:00–10:00 IDT)
    print(f"\n5.  PRE-FRANKFURT WINDOW (07:00–10:00 IDT) — DID GAP NARROW BEFORE OPEN?")
    narrowed = [r for r in records
                if (r["bullish"]  and r["fk_open"] > r["entry_price"])
                or (not r["bullish"] and r["fk_open"] < r["entry_price"])]
    print(f"    Gap narrowed before Frankfurt open: {pct(len(narrowed),n)} ({len(narrowed)}/{n})")
    move_pct = [(abs(r["fk_open"] - r["entry_price"]) / abs(r["fkft_close"] - r["entry_price"]) * 100)
                for r in records]
    print(f"    Median pre-Frankfurt fill: {median(move_pct):.0f}% of gap closed by 10:00 IDT")

    # 6. Day of week
    print(f"\n6.  BY DAY OF WEEK")
    for dow, nm in enumerate(["Mon","Tue","Wed","Thu","Fri"]):
        g = [r for r in records if r["date"].weekday() == dow]
        if not g: continue
        t  = [r for r in g if r["fk_bias_toward"]]
        ff = [r for r in g if r["gap_filled_fk"]]
        print(f"    {nm}: n={len(g):>2}  toward={pct(len(t),len(g)):>4s}  full fill={pct(len(ff),len(g)):>4s}")

    # 7. SERPE implication
    print(f"\n7.  IMPLICATION FOR DAX SERPE")
    print(f"    SERPE trades counter-trend (fades expansion) during 10:00–15:30 IDT.")
    print(f"    Large overnight gap = extended move in one direction overnight.")
    print(f"    If SERPE fires IN SAME DIRECTION as gap fill:")
    print(f"      → Both forces push the same way (gap magnetic + SERPE counter-trend)")
    print(f"      → Frankfurt moves toward close: {pct(len(toward),n)} of large-gap days")
    print(f"    ")
    print(f"    Suggested filter: when gap > 2×ATR exists at 07:00 IDT,")
    print(f"      prefer SERPE entries that ALIGN with gap fill direction.")
    print(f"      Skip or reduce size on SERPE entries that OPPOSE gap fill.")

    # 8. Per-day detail
    print(f"\n8.  PER-DAY DETAIL")
    print(f"    {'Date':12s}  {'DoW':3s}  {'Dir':4s}  {'GapATR':>6s}  "
          f"{'FkFill%':>7s}  {'Toward':>6s}  {'FullFill':>8s}")
    for r in records:
        dow  = ["Mon","Tue","Wed","Thu","Fri"][r["date"].weekday()]
        twd  = "YES" if r["fk_bias_toward"] else "no"
        fulf = "YES" if r["gap_filled_fk"]  else "no"
        print(f"    {str(r['date']):12s}  {dow}  {'bull' if r['bullish'] else 'bear':4s}  "
              f"{r['gap_atr']:6.2f}x  {r['fk_fill_pct']*100:6.0f}%  {twd:>6s}  {fulf:>8s}")


if __name__ == "__main__":
    main()
