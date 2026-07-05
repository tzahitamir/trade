"""Tests for momentum_monitor_job: SL, TP, stall, expire, no-double-notify."""
import time
from unittest.mock import MagicMock
from main import momentum_monitor_job


def _make_am():
    am = MagicMock()
    return am


def _open_monitor(db, alert_id, symbol, direction, entry, sl, tp, breakout_ts):
    """Insert an open trade_monitor row directly."""
    conn = db._get_conn()
    conn.execute(
        """INSERT OR IGNORE INTO trade_monitors
           (alert_id, symbol, direction, entry, sl, tp, breakout_ts, status, created_at)
           VALUES (?,?,?,?,?,?,?,'open',?)""",
        (alert_id, symbol, direction, entry, sl, tp, breakout_ts, int(time.time())),
    )
    conn.commit()


def _insert_candles(db, symbol, timeframe, candles):
    """Insert candle rows into fx_candles."""
    conn = db._get_conn()
    for c in candles:
        conn.execute(
            "INSERT OR IGNORE INTO fx_candles (symbol, timeframe, timestamp, open, high, low, close, volume)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (symbol, timeframe, c["timestamp"], c["open"], c["high"], c["low"], c["close"], 1000),
        )
    conn.commit()


def _candle(ts, o, h, l, c):
    return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c}


# ── TP hit ─────────────────────────────────────────────────────────────────────

def test_tp_hit_bullish_sends_alert(db):
    entry, sl, tp = 1.1000, 1.0950, 1.1100
    bos_ts = 1_000_000
    _open_monitor(db, "A1", "EURUSD", "bullish", entry, sl, tp, bos_ts)

    # 3 candles: last one touches above TP
    candles = [
        _candle(bos_ts + 900,  1.1000, 1.1020, 1.0995, 1.1015),
        _candle(bos_ts + 1800, 1.1015, 1.1050, 1.1010, 1.1045),
        _candle(bos_ts + 2700, 1.1045, 1.1110, 1.1040, 1.1105),  # high > TP
    ]
    _insert_candles(db, "EURUSD", "15m", candles)
    am = _make_am()
    momentum_monitor_job(db, am)

    am.send_alert.assert_called_once()
    msg = am.send_alert.call_args[0][0]
    assert "TP HIT" in msg
    assert "EURUSD" in msg

    # status should be updated
    monitors = db.get_open_monitors()
    assert not monitors  # closed


def test_sl_hit_bullish_closes_monitor(db):
    entry, sl, tp = 1.1000, 1.0950, 1.1100
    bos_ts = 2_000_000
    _open_monitor(db, "A2", "EURUSD", "bullish", entry, sl, tp, bos_ts)

    candles = [
        _candle(bos_ts + 900,  1.1000, 1.1010, 1.0940, 1.0945),  # low < SL
    ]
    _insert_candles(db, "EURUSD", "15m", candles)
    am = _make_am()
    momentum_monitor_job(db, am)

    # SL hit closes the monitor silently (no Telegram noise on losses)
    am.send_alert.assert_not_called()
    assert not db.get_open_monitors()


def test_tp_hit_bearish_sends_alert(db):
    entry, sl, tp = 1.1000, 1.1050, 1.0900
    bos_ts = 3_000_000
    _open_monitor(db, "A3", "EURUSD", "bearish", entry, sl, tp, bos_ts)

    candles = [
        _candle(bos_ts + 900,  1.1000, 1.1005, 1.0895, 1.0900),  # low <= TP
    ]
    _insert_candles(db, "EURUSD", "15m", candles)
    am = _make_am()
    momentum_monitor_job(db, am)

    am.send_alert.assert_called_once()
    assert "TP HIT" in am.send_alert.call_args[0][0]


# ── Stall at candle 4 ─────────────────────────────────────────────────────────

def test_stall_alert_at_candle_4_bullish(db):
    entry, sl, tp = 1.1000, 1.0950, 1.1100
    bos_ts = 4_000_000
    _open_monitor(db, "A4", "EURUSD", "bullish", entry, sl, tp, bos_ts)

    # 4 candles that barely move — progress < 50% of TP distance
    candles = [
        _candle(bos_ts + 900,  1.1000, 1.1005, 1.0995, 1.1002),
        _candle(bos_ts + 1800, 1.1002, 1.1008, 1.0998, 1.1005),
        _candle(bos_ts + 2700, 1.1005, 1.1010, 1.1000, 1.1008),
        _candle(bos_ts + 3600, 1.1008, 1.1015, 1.1005, 1.1012),  # best high 1.1015, far from TP 1.1100
    ]
    _insert_candles(db, "EURUSD", "15m", candles)
    am = _make_am()
    momentum_monitor_job(db, am)

    am.send_alert.assert_called_once()
    msg = am.send_alert.call_args[0][0]
    assert "stall" in msg.lower() or "Momentum" in msg


def test_stall_not_fired_twice(db):
    entry, sl, tp = 1.1000, 1.0950, 1.1100
    bos_ts = 5_000_000
    _open_monitor(db, "A5", "EURUSD", "bullish", entry, sl, tp, bos_ts)

    candles = [
        _candle(bos_ts + i * 900, 1.1000, 1.1005, 1.0995, 1.1002) for i in range(1, 5)
    ]
    _insert_candles(db, "EURUSD", "15m", candles)
    am = _make_am()
    momentum_monitor_job(db, am)  # first scan — should stall
    momentum_monitor_job(db, am)  # second scan — same candles, must NOT fire again

    # Expect exactly 1 alert from both scans combined
    assert am.send_alert.call_count == 1


# ── Expire after 50 candles ────────────────────────────────────────────────────

def test_expire_after_50_candles_no_alert(db):
    entry, sl, tp = 1.1000, 1.0950, 1.1100
    bos_ts = 6_000_000
    _open_monitor(db, "A6", "EURUSD", "bullish", entry, sl, tp, bos_ts)

    # 51 candles that never touch SL or TP
    candles = [
        _candle(bos_ts + i * 900, 1.1000, 1.1005, 1.0995, 1.1002) for i in range(1, 52)
    ]
    _insert_candles(db, "EURUSD", "15m", candles)
    am = _make_am()
    momentum_monitor_job(db, am)

    am.send_alert.assert_not_called()
    monitors = db.get_open_monitors()
    assert not monitors  # expired


def test_no_monitors_returns_early(db):
    am = _make_am()
    momentum_monitor_job(db, am)
    am.send_alert.assert_not_called()
