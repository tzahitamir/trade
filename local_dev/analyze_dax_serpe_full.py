#!/usr/bin/env python3
"""
DAX SERPE full-year analysis (Jun 2025 – Jun 2026).

Uses local DB GER40 5m data (72k candles from MT5 export).
Resamples 5m → 15m internally for BOS detection.
Sweeps key parameters to find optimal settings.

Session window: 09:00–12:30 IDT (= 06:00–09:30 UTC summer)
"""
import sys, math, statistics
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from db.local_db import LocalDB
from analysis.smc_analyzer import SMCAnalyzer

_ISR = ZoneInfo("Asia/Jerusalem")

DB_PATH = str(Path(__file__).resolve().parents[1] / "src" / "data" / "trade.db")

PEAK_CUTOFF = (11, 45)   # IDT — peak must form before this
SKIP_MONDAY = True


# ── Helpers ───────────────────────────────────────────────────────────────────

def session_window(trade_date):
    """09:00–12:30 IDT → UTC timestamps."""
    from datetime import datetime as _dt
    s = _dt(trade_date.year, trade_date.month, trade_date.day, 9, 0, tzinfo=_ISR)
    e = _dt(trade_date.year, trade_date.month, trade_date.day, 12, 30, tzinfo=_ISR)
    return int(s.timestamp()), int(e.timestamp())


def resample_5m_to_15m(candles_5m: list) -> list:
    """Resample 5m candles (oldest→newest) to 15m aligned candles."""
    out = []
    i   = 0
    while i < len(candles_5m):
        ts0 = candles_5m[i]["timestamp"]
        # align to 15m boundary
        aligned = (ts0 // 900) * 900
        group   = [c for c in candles_5m[i:i+3] if c["timestamp"] < aligned + 900]
        if not group:
            i += 1
            continue
        out.append({
            "timestamp": aligned,
            "open":   group[0]["open"],
            "high":   max(c["high"] for c in group),
            "low":    min(c["low"]  for c in group),
            "close":  group[-1]["close"],
            "volume": sum(c.get("volume", 0) for c in group),
        })
        i += len(group)
    return out


def peak_too_late(peak_ts: int) -> bool:
    t = datetime.fromtimestamp(peak_ts, tz=_ISR)
    return (t.hour, t.minute) >= PEAK_CUTOFF


def evaluate(sig: dict, candles_5m_after: list) -> tuple[str, float | None]:
    """Return (outcome, eff_r). outcome: WIN / LOSS / OPEN."""
    tp, sl, entry = sig["tp"], sig["sl"], sig["entry"]
    bull    = sig["direction"] == "bullish"
    risk    = sig["risk"] or abs(tp - sl) or 1.0
    outcome = "OPEN"
    for bar in candles_5m_after:
        if bull:
            if bar["low"]  <= sl: outcome = "LOSS"; break
            if bar["high"] >= tp: outcome = "WIN";  break
        else:
            if bar["high"] >= sl: outcome = "LOSS"; break
            if bar["low"]  <= tp: outcome = "WIN";  break
    eff_r = round(abs(tp - entry) / risk, 2) if outcome == "WIN" else (1.0 if outcome == "LOSS" else None)
    return outcome, eff_r


def summary_stats(outcomes: list) -> dict:
    wins   = [r for o, r in outcomes if o == "WIN"]
    losses = [r for o, r in outcomes if o == "LOSS"]
    opens  = [r for o, r in outcomes if o == "OPEN"]
    n      = len(wins) + len(losses)
    wr     = len(wins) / n if n else 0
    ev     = (sum(wins) - len(losses)) / n if n else 0
    return {
        "n": n, "wins": len(wins), "losses": len(losses), "opens": len(opens),
        "wr": wr, "avg_r": statistics.mean(wins) if wins else 0, "ev": ev,
    }


# ── Load data ─────────────────────────────────────────────────────────────────

print("Loading GER40 5m from DB…")
db      = LocalDB(DB_PATH)
raw_desc = db.query_recent("GER40", "5m", limit=100_000)
db.close()

all5m    = list(reversed(raw_desc))          # oldest→newest
all15m   = resample_5m_to_15m(all5m)

print(f"  5m candles : {len(all5m)}")
print(f"  15m candles: {len(all15m)}")

# All trading dates
dates = sorted(set(
    datetime.fromtimestamp(c["timestamp"], _ISR).date()
    for c in all5m
))
dates = [d for d in dates if d.weekday() < 5]
if SKIP_MONDAY:
    dates = [d for d in dates if d.weekday() != 0]

print(f"  Trading days (Tue–Fri): {len(dates)}\n")

# ── Build per-session data ────────────────────────────────────────────────────

sessions = []
for trade_date in dates:
    start_ts, end_ts = session_window(trade_date)
    lookahead_end    = end_ts + 6 * 3600

    sess_15m = [c for c in all15m if start_ts <= c["timestamp"] <= end_ts]
    pre_15m  = [c for c in all15m if c["timestamp"] < start_ts][-16:]
    day_5m   = [c for c in all5m  if start_ts <= c["timestamp"] <= lookahead_end]

    if len(sess_15m) >= 3 and len(day_5m) >= 6:
        sessions.append((trade_date, start_ts, end_ts, sess_15m, pre_15m, day_5m))

print(f"Sessions with enough data: {len(sessions)}\n")

# ── Parameter sweep ───────────────────────────────────────────────────────────

analyzer = SMCAnalyzer()

TP_PCTS        = [0.40, 0.50, 0.55, 0.60, 0.70, 0.80, 1.00]
ENTRY_ZONES    = [0.50, 0.60, 0.70, 0.80]
EXP_ATRS       = [0.75, 1.00, 1.50]
SL_MULTS       = [0.25, 0.50, 0.75]

# ── Main scan with current gold params ───────────────────────────────────────

GOLD = {
    "tp_pct":             0.55,
    "sl_atr_mult":        0.50,
    "min_expansion_atr":  1.00,
    "entry_zone_min_pct": 0.70,
    "symbol":             "GER40",
}

print("=" * 64)
print("SECTION 1 — Gold params (current live: tp=55%, ez=70%, exp≥1×ATR)")
print("=" * 64)

gold_outcomes    = []          # (outcome, eff_r)
gold_by_dir      = defaultdict(list)
gold_by_dow      = defaultdict(list)
gold_by_month    = defaultdict(list)
gold_by_peak_h   = defaultdict(list)
gold_per_day     = []          # (date, direction, peak_idt, entry, tp, sl, outcome)

for trade_date, start_ts, end_ts, sess_15m, pre_15m, day_5m in sessions:
    signals = analyzer.detect_dax_session_setup(
        sess_15m, day_5m,
        params=GOLD,
        candles_15m_presession=pre_15m,
    )
    for sig in signals:
        if peak_too_late(sig["peak_ts"]):
            continue
        post = [c for c in day_5m if c["timestamp"] > sig["breakout_ts"]]
        outcome, eff_r = evaluate(sig, post)

        peak_idt = datetime.fromtimestamp(sig["peak_ts"], tz=_ISR)
        peak_h   = f"{peak_idt.hour:02d}:{peak_idt.minute:02d}"
        month    = trade_date.strftime("%Y-%m")
        dow      = trade_date.strftime("%a")

        gold_outcomes.append((outcome, eff_r))
        gold_by_dir[sig["expansion_dir"]].append((outcome, eff_r))
        gold_by_dow[dow].append((outcome, eff_r))
        gold_by_month[month].append((outcome, eff_r))
        gold_by_peak_h[peak_idt.hour].append((outcome, eff_r))
        gold_per_day.append((trade_date, sig["expansion_dir"], peak_h,
                             sig["entry"], sig["tp"], sig["sl"], outcome,
                             eff_r, sig["expansion_range"], sig.get("entry_pct_from_origin", 0)))

s = summary_stats(gold_outcomes)
print(f"\nOverall: {s['n']} trades | WR {s['wr']:.0%} | avg_R {s['avg_r']:.2f} | EV {s['ev']:+.2f}R"
      f" | open {s['opens']}")

print("\nBy expansion direction:")
for d in ["bullish", "bearish"]:
    if gold_by_dir[d]:
        s2 = summary_stats(gold_by_dir[d])
        label = "Bull exp → SHORT" if d == "bullish" else "Bear exp → LONG"
        print(f"  {label:22s}: {s2['n']:3d} trades | WR {s2['wr']:.0%} | EV {s2['ev']:+.2f}R")

print("\nBy day of week:")
for dow in ["Tue","Wed","Thu","Fri"]:
    if gold_by_dow[dow]:
        s2 = summary_stats(gold_by_dow[dow])
        print(f"  {dow}: {s2['n']:3d} trades | WR {s2['wr']:.0%} | EV {s2['ev']:+.2f}R")

print("\nBy peak hour (IDT):")
for h in sorted(gold_by_peak_h):
    s2 = summary_stats(gold_by_peak_h[h])
    print(f"  {h:02d}:xx IDT  {s2['n']:3d} trades | WR {s2['wr']:.0%} | EV {s2['ev']:+.2f}R")

print("\nBy month:")
for m in sorted(gold_by_month):
    s2 = summary_stats(gold_by_month[m])
    bar = "W" * s2["wins"] + "L" * s2["losses"]
    print(f"  {m}: {s2['n']:3d} | WR {s2['wr']:.0%} | EV {s2['ev']:+.2f}R  {bar}")

# ── Per-day detail ────────────────────────────────────────────────────────────
print("\n--- Per-day detail ---")
print(f"{'Date':<12} {'Dir':>6} {'Peak':>6} {'Entry':>7} {'TP':>7} {'SL':>7}  "
      f"{'Range':>6} {'Ent%':>5} {'Outcome'}")
for (td, exp_dir, peak_h, entry, tp, sl, outcome, eff_r, exp_range, entry_pct) in gold_per_day:
    mark   = "✓" if outcome == "WIN" else ("✗" if outcome == "LOSS" else "~")
    dir_s  = "↑bull" if exp_dir == "bullish" else "↓bear"
    r_str  = f"  {eff_r:.2f}R" if eff_r else ""
    print(f"{str(td):<12} {dir_s:>6} {peak_h:>6} {entry:>7.0f} {tp:>7.0f} {sl:>7.0f}"
          f"  {exp_range:>6.0f} {entry_pct:>4.0%}  {mark} {outcome}{r_str}")

# ── Section 2: TP sweep ───────────────────────────────────────────────────────

print("\n\n" + "=" * 64)
print("SECTION 2 — TP% sweep (entry_zone=70%, exp≥1×ATR, sl=0.5×ATR)")
print("=" * 64)
print(f"\n{'TP%':>5} {'N':>4} {'WR':>6} {'avg_R':>6} {'EV':>6}")

for tp_pct in TP_PCTS:
    params = {**GOLD, "tp_pct": tp_pct}
    oc = []
    for trade_date, start_ts, end_ts, sess_15m, pre_15m, day_5m in sessions:
        sigs = analyzer.detect_dax_session_setup(sess_15m, day_5m, params=params,
                                                  candles_15m_presession=pre_15m)
        for sig in sigs:
            if peak_too_late(sig["peak_ts"]):
                continue
            post = [c for c in day_5m if c["timestamp"] > sig["breakout_ts"]]
            oc.append(evaluate(sig, post))
    s = summary_stats(oc)
    print(f" {tp_pct*100:>4.0f}%  {s['n']:>4}  {s['wr']:>5.0%}  {s['avg_r']:>5.2f}  {s['ev']:>+5.2f}R")

# ── Section 3: Entry zone sweep ───────────────────────────────────────────────

print("\n\n" + "=" * 64)
print("SECTION 3 — Entry zone sweep (tp=55%, exp≥1×ATR, sl=0.5×ATR)")
print("=" * 64)
print(f"\n{'EZ%':>5} {'N':>4} {'WR':>6} {'avg_R':>6} {'EV':>6}")

for ez in ENTRY_ZONES:
    params = {**GOLD, "entry_zone_min_pct": ez}
    oc = []
    for trade_date, start_ts, end_ts, sess_15m, pre_15m, day_5m in sessions:
        sigs = analyzer.detect_dax_session_setup(sess_15m, day_5m, params=params,
                                                  candles_15m_presession=pre_15m)
        for sig in sigs:
            if peak_too_late(sig["peak_ts"]):
                continue
            post = [c for c in day_5m if c["timestamp"] > sig["breakout_ts"]]
            oc.append(evaluate(sig, post))
    s = summary_stats(oc)
    print(f" {ez*100:>4.0f}%  {s['n']:>4}  {s['wr']:>5.0%}  {s['avg_r']:>5.2f}  {s['ev']:>+5.2f}R")

# ── Section 4: Min expansion ATR sweep ───────────────────────────────────────

print("\n\n" + "=" * 64)
print("SECTION 4 — Min expansion size sweep (tp=55%, ez=70%, sl=0.5×ATR)")
print("=" * 64)
print(f"\n{'minExp':>7} {'N':>4} {'WR':>6} {'avg_R':>6} {'EV':>6}")

for exp in EXP_ATRS:
    params = {**GOLD, "min_expansion_atr": exp}
    oc = []
    for trade_date, start_ts, end_ts, sess_15m, pre_15m, day_5m in sessions:
        sigs = analyzer.detect_dax_session_setup(sess_15m, day_5m, params=params,
                                                  candles_15m_presession=pre_15m)
        for sig in sigs:
            if peak_too_late(sig["peak_ts"]):
                continue
            post = [c for c in day_5m if c["timestamp"] > sig["breakout_ts"]]
            oc.append(evaluate(sig, post))
    s = summary_stats(oc)
    print(f" {exp:>6.2f}×  {s['n']:>4}  {s['wr']:>5.0%}  {s['avg_r']:>5.2f}  {s['ev']:>+5.2f}R")

# ── Section 5: SL mult sweep ──────────────────────────────────────────────────

print("\n\n" + "=" * 64)
print("SECTION 5 — SL ATR multiplier sweep (tp=55%, ez=70%, exp≥1×ATR)")
print("=" * 64)
print(f"\n{'SL_mult':>8} {'N':>4} {'WR':>6} {'avg_R':>6} {'EV':>6}")

for sl_m in SL_MULTS:
    params = {**GOLD, "sl_atr_mult": sl_m}
    oc = []
    for trade_date, start_ts, end_ts, sess_15m, pre_15m, day_5m in sessions:
        sigs = analyzer.detect_dax_session_setup(sess_15m, day_5m, params=params,
                                                  candles_15m_presession=pre_15m)
        for sig in sigs:
            if peak_too_late(sig["peak_ts"]):
                continue
            post = [c for c in day_5m if c["timestamp"] > sig["breakout_ts"]]
            oc.append(evaluate(sig, post))
    s = summary_stats(oc)
    print(f" {sl_m:>7.2f}×  {s['n']:>4}  {s['wr']:>5.0%}  {s['avg_r']:>5.2f}  {s['ev']:>+5.2f}R")

# ── Section 6: Peak gate sweep ────────────────────────────────────────────────

print("\n\n" + "=" * 64)
print("SECTION 6 — Peak gate sweep (gold params, vary cutoff hour IDT)")
print("=" * 64)
print(f"\n{'Gate':>8} {'N':>4} {'WR':>6} {'EV':>6}")

for cutoff_h in [10, 11, 12]:
    for cutoff_m in [0, 30]:
        oc = []
        for trade_date, start_ts, end_ts, sess_15m, pre_15m, day_5m in sessions:
            sigs = analyzer.detect_dax_session_setup(sess_15m, day_5m, params=GOLD,
                                                      candles_15m_presession=pre_15m)
            for sig in sigs:
                pt = datetime.fromtimestamp(sig["peak_ts"], tz=_ISR)
                if (pt.hour, pt.minute) >= (cutoff_h, cutoff_m):
                    continue
                post = [c for c in day_5m if c["timestamp"] > sig["breakout_ts"]]
                oc.append(evaluate(sig, post))
        s = summary_stats(oc)
        print(f" <{cutoff_h:02d}:{cutoff_m:02d} IDT  {s['n']:>4}  {s['wr']:>5.0%}  {s['ev']:>+5.2f}R")

# ── Section 7: Best combo ─────────────────────────────────────────────────────

print("\n\n" + "=" * 64)
print("SECTION 7 — Top parameter combos (grid search, bull+bear, peak<11:45)")
print("=" * 64)

results = []
for tp_pct in [0.50, 0.55, 0.60, 0.65]:
    for ez in [0.60, 0.70, 0.80]:
        for exp in [1.00, 1.50]:
            params = {**GOLD, "tp_pct": tp_pct, "entry_zone_min_pct": ez, "min_expansion_atr": exp}
            oc = []
            for trade_date, start_ts, end_ts, sess_15m, pre_15m, day_5m in sessions:
                sigs = analyzer.detect_dax_session_setup(sess_15m, day_5m, params=params,
                                                          candles_15m_presession=pre_15m)
                for sig in sigs:
                    if peak_too_late(sig["peak_ts"]):
                        continue
                    post = [c for c in day_5m if c["timestamp"] > sig["breakout_ts"]]
                    oc.append(evaluate(sig, post))
            s = summary_stats(oc)
            if s["n"] >= 10:
                results.append((s["ev"], s["wr"], s["n"], tp_pct, ez, exp, s))

results.sort(reverse=True)
print(f"\n{'EV':>6}  {'WR':>5}  {'N':>4}  {'TP%':>5}  {'EZ%':>5}  {'minExp':>7}")
for ev, wr, n, tp_pct, ez, exp, s in results[:15]:
    print(f" {ev:>+5.2f}R  {wr:>4.0%}  {n:>4}  {tp_pct*100:>4.0f}%  {ez*100:>4.0f}%  {exp:>6.2f}×")

print("\nDone.")
