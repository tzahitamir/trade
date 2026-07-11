# /research-stock

Research a trading instrument using the full backtesting methodology developed in this project.
Instrument: **$ARGUMENTS**

---

## Step 0 — Check cached results

Before asking any questions, call `load_research_results(symbol, signal_tf)`:

```python
from research_framework import load_research_results, summarise_cached, SKILL_VERSION
data = load_research_results(symbol, signal_tf)
```

- If `data` is **None**: no cache. Proceed to Step 1.
- If `data` is found: print the cached summary using `summarise_cached(data)`, then ask the user:
  - "This instrument was already researched. Re-run fresh, or use cached results?"
  - If skill version changed (`data["_skill_changed"]` is True): **flag it clearly** — the logic has changed since the last run, results may not reflect current confluences.
  - If the user wants to compare params: show `data["_params_note"]` and let the user decide.
  - If user chooses cached: skip to Step 7 (Report), loading results from cache.
  - If user chooses fresh: proceed to Step 1.

---

## Step 1 — Clarify inputs

Ask the user **five questions** in one AskUserQuestion call:

1. **Data source**
   - `yfinance` — US stocks/ETFs/indices (free, 59-day limit on 5m)
   - `MT5 CSV` — instruments exported via EA bridge (GER40, US100, XAUUSD)

2. **Signal TF** — timeframe to detect BOS on
   - Suggest `15m` for FX/DAX, `30m` for stocks, `1h` for broad analysis
   - Other options: `5m`, `30m`, `1h`

3. **HTF for alignment** — higher TF to check trend bias
   - Suggest `4h` (default). Other options: `1h`, `1d`
   - If only one TF data source is available, agent will warn and skip HTF confluences

4. **LTF for entry** — lower TF for entry refinement
   - Suggest `5m` (default). Other options: `1m`, `15m`
   - If same as signal TF, LTF entry confluences are skipped

5. **Sessions to include** (multi-select)
   - London (08:00–12:00 UTC) — default ON
   - NY (13:30–17:30 UTC) — default ON
   - Frankfurt (07:00–10:30 UTC) — for DAX/European, default ON
   - Pre-London (07:00–08:00 UTC) — optional

   Asian session (00:00–07:00 UTC) is always excluded from trading signals.
   It is used only for pre-session bias calculation.

Do not proceed until all answers are received.

---

## Step 2 — Load data

Download or load candles for **three timeframes**: signal TF, LTF, and HTF.
Use `load_yfinance()` or `load_mt5()` from the framework, then `resample()` to derive TFs.

### If yfinance:

```python
# Base data: use 5m or 1m as the finest available TF
candles_5m  = load_yfinance(symbol, interval="5m",  period="59d")   # ~59 day limit
candles_30m = resample(candles_5m, 30)    # signal TF example
candles_4h  = resample(candles_5m, 240)   # HTF example
```

If signal TF > 30m or history > 59 days is needed, switch base to 30m (up to 60d) or 1h (up to 730d):
```python
candles_1h  = load_yfinance(symbol, interval="1h", period="730d")
candles_4h  = resample(candles_1h, 240)
candles_1d  = resample(candles_1h, 1440)
```

Warn the user about the history limitation and offer the 1h base if they want more data.

### If MT5 CSV:
```python
candles_5m = load_mt5(instrument)   # already 5m candles
candles_signal = resample(candles_5m, signal_tf_minutes)
candles_htf    = resample(candles_5m, htf_tf_minutes)
candles_ltf    = candles_5m  # LTF is 5m
```

If HTF cannot be derived from available data (e.g. base TF is already the signal TF):
**warn the user**: "HTF data not available from this source — HTF alignment confluences will be skipped."
Let the user decide whether to continue without HTF or change data source.

### Candle dict format
```python
{"timestamp": int_unix_utc, "open": float, "high": float, "low": float,
 "close": float, "volume": float_or_None}
```

---

## Step 3 — SERPE analysis

Based on `local_dev/analyze_dax_serpe_full.py`. Adapt for this instrument.

**Concept**: Session expansion (15m BOS against pre-session structure) → peak → LH/HL entry → retrace to EQ (50%).

Write `local_dev/research_{SYMBOL}_serpe.py`:

### Detection logic:
1. Resample 5m → 15m
2. For each trading day (skip Monday by default, skip weekends):
   - `sess_15m` = candles within session window
   - `pre_15m` = last 16 candles before session start
   - Call `analyzer.detect_dax_session_setup(sess_15m, day_5m, params=GOLD, candles_15m_presession=pre_15m)`
   - Filter: peak must form before session_end - 1h (equivalent of `peak < 12:00` gate)

### Gold params to start with:
```python
GOLD = {
    "tp_pct": 0.50,
    "sl_atr_mult": 0.25,
    "min_expansion_atr": 1.00,
    "entry_zone_min_pct": 0.80,
    "symbol": SYMBOL,
}
```

### Output:
- Overall N, WR%, avg_R, EV
- By direction (bull exp → SHORT, bear exp → LONG)
- By day of week
- By peak hour
- TP% sweep (0.40 → 0.75)
- Entry zone sweep (0.60 → 0.90)
- Per-day detail table

---

## Step 4 — Initial expansion analysis

Based on `local_dev/tune_ger40_expansion.py` and `local_dev/analyze_ger40_frankfurt_open.py`.

**Concept**: At session open, price expands X% from open price. After Def-D bar (first opposite-colour candle after expansion ends), enter a fade trade to EQ (50%).

Write `local_dev/research_{SYMBOL}_expansion.py`:

### Detection logic:
```
open_price = first candle of session open
expansion bars = candles where price moves monotonically from open_price
expansion_pct = abs(peak - open_price) / open_price * 100
Def-D bar = first candle after peak that closes in opposite direction
entry = close of Def-D bar (if retrace >= retrace_pct of expansion range)
TP = open_price + tp_pct * (peak - open_price)   # toward open price
SL = expansion_low (for BEAR) or expansion_high (for BULL)
```

### Parameter sweep:
```python
EXP_PCTS   = [0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.60]
TP_PCTS    = [0.40, 0.45, 0.50, 0.55, 0.60]
RETRACE    = [0.20, 0.25, 0.30]
DIRECTIONS = ["BEAR", "BULL", "BOTH"]
```

### Output:
- Grid search table: exp_pct × tp_pct → WR, EV, N
- Best combo per direction
- Recommendation: which direction performs better and by how much

---

## Step 5 — SL optimization

Based on `local_dev/analyze_nas100_sl.py`.

**Concept**: Compare two SL placements for the best setup found in Step 3 or 4:
- SL-A: expansion extreme (peak high for SHORT, peak low for LONG) + ATR buffer
- SL-B: Def-D bar extreme (LH high for SHORT, HL low for LONG) + small buffer

Run both on the same signals. Report:
- N wins/losses for each
- How many trades SL-B saves vs adds losses
- Recommendation: which SL is better

---

## Step 6 — Standalone BOS / CHOC analysis with confluence testing

**Concept**: Trade the BOS breakout directly — enter in the break direction, targeting a fixed R multiple.
This is separate from SERPE (which fades the expansion). BOS trades WITH the breakout momentum.

Use the two-step architecture:
1. `collect_bos_signals()` — detect all BOS events once with full metadata
2. `test_all_confluences()` — test every confluence independently and in combination

### Step 6a — Collect signals

```python
signals = collect_bos_signals(
    candles_signal = candles_30m,   # signal TF (user's choice)
    candles_ltf    = candles_5m,    # LTF for entry refinement + evaluation
    dates          = dates,
    sl_atr_mult    = 0.5,
    tp_r           = 2.0,
    swing_lookback = 15,
    htf_candles    = candles_4h,    # None if HTF data unavailable
)
print(f"Total BOS signals: {len(signals)}")
```

Asian session (00:00–07:00 UTC) is automatically excluded. All other sessions are scanned.
If the user selected specific sessions (e.g. London only), post-filter the signals:
```python
# Example: London + NY only
signals = [s for s in signals if s["session_london"] or s["session_ny"]]
```

Each signal carries all these fields — from `detect_bos` plus computed metadata:

| Field | Source | Description |
|-------|--------|-------------|
| `break_body_pct` | detect_bos | Body of BOS candle / total range (0–1) |
| `liquidity_sweep` | detect_bos | Sweep event object before BOS, or None |
| `swing_age_candles` | detect_bos | How old the broken level was in candles |
| `swing_test_count` | detect_bos | Times the level was tested before breaking |
| `break_strength` | detect_bos | ATR-relative strength of the break |
| `session_minute` | computed | Minutes since session start when BOS fired |
| `presession_bias` | computed | "bullish" / "bearish" / "neutral" |
| `presession_bias_aligned` | computed | True if presession direction = BOS direction |
| `volume_ratio` | computed | BOS bar volume / session average (None if unavailable) |
| `has_nearby_fvg` | computed | True if 3-candle imbalance in last 10 pre-BOS bars |
| `pre_bos_expansion_atr` | computed | Pre-BOS session range / ATR |

### Step 6b — Test all confluences

```python
confluences = test_all_confluences(signals, min_n=5, n_trading_days=len(dates))
```

Tests **11 confluence categories** with parameter sweeps, plus all pairwise dual-combinations:

| # | Confluence | Parameters swept |
|---|-----------|-----------------|
| 1 | Baseline (no filter) | — |
| 2 | **Liquidity Sweep** | any sweep preceding BOS |
| 3 | **Momentum Candle** | body ≥ 50% / 60% / 70% / 80% of range |
| 4 | **Break Strength** | strength ≥ 0.3 / 0.5 / 0.7 / 1.0 |
| 5 | **Level Age** | swing age ≥ 5 / 10 / 15 / 20 candles |
| 6 | **Level Tests** | level tested ≥ 1 / 2 / 3 times |
| 7 | **Early Session** | BOS within first 30 / 45 / 60 / 90 min |
| 8 | **Pre-session Bias Aligned** | presession direction = BOS direction |
| 9 | **Volume Spike** | BOS volume ≥ 1.5× / 2.0× / 3.0× session avg |
| 10 | **Nearby FVG** | 3-candle imbalance in last 10 pre-BOS bars |
| 11 | **ATR Expansion** | pre-BOS session range ≥ 0.5 / 1.0 / 1.5 × ATR |
| 12 | **Dual combos** | best pairwise combinations of the above |

**Always highlight `multi_bar_conf` (conf2) in the output.** Based on EURUSD 15m research this is the most reliable standalone BOS filter found to date — check whether it appears as viable on the new instrument, at what N and WR, and whether combos with it (Frankfurt+conf2, FVG+conf2, etc.) improve further. See *Known Findings* at the bottom of this file.

### Step 6c — TP/SL sweep on the best confluence

After finding the best single or dual confluence, sweep TP/SL:

```python
# Example: liquidity sweep was the best confluence
best_sigs = [s for s in signals if s["liquidity_sweep"] is not None]
tp_rows = bos_tp_sweep(best_sigs, tp_rs=[1.5, 2.0, 2.5, 3.0], sl_atr_mults=[0.25, 0.50, 0.75])
```

**SL multiplier note**: On EURUSD 15m the SL multiplier (0.25/0.50/0.75×ATR) had zero effect on WR or EV. This is expected when the broken level itself is the invalidation point — either the trade has momentum and reaches TP, or it reverses hard through the level regardless of ATR buffer size. If the sweep confirms the same on this instrument, report it clearly and recommend the tightest SL (0.25×ATR).

### Output:
- Sorted confluence table (highest EV first) with N, WR%, EV, ~/wk, verdict for each
- Top 5 TP/SL combos for the best viable confluence
- Note: BOS results are often marginal — document honestly even if not viable

---

## Step 7 — Report

After all scripts run, print a consolidated summary:

```
========================================
RESEARCH REPORT: {SYMBOL}
Data: {source} | Session: {window} | Period: {date_range}
========================================

SERPE STRATEGY
  Best params:  tp={X}% ez={Y}% exp≥{Z}×ATR
  N={n} | WR={wr}% | EV={ev}R
  Bull exp→SHORT: {n} | {wr}% | {ev}R
  Bear exp→LONG:  {n} | {wr}% | {ev}R
  Verdict: [VIABLE / MARGINAL / NOT VIABLE]

INITIAL EXPANSION
  Best params:  direction={dir} exp={pct}% tp={tp}%
  N={n} | WR={wr}% | EV={ev}R
  Verdict: [VIABLE / MARGINAL / NOT VIABLE]

SL COMPARISON
  SL-A (peak extreme): WR={wr}%
  SL-B (Def-D bar):    WR={wr}%
  Recommendation: use SL-{X}

BOS / CHOC CONFLUENCES (top 5 by EV)
  Confluence           Params           N   WR       EV    ~/wk
  {confluence_1}       {params}         n   wr%    ev R    x.x   [verdict]
  {confluence_2}       ...
  Best TP/SL:  tp={X}R  sl={Y}×ATR  → WR={wr}%  EV={ev}R

OVERALL RECOMMENDATION
  [Your summary of whether this instrument is worth trading with these strategies]
========================================
```

A strategy is **VIABLE** if: N≥20 AND WR≥65% AND EV≥+0.5R
A strategy is **MARGINAL** if: N≥10 AND WR≥55% AND EV≥+0.2R
Otherwise: **NOT VIABLE**

After printing the report, save results:
```python
save_research_results(
    symbol     = SYMBOL,
    signal_tf  = "30m",
    params     = {
        "htf_tf": "4h", "ltf_tf": "5m",
        "sessions": ["london", "ny"],
        "data_source": "yfinance",
        "swing_lookback": 15,
        "sl_atr_mult": 0.5,
        "tp_r": 2.0,
        "data_from": str(dates[0]),
        "data_to":   str(dates[-1]),
        "n_trading_days": len(dates),
    },
    results    = {
        "serpe":          serpe_stats,
        "expansion":      expansion_top[0] if expansion_top else None,
        "bos_confluences": confluences[:10],   # top 10 rows
        "best_bos":        confluences[0] if confluences else None,
        "verdict":         overall_verdict,
    },
)
```

---

## Notes for the agent

### Use the research framework — do not reimplement core logic

All reusable functions live in `local_dev/research_framework.py`. Import from there:

```python
from research_framework import (
    SKILL_VERSION,
    load_yfinance, load_mt5, load_mt5_db,
    resample, trading_dates, atr,
    NYSE_SESSION, FRANKFURT_SESSION, LONDON_SESSION, make_session_fn,
    evaluate, stats, verdict,
    run_serpe, serpe_tp_sweep,
    run_expansion, expansion_grid_search,
    collect_bos_signals, test_all_confluences, bos_tp_sweep,
    compare_sl,
    print_report,
    save_research_results, load_research_results, summarise_cached,
)
```

Write a single script `local_dev/research_{SYMBOL}.py` that:
1. Calls `load_research_results()` at the top — if cached and user accepts, skip to reporting
2. Loads data at signal TF, LTF, and HTF using the appropriate loaders + `resample()`
3. Calls `run_serpe()`, `serpe_tp_sweep()`, `expansion_grid_search()`, `compare_sl()`
4. Calls `collect_bos_signals()` → post-filters by user's session selection → `test_all_confluences()`
5. Takes the top-3 BOS confluences by EV and runs `bos_tp_sweep()` on each
6. Calls `print_report()` with all results
7. Calls `save_research_results()` to persist results for future runs

The framework handles: data loading, resampling, ATR, session windowing, evaluation, stats, and report formatting.

### Additional rules
- Script goes in `local_dev/` named `research_{SYMBOL}.py`
- Always run from the `local_dev/` directory so relative imports resolve
- Never render charts in the scan loop (performance rule)
- Gate scans to last 2 years (`max_years=2.0` in `trading_dates()`)
- yfinance 5m is limited to ~59 days — ask user if they want 30m bars for longer history

---

## Known Findings

Empirical results discovered so far, to inform research on new instruments.

### conf2 — multi-bar confirmation (the most important BOS finding)

**What it is**: `confirmation_bars ≥ 2` — the 2 signal-TF bars immediately after the BOS bar both close in the BOS direction. Computed in `collect_bos_signals`; stored in `sig["confirmation_bars"]`.

**EURUSD 15m result** (60 trading days, London+NY, Jul 2026):
- Baseline (no filter): N=296, WR=32%, EV=-0.04R — NOT VIABLE
- conf2 alone: N=44, WR=70%, EV=+1.11R, 6.2/wk — VIABLE
- Frankfurt+conf2: N=20, WR=80%, EV=+1.40R, 2.5/wk — VIABLE
- FVG+conf2: N=36, WR=75%, EV=+1.25R, 5.4/wk — VIABLE
- Nearly every viable combo was some filter combined with conf2 — conf2 is the primary driver

**Interpretation**: conf2 selects BOS events with immediate follow-through momentum. BOS signals with 0 confirmations (60% of all signals) are strongly negative: WR=19%, EV=-0.42R. One confirmation bar (conf1) brings WR to 42% — still not viable. Two bars (conf2) crosses the viability threshold.

**Frequency split** (EURUSD 15m, London+NY):
- 0 confirmations: 60% of signals
- 1 confirmation only: 23%
- 2+ confirmations (conf2): 17%

**Is conf2 predictable at the close of conf1?** No. Tested 15 5m features (body strength, close position in range, level held, volume trend, bar direction patterns, distance from level, bar size vs ATR). Best predictor was "conf1 bar closes in top/bottom 80% of its range" — only 55% probability of conf2 vs 43% base rate (lift 1.29×). Not tradeable. **You cannot reliably anticipate conf2 from conf1 patterns; you must wait for conf2 to actually fire.**

### conf2 entry timing

**Enter at the close of the conf2 bar** (bar +2 after the BOS bar), not at BOS close.
The backtested entry in `collect_bos_signals` uses the BOS close price, but conf2 is evaluated retrospectively. In live trading, enter when bar +2 confirms — you'll be 2 bars late vs the backtest entry, at a somewhat worse price, but you avoid the 83% of BOS signals that don't reach conf2.

### SL placement

**SL = broken swing level ± 0.5×ATR buffer.** The ATR buffer size is cosmetic on EURUSD 15m — WR and EV were identical at 0.25/0.50/0.75×ATR in the sweep. The broken level itself is the real invalidation point: price either has momentum and reaches TP, or it reverses hard through the level regardless of buffer. If confirmed on other instruments, use the tightest buffer (0.25×ATR) to minimise capital at risk.

### LTF retest as an optional entry refinement

After conf2 fires on the signal TF, watch for price to pull back to the broken level on the LTF (5m):
- conf2 + LTF retest: N=25, WR=80%, entry 40% closer to level → 40% less capital at risk for same trade
- conf2 + no retest (went straight): N=21, WR=76%
- Both are viable; the retest gives a better entry when it occurs (35% of conf2 signals)

**LTF LH/HL entry** (waiting for a Lower High / Higher Low on 5m after BOS) showed no improvement vs immediate entry on EURUSD 15m (68% WR vs 70%, and entry was actually 10% further from the level). Skip this for now.

### conf3 — when to use it instead of conf2

Some instruments are too choppy for conf2 — adding a third confirmation bar (conf3) is the only way to reach WR≥65%. Research findings:

| Instrument | conf2 WR | conf3 needed? | Note |
|---|---|---|---|
| GBPUSD | 64% | No — conf2 viable | NY session is the edge (92% tod_15+conf2) |
| EURUSD | 70% | No — conf2 viable | Frankfurt session best (80%) |
| EURJPY | 56% | Yes — htf_ema+conf3 76% | 4h EMA alignment is critical gate |
| XAUUSD | — | Yes — all viable confluences conf3-based | 11 viable confluences, all conf3 |
| GER40  | — | Yes — all viable confluences conf3-based | Frankfurt+London sessions |
| USDJPY | 46% | Yes — age+conf3 70% (low freq, 2/wk) | Marginal overall |

**Rule of thumb**: if conf2 baseline WR is <55%, try conf3 before giving up. The price is ~50% fewer signals.

### Already-researched instruments — do not re-run unless data is stale

Full results cached in `local_dev/research_results/`. Verdicts as of Jul 2026:

| Instrument | Verdict | Best confluence | ~/wk |
|---|---|---|---|
| **GBPUSD** | ✅★★★ | tod_15+conf2, WR 92% | 3.6 |
| **EURUSD** | ✅★★ | Frankfurt+conf2, WR 80% | 2.5 |
| **EURJPY** | ✅★★ | htf_ema+conf3, WR 76% | 2.8 |
| **XAUUSD** | ✅ (conf3) | (see cached results) | — |
| **GER40** | ✅ (conf3) | (see cached results) | — |
| USDJPY | ⚠️ marginal | age+conf3, WR 70%, 2/wk | 2.0 |
| GBPJPY | ⚠️ marginal | london+conf3, WR 67%, N=24 | 2.6 |
| USDCAD | ❌ | none viable | — |
| NZDUSD | ❌ | none viable | — |
| US100 | ❌ | nothing at N≥20+65% | — |

### What to check when researching a new instrument

1. **Does conf2 work?** Check `multi_bar_conf ≥2_bars` in the confluence table. If it's viable (N≥20, WR≥65%), it's the primary signal.
2. **If conf2 is not viable, try conf3.** Check `multi_bar_conf ≥3_bars`. Instruments like XAUUSD, GER40, EURJPY needed conf3.
3. **What frequency?** conf2 giving 6/wk is healthy. Below 2/wk is too infrequent for live trading.
4. **Do session filters add value on top of conf2?** On EURUSD, Frankfurt session improved conf2 from 70%→80% WR. Check `frankfurt+conf2`, `london+conf2`, `ny+conf2`.
5. **Does FVG add value?** `fvg+conf2` gave 75% WR on EURUSD. Check on new instruments.
6. **Does SL multiplier matter?** Run the TP/SL sweep. If WR is identical across 0.25/0.50/0.75, note it and recommend tightest SL.
7. **LTF retest frequency**: What % of conf2 signals also get a LTF retest? On EURUSD it was 35%. Higher retest rate = more opportunities to enter at a better price.
8. **Direction bias**: Check bull vs bear split. On EURUSD bear BOS had slightly higher WR (36% vs 27% baseline) — may matter more on trending instruments.
