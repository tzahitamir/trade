#!/usr/bin/env python3
"""
Full-data 1h BOS retrace analysis with combo gate.

For each pair / XAUUSD, uses ALL available 1h + 5m data.
Reports:
  - baseline WR (all very-deep retrace signals, MAE 0.75-1R)
  - mini-BOS C1 only (fire at C1 close)
  - combo gate: mini-BOS C1 + ≥2/3 of C1–C3 aligned (fire at C3 close)
  - per-pair 4h alignment filter applied

Data coverage is reported for each pair.
Run from repo root: python local_dev/analyze_1h_combo_gate.py
"""
import sys
import bisect
import math
import statistics
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from db.local_db import LocalDB
from analysis.smc_analyzer import SMCAnalyzer

DB_PATH = str(Path(__file__).resolve().parents[1] / "src" / "data" / "trade.db")
PAIRS   = ["NZDUSD", "EURUSD", "EURJPY", "USDCHF", "XAUUSD"]
LA_5M   = 120   # 10h lookahead for 1h BOS resolution

# Per-pair 4h gate (from analyze_retrace_4h_alignment.py backtest)
#   EURUSD / XAUUSD : both prev + curr 4h aligned
#   NZDUSD / USDCHF : curr 4h aligned
def h4_pass(symbol, prev_aln, curr_aln):
    if symbol in ("EURUSD", "XAUUSD"): return prev_aln and curr_aln
    if symbol in ("NZDUSD", "USDCHF"): return curr_aln
    return False   # EURJPY excluded

def z_test(wins, n, base):
    if n < 8 or base in (0.0, 1.0): return 0.0, ""
    se = math.sqrt(base * (1 - base) / n)
    z  = (wins / n - base) / se if se else 0
    st = "★★★" if abs(z) >= 2.576 else "★★" if abs(z) >= 1.645 else "★" if abs(z) >= 0.842 else ""
    return z, st

def wr_s(wins, n, base=None):
    if n < 8: return f"n={n} (thin)"
    pct = wins / n * 100
    s = f"{pct:.1f}%  n={n}"
    if base is not None:
        z, st = z_test(wins, n, base)
        diff  = pct - base * 100
        sign  = "+" if diff >= 0 else ""
        s += f"  ({sign}{diff:.1f}pp  z={z:+.2f}{st})"
    return s

def median(lst):
    if not lst: return float("nan")
    s = sorted(lst); n = len(s)
    return s[n//2] if n % 2 else (s[n//2-1]+s[n//2])/2

def ev(wins, sigs):
    if not sigs: return float("nan")
    wr = wins / len(sigs)
    avg_rr = statistics.mean(s["rr"] for s in sigs)
    return wr * avg_rr - (1 - wr)


def analyze_pair(symbol, db, analyzer):
    h1_desc = db.query_recent(symbol, "1h",  limit=12000)
    m5_desc = db.query_recent(symbol, "5m",  limit=130000)
    h4_desc = db.query_recent(symbol, "4h",  limit=2000)

    if not h1_desc or not m5_desc:
        return [], None, None

    m5_chron = list(reversed(m5_desc))
    h1_n     = len(h1_desc)
    m5_ts    = [c["timestamp"] for c in m5_chron]
    m5_map   = {c["timestamp"]: i for i, c in enumerate(m5_chron)}
    h4_chron = list(reversed(h4_desc)) if h4_desc else []
    h4_ts    = [c["timestamp"] for c in h4_chron]

    min_5m_ts = m5_chron[0]["timestamp"]
    max_5m_ts = m5_chron[-1]["timestamp"]

    data_start = datetime.fromtimestamp(min_5m_ts, tz=timezone.utc)
    data_end   = datetime.fromtimestamp(max_5m_ts, tz=timezone.utc)

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

        for ev_bos in bos_events:
            direction    = ev_bos["direction"]
            broken_level = ev_bos["broken_level"]
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
                if bullish and c["close"] > broken_level:     break_idx = i; break
                if not bullish and c["close"] < broken_level: break_idx = i; break
            if break_idx is None:
                continue

            break_c    = period[break_idx]
            orig_entry = break_c["close"]
            orig_risk  = abs(orig_entry - broken_level)
            if orig_risk < atr_5m * 0.05:
                continue

            orig_tp = (orig_entry + 2 * orig_risk) if bullish else (orig_entry - 2 * orig_risk)
            g_idx   = m5_map.get(break_c["timestamp"])
            if g_idx is None:
                continue

            # Walk forward: track MAE, bottom, resolution
            outcome     = "OPEN"
            mae_r       = 0.0
            max_adv_idx = g_idx

            for j, fc in enumerate(m5_chron[g_idx + 1: g_idx + 1 + LA_5M]):
                adverse = (max(0.0, orig_entry - fc["low"]) if bullish
                           else max(0.0, fc["high"] - orig_entry))
                if adverse / orig_risk > mae_r:
                    mae_r       = adverse / orig_risk
                    max_adv_idx = g_idx + 1 + j

                if bullish:
                    if fc["high"] >= orig_tp:      outcome = "WIN";  break
                    if fc["low"]  <= broken_level: outcome = "LOSS"; break
                else:
                    if fc["low"]  <= orig_tp:      outcome = "WIN";  break
                    if fc["high"] >= broken_level: outcome = "LOSS"; break

            if outcome == "OPEN" or mae_r < 0.75:
                continue

            win        = outcome == "WIN"
            bottom_c   = m5_chron[max_adv_idx]

            # C1, C2, C3 after bottom
            c1_idx = max_adv_idx + 1
            c2_idx = max_adv_idx + 2
            c3_idx = max_adv_idx + 3
            if c3_idx >= len(m5_chron):
                continue

            c1 = m5_chron[c1_idx]
            c2 = m5_chron[c2_idx]
            c3 = m5_chron[c3_idx]

            # mini-BOS C1: closes past bottom extreme
            mini_bos_c1 = ((c1["close"] > bottom_c["high"]) if bullish
                           else (c1["close"] < bottom_c["low"]))

            # Alignment of C1, C2, C3
            aln = [(c["close"] > c["open"]) == bullish for c in [c1, c2, c3]]
            n_aligned_3 = sum(aln)

            # combo gate: mini-BOS C1 + ≥2/3 of C1–C3 aligned
            combo = mini_bos_c1 and n_aligned_3 >= 2

            # Entry at C1 close (mini-BOS C1 strategy)
            new_entry_c1 = c1["close"]
            risk_c1      = abs(new_entry_c1 - broken_level)
            tp_dist_c1   = abs(orig_tp - new_entry_c1)
            rr_c1        = tp_dist_c1 / risk_c1 if risk_c1 > 1e-8 else 0

            # Entry at C3 close (combo gate strategy)
            new_entry_c3 = c3["close"]
            risk_c3      = abs(new_entry_c3 - broken_level)
            tp_dist_c3   = abs(orig_tp - new_entry_c3)
            rr_c3        = tp_dist_c3 / risk_c3 if risk_c3 > 1e-8 else 0

            # 4h alignment at the time of C1 (mini-BOS fire time)
            h4_pos = bisect.bisect_left(h4_ts, c1["timestamp"])
            prev_h4_aln = curr_h4_aln = None
            if h4_pos >= 2:
                prev_h4 = h4_chron[h4_pos - 2]
                curr_h4 = h4_chron[h4_pos - 1]
                prev_h4_aln = ((prev_h4["close"] > prev_h4["open"]) if bullish
                               else (prev_h4["close"] < prev_h4["open"]))
                curr_h4_aln = ((curr_h4["close"] > curr_h4["open"]) if bullish
                               else (curr_h4["close"] < curr_h4["open"]))

            h4_aligned = (h4_pass(symbol, prev_h4_aln, curr_h4_aln)
                          if prev_h4_aln is not None else None)

            signals.append({
                "win":          win,
                "mini_bos_c1":  mini_bos_c1,
                "combo":        combo,
                "h4_aligned":   h4_aligned,
                "rr_c1":        rr_c1,
                "rr_c3":        rr_c3,
                "risk_c1_atr":  risk_c1 / atr_5m,
                "risk_c3_atr":  risk_c3 / atr_5m,
                "orig_risk_atr": orig_risk / atr_5m,
                "mae_r":        mae_r,
            })

    return signals, data_start, data_end


def report(label, sigs, base_wr, entry="c1"):
    n    = len(sigs)
    wins = sum(1 for s in sigs if s["win"])
    if n == 0:
        print(f"    {label}: n=0"); return

    rr_key = "rr_c1" if entry == "c1" else "rr_c3"
    rr_all = [s[rr_key] for s in sigs if s[rr_key] > 0]
    wr     = wins / n
    avg_rr = statistics.mean(rr_all) if rr_all else 0
    ev_val = wr * avg_rr - (1 - wr)
    med_rr = median(rr_all) if rr_all else 0
    z, st  = z_test(wins, n, base_wr)
    diff   = wr * 100 - base_wr * 100
    sign   = "+" if diff >= 0 else ""

    risk_key = "risk_c1_atr" if entry == "c1" else "risk_c3_atr"
    risk_med = median([s[risk_key] for s in sigs])
    orig_med = median([s["orig_risk_atr"] for s in sigs])
    pct_pass = n / max(1, sum(1 for _ in sigs)) * 100  # always 100% here — shown at call site

    print(f"    {label}")
    print(f"      WR={wr*100:.1f}%  n={n}  ({sign}{diff:.1f}pp  z={z:+.2f}{st})")
    print(f"      R:R  med={med_rr:.2f}  avg={avg_rr:.2f} | EV={ev_val:+.2f}R")
    print(f"      Risk  entry={risk_med:.2f}×ATR  orig={orig_med:.2f}×ATR  ({risk_med/orig_med*100:.0f}% of orig)")


def main():
    db       = LocalDB(DB_PATH)
    analyzer = SMCAnalyzer()

    print("1h BOS retrace entry — full available data, combo gate analysis")
    print("Very-deep retrace only (MAE 0.75-1R) | SL=broken_level TP=orig_1h_TP\n")

    for symbol in PAIRS:
        print(f"\n{'='*68}")
        sigs, d_start, d_end = analyze_pair(symbol, db, analyzer)

        span = ""
        if d_start and d_end:
            days = (d_end - d_start).days
            span = (f"  data: {d_start.strftime('%Y-%m-%d')} → {d_end.strftime('%Y-%m-%d')}"
                    f"  ({days}d)")
        print(f"  {symbol}{span}")
        print(f"{'='*68}")

        if not sigs:
            print("  No signals"); continue

        n    = len(sigs)
        wins = sum(1 for s in sigs if s["win"])
        bwr  = wins / n
        print(f"\n  All very-deep retrace signals: n={n}  WR={bwr*100:.1f}%  (baseline)")

        # ── mini-BOS C1 (no 4h filter, entry at C1) ──────────────────────────
        mb  = [s for s in sigs if s["mini_bos_c1"]]
        nmb = [s for s in sigs if not s["mini_bos_c1"]]
        w_mb  = sum(1 for s in mb  if s["win"])
        w_nmb = sum(1 for s in nmb if s["win"])
        pct_mb = len(mb) / n * 100

        print(f"\n  ── Gate 1: mini-BOS C1 (entry at C1 close) [{pct_mb:.0f}% of signals] ──")
        report(f"mini-BOS C1 fired  (n={len(mb)})", mb, bwr, entry="c1")
        if len(nmb) >= 8:
            report(f"no mini-BOS C1     (n={len(nmb)})", nmb, bwr, entry="c1")

        # ── combo gate (mini-BOS C1 + ≥2/3 aligned, entry at C3) ────────────
        combo     = [s for s in sigs if s["combo"]]
        no_combo  = [s for s in sigs if s["mini_bos_c1"] and not s["combo"]]
        w_cb = sum(1 for s in combo    if s["win"])
        w_nc = sum(1 for s in no_combo if s["win"])
        pct_combo = len(combo) / n * 100

        print(f"\n  ── Gate 2: combo (mini-BOS C1 + ≥2/3 aligned, entry at C3) [{pct_combo:.0f}% of signals] ──")
        report(f"combo PASS (n={len(combo)})",    combo,    bwr, entry="c3")
        if len(no_combo) >= 8:
            report(f"combo FAIL (n={len(no_combo)})", no_combo, bwr, entry="c3")

        # ── 4h alignment filter on top of each gate ───────────────────────────
        if symbol != "EURJPY":
            mb_h4 = [s for s in mb    if s["h4_aligned"]]
            cb_h4 = [s for s in combo if s["h4_aligned"]]
            pct_mb_h4 = len(mb_h4) / n * 100
            pct_cb_h4 = len(cb_h4) / n * 100

            print(f"\n  ── Gate 1 + 4h filter [{pct_mb_h4:.0f}% of signals] ──")
            report(f"mini-BOS C1 + 4h aligned (n={len(mb_h4)})", mb_h4, bwr, entry="c1")

            print(f"\n  ── Gate 2 + 4h filter (tightest) [{pct_cb_h4:.0f}% of signals] ──")
            report(f"combo + 4h aligned (n={len(cb_h4)})", cb_h4, bwr, entry="c3")

            # Negative: 4h counter
            mb_h4_no = [s for s in mb    if s["h4_aligned"] is False]
            if len(mb_h4_no) >= 8:
                w_no = sum(1 for s in mb_h4_no if s["win"])
                print(f"\n  ── 4h COUNTER (avoid) ──")
                report(f"mini-BOS C1 + 4h COUNTER (n={len(mb_h4_no)})", mb_h4_no, bwr, entry="c1")
        else:
            print(f"\n  (EURJPY excluded from live alerts — negative EV at 1h)")

    db.close()


if __name__ == "__main__":
    main()
