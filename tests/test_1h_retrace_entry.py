"""Unit tests for 1h BOS retrace-entry helpers."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from main import _h1re_alert_id, _h1re_4h_pass


# ─── _h1re_alert_id ────────────────────────────────────────────────────────


class TestH1reAlertId:
    def test_bullish_format(self):
        # 2026-06-13 09:00 UTC → ts = 1749805200
        ts = 1749805200
        result = _h1re_alert_id("EURUSD", "bullish", ts)
        assert result.startswith("h1re_u_")
        assert "eurusd" in result

    def test_bearish_format(self):
        ts = 1749805200
        result = _h1re_alert_id("XAUUSD", "bearish", ts)
        assert result.startswith("h1re_d_")
        assert "xauusd" in result

    def test_unique_per_direction(self):
        ts = 1749805200
        bull = _h1re_alert_id("NZDUSD", "bullish", ts)
        bear = _h1re_alert_id("NZDUSD", "bearish", ts)
        assert bull != bear

    def test_unique_per_timestamp(self):
        ts1 = 1749805200
        ts2 = 1749808800   # +1h
        id1 = _h1re_alert_id("USDCHF", "bullish", ts1)
        id2 = _h1re_alert_id("USDCHF", "bullish", ts2)
        assert id1 != id2

    def test_unique_per_symbol(self):
        ts = 1749805200
        id1 = _h1re_alert_id("EURUSD", "bullish", ts)
        id2 = _h1re_alert_id("XAUUSD", "bullish", ts)
        assert id1 != id2

    def test_symbol_lowercase(self):
        ts = 1749805200
        result = _h1re_alert_id("EURUSD", "bullish", ts)
        assert "EURUSD" not in result
        assert "eurusd" in result


# ─── _h1re_4h_pass ─────────────────────────────────────────────────────────


class TestH1re4hPass:

    # EURUSD: requires BOTH prev and curr aligned
    class TestEURUSD:
        sym = "EURUSD"

        def test_both_aligned_passes(self):
            assert _h1re_4h_pass("EURUSD", prev_h4_aligned=True, curr_h4_aligned=True)

        def test_only_prev_aligned_fails(self):
            assert not _h1re_4h_pass("EURUSD", prev_h4_aligned=True, curr_h4_aligned=False)

        def test_only_curr_aligned_fails(self):
            assert not _h1re_4h_pass("EURUSD", prev_h4_aligned=False, curr_h4_aligned=True)

        def test_neither_aligned_fails(self):
            assert not _h1re_4h_pass("EURUSD", prev_h4_aligned=False, curr_h4_aligned=False)

    # XAUUSD: no 4h gate — always passes (4h counter still EV +2.33R for XAU)
    class TestXAUUSD:
        def test_both_aligned_passes(self):
            assert _h1re_4h_pass("XAUUSD", prev_h4_aligned=True, curr_h4_aligned=True)

        def test_only_prev_passes(self):
            assert _h1re_4h_pass("XAUUSD", prev_h4_aligned=True, curr_h4_aligned=False)

        def test_only_curr_passes(self):
            assert _h1re_4h_pass("XAUUSD", prev_h4_aligned=False, curr_h4_aligned=True)

        def test_neither_passes(self):
            assert _h1re_4h_pass("XAUUSD", prev_h4_aligned=False, curr_h4_aligned=False)

    # EURJPY: excluded — must never pass (returns False regardless)
    class TestEURJPY:
        def test_always_fails_even_both_aligned(self):
            assert not _h1re_4h_pass("EURJPY", prev_h4_aligned=True, curr_h4_aligned=True)

        def test_always_fails_curr_only(self):
            assert not _h1re_4h_pass("EURJPY", prev_h4_aligned=False, curr_h4_aligned=True)

    # Unknown symbol: should not pass
    class TestUnknown:
        def test_unknown_always_fails(self):
            assert not _h1re_4h_pass("GBPUSD", prev_h4_aligned=True, curr_h4_aligned=True)

        def test_usdjpy_always_fails(self):
            assert not _h1re_4h_pass("USDJPY", prev_h4_aligned=True, curr_h4_aligned=True)
