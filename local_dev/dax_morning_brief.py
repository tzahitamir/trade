#!/usr/bin/env python3
"""
DAX Morning Brief — run at 07:00 IDT (04:00 UTC) before Frankfurt opens.

Data sources (tried in order):
  1. Live: MT5 EA file  (GER40_M5.csv in MT5 Common/Files — updated every 30s)
  2. Fallback: historical DB  (src/data/trade.db — requires manual CSV import)

Usage:
  python3 dax_morning_brief.py            # live run (07:00 IDT)
  python3 dax_morning_brief.py 2026-06-18 # backtest / simulate a date (uses DB)

Key times (UTC, summer CEST = UTC+2, IDT = UTC+3):
  15:25 UTC → last 5m before Frankfurt close (Xetra)
  22:05 UTC → Asian session opens (after CFD daily reset)
  04:00 UTC → entry / brief time (07:00 IDT)
  06:30 UTC → window closes (09:30 IDT)
  07:00 UTC → Frankfurt opens (10:00 IDT) — avoid 06:50–07:10 UTC
"""

import sys
import logging
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta, date

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyArrowPatch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from db.local_db import LocalDB
from data.ger40_file_reader import read_candles, check_staleness, get_file_paths, StaleFeedError

DB_PATH = str(Path(__file__).resolve().parents[1] / "src" / "data" / "trade.db")
SYMBOL  = "GER40"
TF      = "5m"

# Thresholds from backtest
GAP_MIN_ATR      = 0.3
GAP_LARGE_ATR    = 2.0   # above this → large-gap rules apply
GAP_EVENT_ATR    = 6.0   # above this → macro event, skip entirely
SWEEP_LIKELY_ATR = 0.5   # dist to extreme < 0.5 → sweep very likely (87–94%)
SWEEP_WAIT_ATR   = 1.0   # dist 0.5–1.0 → uncertain, wait 20 min

# Fill rate lookup by gap band (from backtest, no-sweep days, pre-Frankfurt window)
_FILL_STATS = {
    (0.3, 0.5):  (89, 100),
    (0.5, 0.75): (70, 70),
    (0.75, 1.0): (69, 88),
    (1.0, 1.5):  (57, 79),
    (1.5, 2.0):  (19, 62),
}

# Setup type constants
SETUP_SKIP_SMALL    = "skip_small"
SETUP_SKIP_EVENT    = "skip_event"
SETUP_A             = "A"
SETUP_A_WEAK        = "A_weak"
SETUP_UNCERTAIN     = "uncertain"
SETUP_B             = "B"
SETUP_LARGE_SWEEP   = "large_gap_sweep"
SETUP_LARGE_SERPE   = "large_gap_serpe"


def ts_utc(d: date, h: int, m: int) -> int:
    return int(datetime(d.year, d.month, d.day, h, m, tzinfo=timezone.utc).timestamp())


def simple_atr(candles, n=20):
    tail = candles[-n:] if len(candles) >= n else candles
    if len(tail) < 2:
        return 0.0
    trs = []
    for i in range(1, len(tail)):
        c, p = tail[i], tail[i - 1]
        trs.append(max(c["high"] - c["low"],
                       abs(c["high"] - p["close"]),
                       abs(c["low"]  - p["close"])))
    return sum(trs) / len(trs)


def get_fill_stats(gap_atr):
    for (lo, hi), (full, half) in _FILL_STATS.items():
        if lo <= gap_atr < hi:
            return full, half
    return None, None


def prev_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def run_brief(target_date: date, db: LocalDB | None = None,
              all5m: list | None = None, verbose=True) -> dict | None:
    """
    all5m: pre-loaded candle list (oldest→newest). If None, loads from db.
    db:    LocalDB instance. Used only when all5m is None.
    """
    if all5m is None:
        if db is None:
            raise ValueError("Must supply either all5m or db")
        raw   = db.query_recent(SYMBOL, TF, limit=80000)
        all5m = list(reversed(raw))

    by_date: dict = {}
    for c in all5m:
        d = datetime.fromtimestamp(c["timestamp"], timezone.utc).date()
        by_date.setdefault(d, []).append(c)

    # ── Previous trading day Frankfurt close ──────────────────────────────────
    prev_date   = prev_trading_day(target_date)
    close_cutoff = ts_utc(prev_date, 15, 25)
    prev_xetra  = [c for c in by_date.get(prev_date, []) if c["timestamp"] <= close_cutoff]
    if not prev_xetra:
        if verbose:
            print(f"No Xetra data found for {prev_date} — is DB up to date?")
        return None

    fkft_close = prev_xetra[-1]["close"]
    atr        = simple_atr(prev_xetra, n=20)
    if atr < 1:
        if verbose:
            print("ATR too small — check data")
        return None

    # ── Asian session range (22:05 UTC prev day → 03:55 UTC today) ───────────
    asian_start = ts_utc(prev_date, 22,  5)
    asian_end   = ts_utc(target_date, 3, 55)
    asian_c     = [c for c in all5m if asian_start <= c["timestamp"] <= asian_end]
    if len(asian_c) < 5:
        if verbose:
            print(f"No Asian session data for {target_date} — import fresh GER40 data from MT5.")
        return None

    asian_high = max(c["high"] for c in asian_c)
    asian_low  = min(c["low"]  for c in asian_c)
    asian_mid  = (asian_high + asian_low) / 2
    asian_range_atr = (asian_high - asian_low) / atr

    # ── Current price: most recent candle at or just after 04:00 UTC ─────────
    entry_ts    = ts_utc(target_date, 4, 0)
    entry_c     = [c for c in by_date.get(target_date, []) if c["timestamp"] >= entry_ts]
    if not entry_c:
        # fall back to most recent available before entry
        entry_c = [c for c in by_date.get(target_date, []) if c["timestamp"] < entry_ts]
        if not entry_c:
            if verbose:
                print(f"No price data found at 07:00 IDT for {target_date}.")
            return None
    current_price = entry_c[0]["open"]

    # ── Core metrics ──────────────────────────────────────────────────────────
    gap_pts   = current_price - fkft_close
    gap_atr   = abs(gap_pts) / atr
    bullish   = gap_pts < 0        # price below close → buy up toward close
    trade_dir = "BUY ↑" if bullish else "SELL ↓"
    dow_name  = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][target_date.weekday()]

    # Distance from current price to Asian extreme in sweep direction
    # Bullish: sweep = price dips below Asian LOW first → dist = current - asian_low
    # Bearish: sweep = price spikes above Asian HIGH first → dist = asian_high - current
    if bullish:
        dist_to_sweep   = current_price - asian_low
        sweep_level     = asian_low
        sweep_label     = "Asian LOW"
    else:
        dist_to_sweep   = asian_high - current_price
        sweep_level     = asian_high
        sweep_label     = "Asian HIGH"
    dist_to_sweep_atr = dist_to_sweep / atr

    # SL for Setup A = Asian extreme (below asian_low for bull, above asian_high for bear)
    sl_a = asian_low  if bullish else asian_high
    sl_dist_a = abs(current_price - sl_a)

    # ── Determine setup type ──────────────────────────────────────────────────
    sl_b      = (asian_low  - atr * 0.25) if bullish else (asian_high + atr * 0.25)
    sl_b_dist = abs(sweep_level - sl_b)

    if gap_atr < GAP_MIN_ATR:
        setup_type     = SETUP_SKIP_SMALL
        sweep_expected = False
    elif gap_atr >= GAP_EVENT_ATR:
        setup_type     = SETUP_SKIP_EVENT
        sweep_expected = False
    elif gap_atr >= GAP_LARGE_ATR:
        # Large gap rules
        if dist_to_sweep_atr < SWEEP_LIKELY_ATR:
            setup_type     = SETUP_LARGE_SWEEP   # 94% sweep → wait, target 33%
            sweep_expected = True
        else:
            setup_type     = SETUP_LARGE_SERPE   # skip pre-fkt, note for SERPE
            sweep_expected = False
    else:
        # Normal gap (0.3–2×ATR)
        if dist_to_sweep_atr < SWEEP_LIKELY_ATR:
            setup_type     = SETUP_B
            sweep_expected = True
        elif dist_to_sweep_atr < SWEEP_WAIT_ATR:
            setup_type     = SETUP_UNCERTAIN
            sweep_expected = True   # possible, watch for it
        elif gap_atr >= 1.5:
            setup_type     = SETUP_A_WEAK
            sweep_expected = False
        else:
            setup_type     = SETUP_A
            sweep_expected = False

    # ── Build output ──────────────────────────────────────────────────────────
    lines = []
    lines.append(f"{'─'*52}")
    lines.append(f"DAX Morning Brief — {dow_name} {target_date}  07:00 IDT")
    lines.append(f"{'─'*52}")
    lines.append(f"")
    lines.append(f"Yesterday Frankfurt close : {fkft_close:.0f}")
    lines.append(f"ATR (prev session, 5m×20) : {atr:.0f} pts")
    lines.append(f"Current price (07:00 IDT) : {current_price:.0f}")
    lines.append(f"")
    lines.append(f"Gap : {gap_pts:+.0f} pts  ({gap_atr:.2f}×ATR)  [{trade_dir} toward close]")
    lines.append(f"")

    if setup_type == SETUP_SKIP_SMALL:
        lines.append(f"SKIP — gap too small ({gap_atr:.2f}×ATR < {GAP_MIN_ATR}×)")

    elif setup_type == SETUP_SKIP_EVENT:
        lines.append(f"SKIP — event gap ({gap_atr:.2f}×ATR >= {GAP_EVENT_ATR}×)")
        lines.append(f"Macro event gap. Only 16% fill during Frankfurt. No trade.")

    elif setup_type == SETUP_LARGE_SERPE:
        lines.append(f"Asian range : {asian_low:.0f} – {asian_high:.0f}  (mid {asian_mid:.0f})")
        lines.append(f"Dist to {sweep_label}: {dist_to_sweep:.0f} pts ({dist_to_sweep_atr:.2f}×ATR)")
        lines.append(f"")
        lines.append(f"LARGE GAP — SKIP pre-Frankfurt")
        lines.append(f"Gap {gap_atr:.2f}×ATR: 33% fill rate in 07:00-09:30 IDT. Poor R:R.")
        lines.append(f"")
        lines.append(f"Watch for SERPE alignment at Frankfurt open (10:00 IDT):")
        lines.append(f"  If SERPE fires {trade_dir} — gap adds conviction (58-62% fill during session)")
        lines.append(f"  Direction bias: {trade_dir} toward {fkft_close:.0f}")

    elif setup_type == SETUP_LARGE_SWEEP:
        lines.append(f"Asian range : {asian_low:.0f} – {asian_high:.0f}  (mid {asian_mid:.0f})")
        lines.append(f"Dist to {sweep_label}: {dist_to_sweep:.0f} pts ({dist_to_sweep_atr:.2f}×ATR)")
        lines.append(f"")
        lines.append(f"LARGE GAP + SWEEP NEARLY CERTAIN (94% at this distance)")
        lines.append(f"Expected before: 08:00 IDT  (median 07:42 IDT)")
        lines.append(f"")
        lines.append(f"SETUP: Wait for sweep, then enter (target 33% of gap only)")
        tp_large = sweep_level + (fkft_close - sweep_level) * 0.33 if bullish \
                   else sweep_level - (sweep_level - fkft_close) * 0.33
        lines.append(f"  Watch level : {sweep_level:.0f} ({sweep_label})")
        lines.append(f"  Entry       : at sweep ({sweep_level:.0f})")
        lines.append(f"  TP          : {tp_large:.0f} (33% of gap toward Frankfurt close)")
        lines.append(f"  SL          : {sl_b:.0f} ({atr*0.25:.0f} pts beyond sweep)")
        rr_large = abs(tp_large - sweep_level) / sl_b_dist if sl_b_dist > 0 else 0
        lines.append(f"  R:R         : ~{rr_large:.1f}×")
        lines.append(f"  Exit hard   : 09:30 IDT — DO NOT hold into Frankfurt open")
        lines.append(f"  No sweep by 08:30 IDT → skip")

    elif setup_type == SETUP_B:
        full_fill_pct, _ = get_fill_stats(gap_atr)
        rr_b = abs(fkft_close - sweep_level) / sl_b_dist if sl_b_dist > 0 else 0
        lines.append(f"Asian range : {asian_low:.0f} – {asian_high:.0f}  (mid {asian_mid:.0f})")
        lines.append(f"Dist to {sweep_label}: {dist_to_sweep:.0f} pts ({dist_to_sweep_atr:.2f}×ATR)")
        lines.append(f"")
        lines.append(f"SWEEP LIKELY — {87 if dist_to_sweep_atr < 0.3 else 90}% probability")
        lines.append(f"Expected before: 08:00 IDT  (median 07:42 IDT)")
        lines.append(f"")
        lines.append(f"SETUP B — Wait for sweep, then enter")
        lines.append(f"  Watch level : {sweep_level:.0f} ({sweep_label})")
        lines.append(f"  Entry       : at sweep ({sweep_level:.0f}) — {trade_dir}")
        lines.append(f"  TP          : {fkft_close:.0f} (Frankfurt close)")
        lines.append(f"  SL          : {sl_b:.0f} ({atr*0.25:.0f} pts beyond sweep)")
        lines.append(f"  R:R         : ~{rr_b:.1f}×  (historical median ~8.5×)")
        lines.append(f"  No sweep by 08:30 IDT → skip")

    elif setup_type == SETUP_UNCERTAIN:
        full_fill_pct, _ = get_fill_stats(gap_atr)
        lines.append(f"Asian range : {asian_low:.0f} – {asian_high:.0f}  (mid {asian_mid:.0f})")
        lines.append(f"Dist to {sweep_label}: {dist_to_sweep:.0f} pts ({dist_to_sweep_atr:.2f}×ATR)")
        lines.append(f"")
        lines.append(f"UNCERTAIN — sweep possible (50-70%). Wait 20 min.")
        lines.append(f"")
        lines.append(f"  If sweep before 07:20 IDT → enter at {sweep_level:.0f} (SETUP B)")
        lines.append(f"    TP={fkft_close:.0f}  SL={sl_b:.0f}  R:R ~8.5×")
        lines.append(f"  If no sweep by 07:20 IDT → enter at market (SETUP A)")
        lines.append(f"    Entry={current_price:.0f}  TP1={asian_mid:.0f}  TP2={fkft_close:.0f}")
        sl_dist_a = abs(current_price - sl_a)
        lines.append(f"    SL={sl_a:.0f} ({sl_dist_a:.0f} pts)  "
                     f"fill rate: {'N/A' if full_fill_pct is None else str(full_fill_pct)+'%'}")

    else:  # SETUP_A or SETUP_A_WEAK
        full_fill_pct, half_fill_pct = get_fill_stats(gap_atr)
        sl_dist_a = abs(current_price - sl_a)
        rr_tp1 = abs(asian_mid    - current_price) / sl_dist_a if sl_dist_a > 0 else 0
        rr_tp2 = abs(fkft_close   - current_price) / sl_dist_a if sl_dist_a > 0 else 0
        lines.append(f"Asian range : {asian_low:.0f} – {asian_high:.0f}  (mid {asian_mid:.0f})")
        lines.append(f"Dist to {sweep_label}: {dist_to_sweep:.0f} pts ({dist_to_sweep_atr:.2f}×ATR)")
        lines.append(f"")
        if setup_type == SETUP_A_WEAK:
            lines.append(f"SETUP A (weak) — sweep unlikely, but large gap reduces fill odds")
        else:
            lines.append(f"SETUP A — enter now, sweep unlikely")
        lines.append(f"")
        lines.append(f"  Entry  : {current_price:.0f}  [{trade_dir}]")
        lines.append(f"  TP1    : {asian_mid:.0f} (Asian mid — 81% hit, take partial)")
        lines.append(f"  TP2    : {fkft_close:.0f} (Frankfurt close)")
        lines.append(f"  SL     : {sl_a:.0f} ({sweep_label}, {sl_dist_a:.0f} pts)")
        lines.append(f"  R:R    : {rr_tp1:.1f}× to TP1  |  {rr_tp2:.1f}× to TP2")
        if full_fill_pct:
            lines.append(f"  Stats  : {full_fill_pct}% full fill | {half_fill_pct}% half fill")
        dow_notes = {1: "Tue: best fill timing",
                     3: "Thu: 89% fill on no-sweep days",
                     0: "Mon: below-average fill rate",
                     4: "Fri: below-average fill rate"}
        if target_date.weekday() in dow_notes:
            lines.append(f"  DoW    : {dow_notes[target_date.weekday()]}")

    if setup_type not in (SETUP_SKIP_SMALL, SETUP_SKIP_EVENT, SETUP_LARGE_SERPE):
        lines.append(f"")
        lines.append(f"Exit hard stop : 09:30 IDT")
        lines.append(f"Avoid entries  : 09:50-10:10 IDT (Frankfurt open)")
    lines.append(f"{'─'*52}")

    brief = "\n".join(lines)
    if verbose:
        print(brief)

    # ── Structured state (used by sweep watcher) ──────────────────────────────
    tp_level = fkft_close
    if setup_type == SETUP_LARGE_SWEEP:
        tp_level = sweep_level + (fkft_close - sweep_level) * 0.33 if bullish \
                   else sweep_level - (sweep_level - fkft_close) * 0.33

    state = {
        # Identity
        "date":               target_date,
        "setup_type":         setup_type,
        "sweep_expected":     sweep_expected,
        # Prices
        "fkft_close":         fkft_close,
        "atr":                atr,
        "current_price":      current_price,
        "gap_pts":            gap_pts,
        "gap_atr":            gap_atr,
        "bullish":            bullish,
        "trade_dir":          trade_dir,
        # Asian range
        "asian_low":          asian_low,
        "asian_high":         asian_high,
        "asian_mid":          asian_mid,
        # Sweep
        "sweep_level":        sweep_level,
        "sweep_label":        sweep_label,
        "dist_to_sweep_atr":  dist_to_sweep_atr,
        # Trade levels
        "tp_level":           tp_level,
        "sl_level":           sl_b if sweep_expected else sl_a,
        # Output
        "brief":              brief,
        "chart_path":         None,
    }

    # Render chart (skip_small / skip_event don't need one)
    if setup_type not in (SETUP_SKIP_SMALL, SETUP_SKIP_EVENT):
        try:
            state["chart_path"] = render_brief_chart(state, all5m)
        except Exception as exc:
            logging.warning("Brief chart render failed: %s", exc)

    return state


def render_brief_chart(result: dict, all5m: list, out_path: str | None = None) -> str:
    """
    Render overnight GER40 5m chart for the morning brief and save as PNG.
    Returns the file path.
    """
    target_date   = result["date"]
    prev_date     = prev_trading_day(target_date)
    fkft_close    = result["fkft_close"]
    asian_low     = result["asian_low"]
    asian_high    = result["asian_high"]
    asian_mid     = result["asian_mid"]
    sweep_level   = result["sweep_level"]
    sweep_label   = result["sweep_label"]
    current_price = result["current_price"]
    setup_type    = result["setup_type"]
    gap_atr       = result["gap_atr"]
    bullish       = result["bullish"]
    trade_dir     = result["trade_dir"]
    tp_level      = result["tp_level"]
    sl_level      = result["sl_level"]
    sweep_expected = result["sweep_expected"]

    # Entry price: at sweep level for B/large_gap_sweep, at current price otherwise
    entry_price = sweep_level if sweep_expected else current_price

    # R:R
    sl_pts = abs(entry_price - sl_level)
    tp_pts = abs(tp_level    - entry_price)
    rr     = tp_pts / sl_pts if sl_pts > 0 else 0

    # ── Filter candles: 14:30 UTC prev_date → 04:30 UTC target_date ──────────
    t_start = ts_utc(prev_date, 14, 30)
    t_end   = ts_utc(target_date, 4, 30)
    candles = [c for c in all5m if t_start <= c["timestamp"] <= t_end]
    if len(candles) < 10:
        raise ValueError(f"Too few candles for chart ({len(candles)})")

    dts = [datetime.fromtimestamp(c["timestamp"], timezone.utc) for c in candles]
    xs  = list(range(len(candles)))

    # ── Key x positions ───────────────────────────────────────────────────────
    def nearest_x(ts: int) -> int:
        best, best_x = None, 0
        for i, c in enumerate(candles):
            diff = abs(c["timestamp"] - ts)
            if best is None or diff < best:
                best, best_x = diff, i
        return best_x

    x_fkft_close  = nearest_x(ts_utc(prev_date, 15, 25))
    x_asian_start = nearest_x(ts_utc(prev_date, 22,  5))
    x_entry       = nearest_x(ts_utc(target_date, 4,  0))

    # ── Figure ────────────────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(14, 6))
    fig.patch.set_facecolor("#1a1a2e")
    ax.set_facecolor("#16213e")

    # Candles
    W = 0.6
    for i, c in enumerate(candles):
        o, h_, l, cl = c["open"], c["high"], c["low"], c["close"]
        color = "#26a69a" if cl >= o else "#ef5350"
        ax.add_patch(plt.Rectangle((i - W/2, min(o, cl)), W, abs(cl - o),
                                   color=color, zorder=3))
        ax.plot([i, i], [l, h_], color=color, linewidth=0.8, zorder=2)

    # ── Session shading ───────────────────────────────────────────────────────
    ax.axvspan(x_fkft_close,  x_asian_start, color="#ffffff", alpha=0.03, zorder=1)
    ax.axvspan(x_asian_start, x_entry,        color="#4fc3f7", alpha=0.07, zorder=1)

    # ── Vertical markers ──────────────────────────────────────────────────────
    ax.axvline(x_fkft_close,  color="#FFD700", linestyle="--", linewidth=0.9, alpha=0.5, zorder=2)
    ax.axvline(x_asian_start, color="#4fc3f7",  linestyle="--", linewidth=0.9, alpha=0.5, zorder=2)
    ax.axvline(x_entry,       color="#90caf9",  linestyle=":",  linewidth=1.2, alpha=0.9, zorder=2)

    all_prices = [c["high"] for c in candles] + [c["low"] for c in candles]
    y_min, y_max = min(all_prices), max(all_prices)
    y_pad  = (y_max - y_min) * 0.08
    LABEL_X = len(xs) + 0.8

    # Top vertical-marker labels
    label_y = y_max + y_pad * 0.25
    for vx, lbl, col in [
        (x_fkft_close,  "Prev Close", "#FFD700"),
        (x_asian_start, "Asian Open", "#4fc3f7"),
        (x_entry,       "07:00 IDT",  "#90caf9"),
    ]:
        ax.text(vx + 0.3, label_y, lbl, color=col, fontsize=7, va="bottom", ha="left",
                bbox=dict(facecolor="#1a1a2e", edgecolor="none", pad=1), zorder=5)

    # ── Horizontal price lines ────────────────────────────────────────────────
    def hline(price, color, lw, ls, alpha, label_txt, label_color=None):
        ax.axhline(price, color=color, linewidth=lw, linestyle=ls, alpha=alpha, zorder=4)
        ax.text(LABEL_X, price, f" {label_txt}", color=label_color or color,
                fontsize=8, va="center", ha="left", zorder=5,
                bbox=dict(facecolor="#1a1a2e", edgecolor="none", pad=1))

    sweep_color = "#26a69a" if bullish else "#ef5350"
    dir_color   = "#26a69a" if bullish else "#ef5350"

    # Background context lines (dim)
    hline(asian_high, "#ef5350", 0.9, "--", 0.55, f"Asian H  {asian_high:.0f}")
    hline(asian_low,  "#26a69a", 0.9, "--", 0.55, f"Asian L  {asian_low:.0f}")
    hline(asian_mid,  "#666688", 0.7, ":",  0.45, f"Mid      {asian_mid:.0f}")
    hline(current_price, "#90caf9", 0.9, ":", 0.70, f"Now  {current_price:.0f}")

    # Sweep level — bold if sweep expected
    if sweep_expected:
        hline(sweep_level, sweep_color, 1.8, "--", 0.95,
              f"SWEEP  {sweep_level:.0f}", label_color="#ffffff")
    else:
        hline(sweep_level, sweep_color, 1.0, "--", 0.55, f"Sweep  {sweep_level:.0f}")

    # Frankfurt close — the target
    hline(fkft_close, "#FFD700", 2.0, "-", 0.95, f"Close  {fkft_close:.0f}")

    # ── Trade lines: SL and TP ────────────────────────────────────────────────
    # SL — red, solid, thick
    ax.axhline(sl_level, color="#ff1744", linewidth=2.0, linestyle="-", alpha=0.90, zorder=5)
    ax.text(LABEL_X, sl_level, f" SL  {sl_level:.0f}", color="#ff1744",
            fontsize=9, fontweight="bold", va="center", ha="left", zorder=6,
            bbox=dict(facecolor="#2a0010", edgecolor="#ff1744", linewidth=0.8, pad=2))

    # TP — green for bull, amber for bear (reaching up/down toward Frankfurt close)
    tp_color = "#00e676" if bullish else "#ffab40"
    ax.axhline(tp_level, color=tp_color, linewidth=2.0, linestyle="-", alpha=0.90, zorder=5)
    tp_suffix = "  (33% gap)" if setup_type == SETUP_LARGE_SWEEP else "  (Frankfurt close)"
    ax.text(LABEL_X, tp_level, f" TP  {tp_level:.0f}{tp_suffix}", color=tp_color,
            fontsize=9, fontweight="bold", va="center", ha="left", zorder=6,
            bbox=dict(facecolor="#001a0a" if bullish else "#1a1000",
                      edgecolor=tp_color, linewidth=0.8, pad=2))

    # Shade the trade zone between entry and TP
    tp_shade_color = "#00e676" if bullish else "#ffab40"
    ax.axhspan(min(entry_price, tp_level), max(entry_price, tp_level),
               color=tp_shade_color, alpha=0.06, zorder=1)
    # Shade the risk zone between entry and SL
    ax.axhspan(min(entry_price, sl_level), max(entry_price, sl_level),
               color="#ff1744", alpha=0.06, zorder=1)

    # ── Gap arrow ─────────────────────────────────────────────────────────────
    mid_x = (x_fkft_close + x_entry) // 2
    ax.annotate("", xy=(mid_x, fkft_close), xytext=(mid_x, current_price),
                arrowprops=dict(arrowstyle="<->", color="#FFD700", lw=1.2), zorder=5)
    ax.text(mid_x + 0.5, (current_price + fkft_close) / 2,
            f"gap\n{gap_atr:.2f}×ATR", color="#FFD700", fontsize=7.5,
            va="center", ha="left", zorder=5,
            bbox=dict(facecolor="#1a1a2e", edgecolor="none", pad=1))

    # ── X-axis labels (IDT time) ──────────────────────────────────────────────
    label_xs, label_strs = [], []
    for i, c in enumerate(candles):
        dt    = dts[i]
        idt_h = (dt.hour + 3) % 24
        if dt.minute == 0 and idt_h % 2 == 1:
            label_xs.append(i)
            label_strs.append(
                f"{idt_h:02d}:00\n{dt.strftime('%d/%m')}" if dt.date() != prev_date
                else f"{idt_h:02d}:00"
            )
    ax.set_xticks(label_xs)
    ax.set_xticklabels(label_strs, fontsize=7.5, color="#cccccc")

    ax.set_ylim(y_min - y_pad, y_max + y_pad * 2.2)
    ax.set_xlim(-1, len(xs) + 14)
    ax.tick_params(axis="y", colors="#cccccc", labelsize=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    for s in ["bottom", "left"]:
        ax.spines[s].set_color("#444466")
    ax.yaxis.grid(color="#2a2a4a", linewidth=0.5, zorder=0)

    # ── Title ─────────────────────────────────────────────────────────────────
    dow_name = ["Mon", "Tue", "Wed", "Thu", "Fri"][target_date.weekday()]
    ax.set_title(
        f"GER40 5m  │  {dow_name} {target_date}  │  gap {gap_atr:.2f}×ATR",
        fontsize=10.5, fontweight="bold", color="#e0e0e0", pad=8,
    )

    # ── Trade info panel (top-left) ───────────────────────────────────────────
    if setup_type in (SETUP_B, SETUP_LARGE_SWEEP):
        action_line = f"{'SELL' if not bullish else 'BUY'} ↓  @  SWEEP {entry_price:.0f}"
        wait_line   = f"Wait for sweep first →"
    elif setup_type == SETUP_UNCERTAIN:
        action_line = f"{'BUY ↑' if bullish else 'SELL ↓'}  (sweep → {sweep_level:.0f} or enter @{current_price:.0f})"
        wait_line   = "Wait until 07:20 IDT"
    elif setup_type == SETUP_LARGE_SERPE:
        action_line = f"Skip — watch SERPE @ Frankfurt open (10:00)"
        wait_line   = f"Direction bias: {trade_dir}"
    else:
        action_line = f"{'BUY ↑' if bullish else 'SELL ↓'}  @  {entry_price:.0f}  (enter now)"
        wait_line   = None

    setup_label = {
        SETUP_A:           "Setup A",
        SETUP_A_WEAK:      "Setup A (weak)",
        SETUP_B:           "Setup B",
        SETUP_UNCERTAIN:   "Uncertain",
        SETUP_LARGE_SWEEP: "Large Gap Sweep",
        SETUP_LARGE_SERPE: "Large Gap — SKIP",
    }.get(setup_type, setup_type.upper())

    panel_lines = [f"◆ {setup_label}", action_line]
    if wait_line:
        panel_lines.append(wait_line)
    if setup_type != SETUP_LARGE_SERPE:
        panel_lines += [
            f"TP  {tp_level:.0f}",
            f"SL  {sl_level:.0f}",
            f"R:R  {rr:.1f}×",
        ]

    panel_txt = "\n".join(panel_lines)
    ax.text(0.01, 0.99, panel_txt,
            transform=ax.transAxes, fontsize=9, family="monospace",
            color="#e0e0e0", va="top", ha="left", zorder=7,
            bbox=dict(boxstyle="round,pad=0.5", facecolor="#0d0d1a",
                      edgecolor=dir_color, linewidth=1.5, alpha=0.92))

    plt.tight_layout()

    if out_path is None:
        fd, out_path = tempfile.mkstemp(suffix=".png", prefix="dax_brief_")
        import os; os.close(fd)
    plt.savefig(out_path, dpi=130, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    return out_path


def _send_telegram(msg: str) -> None:
    """Send a Telegram message using the app's alert infrastructure."""
    try:
        import os, requests
        token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            # Try loading from credentials file
            creds = Path(__file__).resolve().parents[1] / "secrets" / "credentials.env"
            if creds.exists():
                for line in creds.read_text().splitlines():
                    if "=" in line and not line.startswith("#"):
                        k, _, v = line.partition("=")
                        os.environ.setdefault(k.strip(), v.strip())
            token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if token and chat_id:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
                timeout=10,
            )
    except Exception as exc:
        print(f"[telegram] send failed: {exc}")


def main():
    if len(sys.argv) > 1:
        try:
            target = date.fromisoformat(sys.argv[1])
        except ValueError:
            print("Usage: python3 dax_morning_brief.py [YYYY-MM-DD]")
            return
        # Simulate mode: always use DB (live file may not have historical dates)
        if target.weekday() >= 5:
            print(f"{target} is a weekend — no DAX trading.")
            return
        db = LocalDB(DB_PATH)
        run_brief(target, db=db, verbose=True)
        db.close()
        return

    # ── Live mode (no date argument = run now) ────────────────────────────────
    target = datetime.now(timezone.utc).date()
    if target.weekday() >= 5:
        print(f"{target} is a weekend — no DAX trading.")
        return

    now_utc = datetime.now(timezone.utc)
    _, hb_path = get_file_paths()

    # 1. Check if MT5/EA is alive — alert if stale during pre-market window
    try:
        check_staleness(hb_path, now_utc=now_utc)
    except StaleFeedError as exc:
        msg = (
            f"⚠️ <b>DAX Morning Brief — MT5 FEED DOWN</b>\n"
            f"{now_utc.strftime('%H:%M UTC')} ({(now_utc.hour+3)%24:02d}:{now_utc.minute:02d} IDT)\n\n"
            f"{exc}\n\n"
            f"Cannot generate pre-Frankfurt brief. Check MT5 is running "
            f"and GER40_M5_export EA is attached to the GER40 M5 chart."
        )
        print(msg)
        _send_telegram(msg)
        return

    # 2. Load live candles from EA file
    try:
        all5m = read_candles(limit=350)
        source = "live MT5 feed"
    except (FileNotFoundError, StaleFeedError) as exc:
        print(f"[live feed] {exc}")
        print("Falling back to DB...")
        db = LocalDB(DB_PATH)
        result = run_brief(target, db=db, verbose=True)
        db.close()
        return

    # 3. Generate and send the brief
    result = run_brief(target, all5m=all5m, verbose=True)
    if result:
        brief_with_source = f"<pre>{result['brief']}</pre>\n<i>Source: {source}</i>"
        _send_telegram(brief_with_source)


if __name__ == "__main__":
    main()
