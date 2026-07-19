"""Tests for weekly_retest_scanner — _pre_score, watchlist functions, and existing scan logic."""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import analysis.weekly_retest_scanner as wrs

# ── DataFrame builders ─────────────────────────────────────────────────────────

LEVEL    = 100.0
SWING_I  = 5
BOS_I    = 11   # first close above level
N        = 30


def _make_watchlist_df(
    level=LEVEL, n=N, swing_i=SWING_I, bos_i=BOS_I,
    post_close=114.0, post_high=122.0, post_low=113.5,
    bos_vol=1_500_000.0, base_vol=1_000_000.0,
    broken_at=None, retested_at=None,
    consol_closes=None,
):
    """
    Synthetic weekly DataFrame with a live, unretested BOS structure.

    Layout:
      bars 0..swing_i-1   → below level
      bar  swing_i         → swing high = level
      bars swing_i+1..bos_i-1 → close below level
      bars bos_i..bos_i+3  → 4 consecutive closes above level (BOS confirmed)
      bars bos_i+4..n-1    → post-confirm (elevated, no touch of 3% zone)
    """
    dates  = pd.date_range("2020-01-01", periods=n, freq="W")
    highs  = np.full(n, level * 0.92)
    lows   = np.full(n, level * 0.88)
    opens  = np.full(n, level * 0.90)
    closes = np.full(n, level * 0.90)
    vols   = np.full(n, base_vol)

    highs[swing_i] = level

    for k in range(bos_i, bos_i + 4):
        closes[k] = level * 1.01
        highs[k]  = level * 1.02
        lows[k]   = level * 1.005

    if consol_closes is not None:
        for k, c in zip(range(bos_i, bos_i + 4), consol_closes):
            closes[k] = c

    vols[bos_i] = bos_vol

    confirm_end = bos_i + 3
    for k in range(confirm_end + 1, n):
        closes[k] = post_close
        highs[k]  = post_high
        lows[k]   = post_low

    if broken_at is not None:
        closes[broken_at] = level * 0.99
    if retested_at is not None:
        lows[retested_at] = level * 1.02   # ≤ level × 1.03 → triggers retested flag

    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vols},
        index=dates,
    )


def _make_scan_df(n=60, level=100.0):
    """
    Synthetic weekly DataFrame for _find_setups:
    swing high at bar 5, BOS at 11, 4-bar confirmation, retest at bar 20.
    """
    dates  = pd.date_range("2018-01-01", periods=n, freq="W")
    highs  = np.full(n, level * 0.92)
    lows   = np.full(n, level * 0.88)
    opens  = np.full(n, level * 0.90)
    closes = np.full(n, level * 0.90)
    vols   = np.full(n, 1_000_000.0)

    highs[5] = level  # swing high

    # BOS + confirmation bars 11-14
    for k in range(11, 15):
        closes[k] = level * 1.01
        highs[k]  = level * 1.02
        lows[k]   = level * 1.005

    # Hold above level bars 15-19
    for k in range(15, 20):
        closes[k] = level * 1.01
        highs[k]  = level * 1.02
        lows[k]   = level * 1.005

    # Retest bar 20: low ≤ level × 1.02
    closes[20] = level * 1.008
    highs[20]  = level * 1.015
    lows[20]   = level * 1.001

    # Rally after retest
    for k in range(21, n):
        closes[k] = level * 1.15
        highs[k]  = level * 1.17
        lows[k]   = level * 1.12

    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vols},
        index=dates,
    )


# ── _pre_score ──────────────────────────────────────────────────────────────────

class TestPreScore:
    def test_max_score_all_criteria_met(self):
        assert wrs._pre_score(20, 20.0, 1.5, 1.5) == 5

    def test_zero_score_nothing_met(self):
        assert wrs._pre_score(3, 5.0, 0.5, 1.0) == 0

    def test_consec_exactly_6_gives_one_point(self):
        assert wrs._pre_score(6, 0.0, 0.0, 0.0) == 1

    def test_consec_14_only_one_point(self):
        # consec >= 6 (+1) but < 15 → only 1 pt
        assert wrs._pre_score(14, 0.0, 0.0, 0.0) == 1

    def test_consec_15_gives_two_points(self):
        assert wrs._pre_score(15, 0.0, 0.0, 0.0) == 2

    def test_peak_gain_threshold(self):
        # consec=6(+1) + peak=15%(+1)
        assert wrs._pre_score(6, 15.0, 0.0, 0.0) == 2

    def test_peak_below_threshold_no_point(self):
        assert wrs._pre_score(6, 14.9, 0.0, 0.0) == 1

    def test_consol_threshold(self):
        # consec=6(+1) + consol=1.0%(+1)
        assert wrs._pre_score(6, 0.0, 1.0, 0.0) == 2

    def test_vol_ratio_threshold(self):
        # consec=6(+1) + vol=1.25(+1)
        assert wrs._pre_score(6, 0.0, 0.0, 1.25) == 2

    def test_score_4_missing_vol(self):
        # all except bos_vol_ratio < 1.25
        assert wrs._pre_score(15, 20.0, 1.5, 1.0) == 4

    def test_score_4_missing_consol(self):
        # all except consol_min_pct
        assert wrs._pre_score(15, 20.0, 0.9, 1.5) == 4

    def test_score_boundaries_exact(self):
        # Each threshold exactly at boundary
        assert wrs._pre_score(
            wrs.WATCHLIST_MIN_CONSEC,
            wrs.WATCHLIST_MIN_PEAK,
            wrs.WATCHLIST_MIN_CONSOL,
            wrs.WATCHLIST_MIN_BOS_VOL,
        ) == 5


# ── _find_watchlist_candidate ───────────────────────────────────────────────────

class TestFindWatchlistCandidate:

    def test_clean_bos_found(self):
        df = _make_watchlist_df()
        r = wrs._find_watchlist_candidate(df, "TEST")
        assert r is not None
        assert r["ticker"] == "TEST"
        assert abs(r["level"] - LEVEL) < 0.01

    def test_consec_total_correct(self):
        df = _make_watchlist_df()
        r = wrs._find_watchlist_candidate(df, "TEST")
        assert r is not None
        # consec_total = n - 1 - bos_idx = 29 - 11 = 18
        assert r["consec_total"] == N - 1 - BOS_I

    def test_current_pct_above_within_bounds(self):
        df = _make_watchlist_df(post_close=114.0)
        r = wrs._find_watchlist_candidate(df, "TEST")
        assert r is not None
        assert 0 < r["current_pct_above"] < wrs.WATCHLIST_MAX_PCT_ABOVE

    def test_bos_vol_ratio_computed(self):
        df = _make_watchlist_df(bos_vol=2_000_000.0, base_vol=1_000_000.0)
        r = wrs._find_watchlist_candidate(df, "TEST")
        assert r is not None
        assert r["bos_vol_ratio"] >= 1.5

    def test_peak_gain_computed(self):
        df = _make_watchlist_df(post_high=125.0)
        r = wrs._find_watchlist_candidate(df, "TEST")
        assert r is not None
        assert r["peak_gain_pct"] >= 20.0

    def test_consol_min_pct_computed(self):
        df = _make_watchlist_df(
            consol_closes=[LEVEL * 1.015, LEVEL * 1.012, LEVEL * 1.020, LEVEL * 1.018]
        )
        r = wrs._find_watchlist_candidate(df, "TEST")
        assert r is not None
        # min consol close = 101.2 → consol_min_pct = 1.2%
        assert r["consol_min_pct"] >= 1.0

    def test_broken_structure_returns_none(self):
        """A close below level invalidates the structure."""
        df = _make_watchlist_df(broken_at=20)
        r = wrs._find_watchlist_candidate(df, "TEST")
        assert r is None

    def test_retested_returns_none(self):
        """A low entering the 3% alert zone means it already started retracing."""
        df = _make_watchlist_df(retested_at=20)
        r = wrs._find_watchlist_candidate(df, "TEST")
        assert r is None

    def test_insufficient_confirmation_returns_none(self):
        """Only 2 consecutive closes above level (need 4) → no candidate."""
        df = _make_watchlist_df()
        # Break the 3rd confirmation bar (index 13) to close below level
        df.iloc[BOS_I + 2, df.columns.get_loc("Close")] = LEVEL * 0.99
        r = wrs._find_watchlist_candidate(df, "TEST")
        assert r is None

    def test_too_few_bars_returns_none(self):
        """DataFrame with fewer bars than 2×SWING_N+1 → no swing range to scan."""
        n = 8
        dates = pd.date_range("2020-01-01", periods=n, freq="W")
        arr = np.full(n, LEVEL * 0.90)
        df = pd.DataFrame(
            {"Open": arr, "High": arr * 1.01, "Low": arr * 0.99,
             "Close": arr, "Volume": np.ones(n) * 1e6},
            index=dates,
        )
        r = wrs._find_watchlist_candidate(df, "TINY")
        assert r is None

    def test_entry_and_sl_present(self):
        df = _make_watchlist_df()
        r = wrs._find_watchlist_candidate(df, "TEST")
        assert r is not None
        assert r["entry"] > r["level"]
        assert r["sl"] < r["level"]

    def test_bos_date_is_string(self):
        df = _make_watchlist_df()
        r = wrs._find_watchlist_candidate(df, "TEST")
        assert r is not None
        assert isinstance(r["bos_date"], str)
        assert len(r["bos_date"]) == 10  # YYYY-MM-DD


# ── format_watchlist_update ─────────────────────────────────────────────────────

class TestFormatWatchlistUpdate:
    def test_empty_list_message(self):
        msg = wrs.format_watchlist_update([])
        assert "No score-5" in msg
        assert "📋 BOS Watchlist" in msg

    def test_single_candidate_shows_ticker(self):
        msg = wrs.format_watchlist_update([{
            "ticker": "AAPL", "level": 150.0, "current_pct_above": 5.0,
            "consec_total": 20, "peak_gain_pct": 30.0, "has_panic_sell": False,
        }])
        assert "AAPL" in msg
        assert "150.00" in msg

    def test_panic_sell_flag_shown(self):
        msg = wrs.format_watchlist_update([{
            "ticker": "TSLA", "level": 200.0, "current_pct_above": 7.0,
            "consec_total": 10, "peak_gain_pct": 25.0, "has_panic_sell": True,
        }])
        assert "panic-sell" in msg

    def test_no_panic_sell_flag_hidden(self):
        msg = wrs.format_watchlist_update([{
            "ticker": "MSFT", "level": 300.0, "current_pct_above": 5.0,
            "consec_total": 8, "peak_gain_pct": 18.0, "has_panic_sell": False,
        }])
        assert "panic-sell" not in msg

    def test_alert_zone_shows_red_icon(self):
        """Stock ≤3% from support → red circle icon."""
        msg = wrs.format_watchlist_update([{
            "ticker": "NVDA", "level": 500.0, "current_pct_above": 2.0,
            "consec_total": 12, "peak_gain_pct": 20.0, "has_panic_sell": False,
        }])
        assert "🔴" in msg

    def test_far_stock_shows_green_icon(self):
        """Stock >8% from support → green circle icon."""
        msg = wrs.format_watchlist_update([{
            "ticker": "AMD", "level": 100.0, "current_pct_above": 10.0,
            "consec_total": 12, "peak_gain_pct": 20.0, "has_panic_sell": False,
        }])
        assert "🟢" in msg

    def test_count_line_shown(self):
        candidates = [
            {"ticker": f"T{i}", "level": 100.0, "current_pct_above": 5.0,
             "consec_total": 8, "peak_gain_pct": 18.0, "has_panic_sell": False}
            for i in range(3)
        ]
        msg = wrs.format_watchlist_update(candidates)
        assert "3 stocks on watch" in msg


# ── format_watchlist_alert ──────────────────────────────────────────────────────

class TestFormatWatchlistAlert:
    BASE_ALERT = {
        "ticker": "NVDA", "level": 500.0, "alert_price": 510.0,
        "alert_pct_above": 2.0, "entry": 507.5, "sl": 487.0,
        "consec_total": 25, "bos_date": "2025-01-15",
        "peak_gain_pct": 40.0, "has_panic_sell": False,
    }

    def test_header_present(self):
        msg = wrs.format_watchlist_alert([self.BASE_ALERT])
        assert "⚡" in msg

    def test_ticker_and_prices_shown(self):
        msg = wrs.format_watchlist_alert([self.BASE_ALERT])
        assert "NVDA" in msg
        assert "500.00" in msg
        assert "510.00" in msg

    def test_buy_limit_instruction_present(self):
        msg = wrs.format_watchlist_alert([self.BASE_ALERT])
        assert "BUY LIMIT" in msg
        assert "500.00" in msg  # limit at level

    def test_sl_and_tp_present(self):
        msg = wrs.format_watchlist_alert([self.BASE_ALERT])
        assert "SL:" in msg
        assert "TP1:" in msg
        assert "TP2:" in msg

    def test_friday_check_reminder_present(self):
        msg = wrs.format_watchlist_alert([self.BASE_ALERT])
        assert "Friday" in msg
        assert "cancel" in msg.lower()

    def test_tp_levels_are_2r_and_4r(self):
        level = self.BASE_ALERT["level"]   # 500.0
        sl    = self.BASE_ALERT["sl"]      # 487.0
        risk  = level - sl                 # 13.0
        tp1_expected = round(level + 2 * risk, 2)  # 526.0
        tp2_expected = round(level + 4 * risk, 2)  # 552.0
        msg = wrs.format_watchlist_alert([self.BASE_ALERT])
        assert str(tp1_expected) in msg
        assert str(tp2_expected) in msg

    def test_panic_sell_shown_in_alert(self):
        alert = dict(self.BASE_ALERT, has_panic_sell=True)
        msg = wrs.format_watchlist_alert([alert])
        assert "panic" in msg.lower()

    def test_panic_sell_not_shown_when_false(self):
        msg = wrs.format_watchlist_alert([self.BASE_ALERT])
        assert "panic" not in msg.lower()

    def test_multiple_alerts(self):
        alerts = [
            dict(self.BASE_ALERT, ticker="NVDA"),
            dict(self.BASE_ALERT, ticker="AAPL", level=150.0, alert_price=153.0,
                 sl=144.0, entry=152.25),
        ]
        msg = wrs.format_watchlist_alert(alerts)
        assert "NVDA" in msg
        assert "AAPL" in msg


# ── check_watchlist_alerts ──────────────────────────────────────────────────────

SAMPLE_WATCHLIST = [
    {
        "ticker": "AAPL", "level": 150.0, "entry": 152.25, "sl": 147.0,
        "bos_date": "2024-06-01", "consec_total": 20, "peak_gain_pct": 30.0,
        "consol_min_pct": 1.5, "bos_vol_ratio": 1.5, "has_panic_sell": False,
        "current_pct_above": 5.0, "pre_score": 5,
    },
    {
        "ticker": "MSFT", "level": 300.0, "entry": 304.5, "sl": 294.0,
        "bos_date": "2024-07-01", "consec_total": 18, "peak_gain_pct": 25.0,
        "consol_min_pct": 1.2, "bos_vol_ratio": 1.4, "has_panic_sell": False,
        "current_pct_above": 8.0, "pre_score": 5,
    },
]


def _close_df(prices: dict) -> pd.DataFrame:
    return pd.DataFrame(
        {t: [p] * 5 for t, p in prices.items()},
        index=pd.date_range("2026-01-01", periods=5, freq="D"),
    )


class TestCheckWatchlistAlerts:
    def test_alert_fires_when_in_zone(self, tmp_path):
        """AAPL at 1% above support → alert; MSFT at 6.7% → no alert."""
        wl = tmp_path / "wl.json"
        wl.write_text(json.dumps(SAMPLE_WATCHLIST))
        close_df = _close_df({"AAPL": 151.5, "MSFT": 320.0})

        with (
            patch.object(wrs, "WATCHLIST_PATH", wl),
            patch("analysis.weekly_retest_scanner.yf.download",
                  return_value={"Close": close_df}),
        ):
            alerts = wrs.check_watchlist_alerts()

        assert len(alerts) == 1
        assert alerts[0]["ticker"] == "AAPL"
        assert alerts[0]["alert_pct_above"] < wrs.WATCHLIST_ALERT_PCT

    def test_no_alerts_when_all_far(self, tmp_path):
        wl = tmp_path / "wl.json"
        wl.write_text(json.dumps(SAMPLE_WATCHLIST))
        close_df = _close_df({"AAPL": 170.0, "MSFT": 340.0})

        with (
            patch.object(wrs, "WATCHLIST_PATH", wl),
            patch("analysis.weekly_retest_scanner.yf.download",
                  return_value={"Close": close_df}),
        ):
            alerts = wrs.check_watchlist_alerts()

        assert alerts == []

    def test_both_alert_when_both_in_zone(self, tmp_path):
        wl = tmp_path / "wl.json"
        wl.write_text(json.dumps(SAMPLE_WATCHLIST))
        # AAPL at +1%, MSFT at +2%
        close_df = _close_df({"AAPL": 151.5, "MSFT": 306.0})

        with (
            patch.object(wrs, "WATCHLIST_PATH", wl),
            patch("analysis.weekly_retest_scanner.yf.download",
                  return_value={"Close": close_df}),
        ):
            alerts = wrs.check_watchlist_alerts()

        tickers = [a["ticker"] for a in alerts]
        assert "AAPL" in tickers
        assert "MSFT" in tickers

    def test_price_below_level_not_alerted(self, tmp_path):
        """Price below BOS level (broken structure) should not trigger alert."""
        wl = tmp_path / "wl.json"
        wl.write_text(json.dumps(SAMPLE_WATCHLIST))
        # AAPL at 148 = below level 150 → pct < 0 → excluded
        close_df = _close_df({"AAPL": 148.0, "MSFT": 340.0})

        with (
            patch.object(wrs, "WATCHLIST_PATH", wl),
            patch("analysis.weekly_retest_scanner.yf.download",
                  return_value={"Close": close_df}),
        ):
            alerts = wrs.check_watchlist_alerts()

        assert alerts == []

    def test_empty_watchlist_file(self, tmp_path):
        wl = tmp_path / "wl.json"
        wl.write_text("[]")
        with patch.object(wrs, "WATCHLIST_PATH", wl):
            assert wrs.check_watchlist_alerts() == []

    def test_missing_watchlist_file(self, tmp_path):
        missing = tmp_path / "nonexistent.json"
        with patch.object(wrs, "WATCHLIST_PATH", missing):
            assert wrs.check_watchlist_alerts() == []

    def test_alert_contains_price_fields(self, tmp_path):
        wl = tmp_path / "wl.json"
        wl.write_text(json.dumps(SAMPLE_WATCHLIST))
        close_df = _close_df({"AAPL": 151.5, "MSFT": 320.0})

        with (
            patch.object(wrs, "WATCHLIST_PATH", wl),
            patch("analysis.weekly_retest_scanner.yf.download",
                  return_value={"Close": close_df}),
        ):
            alerts = wrs.check_watchlist_alerts()

        a = alerts[0]
        assert "alert_price" in a
        assert "alert_pct_above" in a
        assert abs(a["alert_price"] - 151.5) < 0.01


# ── _find_setups (existing scan logic) ─────────────────────────────────────────

class TestFindSetups:
    def test_finds_retest_setup(self):
        df = _make_scan_df()
        setups = wrs._find_setups(df, "TEST")
        assert len(setups) >= 1
        s = setups[0]
        assert s["ticker"] == "TEST"
        assert abs(s["level"] - LEVEL) < 0.5
        assert s["outcome"] in ("TP", "TIMEOUT", "SL")

    def test_setup_fields_present(self):
        df = _make_scan_df()
        setups = wrs._find_setups(df, "TEST")
        assert len(setups) >= 1
        s = setups[0]
        for field in ("ticker", "level", "entry", "sl", "risk_pct", "retest_date", "outcome"):
            assert field in s, f"Missing field: {field}"

    def test_entry_above_level(self):
        df = _make_scan_df()
        setups = wrs._find_setups(df, "TEST")
        assert len(setups) >= 1
        s = setups[0]
        assert s["entry"] > s["level"]

    def test_sl_below_level(self):
        df = _make_scan_df()
        setups = wrs._find_setups(df, "TEST")
        assert len(setups) >= 1
        s = setups[0]
        assert s["sl"] < s["level"]

    def test_no_setup_monotone_decline(self):
        """Monotonically declining prices → no swing high → no BOS → no setups."""
        dates = pd.date_range("2018-01-01", periods=50, freq="W")
        arr = np.linspace(100, 70, 50)
        df = pd.DataFrame(
            {"Open": arr, "High": arr * 1.005, "Low": arr * 0.995,
             "Close": arr, "Volume": np.ones(50) * 1e6},
            index=dates,
        )
        assert wrs._find_setups(df, "TEST") == []

    def test_no_setup_no_bos(self):
        """Swing high exists but price never closes above it → no BOS → no setup."""
        dates = pd.date_range("2018-01-01", periods=30, freq="W")
        highs  = np.full(30, 90.0)
        lows   = np.full(30, 85.0)
        closes = np.full(30, 88.0)
        highs[5] = LEVEL  # swing high, but closes never exceed it
        df = pd.DataFrame(
            {"Open": closes, "High": highs, "Low": lows,
             "Close": closes, "Volume": np.ones(30) * 1e6},
            index=dates,
        )
        assert wrs._find_setups(df, "TEST") == []
