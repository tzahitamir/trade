"""Tests for should_fetch_timeframe — schedule correctness."""
from datetime import datetime, timezone
from main import should_fetch_timeframe


def dt(weekday, hour, minute):
    """Create a UTC datetime with the given weekday (0=Mon), hour, minute."""
    # Use a Monday (2026-06-01) as the base and offset by weekday
    base = datetime(2026, 6, 1 + weekday, hour, minute, 0, tzinfo=timezone.utc)
    return base


# --- Weekend gate ---

def test_saturday_always_false():
    for hour in range(24):
        for minute in [0, 4, 15, 30]:
            assert should_fetch_timeframe("15m", dt(5, hour, minute)) is False
            assert should_fetch_timeframe("4h",  dt(5, hour, minute)) is False


def test_sunday_always_false():
    for hour in range(24):
        for minute in [0, 4, 15, 30]:
            assert should_fetch_timeframe("15m", dt(6, hour, minute)) is False
            assert should_fetch_timeframe("4h",  dt(6, hour, minute)) is False


# --- 15m schedule ---

def test_15m_fires_at_correct_minutes():
    for hour in range(24):
        for base_min in [0, 15, 30, 45]:
            assert should_fetch_timeframe("15m", dt(0, hour, base_min + 4)) is True


def test_15m_silent_at_wrong_minutes():
    # Valid fire minutes: 4, 19, 34, 49 (those where minute % 15 == 4)
    wrong = [0, 1, 2, 3, 5, 10, 14, 15, 20, 29]
    for minute in wrong:
        assert should_fetch_timeframe("15m", dt(0, 9, minute)) is False


def test_15m_fires_96_times_per_weekday():
    count = sum(
        1 for h in range(24) for m in range(60)
        if should_fetch_timeframe("15m", dt(0, h, m))
    )
    assert count == 96  # 4/hr × 24h


# --- 4h schedule ---

def test_4h_fires_at_boundary_minutes():
    for hour in [0, 4, 8, 12, 16, 20]:
        assert should_fetch_timeframe("4h", dt(0, hour, 0)) is True


def test_4h_silent_at_non_boundary_hours():
    for hour in [1, 2, 3, 5, 6, 7, 9, 10, 11]:
        assert should_fetch_timeframe("4h", dt(0, hour, 0)) is False


def test_4h_silent_at_non_zero_minutes():
    assert should_fetch_timeframe("4h", dt(0, 0, 1)) is False
    assert should_fetch_timeframe("4h", dt(0, 0, 4)) is False
    assert should_fetch_timeframe("4h", dt(0, 4, 15)) is False


def test_4h_fires_6_times_per_weekday():
    count = sum(
        1 for h in range(24) for m in range(60)
        if should_fetch_timeframe("4h", dt(0, h, m))
    )
    assert count == 6


# --- Removed timeframes ---

def test_5m_never_fires():
    for h in range(24):
        for m in range(60):
            assert should_fetch_timeframe("5m", dt(0, h, m)) is False


def test_30m_fires_at_bar_close_in_ny_window():
    # Fires at minute :04 and :34 during hours 12-15 UTC on weekdays
    for h in range(12, 16):
        assert should_fetch_timeframe("30m", dt(0, h, 4))  is True
        assert should_fetch_timeframe("30m", dt(0, h, 34)) is True

def test_30m_silent_outside_ny_window():
    for h in list(range(0, 12)) + [16, 17, 23]:
        for m in (4, 34):
            assert should_fetch_timeframe("30m", dt(0, h, m)) is False

def test_30m_silent_at_wrong_minutes():
    for m in range(60):
        if m not in (4, 34):
            assert should_fetch_timeframe("30m", dt(0, 13, m)) is False


def test_1h_never_fires():
    for h in range(24):
        for m in range(60):
            assert should_fetch_timeframe("1h", dt(0, h, m)) is False


# --- Full 24h coverage (Asian session not blocked) ---

def test_asian_session_hours_not_blocked():
    """Hours 0-9 UTC (Asian session) should be fetchable on weekdays."""
    for hour in range(0, 10):
        assert should_fetch_timeframe("15m", dt(0, hour, 4)) is True


def test_late_ny_hours_not_blocked():
    """Hours 18-21 UTC (previously dead hours) should now be fetchable."""
    for hour in [18, 19, 20, 21]:
        assert should_fetch_timeframe("15m", dt(0, hour, 4)) is True
