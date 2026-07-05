#!/usr/bin/env python3
"""
DAX Continuation v5 — Institutional context filters.

Core setup: wick≥7, pb=10%, SL=15pts, TP=PDH, time-filtered (11:00-11:30 + 12:00-12:30)

Questions:
  1. DAILY TREND — does sweep direction align with daily bias?
     → Only take LONG sweeps when daily trend is bullish, SHORT when bearish
  2. ASIAN SESSION HIGH/LOW — did the sweep target the Asian session high/low?
     → Asian high swept = stronger BSL sweep (double liquidity pool)
  3. PDH DISTANCE — is PDH far enough from morning level to justify the trade?
     → Minimum distance filter (too close = not worth it; too far = unreachable?)
  4. DAY OF WEEK — Mon–Fri institutional profile
  5. CAT A LOSS AUTOPSY — trend direction on bad days
  6. BEST COMPOSITE FILTER — what combination maximises EV with N≥8 closed?
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
GOOD_WINDOWS = [(660, 690), (720, 750)]   # 11:00–11:30, 12:00–12:30 UTC

ENTRY_PCT  = 0.10
SL_PTS     = 15
MIN_WICK   = 7


def _ts(d: date, h: int, m: int) -> int:
    return int(datetime(d.year, d.month, d.day, h, m, tzinfo=_UTC).timestamp())

def _atr(bars, period=14):
    if len(bars) < 2: return 0.0
    trs = [max(b["high"] - b["low"],
               abs(b["high"] - bars[i-1]["close"]),
               abs(b["low"]  - bars[i-1]["close"]))
           for i, b in enumerate(bars[1:], 1)]
    return statistics.mean(trs[-period:]) if trs else 0.0


# ── Load data ──────────────────────────────────────────────────────────────────

print("Loading GER40 5m data …")
db = LocalDB(DB_PATH)
all5m = list(reversed(db.query_recent("GER40", "5m", limit=130_000)))
dates = sorted(set(datetime.fromtimestamp(c["timestamp"], tz=_UTC).date() for c in all5m))
dates = [d for d in dates if d.weekday() < 5]

# Daily OHLC
daily = {}
for d in dates:
    day_bars = [b for b in all5m if _ts(d,0,0) <= b["timestamp"] < _ts(d,23,59)]
    if day_bars:
        daily[d] = {
            "open":  day_bars[0]["open"],
            "high":  max(b["high"] for b in day_bars),
            "low":   min(b["low"]  for b in day_bars),
            "close": day_bars[-1]["close"],
        }

# Previous-day H/L
prev_hl = {}
for i, d in enumerate(dates):
    if i == 0: continue
    pd = dates[i-1]
    if pd in daily:
        prev_hl[d] = (daily[pd]["high"], daily[pd]["low"], daily[pd]["close"])

# Daily trend: direction of last N closes relative to N closes ago
def day_trend(d, dates, daily, n=3):
    idx = dates.index(d) if d in dates else -1
    if idx < n: return 0
    closes = [daily[dates[idx-k]]["close"] for k in range(n+1) if dates[idx-k] in daily]
    if len(closes) < n+1: return 0
    # slope: close[0] vs close[n]  (closes[0]=today, closes[n]=n days ago)
    return 1 if closes[0] > closes[n] else -1


# ── Build sweep records ─────────────────────────────────────────────────────

print("Building sweep records …")
sweeps_all = []
for d in dates:
    ts_ms = _ts(d, MORNING_H,   MORNING_M)
    ts_re = _ts(d, RANGE_END_H, RANGE_END_M)
    ts_se = _ts(d, SWEEP_END_H, SWEEP_END_M)
    ts_co = _ts(d, CUTOFF_H,    CUTOFF_M)
    ts_asia_end = ts_ms  # Asian session: 00:00–07:00 UTC

    morning = [c for c in all5m if ts_ms <= c["timestamp"] < ts_re]
    sweepw  = [c for c in all5m if ts_re  <= c["timestamp"] < ts_se]
    day5m   = [c for c in all5m if ts_ms  <= c["timestamp"] <= ts_co]

    # Asian session (00:00–07:00)
    asia = [c for c in all5m if _ts(d,0,0) <= c["timestamp"] < ts_ms]
    asia_high = max((c["high"] for c in asia), default=None)
    asia_low  = min((c["low"]  for c in asia), default=None)

    if len(morning) < 6: continue
    mh  = max(c["high"]  for c in morning)
    ml  = min(c["low"]   for c in morning)
    rng = mh - ml
    atr = _atr(morning) or 20.0
    if rng < 0.5 * atr: continue

    for bar in sweepw:
        is_high = bar["high"] > mh and bar["close"] <= mh
        is_low  = bar["low"]  < ml and bar["close"] >= ml
        if is_high or is_low:
            is_long  = is_high
            wick_ext = (bar["high"] - mh) if is_long else (ml - bar["low"])
            level    = mh if is_long else ml
            sweep_dt = datetime.fromtimestamp(bar["timestamp"], tz=_UTC)

            pdh, pdl, pd_close = prev_hl.get(d, (None, None, None))
            post = [b for b in day5m if b["timestamp"] > bar["timestamp"]]

            # PDH distance in pts
            pdh_dist = (pdh - level) if (is_long and pdh) else (level - pdl) if (not is_long and pdl) else None

            # Asian session alignment: did morning level sweep the Asian high/low?
            if is_long and asia_high is not None:
                asia_sweep = abs(mh - asia_high) <= 10  # within 10 pts of Asian high
            elif not is_long and asia_low is not None:
                asia_sweep = abs(ml - asia_low) <= 10
            else:
                asia_sweep = False

            # Daily trend
            trend = day_trend(d, dates, daily, n=3)
            trend_aligned = (is_long and trend >= 0) or (not is_long and trend <= 0)

            sweeps_all.append({
                "date": d, "dow": d.weekday(),  # 0=Mon, 4=Fri
                "is_long": is_long, "level": level,
                "mh": mh, "ml": ml, "range": rng, "atr": atr,
                "wick_ext": wick_ext,
                "sweep_min": sweep_dt.hour*60 + sweep_dt.minute,
                "sweep_hhmm": sweep_dt.strftime("%H:%M"),
                "pdh": pdh, "pdl": pdl, "pdh_dist": pdh_dist,
                "asia_high": asia_high, "asia_low": asia_low,
                "asia_sweep": asia_sweep,
                "trend": trend, "trend_aligned": trend_aligned,
                "pd_close": pd_close,
                "post": post,
            })
            break

sweeps_base = [s for s in sweeps_all
               if s["wick_ext"] >= MIN_WICK
               and any(t0 <= s["sweep_min"] < t1 for t0,t1 in GOOD_WINDOWS)]
print(f"  Total sweeps: {len(sweeps_all)}  |  Base filtered (wick≥{MIN_WICK} + time): {len(sweeps_base)}\n")


# ── Core trade evaluator ───────────────────────────────────────────────────────

def eval_trade(s, sl_pts=SL_PTS, entry_pct=ENTRY_PCT):
    is_long = s["is_long"]
    level   = s["level"]
    rng     = s["range"]

    el = (level - entry_pct * rng) if is_long else (level + entry_pct * rng)
    sl = el - sl_pts if is_long else el + sl_pts

    tp = s["pdh"] if is_long else s["pdl"]
    if tp is None or (is_long and tp <= el) or (not is_long and tp >= el): return None

    risk   = abs(el - sl)
    reward = abs(el - tp)
    if risk < 0.5: return None
    rr = reward / risk

    entry_filled = False
    hit_level = False
    mae = mfe = 0.0

    for bar in s["post"]:
        if not entry_filled:
            if is_long  and bar["high"] >= tp: return None
            if not is_long and bar["low"]  <= tp: return None
            if is_long  and bar["low"]  <= el: entry_filled = True
            if not is_long and bar["high"] >= el: entry_filled = True
            if not entry_filled: continue
        if is_long:
            mae = max(mae, el - bar["low"])
            mfe = max(mfe, bar["high"] - el)
            if bar["high"] >= level: hit_level = True
            if bar["low"]  <= sl: return {"oc":"LOSS","r":-1.0,"rr":rr,"mae":mae,"mfe":mfe,"hit_level":hit_level,"risk":risk,"rwd":reward}
            if bar["high"] >= tp: return {"oc":"WIN","r":rr,"rr":rr,"mae":mae,"mfe":mfe,"hit_level":True,"risk":risk,"rwd":reward}
        else:
            mae = max(mae, bar["high"] - el)
            mfe = max(mfe, el - bar["low"])
            if bar["low"]  <= level: hit_level = True
            if bar["high"] >= sl: return {"oc":"LOSS","r":-1.0,"rr":rr,"mae":mae,"mfe":mfe,"hit_level":hit_level,"risk":risk,"rwd":reward}
            if bar["low"]  <= tp: return {"oc":"WIN","r":rr,"rr":rr,"mae":mae,"mfe":mfe,"hit_level":True,"risk":risk,"rwd":reward}
    if entry_filled:
        return {"oc":"OPEN","r":None,"rr":rr,"mae":mae,"mfe":mfe,"hit_level":hit_level,"risk":risk,"rwd":reward}
    return None

def stats(trades, label="", min_n=5):
    t  = [r for r in trades if r]
    cl = [r for r in t if r["oc"] != "OPEN"]
    ws = [r for r in cl if r["oc"] == "WIN"]
    if len(cl) < min_n: return None
    wr  = len(ws)/len(cl)
    rr  = statistics.mean(r["rr"] for r in ws) if ws else 0
    ev  = wr*rr-(1-wr)*1.0
    pts = (wr*statistics.mean(r["rwd"] for r in ws) - (1-wr)*SL_PTS) if ws else -SL_PTS
    return {"n":len(cl),"n_op":len(t)-len(cl),"wr":wr,"rr":rr,"ev":ev,"pts":pts}


# ── SECTION 1: Daily trend alignment ─────────────────────────────────────────

print("=" * 72)
print("SECTION 1 — DAILY TREND ALIGNMENT (3-day slope)")
print("=" * 72)

for aligned in [True, False, None]:
    if aligned is None:
        subset = sweeps_base
        label  = "All (no trend filter)"
    else:
        subset = [s for s in sweeps_base if s["trend_aligned"] == aligned]
        label  = "Trend ALIGNED" if aligned else "Trend COUNTER"
    trades = [eval_trade(s) for s in subset]
    sm = stats(trades, label, min_n=3)
    if sm:
        print(f"\n  {label}  (N={sm['n']} closed, {sm['n_op']} open)")
        print(f"    WR={sm['wr']:.0%}  avgR:R={sm['rr']:.2f}R  EV={sm['ev']:+.2f}R  avg_pts={sm['pts']:+.0f}pt")

# Show per-trade trend tag
print(f"\n  {'Date':>10}  {'Dir':>5}  {'Trend':>6}  {'Align':>6}  {'Oc':>5}  R")
print("  " + "─" * 52)
for s in sweeps_base:
    t = eval_trade(s)
    if t is None: continue
    trend_s = "↑" if s["trend"]>0 else "↓" if s["trend"]<0 else "→"
    align_s = "✓" if s["trend_aligned"] else "✗"
    r_s = f"{t['r']:+.2f}R" if t["r"] is not None else "OPEN"
    print(f"  {s['date']}  {'LONG' if s['is_long'] else 'SHORT':>5}  {trend_s:>6}  {align_s:>6}  {t['oc']:>5}  {r_s}")


# ── SECTION 2: Asian session sweep alignment ─────────────────────────────────

print(f"\n\n{'='*72}")
print("SECTION 2 — ASIAN SESSION HIGH/LOW ALIGNMENT (morning level within 10pt)")
print(f"{'='*72}")
print(f"\n  (Did the morning high/low also sweep the Asia session high/low?)\n")

for asia_f in [True, False, None]:
    if asia_f is None:
        subset = sweeps_base
        label  = "All"
    else:
        subset = [s for s in sweeps_base if s["asia_sweep"] == asia_f]
        label  = "Asia HIGH swept" if asia_f else "Asia NOT swept"
    trades = [eval_trade(s) for s in subset]
    sm = stats(trades, min_n=3)
    if sm:
        print(f"  {label:<20}  N={sm['n']:>2}  WR={sm['wr']:.0%}  EV={sm['ev']:+.2f}R  avg_pts={sm['pts']:+.0f}pt")

# Different tolerance thresholds
print(f"\n  Tolerance sweep (morning level within X pts of Asian session extreme):\n")
print(f"  {'Tolerance':>10}  {'N_match':>8}  {'N_cl':>5}  {'WR':>5}  {'EV':>7}")
for tol in [5, 10, 15, 20, 30, 50]:
    subset = []
    for s in sweeps_base:
        al = s["asia_high"] if s["is_long"] else s["asia_low"]
        if al is None: continue
        if abs(s["level"] - al) <= tol:
            subset.append(s)
    trades = [eval_trade(s) for s in subset]
    sm = stats(trades, min_n=3)
    ev_s = f"{sm['ev']:+.2f}R" if sm else "—"
    wr_s = f"{sm['wr']:.0%}" if sm else "—"
    ncl  = sm['n'] if sm else "—"
    print(f"  {tol:>10}pt  {len(subset):>8}  {ncl:>5}  {wr_s:>5}  {ev_s:>7}")


# ── SECTION 3: PDH distance filter ──────────────────────────────────────────

print(f"\n\n{'='*72}")
print("SECTION 3 — PDH DISTANCE FROM MORNING LEVEL")
print(f"{'='*72}")
print(f"\n  Intuition: if PDH is too close, R:R too small; too far, unreachable.")
print(f"  Find the sweet spot.\n")

# Distribution of PDH distance across all setups
dists = [(s["pdh_dist"], eval_trade(s)) for s in sweeps_base if s["pdh_dist"] is not None]
print(f"  PDH distance distribution (all {len(dists)} setups):")
bkts = [(0,30),(30,60),(60,100),(100,150),(150,200),(200,300),(300,999)]
for lo, hi in bkts:
    pts = [(d, t) for d, t in dists if lo <= d < hi and t is not None]
    cl = [(d, t) for d, t in pts if t["oc"] != "OPEN"]
    ws = [(d, t) for d, t in cl if t["oc"] == "WIN"]
    wr_s = f"{len(ws)/len(cl):.0%}" if cl else "—"
    ev_s = f"{(len(ws)/len(cl)*statistics.mean(t['rr'] for _,t in ws) - (1-len(ws)/len(cl))*1.0):+.2f}R" if cl and ws else ("—" if not cl else f"{-1.0:.2f}R")
    print(f"  {lo:>4}–{hi:>4}pt:  {len(pts):>3} setups   closed={len(cl):>2}  WR={wr_s:>5}  EV={ev_s:>8}  {'W'*len(ws)+'L'*(len(cl)-len(ws))}")

# Min distance filter sweep
print(f"\n  Min PDH distance filter:\n")
print(f"  {'MinDist':>8}  {'N_cl':>5}  {'WR':>5}  {'avgR:R':>7}  {'EV':>7}  {'avg_pts':>9}")
print("  " + "─" * 54)
for min_d in [0, 30, 50, 60, 75, 100, 120, 150]:
    subset = [s for s in sweeps_base if s["pdh_dist"] is not None and s["pdh_dist"] >= min_d]
    trades = [eval_trade(s) for s in subset]
    sm = stats(trades, min_n=4)
    if sm:
        print(f"  {min_d:>8}pt  {sm['n']:>5}  {sm['wr']:.0%}  {sm['rr']:>7.2f}R  {sm['ev']:>+7.2f}R  {sm['pts']:>+9.1f}pt")

# Max distance filter (exclude too-far PDH)
print(f"\n  Max PDH distance filter (exclude very far TP):\n")
print(f"  {'MaxDist':>8}  {'N_cl':>5}  {'WR':>5}  {'avgR:R':>7}  {'EV':>7}  {'avg_pts':>9}")
print("  " + "─" * 54)
for max_d in [999, 300, 250, 200, 150, 120, 100]:
    subset = [s for s in sweeps_base if s["pdh_dist"] is not None and s["pdh_dist"] <= max_d]
    trades = [eval_trade(s) for s in subset]
    sm = stats(trades, min_n=4)
    if sm:
        print(f"  {max_d:>8}pt  {sm['n']:>5}  {sm['wr']:.0%}  {sm['rr']:>7.2f}R  {sm['ev']:>+7.2f}R  {sm['pts']:>+9.1f}pt")


# ── SECTION 4: Day of week ────────────────────────────────────────────────────

print(f"\n\n{'='*72}")
print("SECTION 4 — DAY OF WEEK")
print(f"{'='*72}")
print(f"\n  {'Day':>9}  {'Sweeps':>7}  {'N_cl':>5}  {'WR':>5}  {'EV':>7}")
print("  " + "─" * 44)
dow_names = ["Mon","Tue","Wed","Thu","Fri"]
for dow in range(5):
    subset = [s for s in sweeps_base if s["dow"] == dow]
    trades = [eval_trade(s) for s in subset]
    sm = stats(trades, min_n=2)
    ev_s  = f"{sm['ev']:+.2f}R" if sm else "—"
    wr_s  = f"{sm['wr']:.0%}" if sm else "—"
    ncl_s = f"{sm['n']}" if sm else "—"
    print(f"  {dow_names[dow]:>9}  {len(subset):>7}  {ncl_s:>5}  {wr_s:>5}  {ev_s:>7}")


# ── SECTION 5: Cat A autopsy — trend on bad days ──────────────────────────────

print(f"\n\n{'='*72}")
print("SECTION 5 — CAT A LOSS AUTOPSY (direction wrong, SL before level)")
print(f"{'='*72}")
print(f"\n  Checking: daily trend, Asian alignment, PDH distance, DOW\n")
print(f"  {'Date':>10}  {'Dir':>5}  {'Trend':>5}  {'Asia':>5}  {'PDH_d':>6}  {'DOW':>4}  {'Oc':>5}  note")
print("  " + "─" * 64)
for s in sweeps_base:
    t = eval_trade(s)
    if t is None: continue
    if t["oc"] == "LOSS" and not t["hit_level"]:
        cat = "Cat A"
    elif t["oc"] == "WIN":
        cat = "WIN"
    else:
        cat = "other"
    trend_s = "↑" if s["trend"]>0 else "↓" if s["trend"]<0 else "→"
    asia_s  = "✓" if s["asia_sweep"] else "·"
    pdh_d   = f"{s['pdh_dist']:.0f}" if s["pdh_dist"] else "?"
    dow_s   = dow_names[s["dow"]]
    if cat == "Cat A":
        print(f"  {s['date']}  {'LONG' if s['is_long'] else 'SHORT':>5}  {trend_s:>5}  {asia_s:>5}  {pdh_d:>6}  {dow_s:>4}  LOSS  ← CAT A")
    elif cat == "WIN":
        r_s = f"{t['r']:+.2f}R"
        print(f"  {s['date']}  {'LONG' if s['is_long'] else 'SHORT':>5}  {trend_s:>5}  {asia_s:>5}  {pdh_d:>6}  {dow_s:>4}   WIN  {r_s}")
    else:
        r_s = f"{t['r']:+.2f}R" if t['r'] else "OPEN"
        print(f"  {s['date']}  {'LONG' if s['is_long'] else 'SHORT':>5}  {trend_s:>5}  {asia_s:>5}  {pdh_d:>6}  {dow_s:>4}  {t['oc']:>5}  {r_s}")


# ── SECTION 6: Composite filter — best combination ───────────────────────────

print(f"\n\n{'='*72}")
print("SECTION 6 — COMPOSITE FILTER SWEEP")
print(f"{'='*72}")
print(f"\n  Testing combinations of: trend_align, asia_sweep, min_pdh_dist\n")
print(f"  {'Filters':>42}  {'N_cl':>5}  {'WR':>5}  {'EV':>7}  {'avg_pts':>9}")
print("  " + "─" * 70)

combos = []
for trend_filter in [None, True]:
    for asia_filter in [None, True]:
        for min_pdh in [0, 50, 75, 100]:
            subset = sweeps_base.copy()
            if trend_filter is not None:
                subset = [s for s in subset if s["trend_aligned"] == trend_filter]
            if asia_filter is not None:
                subset = [s for s in subset if s["asia_sweep"] == asia_filter]
            if min_pdh > 0:
                subset = [s for s in subset if s["pdh_dist"] is not None and s["pdh_dist"] >= min_pdh]
            trades = [eval_trade(s) for s in subset]
            sm = stats(trades, min_n=6)
            if sm:
                combos.append({"sm":sm,"trend":trend_filter,"asia":asia_filter,"min_pdh":min_pdh,"n_sub":len(subset)})

combos.sort(key=lambda x: x["sm"]["ev"], reverse=True)
for c in combos[:20]:
    sm = c["sm"]
    parts = []
    if c["trend"]: parts.append("trend✓")
    if c["asia"]:  parts.append("asia_sweep✓")
    if c["min_pdh"]: parts.append(f"PDH≥{c['min_pdh']}pt")
    label = "+".join(parts) if parts else "(no extra filter)"
    print(f"  {label:>42}  {sm['n']:>5}  {sm['wr']:.0%}  {sm['ev']:>+7.2f}R  {sm['pts']:>+9.1f}pt")

print("\nDone.")
