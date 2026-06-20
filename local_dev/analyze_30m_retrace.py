#!/usr/bin/env python3
"""
30m BOS retrace entry — same logic as the 1h strategy, one TF down.

Signal: 30m BOS → 5m very-deep retrace (MAE 0.75-1R) → 5m mini-BOS C1
Gate candidate: 1h alignment (same role as 4h plays for the 1h strategy)
Lookahead: 120 5m candles (~10h), TP = 2:1 from original 5m break
"""
import sys, bisect, math, statistics
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from db.local_db import LocalDB
from analysis.smc_analyzer import SMCAnalyzer
from datetime import datetime, timezone

DB_PATH = str(Path(__file__).resolve().parents[1] / "src" / "data" / "trade.db")
PAIRS   = ["NZDUSD", "EURUSD", "EURJPY", "USDCHF", "XAUUSD"]
LA_5M   = 120

def median(lst):
    if not lst: return float("nan")
    s = sorted(lst); n = len(s)
    return s[n//2] if n % 2 else (s[n//2-1]+s[n//2])/2

def z_test(wins, n, base):
    if n < 8 or base in (0,1): return 0.0, ""
    se = math.sqrt(base*(1-base)/n)
    z  = (wins/n - base) / se
    st = "★★★" if abs(z)>=2.576 else "★★" if abs(z)>=1.645 else "★" if abs(z)>=0.842 else ""
    return z, st

def ev(wins, n, rrs):
    if not rrs or n == 0: return float("nan")
    return (wins/n) * statistics.mean(rrs) - (1 - wins/n)

def analyze_pair(symbol, db, analyzer):
    m30_desc = db.query_recent(symbol, "30m",  limit=20000)
    m5_desc  = db.query_recent(symbol, "5m",   limit=130000)
    h1_desc  = db.query_recent(symbol, "1h",   limit=6000)
    if not m30_desc or not m5_desc: return []

    m30_chron = list(reversed(m30_desc))
    m5_chron  = list(reversed(m5_desc))
    h1_chron  = list(reversed(h1_desc)) if h1_desc else []
    m30_n     = len(m30_chron)

    m30_ts = [c["timestamp"] for c in m30_chron]
    m5_ts  = [c["timestamp"] for c in m5_chron]
    h1_ts  = [c["timestamp"] for c in h1_chron]
    m5_map = {c["timestamp"]: i for i, c in enumerate(m5_chron)}
    min_5m = m5_chron[0]["timestamp"]
    max_5m = m5_chron[-1]["timestamp"]

    seen = set(); records = []

    for k in range(20, m30_n - 5):
        window   = m30_desc[m30_n - 1 - k:]   # DESC slice for BOS detection
        m30_open = window[0]["timestamp"]
        m30_end  = m30_open + 1800             # 30 min
        if m30_open < min_5m or m30_end > max_5m: continue

        bos_events = analyzer.detect_bos(window, params={
            "symbol": symbol, "timeframe": "30m",
            "min_break_strength": 0.0, "require_liquidity_sweep": False})
        if not bos_events: continue
        if not analyzer.calculate_atr(window): continue

        for ev_ in bos_events:
            direction    = ev_["direction"]
            broken_level = ev_["broken_level"]
            bullish      = direction == "bullish"
            sig_key      = (direction, round(broken_level, 5), m30_open)
            if sig_key in seen: continue
            seen.add(sig_key)

            lo = bisect.bisect_left(m5_ts, m30_open)
            hi = bisect.bisect_left(m5_ts, m30_end)
            period = m5_chron[lo:hi]
            if len(period) < 2 or lo < 20: continue
            atr_5m = analyzer.calculate_atr(m5_chron[lo-20:lo])
            if not atr_5m: continue

            break_idx = None
            for i, c in enumerate(period):
                if bullish and c["close"] > broken_level: break_idx=i; break
                if not bullish and c["close"] < broken_level: break_idx=i; break
            if break_idx is None: continue

            break_c   = period[break_idx]; orig_entry = break_c["close"]
            orig_risk = abs(orig_entry - broken_level)
            if orig_risk < atr_5m * 0.05: continue

            orig_tp = (orig_entry + 2*orig_risk) if bullish else (orig_entry - 2*orig_risk)
            g_idx   = m5_map.get(break_c["timestamp"])
            if g_idx is None: continue

            outcome = "OPEN"; mae_r = 0.0; max_adv_idx = g_idx
            for j, fc in enumerate(m5_chron[g_idx+1: g_idx+1+LA_5M]):
                adverse = (max(0.0, orig_entry-fc["low"]) if bullish
                           else max(0.0, fc["high"]-orig_entry))
                if adverse/orig_risk > mae_r:
                    mae_r = adverse/orig_risk; max_adv_idx = g_idx+1+j
                if bullish:
                    if fc["high"] >= orig_tp: outcome="WIN"; break
                    if fc["low"]  <= broken_level: outcome="LOSS"; break
                else:
                    if fc["low"]  <= orig_tp: outcome="WIN"; break
                    if fc["high"] >= broken_level: outcome="LOSS"; break
            if outcome == "OPEN" or mae_r < 0.75: continue

            # C1
            c1_idx = max_adv_idx + 1
            if c1_idx >= len(m5_chron): continue
            c1 = m5_chron[c1_idx]; bottom_c = m5_chron[max_adv_idx]
            mini_bos = (c1["close"]>bottom_c["high"]) if bullish else (c1["close"]<bottom_c["low"])
            if not mini_bos: continue

            win = outcome == "WIN"
            new_entry = c1["close"]
            risk2 = abs(new_entry - broken_level)
            if risk2 < 1e-8: continue
            rr_val = abs(orig_tp - new_entry) / risk2

            # Combo gate
            post3 = m5_chron[max_adv_idx+1: max_adv_idx+4]
            n_aln = sum(1 for c in post3 if (c["close"]>c["open"]) == bullish)
            combo = (n_aln >= 2)

            # 1h alignment at C1 (same role as 4h gate in the 1h strategy)
            c1_ts  = c1["timestamp"]
            h1_aln_prev = h1_aln_curr = False
            if h1_chron:
                h1_pos = bisect.bisect_left(h1_ts, c1_ts)
                if h1_pos >= 2:
                    prev_h1 = h1_chron[h1_pos-2]; curr_h1 = h1_chron[h1_pos-1]
                    h1_aln_prev = (prev_h1["close"]>prev_h1["open"]) if bullish else (prev_h1["close"]<prev_h1["open"])
                    h1_aln_curr = (curr_h1["close"]>curr_h1["open"]) if bullish else (curr_h1["close"]<curr_h1["open"])

            records.append({
                "win": win, "rr": rr_val, "combo": combo,
                "h1_prev": h1_aln_prev, "h1_curr": h1_aln_curr,
                "h1_both": h1_aln_prev and h1_aln_curr,
                "mae_r": mae_r, "direction": direction,
            })
    return records


def grp(sigs, label, base_wr, base_n, show_ev=True):
    n = len(sigs)
    if n < 8:
        print(f"    {label:42s}  n={n:>3} (thin)")
        return
    wins = sum(1 for s in sigs if s["win"])
    wr   = wins/n
    rrs  = [s["rr"] for s in sigs]
    ev_v = ev(wins, n, rrs)
    rr_m = median(rrs)
    z, st = z_test(wins, n, base_wr)
    cov = n/base_n*100
    print(f"    {label:42s}  n={n:>3} ({cov:3.0f}%)  WR={wr*100:5.1f}%  "
          f"rr_med={rr_m:.2f}  EV={ev_v:+.2f}R  {st}")


def main():
    db = LocalDB(DB_PATH); analyzer = SMCAnalyzer()
    print("30m BOS retrace entry analysis")
    print("Signal: 30m BOS → 5m MAE≥0.75R retrace → 5m mini-BOS C1")
    print("Gate candidate: 1h alignment (prev/curr/both)\n")

    for symbol in PAIRS:
        m30_desc = db.query_recent(symbol, "30m", limit=1)
        m5_desc  = db.query_recent(symbol, "5m",  limit=1)
        if not m30_desc or not m5_desc:
            print(f"\n{symbol}: no 30m or 5m data"); continue

        # Data coverage
        m30_all = db.query_recent(symbol, "30m", limit=20000)
        m5_all  = db.query_recent(symbol, "5m",  limit=130000)
        m30_lo  = datetime.fromtimestamp(m30_all[-1]["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d")
        m30_hi  = datetime.fromtimestamp(m30_all[0]["timestamp"],  tz=timezone.utc).strftime("%Y-%m-%d")
        m5_lo   = datetime.fromtimestamp(m5_all[-1]["timestamp"],  tz=timezone.utc).strftime("%Y-%m-%d")
        days    = (m30_all[0]["timestamp"] - max(m30_all[-1]["timestamp"], m5_all[-1]["timestamp"])) / 86400

        print(f"\n{'='*72}")
        print(f"  {symbol}   30m: {len(m30_all):,} candles ({m30_lo}→{m30_hi})")
        print(f"           5m:  {len(m5_all):,} candles  effective window: ~{days:.0f} days")
        print(f"{'='*72}")

        recs = analyze_pair(symbol, db, analyzer)
        n    = len(recs)
        if n < 8:
            print(f"  mini-BOS C1 signals: n={n} (too thin)"); continue

        wins    = sum(1 for r in recs if r["win"])
        base_wr = wins/n
        rrs_all = [r["rr"] for r in recs]
        ev_all  = ev(wins, n, rrs_all)

        # Get very-deep retrace count from the BOS scan (re-scan quickly)
        print(f"\n  mini-BOS C1 signals: n={n}  WR={base_wr*100:.1f}%  "
              f"rr_med={median(rrs_all):.2f}  EV={ev_all:+.2f}R")

        print(f"\n  ── Retrace depth for context ──────────────────────────────────────")
        print(f"    (all above are already filtered to MAE 0.75–1R very-deep retrace)")

        print(f"\n  ── mini-BOS C1 gates ──────────────────────────────────────────────")
        grp(recs,                                  "no gate (C1 only)",        base_wr, n)
        grp([r for r in recs if r["combo"]],       "combo gate (≥2/3 aligned)", base_wr, n)

        print(f"\n  ── 1h alignment gate ──────────────────────────────────────────────")
        grp([r for r in recs if r["h1_curr"]],                   "curr 1h aligned",          base_wr, n)
        grp([r for r in recs if r["h1_both"]],                   "BOTH 1h aligned",          base_wr, n)
        grp([r for r in recs if not r["h1_curr"]],               "curr 1h COUNTER",          base_wr, n)
        grp([r for r in recs if r["combo"] and r["h1_curr"]],    "combo + curr 1h aligned",  base_wr, n)
        grp([r for r in recs if r["combo"] and r["h1_both"]],    "combo + BOTH 1h aligned",  base_wr, n)

        print(f"\n  ── Direction split ────────────────────────────────────────────────")
        grp([r for r in recs if r["direction"]=="bullish"],  "bullish BOS", base_wr, n)
        grp([r for r in recs if r["direction"]=="bearish"],  "bearish BOS", base_wr, n)

    db.close()

if __name__ == "__main__":
    main()
