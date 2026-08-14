"""
Unit tests for analysis.bollinger_scanner.

Synthetic test data is crafted so the BB/MA conditions are known exactly:
  Signal data:       22 bars at 95, then [84, 83, 84.5, 84.7]
  No-proximity data: same shape but last bar at 90 (>2% above lower BB)
  No-cross data:     22 bars at 95, then [84.5, 84.7, 84.6, 84.3] (declining, no upward cross)

For signal data the 20-bar window (bars 6-25) is [95]×16 + [84, 83, 84.5, 84.7]:
  mean ≈ 92.81, std ≈ 4.50, lower_bb ≈ 83.81, upper_bb ≈ 101.81
  2MA[-1]=(84.7+84.5)/2=84.6 > 4MA[-1]=(84.7+84.5+83+84)/4=84.05  → cross  ✓
  2MA[-2]=(84.5+83)/2=83.75  ≤ 4MA[-2]=(84.5+83+84+95)/4=86.625   → prev ok ✓
  close=84.7 ≤ 83.81×1.02=85.49                                      → prox  ✓

Lookback test data: 22 bars at 95, then [84, 83, 86.5, 88.0]
  Touch bars: bars[-4]=84, bars[-3]=83 both within 2% of lower_bb (~83.81)
  bars[-2]=86.5, bars[-1]=88.0 — both above proximity threshold (~85.49)
  2MA[-1]=(88+86.5)/2=87.25 > 4MA[-1]=(88+86.5+83+84)/4=85.375  → cross ✓
  → lookback=0: no signal (last bar=88 > 85.49)
  → lookback=5: signal (touch at bars[-3] or [-4])
"""

import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from analysis.bollinger_scanner import (
    _bb_ma_cross,
    _bw_metrics,
    _detect_signal,
    _get_earnings_gap,
    _weekly_pct,
    format_bollinger_daily,
    format_bollinger_earnings_gap,
    format_bollinger_weekly,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _signal_data(n: int = 26) -> np.ndarray:
    c = np.zeros(n)
    c[: n - 4] = 95.0
    c[n - 4] = 84.0
    c[n - 3] = 83.0
    c[n - 2] = 84.5
    c[n - 1] = 84.7
    return c


def _no_proximity_data(n: int = 26) -> np.ndarray:
    """Same structure but last bar at 90 — above lower_bb×1.02≈85.49."""
    c = np.zeros(n)
    c[: n - 4] = 95.0
    c[n - 4] = 84.0
    c[n - 3] = 83.0
    c[n - 2] = 84.5
    c[n - 1] = 90.0
    return c


def _no_cross_data(n: int = 26) -> np.ndarray:
    """Declining last bar: 2MA[-1]<4MA[-1] → no upward cross."""
    c = np.zeros(n)
    c[: n - 4] = 95.0
    c[n - 4] = 84.5
    c[n - 3] = 84.7
    c[n - 2] = 84.6
    c[n - 1] = 84.3
    return c


def _lookback_data(n: int = 26) -> np.ndarray:
    """Touch at bars[-4] and [-3], cross at bars[-1]. Cross bar above proximity."""
    c = np.zeros(n)
    c[: n - 4] = 95.0
    c[n - 4] = 84.0   # within 2% of lower_bb
    c[n - 3] = 83.0   # within 2% of lower_bb
    c[n - 2] = 86.5   # above proximity
    c[n - 1] = 88.0   # cross bar, above proximity
    return c


# ── _detect_signal (and _bb_ma_cross alias) ───────────────────────────────────

def test_detect_signal_basic():
    result = _detect_signal(_signal_data())
    assert result is not None
    assert result["close"] == pytest.approx(84.7)
    assert result["pct_above_lower"] <= 2.0
    assert result["upper_bb"] > result["mid_bb"] > result["lower_bb"]


def test_detect_signal_returns_bars_since_touch():
    result = _detect_signal(_signal_data())
    assert result is not None
    assert "bars_since_touch" in result
    assert result["bars_since_touch"] == 0


def test_detect_signal_returns_all_keys():
    result = _detect_signal(_signal_data())
    assert result is not None
    for key in ("close", "lower_bb", "mid_bb", "upper_bb", "pct_above_lower", "bars_since_touch"):
        assert key in result


def test_detect_signal_no_proximity():
    result = _detect_signal(_no_proximity_data())
    assert result is None


def test_detect_signal_no_cross():
    result = _detect_signal(_no_cross_data())
    assert result is None


def test_detect_signal_insufficient_data():
    result = _detect_signal(np.ones(10))
    assert result is None


def test_detect_signal_lookback_0_no_signal():
    """lookback=0: touch must be on the cross bar; lookback data has touch 1-2 bars before → None."""
    result = _detect_signal(_lookback_data(), lookback=0)
    assert result is None


def test_detect_signal_lookback_5_signal():
    """lookback=5: touch 2 bars ago is within window → signal returned."""
    result = _detect_signal(_lookback_data(), lookback=5)
    assert result is not None
    assert result["bars_since_touch"] >= 1


def test_detect_signal_lookback_bars_since_touch():
    """bars_since_touch is non-zero: touch happened before the cross bar."""
    result = _detect_signal(_lookback_data(), lookback=5)
    assert result is not None
    # Touch was before the cross bar (search newest-to-oldest, so >= 1)
    assert result["bars_since_touch"] >= 1


# ── _bw_metrics ──────────────────────────────────────────────────────────────

def test_bw_metrics_signal_data():
    # _bw_metrics needs BB_PERIOD(20) + BW_AVG_PERIOD(10) + 2 = 32 bars minimum
    result = _bw_metrics(_signal_data(n=36))
    assert result is not None
    bwp, bwc_ratio = result
    assert bwp > 0
    assert bwc_ratio > 0


def test_bw_metrics_insufficient_data():
    result = _bw_metrics(np.ones(10))
    assert result is None


def test_bw_metrics_bwp_reasonable():
    result = _bw_metrics(_signal_data(n=36))
    assert result is not None
    bwp, _ = result
    assert 5.0 < bwp < 50.0


# ── _weekly_pct ───────────────────────────────────────────────────────────────

def _make_weekly_series(n: int = 30, trend: str = "up") -> pd.Series:
    """Synthetic weekly series — uptrend ends near upper BB, downtrend near lower."""
    if trend == "up":
        closes = np.linspace(80, 120, n)
    else:
        closes = np.linspace(120, 80, n)
    idx = pd.date_range(end="2026-08-08", periods=n, freq="W-FRI")
    return pd.Series(closes, index=idx)


def test_weekly_pct_uptrend_near_upper():
    s = _make_weekly_series(30, trend="up")
    pct = _weekly_pct(s)
    assert pct is not None
    assert pct > 0.5   # rising into upper band


def test_weekly_pct_downtrend_near_lower():
    s = _make_weekly_series(30, trend="down")
    pct = _weekly_pct(s)
    assert pct is not None
    assert pct < 0.5   # falling toward lower band


def test_weekly_pct_insufficient_data():
    s = pd.Series(np.ones(5))
    assert _weekly_pct(s) is None


# ── _bb_ma_cross alias ────────────────────────────────────────────────────────

def test_bb_ma_cross_signal_detected():
    result = _bb_ma_cross(_signal_data())
    assert result is not None
    assert result["close"] == pytest.approx(84.7)
    assert result["pct_above_lower"] <= 2.0
    assert result["upper_bb"] > result["mid_bb"] > result["lower_bb"]


def test_bb_ma_cross_returns_all_keys():
    result = _bb_ma_cross(_signal_data())
    assert result is not None
    for key in ("close", "lower_bb", "mid_bb", "upper_bb", "pct_above_lower"):
        assert key in result


def test_bb_ma_cross_no_proximity():
    result = _bb_ma_cross(_no_proximity_data())
    assert result is None


def test_bb_ma_cross_no_cross():
    result = _bb_ma_cross(_no_cross_data())
    assert result is None


def test_bb_ma_cross_insufficient_data():
    result = _bb_ma_cross(np.ones(10))
    assert result is None


def test_bb_ma_cross_minimum_bars():
    result = _bb_ma_cross(_signal_data(n=26))
    assert result is not None


def test_bb_ma_cross_one_bar_below_minimum():
    result = _bb_ma_cross(_signal_data(n=25))
    assert result is None


# ── _get_earnings_gap ─────────────────────────────────────────────────────────

def _make_price_series(gap_date: date, gap_magnitude: float = -0.09) -> pd.Series:
    """Business-day price series ending today, with a gap on *gap_date*."""
    end = pd.Timestamp.today().normalize()
    dates = pd.bdate_range(end=end, periods=80)
    closes = pd.Series(np.ones(80) * 100.0, index=dates)
    pos_arr = np.where(dates.date == gap_date)[0]
    if len(pos_arr) > 0:
        p = int(pos_arr[0])
        if p > 0:
            closes.iloc[p] = closes.iloc[p - 1] * (1 + gap_magnitude)
    return closes


def _weekday_before(d: date, offset_days: int) -> date:
    """Return a weekday (Mon-Fri) approximately *offset_days* ago."""
    candidate = d - timedelta(days=offset_days)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _mock_ticker(gap_date: date):
    mock_ed = pd.DataFrame(index=pd.DatetimeIndex([pd.Timestamp(gap_date)]))
    mock_t = MagicMock()
    mock_t.earnings_dates = mock_ed
    return mock_t


def test_get_earnings_gap_within_window():
    gap_date = _weekday_before(date.today(), 20)
    closes = _make_price_series(gap_date, gap_magnitude=-0.09)
    with patch("analysis.bollinger_scanner.yf") as mock_yf:
        mock_yf.Ticker.return_value = _mock_ticker(gap_date)
        result_date, result_pct = _get_earnings_gap("TEST", closes)
    assert result_date is not None
    assert result_pct <= -7.0


def test_get_earnings_gap_outside_lookback():
    gap_date = _weekday_before(date.today(), 75)  # older than 60-day lookback
    closes = _make_price_series(gap_date, gap_magnitude=-0.09)
    with patch("analysis.bollinger_scanner.yf") as mock_yf:
        mock_yf.Ticker.return_value = _mock_ticker(gap_date)
        result_date, result_pct = _get_earnings_gap("TEST", closes)
    assert result_date is None


def test_get_earnings_gap_insufficient_drop():
    gap_date = _weekday_before(date.today(), 15)
    closes = _make_price_series(gap_date, gap_magnitude=-0.03)
    with patch("analysis.bollinger_scanner.yf") as mock_yf:
        mock_yf.Ticker.return_value = _mock_ticker(gap_date)
        result_date, result_pct = _get_earnings_gap("TEST", closes)
    assert result_date is None


def test_get_earnings_gap_no_earnings_data():
    closes = _make_price_series(_weekday_before(date.today(), 15))
    with patch("analysis.bollinger_scanner.yf") as mock_yf:
        mock_t = MagicMock()
        mock_t.earnings_dates = None
        mock_yf.Ticker.return_value = mock_t
        result_date, result_pct = _get_earnings_gap("TEST", closes)
    assert result_date is None
    assert result_pct == 0.0


# ── format_bollinger_weekly ───────────────────────────────────────────────────

def test_format_weekly_empty():
    msg = format_bollinger_weekly([])
    assert "bollinger_weekly" in msg
    assert "No bollinger_weekly setups" in msg


def test_format_weekly_with_setup():
    setups = [
        {
            "ticker":          "AAPL",
            "bar_date":        date(2026, 8, 14),
            "close":           185.50,
            "lower_bb":        184.00,
            "mid_bb":          200.00,
            "upper_bb":        216.00,
            "pct_above_lower": 0.82,
            "bars_since_touch": 1,
        }
    ]
    msg = format_bollinger_weekly(setups)
    assert "AAPL" in msg
    assert "bollinger_weekly" in msg
    assert "SL" in msg
    assert "TP" in msg
    assert "184.00" in msg
    assert "216.00" in msg
    assert "upper BB" in msg


# ── format_bollinger_daily ────────────────────────────────────────────────────

def test_format_daily_empty():
    msg = format_bollinger_daily([])
    assert "bollinger_daily" in msg
    assert "No bollinger_daily setups" in msg


def test_format_daily_with_setup_today():
    setups = [
        {
            "ticker":           "MSFT",
            "bar_date":         date(2026, 8, 13),
            "close":            390.00,
            "lower_bb":         385.00,
            "mid_bb":           420.00,
            "upper_bb":         455.00,
            "pct_above_lower":  1.30,
            "bars_since_touch": 0,
            "bwp":              18.0,
            "bwc_ratio":        1.20,
            "weekly_pct":       0.85,
        }
    ]
    msg = format_bollinger_daily(setups)
    assert "MSFT" in msg
    assert "bollinger_daily" in msg
    assert "SL" in msg
    assert "MA cross" in msg
    assert "385.00" in msg
    assert "today" in msg
    assert "18.0%" in msg
    assert "85" in msg   # weekly_pct shown as 85%


def test_format_daily_with_setup_bars_ago():
    setups = [
        {
            "ticker":           "GOOGL",
            "bar_date":         date(2026, 8, 13),
            "close":            175.00,
            "lower_bb":         170.00,
            "mid_bb":           185.00,
            "upper_bb":         200.00,
            "pct_above_lower":  2.0,
            "bars_since_touch": 3,
            "bwp":              10.5,
            "bwc_ratio":        1.30,
            "weekly_pct":       0.82,
        }
    ]
    msg = format_bollinger_daily(setups)
    assert "GOOGL" in msg
    assert "3d ago" in msg
    assert "expanding" in msg   # bwc_ratio 1.30 → "expanding ↗"


def test_format_daily_stats_line():
    msg = format_bollinger_daily([])
    assert "77.1%" in msg
    assert "EV" in msg


# ── format_bollinger_earnings_gap ─────────────────────────────────────────────

def test_format_earnings_gap_empty():
    msg = format_bollinger_earnings_gap([])
    assert "bollinger_earnings_gap" in msg
    assert "No bollinger_earnings_gap setups" in msg


def test_format_earnings_gap_with_setup():
    setups = [
        {
            "ticker":          "ORCL",
            "bar_date":        date(2026, 8, 14),
            "close":           185.00,
            "lower_bb":        183.00,
            "mid_bb":          200.00,
            "upper_bb":        217.00,
            "pct_above_lower": 1.09,
            "gap_date":        date(2026, 7, 15),
            "gap_pct":         -8.5,
            "days_since_gap":  30,
        }
    ]
    msg = format_bollinger_earnings_gap(setups)
    assert "ORCL" in msg
    assert "bollinger_earnings_gap" in msg
    assert "-8.5%" in msg
    assert "30d ago" in msg
    assert "SL" in msg
    assert "TP" in msg
    assert "183.00" in msg
    assert "217.00" in msg
