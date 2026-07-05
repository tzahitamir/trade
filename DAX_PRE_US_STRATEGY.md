# DAX Pre-US Open Strategy — Research Summary

**Instrument:** GER40 (DAX) · **Timeframe:** 5m · **Data:** ~1 year (Jun 2025 – Jul 2026)  
**Analysis scripts:** `local_dev/dax_pattern_a_*.py`, `dax_continuation*.py`

---

## Thesis

During the pre-US window (11:00–13:30 UTC), DAX frequently makes a sweep of its morning range high or low — a wick that briefly breaks the level then closes back inside. 86% of those sweeps result in price eventually **breaking back through** the swept extreme (continuation), not reversing. This is a liquidity grab by institutions clearing stops, followed by a directional delivery to a structural target (PDH/PDL).

---

## Setup Definition

| Element | Rule |
|---|---|
| **Morning range** | 07:00–11:00 UTC high (MH) and low (ML) |
| **Sweep candle** | Wick beyond MH or ML **and** closes back inside the range |
| **Direction** | High sweep → LONG continuation; Low sweep → SHORT continuation |
| **Sweep window** | 11:00–13:30 UTC (pre-US open) |
| **Min wick** | ≥ 7 pts beyond morning level |
| **Time filter** | 11:00–11:30 or 12:00–12:30 UTC only (other windows negative EV) |
| **Entry** | Limit at morning\_level − 10% of range (for LONG) |
| **SL** | 15 pts below entry |
| **TP** | Previous day High (LONG) / Previous day Low (SHORT) |
| **Cutoff** | No new entries after 13:30 UTC; evaluate until 15:30 UTC |

---

## Analysis Progression

### Phase 1 — Sweep behaviour (dax_pattern_a_behavior.py)

Categorised all 160 sweeps found in the dataset:

| Category | % | Description |
|---|---|---|
| CONTINUATION | 86% | Price eventually breaks back through swept extreme |
| FULL_REVERSAL | 8% | Price reverses to opposite end of morning range |
| EQ_ONLY | 4% | Price reaches midpoint, stalls |
| STALL | 2% | No meaningful movement |

**Key stat:** Avg continuation = 75.6 pts beyond the swept extreme. Larger wicks (30+ pts) show lower continuation rate (44%) than small wicks (96%).

---

### Phase 2 — Fade trade (Setup A — dax_pattern_a_setup_a.py)

Tested entering AGAINST continuation direction (fade the sweep).

| TP level | EV | WR | Note |
|---|---|---|---|
| 25% of range | +0.20R | 34% | Best fade TP |
| EQ (50%) | −0.30R | 14% | Too far |

**Verdict:** Fade is marginally viable only at 25% of range, only during 12:00–12:30 UTC. Not pursued.

---

### Phase 3 — Pullback to EQ (Setup B — dax_pattern_a_setup_b.py)

Wait for 50%+ retrace into range, then enter continuation.

| Best combo | EV | N/yr |
|---|---|---|
| Entry 65%, SL 0.75×ATR | +1.02R | 12 |

**Verdict:** Too rare (12 trades/year). Dropped.

---

### Phase 4 — Directional fade at US open (Pattern C — dax_pattern_c.py)

Fade a directional pre-US move (>2×ATR) at the 13:30 UTC open.

| Best combo | EV |
|---|---|
| minATR=2.0, SL=0.75×ATR, TP=25% | +0.06R |

**Verdict:** No standalone edge. Extreme moves (>2×ATR) show 20% WR when faded. Dropped.

---

### Phase 5 — Continuation at sweep close (dax_continuation.py)

Enter at the sweep candle close in the continuation direction.

| Best combo | EV | WR | N |
|---|---|---|---|
| wick≥7, SL=0.75×ATR, TP=level+25% | +0.27R | — | 88 |
| (time-filtered) same | +1.78R | — | 11 closed |

**SL tolerance cliff:** 10% of range SL → +0.26R EV; 15% of range → +0.03R; 20% → negative.

---

### Phase 6 — Confirmed breakout entry (dax_continuation_v2.py)

Wait for first 5m candle to **close above** morning level (confirmed break), then enter.

| Best combo | EV | WR | N |
|---|---|---|---|
| wick≥7, SL=15pts, TP=PDH (time-filtered) | +1.18R | 36% | 11 closed |

**Per-trade wins (4 winners):** +5.97R, +2.98R, +6.93R, +4.11R (avg ≈ 5R).  
**Problem:** 36% WR because PDH is far; confirmed entry comes late (misses fast moves).

---

### Phase 7 — Pullback limit entry (dax_continuation_v3.py)

**User insight:** if 90% of sweeps reach the morning level and the median retrace is only 9-20% of range, wait for that retrace and enter there — better price, tighter SL, higher R:R.

**Pullback reach rates (wick≥7, no SL):**

| Pullback depth | Reach % |
|---|---|
| 5% of range | 100% |
| 10% of range | 92% |
| 15% of range | 89% |
| 20% of range | 85% |
| 25% of range | 80% |
| 30% of range | 73% |

**TP reach rates (after 10% pullback entry, no SL, time-filtered, N=34):**

| Target | Reach % |
|---|---|
| Morning level | 97% |
| Level + 10% range | 76% |
| Level + 25% range | 53% |
| PDH / PDL | 74% |

**Best unfiltered combos:**

| wick | Pullback | SL | TP | EV | WR | N |
|---|---|---|---|---|---|---|
| ≥10 | 25% | 5 pt | level+10% | +2.81R | 23% | 22 |
| ≥7 | 25% | 15 pt | level+10% | +0.71R | 30% | 47 |

**Best time-filtered combos:**

| wick | Pullback | SL | TP | EV | WR | N |
|---|---|---|---|---|---|---|
| ≥10 | 10% | 15 pt | PDH | +2.93R | 44% | 9 |
| ≥7 | 10% | 15 pt | PDH | +2.38R | 36% | 14 |

---

### Phase 8 — Loss diagnosis (dax_continuation_v4.py)

Focused on: wick≥7, pb=10%, SL=15pts, TP=PDH, time-filtered.

**Why is WR 36%?**

| Loss category | Count | % of losses | Root cause |
|---|---|---|---|
| Cat A — SL hit before morning level | 4 | 44% | Direction simply wrong; wider SL doesn't help |
| Cat B — reached morning level, reversed before PDH | 4 | 44% | PDH not reached that day |
| Cat C — past level+10%, reversed | 1 | 11% | Near-miss |

**SL sensitivity:**

| SL | WR | avgR:R | EV |
|---|---|---|---|
| 5 pt | 14% | 38.9R | +4.70R |
| 8 pt | 29% | 19.2R | **+4.77R** ← best EV |
| 15 pt | 36% | 8.5R | +2.38R |
| 25 pt | 36% | 5.1R | +1.17R |
| 30 pt | 36% | 4.2R | +0.87R |

**Widening SL from 15→30pt does not improve WR** (stays 36%). Cat A losses continue going down regardless — direction was wrong.

**TP level sweep (SL=15pt, time-filtered):**

| TP | WR | avgR:R | EV |
|---|---|---|---|
| Morning level | 33% | 1.5R | **−0.18R** ← negative! |
| Level + 10% | 40% | 2.5R | +0.38R |
| Level + 35% | 30% | 4.7R | +0.74R |
| PDH / PDL | 36% | 8.5R | **+2.38R** ← best |

**Tiered exit (50% at morning level, 50% at PDH):** EV = +1.30R — worse than PDH-only. Splitting the position cuts the big wins without reducing losses.

**Conclusion on TP:** PDH is the correct target. A closer TP gives negative EV because R:R is too small relative to WR. The math is: 36 wins × 8.5R − 64 losses × 1R = **+2.38R/trade**.

---

### Phase 9 — Institutional context (dax_continuation_v5.py)

Tested four institutional filters on the same base setup.

#### Day of week

| Day | N closed | WR | EV |
|---|---|---|---|
| Monday | 2 | 0% | −1.00R |
| Tuesday | 5 | 20% | +2.15R |
| Wednesday | — | — | — |
| Thursday | 3 | 33% | +2.99R |
| **Friday** | **4** | **75%** | **+3.89R** |

**Friday is the strongest setup.** Interpretation: institutions deliver price to weekly structural targets (PDH/PDL) on Friday as part of weekly position settlement. Monday sweeps are gap/noise-related and consistently fail.

#### Daily trend alignment (3-day slope)

| Filter | WR | avgR:R | EV | N |
|---|---|---|---|---|
| Trend aligned | 67% | 2.7R | +1.48R | 3 |
| Trend counter | 27% | 12.3R | +2.63R | 11 |

Counter-trend sweeps have **lower WR but much larger wins**. The 3 biggest trades (+10.97R, +11.15R, +14.77R) are all counter-trend. Interpretation: the sweep IS often the institutional reversal — they are pivoting against recent trend momentum. The 3-day trend is not a useful filter in the expected direction.

#### PDH distance from morning level

| PDH distance | N | WR | EV |
|---|---|---|---|
| < 60 pt | 3 | 33% | +0.5R |
| 60–100 pt | 1 | 0% | −1.00R |
| 100–300 pt | 8 | 37% | ~+4R (sweet spot) |
| > 300 pt | 2 | 0% | −1.00R |

**PDH > 300pt = 0% WR** — target is unreachable in a single session. Filter out these trades.  
**PDH < 60pt = poor R:R** — not enough room.  
**Sweet spot: PDH 100–300pt from morning level.**

Applying max PDH ≤ 300pt: WR 36%→42%, EV +2.38R→+2.94R, N=12.

#### Asian session alignment

Morning level within 10–30pt of Asian session high/low (double liquidity pool):

| Tolerance | N matched | WR | EV |
|---|---|---|---|
| 10 pt | 2 | — | — |
| 30 pt | 5 | 40% | +3.17R |

Direction is positive but sample is too small to rely on. Needs a larger dataset.

---

## Best Parameters (Current State)

| Parameter | Value |
|---|---|
| Instrument | GER40 (DAX) 5m |
| Morning range | 07:00–11:00 UTC |
| Sweep window | 11:00–11:30 UTC or 12:00–12:30 UTC only |
| Min wick beyond level | 7 pts |
| Entry | Limit at level − 10% of morning range |
| Stop loss | 15 pts below entry |
| Take profit | Previous day High (LONG) / Previous day Low (SHORT) |
| Skip if | PDH distance from morning level > 300 pt |
| Skip if | PDH distance from morning level < 60 pt |
| Best day | Friday (75% WR) |
| Avoid | Monday (0% WR) |

**Performance with base filters (wick≥7, time, pb=10%, SL=15, TP=PDH):**

| Metric | Value |
|---|---|
| N closed | 14 |
| WR | 36% |
| avg win R:R | 8.5R |
| EV per trade | +2.38R |
| avg pts per trade | +35.7 pt |

**With PDH distance filter (60–300pt):**

| Metric | Value |
|---|---|
| N closed | ~10–12 |
| WR | ~42% |
| EV per trade | ~+2.94R |

---

## Open Questions

1. **Friday filter reliability** — only 4 Friday trades in dataset; need more data to confirm 75% WR
2. **Asian session double-liquidity** — too few samples; test with a 2-3 year dataset
3. **Cat A loss predictor** — 44% of losses are "direction wrong"; no current filter catches them cleanly. A price-action confirmation (e.g. rejection candle at entry level) could help
4. **SL=8pt practical viability** — best paper EV (+4.77R) but spread/slippage on GER40 makes 8pt SL very difficult live
5. **Wick targeting known structural level** — did the sweep wick reach a prior swing high (1h / 4h)? If yes, stronger signal. Not yet tested
6. **Larger dataset** — current dataset is ~1 year; many conclusions rest on N=10–14 closed trades

---

## Analysis Files

| File | Purpose |
|---|---|
| `local_dev/dax_pattern_a_behavior.py` | Post-sweep categorisation; 86% continuation finding |
| `local_dev/dax_pattern_a_setup_a.py` | Fade trade analysis (Setup A) |
| `local_dev/dax_pattern_a_setup_b.py` | EQ pullback entry (Setup B) |
| `local_dev/dax_pattern_c.py` | Directional US-open fade (Pattern C) |
| `local_dev/dax_continuation.py` | Sweep-close entry, full parameter sweep |
| `local_dev/dax_continuation_v2.py` | Confirmed breakout entry + PDH TP |
| `local_dev/dax_continuation_v3.py` | Pullback limit entry + full TP/SL grid |
| `local_dev/dax_continuation_v4.py` | Loss diagnosis: Cat A/B/C, MAE/MFE, SL/TP sweep |
| `local_dev/dax_continuation_v5.py` | Institutional filters: DOW, trend, PDH dist, Asian session |

---

*Last updated: 2026-07-05*
