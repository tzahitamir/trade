import sqlite3
import threading
from pathlib import Path
from typing import Dict, List, Optional

class LocalDB:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # thread-local storage for connections
        self._local = threading.local()
        # ensure schema exists using a short-lived connection
        tmp_conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        try:
            self._init_schema(tmp_conn)
        finally:
            tmp_conn.close()

    def _init_schema(self, conn=None) -> None:
        is_local = False
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            is_local = True
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fx_candles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL,
                UNIQUE(symbol, timeframe, timestamp)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS smc_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id TEXT,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                ts INTEGER NOT NULL,
                type TEXT NOT NULL,
                reason TEXT,
                image_path TEXT,
                params TEXT,
                sent INTEGER DEFAULT 0,
                feedback TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS param_sets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                params_hash TEXT NOT NULL UNIQUE,
                params TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS raw_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_run_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                breakout_ts INTEGER NOT NULL,
                alert_id TEXT NOT NULL,
                direction TEXT NOT NULL,
                broken_level REAL NOT NULL,
                break_strength REAL NOT NULL,
                htf_bias TEXT,
                confluences TEXT NOT NULL,
                outcome TEXT NOT NULL,
                hour INTEGER NOT NULL,
                month TEXT NOT NULL,
                has_liquidity_sweep INTEGER NOT NULL DEFAULT 0,
                UNIQUE(scan_run_id, alert_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scan_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                param_set_id INTEGER NOT NULL,
                scan_date INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                total_signals INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                open_trades INTEGER NOT NULL DEFAULT 0,
                stats_json TEXT NOT NULL,
                FOREIGN KEY (param_set_id) REFERENCES param_sets(id)
            )
            """
        )
        # migrate: add columns to existing DBs that predate this schema
        for col_sql in [
            "ALTER TABLE smc_alerts ADD COLUMN alert_id TEXT",
            "ALTER TABLE smc_alerts ADD COLUMN param_set_id INTEGER",
            "ALTER TABLE raw_signals ADD COLUMN swing_age_candles INTEGER",
            "ALTER TABLE raw_signals ADD COLUMN session TEXT",
            "ALTER TABLE raw_signals ADD COLUMN dow INTEGER",
        ]:
            try:
                conn.execute(col_sql)
            except sqlite3.OperationalError:
                pass  # column already exists
        conn.commit()
        if is_local:
            conn.close()

    def insert_candles(self, symbol: str, timeframe: str, candles: List[Dict]) -> None:
        query = """
            INSERT OR IGNORE INTO fx_candles
            (symbol, timeframe, timestamp, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        values = [
            (
                symbol,
                timeframe,
                candle["timestamp"],
                candle["open"],
                candle["high"],
                candle["low"],
                candle["close"],
                candle.get("volume"),
            )
            for candle in candles
        ]
        with self._lock:
            conn = self._get_conn()
            conn.executemany(query, values)
            conn.commit()

    def query_recent(self, symbol: str, timeframe: str, limit: int = 100) -> List[Dict]:
        query = """
            SELECT timestamp, open, high, low, close, volume
            FROM fx_candles
            WHERE symbol = ? AND timeframe = ?
            ORDER BY timestamp DESC
            LIMIT ?
        """
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(query, (symbol, timeframe, limit))
            rows = cursor.fetchall()
        return [
            {
                "timestamp": row[0],
                "open": row[1],
                "high": row[2],
                "low": row[3],
                "close": row[4],
                "volume": row[5],
            }
            for row in rows
        ]

    def get_latest_timestamp(self, symbol: str, timeframe: str) -> Optional[int]:
        query = """
            SELECT timestamp
            FROM fx_candles
            WHERE symbol = ? AND timeframe = ?
            ORDER BY timestamp DESC
            LIMIT 1
        """
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(query, (symbol, timeframe))
            row = cursor.fetchone()
        return row[0] if row else None

    def get_earliest_timestamp(self, symbol: str, timeframe: str) -> Optional[int]:
        query = """
            SELECT timestamp
            FROM fx_candles
            WHERE symbol = ? AND timeframe = ?
            ORDER BY timestamp ASC
            LIMIT 1
        """
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(query, (symbol, timeframe))
            row = cursor.fetchone()
        return row[0] if row else None

    def get_or_create_param_set(self, params: dict) -> int:
        """Return the ID of this parameter set, inserting it if it doesn't exist yet."""
        import json, hashlib, time as _time
        params_json = json.dumps(params, sort_keys=True)
        params_hash = hashlib.sha256(params_json.encode()).hexdigest()[:16]
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT id FROM param_sets WHERE params_hash = ?", (params_hash,)
            ).fetchone()
            if row:
                return row[0]
            cursor = conn.execute(
                "INSERT INTO param_sets (created_at, params_hash, params) VALUES (?, ?, ?)",
                (int(_time.time()), params_hash, params_json),
            )
            conn.commit()
            return cursor.lastrowid

    def close(self) -> None:
        # close thread-local connection if exists
        with self._lock:
            conn = getattr(self._local, "conn", None)
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
                self._local.conn = None

    def _get_conn(self):
        """Return a sqlite3 connection specific to the current thread."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._local.conn = conn
        return conn

    def insert_alert(self, symbol: str, timeframe: str, ts: int, type_: str, reason: str = None, image_path: str = None, params: str = None, alert_id: str = None, param_set_id: int = None) -> int:
        query = """
            INSERT INTO smc_alerts (alert_id, symbol, timeframe, ts, type, reason, image_path, params, param_set_id, sent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        """
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(query, (alert_id, symbol, timeframe, ts, type_, reason, image_path, params, param_set_id))
            conn.commit()
            return cursor.lastrowid

    def insert_raw_signals(self, scan_run_id: int, signals: list) -> None:
        query = """
            INSERT OR IGNORE INTO raw_signals
            (scan_run_id, symbol, timeframe, breakout_ts, alert_id, direction,
             broken_level, break_strength, htf_bias, confluences, outcome,
             hour, month, has_liquidity_sweep, swing_age_candles, session, dow)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        rows = [
            (scan_run_id, r["symbol"], r["timeframe"], r["breakout_ts"], r["alert_id"],
             r["direction"], r["broken_level"], r["break_strength"], r.get("htf_bias"),
             r["confluences"], r["outcome"], r["hour"], r["month"],
             r.get("has_liquidity_sweep", 0), r.get("swing_age_candles"),
             r.get("session"), r.get("dow"))
            for r in signals
        ]
        with self._lock:
            conn = self._get_conn()
            conn.executemany(query, rows)
            conn.commit()

    def get_latest_raw_signals(self) -> tuple:
        """Return (scan_run_id, list-of-dicts) for the most recent scan run."""
        cols = ["id", "scan_run_id", "symbol", "timeframe", "breakout_ts", "alert_id",
                "direction", "broken_level", "break_strength", "htf_bias", "confluences",
                "outcome", "hour", "month", "has_liquidity_sweep",
                "swing_age_candles", "session", "dow"]
        with self._lock:
            conn = self._get_conn()
            row = conn.execute("SELECT MAX(scan_run_id) FROM raw_signals").fetchone()
            if not row or row[0] is None:
                return None, []
            scan_run_id = row[0]
            rows = conn.execute(
                "SELECT " + ", ".join(cols) + " FROM raw_signals WHERE scan_run_id=?",
                (scan_run_id,)
            ).fetchall()
        return scan_run_id, [dict(zip(cols, r)) for r in rows]

    def insert_scan_stats(self, param_set_id: int, symbol: str, total: int, wins: int, losses: int, open_count: int, stats_json: str) -> int:
        import time as _time
        query = """
            INSERT INTO scan_stats
            (param_set_id, scan_date, symbol, total_signals, wins, losses, open_trades, stats_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(query, (param_set_id, int(_time.time()), symbol, total, wins, losses, open_count, stats_json))
            conn.commit()
            return cursor.lastrowid

    def update_alert_feedback(self, alert_id: int, feedback: str) -> None:
        query = """
            UPDATE smc_alerts SET feedback = ? WHERE id = ?
        """
        with self._lock:
            conn = self._get_conn()
            conn.execute(query, (feedback, alert_id))
            conn.commit()
