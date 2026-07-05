#!/usr/bin/env python3
"""
DAX Continuation v3 — Pullback entry optimization.

User insight: after a sweep, ~80-90% of cases reach the morning level.
Price typically retraces 10-20% of range before continuing. Rather than
entering at the sweep close (sloppy) or waiting for breakout confirmation
(late), place a LIMIT ORDER at the expected pullback level.

Sequence:
  1. Morning range 7:00–11:00 UTC
  2. Sweep candle 11:00–13:30 UTC (wick ≥ min_wick beyond level, close inside)
  3. Place limit at: morning_high - entry_pct × range  (for LONG)
     Wait for price to touch this level
  4. SL = entry_level - sl_pts  (tight fixed pts below entry)
  5. TP = various structural targets
  6. Skip if price hits TP before touching entry_level (missed pullback)
  7. Evaluate until 15:30 UTC

TP targets tested:
  "level"    — morning_high itself (close target, high WR expected)
  "rng_0.1"  — morning_high + 10% of range (small extension)
  "rng_0.2"  — morning_high + 20% of range
  "rng_0.3"  — morning_high + 30% of range
  "pdh"      — previous day high (structural)

Parameters:
  min_wick   [5, 7, 10]
  entry_pct  [0.10, 0.15, 0.20, 0.25, 0.30]  (pullback depth from morning level)
  sl_pts     [5, 10, 15, 20]
  tp_mode    5 options above
"""

import sys, statistics
from pathlib import Path
from datetime import datetime, timezone, date
from collections import defaultdict
from itertools import product

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from db.local_db import LocalDB

DB_PATH = str(Path(__file__).resolve().parents[1] / "src" / "data" / "trade.db")
_UTC = timezone.utc

MORNING_H,   MORNING_M   =  7,  0
RANGE_END_H, RANGE_END_M = 11,  0
SWEEP_END_H, SWEEP_END_M = 13, 30
CUTOFF_H,    CUTOFF_M    = 15, 30

MIN_MORNING_BARS = 6
MIN_RANGE_ATR    = 0.5
GOOD_WINDOWS     = [(660, 690), (720, 750)]   # 11:00–11:30, 12:00–12:30 UTC

MIN_WICK_LIST  = [5, 7, 10]
ENTRY_PCT_LIST = [0.10, 0.15, 0.20, 0.25, 0.30]
SL_PTS_LIST    = [5, 10, 15, 20]
TP_MODES       = ["level", "rng_0.1", "rng_0.2", "rng_0.3", "pdh"]


def _ts(d: date, h: int, m: int) -> int:
    return int(datetime(d.year, d.month, d.day, h, m, tzinfo=_UTC).timestamp())

def _atr(bars, period=14):
    if len(bars) < 2: return 0.0
    trs = [max(b["high"] - b["low"],
               abs(b["high"] - bars[i-1]["close"]),
               abs(b["low"]  - bars[i-1]["close"]))
           for i, b in enumerate(bars[1:], 1)]
    return statistics.mean(trs[-period:]) if trs else 0.0


# ── Load data ─────────────────────────────────────────────────────────────────

print("Loading GER40 5m data …")
db = LocalDB(DB_PATH)
all5m = list(reversed(db.query_recent("GER40", "5m", limit=130_000)))
print(f"  {len(all5m)} bars")

dates = sorted(set(datetime.fromtimestamp(c["timestamp"], tz=_UTC).date() for c in all5m))
dates = [d for d in dates if d.weekday() < 5]

# Previous-day high/low
prev_hl = {}
for i, d in enumerate(dates):
    if i == 0: continue
    pd = dates[i-1]
    pd_bars = [b for b in all5m if _ts(pd,0,0) <= b["timestamp"] <= _ts(pd,23,59)]
    if pd_bars:
        prev_hl[d] = (max(b["high"] for b in pd_bars), min(b["low"] for b in pd_bars))

# ── Build sweep records ───────────────────────────────────────────────────────

print("Building sweep records …")
sweeps = []
for d in dates:
    ts_ms = _ts(d, MORNING_H,   MORNING_M)
    ts_re = _ts(d, RANGE_END_H, RANGE_END_M)
    ts_se = _ts(d, SWEEP_END_H, SWEEP_END_M)
    ts_co = _ts(d, CUTOFF_H,    CUTOFF_M)

    morning = [c for c in all5m if ts_ms <= c["timestamp"] < ts_re]
    sweepw  = [c for c in all5m if ts_re  <= c["timestamp"] < ts_se]
    day5m   = [c for c in all5m if ts_ms  <= c["timestamp"] <= ts_co]

    if len(morning) < MIN_MORNING_BARS: continue
    mh  = max(c["high"]  for c in morning)
    ml  = min(c["low"]   for c in morning)
    rng = mh - ml
    atr = _atr(morning) or 20.0
    if rng < MIN_RANGE_ATR * atr: continue

    for bar in sweepw:
        is_high = bar["high"] > mh and bar["close"] <= mh
        is_low  = bar["low"]  < ml and bar["close"] >= ml
        if is_high or is_low:
            is_long  = is_high
            wick_ext = (bar["high"] - mh) if is_long else (ml - bar["low"])
            level    = mh if is_long else ml
            sweep_dt = datetime.fromtimestamp(bar["timestamp"], tz=_UTC)
            pdh, pdl = prev_hl.get(d, (None, None))
            post     = [b for b in day5m if b["timestamp"] > bar["timestamp"]]
            sweeps.append({
                "date": d, "is_long": is_long, "level": level,
                "mh": mh, "ml": ml, "range": rng, "atr": atr,
                "wick_ext": wick_ext,
                "sweep_min": sweep_dt.hour*60 + sweep_dt.minute,
                "sweep_hhmm": sweep_dt.strftime("%H:%M"),
                "pdh": pdh, "pdl": pdl, "post": post,
            })
            break

print(f"  {len(sweeps)} sweeps\n")


# ── Core evaluator ────────────────────────────────────────────────────────────

def run_trade(s, entry_pct, sl_pts, tp_mode):
    is_long = s["is_long"]
    mh, ml  = s["mh"], s["ml"]
    rng, atr = s["range"], s["atr"]
    level   = s["level"]

    # Pullback entry level
    el = (level - entry_pct * rng) if is_long else (level + entry_pct * rng)
    sl = el - sl_pts if is_long else el + sl_pts

    # TP
    if tp_mode == "level":
        tp = level
    elif tp_mode.startswith("rng_"):
        ext = float(tp_mode[4:])
        tp = (level + ext * rng) if is_long else (level - ext * rng)
    elif tp_mode == "pdh":
        tp = s["pdh"] if is_long else s["pdl"]
        if tp is None: return None

    if is_long  and tp <= el: return None   # TP must be above entry
    if not is_long and tp >= el: return None

    risk   = abs(el - sl)
    reward = abs(el - tp)
    if risk < 0.5: return None
    rr = reward / risk

    entry_filled = False
    for bar in s["post"]:
        if not entry_filled:
            # Skip if TP hit before pullback touches entry level
            if is_long  and bar["high"] >= tp: return None   # missed — went straight up
            if not is_long and bar["low"]  <= tp: return None
            # Pullback touches entry level?
            if is_long  and bar["low"]  <= el: entry_filled = True
            if not is_long and bar["high"] >= el: entry_filled = True
            if not entry_filled: continue

        # Evaluate from fill
        if is_long:
            if bar["low"]  <= sl: return {"oc":"LOSS","r":-1.0,"rr":rr,"risk":risk,"rwd":reward,"el":el,"sl":sl,"tp":tp}
            if bar["high"] >= tp: return {"oc":"WIN", "r": rr, "rr":rr,"risk":risk,"rwd":reward,"el":el,"sl":sl,"tp":tp}
        else:
            if bar["high"] >= sl: return {"oc":"LOSS","r":-1.0,"rr":rr,"risk":risk,"rwd":reward,"el":el,"sl":sl,"tp":tp}
            if bar["low"]  <= tp: return {"oc":"WIN", "r": rr, "rr":rr,"risk":risk,"rwd":reward,"el":el,"sl":sl,"tp":tp}

    if entry_filled:
        return {"oc":"OPEN","r":None,"rr":rr,"risk":risk,"rwd":reward,"el":el,"sl":sl,"tp":tp}
    return None   # pullback never reached


def summarise(results, min_n=10):
    closed = [r for r in results if r["oc"] != "OPEN"]
    if len(closed) < min_n: return None
    wins = [r for r in closed if r["oc"] == "WIN"]
    wr   = len(wins) / len(closed)
    avg_rr = statistics.mean(r["rr"] for r in wins) if wins else 0
    ev   = wr * avg_rr - (1 - wr) * 1.0
    avg_pts = (wr * statistics.mean(r["rwd"] for r in wins) -
               (1-wr) * statistics.mean(r["risk"] for r in closed if r["oc"]=="LOSS")) if wins else -1
    return {"n_cl": len(closed), "n_op": len(results)-len(closed),
            "wr": wr, "avg_rr": avg_rr, "ev": ev, "avg_pts": avg_pts,
            "n_sig": len(results)}


# ── Full grid — no time filter ────────────────────────────────────────────────

print("Running full grid (no time filter) …")
grid_all = []
for wick, ep, sl_p, tp_m in product(MIN_WICK_LIST, ENTRY_PCT_LIST, SL_PTS_LIST, TP_MODES):
    rs = []
    for s in sweeps:
        if s["wick_ext"] < wick: continue
        r = run_trade(s, ep, sl_p, tp_m)
        if r: rs.append(r)
    sm = summarise(rs)
    if sm: grid_all.append({"wick":wick,"ep":ep,"sl":sl_p,"tp":tp_m,**sm})
grid_all.sort(key=lambda x: x["ev"], reverse=True)

# ── Time-filtered grid ────────────────────────────────────────────────────────

print("Running time-filtered grid …")
grid_flt = []
for wick, ep, sl_p, tp_m in product(MIN_WICK_LIST, ENTRY_PCT_LIST, SL_PTS_LIST, TP_MODES):
    rs = []
    for s in sweeps:
        if s["wick_ext"] < wick: continue
        if not any(t0 <= s["sweep_min"] < t1 for t0,t1 in GOOD_WINDOWS): continue
        r = run_trade(s, ep, sl_p, tp_m)
        if r: rs.append(r)
    sm = summarise(rs, min_n=8)
    if sm: grid_flt.append({"wick":wick,"ep":ep,"sl":sl_p,"tp":tp_m,**sm})
grid_flt.sort(key=lambda x: x["ev"], reverse=True)
print(f"Done.\n")


# ── Results: no filter ────────────────────────────────────────────────────────

def print_grid(title, rows, top=25):
    print("=" * 84)
    print(title)
    print("=" * 84)
    print(f"\n  {'wick':>4}  {'pb%':>5}  {'SL':>4}  {'TP':>9}  {'N':>4}  "
          f"{'WR':>5}  {'avgR:R':>7}  {'EV':>7}  {'avg_pts':>8}  {'Open':>5}")
    print("  " + "─" * 72)
    for r in rows[:top]:
        print(f"  {r['wick']:>4}  {r['ep']:>5.0%}  {r['sl']:>4}  {r['tp']:>9}  "
              f"{r['n_cl']:>4}  {r['wr']:>5.0%}  {r['avg_rr']:>7.2f}R  "
              f"{r['ev']:>+7.2f}R  {r['avg_pts']:>+8.1f}pt  {r['n_op']:>5}")
    if rows:
        b = rows[0]
        print(f"\n  Best: wick≥{b['wick']}  pullback={b['ep']:.0%}  SL={b['sl']}pts  "
              f"TP={b['tp']}  →  EV={b['ev']:+.2f}R  WR={b['wr']:.0%}  N={b['n_cl']}")

print_grid("NO TIME FILTER — top 25 by EV", grid_all)
print()
print_grid("TIME FILTERED (11:00–11:30 + 12:00–12:30) — top 25 by EV", grid_flt)


# ── Pullback reach rate: how many sweeps touch each pullback depth ─────────────

print(f"\n\n{'='*82}")
print("PULLBACK REACH RATE  (wick≥7, no SL/TP — just: does price touch this level?)")
print(f"{'='*82}")
print(f"\n  {'Depth':>7}  {'Total':>6}  {'Reached':>8}  {'Reach%':>7}  │ then: {'WR→level':>9}  {'WR→rng.1':>9}  EV→level  EV→rng.1")
print("  " + "─" * 76)
total_sweeps = sum(1 for s in sweeps if s["wick_ext"] >= 7)
for ep in [0.05, 0.10, 0.15, 0.20, 0.25, 0.30]:
    reached = 0
    for s in sweeps:
        if s["wick_ext"] < 7: continue
        el = (s["level"] - ep * s["range"]) if s["is_long"] else (s["level"] + ep * s["range"])
        touched = any(
            (b["low"] <= el if s["is_long"] else b["high"] >= el)
            for b in s["post"]
        )
        if touched: reached += 1
    reach_pct = reached / total_sweeps if total_sweeps else 0

    # WR at level and rng_0.1 for those that reached this depth (SL=10pts)
    for_level, for_rng = [], []
    for s in sweeps:
        if s["wick_ext"] < 7: continue
        r_lv = run_trade(s, ep, 10, "level")
        r_r1 = run_trade(s, ep, 10, "rng_0.1")
        if r_lv: for_level.append(r_lv)
        if r_r1: for_rng.append(r_r1)
    def quick_ev(rs):
        cl = [r for r in rs if r["oc"]!="OPEN"]
        if not cl: return "—"
        wins = [r for r in cl if r["oc"]=="WIN"]
        wr = len(wins)/len(cl)
        avg_rr = statistics.mean(r["rr"] for r in wins) if wins else 0
        ev = wr*avg_rr-(1-wr)*1.0
        return f"{wr:.0%}/{ev:+.2f}R"
    print(f"  {ep:>7.0%}  {total_sweeps:>6}  {reached:>8}  {reach_pct:>7.0%}  │         "
          f"{quick_ev(for_level):>12}  {quick_ev(for_rng):>12}")


# ── TP comparison at best entry_pct ──────────────────────────────────────────

best_ep = grid_all[0]["ep"] if grid_all else 0.20


# ── TP comparison at best entry_pct ──────────────────────────────────────────

best_ep = grid_all[0]["ep"] if grid_all else 0.20
print(f"\n\n{'='*68}")
print(f"TP COMPARISON  (wick≥7, SL=10pts, pullback={best_ep:.0%}, no time filter)")
print(f"{'='*68}")
print(f"\n  {'TP mode':>9}  {'N':>4}  {'WR':>5}  {'avgR:R':>7}  {'EV':>7}  {'avg_pts':>8}")
print("  " + "─" * 52)
for tp_m in TP_MODES:
    rs = []
    for s in sweeps:
        if s["wick_ext"] < 7: continue
        r = run_trade(s, best_ep, 10, tp_m)
        if r: rs.append(r)
    cl = [r for r in rs if r["oc"]!="OPEN"]
    if not cl: continue
    wins = [r for r in cl if r["oc"]=="WIN"]
    wr = len(wins)/len(cl)
    avg_rr = statistics.mean(r["rr"] for r in wins) if wins else 0
    ev = wr*avg_rr-(1-wr)*1.0
    avg_pts = (wr*statistics.mean(r["rwd"] for r in wins) -
               (1-wr)*statistics.mean(r["risk"] for r in cl if r["oc"]=="LOSS")) if wins else -1
    print(f"  {tp_m:>9}  {len(cl):>4}  {wr:>5.0%}  {avg_rr:>7.2f}R  {ev:>+7.2f}R  {avg_pts:>+8.1f}pt")


# ── Per-trade: best time-filtered combo ──────────────────────────────────────

fb = grid_flt[0] if grid_flt else grid_all[0]
print(f"\n\n{'─'*108}")
print(f"PER-TRADE — {'filtered' if grid_flt else 'unfiltered'} best: "
      f"wick≥{fb['wick']}  pullback={fb['ep']:.0%}  SL={fb['sl']}pts  TP={fb['tp']}")
print(f"{'─'*108}")
print(f"  {'#':>3}  {'Date':>10}  {'Dir':>5}  {'SweepT':>6}  {'Wick':>5}  "
      f"{'Level':>7}  {'Entry':>7}  {'TP':>7}  {'SL':>7}  "
      f"{'Risk':>5}  {'R:R':>5}  {'Oc':>5}  {'R':>7}")
print(f"  {'─'*105}")

detail = []
for s in sweeps:
    if s["wick_ext"] < fb["wick"]: continue
    if grid_flt and not any(t0<=s["sweep_min"]<t1 for t0,t1 in GOOD_WINDOWS): continue
    r = run_trade(s, fb["ep"], fb["sl"], fb["tp"])
    if r is None: continue
    detail.append({**r, "date":s["date"], "dir":"LONG" if s["is_long"] else "SHORT",
                   "sweep_t":s["sweep_hhmm"], "wick":s["wick_ext"], "level":s["level"]})

closed = [d for d in detail if d["oc"]!="OPEN"]
wins   = [d for d in closed if d["oc"]=="WIN"]
cum = 0.0
for i, d in enumerate(detail, 1):
    r_s = f"{d['r']:>+6.2f}R" if d["r"] is not None else "   OPEN"
    if d["r"] is not None: cum += d["r"]
    print(f"  {i:>3}  {str(d['date']):>10}  {d['dir']:>5}  {d['sweep_t']:>6}  "
          f"{d['wick']:>5.1f}  {d['level']:>7.1f}  {d['el']:>7.1f}  "
          f"{d['tp']:>7.1f}  {d['sl']:>7.1f}  {d['risk']:>5.0f}  "
          f"{d['rr']:>5.2f}  {d['oc']:>5}  {r_s}")

if closed:
    wr = len(wins)/len(closed)
    avg_rr = statistics.mean(d["rr"] for d in wins) if wins else 0
    print(f"\n  Closed: {len(closed)}  Wins: {len(wins)}  WR: {wr:.0%}  "
          f"avg_win_R: {avg_rr:.2f}R  Cum: {cum:+.2f}R  ({cum/len(closed):+.2f}R/trade)")

# Monthly consistency
print(f"\n  Monthly:")
months = defaultdict(list)
for d in closed:
    months[(d["date"].year, d["date"].month)].append(d)
for ym in sorted(months):
    sub = months[ym]; w = [r for r in sub if r["oc"]=="WIN"]
    wr_m = len(w)/len(sub)
    ev_m = wr_m*(statistics.mean(r["rr"] for r in w) if w else 0)-(1-wr_m)*1.0
    print(f"    {ym[0]}-{ym[1]:02d}  n={len(sub):>2}  WR={wr_m:.0%}  "
          f"EV={ev_m:+.2f}R  {'█'*len(w)}{'·'*(len(sub)-len(w))}")

print("\nDone.")
