"""
GER40 Frankfurt open expansion — same logic as NAS100 US open strategy.

Frankfurt Xetra cash open: 07:00 UTC (09:00 CET / 10:00 IDT).
Looks for initial expansion ≥3 bars, 0.2–1.2% range, then Def-D trigger
(first opposite-colour bar after peak). Entry at close, SL at expansion
extreme, TP at EQ (50%).
"""

import csv
import glob
from collections import Counter
from datetime import datetime, timezone, date as date_type
from pathlib import Path

# ── config ───────────────────────────────────────────────────────────────────
OPEN_HOUR    = 7       # 07:00 UTC = Frankfurt Xetra cash open
OPEN_MINUTE  = 0
EXP_BARS     = 12      # scan up to 60 min for expansion peak
MIN_EXP_PCT  = 0.20
MAX_EXP_PCT  = 1.20
RETRACE_PCT  = 0.30    # give-back threshold capping the expansion
MAX_HOLD     = 48      # 4 hours max hold

CSV_PATH = Path(
    glob.glob(
        "/mnt/c/Users/*/AppData/Roaming/MetaQuotes/Terminal/Common/Files/GER40_M5.csv"
    )[0]
)


# ── data loader ──────────────────────────────────────────────────────────────
def load_candles():
    candles = []
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                dt = datetime.strptime(
                    row["datetime_utc"], "%Y.%m.%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
                candles.append({
                    "ts":    dt,
                    "open":  float(row["open"]),
                    "high":  float(row["high"]),
                    "low":   float(row["low"]),
                    "close": float(row["close"]),
                })
            except (KeyError, ValueError):
                continue
    candles.sort(key=lambda c: c["ts"])
    return candles


# ── expansion finder ─────────────────────────────────────────────────────────
def find_expansion(bars):
    b0 = bars[0]
    direction   = "BULL" if b0["close"] > b0["open"] else "BEAR"
    high_so_far = b0["high"]
    low_so_far  = b0["low"]
    exp_end     = 0

    for k in range(1, min(EXP_BARS, len(bars))):
        b = bars[k]
        exp_range = high_so_far - low_so_far
        if direction == "BULL":
            if b["high"] > high_so_far:
                high_so_far = b["high"]; exp_end = k
            elif exp_range > 0 and b["high"] < high_so_far - exp_range * RETRACE_PCT:
                break
        else:
            if b["low"] < low_so_far:
                low_so_far = b["low"]; exp_end = k
            elif exp_range > 0 and b["low"] > low_so_far + exp_range * RETRACE_PCT:
                break

    if exp_end < 2:
        return None

    exp_range = high_so_far - low_so_far
    mid_price  = (high_so_far + low_so_far) / 2
    exp_pct    = exp_range / mid_price * 100

    if not (MIN_EXP_PCT <= exp_pct < MAX_EXP_PCT):
        return None

    return {
        "direction": direction,
        "exp_high":  high_so_far,
        "exp_low":   low_so_far,
        "exp_range": exp_range,
        "exp_pct":   round(exp_pct, 3),
        "mid_price": mid_price,
        "exp_end":   exp_end,
    }


# ── trade simulator ──────────────────────────────────────────────────────────
def simulate(sl, tp, direction, forward_bars):
    """After BULL expansion → SHORT (TP below). After BEAR expansion → LONG (TP above)."""
    for bar in forward_bars[:MAX_HOLD]:
        if direction == "BULL":
            if bar["low"]  <= tp: return "WIN"
            if bar["high"] >= sl: return "LOSS"
        else:
            if bar["high"] >= tp: return "WIN"
            if bar["low"]  <= sl: return "LOSS"
    return "TIMEOUT"


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    print("Loading candles…", flush=True)
    candles = load_candles()
    print(f"  {len(candles):,} bars  {candles[0]['ts'].date()} → {candles[-1]['ts'].date()}")

    by_date = {}
    for i, c in enumerate(candles):
        by_date.setdefault(c["ts"].date(), []).append(i)

    results  = []
    skipped  = 0

    for day, idxs in sorted(by_date.items()):
        if day.weekday() >= 5:   # skip weekends
            continue

        open_idx = next(
            (i for i in idxs
             if candles[i]["ts"].hour == OPEN_HOUR
             and candles[i]["ts"].minute == OPEN_MINUTE),
            None,
        )
        if open_idx is None:
            continue

        bars_from_open = candles[open_idx:]
        if len(bars_from_open) < EXP_BARS + 2:
            continue

        exp = find_expansion(bars_from_open)
        if exp is None:
            skipped += 1
            continue

        trigger_idx = open_idx + exp["exp_end"] + 1
        if trigger_idx >= len(candles):
            continue
        tb = candles[trigger_idx]

        if exp["direction"] == "BULL":
            if tb["close"] >= tb["open"]:
                skipped += 1; continue
            entry = tb["close"]; sl = exp["exp_high"]; sl_b = tb["high"]
        else:
            if tb["close"] <= tb["open"]:
                skipped += 1; continue
            entry = tb["close"]; sl = exp["exp_low"];  sl_b = tb["low"]

        tp = exp["mid_price"]

        if exp["direction"] == "BULL" and not (entry > tp and entry < sl):
            skipped += 1; continue
        if exp["direction"] == "BEAR" and not (entry < tp and entry > sl):
            skipped += 1; continue

        forward = candles[trigger_idx + 1:]
        outcome  = simulate(sl, tp, exp["direction"], forward)

        r        = abs(tp - entry) / abs(sl - entry) if outcome == "WIN" else -1.0
        tp_pct   = abs(tp - entry) / entry * 100
        sl_pct   = abs(sl - entry) / entry * 100
        net_pct  = tp_pct if outcome == "WIN" else -sl_pct

        results.append({
            "date":      day,
            "direction": exp["direction"],
            "exp_pct":   exp["exp_pct"],
            "outcome":   outcome,
            "r":         r,
            "tp_pct":    tp_pct,
            "sl_pct":    sl_pct,
            "net_pct":   net_pct,
        })

    # ── summary ───────────────────────────────────────────────────────────────
    n       = len(results)
    wins    = [r for r in results if r["outcome"] == "WIN"]
    losses  = [r for r in results if r["outcome"] == "LOSS"]
    timeouts= [r for r in results if r["outcome"] == "TIMEOUT"]

    wr      = len(wins) / n * 100 if n else 0
    ev      = (sum(r["r"] for r in wins) - len(losses)) / n if n else 0
    avg_tp  = sum(r["tp_pct"] for r in wins)    / len(wins)   if wins   else 0
    avg_sl  = sum(r["sl_pct"] for r in losses)  / len(losses) if losses else 0
    avg_net = sum(r["net_pct"] for r in results) / n if n else 0

    print(f"\nSetups found: {n}  |  skipped/no-defd: {skipped}")
    print(f"\n{'─'*44}")
    print(f"  GER40 Frankfurt open (07:00 UTC)  —  {n} trades")
    print(f"  WR:            {wr:.1f}%  ({len(wins)}W / {len(losses)}L / {len(timeouts)}T)")
    print(f"  EV:            {ev:+.3f}R")
    print(f"  Avg win:       +{avg_tp:.3f}%")
    print(f"  Avg loss:      -{avg_sl:.3f}%")
    print(f"  Avg net/trade: {avg_net:+.3f}%")

    for d in ("BULL", "BEAR"):
        sub = [r for r in results if r["direction"] == d]
        if not sub: continue
        sw  = sum(1 for r in sub if r["outcome"] == "WIN")
        ev_d = (sum(r["r"] for r in sub if r["outcome"] == "WIN") - sum(1 for r in sub if r["outcome"] == "LOSS")) / len(sub)
        print(f"    {d}: {sw}/{len(sub)} ({sw/len(sub)*100:.0f}% WR)  EV={ev_d:+.3f}R")

    # ── frequency ─────────────────────────────────────────────────────────────
    if results:
        first_d = results[0]["date"]; last_d = results[-1]["date"]
        total_days   = (last_d - first_d).days + 1
        trading_days = len([d for d in by_date if first_d <= d <= last_d and d.weekday() < 5])
        by_month     = Counter(r["date"].strftime("%Y-%m") for r in results)
        months       = sorted(by_month)

        print(f"\n{'─'*44}")
        print(f"  Frequency  ({first_d} → {last_d})")
        print(f"  Hit rate:      {n/trading_days*100:.1f}% of trading days")
        print(f"  Avg per week:  {n/(total_days/7):.2f}")
        print(f"  Avg per month: {n/len(months):.1f}")
        print()
        print("  Monthly breakdown:")
        for m in months:
            bar = "█" * by_month[m]
            print(f"    {m}  {bar:15s} {by_month[m]}")


if __name__ == "__main__":
    main()
