#!/usr/bin/env python3
"""
Full retrace-entry analysis for 4h BOS signals.

Analogous to the 1h BOS research arc but one timeframe up:
  4h BOS detected → first 5m close past the 4h broken level → entry
  Resolve on 400 5m candles (~33h)
  Track: MAE, retrace depth, mini-BOS C1 after bottom, daily alignment

For each 4h BOS:
  1. TIMING: which 5m candle (c01-c48) in the 4h period breaks the level
  2. RETRACE: how deep does price pull back before TP or SL?
  3. MINI-BOS C1: does the first post-bottom 5m candle close past bottom extreme?
  4. DAILY ALIGNMENT: is the daily candle aligned with the BOS direction?
  5. R:R MODEL: for mini-BOS C1 group, what is the EV with daily alignment filter?

Run from repo root: python local_dev/analyze_4h_bos_retrace.py
"""
import sys
import bisect
import math
import statistics
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from db.local_db import LocalDB
from analysis.smc_analyzer import SMCAnalyzer

DB_PATH = str(Path(__file__).resolve().parents[1] / "src" / "data" / "trade.db")
PAIRS   = ["NZDUSD", "EURUSD", "EURJPY", "USDCHF", "XAUUSD"]
LA_5M   = 400     # lookahead: ~33h of 5m candles

# ─── helpers ─────────────────────────────────────────────────────────────────

def z_test(wins, n, base):
    if n < 8 or base in (0, 1): return 0.0, ""
    se = math.sqrt(base * (1 - base) / n)
    if se == 0: return 0.0, ""
    z = (wins / n - base) / se
    st = ("★★★" if abs(z) >= 2.576 else "★★" if abs(z) >= 1.645 else "★" if abs(z) >= 0.842 else "")
    return z, st

def wr_s(wins, n, base=None, min_n=8):
    if n < min_n: return f"n={n} (thin)"
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

def print_split(label, rows, key_fn, base_wr, min_n=8):
    g = defaultdict(list)
    for r in rows:
        g[key_fn(r)].append(r)
    if not g: return
    print(f"\n  {label}:")
    for k in sorted(g.keys()):
        items = g[k]
        if len(items) < min_n: continue
        wins = sum(1 for x in items if x["win"])
        print(f"    {str(k):38s}  WR={wr_s(wins, len(items), base_wr)}")


# ─── per-pair analysis ────────────────────────────────────────────────────────

def analyze_pair(symbol, db, analyzer):
    h4_desc = db.query_recent(symbol, "4h",  limit=2600)   # ~1yr of 4h (6/day×365)
    m5_desc = db.query_recent(symbol, "5m",  limit=110000)
    d1_desc = db.query_recent(symbol, "1d",  limit=400)    # daily candles for alignment

    if not h4_desc or not m5_desc:
        return []

    m5_chron = list(reversed(m5_desc))
    d1_chron = list(reversed(d1_desc)) if d1_desc else []
    h4_n     = len(h4_desc)

    m5_ts  = [c["timestamp"] for c in m5_chron]
    d1_ts  = [c["timestamp"] for c in d1_chron]
    m5_map = {c["timestamp"]: i for i, c in enumerate(m5_chron)}

    min_5m_ts = m5_chron[0]["timestamp"]
    max_5m_ts = m5_chron[-1]["timestamp"]

    seen    = set()
    signals = []

    for k in range(30, h4_n - 5):
        window  = h4_desc[h4_n - 1 - k:]
        h4_open = window[0]["timestamp"]
        h4_end  = h4_open + 14400   # 4h = 14400s

        if h4_open < min_5m_ts or h4_end > max_5m_ts:
            continue

        bos_events = analyzer.detect_bos(
            window,
            params={"symbol": symbol, "timeframe": "4h",
                    "min_break_strength": 0.0,
                    "require_liquidity_sweep": False},
        )
        if not bos_events:
            continue

        atr_4h = analyzer.calculate_atr(window)
        if not atr_4h:
            continue

        for ev in bos_events:
            direction    = ev["direction"]
            broken_level = ev["broken_level"]
            bullish      = direction == "bullish"

            sig_key = (direction, round(broken_level, 5), h4_open)
            if sig_key in seen:
                continue
            seen.add(sig_key)

            # 5m candles inside the breaking 4h candle
            lo = bisect.bisect_left(m5_ts, h4_open)
            hi = bisect.bisect_left(m5_ts, h4_end)
            period = m5_chron[lo:hi]

            if len(period) < 2 or lo < 20:
                continue

            atr_5m = analyzer.calculate_atr(m5_chron[lo - 20:lo])
            if not atr_5m:
                continue

            # First 5m close past broken level
            break_idx = None
            for i, c in enumerate(period):
                if bullish and c["close"] > broken_level:     break_idx = i; break
                if not bullish and c["close"] < broken_level: break_idx = i; break
            if break_idx is None:
                continue

            candle_num = break_idx + 1   # 1-indexed within the 4h period (up to 48)

            break_c    = period[break_idx]
            orig_entry = break_c["close"]
            orig_risk  = abs(orig_entry - broken_level)
            if orig_risk < atr_5m * 0.05:
                continue

            orig_tp = (orig_entry + 2 * orig_risk) if bullish else (orig_entry - 2 * orig_risk)
            g_idx   = m5_map.get(break_c["timestamp"])
            if g_idx is None:
                continue

            # Resolve outcome + track MAE + retrace bottom
            outcome     = "OPEN"
            mae_r       = 0.0
            max_adv_idx = g_idx
            candles_to  = 0

            for j, fc in enumerate(m5_chron[g_idx + 1: g_idx + 1 + LA_5M]):
                adverse = (max(0.0, orig_entry - fc["low"]) if bullish
                           else max(0.0, fc["high"] - orig_entry))
                if adverse / orig_risk > mae_r:
                    mae_r       = adverse / orig_risk
                    max_adv_idx = g_idx + 1 + j

                if bullish:
                    if fc["high"] >= orig_tp: outcome = "WIN";  candles_to = j + 1; break
                    if fc["low"]  <= broken_level: outcome = "LOSS"; candles_to = j + 1; break
                else:
                    if fc["low"]  <= orig_tp: outcome = "WIN";  candles_to = j + 1; break
                    if fc["high"] >= broken_level: outcome = "LOSS"; candles_to = j + 1; break

            if outcome == "OPEN":
                continue

            win = outcome == "WIN"

            # Retrace depth bucket
            if mae_r < 0.25:
                retrace = "direct (<0.25R)"
            elif mae_r < 0.50:
                retrace = "shallow (0.25-0.5R)"
            elif mae_r < 0.75:
                retrace = "deep (0.5-0.75R)"
            else:
                retrace = "very_deep (0.75-1R)"

            # Mini-BOS C1 detection (post-bottom)
            bottom_c    = m5_chron[max_adv_idx]
            mini_bos_c1 = False
            new_entry   = None
            risk_level  = None
            rr_level    = None

            if max_adv_idx + 1 < len(m5_chron):
                c1 = m5_chron[max_adv_idx + 1]
                mini_bos_c1 = ((c1["close"] > bottom_c["high"]) if bullish
                                else (c1["close"] < bottom_c["low"]))
                if mini_bos_c1:
                    new_entry  = c1["close"]
                    risk_level = abs(new_entry - broken_level)
                    if risk_level > 1e-8:
                        tp_dist   = abs(orig_tp - new_entry)
                        rr_level  = tp_dist / risk_level

            # Post-bottom 3-candle alignment
            post3 = m5_chron[max_adv_idx + 1: max_adv_idx + 4]
            n_aligned_3 = sum(1 for c in post3 if (c["close"] > c["open"]) == bullish)

            # Daily candle alignment at the time of the 4h BOS
            d1_aligned = None
            if d1_chron:
                # Find the daily candle that was COMPLETED before the 4h BOS
                d1_pos = bisect.bisect_left(d1_ts, h4_open)
                if d1_pos >= 2:
                    prev_d1 = d1_chron[d1_pos - 2]   # completed before h4_open
                    d1_aligned = ((prev_d1["close"] > prev_d1["open"]) if bullish
                                  else (prev_d1["close"] < prev_d1["open"]))
                elif d1_pos >= 1:
                    prev_d1 = d1_chron[d1_pos - 1]
                    d1_aligned = ((prev_d1["close"] > prev_d1["open"]) if bullish
                                  else (prev_d1["close"] < prev_d1["open"]))

            # Current daily candle (in-progress at h4_open)
            curr_d1_aligned = None
            if d1_chron:
                d1_pos = bisect.bisect_left(d1_ts, h4_open)
                if d1_pos >= 1:
                    curr_d1 = d1_chron[d1_pos - 1]
                    curr_d1_aligned = ((curr_d1["close"] > curr_d1["open"]) if bullish
                                       else (curr_d1["close"] < curr_d1["open"]))

            # Timing bucket
            if candle_num <= 6:   timing = "early  (c01-c06)"
            elif candle_num <= 12: timing = "mid-e  (c07-c12)"
            elif candle_num <= 24: timing = "mid    (c13-c24)"
            elif candle_num <= 36: timing = "late   (c25-c36)"
            else:                  timing = "final  (c37-c48)"

            signals.append({
                "win":            win,
                "mae_r":          mae_r,
                "retrace":        retrace,
                "candle_num":     candle_num,
                "timing":         timing,
                "candles_to":     candles_to,
                "mini_bos_c1":    mini_bos_c1,
                "n_aligned_3":    n_aligned_3,
                "d1_aligned":     d1_aligned,
                "curr_d1_aligned": curr_d1_aligned,
                "new_entry":      new_entry,
                "risk_level_atr": risk_level / atr_5m if risk_level else None,
                "rr_level":       rr_level,
                "orig_risk_atr":  orig_risk / atr_5m,
                "tp_dist_atr":    abs(orig_tp - (new_entry or orig_entry)) / atr_5m,
            })

    return signals


def main():
    db       = LocalDB(DB_PATH)
    analyzer = SMCAnalyzer()

    print("4h BOS retrace analysis — MTF cascade: daily → 4h → 1h → 5m mini-BOS C1")
    print("Lookahead: 400 5m candles (~33h) | SL=broken_level TP=entry+2R\n")

    for symbol in PAIRS:
        print(f"\n{'='*68}")
        print(f"  {symbol}")
        print(f"{'='*68}")

        sigs = analyze_pair(symbol, db, analyzer)
        if not sigs:
            print("  No data"); continue

        n    = len(sigs)
        wins = sum(1 for s in sigs if s["win"])
        bwr  = wins / n
        print(f"\n  Resolved 4h BOS signals: {n}  baseline WR={bwr*100:.1f}%")

        # ── 1. Timing ────────────────────────────────────────────────────────
        print_split("Timing — which 5m candle in the 4h breaks the level",
                    sigs, lambda s: s["timing"], bwr)

        # ── 2. Retrace depth ─────────────────────────────────────────────────
        print_split("Retrace depth before resolution",
                    sigs, lambda s: s["retrace"], bwr)

        # ── 3. Mini-BOS C1 after deep retrace ────────────────────────────────
        deep = [s for s in sigs if s["mae_r"] >= 0.75]
        if deep:
            n_d  = len(deep)
            w_d  = sum(1 for s in deep if s["win"])
            bwr_d = w_d / n_d
            print(f"\n  Very-deep retrace (MAE 0.75-1R): n={n_d}  WR={bwr_d*100:.1f}%")

            mini_bos_sigs = [s for s in deep if s["mini_bos_c1"]]
            no_mini_bos   = [s for s in deep if not s["mini_bos_c1"]]
            w_mb = sum(1 for s in mini_bos_sigs if s["win"])
            w_nm = sum(1 for s in no_mini_bos if s["win"])
            print(f"\n  mini-BOS C1 after retrace bottom:")
            print(f"    FIRED   WR={wr_s(w_mb, len(mini_bos_sigs), bwr_d)}")
            print(f"    NO fire WR={wr_s(w_nm, len(no_mini_bos),   bwr_d)}")

            # Combo gate
            combo     = [s for s in mini_bos_sigs if s["n_aligned_3"] >= 2]
            no_combo  = [s for s in mini_bos_sigs if s["n_aligned_3"] < 2]
            w_cb = sum(1 for s in combo if s["win"])
            w_nc = sum(1 for s in no_combo if s["win"])
            print(f"\n  mini-BOS C1 + combo gate (≥2/3 aligned):")
            print(f"    PASS WR={wr_s(w_cb, len(combo),    bwr_d)}")
            print(f"    FAIL WR={wr_s(w_nc, len(no_combo), bwr_d)}")

        # ── 4. Daily alignment ───────────────────────────────────────────────
        d1_yes = [s for s in sigs if s["d1_aligned"] is True]
        d1_no  = [s for s in sigs if s["d1_aligned"] is False]
        curr_d1_yes = [s for s in sigs if s["curr_d1_aligned"] is True]
        curr_d1_no  = [s for s in sigs if s["curr_d1_aligned"] is False]
        both_d1 = [s for s in sigs if s["d1_aligned"] and s["curr_d1_aligned"]]
        not_both_d1 = [s for s in sigs if not (s["d1_aligned"] and s["curr_d1_aligned"])]

        print(f"\n  ── Daily candle alignment ──────────────────────────────────")
        w_dy = sum(1 for s in d1_yes if s["win"])
        w_dn = sum(1 for s in d1_no  if s["win"])
        w_cy = sum(1 for s in curr_d1_yes if s["win"])
        w_cn = sum(1 for s in curr_d1_no  if s["win"])
        w_by = sum(1 for s in both_d1 if s["win"])
        w_bn = sum(1 for s in not_both_d1 if s["win"])
        print(f"    Prev daily aligned:   WR={wr_s(w_dy, len(d1_yes), bwr)}")
        print(f"    Prev daily counter:   WR={wr_s(w_dn, len(d1_no),  bwr)}")
        print(f"    Curr daily aligned:   WR={wr_s(w_cy, len(curr_d1_yes), bwr)}")
        print(f"    Curr daily counter:   WR={wr_s(w_cn, len(curr_d1_no),  bwr)}")
        print(f"    BOTH daily aligned:   WR={wr_s(w_by, len(both_d1),     bwr)}")
        print(f"    NOT both aligned:     WR={wr_s(w_bn, len(not_both_d1), bwr)}")

        # ── 5. R:R model for mini-BOS C1 group ───────────────────────────────
        if deep:
            rr_sigs = [s for s in deep if s["mini_bos_c1"] and s["rr_level"] is not None]
            if rr_sigs:
                rr_wins = [s for s in rr_sigs if s["win"]]
                wr_mb   = sum(1 for s in rr_sigs if s["win"]) / len(rr_sigs)
                avg_rr  = statistics.mean(s["rr_level"] for s in rr_sigs)
                med_rr  = median([s["rr_level"] for s in rr_sigs])
                ev      = wr_mb * avg_rr - (1 - wr_mb) * 1

                orig_med  = median([s["orig_risk_atr"] for s in rr_sigs])
                new_med   = median([s["risk_level_atr"] for s in rr_sigs if s["risk_level_atr"]])

                print(f"\n  ── R:R model (mini-BOS C1 retrace entry, SL=broken_level) ──")
                print(f"    n={len(rr_sigs)}  WR={wr_mb*100:.1f}%  R:R med={med_rr:.2f}  avg={avg_rr:.2f}  EV={ev:+.2f}R")
                print(f"    Orig break entry risk : {orig_med:.2f}×ATR5m")
                print(f"    Retrace entry risk    : {new_med:.2f}×ATR5m  ({new_med/orig_med*100:.0f}% of orig)")

                # Daily alignment × mini-BOS C1
                mb_d1_yes = [s for s in rr_sigs if s["d1_aligned"]]
                mb_d1_no  = [s for s in rr_sigs if s["d1_aligned"] is False]
                mb_both   = [s for s in rr_sigs if s["d1_aligned"] and s["curr_d1_aligned"]]
                mb_curr   = [s for s in rr_sigs if s["curr_d1_aligned"]]

                print(f"\n  ── mini-BOS C1 × daily alignment ───────────────────────────")
                for lbl, grp in [
                    ("prev daily aligned", mb_d1_yes),
                    ("prev daily counter", mb_d1_no),
                    ("curr daily aligned", mb_curr),
                    ("BOTH daily aligned", mb_both),
                ]:
                    if len(grp) < 8: continue
                    w    = sum(1 for s in grp if s["win"])
                    ev_g = (w / len(grp)) * statistics.mean(s["rr_level"] for s in grp) - (1 - w/len(grp))
                    pct  = len(grp) / len(rr_sigs) * 100
                    z, st = z_test(w, len(grp), wr_mb)
                    print(f"    {lbl:25s}  WR={wr_s(w, len(grp))}  EV={ev_g:+.2f}R  "
                          f"({pct:.0f}% of mini-BOS C1)  z={z:+.2f}{st}")

    db.close()


if __name__ == "__main__":
    main()
