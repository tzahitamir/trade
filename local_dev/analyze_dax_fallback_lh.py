#!/usr/bin/env python3
"""
Test a fallback entry trigger for DAX SERPE:
  - Primary: formal 5m counter-BOS (min_break_distance = 0.3×ATR_5m)
  - Fallback: if no BOS fires within BOS_TIMEOUT_MIN of peak,
              enter on first lower high (peak_gate ≤11:45) while price > EQ

TP / SL sizing matches the live SERPE params:
  TP = 0.60 × am_range from entry toward origin
  SL = entry + sl_buffer (above entry for bearish fade, below for bullish fade)
  sl_buffer = 0.15 × am_range

Reports:
  1. Primary-only performance (baseline reference)
  2. Fallback-only performance
  3. Combined performance
  4. Per-day detail for fallback signals
"""
import sys, logging
from pathlib import Path
from datetime import datetime, timezone, timedelta, date

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from db.local_db import LocalDB
from analysis.smc_analyzer import SMCAnalyzer

logging.basicConfig(level=logging.WARNING)

DB_PATH       = str(Path(__file__).resolve().parents[1] / "src" / "data" / "trade.db")
SCAN_DAYS     = 75
LOOKFWD_H     = 6       # hours of candles to look forward after peak
BOS_TIMEOUT_MIN = 90    # if no BOS in this many minutes, consider fallback
PEAK_GATE_H   = 11
PEAK_GATE_M   = 45
# Live gold params (param_set 440):
TP_PCT        = 0.55    # tp = origin + 0.55 × expansion_range (above EQ)
SL_ATR_MULT   = 0.5     # sl = recent_swing_high + 0.5 × atr_5m
ENTRY_ZONE    = 0.70    # entry must be ≥ 70% from origin (still in premium)
BEARISH_ONLY  = True    # only fade bullish Frankfurt expansions
MIN_BREAK_DIST_MULT = 0.30   # × ATR_5m (live SERPE param)


def ts_for(d, h, m, tz):
    return int(datetime(d.year, d.month, d.day, h, m, tzinfo=tz).timestamp())


def median(lst):
    if not lst: return float("nan")
    s = sorted(lst); n = len(s)
    return s[n//2] if n % 2 else (s[n//2-1] + s[n//2]) / 2


def calc_pnl(entry, tp, sl, candles_fwd, bullish_exp):
    """
    Returns ('WIN'|'LOSS'|'OPEN', R).
    bullish_exp=True → we are SHORT (fading bull move): win if price falls to tp, lose if rises to sl.
    bullish_exp=False → we are LONG  (fading bear move): win if price rises to tp, lose if falls to sl.
    """
    risk = abs(entry - sl) or 1.0
    reward = abs(entry - tp) or 1.0
    rr = reward / risk
    for c in candles_fwd:
        if bullish_exp:      # short: want lower
            if c["low"]  <= tp: return "WIN",  rr
            if c["high"] >= sl: return "LOSS", -1.0
        else:                # long: want higher
            if c["high"] >= tp: return "WIN",  rr
            if c["low"]  <= sl: return "LOSS", -1.0
    return "OPEN", 0.0


def detect_bos(candles, am_dir, min_break_pts, atr_5m):
    """
    Return (idx, bos_ts, entry_price) of the first qualifying 5m counter-BOS, or None.
    Counter-BOS for bullish expansion = close below a prior swing low.
    Counter-BOS for bearish expansion = close above a prior swing high.
    """
    swing_highs = []
    swing_lows  = []
    for i in range(2, len(candles)):
        prev2, prev1, cur = candles[i-2], candles[i-1], candles[i]
        if prev1["high"] > prev2["high"] and prev1["high"] > cur["high"]:
            swing_highs.append((i-1, prev1["high"]))
        if prev1["low"] < prev2["low"] and prev1["low"] < cur["low"]:
            swing_lows.append((i-1, prev1["low"]))

        if am_dir == "bullish":
            # counter: look for close below a prior swing low
            for sl_i, sl_price in swing_lows:
                if sl_i < i and cur["close"] < sl_price:
                    break_dist = sl_price - cur["close"]
                    if break_dist >= min_break_pts:
                        return i, cur["timestamp"], cur["close"]
        else:
            for sh_i, sh_price in swing_highs:
                if sh_i < i and cur["close"] > sh_price:
                    break_dist = cur["close"] - sh_price
                    if break_dist >= min_break_pts:
                        return i, cur["timestamp"], cur["close"]
    return None


def detect_first_lower_high_in_premium(candles, am_dir, am_eq):
    """
    First candle that makes a lower high (bullish) / higher low (bearish)
    than the peak candle, while price is still in premium zone (above EQ for bull fade).
    candles[0] is the peak candle.
    """
    if not candles:
        return None
    peak_high = candles[0]["high"]
    peak_low  = candles[0]["low"]
    for i in range(1, len(candles)):
        c = candles[i]
        if am_dir == "bullish":
            in_premium = c["close"] > am_eq
            lower_high = c["high"] < peak_high
            if lower_high and in_premium:
                return i, c["timestamp"], c["close"]
        else:
            in_premium = c["close"] < am_eq
            higher_low = c["low"] > peak_low
            if higher_low and in_premium:
                return i, c["timestamp"], c["close"]
    return None


def main():
    import pytz
    IL_TZ  = pytz.timezone("Asia/Jerusalem")
    db     = LocalDB(DB_PATH)
    an     = SMCAnalyzer()
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=SCAN_DAYS)).timestamp())

    seen = set()
    for c in db.query_recent("DAX", "5m", limit=8000):
        dt = datetime.fromtimestamp(c["timestamp"], tz=IL_TZ).date()
        if dt.weekday() < 5 and c["timestamp"] >= cutoff:
            seen.add(dt)
    trading_dates = sorted(seen)

    all_5m  = list(reversed(db.query_recent("DAX", "5m",  limit=8000)))
    all_15m = list(reversed(db.query_recent("DAX", "15m", limit=2000)))

    primary_results  = []
    fallback_results = []
    skipped          = 0   # no-peak-gate, no-range, Monday, etc.

    for trade_date in trading_dates:
        if trade_date.weekday() == 0:  # skip Monday (live rule)
            continue

        am_start = ts_for(trade_date, 9,  0, IL_TZ)
        am_end   = ts_for(trade_date, 12, 30, IL_TZ)
        fwd_end  = am_end + LOOKFWD_H * 3600

        am_5m  = [c for c in all_5m if am_start  <= c["timestamp"] <= am_end]
        fwd_5m = [c for c in all_5m if am_start  <= c["timestamp"] <= fwd_end]
        am_15m = [c for c in all_15m if am_start <= c["timestamp"] <= am_end]

        if len(am_5m) < 10:
            skipped += 1; continue

        am_high  = max(c["high"] for c in am_5m)
        am_low   = min(c["low"]  for c in am_5m)
        am_range = am_high - am_low
        if am_range < 20:
            skipped += 1; continue
        am_eq    = (am_high + am_low) / 2
        am_open  = am_5m[0]["open"]
        am_dir   = "bullish" if (am_high - am_open) >= (am_open - am_low) else "bearish"

        # bearish_only = True: only fade bullish Frankfurt expansions (short setups)
        if BEARISH_ONLY and am_dir != "bullish":
            skipped += 1; continue

        if am_dir == "bullish":
            peak_c = max(am_5m, key=lambda c: c["high"])
        else:
            peak_c = min(am_5m, key=lambda c: c["low"])
        peak_ts  = peak_c["timestamp"]
        peak_idt = datetime.fromtimestamp(peak_ts, tz=IL_TZ)

        # ── peak gate ──
        gate_ts = ts_for(trade_date, PEAK_GATE_H, PEAK_GATE_M, IL_TZ)
        if peak_ts > gate_ts:
            skipped += 1; continue

        # ATR
        pre_15m = sorted([c for c in all_15m if c["timestamp"] < am_start],
                         key=lambda c: c["timestamp"])[-16:]
        atr_15m = an.calculate_atr(list(reversed(pre_15m))) or am_range * 0.15 or 50.0
        atr_5m  = an.calculate_atr(list(reversed(am_5m))) or atr_15m
        min_break_pts = MIN_BREAK_DIST_MULT * atr_5m

        # Post-peak candles
        post_peak_all = [c for c in fwd_5m if c["timestamp"] > peak_ts]
        if len(post_peak_all) < 5:
            skipped += 1; continue

        # Live TP/SL formula (from smc_analyzer.detect_dax_session_setup):
        # tp = origin + tp_pct × expansion_range  (for bullish exp: 55% from origin upward)
        # sl = recent_swing_high + sl_atr_mult × atr_5m  (for bullish exp short)
        # entry_floor = origin + entry_zone_pct × expansion_range (must be in premium zone)
        if am_dir == "bullish":
            origin      = am_low
            tp_level    = origin + TP_PCT * am_range
            entry_floor = origin + ENTRY_ZONE * am_range
        else:
            origin      = am_high
            tp_level    = origin - TP_PCT * am_range
            entry_floor = origin - ENTRY_ZONE * am_range

        def make_sl(entry_price, candles_window, bullish_exp):
            if bullish_exp:
                swing_high = max(c["high"] for c in candles_window) if candles_window else entry_price
                return swing_high + SL_ATR_MULT * atr_5m
            else:
                swing_low = min(c["low"] for c in candles_window) if candles_window else entry_price
                return swing_low - SL_ATR_MULT * atr_5m

        bullish_exp = (am_dir == "bullish")

        def entry_ok(entry_price):
            if bullish_exp:
                return entry_price >= entry_floor and entry_price > am_eq
            else:
                return entry_price <= entry_floor and entry_price < am_eq

        # ── Primary: 5m counter-BOS ──
        bos_timeout_ts = peak_ts + BOS_TIMEOUT_MIN * 60
        bos_result = detect_bos(post_peak_all, am_dir, min_break_pts, atr_5m)

        primary_fired = False
        primary_late  = False
        if bos_result:
            bos_idx, bos_ts, entry = bos_result
            if entry_ok(entry):
                primary_fired = True
                primary_late  = bos_ts > bos_timeout_ts
                recent_w = post_peak_all[max(0, bos_idx - 3): bos_idx + 1]
                sl = make_sl(entry, recent_w, bullish_exp)
                outcome, R = calc_pnl(entry, tp_level, sl, post_peak_all[bos_idx+1:], bullish_exp)
                primary_results.append({
                    "date": trade_date, "entry": entry,
                    "tp": tp_level, "sl": sl,
                    "outcome": outcome, "R": R,
                    "peak_idt": peak_idt.strftime("%H:%M"),
                    "entry_idt": datetime.fromtimestamp(bos_ts, tz=IL_TZ).strftime("%H:%M"),
                    "lag_min": (bos_ts - peak_ts) // 60,
                    "range_atr": am_range / atr_15m,
                    "am_dir": am_dir,
                })

        # ── Fallback: first lower high in premium, only if BOS didn't fire within timeout ──
        bos_fired_in_time = primary_fired and not primary_late
        if not bos_fired_in_time:
            peak_and_after = [peak_c] + post_peak_all
            lh_result = detect_first_lower_high_in_premium(peak_and_after, am_dir, am_eq)
            if lh_result:
                lh_idx, lh_ts, entry = lh_result
                if entry_ok(entry):
                    # SL above the peak candle (the structural high)
                    sl = make_sl(entry, [peak_c], bullish_exp)
                    fwd = [c for c in post_peak_all if c["timestamp"] > lh_ts]
                    outcome, R = calc_pnl(entry, tp_level, sl, fwd, bullish_exp)
                    fallback_results.append({
                        "date": trade_date, "entry": entry,
                        "tp": tp_level, "sl": sl,
                        "outcome": outcome, "R": R,
                        "peak_idt": peak_idt.strftime("%H:%M"),
                        "entry_idt": datetime.fromtimestamp(lh_ts, tz=IL_TZ).strftime("%H:%M"),
                        "lag_min": (lh_ts - peak_ts) // 60,
                        "range_atr": am_range / atr_15m,
                        "am_dir": am_dir,
                        "bos_fired_late": primary_late,
                    })

    db.close()

    def summary(results, label):
        if not results:
            print(f"\n{label}: 0 signals\n"); return
        n     = len(results)
        wins  = [r for r in results if r["outcome"] == "WIN"]
        losses= [r for r in results if r["outcome"] == "LOSS"]
        opens = [r for r in results if r["outcome"] == "OPEN"]
        closed = n - len(opens)
        wr    = len(wins) / closed if closed else 0
        ev    = sum(r["R"] for r in wins + losses) / closed if closed else 0
        rrs   = [r["R"] for r in wins]
        lags  = [r["lag_min"] for r in results]
        ranges= [r["range_atr"] for r in results]
        median_rr = median(rrs) if rrs else 0
        print(f"\n{'─'*60}")
        print(f"  {label}")
        print(f"  Signals: {n}  |  WIN: {len(wins)}  LOSS: {len(losses)}  OPEN: {len(opens)}")
        print(f"  WR (closed): {wr*100:.0f}%   Median RR: 1:{median_rr:.1f}   EV: {ev:+.2f}R/trade")
        print(f"  Lag after peak: min={min(lags)}m  median={median(lags):.0f}m  max={max(lags)}m")
        print(f"  Range (×ATR15m): min={min(ranges):.1f}  median={median(ranges):.1f}  max={max(ranges):.1f}")
        print(f"{'─'*60}")

    total_days = len(trading_dates) - sum(1 for d in trading_dates if d.weekday() == 0)
    print(f"\nDAX SERPE — Primary BOS vs Fallback Lower-High  ({SCAN_DAYS}-day scan)")
    print(f"Trading days (excl. Monday): {total_days}  |  Skipped (no gate): {skipped}")
    print(f"Peak gate: ≤{PEAK_GATE_H}:{PEAK_GATE_M:02d} IDT  |  BOS timeout: {BOS_TIMEOUT_MIN}m  |  "
          f"TP: origin+{TP_PCT*100:.0f}%  SL: swing+{SL_ATR_MULT}×ATR5m  bearish_only={BEARISH_ONLY}")

    summary(primary_results,  f"PRIMARY  (5m counter-BOS, min_break={MIN_BREAK_DIST_MULT}×ATR5m)")
    summary(fallback_results, f"FALLBACK (first lower high in premium, BOS timeout {BOS_TIMEOUT_MIN}m)")

    combined = primary_results + fallback_results
    summary(combined, "COMBINED (primary + fallback)")

    # ── Per-day detail for fallback signals ──────────────────────────────────
    if fallback_results:
        print(f"\n  FALLBACK signal detail:")
        print(f"  {'Date':10}  {'Dir':5}  {'Peak':5}  {'Entry':5}  {'Lag':>4}  "
              f"{'R×ATR':>5}  {'Entry@':>7}  {'TP@':>7}  {'SL@':>7}  {'Outcome':>7}  {'Note':}")
        for r in sorted(fallback_results, key=lambda x: x["date"]):
            note = "BOS-late" if r["bos_fired_late"] else "no-BOS"
            print(f"  {str(r['date']):10}  {r['am_dir']:5}  {r['peak_idt']:5}  "
                  f"{r['entry_idt']:5}  {r['lag_min']:>3}m  "
                  f"{r['range_atr']:>5.1f}  {r['entry']:>7.1f}  {r['tp']:>7.1f}  "
                  f"{r['sl']:>7.1f}  {r['outcome']:>7}  {note}")

    # ── Days where neither primary nor fallback fired ──────────────────────
    primary_dates  = {r["date"] for r in primary_results}
    fallback_dates = {r["date"] for r in fallback_results}
    covered = primary_dates | fallback_dates

    # Rebuild eligible days (same filters as main loop)
    eligible = []
    import pytz
    IL_TZ2 = pytz.timezone("Asia/Jerusalem")
    for d in trading_dates:
        if d.weekday() == 0: continue
        am_s = ts_for(d, 9, 0, IL_TZ2)
        am_e = ts_for(d, 12, 30, IL_TZ2)
        am_5m_check = [c for c in all_5m if am_s <= c["timestamp"] <= am_e]
        if len(am_5m_check) < 10: continue
        am_h = max(c["high"] for c in am_5m_check)
        am_l = min(c["low"]  for c in am_5m_check)
        if (am_h - am_l) < 20: continue
        am_o = am_5m_check[0]["open"]
        am_d = "bullish" if (am_h - am_o) >= (am_o - am_l) else "bearish"
        if BEARISH_ONLY and am_d != "bullish": continue
        pk = max(am_5m_check, key=lambda c: c["high"]) if am_d == "bullish" else min(am_5m_check, key=lambda c: c["low"])
        gate_t = ts_for(d, PEAK_GATE_H, PEAK_GATE_M, IL_TZ2)
        if pk["timestamp"] <= gate_t:
            eligible.append(d)

    not_covered = [d for d in eligible if d not in covered]
    print(f"\n  Eligible days (peak gate passed): {len(eligible)}")
    print(f"  Covered by primary or fallback:   {len(covered)}")
    print(f"  Not covered by either:            {len(not_covered)}")
    if not_covered:
        print(f"  Dates: {[str(d) for d in not_covered]}")


if __name__ == "__main__":
    main()
