#!/usr/bin/env python3
"""
Compare SL placements for 1h retrace mini-BOS C1 entry (gate1+4h):
  CURRENT : SL = broken 1h level
  TIGHT   : SL = bottom candle extreme (retrace bottom low/high)
"""
import sys, bisect, math, statistics
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from db.local_db import LocalDB
from analysis.smc_analyzer import SMCAnalyzer

DB_PATH = str(Path(__file__).resolve().parents[1] / "src" / "data" / "trade.db")
PAIRS   = ["NZDUSD", "EURUSD", "USDCHF", "XAUUSD"]
LA_5M   = 120

def median(lst):
    if not lst: return float("nan")
    s = sorted(lst); n = len(s)
    return s[n//2] if n % 2 else (s[n//2-1]+s[n//2])/2

def analyze_pair(symbol, db, analyzer):
    h1_desc = db.query_recent(symbol, "1h",  limit=12000)
    m5_desc = db.query_recent(symbol, "5m",  limit=130000)
    h4_desc = db.query_recent(symbol, "4h",  limit=2000)
    if not h1_desc or not m5_desc or not h4_desc: return []

    m5_chron  = list(reversed(m5_desc))
    h4_chron  = list(reversed(h4_desc))
    h1_n      = len(h1_desc)
    m5_ts     = [c["timestamp"] for c in m5_chron]
    h4_ts     = [c["timestamp"] for c in h4_chron]
    m5_map    = {c["timestamp"]: i for i, c in enumerate(m5_chron)}
    min_5m_ts = m5_chron[0]["timestamp"]
    max_5m_ts = m5_chron[-1]["timestamp"]

    seen = set(); results = []

    for k in range(30, h1_n - 5):
        window  = h1_desc[h1_n - 1 - k:]
        h1_open = window[0]["timestamp"]
        h1_end  = h1_open + 3600
        if h1_open < min_5m_ts or h1_end > max_5m_ts: continue

        bos_events = analyzer.detect_bos(window, params={"symbol":symbol,"timeframe":"1h","min_break_strength":0.0,"require_liquidity_sweep":False})
        if not bos_events: continue
        if not analyzer.calculate_atr(window): continue

        for ev in bos_events:
            direction = ev["direction"]; broken_level = ev["broken_level"]; bullish = direction=="bullish"
            sig_key = (direction, round(broken_level,5), h1_open)
            if sig_key in seen: continue
            seen.add(sig_key)

            lo = bisect.bisect_left(m5_ts, h1_open); hi = bisect.bisect_left(m5_ts, h1_end)
            period = m5_chron[lo:hi]
            if len(period)<2 or lo<20: continue
            atr_5m = analyzer.calculate_atr(m5_chron[lo-20:lo])
            if not atr_5m: continue

            break_idx = None
            for i,c in enumerate(period):
                if bullish and c["close"]>broken_level: break_idx=i; break
                if not bullish and c["close"]<broken_level: break_idx=i; break
            if break_idx is None: continue

            break_c   = period[break_idx]; orig_entry = break_c["close"]
            orig_risk = abs(orig_entry - broken_level)
            if orig_risk < atr_5m*0.05: continue

            orig_tp = (orig_entry + 2*orig_risk) if bullish else (orig_entry - 2*orig_risk)
            g_idx   = m5_map.get(break_c["timestamp"])
            if g_idx is None: continue

            # Walk forward: track MAE and outcome of the original trade
            outcome = "OPEN"; mae_r = 0.0; max_adv_idx = g_idx
            for j, fc in enumerate(m5_chron[g_idx+1: g_idx+1+LA_5M]):
                adverse = (max(0.0, orig_entry-fc["low"]) if bullish else max(0.0, fc["high"]-orig_entry))
                if adverse/orig_risk > mae_r: mae_r = adverse/orig_risk; max_adv_idx = g_idx+1+j
                if bullish:
                    if fc["high"]>=orig_tp: outcome="WIN"; break
                    if fc["low"]<=broken_level: outcome="LOSS"; break
                else:
                    if fc["low"]<=orig_tp: outcome="WIN"; break
                    if fc["high"]>=broken_level: outcome="LOSS"; break

            # Must have resolved AND retraced deeply
            if outcome == "OPEN" or mae_r < 0.75: continue

            # C1 check
            if max_adv_idx+1 >= len(m5_chron): continue
            c1      = m5_chron[max_adv_idx+1]
            bottom_c= m5_chron[max_adv_idx]
            mini_bos= (c1["close"]>bottom_c["high"]) if bullish else (c1["close"]<bottom_c["low"])
            if not mini_bos: continue

            # 4h gate (same as live)
            c1_ts  = c1["timestamp"]
            h4_pos = bisect.bisect_left(h4_ts, c1_ts)
            if h4_pos < 2: continue
            prev_h4 = h4_chron[h4_pos-2]; curr_h4 = h4_chron[h4_pos-1]
            prev_aln = (prev_h4["close"]>prev_h4["open"]) if bullish else (prev_h4["close"]<prev_h4["open"])
            curr_aln = (curr_h4["close"]>curr_h4["open"]) if bullish else (curr_h4["close"]<curr_h4["open"])
            if symbol == "EURUSD":
                if not (prev_aln and curr_aln): continue
            elif symbol in ("NZDUSD","USDCHF"):
                if not curr_aln: continue
            # XAUUSD: no gate

            new_entry = c1["close"]
            c1_idx    = max_adv_idx+1

            # SL placements
            orig_sl   = broken_level
            tight_sl  = bottom_c["low"] if bullish else bottom_c["high"]

            orig_risk2  = abs(new_entry - orig_sl)
            tight_risk2 = abs(new_entry - tight_sl)
            if orig_risk2 < 1e-8 or tight_risk2 < 1e-8: continue

            tp_dist = abs(orig_tp - new_entry)
            orig_rr  = tp_dist / orig_risk2
            tight_rr = tp_dist / tight_risk2

            # Outcomes from C1 forward (fresh lookahead)
            orig_out = "OPEN"; tight_out = "OPEN"
            for fc in m5_chron[c1_idx+1: c1_idx+1+LA_5M]:
                if orig_out == "OPEN":
                    if bullish:
                        if fc["high"]>=orig_tp:    orig_out="WIN"
                        elif fc["low"]<=orig_sl:   orig_out="LOSS"
                    else:
                        if fc["low"]<=orig_tp:     orig_out="WIN"
                        elif fc["high"]>=orig_sl:  orig_out="LOSS"
                if tight_out == "OPEN":
                    if bullish:
                        if fc["high"]>=orig_tp:    tight_out="WIN"
                        elif fc["low"]<=tight_sl:  tight_out="LOSS"
                    else:
                        if fc["low"]<=orig_tp:     tight_out="WIN"
                        elif fc["high"]>=tight_sl: tight_out="LOSS"
                if orig_out!="OPEN" and tight_out!="OPEN": break

            if orig_out=="OPEN" or tight_out=="OPEN": continue  # not enough lookahead

            results.append({
                "orig_win":      orig_out=="WIN",
                "tight_win":     tight_out=="WIN",
                "orig_rr":       orig_rr,
                "tight_rr":      tight_rr,
                "orig_risk_atr": orig_risk2/atr_5m,
                "tight_risk_atr":tight_risk2/atr_5m,
                "mae_r":         mae_r,
                "bottom_wick":   tight_risk2/atr_5m,  # risk in ATR units
            })
    return results


def report(symbol, results):
    n = len(results)
    if n < 5:
        print(f"\n  {symbol}: n={n} (too thin)"); return

    orig_wins  = sum(1 for r in results if r["orig_win"])
    tight_wins = sum(1 for r in results if r["tight_win"])
    orig_wr    = orig_wins/n;  tight_wr = tight_wins/n

    orig_avg_rr   = statistics.mean([r["orig_rr"]  for r in results])
    tight_avg_rr  = statistics.mean([r["tight_rr"] for r in results])
    orig_med_rr   = median([r["orig_rr"]  for r in results])
    tight_med_rr  = median([r["tight_rr"] for r in results])

    orig_ev_avg   = orig_wr  * orig_avg_rr  - (1-orig_wr)
    tight_ev_avg  = tight_wr * tight_avg_rr - (1-tight_wr)
    orig_ev_med   = orig_wr  * orig_med_rr  - (1-orig_wr)
    tight_ev_med  = tight_wr * tight_med_rr - (1-tight_wr)

    win_to_loss   = sum(1 for r in results if r["orig_win"] and not r["tight_win"])
    avg_tight_risk= statistics.mean([r["tight_risk_atr"] for r in results])
    avg_orig_risk = statistics.mean([r["orig_risk_atr"]  for r in results])

    print(f"\n{'='*62}")
    print(f"  {symbol}  (n={n} after gate1+4h)")
    print(f"{'='*62}")
    print(f"                        ORIG SL (1h level)   TIGHT SL (bottom candle)")
    print(f"  Win rate            :     {orig_wr*100:5.1f}%              {tight_wr*100:5.1f}%")
    print(f"  Avg R:R             :     {orig_avg_rr:5.2f}:1             {tight_avg_rr:5.2f}:1")
    print(f"  Median R:R          :     {orig_med_rr:5.2f}:1             {tight_med_rr:5.2f}:1")
    print(f"  EV (avg R:R)        :    {orig_ev_avg:+.2f}R              {tight_ev_avg:+.2f}R")
    print(f"  EV (median R:R)     :    {orig_ev_med:+.2f}R              {tight_ev_med:+.2f}R")
    print(f"  Avg risk (ATR×)     :     {avg_orig_risk:5.2f}×ATR          {avg_tight_risk:5.2f}×ATR")
    print(f"  Wins→Loss (tight too close): {win_to_loss} / {orig_wins}  ({win_to_loss/max(orig_wins,1)*100:.0f}% of wins)")


def main():
    db = LocalDB(DB_PATH); analyzer = SMCAnalyzer()
    print("Tight SL: bottom candle low/high vs broken 1h level\n")
    for sym in PAIRS:
        r = analyze_pair(sym, db, analyzer)
        report(sym, r)
    db.close()

if __name__ == "__main__":
    main()
