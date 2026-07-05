#!/usr/bin/env python3
"""
MSFT SERPE backtest — Jun 2025 to Jun 2026.

Fetches 1 year of MSFT 5m data from Twelve Data (5 API calls),
then runs the SERPE algo on the US morning session (9:30–12:30 ET).

Session logic mirrors the DAX script:
  - Signal detection : 09:30–12:30 ET
  - Pre-session ctx  : last 16×15m candles before 09:30 (prev day afternoon)
  - Evaluation lookahead: up to 16:00 ET (market close)
  - Peak cutoff       : 11:45 ET
  - Skip Mondays      : True
"""
import sys, time, requests, subprocess, statistics
from pathlib import Path
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from analysis.smc_analyzer import SMCAnalyzer

_ET  = ZoneInfo("America/New_York")
_ISR = ZoneInfo("Asia/Jerusalem")

SYMBOL = sys.argv[1] if len(sys.argv) > 1 else "MSFT"

GOLD = {
    "tp_pct":             0.55,
    "sl_atr_mult":        0.50,
    "min_expansion_atr":  1.00,
    "entry_zone_min_pct": 0.70,
    "symbol":             SYMBOL,
}
PEAK_CUTOFF = (11, 45)   # ET
SKIP_MONDAY = True


def get_api_key():
    return subprocess.check_output(
        ["grep", "TWELVE_DATA_API_KEY",
         str(Path(__file__).resolve().parents[1] / "secrets" / "credentials.env")],
        text=True
    ).strip().split("=", 1)[1]


def fetch_chunk(symbol, start_dt, end_dt, apikey):
    """Fetch up to 5000 5m bars in a date range (ET times)."""
    r = requests.get("https://api.twelvedata.com/time_series", params={
        "symbol":     symbol,
        "interval":   "5min",
        "outputsize": 5000,
        "start_date": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "end_date":   end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "timezone":   "America/New_York",
        "apikey":     apikey,
        "format":     "JSON",
        "order":      "ASC",
    }, timeout=30)
    data = r.json()
    if "values" not in data:
        raise RuntimeError(f"API error: {data.get('message', data)}")
    candles = []
    for v in data["values"]:
        dt = datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_ET)
        candles.append({
            "timestamp": int(dt.timestamp()),
            "open":  float(v["open"]),
            "high":  float(v["high"]),
            "low":   float(v["low"]),
            "close": float(v["close"]),
            "volume": int(float(v.get("volume", 0))),
        })
    return candles


def fetch_year(symbol):
    """Pull ~1 year of 5m data in quarterly chunks."""
    apikey = get_api_key()
    chunks = [
        (datetime(2025, 6,  1,  9, 30, tzinfo=_ET), datetime(2025, 8, 31, 16, 0, tzinfo=_ET)),
        (datetime(2025, 9,  1,  9, 30, tzinfo=_ET), datetime(2025, 11,30, 16, 0, tzinfo=_ET)),
        (datetime(2025,12,  1,  9, 30, tzinfo=_ET), datetime(2026,  2,28, 16, 0, tzinfo=_ET)),
        (datetime(2026, 3,  1,  9, 30, tzinfo=_ET), datetime(2026,  5,31, 16, 0, tzinfo=_ET)),
        (datetime(2026, 6,  1,  9, 30, tzinfo=_ET), datetime(2026,  6,30, 16, 0, tzinfo=_ET)),
    ]
    all_candles = []
    for i, (s, e) in enumerate(chunks):
        print(f"  Fetching chunk {i+1}/{len(chunks)}: {s.date()} → {e.date()} …", end=" ", flush=True)
        c = fetch_chunk(symbol, s, e, apikey)
        print(f"{len(c)} bars")
        all_candles.extend(c)
        if i < len(chunks) - 1:
            time.sleep(1)   # rate-limit courtesy pause

    # Deduplicate and sort
    seen = set()
    out  = []
    for c in all_candles:
        if c["timestamp"] not in seen:
            seen.add(c["timestamp"])
            out.append(c)
    out.sort(key=lambda x: x["timestamp"])
    return out


def resample_15m(candles_5m):
    out, i = [], 0
    while i < len(candles_5m):
        ts0 = candles_5m[i]["timestamp"]
        al  = (ts0 // 900) * 900
        g   = [c for c in candles_5m[i:i+3] if c["timestamp"] < al + 900]
        if not g: i += 1; continue
        out.append({"timestamp": al,
                    "open":  g[0]["open"],
                    "high":  max(c["high"] for c in g),
                    "low":   min(c["low"]  for c in g),
                    "close": g[-1]["close"], "volume": 0})
        i += len(g)
    return out


def session_ts(d):
    """9:30–12:30 ET as UTC timestamps."""
    s = datetime(d.year, d.month, d.day,  9, 30, tzinfo=_ET)
    e = datetime(d.year, d.month, d.day, 12, 30, tzinfo=_ET)
    return int(s.timestamp()), int(e.timestamp())


def market_close_ts(d):
    c = datetime(d.year, d.month, d.day, 16, 0, tzinfo=_ET)
    return int(c.timestamp())


def evaluate(entry, sl, tp, is_short, post_5m):
    risk = abs(entry - sl) or 1.0
    rwd  = abs(entry - tp)
    for bar in post_5m:
        if is_short:
            if bar["high"] >= sl: return "LOSS", -1.0
            if bar["low"]  <= tp: return "WIN",  rwd / risk
        else:
            if bar["low"]  <= sl: return "LOSS", -1.0
            if bar["high"] >= tp: return "WIN",  rwd / risk
    return "OPEN", None


# ── Fetch data ────────────────────────────────────────────────────────────────

print(f"Fetching {SYMBOL} 5m data from Twelve Data …")
all5m  = fetch_year(SYMBOL)
all15m = resample_15m(all5m)
print(f"Total: {len(all5m)} 5m candles  |  {len(all15m)} 15m candles\n")

# ── Build sessions ────────────────────────────────────────────────────────────

dates = sorted(set(
    datetime.fromtimestamp(c["timestamp"], _ET).date() for c in all5m
))
dates = [d for d in dates if d.weekday() < 5]
if SKIP_MONDAY:
    dates = [d for d in dates if d.weekday() != 0]

print(f"Trading days (Tue–Fri): {len(dates)}")

sessions = []
for d in dates:
    ss, se  = session_ts(d)
    mc      = market_close_ts(d)
    sess_15m = [c for c in all15m if ss <= c["timestamp"] <= se]
    pre_15m  = [c for c in all15m if c["timestamp"] < ss][-16:]
    day_5m   = [c for c in all5m  if ss <= c["timestamp"] <= mc]
    if len(sess_15m) >= 3 and len(day_5m) >= 6:
        sessions.append((d, ss, se, mc, sess_15m, pre_15m, day_5m))

print(f"Sessions with data:     {len(sessions)}\n")

# ── Run SERPE ─────────────────────────────────────────────────────────────────

ana     = SMCAnalyzer()
rows    = []
by_month   = defaultdict(list)
by_dir     = defaultdict(list)

for trade_date, ss, se, mc, sess_15m, pre_15m, day_5m in sessions:
    sigs = ana.detect_dax_session_setup(
        sess_15m, day_5m, params=GOLD, candles_15m_presession=pre_15m
    )
    if not sigs:
        continue

    sig = sigs[0]
    pt  = datetime.fromtimestamp(sig["peak_ts"], tz=_ET)
    if (pt.hour, pt.minute) >= PEAK_CUTOFF:
        continue

    is_short = sig["direction"] == "bearish"
    entry    = sig["entry"]
    sl       = sig["sl"]
    tp       = sig["tp"]
    bts      = sig["breakout_ts"]
    post     = [c for c in day_5m if c["timestamp"] > bts]

    oc, r    = evaluate(entry, sl, tp, is_short, post)
    pts      = abs(entry - tp) if oc == "WIN" else (-abs(entry - sl) if oc == "LOSS" else None)
    ent_pct  = sig.get("entry_pct_from_origin",
                        abs(entry - sig["origin"]) / max(sig["expansion_range"], 0.01))

    row = {
        "date":    trade_date,
        "dir":     "SHORT" if is_short else "LONG",
        "peak_t":  pt.strftime("%H:%M"),
        "entry":   entry,
        "tp":      tp,
        "sl":      sl,
        "range":   sig["expansion_range"],
        "ent_pct": ent_pct,
        "outcome": oc,
        "r":       r,
        "pts":     pts,
    }
    rows.append(row)
    by_month[trade_date.strftime("%Y-%m")].append(row)
    by_dir[sig["expansion_dir"]].append(row)

# ── Summary ───────────────────────────────────────────────────────────────────

def stats(rlist):
    closed = [r for r in rlist if r["outcome"] != "OPEN"]
    if not closed:
        return None
    wins   = [r for r in closed if r["outcome"] == "WIN"]
    rs     = [r["r"]   for r in closed if r["r"]   is not None]
    pts    = [r["pts"] for r in closed if r["pts"] is not None]
    wr     = len(wins) / len(closed)
    ev     = sum(rs) / len(rs) if rs else 0
    return dict(n=len(closed), wins=len(wins), losses=len(closed)-len(wins),
                wr=wr, avg_r=statistics.mean(rs) if rs else 0,
                ev=ev, avg_pts=statistics.mean(pts) if pts else 0,
                total_pts=sum(pts) if pts else 0)

s = stats(rows)
if not s:
    print("No signals found.")
    sys.exit()

print("=" * 68)
print(f"{SYMBOL} SERPE — Jun 2025 to Jun 2026  (gold params: tp=55%, ez=70%)")
print("=" * 68)
print(f"\nTotal signals : {len(rows)}  ({len(rows)/len(sessions)*100:.0f}% of sessions)")
print(f"Closed trades : {s['n']}   ({s['wins']}W / {s['losses']}L)")
print(f"Win rate      : {s['wr']:.0%}")
print(f"Avg R         : {s['avg_r']:+.2f}R")
print(f"EV            : {s['ev']:+.2f}R")
print(f"Avg pts/trade : {s['avg_pts']:+.1f}")
print(f"Total pts     : {s['total_pts']:+.0f}")

print("\nBy direction:")
for d in ["bullish", "bearish"]:
    label = "Bull exp → SHORT" if d == "bullish" else "Bear exp → LONG"
    sd = stats(by_dir[d])
    if sd:
        print(f"  {label:20s}: {sd['n']:3d} trades  WR {sd['wr']:.0%}  "
              f"avg_R {sd['avg_r']:+.2f}R  avg_pts {sd['avg_pts']:+.1f}")

print("\nBy month:")
for m in sorted(by_month):
    sm = stats(by_month[m])
    if sm:
        bar = "W" * sm["wins"] + "L" * sm["losses"]
        print(f"  {m}:  {sm['n']:3d} trades  WR {sm['wr']:.0%}  "
              f"avg_pts {sm['avg_pts']:+.1f}  {bar}")

print(f"\n{'#':>3}  {'Date':>10}  {'Dir':>5}  {'Peak':>5}  {'Entry':>7}  "
      f"{'TP':>7}  {'SL':>7}  {'Range':>6}  {'Ent%':>5}  {'Oc':>6}  {'R':>6}  {'Pts':>6}")
print("-" * 92)

cum = 0.0
for i, r in enumerate(rows, 1):
    r_s   = f"{r['r']:>+5.2f}R"   if r["r"]   is not None else "  OPEN"
    pts_s = f"{r['pts']:>+5.1f}"  if r["pts"] is not None else "  ---"
    if r["pts"] is not None:
        cum += r["pts"]
    print(f"{i:>3}  {str(r['date']):>10}  {r['dir']:>5}  {r['peak_t']:>5}  "
          f"{r['entry']:>7.2f}  {r['tp']:>7.2f}  {r['sl']:>7.2f}  "
          f"{r['range']:>6.2f}  {r['ent_pct']:>4.0%}  {r['outcome']:>6}  {r_s}  {pts_s}")

print(f"\nCumulative pts (closed trades): {cum:+.1f}")
print("\nDone.")
