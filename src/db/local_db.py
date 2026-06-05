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
        if conn is None:
            conn = self._get_conn()
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
        # SMC alerts table
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS smc_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        conn.commit()

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

    def insert_alert(self, symbol: str, timeframe: str, ts: int, type_: str, reason: str = None, image_path: str = None, params: str = None) -> int:
        query = """
            INSERT INTO smc_alerts (symbol, timeframe, ts, type, reason, image_path, params, sent)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
        """
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(query, (symbol, timeframe, ts, type_, reason, image_path, params))
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
