#!/usr/bin/env python3
"""Find a clean, visually clear 1h retrace WIN example and plot it."""
import sys, bisect
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from db.local_db import LocalDB
from analysis.smc_analyzer import SMCAnalyzer
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyArrow
from datetime import datetime, timezone

DB_PATH = str(Path(__file__).resolve().parents[1] / "src" / "data" / "trade.db")
LA_5M   = 120

def find_examples(symbol, db, analyzer, n=30):
    h1_desc = db.query_recent(symbol, "1h",  limit=12000)
    m5_desc = db.query_recent(symbol, "5m",  limit=130000)
    h4_desc = db.query_recent(symbol, "4h",  limit=2000)
    if not h1_desc or not m5_desc or not h4_desc: return []

    m5_chron  = list(reversed(m5_desc))
    h4_chron  = list(reversed(h4_desc))
    h1_n      = len(h1_desc)
    m5_ts     = [c["timestamp"] for c in m5_chron]
    h4_ts     = [c["timestamp"] for c in h4_chron]
    m5_map    = {c["timestamp"]: i for i, c in enumerate(m5_chron)}
    min_5m_ts = m5_chron[0]["timestamp"]
    max_5m_ts = m5_chron[-1]["timestamp"]
    seen = set(); results = []

    for k in range(30, h1_n - 5):
        window  = h1_desc[h1_n - 1 - k:]
        h1_open = window[0]["timestamp"]
        h1_end  = h1_open + 3600
        if h1_open < min_5m_ts or h1_end > max_5m_ts: continue

        bos_events = analyzer.detect_bos(window, params={"symbol":symbol,"timeframe":"1h","min_break_strength":0.0,"require_liquidity_sweep":False})
        if not bos_events: continue
        atr_1h = analyzer.calculate_atr(window)
        if not atr_1h: continue

        for ev in bos_events:
            direction = ev["direction"]; broken_level = ev["broken_level"]; bullish = direction=="bullish"
            sig_key = (direction, round(broken_level,5), h1_open)
            if sig_key in seen: continue
            seen.add(sig_key)

            lo = bisect.bisect_left(m5_ts, h1_open); hi = bisect.bisect_left(m5_ts, h1_end)
            period = m5_chron[lo:hi]
            if len(period)<2 or lo<20: continue
            atr_5m = analyzer.calculate_atr(m5_chron[lo-20:lo])
            if not atr_5m: continue

            break_idx = None
            for i,c in enumerate(period):
                if bullish and c["close"]>broken_level: break_idx=i; break
                if not bullish and c["close"]<broken_level: break_idx=i; break
            if break_idx is None: continue

            break_c   = period[break_idx]; orig_entry = break_c["close"]
            orig_risk = abs(orig_entry - broken_level)
            # Require a meaningful initial break
            if orig_risk < atr_5m * 0.3: continue
            orig_tp = (orig_entry + 2*orig_risk) if bullish else (orig_entry - 2*orig_risk)
            g_idx = m5_map.get(break_c["timestamp"])
            if g_idx is None: continue

            outcome="OPEN"; mae_r=0.0; max_adv_idx=g_idx
            for j, fc in enumerate(m5_chron[g_idx+1: g_idx+1+LA_5M]):
                adverse=(max(0.0,orig_entry-fc["low"]) if bullish else max(0.0,fc["high"]-orig_entry))
                if adverse/orig_risk>mae_r: mae_r=adverse/orig_risk; max_adv_idx=g_idx+1+j
                if bullish:
                    if fc["high"]>=orig_tp: outcome="WIN"; break
                    if fc["low"]<=broken_level: outcome="LOSS"; break
                else:
                    if fc["low"]<=orig_tp: outcome="WIN"; break
                    if fc["high"]>=broken_level: outcome="LOSS"; break
            if outcome!="WIN" or mae_r<0.75 or mae_r>0.97: continue

            c1_idx=max_adv_idx+1
            if c1_idx>=len(m5_chron): continue
            c1=m5_chron[c1_idx]; bottom_c=m5_chron[max_adv_idx]
            mini_bos=(c1["close"]>bottom_c["high"]) if bullish else (c1["close"]<bottom_c["low"])
            if not mini_bos: continue

            c1_ts=c1["timestamp"]
            h4_pos=bisect.bisect_left(h4_ts,c1_ts)
            if h4_pos<2: continue
            prev_h4=h4_chron[h4_pos-2]; curr_h4=h4_chron[h4_pos-1]
            prev_aln=(prev_h4["close"]>prev_h4["open"]) if bullish else (prev_h4["close"]<prev_h4["open"])
            curr_aln=(curr_h4["close"]>curr_h4["open"]) if bullish else (curr_h4["close"]<curr_h4["open"])
            if symbol=="EURUSD":
                if not (prev_aln and curr_aln): continue
            elif symbol in ("NZDUSD","USDCHF"):
                if not curr_aln: continue

            new_entry=c1["close"]
            tight_sl = (max(bottom_c["low"], broken_level) if bullish
                        else min(bottom_c["high"], broken_level))
            orig_sl  = broken_level
            tp_dist  = abs(orig_tp - new_entry)
            orig_risk2  = abs(new_entry - orig_sl)
            tight_risk2 = abs(new_entry - tight_sl)
            if orig_risk2 < atr_5m*0.1 or tight_risk2 < 1e-8: continue

            results.append({
                "symbol": symbol, "direction": direction, "bullish": bullish,
                "broken_level": broken_level,
                "orig_entry": orig_entry, "orig_tp": orig_tp,
                "new_entry": new_entry, "orig_sl": orig_sl, "tight_sl": tight_sl,
                "orig_rr": tp_dist/orig_risk2, "tight_rr": tp_dist/tight_risk2,
                "mae_r": mae_r, "outcome": outcome,
                "g_idx": g_idx, "c1_idx": c1_idx, "max_adv_idx": max_adv_idx,
                "m5_chron": m5_chron, "atr_5m": atr_5m,
                "orig_risk": orig_risk,
                # score: prefer larger spread, clear retrace
                "score": orig_risk / atr_5m * min(orig_risk2/atr_5m, 1.0),
            })
        if len(results) >= n: break

    results.sort(key=lambda x: -x["score"])
    return results


def plot_example(sig, out_path):
    m5    = sig["m5_chron"]
    bull  = sig["bullish"]
    g_idx = sig["g_idx"]; c1_idx = sig["c1_idx"]; max_adv_idx = sig["max_adv_idx"]
    broken = sig["broken_level"]; orig_tp = sig["orig_tp"]
    orig_sl = sig["orig_sl"]; tight_sl = sig["tight_sl"]; new_entry = sig["new_entry"]
    orig_entry = sig["orig_entry"]

    # Window: 20 before break, enough after C1 to show the run to TP
    tp_idx = c1_idx
    for i, c in enumerate(m5[c1_idx+1: c1_idx+1+LA_5M]):
        hit = (c["high"] >= orig_tp) if bull else (c["low"] <= orig_tp)
        if hit: tp_idx = c1_idx + 1 + i; break

    start = max(0, g_idx - 20)
    end   = min(len(m5), tp_idx + 8)
    candles = m5[start:end]
    xs = list(range(len(candles)))
    g_x   = g_idx   - start
    c1_x  = c1_idx  - start
    bot_x = max_adv_idx - start
    tp_x  = tp_idx  - start

    BG   = "#0d1117"; GRID = "#1e2530"
    BULL = "#26a69a"; BEAR = "#ef5350"

    fig, ax = plt.subplots(figsize=(16, 8))
    fig.patch.set_facecolor(BG); ax.set_facecolor(BG)
    ax.grid(axis="y", color=GRID, lw=0.5, zorder=0)

    # Candlesticks
    for i, c in enumerate(candles):
        col = BULL if c["close"] >= c["open"] else BEAR
        bl  = min(c["open"], c["close"]); bh = max(c["open"], c["close"])
        ax.add_patch(Rectangle((i-0.38, bl), 0.76, max(bh-bl, 1e-8),
                                color=col, zorder=3))
        ax.plot([i,i],[c["low"],bl], color=col, lw=0.9, zorder=2)
        ax.plot([i,i],[bh,c["high"]], color=col, lw=0.9, zorder=2)

    y_vals = [c["high"] for c in candles] + [c["low"] for c in candles]
    y_min  = min(y_vals); y_max = max(y_vals); rng = y_max - y_min

    # Phase background shading
    ax.axvspan(g_x,   bot_x, alpha=0.07, color="#f39c12", zorder=1, label="_retrace zone")
    ax.axvspan(bot_x, c1_x,  alpha=0.07, color="#e74c3c", zorder=1)
    ax.axvspan(c1_x,  tp_x,  alpha=0.06, color="#27ae60", zorder=1)

    # Key levels
    def hline(y, color, ls, lw, label, x0=0, x1=None):
        x1 = x1 or len(candles)-1
        ax.plot([x0,x1],[y,y], color=color, ls=ls, lw=lw, zorder=4, label=label)

    # Broken 1h level = original SL
    hline(broken,    "#ff9800", "--", 1.6, f"1h BOS level = orig SL  ({broken:.5f})")
    # TP
    hline(orig_tp,   "#4caf50", "--", 1.6, f"TP  ({orig_tp:.5f})")
    # Entry
    hline(new_entry, "#29b6f6", ":",  1.3, f"Entry C1 close  ({new_entry:.5f})", x0=c1_x, x1=tp_x+2)
    # Tight SL (if different from broken level)
    if abs(tight_sl - broken) > sig["atr_5m"]*0.02:
        hline(tight_sl, "#f06292", "--", 1.6, f"Tight SL bottom candle  ({tight_sl:.5f})")

    # Orig entry reference
    hline(orig_entry, "#ff9800", ":", 0.8, f"orig 5m entry  ({orig_entry:.5f})", x0=g_x, x1=g_x+12)

    # Vertical phase markers
    for x, col in [(g_x,"#ff9800"), (bot_x,"#f06292"), (c1_x,"#29b6f6")]:
        ax.axvline(x, color=col, lw=1.2, ls=":", alpha=0.8, zorder=4)

    # Annotations
    kw = dict(fontsize=9, fontweight="bold", ha="center",
              arrowprops=dict(arrowstyle="-|>", lw=1.0, mutation_scale=10))

    off = rng * 0.12
    # Break candle
    bc = candles[g_x]
    ay = bc["high"] if bull else bc["low"]
    ax.annotate("5m BOS break\n(orig entry)", xy=(g_x, ay),
                xytext=(g_x, ay + off*(1 if not bull else -1)),
                color="#ff9800", **kw)

    # Retrace bottom
    btc = candles[bot_x]
    ay2 = btc["low"] if bull else btc["high"]
    ax.annotate(f"Retrace bottom\n({sig['mae_r']*100:.0f}% of R back)",
                xy=(bot_x, ay2),
                xytext=(bot_x, ay2 + off*(-1 if bull else 1)),
                color="#f06292", **kw)

    # C1
    c1c = candles[c1_x]
    ay3 = c1c["high"] if not bull else c1c["low"]
    ax.annotate("C1 mini-BOS\n→ ALERT + ENTRY",
                xy=(c1_x, c1c["close"]),
                xytext=(c1_x, c1c["close"] + off*(1 if not bull else -1)),
                color="#29b6f6", **kw)

    # R:R bracket on right side (just after C1)
    rx = c1_x + 3
    mid = (new_entry + orig_tp) / 2
    ax.annotate("", xy=(rx, orig_tp),   xytext=(rx, new_entry),
                arrowprops=dict(arrowstyle="<->", color="#4caf50", lw=1.5))
    ax.text(rx+0.5, mid, f"TP dist\nR:R orig {sig['orig_rr']:.1f}:1\nR:R tight {sig['tight_rr']:.1f}:1",
            color="#4caf50", fontsize=8, va="center")
    mid_sl = (new_entry + orig_sl) / 2
    ax.annotate("", xy=(rx, orig_sl),   xytext=(rx, new_entry),
                arrowprops=dict(arrowstyle="<->", color="#ff9800", lw=1.5))
    ax.text(rx+0.5, mid_sl, f"Orig risk\n{abs(new_entry-orig_sl)/sig['atr_5m']:.1f}×ATR",
            color="#ff9800", fontsize=8, va="center")
    if abs(tight_sl-broken) > sig["atr_5m"]*0.02:
        mid_ts = (new_entry + tight_sl) / 2
        ax.text(rx+0.5, mid_ts, f"Tight risk\n{abs(new_entry-tight_sl)/sig['atr_5m']:.1f}×ATR",
                color="#f06292", fontsize=8, va="center")

    # Phase labels at top
    for xs_range, lbl, col in [
        ((0, g_x),     "pre-break",   "#aaa"),
        ((g_x, bot_x), "retrace",     "#f39c12"),
        ((bot_x, c1_x),"reversal",    "#e74c3c"),
        ((c1_x, tp_x), "→ TP run",    "#27ae60"),
    ]:
        mid_x = (xs_range[0]+xs_range[1])/2
        ax.text(mid_x, y_max+rng*0.03, lbl, color=col, fontsize=8.5, ha="center", va="bottom")

    # Outcome badge
    ax.text(0.99, 0.98, f"✓ WIN", transform=ax.transAxes,
            color="#4caf50", fontsize=14, ha="right", va="top", fontweight="bold",
            bbox=dict(boxstyle="round,pad=0.4", fc=BG, ec="#4caf50", lw=2))

    # x-axis: show UTC times at key points
    tick_xs = [0, g_x, bot_x, c1_x, tp_x, len(candles)-1]
    tick_xs = sorted(set(max(0,min(len(candles)-1,x)) for x in tick_xs))
    tick_labels = []
    for x in tick_xs:
        ts = m5[start+x]["timestamp"]
        tick_labels.append(datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M\n%d-%b"))
    ax.set_xticks(tick_xs); ax.set_xticklabels(tick_labels, color="#aaa", fontsize=7.5)

    # Title
    dir_str = "BULLISH" if bull else "BEARISH"
    ts_c1   = datetime.fromtimestamp(m5[c1_idx]["timestamp"], tz=timezone.utc)
    ax.set_title(f"{sig['symbol']} 5m  ·  1h BOS {dir_str} retrace entry  "
                 f"·  C1 @ {ts_c1.strftime('%Y-%m-%d %H:%M UTC')}  "
                 f"·  retrace {sig['mae_r']*100:.0f}%R",
                 color="white", fontsize=12, pad=10)
    ax.set_ylabel("Price", color="#aaa"); ax.tick_params(colors="#aaa")
    ax.spines[:].set_color("#333")
    ax.set_xlim(-1, len(candles)+1)
    ax.set_ylim(y_min - rng*0.10, y_max + rng*0.22)
    ax.legend(loc="upper left", facecolor="#1a1a2e", edgecolor="#444",
              labelcolor="white", fontsize=8.5, framealpha=0.9)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close()
    print(f"Saved: {out_path}")


def main():
    db = LocalDB(DB_PATH); analyzer = SMCAnalyzer()
    out = Path(__file__).resolve().parents[1] / "local_dev" / "h1re_example.png"

    best = None; best_score = -1
    for sym in ["XAUUSD", "EURUSD", "NZDUSD", "USDCHF"]:
        print(f"Scanning {sym}...")
        examples = find_examples(sym, db, analyzer, n=50)
        if examples:
            top = examples[0]
            print(f"  best: {top['direction']} MAE={top['mae_r']:.2f}R orig_risk={top['orig_risk']/top['atr_5m']:.2f}xATR score={top['score']:.4f}")
            if top["score"] > best_score:
                best = top; best_score = top["score"]

    if best:
        print(f"\nPlotting: {best['symbol']} {best['direction']} mae={best['mae_r']:.2f}R")
        plot_example(best, str(out))
    db.close()

if __name__ == "__main__":
    main()
