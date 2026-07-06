# NAS100 US Open — Initial Expansion + 50% Retrace Strategy

## Concept

At 09:30 ET (16:30 IDT), the market prints an initial expansion — a sharp directional move in the first few candles. If that expansion stalls (momentum slows, range contracts), price often retraces back to ~50% of the expansion range before continuing or reversing further. The idea is to enter the 50% retrace in the direction of continuation.

---

## What we need to define before we can test

### 1. The expansion itself
- **Window**: how many 5m bars qualify as the "initial expansion"? Options: first 1 bar, first 2 bars, first 3 bars after 09:30 ET.
- **Minimum size**: is there a minimum range to count as an expansion (e.g. ≥ 0.15% of price, or ≥ ATR multiple)?
- **Direction**: do we trade both directions, or only with a pre-open bias?

### 2. "Slows down" — what qualifies?
- Option A: next bar range < 50% of expansion bar range
- Option B: price fails to make a new high/low after the expansion
- Option C: a specific number of inside bars / doji bars
- Option D: explicit reversal candle (engulfing, pin bar)

### 3. The 50% level
- High and low of the expansion range (from 09:30 candle open to the extreme of the expansion).
- 50% = midpoint of that range.
- Tolerance band: ±0.05% around the 50% level, or exact touch?

### 4. Entry
- At the 50% touch (limit order)?
- After a confirmation bar closes at/near 50%?
- On a rejection candle at 50%?

### 5. Stop loss
- Beyond the expansion extreme (high or low of the initial move)?
- Fixed distance?

### 6. Take profit
- Back to the other end of the expansion (full range = ~1:1 R)?
- Extension beyond the expansion (1.5R, 2R)?
- Structural level?

### 7. Time limit
- Abort if 50% not reached within X bars?
- Abort if 50% not reached by 10:00 ET?

---

## Data requirements

| Need | Source | Notes |
|------|--------|-------|
| NAS100 5m bars | MT5 EA (NAS100_M5_export.mq5) | Same pattern as XAUUSD_M15_export.mq5 |
| At least 1–2 years | MT5 fills ~4 years for other instruments | Need to attach EA to chart |
| 09:30 ET open bar | Filter by time: 13:30 UTC / 16:30 IDT | |
| Pre-open bias (optional) | NAS100 H4 MA direction | Already have H4 pattern from XAU |

---

## Open questions before running backtest

1. Do we enter **with** the expansion (long after bull expansion hits 50%) or **against** it (short after bull expansion stalls)?
2. Is there a pre-open filter — e.g. price above/below prior day close, or overnight range direction?
3. What is the typical expansion size on NAS100 at open? Need to measure first to set sensible thresholds.
4. Gap days (price opens far from prior close) — include or exclude?

---

## Next steps

1. Set up MT5 EA to export NAS100 5m data (copy XAUUSD_M15_export.mq5, change symbol + period).
2. Measure distribution of first-bar and first-3-bars range at 09:30 ET to calibrate "expansion" threshold.
3. Run initial backtest with simplest definition: first 1-bar expansion, 50% midpoint entry, SL beyond extreme, TP = full range.
4. Grid over: expansion window (1/2/3 bars), slowdown definition, tolerance band.
