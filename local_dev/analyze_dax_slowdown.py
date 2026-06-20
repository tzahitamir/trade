#!/usr/bin/env python3
"""
DAX SERPE slowdown sequence analysis.

For each 5m BOS (C1) that forms the SERPE entry, look at the N candles
immediately before it. A "slowdown" is present if ANY of those candles
shows ANY sign of stalling — loose definition, captures many forms:

  - Small body:       |close - open| <= 0.4 * ATR_5m
  - Inside bar:       high <= prev.high AND low >= prev.low
  - Compressed range: high - low <= 0.6 * ATR_5m

OR the net drift of those N candles combined is tiny:
  - Net churn:        |last.close - first.open| <= 0.7 * ATR_5m

Tests both N=2 and N=3 lookback. Compares WR/EV with vs without slowdown.
"""
import sys, math, statistics, logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from db.local_db import LocalDB
from analysis.smc_analyzer import SMCAnalyzer
from main import _dax_session_window, _evaluate_dax_outcome

logging.basicConfig(level=logging.WARNING)

DB_PATH   = str(Path(__file__).resolve().parents[1] / "src" / "data" / "trade.db")
SCAN_DAYS = 70

# Slowdown thresholds
BODY_ATR_MULT  = 0.40   # small body: candle body ≤ 40% of ATR_5m
RANGE_ATR_MULT = 0.60   # compressed range: high-low ≤ 60% of ATR_5m
CHURN_ATR_MULT = 0.70   # net drift over N candles ≤ 70% of ATR_5m


def is_stall_candle(c, prev_c, atr):
    """True if candle shows any sign of stalling."""
    body  = abs(c["close"] - c["open"])
    rng   = c["high"] - c["low"]
    small_body       = body <= BODY_ATR_MULT  * atr
    compressed_range = rng  <= RANGE_ATR_MULT * atr
    inside_bar       = (prev_c is not None and
                        c["high"] <= prev_c["high"] and
                        c["low"]  >= prev_c["low"])
    return small_body or compressed_range or inside_bar


def has_slowdown(pre_bos_candles, atr, n, strict=False):
    """
    Check for slowdown in the last n candles before the BOS candle.

    loose  (strict=False): ANY stall candle OR net churn qualifies
    strict (strict=True):  AT LEAST 2 stall candles required (no churn shortcut)
    """
    window = pre_bos_candles[-n:]
    if len(window) < n:
        return False

    stall_count = sum(
        1 for i, c in enumerate(window)
        if is_stall_candle(c, window[i - 1] if i > 0 else None, atr)
    )

    if strict:
        return stall_count >= 2

    # loose: any stall candle OR net churn
    if stall_count >= 1:
        return True
    net = abs(window[-1]["close"] - window[0]["open"])
    return net <= CHURN_ATR_MULT * atr


def median(lst):
    if not lst: return float("nan")
    s = sorted(lst); n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def ev(wins, n, rrs):
    if not rrs or n == 0: return float("nan")
    return (wins / n) * statistics.mean(rrs) - (1 - wins / n)


def report(label, sigs):
    n = len(sigs)
    if n == 0:
        print(f"    {label:45s}  n=0")
        return
    wins = sum(1 for s in sigs if s["outcome"] == "WIN")
    rrs  = [s["eff_r"] for s in sigs if s["outcome"] in ("WIN", "LOSS")]
    wr   = wins / n
    ev_v = ev(wins, n, rrs) if rrs else float("nan")
    rr_m = median([s["eff_r"] for s in sigs if s["outcome"] == "WIN"] or [0])
    print(f"    {label:45s}  n={n:>2}  WR={wr*100:5.1f}%  avgR={statistics.mean(rrs) if rrs else 0:.2f}"
          f"  EV={ev_v:+.3f}R  rr_win_med={rr_m:.2f}")


def main():
    db       = LocalDB(DB_PATH)
    analyzer = SMCAnalyzer()

    cutoff_ts = int((datetime.now(timezone.utc) - timedelta(days=SCAN_DAYS)).timestamp())

    candles_15m_desc = db.query_recent("DAX", "15m", limit=2000)
    candles_5m_desc  = db.query_recent("DAX", "5m",  limit=6000)
    if not candles_15m_desc or not candles_5m_desc:
        print("No DAX data"); return

    candles_15m = list(reversed(candles_15m_desc))
    candles_5m  = list(reversed(candles_5m_desc))

    # Collect all trading dates in range
    from datetime import date
    import pytz
    IL_TZ = pytz.timezone("Asia/Jerusalem")

    seen_dates = set()
    for c in candles_15m:
        dt = datetime.fromtimestamp(c["timestamp"], tz=IL_TZ).date()
        if dt.weekday() < 5 and c["timestamp"] >= cutoff_ts:
            seen_dates.add(dt)
    trading_dates = sorted(seen_dates)

    print(f"DAX SERPE — Slowdown sequence analysis")
    print(f"Scan: {len(trading_dates)} trading days  |  "
          f"Thresholds: body≤{BODY_ATR_MULT}×ATR  range≤{RANGE_ATR_MULT}×ATR  churn≤{CHURN_ATR_MULT}×ATR")
    print()

    records = []

    for trade_date in trading_dates:
        start_ts, end_ts = _dax_session_window(trade_date)

        sess_15m = sorted([c for c in candles_15m
                           if start_ts <= c["timestamp"] <= end_ts],
                          key=lambda c: c["timestamp"])
        if len(sess_15m) < 3:
            continue

        pre_15m = sorted([c for c in candles_15m if c["timestamp"] < start_ts],
                         key=lambda c: c["timestamp"])[-16:]
        day_5m  = sorted([c for c in candles_5m if c["timestamp"] >= start_ts],
                         key=lambda c: c["timestamp"])

        # Base params (tp0.5, the no-filter baseline)
        try:
            sigs = analyzer.detect_dax_session_setup(
                sess_15m, day_5m,
                params={"tp_pct": 0.5, "sl_atr_mult": 0.5,
                        "min_expansion_atr": 1.0, "entry_zone_min_pct": 0.5,
                        "swing_lookback_5m": 12, "min_break_str_5m": 0.3,
                        "symbol": "DAX"},
                candles_15m_presession=pre_15m,
            )
        except Exception:
            continue

        if not sigs:
            continue

        sig = sigs[0]
        bos_ts   = sig["breakout_ts"]
        peak_ts  = sig["peak_ts"]

        # Reconstruct post-peak 5m candles
        post_peak_5m = [c for c in day_5m if c["timestamp"] >= peak_ts]

        # Find the BOS candle in post_peak_5m
        bos_idx = next((i for i, c in enumerate(post_peak_5m)
                        if c["timestamp"] == bos_ts), None)
        if bos_idx is None or bos_idx < 3:
            # Can't get enough pre-BOS candles
            outcome, eff_r = _evaluate_dax_outcome(sig, post_peak_5m[bos_idx+1:] if bos_idx else [])
            if outcome == "OPEN":
                continue
            records.append({
                "date": trade_date, "outcome": outcome, "eff_r": eff_r,
                "sd2": None, "sd3": None, "sd2s": None, "sd3s": None,
                "bos_ts": bos_ts,
                "peak_min": None, "bos_min": None, "exp_range": None, "atr_ratio": None,
            })
            continue

        # ATR over the 20 5m candles ending just before the BOS
        atr_window = post_peak_5m[max(0, bos_idx - 20): bos_idx]
        atr_5m = analyzer.calculate_atr(list(reversed(atr_window))) if len(atr_window) >= 5 else None
        if not atr_5m:
            continue

        # Candles before the BOS candle
        pre_bos = post_peak_5m[:bos_idx]

        sd2_loose  = has_slowdown(pre_bos, atr_5m, n=2, strict=False)
        sd3_loose  = has_slowdown(pre_bos, atr_5m, n=3, strict=False)
        sd2_strict = has_slowdown(pre_bos, atr_5m, n=2, strict=True)
        sd3_strict = has_slowdown(pre_bos, atr_5m, n=3, strict=True)

        # Evaluate outcome from BOS candle onward
        post_bos_5m = post_peak_5m[bos_idx + 1:]
        outcome, eff_r = _evaluate_dax_outcome(sig, post_bos_5m)
        if outcome == "OPEN":
            continue

        peak_offset_min = (sig["peak_ts"]  - start_ts) / 60
        bos_offset_min  = (bos_ts         - start_ts) / 60
        exp_range       = sig.get("expansion_range", 0)
        atr_ratio       = exp_range / atr_5m if atr_5m else 0

        records.append({
            "date": trade_date, "outcome": outcome, "eff_r": eff_r,
            "sd2":  sd2_loose,  "sd3":  sd3_loose,
            "sd2s": sd2_strict, "sd3s": sd3_strict,
            "bos_ts": bos_ts,
            "peak_min": peak_offset_min,
            "bos_min":  bos_offset_min,
            "exp_range": exp_range,
            "atr_ratio": atr_ratio,
        })

    print(f"Resolved signals: {len(records)}")
    print()

    def f(v): return "Y" if v else ("N" if v is False else "?")

    # --- Full breakdown ---
    print("  ── Baseline (all resolved) ─────────────────────────────────────────────")
    report("all signals", records)
    print()

    print("  ── Loose: ANY stall candle or net churn ────────────────────────────────")
    print("  N=2")
    report("  slowdown present (sd2 loose)",  [r for r in records if r["sd2"] is True])
    report("  no slowdown      (sd2 loose)",  [r for r in records if r["sd2"] is False])
    print("  N=3")
    report("  slowdown present (sd3 loose)",  [r for r in records if r["sd3"] is True])
    report("  no slowdown      (sd3 loose)",  [r for r in records if r["sd3"] is False])
    print()

    print("  ── Strict: ≥2 stall candles required ──────────────────────────────────")
    print("  N=2")
    report("  slowdown present (sd2 strict)", [r for r in records if r["sd2s"] is True])
    report("  no slowdown      (sd2 strict)", [r for r in records if r["sd2s"] is False])
    print("  N=3")
    report("  slowdown present (sd3 strict)", [r for r in records if r["sd3s"] is True])
    report("  no slowdown      (sd3 strict)", [r for r in records if r["sd3s"] is False])
    print()

    # --- Coverage summary ---
    print("  ── Coverage (how many signals each filter captures) ────────────────────")
    n = len(records)
    for key, label in [("sd2","loose N=2"),("sd3","loose N=3"),("sd2s","strict N=2"),("sd3s","strict N=3")]:
        cnt = sum(1 for r in records if r[key] is True)
        print(f"    {label:15s}: {cnt}/{n} signals ({cnt/n*100:.0f}%)")
    print()

    # --- Timing analysis ---
    timed = [r for r in records if r["peak_min"] is not None]
    peak_mins = sorted(r["peak_min"] for r in timed)
    bos_mins  = sorted(r["bos_min"]  for r in timed)
    print(f"  Peak range: {min(peak_mins):.0f}–{max(peak_mins):.0f} min after open "
          f"({9+min(peak_mins)/60:.1f}–{9+max(peak_mins)/60:.1f} IDT)")
    print(f"  BOS  range: {min(bos_mins):.0f}–{max(bos_mins):.0f} min after open")
    print()

    print("  ── Peak timing (minutes after session open = 09:00 IDT) ─────────────────")
    for cutoff in [120, 150, 165, 180]:
        idt = f"{9+cutoff//60}:{cutoff%60:02d}"
        early = [r for r in timed if r["peak_min"] <= cutoff]
        late  = [r for r in timed if r["peak_min"] >  cutoff]
        report(f"  peak ≤ {cutoff:3d} min / {idt} IDT", early)
        report(f"  peak >  {cutoff:3d} min / {idt} IDT", late)
        print()

    print("  ── BOS timing (minutes after session open) ──────────────────────────────")
    for cutoff in [240, 300, 360]:
        idt = f"{9+cutoff//60}:{cutoff%60:02d}"
        early = [r for r in timed if r["bos_min"] <= cutoff]
        late  = [r for r in timed if r["bos_min"] >  cutoff]
        report(f"  BOS ≤ {cutoff:3d} min / {idt} IDT", early)
        report(f"  BOS >  {cutoff:3d} min / {idt} IDT", late)
        print()

    print("  ── Expansion size (range as multiple of 5m ATR) ─────────────────────────")
    for cutoff in [15.0, 20.0, 25.0]:
        shallow = [r for r in timed if r["atr_ratio"] <= cutoff]
        deep    = [r for r in timed if r["atr_ratio"] >  cutoff]
        report(f"  exp ≤ {cutoff:.0f}×ATR (shallow)", shallow)
        report(f"  exp >  {cutoff:.0f}×ATR (deep)",   deep)
        print()

    # --- Per-signal detail ---
    print("  ── Per-signal detail ───────────────────────────────────────────────────")
    print(f"    {'Date':12s}  {'Out':5s}  {'EffR':5s}  {'Pk':>5s}  {'BOS':>5s}  {'ExpATR':>6s}  L2  S3")
    print(f"    {'────':12s}  {'───':5s}  {'────':5s}  {'min':>5s}  {'min':>5s}  {'ratio':>6s}  ──  ──")
    for r in sorted(records, key=lambda x: x["date"]):
        pm  = f"{r['peak_min']:.0f}"  if r["peak_min"]  is not None else "?"
        bm  = f"{r['bos_min']:.0f}"   if r["bos_min"]   is not None else "?"
        ar  = f"{r['atr_ratio']:.1f}" if r["atr_ratio"] is not None else "?"
        print(f"    {str(r['date']):12s}  {r['outcome']:5s}  {r['eff_r']:5.2f}"
              f"  {pm:>5s}  {bm:>5s}  {ar:>6s}  {f(r['sd2']):2s}  {f(r['sd3s']):2s}")

    db.close()


if __name__ == "__main__":
    main()
