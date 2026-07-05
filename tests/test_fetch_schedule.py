"""Tests for should_fetch_timeframe — schedule correctness."""
from datetime import datetime, timezone
from main import should_fetch_timeframe


def dt(weekday, hour, minute):
    """Create a UTC datetime with the given weekday (0=Mon), hour, minute."""
    base = datetime(2026, 6, 1 + weekday, hour, minute, 0, tzinfo=timezone.utc)
    return base


# --- Weekend gate ---

def test_saturday_always_false():
    for hour in range(24):
        for minute in [0, 1, 15, 30]:
            assert should_fetch_timeframe("15m", dt(5, hour, minute)) is False
            assert should_fetch_timeframe("4h",  dt(5, hour, minute)) is False


def test_sunday_always_false():
    for hour in range(24):
        for minute in [0, 1, 15, 30]:
            assert should_fetch_timeframe("15m", dt(6, hour, minute)) is False
            assert should_fetch_timeframe("4h",  dt(6, hour, minute)) is False


# --- 15m schedule (fires at minute % 15 == 1, session 07:00–21:00 UTC) ---

def test_15m_fires_at_correct_minutes():
    # Fire minutes within session: 1, 16, 31, 46
    for base_min in [0, 15, 30, 45]:
        assert should_fetch_timeframe("15m", dt(0, 9, base_min + 1)) is True


def test_15m_silent_at_wrong_minutes():
    # Minutes that are NOT 1, 16, 31, 46
    wrong = [0, 2, 3, 4, 5, 10, 14, 15, 17, 20, 29, 30]
    for minute in wrong:
        assert should_fetch_timeframe("15m", dt(0, 9, minute)) is False


def test_15m_session_gated():
    # Only fires within 07:00–21:00 UTC
    assert should_fetch_timeframe("15m", dt(0, 7,  1)) is True
    assert should_fetch_timeframe("15m", dt(0, 20, 1)) is True
    assert should_fetch_timeframe("15m", dt(0, 6,  1)) is False   # before session
    assert should_fetch_timeframe("15m", dt(0, 21, 1)) is False   # after session


def test_15m_fires_56_times_per_weekday():
    # 4 fires/hr × 14 hours (07:00–21:00) = 56
    count = sum(
        1 for h in range(24) for m in range(60)
        if should_fetch_timeframe("15m", dt(0, h, m))
    )
    assert count == 56


# --- 4h schedule ---

def test_4h_fires_at_boundary_minutes():
    for hour in [0, 4, 8, 12, 16, 20]:
        assert should_fetch_timeframe("4h", dt(0, hour, 0)) is True


def test_4h_silent_at_non_boundary_hours():
    for hour in [1, 2, 3, 5, 6, 7, 9, 10, 11]:
        assert should_fetch_timeframe("4h", dt(0, hour, 0)) is False


def test_4h_silent_at_non_zero_minutes():
    assert should_fetch_timeframe("4h", dt(0, 0, 1))  is False
    assert should_fetch_timeframe("4h", dt(0, 0, 4))  is False
    assert should_fetch_timeframe("4h", dt(0, 4, 15)) is False


def test_4h_fires_6_times_per_weekday():
    count = sum(
        1 for h in range(24) for m in range(60)
        if should_fetch_timeframe("4h", dt(0, h, m))
    )
    assert count == 6


# --- 5m schedule (fires at minute % 5 == 1, session 07:00–21:00 UTC) ---

def test_5m_fires_in_session():
    assert should_fetch_timeframe("5m", dt(0, 9,  1)) is True
    assert should_fetch_timeframe("5m", dt(0, 9,  6)) is True
    assert should_fetch_timeframe("5m", dt(0, 9, 11)) is True


def test_5m_session_gated():
    assert should_fetch_timeframe("5m", dt(0, 7,  1)) is True
    assert should_fetch_timeframe("5m", dt(0, 20, 1)) is True
    assert should_fetch_timeframe("5m", dt(0, 6,  1)) is False
    assert should_fetch_timeframe("5m", dt(0, 21, 1)) is False


def test_5m_silent_at_wrong_minutes():
    # Fires at 1,6,11,16,21,26,31,36,41,46,51,56 — others silent
    for m in [0, 2, 3, 4, 5, 7, 10, 15, 20, 25]:
        assert should_fetch_timeframe("5m", dt(0, 9, m)) is False


# --- 30m schedule ---

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


# --- Unused timeframes ---

def test_1h_never_fires():
    for h in range(24):
        for m in range(60):
            assert should_fetch_timeframe("1h", dt(0, h, m)) is False
