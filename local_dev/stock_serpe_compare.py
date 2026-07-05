#!/usr/bin/env python3
"""
Compare SERPE signal quality across multiple US stocks.
Runs sequentially to respect Twelve Data rate limits (8 credits/min).

Metrics per symbol:
  signals/yr, signals/week, WR, avg R, avg pts won,
  avg SL distance ($), open%, total pts
"""
import sys, time, requests, subprocess, statistics
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from analysis.smc_analyzer import SMCAnalyzer

_ET = ZoneInfo("America/New_York")

SYMBOLS     = ["GOOGL", "NVDA", "MSTR", "TSLA"]
GOLD        = {"tp_pct": 0.55, "sl_atr_mult": 0.50,
               "min_expansion_atr": 1.00, "entry_zone_min_pct": 0.70}
PEAK_CUTOFF = (11, 45)
SKIP_MONDAY = True
CHUNKS = [
    (datetime(2025, 6,  1,  9, 30, tzinfo=_ET), datetime(2025, 8, 31, 16, 0, tzinfo=_ET)),
    (datetime(2025, 9,  1,  9, 30, tzinfo=_ET), datetime(2025,11, 30, 16, 0, tzinfo=_ET)),
    (datetime(2025,12,  1,  9, 30, tzinfo=_ET), datetime(2026, 2, 28, 16, 0, tzinfo=_ET)),
    (datetime(2026, 3,  1,  9, 30, tzinfo=_ET), datetime(2026, 5, 31, 16, 0, tzinfo=_ET)),
    (datetime(2026, 6,  1,  9, 30, tzinfo=_ET), datetime(2026, 6, 30, 16, 0, tzinfo=_ET)),
]


def get_api_key():
    return subprocess.check_output(
        ["grep", "TWELVE_DATA_API_KEY",
         str(Path(__file__).resolve().parents[1] / "secrets" / "credentials.env")],
        text=True
    ).strip().split("=", 1)[1]


def fetch_year(symbol, apikey):
    all_candles = []
    for i, (s, e) in enumerate(CHUNKS):
        r = requests.get("https://api.twelvedata.com/time_series", params={
            "symbol": symbol, "interval": "5min", "outputsize": 5000,
            "start_date": s.strftime("%Y-%m-%d %H:%M:%S"),
            "end_date":   e.strftime("%Y-%m-%d %H:%M:%S"),
            "timezone": "America/New_York",
            "apikey": apikey, "format": "JSON", "order": "ASC",
        }, timeout=30)
        data = r.json()
        if "values" not in data:
            raise RuntimeError(f"{symbol} chunk {i+1}: {data.get('message', data)}")
        for v in data["values"]:
            dt = datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_ET)
            all_candles.append({
                "timestamp": int(dt.timestamp()),
                "open": float(v["open"]), "high": float(v["high"]),
                "low":  float(v["low"]),  "close": float(v["close"]),
                "volume": int(float(v.get("volume", 0))),
            })
        print(f"    chunk {i+1}/5: {len(data['values'])} bars", end="\r", flush=True)
        if i < len(CHUNKS) - 1:
            time.sleep(8)   # stay under 8 credits/min

    seen, out = set(), []
    for c in all_candles:
        if c["timestamp"] not in seen:
            seen.add(c["timestamp"]); out.append(c)
    out.sort(key=lambda x: x["timestamp"])
    return out


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


def run_symbol(symbol, apikey):
    print(f"\n{symbol}: fetching …")
    try:
        all5m = fetch_year(symbol, apikey)
    except RuntimeError as e:
        print(f"  ERROR: {e}")
        return None
    all15m = resample_15m(all5m)
    print(f"  {len(all5m)} bars total            ")

    dates = sorted(set(datetime.fromtimestamp(c["timestamp"], _ET).date() for c in all5m))
    dates = [d for d in dates if d.weekday() < 5]
    if SKIP_MONDAY:
        dates = [d for d in dates if d.weekday() != 0]

    ana    = SMCAnalyzer()
    params = {**GOLD, "symbol": symbol}
    rows   = []

    for d in dates:
        s  = datetime(d.year, d.month, d.day,  9, 30, tzinfo=_ET)
        e  = datetime(d.year, d.month, d.day, 12, 30, tzinfo=_ET)
        mc = datetime(d.year, d.month, d.day, 16,  0, tzinfo=_ET)
        ss, se, mc_ts = int(s.timestamp()), int(e.timestamp()), int(mc.timestamp())

        sess_15m = [c for c in all15m if ss <= c["timestamp"] <= se]
        pre_15m  = [c for c in all15m if c["timestamp"] < ss][-16:]
        day_5m   = [c for c in all5m  if ss <= c["timestamp"] <= mc_ts]
        if len(sess_15m) < 3 or len(day_5m) < 6:
            continue

        sigs = ana.detect_dax_session_setup(sess_15m, day_5m, params=params,
                                             candles_15m_presession=pre_15m)
        if not sigs: continue
        sig = sigs[0]
        pt  = datetime.fromtimestamp(sig["peak_ts"], tz=_ET)
        if (pt.hour, pt.minute) >= PEAK_CUTOFF: continue

        is_short = sig["direction"] == "bearish"
        entry, sl, tp = sig["entry"], sig["sl"], sig["tp"]
        post = [c for c in day_5m if c["timestamp"] > sig["breakout_ts"]]
        oc, r = evaluate(entry, sl, tp, is_short, post)

        rows.append({
            "date":    d,
            "dir":     "SHORT" if is_short else "LONG",
            "entry":   entry, "tp": tp, "sl": sl,
            "range":   sig["expansion_range"],
            "outcome": oc, "r": r,
            "sl_dist": abs(entry - sl),
            "pts":     abs(entry - tp) if oc == "WIN" else (-abs(entry - sl) if oc == "LOSS" else None),
        })

    n_sessions = len(dates)
    return {"symbol": symbol, "rows": rows, "n_sessions": n_sessions}


# ── Main ──────────────────────────────────────────────────────────────────────

apikey  = get_api_key()
results = {}

for sym in SYMBOLS:
    res = run_symbol(sym, apikey)
    if res:
        results[sym] = res
    print(f"  waiting 60s before next symbol …")
    time.sleep(60)

# ── Comparison table ──────────────────────────────────────────────────────────

print("\n\n" + "=" * 90)
print("SERPE STOCK COMPARISON — Jun 2025 to Jun 2026  (tp=55%, ez=70%)")
print("(MSFT reference from previous run added manually)")
print("=" * 90)

MSFT_REF = {
    "symbol": "MSFT", "n_sig": 75, "n_closed": 65, "n_open": 10,
    "wr": 1.00, "avg_r": 2.05, "avg_pts": 2.6, "avg_sl": None,
    "total_pts": 167.3, "n_sessions": 218,
}

header = (f"\n  {'Sym':>5}  {'Sigs':>5}  {'Sig/wk':>7}  {'Closed':>7}  "
          f"{'Open%':>6}  {'WR':>5}  {'avg_R':>6}  {'avg_pts':>8}  "
          f"{'avg_SL$':>8}  {'total_pts':>10}")
print(header)
print("  " + "-" * 80)

def print_row(sym, rows, n_sessions, ref=None):
    if ref:
        n_sig, n_cl, n_op = ref["n_sig"], ref["n_closed"], ref["n_open"]
        wr    = ref["wr"]
        avg_r = ref["avg_r"]
        avg_pts = ref["avg_pts"]
        avg_sl  = "  n/a"
        tot_pts = ref["total_pts"]
        weeks   = n_sessions / 4
    else:
        n_sig = len(rows)
        closed = [r for r in rows if r["outcome"] != "OPEN"]
        opens  = [r for r in rows if r["outcome"] == "OPEN"]
        wins   = [r for r in closed if r["outcome"] == "WIN"]
        n_cl, n_op = len(closed), len(opens)
        wr     = len(wins) / n_cl if n_cl else 0
        rs     = [r["r"]   for r in closed if r["r"]   is not None]
        pts    = [r["pts"] for r in closed if r["pts"] is not None]
        sls    = [r["sl_dist"] for r in rows]
        avg_r  = statistics.mean(rs)   if rs  else 0
        avg_pts = statistics.mean(pts) if pts else 0
        avg_sl  = f"${statistics.mean(sls):>5.2f}" if sls else "  n/a"
        tot_pts = sum(pts) if pts else 0
        weeks   = n_sessions / 4

    open_pct = f"{n_op/n_sig*100:.0f}%" if n_sig else "n/a"
    print(f"  {sym:>5}  {n_sig:>5}  {n_sig/weeks:>7.1f}  {n_cl:>7}  "
          f"{open_pct:>6}  {wr:>5.0%}  {avg_r:>+6.2f}R  {avg_pts:>+7.1f}  "
          f"  {avg_sl if isinstance(avg_sl, str) else avg_sl:>7}  {tot_pts:>+10.1f}")

print_row("MSFT", [], 218, ref=MSFT_REF)
for sym in SYMBOLS:
    if sym in results:
        r = results[sym]
        print_row(sym, r["rows"], r["n_sessions"])

# ── Per-symbol detail ─────────────────────────────────────────────────────────

for sym in SYMBOLS:
    if sym not in results:
        continue
    rows = results[sym]["rows"]
    if not rows:
        continue

    closed = [r for r in rows if r["outcome"] != "OPEN"]
    wins   = [r for r in closed if r["outcome"] == "WIN"]

    print(f"\n\n{'─'*70}")
    print(f"{sym} — {len(rows)} signals  |  {len(closed)} closed  |  "
          f"WR {len(wins)/len(closed):.0%}  |  "
          f"avg SL ${statistics.mean(r['sl_dist'] for r in rows):.2f}")
    print(f"{'─'*70}")
    print(f"  {'#':>3}  {'Date':>10}  {'Dir':>5}  {'Entry':>8}  {'TP':>8}  "
          f"{'SL':>8}  {'SL$':>6}  {'Oc':>6}  {'R':>6}  {'Pts':>7}")
    print(f"  {'─'*75}")

    cum = 0.0
    for i, r in enumerate(rows, 1):
        r_s   = f"{r['r']:>+5.2f}R" if r["r"]   is not None else "  OPEN"
        pts_s = f"{r['pts']:>+6.1f}" if r["pts"] is not None else "   ---"
        if r["pts"] is not None:
            cum += r["pts"]
        print(f"  {i:>3}  {str(r['date']):>10}  {r['dir']:>5}  {r['entry']:>8.2f}  "
              f"{r['tp']:>8.2f}  {r['sl']:>8.2f}  "
              f"${r['sl_dist']:>5.2f}  {r['outcome']:>6}  {r_s}  {pts_s}")

    print(f"\n  Cumulative pts (closed): {cum:+.1f}")

print("\nDone.")
