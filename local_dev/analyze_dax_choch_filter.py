#!/usr/bin/env python3
"""
Compare three SERPE entry variants on 1-year data:

  A  BASELINE        — current: first LH/HL close, SL at peak + ATR buffer
  B  CHOCH_SL_PEAK   — CHoCH confirmed entry (close past LH low / HL high),
                        SL still at peak + ATR buffer
  C  CHOCH_SL_LH     — CHoCH confirmed entry,
                        SL at LH high / HL low + ATR buffer (tighter)

For SHORT (bullish expansion faded):
  LH candle   : breakout_ts candle (first LH in premium zone)
  CHoCH level : LH candle LOW — CHoCH fires on first close below it
  SL_C        : LH candle HIGH + ATR_buffer

For LONG (bearish expansion faded):
  HL candle   : breakout_ts candle (first HL in discount zone)
  CHoCH level : HL candle HIGH — CHoCH fires on first close above it
  SL_C        : HL candle LOW - ATR_buffer
"""
import sys, statistics
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from db.local_db import LocalDB
from analysis.smc_analyzer import SMCAnalyzer

_ISR = ZoneInfo("Asia/Jerusalem")
DB_PATH = str(Path(__file__).resolve().parents[1] / "src" / "data" / "trade.db")

GOLD = {
    "tp_pct":             0.50,
    "sl_atr_mult":        0.50,
    "min_expansion_atr":  1.00,
    "entry_zone_min_pct": 0.50,
    "symbol":             "GER40",
}
PEAK_CUTOFF  = (11, 45)   # IDT
SKIP_MONDAY  = True
ATR_MULT_LH  = 0.5        # same as GOLD sl_atr_mult, for SL_C buffer


def resample_5m_to_15m(candles_5m):
    out, i = [], 0
    while i < len(candles_5m):
        ts0     = candles_5m[i]["timestamp"]
        aligned = (ts0 // 900) * 900
        group   = [c for c in candles_5m[i:i+3] if c["timestamp"] < aligned + 900]
        if not group:
            i += 1; continue
        out.append({"timestamp": aligned,
                    "open":  group[0]["open"],
                    "high":  max(c["high"] for c in group),
                    "low":   min(c["low"]  for c in group),
                    "close": group[-1]["close"],
                    "volume": 0})
        i += len(group)
    return out


def session_window(d):
    s = datetime(d.year, d.month, d.day,  9, 0, tzinfo=_ISR)
    e = datetime(d.year, d.month, d.day, 12, 30, tzinfo=_ISR)
    return int(s.timestamp()), int(e.timestamp())


def evaluate(entry, sl, tp, is_short, post_5m):
    """Return (outcome, eff_r)."""
    risk = abs(entry - sl) or 1.0
    rwd  = abs(entry - tp)
    for bar in post_5m:
        if is_short:
            if bar["high"] >= sl: return "LOSS", -1.0
            if bar["low"]  <= tp: return "WIN",  rwd / risk
        else:
            if bar["low"]  <= sl: return "LOSS", -1.0
            if bar["high"] >= tp: return "WIN",  rwd / risk
    return "OPEN", None


def calc_atr(candles_5m_recent):
    ana = SMCAnalyzer()
    v = ana.calculate_atr(list(reversed(candles_5m_recent[:60])))
    return v or 1.0


def stats(outcomes):
    closed = [(o, r) for o, r in outcomes if o != "OPEN"]
    if not closed:
        return dict(n=0, wr=0.0, avg_r=0.0, ev=0.0, wins=0, losses=0)
    wins   = [r for o, r in closed if o == "WIN"]
    losses = [r for o, r in closed if o == "LOSS"]
    wr     = len(wins) / len(closed)
    avg_r  = statistics.mean([r for _, r in closed])
    ev     = wr * statistics.mean(wins) + (1 - wr) * (-1.0) if wins else -1.0
    return dict(n=len(closed), wr=wr, avg_r=avg_r, ev=ev,
                wins=len(wins), losses=len(losses))


# ── Load all data ─────────────────────────────────────────────────────────────

print("Loading GER40 5m from DB …")
db       = LocalDB(DB_PATH)
raw_desc = db.query_recent("GER40", "5m", limit=100_000)
db.close()

all5m  = list(reversed(raw_desc))   # oldest → newest
all15m = resample_5m_to_15m(all5m)

print(f"  5m candles : {len(all5m)}")
print(f"  15m candles: {len(all15m)}")

dates = sorted(set(
    datetime.fromtimestamp(c["timestamp"], _ISR).date() for c in all5m
))
dates = [d for d in dates if d.weekday() < 5]
if SKIP_MONDAY:
    dates = [d for d in dates if d.weekday() != 0]

print(f"  Trading days (Tue–Fri): {len(dates)}")

# ── Build per-session slices ───────────────────────────────────────────────────

sessions = []
for trade_date in dates:
    ss, se = session_window(trade_date)
    lookahead_end = se + 6 * 3600

    sess_15m = [c for c in all15m if ss <= c["timestamp"] <= se]
    pre_15m  = [c for c in all15m if c["timestamp"] < ss][-16:]
    day_5m   = [c for c in all5m  if ss <= c["timestamp"] <= lookahead_end]

    if len(sess_15m) >= 3 and len(day_5m) >= 6:
        sessions.append((trade_date, ss, se, sess_15m, pre_15m, day_5m))

print(f"  Sessions with data: {len(sessions)}\n")

# ── Per-trade comparison ───────────────────────────────────────────────────────

analyzer    = SMCAnalyzer()
results_A, results_B, results_C = [], [], []
detail_rows = []
skipped = 0   # CHoCH never confirmed within session

for trade_date, ss, se, sess_15m, pre_15m, day_5m in sessions:
    sigs = analyzer.detect_dax_session_setup(
        sess_15m, day_5m, params=GOLD, candles_15m_presession=pre_15m
    )
    if not sigs:
        continue

    sig = sigs[0]
    pt  = datetime.fromtimestamp(sig["peak_ts"], tz=_ISR)
    if (pt.hour, pt.minute) >= PEAK_CUTOFF:
        continue

    is_short = sig["direction"] == "bearish"   # bearish = SHORT (fade bullish exp)
    entry_A  = sig["entry"]
    sl_A     = sig["sl"]
    tp       = sig["tp"]
    bts      = sig["breakout_ts"]              # LH/HL candle timestamp

    # Locate the LH/HL candle in 5m data
    lh_candle = next((c for c in day_5m if c["timestamp"] == bts), None)
    if lh_candle is None:
        continue

    lh_high = lh_candle["high"]
    lh_low  = lh_candle["low"]

    post_lh = [c for c in day_5m if c["timestamp"] > bts]

    # ── A: baseline ───────────────────────────────────────────────────────────
    oc_A, r_A = evaluate(entry_A, sl_A, tp, is_short, post_lh)
    results_A.append((oc_A, r_A))

    # ── CHoCH scan ────────────────────────────────────────────────────────────
    # Limit CHoCH search to session window (no overnight spillover)
    post_session = [c for c in post_lh if c["timestamp"] <= se]
    choch_candle = None
    for c in post_session:
        if is_short and c["close"] < lh_low:
            choch_candle = c; break
        if not is_short and c["close"] > lh_high:
            choch_candle = c; break

    if choch_candle is None:
        skipped += 1
        detail_rows.append({
            "date": trade_date.isoformat(), "dir": "SHORT" if is_short else "LONG",
            "oc_A": oc_A, "r_A": r_A,
            "oc_B": "FILTERED", "r_B": None,
            "oc_C": "FILTERED", "r_C": None,
            "entry_A": entry_A, "entry_BC": None,
            "sl_A": sl_A, "sl_C": None,
        })
        continue

    entry_BC = choch_candle["close"]
    post_BC  = [c for c in day_5m if c["timestamp"] > choch_candle["timestamp"]]

    # ATR from the 5m candles leading up to the LH (same window the live system uses)
    atr_val = calc_atr(post_lh[:60]) if post_lh else 1.0

    # ── B: CHoCH entry, SL at peak ────────────────────────────────────────────
    oc_B, r_B = evaluate(entry_BC, sl_A, tp, is_short, post_BC)

    # ── C: CHoCH entry, SL at LH high / HL low ───────────────────────────────
    if is_short:
        sl_C = round(lh_high + ATR_MULT_LH * atr_val, 2)
    else:
        sl_C = round(lh_low  - ATR_MULT_LH * atr_val, 2)

    # Guard: SL_C must not be worse than SL_A (can't give more room than peak)
    if is_short:
        sl_C = min(sl_C, sl_A)   # SL_C ≤ peak SL (lower is tighter for SHORT)
    else:
        sl_C = max(sl_C, sl_A)   # SL_C ≥ peak SL (higher is tighter for LONG)

    oc_C, r_C = evaluate(entry_BC, sl_C, tp, is_short, post_BC)

    results_B.append((oc_B, r_B))
    results_C.append((oc_C, r_C))

    detail_rows.append({
        "date": trade_date.isoformat(), "dir": "SHORT" if is_short else "LONG",
        "oc_A": oc_A, "r_A": r_A,
        "oc_B": oc_B, "r_B": r_B,
        "oc_C": oc_C, "r_C": r_C,
        "entry_A": entry_A, "entry_BC": entry_BC,
        "sl_A": sl_A, "sl_C": sl_C,
        "lh_high": lh_high, "lh_low": lh_low, "tp": tp,
    })

# ── Summary ───────────────────────────────────────────────────────────────────

sA = stats(results_A)
sB = stats(results_B)
sC = stats(results_C)

print("=" * 72)
print("SERPE CHoCH FILTER — 1-year comparison (Jun 2025 – Jun 2026)")
print("=" * 72)
print(f"\n{'Variant':<32} {'N':>4}  {'WR':>6}  {'avg_R':>7}  {'EV':>7}  {'W':>4}  {'L':>4}")
print("-" * 72)

def fmtrow(label, s, skp=0):
    suf = f"  (+{skp} CHoCH never fired)" if skp else ""
    print(f"  {label:<30} {s['n']:>4}  {s['wr']:>5.0%}  {s['avg_r']:>+6.2f}R  "
          f"{s['ev']:>+6.2f}R  {s['wins']:>4}  {s['losses']:>4}{suf}")

fmtrow("A  Baseline (current)",  sA)
fmtrow("B  CHoCH + SL at peak",  sB, skipped)
fmtrow("C  CHoCH + SL at LH/HL", sC, skipped)
print()

# ── Trade-by-trade detail ──────────────────────────────────────────────────────

print("=" * 100)
print("TRADE-BY-TRADE DETAIL")
print("=" * 100)
print(f"  {'Date':>10}  {'Dir':>5}  {'EntryA':>7}  {'OcA':>6}  {'RA':>5}"
      f"  {'EntryBC':>7}  {'OcB':>8}  {'OcC':>8}  {'SL_A':>6}  {'SL_C':>6}  Note")
print("-" * 100)

loss_to_win = win_to_loss = loss_saved = win_missed = 0

for r in detail_rows:
    ra_s    = f"{r['r_A']:>+5.1f}R" if r['r_A'] is not None else "  ---"
    ebc_s   = f"{r['entry_BC']:>7.0f}" if r['entry_BC'] else "   ----"
    oc_b_s  = r.get('oc_B', 'FILTERED')
    oc_c_s  = r.get('oc_C', 'FILTERED')
    sla_s   = f"{r['sl_A']:>6.0f}"  if r['sl_A'] else "   ---"
    slc_s   = f"{r['sl_C']:>6.0f}"  if r['sl_C'] else "   ---"

    note = ""
    if r['oc_A'] == "LOSS" and oc_c_s == "WIN":
        note = "LOSS→WIN"; loss_to_win += 1
    elif r['oc_A'] == "WIN" and oc_c_s == "LOSS":
        note = "WIN→LOSS"; win_to_loss += 1
    elif oc_c_s == "FILTERED" and r['oc_A'] == "LOSS":
        note = "saved (bad trade skipped)"; loss_saved += 1
    elif oc_c_s == "FILTERED" and r['oc_A'] == "WIN":
        note = "missed"; win_missed += 1

    print(f"  {r['date']:>10}  {r['dir']:>5}  {r['entry_A']:>7.0f}  {r['oc_A']:>6}  {ra_s}"
          f"  {ebc_s}  {oc_b_s:>8}  {oc_c_s:>8}  {sla_s}  {slc_s}  {note}")

print(f"""
Outcome changes A → C:
  LOSS → WIN  (filter improved trade)  : {loss_to_win}
  WIN  → LOSS (filter introduced loss) : {win_to_loss}
  LOSS filtered (bad trade skipped)    : {loss_saved}
  WIN  filtered (good trade missed)    : {win_missed}
  Unchanged                            : {len(detail_rows) - loss_to_win - win_to_loss - loss_saved - win_missed}
""")
print("Done.")
