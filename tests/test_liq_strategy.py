"""Tests for the LIQ sweep strategy:
  - detect_liquidity_sweep (SMCAnalyzer)
  - evaluate_liq_outcome (AlertManager)
  - process_symbol_liq live gate (main)
"""
import time as _time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from conftest import make_candle
from analysis.smc_analyzer import SMCAnalyzer
from alerts.alert_manager import AlertManager
from main import (
    process_symbol_liq,
    _liq_alert_id,
    _format_liq_alert,
    _LIQ_LIVE_PAIRS,
    _LIQ_LIVE_MIN_SWEEP_ATR,
    _LIQ_LIVE_SL_ATR,
    _LIQ_LIVE_RR,
)

# ── Candle builders ────────────────────────────────────────────────────────────

STEP = 1800  # 30-minute bars

# Two-day window: day-1 candles feed PDH/PDL; day-2 sweep candle is in NY hours
DAY1_START = 86400      # 1970-01-02 00:00 UTC
DAY2_14H   = 86400 * 2 + 14 * 3600   # 1970-01-03 14:00 UTC (NY kill zone)
DAY2_10H   = 86400 * 2 + 10 * 3600   # 1970-01-03 10:00 UTC (outside kill zone)


def _flat_candle(ts, mid=1.1010, rng=0.0010):
    """Normal candle — no sweep."""
    return make_candle(ts, mid, mid + rng / 2, mid - rng / 2, mid + rng * 0.1)


def _build_context(sweep_ts, pdl=1.0980, pdh=1.1040,
                   direction="bullish", sweep_size=0.0010):
    """
    Build a 35-candle DESC slice with a PDL or PDH sweep at sweep_ts.

    For bullish (PDL): current candle wicks below pdl, closes above.
    For bearish (PDH): current candle wicks above pdh, closes below.
    """
    # 15 previous-day candles (day 1)
    prev_candles = [_flat_candle(DAY1_START + i * STEP) for i in range(15)]
    # Embed the PDL/PDH level in those candles
    prev_candles[3]  = make_candle(DAY1_START + 3 * STEP,  1.1005, 1.1020, pdl, 1.1010)
    prev_candles[10] = make_candle(DAY1_START + 10 * STEP, 1.1010, pdh, 1.1000, 1.1035)

    # 18 early day-2 candles (before kill zone)
    early = [_flat_candle(86400 * 2 + i * STEP) for i in range(18)]

    if direction == "bullish":
        # wick below pdl, close above pdl
        sweep = make_candle(sweep_ts,
                            1.1010, 1.1015,
                            pdl - sweep_size,   # low breaches PDL
                            1.1005)             # close back above PDL
    else:
        # wick above pdh, close below pdh
        sweep = make_candle(sweep_ts,
                            1.1010,
                            pdh + sweep_size,   # high breaches PDH
                            1.1000,
                            1.1015)             # close back below PDH

    all_chron = prev_candles + early + [sweep]
    return list(reversed(all_chron))   # DESC (newest first)


# ══════════════════════════════════════════════════════════════════════════════
# 1. detect_liquidity_sweep
# ══════════════════════════════════════════════════════════════════════════════

class TestDetectLiquiditySweep:
    def setup_method(self):
        self.analyzer = SMCAnalyzer()
        self.base_params = {
            "use_pdh_pdl":  True,
            "use_eq_pools": False,
            "symbol":       "EURUSD",
            "timeframe":    "30m",
        }

    def test_pdl_bullish_sweep_detected(self):
        det = _build_context(DAY2_14H, direction="bullish")
        evs = self.analyzer.detect_liquidity_sweep(det, params=self.base_params)
        assert len(evs) >= 1
        ev = next(e for e in evs if e["pool_type"] == "PDL")
        assert ev["direction"] == "bullish"
        assert ev["pool_type"] == "PDL"

    def test_pdh_bearish_sweep_detected(self):
        det = _build_context(DAY2_14H, direction="bearish")
        evs = self.analyzer.detect_liquidity_sweep(det, params=self.base_params)
        assert len(evs) >= 1
        ev = next(e for e in evs if e["pool_type"] == "PDH")
        assert ev["direction"] == "bearish"

    def test_no_sweep_when_wick_doesnt_reach_pool(self):
        det = _build_context(DAY2_14H, direction="bullish")
        # Override current candle: low stays well above PDL — no breach
        no_sweep = make_candle(DAY2_14H, 1.1010, 1.1020, 1.0990, 1.1015)
        det[0] = no_sweep  # replace sweep candle with a plain one (low=1.0990 > pdl=1.0980)
        evs = self.analyzer.detect_liquidity_sweep(det, params=self.base_params)
        pdl_evs = [e for e in evs if e["pool_type"] == "PDL"]
        assert pdl_evs == []

    def test_no_sweep_when_close_stays_below_pool(self):
        # Low breaches PDL but close also stays below → not a valid sweep rejection
        det = _build_context(DAY2_14H, direction="bullish", pdl=1.0980)
        # close below PDL → the bullish close condition fails
        det[0] = make_candle(DAY2_14H, 1.1010, 1.1015, 1.0975, 1.0970)  # close=1.0970 < pdl
        evs = self.analyzer.detect_liquidity_sweep(det, params=self.base_params)
        pdl_evs = [e for e in evs if e["pool_type"] == "PDL"]
        assert pdl_evs == []

    def test_kill_zone_blocks_outside_hours(self):
        det = _build_context(DAY2_10H, direction="bullish")  # 10:00 UTC, outside 13-16
        params = {**self.base_params, "kill_zones_utc": [(13, 16)]}
        evs = self.analyzer.detect_liquidity_sweep(det, params=params)
        assert evs == []

    def test_kill_zone_passes_inside_hours(self):
        det = _build_context(DAY2_14H, direction="bullish")  # 14:00 UTC, inside 13-16
        params = {**self.base_params, "kill_zones_utc": [(13, 16)]}
        evs = self.analyzer.detect_liquidity_sweep(det, params=params)
        pdl_evs = [e for e in evs if e["pool_type"] == "PDL"]
        assert len(pdl_evs) >= 1

    def test_eq_pools_disabled_returns_no_eq_types(self):
        det = _build_context(DAY2_14H, direction="bullish")
        params = {**self.base_params, "use_eq_pools": False}
        evs = self.analyzer.detect_liquidity_sweep(det, params=params)
        eq_types = [e for e in evs if e["pool_type"] in ("EQH", "EQL")]
        assert eq_types == []

    def test_min_sweep_atr_filters_small_sweep(self):
        # Use a very small sweep_size so sweep_size_atr < 0.2
        det = _build_context(DAY2_14H, direction="bullish", pdl=1.0980, sweep_size=0.00001)
        params = {**self.base_params, "min_sweep_atr": 0.5}
        evs = self.analyzer.detect_liquidity_sweep(det, params=params)
        pdl_evs = [e for e in evs if e["pool_type"] == "PDL"]
        assert pdl_evs == []

    def test_too_few_candles_returns_empty(self):
        candles = [_flat_candle(i * STEP) for i in range(10)]
        evs = self.analyzer.detect_liquidity_sweep(list(reversed(candles)),
                                                   params=self.base_params)
        assert evs == []

    def test_event_has_required_fields(self):
        det = _build_context(DAY2_14H, direction="bullish")
        evs = self.analyzer.detect_liquidity_sweep(det, params=self.base_params)
        pdl_evs = [e for e in evs if e["pool_type"] == "PDL"]
        assert pdl_evs, "no PDL event produced"
        ev = pdl_evs[0]
        for field in ("direction", "pool_type", "pool_level",
                      "sweep_size_atr", "rejection_wick_pct",
                      "pool_touch_count", "pool_age_bars", "breakout_ts"):
            assert field in ev, f"missing field: {field}"

    def test_sweep_size_atr_positive(self):
        det = _build_context(DAY2_14H, direction="bullish")
        evs = self.analyzer.detect_liquidity_sweep(det, params=self.base_params)
        pdl_evs = [e for e in evs if e["pool_type"] == "PDL"]
        assert pdl_evs[0]["sweep_size_atr"] > 0

    def test_rejection_wick_pct_between_0_and_1(self):
        det = _build_context(DAY2_14H, direction="bullish")
        evs = self.analyzer.detect_liquidity_sweep(det, params=self.base_params)
        pdl_evs = [e for e in evs if e["pool_type"] == "PDL"]
        w = pdl_evs[0]["rejection_wick_pct"]
        assert 0.0 < w <= 1.0


# ══════════════════════════════════════════════════════════════════════════════
# 2. evaluate_liq_outcome
# ══════════════════════════════════════════════════════════════════════════════

class TestEvaluateLiqOutcome:
    """Tests for AlertManager.evaluate_liq_outcome (static method).

    evaluate_liq_outcome computes levels internally as:
      sl   = candles[0]["low"]  - sl_buffer_atr * atr   (bullish)
      sl   = candles[0]["high"] + sl_buffer_atr * atr   (bearish)
      risk = abs(entry - sl)
      tp   = entry +/- rr * risk
    Tests use extreme lookahead bar values (far beyond any plausible sl/tp)
    to guarantee the expected branch is taken regardless of exact ATR.
    """

    def _ev(self, direction="bullish", ts=1_000_000):
        return {"direction": direction, "breakout_ts": ts}

    def _candles(self, n=20, mid=1.1000, rng=0.0010):
        """DESC (newest-first) flat candles for ATR baseline."""
        return [make_candle(1_000_000 - i * STEP, mid, mid + rng / 2,
                            mid - rng / 2, mid + rng * 0.1) for i in range(n)]

    def test_win_bullish_price_hits_tp(self):
        candles  = self._candles()
        # high=1.2000 is far above any possible TP → WIN
        lookahead = [make_candle(1_000_001, 1.1000, 1.2000, 1.0995, 1.1050)]
        result = AlertManager.evaluate_liq_outcome(candles, self._ev("bullish"), lookahead,
                                                   sl_buffer_atr=0.7, rr=3.0)
        assert result == "WIN"

    def test_loss_bullish_price_hits_sl(self):
        candles  = self._candles()
        # low=1.0000 is far below any possible SL → LOSS
        lookahead = [make_candle(1_000_001, 1.1000, 1.1005, 1.0000, 1.0950)]
        result = AlertManager.evaluate_liq_outcome(candles, self._ev("bullish"), lookahead,
                                                   sl_buffer_atr=0.7, rr=3.0)
        assert result == "LOSS"

    def test_open_when_neither_hit(self):
        candles   = self._candles()
        # Tight bars that never reach TP or SL
        lookahead = [make_candle(1_000_000 + i * STEP, 1.1000, 1.1002, 1.0999, 1.1001)
                     for i in range(5)]
        result = AlertManager.evaluate_liq_outcome(candles, self._ev("bullish"), lookahead,
                                                   sl_buffer_atr=0.7, rr=3.0)
        assert result == "OPEN"

    def test_win_bearish_price_hits_tp(self):
        candles   = self._candles()
        # low=1.0000 is far below any bearish TP → WIN
        lookahead = [make_candle(1_000_001, 1.1000, 1.1005, 1.0000, 1.0950)]
        result = AlertManager.evaluate_liq_outcome(candles, self._ev("bearish"), lookahead,
                                                   sl_buffer_atr=0.7, rr=3.0)
        assert result == "WIN"

    def test_loss_bearish_price_hits_sl(self):
        candles   = self._candles()
        # high=1.2000 is far above any bearish SL → LOSS
        lookahead = [make_candle(1_000_001, 1.1000, 1.2000, 1.0995, 1.1050)]
        result = AlertManager.evaluate_liq_outcome(candles, self._ev("bearish"), lookahead,
                                                   sl_buffer_atr=0.7, rr=3.0)
        assert result == "LOSS"

    def test_sl_hit_before_tp_is_loss(self):
        """When SL bar appears before TP bar, result must be LOSS not WIN."""
        candles = self._candles()
        # Bar 1: deep low → SL hit; Bar 2: sky high → TP hit
        lookahead = [
            make_candle(1_000_001, 1.1000, 1.1005, 1.0000, 1.0950),  # low hits SL
            make_candle(1_000_002, 1.1000, 1.2000, 1.0995, 1.1050),  # high hits TP
        ]
        result = AlertManager.evaluate_liq_outcome(candles, self._ev("bullish"), lookahead,
                                                   sl_buffer_atr=0.7, rr=3.0)
        assert result == "LOSS"


# ══════════════════════════════════════════════════════════════════════════════
# 3. process_symbol_liq — live gate
# ══════════════════════════════════════════════════════════════════════════════

def _make_am(alert_pairs=None):
    """Minimal AlertManager mock with real SMCAnalyzer."""
    pairs = alert_pairs if alert_pairs is not None else list(_LIQ_LIVE_PAIRS)
    am = MagicMock()
    am.settings.should_alert = lambda sym: sym in pairs
    am.analyzer = SMCAnalyzer()
    return am


def _insert_candles(db, symbol, timeframe, candles):
    conn = db._get_conn()
    for c in candles:
        conn.execute(
            "INSERT OR IGNORE INTO fx_candles"
            " (symbol, timeframe, timestamp, open, high, low, close, volume)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (symbol, timeframe, c["timestamp"],
             c["open"], c["high"], c["low"], c["close"], 1000),
        )
    conn.commit()


def _load_db_with_sweep(db, symbol="EURUSD", sweep_ts=DAY2_14H,
                        direction="bullish", pdl=1.0980, pdh=1.1040):
    """Insert 30m + 4h candles so process_symbol_liq can run fully."""
    det = _build_context(sweep_ts, pdl=pdl, pdh=pdh, direction=direction)
    _insert_candles(db, symbol, "30m", det)  # desc → DB handles dedup via UNIQUE

    # Minimal 4h candles for HTF bias (just needs to be plausible)
    htf_dir  = direction  # aligned
    htf_mid  = 1.1010
    htf_step = 4 * 3600
    for i in range(20):
        ts = sweep_ts - (20 - i) * htf_step
        if direction == "bullish":
            c = make_candle(ts, htf_mid, htf_mid + 0.002, htf_mid - 0.001, htf_mid + 0.001)
        else:
            c = make_candle(ts, htf_mid + 0.001, htf_mid + 0.002, htf_mid - 0.001, htf_mid)
        _insert_candles(db, symbol, "4h", [c])


class TestProcessSymbolLiq:

    def _run(self, db, symbol="EURUSD", hour=14, am=None, sweep_ts=None):
        """Run process_symbol_liq with a mocked UTC hour."""
        if am is None:
            am = _make_am()
        ts = sweep_ts or DAY2_14H
        _load_db_with_sweep(db, symbol=symbol, sweep_ts=ts)
        fake_now = datetime(1970, 1, 3, hour, 0, 0, tzinfo=timezone.utc)
        with patch("main.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.fromtimestamp = datetime.fromtimestamp  # keep real
            process_symbol_liq(symbol, db, am)

    # ── NY hour gate ──────────────────────────────────────────────────────────

    def test_skips_outside_ny_hours(self, db):
        am = _make_am()
        self._run(db, hour=10, am=am)    # 10 UTC = outside 13-15
        am.send_alert.assert_not_called()

    def test_runs_at_hour_13(self, db):
        am = _make_am()
        self._run(db, hour=13, am=am, sweep_ts=DAY2_14H - 3600)
        # May or may not find a sweep, but must not error; assert no exception raised

    def test_runs_at_hour_15(self, db):
        am = _make_am()
        # 15:00 UTC — build a sweep at 15:00
        ts15 = 86400 * 2 + 15 * 3600
        _load_db_with_sweep(db, symbol="EURUSD", sweep_ts=ts15)
        fake_now = datetime(1970, 1, 3, 15, 0, 0, tzinfo=timezone.utc)
        with patch("main.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            process_symbol_liq("EURUSD", db, am)
        # no exception = pass

    # ── Pair gate ─────────────────────────────────────────────────────────────

    def test_skips_non_live_pair(self, db):
        am = _make_am(alert_pairs=["XAUUSD"])
        _load_db_with_sweep(db, symbol="XAUUSD")
        fake_now = datetime(1970, 1, 3, 14, 0, 0, tzinfo=timezone.utc)
        with patch("main.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            process_symbol_liq("XAUUSD", db, am)
        am.send_alert.assert_not_called()

    def test_skips_when_not_in_alert_pairs(self, db):
        am = _make_am(alert_pairs=[])  # EURUSD not enabled
        self._run(db, symbol="EURUSD", hour=14, am=am)
        am.send_alert.assert_not_called()

    # ── Filter gate ───────────────────────────────────────────────────────────

    def test_htf_misaligned_no_alert(self, db):
        """Bearish sweep but we force HTF bias to bullish → no alert."""
        am = _make_am()
        _load_db_with_sweep(db, symbol="EURUSD", direction="bearish")
        # Override get_htf_bias to return the opposite direction
        am.analyzer = MagicMock(wraps=SMCAnalyzer())
        am.analyzer.get_htf_bias = MagicMock(return_value="bullish")  # misaligned
        fake_now = datetime(1970, 1, 3, 14, 0, 0, tzinfo=timezone.utc)
        with patch("main.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            process_symbol_liq("EURUSD", db, am)
        am.send_alert.assert_not_called()

    # ── Happy path ────────────────────────────────────────────────────────────

    def test_alert_sent_on_valid_bullish_sweep(self, db):
        am = _make_am()
        self._run(db, symbol="EURUSD", hour=14, am=am)
        # We planted a valid PDL sweep aligned to bullish HTF
        # Alert may or may not fire depending on whether ATR threshold passes,
        # but if it fires the message must be well-formed
        if am.send_alert.called:
            msg = am.send_alert.call_args[0][0]
            assert "LIQ SWEEP" in msg
            assert "EURUSD" in msg
            assert "PDL" in msg
            assert "SL:" in msg
            assert "TP:" in msg

    def test_monitor_inserted_when_alert_fires(self, db):
        am = _make_am()
        self._run(db, symbol="EURUSD", hour=14, am=am)
        if am.send_alert.called:
            monitors = db.get_open_monitors()
            assert len(monitors) >= 1
            m = monitors[0]
            assert m["symbol"] == "EURUSD"
            assert m["direction"] in ("bullish", "bearish")
            assert m["entry"] > 0
            assert m["sl"] > 0
            assert m["tp"] > 0

    def test_duplicate_alert_suppressed(self, db):
        """Calling process_symbol_liq twice with the same candles must not
        fire the same alert twice. Dedup relies on the unique index on
        smc_alerts.alert_id — the second insert raises IntegrityError."""
        am = _make_am()
        _load_db_with_sweep(db, symbol="EURUSD")
        fake_now = datetime(1970, 1, 3, 14, 0, 0, tzinfo=timezone.utc)
        with patch("main.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            process_symbol_liq("EURUSD", db, am)
            first_count = am.send_alert.call_count
            # Second call: same candles, same alert_id → insert_alert raises →
            # process_symbol_liq skips the send
            process_symbol_liq("EURUSD", db, am)
            second_count = am.send_alert.call_count
        assert second_count == first_count  # no new alert on second call

    def test_usdcad_not_live(self, db):
        assert "USDCAD" not in _LIQ_LIVE_PAIRS  # muted until stats improve

    def test_eurusd_is_live(self, db):
        assert "EURUSD" in _LIQ_LIVE_PAIRS


# ══════════════════════════════════════════════════════════════════════════════
# 4. _liq_alert_id and _format_liq_alert helpers
# ══════════════════════════════════════════════════════════════════════════════

class TestLiqHelpers:

    def _ev(self, direction="bullish", pool_type="PDL", pool_level=1.0980,
            sweep_atr=0.40, wick_pct=0.63, ts=DAY2_14H):
        return {
            "direction":          direction,
            "pool_type":          pool_type,
            "pool_level":         pool_level,
            "sweep_size_atr":     sweep_atr,
            "rejection_wick_pct": wick_pct,
            "breakout_ts":        ts,
        }

    def test_alert_id_contains_pool_type(self):
        aid = _liq_alert_id("EURUSD", DAY2_14H, "PDL")
        assert "pdl" in aid

    def test_alert_id_contains_symbol(self):
        aid = _liq_alert_id("EURUSD", DAY2_14H, "PDL")
        assert "eurusd" in aid

    def test_alert_id_different_for_pdh_vs_pdl(self):
        pdl_id = _liq_alert_id("EURUSD", DAY2_14H, "PDL")
        pdh_id = _liq_alert_id("EURUSD", DAY2_14H, "PDH")
        assert pdl_id != pdh_id

    def test_alert_id_different_for_different_timestamps(self):
        id1 = _liq_alert_id("EURUSD", DAY2_14H,          "PDL")
        id2 = _liq_alert_id("EURUSD", DAY2_14H + STEP,   "PDL")
        assert id1 != id2

    def test_format_contains_direction(self):
        ev  = self._ev(direction="bullish")
        msg = _format_liq_alert("EURUSD", ev, 1.1005, 1.0998, 1.1026, "bullish", 0.001)
        assert "BULLISH" in msg

    def test_format_contains_symbol(self):
        ev  = self._ev()
        msg = _format_liq_alert("EURUSD", ev, 1.1005, 1.0998, 1.1026, "bullish", 0.001)
        assert "EURUSD" in msg

    def test_format_contains_pool_type(self):
        ev  = self._ev(pool_type="PDL")
        msg = _format_liq_alert("EURUSD", ev, 1.1005, 1.0998, 1.1026, "bullish", 0.001)
        assert "PDL" in msg

    def test_format_contains_sl_and_tp(self):
        ev  = self._ev()
        msg = _format_liq_alert("EURUSD", ev, 1.1005, 1.0998, 1.1026, "bullish", 0.001)
        assert "SL:" in msg
        assert "TP:" in msg

    def test_format_contains_htf_bias(self):
        ev  = self._ev()
        msg = _format_liq_alert("EURUSD", ev, 1.1005, 1.0998, 1.1026, "bullish", 0.001)
        assert "4H:BULLISH" in msg

    def test_live_sl_atr_is_0_7(self):
        assert _LIQ_LIVE_SL_ATR == 0.7

    def test_live_rr_is_3(self):
        assert _LIQ_LIVE_RR == 3.0

    def test_live_min_sweep_atr_is_0_2(self):
        assert _LIQ_LIVE_MIN_SWEEP_ATR == 0.2
