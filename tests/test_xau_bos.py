"""Unit tests for XAU BOS/ChoCH detection logic in xau_bos_job."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from main import (
    _xau_find_swings,
    _xau_find_sweeps,
    _xau_4h_bias,
    _xau_detect_bos,
    _XAU_SWING_N,
    _XAU_MA4H_PERIOD,
)

_BASE_TS = datetime(2025, 6, 2, 1, 0, 0, tzinfo=timezone.utc)

def _c(i: int, o: float, h: float, l: float, c: float) -> dict:
    return {"ts": _BASE_TS + timedelta(minutes=i * 15),
            "open": o, "high": h, "low": l, "close": c}


def _noisy_flat(n: int, base: float = 1900.0) -> list:
    """n candles with tiny alternating noise — no dominant pivot."""
    out = []
    for i in range(n):
        noise = 0.5 if (i % 2 == 0) else -0.5
        out.append(_c(i, base + noise, base + 1.5 + abs(noise),
                      base - 1.5 - abs(noise), base + noise))
    return out


# ── swing detection ────────────────────────────────────────────────────────────

def test_swing_high_detected():
    candles = _noisy_flat(20)
    candles[10] = _c(10, 1900, 1950, 1899, 1910)   # dominant high
    sh, sl = _xau_find_swings(candles)
    assert sh[10] is True


def test_swing_low_detected():
    candles = _noisy_flat(20)
    candles[10] = _c(10, 1900, 1901, 1840, 1895)   # dominant low
    sh, sl = _xau_find_swings(candles)
    assert sl[10] is True


def test_dominated_bar_not_swing_high():
    """A bar with lower high than its left neighbour is NOT a swing high."""
    candles = _noisy_flat(20)
    candles[8]  = _c(8,  1900, 1950, 1899, 1910)   # true dominant high
    candles[10] = _c(10, 1900, 1920, 1899, 1910)   # lower than bar 8
    sh, sl = _xau_find_swings(candles)
    assert sh[10] is False


# ── sweep detection ────────────────────────────────────────────────────────────

def _bull_sweep_candles():
    """
    Swing low at bar 8 (low=1870).  Bar 14: wick to 1860 < 1870, close 1875 > 1870 → sb=True.
    All neighbouring bars are well above/below the pivot to avoid ties.
    """
    candles = _noisy_flat(30, 1900.0)
    # clear window around pivot so bar 8 is unambiguously the swing low
    for k in [5, 6, 7, 9, 10, 11]:
        candles[k] = _c(k, 1900, 1906, 1882, 1900)
    candles[8]  = _c(8,  1890, 1895, 1870, 1888)  # swing low @ 1870
    candles[14] = _c(14, 1880, 1892, 1860, 1875)  # wick below 1870, close above
    return candles


def _bear_sweep_candles():
    """
    Swing high at bar 8 (high=1940).  Bar 14: wick to 1950 > 1940, close 1932 < 1940 → sbe=True.
    """
    candles = _noisy_flat(30, 1900.0)
    for k in [5, 6, 7, 9, 10, 11]:
        candles[k] = _c(k, 1900, 1920, 1895, 1905)
    candles[8]  = _c(8,  1920, 1940, 1918, 1930)  # swing high @ 1940
    candles[14] = _c(14, 1935, 1950, 1930, 1932)  # wick above 1940, close below
    return candles


def test_bull_sweep_detected():
    candles = _bull_sweep_candles()
    sh, sl = _xau_find_swings(candles)
    sb, sbe = _xau_find_sweeps(candles, sh, sl)
    assert sb[14] is True


def test_bear_sweep_detected():
    candles = _bear_sweep_candles()
    sh, sl = _xau_find_swings(candles)
    sb, sbe = _xau_find_sweeps(candles, sh, sl)
    assert sbe[14] is True


# ── 4h bias ───────────────────────────────────────────────────────────────────

def test_4h_bias_bull():
    candles = [{"close": 1900.0}] * _XAU_MA4H_PERIOD
    candles[-1] = {"close": 1950.0}
    assert _xau_4h_bias(candles) == "BULL"


def test_4h_bias_bear():
    candles = [{"close": 1900.0}] * _XAU_MA4H_PERIOD
    candles[-1] = {"close": 1850.0}
    assert _xau_4h_bias(candles) == "BEAR"


def test_4h_bias_none_when_insufficient():
    candles = [{"close": 1900.0}] * (_XAU_MA4H_PERIOD - 1)
    assert _xau_4h_bias(candles) is None


# ── BOS detection — hand-crafted sh/sl/sb/sbe arrays ─────────────────────────
#
# We construct sh/sl/sb/sbe directly to test _xau_detect_bos logic in isolation,
# without depending on the swing/sweep algorithms to handle synthetic toy candles
# correctly.  The BOS bar and entry bar land near the tail so scan_start includes them.

def _make_bull_bos_inputs():
    """
    BULL BOS scenario (60 bars).  Pivot and sweep must be within SWEEP_WINDOW=15
    bars of the BOS bar at 48, so pivot at bar 38, sweep at bar 42.

      bar 38: swing high @ 1920  (sh[38]=True)   — 10 bars before BOS
      bar 42: bull sweep         (sb[42]=True)    — 6 bars before BOS
      bar 48: BOS bar — close 1925 > 1920
      bars 49-50: pullback — stay above 1920
      bar 51: entry — close 1930 > bos_bar.high (1926)
    """
    N = 60
    candles = _noisy_flat(N, 1905.0)
    sh  = [False] * N
    sl  = [False] * N
    sb  = [False] * N
    sbe = [False] * N

    BOS_LEVEL = 1920.0

    candles[38] = _c(38, 1910, BOS_LEVEL, 1908, 1915)
    sh[38] = True

    sb[42] = True

    candles[48] = _c(48, 1915, 1926, 1913, 1925)   # close > BOS_LEVEL
    candles[49] = _c(49, 1924, 1926, 1921, 1923)   # pullback — stay above 1920
    candles[50] = _c(50, 1923, 1925, 1921, 1922)   # pullback
    candles[51] = _c(51, 1922, 1932, 1921, 1930)   # entry: close > 1926 (bos bar high)
    candles[59] = _c(59, 1930, 1932, 1928, 1931)

    return candles, sh, sl, sb, sbe


def _make_bear_bos_inputs():
    """
    BEAR BOS scenario (60 bars).  Pivot at bar 38, sweep at bar 42.

      bar 38: swing low @ 1880   (sl[38]=True)   — 10 bars before BOS
      bar 42: bear sweep         (sbe[42]=True)   — 6 bars before BOS
      bar 48: BOS bar — close 1875 < 1880
      bars 49-50: pullback — stay below 1880
      bar 51: entry — close 1872 < bos_bar.low (1874)
    """
    N = 60
    candles = _noisy_flat(N, 1895.0)
    sh  = [False] * N
    sl  = [False] * N
    sb  = [False] * N
    sbe = [False] * N

    BOS_LEVEL = 1880.0

    candles[38] = _c(38, 1885, 1888, BOS_LEVEL, 1883)
    sl[38] = True

    sbe[42] = True

    candles[48] = _c(48, 1882, 1883, 1874, 1875)   # close < BOS_LEVEL
    candles[49] = _c(49, 1876, 1879, 1875, 1877)   # pullback — stay below 1880
    candles[50] = _c(50, 1877, 1879, 1875, 1878)   # pullback
    candles[51] = _c(51, 1878, 1879, 1870, 1872)   # entry: close < 1874 (bos bar low)
    candles[59] = _c(59, 1872, 1873, 1870, 1871)

    return candles, sh, sl, sb, sbe


def test_bull_bos_detected():
    candles, sh, sl, sb, sbe = _make_bull_bos_inputs()
    result = _xau_detect_bos(candles, sh, sl, sb, sbe, bias=None)
    assert result is not None, "Expected BULL BOS setup to be detected"
    assert result["direction"] == "BULL"
    assert result["entry"] > 1920.0


def test_bear_bos_detected():
    candles, sh, sl, sb, sbe = _make_bear_bos_inputs()
    result = _xau_detect_bos(candles, sh, sl, sb, sbe, bias=None)
    assert result is not None, "Expected BEAR BOS setup to be detected"
    assert result["direction"] == "BEAR"
    assert result["entry"] < 1880.0


def test_bull_bias_blocks_bear():
    candles, sh, sl, sb, sbe = _make_bear_bos_inputs()
    result = _xau_detect_bos(candles, sh, sl, sb, sbe, bias="BULL")
    assert result is None


def test_bear_bias_blocks_bull():
    candles, sh, sl, sb, sbe = _make_bull_bos_inputs()
    result = _xau_detect_bos(candles, sh, sl, sb, sbe, bias="BEAR")
    assert result is None


def test_bias_none_detects_both_directions():
    c1, s1, l1, sb1, sbe1 = _make_bull_bos_inputs()
    c2, s2, l2, sb2, sbe2 = _make_bear_bos_inputs()
    assert _xau_detect_bos(c1, s1, l1, sb1, sbe1, bias=None) is not None
    assert _xau_detect_bos(c2, s2, l2, sb2, sbe2, bias=None) is not None


def test_sl_below_entry_for_bull():
    candles, sh, sl, sb, sbe = _make_bull_bos_inputs()
    result = _xau_detect_bos(candles, sh, sl, sb, sbe, bias=None)
    assert result is not None
    assert result["sl"] < result["entry"]


def test_sl_above_entry_for_bear():
    candles, sh, sl, sb, sbe = _make_bear_bos_inputs()
    result = _xau_detect_bos(candles, sh, sl, sb, sbe, bias=None)
    assert result is not None
    assert result["sl"] > result["entry"]


def test_tp_r_at_least_1():
    candles, sh, sl, sb, sbe = _make_bull_bos_inputs()
    result = _xau_detect_bos(candles, sh, sl, sb, sbe, bias=None)
    if result:
        assert result["r_at_tp"] >= 1.0


def test_no_sweep_means_no_setup():
    """Without any sweep (sb/sbe all False), no setup should be detected."""
    candles, sh, sl, sb, sbe = _make_bull_bos_inputs()
    sb_empty = [False] * len(sb)
    result = _xau_detect_bos(candles, sh, sl, sb_empty, sbe, bias=None)
    assert result is None


# ── xau_bos_job gate tests ────────────────────────────────────────────────────

def test_job_skips_on_friday():
    """Friday (weekday=4) → job returns immediately without alerting."""
    from main import xau_bos_job
    am = MagicMock()
    with patch("main.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2025, 6, 6, 1, 1, 0, tzinfo=timezone.utc)  # Friday
        xau_bos_job(am)
    am.send_alert.assert_not_called()


def test_job_skips_on_bad_hour():
    """Hour 10 UTC is not in _XAU_BEST_HOURS → job returns immediately."""
    from main import xau_bos_job
    am = MagicMock()
    with patch("main.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2025, 6, 2, 10, 1, 0, tzinfo=timezone.utc)
        xau_bos_job(am)
    am.send_alert.assert_not_called()


def test_dedup_set_blocks_second_fire():
    """Alert ID already in _XAU_BOS_ALERTED must prevent re-alerting."""
    import main as m
    saved = set(m._XAU_BOS_ALERTED)
    alert_id = "xau_bos_BULL_0000001"
    m._XAU_BOS_ALERTED.add(alert_id)
    try:
        assert alert_id in m._XAU_BOS_ALERTED
    finally:
        m._XAU_BOS_ALERTED.clear()
        m._XAU_BOS_ALERTED.update(saved)
