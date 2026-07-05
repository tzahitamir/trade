#!/usr/bin/env python3
"""
DAX Continuation v2 — Confirmed breakout entry.

Change from v1:
  Entry  : first 5m candle AFTER the sweep that CLOSES above morning_high
           (confirms the breakout is real, not just a wick)
  SL     : morning_high - sl_pts  (the level is now support)
  TP     : structural — previous day high (PDH), ATR multiples, range multiples

Sequence:
  1. Morning range 7:00–11:00 UTC (high/low defined)
  2. Sweep candle 11:00–13:30 UTC: wick ≥ min_wick pts beyond level, closes inside
  3. Confirmation: next candle(s) until 13:30 — first one that closes ABOVE morning_high
     (or BELOW morning_low for SHORT)  → that is the entry candle
  4. Entry at confirmation candle close
  5. SL = morning_high − sl_pts  (for LONG)
  6. TP = structural target above morning_high
  7. Evaluate until 15:30 UTC

Parameter grid:
  min_wick  [5, 7, 10, 15]        pts wick extension
  sl_pts    [5, 10, 15, 20, 25]   pts below morning_high for SL
  tp_mode   pdh  | atr×{0.25,0.5,0.75,1.0} | rng×{0.10,0.20,0.30,0.50}
"""

import sys, statistics
from pathlib import Path
from datetime import datetime, timezone, date, timedelta
from collections import defaultdict
from itertools import product

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from db.local_db import LocalDB

DB_PATH = str(Path(__file__).resolve().parents[1] / "src" / "data" / "trade.db")
_UTC = timezone.utc

MORNING_H,  MORNING_M  =  7,  0
RANGE_END_H, RANGE_END_M = 11,  0
SWEEP_END_H, SWEEP_END_M = 13, 30
CUTOFF_H,   CUTOFF_M   = 15, 30

MIN_MORNING_BARS = 6
MIN_RANGE_ATR    = 0.5

MIN_WICK_LIST = [5, 7, 10, 15]
SL_PTS_LIST   = [5, 10, 15, 20, 25]
TP_MODES      = (
    ["pdh"] +
    [f"atr_{m}" for m in [0.25, 0.50, 0.75, 1.0]] +
    [f"rng_{p}" for p in [0.10, 0.20, 0.30, 0.50]]
)


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

# Previous-day high/low lookup
prev_day_hl = {}   # date → (pdh, pdl)
for i, d in enumerate(dates):
    if i == 0: continue
    pd = dates[i-1]
    pd_start = _ts(pd, 0, 0)
    pd_end   = _ts(pd, 23, 59)
    pd_bars  = [b for b in all5m if pd_start <= b["timestamp"] <= pd_end]
    if pd_bars:
        prev_day_hl[d] = (max(b["high"] for b in pd_bars),
                          min(b["low"]  for b in pd_bars))

# ── Build sweep + confirmation records ───────────────────────────────────────

print("Building records …")
records = []

for d in dates:
    ts_ms = _ts(d, MORNING_H,    MORNING_M)
    ts_re = _ts(d, RANGE_END_H,  RANGE_END_M)
    ts_se = _ts(d, SWEEP_END_H,  SWEEP_END_M)
    ts_co = _ts(d, CUTOFF_H,     CUTOFF_M)

    morning_bars = [c for c in all5m if ts_ms <= c["timestamp"] < ts_re]
    sweep_bars   = [c for c in all5m if ts_re  <= c["timestamp"] < ts_se]
    day5m        = [c for c in all5m if ts_ms  <= c["timestamp"] <= ts_co]

    if len(morning_bars) < MIN_MORNING_BARS: continue

    mh  = max(c["high"]  for c in morning_bars)
    ml  = min(c["low"]   for c in morning_bars)
    rng = mh - ml
    atr = _atr(morning_bars) or 20.0
    if rng < MIN_RANGE_ATR * atr: continue

    # First sweep
    sweep_bar = None
    is_long   = None
    for bar in sweep_bars:
        if bar["high"] > mh and bar["close"] <= mh:
            sweep_bar = bar; is_long = True; break    # high sweep → LONG
        if bar["low"]  < ml and bar["close"] >= ml:
            sweep_bar = bar; is_long = False; break   # low sweep  → SHORT

    if sweep_bar is None: continue

    sweep_dt = datetime.fromtimestamp(sweep_bar["timestamp"], tz=_UTC)
    wick_ext = (sweep_bar["high"] - mh) if is_long else (ml - sweep_bar["low"])
    level    = mh if is_long else ml

    # Confirmation: first candle after sweep that closes ABOVE mh (LONG) / BELOW ml (SHORT)
    post_sweep = [b for b in day5m if b["timestamp"] > sweep_bar["timestamp"]]
    conf_bar = None
    for bar in post_sweep:
        if bar["timestamp"] >= ts_se: break  # no entry after sweep window
        if is_long  and bar["close"] > mh: conf_bar = bar; break
        if not is_long and bar["close"] < ml: conf_bar = bar; break

    if conf_bar is None: continue  # no confirmed breakout → skip

    conf_dt = datetime.fromtimestamp(conf_bar["timestamp"], tz=_UTC)
    entry   = conf_bar["close"]

    # Post-confirmation bars for evaluation
    post_conf = [b for b in day5m if b["timestamp"] > conf_bar["timestamp"]]

    # PDH/PDL
    pdh, pdl = prev_day_hl.get(d, (None, None))

    records.append({
        "date":       d,
        "is_long":    is_long,
        "level":      level,
        "mh":         mh,
        "ml":         ml,
        "range":      rng,
        "atr":        atr,
        "wick_ext":   wick_ext,
        "sweep_dt":   sweep_dt,
        "sweep_min":  sweep_dt.hour * 60 + sweep_dt.minute,
        "sweep_hhmm": sweep_dt.strftime("%H:%M"),
        "conf_dt":    conf_dt,
        "conf_hhmm":  conf_dt.strftime("%H:%M"),
        "entry":      entry,
        "pdh":        pdh,
        "pdl":        pdl,
        "post_conf":  post_conf,
    })

print(f"  {len(records)} sweep+confirmation events\n")
print(f"  (of {sum(1 for d in dates if any(s['date']==d for s in records))} sweep days "
      f"→ {len(records)/160*100:.0f}% get a confirmation)\n")


# ── Core evaluator ────────────────────────────────────────────────────────────

def get_tp(rec, tp_mode):
    mh, ml = rec["mh"], rec["ml"]
    rng, atr = rec["range"], rec["atr"]
    if tp_mode == "pdh":
        return rec["pdh"] if rec["is_long"] else rec["pdl"]
    if tp_mode.startswith("atr_"):
        mult = float(tp_mode[4:])
        return (mh + mult * atr) if rec["is_long"] else (ml - mult * atr)
    if tp_mode.startswith("rng_"):
        pct = float(tp_mode[4:])
        return (mh + pct * rng) if rec["is_long"] else (ml - pct * rng)
    return None


def evaluate_record(rec, sl_pts, tp_price):
    entry   = rec["entry"]
    is_long = rec["is_long"]
    level   = rec["level"]

    sl_price = (level - sl_pts) if is_long else (level + sl_pts)
    if tp_price is None: return None
    if is_long  and (tp_price <= entry or tp_price <= level): return None
    if not is_long and (tp_price >= entry or tp_price >= level): return None

    risk   = abs(entry - sl_price)
    reward = abs(entry - tp_price)
    if risk < 0.5: return None
    rr = reward / risk

    for bar in rec["post_conf"]:
        if is_long:
            if bar["low"]  <= sl_price: return {"oc":"LOSS","r":-1.0,"rr":rr,"risk":risk,"rwd":reward}
            if bar["high"] >= tp_price: return {"oc":"WIN", "r": rr, "rr":rr,"risk":risk,"rwd":reward}
        else:
            if bar["high"] >= sl_price: return {"oc":"LOSS","r":-1.0,"rr":rr,"risk":risk,"rwd":reward}
            if bar["low"]  <= tp_price: return {"oc":"WIN", "r": rr, "rr":rr,"risk":risk,"rwd":reward}
    return {"oc":"OPEN","r":None,"rr":rr,"risk":risk,"rwd":reward}


def summarise(results):
    closed = [r for r in results if r["oc"] != "OPEN"]
    if len(closed) < 10: return None
    wins = [r for r in closed if r["oc"] == "WIN"]
    wr   = len(wins) / len(closed)
    avg_rr = statistics.mean(r["rr"] for r in wins) if wins else 0
    ev = wr * avg_rr - (1 - wr) * 1.0
    avg_pts = (wr * statistics.mean(r["rwd"] for r in wins) -
               (1-wr)*statistics.mean(r["risk"] for r in closed if r["oc"]=="LOSS")) if wins else -1
    return {"n_cl": len(closed), "n_op": len(results)-len(closed),
            "wr": wr, "avg_rr": avg_rr, "ev": ev, "avg_pts": avg_pts}


# ── Parameter sweep ───────────────────────────────────────────────────────────

print("Running parameter sweep …")
sweep_out = []
for wick, sl_p, tp_m in product(MIN_WICK_LIST, SL_PTS_LIST, TP_MODES):
    results = []
    for rec in records:
        if rec["wick_ext"] < wick: continue
        tp = get_tp(rec, tp_m)
        r  = evaluate_record(rec, sl_p, tp)
        if r: results.append(r)
    sm = summarise(results)
    if sm: sweep_out.append({"wick":wick,"sl_pts":sl_p,"tp_mode":tp_m,**sm})

sweep_out.sort(key=lambda x: x["ev"], reverse=True)
print(f"Done — {len(sweep_out)} valid combos\n")


# ── Top combos ────────────────────────────────────────────────────────────────

print("=" * 82)
print("CONFIRMED ENTRY — top 30 by EV  (≥10 closed)")
print("=" * 82)
print(f"\n  {'wick':>4}  {'SL pts':>6}  {'TP mode':>12}  {'N':>4}  "
      f"{'WR':>5}  {'avgR:R':>7}  {'EV':>7}  {'avg_pts':>8}  {'Open':>5}")
print("  " + "─" * 72)
for r in sweep_out[:30]:
    print(f"  {r['wick']:>4}  {r['sl_pts']:>6}  {r['tp_mode']:>12}  {r['n_cl']:>4}  "
          f"{r['wr']:>5.0%}  {r['avg_rr']:>7.2f}R  {r['ev']:>+7.2f}R  "
          f"{r['avg_pts']:>+8.1f}pt  {r['n_op']:>5}")

best = sweep_out[0]
print(f"\nBest: wick≥{best['wick']}pts  SL={best['sl_pts']}pts below level  "
      f"TP={best['tp_mode']}  EV={best['ev']:+.2f}R  WR={best['wr']:.0%}")


# ── Confirmation lag ──────────────────────────────────────────────────────────

print(f"\n\n{'='*68}")
print("CONFIRMATION LAG — how many 5m bars after sweep until breakout confirmed?")
print(f"{'='*68}")
lags = []
for rec in records:
    post = [b for b in rec["post_conf"]]
    # lag = bars between sweep and confirmation
    lags.append((rec["conf_dt"].hour*60+rec["conf_dt"].minute
                  - rec["sweep_dt"].hour*60-rec["sweep_dt"].minute) // 5)
for lo, hi, label in [(0,1,"same bar or next (0–5min)"),(1,3,"1–2 bars (5–15min)"),
                       (3,7,"3–6 bars (15–30min)"),(7,999,"7+ bars (35min+)")]:
    sub = [l for l in lags if lo <= l < hi]
    print(f"  {label:<30}: {len(sub):>3} ({len(sub)/len(lags):.0%})")


# ── Time filter breakdown ─────────────────────────────────────────────────────

print(f"\n\n{'='*68}")
print(f"SWEEP TIME VS EV  (best params: wick≥{best['wick']}, "
      f"SL={best['sl_pts']}pts, TP={best['tp_mode']})")
print(f"{'='*68}")
print(f"\n  {'Window':>20}  {'N':>4}  {'WR':>5}  {'avgR:R':>7}  {'EV':>7}")
print("  " + "─" * 52)
for t0, t1, label in [(660,690,"11:00–11:30"),(690,720,"11:30–12:00"),
                       (720,750,"12:00–12:30"),(750,810,"12:30–13:30")]:
    sub = [rec for rec in records
           if t0 <= rec["sweep_min"] < t1 and rec["wick_ext"] >= best["wick"]]
    results = []
    for rec in sub:
        tp = get_tp(rec, best["tp_mode"])
        r  = evaluate_record(rec, best["sl_pts"], tp)
        if r: results.append(r)
    cl = [r for r in results if r["oc"] != "OPEN"]
    if not cl: continue
    w = [r for r in cl if r["oc"]=="WIN"]
    wr = len(w)/len(cl)
    avg_rr = statistics.mean(r["rr"] for r in w) if w else 0
    ev = wr*avg_rr-(1-wr)*1.0
    print(f"  {label:>20}: {len(cl):>4}  {wr:>5.0%}  {avg_rr:>7.2f}R  {ev:>+7.2f}R")


# ── Time-filtered best params ─────────────────────────────────────────────────

GOOD_WINDOWS = [(660, 690), (720, 750)]   # 11:00–11:30 and 12:00–12:30

print(f"\n\n{'='*68}")
print("WITH TIME FILTER (11:00–11:30 + 12:00–12:30 UTC only)")
print(f"{'='*68}")
print(f"\n  {'wick':>4}  {'SL pts':>6}  {'TP mode':>12}  {'N':>4}  "
      f"{'WR':>5}  {'avgR:R':>7}  {'EV':>7}  {'Open':>5}")
print("  " + "─" * 60)

filtered_out = []
for wick, sl_p, tp_m in product(MIN_WICK_LIST, SL_PTS_LIST, TP_MODES):
    results = []
    for rec in records:
        if rec["wick_ext"] < wick: continue
        if not any(t0 <= rec["sweep_min"] < t1 for t0, t1 in GOOD_WINDOWS): continue
        tp = get_tp(rec, tp_m)
        r  = evaluate_record(rec, sl_p, tp)
        if r: results.append(r)
    sm = summarise(results)
    if sm: filtered_out.append({"wick":wick,"sl_pts":sl_p,"tp_mode":tp_m,**sm})

filtered_out.sort(key=lambda x: x["ev"], reverse=True)
for r in filtered_out[:15]:
    print(f"  {r['wick']:>4}  {r['sl_pts']:>6}  {r['tp_mode']:>12}  {r['n_cl']:>4}  "
          f"{r['wr']:>5.0%}  {r['avg_rr']:>7.2f}R  {r['ev']:>+7.2f}R  {r['n_op']:>5}")

if filtered_out:
    fb = filtered_out[0]
    print(f"\nBest (filtered): wick≥{fb['wick']}  SL={fb['sl_pts']}pts  "
          f"TP={fb['tp_mode']}  EV={fb['ev']:+.2f}R  WR={fb['wr']:.0%}  N={fb['n_cl']}")


# ── Monthly consistency — unfiltered best ─────────────────────────────────────

print(f"\n\n{'='*68}")
print(f"MONTHLY — best unfiltered (wick≥{best['wick']} SL={best['sl_pts']}pts TP={best['tp_mode']})")
print(f"{'='*68}")
month_trades = defaultdict(list)
for rec in records:
    if rec["wick_ext"] < best["wick"]: continue
    tp = get_tp(rec, best["tp_mode"])
    r  = evaluate_record(rec, best["sl_pts"], tp)
    if r and r["oc"] != "OPEN":
        month_trades[(rec["date"].year, rec["date"].month)].append(r)
for ym in sorted(month_trades):
    sub = month_trades[ym]
    w   = [r for r in sub if r["oc"]=="WIN"]
    wr  = len(w)/len(sub)
    ev  = wr*(statistics.mean(r["rr"] for r in w) if w else 0) - (1-wr)*1.0
    print(f"  {ym[0]}-{ym[1]:02d}  n={len(sub):>2}  WR={wr:.0%}  EV={ev:+.2f}R  "
          f"{'█'*len(w)}{'·'*(len(sub)-len(w))}")


# ── Per-trade detail — filtered best ─────────────────────────────────────────

if not filtered_out: sys.exit(0)
fb = filtered_out[0]

print(f"\n\n{'─'*105}")
print(f"Per-trade — filtered best: wick≥{fb['wick']}  SL={fb['sl_pts']}pts below level  "
      f"TP={fb['tp_mode']}  time=11:00-11:30 or 12:00-12:30")
print(f"{'─'*105}")
print(f"  {'#':>3}  {'Date':>10}  {'Dir':>5}  {'SweepT':>6}  {'ConfT':>5}  "
      f"{'Wick':>5}  {'Level':>7}  {'Entry':>7}  {'TP':>7}  {'SL':>7}  "
      f"{'Risk':>5}  {'R:R':>5}  {'Oc':>5}  {'R':>7}")
print(f"  {'─'*102}")

detail = []
for rec in records:
    if rec["wick_ext"] < fb["wick"]: continue
    if not any(t0 <= rec["sweep_min"] < t1 for t0, t1 in GOOD_WINDOWS): continue
    tp = get_tp(rec, fb["tp_mode"])
    r  = evaluate_record(rec, fb["sl_pts"], tp)
    if r is None: continue
    detail.append({**r, **{"date": rec["date"],
                            "dir":  "LONG" if rec["is_long"] else "SHORT",
                            "sweep_t": rec["sweep_hhmm"],
                            "conf_t":  rec["conf_hhmm"],
                            "wick": rec["wick_ext"],
                            "level": rec["level"],
                            "entry": rec["entry"],
                            "tp_price": tp,
                            "sl_price": (rec["level"]-fb["sl_pts"]) if rec["is_long"]
                                        else (rec["level"]+fb["sl_pts"])}})

closed = [d for d in detail if d["oc"]!="OPEN"]
wins   = [d for d in closed if d["oc"]=="WIN"]
cum = 0.0
for i, d in enumerate(detail, 1):
    r_s = f"{d['r']:>+6.2f}R" if d["r"] is not None else "   OPEN"
    if d["r"] is not None: cum += d["r"]
    print(f"  {i:>3}  {str(d['date']):>10}  {d['dir']:>5}  {d['sweep_t']:>6}  "
          f"{d['conf_t']:>5}  {d['wick']:>5.1f}  {d['level']:>7.1f}  "
          f"{d['entry']:>7.1f}  {d['tp_price']:>7.1f}  {d['sl_price']:>7.1f}  "
          f"{d['risk']:>5.0f}  {d['rr']:>5.2f}  {d['oc']:>5}  {r_s}")

if closed:
    wr = len(wins)/len(closed)
    avg_rr = statistics.mean(d["rr"] for d in wins) if wins else 0
    print(f"\n  Closed: {len(closed)}  Wins: {len(wins)}  WR: {wr:.0%}  "
          f"avg_win_R: {avg_rr:.2f}R  Cum R: {cum:+.2f}R  Avg: {cum/len(closed):+.2f}R")
    if len(detail) > len(closed):
        print(f"  Open: {len(detail)-len(closed)} (didn't resolve by 15:30)")

print("\nDone.")
