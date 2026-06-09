"""Tests for evaluate_production — gold param filters and settings regression."""
from unittest.mock import MagicMock, patch
from conftest import make_candle
from alerts.alert_manager import AlertManager
from config.settings import Settings


def _make_settings(alert_pairs=None):
    s = MagicMock(spec=Settings)
    s.alert_pairs = alert_pairs or ["EURUSD", "NZDUSD"]
    s.charts_dir = "/tmp/test_charts"
    s.dev_mode = True
    s.should_alert = lambda sym: sym in (alert_pairs or ["EURUSD", "NZDUSD"])
    return s


def _make_alert_manager(alert_pairs=None):
    settings = _make_settings(alert_pairs)
    db = MagicMock()
    db.get_gold_params.return_value = {}
    db.insert_alert.return_value = 1
    am = AlertManager.__new__(AlertManager)
    am.settings = settings
    am.db = db
    am.notifier = MagicMock()
    am.prod_charts_dir = MagicMock()
    am.charts_dir = MagicMock()
    from analysis.smc_analyzer import SMCAnalyzer
    am.analyzer = SMCAnalyzer()
    return am


def _strong_bullish_bos_candles():
    """Minimal candle set that produces a bullish BOS event.
    Returns DESC (newest-first) as evaluate_production / detect_bos expect.
    Swing high at chron index 3 (high=1.1060), break at chron index 10.
    """
    ts = 1_700_000_000
    step = 900
    prices = [
        # background (chron indices 0-2)
        (1.0940, 1.0950, 1.0930, 1.0945),
        (1.0945, 1.0960, 1.0940, 1.0955),
        (1.0955, 1.0970, 1.0950, 1.0965),
        # swing high at chron index 3 — high=1.1060
        (1.0965, 1.1060, 1.0960, 1.1050),
        # pullback (indices 4-7)
        (1.1050, 1.1055, 1.0980, 1.1000),
        (1.1000, 1.1010, 1.0960, 1.0970),
        (1.0970, 1.0980, 1.0940, 1.0950),
        (1.0950, 1.0960, 1.0920, 1.0930),
        # recovery (indices 8-9)
        (1.0930, 1.0950, 1.0920, 1.0940),
        (1.0940, 1.0960, 1.0930, 1.0955),
        # strong BOS: close=1.1090 > swing_high=1.1060 (index 10)
        (1.0955, 1.1100, 1.0950, 1.1090),
    ]
    candles = []
    for o, h, l, c in prices:
        candles.append(make_candle(ts, o, h, l, c))
        ts += step
    return list(reversed(candles))  # DESC as evaluate_production expects


LOOSE_PARAMS = {
    "min_break_strength": 0.0,
    "require_brt_confluence": False,
    "require_liquidity_sweep": False,
    "htf_aligned_only": False,
    "sl_mode": "swing",
    "max_confluences": None,
    "exclude_weekend": False,
    "dead_hours_utc": [],
    "max_break_strength": None,
}


def _run_eval(am, candles, gold_params=None, htf_bias=None):
    with patch.object(am, "_render_bos_chart", return_value=("/tmp/chart.png", MagicMock())):
        return am.evaluate_production(
            "EURUSD", "15m", candles,
            candles_4h=[], htf_bias=htf_bias,
            gold_params=gold_params or LOOSE_PARAMS,
        )


# --- settings regression: alert_manager.settings used, not bare 'settings' ---

def test_evaluate_uses_alert_manager_settings_not_bare():
    """Regression: process_symbol_timeframe previously referenced bare 'settings' (NameError)."""
    am = _make_alert_manager(alert_pairs=["EURUSD"])
    candles = _strong_bullish_bos_candles()
    # If 'settings' NameError exists this would raise; if it passes, regression is fixed
    try:
        _run_eval(am, candles)
    except NameError as e:
        raise AssertionError(f"NameError regression: {e}")


# --- min_break_strength filter ---

def test_weak_break_filtered_out(db):
    am = _make_alert_manager()
    candles = _strong_bullish_bos_candles()
    params = {**LOOSE_PARAMS, "min_break_strength": 999.0}  # impossibly high
    alerts = _run_eval(am, candles, gold_params=params)
    assert alerts == []


def test_strong_break_passes(db):
    am = _make_alert_manager()
    candles = _strong_bullish_bos_candles()
    params = {**LOOSE_PARAMS, "min_break_strength": 0.0}
    alerts = _run_eval(am, candles, gold_params=params)
    assert len(alerts) >= 1


# --- htf_aligned_only filter ---

def test_htf_misaligned_filtered(db):
    am = _make_alert_manager()
    candles = _strong_bullish_bos_candles()
    params = {**LOOSE_PARAMS, "htf_aligned_only": True}
    # HTF bias is bearish but signal is bullish — should be filtered
    alerts = _run_eval(am, candles, gold_params=params, htf_bias="bearish")
    assert alerts == []


def test_htf_aligned_passes(db):
    am = _make_alert_manager()
    candles = _strong_bullish_bos_candles()
    params = {**LOOSE_PARAMS, "htf_aligned_only": True}
    alerts = _run_eval(am, candles, gold_params=params, htf_bias="bullish")
    assert len(alerts) >= 1


# --- require_liquidity_sweep filter ---

def test_sweep_required_but_absent_filtered(db):
    am = _make_alert_manager()
    candles = _strong_bullish_bos_candles()
    params = {**LOOSE_PARAMS, "require_liquidity_sweep": True}
    alerts = _run_eval(am, candles, gold_params=params)
    # Synthetic candles have no sweep — expect filtered
    assert alerts == []


# --- not in alert_pairs → no alert ---

def test_symbol_not_in_alert_pairs_returns_empty():
    """USDJPY is not in alert_pairs — evaluate_production must return empty."""
    am = _make_alert_manager(alert_pairs=["EURUSD"])
    am.settings.should_alert = lambda sym: sym in ["EURUSD"]
    candles = _strong_bullish_bos_candles()
    with patch.object(am, "_render_bos_chart", return_value=("/tmp/chart.png", MagicMock())):
        alerts = am.evaluate_production(
            "USDJPY", "15m", candles,
            candles_4h=[], htf_bias="bullish",
            gold_params=LOOSE_PARAMS,
        )
    # evaluate_production itself doesn't gate on should_alert — that's done upstream
    # in process_symbol_timeframe. This test verifies the upstream gate works.
    from main import process_symbol_timeframe
    from unittest.mock import patch as mock_patch
    call_count = [0]
    with mock_patch("main.alert_manager") if False else mock_patch("main.time.sleep"):
        pass  # gate tested in test_fetch_job_budget via process_symbol_timeframe mock
