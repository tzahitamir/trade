"""
Backtest: 5m-early entry for XAUUSD BOS15m signals — ATR filter sweep.

For each confirmed 15m BOS alert (gold params), measures how different
minimum-distance filters (N × 5m-ATR past the structural level) affect:
  - Signal count retained
  - Fake-out rate (5m crosses level but 15m closes back without confirming)
  - In-period stop risk (after 5m-early entry, price wicks back to SL
    before the 15m candle closes)
  - Lead time and entry improvement preserved

Run from src/:  ../.venv/bin/python backtest_5m_early.py
"""
import sys, os
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from db.local_db import LocalDB
from alerts.alert_manager import AlertManager
from analysis.smc_analyzer import SMCAnalyzer
from config.settings import Settings

# ── Config ─────────────────────────────────────────────────────────────────
SYMBOL     = "XAUUSD"
PERIOD_15M = 900   # seconds
CONTEXT_15M = 200

ATR_THRESHOLDS = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5]   # multiples of 5m ATR
ATR_PERIOD     = 14                                   # candles for ATR

settings = Settings.load_from_yaml(str(Path(__file__).parents[1] / "config.yaml"))
db       = LocalDB(settings.db_path)
am       = AlertManager(settings, db=db)
analyzer = SMCAnalyzer()

# ── Load candle data ────────────────────────────────────────────────────────
print("Loading candles…")
all_15m = db.query_recent(SYMBOL, "15m", limit=50000)
all_4h  = db.query_recent(SYMBOL, "4h",  limit=5000)
all_5m  = db.query_recent(SYMBOL, "5m",  limit=200000)

c15_by_ts = {c["timestamp"]: c for c in all_15m}
c5_by_ts  = {c["timestamp"]: c for c in all_5m}
ts15_sorted = sorted(c15_by_ts)
ts5_sorted  = sorted(c5_by_ts)

T_START = min(c5_by_ts)
T_END   = max(c5_by_ts)
print(f"5m window: {datetime.fromtimestamp(T_START, tz=timezone.utc).strftime('%Y-%m-%d')} "
      f"→ {datetime.fromtimestamp(T_END, tz=timezone.utc).strftime('%Y-%m-%d')} "
      f"({len([t for t in ts5_sorted if T_START <= t <= T_END])} candles)")

# ── 15m BOS scan ────────────────────────────────────────────────────────────
print("Running 15m BOS scan…")
gold_map = db.get_gold_params("BOS15m", SYMBOL)
gold     = gold_map.get(SYMBOL)
gold_params: dict = {}
if gold:
    gold_params = db.get_param_set_by_id(gold["param_set_id"])

bars = [t for t in ts15_sorted if T_START <= t <= T_END
        and 7 <= datetime.fromtimestamp(t, tz=timezone.utc).hour < 21]

confirmed_bos = []
seen_levels: set = set()

for bar_ts in bars:
    ctx_ts  = [t for t in ts15_sorted if t <= bar_ts][-CONTEXT_15M:]
    context = [c15_by_ts[t] for t in reversed(ctx_ts)]
    if len(context) < 60:
        continue
    htf_bias = am.analyzer.get_htf_bias(all_4h, bar_ts)
    alerts   = am.evaluate_production(
        SYMBOL, "15m", context, candles_4h=all_4h,
        htf_bias=htf_bias, gold_params=gold_params,
        min_breakout_ts=bar_ts,
    )
    for a in alerts:
        ev  = a["event"]
        key = (ev["direction"], round(ev.get("broken_level", 0), 2))
        if key in seen_levels:
            continue
        seen_levels.add(key)
        confirmed_bos.append({
            "breakout_ts":  bar_ts,
            "broken_level": ev.get("broken_level", 0),
            "direction":    ev["direction"],
            "entry":        a["entry"],
            "sl":           a["sl"],
            "tp":           a["tp"],
            "htf_bias":     htf_bias,
        })

print(f"Confirmed 15m BOS events: {len(confirmed_bos)}")

# ── Per-signal 5m analysis ──────────────────────────────────────────────────
# For each BOS, find: first 5m close past level, 5m ATR at that bar,
# distance past level, and whether price wicked back to SL before 15m closed.

enriched = []

for bos in confirmed_bos:
    bts      = bos["breakout_ts"]
    level    = bos["broken_level"]
    bullish  = bos["direction"] == "bullish"
    sl       = bos["sl"]
    period_start = bts - PERIOD_15M

    intra_5m = [c5_by_ts[t] for t in ts5_sorted
                if period_start <= t < bts and t in c5_by_ts]

    if not intra_5m:
        enriched.append({**bos, "early_ts": None, "early_close": None,
                         "distance_usd": None, "atr5m": None,
                         "distance_atr": None, "lead_minutes": None,
                         "intra_period_stop": None})
        continue

    # 5m ATR at the time of the breakout period (use candles before the period)
    atr_ctx = [c5_by_ts[t] for t in ts5_sorted if t < period_start][-ATR_PERIOD-5:]
    atr5m   = analyzer.calculate_atr(list(reversed(atr_ctx))) or 1.0

    # Find first 5m close past level
    early_ts    = None
    early_close = None
    for c5 in intra_5m:
        if bullish  and c5["close"] > level:
            early_ts    = c5["timestamp"]
            early_close = c5["close"]
            break
        elif not bullish and c5["close"] < level:
            early_ts    = c5["timestamp"]
            early_close = c5["close"]
            break

    if early_ts is None:
        enriched.append({**bos, "early_ts": None, "early_close": None,
                         "distance_usd": None, "atr5m": atr5m,
                         "distance_atr": None, "lead_minutes": None,
                         "intra_period_stop": None})
        continue

    distance_usd = abs(early_close - level)
    distance_atr = distance_usd / atr5m
    lead_minutes = round((bts - early_ts) / 60)

    # In-period stop risk: after 5m-early entry, do subsequent 5m candles
    # wick to or past SL before the 15m period closes?
    post_entry_5m = [c5_by_ts[t] for t in ts5_sorted
                     if early_ts < t < bts and t in c5_by_ts]
    intra_stop = False
    for c5 in post_entry_5m:
        if bullish  and c5["low"]  <= sl:
            intra_stop = True; break
        elif not bullish and c5["high"] >= sl:
            intra_stop = True; break

    enriched.append({
        **bos,
        "early_ts":         early_ts,
        "early_close":      early_close,
        "distance_usd":     distance_usd,
        "atr5m":            atr5m,
        "distance_atr":     distance_atr,
        "lead_minutes":     lead_minutes,
        "intra_period_stop": intra_stop,
    })

# ── Fake-out analysis (prior-period wicks) ──────────────────────────────────
# For each confirmed BOS, check whether the level was wicked through on 5m
# in the 3 prior 15m periods WITHOUT a 15m close confirmation.
# Each signal gets labelled with its fake-out history.

def had_prior_5m_cross(bos, lookback=3):
    """True if any of the prior N periods had a 5m close past the level
    but the 15m candle of that period did NOT close past it."""
    level   = bos["broken_level"]
    bullish = bos["direction"] == "bullish"
    bts     = bos["breakout_ts"]
    for k in range(1, lookback + 1):
        pe  = bts - k * PERIOD_15M
        ps  = pe  - PERIOD_15M
        prior_5m = [c5_by_ts[t] for t in ts5_sorted if ps <= t < pe and t in c5_by_ts]
        crossed_5m = any((c["close"] > level if bullish else c["close"] < level)
                         for c in prior_5m)
        if not crossed_5m:
            continue
        c15 = c15_by_ts.get(pe)
        if c15 is None:
            continue
        confirmed_on_15m = (c15["close"] > level) if bullish else (c15["close"] < level)
        if not confirmed_on_15m:
            return True  # fake-out in a prior period
    return False

# Tag each enriched record
for r in enriched:
    r["had_prior_fakeout"] = had_prior_5m_cross(r)

# ── ATR threshold sweep ──────────────────────────────────────────────────────

print(f"\n{'='*72}")
print(f"ATR FILTER SWEEP — minimum distance past level = N × 5m-ATR")
print(f"{'='*72}")
print(f"{'Threshold':<12} {'Signals':>8} {'Coverage':>9} {'Fakeout%':>9} "
      f"{'IntrStop%':>10} {'AvgLead':>8} {'AvgImpr$':>9}")
print("-" * 72)

has_early = [r for r in enriched if r["early_ts"] is not None]

for thresh in ATR_THRESHOLDS:
    if thresh == 0.0:
        subset = has_early
    else:
        subset = [r for r in has_early if r["distance_atr"] is not None
                  and r["distance_atr"] >= thresh]

    if not subset:
        print(f"{thresh:.1f}×ATR        {'0':>8}  {'n/a':>8}  {'n/a':>8}  {'n/a':>9}")
        continue

    n          = len(subset)
    coverage   = 100 * n / len(confirmed_bos)
    fakeout_ct = sum(1 for r in subset if r["had_prior_fakeout"])
    fakeout_r  = 100 * fakeout_ct / n
    stop_ct    = sum(1 for r in subset if r["intra_period_stop"])
    stop_r     = 100 * stop_ct / n
    avg_lead   = sum(r["lead_minutes"] for r in subset) / n
    avg_impr   = sum(r["distance_usd"] for r in subset) / n

    print(f"{thresh:.1f}×ATR      {n:>8}  {coverage:>8.0f}%  {fakeout_r:>8.0f}%  "
          f"{stop_r:>9.0f}%  {avg_lead:>7.1f}m  ${avg_impr:>7.2f}")

# ── Detailed 0.3×ATR breakdown ───────────────────────────────────────────────
SHOW_THRESH = 0.3
filtered = [r for r in has_early
            if r["distance_atr"] is not None and r["distance_atr"] >= SHOW_THRESH]

print(f"\n{'='*72}")
print(f"DETAIL: {SHOW_THRESH}×ATR filter — {len(filtered)} signals")
print(f"{'='*72}")

leads = [r["lead_minutes"] for r in filtered]
imprs = [r["distance_usd"] for r in filtered]
stops = [r for r in filtered if r["intra_period_stop"]]
fakes = [r for r in filtered if r["had_prior_fakeout"]]

print(f"Lead time  — min {min(leads)}m / median {sorted(leads)[len(leads)//2]}m / "
      f"mean {sum(leads)/len(leads):.1f}m / max {max(leads)}m")
print(f"Entry impr — min ${min(imprs):.2f} / median ${sorted(imprs)[len(imprs)//2]:.2f} / "
      f"mean ${sum(imprs)/len(imprs):.2f} / max ${max(imprs):.2f}")
print(f"In-period stops:  {len(stops)} / {len(filtered)} ({100*len(stops)/len(filtered):.0f}%)")
print(f"Prior fakeouts:   {len(fakes)} / {len(filtered)} ({100*len(fakes)/len(filtered):.0f}%)")

print(f"\nLead distribution:")
for bucket, lo, hi in [("5m", 0, 5), ("10m", 6, 10), ("15m", 11, 15)]:
    ct = sum(1 for l in leads if lo <= l <= hi)
    print(f"  {bucket}: {ct} ({100*ct/len(leads):.0f}%)")

print(f"\nSignals passing {SHOW_THRESH}×ATR filter:")
print(f"{'Date (UTC)':<17} {'Dir':<8} {'Level':>8} {'5m cls':>8} "
      f"{'Dist$':>7} {'DistATR':>8} {'Lead':>6} {'IntraStop':>10} {'PriorFO':>8}")
print("-" * 84)
for r in sorted(filtered, key=lambda x: x["breakout_ts"]):
    dt  = datetime.fromtimestamp(r["breakout_ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    ec  = f"{r['early_close']:.2f}"
    du  = f"{r['distance_usd']:.2f}"
    da  = f"{r['distance_atr']:.2f}"
    ist = "YES" if r["intra_period_stop"] else "no"
    pf  = "YES" if r["had_prior_fakeout"] else "no"
    print(f"{dt:<17} {r['direction']:<8} {r['broken_level']:>8.2f} {ec:>8} "
          f"{du:>7} {da:>8} {r['lead_minutes']:>5}m {ist:>10} {pf:>8}")

db.close()
print(f"\nDone.")
