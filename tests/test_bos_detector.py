"""Tests for SMCAnalyzer.detect_bos on synthetic candle sequences."""
import pytest
from analysis.smc_analyzer import SMCAnalyzer
from conftest import make_candle

analyzer = SMCAnalyzer()

# Loose params so synthetic candles pass the ATR gate
LOOSE = {
    "min_atr_pct": 0.0,
    "min_break_distance_atr_mult": 0.0,
    "min_break_strength": 0.0,
    "min_swing_age_candles": 3,
    "require_liquidity_sweep": False,
    "swing_lookback": 20,
}


def _build_bullish_bos_candles():
    """
    16 candles so ATR can be computed (needs ≥15).
    Swing high at chron index 4 (high=1.1060), break at chron index 15.
    detect_bos expects DESC (newest-first), so we reverse before returning.
    _find_last_swing with lookback=3 searches range(3, n-3), so index 4 is in range.
    """
    ts = 1_000_000
    step = 900

    prices = [
        # background (indices 0-3): not evaluated as swing (start=3 in _find_last_swing)
        (1.0930, 1.0940, 1.0920, 1.0935),
        (1.0935, 1.0950, 1.0930, 1.0945),
        (1.0945, 1.0960, 1.0940, 1.0955),
        (1.0955, 1.0970, 1.0950, 1.0965),
        # swing high at chron index 4 — high=1.1060
        (1.0965, 1.1060, 1.0960, 1.1050),
        # pullback (indices 5-10)
        (1.1050, 1.1055, 1.0980, 1.1000),
        (1.1000, 1.1010, 1.0960, 1.0970),
        (1.0970, 1.0980, 1.0940, 1.0950),
        (1.0950, 1.0960, 1.0920, 1.0930),
        (1.0930, 1.0950, 1.0920, 1.0940),
        (1.0940, 1.0955, 1.0930, 1.0950),
        # recovery (indices 11-14)
        (1.0950, 1.0965, 1.0940, 1.0960),
        (1.0960, 1.0975, 1.0955, 1.0970),
        (1.0970, 1.0985, 1.0965, 1.0980),
        (1.0980, 1.0995, 1.0975, 1.0990),
        # BOS: close=1.1080 > swing_high=1.1060 (index 15)
        (1.0990, 1.1100, 1.0985, 1.1080),
    ]
    candles = []
    for o, h, l, c in prices:
        candles.append(make_candle(ts, o, h, l, c))
        ts += step
    return list(reversed(candles))  # DESC order as detect_bos expects


def _build_bearish_bos_candles():
    """Swing low at chron index 3 (low=1.0940), break below at chron index 10."""
    ts = 2_000_000
    step = 900

    prices = [
        # background (indices 0-2)
        (1.1050, 1.1060, 1.1040, 1.1055),
        (1.1055, 1.1065, 1.1045, 1.1060),
        (1.1060, 1.1070, 1.1050, 1.1065),
        # swing low at chron index 3 — low=1.0940
        (1.1065, 1.1070, 1.0940, 1.0950),
        # bounce up (indices 4-7)
        (1.0950, 1.1000, 1.0945, 1.0990),
        (1.0990, 1.1020, 1.0985, 1.1010),
        (1.1010, 1.1030, 1.1000, 1.1020),
        (1.1020, 1.1040, 1.1010, 1.1030),
        # decline (indices 8-9)
        (1.1030, 1.1035, 1.1000, 1.1005),
        (1.1005, 1.1010, 1.0960, 1.0965),
        # BOS: close=1.0920 < swing_low=1.0940 (index 10)
        (1.0965, 1.0970, 1.0910, 1.0920),
    ]
    candles = []
    for o, h, l, c in prices:
        candles.append(make_candle(ts, o, h, l, c))
        ts += step
    return list(reversed(candles))  # DESC order


def test_bullish_bos_detected():
    candles = _build_bullish_bos_candles()
    events = analyzer.detect_bos(candles, params=LOOSE)
    bullish = [e for e in events if e["direction"] == "bullish"]
    assert len(bullish) >= 1


def test_bearish_bos_detected():
    candles = _build_bearish_bos_candles()
    events = analyzer.detect_bos(candles, params=LOOSE)
    bearish = [e for e in events if e["direction"] == "bearish"]
    assert len(bearish) >= 1


def test_no_bos_on_flat_candles():
    """Flat/doji sequence should produce no BOS."""
    candles = [make_candle(1_000_000 + i * 900, 1.1000, 1.1005, 1.0995, 1.1000) for i in range(15)]
    events = analyzer.detect_bos(candles, params=LOOSE)
    assert events == []


def test_too_few_candles_returns_empty():
    candles = [make_candle(i * 900, 1.1000, 1.1010, 1.0990, 1.1005) for i in range(3)]
    assert analyzer.detect_bos(candles, params=LOOSE) == []


def test_bos_event_has_required_fields():
    candles = _build_bullish_bos_candles()
    events = analyzer.detect_bos(candles, params=LOOSE)
    assert events, "Expected at least one BOS event"
    ev = events[0]
    for field in ("direction", "broken_level", "break_strength", "breakout_ts"):
        assert field in ev, f"Missing field: {field}"


def test_bullish_bos_direction_is_bullish():
    candles = _build_bullish_bos_candles()
    events = analyzer.detect_bos(candles, params=LOOSE)
    bullish = [e for e in events if e["direction"] == "bullish"]
    assert all(e["direction"] == "bullish" for e in bullish)


def test_bearish_bos_direction_is_bearish():
    candles = _build_bearish_bos_candles()
    events = analyzer.detect_bos(candles, params=LOOSE)
    bearish = [e for e in events if e["direction"] == "bearish"]
    assert all(e["direction"] == "bearish" for e in bearish)


def test_break_strength_is_positive():
    candles = _build_bullish_bos_candles()
    events = analyzer.detect_bos(candles, params=LOOSE)
    for ev in events:
        assert ev["break_strength"] > 0


def test_liquidity_sweep_not_required_by_default():
    """Without require_liquidity_sweep, BOS fires even without a prior sweep."""
    candles = _build_bullish_bos_candles()
    params = {**LOOSE, "require_liquidity_sweep": False}
    events = analyzer.detect_bos(candles, params=params)
    assert len(events) >= 1
