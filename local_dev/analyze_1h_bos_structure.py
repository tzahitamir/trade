#!/usr/bin/env python3
"""
Analyze the multi-TF structure of 1h BOS signals.

For each 1h BOS (5m early entry at first 5m close past broken level):
  1. Did the 15m also BOS the same level? (same direction)
  2. After the 5m break, how deep was the retrace before TP or SL?
  3. How many 5m candles elapsed before resolution?
  4. For WIN: was there a retrace > 0.5R before TP? (retrace-then-expand pattern)
  5. Is 15m BOS + retrace a cleaner entry than direct 5m entry?

The sequence the user is asking about:
  5m breaks 1h level → 15m confirms BOS → 5m retraces (C→A on 5m)
  → 5m expands again → hits 1h TP

Run from repo root: python local_dev/analyze_1h_bos_structure.py
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
LA_5M   = 120   # lookahead candles for resolution (~10h)

def z_test(wins, n, base):
    if n < 8: return 0.0, ""
    p  = wins / n
    se = math.sqrt(base * (1 - base) / n)
    if se == 0: return 0.0, ""
    z = (p - base) / se
    stars = ("★★★" if abs(z) >= 2.576 else
             "★★"  if abs(z) >= 1.645 else
             "★"   if abs(z) >= 0.842 else "")
    return z, stars

def wr_s(wins, n, base=None, min_n=8):
    if n < min_n: return f"n={n}"
    pct = wins / n * 100
    s   = f"{pct:.1f}%  n={n}"
    if base is not None:
        z, st = z_test(wins, n, base)
        diff  = pct - base * 100
        sign  = "+" if diff >= 0 else ""
        s += f"  ({sign}{diff:.1f}pp z={z:+.2f}{st})"
    return s

def analyze_pair(symbol, db, analyzer):
    h1_desc = db.query_recent(symbol, "1h",  limit=5600)
    m5_desc = db.query_recent(symbol, "5m",  limit=110000)
    m15_desc= db.query_recent(symbol, "15m", limit=32000)

    if not h1_desc or not m5_desc or not m15_desc:
        return []

    m5_chron  = list(reversed(m5_desc))
    m15_chron = list(reversed(m15_desc))
    h1_n      = len(h1_desc)

    m5_ts  = [c["timestamp"] for c in m5_chron]
    m15_ts = [c["timestamp"] for c in m15_chron]
    m5_map = {c["timestamp"]: i for i, c in enumerate(m5_chron)}

    min_5m_ts = m5_chron[0]["timestamp"]
    max_5m_ts = m5_chron[-1]["timestamp"]

    seen    = set()
    signals = []

    for k in range(30, h1_n - 5):
        window   = h1_desc[h1_n - 1 - k:]
        break_1h = window[0]
        h1_open  = break_1h["timestamp"]
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

            # ── 5m candles inside the breaking 1h ──────────────────────
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
                if bullish and c["close"] > broken_level:
                    break_idx = i; break
                if not bullish and c["close"] < broken_level:
                    break_idx = i; break

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

            # ── Resolve outcome + track MAE + time-to-resolution ───────
            outcome     = "OPEN"
            mae_r       = 0.0   # max adverse excursion in R units (0→1)
            candles_to  = 0     # 5m candles from entry to resolution
            for j, fc in enumerate(m5_chron[g_idx + 1: g_idx + 1 + LA_5M]):
                # adverse move
                if bullish:
                    adverse = max(0.0, entry - fc["low"])
                else:
                    adverse = max(0.0, fc["high"] - entry)
                mae_r = max(mae_r, adverse / risk)

                if bullish:
                    if fc["high"] >= tp:  outcome = "WIN";  candles_to = j + 1; break
                    if fc["low"]  <= sl:  outcome = "LOSS"; candles_to = j + 1; break
                else:
                    if fc["low"]  <= tp:  outcome = "WIN";  candles_to = j + 1; break
                    if fc["high"] >= sl:  outcome = "LOSS"; candles_to = j + 1; break

            if outcome == "OPEN":
                continue

            win = outcome == "WIN"

            # ── Did 15m BOS the same level? ────────────────────────────
            # Find 15m candles closing past broken_level in [h1_open, h1_open+7200)
            # (within 2h of the 1h break — allowing one 15m period after 1h close)
            lo15 = bisect.bisect_left(m15_ts, h1_open)
            hi15 = bisect.bisect_left(m15_ts, h1_end + 3600)
            period_15m = m15_chron[lo15:hi15]

            m15_bos = False
            for c15 in period_15m:
                if bullish and c15["close"] > broken_level:
                    m15_bos = True; break
                if not bullish and c15["close"] < broken_level:
                    m15_bos = True; break

            # ── Retrace depth bucket ────────────────────────────────────
            if mae_r < 0.25:
                retrace = "direct (<0.25R)"
            elif mae_r < 0.50:
                retrace = "shallow (0.25-0.5R)"
            elif mae_r < 0.75:
                retrace = "deep (0.5-0.75R)"
            else:
                retrace = "very_deep (0.75-1R)"

            # ── 5m candle right after break — is it counter? ───────────
            c_post_counter = False
            if g_idx + 1 < len(m5_chron):
                cnext = m5_chron[g_idx + 1]
                c_post_counter = (cnext["close"] < cnext["open"]) == bullish

            signals.append({
                "win":            win,
                "mae_r":          mae_r,
                "candles_to":     candles_to,
                "retrace":        retrace,
                "m15_bos":        m15_bos,
                "c_post_counter": c_post_counter,
                "candle_num":     break_idx + 1,
                "risk_atr":       risk / atr_5m,
            })

    return signals


def print_split(label, rows, key_fn, base_wr, min_n=8):
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
        print(f"    {str(k):32s}  WR={wr_s(wins, len(items), base_wr)}")


def main():
    db       = LocalDB(DB_PATH)
    analyzer = SMCAnalyzer()

    for symbol in PAIRS:
        print(f"\n{'='*64}")
        print(f"  {symbol}")
        print(f"{'='*64}")

        sigs = analyze_pair(symbol, db, analyzer)
        if not sigs:
            print("  No data"); continue

        n    = len(sigs)
        wins = sum(1 for s in sigs if s["win"])
        bwr  = wins / n
        print(f"\n  Resolved: {n}    baseline WR={bwr*100:.1f}%")

        if n < 20:
            print("  Too few"); continue

        # ── 1. Did the 1h BOS also create a 15m BOS? ───────────────────
        has_m15 = [s for s in sigs if s["m15_bos"]]
        no_m15  = [s for s in sigs if not s["m15_bos"]]
        w15  = sum(1 for s in has_m15 if s["win"])
        wno  = sum(1 for s in no_m15  if s["win"])
        print(f"\n  15m also BOS same level?")
        print(f"    YES  {wr_s(w15, len(has_m15), bwr)}")
        print(f"    NO   {wr_s(wno, len(no_m15),  bwr)}")

        # ── 2. Retrace depth after 5m entry ────────────────────────────
        print_split("Retrace depth before resolution",
                    sigs, lambda s: s["retrace"], bwr)

        # ── 3. Retrace depth — WIN only (how deep before TP?) ──────────
        win_sigs = [s for s in sigs if s["win"]]
        bwr_win  = 1.0   # all WIN, so baseline = 1 (irrelevant)
        if win_sigs:
            g = defaultdict(int)
            for s in win_sigs:
                g[s["retrace"]] += 1
            total_w = len(win_sigs)
            print(f"\n  Retrace depth for WIN trades only  (n={total_w}):")
            for k in sorted(g.keys()):
                print(f"    {k:32s}  {g[k]:3d}  ({g[k]/total_w*100:.1f}%)")

        # ── 4. 15m BOS × retrace depth ─────────────────────────────────
        print(f"\n  15m BOS × retrace depth:")
        combos = defaultdict(list)
        for s in sigs:
            key = f"{'15m✓' if s['m15_bos'] else '15m✗'} | {s['retrace']}"
            combos[key].append(s)
        for k in sorted(combos.keys()):
            items = combos[k]
            if len(items) < 8:
                continue
            w = sum(1 for x in items if x["win"])
            print(f"    {k:50s}  WR={wr_s(w, len(items), bwr)}")

        # ── 5. C_post counter × retrace depth ─────────────────────────
        print_split("C_post counter × retrace bucket",
                    [s for s in sigs if s["c_post_counter"]],
                    lambda s: s["retrace"], bwr)

        # ── 6. Time to resolution ───────────────────────────────────────
        def time_bucket(s):
            c = s["candles_to"]
            if c <= 3:   return "≤15m (c1-3)"
            if c <= 12:  return "15m-1h (c4-12)"
            if c <= 48:  return "1h-4h (c13-48)"
            return               ">4h (c49+)"

        print_split("Time to resolution", sigs, time_bucket, bwr)

        # WIN-only time distribution
        if win_sigs:
            g2 = defaultdict(int)
            for s in win_sigs:
                g2[time_bucket(s)] += 1
            print(f"\n  Time to TP (WIN only, n={len(win_sigs)}):")
            for k in sorted(g2.keys()):
                print(f"    {k:28s}  {g2[k]:3d}  ({g2[k]/len(win_sigs)*100:.1f}%)")

        # ── 7. Key question: retrace-then-win vs direct-win ───────────
        # "Did price come back into the broken level region (>0.5R) but still WIN?"
        retrace_win  = sum(1 for s in sigs if s["win"] and s["mae_r"] >= 0.5)
        direct_win   = sum(1 for s in sigs if s["win"] and s["mae_r"] <  0.5)
        retrace_tot  = sum(1 for s in sigs if s["mae_r"] >= 0.5)
        direct_tot   = sum(1 for s in sigs if s["mae_r"] <  0.5)
        print(f"\n  Direct (MAE<0.5R)   WR={wr_s(direct_win,  direct_tot,  bwr)}")
        print(f"  Retrace (MAE≥0.5R)  WR={wr_s(retrace_win, retrace_tot, bwr)}")

        # ── 8. MAE distribution for C_post counter trades that WIN ─────
        cpc_wins = [s for s in sigs if s["c_post_counter"] and s["win"]]
        cpc_loss = [s for s in sigs if s["c_post_counter"] and not s["win"]]
        if cpc_wins:
            avg_mae_w = sum(s["mae_r"] for s in cpc_wins) / len(cpc_wins)
            avg_mae_l = sum(s["mae_r"] for s in cpc_loss) / len(cpc_loss) if cpc_loss else 0
            print(f"\n  C_post counter trades:")
            print(f"    WIN  n={len(cpc_wins):3d}  avg MAE={avg_mae_w:.2f}R")
            print(f"    LOSS n={len(cpc_loss):3d}  avg MAE={avg_mae_l:.2f}R (then SL)")

    db.close()


if __name__ == "__main__":
    main()
