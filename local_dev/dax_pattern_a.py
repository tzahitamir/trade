#!/usr/bin/env python3
"""
DAX Pattern A: Pre-US open liquidity sweep + reversal.

Morning range : 7:00–11:00 UTC  (Frankfurt open → 2.5h before US open)
Sweep window  : 11:00–13:30 UTC (2.5h before US open)
Evaluation    : 13:30–15:30 UTC (US open → Frankfurt close)

Sweep definition:
  5m bar whose wick pierces the morning high (bear) or low (bull)
  AND whose close is back INSIDE the range.
Entry = sweep candle close.
SL    = wick extreme + sl_atr_mult × ATR.
TP    = opposite end of morning range OR midpoint (EQ).
Only the first sweep per day is taken.
"""

import sys, statistics
from pathlib import Path
from datetime import datetime, timezone, date
from collections import defaultdict
from itertools import product

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from db.local_db import LocalDB
from analysis.smc_analyzer import SMCAnalyzer

DB_PATH = str(Path(__file__).resolve().parents[1] / "src" / "data" / "trade.db")

_UTC = timezone.utc

# ── Time constants (all UTC) ───────────────────────────────────────────────────
MORNING_H,  MORNING_M  =  7,  0   # Frankfurt open  → start of morning range
RANGE_END_H, RANGE_END_M = 11, 0  # 2.5h before US open → end of morning range
SWEEP_END_H, SWEEP_END_M = 13, 30 # US open          → end of sweep window
EVAL_END_H, EVAL_END_M   = 15, 30 # Frankfurt close  → end of evaluation

MIN_MORNING_BARS = 6   # skip days with too little morning data

# ── Parameter grid ─────────────────────────────────────────────────────────────
MIN_WICK_PTS_LIST = [0, 5, 10, 15]      # min wick extension beyond level (pts)
MIN_RANGE_ATR_LIST = [0.0, 0.5, 1.0]   # min morning range size in ×ATR
SL_ATR_MULT_LIST   = [0.10, 0.25, 0.50]
TP_MODES           = ["opposite", "eq"] # eq = midpoint of morning range


def _ts(d: date, h: int, m: int) -> int:
    return int(datetime(d.year, d.month, d.day, h, m, tzinfo=_UTC).timestamp())


def _atr(bars: list, period: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    trs = [max(b["high"] - b["low"],
               abs(b["high"] - bars[i-1]["close"]),
               abs(b["low"]  - bars[i-1]["close"]))
           for i, b in enumerate(bars[1:], 1)]
    return statistics.mean(trs[-period:]) if trs else 0.0


def find_first_sweep(bars_sweep_window, morning_high, morning_low, min_wick_pts):
    """Return (direction, bar, wick_extension) or None."""
    for bar in bars_sweep_window:
        bear = bar["high"] > morning_high and bar["close"] <= morning_high
        bull = bar["low"]  < morning_low  and bar["close"] >= morning_low
        if bear:
            ext = bar["high"] - morning_high
            if ext >= min_wick_pts:
                return "bear", bar, ext
        elif bull:
            ext = morning_low - bar["low"]
            if ext >= min_wick_pts:
                return "bull", bar, ext
    return None


def evaluate(entry, sl, tp, is_short, post_bars):
    risk = abs(entry - sl) or 1e-6
    rwd  = abs(entry - tp)
    for bar in post_bars:
        if is_short:
            if bar["high"] >= sl: return "LOSS", -1.0
            if bar["low"]  <= tp: return "WIN",  rwd / risk
        else:
            if bar["low"]  <= sl: return "LOSS", -1.0
            if bar["high"] >= tp: return "WIN",  rwd / risk
    return "OPEN", None


# ── Load data ──────────────────────────────────────────────────────────────────

print("Loading GER40 5m data from DB …")
db = LocalDB(DB_PATH)

all5m_desc = db.query_recent("GER40", "5m", limit=130_000)
all5m = list(reversed(all5m_desc))   # oldest → newest
print(f"  {len(all5m)} bars loaded")

dates = sorted(set(
    datetime.fromtimestamp(c["timestamp"], tz=_UTC).date()
    for c in all5m
))
dates = [d for d in dates if d.weekday() < 5]   # weekdays only
print(f"  {len(dates)} trading days\n")

# ── Pre-slice sessions once ────────────────────────────────────────────────────

sessions = []
for d in dates:
    ts_morning_start = _ts(d, MORNING_H,   MORNING_M)
    ts_range_end     = _ts(d, RANGE_END_H, RANGE_END_M)
    ts_us_open       = _ts(d, SWEEP_END_H, SWEEP_END_M)
    ts_eval_end      = _ts(d, EVAL_END_H,  EVAL_END_M)

    morning_bars = [c for c in all5m
                    if ts_morning_start <= c["timestamp"] < ts_range_end]
    sweep_bars   = [c for c in all5m
                    if ts_range_end    <= c["timestamp"] < ts_us_open]
    eval_bars    = [c for c in all5m
                    if ts_us_open      <= c["timestamp"] <= ts_eval_end]

    if len(morning_bars) < MIN_MORNING_BARS:
        continue

    morning_high = max(c["high"]  for c in morning_bars)
    morning_low  = min(c["low"]   for c in morning_bars)
    atr_val      = _atr(morning_bars) or 20.0

    sessions.append({
        "date":         d,
        "morning_high": morning_high,
        "morning_low":  morning_low,
        "morning_range": morning_high - morning_low,
        "atr":          atr_val,
        "sweep_bars":   sweep_bars,
        "eval_bars":    eval_bars,
    })

print(f"Valid sessions: {len(sessions)}\n")


# ── Parameter sweep ────────────────────────────────────────────────────────────

def run_params(min_wick_pts, min_range_atr, sl_mult, tp_mode):
    outcomes = []
    for s in sessions:
        if s["morning_range"] < min_range_atr * s["atr"]:
            continue
        result = find_first_sweep(
            s["sweep_bars"], s["morning_high"], s["morning_low"], min_wick_pts
        )
        if result is None:
            continue
        direction, bar, wick_ext = result
        is_short = direction == "bear"
        entry = bar["close"]
        sl    = (bar["high"] + sl_mult * s["atr"]) if is_short \
                else (bar["low"]  - sl_mult * s["atr"])
        if tp_mode == "opposite":
            tp = s["morning_low"]  if is_short else s["morning_high"]
        else:  # eq
            tp = (s["morning_high"] + s["morning_low"]) / 2

        # TP must be on the right side of entry
        if is_short and tp >= entry: continue
        if not is_short and tp <= entry: continue

        oc, r = evaluate(entry, sl, tp, is_short, s["eval_bars"])
        outcomes.append((oc, r))
    return outcomes


def summarise(outcomes):
    closed = [(o, r) for o, r in outcomes if o != "OPEN"]
    if not closed:
        return None
    wins = [(o, r) for o, r in closed if o == "WIN"]
    wr   = len(wins) / len(closed)
    avg_r = statistics.mean(r for _, r in closed)
    win_r = statistics.mean(r for _, r in wins) if wins else 0.0
    ev    = wr * win_r - (1 - wr) * 1.0
    return {
        "n_sig":  len(outcomes),
        "n_cl":   len(closed),
        "n_op":   len(outcomes) - len(closed),
        "wr":     wr,
        "avg_r":  avg_r,
        "ev":     ev,
    }


combos = list(product(MIN_WICK_PTS_LIST, MIN_RANGE_ATR_LIST,
                      SL_ATR_MULT_LIST, TP_MODES))
print(f"Running {len(combos)} param combinations …")

results = []
for wick, rng, sl, tp in combos:
    outs = run_params(wick, rng, sl, tp)
    s = summarise(outs)
    if s and s["n_cl"] >= 10:
        results.append({"wick": wick, "rng": rng, "sl": sl, "tp": tp, **s})

results.sort(key=lambda x: x["ev"], reverse=True)
print(f"Done — {len(results)} valid combos (≥10 closed)\n")

# ── Top combos ─────────────────────────────────────────────────────────────────

print("=" * 80)
print("PATTERN A — top 20 by EV  (min 10 closed trades)")
print("=" * 80)
print(f"\n  {'wick':>5}  {'rng':>5}  {'sl':>5}  {'tp':>8}  "
      f"{'N':>4}  {'WR':>5}  {'avg_R':>6}  {'EV':>6}  {'Open':>5}")
print("  " + "─" * 65)
for r in results[:20]:
    print(f"  {r['wick']:>5}  {r['rng']:>5.1f}  {r['sl']:>5.2f}  {r['tp']:>8}  "
          f"{r['n_cl']:>4}  {r['wr']:>5.0%}  {r['avg_r']:>+6.2f}R  "
          f"{r['ev']:>+6.2f}R  {r['n_op']:>5}")

# ── Timing + direction split for baseline params ───────────────────────────────

if results:
    best = results[0]
    print(f"\n\nBEST: wick≥{best['wick']}pts  range≥{best['rng']}×ATR  "
          f"sl={best['sl']}×ATR  tp={best['tp']}")
    print(f"      EV={best['ev']:+.2f}R  WR={best['wr']:.0%}  "
          f"N={best['n_cl']} closed  {best['n_op']} open\n")

# Use baseline params for detail analysis
BASE = {"wick": 0, "rng": 0.5, "sl": 0.25, "tp": "opposite"}
print(f"{'='*72}")
print(f"DETAIL — baseline params (wick≥{BASE['wick']}  range≥{BASE['rng']}×ATR  "
      f"sl={BASE['sl']}×ATR  tp={BASE['tp']})")
print(f"{'='*72}")

sweep_times = defaultdict(int)
bear_outcomes, bull_outcomes = [], []
detail_rows = []

for s in sessions:
    if s["morning_range"] < BASE["rng"] * s["atr"]:
        continue
    result = find_first_sweep(
        s["sweep_bars"], s["morning_high"], s["morning_low"], BASE["wick"]
    )
    if result is None:
        continue
    direction, bar, wick_ext = result
    is_short = direction == "bear"
    entry = bar["close"]
    sl    = (bar["high"] + BASE["sl"] * s["atr"]) if is_short \
            else (bar["low"]  - BASE["sl"] * s["atr"])
    tp    = s["morning_low"] if is_short else s["morning_high"]
    if is_short and tp >= entry: continue
    if not is_short and tp <= entry: continue

    rr_ratio = abs(tp - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0

    oc, r = evaluate(entry, sl, tp, is_short, s["eval_bars"])
    bar_time = datetime.fromtimestamp(bar["timestamp"], tz=_UTC).strftime("%H:%M")
    sweep_times[bar_time] += 1

    pts = abs(entry - tp) if oc == "WIN" else (-abs(entry - sl) if oc == "LOSS" else None)
    row = {
        "date": s["date"], "dir": "SHORT" if is_short else "LONG",
        "sweep_t": bar_time,
        "morning_range": s["morning_range"], "atr": s["atr"],
        "wick_ext": wick_ext,
        "entry": entry, "sl": sl, "tp": tp, "rr": rr_ratio,
        "outcome": oc, "r": r, "pts": pts,
    }
    detail_rows.append(row)
    if is_short: bear_outcomes.append((oc, r))
    else:        bull_outcomes.append((oc, r))

# Direction split summary
def dir_summary(label, outs):
    cl = [(o, r) for o, r in outs if o != "OPEN"]
    if not cl: return
    wins = [r for o, r in cl if o == "WIN"]
    wr = len(wins) / len(cl)
    avg_r = statistics.mean(r for _, r in cl)
    ev = wr * (statistics.mean(wins) if wins else 0) - (1-wr) * 1.0
    print(f"  {label:5}: {len(outs):3} signals  {len(cl):3} closed  "
          f"WR {wr:.0%}  avg_R {avg_r:+.2f}R  EV {ev:+.2f}R")

print(f"\nDirection split (baseline params):")
dir_summary("BEAR", bear_outcomes)
dir_summary("BULL", bull_outcomes)

# Timing distribution
print(f"\nSweep time distribution (UTC):")
for t in sorted(sweep_times):
    bar_s = "█" * sweep_times[t]
    print(f"  {t}  {bar_s}  ({sweep_times[t]})")

# Per-trade table
closed = [r for r in detail_rows if r["outcome"] != "OPEN"]
wins   = [r for r in closed if r["outcome"] == "WIN"]
pts_cl = [r["pts"] for r in closed if r["pts"] is not None]

print(f"\n\n{'─'*95}")
print(f"Per-trade  ({len(detail_rows)} signals  {len(closed)} closed  "
      f"{len(wins)} wins  {len(closed)-len(wins)} losses)")
print(f"{'─'*95}")
print(f"  {'#':>3}  {'Date':>10}  {'Dir':>5}  {'T':>5}  "
      f"{'MRng':>6}  {'Wick':>5}  {'Entry':>7}  {'TP':>7}  {'SL$':>5}  "
      f"{'R:R':>4}  {'Oc':>5}  {'R':>6}  {'Pts':>6}")
print(f"  {'─'*92}")
cum = 0.0
for i, r in enumerate(detail_rows, 1):
    r_s   = f"{r['r']:>+5.2f}R" if r["r"]   is not None else "  OPEN"
    pts_s = f"{r['pts']:>+5.1f}" if r["pts"] is not None else "   ---"
    if r["pts"] is not None: cum += r["pts"]
    sl_dist = abs(r["entry"] - r["sl"])
    print(f"  {i:>3}  {str(r['date']):>10}  {r['dir']:>5}  {r['sweep_t']:>5}  "
          f"{r['morning_range']:>6.0f}  {r['wick_ext']:>5.1f}  "
          f"{r['entry']:>7.1f}  {r['tp']:>7.1f}  ${sl_dist:>4.0f}  "
          f"{r['rr']:>4.1f}  {r['outcome']:>5}  {r_s}  {pts_s}")

if pts_cl:
    print(f"\n  Avg pts (closed): {statistics.mean(pts_cl):+.1f}")
    print(f"  Total pts:        {cum:+.1f}")

# Wick extension vs WR
print(f"\n\nWick extension → WR (baseline, all directions):")
buckets = defaultdict(list)
for r in detail_rows:
    if r["r"] is not None:
        bucket = int(r["wick_ext"] // 5) * 5
        buckets[bucket].append(r["outcome"])
for b in sorted(buckets):
    outs = buckets[b]
    wins_b = sum(1 for o in outs if o == "WIN")
    wr_b = wins_b / len(outs) if outs else 0
    print(f"  wick {b:>3}–{b+4:<3}pts : {len(outs):>3} trades  WR {wr_b:.0%}")

print("\nDone.")
