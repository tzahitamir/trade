"""Tests for run_decay_check: WR decay flagging (🔴/🟡/🟢)."""
import time
from unittest.mock import MagicMock
from main import run_decay_check


def _make_am(notifier=None):
    am = MagicMock()
    am.notifier = notifier or MagicMock()
    return am


def _set_gold(db, symbol, win_rate):
    """Insert a gold_params row for BOS15m strategy."""
    conn = db._get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO gold_params "
        "(strategy, symbol, param_set_id, win_rate, ev_1_2, trade_count, set_at) "
        "VALUES (?, ?, 1, ?, 1.5, 50, ?)",
        ("BOS15m", symbol, win_rate, int(time.time())),
    )
    conn.commit()


def _add_closed_trades(db, symbol, tp_count, sl_count):
    """Insert closed trade_monitor rows."""
    conn = db._get_conn()
    ts = int(time.time())
    for i in range(tp_count):
        conn.execute(
            "INSERT OR IGNORE INTO trade_monitors "
            "(alert_id, symbol, direction, entry, sl, tp, breakout_ts, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (f"{symbol}_tp_{i}", symbol, "bullish", 1.1, 1.09, 1.12, ts, "tp_hit", ts),
        )
    for i in range(sl_count):
        conn.execute(
            "INSERT OR IGNORE INTO trade_monitors "
            "(alert_id, symbol, direction, entry, sl, tp, breakout_ts, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (f"{symbol}_sl_{i}", symbol, "bullish", 1.1, 1.09, 1.12, ts, "sl_hit", ts),
        )
    conn.commit()


# ── No live trades ─────────────────────────────────────────────────────────────

def test_no_closed_trades_returns_early(db):
    am = _make_am()
    run_decay_check(db, am)
    am.notifier.send_message.assert_not_called()


# ── Below min_live_trades threshold ───────────────────────────────────────────

def test_skips_symbol_below_min_live_trades(db):
    _set_gold(db, "EURUSD", 0.65)
    _add_closed_trades(db, "EURUSD", 2, 1)  # n=3, below default min=5

    am = _make_am()
    run_decay_check(db, am, min_live_trades=5)

    msg = am.notifier.send_message.call_args[0][0]
    assert "skipping" in msg.lower() or "below min" in msg.lower()


# ── 🔴 DEGRADED (live WR ≥ 15pp below gold) ───────────────────────────────────

def test_degraded_flag_when_live_wr_below_threshold(db):
    _set_gold(db, "EURUSD", 0.65)
    # live WR = 4/10 = 40%, gold 65%, delta = -25pp → 🔴
    _add_closed_trades(db, "EURUSD", 4, 6)

    am = _make_am()
    run_decay_check(db, am, min_live_trades=5, decay_threshold=0.15)

    msg = am.notifier.send_message.call_args[0][0]
    assert "DEGRADED" in msg or "🔴" in msg


# ── 🟡 watch (live WR 7.5–15pp below gold) ────────────────────────────────────

def test_watch_flag_when_live_wr_slightly_below_threshold(db):
    _set_gold(db, "EURUSD", 0.65)
    # live WR = 55/100 = 55%, gold 65%, delta = -10pp → 🟡
    _add_closed_trades(db, "EURUSD", 55, 45)

    am = _make_am()
    run_decay_check(db, am, min_live_trades=5, decay_threshold=0.15)

    msg = am.notifier.send_message.call_args[0][0]
    assert "watch" in msg.lower() or "🟡" in msg


# ── 🟢 ok (live WR within tolerance) ─────────────────────────────────────────

def test_ok_flag_when_live_wr_acceptable(db):
    _set_gold(db, "EURUSD", 0.65)
    # live WR = 65/100 = 65%, gold 65%, delta = 0pp → 🟢
    _add_closed_trades(db, "EURUSD", 65, 35)

    am = _make_am()
    run_decay_check(db, am, min_live_trades=5, decay_threshold=0.15)

    msg = am.notifier.send_message.call_args[0][0]
    assert "ok" in msg.lower() or "🟢" in msg


# ── No gold baseline ──────────────────────────────────────────────────────────

def test_no_gold_baseline_reports_without_crash(db):
    # No gold_params row for EURUSD
    _add_closed_trades(db, "EURUSD", 10, 5)

    am = _make_am()
    run_decay_check(db, am, min_live_trades=5)

    msg = am.notifier.send_message.call_args[0][0]
    assert "no gold baseline" in msg.lower() or "no gold" in msg.lower()


# ── Degraded symbol gets warning footer ───────────────────────────────────────

def test_degraded_footer_present(db):
    _set_gold(db, "EURUSD", 0.65)
    _add_closed_trades(db, "EURUSD", 3, 7)  # WR 30%, delta -35pp

    am = _make_am()
    run_decay_check(db, am, min_live_trades=5, decay_threshold=0.15)

    msg = am.notifier.send_message.call_args[0][0]
    assert "manual review" in msg.lower() or "Degraded" in msg
