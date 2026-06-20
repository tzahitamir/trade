#!/usr/bin/env python3
"""
4h BOS retrace entry — find optimal daily alignment gate per pair.
Same approach used to derive per-pair 4h gates for the 1h strategy.

Signal: 4h BOS → 5m very-deep retrace (0.75-1R) → 5m mini-BOS C1
Gate candidates: prev daily / curr daily / both daily aligned
Also checks combo gate (≥2/3 of C1-C3 aligned) × daily gate.
"""
import sys, bisect, math, statistics
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from db.local_db import LocalDB
from analysis.smc_analyzer import SMCAnalyzer
from datetime import datetime, timezone

DB_PATH = str(Path(__file__).resolve().parents[1] / "src" / "data" / "trade.db")
PAIRS   = ["NZDUSD", "EURUSD", "EURJPY", "USDCHF", "XAUUSD"]
LA_5M   = 400   # ~33h lookahead (same as original 4h analysis)

def median(lst):
    if not lst: return float("nan")
    s = sorted(lst); n = len(s)
    return s[n//2] if n % 2 else (s[n//2-1]+s[n//2])/2

def z_test(wins, n, base):
    if n < 5 or base in (0, 1): return 0.0, ""
    se = math.sqrt(base*(1-base)/n)
    if se == 0: return 0.0, ""
    z = (wins/n - base) / se
    st = "★★★" if abs(z)>=2.576 else "★★" if abs(z)>=1.645 else "★" if abs(z)>=0.842 else ""
    return z, st

def ev(wins, n, rrs):
    if not rrs: return float("nan")
    wr = wins/n
    return wr * statistics.mean(rrs) - (1-wr)

def analyze_pair(symbol, db, analyzer):
    h4_desc  = db.query_recent(symbol, "4h", limit=2600)
    m5_desc  = db.query_recent(symbol, "5m", limit=130000)
    d1_desc  = db.query_recent(symbol, "1d", limit=400)
    if not h4_desc or not m5_desc or not d1_desc: return []

    h4_chron = list(reversed(h4_desc))
    m5_chron = list(reversed(m5_desc))
    d1_chron = list(reversed(d1_desc))
    h4_n     = len(h4_chron)

    h4_ts = [c["timestamp"] for c in h4_chron]
    m5_ts = [c["timestamp"] for c in m5_chron]
    d1_ts = [c["timestamp"] for c in d1_chron]
    m5_map = {c["timestamp"]: i for i, c in enumerate(m5_chron)}
    min_5m  = m5_chron[0]["timestamp"]
    max_5m  = m5_chron[-1]["timestamp"]

    seen = set(); records = []

    for k in range(30, h4_n - 5):
        window  = h4_desc[h4_n - 1 - k:]  # DESC slice → BOS detection
        h4_open = window[0]["timestamp"]
        h4_end  = h4_open + 4 * 3600
        if h4_open < min_5m or h4_end > max_5m: continue

        bos_events = analyzer.detect_bos(window, params={
            "symbol": symbol, "timeframe": "4h",
            "min_break_strength": 0.0, "require_liquidity_sweep": False})
        if not bos_events: continue
        if not analyzer.calculate_atr(window): continue

        for ev_ in bos_events:
            direction    = ev_["direction"]
            broken_level = ev_["broken_level"]
            bullish      = direction == "bullish"
            sig_key      = (direction, round(broken_level, 2), h4_open)
            if sig_key in seen: continue
            seen.add(sig_key)

            lo = bisect.bisect_left(m5_ts, h4_open)
            hi = bisect.bisect_left(m5_ts, h4_end)
            period = m5_chron[lo:hi]
            if len(period) < 4 or lo < 20: continue
            atr_5m = analyzer.calculate_atr(m5_chron[lo-20:lo])
            if not atr_5m: continue

            break_idx = None
            for i, c in enumerate(period):
                if bullish and c["close"] > broken_level: break_idx=i; break
                if not bullish and c["close"] < broken_level: break_idx=i; break
            if break_idx is None: continue

            break_c   = period[break_idx]
            orig_entry = break_c["close"]
            orig_risk  = abs(orig_entry - broken_level)
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

            # Combo gate (≥2/3 of C1-C3 aligned)
            post3 = m5_chron[max_adv_idx+1: max_adv_idx+4]
            n_aln = sum(1 for c in post3 if (c["close"]>c["open"]) == bullish)
            combo = (n_aln >= 2)

            # Daily alignment at C1
            c1_ts  = c1["timestamp"]
            d1_pos = bisect.bisect_left(d1_ts, c1_ts)
            if d1_pos < 2: continue
            prev_d = d1_chron[d1_pos-2]
            curr_d = d1_chron[d1_pos-1]
            prev_d_aln = (prev_d["close"]>prev_d["open"]) if bullish else (prev_d["close"]<prev_d["open"])
            curr_d_aln = (curr_d["close"]>curr_d["open"]) if bullish else (curr_d["close"]<curr_d["open"])
            both_d_aln = prev_d_aln and curr_d_aln

            records.append({
                "win": win, "rr": rr_val, "combo": combo,
                "prev_d": prev_d_aln, "curr_d": curr_d_aln, "both_d": both_d_aln,
                "mae_r": mae_r,
            })
    return records


def report_gate(label, sigs, base_wr, base_n):
    n = len(sigs)
    if n < 5:
        print(f"    {label:40s}  n={n:>3} (thin)")
        return
    wins = sum(1 for s in sigs if s["win"])
    wr   = wins/n
    rrs  = [s["rr"] for s in sigs]
    ev_v = ev(wins, n, rrs)
    rr_m = median(rrs)
    z, st = z_test(wins, n, base_wr)
    cov = n/base_n*100
    print(f"    {label:40s}  n={n:>3} ({cov:3.0f}%)  WR={wr*100:5.1f}%  "
          f"med_rr={rr_m:.2f}  EV={ev_v:+.2f}R  {st}")


def main():
    db = LocalDB(DB_PATH); analyzer = SMCAnalyzer()
    print("4h BOS retrace — daily alignment gate analysis\n")
    print("Signal: 4h BOS → 5m very-deep retrace (MAE 0.75-1R) → 5m mini-BOS C1\n")

    for symbol in PAIRS:
        recs = analyze_pair(symbol, db, analyzer)
        n    = len(recs)
        if n < 5:
            print(f"\n{symbol}: n={n} (too thin)"); continue

        wins   = sum(1 for r in recs if r["win"])
        base   = wins/n
        rrs_all= [r["rr"] for r in recs]
        ev_all = ev(wins, n, rrs_all)

        print(f"\n{'='*72}")
        print(f"  {symbol}   C1 signals: n={n}  WR={base*100:.1f}%  "
              f"med_rr={median(rrs_all):.2f}  EV={ev_all:+.2f}R  (base)")
        print(f"{'='*72}")

        # ── Daily gate options ────────────────────────────────────────────
        print(f"\n  ── Daily alignment gates ──────────────────────────────────────────")
        for lbl, filt in [
            ("no gate (all C1)",         lambda r: True),
            ("prev daily aligned",       lambda r: r["prev_d"]),
            ("curr daily aligned",       lambda r: r["curr_d"]),
            ("BOTH daily aligned",       lambda r: r["both_d"]),
            ("curr daily COUNTER",       lambda r: not r["curr_d"]),
            ("prev daily COUNTER",       lambda r: not r["prev_d"]),
        ]:
            report_gate(lbl, [r for r in recs if filt(r)], base, n)

        # ── Combo gate alone and with daily ──────────────────────────────
        print(f"\n  ── Combo gate (≥2/3 C1-C3 aligned) ───────────────────────────────")
        combo_all  = [r for r in recs if r["combo"]]
        report_gate("combo gate only", combo_all, base, n)
        for lbl, filt in [
            ("combo + prev daily aligned", lambda r: r["combo"] and r["prev_d"]),
            ("combo + curr daily aligned", lambda r: r["combo"] and r["curr_d"]),
            ("combo + BOTH daily aligned", lambda r: r["combo"] and r["both_d"]),
        ]:
            report_gate(lbl, [r for r in recs if filt(r)], base, n)

        # ── Counter veto impact ───────────────────────────────────────────
        print(f"\n  ── Counter veto: exclude curr daily counter ───────────────────────")
        no_counter = [r for r in recs if r["curr_d"]]
        counter    = [r for r in recs if not r["curr_d"]]
        report_gate("curr aligned (counter excluded)", no_counter, base, n)
        report_gate("curr COUNTER (would be excluded)", counter, base, n)

    db.close()

if __name__ == "__main__":
    main()
