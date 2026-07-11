#!/usr/bin/env python3
"""US100 (NAS100) BOS/conf2 research — yfinance NQ=F, London+NY sessions."""
import sys
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from research_framework import (
    SKILL_VERSION, load_yfinance, resample, trading_dates, stats, verdict,
    collect_bos_signals, test_all_confluences, bos_tp_sweep,
    save_research_results, _eval_bos_signals,
)

SYMBOL    = "US100"
YF_SYMBOL = "NQ=F"

print(f"=== {SYMBOL} ({YF_SYMBOL}) ===")

candles_5m  = load_yfinance(YF_SYMBOL, interval="5m", period="59d")
candles_15m = resample(candles_5m, 15)
candles_4h  = resample(candles_5m, 240)
dates = trading_dates(candles_15m, ZoneInfo("UTC"), skip_monday=False, max_years=2.0)
print(f"  {len(candles_5m)} 5m bars | {len(candles_15m)} 15m bars | {len(dates)} trading days")
if len(dates) < 10:
    print("ERROR: too few days"); sys.exit(1)

all_signals = collect_bos_signals(
    candles_signal=candles_15m, candles_ltf=candles_5m,
    dates=dates, sl_atr_mult=0.5, tp_r=2.0,
    swing_lookback=15, htf_candles=candles_4h,
)
# US100: London + NY sessions (08:00-12:00 UTC + 13:30-17:30 UTC)
signals = [s for s in all_signals if s["session_london"] or s["session_ny"]]
bull    = [s for s in signals if s["direction"] == "bullish"]
bear    = [s for s in signals if s["direction"] == "bearish"]
conf2   = [s for s in signals if s["confirmation_bars"] >= 2]
conf1only = [s for s in signals if s["confirmation_bars"] == 1]
conf0   = [s for s in signals if s["confirmation_bars"] == 0]
print(f"  {len(signals)} London+NY signals | {len(conf2)} conf2 ({len(conf2)*100//max(len(signals),1)}%)")

confluences = test_all_confluences(signals, min_n=5, n_trading_days=len(dates))
viable = [r for r in confluences if r["n"] >= 20 and r["wr"] >= 0.65 and r["ev"] >= 0.5]

top3_tp = []
for row in viable[:3]:
    if len(row["_subset"]) >= 5:
        tp = bos_tp_sweep(row["_subset"], tp_rs=[1.5, 2.0, 2.5, 3.0], sl_atr_mults=[0.25, 0.50, 0.75])
        top3_tp.append((row["confluence"], row["params"], tp[:6]))

w = 62
print("\n" + "=" * w)
print(f"  RESEARCH REPORT: {SYMBOL}")
print(f"  Signal: 15m | HTF: 4h | LTF: 5m | Sessions: London+NY | v{SKILL_VERSION}")
print(f"  {len(dates)} trading days | {len(signals)} signals")
print("=" * w)

b  = stats(_eval_bos_signals(signals))
sb = stats(_eval_bos_signals(bull)); se = stats(_eval_bos_signals(bear))
c0 = stats(_eval_bos_signals(conf0)); c1 = stats(_eval_bos_signals(conf1only))
c2 = stats(_eval_bos_signals(conf2))

print(f"\n── BASELINE ──────────────────────────────────────────")
print(f"  All:      N={b['n']:>3}  WR={b['wr']:.0%}  EV={b['ev']:>+.2f}R  {verdict(b)}")
print(f"  Bullish:  N={sb['n']:>3}  WR={sb['wr']:.0%}  EV={sb['ev']:>+.2f}R")
print(f"  Bearish:  N={se['n']:>3}  WR={se['wr']:.0%}  EV={se['ev']:>+.2f}R")
print(f"\n── CONF2 CHECK ───────────────────────────────────────")
print(f"  conf0:  N={c0['n']:>3}  WR={c0['wr']:.0%}  EV={c0['ev']:>+.2f}R")
print(f"  conf1:  N={c1['n']:>3}  WR={c1['wr']:.0%}  EV={c1['ev']:>+.2f}R")
print(f"  conf2+: N={c2['n']:>3}  WR={c2['wr']:.0%}  EV={c2['ev']:>+.2f}R  {verdict(c2)}")
print(f"  conf2 frequency: {len(conf2)} signals / {len(dates)} days = {len(conf2)/(len(dates)/5):.1f}/wk")
print(f"\n── TOP CONFLUENCES ───────────────────────────────────")
print(f"  {'Confluence':<22} {'Params':<16} {'N':>4}  {'WR':>5}  {'EV':>7}  {'~/wk':>5}  Verdict")
print(f"  {'-'*22} {'-'*16} {'-'*4}  {'-'*5}  {'-'*7}  {'-'*5}  {'-'*10}")
for r in confluences[:25]:
    pw = f"{r['per_week']:>4.1f}" if r["per_week"] is not None else "  n/a"
    print(f"  {r['confluence']:<22} {r['params']:<16} {r['n']:>4}  {r['wr']:>4.0%}  {r['ev']:>+6.2f}R  {pw}  {r['verdict']}")
for cf_name, cf_par, tp_rows in top3_tp:
    print(f"\n── TP/SL SWEEP: {cf_name} ({cf_par}) ──────────────────")
    print(f"  {'tp_r':>5}  {'sl':>5}  {'N':>4}  {'WR':>5}  {'EV':>7}  Verdict")
    for r in tp_rows:
        print(f"  {r['tp_r']:>5.1f}  {r['sl_mult']:>5.2f}  {r['n']:>4}  {r['wr']:>4.0%}  {r['ev']:>+6.2f}R  {r['verdict']}")

best = viable[0] if viable else (confluences[0] if confluences else None)
overall = verdict(best) if best else "NOT VIABLE"
print(f"\n── OVERALL ───────────────────────────────────────────")
if viable:
    print(f"  {len(viable)} viable confluences. Top 5:")
    for r in viable[:5]:
        pw = f"  {r['per_week']:.1f}/wk" if r["per_week"] else ""
        print(f"    {r['confluence']:<22} {r['params']:<16} N={r['n']:>3}  WR={r['wr']:.0%}  EV={r['ev']:>+.2f}R{pw}")
else:
    print(f"  No viable confluences.")
print(f"  Verdict: {overall}")
print("=" * w)

def _strip(rows): return [{k: v for k, v in r.items() if k != "_subset"} for r in rows]
save_research_results(symbol=SYMBOL, signal_tf="15m",
    params={"htf_tf":"4h","ltf_tf":"5m","sessions":["london","ny"],"data_source":"yfinance",
            "yf_symbol":YF_SYMBOL,
            "swing_lookback":15,"sl_atr_mult":0.5,"tp_r":2.0,
            "data_from":str(dates[0]),"data_to":str(dates[-1]),"n_trading_days":len(dates)},
    results={"serpe":None,"expansion":None,"bos_confluences":_strip(confluences[:10]),
             "best_bos":{k:v for k,v in best.items() if k!="_subset"} if best else None,
             "verdict":overall})
print(f"Saved → research_results/{SYMBOL}_15m.json")
