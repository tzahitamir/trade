"""Tests for the API daily usage counter in LocalDB."""
from datetime import datetime, timezone, timedelta
from unittest.mock import patch


def test_initial_count_is_zero(db):
    assert db.get_api_calls_today() == 0


def test_increment_accumulates(db):
    db.increment_api_calls(1)
    db.increment_api_calls(1)
    db.increment_api_calls(3)
    assert db.get_api_calls_today() == 5


def test_increment_returns_new_total(db):
    total = db.increment_api_calls(10)
    assert total == 10
    total = db.increment_api_calls(5)
    assert total == 15


def test_yesterday_calls_dont_count_today(db):
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO api_daily_usage (date, calls_used) VALUES (?, 500)", (yesterday,)
    )
    conn.commit()
    assert db.get_api_calls_today() == 0


def test_tomorrow_calls_dont_count_today(db):
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO api_daily_usage (date, calls_used) VALUES (?, 300)", (tomorrow,)
    )
    conn.commit()
    assert db.get_api_calls_today() == 0


def test_limit_alerted_false_by_default(db):
    assert db.api_limit_already_alerted() is False


def test_mark_and_check_limit_alerted(db):
    db.mark_api_limit_alerted()
    assert db.api_limit_already_alerted() is True


def test_mark_alerted_idempotent(db):
    db.mark_api_limit_alerted()
    db.mark_api_limit_alerted()
    assert db.api_limit_already_alerted() is True


def test_alerted_flag_isolated_to_today(db):
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO api_daily_usage (date, calls_used, limit_alerted) VALUES (?, 0, 1)",
        (yesterday,),
    )
    conn.commit()
    assert db.api_limit_already_alerted() is False


def test_increment_and_alert_independent(db):
    db.increment_api_calls(800)
    assert db.api_limit_already_alerted() is False
    db.mark_api_limit_alerted()
    assert db.get_api_calls_today() == 800
    assert db.api_limit_already_alerted() is True
