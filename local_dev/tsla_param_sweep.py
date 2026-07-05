#!/usr/bin/env python3
"""
TSLA SERPE parameter optimization — Jun 2025 to Jun 2026.

Sweeps tp_pct, entry_zone_min_pct, sl_atr_mult, min_expansion_atr.
Primary sort: EV (expected value per trade in R).
Also shows peak-time and entry-time distribution.
"""
import sys, json, statistics
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict
from itertools import product

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from analysis.smc_analyzer import SMCAnalyzer

_ET   = ZoneInfo("America/New_York")
CACHE = Path(__file__).parent / "tsla_5m_cache.json"

PEAK_CUTOFF = (11, 45)
SKIP_MONDAY = False

# ── Load cached data ──────────────────────────────────────────────────────────

if not CACHE.exists():
    print("ERROR: run tsla_dual_strategy.py first to cache data.")
    sys.exit(1)

print("Loading cached TSLA data …")
with open(CACHE) as f:
    all5m = json.load(f)

def resample_15m(c5m):
    out, i = [], 0
    while i < len(c5m):
        ts0 = c5m[i]["timestamp"]; al = (ts0 // 900) * 900
        g = [c for c in c5m[i:i+3] if c["timestamp"] < al + 900]
        if not g: i += 1; continue
        out.append({"timestamp": al, "open": g[0]["open"],
                    "high": max(c["high"] for c in g),
                    "low":  min(c["low"]  for c in g),
                    "close": g[-1]["close"], "volume": 0})
        i += len(g)
    return out

all15m = resample_15m(all5m)

dates = sorted(set(datetime.fromtimestamp(c["timestamp"], _ET).date() for c in all5m))
dates = [d for d in dates if d.weekday() < 5]
if SKIP_MONDAY:
    dates = [d for d in dates if d.weekday() != 0]

# Build session slices once (expensive, reuse across param combos)
sessions = []
for d in dates:
    s  = datetime(d.year, d.month, d.day,  9, 30, tzinfo=_ET)
    e  = datetime(d.year, d.month, d.day, 12, 30, tzinfo=_ET)
    mc = datetime(d.year, d.month, d.day, 16,  0, tzinfo=_ET)
    ss, se, mc_ts = int(s.timestamp()), int(e.timestamp()), int(mc.timestamp())
    sess_15m = [c for c in all15m if ss <= c["timestamp"] <= se]
    pre_15m  = [c for c in all15m if c["timestamp"] < ss][-16:]
    day_5m   = [c for c in all5m  if ss <= c["timestamp"] <= mc_ts]
    if len(sess_15m) >= 3 and len(day_5m) >= 6:
        sessions.append((d, ss, se, mc_ts, sess_15m, pre_15m, day_5m))

print(f"  {len(all5m)} bars  |  {len(sessions)} sessions\n")

# ── Parameter grid ────────────────────────────────────────────────────────────

TP_PCTS    = [0.40, 0.50, 0.55, 0.60, 0.70]
EZ_PCTS    = [0.50, 0.60, 0.70, 0.80]
SL_MULTS   = [0.25, 0.50, 0.75]
EXP_ATRS   = [0.75, 1.00, 1.50]

def evaluate(entry, sl, tp, is_short, post):
    risk = abs(entry - sl) or 1e-6
    rwd  = abs(entry - tp)
    for bar in post:
        if is_short:
            if bar["high"] >= sl: return "LOSS", -1.0
            if bar["low"]  <= tp: return "WIN",  rwd / risk
        else:
            if bar["low"]  <= sl: return "LOSS", -1.0
            if bar["high"] >= tp: return "WIN",  rwd / risk
    return "OPEN", None

def run_params(tp_pct, ez_pct, sl_mult, exp_atr, ana):
    params = {"tp_pct": tp_pct, "sl_atr_mult": sl_mult,
              "min_expansion_atr": exp_atr, "entry_zone_min_pct": ez_pct,
              "symbol": "TSLA"}
    outcomes = []
    for _, _, _, _, sess_15m, pre_15m, day_5m in sessions:
        sigs = ana.detect_dax_session_setup(
            sess_15m, day_5m, params=params, candles_15m_presession=pre_15m)
        if not sigs: continue
        sig = sigs[0]
        pt = datetime.fromtimestamp(sig["peak_ts"], tz=_ET)
        if (pt.hour, pt.minute) >= PEAK_CUTOFF: continue
        is_short = sig["direction"] == "bearish"
        post = [c for c in day_5m if c["timestamp"] > sig["breakout_ts"]]
        oc, r = evaluate(sig["entry"], sig["sl"], sig["tp"], is_short, post)
        outcomes.append((oc, r))
    return outcomes

def ev(outcomes):
    closed = [(o, r) for o, r in outcomes if o != "OPEN"]
    if not closed: return None, 0, 0, 0, 0
    wins  = [(o, r) for o, r in closed if o == "WIN"]
    wr    = len(wins) / len(closed)
    avg_r = statistics.mean(r for _, r in closed)
    ev_   = wr * statistics.mean(r for _, r in wins) + (1-wr)*(-1.0) if wins else -1.0
    return ev_, wr, avg_r, len(closed), len(outcomes) - len(closed)

# ── Run sweep ─────────────────────────────────────────────────────────────────

ana = SMCAnalyzer()
combos = list(product(TP_PCTS, EZ_PCTS, SL_MULTS, EXP_ATRS))
print(f"Running {len(combos)} param combinations …")

results = []
for i, (tp, ez, sl, exp) in enumerate(combos):
    outs = run_params(tp, ez, sl, exp, ana)
    ev_, wr, avg_r, n_cl, n_op = ev(outs)
    if ev_ is None or n_cl < 8: continue
    results.append({
        "tp": tp, "ez": ez, "sl": sl, "exp": exp,
        "ev": ev_, "wr": wr, "avg_r": avg_r,
        "n_cl": n_cl, "n_op": n_op, "n_sig": n_cl + n_op,
    })
    if (i+1) % 30 == 0:
        print(f"  {i+1}/{len(combos)} …")

results.sort(key=lambda x: x["ev"], reverse=True)
print(f"Done — {len(results)} valid combos\n")

# ── Top results ───────────────────────────────────────────────────────────────

print("=" * 78)
print("TSLA SERPE — PARAMETER SWEEP  (top 30 by EV, min 8 closed trades)")
print("=" * 78)
print(f"\n  {'tp':>5}  {'ez':>5}  {'sl':>5}  {'exp':>5}  {'N':>4}  "
      f"{'WR':>5}  {'avg_R':>6}  {'EV':>6}  {'Open':>5}")
print("  " + "-" * 62)

for r in results[:30]:
    print(f"  {r['tp']:>5.2f}  {r['ez']:>5.2f}  {r['sl']:>5.2f}  {r['exp']:>5.2f}  "
          f"{r['n_cl']:>4}  {r['wr']:>5.0%}  {r['avg_r']:>+5.2f}R  "
          f"{r['ev']:>+5.2f}R  {r['n_op']:>5}")

# ── Best combo detail ─────────────────────────────────────────────────────────

best = results[0]
print(f"\n\n{'='*70}")
print(f"BEST: tp={best['tp']}  ez={best['ez']}  sl={best['sl']}  exp={best['exp']}")
print(f"      EV={best['ev']:+.2f}R  WR={best['wr']:.0%}  avg_R={best['avg_r']:+.2f}R  "
      f"N={best['n_cl']} closed")
print(f"{'='*70}")

params_best = {"tp_pct": best["tp"], "sl_atr_mult": best["sl"],
               "min_expansion_atr": best["exp"], "entry_zone_min_pct": best["ez"],
               "symbol": "TSLA"}

peak_times  = defaultdict(int)
entry_times = defaultdict(int)
detail_rows = []

for trade_date, _, _, _, sess_15m, pre_15m, day_5m in sessions:
    sigs = ana.detect_dax_session_setup(
        sess_15m, day_5m, params=params_best, candles_15m_presession=pre_15m)
    if not sigs: continue
    sig = sigs[0]
    pt  = datetime.fromtimestamp(sig["peak_ts"], tz=_ET)
    et_ = datetime.fromtimestamp(sig["breakout_ts"], tz=_ET)
    if (pt.hour, pt.minute) >= PEAK_CUTOFF: continue

    is_short = sig["direction"] == "bearish"
    post = [c for c in day_5m if c["timestamp"] > sig["breakout_ts"]]
    oc, r = evaluate(sig["entry"], sig["sl"], sig["tp"], is_short, post)
    pts = abs(sig["entry"] - sig["tp"]) if oc == "WIN" else \
          (-abs(sig["entry"] - sig["sl"]) if oc == "LOSS" else None)

    peak_times[pt.strftime("%H:%M")] += 1
    entry_times[et_.strftime("%H:%M")] += 1

    detail_rows.append({
        "date":  trade_date, "dir": "SHORT" if is_short else "LONG",
        "peak_t": pt.strftime("%H:%M"), "entry_t": et_.strftime("%H:%M"),
        "entry": sig["entry"], "tp": sig["tp"], "sl": sig["sl"],
        "sl_dist": abs(sig["entry"] - sig["sl"]),
        "range": sig["expansion_range"],
        "ent_pct": sig.get("entry_pct_from_origin", 0),
        "outcome": oc, "r": r, "pts": pts,
    })

# ── Timing distribution ───────────────────────────────────────────────────────

print("\nPeak formation time (ET):")
for t in sorted(peak_times):
    bar = "█" * peak_times[t]
    print(f"  {t}  {bar}  ({peak_times[t]})")

print("\nEntry (LH/HL) time (ET):")
for t in sorted(entry_times):
    bar = "█" * entry_times[t]
    print(f"  {t}  {bar}  ({entry_times[t]})")

# ── Per-trade table ───────────────────────────────────────────────────────────

closed = [r for r in detail_rows if r["outcome"] != "OPEN"]
wins   = [r for r in closed if r["outcome"] == "WIN"]
pts_cl = [r["pts"] for r in closed if r["pts"] is not None]

print(f"\n\n{'─'*85}")
print(f"Per-trade detail — best params ({len(detail_rows)} signals, "
      f"{len(closed)} closed, {len(wins)} wins)")
print(f"{'─'*85}")
print(f"  {'#':>3}  {'Date':>10}  {'Dir':>5}  {'PkT':>5}  {'EntT':>5}  "
      f"{'Entry':>7}  {'TP':>7}  {'SL$':>6}  {'Ent%':>5}  {'Oc':>6}  {'R':>6}  {'Pts':>6}")
print(f"  {'─'*82}")
cum = 0.0
for i, r in enumerate(detail_rows, 1):
    r_s   = f"{r['r']:>+5.2f}R" if r["r"]   is not None else "  OPEN"
    pts_s = f"{r['pts']:>+5.1f}" if r["pts"] is not None else "  ---"
    if r["pts"] is not None: cum += r["pts"]
    print(f"  {i:>3}  {str(r['date']):>10}  {r['dir']:>5}  {r['peak_t']:>5}  "
          f"{r['entry_t']:>5}  {r['entry']:>7.2f}  {r['tp']:>7.2f}  "
          f"${r['sl_dist']:>5.2f}  {r['ent_pct']:>4.0%}  "
          f"{r['outcome']:>6}  {r_s}  {pts_s}")

print(f"\n  Avg pts (closed): {statistics.mean(pts_cl):+.2f}")
print(f"  Total pts:        {cum:+.1f}")

# ── Compare current DAX gold params vs best ───────────────────────────────────

dax_gold = next((r for r in results
                 if r["tp"]==0.55 and r["ez"]==0.70 and r["sl"]==0.50 and r["exp"]==1.00),
                None)
print(f"\n\nDAX gold params on TSLA: ", end="")
if dax_gold:
    print(f"EV={dax_gold['ev']:+.2f}R  WR={dax_gold['wr']:.0%}  "
          f"avg_R={dax_gold['avg_r']:+.2f}R  N={dax_gold['n_cl']}")
else:
    print("not in results (below 8-trade threshold)")

print(f"Best TSLA params:       EV={best['ev']:+.2f}R  WR={best['wr']:.0%}  "
      f"avg_R={best['avg_r']:+.2f}R  N={best['n_cl']}")
print("\nDone.")
