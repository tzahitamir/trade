#!/usr/bin/env python3
"""
Check whether 4h alignment at the time of the mini-BOS C1 retrace entry
improves win rate.

For each mini-BOS C1 signal (1h BOS → very-deep retrace 0.75-1R → first
post-bottom candle closes past bottom extreme):

  "4h aligned" = the last COMPLETED 4h candle before the mini-BOS C1
                 close is bullish (for bullish BOS) or bearish (for bearish BOS)

If 4h is aligned:
  cascade = 4h → 1h BOS → 15m BOS → 5m retrace → 5m mini-BOS C1
  (4 timeframes all pointing the same direction at entry)

Hypothesis: this cascade produces a materially higher WR than the base
mini-BOS C1 group.

Run from repo root: python local_dev/analyze_retrace_4h_alignment.py
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

def z_test(wins, n, base):
    if n < 8 or base in (0, 1): return 0.0, ""
    se = math.sqrt(base * (1 - base) / n)
    if se == 0: return 0.0, ""
    z = (wins / n - base) / se
    st = ("★★★" if abs(z) >= 2.576 else "★★" if abs(z) >= 1.645 else "★" if abs(z) >= 0.842 else "")
    return z, st

def wr_s(wins, n, base=None):
    if n < 8: return f"n={n} (thin)"
    pct = wins / n * 100
    s = f"{pct:.1f}%  n={n}"
    if base is not None:
        z, st = z_test(wins, n, base)
        diff = pct - base * 100
        sign = "+" if diff >= 0 else ""
        s += f"  ({sign}{diff:.1f}pp  z={z:+.2f}{st})"
    return s

def median(lst):
    if not lst: return float("nan")
    s = sorted(lst)
    n = len(s)
    return s[n//2] if n % 2 else (s[n//2-1]+s[n//2])/2

def analyze_pair(symbol, db, analyzer):
    h1_desc = db.query_recent(symbol, "1h",  limit=5600)
    m5_desc = db.query_recent(symbol, "5m",  limit=110000)
    h4_desc = db.query_recent(symbol, "4h",  limit=2000)

    if not h1_desc or not m5_desc or not h4_desc:
        return []

    m5_chron = list(reversed(m5_desc))
    h4_chron = list(reversed(h4_desc))
    h1_n     = len(h1_desc)

    m5_ts  = [c["timestamp"] for c in m5_chron]
    h4_ts  = [c["timestamp"] for c in h4_chron]
    m5_map = {c["timestamp"]: i for i, c in enumerate(m5_chron)}

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

            break_idx = None
            for i, c in enumerate(period):
                if bullish and c["close"] > broken_level: break_idx = i; break
                if not bullish and c["close"] < broken_level: break_idx = i; break
            if break_idx is None:
                continue

            break_c    = period[break_idx]
            orig_entry = break_c["close"]
            orig_risk  = abs(orig_entry - broken_level)
            if orig_risk < atr_5m * 0.05:
                continue

            orig_tp = (orig_entry + 2 * orig_risk) if bullish else (orig_entry - 2 * orig_risk)
            sl      = broken_level
            g_idx   = m5_map.get(break_c["timestamp"])
            if g_idx is None:
                continue

            outcome     = "OPEN"
            mae_r       = 0.0
            max_adv_idx = g_idx
            future      = m5_chron[g_idx + 1: g_idx + 1 + LA_5M]

            for j, fc in enumerate(future):
                adverse = (max(0.0, orig_entry - fc["low"]) if bullish
                           else max(0.0, fc["high"] - orig_entry))
                cur_mae = adverse / orig_risk
                if cur_mae > mae_r:
                    mae_r       = cur_mae
                    max_adv_idx = g_idx + 1 + j

                if bullish:
                    if fc["high"] >= orig_tp: outcome = "WIN";  break
                    if fc["low"]  <= sl:      outcome = "LOSS"; break
                else:
                    if fc["low"]  <= orig_tp: outcome = "WIN";  break
                    if fc["high"] >= sl:      outcome = "LOSS"; break

            if outcome == "OPEN" or mae_r < 0.75:
                continue

            win = outcome == "WIN"

            bottom_c = m5_chron[max_adv_idx]

            if max_adv_idx + 1 >= len(m5_chron):
                continue
            c1 = m5_chron[max_adv_idx + 1]

            mini_bos_c1 = ((c1["close"] > bottom_c["high"]) if bullish
                           else (c1["close"] < bottom_c["low"]))
            if not mini_bos_c1:
                continue

            new_entry  = c1["close"]
            c1_ts      = c1["timestamp"]

            # ── 4h alignment ─────────────────────────────────────────────────
            # Find the last COMPLETED 4h candle strictly before the mini-BOS C1
            # A 4h candle "completed" = its close timestamp < c1_ts
            # h4_ts is ascending; bisect gives insert point for c1_ts
            h4_pos = bisect.bisect_left(h4_ts, c1_ts)
            # h4_pos-1 is the last 4h candle that OPENED before c1_ts (may be in-progress)
            # h4_pos-2 is the last COMPLETED 4h candle (fully closed before c1_ts)
            if h4_pos < 2:
                continue   # not enough 4h history

            # Last completed 4h candle (the one before the current in-progress one)
            prev_h4 = h4_chron[h4_pos - 2]
            curr_h4 = h4_chron[h4_pos - 1]   # may still be forming

            # "4h aligned" = previous completed 4h candle is bullish (for bullish BOS)
            prev_h4_aligned = ((prev_h4["close"] > prev_h4["open"]) if bullish
                               else (prev_h4["close"] < prev_h4["open"]))

            # Also check the current (possibly in-progress) 4h candle
            curr_h4_aligned = ((curr_h4["close"] > curr_h4["open"]) if bullish
                               else (curr_h4["close"] < curr_h4["open"]))

            # Both aligned = strongest confluence
            both_h4_aligned = prev_h4_aligned and curr_h4_aligned

            # R:R metrics
            risk_level = abs(new_entry - broken_level)
            tp_dist    = abs(orig_tp - new_entry)
            if risk_level < 1e-8:
                continue
            rr_level = tp_dist / risk_level

            # Post-bottom alignment for combo gate
            post3 = m5_chron[max_adv_idx + 1: max_adv_idx + 4]
            n_aligned_3 = sum(
                1 for c in post3
                if (c["close"] > c["open"]) == bullish
            )
            combo_gate = (n_aligned_3 >= 2)

            signals.append({
                "win":              win,
                "prev_h4_aligned":  prev_h4_aligned,
                "curr_h4_aligned":  curr_h4_aligned,
                "both_h4_aligned":  both_h4_aligned,
                "combo_gate":       combo_gate,
                "rr_level":         rr_level,
                "risk_level_atr":   risk_level / atr_5m,
                "tp_dist_atr":      tp_dist / atr_5m,
                "orig_risk_atr":    orig_risk / atr_5m,
            })

    return signals


def group_stats(label, sigs, overall_base_wr):
    n    = len(sigs)
    wins = sum(1 for s in sigs if s["win"])
    if n == 0:
        print(f"    {label}: n=0")
        return

    wr_val = wins / n
    rr_med = median([s["rr_level"] for s in sigs])
    ev     = wr_val * statistics.mean([s["rr_level"] for s in sigs]) - (1 - wr_val) * 1
    z, st  = z_test(wins, n, overall_base_wr)
    diff   = wr_val * 100 - overall_base_wr * 100
    sign   = "+" if diff >= 0 else ""

    print(f"    {label}")
    print(f"      WR={wr_s(wins, n, overall_base_wr)}  "
          f"median R:R={rr_med:.2f}:1  EV={ev:+.2f}R")


def main():
    db       = LocalDB(DB_PATH)
    analyzer = SMCAnalyzer()

    print("4h MTF alignment check — mini-BOS C1 retrace entry")
    print("Hypothesis: 4h aligned + 1h BOS + retrace + mini-BOS C1 → higher WR\n")

    for symbol in PAIRS:
        print(f"\n{'='*66}")
        print(f"  {symbol}")
        print(f"{'='*66}")

        sigs = analyze_pair(symbol, db, analyzer)
        if not sigs:
            print("  No signals"); continue

        n    = len(sigs)
        wins = sum(1 for s in sigs if s["win"])
        bwr  = wins / n

        print(f"\n  All mini-BOS C1 signals: n={n}  WR={bwr*100:.1f}%  (base)\n")

        # ── Split: prev 4h aligned vs counter ────────────────────────────────
        grp_4h_yes = [s for s in sigs if s["prev_h4_aligned"]]
        grp_4h_no  = [s for s in sigs if not s["prev_h4_aligned"]]

        print("  ── Previous completed 4h candle alignment ──────────────────")
        group_stats("4h ALIGNED (prev 4h candle aligned with BOS dir)", grp_4h_yes, bwr)
        group_stats("4h COUNTER (prev 4h candle counter to BOS dir)", grp_4h_no,  bwr)

        # ── Current 4h candle ─────────────────────────────────────────────────
        grp_c4_yes = [s for s in sigs if s["curr_h4_aligned"]]
        grp_c4_no  = [s for s in sigs if not s["curr_h4_aligned"]]

        print("\n  ── Current (in-progress) 4h candle alignment ───────────────")
        group_stats("4h ALIGNED (current 4h candle aligned)", grp_c4_yes, bwr)
        group_stats("4h COUNTER (current 4h candle counter)", grp_c4_no,  bwr)

        # ── Both 4h candles aligned ───────────────────────────────────────────
        grp_both_yes = [s for s in sigs if s["both_h4_aligned"]]
        grp_both_no  = [s for s in sigs if not s["both_h4_aligned"]]

        print("\n  ── Both 4h candles (prev + current) aligned ────────────────")
        group_stats("BOTH aligned  (strongest 4h confluence)", grp_both_yes, bwr)
        group_stats("NOT both aligned", grp_both_no, bwr)

        # ── The full cascade: prev 4h aligned + combo gate ───────────────────
        grp_cascade = [s for s in sigs if s["prev_h4_aligned"] and s["combo_gate"]]
        grp_cascade_c = [s for s in sigs if s["curr_h4_aligned"] and s["combo_gate"]]
        grp_both_cascade = [s for s in sigs if s["both_h4_aligned"] and s["combo_gate"]]

        print("\n  ── Full cascade (4h aligned + mini-BOS C1 + ≥2/3 post aligned) ──")
        group_stats("prev-4h + combo gate", grp_cascade, bwr)
        group_stats("curr-4h + combo gate", grp_cascade_c, bwr)
        group_stats("BOTH-4h + combo gate  ← strongest filter", grp_both_cascade, bwr)

        # ── Coverage: what % of signals fire in each group ───────────────────
        print(f"\n  Signal coverage (% of all mini-BOS C1 signals that pass each gate):")
        for lbl, g in [
            ("prev-4h aligned",       grp_4h_yes),
            ("curr-4h aligned",       grp_c4_yes),
            ("both-4h aligned",       grp_both_yes),
            ("prev-4h + combo",       grp_cascade),
            ("curr-4h + combo",       grp_cascade_c),
            ("both-4h + combo (top)", grp_both_cascade),
        ]:
            pct = len(g) / n * 100 if n else 0
            w   = sum(1 for s in g if s["win"])
            print(f"    {lbl:35s}  {pct:4.0f}%  ({len(g)} signals,  WR={wr_s(w, len(g))})")

    db.close()


if __name__ == "__main__":
    main()
