#!/usr/bin/env python3
"""
TSLA dual-strategy backtest — Jun 2025 to Jun 2026.

Strategy A — SERPE (counter-trend):
  Expansion → first LH/HL close to peak (≥70% from origin) → fade back to origin
  Entry: LH/HL close  |  SL: above peak  |  TP: 55% toward origin

Strategy B — Continuation (momentum):
  Expansion → pullback (≥30% retrace from peak) → HL/LH → enter in expansion direction
  Entry: HL/LH close  |  SL: below HL / above LH  |  TP: peak + N × expansion_range

Both use identical BOS + expansion detection (same 15m foundation).
"""
import sys, time, json, requests, subprocess, statistics
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from analysis.smc_analyzer import SMCAnalyzer

_ET    = ZoneInfo("America/New_York")
SYMBOL = "TSLA"
CACHE  = Path(__file__).parent / "tsla_5m_cache.json"

PEAK_CUTOFF  = (11, 45)   # ET
SKIP_MONDAY  = True
MIN_PULLBACK = 0.30       # HL must form after ≥30% retrace from peak
MAX_PULLBACK = 0.70       # HL must form before >70% retrace (still above origin side)
ATR_MULT     = 0.50       # SL buffer in ATR units (same as SERPE)

SERPE_PARAMS = {
    "tp_pct": 0.55, "sl_atr_mult": 0.50,
    "min_expansion_atr": 1.00, "entry_zone_min_pct": 0.70,
    "symbol": SYMBOL,
}


def get_api_key():
    return subprocess.check_output(
        ["grep", "TWELVE_DATA_API_KEY",
         str(Path(__file__).resolve().parents[1] / "secrets" / "credentials.env")],
        text=True
    ).strip().split("=", 1)[1]


def fetch_and_cache():
    if CACHE.exists():
        print(f"Loading cached TSLA data from {CACHE.name} …")
        with open(CACHE) as f:
            return json.load(f)
    apikey = get_api_key()
    chunks = [
        ("2025-06-01 09:30:00", "2025-08-31 16:00:00"),
        ("2025-09-01 09:30:00", "2025-11-30 16:00:00"),
        ("2025-12-01 09:30:00", "2026-02-28 16:00:00"),
        ("2026-03-01 09:30:00", "2026-05-31 16:00:00"),
        ("2026-06-01 09:30:00", "2026-06-30 16:00:00"),
    ]
    all_candles = []
    print("Fetching TSLA 5m data …")
    for i, (s, e) in enumerate(chunks):
        r = requests.get("https://api.twelvedata.com/time_series", params={
            "symbol": SYMBOL, "interval": "5min", "outputsize": 5000,
            "start_date": s, "end_date": e, "timezone": "America/New_York",
            "apikey": apikey, "format": "JSON", "order": "ASC",
        }, timeout=30)
        data = r.json()
        if "values" not in data:
            raise RuntimeError(data.get("message", data))
        for v in data["values"]:
            dt = datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_ET)
            all_candles.append({"timestamp": int(dt.timestamp()),
                                 "open": float(v["open"]), "high": float(v["high"]),
                                 "low": float(v["low"]),   "close": float(v["close"]),
                                 "volume": int(float(v.get("volume", 0)))})
        print(f"  chunk {i+1}/5: {len(data['values'])} bars")
        if i < len(chunks) - 1:
            time.sleep(8)
    seen, out = set(), []
    for c in all_candles:
        if c["timestamp"] not in seen:
            seen.add(c["timestamp"]); out.append(c)
    out.sort(key=lambda x: x["timestamp"])
    with open(CACHE, "w") as f:
        json.dump(out, f)
    print(f"Cached {len(out)} bars to {CACHE.name}")
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


def find_continuation(sig, day_5m, ext_pct):
    """
    After SERPE's LH/HL candle (breakout_ts), look for a continuation setup.

    For BULLISH expansion (SHORT in SERPE, LONG in continuation):
      - Pullback: price falls from peak toward origin
      - HL forms at 30–70% retrace from peak
      - Entry LONG at HL close
      - SL below HL low - ATR buffer
      - TP = peak + ext_pct × expansion_range

    Returns dict or None.
    """
    bullish_exp = sig["expansion_dir"] == "bullish"
    origin      = sig["origin"]
    peak        = sig["peak"]
    exp_range   = sig["expansion_range"]
    peak_ts     = sig["peak_ts"]

    post_peak = [c for c in day_5m if c["timestamp"] > peak_ts]
    if len(post_peak) < 3:
        return None

    atr = SMCAnalyzer().calculate_atr(list(reversed(post_peak[:60]))) or exp_range * 0.005

    # Retrace thresholds (in price terms)
    if bullish_exp:
        # peak is HIGH, pulling back DOWN
        min_pullback_price = peak - MIN_PULLBACK * exp_range   # 30% retrace floor
        max_pullback_price = peak - MAX_PULLBACK * exp_range   # 70% retrace ceiling (origin side)
        tp = peak + ext_pct * exp_range
    else:
        # peak is LOW (bearish expansion), pulling back UP
        min_pullback_price = peak + MIN_PULLBACK * exp_range
        max_pullback_price = peak + MAX_PULLBACK * exp_range
        tp = peak - ext_pct * exp_range

    # Scan for HL (bullish) or LH (bearish) in the pullback zone
    prev_low  = post_peak[0]["low"]   if bullish_exp else None
    prev_high = post_peak[0]["high"]  if not bullish_exp else None

    for i in range(1, len(post_peak)):
        cur = post_peak[i]

        if bullish_exp:
            # HL: low > previous low, AND close is in the pullback zone
            in_zone = max_pullback_price <= cur["close"] <= min_pullback_price
            is_hl   = cur["low"] > prev_low
            if in_zone and is_hl:
                entry = cur["close"]
                sl    = cur["low"] - ATR_MULT * atr
                post  = [c for c in day_5m if c["timestamp"] > cur["timestamp"]]
                return {"entry": entry, "sl": sl, "tp": tp,
                        "is_short": False, "ts": cur["timestamp"],
                        "sl_dist": abs(entry - sl),
                        "retrace_pct": (peak - entry) / exp_range}
            prev_low = min(prev_low, cur["low"])

            # Stop if price goes below origin — continuation invalidated
            if cur["close"] < origin:
                break

        else:
            # LH: high < previous high, AND close is in the pullback zone
            in_zone = min_pullback_price >= cur["close"] >= max_pullback_price
            is_lh   = cur["high"] < prev_high
            if in_zone and is_lh:
                entry = cur["close"]
                sl    = cur["high"] + ATR_MULT * atr
                post  = [c for c in day_5m if c["timestamp"] > cur["timestamp"]]
                return {"entry": entry, "sl": sl, "tp": tp,
                        "is_short": True, "ts": cur["timestamp"],
                        "sl_dist": abs(entry - sl),
                        "retrace_pct": (entry - peak) / exp_range}
            prev_high = max(prev_high, cur["high"])

            if cur["close"] > origin:
                break

    return None


# ── Load data ─────────────────────────────────────────────────────────────────

all5m  = fetch_and_cache()
all15m = resample_15m(all5m)
print(f"  {len(all5m)} 5m bars  |  {len(all15m)} 15m bars\n")

dates = sorted(set(datetime.fromtimestamp(c["timestamp"], _ET).date() for c in all5m))
dates = [d for d in dates if d.weekday() < 5]
if SKIP_MONDAY:
    dates = [d for d in dates if d.weekday() != 0]

# ── Run both strategies ───────────────────────────────────────────────────────

ana = SMCAnalyzer()
rows_serpe = []
rows_cont  = {0.50: [], 1.00: [], 1.50: []}   # keyed by extension_pct

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

    sigs = ana.detect_dax_session_setup(
        sess_15m, day_5m, params=SERPE_PARAMS, candles_15m_presession=pre_15m
    )
    if not sigs:
        continue
    sig = sigs[0]
    pt  = datetime.fromtimestamp(sig["peak_ts"], tz=_ET)
    if (pt.hour, pt.minute) >= PEAK_CUTOFF:
        continue

    is_short = sig["direction"] == "bearish"

    # ── Strategy A: SERPE ─────────────────────────────────────────────────────
    entry, sl, tp = sig["entry"], sig["sl"], sig["tp"]
    post = [c for c in day_5m if c["timestamp"] > sig["breakout_ts"]]
    oc, r = evaluate(entry, sl, tp, is_short, post)
    rows_serpe.append({
        "date": d, "dir": "SHORT" if is_short else "LONG",
        "entry": entry, "tp": tp, "sl": sl,
        "outcome": oc, "r": r,
        "sl_dist": abs(entry - sl),
        "pts": abs(entry - tp) if oc == "WIN" else (-abs(entry - sl) if oc == "LOSS" else None),
    })

    # ── Strategy B: Continuation ──────────────────────────────────────────────
    for ext in rows_cont:
        setup = find_continuation(sig, day_5m, ext)
        if setup is None:
            continue
        post_c = [c for c in day_5m if c["timestamp"] > setup["ts"]]
        oc_c, r_c = evaluate(setup["entry"], setup["sl"], setup["tp"],
                             setup["is_short"], post_c)
        rows_cont[ext].append({
            "date":    d,
            "dir":     "SHORT" if setup["is_short"] else "LONG",
            "entry":   setup["entry"], "tp": setup["tp"], "sl": setup["sl"],
            "sl_dist": setup["sl_dist"],
            "retrace": setup["retrace_pct"],
            "outcome": oc_c, "r": r_c,
            "pts": abs(setup["entry"] - setup["tp"]) if oc_c == "WIN"
                   else (-setup["sl_dist"] if oc_c == "LOSS" else None),
        })


# ── Summary ───────────────────────────────────────────────────────────────────

def stats(rows):
    closed = [r for r in rows if r["outcome"] != "OPEN"]
    opens  = [r for r in rows if r["outcome"] == "OPEN"]
    if not closed:
        return None
    wins   = [r for r in closed if r["outcome"] == "WIN"]
    rs     = [r["r"]   for r in closed if r["r"]   is not None]
    pts    = [r["pts"] for r in closed if r["pts"] is not None]
    sls    = [r["sl_dist"] for r in rows]
    return dict(
        n_sig=len(rows), n_cl=len(closed), n_op=len(opens),
        wins=len(wins), losses=len(closed)-len(wins),
        wr=len(wins)/len(closed),
        avg_r=statistics.mean(rs) if rs else 0,
        avg_pts=statistics.mean(pts) if pts else 0,
        total_pts=sum(pts) if pts else 0,
        avg_sl=statistics.mean(sls) if sls else 0,
    )

sA = stats(rows_serpe)

print("\n" + "=" * 78)
print("TSLA DUAL STRATEGY — Jun 2025 to Jun 2026")
print("=" * 78)
print(f"\n{'Strategy':<35} {'Sigs':>5}  {'Cls':>4}  {'Open%':>6}  {'WR':>5}  "
      f"{'avg_R':>6}  {'avg_pts':>8}  {'avg_SL$':>8}  {'total_pts':>10}")
print("-" * 78)

def row_str(label, s):
    if not s: return f"  {label:<33}  (no signals)"
    op_pct = f"{s['n_op']/s['n_sig']*100:.0f}%"
    return (f"  {label:<33} {s['n_sig']:>5}  {s['n_cl']:>4}  {op_pct:>6}  "
            f"{s['wr']:>5.0%}  {s['avg_r']:>+5.2f}R  {s['avg_pts']:>+7.1f}  "
            f"${s['avg_sl']:>6.2f}  {s['total_pts']:>+10.1f}")

print(row_str("A  SERPE (counter-trend)", sA))
for ext in sorted(rows_cont):
    sB = stats(rows_cont[ext])
    print(row_str(f"B{ext:.0%}  Continuation TP={ext:.0%} ext", sB))

# ── Day-by-day where BOTH fire: interesting comparison ────────────────────────

both_days = {}
for r in rows_serpe:
    both_days[r["date"]] = {"serpe": r}
for ext in [1.00]:   # use 100% extension for the comparison
    for r in rows_cont[ext]:
        if r["date"] in both_days:
            both_days[r["date"]]["cont"] = r

overlap = {d: v for d, v in both_days.items() if "serpe" in v and "cont" in v}

print(f"\n\nDays where BOTH strategies fire (same session): {len(overlap)}")
print(f"{'Date':>10}  {'SERPE_dir':>10}  {'SERPE_oc':>8}  {'SERPE_R':>7}  "
      f"{'CONT_dir':>9}  {'CONT_oc':>8}  {'CONT_R':>7}")
print("-" * 75)
for d in sorted(overlap):
    v  = overlap[d]
    sa = v["serpe"]
    sb = v["cont"]
    sr = f"{sa['r']:>+6.2f}R" if sa["r"] is not None else "  OPEN"
    cr = f"{sb['r']:>+6.2f}R" if sb["r"] is not None else "  OPEN"
    print(f"{str(d):>10}  {sa['dir']:>10}  {sa['outcome']:>8}  {sr}  "
          f"{sb['dir']:>9}  {sb['outcome']:>8}  {cr}")

# ── Full Continuation detail (100% ext) ───────────────────────────────────────

ext = 1.00
rows = rows_cont[ext]
sB   = stats(rows)
if sB:
    print(f"\n\n{'─'*80}")
    print(f"B100%  Continuation full detail (TP = peak + 1× expansion)")
    print(f"{'─'*80}")
    print(f"  {'#':>3}  {'Date':>10}  {'Dir':>5}  {'Entry':>7}  {'TP':>7}  "
          f"{'SL':>7}  {'SL$':>6}  {'Ret%':>5}  {'Oc':>6}  {'R':>6}  {'Pts':>7}")
    print(f"  {'─'*76}")
    cum = 0.0
    for i, r in enumerate(rows, 1):
        r_s   = f"{r['r']:>+5.2f}R" if r["r"]   is not None else "  OPEN"
        pts_s = f"{r['pts']:>+6.1f}" if r["pts"] is not None else "   ---"
        if r["pts"] is not None:
            cum += r["pts"]
        print(f"  {i:>3}  {str(r['date']):>10}  {r['dir']:>5}  {r['entry']:>7.2f}  "
              f"{r['tp']:>7.2f}  {r['sl']:>7.2f}  ${r['sl_dist']:>5.2f}  "
              f"{r['retrace']:>4.0%}  {r['outcome']:>6}  {r_s}  {pts_s}")
    print(f"\n  Cumulative pts: {cum:+.1f}")

print("\nDone.")
