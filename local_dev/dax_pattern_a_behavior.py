#!/usr/bin/env python3
"""
DAX Pattern A — post-sweep price behavior analysis.

Instead of asking "did price hit TP/SL?", ask:
  - What did price actually DO after the sweep?
  - Does wick size / sweep time / range size predict behavior?
  - Is there a repeating pattern we can exploit?

Behavior categories (mutually exclusive):
  FULL_REVERSAL  — price reached the opposite end of morning range
  EQ_ONLY        — price reached midpoint but not opposite end
  STALL          — price stayed between entry and midpoint
  CONTINUATION   — price broke back beyond the sweep extreme

Cross-tabbed by:
  • wick extension size (0–5, 5–10, 10–20, 20+ pts)
  • sweep time bucket (11:00–11:30, 11:30–12:00, 12:00–12:30, 12:30–13:30 UTC)
  • morning range size (<0.5×ATR, 0.5–1×ATR, 1–2×ATR, >2×ATR)
"""

import sys, statistics
from pathlib import Path
from datetime import datetime, timezone, date
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from db.local_db import LocalDB

DB_PATH = str(Path(__file__).resolve().parents[1] / "src" / "data" / "trade.db")
_UTC = timezone.utc

# ── Time constants (UTC) ──────────────────────────────────────────────────────
MORNING_H,    MORNING_M    =  7,  0
RANGE_END_H,  RANGE_END_M  = 11,  0   # end of morning range
SWEEP_END_H,  SWEEP_END_M  = 13, 30   # end of sweep window
EVAL_END_H,   EVAL_END_M   = 15, 30   # Frankfurt close

MIN_MORNING_BARS = 6
MIN_RANGE_ATR    = 0.5   # filter tiny days


def _ts(d: date, h: int, m: int) -> int:
    return int(datetime(d.year, d.month, d.day, h, m, tzinfo=_UTC).timestamp())


def _atr(bars, period=14):
    if len(bars) < 2:
        return 0.0
    trs = [max(b["high"] - b["low"],
               abs(b["high"] - bars[i-1]["close"]),
               abs(b["low"]  - bars[i-1]["close"]))
           for i, b in enumerate(bars[1:], 1)]
    return statistics.mean(trs[-period:]) if trs else 0.0


def find_all_sweeps(bars_sweep, morning_high, morning_low, min_wick=0):
    """Return list of all sweeps (not just first) for timing analysis."""
    sweeps = []
    for bar in bars_sweep:
        bear = bar["high"] > morning_high and bar["close"] <= morning_high
        bull = bar["low"]  < morning_low  and bar["close"] >= morning_low
        if bear:
            sweeps.append(("bear", bar, bar["high"] - morning_high))
        elif bull:
            sweeps.append(("bull", bar, morning_low - bar["low"]))
    return sweeps


def analyze_post_sweep(direction, sweep_bar, morning_high, morning_low, all5m_day, eval_end_ts):
    """
    Track price behavior after the sweep candle until 15:30 UTC.
    Returns a rich dict of what happened.
    """
    eq_level = (morning_high + morning_low) / 2
    range_size = morning_high - morning_low or 1.0
    is_short = direction == "bear"

    # bars AFTER the sweep candle (not the sweep candle itself)
    post = [b for b in all5m_day
            if b["timestamp"] > sweep_bar["timestamp"]
            and b["timestamp"] <= eval_end_ts]

    reached_eq          = False
    reached_opposite    = False
    reached_continuation = False
    eq_bars             = None
    opposite_bars       = None
    continuation_bars   = None
    max_retrace_pct     = 0.0   # 0=entry, 1=opposite end
    max_continuation_pts = 0.0  # pts beyond sweep extreme

    sweep_extreme = sweep_bar["high"] if is_short else sweep_bar["low"]
    entry_price   = sweep_bar["close"]

    for i, b in enumerate(post):
        if is_short:
            # we want DOWN; DOWN means lower prices
            low_retrace = (entry_price - b["low"]) / range_size
            max_retrace_pct = max(max_retrace_pct, low_retrace)
            cont_dist = b["high"] - sweep_extreme
            max_continuation_pts = max(max_continuation_pts, max(0, cont_dist))

            if not reached_eq and b["low"] <= eq_level:
                reached_eq = True; eq_bars = i + 1
            if not reached_opposite and b["low"] <= morning_low:
                reached_opposite = True; opposite_bars = i + 1
            if not reached_continuation and b["high"] > sweep_extreme:
                reached_continuation = True; continuation_bars = i + 1
        else:
            # we want UP
            up_retrace = (b["high"] - entry_price) / range_size
            max_retrace_pct = max(max_retrace_pct, up_retrace)
            cont_dist = sweep_extreme - b["low"]
            max_continuation_pts = max(max_continuation_pts, max(0, cont_dist))

            if not reached_eq and b["high"] >= eq_level:
                reached_eq = True; eq_bars = i + 1
            if not reached_opposite and b["high"] >= morning_high:
                reached_opposite = True; opposite_bars = i + 1
            if not reached_continuation and b["low"] < sweep_extreme:
                reached_continuation = True; continuation_bars = i + 1

    # Assign category (priority: continuation > reversal > eq > stall)
    # Rationale: if price first hit continuation AND then reversed, it's still CONTINUATION
    # because the reversal setup was false — we'd have been stopped out first.
    if reached_continuation and (continuation_bars or 999) < (opposite_bars or 999):
        category = "CONTINUATION"
    elif reached_opposite:
        category = "FULL_REVERSAL"
    elif reached_eq:
        category = "EQ_ONLY"
    else:
        category = "STALL"

    return {
        "category":              category,
        "reached_eq":            reached_eq,
        "reached_opposite":      reached_opposite,
        "reached_continuation":  reached_continuation,
        "eq_bars":               eq_bars,
        "opposite_bars":         opposite_bars,
        "continuation_bars":     continuation_bars,
        "max_retrace_pct":       max_retrace_pct,
        "max_continuation_pts":  max_continuation_pts,
        "n_post_bars":           len(post),
    }


# ── Load data ─────────────────────────────────────────────────────────────────

print("Loading GER40 5m data …")
db = LocalDB(DB_PATH)
all5m_desc = db.query_recent("GER40", "5m", limit=130_000)
all5m = list(reversed(all5m_desc))
print(f"  {len(all5m)} bars")

dates = sorted(set(datetime.fromtimestamp(c["timestamp"], tz=_UTC).date() for c in all5m))
dates = [d for d in dates if d.weekday() < 5]

# ── Build session data ────────────────────────────────────────────────────────

print("Building sessions …")
records = []   # one record per sweep event

for d in dates:
    ts_morn_start = _ts(d, MORNING_H,   MORNING_M)
    ts_range_end  = _ts(d, RANGE_END_H, RANGE_END_M)
    ts_sweep_end  = _ts(d, SWEEP_END_H, SWEEP_END_M)
    ts_eval_end   = _ts(d, EVAL_END_H,  EVAL_END_M)

    morning_bars = [c for c in all5m if ts_morn_start <= c["timestamp"] < ts_range_end]
    sweep_bars   = [c for c in all5m if ts_range_end  <= c["timestamp"] < ts_sweep_end]
    day5m        = [c for c in all5m if ts_morn_start <= c["timestamp"] <= ts_eval_end]

    if len(morning_bars) < MIN_MORNING_BARS:
        continue

    morning_high = max(c["high"]  for c in morning_bars)
    morning_low  = min(c["low"]   for c in morning_bars)
    morning_range = morning_high - morning_low
    atr_val      = _atr(morning_bars) or 20.0

    if morning_range < MIN_RANGE_ATR * atr_val:
        continue

    sweeps = find_all_sweeps(sweep_bars, morning_high, morning_low)
    # Only take first sweep per day (same as trading)
    if not sweeps:
        continue

    direction, sweep_bar, wick_ext = sweeps[0]
    sweep_dt = datetime.fromtimestamp(sweep_bar["timestamp"], tz=_UTC)

    behavior = analyze_post_sweep(
        direction, sweep_bar, morning_high, morning_low, day5m, ts_eval_end
    )

    records.append({
        "date":           d,
        "direction":      direction,
        "sweep_hhmm":     sweep_dt.strftime("%H:%M"),
        "sweep_hour":     sweep_dt.hour,
        "sweep_minute":   sweep_dt.hour * 60 + sweep_dt.minute,
        "wick_ext":       wick_ext,
        "morning_range":  morning_range,
        "atr":            atr_val,
        "range_atr_ratio": morning_range / atr_val,
        **behavior,
    })

print(f"  {len(records)} sweep events\n")


# ── Helper: cross-tab ─────────────────────────────────────────────────────────

CATS = ["FULL_REVERSAL", "EQ_ONLY", "STALL", "CONTINUATION"]
CAT_LABELS = {
    "FULL_REVERSAL": "Full rev",
    "EQ_ONLY":       "EQ only ",
    "STALL":         "Stall   ",
    "CONTINUATION":  "Cont.   ",
}

def print_crosstab(title, groups, records):
    """Print category distribution for each group."""
    print(f"\n{'─'*68}")
    print(f"  {title}")
    print(f"{'─'*68}")
    header = f"  {'Group':<20}  {'N':>4}  " + \
             "  ".join(f"{CAT_LABELS[c][:8]:>8}" for c in CATS) + \
             "  {'max_ret%':>8}  {'cont_pts':>8}"
    print(header)
    print(f"  {'─'*65}")

    for label, mask in groups:
        subset = [r for r in records if mask(r)]
        if not subset:
            continue
        n = len(subset)
        counts = {c: sum(1 for r in subset if r["category"] == c) for c in CATS}
        pcts   = {c: counts[c] / n for c in CATS}
        avg_ret  = statistics.mean(r["max_retrace_pct"]     for r in subset)
        avg_cont = statistics.mean(r["max_continuation_pts"] for r in subset)
        row = f"  {label:<20}  {n:>4}  "
        row += "  ".join(f"{pcts[c]:>7.0%} " for c in CATS)
        row += f"  {avg_ret:>8.0%}  {avg_cont:>8.1f}"
        print(row)


def print_timing_bars(title, key_fn, records, width=40):
    """Show sweep timing distribution with category stacked bars."""
    from collections import Counter
    buckets = defaultdict(list)
    for r in records:
        buckets[key_fn(r)].append(r["category"])

    print(f"\n{'─'*68}")
    print(f"  {title}")
    print(f"  (full=■  eq=□  stall=·  cont=▲)")
    print(f"{'─'*68}")
    symbols = {"FULL_REVERSAL": "■", "EQ_ONLY": "□", "STALL": "·", "CONTINUATION": "▲"}

    for k in sorted(buckets):
        cats = buckets[k]
        n = len(cats)
        bar = "".join(symbols[c] for c in cats)
        full_pct = sum(1 for c in cats if c == "FULL_REVERSAL") / n
        eq_pct   = sum(1 for c in cats if c == "EQ_ONLY")       / n
        cont_pct = sum(1 for c in cats if c == "CONTINUATION")   / n
        print(f"  {str(k):<8}  {bar:<{width}}  n={n:>3}  "
              f"full={full_pct:.0%} eq={eq_pct:.0%} cont={cont_pct:.0%}")


# ── OVERALL DISTRIBUTION ──────────────────────────────────────────────────────

print("=" * 68)
print("POST-SWEEP BEHAVIOR — OVERALL")
print("=" * 68)
n = len(records)
for c in CATS:
    cnt = sum(1 for r in records if r["category"] == c)
    bar = "█" * cnt
    print(f"  {CAT_LABELS[c]}: {cnt:>3} ({cnt/n:.0%})  {bar}")

avg_ret  = statistics.mean(r["max_retrace_pct"]     for r in records)
avg_cont = statistics.mean(r["max_continuation_pts"] for r in records)
print(f"\n  Avg max retrace toward opposite: {avg_ret:.0%} of morning range")
print(f"  Avg max continuation beyond wick: {avg_cont:.1f} pts")


# ── WICK SIZE CROSS-TAB ───────────────────────────────────────────────────────

wick_groups = [
    ("wick  0– 5 pts", lambda r: r["wick_ext"] <  5),
    ("wick  5–10 pts", lambda r:  5 <= r["wick_ext"] < 10),
    ("wick 10–20 pts", lambda r: 10 <= r["wick_ext"] < 20),
    ("wick 20–30 pts", lambda r: 20 <= r["wick_ext"] < 30),
    ("wick 30+   pts", lambda r: r["wick_ext"] >= 30),
]
print_crosstab("BY WICK EXTENSION SIZE", wick_groups, records)


# ── SWEEP TIME CROSS-TAB ─────────────────────────────────────────────────────

time_groups = [
    ("11:00–11:30 UTC", lambda r: 660 <= r["sweep_minute"] < 690),
    ("11:30–12:00 UTC", lambda r: 690 <= r["sweep_minute"] < 720),
    ("12:00–12:30 UTC", lambda r: 720 <= r["sweep_minute"] < 750),
    ("12:30–13:00 UTC", lambda r: 750 <= r["sweep_minute"] < 780),
    ("13:00–13:30 UTC", lambda r: 780 <= r["sweep_minute"] < 810),
]
print_crosstab("BY SWEEP TIME (UTC)", time_groups, records)


# ── MORNING RANGE SIZE ────────────────────────────────────────────────────────

range_groups = [
    ("range 0.5–0.8×ATR", lambda r: 0.5 <= r["range_atr_ratio"] < 0.8),
    ("range 0.8–1.2×ATR", lambda r: 0.8 <= r["range_atr_ratio"] < 1.2),
    ("range 1.2–1.8×ATR", lambda r: 1.2 <= r["range_atr_ratio"] < 1.8),
    ("range  >1.8×ATR",   lambda r: r["range_atr_ratio"] >= 1.8),
]
print_crosstab("BY MORNING RANGE SIZE (×ATR)", range_groups, records)


# ── DIRECTION SPLIT ───────────────────────────────────────────────────────────

dir_groups = [
    ("Bear sweep (SHORT)", lambda r: r["direction"] == "bear"),
    ("Bull sweep (LONG)",  lambda r: r["direction"] == "bull"),
]
print_crosstab("BY SWEEP DIRECTION", dir_groups, records)


# ── TIMING DISTRIBUTION ───────────────────────────────────────────────────────

print_timing_bars(
    "SWEEP TIME (UTC) vs OUTCOME",
    lambda r: r["sweep_hhmm"],
    records,
    width=30,
)


# ── WICK × TIME INTERACTION ──────────────────────────────────────────────────

print(f"\n{'─'*68}")
print("  WICK SIZE × SWEEP TIME (reversal rate = full+eq)")
print(f"{'─'*68}")
print(f"  {'':20}  {'11:00-11:30':>11}  {'11:30-12:00':>11}  "
      f"{'12:00-12:30':>11}  {'12:30-13:30':>11}")
print(f"  {'─'*65}")

wick_buckets_def = [
    ("wick  0– 5 pts", lambda r: r["wick_ext"] <  5),
    ("wick  5–10 pts", lambda r:  5 <= r["wick_ext"] < 10),
    ("wick 10–20 pts", lambda r: 10 <= r["wick_ext"] < 20),
    ("wick 20+   pts", lambda r: r["wick_ext"] >= 20),
]
time_buckets_def = [
    (660, 690), (690, 720), (720, 750), (750, 810),
]

for wick_label, wick_fn in wick_buckets_def:
    row = f"  {wick_label:<20}"
    for t_start, t_end in time_buckets_def:
        sub = [r for r in records
               if wick_fn(r) and t_start <= r["sweep_minute"] < t_end]
        if not sub:
            row += f"  {'—':>11}"
        else:
            rev_rate = sum(1 for r in sub
                           if r["category"] in ("FULL_REVERSAL", "EQ_ONLY")) / len(sub)
            row += f"  {rev_rate:>9.0%} ({len(sub):>2})"
    print(row)


# ── MAX RETRACE DISTRIBUTION ─────────────────────────────────────────────────

print(f"\n{'─'*68}")
print("  HOW FAR DID PRICE RETRACE (% of morning range toward opposite end)?")
print(f"{'─'*68}")
retrace_buckets = [(0,20,"0–20%"),(20,40,"20–40%"),(40,60,"40–60% (EQ zone)"),
                   (60,80,"60–80%"),(80,101,"80–100% (full rev)")]
for lo, hi, label in retrace_buckets:
    sub = [r for r in records if lo <= r["max_retrace_pct"]*100 < hi]
    bar = "█" * len(sub)
    print(f"  {label:<25}  {len(sub):>3} ({len(sub)/n:.0%})  {bar}")


# ── CONTINUATION DEPTH ───────────────────────────────────────────────────────

print(f"\n{'─'*68}")
print("  CONTINUATION DEPTH — among CONTINUATION events, how far beyond wick?")
print(f"{'─'*68}")
cont_records = [r for r in records if r["category"] == "CONTINUATION"]
if cont_records:
    for lo, hi, label in [(0,10,"0–10 pts"),(10,20,"10–20 pts"),
                           (20,40,"20–40 pts"),(40,1000,"40+ pts")]:
        sub = [r for r in cont_records if lo <= r["max_continuation_pts"] < hi]
        print(f"  {label:<15}  {len(sub):>3} ({len(sub)/len(cont_records):.0%})")
    print(f"  Avg continuation beyond wick: "
          f"{statistics.mean(r['max_continuation_pts'] for r in cont_records):.1f} pts")
    print(f"  Avg continuation bars after sweep: "
          f"{statistics.mean(r['continuation_bars'] for r in cont_records if r['continuation_bars']):.1f}")


# ── TIME-TO-EQ when it's reached ─────────────────────────────────────────────

print(f"\n{'─'*68}")
print("  TIME TO EQ — bars after sweep until midpoint reached (for EQ+FULL_REV)")
print(f"{'─'*68}")
eq_reached = [r for r in records if r["eq_bars"] is not None]
if eq_reached:
    for lo, hi, label in [(1,6,"1–5 bars (25min)"),(6,13,"6–12 bars (1h)"),
                           (13,25,"13–24 bars (2h)"),(25,999,"25+ bars (2h+)")]:
        sub = [r for r in eq_reached if lo <= r["eq_bars"] < hi]
        print(f"  {label:<20}  {len(sub):>3} ({len(sub)/len(eq_reached):.0%})")
    print(f"  Median bars to EQ: {sorted(r['eq_bars'] for r in eq_reached)[len(eq_reached)//2]}")


print("\nDone.")
