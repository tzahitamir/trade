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
from pathlib import Path
from datetime import datetime, timezone, timedelta, date

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

    return {
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
    }


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
