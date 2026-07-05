#!/usr/bin/env python3
"""
DAX Pre-US Continuation: full parameter sweep.

After a morning-range sweep, enter IN THE SWEEP DIRECTION immediately.
  Bear sweep (wick above morning_high, close inside) → LONG (price continues UP)
  Bull sweep (wick below morning_low, close inside)  → SHORT (price continues DOWN)

Entry  : sweep candle close
SL     : entry – sl_pct × range  (price allowed to retrace sl_pct before stopping)
         OR  entry – sl_atr × ATR
TP     : morning_level + tp_ext × range  (beyond the swept level by tp_ext)
         (morning_level = morning_high for LONG, morning_low for SHORT)
Cutoff : 15:30 UTC

Parameters swept:
  min_wick_pts  : minimum wick extension beyond level (0, 3, 7, 15 pts)
  sl_pct        : SL as % of morning range   (0.10, 0.15, 0.20, 0.25, 0.30)
  sl_atr        : SL as × ATR                (0.25, 0.50, 0.75)  [separate grid]
  tp_ext        : TP extension beyond level, as % of range (0.0, 0.25, 0.50, 1.0)

Also shows:
  - WR vs wick size
  - WR vs sweep time
  - Reach rate: how often price reaches each TP level at all (ignoring SL)
  - Monthly consistency
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

MORNING_H,  MORNING_M  =  7,  0
RANGE_END_H, RANGE_END_M = 11, 0
SWEEP_END_H, SWEEP_END_M = 13, 30
CUTOFF_H,   CUTOFF_M   = 15, 30
MIN_MORNING_BARS = 6
MIN_RANGE_ATR    = 0.5

MIN_WICK_PTS_LIST = [0, 3, 7, 15]
SL_PCT_LIST       = [0.10, 0.15, 0.20, 0.25, 0.30]
SL_ATR_LIST       = [0.25, 0.50, 0.75]
TP_EXT_LIST       = [0.0, 0.25, 0.50, 1.0]


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

# ── Build sweep records ───────────────────────────────────────────────────────

print("Building sweep records …")
sweeps = []

for d in dates:
    ts_ms = _ts(d, MORNING_H,   MORNING_M)
    ts_re = _ts(d, RANGE_END_H, RANGE_END_M)
    ts_se = _ts(d, SWEEP_END_H, SWEEP_END_M)
    ts_co = _ts(d, CUTOFF_H,    CUTOFF_M)

    morning_bars = [c for c in all5m if ts_ms <= c["timestamp"] < ts_re]
    sweep_bars   = [c for c in all5m if ts_re  <= c["timestamp"] < ts_se]
    day5m        = [c for c in all5m if ts_ms  <= c["timestamp"] <= ts_co]

    if len(morning_bars) < MIN_MORNING_BARS: continue

    mh  = max(c["high"]  for c in morning_bars)
    ml  = min(c["low"]   for c in morning_bars)
    rng = mh - ml
    atr = _atr(morning_bars) or 20.0
    if rng < MIN_RANGE_ATR * atr: continue

    # first sweep only
    for bar in sweep_bars:
        is_bear = bar["high"] > mh and bar["close"] <= mh
        is_bull = bar["low"]  < ml and bar["close"] >= ml
        if is_bear or is_bull:
            post = [b for b in day5m if b["timestamp"] > bar["timestamp"]]
            sweep_dt = datetime.fromtimestamp(bar["timestamp"], tz=_UTC)
            sweeps.append({
                "date":        d,
                "is_long":     is_bear,          # bear sweep → continuation LONG
                "sweep_bar":   bar,
                "entry":       bar["close"],
                "wick_ext":    (bar["high"] - mh) if is_bear else (ml - bar["low"]),
                "level":       mh if is_bear else ml,    # morning level that was swept
                "morning_high": mh,
                "morning_low":  ml,
                "range":       rng,
                "atr":         atr,
                "post":        post,
                "sweep_minute": sweep_dt.hour * 60 + sweep_dt.minute,
                "sweep_hhmm":  sweep_dt.strftime("%H:%M"),
            })
            break

print(f"  {len(sweeps)} sweeps\n")


# ── Core evaluator ────────────────────────────────────────────────────────────

def evaluate(s, sl_price, tp_price):
    risk   = abs(s["entry"] - sl_price)
    reward = abs(s["entry"] - tp_price)
    if risk < 0.5: return None
    rr = reward / risk
    for bar in s["post"]:
        if s["is_long"]:
            if bar["low"]  <= sl_price: return {"oc":"LOSS","r":-1.0,"rr":rr,"risk":risk,"rwd":reward}
            if bar["high"] >= tp_price: return {"oc":"WIN", "r":rr,  "rr":rr,"risk":risk,"rwd":reward}
        else:
            if bar["high"] >= sl_price: return {"oc":"LOSS","r":-1.0,"rr":rr,"risk":risk,"rwd":reward}
            if bar["low"]  <= tp_price: return {"oc":"WIN", "r":rr,  "rr":rr,"risk":risk,"rwd":reward}
    return {"oc":"OPEN","r":None,"rr":rr,"risk":risk,"rwd":reward}


def run_sweep(min_wick, sl_val, sl_mode, tp_ext):
    """sl_mode: 'pct' or 'atr'"""
    results = []
    for s in sweeps:
        if s["wick_ext"] < min_wick: continue

        if sl_mode == "pct":
            sl_dist = sl_val * s["range"]
        else:
            sl_dist = sl_val * s["atr"]

        sl_price = (s["entry"] - sl_dist) if s["is_long"] else (s["entry"] + sl_dist)
        tp_price = (s["level"] + tp_ext * s["range"]) if s["is_long"] \
                   else (s["level"] - tp_ext * s["range"])

        # entry must be on correct side of TP
        if s["is_long"]  and tp_price <= s["entry"]: continue
        if not s["is_long"] and tp_price >= s["entry"]: continue

        r = evaluate(s, sl_price, tp_price)
        if r: results.append({**r, **{"wick": s["wick_ext"], "sm": s["sweep_minute"],
                                       "date": s["date"], "is_long": s["is_long"],
                                       "range": s["range"], "atr": s["atr"]}})
    return results


def summarise(results):
    closed = [r for r in results if r["oc"] != "OPEN"]
    if len(closed) < 15: return None
    wins = [r for r in closed if r["oc"] == "WIN"]
    wr = len(wins) / len(closed)
    avg_rr = statistics.mean(r["rr"] for r in wins) if wins else 0
    ev = wr * avg_rr - (1 - wr) * 1.0
    avg_pts = (wr * statistics.mean(r["rwd"] for r in wins)
               - (1-wr) * statistics.mean(r["risk"] for r in closed if r["oc"]=="LOSS")) if wins else -1
    return {"n": len(results), "n_cl": len(closed), "n_op": len(results)-len(closed),
            "wr": wr, "avg_rr": avg_rr, "ev": ev, "avg_pts": avg_pts}


# ── GRID 1: SL as % of range ──────────────────────────────────────────────────

print("Running SL-as-%range grid …")
pct_results = []
for wick, sl_p, tp_e in product(MIN_WICK_PTS_LIST, SL_PCT_LIST, TP_EXT_LIST):
    rs = run_sweep(wick, sl_p, "pct", tp_e)
    sm = summarise(rs)
    if sm: pct_results.append({"wick":wick,"sl":sl_p,"sl_mode":"pct","tp":tp_e,**sm})

# ── GRID 2: SL as × ATR ──────────────────────────────────────────────────────

print("Running SL-as-ATR grid …")
atr_results = []
for wick, sl_a, tp_e in product(MIN_WICK_PTS_LIST, SL_ATR_LIST, TP_EXT_LIST):
    rs = run_sweep(wick, sl_a, "atr", tp_e)
    sm = summarise(rs)
    if sm: atr_results.append({"wick":wick,"sl":sl_a,"sl_mode":"atr","tp":tp_e,**sm})

all_results = sorted(pct_results + atr_results, key=lambda x: x["ev"], reverse=True)
print(f"Done — {len(all_results)} valid combos\n")


# ── Top combos ────────────────────────────────────────────────────────────────

print("=" * 86)
print("CONTINUATION — top 30 by EV  (≥15 closed)")
print("=" * 86)
print(f"\n  {'wick':>4}  {'SL':>10}  {'TP ext':>6}  {'N':>4}  "
      f"{'WR':>5}  {'avgR:R':>7}  {'EV':>7}  {'avg_pts':>8}  {'Open':>5}")
print("  " + "─" * 74)
for r in all_results[:30]:
    sl_label = f"{r['sl']:.0%}rng" if r["sl_mode"]=="pct" else f"{r['sl']:.2f}ATR"
    print(f"  {r['wick']:>4}  {sl_label:>10}  {r['tp']:>6.2f}  {r['n_cl']:>4}  "
          f"{r['wr']:>5.0%}  {r['avg_rr']:>7.2f}R  {r['ev']:>+7.2f}R  "
          f"{r['avg_pts']:>+8.1f}pt  {r['n_op']:>5}")

best = all_results[0]
sl_label_b = f"{best['sl']:.0%} of range" if best["sl_mode"]=="pct" else f"{best['sl']:.2f}×ATR"
print(f"\nBest: wick≥{best['wick']}pts  SL={sl_label_b}  TP=level+{best['tp']:.0%}×range  "
      f"EV={best['ev']:+.2f}R  WR={best['wr']:.0%}  N={best['n_cl']}")


# ── TP reach rate (no SL) ─────────────────────────────────────────────────────

print(f"\n\n{'='*68}")
print("TP REACH RATE — how often price reaches each level after sweep")
print("(no SL, just: does price ever touch the target before 15:30?)")
print(f"{'='*68}")
print(f"\n  {'TP target':>28}  {'N':>4}  {'Reach%':>7}  bar")
for tp_e in [0.0, 0.10, 0.25, 0.50, 0.75, 1.0, 1.5]:
    reached = 0
    for s in sweeps:
        tp = (s["level"] + tp_e * s["range"]) if s["is_long"] \
             else (s["level"] - tp_e * s["range"])
        if s["is_long"] and tp <= s["entry"]: continue
        if not s["is_long"] and tp >= s["entry"]: continue
        for bar in s["post"]:
            if (s["is_long"] and bar["high"] >= tp) or \
               (not s["is_long"] and bar["low"] <= tp):
                reached += 1; break
    pct = reached / len(sweeps)
    label = f"level+{tp_e:.2f}×range" if tp_e > 0 else "level (morning high/low)"
    bar_s = "█" * int(pct * 30)
    print(f"  {label:>28}: {reached:>3}/{len(sweeps)}  ({pct:.0%})  {bar_s}")


# ── WR vs wick size (best SL/TP) ─────────────────────────────────────────────

print(f"\n\n{'='*68}")
print(f"WR VS WICK SIZE  (SL={best['sl']:.0%}rng, TP=level+{best['tp']:.0%}×range)")
print(f"{'='*68}")
for lo, hi, label in [(0,3,"0–2 pts"),(3,7,"3–6 pts"),(7,15,"7–14 pts"),(15,999,"15+ pts")]:
    rs = [r for r in run_sweep(0, best["sl"], best["sl_mode"], best["tp"])
          if lo <= r["wick"] < hi]
    cl = [r for r in rs if r["oc"]!="OPEN"]
    if not cl: continue
    w  = [r for r in cl if r["oc"]=="WIN"]
    wr = len(w)/len(cl)
    avg_rr = statistics.mean(r["rr"] for r in w) if w else 0
    ev = wr*avg_rr-(1-wr)*1.0
    print(f"  wick {label:>8}: n={len(cl):>3}  WR={wr:.0%}  avg_win_R={avg_rr:.2f}  EV={ev:+.2f}R")


# ── WR vs sweep time ──────────────────────────────────────────────────────────

print(f"\n\n{'='*68}")
print(f"WR VS SWEEP TIME  (best params: wick≥{best['wick']}, "
      f"SL={best['sl']:.0%}rng, TP+{best['tp']:.0%}×range)")
print(f"{'='*68}")
base_rs = run_sweep(best["wick"], best["sl"], best["sl_mode"], best["tp"])
for t0, t1, label in [(660,690,"11:00–11:30"),(690,720,"11:30–12:00"),
                       (720,750,"12:00–12:30"),(750,810,"12:30–13:30")]:
    cl = [r for r in base_rs if t0 <= r["sm"] < t1 and r["oc"]!="OPEN"]
    if not cl: continue
    w  = [r for r in cl if r["oc"]=="WIN"]
    wr = len(w)/len(cl)
    avg_rr = statistics.mean(r["rr"] for r in w) if w else 0
    ev = wr*avg_rr-(1-wr)*1.0
    print(f"  {label}: n={len(cl):>3}  WR={wr:.0%}  avg_win_R={avg_rr:.2f}  EV={ev:+.2f}R")


# ── Monthly consistency (best params) ────────────────────────────────────────

print(f"\n\n{'='*68}")
print("MONTHLY BREAKDOWN — best params")
print(f"{'='*68}")
months = defaultdict(list)
for r in base_rs:
    if r["oc"] != "OPEN":
        months[(r["date"].year, r["date"].month)].append(r)

for ym in sorted(months):
    sub = months[ym]
    w = [r for r in sub if r["oc"]=="WIN"]
    wr_m = len(w)/len(sub)
    ev_m = wr_m * (statistics.mean(r["rr"] for r in w) if w else 0) - (1-wr_m)*1.0
    wins_bar  = "█" * len(w)
    loss_bar  = "·" * (len(sub)-len(w))
    print(f"  {ym[0]}-{ym[1]:02d}  n={len(sub):>2}  WR={wr_m:.0%}  EV={ev_m:+.2f}R  "
          f"{wins_bar}{loss_bar}")


# ── SL tolerance chart ────────────────────────────────────────────────────────

print(f"\n\n{'='*68}")
print("SL TOLERANCE — how does WR/EV change with SL width?")
print(f"(wick≥{best['wick']}, TP=level+{best['tp']:.0%}×range, SL as % of range)")
print(f"{'='*68}")
print(f"\n  {'SL%':>7}  {'N':>4}  {'WR':>5}  {'avg R:R':>7}  {'EV':>7}  {'avg_pts':>8}")
print("  " + "─" * 50)
for sl_p in [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]:
    rs = run_sweep(best["wick"], sl_p, "pct", best["tp"])
    sm = summarise(rs)
    if sm:
        print(f"  {sl_p:>7.0%}  {sm['n_cl']:>4}  {sm['wr']:>5.0%}  "
              f"{sm['avg_rr']:>7.2f}R  {sm['ev']:>+7.2f}R  {sm['avg_pts']:>+8.1f}pt")


# ── Per-trade table (best params) ────────────────────────────────────────────

print(f"\n\n{'─'*100}")
print(f"Per-trade — best params (wick≥{best['wick']}  SL={best['sl']:.0%}rng  "
      f"TP=level+{best['tp']:.0%}×range)")
detail = []
for s in sweeps:
    if s["wick_ext"] < best["wick"]: continue
    sl_dist = best["sl"] * s["range"]
    sl_price = (s["entry"] - sl_dist) if s["is_long"] else (s["entry"] + sl_dist)
    tp_price = (s["level"] + best["tp"] * s["range"]) if s["is_long"] \
               else (s["level"] - best["tp"] * s["range"])
    if s["is_long"]  and tp_price <= s["entry"]: continue
    if not s["is_long"] and tp_price >= s["entry"]: continue
    r = evaluate(s, sl_price, tp_price)
    if r is None: continue
    detail.append({**r, "date": s["date"], "dir": "LONG" if s["is_long"] else "SHORT",
                   "sweep_t": s["sweep_hhmm"], "wick": s["wick_ext"],
                   "entry": s["entry"], "sl": sl_price, "tp": tp_price,
                   "range": s["range"]})

closed = [d for d in detail if d["oc"]!="OPEN"]
wins   = [d for d in closed if d["oc"]=="WIN"]
print(f"{'─'*100}")
print(f"  {'#':>3}  {'Date':>10}  {'Dir':>5}  {'T':>5}  {'Wick':>5}  "
      f"{'Entry':>7}  {'TP':>7}  {'SL':>7}  {'Range':>6}  {'R:R':>5}  {'Oc':>5}  {'R':>7}")
print(f"  {'─'*97}")
cum = 0.0
for i, d in enumerate(detail, 1):
    r_s = f"{d['r']:>+6.2f}R" if d["r"] is not None else "   OPEN"
    if d["r"] is not None: cum += d["r"]
    print(f"  {i:>3}  {str(d['date']):>10}  {d['dir']:>5}  {d['sweep_t']:>5}  "
          f"{d['wick']:>5.1f}  {d['entry']:>7.1f}  {d['tp']:>7.1f}  "
          f"{d['sl']:>7.1f}  {d['range']:>6.0f}  {d['rr']:>5.2f}  "
          f"{d['oc']:>5}  {r_s}")

print(f"\n  Closed: {len(closed)}  Wins: {len(wins)}  WR: {len(wins)/len(closed):.0%}")
print(f"  Cum R: {cum:+.2f}R  ({cum/len(closed):+.2f}R avg)")
print("\nDone.")
