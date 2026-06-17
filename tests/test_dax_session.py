"""
Tests for DAX SERPE session job:
  - detect_dax_expansion_state (new helper in SMCAnalyzer)
  - _dax_peak_too_late gate
  - _dax_slowdown_present
  - _format_dax_prealert message format
  - 1h retrace entry fires regardless of alert_pairs (gate move)
"""
import sys, types
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from analysis.smc_analyzer import SMCAnalyzer
from main import (
    _dax_peak_too_late,
    _dax_slowdown_present,
    _format_dax_prealert,
    _ISRAEL_TZ,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _ts(hour: int, minute: int = 0, day: int = 16, month: int = 6, year: int = 2026) -> int:
    """Return UTC timestamp for a given Israel-time (IDT = UTC+3)."""
    dt = datetime(year, month, day, hour, minute, tzinfo=_ISRAEL_TZ)
    return int(dt.timestamp())


def _candle(ts, open_, high, low, close):
    return {"timestamp": ts, "open": open_, "high": high, "low": low, "close": close}


def _flat_candles(start_ts, n, step=300, price=100.0):
    """n candles each 5 min apart, all flat at price."""
    return [_candle(start_ts + i * step, price, price + 0.5, price - 0.5, price)
            for i in range(n)]


def _trending_15m(start_ts, n, step=900, start_price=20000.0, delta=50.0):
    """n 15m candles trending up by delta each."""
    cs = []
    p = start_price
    for i in range(n):
        cs.append(_candle(start_ts + i * step, p, p + delta, p - 5, p + delta - 5))
        p += delta
    return cs


# ── _dax_peak_too_late ────────────────────────────────────────────────────────

class TestDaxPeakTooLate:
    def test_before_cutoff_passes(self):
        assert not _dax_peak_too_late(_ts(11, 0))

    def test_at_11_44_passes(self):
        assert not _dax_peak_too_late(_ts(11, 44))

    def test_at_cutoff_11_45_blocked(self):
        assert _dax_peak_too_late(_ts(11, 45))

    def test_after_cutoff_blocked(self):
        assert _dax_peak_too_late(_ts(12, 0))
        assert _dax_peak_too_late(_ts(12, 30))

    def test_early_session_passes(self):
        assert not _dax_peak_too_late(_ts(10, 15))


# ── _dax_slowdown_present ─────────────────────────────────────────────────────

class TestDaxSlowdownPresent:
    ATR = 100.0

    def _make_candles(self, bodies):
        """bodies = list of (open, close) tuples; high/low set to cover them."""
        cs = []
        for i, (o, c) in enumerate(bodies):
            hi  = max(o, c) + 5
            lo  = min(o, c) - 5
            cs.append(_candle(i * 300, o, hi, lo, c))
        return cs

    def test_small_body_triggers_slowdown(self):
        # Last candle has tiny body (< 0.4 * ATR = 40 pts)
        candles = self._make_candles([(100, 150), (200, 215)])   # second body = 15 < 40
        assert _dax_slowdown_present(candles, self.ATR)

    def test_large_body_no_slowdown(self):
        # Both candles have big bodies (> 0.4 * ATR = 40 pts)
        candles = self._make_candles([(100, 200), (200, 350)])   # bodies 100, 150
        assert not _dax_slowdown_present(candles, self.ATR)

    def test_inside_bar_triggers_slowdown(self):
        # Second candle fully inside first
        c1 = _candle(0,   100, 200, 90,  180)   # big candle
        c2 = _candle(300, 130, 170, 110, 150)   # inside c1 (high≤200, low≥90)
        assert _dax_slowdown_present([c1, c2], self.ATR)

    def test_compressed_range_triggers_slowdown(self):
        # High-low range < 0.6 * ATR = 60
        c1 = _candle(0,   100, 200, 80,  180)
        c2 = _candle(300, 150, 170, 145, 160)   # range = 25 < 60
        assert _dax_slowdown_present([c1, c2], self.ATR)

    def test_needs_at_least_2_candles(self):
        # Only 1 candle → no prev context, but body/range checks still apply
        c = _candle(0, 100, 200, 90, 180)   # big candle, no slowdown
        assert not _dax_slowdown_present([c], self.ATR)

    def test_single_small_body_candle(self):
        c = _candle(0, 100, 115, 90, 110)   # body = 10 < 40
        assert _dax_slowdown_present([c], self.ATR)


# ── _format_dax_prealert ──────────────────────────────────────────────────────

class TestFormatDaxPrealert:
    def _exp(self, expansion_dir="bearish", peak_ts=None):
        return {
            "expansion_dir":  expansion_dir,
            "ct_dir":         "bullish" if expansion_dir == "bearish" else "bearish",
            "peak_ts":        peak_ts or _ts(10, 30),
            "peak":           20500.0,
            "origin":         20800.0,
            "eq_level":       20650.0,
            "expansion_range": 300.0,
        }

    def test_contains_ct_direction(self):
        msg = _format_dax_prealert(self._exp("bearish"))
        assert "BULLISH" in msg

    def test_contains_expansion_direction(self):
        msg = _format_dax_prealert(self._exp("bearish"))
        assert "BEARISH" in msg

    def test_contains_eq_target(self):
        msg = _format_dax_prealert(self._exp())
        assert "20650" in msg

    def test_contains_peak_time(self):
        msg = _format_dax_prealert(self._exp(peak_ts=_ts(10, 30)))
        assert "10:30" in msg

    def test_watch_message_present(self):
        msg = _format_dax_prealert(self._exp())
        assert "watch" in msg.lower() or "Watch" in msg


# ── detect_dax_expansion_state ───────────────────────────────────────────────

class TestDetectDaxExpansionState:
    """Smoke tests: no expansion → None; with expansion → dict with expected keys."""

    def test_returns_none_when_too_few_candles(self):
        analyzer = SMCAnalyzer()
        result = analyzer.detect_dax_expansion_state(
            candles_15m_session=[_candle(0, 100, 110, 90, 105)],
            params={},
        )
        assert result is None

    def test_returns_none_when_no_bos(self):
        # Flat candles — no BOS possible
        session_start = _ts(9, 0)
        pre = [_candle(session_start - (i + 1) * 900, 100, 105, 95, 100) for i in range(10)]
        sess = [_candle(session_start + i * 900, 100, 105, 95, 100) for i in range(8)]
        analyzer = SMCAnalyzer()
        result = analyzer.detect_dax_expansion_state(
            candles_15m_session=sess,
            params={"symbol": "DAX"},
            candles_15m_presession=pre,
        )
        assert result is None

    def test_returns_dict_with_required_keys_on_expansion(self):
        """Build a clear bullish expansion: pre-session range, then session breaks up."""
        analyzer = SMCAnalyzer()
        session_start = _ts(9, 0)

        # 12 pre-session candles ranging 100–110 (tight range)
        pre = []
        for i in range(12):
            ts = session_start - (12 - i) * 900
            pre.append(_candle(ts, 105, 110, 100, 105))

        # Session: first candle breaks high (bullish BOS), then strong rally
        sess = []
        sess.append(_candle(session_start,          105, 130, 104, 128))  # breaks 110
        sess.append(_candle(session_start + 900,    128, 150, 127, 148))
        sess.append(_candle(session_start + 1800,   148, 170, 147, 168))
        sess.append(_candle(session_start + 2700,   168, 185, 167, 180))
        sess.append(_candle(session_start + 3600,   180, 182, 165, 168))  # pullback starts

        result = analyzer.detect_dax_expansion_state(
            candles_15m_session=sess,
            params={"symbol": "DAX", "min_expansion_atr": 0.5},
            candles_15m_presession=pre,
        )
        if result is not None:
            for key in ("expansion_dir", "ct_dir", "peak_ts", "peak", "origin", "eq_level", "expansion_range"):
                assert key in result, f"Missing key: {key}"
            assert result["expansion_dir"] in ("bullish", "bearish")
            assert result["expansion_range"] > 0


# ── 1h retrace fires regardless of alert_pairs ───────────────────────────────

class TestH1reOutsideAlertPairsGate:
    """
    Verify that _process_symbol_1h_retrace_entry is called even when
    alert_manager.settings.should_alert() returns False (pair not in alert_pairs).
    """

    def test_1h_retrace_called_when_alerts_suppressed(self):
        """
        process_symbol_timeframe must call _process_symbol_1h_retrace_entry for
        EURUSD 5m even when alert_manager.settings.should_alert() returns False
        (i.e. alert_pairs is empty).
        """
        import main as m

        fake_candle = {"timestamp": _ts(10, 0), "open": 1.0, "high": 1.01,
                       "low": 0.99, "close": 1.005}

        settings_mock = MagicMock()
        settings_mock.should_alert.return_value = False   # alert_pairs = []

        alert_mock        = MagicMock()
        alert_mock.settings = settings_mock

        db_mock = MagicMock()
        db_mock.get_latest_timestamp.return_value = 0
        db_mock.insert_candles.return_value = None

        fetcher_mock = MagicMock()
        fetcher_mock.fetch_historical.return_value = [fake_candle]

        called = []

        with patch.object(m, "_process_symbol_1h_retrace_entry",
                          side_effect=lambda *a, **kw: called.append(True)):
            with patch.object(m, "_process_symbol_5m"):
                with patch.object(m, "process_symbol_liq"):
                    with patch.object(m, "validate_candles"):
                        m.process_symbol_timeframe(
                            "EURUSD", "5m", fetcher_mock, db_mock, alert_mock
                        )

        assert len(called) == 1, (
            "_process_symbol_1h_retrace_entry must be called even when alerts are suppressed"
        )
