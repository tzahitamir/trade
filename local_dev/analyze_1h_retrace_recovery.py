#!/usr/bin/env python3
"""
For 1h BOS signals where price retrace deeply (MAE 0.75-1R, i.e. almost hit SL):
  1. Find the retrace bottom (lowest wick for bullish, highest wick for bearish)
  2. Analyze the 5m candles AFTER the bottom: C1..C8 from the bottom candle
  3. Compare WIN vs LOSS: what pattern signals momentum has recovered?

Key questions:
  - How many candles after the bottom before an "aligned" candle closes?
  - What body ATR does the first recovery candle need?
  - Does a mini 5m BOS at the bottom (close > prev high) signal recovery?
  - How many lookback candles do we need to confirm direction is back?

Run from repo root: python local_dev/analyze_1h_retrace_recovery.py
"""
import sys
import bisect
import math
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from db.local_db import LocalDB
from analysis.smc_analyzer import SMCAnalyzer

DB_PATH = str(Path(__file__).resolve().parents[1] / "src" / "data" / "trade.db")
PAIRS   = ["NZDUSD", "EURUSD", "EURJPY", "USDCHF", "XAUUSD"]
LA_5M   = 120

def z_test(wins, n, base):
    if n < 8: return 0.0, ""
    p  = wins / n
    se = math.sqrt(base * (1 - base) / n)
    if se == 0: return 0.0, ""
    z = (p - base) / se
    st = ("★★★" if abs(z) >= 2.576 else
          "★★"  if abs(z) >= 1.645 else
          "★"   if abs(z) >= 0.842 else "")
    return z, st

def wr_s(wins, n, base=None, min_n=8):
    if n < min_n: return f"n={n}"
    pct = wins / n * 100
    s   = f"{pct:.1f}%  n={n}"
    if base is not None:
        z, st = z_test(wins, n, base)
        diff  = pct - base * 100
        sign  = "+" if diff >= 0 else ""
        s    += f"  ({sign}{diff:.1f}pp z={z:+.2f}{st})"
    return s

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
        window   = h1_desc[h1_n - 1 - k:]
        h1_open  = window[0]["timestamp"]
        h1_end   = h1_open + 3600

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

            # First 5m close past broken level
            break_idx = None
            for i, c in enumerate(period):
                if bullish and c["close"] > broken_level: break_idx = i; break
                if not bullish and c["close"] < broken_level: break_idx = i; break
            if break_idx is None:
                continue

            break_c = period[break_idx]
            entry   = break_c["close"]
            risk    = abs(entry - broken_level)
            if risk < atr_5m * 0.05:
                continue

            tp  = (entry + 2 * risk) if bullish else (entry - 2 * risk)
            sl  = broken_level
            g_idx = m5_map.get(break_c["timestamp"])
            if g_idx is None:
                continue

            # Resolve outcome + track full price path for retrace analysis
            outcome    = "OPEN"
            mae_r      = 0.0
            max_adv_idx = g_idx  # index of candle with worst adverse

            future = m5_chron[g_idx + 1: g_idx + 1 + LA_5M]
            for j, fc in enumerate(future):
                if bullish:
                    adverse = max(0.0, entry - fc["low"])
                else:
                    adverse = max(0.0, fc["high"] - entry)
                cur_mae = adverse / risk
                if cur_mae > mae_r:
                    mae_r       = cur_mae
                    max_adv_idx = g_idx + 1 + j

                if bullish:
                    if fc["high"] >= tp:  outcome = "WIN";  break
                    if fc["low"]  <= sl:  outcome = "LOSS"; break
                else:
                    if fc["low"]  <= tp:  outcome = "WIN";  break
                    if fc["high"] >= sl:  outcome = "LOSS"; break

            if outcome == "OPEN":
                continue

            # Only analyse very-deep retraces (0.75-1R) — the interesting group
            if mae_r < 0.75:
                continue

            win = outcome == "WIN"

            # ── Analyse 5m candles after the retrace bottom ──────────────
            # max_adv_idx = index of the "bottom" candle in m5_chron
            # We want candles C1..C8 AFTER the bottom
            post = m5_chron[max_adv_idx + 1: max_adv_idx + 9]  # up to 8 candles

            if not post:
                continue

            # For each post-bottom candle: aligned?, body ATR, close > prev high (mini-BOS)?
            post_info = []
            prev_high = m5_chron[max_adv_idx]["high"]
            prev_low  = m5_chron[max_adv_idx]["low"]
            for c in post:
                aligned  = (c["close"] > c["open"]) == bullish
                body_atr = abs(c["close"] - c["open"]) / atr_5m
                # mini-BOS: close beats the prior candle's extreme in BOS direction
                if bullish:
                    mini_bos = c["close"] > prev_high
                else:
                    mini_bos = c["close"] < prev_low
                post_info.append({
                    "aligned":  aligned,
                    "body_atr": body_atr,
                    "mini_bos": mini_bos,
                })
                prev_high = c["high"]
                prev_low  = c["low"]

            # First aligned candle position (1-indexed, 0 = none in 8 candles)
            first_aligned = 0
            for i, p in enumerate(post_info):
                if p["aligned"]:
                    first_aligned = i + 1
                    break

            # First mini-BOS candle position
            first_mini_bos = 0
            for i, p in enumerate(post_info):
                if p["mini_bos"]:
                    first_mini_bos = i + 1
                    break

            # Consecutive counter candles at bottom (before first aligned)
            consec_counter = first_aligned - 1 if first_aligned > 0 else len(post_info)

            # How many aligned in first 3 post-bottom candles?
            n_aligned_3 = sum(1 for p in post_info[:3] if p["aligned"])
            n_aligned_5 = sum(1 for p in post_info[:5] if p["aligned"])

            # Body ATR of first aligned candle
            body_first_aln = post_info[first_aligned - 1]["body_atr"] if first_aligned > 0 else None

            # Sequence pattern of first 3 post-bottom candles (A/C)
            seq3 = "".join("A" if p["aligned"] else "C" for p in post_info[:3])

            signals.append({
                "win":            win,
                "mae_r":          mae_r,
                "first_aligned":  first_aligned,
                "first_mini_bos": first_mini_bos,
                "consec_counter": consec_counter,
                "n_aligned_3":    n_aligned_3,
                "n_aligned_5":    n_aligned_5,
                "body_first_aln": body_first_aln,
                "seq3":           seq3,
                "n_post":         len(post_info),
            })

    return signals


def atr_bucket(v):
    if v is None: return "n/a"
    if v < 0.3:   return "<0.3"
    if v < 0.7:   return "0.3–0.7"
    if v < 1.5:   return "0.7–1.5"
    return               "≥1.5"

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
        print(f"    {str(k):35s}  WR={wr_s(wins, len(items), base_wr)}")


def main():
    db       = LocalDB(DB_PATH)
    analyzer = SMCAnalyzer()

    for symbol in PAIRS:
        print(f"\n{'='*66}")
        print(f"  {symbol}  — post-retrace 5m recovery analysis")
        print(f"  (only signals where price retraced 0.75–1R before resolution)")
        print(f"{'='*66}")

        sigs = analyze_pair(symbol, db, analyzer)
        if not sigs:
            print("  No data"); continue

        n    = len(sigs)
        wins = sum(1 for s in sigs if s["win"])
        bwr  = wins / n
        print(f"\n  Very-deep retrace signals: {n}")
        print(f"  WIN={wins} ({bwr*100:.1f}%)  LOSS={n-wins} ({(n-wins)/n*100:.1f}%)")

        # ── 1. First aligned candle after bottom ──────────────────────────
        print_split("First aligned candle after bottom (position 1–8)",
                    sigs, lambda s: f"C{s['first_aligned']}" if s["first_aligned"] else "none in 8",
                    bwr, min_n=6)

        # ── 2. First mini-BOS (close past prior candle's extreme) ─────────
        print_split("First mini-BOS after bottom (close > prev high/low)",
                    sigs, lambda s: f"C{s['first_mini_bos']}" if s["first_mini_bos"] else "none in 8",
                    bwr, min_n=6)

        # ── 3. # aligned in first 3 post-bottom candles ───────────────────
        print_split("# aligned in first 3 post-bottom candles",
                    sigs, lambda s: f"{s['n_aligned_3']}/3 aligned",
                    bwr)

        # ── 4. # aligned in first 5 post-bottom candles ───────────────────
        print_split("# aligned in first 5 post-bottom candles",
                    sigs, lambda s: f"{s['n_aligned_5']}/5 aligned",
                    bwr)

        # ── 5. Consecutive counter candles at bottom ───────────────────────
        print_split("Consecutive counter candles at the bottom",
                    sigs, lambda s: f"{min(s['consec_counter'], 5)}{'+'if s['consec_counter']>=5 else ''} counter",
                    bwr, min_n=6)

        # ── 6. Body ATR of first aligned recovery candle ──────────────────
        aln_sigs = [s for s in sigs if s["body_first_aln"] is not None]
        print_split("First aligned candle body ATR",
                    aln_sigs, lambda s: atr_bucket(s["body_first_aln"]),
                    bwr)

        # ── 7. Seq3 (A/C pattern of first 3 post-bottom candles) ──────────
        seq_sigs = [s for s in sigs if len(s["seq3"]) == 3]
        print_split("Post-bottom 3-candle seq (A=aligned BOS dir, C=counter)",
                    seq_sigs, lambda s: s["seq3"],
                    bwr, min_n=6)

        # ── 8. Cumulative: by C1-C3 aligned AND mini-BOS ─────────────────
        print(f"\n  Recovery gate combos (first N post-bottom candles):")
        combos = [
            ("C1 aligned",           lambda s: s["first_aligned"] == 1),
            ("C1 or C2 aligned",     lambda s: s["first_aligned"] in (1, 2)),
            ("mini-BOS in C1",       lambda s: s["first_mini_bos"] == 1),
            ("mini-BOS in C1–C2",    lambda s: s["first_mini_bos"] in (1, 2)),
            ("≥2/3 aligned",         lambda s: s["n_aligned_3"] >= 2),
            ("3/3 aligned",          lambda s: s["n_aligned_3"] == 3),
            ("C1 aln + body≥0.3",    lambda s: s["first_aligned"] == 1 and
                                               (s["body_first_aln"] or 0) >= 0.3),
            ("mini-BOS C1 + ≥2/3",   lambda s: s["first_mini_bos"] == 1 and
                                               s["n_aligned_3"] >= 2),
        ]
        for label, fn in combos:
            subset = [s for s in sigs if fn(s)]
            complement = [s for s in sigs if not fn(s)]
            ws  = sum(1 for x in subset if x["win"])
            wc  = sum(1 for x in complement if x["win"])
            ns  = len(subset)
            nc  = len(complement)
            pct_signals = ns / n * 100 if n else 0
            print(f"    {label:32s}  "
                  f"PASS {wr_s(ws, ns, bwr, min_n=6)}  |  "
                  f"FAIL {wr_s(wc, nc, bwr, min_n=6)}  "
                  f"[{pct_signals:.0f}% of signals pass]")

    db.close()


if __name__ == "__main__":
    main()
