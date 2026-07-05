#!/usr/bin/env python3
"""
DAX Pattern C: Pre-US directional fade.

Hypothesis: in the 2.5h before US open, early US traders push DAX in one
direction (positioning / stop hunting). At the US open the move exhausts
and partially reverses. Trade the reversal.

Timeframe : 15m (resampled from 5m DB data)
Morning   : 7:00–11:00 UTC  → ATR reference
Pre-US    : 11:00–13:30 UTC → directional move measured here
Entry     : at 13:30 UTC (US open), IN THE OPPOSITE direction of pre-US move
Evaluation: 13:30–16:00 UTC (covers the US morning session)

Directional filter:
  net_move = close(last pre-US bar) − open(first pre-US bar)
  only trade if abs(net_move) ≥ min_atr_mult × morning_ATR

SL: pre-US extreme + sl_mult × ATR (above high for SHORT, below low for LONG)
TP: entry − tp_pct × pre_us_range  (for SHORT, going down tp_pct of range)

Parameter grid:
  min_atr_mult : [0.5, 1.0, 1.5, 2.0]     minimum pre-US net move
  sl_mult      : [0.25, 0.50, 0.75]         ATR buffer above/below extreme
  tp_pct       : [0.25, 0.50, 0.75, 1.00]  fraction of pre-US range to target
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
PRE_US_H,   PRE_US_M   = 11,  0
US_OPEN_H,  US_OPEN_M  = 13, 30
EVAL_END_H, EVAL_END_M = 16,  0
MIN_MORNING_BARS_15m = 4

MIN_ATR_MULTS = [0.5, 1.0, 1.5, 2.0]
SL_MULTS      = [0.25, 0.50, 0.75]
TP_PCTS       = [0.25, 0.50, 0.75, 1.00]


def _ts(d: date, h: int, m: int) -> int:
    return int(datetime(d.year, d.month, d.day, h, m, tzinfo=_UTC).timestamp())


def _atr(bars, period=14):
    if len(bars) < 2: return 0.0
    trs = [max(b["high"] - b["low"],
               abs(b["high"] - bars[i-1]["close"]),
               abs(b["low"]  - bars[i-1]["close"]))
           for i, b in enumerate(bars[1:], 1)]
    return statistics.mean(trs[-period:]) if trs else 0.0


def resample_15m(bars_5m):
    buckets = defaultdict(list)
    for b in bars_5m:
        bucket = (b["timestamp"] // 900) * 900
        buckets[bucket].append(b)
    result = []
    for ts in sorted(buckets):
        group = sorted(buckets[ts], key=lambda x: x["timestamp"])
        result.append({
            "timestamp": ts,
            "open":  group[0]["open"],
            "high":  max(b["high"] for b in group),
            "low":   min(b["low"]  for b in group),
            "close": group[-1]["close"],
        })
    return result


# ── Load data ─────────────────────────────────────────────────────────────────

print("Loading GER40 5m data and resampling to 15m …")
db = LocalDB(DB_PATH)
all5m = list(reversed(db.query_recent("GER40", "5m", limit=130_000)))
all15m = resample_15m(all5m)
print(f"  5m: {len(all5m)} bars  →  15m: {len(all15m)} bars")

dates = sorted(set(datetime.fromtimestamp(c["timestamp"], tz=_UTC).date() for c in all5m))
dates = [d for d in dates if d.weekday() < 5]

# ── Build sessions ────────────────────────────────────────────────────────────

print("Building sessions …")
sessions = []

for d in dates:
    ts_ms = _ts(d, MORNING_H,  MORNING_M)
    ts_ps = _ts(d, PRE_US_H,   PRE_US_M)
    ts_uo = _ts(d, US_OPEN_H,  US_OPEN_M)
    ts_ee = _ts(d, EVAL_END_H, EVAL_END_M)

    morning_15m  = [b for b in all15m if ts_ms <= b["timestamp"] < ts_ps]
    pre_us_15m   = [b for b in all15m if ts_ps <= b["timestamp"] < ts_uo]
    eval_15m     = [b for b in all15m if ts_uo <= b["timestamp"] <= ts_ee]
    # 5m eval bars for finer entry/exit
    eval_5m      = [b for b in all5m  if ts_uo <= b["timestamp"] <= ts_ee]

    if len(morning_15m) < MIN_MORNING_BARS_15m: continue
    if len(pre_us_15m)  < 3: continue
    if not eval_5m: continue

    atr = _atr(morning_15m) or 20.0

    pre_us_open  = pre_us_15m[0]["open"]
    pre_us_close = pre_us_15m[-1]["close"]
    pre_us_high  = max(b["high"] for b in pre_us_15m)
    pre_us_low   = min(b["low"]  for b in pre_us_15m)
    pre_us_range = pre_us_high - pre_us_low
    net_move     = pre_us_close - pre_us_open

    # Directionality: how "clean" was the move?
    directionality = abs(net_move) / pre_us_range if pre_us_range > 0 else 0

    morning_high = max(b["high"] for b in morning_15m)
    morning_low  = min(b["low"]  for b in morning_15m)

    if pre_us_range < 5: continue   # degenerate day

    sessions.append({
        "date":           d,
        "atr":            atr,
        "net_move":       net_move,
        "net_atr_ratio":  net_move / atr,
        "pre_us_open":    pre_us_open,
        "pre_us_close":   pre_us_close,
        "pre_us_high":    pre_us_high,
        "pre_us_low":     pre_us_low,
        "pre_us_range":   pre_us_range,
        "directionality": directionality,
        "morning_high":   morning_high,
        "morning_low":    morning_low,
        "eval_5m":        eval_5m,
    })

print(f"  {len(sessions)} valid sessions\n")


# ── Characterise pre-US direction ─────────────────────────────────────────────

print("=" * 68)
print("PRE-US MOVE DISTRIBUTION (net move / morning ATR)")
print("=" * 68)

net_ratios = [abs(s["net_atr_ratio"]) for s in sessions]
print(f"\n  Sessions: {len(sessions)}")
print(f"  Avg |net_move|:   {statistics.mean(net_ratios):.2f}×ATR")
print(f"  Median |net_move|:{statistics.median(net_ratios):.2f}×ATR")

# How many days have net move ≥ X×ATR?
for threshold in [0.5, 1.0, 1.5, 2.0, 2.5]:
    n = sum(1 for r in net_ratios if r >= threshold)
    print(f"  |net_move| ≥ {threshold}×ATR: {n:>3} days ({n/len(sessions):.0%})")

print(f"\n  Directionality distribution (|net|/range):")
for lo, hi, label in [(0,.3,"0–30% (choppy)"),(0.3,.5,"30–50%"),
                       (0.5,.7,"50–70%"),(0.7,.9,"70–90%"),(0.9,1.1,"90–100% (clean)")]:
    n = sum(1 for s in sessions if lo <= s["directionality"] < hi)
    print(f"  {label:<20}: {n:>3} ({n/len(sessions):.0%})")


# ── Core evaluator ────────────────────────────────────────────────────────────

def run_session(s, min_atr_mult, sl_mult, tp_pct):
    """
    Attempt Pattern C fade. Returns outcome dict or None if filter fails.
    """
    net = s["net_move"]
    atr = s["atr"]

    if abs(net) < min_atr_mult * atr:
        return None

    # Direction: fade the pre-US move
    is_short = net > 0   # pre-US moved UP → fade = SHORT

    entry      = s["pre_us_close"]   # enter at US open price
    sl_level   = (s["pre_us_high"] + sl_mult * atr) if is_short \
                 else (s["pre_us_low"] - sl_mult * atr)
    tp_level   = entry - tp_pct * s["pre_us_range"] if is_short \
                 else entry + tp_pct * s["pre_us_range"]

    risk = abs(entry - sl_level)
    if risk < 0.5: return None
    reward = abs(entry - tp_level)
    rr = reward / risk

    for bar in s["eval_5m"]:
        if is_short:
            if bar["high"] >= sl_level: return {"oc": "LOSS", "r": -1.0, "rr": rr,
                                                "risk": risk, "reward": reward}
            if bar["low"]  <= tp_level: return {"oc": "WIN",  "r": rr,   "rr": rr,
                                                "risk": risk, "reward": reward}
        else:
            if bar["low"]  <= sl_level: return {"oc": "LOSS", "r": -1.0, "rr": rr,
                                                "risk": risk, "reward": reward}
            if bar["high"] >= tp_level: return {"oc": "WIN",  "r": rr,   "rr": rr,
                                                "risk": risk, "reward": reward}

    return {"oc": "OPEN", "r": None, "rr": rr, "risk": risk, "reward": reward}


def summarise(results):
    closed = [r for r in results if r["oc"] != "OPEN"]
    if len(closed) < 10: return None
    wins = [r for r in closed if r["oc"] == "WIN"]
    wr   = len(wins) / len(closed)
    avg_rr = statistics.mean(r["rr"] for r in wins) if wins else 0
    ev = wr * avg_rr - (1 - wr) * 1.0
    avg_pts = ((wr * statistics.mean(r["reward"] for r in wins) -
                (1-wr) * statistics.mean(r["risk"] for r in closed if r["oc"]=="LOSS"))
               if wins else -statistics.mean(r["risk"] for r in closed))
    return {"n_cl": len(closed), "n_op": len(results)-len(closed),
            "wr": wr, "avg_rr": avg_rr, "ev": ev, "avg_pts": avg_pts}


# ── Parameter sweep ───────────────────────────────────────────────────────────

print(f"\n\nRunning {len(MIN_ATR_MULTS)*len(SL_MULTS)*len(TP_PCTS)} param combos …")
combos_out = []

for min_atr, sl_m, tp_p in product(MIN_ATR_MULTS, SL_MULTS, TP_PCTS):
    results = []
    for s in sessions:
        r = run_session(s, min_atr, sl_m, tp_p)
        if r: results.append(r)
    sm = summarise(results)
    if sm:
        combos_out.append({"min_atr": min_atr, "sl_m": sl_m, "tp_p": tp_p, **sm})

combos_out.sort(key=lambda x: x["ev"], reverse=True)
print(f"Done — {len(combos_out)} valid combos\n")

print("=" * 80)
print("PATTERN C — top 30 by EV  (min 10 closed)")
print("=" * 80)
print(f"\n  {'minATR':>6}  {'sl×ATR':>6}  {'tp%':>5}  {'N':>4}  "
      f"{'WR':>5}  {'avgR:R':>7}  {'EV':>7}  {'avg_pts':>8}  {'Open':>5}")
print("  " + "─" * 68)
for r in combos_out[:30]:
    print(f"  {r['min_atr']:>6.1f}  {r['sl_m']:>6.2f}  {r['tp_p']:>5.2f}  "
          f"{r['n_cl']:>4}  {r['wr']:>5.0%}  {r['avg_rr']:>7.2f}R  "
          f"{r['ev']:>+7.2f}R  {r['avg_pts']:>+8.1f}pt  {r['n_op']:>5}")

if not combos_out: sys.exit(0)

best = combos_out[0]
print(f"\nBest: minATR={best['min_atr']}  sl={best['sl_m']}×ATR  tp={best['tp_p']:.0%}  "
      f"EV={best['ev']:+.2f}R  WR={best['wr']:.0%}  N={best['n_cl']} closed")


# ── WR vs net move size ────────────────────────────────────────────────────────

print(f"\n\n{'='*68}")
print("FADE WR vs PRE-US NET MOVE SIZE  (tp=50%, sl=0.50×ATR)")
print(f"{'='*68}")
print(f"\n  {'Net move':>12}  {'N':>4}  {'WR':>5}  {'avg R:R':>7}  {'EV':>7}")
print("  " + "─" * 45)

for lo, hi, label in [(-99,-2.0,"< −2×ATR"),(-2.0,-1.5,"−2 to −1.5"),
                       (-1.5,-1.0,"−1.5 to −1"),(-1.0,-0.5,"−1 to −0.5"),
                       (-0.5,0.0,"−0.5 to 0"),(0.0,0.5,"0 to +0.5"),
                       (0.5,1.0,"0.5 to +1"),(1.0,1.5,"1 to +1.5"),
                       (1.5,2.0,"1.5 to +2"),(2.0,99,"> +2×ATR")]:
    sub = [s for s in sessions if lo <= s["net_atr_ratio"] < hi]
    if len(sub) < 3: continue
    results = [run_session(s, 0.0, 0.50, 0.50) for s in sub]
    results = [r for r in results if r]
    closed  = [r for r in results if r["oc"] != "OPEN"]
    if not closed: continue
    wins = [r for r in closed if r["oc"] == "WIN"]
    wr = len(wins)/len(closed)
    avg_rr = statistics.mean(r["rr"] for r in wins) if wins else 0
    ev = wr * avg_rr - (1-wr)*1.0
    bar = "█" * int(wr * 20)
    print(f"  {label:>12}  {len(closed):>4}  {wr:>5.0%}  {avg_rr:>7.2f}R  {ev:>+7.2f}R  {bar}")


# ── Directionality filter ─────────────────────────────────────────────────────

print(f"\n\n{'='*68}")
print("FADE WR vs DIRECTIONALITY FILTER  (min_atr=1.0, sl=0.50, tp=50%)")
print("Directionality = |net_move| / pre_US_range (1.0 = perfectly linear)")
print(f"{'='*68}")
print(f"\n  {'Filter':>18}  {'N':>4}  {'WR':>5}  {'avg R:R':>7}  {'EV':>7}")
print("  " + "─" * 50)

for lo, hi, label in [(0.0,1.1,"any (no filter)"),
                       (0.4,1.1,"≥0.40 directional"),
                       (0.5,1.1,"≥0.50 directional"),
                       (0.6,1.1,"≥0.60 directional"),
                       (0.7,1.1,"≥0.70 directional")]:
    sub = [s for s in sessions if lo <= s["directionality"] < hi]
    results = [run_session(s, 1.0, 0.50, 0.50) for s in sub]
    results = [r for r in results if r]
    closed  = [r for r in results if r["oc"] != "OPEN"]
    if len(closed) < 5: continue
    wins = [r for r in closed if r["oc"] == "WIN"]
    wr   = len(wins)/len(closed)
    avg_rr = statistics.mean(r["rr"] for r in wins) if wins else 0
    ev   = wr * avg_rr - (1-wr)*1.0
    print(f"  {label:>18}  {len(closed):>4}  {wr:>5.0%}  {avg_rr:>7.2f}R  {ev:>+7.2f}R")


# ── Best combo deep dive ──────────────────────────────────────────────────────

print(f"\n\n{'='*80}")
print(f"DEEP DIVE — minATR={best['min_atr']}  sl={best['sl_m']}×ATR  tp={best['tp_p']:.0%}")
print(f"{'='*80}")

detail = []
for s in sessions:
    r = run_session(s, best["min_atr"], best["sl_m"], best["tp_p"])
    if r is None: continue
    is_short = s["net_move"] > 0
    detail.append({
        "date":       s["date"],
        "dir":        "SHORT" if is_short else "LONG",
        "net_atr":    s["net_atr_ratio"],
        "dir_score":  s["directionality"],
        "pre_range":  s["pre_us_range"],
        "oc":         r["oc"],
        "r_val":      r["r"],
        "rr":         r["rr"],
        "risk":       r["risk"],
        "reward":     r["reward"],
    })

closed = [d for d in detail if d["oc"] != "OPEN"]
wins   = [d for d in closed if d["oc"] == "WIN"]

print(f"\n  Signals: {len(detail)}  Closed: {len(closed)}  "
      f"Wins: {len(wins)}  Losses: {len(closed)-len(wins)}")
if closed:
    wr = len(wins)/len(closed)
    avg_rr = statistics.mean(d["rr"] for d in wins) if wins else 0
    ev = wr * avg_rr - (1-wr)*1.0
    print(f"  WR: {wr:.0%}  avg win R:R: {avg_rr:.2f}R  EV: {ev:+.2f}R")
    print(f"  Avg risk: {statistics.mean(d['risk'] for d in closed):.1f} pts")
    print(f"  Avg reward (wins): {statistics.mean(d['reward'] for d in wins):.1f} pts" if wins else "")

# Monthly breakdown
print(f"\n  Monthly breakdown:")
months = defaultdict(list)
for d in closed:
    months[(d["date"].year, d["date"].month)].append(d)
for ym in sorted(months):
    sub = months[ym]
    w = [d for d in sub if d["oc"]=="WIN"]
    wr_m = len(w)/len(sub)
    ev_m = wr_m * (statistics.mean(d["rr"] for d in w) if w else 0) - (1-wr_m)*1.0
    print(f"    {ym[0]}-{ym[1]:02d}: n={len(sub):>2}  WR={wr_m:.0%}  EV={ev_m:+.2f}R  "
          f"{'█'*len(w)}{'·'*(len(sub)-len(w))}")

# UP vs DOWN breakdown
print(f"\n  By fade direction:")
for fade_dir in ["SHORT", "LONG"]:
    sub = [d for d in closed if d["dir"] == fade_dir]
    if not sub: continue
    w = [d for d in sub if d["oc"]=="WIN"]
    wr_d = len(w)/len(sub)
    avg_rr_d = statistics.mean(d["rr"] for d in w) if w else 0
    ev_d = wr_d * avg_rr_d - (1-wr_d)*1.0
    print(f"    Fade {fade_dir}: n={len(sub):>3}  WR={wr_d:.0%}  "
          f"avg_win_R={avg_rr_d:.2f}  EV={ev_d:+.2f}R")

# Net move size breakdown
print(f"\n  By pre-US net move magnitude:")
for lo, hi, label in [(0,1,"0–1×ATR"),(1,1.5,"1–1.5×ATR"),
                       (1.5,2,"1.5–2×ATR"),(2,99,"2+×ATR")]:
    sub = [d for d in closed if lo <= abs(d["net_atr"]) < hi]
    if not sub: continue
    w = [d for d in sub if d["oc"]=="WIN"]
    wr_b = len(w)/len(sub)
    avg_rr_b = statistics.mean(d["rr"] for d in w) if w else 0
    ev_b = wr_b * avg_rr_b - (1-wr_b)*1.0
    print(f"    {label}: n={len(sub):>3}  WR={wr_b:.0%}  "
          f"avg_win_R={avg_rr_b:.2f}  EV={ev_b:+.2f}R")

# Directionality breakdown
print(f"\n  By directionality score:")
for lo, hi, label in [(0,.4,"<0.4 (choppy)"),(0.4,.6,"0.4–0.6"),
                       (0.6,.8,"0.6–0.8"),(0.8,1.1,"0.8+ (clean)")]:
    sub = [d for d in closed if lo <= d["dir_score"] < hi]
    if not sub: continue
    w = [d for d in sub if d["oc"]=="WIN"]
    wr_b = len(w)/len(sub)
    avg_rr_b = statistics.mean(d["rr"] for d in w) if w else 0
    ev_b = wr_b * avg_rr_b - (1-wr_b)*1.0
    print(f"    {label}: n={len(sub):>3}  WR={wr_b:.0%}  "
          f"avg_win_R={avg_rr_b:.2f}  EV={ev_b:+.2f}R")


# ── Per-trade table ───────────────────────────────────────────────────────────

print(f"\n\n{'─'*85}")
print(f"Per-trade  ({len(detail)} signals  {len(closed)} closed  {len(wins)} wins)")
print(f"{'─'*85}")
print(f"  {'#':>3}  {'Date':>10}  {'Dir':>5}  {'Net×ATR':>7}  {'Dir%':>5}  "
      f"{'Range':>6}  {'Risk':>5}  {'Rwd':>5}  {'R:R':>5}  {'Oc':>5}  {'R':>7}")
print(f"  {'─'*82}")
cum_r = 0.0
for i, d in enumerate(detail, 1):
    r_s = f"{d['r_val']:>+6.2f}R" if d["r_val"] is not None else "   OPEN"
    if d["r_val"] is not None: cum_r += d["r_val"]
    print(f"  {i:>3}  {str(d['date']):>10}  {d['dir']:>5}  {d['net_atr']:>+7.2f}  "
          f"{d['dir_score']:>5.0%}  {d['pre_range']:>6.0f}  {d['risk']:>5.0f}  "
          f"{d['reward']:>5.0f}  {d['rr']:>5.2f}  {d['oc']:>5}  {r_s}")

if closed:
    print(f"\n  Cumulative R: {cum_r:+.2f}R  ({cum_r/len(closed):+.2f}R per trade)")

print("\nDone.")
