#!/usr/bin/env python3
"""
DAX Continuation v4 — Loss diagnosis + TP reach cascade.

Focus: wick≥7, pb=10%, time-filtered (11:00-11:30 + 12:00-12:30 UTC)
Answer:
  1. WHY do losses lose? SL too tight? Or TP too far?
  2. What % reach each structural level (TP reach cascade)?
  3. MAE / MFE distribution — is the SL in the right place?
  4. Optimal TP: what single exit level maximises EV?
  5. Tiered exit: half at morning_level, half at PDH — does it help?
  6. SL sensitivity: try 5, 10, 15, 20, 25, 30 pt SLs for this exact entry
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
CUTOFF_H,    CUTOFF_M    = 15, 30
GOOD_WINDOWS = [(660, 690), (720, 750)]   # 11:00–11:30, 12:00–12:30 UTC
MIN_MORNING_BARS = 6
MIN_RANGE_ATR    = 0.5

ENTRY_PCT  = 0.10    # 10% pullback from morning level
SL_PTS_REF = 15      # reference SL for main analysis


def _ts(d: date, h: int, m: int) -> int:
    return int(datetime(d.year, d.month, d.day, h, m, tzinfo=_UTC).timestamp())

def _atr(bars, period=14):
    if len(bars) < 2: return 0.0
    trs = [max(b["high"] - b["low"],
               abs(b["high"] - bars[i-1]["close"]),
               abs(b["low"]  - bars[i-1]["close"]))
           for i, b in enumerate(bars[1:], 1)]
    return statistics.mean(trs[-period:]) if trs else 0.0


print("Loading GER40 5m data …")
db = LocalDB(DB_PATH)
all5m = list(reversed(db.query_recent("GER40", "5m", limit=130_000)))
dates = sorted(set(datetime.fromtimestamp(c["timestamp"], tz=_UTC).date() for c in all5m))
dates = [d for d in dates if d.weekday() < 5]

prev_hl = {}
for i, d in enumerate(dates):
    if i == 0: continue
    pd = dates[i-1]
    pd_bars = [b for b in all5m if _ts(pd,0,0) <= b["timestamp"] <= _ts(pd,23,59)]
    if pd_bars:
        prev_hl[d] = (max(b["high"] for b in pd_bars), min(b["low"] for b in pd_bars))

print("Building sweep records …")
sweeps_all = []
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
            sweeps_all.append({
                "date": d, "is_long": is_long, "level": level,
                "mh": mh, "ml": ml, "range": rng, "atr": atr,
                "wick_ext": wick_ext,
                "sweep_min": sweep_dt.hour*60 + sweep_dt.minute,
                "sweep_hhmm": sweep_dt.strftime("%H:%M"),
                "pdh": pdh, "pdl": pdl, "post": post,
            })
            break

# Apply filters: wick≥7 + time window
sweeps = [s for s in sweeps_all if s["wick_ext"] >= 7
          and any(t0 <= s["sweep_min"] < t1 for t0,t1 in GOOD_WINDOWS)]
print(f"  All sweeps: {len(sweeps_all)}   Filtered (wick≥7 + time): {len(sweeps)}\n")


# ── Build detailed trade record ───────────────────────────────────────────────

def build_trade(s, sl_pts, tp_mode="pdh"):
    is_long = s["is_long"]
    level   = s["level"]
    rng     = s["range"]

    el = (level - ENTRY_PCT * rng) if is_long else (level + ENTRY_PCT * rng)
    sl = el - sl_pts if is_long else el + sl_pts

    pdh, pdl = s["pdh"], s["pdl"]
    if tp_mode == "pdh":
        tp = pdh if is_long else pdl
        if tp is None: return None
    elif tp_mode == "level":
        tp = level
    elif tp_mode.startswith("rng_"):
        ext = float(tp_mode[4:])
        tp = (level + ext * rng) if is_long else (level - ext * rng)
    else:
        return None

    if is_long  and tp <= el: return None
    if not is_long and tp >= el: return None

    risk   = abs(el - sl)
    reward = abs(el - tp)
    if risk < 0.5: return None
    rr = reward / risk

    # Structural checkpoints between entry and TP
    chk_level   = level
    chk_rng_010 = (level + 0.10*rng) if is_long else (level - 0.10*rng)
    chk_rng_025 = (level + 0.25*rng) if is_long else (level - 0.25*rng)
    chk_rng_050 = (level + 0.50*rng) if is_long else (level - 0.50*rng)

    entry_filled = False
    mae = 0.0   # max adverse excursion (pts against us from entry)
    mfe = 0.0   # max favourable excursion (pts in our favour from entry)
    hit_level = hit_rng010 = hit_rng025 = hit_rng050 = hit_tp = False

    for bar in s["post"]:
        if not entry_filled:
            if is_long  and bar["high"] >= tp: return None  # went straight to TP before fill
            if not is_long and bar["low"]  <= tp: return None
            if is_long  and bar["low"]  <= el: entry_filled = True
            if not is_long and bar["high"] >= el: entry_filled = True
            if not entry_filled: continue

        # Update MAE / MFE
        if is_long:
            bar_adv = el - bar["low"]   # adverse = how far below entry
            bar_fav = bar["high"] - el  # favourable = how far above entry
        else:
            bar_adv = bar["high"] - el
            bar_fav = el - bar["low"]
        mae = max(mae, bar_adv)
        mfe = max(mfe, bar_fav)

        # Checkpoint hits
        if is_long:
            if bar["high"] >= chk_level:    hit_level   = True
            if bar["high"] >= chk_rng_010:  hit_rng010  = True
            if bar["high"] >= chk_rng_025:  hit_rng025  = True
            if bar["high"] >= chk_rng_050:  hit_rng050  = True
        else:
            if bar["low"]  <= chk_level:    hit_level   = True
            if bar["low"]  <= chk_rng_010:  hit_rng010  = True
            if bar["low"]  <= chk_rng_025:  hit_rng025  = True
            if bar["low"]  <= chk_rng_050:  hit_rng050  = True

        # Outcome
        if is_long:
            if bar["low"]  <= sl:
                return {"oc":"LOSS","r":-1.0,"rr":rr,"risk":risk,"rwd":reward,
                        "mae":mae,"mfe":mfe,"hit_level":hit_level,
                        "hit_rng010":hit_rng010,"hit_rng025":hit_rng025,"hit_rng050":hit_rng050,
                        "hit_tp":False, "el":el,"sl":sl,"tp":tp,"rng":rng,"level":level}
            if bar["high"] >= tp:
                hit_tp = True
                return {"oc":"WIN","r":rr,"rr":rr,"risk":risk,"rwd":reward,
                        "mae":mae,"mfe":mfe,"hit_level":True,"hit_rng010":True,
                        "hit_rng025":True,"hit_rng050":hit_rng050,
                        "hit_tp":True, "el":el,"sl":sl,"tp":tp,"rng":rng,"level":level}
        else:
            if bar["high"] >= sl:
                return {"oc":"LOSS","r":-1.0,"rr":rr,"risk":risk,"rwd":reward,
                        "mae":mae,"mfe":mfe,"hit_level":hit_level,
                        "hit_rng010":hit_rng010,"hit_rng025":hit_rng025,"hit_rng050":hit_rng050,
                        "hit_tp":False, "el":el,"sl":sl,"tp":tp,"rng":rng,"level":level}
            if bar["low"]  <= tp:
                hit_tp = True
                return {"oc":"WIN","r":rr,"rr":rr,"risk":risk,"rwd":reward,
                        "mae":mae,"mfe":mfe,"hit_level":True,"hit_rng010":True,
                        "hit_rng025":True,"hit_rng050":hit_rng050,
                        "hit_tp":True, "el":el,"sl":sl,"tp":tp,"rng":rng,"level":level}

    if entry_filled:
        return {"oc":"OPEN","r":None,"rr":rr,"risk":risk,"rwd":reward,
                "mae":mae,"mfe":mfe,"hit_level":hit_level,
                "hit_rng010":hit_rng010,"hit_rng025":hit_rng025,"hit_rng050":hit_rng050,
                "hit_tp":False, "el":el,"sl":sl,"tp":tp,"rng":rng,"level":level}
    return None


# ── SECTION 1: TP reach cascade (no SL) ──────────────────────────────────────

print("=" * 72)
print("SECTION 1 — TP REACH CASCADE  (no SL, what % reach each level?)")
print("=" * 72)

def reach_pct(s, target_fn):
    el = (s["level"] - ENTRY_PCT * s["range"]) if s["is_long"] else (s["level"] + ENTRY_PCT * s["range"])
    entry_filled = False
    for bar in s["post"]:
        if not entry_filled:
            if s["is_long"]  and bar["low"]  <= el: entry_filled = True
            if not s["is_long"] and bar["high"] >= el: entry_filled = True
            if not entry_filled: continue
        tgt = target_fn(s)
        if tgt is None: return None
        if s["is_long"]  and bar["high"] >= tgt: return True
        if not s["is_long"] and bar["low"]  <= tgt: return True
    return False if entry_filled else None

levels = [
    ("Morning level (level)",    lambda s: s["level"]),
    ("level + 10% rng",          lambda s: (s["level"] + 0.10*s["range"]) if s["is_long"] else (s["level"] - 0.10*s["range"])),
    ("level + 25% rng",          lambda s: (s["level"] + 0.25*s["range"]) if s["is_long"] else (s["level"] - 0.25*s["range"])),
    ("level + 50% rng",          lambda s: (s["level"] + 0.50*s["range"]) if s["is_long"] else (s["level"] - 0.50*s["range"])),
    ("PDH/PDL",                  lambda s: s["pdh"] if s["is_long"] else s["pdl"]),
]
print(f"\n  Sweep universe: wick≥7 + time-filtered ({len(sweeps)} setups)\n")
print(f"  {'Level':>25}   {'Triggered':>9}  {'Reached':>8}  {'Reach%':>7}")
print("  " + "─" * 60)
triggered_base = sum(1 for s in sweeps if any(
    (b["low"] <= (s["level"] - ENTRY_PCT*s["range"]) if s["is_long"] else b["high"] >= (s["level"] + ENTRY_PCT*s["range"]))
    for b in s["post"]
))
for name, fn in levels:
    results = [reach_pct(s, fn) for s in sweeps]
    reached    = sum(1 for r in results if r is True)
    triggered  = sum(1 for r in results if r is not None)
    pct = reached / triggered if triggered else 0
    print(f"  {name:>25}   {triggered:>9}  {reached:>8}  {pct:>7.0%}")


# ── SECTION 2: Loss anatomy ───────────────────────────────────────────────────

trades_ref = [build_trade(s, SL_PTS_REF, "pdh") for s in sweeps]
trades_ref = [t for t in trades_ref if t is not None]
closed = [t for t in trades_ref if t["oc"] != "OPEN"]
wins   = [t for t in closed if t["oc"] == "WIN"]
losses = [t for t in closed if t["oc"] == "LOSS"]

print(f"\n\n{'='*72}")
print(f"SECTION 2 — LOSS ANATOMY  (wick≥7, pb=10%, SL={SL_PTS_REF}pts, TP=PDH)")
print(f"{'='*72}")
print(f"\n  Total signals: {len(trades_ref)}  |  Closed: {len(closed)}  |  Wins: {len(wins)}  |  Losses: {len(losses)}  |  Open: {len(trades_ref)-len(closed)}")
print(f"  WR: {len(wins)/len(closed):.0%}   EV: {len(wins)/len(closed)*statistics.mean(t['rr'] for t in wins) - len(losses)/len(closed)*1.0:+.2f}R" if wins else "")

print(f"\n  ── Loss breakdown by how far price reached before SL ──\n")

cat_a = [t for t in losses if not t["hit_level"]]   # never reached morning level
cat_b = [t for t in losses if t["hit_level"] and not t["hit_rng010"]]  # hit level, not rng+10%
cat_c = [t for t in losses if t["hit_rng010"]]       # went past level+10% then reversed

print(f"  Category A — SL hit WITHOUT reaching morning level:       {len(cat_a):>3}  ({len(cat_a)/len(losses):.0%} of losses)")
print(f"    → SL may be too tight, OR direction was wrong")
print(f"  Category B — Reached morning level but not level+10%:     {len(cat_b):>3}  ({len(cat_b)/len(losses):.0%} of losses)")
print(f"    → Would have been a WIN if TP = morning level")
print(f"  Category C — Went past level+10%, then reversed to SL:    {len(cat_c):>3}  ({len(cat_c)/len(losses):.0%} of losses)")
print(f"    → TP missed by just a bit, or PDH never reached")

if cat_a:
    mae_a = [t["mae"] for t in cat_a]
    mfe_a = [t["mfe"] for t in cat_a]
    print(f"\n  Cat A detail — MAE (how far below entry before SL):")
    print(f"    Median MAE: {statistics.median(mae_a):.1f}pt  Max: {max(mae_a):.1f}pt")
    print(f"    MFE before SL — Median: {statistics.median(mfe_a):.1f}pt  Max: {max(mfe_a):.1f}pt")

print(f"\n  ── Max adverse excursion (MAE) — ALL losses ──\n")
mae_all = sorted(t["mae"] for t in losses)
buckets = [5, 10, 15, 20, 25, 30, 50]
prev_b = 0
for b in buckets:
    cnt = sum(1 for m in mae_all if prev_b < m <= b)
    print(f"    MAE {prev_b:>2}–{b:>2}pt:  {cnt:>2}  {'█'*cnt}")
    prev_b = b
cnt_over = sum(1 for m in mae_all if m > 50)
if cnt_over:
    print(f"    MAE >50pt:    {cnt_over:>2}  {'█'*cnt_over}")

print(f"\n  ── MFE distribution — ALL trades (how far toward TP before outcome) ──\n")
mfe_w = [t["mfe"] for t in wins]
mfe_l = [t["mfe"] for t in losses]
buckets2 = [(0,5),(5,15),(15,30),(30,60),(60,100),(100,999)]
print(f"  {'Range':>10}  {'Wins':>5}  {'Losses':>7}")
for lo, hi in buckets2:
    w = sum(1 for m in mfe_w if lo <= m < hi)
    l = sum(1 for m in mfe_l if lo <= m < hi)
    print(f"  {lo:>4}–{hi:>4}pt  {w:>5}  {l:>7}  {'W'*w + 'L'*l}")


# ── SECTION 3: SL sensitivity ─────────────────────────────────────────────────

print(f"\n\n{'='*72}")
print("SECTION 3 — SL SENSITIVITY  (wick≥7, pb=10%, TP=PDH, time-filtered)")
print(f"{'='*72}")
print(f"\n  {'SL':>5}  {'N':>4}  {'WR':>5}  {'avgR:R':>7}  {'EV':>7}  {'avg_pts':>8}  "
      f"{'LossesAtLevel%':>15}  note")
print("  " + "─" * 72)
for sl_p in [5, 8, 10, 12, 15, 18, 20, 25, 30]:
    ts = [build_trade(s, sl_p, "pdh") for s in sweeps]
    ts = [t for t in ts if t is not None]
    cl = [t for t in ts if t["oc"] != "OPEN"]
    if not cl: continue
    ws = [t for t in cl if t["oc"] == "WIN"]
    ls = [t for t in cl if t["oc"] == "LOSS"]
    wr = len(ws)/len(cl)
    avg_rr = statistics.mean(t["rr"] for t in ws) if ws else 0
    ev = wr*avg_rr - (1-wr)*1.0
    avg_pts = (wr*statistics.mean(t["rwd"] for t in ws) - (1-wr)*sl_p) if ws else -sl_p
    # losses that DID reach morning level
    ls_at_lv = sum(1 for t in ls if t["hit_level"])
    pct_at_lv = ls_at_lv/len(ls) if ls else 0
    note = "←best WR" if wr == max(len([t for t in [build_trade(s,sp,"pdh") for s in sweeps if build_trade(s,sp,"pdh")] if t and t["oc"]=="WIN"]) /
                                   max(len([t for t in [build_trade(s,sp,"pdh") for s in sweeps if build_trade(s,sp,"pdh")] if t and t["oc"]!="OPEN"]),1)
                                   for sp in [5,8,10,12,15,18,20,25,30]) else ""
    print(f"  {sl_p:>5}  {len(cl):>4}  {wr:>5.0%}  {avg_rr:>7.2f}R  {ev:>+7.2f}R  {avg_pts:>+8.1f}pt  "
          f"{pct_at_lv:>14.0%}  {note}")


# ── SECTION 4: TP sweep — every level at optimal SL ──────────────────────────

print(f"\n\n{'='*72}")
print("SECTION 4 — TP LEVEL SWEEP  (wick≥7, pb=10%, SL=15pts, time-filtered)")
print(f"{'='*72}")
tp_options = [
    ("level",     "Morning level"),
    ("rng_0.05",  "level+5%rng"),
    ("rng_0.10",  "level+10%rng"),
    ("rng_0.15",  "level+15%rng"),
    ("rng_0.20",  "level+20%rng"),
    ("rng_0.25",  "level+25%rng"),
    ("rng_0.35",  "level+35%rng"),
    ("rng_0.50",  "level+50%rng"),
    ("pdh",       "PDH/PDL"),
]
print(f"\n  {'TP mode':>14}  {'N':>4}  {'WR':>5}  {'avgR:R':>7}  {'EV':>7}  {'avg_pts':>9}")
print("  " + "─" * 62)
best_ev_tp = -999
best_tp = None
for tm, desc in tp_options:
    ts = [build_trade(s, 15, tm) for s in sweeps]
    ts = [t for t in ts if t is not None]
    cl = [t for t in ts if t["oc"] != "OPEN"]
    if not cl: continue
    ws = [t for t in cl if t["oc"] == "WIN"]
    wr = len(ws)/len(cl)
    avg_rr = statistics.mean(t["rr"] for t in ws) if ws else 0
    ev = wr*avg_rr-(1-wr)*1.0
    avg_pts = (wr*statistics.mean(t["rwd"] for t in ws) - (1-wr)*15) if ws else -15
    flag = " ◄ best EV" if ev > best_ev_tp else ""
    if ev > best_ev_tp: best_ev_tp = ev; best_tp = tm
    print(f"  {desc:>14}  {len(cl):>4}  {wr:>5.0%}  {avg_rr:>7.2f}R  {ev:>+7.2f}R  {avg_pts:>+9.1f}pt{flag}")


# ── SECTION 5: Tiered exit ────────────────────────────────────────────────────

print(f"\n\n{'='*72}")
print("SECTION 5 — TIERED EXIT  (TP1=morning_level, TP2=PDH, SL=15pts)")
print(f"{'='*72}")
print("\n  Idea: take half position at morning_level, run half to PDH.")
print("  Modelled as: EV = 0.5×(win_at_level result) + 0.5×(win_at_pdh result)\n")

results_level = []
results_pdh   = []
for s in sweeps:
    tl = build_trade(s, 15, "level")
    tp = build_trade(s, 15, "pdh")
    if tl and tp:
        results_level.append(tl)
        results_pdh.append(tp)

paired = list(zip(results_level, results_pdh))
cl_paired = [(l, p) for l, p in paired if l["oc"] != "OPEN" and p["oc"] != "OPEN"]

if cl_paired:
    blended_r = []
    for l, p in cl_paired:
        r_l = l["rr"] if l["oc"] == "WIN" else -1.0
        r_p = p["rr"] if p["oc"] == "WIN" else -1.0
        blended_r.append(0.5 * r_l + 0.5 * r_p)

    ev_blended = statistics.mean(blended_r)
    # outcome categories
    ww = sum(1 for l,p in cl_paired if l["oc"]=="WIN" and p["oc"]=="WIN")
    wl = sum(1 for l,p in cl_paired if l["oc"]=="WIN" and p["oc"]=="LOSS")
    lx = sum(1 for l,p in cl_paired if l["oc"]=="LOSS")
    n  = len(cl_paired)
    print(f"  N={n}   EV={ev_blended:+.2f}R/trade  (sum={sum(blended_r):+.1f}R)\n")
    print(f"  Outcome breakdown:")
    print(f"    Both TP hit (WIN/WIN):   {ww:>3}  {ww/n:>5.0%}  ({ww/n * (results_pdh[0]['rr'] if results_pdh else 0):.1f}R each)")
    print(f"    Only TP1 hit (WIN/LOSS): {wl:>3}  {wl/n:>5.0%}  (half TP1, half loss)")
    print(f"    Both loss (LOSS/LOSS):   {lx:>3}  {lx/n:>5.0%}")
    # compare to single TPs
    ts_level_all = [build_trade(s, 15, "level") for s in sweeps]
    ts_pdh_all   = [build_trade(s, 15, "pdh")   for s in sweeps]
    cl_l = [t for t in ts_level_all if t and t["oc"]!="OPEN"]
    cl_p = [t for t in ts_pdh_all   if t and t["oc"]!="OPEN"]
    ev_l = (sum(t["rr"] if t["oc"]=="WIN" else -1.0 for t in cl_l)/len(cl_l)) if cl_l else 0
    ev_p = (sum(t["rr"] if t["oc"]=="WIN" else -1.0 for t in cl_p)/len(cl_p)) if cl_p else 0
    print(f"\n  Compare:")
    print(f"    TP=level only:   EV={ev_l:+.2f}R")
    print(f"    TP=PDH only:     EV={ev_p:+.2f}R")
    print(f"    Tiered (50/50):  EV={ev_blended:+.2f}R")


# ── SECTION 6: Per-trade MAE/MFE table ───────────────────────────────────────

print(f"\n\n{'─'*108}")
print(f"SECTION 6 — PER-TRADE DETAIL  (wick≥7, pb=10%, SL={SL_PTS_REF}pts, TP=PDH)")
print(f"{'─'*108}")
print(f"  {'#':>3}  {'Date':>10}  {'Dir':>5}  {'SweepT':>6}  {'Wick':>5}  "
      f"{'Entry':>7}  {'SL':>7}  {'TP':>7}  "
      f"{'MAE':>6}  {'MFE':>6}  {'→Lv':>4}  {'→.1':>4}  {'→.25':>5}  "
      f"{'Oc':>5}  {'R':>7}  note")
print(f"  {'─'*104}")

n_all = 0
for i, (s, t) in enumerate(
    [(s, build_trade(s, SL_PTS_REF, "pdh")) for s in sweeps], 1
):
    if t is None: continue
    n_all += 1
    lv = "✓" if t["hit_level"]   else "·"
    r1 = "✓" if t["hit_rng010"]  else "·"
    r25= "✓" if t["hit_rng025"]  else "·"
    r_s = f"{t['r']:>+6.2f}R" if t["r"] is not None else "  OPEN"
    if t["oc"] == "LOSS":
        if not t["hit_level"]: note = "SL before level"
        elif not t["hit_rng010"]: note = "level hit, reversed"
        else: note = "past level+10%, reversed"
    elif t["oc"] == "WIN": note = "PDH reached"
    else: note = "open"
    print(f"  {n_all:>3}  {str(s['date']):>10}  {'LONG' if s['is_long'] else 'SHORT':>5}  "
          f"{s['sweep_hhmm']:>6}  {s['wick_ext']:>5.1f}  "
          f"{t['el']:>7.1f}  {t['sl']:>7.1f}  {t['tp']:>7.1f}  "
          f"{t['mae']:>6.1f}  {t['mfe']:>6.1f}  "
          f"{lv:>4}  {r1:>4}  {r25:>5}  "
          f"{t['oc']:>5}  {r_s}  {note}")

print("\nDone.")
