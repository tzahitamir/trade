#!/usr/bin/env python3
"""Print points earned per trade for the 59 SERPE gold-param signals."""
import sys, statistics
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from db.local_db import LocalDB
from analysis.smc_analyzer import SMCAnalyzer

_ISR    = ZoneInfo("Asia/Jerusalem")
DB_PATH = str(Path(__file__).resolve().parents[1] / "src" / "data" / "trade.db")

GOLD = {
    "tp_pct":             0.55,
    "sl_atr_mult":        0.50,
    "min_expansion_atr":  1.00,
    "entry_zone_min_pct": 0.70,
    "symbol":             "GER40",
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


db       = LocalDB(DB_PATH)
raw_desc = db.query_recent("GER40", "5m", limit=100_000)
db.close()
all5m  = list(reversed(raw_desc))
all15m = resample(all5m)

from datetime import date, timedelta
dates = sorted(set(datetime.fromtimestamp(c["timestamp"], _ISR).date() for c in all5m))
dates = [d for d in dates if d.weekday() < 5]
if SKIP_MONDAY:
    dates = [d for d in dates if d.weekday() != 0]

ana = SMCAnalyzer()

rows = []
for trade_date in dates:
    ss, se = session_window(trade_date)
    lk_end = se + 6 * 3600
    sess_15m = [c for c in all15m if ss <= c["timestamp"] <= se]
    pre_15m  = [c for c in all15m if c["timestamp"] < ss][-16:]
    day_5m   = [c for c in all5m  if ss <= c["timestamp"] <= lk_end]
    if len(sess_15m) < 3 or len(day_5m) < 6:
        continue

    sigs = ana.detect_dax_session_setup(sess_15m, day_5m, params=GOLD, candles_15m_presession=pre_15m)
    if not sigs:
        continue

    sig = sigs[0]
    pt  = datetime.fromtimestamp(sig["peak_ts"], tz=_ISR)
    if (pt.hour, pt.minute) >= PEAK_CUTOFF:
        continue

    is_short = sig["direction"] == "bearish"
    entry    = sig["entry"]
    sl       = sig["sl"]
    tp       = sig["tp"]
    bts      = sig["breakout_ts"]
    post     = [c for c in day_5m if c["timestamp"] > bts]

    outcome  = evaluate(entry, sl, tp, is_short, post)
    if outcome == "WIN":
        pts = abs(entry - tp)
    elif outcome == "LOSS":
        pts = -abs(entry - sl)
    else:
        pts = None

    rows.append({
        "date":    trade_date,
        "dir":     "SHORT" if is_short else "LONG",
        "entry":   entry,
        "tp":      tp,
        "sl":      sl,
        "outcome": outcome,
        "pts":     pts,
        "range":   sig["expansion_range"],
    })

print(f"\n{'#':>3}  {'Date':>10}  {'Dir':>5}  {'Entry':>7}  {'TP':>7}  {'SL':>7}  "
      f"{'Range':>6}  {'Outcome':>6}  {'Pts':>6}  {'CumPts':>8}")
print("-" * 80)

cum = 0.0
wins = [r for r in rows if r["outcome"] == "WIN"]
losses = [r for r in rows if r["outcome"] == "LOSS"]
closed = wins + losses

for i, r in enumerate(rows, 1):
    pts_s = f"{r['pts']:>+6.0f}" if r['pts'] is not None else "  OPEN"
    if r['pts'] is not None:
        cum += r['pts']
    cum_s = f"{cum:>+8.0f}" if r['pts'] is not None else ""
    print(f"{i:>3}  {str(r['date']):>10}  {r['dir']:>5}  {r['entry']:>7.0f}  "
          f"{r['tp']:>7.0f}  {r['sl']:>7.0f}  {r['range']:>6.0f}  "
          f"{r['outcome']:>6}  {pts_s}  {cum_s}")

print("-" * 80)
win_pts   = [r["pts"] for r in wins]
loss_pts  = [r["pts"] for r in losses]

print(f"\nSummary ({len(closed)} closed trades):")
print(f"  Wins   : {len(wins):>3}  |  Total pts won   : {sum(win_pts):>+7.0f}")
if loss_pts:
    print(f"  Losses : {len(losses):>3}  |  Total pts lost  : {sum(loss_pts):>+7.0f}")
    print(f"  Net pts:        |  Net               : {sum(win_pts)+sum(loss_pts):>+7.0f}")
else:
    print(f"  Losses :   0  |  Net pts           : {sum(win_pts):>+7.0f}")
print(f"  Avg pts/trade   : {statistics.mean([r['pts'] for r in closed if r['pts'] is not None]):>+7.1f}")
print(f"  Median pts/trade: {statistics.median([r['pts'] for r in closed if r['pts'] is not None]):>+7.1f}")
print(f"  Min win         : {min(win_pts):>+7.0f}")
print(f"  Max win         : {max(win_pts):>+7.0f}")
