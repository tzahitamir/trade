#!/usr/bin/env python3
"""
Compare entry_zone 0.70 vs 0.65 — show marginal trades and their R.
"""
import sys, statistics
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from db.local_db import LocalDB
from analysis.smc_analyzer import SMCAnalyzer

_ISR    = ZoneInfo("Asia/Jerusalem")
DB_PATH = str(Path(__file__).resolve().parents[1] / "src" / "data" / "trade.db")

BASE = {
    "tp_pct":            0.55,
    "sl_atr_mult":       0.50,
    "min_expansion_atr": 1.00,
    "symbol":            "GER40",
}
PEAK_CUTOFF = (11, 45)
SKIP_MONDAY = True


def resample(c5m):
    out, i = [], 0
    while i < len(c5m):
        ts0 = c5m[i]["timestamp"]
        al  = (ts0 // 900) * 900
        g   = [c for c in c5m[i:i+3] if c["timestamp"] < al + 900]
        if not g: i += 1; continue
        out.append({"timestamp": al, "open": g[0]["open"],
                    "high": max(c["high"] for c in g),
                    "low":  min(c["low"]  for c in g),
                    "close": g[-1]["close"], "volume": 0})
        i += len(g)
    return out


def session_window(d):
    s = datetime(d.year, d.month, d.day,  9, 0, tzinfo=_ISR)
    e = datetime(d.year, d.month, d.day, 12, 30, tzinfo=_ISR)
    return int(s.timestamp()), int(e.timestamp())


def evaluate(entry, sl, tp, is_short, post_5m):
    for bar in post_5m:
        if is_short:
            if bar["high"] >= sl: return "LOSS"
            if bar["low"]  <= tp: return "WIN"
        else:
            if bar["low"]  <= sl: return "LOSS"
            if bar["high"] >= tp: return "WIN"
    return "OPEN"


def eff_r(entry, sl, tp, outcome):
    risk = abs(entry - sl)
    rwd  = abs(entry - tp)
    if outcome == "WIN":  return rwd / risk
    if outcome == "LOSS": return -1.0
    return None


# ── Load data ─────────────────────────────────────────────────────────────────

db       = LocalDB(DB_PATH)
raw_desc = db.query_recent("GER40", "5m", limit=100_000)
db.close()
all5m  = list(reversed(raw_desc))
all15m = resample(all5m)

dates = sorted(set(datetime.fromtimestamp(c["timestamp"], _ISR).date() for c in all5m))
dates = [d for d in dates if d.weekday() < 5]
if SKIP_MONDAY:
    dates = [d for d in dates if d.weekday() != 0]

ana = SMCAnalyzer()

# Run both param sets
results = {}  # ez → list of trade dicts
for ez in (0.70, 0.65):
    params = {**BASE, "entry_zone_min_pct": ez}
    trades = []
    for trade_date in dates:
        ss, se = session_window(trade_date)
        lk_end = se + 6 * 3600
        sess_15m = [c for c in all15m if ss <= c["timestamp"] <= se]
        pre_15m  = [c for c in all15m if c["timestamp"] < ss][-16:]
        day_5m   = [c for c in all5m  if ss <= c["timestamp"] <= lk_end]
        if len(sess_15m) < 3 or len(day_5m) < 6:
            continue
        sigs = ana.detect_dax_session_setup(sess_15m, day_5m, params=params,
                                             candles_15m_presession=pre_15m)
        if not sigs: continue
        sig = sigs[0]
        pt = datetime.fromtimestamp(sig["peak_ts"], tz=_ISR)
        if (pt.hour, pt.minute) >= PEAK_CUTOFF: continue

        is_short = sig["direction"] == "bearish"
        entry    = sig["entry"]
        sl       = sig["sl"]
        tp       = sig["tp"]
        bts      = sig["breakout_ts"]
        post     = [c for c in day_5m if c["timestamp"] > bts]
        oc       = evaluate(entry, sl, tp, is_short, post)
        r        = eff_r(entry, sl, tp, oc)
        ent_pct  = sig.get("entry_pct_from_origin", abs(entry - sig["origin"]) / sig["expansion_range"])

        trades.append({
            "date":    trade_date,
            "dir":     "SHORT" if is_short else "LONG",
            "entry":   entry,
            "tp":      tp,
            "sl":      sl,
            "range":   sig["expansion_range"],
            "ent_pct": ent_pct,
            "outcome": oc,
            "r":       r,
            "pts":     abs(entry - tp) if oc == "WIN" else (-abs(entry - sl) if oc == "LOSS" else None),
        })
    results[ez] = trades

# ── Identify marginal trades (in 0.65 but not 0.70) ──────────────────────────

dates_70 = {t["date"] for t in results[0.70]}
dates_65 = {t["date"] for t in results[0.65]}

marginal_dates = dates_65 - dates_70
shared_dates   = dates_65 & dates_70

# Also find days where 0.65 fires a DIFFERENT signal than 0.70
# (same day but different entry — entry_pct changed)
changed = []
for t65 in results[0.65]:
    if t65["date"] not in dates_70: continue
    t70 = next(t for t in results[0.70] if t["date"] == t65["date"])
    if abs(t65["entry"] - t70["entry"]) > 0.5:
        changed.append((t65["date"], t70, t65))

marginal = [t for t in results[0.65] if t["date"] in marginal_dates]

# ── Print ──────────────────────────────────────────────────────────────────────

def fmt_summary(trades):
    closed = [t for t in trades if t["outcome"] != "OPEN"]
    if not closed: return "  no closed trades"
    wins   = [t for t in closed if t["outcome"] == "WIN"]
    rs     = [t["r"] for t in closed if t["r"] is not None]
    pts    = [t["pts"] for t in closed if t["pts"] is not None]
    wr     = len(wins) / len(closed)
    return (f"  n={len(closed)}  WR={wr:.0%}  "
            f"avg_R={statistics.mean(rs):+.2f}R  "
            f"avg_pts={statistics.mean(pts):+.1f}  "
            f"total_pts={sum(pts):+.0f}")


print("=" * 70)
print("ENTRY ZONE COMPARISON: 0.70 vs 0.65")
print("=" * 70)
print(f"\nentry_zone=0.70 : {len(results[0.70])} total signals")
print(fmt_summary(results[0.70]))
print(f"\nentry_zone=0.65 : {len(results[0.65])} total signals")
print(fmt_summary(results[0.65]))

print(f"\n── Marginal trades (new at 0.65, not in 0.70): {len(marginal)} ──")
if marginal:
    print(f"\n  {'Date':>10}  {'Dir':>5}  {'Entry':>7}  {'TP':>7}  {'SL':>7}  "
          f"{'Range':>6}  {'Ent%':>5}  {'Oc':>6}  {'R':>6}  {'Pts':>6}")
    print("  " + "-" * 70)
    for t in sorted(marginal, key=lambda x: x["date"]):
        r_s  = f"{t['r']:>+5.2f}R" if t["r"] is not None else "  OPEN"
        pts_s = f"{t['pts']:>+5.0f}" if t["pts"] is not None else "  ---"
        print(f"  {str(t['date']):>10}  {t['dir']:>5}  {t['entry']:>7.0f}  "
              f"{t['tp']:>7.0f}  {t['sl']:>7.0f}  {t['range']:>6.0f}  "
              f"{t['ent_pct']:>4.0%}  {t['outcome']:>6}  {r_s}  {pts_s}")
    closed_m = [t for t in marginal if t["outcome"] != "OPEN"]
    if closed_m:
        rs   = [t["r"]   for t in closed_m if t["r"]   is not None]
        pts  = [t["pts"] for t in closed_m if t["pts"] is not None]
        wins = [t for t in closed_m if t["outcome"] == "WIN"]
        print(f"\n  Marginal summary: n={len(closed_m)}  WR={len(wins)/len(closed_m):.0%}  "
              f"avg_R={statistics.mean(rs):+.2f}R  avg_pts={statistics.mean(pts):+.1f}")
else:
    print("  (none — same signals fire at both thresholds)")

if changed:
    print(f"\n── Days where entry shifted (same day, different LH candle): {len(changed)} ──")
    print(f"\n  {'Date':>10}  {'Dir':>5}  {'Entry70':>8}  {'R_70':>6}  "
          f"{'Entry65':>8}  {'R_65':>6}  {'Ent%70':>7}  {'Ent%65':>7}")
    print("  " + "-" * 70)
    for d, t70, t65 in sorted(changed, key=lambda x: x[0]):
        r70 = f"{t70['r']:>+5.2f}R" if t70["r"] is not None else "  OPEN"
        r65 = f"{t65['r']:>+5.2f}R" if t65["r"] is not None else "  OPEN"
        print(f"  {str(d):>10}  {t70['dir']:>5}  {t70['entry']:>8.0f}  {r70}  "
              f"{t65['entry']:>8.0f}  {r65}  {t70['ent_pct']:>6.0%}  {t65['ent_pct']:>6.0%}")

print("\nDone.")
