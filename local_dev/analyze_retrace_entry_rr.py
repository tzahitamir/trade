#!/usr/bin/env python3
"""
Model the retrace-entry trade for 1h BOS signals.

When the mini-BOS C1 gate fires after the deep retrace (price came back
0.75–1R toward broken level then the first post-bottom candle immediately
closes past the bottom candle's extreme):

For each such signal compute:
  new_entry   = C1 close (the mini-BOS close)
  SL_level    = broken_level (original SL, tighter from new entry)
  SL_wick     = retrace bottom candle's wick (even tighter)
  TP          = original 1h TP (same absolute price, unchanged)

Then:
  risk_level  = abs(new_entry - SL_level)          ← SL option A
  risk_wick   = abs(new_entry - SL_wick)            ← SL option B
  tp_dist     = abs(TP - new_entry)
  rr_level    = tp_dist / risk_level
  rr_wick     = tp_dist / risk_wick
  EV_level    = WR * rr_level - (1-WR) * 1
  EV_wick     = WR * rr_wick  - (1-WR) * 1

Also checks: for SL_wick trades, how many WIN signals had a subsequent
candle go back through the wick level (false stop risk)?

Run from repo root: python local_dev/analyze_retrace_entry_rr.py
"""
import sys
import bisect
import math
from pathlib import Path
from collections import defaultdict
import statistics

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from db.local_db import LocalDB
from analysis.smc_analyzer import SMCAnalyzer

DB_PATH = str(Path(__file__).resolve().parents[1] / "src" / "data" / "trade.db")
PAIRS   = ["NZDUSD", "EURUSD", "EURJPY", "USDCHF", "XAUUSD"]
LA_5M   = 120

def pct(v): return f"{v*100:.1f}%"
def f2(v):  return f"{v:.2f}"
def f1(v):  return f"{v:.1f}"

def median(lst):
    if not lst: return float("nan")
    s = sorted(lst)
    n = len(s)
    return (s[n//2] if n % 2 else (s[n//2-1]+s[n//2])/2)

def analyze_pair(symbol, db, analyzer):
    h1_desc = db.query_recent(symbol, "1h",  limit=5600)
    m5_desc = db.query_recent(symbol, "5m",  limit=110000)
    if not h1_desc or not m5_desc:
        return []

    m5_chron = list(reversed(m5_desc))
    h1_n     = len(h1_desc)
    m5_ts    = [c["timestamp"] for c in m5_chron]
    m5_map   = {c["timestamp"]: i for i, c in enumerate(m5_chron)}
    min_5m_ts = m5_chron[0]["timestamp"]
    max_5m_ts = m5_chron[-1]["timestamp"]

    seen    = set()
    signals = []

    for k in range(30, h1_n - 5):
        window  = h1_desc[h1_n - 1 - k:]
        h1_open = window[0]["timestamp"]
        h1_end  = h1_open + 3600

        if h1_open < min_5m_ts or h1_end > max_5m_ts:
            continue

        bos_events = analyzer.detect_bos(
            window,
            params={"symbol": symbol, "timeframe": "1h",
                    "min_break_strength": 0.0,
                    "require_liquidity_sweep": False},
        )
        if not bos_events:
            continue

        atr_1h = analyzer.calculate_atr(window)
        if not atr_1h:
            continue

        for ev in bos_events:
            direction    = ev["direction"]
            broken_level = ev["broken_level"]
            bullish      = direction == "bullish"

            sig_key = (direction, round(broken_level, 5), h1_open)
            if sig_key in seen:
                continue
            seen.add(sig_key)

            lo = bisect.bisect_left(m5_ts, h1_open)
            hi = bisect.bisect_left(m5_ts, h1_end)
            period = m5_chron[lo:hi]
            if len(period) < 2 or lo < 20:
                continue

            atr_5m = analyzer.calculate_atr(m5_chron[lo - 20:lo])
            if not atr_5m:
                continue

            # First 5m close past broken level (original break candle)
            break_idx = None
            for i, c in enumerate(period):
                if bullish and c["close"] > broken_level: break_idx = i; break
                if not bullish and c["close"] < broken_level: break_idx = i; break
            if break_idx is None:
                continue

            break_c = period[break_idx]
            orig_entry = break_c["close"]
            orig_risk  = abs(orig_entry - broken_level)
            if orig_risk < atr_5m * 0.05:
                continue

            orig_tp = (orig_entry + 2 * orig_risk) if bullish else (orig_entry - 2 * orig_risk)
            sl      = broken_level
            g_idx   = m5_map.get(break_c["timestamp"])
            if g_idx is None:
                continue

            # Walk forward to find outcome, MAE, and retrace bottom
            outcome      = "OPEN"
            mae_r        = 0.0
            max_adv_idx  = g_idx
            future       = m5_chron[g_idx + 1: g_idx + 1 + LA_5M]

            for j, fc in enumerate(future):
                if bullish:
                    adverse = max(0.0, orig_entry - fc["low"])
                else:
                    adverse = max(0.0, fc["high"] - orig_entry)
                cur_mae = adverse / orig_risk
                if cur_mae > mae_r:
                    mae_r       = cur_mae
                    max_adv_idx = g_idx + 1 + j

                if bullish:
                    if fc["high"] >= orig_tp:  outcome = "WIN";  break
                    if fc["low"]  <= sl:        outcome = "LOSS"; break
                else:
                    if fc["low"]  <= orig_tp:  outcome = "WIN";  break
                    if fc["high"] >= sl:        outcome = "LOSS"; break

            if outcome == "OPEN" or mae_r < 0.75:
                continue

            win = outcome == "WIN"

            # ── Retrace bottom ──────────────────────────────────────────
            bottom_c   = m5_chron[max_adv_idx]
            # The extreme wick of the bottom candle
            bottom_wick = bottom_c["low"] if bullish else bottom_c["high"]

            # ── C1 after bottom: the mini-BOS candle ───────────────────
            if max_adv_idx + 1 >= len(m5_chron):
                continue
            c1 = m5_chron[max_adv_idx + 1]

            # Mini-BOS: C1 close past bottom_c's extreme in BOS direction
            mini_bos_c1 = (c1["close"] > bottom_c["high"]) if bullish else (c1["close"] < bottom_c["low"])
            if not mini_bos_c1:
                continue   # only analyse mini-BOS C1 events

            new_entry = c1["close"]

            # ── Risk and R:R ────────────────────────────────────────────
            risk_level = abs(new_entry - broken_level)         # SL at broken level
            risk_wick  = abs(new_entry - bottom_wick)          # SL at bottom wick
            tp_dist    = abs(orig_tp - new_entry)

            if risk_level < 1e-8 or risk_wick < 1e-8:
                continue

            rr_level = tp_dist / risk_level
            rr_wick  = tp_dist / risk_wick

            # ── Alignment check: C2 and C3 after bottom ─────────────────
            post3 = m5_chron[max_adv_idx + 1: max_adv_idx + 4]   # C1, C2, C3
            n_aligned_3 = sum(
                1 for c in post3
                if (c["close"] > c["open"]) == bullish
            )
            combo_gate = (n_aligned_3 >= 2)   # mini-BOS C1 + ≥2/3 aligned

            # ── SL_wick false-stop check (for WIN trades) ───────────────
            # Did any future candle after C1 go back through bottom_wick
            # before hitting orig_tp? (would be a false stop if SL=wick)
            wick_false_stop = False
            if win:
                c1_idx = max_adv_idx + 1
                for fc in m5_chron[c1_idx + 1: c1_idx + 1 + LA_5M]:
                    if bullish:
                        if fc["high"] >= orig_tp:  break  # TP hit first
                        if fc["low"]  <= bottom_wick:
                            wick_false_stop = True; break
                    else:
                        if fc["low"]  <= orig_tp:  break
                        if fc["high"] >= bottom_wick:
                            wick_false_stop = True; break

            # ── Express risks in 5m ATR units ───────────────────────────
            risk_level_atr = risk_level / atr_5m
            risk_wick_atr  = risk_wick  / atr_5m
            tp_dist_atr    = tp_dist    / atr_5m

            signals.append({
                "win":              win,
                "combo_gate":       combo_gate,
                "rr_level":         rr_level,
                "rr_wick":          rr_wick,
                "risk_level_atr":   risk_level_atr,
                "risk_wick_atr":    risk_wick_atr,
                "tp_dist_atr":      tp_dist_atr,
                "wick_false_stop":  wick_false_stop,
                "atr_5m":           atr_5m,
                "orig_risk_atr":    orig_risk / atr_5m,
            })

    return signals


def summary_stats(label, sigs, base_wr):
    n    = len(sigs)
    wins = sum(1 for s in sigs if s["win"])
    if n == 0:
        print(f"\n  {label}: n=0"); return

    wr = wins / n

    rr_l_all  = [s["rr_level"] for s in sigs]
    rr_w_all  = [s["rr_wick"]  for s in sigs]
    rr_l_wins = [s["rr_level"] for s in sigs if s["win"]]
    rr_w_wins = [s["rr_wick"]  for s in sigs if s["win"]]

    ev_level = wr * statistics.mean(rr_l_all) - (1 - wr) * 1
    ev_wick  = wr * statistics.mean(rr_w_all) - (1 - wr) * 1

    risk_l_med = median([s["risk_level_atr"] for s in sigs])
    risk_w_med = median([s["risk_wick_atr"]  for s in sigs])
    tp_med     = median([s["tp_dist_atr"]    for s in sigs])
    orig_risk  = median([s["orig_risk_atr"]  for s in sigs])

    false_stops = sum(1 for s in sigs if s["win"] and s["wick_false_stop"])
    pct_fs = false_stops / wins * 100 if wins else 0

    # z-test vs baseline
    se = math.sqrt(base_wr * (1 - base_wr) / n)
    z  = (wr - base_wr) / se if se else 0
    st = ("★★★" if abs(z) >= 2.576 else "★★" if abs(z) >= 1.645 else "★" if abs(z) >= 0.842 else "")

    print(f"\n  {label}  (n={n})")
    print(f"    WR           = {pct(wr)}   (baseline {pct(base_wr)}  z={z:+.2f}{st})")
    print(f"    Orig risk    = {f2(orig_risk)}×ATR5m  (original break entry)")
    print()
    print(f"    ── SL at broken level ──────────────────────────────────")
    print(f"    Risk         = {f2(risk_l_med)}×ATR5m  (median)")
    print(f"    TP dist      = {f2(tp_med)}×ATR5m  (median)")
    print(f"    R:R          = {f2(statistics.median(rr_l_all))}:1  (median)   avg={statistics.mean(rr_l_all):.2f}")
    if rr_l_wins:
        print(f"    R:R on wins  = {f2(statistics.median(rr_l_wins))}:1  (median)")
    print(f"    EV           = {f2(ev_level)}R per trade")
    print()
    print(f"    ── SL at bottom wick ───────────────────────────────────")
    print(f"    Risk         = {f2(risk_w_med)}×ATR5m  (median)")
    print(f"    R:R          = {f2(statistics.median(rr_w_all))}:1  (median)   avg={statistics.mean(rr_w_all):.2f}")
    if rr_w_wins:
        print(f"    R:R on wins  = {f2(statistics.median(rr_w_wins))}:1  (median)")
    print(f"    EV           = {f2(ev_wick)}R per trade")
    print(f"    False stops  = {false_stops}/{wins} WIN trades re-pierced wick ({pct_fs:.0f}%)")

    # Distribution of R:R buckets
    print(f"\n    R:R distribution (SL=level):")
    buckets = {"<2R": 0, "2–4R": 0, "4–8R": 0, "8–15R": 0, "≥15R": 0}
    for r in rr_l_all:
        if r < 2:   buckets["<2R"]   += 1
        elif r < 4: buckets["2–4R"]  += 1
        elif r < 8: buckets["4–8R"]  += 1
        elif r < 15:buckets["8–15R"] += 1
        else:       buckets["≥15R"]  += 1
    for k, v in buckets.items():
        bar = "█" * (v * 30 // max(buckets.values(), default=1))
        print(f"      {k:8s}  {v:3d}  ({v/n*100:.0f}%)  {bar}")


def main():
    db       = LocalDB(DB_PATH)
    analyzer = SMCAnalyzer()

    print("Mini-BOS C1 retrace-entry: Risk / R:R / EV model")
    print("Base = all very-deep retrace (MAE 0.75–1R) signals")

    for symbol in PAIRS:
        print(f"\n{'='*64}")
        print(f"  {symbol}")
        print(f"{'='*64}")

        sigs = analyze_pair(symbol, db, analyzer)
        if not sigs:
            print("  No mini-BOS C1 signals"); continue

        n_total = len(sigs)
        wins    = sum(1 for s in sigs if s["win"])
        base_wr = wins / n_total   # WR within mini-BOS C1 group

        # All mini-BOS C1 signals
        summary_stats("All mini-BOS C1 signals", sigs, base_wr)

        # Combo gate: mini-BOS C1 + ≥2/3 aligned
        combo = [s for s in sigs if s["combo_gate"]]
        non_combo = [s for s in sigs if not s["combo_gate"]]
        if combo:
            combo_bwr = sum(1 for s in combo if s["win"]) / len(combo)
            summary_stats("Combo gate (mini-BOS C1 + ≥2/3 aligned)", combo, combo_bwr)
        if non_combo:
            nc_bwr = sum(1 for s in non_combo if s["win"]) / len(non_combo)
            summary_stats("No combo (mini-BOS C1 but <2/3 aligned)", non_combo, nc_bwr)

        # Compare original entry risk vs retrace entry risk
        print(f"\n  Risk compression (retrace entry vs original break entry):")
        orig_med  = median([s["orig_risk_atr"] for s in sigs])
        new_med_l = median([s["risk_level_atr"] for s in sigs])
        new_med_w = median([s["risk_wick_atr"]  for s in sigs])
        print(f"    Original break entry risk : {orig_med:.2f}×ATR5m")
        print(f"    Retrace entry  (SL=level) : {new_med_l:.2f}×ATR5m  "
              f"({new_med_l/orig_med*100:.0f}% of original)")
        print(f"    Retrace entry  (SL=wick)  : {new_med_w:.2f}×ATR5m  "
              f"({new_med_w/orig_med*100:.0f}% of original)")

    db.close()


if __name__ == "__main__":
    main()
