#!/usr/bin/env python3
"""XAUUSD 1h retrace entry — MFE overshoot + time-of-day analysis (full signal set)."""
import sys, bisect, math, statistics
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from db.local_db import LocalDB
from analysis.smc_analyzer import SMCAnalyzer
from datetime import datetime, timezone

DB_PATH = str(Path(__file__).resolve().parents[1] / "src" / "data" / "trade.db")
LA_5M   = 200   # extended to see full run beyond TP

def median(lst):
    if not lst: return float("nan")
    s = sorted(lst); n = len(s)
    return s[n//2] if n % 2 else (s[n//2-1]+s[n//2])/2

def run():
    db = LocalDB(DB_PATH)
    analyzer = SMCAnalyzer()
    h1_desc = db.query_recent("XAUUSD", "1h",  limit=12000)
    m5_desc = db.query_recent("XAUUSD", "5m",  limit=130000)
    m5_chron  = list(reversed(m5_desc))
    h1_n      = len(h1_desc)
    m5_ts     = [c["timestamp"] for c in m5_chron]
    m5_map    = {c["timestamp"]: i for i, c in enumerate(m5_chron)}
    min_5m_ts = m5_chron[0]["timestamp"]
    max_5m_ts = m5_chron[-1]["timestamp"]
    seen = set(); records = []

    for k in range(30, h1_n - 5):
        window  = h1_desc[h1_n - 1 - k:]
        h1_open = window[0]["timestamp"]
        h1_end  = h1_open + 3600
        if h1_open < min_5m_ts or h1_end > max_5m_ts: continue
        bos_events = analyzer.detect_bos(window, params={"symbol":"XAUUSD","timeframe":"1h","min_break_strength":0.0,"require_liquidity_sweep":False})
        if not bos_events: continue
        if not analyzer.calculate_atr(window): continue

        for ev in bos_events:
            direction = ev["direction"]; broken_level = ev["broken_level"]
            bullish   = direction == "bullish"
            sig_key   = (direction, round(broken_level, 1), h1_open)
            if sig_key in seen: continue
            seen.add(sig_key)

            lo = bisect.bisect_left(m5_ts, h1_open)
            hi = bisect.bisect_left(m5_ts, h1_end)
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
            if orig_risk < atr_5m * 0.05: continue  # same as original analysis
            orig_tp = (orig_entry + 2*orig_risk) if bullish else (orig_entry - 2*orig_risk)
            g_idx   = m5_map.get(break_c["timestamp"])
            if g_idx is None: continue

            # MAE walk — same 120-candle window as original
            outcome = "OPEN"; mae_r = 0.0; max_adv_idx = g_idx
            for j, fc in enumerate(m5_chron[g_idx+1: g_idx+1+120]):
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
            if outcome == "OPEN" or mae_r < 0.75: continue  # no upper limit on mae

            c1_idx = max_adv_idx + 1
            if c1_idx >= len(m5_chron): continue
            c1       = m5_chron[c1_idx]
            bottom_c = m5_chron[max_adv_idx]
            mini_bos = (c1["close"] > bottom_c["high"]) if bullish else (c1["close"] < bottom_c["low"])
            if not mini_bos: continue
            # no 4h gate for XAUUSD

            new_entry = c1["close"]
            risk2     = abs(new_entry - broken_level)
            if risk2 < 1e-6: continue

            rr_to_tp = abs(orig_tp - new_entry) / risk2

            # Forward check from C1 (extended to LA_5M to capture full run)
            c1_out = "OPEN"; mfe_r = 0.0
            for fc in m5_chron[c1_idx+1: c1_idx+1+LA_5M]:
                fav = (max(0.0, fc["high"]-new_entry) if bullish
                       else max(0.0, new_entry-fc["low"]))
                if fav/risk2 > mfe_r: mfe_r = fav/risk2
                if c1_out == "OPEN":
                    if bullish:
                        if fc["high"] >= orig_tp:        c1_out="WIN"
                        elif fc["low"] <= broken_level:  c1_out="LOSS"
                    else:
                        if fc["low"]  <= orig_tp:        c1_out="WIN"
                        elif fc["high"] >= broken_level: c1_out="LOSS"
            if c1_out == "OPEN": continue

            c1_hour = datetime.fromtimestamp(c1["timestamp"], tz=timezone.utc).hour
            records.append({
                "win":       c1_out=="WIN",
                "mfe_r":     mfe_r,
                "rr_tp":     rr_to_tp,
                "mae_r":     mae_r,
                "hour":      c1_hour,
                "direction": direction,
            })
    db.close()

    n     = len(records)
    wins  = [r for r in records if r["win"]]
    losses= [r for r in records if not r["win"]]
    wr    = len(wins)/n
    print(f"XAUUSD mini-BOS C1 (no 4h gate)  n={n}  WR={wr*100:.1f}%")

    # ── 1. Does price run BEYOND the TP? ─────────────────────────────────
    print("\n── MFE on WINNING trades (R from entry) ─────────────────────────────")
    mfe_w = [r["mfe_r"] for r in wins]
    rr_w  = [r["rr_tp"] for r in wins]
    print(f"  MFE median={median(mfe_w):.2f}R  avg={statistics.mean(mfe_w):.2f}R  "
          f"min={min(mfe_w):.2f}R  max={max(mfe_w):.2f}R")
    print(f"  R:R to TP  median={median(rr_w):.2f}  avg={statistics.mean(rr_w):.2f}")

    beyond = [mfe/rr for mfe, rr in zip(mfe_w, rr_w) if rr > 0.1]
    print(f"\n  MFE / TP_dist  (1.0 = exactly at TP, 2.0 = ran twice as far):")
    b_med = median(beyond)
    for lo, hi, lbl in [(0,1.3,"barely reached TP"),(1.3,2.0,"ran 30-100% past TP"),
                         (2.0,3.5,"ran 2-3.5× past TP"),(3.5,99,"strong extended run")]:
        cnt = sum(1 for x in beyond if lo <= x < hi)
        print(f"    {lo:.1f}–{hi if hi<99 else '∞':>4}×  {cnt:>3} ({cnt/len(beyond)*100:4.0f}%)  ← {lbl}")
    print(f"  Median overshoot: {b_med:.2f}× TP dist")
    print(f"  Interpretation: {'price typically runs well PAST TP → liquidity pool further out' if b_med > 1.5 else 'price mostly just reaches TP, not a big extended run'}")

    if losses:
        mfe_l = [r["mfe_r"] for r in losses]
        print(f"\n  LOSS trades  MFE: median={median(mfe_l):.2f}R  avg={statistics.mean(mfe_l):.2f}R  n={len(losses)}")

    # ── 2. Time of day ────────────────────────────────────────────────────
    print("\n── WR by hour of day (UTC) ──────────────────────────────────────────")
    print(f"  {'Hour':>5}  {'n':>3}  {'WR':>6}  {'MFE_med':>8}  note")
    for h in range(24):
        grp = [r for r in records if r["hour"]==h]
        if not grp: continue
        w   = sum(1 for r in grp if r["win"])
        mfe = median([r["mfe_r"] for r in grp if r["win"]] or [0])
        flag = "  ← LOW" if (len(grp)>=3 and w/len(grp)<0.55) else ""
        print(f"  {h:02d}:xx  {len(grp):>3}  {w/len(grp)*100:5.1f}%  {mfe:7.2f}R{flag}")

    print("\n── Session WR ───────────────────────────────────────────────────────")
    for name, hrs in [("Tokyo / Asia  (00–07)",range(0,7)),
                       ("London open  (07–12)", range(7,12)),
                       ("LN/NY overlap(12–16)", range(12,16)),
                       ("Late NY      (16–21)", range(16,21)),
                       ("NY close     (21–24)", range(21,24))]:
        grp = [r for r in records if r["hour"] in hrs]
        if not grp: continue
        w   = sum(1 for r in grp if r["win"])
        mfe = median([r["mfe_r"] for r in grp if r["win"]] or [0])
        print(f"  {name:25s}  n={len(grp):>3}  WR={w/len(grp)*100:5.1f}%  MFE_med={mfe:.2f}R")

    print("\n── Direction ────────────────────────────────────────────────────────")
    for d in ["bullish","bearish"]:
        grp=[r for r in records if r["direction"]==d]
        if not grp: continue
        w=sum(1 for r in grp if r["win"])
        mfe=median([r["mfe_r"] for r in grp if r["win"]] or [0])
        print(f"  {d:8s}  n={len(grp):>3}  WR={w/len(grp)*100:.1f}%  MFE_med={mfe:.2f}R")

if __name__ == "__main__":
    run()
