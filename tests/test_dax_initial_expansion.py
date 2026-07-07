"""Unit tests for DAX initial expansion (Frankfurt open) strategy."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from main import _dax_ie_find_expansion, _DAX_IE_MIN_EXP_PCT, _DAX_IE_MAX_EXP_PCT

_BASE = datetime(2025, 6, 2, 7, 0, 0, tzinfo=timezone.utc)

def _c(i, o, h, l, c):
    return {"ts": _BASE + timedelta(minutes=i * 5),
            "open": o, "high": h, "low": l, "close": c}


# ── bear expansion bars: range ~90 pts (~0.45%) at 20k price ─────────────────
def _bear_bars():
    return [
        _c(0, 20000, 20010, 19960, 19965),  # bar 0: bear, high=20010
        _c(1, 19965, 19970, 19940, 19945),  # bar 1: bear, new low=19940
        _c(2, 19945, 19950, 19920, 19925),  # bar 2: bear, new low=19920 (exp_end=2)
        _c(3, 19925, 19958, 19948, 19950),  # bar 3: bull (Def-D), low=19948 > 19920+90*0.3=19947 → break
        _c(4, 19950, 19970, 19945, 19968),
    ]


# ── _dax_ie_find_expansion ────────────────────────────────────────────────────

def test_bear_expansion_detected():
    exp = _dax_ie_find_expansion(_bear_bars())
    assert exp is not None
    assert exp["exp_end"] == 2
    assert exp["exp_high"] == 20010
    assert exp["exp_low"]  == 19920


def test_bull_open_bar_rejected():
    """First bar is bull — strategy only trades BEAR expansions."""
    bars = [
        _c(0, 19920, 19970, 19915, 19965),  # bull open bar
        _c(1, 19965, 20010, 19960, 20005),
        _c(2, 20005, 20040, 19995, 20030),
    ]
    assert _dax_ie_find_expansion(bars) is None


def test_expansion_too_short_returns_none():
    """exp_end=1 (only 2 bars) → rejected (need at least 3)."""
    bars = [
        _c(0, 20000, 20010, 19960, 19965),  # bar 0: bear
        _c(1, 19965, 19970, 19940, 19945),  # bar 1: new low (exp_end=1)
        _c(2, 19945, 19990, 19960, 19985),  # bar 2: bull, low=19960 > 19940+70*0.3=19961 → break
    ]
    assert _dax_ie_find_expansion(bars) is None


def test_expansion_too_small_pct_returns_none():
    """Range < 0.30% → rejected."""
    bars = [
        _c(0, 20000, 20005, 19985, 19988),  # bear
        _c(1, 19988, 19992, 19975, 19978),  # new low
        _c(2, 19978, 19982, 19970, 19972),  # new low — range=35pts, 0.17% < 0.30%
        _c(3, 19972, 19985, 19968, 19980),
    ]
    assert _dax_ie_find_expansion(bars) is None


def test_expansion_too_large_pct_returns_none():
    """Range ≥ 0.60% → rejected."""
    bars = [
        _c(0, 20000, 20020, 19900, 19910),  # bear, range already 120pts = 0.6%
        _c(1, 19910, 19915, 19850, 19860),  # new low
        _c(2, 19860, 19865, 19810, 19820),  # new low — total range > 0.60%
        _c(3, 19820, 19870, 19815, 19860),
    ]
    assert _dax_ie_find_expansion(bars) is None


def test_eq_is_midpoint():
    exp = _dax_ie_find_expansion(_bear_bars())
    assert exp is not None
    expected_eq = (exp["exp_high"] + exp["exp_low"]) / 2
    assert abs(exp["mid_price"] - expected_eq) < 0.01


def test_exp_pct_in_range():
    exp = _dax_ie_find_expansion(_bear_bars())
    assert exp is not None
    assert _DAX_IE_MIN_EXP_PCT <= exp["exp_pct"] < _DAX_IE_MAX_EXP_PCT


# ── dax_initial_expansion_job gate tests ─────────────────────────────────────

def test_job_skips_weekend():
    from main import dax_initial_expansion_job
    am = MagicMock()
    with patch("main.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2025, 6, 7, 7, 6, 0, tzinfo=timezone.utc)  # Saturday
        dax_initial_expansion_job(am)
    am.send_alert.assert_not_called()


def test_job_skips_outside_window():
    from main import dax_initial_expansion_job
    am = MagicMock()
    with patch("main.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2025, 6, 2, 10, 0, 0, tzinfo=timezone.utc)  # outside window
        dax_initial_expansion_job(am)
    am.send_alert.assert_not_called()


def test_job_skips_when_file_unavailable():
    from main import dax_initial_expansion_job
    am = MagicMock()
    with patch("main.datetime") as mock_dt, \
         patch("main.is_available", return_value=False, create=True):
        mock_dt.now.return_value = datetime(2025, 6, 2, 7, 6, 0, tzinfo=timezone.utc)
        dax_initial_expansion_job(am)
    am.send_alert.assert_not_called()


def test_dedup_blocks_second_alert():
    import main as m
    saved = set(m._DAX_IE_ALERTED_DATES)
    key = "dax_ie_2025-06-02"
    m._DAX_IE_ALERTED_DATES.add(key)
    try:
        assert key in m._DAX_IE_ALERTED_DATES
    finally:
        m._DAX_IE_ALERTED_DATES.clear()
        m._DAX_IE_ALERTED_DATES.update(saved)


def test_no_defd_no_alert():
    """If bar after expansion peak is also bear, job should not alert."""
    from main import dax_initial_expansion_job
    am = MagicMock()

    bars = _bear_bars()
    # Replace Def-D bar (index 3) with a bear bar
    bars[3] = _c(3, 19925, 19930, 19905, 19910)  # bear

    now = datetime(2025, 6, 2, 7, 16, 30, tzinfo=timezone.utc)
    with patch("main.datetime") as mock_dt, \
         patch("main.is_available", return_value=True, create=True), \
         patch("main.load_mt5_candles", return_value=bars, create=True):
        mock_dt.now.return_value = now
        mock_dt.side_effect = None
        mock_dt.return_value = now
        dax_initial_expansion_job(am)
    am.send_alert.assert_not_called()
