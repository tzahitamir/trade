#!/usr/bin/env python3
"""
Analyze what 5m candle sequences lead to valid 1h BOS early entries.

For each 1h BOS detected (base params, no strength filter):
  - Find the first 5m candle to CLOSE past the broken level within the breaking 1h candle
  - Entry = that 5m close, SL = broken level, TP = entry + 2R
  - Resolve outcome on next 100 5m candles
  - Analyze: timing (which 5m #), pre-break alignment, C_break / C_post body ATR

Run from repo root: python local_dev/analyze_1h_bos_5m_sequence.py
"""
import sys
import bisect
import math
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from db.local_db import LocalDB
from analysis.smc_analyzer import SMCAnalyzer

DB_PATH  = str(Path(__file__).resolve().parents[1] / "src" / "data" / "trade.db")
PAIRS    = ["NZDUSD", "EURUSD", "EURJPY", "USDCHF", "XAUUSD"]
LA_5M    = 100   # lookahead 5m candles for TP/SL resolution (~8h)

# ─── helpers ────────────────────────────────────────────────────────────────

def z_test(wins, n, baseline_wr):
    """One-proportion z-test vs baseline WR. Returns (z, stars)."""
    if n < 8:
        return 0.0, ""
    p = wins / n
    se = math.sqrt(baseline_wr * (1 - baseline_wr) / n)
    if se == 0:
        return 0.0, ""
    z = (p - baseline_wr) / se
    stars = ("★★★" if abs(z) >= 2.576 else
             "★★"  if abs(z) >= 1.645 else
             "★"   if abs(z) >= 0.842 else "")
    return z, stars

def wr(wins, n, baseline=None, min_n=8):
    if n < min_n:
        return f"n={n}"
    pct = wins / n * 100
    s = f"{pct:.1f}%  n={n}"
    if baseline is not None:
        z, stars = z_test(wins, n, baseline)
        diff = pct - baseline * 100
        sign = "+" if diff >= 0 else ""
        s += f"  ({sign}{diff:.1f}pp  z={z:+.2f} {stars})"
    return s

def atr_bucket(v):
    if v is None:   return "n/a"
    if v < 0.3:     return "<0.3"
    if v < 0.7:     return "0.3–0.7"
    if v < 1.5:     return "0.7–1.5"
    if v < 2.5:     return "1.5–2.5"
    return          "≥2.5"

def timing_bucket(n):
    if n <= 3:   return "early  (c1–c3)"
    if n <= 6:   return "mid-e  (c4–c6)"
    if n <= 9:   return "mid-l  (c7–c9)"
    return       "late   (c10–c12)"

def print_split(label, rows, key_fn, baseline_wr, min_n=8):
    g = defaultdict(list)
    for r in rows:
        g[key_fn(r)].append(r)
    if not g:
        return
    print(f"\n  {label}:")
    for k in sorted(g.keys()):
        items = g[k]
        if len(items) < min_n:
            continue
        wins = sum(1 for x in items if x["win"])
        print(f"    {str(k):28s}  WR={wr(wins, len(items), baseline_wr)}")

# ─── per-pair analysis ───────────────────────────────────────────────────────

def analyze_pair(symbol, db, analyzer):
    h1_desc = db.query_recent(symbol, "1h", limit=5600)   # newest-first
    m5_desc = db.query_recent(symbol, "5m", limit=110000) # newest-first

    if not h1_desc or not m5_desc:
        return []

    m5_chron = list(reversed(m5_desc))
    h1_n     = len(h1_desc)

    # sorted timestamp list for bisect
    m5_ts = [c["timestamp"] for c in m5_chron]
    m5_map = {c["timestamp"]: i for i, c in enumerate(m5_chron)}

    min_5m_ts = m5_chron[0]["timestamp"]
    max_5m_ts = m5_chron[-1]["timestamp"]

    seen    = set()
    signals = []

    # slide forward through 1h candles; window[0] = break candle (descending)
    for k in range(30, h1_n - 5):
        window   = h1_desc[h1_n - 1 - k:]   # descending; window[0] = break candle
        break_1h = window[0]
        h1_open  = break_1h["timestamp"]
        h1_end   = h1_open + 3600

        if h1_open < min_5m_ts or h1_end > max_5m_ts:
            continue

        bos_events = analyzer.detect_bos(
            window,
            params={
                "symbol": symbol, "timeframe": "1h",
                "min_break_strength": 0.0,
                "require_liquidity_sweep": False,
            },
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

            # 5m candles inside the breaking 1h candle
            lo = bisect.bisect_left(m5_ts, h1_open)
            hi = bisect.bisect_left(m5_ts, h1_end)
            period = m5_chron[lo:hi]

            if len(period) < 2:
                continue

            # ATR on 5m: needs ≥15 candles (period+1) before the period
            if lo < 20:
                continue
            atr_5m = analyzer.calculate_atr(m5_chron[lo - 20:lo])
            if not atr_5m:
                continue

            # First 5m candle to CLOSE past broken level
            break_idx = None
            for i, c in enumerate(period):
                if bullish and c["close"] > broken_level:
                    break_idx = i; break
                if not bullish and c["close"] < broken_level:
                    break_idx = i; break

            if break_idx is None:
                continue

            candle_num = break_idx + 1   # 1–12
            break_c    = period[break_idx]
            entry      = break_c["close"]
            risk       = abs(entry - broken_level)

            if risk < atr_5m * 0.05:    # ignore micro-risk artefacts
                continue

            tp = (entry + 2 * risk) if bullish else (entry - 2 * risk)
            sl = broken_level

            # Resolve on next LA_5M candles
            g_idx   = m5_map.get(break_c["timestamp"])
            if g_idx is None:
                continue

            outcome = "OPEN"
            for fc in m5_chron[g_idx + 1: g_idx + 1 + LA_5M]:
                if bullish:
                    if fc["high"] >= tp:  outcome = "WIN";  break
                    if fc["low"]  <= sl:  outcome = "LOSS"; break
                else:
                    if fc["low"]  <= tp:  outcome = "WIN";  break
                    if fc["high"] >= sl:  outcome = "LOSS"; break

            if outcome == "OPEN":
                continue

            win = outcome == "WIN"

            # ── candle metrics ──────────────────────────────────────────────

            # break candle
            b_brk = abs(break_c["close"] - break_c["open"]) / atr_5m
            a_brk = (break_c["close"] > break_c["open"]) == bullish

            # candle before break (C_pre)
            b_pre = a_pre = None
            if break_idx > 0:
                cp    = period[break_idx - 1]
                b_pre = abs(cp["close"] - cp["open"]) / atr_5m
                a_pre = (cp["close"] > cp["open"]) == bullish

            # candle after break (C_post)
            b_pst = a_pst = None
            if g_idx + 1 < len(m5_chron):
                cn    = m5_chron[g_idx + 1]
                b_pst = abs(cn["close"] - cn["open"]) / atr_5m
                a_pst = (cn["close"] > cn["open"]) == bullish

            # Pre-break alignment sequence (last 3 5m candles before break)
            pre_seq = []
            for i in range(max(0, break_idx - 3), break_idx):
                c  = period[i]
                al = (c["close"] > c["open"]) == bullish
                pre_seq.append("A" if al else "C")

            n_pre_aln = sum(1 for x in pre_seq if x == "A")

            # transition C_pre → C_break
            if a_pre is not None:
                transition = ("A→A" if a_pre and a_brk  else
                              "A→C" if a_pre and not a_brk else
                              "C→A" if not a_pre and a_brk else "C→C")
            else:
                transition = None

            signals.append({
                "win":         win,
                "direction":   direction,
                "candle_num":  candle_num,
                "b_brk":       b_brk,
                "a_brk":       a_brk,
                "b_pre":       b_pre,
                "a_pre":       a_pre,
                "b_pst":       b_pst,
                "a_pst":       a_pst,
                "pre_seq":     "".join(pre_seq),
                "n_pre_aln":   n_pre_aln,
                "transition":  transition,
                "risk_atr":    risk / atr_5m,
            })

    return signals


# ─── main ────────────────────────────────────────────────────────────────────

def main():
    db       = LocalDB(DB_PATH)
    analyzer = SMCAnalyzer()

    for symbol in PAIRS:
        print(f"\n{'='*62}")
        print(f"  {symbol}")
        print(f"{'='*62}")

        sigs = analyze_pair(symbol, db, analyzer)
        if not sigs:
            print("  No data"); continue

        n    = len(sigs)
        wins = sum(1 for s in sigs if s["win"])
        bwr  = wins / n          # baseline win rate
        print(f"\n  Resolved signals : {n}    baseline WR={bwr*100:.1f}%")

        if n < 20:
            print("  Too few signals"); continue

        # ── 1. Timing: which 5m candle in the hour ──────────────────────────
        print_split("Timing — 5m candle # in the 1h",
                    sigs, lambda s: f"c{s['candle_num']:02d}", bwr)

        print_split("Timing bucket",
                    sigs, lambda s: timing_bucket(s["candle_num"]), bwr)

        # ── 2. Break candle ─────────────────────────────────────────────────
        print_split("C_break body (ATR)",
                    sigs, lambda s: atr_bucket(s["b_brk"]), bwr)

        print_split("C_break alignment",
                    sigs, lambda s: "aligned" if s["a_brk"] else "counter", bwr)

        # ── 3. Candle before break ──────────────────────────────────────────
        pre = [s for s in sigs if s["b_pre"] is not None]
        print_split("C_pre body (ATR)",
                    pre, lambda s: atr_bucket(s["b_pre"]), bwr)

        print_split("C_pre alignment",
                    pre, lambda s: "aligned" if s["a_pre"] else "counter", bwr)

        # ── 4. Transition C_pre → C_break ───────────────────────────────────
        tr = [s for s in sigs if s["transition"] is not None]
        print_split("C_pre → C_break transition",
                    tr, lambda s: s["transition"], bwr)

        # ── 5. Post-break follow-through ─────────────────────────────────────
        pst = [s for s in sigs if s["b_pst"] is not None]
        print_split("C_post body (follow-through ATR)",
                    pst, lambda s: atr_bucket(s["b_pst"]), bwr)

        print_split("C_post alignment",
                    pst, lambda s: "aligned" if s["a_pst"] else "counter", bwr)

        # ── 6. Pre-break 3-candle alignment sequence ─────────────────────────
        seq3 = [s for s in sigs if len(s["pre_seq"]) == 3]
        print_split("Pre-break 3-candle seq (A=aligned C=counter)",
                    seq3, lambda s: s["pre_seq"], bwr)

        print_split("Pre-break # aligned (last 3)",
                    seq3, lambda s: f"{s['n_pre_aln']}/3 aligned", bwr)

        # ── 7. Combo: timing + C_break alignment ─────────────────────────────
        print_split("Timing bucket × C_break alignment",
                    sigs,
                    lambda s: f"{timing_bucket(s['candle_num'])} | {'aln' if s['a_brk'] else 'ctr'}",
                    bwr)

    db.close()


if __name__ == "__main__":
    main()
