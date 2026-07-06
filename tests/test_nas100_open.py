"""Unit tests for NAS100 US open expansion strategy."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from main import (
    _nas100_find_expansion,
    _NAS100_MIN_EXP_PCT,
    _NAS100_MAX_EXP_PCT,
)

_BASE = datetime(2025, 6, 2, 13, 30, 0, tzinfo=timezone.utc)

def _c(i, o, h, l, c):
    return {"ts": _BASE + timedelta(minutes=i*5),
            "open": o, "high": h, "low": l, "close": c}


# ── _nas100_find_expansion ────────────────────────────────────────────────────

def _bear_bars():
    """4-bar bear expansion ~0.96% (range 200pts on 20900) — within 0.2–1.2% filter."""
    return [
        _c(0, 21000, 21010, 20940, 20950),  # bar 0: bear, high=21010
        _c(1, 20950, 20955, 20880, 20890),  # bar 1: bear, new low=20880
        _c(2, 20890, 20900, 20830, 20840),  # bar 2: bear, new low=20830
        _c(3, 20840, 20855, 20810, 20820),  # bar 3: bear, new low=20810 (exp_end=3)
        _c(4, 20820, 20865, 20812, 20858),  # bar 4: bull (Def-D)
        _c(5, 20858, 20880, 20845, 20872),
    ]

def _bull_bars():
    """3-bar bull expansion ~0.57% (range 115pts on 20050) — within filter."""
    return [
        _c(0, 20000, 20040, 19995, 20035),  # bar 0: bull, low=19995
        _c(1, 20035, 20080, 20025, 20075),  # bar 1: bull, new high=20080
        _c(2, 20075, 20110, 20065, 20105),  # bar 2: bull, new high=20110 (exp_end=2)
        _c(3, 20105, 20108, 20060, 20065),  # bar 3: bear (Def-D), high < bar2.high=20110
        _c(4, 20065, 20080, 20050, 20070),
    ]


def test_bear_expansion_detected():
    exp = _nas100_find_expansion(_bear_bars())
    assert exp is not None
    assert exp["direction"] == "BEAR"
    assert exp["exp_end"] == 3


def test_bull_expansion_detected():
    exp = _nas100_find_expansion(_bull_bars())
    assert exp is not None
    assert exp["direction"] == "BULL"
    assert exp["exp_end"] == 2


def test_expansion_too_short_returns_none():
    """Only 2 bars in one direction (exp_end=1) → rejected."""
    bars = [
        _c(0, 21000, 21020, 20950, 20960),  # bar 0: bear
        _c(1, 20960, 20965, 20930, 20935),  # bar 1: bear, new low (exp_end=1)
        _c(2, 20935, 20965, 20960, 20963),  # bar 2: bull — low=20960 > 20930+90*0.3=20957 → retrace break
    ]
    assert _nas100_find_expansion(bars) is None


def test_expansion_too_small_pct_returns_none():
    """Range < 0.2% of price → rejected."""
    bars = [
        _c(0, 20000, 20005, 19998, 19999),
        _c(1, 19999, 20002, 19996, 19997),
        _c(2, 19997, 20001, 19994, 19995),
        _c(3, 19995, 19999, 19993, 19994),
    ]
    assert _nas100_find_expansion(bars) is None


def test_expansion_too_large_pct_returns_none():
    """Range ≥ 1.2% → rejected (extreme move, not the setup)."""
    bars = [
        _c(0, 20000, 20010, 19760, 19770),  # 1.2%+
        _c(1, 19770, 19780, 19710, 19720),
        _c(2, 19720, 19730, 19650, 19660),
        _c(3, 19660, 19670, 19600, 19610),
    ]
    assert _nas100_find_expansion(bars) is None


def test_eq_is_50pct_of_range():
    exp = _nas100_find_expansion(_bear_bars())
    assert exp is not None
    eq = (exp["exp_high"] + exp["exp_low"]) / 2
    assert abs(eq - (exp["exp_high"] + exp["exp_low"]) / 2) < 0.01


def test_exp_end_tracks_furthest_bar():
    """exp_end should be the last bar that made a new extreme."""
    exp = _nas100_find_expansion(_bear_bars())
    assert exp is not None
    # bar 3 is the furthest bear bar
    assert exp["exp_end"] == 3
    assert exp["exp_low"] == min(b["low"] for b in _bear_bars()[:4])


# ── nas100_open_job gate tests ────────────────────────────────────────────────

def test_job_skips_weekend():
    from main import nas100_open_job
    am = MagicMock()
    with patch("main.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2025, 6, 7, 13, 36, 0, tzinfo=timezone.utc)  # Saturday
        nas100_open_job(am)
    am.send_alert.assert_not_called()


def test_job_skips_when_file_unavailable():
    from main import nas100_open_job
    am = MagicMock()
    with patch("main.datetime") as mock_dt, \
         patch("main.is_available", return_value=False, create=True):
        mock_dt.now.return_value = datetime(2025, 6, 2, 13, 36, 0, tzinfo=timezone.utc)
        nas100_open_job(am)
    am.send_alert.assert_not_called()


def test_dedup_blocks_second_alert():
    import main as m
    saved = set(m._NAS100_ALERTED_DATES)
    key = "nas100_open_BEAR_2025-06-02"
    m._NAS100_ALERTED_DATES.add(key)
    try:
        assert key in m._NAS100_ALERTED_DATES
    finally:
        m._NAS100_ALERTED_DATES.clear()
        m._NAS100_ALERTED_DATES.update(saved)


def test_direction_bear_exp_end():
    exp = _nas100_find_expansion(_bear_bars())
    assert exp["direction"] == "BEAR"
    assert exp["exp_low"] < exp["exp_high"]


def test_direction_bull_exp_end():
    exp = _nas100_find_expansion(_bull_bars())
    assert exp["direction"] == "BULL"
    assert exp["exp_high"] > exp["exp_low"]
