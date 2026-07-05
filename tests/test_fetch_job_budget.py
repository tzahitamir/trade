"""Tests for fetch_job budget gating, priority ordering, and alert deduplication."""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call
from main import fetch_job, FETCH_PRIORITY, API_DAILY_LIMIT


def _make_settings(pairs=None):
    s = MagicMock()
    s.fx_pairs = pairs or FETCH_PRIORITY
    s.timeframes = ["15m"]
    return s


def _make_alert_manager():
    am = MagicMock()
    am.settings = MagicMock()
    am.settings.should_alert.return_value = True
    return am


# Patch time.sleep and process_symbol_timeframe for all tests
PATCHES = [
    patch("main.time.sleep"),
    patch("main.process_symbol_timeframe"),
    # Fire at a minute that triggers 15m fetch
    patch("main.datetime", wraps=datetime),
]


def _fetch_at_minute(minute):
    """Return a datetime that triggers 15m fetch (minute % 15 == 1)."""
    return datetime(2026, 6, 2, 9, minute, 0, tzinfo=timezone.utc)  # Monday


def test_no_fetch_when_limit_reached(db):
    db.increment_api_calls(API_DAILY_LIMIT)
    settings = _make_settings()
    am = _make_alert_manager()

    with patch("main.time.sleep"), \
         patch("main.process_symbol_timeframe") as mock_fetch, \
         patch("main.datetime") as mock_dt:
        mock_dt.now.return_value = _fetch_at_minute(1)
        fetch_job(settings, MagicMock(), db, am)

    mock_fetch.assert_not_called()


def test_alert_sent_exactly_once_when_limit_crossed(db):
    """First time budget is exhausted mid-loop: one Telegram alert. Next call: silent."""
    # Set budget so it runs out after first pair
    db.increment_api_calls(API_DAILY_LIMIT - 1)
    settings = _make_settings(["NZDUSD", "EURJPY"])
    am = _make_alert_manager()
    fetcher = MagicMock()

    def fake_process(symbol, tf, f, d, a):
        d.increment_api_calls(1)  # simulate the real increment

    with patch("main.time.sleep"), \
         patch("main.process_symbol_timeframe", side_effect=fake_process), \
         patch("main.datetime") as mock_dt:
        mock_dt.now.return_value = _fetch_at_minute(1)
        fetch_job(settings, fetcher, db, am)  # hits limit after NZDUSD
        fetch_job(settings, fetcher, db, am)  # already exhausted — silent

    # Alert fired exactly once
    alert_calls = [c for c in am.send_alert.call_args_list
                   if "Daily API limit" in str(c) or "daily limit" in str(c).lower()]
    assert am.send_alert.call_count == 1


def test_priority_order_respected(db):
    """Pairs fetched in FETCH_PRIORITY order regardless of settings.fx_pairs order."""
    shuffled = list(reversed(FETCH_PRIORITY))
    settings = _make_settings(shuffled)
    fetched_order = []

    def fake_process(symbol, tf, f, d, a):
        fetched_order.append(symbol)
        d.increment_api_calls(1)

    with patch("main.time.sleep"), \
         patch("main.process_symbol_timeframe", side_effect=fake_process), \
         patch("main.datetime") as mock_dt:
        mock_dt.now.return_value = _fetch_at_minute(1)
        fetch_job(settings, MagicMock(), db, MagicMock())

    assert fetched_order == FETCH_PRIORITY


def test_usdjpy_fetched_last(db):
    """USDJPY is always last in priority (no live alerts)."""
    settings = _make_settings()
    fetched_order = []

    def fake_process(symbol, tf, f, d, a):
        fetched_order.append(symbol)
        d.increment_api_calls(1)

    with patch("main.time.sleep"), \
         patch("main.process_symbol_timeframe", side_effect=fake_process), \
         patch("main.datetime") as mock_dt:
        mock_dt.now.return_value = _fetch_at_minute(1)
        fetch_job(settings, MagicMock(), db, MagicMock())

    assert fetched_order[-1] == "USDJPY"


def test_best_pairs_survive_budget_pressure(db):
    """When budget runs short, NZDUSD/EURJPY are fetched; USDJPY is dropped."""
    # Allow only 3 calls
    db.increment_api_calls(API_DAILY_LIMIT - 3)
    settings = _make_settings()
    fetched = []

    def fake_process(symbol, tf, f, d, a):
        fetched.append(symbol)
        d.increment_api_calls(1)

    with patch("main.time.sleep"), \
         patch("main.process_symbol_timeframe", side_effect=fake_process), \
         patch("main.datetime") as mock_dt:
        mock_dt.now.return_value = _fetch_at_minute(1)
        fetch_job(settings, MagicMock(), db, MagicMock())

    assert "NZDUSD" in fetched
    assert "EURJPY" in fetched
    assert "USDJPY" not in fetched


def test_no_fetch_on_weekend(db):
    """No fetches happen on Sunday regardless of budget."""
    settings = _make_settings()

    with patch("main.time.sleep"), \
         patch("main.process_symbol_timeframe") as mock_fetch, \
         patch("main.datetime") as mock_dt:
        # Sunday 2026-06-07 09:04 UTC
        mock_dt.now.return_value = datetime(2026, 6, 7, 9, 4, 0, tzinfo=timezone.utc)
        fetch_job(settings, MagicMock(), db, MagicMock())

    mock_fetch.assert_not_called()
