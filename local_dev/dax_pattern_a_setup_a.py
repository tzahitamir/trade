#!/usr/bin/env python3
"""
DAX Pattern A — Setup A deep analysis.

Question: what TP level (% of morning range from entry side) gives best EV?
SL = sweep extreme (tight) OR sweep extreme + 0.25×ATR (loose).

For each TP level (10% to 80% of morning range, step 5%):
  - WR: % of sweeps where price reaches TP before SL
  - Avg R:R per trade (varies because entry/SL distance differs each trade)
  - EV = WR × avg_win_R - (1-WR) × 1.0

Also shows distribution of max retrace depth so we know the "natural" floor.
"""

import sys, statistics
from pathlib import Path
from datetime import datetime, timezone, date
from collections import defaultdict

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

TP_LEVELS = [i/100 for i in range(10, 85, 5)]   # 10% to 80%, step 5%
SL_BUFFERS = [0.0, 0.25]                          # ×ATR added beyond sweep extreme


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

# ── Build per-sweep records ───────────────────────────────────────────────────

print("Building sweep records …")
sweeps = []

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
    for bar in sweep_bars:
        is_bear = bar["high"] > mh and bar["close"] <= mh
        is_bull = bar["low"]  < ml and bar["close"] >= ml
        if is_bear or is_bull:
            post = [b for b in day5m if b["timestamp"] > bar["timestamp"]]
            sweeps.append({
                "date":       d,
                "is_short":   is_bear,
                "sweep_bar":  bar,
                "entry":      bar["close"],
                "sweep_ext":  bar["high"] if is_bear else bar["low"],
                "wick_ext":   (bar["high"] - mh) if is_bear else (ml - bar["low"]),
                "morning_high": mh,
                "morning_low":  ml,
                "range":      rng,
                "atr":        atr,
                "post_bars":  post,
            })
            break

print(f"  {len(sweeps)} sweeps\n")


# ── For each sweep, compute max retrace depth before SL hit ──────────────────

def max_retrace_before_sl(s, sl_level):
    """
    Walk post bars. Return max retrace depth (% of morning range from entry side)
    reached before price hits sl_level.
    Also return whether SL was ever hit.
    """
    is_short  = s["is_short"]
    entry     = s["entry"]
    mh, ml    = s["morning_high"], s["morning_low"]
    rng       = s["range"]
    max_ret   = 0.0
    sl_hit    = False

    for b in s["post_bars"]:
        if is_short:
            if b["high"] >= sl_level:
                sl_hit = True
                break
            # retrace = price going DOWN from entry, expressed as % of range
            ret = (entry - b["low"]) / rng
        else:
            if b["low"] <= sl_level:
                sl_hit = True
                break
            ret = (b["high"] - entry) / rng
        max_ret = max(max_ret, ret)

    return max_ret, sl_hit


# ── Distribution of max retrace depth (SL = sweep extreme, no buffer) ────────

print("=" * 70)
print("MAX RETRACE DEPTH before SL hit (SL = sweep extreme, no buffer)")
print("= how far does price go in our favour before potentially stopping out")
print("=" * 70)

retrace_depths = []
for s in sweeps:
    sl = s["sweep_ext"]
    max_ret, sl_hit = max_retrace_before_sl(s, sl)
    retrace_depths.append((max_ret, sl_hit, s))

# Show distribution
buckets = defaultdict(int)
for mr, _, _ in retrace_depths:
    b = int(mr * 100 // 5) * 5   # 0, 5, 10, …
    buckets[b] += 1

print(f"\n  {'Retrace depth':>15}   {'N':>4}   bar")
for b in sorted(buckets):
    n = buckets[b]
    bar = "█" * n
    cumulative = sum(v for k, v in buckets.items() if k >= b)
    print(f"  {b:>3}–{b+4:<3}%  of range  {n:>4}   {bar}   (≥{b}%: {cumulative})")

pct_reaches_eq  = sum(1 for mr, _, _ in retrace_depths if mr >= 0.50) / len(retrace_depths)
pct_reaches_30  = sum(1 for mr, _, _ in retrace_depths if mr >= 0.30) / len(retrace_depths)
pct_reaches_40  = sum(1 for mr, _, _ in retrace_depths if mr >= 0.40) / len(retrace_depths)
pct_reaches_60  = sum(1 for mr, _, _ in retrace_depths if mr >= 0.60) / len(retrace_depths)
print(f"\n  Reach ≥30% before SL: {pct_reaches_30:.0%}")
print(f"  Reach ≥40% before SL: {pct_reaches_40:.0%}")
print(f"  Reach ≥50% (EQ):      {pct_reaches_eq:.0%}")
print(f"  Reach ≥60%:           {pct_reaches_60:.0%}")

median_depth = sorted(mr for mr, _, _ in retrace_depths)[len(retrace_depths)//2]
print(f"  Median max retrace:   {median_depth:.0%}")


# ── TP level sweep: WR, R:R, EV ──────────────────────────────────────────────

print(f"\n\n{'='*70}")
print("SETUP A — TP LEVEL SWEEP")
print("Entry = sweep candle close  |  SL = sweep extreme [+ buffer]")
print(f"{'='*70}")

for sl_buffer in SL_BUFFERS:
    label = f"SL = sweep extreme + {sl_buffer}×ATR" if sl_buffer else "SL = sweep extreme (no buffer)"
    print(f"\n  {label}")
    print(f"\n  {'TP%':>5}  {'WR':>6}  {'avgR:R':>7}  {'EV':>7}  "
          f"{'avg_pts':>8}  {'N_wins':>6}  {'N_loss':>6}  {'note'}")
    print("  " + "─" * 65)

    best_ev = -99
    best_tp = None
    for tp_pct in TP_LEVELS:
        wins, losses = [], []
        for s in sweeps:
            is_short = s["is_short"]
            entry    = s["entry"]
            rng      = s["range"]
            mh, ml   = s["morning_high"], s["morning_low"]
            atr      = s["atr"]
            sl       = s["sweep_ext"] + sl_buffer * atr * (1 if is_short else -1)

            # TP is tp_pct of the morning range FROM the entry side (inside the range)
            if is_short:
                tp = mh - tp_pct * rng   # going down from mh
            else:
                tp = ml + tp_pct * rng   # going up from ml

            # TP must be on the correct side of entry
            if is_short and tp >= entry: continue
            if not is_short and tp <= entry: continue
            # TP must be inside the morning range
            if is_short and tp < ml: continue
            if not is_short and tp > mh: continue

            risk = abs(entry - sl)
            if risk < 0.5: continue
            reward = abs(entry - tp)
            rr = reward / risk

            hit_tp = hit_sl = False
            for b in s["post_bars"]:
                if is_short:
                    if b["high"] >= sl: hit_sl = True; break
                    if b["low"]  <= tp: hit_tp = True; break
                else:
                    if b["low"]  <= sl: hit_sl = True; break
                    if b["high"] >= tp: hit_tp = True; break

            if hit_tp:
                wins.append({"rr": rr, "pts": reward})
            elif hit_sl:
                losses.append({"pts": risk})

        n_wins = len(wins)
        n_loss = len(losses)
        n_total = n_wins + n_loss
        if n_total < 10: continue

        wr  = n_wins / n_total
        avg_win_rr = statistics.mean(w["rr"] for w in wins) if wins else 0
        ev  = wr * avg_win_rr - (1 - wr) * 1.0
        avg_win_pts = statistics.mean(w["pts"] for w in wins) if wins else 0
        avg_los_pts = statistics.mean(l["pts"] for l in losses) if losses else 0
        avg_pts = wr * avg_win_pts - (1 - wr) * avg_los_pts

        note = ""
        if ev > best_ev:
            best_ev = ev; best_tp = tp_pct
            note = " ← best EV"

        print(f"  {tp_pct:>5.0%}  {wr:>6.0%}  {avg_win_rr:>7.2f}R  {ev:>+7.2f}R  "
              f"{avg_pts:>+8.1f}pt  {n_wins:>6}  {n_loss:>6}  {note}")

    print(f"\n  Best TP: {best_tp:.0%} of morning range  (EV={best_ev:+.2f}R)")


# ── Deep dive on best TP (tight SL) ──────────────────────────────────────────

BEST_TP_PCT = 0.30   # will override after sweep above; hardcode for deep dive

print(f"\n\n{'='*70}")
print(f"DEEP DIVE — TP={BEST_TP_PCT:.0%} of range, SL=sweep extreme, no buffer")
print(f"{'='*70}")

detail = []
for s in sweeps:
    is_short = s["is_short"]
    entry    = s["entry"]
    rng      = s["range"]
    mh, ml   = s["morning_high"], s["morning_low"]
    sl       = s["sweep_ext"]
    tp = (mh - BEST_TP_PCT * rng) if is_short else (ml + BEST_TP_PCT * rng)

    if is_short and tp >= entry: continue
    if not is_short and tp <= entry: continue
    if is_short and tp < ml: continue
    if not is_short and tp > mh: continue

    risk = abs(entry - sl); reward = abs(entry - tp)
    if risk < 0.5: continue
    rr = reward / risk

    hit_tp = hit_sl = False
    for b in s["post_bars"]:
        if is_short:
            if b["high"] >= sl: hit_sl = True; break
            if b["low"]  <= tp: hit_tp = True; break
        else:
            if b["low"]  <= sl: hit_sl = True; break
            if b["high"] >= tp: hit_tp = True; break

    outcome = "WIN" if hit_tp else ("LOSS" if hit_sl else "OPEN")
    r = rr if hit_tp else (-1.0 if hit_sl else None)
    pts = reward if hit_tp else (-risk if hit_sl else None)

    sweep_dt = datetime.fromtimestamp(s["sweep_bar"]["timestamp"], tz=_UTC)
    detail.append({
        "date":      s["date"],
        "dir":       "SHORT" if is_short else "LONG",
        "sweep_t":   sweep_dt.strftime("%H:%M"),
        "wick":      s["wick_ext"],
        "entry":     entry, "tp": tp, "sl": sl,
        "risk_pts":  risk, "rwd_pts": reward, "rr": rr,
        "outcome":   outcome, "r": r, "pts": pts,
    })

closed = [r for r in detail if r["outcome"] != "OPEN"]
wins   = [r for r in closed if r["outcome"] == "WIN"]
losses = [r for r in closed if r["outcome"] == "LOSS"]

print(f"\n  Signals: {len(detail)}  Closed: {len(closed)}  "
      f"Wins: {len(wins)}  Losses: {len(losses)}")
if closed:
    wr = len(wins)/len(closed)
    avg_rr = statistics.mean(r["rr"] for r in wins) if wins else 0
    ev = wr * avg_rr - (1-wr)*1.0
    avg_risk_pts = statistics.mean(r["risk_pts"] for r in closed)
    avg_rwd_pts  = statistics.mean(r["rwd_pts"]  for r in wins) if wins else 0
    print(f"  WR: {wr:.0%}  avg win R:R: {avg_rr:.2f}R  EV: {ev:+.2f}R")
    print(f"  Avg risk (SL dist): {avg_risk_pts:.1f} pts")
    print(f"  Avg reward (TP dist): {avg_rwd_pts:.1f} pts")

# By wick size
print(f"\n  By wick extension:")
for lo, hi, label in [(0,5,"0–5pt"),(5,10,"5–10pt"),(10,20,"10–20pt"),(20,999,"20+pt")]:
    sub_cl = [r for r in closed if lo <= r["wick"] < hi]
    if not sub_cl: continue
    sub_w  = [r for r in sub_cl if r["outcome"] == "WIN"]
    wr_b = len(sub_w)/len(sub_cl)
    avg_r = statistics.mean(r["rr"] for r in sub_w) if sub_w else 0
    ev_b  = wr_b * avg_r - (1-wr_b)*1.0
    print(f"    wick {label:<10}: n={len(sub_cl):>3}  WR={wr_b:.0%}  "
          f"avg_win_R={avg_r:.2f}  EV={ev_b:+.2f}R")

# By sweep time
print(f"\n  By sweep time (UTC):")
for t0, t1, label in [(660,690,"11:00-11:30"),(690,720,"11:30-12:00"),
                       (720,750,"12:00-12:30"),(750,810,"12:30-13:30")]:
    def sm(r):
        h, m = int(r["sweep_t"][:2]), int(r["sweep_t"][3:])
        return h*60+m
    sub_cl = [r for r in closed if t0 <= sm(r) < t1]
    if not sub_cl: continue
    sub_w  = [r for r in sub_cl if r["outcome"] == "WIN"]
    wr_b = len(sub_w)/len(sub_cl)
    avg_r = statistics.mean(r["rr"] for r in sub_w) if sub_w else 0
    ev_b  = wr_b * avg_r - (1-wr_b)*1.0
    print(f"    {label}: n={len(sub_cl):>3}  WR={wr_b:.0%}  "
          f"avg_win_R={avg_r:.2f}  EV={ev_b:+.2f}R")


# ── Per-trade table ───────────────────────────────────────────────────────────

print(f"\n\n{'─'*90}")
print(f"Per-trade detail (TP={BEST_TP_PCT:.0%}, SL=sweep extreme)")
print(f"{'─'*90}")
print(f"  {'#':>3}  {'Date':>10}  {'Dir':>5}  {'T':>5}  {'Wick':>5}  "
      f"{'Entry':>7}  {'TP':>7}  {'SL':>7}  {'Risk':>5}  {'R:R':>5}  {'Oc':>5}  {'R':>6}  {'Pts':>7}")
print(f"  {'─'*87}")
cum = 0.0
for i, r in enumerate(detail, 1):
    r_s   = f"{r['r']:>+5.2f}R" if r["r"]   is not None else "  OPEN"
    pts_s = f"{r['pts']:>+6.1f}" if r["pts"] is not None else "    ---"
    if r["pts"] is not None: cum += r["pts"]
    print(f"  {i:>3}  {str(r['date']):>10}  {r['dir']:>5}  {r['sweep_t']:>5}  "
          f"{r['wick']:>5.1f}  {r['entry']:>7.1f}  {r['tp']:>7.1f}  {r['sl']:>7.1f}  "
          f"{r['risk_pts']:>5.0f}  {r['rr']:>5.2f}  {r['outcome']:>5}  {r_s}  {pts_s}")

if closed:
    print(f"\n  Total pts (closed): {cum:+.1f}")

print("\nDone.")
