#!/usr/bin/env python3
"""
DAX Pattern A — Setup B: Continuation from EQ pullback.

Sequence:
  1. Morning range forms 7:00–11:00 UTC
  2. First sweep in 11:00–13:30 UTC  (wick beyond range, close back inside)
  3. Price retraces to entry_zone (% of range from sweep side)
  4. Enter IN THE SWEEP DIRECTION (long for bear sweep, short for bull sweep)
  5. SL = entry – sl_mult×ATR  (below for long, above for short)
  6. TP = sweep_level + ext_pct×range  (beyond the morning level)

Intuition: the sweep was a liquidity grab. Price pulls back to EQ shaking out
early faders, then continues in the original sweep direction.

Parameters swept:
  entry_pct : where we wait for (fraction of range from sweep side): 0.35–0.65
  sl_mult   : ×ATR below/above entry: 0.25 / 0.50 / 0.75
  ext_pct   : how far beyond the sweep level to target: 0.0 / 0.25 / 0.50 / 1.0
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
EVAL_END_H,  EVAL_END_M  = 15, 30
MIN_MORNING_BARS = 6
MIN_RANGE_ATR    = 0.5

ENTRY_PCTS = [0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65]
SL_MULTS   = [0.25, 0.50, 0.75]
EXT_PCTS   = [0.0, 0.25, 0.50, 1.0]


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

# ── Build sweep sessions ──────────────────────────────────────────────────────

print("Building sessions …")
sessions = []

for d in dates:
    ts_ms = _ts(d, MORNING_H,   MORNING_M)
    ts_re = _ts(d, RANGE_END_H, RANGE_END_M)
    ts_se = _ts(d, SWEEP_END_H, SWEEP_END_M)
    ts_ee = _ts(d, EVAL_END_H,  EVAL_END_M)

    morning_bars = [c for c in all5m if ts_ms <= c["timestamp"] < ts_re]
    sweep_bars   = [c for c in all5m if ts_re  <= c["timestamp"] < ts_se]
    day5m        = [c for c in all5m if ts_ms  <= c["timestamp"] <= ts_ee]

    if len(morning_bars) < MIN_MORNING_BARS: continue

    mh  = max(c["high"]  for c in morning_bars)
    ml  = min(c["low"]   for c in morning_bars)
    rng = mh - ml
    atr = _atr(morning_bars) or 20.0

    if rng < MIN_RANGE_ATR * atr: continue

    # First sweep only
    sweep_bar = None
    for bar in sweep_bars:
        is_bear = bar["high"] > mh and bar["close"] <= mh
        is_bull = bar["low"]  < ml and bar["close"] >= ml
        if is_bear or is_bull:
            sweep_bar = bar
            is_cont_long = is_bear   # bear sweep → continuation is LONG
            break

    if sweep_bar is None: continue

    post_sweep = [b for b in day5m if b["timestamp"] > sweep_bar["timestamp"]]
    sweep_dt   = datetime.fromtimestamp(sweep_bar["timestamp"], tz=_UTC)

    sessions.append({
        "date":          d,
        "is_cont_long":  is_cont_long,
        "sweep_bar":     sweep_bar,
        "sweep_dt":      sweep_dt,
        "sweep_hhmm":    sweep_dt.strftime("%H:%M"),
        "sweep_minute":  sweep_dt.hour * 60 + sweep_dt.minute,
        "wick_ext":      (sweep_bar["high"] - mh) if not is_cont_long else (ml - sweep_bar["low"]),
        "morning_high":  mh,
        "morning_low":   ml,
        "sweep_level":   mh if is_cont_long else ml,  # the level swept
        "range":         rng,
        "atr":           atr,
        "post_sweep":    post_sweep,
        "ts_ee":         ts_ee,
    })

print(f"  {len(sessions)} sweep sessions\n")


# ── Core evaluator ────────────────────────────────────────────────────────────

def run_session(s, entry_pct, sl_mult, ext_pct):
    """
    Try to enter the continuation trade.
    Returns dict with outcome or None if no entry was taken.
    """
    is_long   = s["is_cont_long"]
    mh, ml    = s["morning_high"], s["morning_low"]
    rng       = s["range"]
    atr       = s["atr"]
    sl_lvl    = s["sweep_level"]   # the morning high or low that was swept

    # Entry level: fraction of range from the sweep side
    if is_long:
        # Bear sweep → continuation long
        # entry_pct=0.50 → enter at morning_high - 0.5×range = EQ
        entry_level = mh - entry_pct * rng
        sl_price    = entry_level - sl_mult * atr
        tp_price    = mh + ext_pct * rng     # above morning_high
    else:
        # Bull sweep → continuation short
        entry_level = ml + entry_pct * rng
        sl_price    = entry_level + sl_mult * atr
        tp_price    = ml - ext_pct * rng     # below morning_low

    # Sanity: entry must be inside morning range
    if is_long  and (entry_level >= mh or entry_level <= ml): return None
    if not is_long and (entry_level <= ml or entry_level >= mh): return None

    # Walk post-sweep bars until entry is triggered or deadline
    entry_bar = None
    for bar in s["post_sweep"]:
        if bar["timestamp"] > s["ts_ee"]: break

        # Did TP get hit before we even entered? → skip
        if is_long  and bar["high"] >= tp_price: return None
        if not is_long and bar["low"]  <= tp_price: return None

        # Entry triggered?
        if is_long  and bar["low"]  <= entry_level:
            entry_bar = bar; break
        if not is_long and bar["high"] >= entry_level:
            entry_bar = bar; break

    if entry_bar is None:
        return None  # price never reached entry zone

    # Evaluate from entry bar onward
    post_entry = [b for b in s["post_sweep"]
                  if b["timestamp"] >= entry_bar["timestamp"]]
    risk = abs(entry_level - sl_price)
    if risk < 0.5: return None
    reward = abs(tp_price - entry_level)
    rr = reward / risk

    for bar in post_entry:
        if is_long:
            if bar["low"]  <= sl_price: return {"oc": "LOSS", "r": -1.0, "rr": rr,
                                                "risk_pts": risk, "rwd_pts": reward,
                                                "entry_bar": entry_bar}
            if bar["high"] >= tp_price: return {"oc": "WIN",  "r": rr,   "rr": rr,
                                                "risk_pts": risk, "rwd_pts": reward,
                                                "entry_bar": entry_bar}
        else:
            if bar["high"] >= sl_price: return {"oc": "LOSS", "r": -1.0, "rr": rr,
                                                "risk_pts": risk, "rwd_pts": reward,
                                                "entry_bar": entry_bar}
            if bar["low"]  <= tp_price: return {"oc": "WIN",  "r": rr,   "rr": rr,
                                                "risk_pts": risk, "rwd_pts": reward,
                                                "entry_bar": entry_bar}

    return {"oc": "OPEN", "r": None, "rr": rr,
            "risk_pts": risk, "rwd_pts": reward, "entry_bar": entry_bar}


def summarise(results):
    closed = [r for r in results if r["oc"] != "OPEN"]
    if len(closed) < 10: return None
    wins = [r for r in closed if r["oc"] == "WIN"]
    wr   = len(wins) / len(closed)
    avg_rr = statistics.mean(r["rr"] for r in wins) if wins else 0
    ev = wr * avg_rr - (1 - wr) * 1.0
    avg_pts = (wr * statistics.mean(r["rwd_pts"] for r in wins) -
               (1-wr) * statistics.mean(r["risk_pts"] for r in closed if r["oc"]=="LOSS")) if wins else -1
    return {"n_sig": len(results), "n_cl": len(closed), "n_op": len(results)-len(closed),
            "wr": wr, "avg_rr": avg_rr, "ev": ev, "avg_pts": avg_pts}


# ── Parameter sweep ───────────────────────────────────────────────────────────

print(f"Running {len(ENTRY_PCTS)*len(SL_MULTS)*len(EXT_PCTS)} param combos …")
combo_results = []

for entry_pct, sl_mult, ext_pct in product(ENTRY_PCTS, SL_MULTS, EXT_PCTS):
    outcomes = []
    for s in sessions:
        r = run_session(s, entry_pct, sl_mult, ext_pct)
        if r is not None:
            outcomes.append(r)
    sm = summarise(outcomes)
    if sm:
        combo_results.append({"entry_pct": entry_pct, "sl_mult": sl_mult,
                               "ext_pct": ext_pct, **sm})

combo_results.sort(key=lambda x: x["ev"], reverse=True)
print(f"Done — {len(combo_results)} valid combos\n")


# ── Top combos by EV ──────────────────────────────────────────────────────────

print("=" * 80)
print("SETUP B — top 25 by EV  (min 10 closed)")
print("=" * 80)
print(f"\n  {'entry%':>7}  {'sl×ATR':>6}  {'ext%':>5}  {'N':>4}  "
      f"{'WR':>5}  {'avgR:R':>7}  {'EV':>7}  {'avg_pts':>8}  {'Open':>5}")
print("  " + "─" * 70)

for r in combo_results[:25]:
    print(f"  {r['entry_pct']:>7.0%}  {r['sl_mult']:>6.2f}  {r['ext_pct']:>5.2f}  "
          f"{r['n_cl']:>4}  {r['wr']:>5.0%}  {r['avg_rr']:>7.2f}R  "
          f"{r['ev']:>+7.2f}R  {r['avg_pts']:>+8.1f}pt  {r['n_op']:>5}")

if combo_results:
    best = combo_results[0]
    print(f"\nBest: entry={best['entry_pct']:.0%}  sl={best['sl_mult']}×ATR  "
          f"ext={best['ext_pct']:.0%}  EV={best['ev']:+.2f}R  WR={best['wr']:.0%}  "
          f"N={best['n_cl']} closed")


# ── How often does price reach the entry zone at all? ────────────────────────

print(f"\n\n{'='*70}")
print("ENTRY ZONE REACH RATE — how often price retraces to each level")
print("(after sweep, before 15:30 UTC, ignoring TP-before-entry)")
print(f"{'='*70}")

for entry_pct in ENTRY_PCTS:
    reached = 0
    for s in sessions:
        is_long = s["is_cont_long"]
        mh, ml  = s["morning_high"], s["morning_low"]
        rng     = s["range"]
        el      = mh - entry_pct * rng if is_long else ml + entry_pct * rng

        for bar in s["post_sweep"]:
            if bar["timestamp"] > s["ts_ee"]: break
            if is_long  and bar["low"]  <= el: reached += 1; break
            if not is_long and bar["high"] >= el: reached += 1; break

    pct = reached / len(sessions)
    bar_s = "█" * int(pct * 40)
    print(f"  Entry {entry_pct:.0%} of range:  {reached:>3}/{len(sessions)}  "
          f"({pct:.0%})  {bar_s}")


# ── Sweep-to-entry time lag ───────────────────────────────────────────────────

# Use entry_pct=0.50 (EQ) to show time distribution
EQ_ENTRY_PCT = 0.50
print(f"\n\n{'='*70}")
print(f"TIME FROM SWEEP TO EQ TOUCH (entry_pct={EQ_ENTRY_PCT:.0%})")
print(f"{'='*70}")

lag_bars = []
for s in sessions:
    is_long = s["is_cont_long"]
    mh, ml  = s["morning_high"], s["morning_low"]
    rng     = s["range"]
    el      = mh - EQ_ENTRY_PCT * rng if is_long else ml + EQ_ENTRY_PCT * rng

    for i, bar in enumerate(s["post_sweep"]):
        if bar["timestamp"] > s["ts_ee"]: break
        if is_long  and bar["low"]  <= el: lag_bars.append(i+1); break
        if not is_long and bar["high"] >= el: lag_bars.append(i+1); break

if lag_bars:
    for lo, hi, label in [(1,4,"1–3 bars (15min)"),(4,13,"4–12 bars (1h)"),
                           (13,25,"13–24 bars (2h)"),(25,999,"25+ bars")]:
        sub = [l for l in lag_bars if lo <= l < hi]
        print(f"  {label:<22}  {len(sub):>3} ({len(sub)/len(lag_bars):.0%})")
    print(f"  Median bars to EQ:  {sorted(lag_bars)[len(lag_bars)//2]}")


# ── Deep dive: best params ────────────────────────────────────────────────────

if not combo_results: sys.exit(0)

best = combo_results[0]
print(f"\n\n{'='*80}")
print(f"DEEP DIVE — entry={best['entry_pct']:.0%}  sl={best['sl_mult']}×ATR  "
      f"ext={best['ext_pct']:.0%}")
print(f"{'='*80}")

detail = []
for s in sessions:
    r = run_session(s, best["entry_pct"], best["sl_mult"], best["ext_pct"])
    if r is None: continue
    entry_dt = datetime.fromtimestamp(r["entry_bar"]["timestamp"], tz=_UTC)
    detail.append({
        "date":     s["date"],
        "dir":      "LONG" if s["is_cont_long"] else "SHORT",
        "sweep_t":  s["sweep_hhmm"],
        "entry_t":  entry_dt.strftime("%H:%M"),
        "wick":     s["wick_ext"],
        "entry":    best["entry_pct"] * s["range"],   # distance into range
        "risk":     r["risk_pts"],
        "rwd":      r["rwd_pts"],
        "rr":       r["rr"],
        "oc":       r["oc"],
        "r_val":    r["r"],
        "s":        s,
        "r":        r,
    })

closed = [d for d in detail if d["oc"] != "OPEN"]
wins   = [d for d in closed if d["oc"] == "WIN"]

print(f"\n  Signals taken: {len(detail)}  Closed: {len(closed)}  "
      f"Wins: {len(wins)}  Losses: {len(closed)-len(wins)}")
if closed:
    wr = len(wins)/len(closed)
    avg_rr = statistics.mean(d["rr"] for d in wins) if wins else 0
    ev = wr * avg_rr - (1-wr)*1.0
    avg_risk = statistics.mean(d["risk"] for d in closed)
    avg_rwd  = statistics.mean(d["rwd"]  for d in wins) if wins else 0
    print(f"  WR: {wr:.0%}  avg win R:R: {avg_rr:.2f}R  EV: {ev:+.2f}R")
    print(f"  Avg risk: {avg_risk:.1f} pts  Avg reward (wins): {avg_rwd:.1f} pts")

# Breakdown by wick
print(f"\n  By wick size:")
for lo, hi, label in [(0,5,"0–5pt"),(5,10,"5–10pt"),(10,20,"10–20pt"),(20,999,"20+pt")]:
    sub = [d for d in closed if lo <= d["wick"] < hi]
    if not sub: continue
    w = [d for d in sub if d["oc"]=="WIN"]
    wr_b = len(w)/len(sub)
    avg_r = statistics.mean(d["rr"] for d in w) if w else 0
    ev_b  = wr_b * avg_r - (1-wr_b)*1.0
    print(f"    wick {label}: n={len(sub):>3}  WR={wr_b:.0%}  avg_win_R={avg_r:.2f}  EV={ev_b:+.2f}R")

# Breakdown by sweep time
print(f"\n  By sweep time (UTC):")
for t0, t1, label in [(660,690,"11:00-11:30"),(690,720,"11:30-12:00"),
                       (720,750,"12:00-12:30"),(750,810,"12:30-13:30")]:
    sub = [d for d in closed
           if t0 <= d["s"]["sweep_minute"] < t1]
    if not sub: continue
    w = [d for d in sub if d["oc"]=="WIN"]
    wr_b = len(w)/len(sub)
    avg_r = statistics.mean(d["rr"] for d in w) if w else 0
    ev_b  = wr_b * avg_r - (1-wr_b)*1.0
    print(f"    {label}: n={len(sub):>3}  WR={wr_b:.0%}  avg_win_R={avg_r:.2f}  EV={ev_b:+.2f}R")

# Entry time breakdown
print(f"\n  By entry time (UTC):")
for t0, t1, label in [(660,720,"11:00-12:00"),(720,780,"12:00-13:00"),
                       (780,840,"13:00-14:00"),(840,960,"14:00-16:00")]:
    def em(d):
        h, m = int(d["entry_t"][:2]), int(d["entry_t"][3:])
        return h*60+m
    sub = [d for d in closed if t0 <= em(d) < t1]
    if not sub: continue
    w = [d for d in sub if d["oc"]=="WIN"]
    wr_b = len(w)/len(sub)
    avg_r = statistics.mean(d["rr"] for d in w) if w else 0
    ev_b  = wr_b * avg_r - (1-wr_b)*1.0
    print(f"    {label}: n={len(sub):>3}  WR={wr_b:.0%}  avg_win_R={avg_r:.2f}  EV={ev_b:+.2f}R")


# ── Per-trade table ───────────────────────────────────────────────────────────

print(f"\n\n{'─'*95}")
print(f"Per-trade — {len(detail)} signals  {len(closed)} closed  {len(wins)} wins")
print(f"{'─'*95}")
print(f"  {'#':>3}  {'Date':>10}  {'Dir':>5}  {'SweepT':>6}  {'EntryT':>6}  "
      f"{'Wick':>5}  {'Risk':>5}  {'Rwd':>5}  {'R:R':>5}  {'Oc':>5}  {'R':>7}")
print(f"  {'─'*92}")
cum_r = 0.0
for i, d in enumerate(detail, 1):
    r_s = f"{d['r_val']:>+6.2f}R" if d["r_val"] is not None else "  OPEN"
    if d["r_val"] is not None: cum_r += d["r_val"]
    print(f"  {i:>3}  {str(d['date']):>10}  {d['dir']:>5}  {d['sweep_t']:>6}  "
          f"{d['entry_t']:>6}  {d['wick']:>5.1f}  {d['risk']:>5.0f}  "
          f"{d['rwd']:>5.0f}  {d['rr']:>5.2f}  {d['oc']:>5}  {r_s}")

if closed:
    print(f"\n  Cumulative R (closed): {cum_r:+.2f}R")
    print(f"  Avg R per closed trade: {cum_r/len(closed):+.2f}R")

print("\nDone.")
