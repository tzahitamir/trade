import sqlite3
import threading
import time as _time_mod
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gold_params (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy TEXT NOT NULL,
                symbol TEXT NOT NULL,
                param_set_id INTEGER NOT NULL,
                win_rate REAL NOT NULL,
                ev_1_2 REAL NOT NULL,
                trade_count INTEGER NOT NULL,
                set_at INTEGER NOT NULL,
                UNIQUE(strategy, symbol)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trade_monitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id TEXT NOT NULL UNIQUE,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry REAL NOT NULL,
                sl REAL NOT NULL,
                tp REAL NOT NULL,
                breakout_ts INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at INTEGER NOT NULL,
                notified_stall INTEGER NOT NULL DEFAULT 0,
                notified_close INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_daily_usage (
                date TEXT PRIMARY KEY,
                calls_used INTEGER NOT NULL DEFAULT 0,
                limit_alerted INTEGER NOT NULL DEFAULT 0
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
            "ALTER TABLE raw_signals ADD COLUMN strategy TEXT NOT NULL DEFAULT 'BOS'",
            "ALTER TABLE raw_signals ADD COLUMN fvg_size_atr REAL",
            "ALTER TABLE raw_signals ADD COLUMN retrace_depth REAL",
            "ALTER TABLE raw_signals ADD COLUMN doji_body_pct REAL",
            "ALTER TABLE raw_signals ADD COLUMN swing_test_count INTEGER",
            "ALTER TABLE raw_signals ADD COLUMN break_body_pct REAL",
            "ALTER TABLE raw_signals ADD COLUMN outcome_broken_level TEXT",
            "ALTER TABLE raw_signals ADD COLUMN outcome_break_candle TEXT",
            "ALTER TABLE raw_signals ADD COLUMN eff_r_broken_level REAL",
            "ALTER TABLE raw_signals ADD COLUMN eff_r_break_candle REAL",
            "ALTER TABLE raw_signals ADD COLUMN rejection_wick_pct REAL",
            "ALTER TABLE raw_signals ADD COLUMN pool_type TEXT",
        ]:
            try:
                conn.execute(col_sql)
            except sqlite3.OperationalError:
                pass  # column already exists
        # Partial unique index on smc_alerts.alert_id (non-NULL values only).
        # Deduplicate first so the index creation succeeds on existing DBs.
        conn.execute(
            "DELETE FROM smc_alerts WHERE id NOT IN ("
            "  SELECT MIN(id) FROM smc_alerts"
            "  WHERE alert_id IS NOT NULL GROUP BY alert_id"
            ") AND alert_id IS NOT NULL"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS smc_alerts_alert_id_uniq"
            " ON smc_alerts (alert_id) WHERE alert_id IS NOT NULL"
        )
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

    def get_all_timestamps(
        self,
        symbol: str,
        timeframe: str,
        since_ts: Optional[int] = None,
    ) -> List[int]:
        """Return all stored timestamps for (symbol, timeframe) in ascending order."""
        if since_ts is not None:
            query = """
                SELECT timestamp FROM fx_candles
                WHERE symbol = ? AND timeframe = ? AND timestamp >= ?
                ORDER BY timestamp ASC
            """
            params = (symbol, timeframe, since_ts)
        else:
            query = """
                SELECT timestamp FROM fx_candles
                WHERE symbol = ? AND timeframe = ?
                ORDER BY timestamp ASC
            """
            params = (symbol, timeframe)
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
        return [row[0] for row in rows]

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

    def get_param_set_by_id(self, param_set_id: int) -> dict:
        """Return the params dict for a given param_set_id, or {} if not found."""
        import json
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT params FROM param_sets WHERE id = ?", (param_set_id,)
            ).fetchone()
        return json.loads(row[0]) if row else {}

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
             hour, month, has_liquidity_sweep, swing_age_candles, session, dow, strategy,
             fvg_size_atr, retrace_depth, doji_body_pct,
             swing_test_count, break_body_pct,
             outcome_broken_level, outcome_break_candle,
             eff_r_broken_level, eff_r_break_candle, rejection_wick_pct, pool_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?)
        """
        rows = [
            (scan_run_id, r["symbol"], r["timeframe"], r["breakout_ts"], r["alert_id"],
             r["direction"], r["broken_level"], r["break_strength"], r.get("htf_bias"),
             r["confluences"], r["outcome"], r["hour"], r["month"],
             r.get("has_liquidity_sweep", 0), r.get("swing_age_candles"),
             r.get("session"), r.get("dow"), r.get("strategy", "BOS"),
             r.get("fvg_size_atr"), r.get("retrace_depth"), r.get("doji_body_pct"),
             r.get("swing_test_count"), r.get("break_body_pct"),
             r.get("outcome_broken_level"), r.get("outcome_break_candle"),
             r.get("eff_r_broken_level"), r.get("eff_r_break_candle"),
             r.get("rejection_wick_pct"), r.get("pool_type"))
            for r in signals
        ]
        with self._lock:
            conn = self._get_conn()
            conn.executemany(query, rows)
            conn.commit()

    def get_latest_raw_signals(self, strategy: str = None) -> tuple:
        """Return (scan_run_id, list-of-dicts) for the most recent scan run, optionally filtered by strategy."""
        cols = ["id", "scan_run_id", "symbol", "timeframe", "breakout_ts", "alert_id",
                "direction", "broken_level", "break_strength", "htf_bias", "confluences",
                "outcome", "hour", "month", "has_liquidity_sweep",
                "swing_age_candles", "session", "dow", "strategy",
                "fvg_size_atr", "retrace_depth", "doji_body_pct",
                "swing_test_count", "break_body_pct",
                "outcome_broken_level", "outcome_break_candle",
                "eff_r_broken_level", "eff_r_break_candle", "rejection_wick_pct",
                "pool_type"]
        with self._lock:
            conn = self._get_conn()
            if strategy:
                row = conn.execute(
                    "SELECT MAX(scan_run_id) FROM raw_signals WHERE strategy=?", (strategy,)
                ).fetchone()
            else:
                row = conn.execute("SELECT MAX(scan_run_id) FROM raw_signals").fetchone()
            if not row or row[0] is None:
                return None, []
            scan_run_id = row[0]
            if strategy:
                rows = conn.execute(
                    "SELECT " + ", ".join(cols) + " FROM raw_signals WHERE scan_run_id=? AND strategy=?",
                    (scan_run_id, strategy)
                ).fetchall()
            else:
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

    def upsert_gold_params(self, strategy: str, symbol: str, param_set_id: int,
                           win_rate: float, ev_1_2: float, trade_count: int) -> None:
        import time as _time
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """
                INSERT INTO gold_params (strategy, symbol, param_set_id, win_rate, ev_1_2, trade_count, set_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy, symbol) DO UPDATE SET
                    param_set_id=excluded.param_set_id,
                    win_rate=excluded.win_rate,
                    ev_1_2=excluded.ev_1_2,
                    trade_count=excluded.trade_count,
                    set_at=excluded.set_at
                """,
                (strategy, symbol, param_set_id, win_rate, ev_1_2, trade_count, int(_time.time())),
            )
            conn.commit()

    # ── trade monitor helpers ──────────────────────────────────────────────────

    def insert_monitor(self, alert_id: str, symbol: str, direction: str,
                       entry: float, sl: float, tp: float, breakout_ts: int) -> None:
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """INSERT OR IGNORE INTO trade_monitors
                   (alert_id, symbol, direction, entry, sl, tp, breakout_ts, status, created_at)
                   VALUES (?,?,?,?,?,?,?,'open',?)""",
                (alert_id, symbol, direction, entry, sl, tp, breakout_ts, int(_time_mod.time())),
            )
            conn.commit()

    def get_open_monitors(self) -> List[Dict]:
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT id, alert_id, symbol, direction, entry, sl, tp, breakout_ts, "
                "notified_stall, notified_close FROM trade_monitors WHERE status='open'"
            ).fetchall()
        keys = ["id", "alert_id", "symbol", "direction", "entry", "sl", "tp",
                "breakout_ts", "notified_stall", "notified_close"]
        return [dict(zip(keys, r)) for r in rows]

    def update_monitor(self, alert_id: str, **kwargs) -> None:
        if not kwargs:
            return
        sets = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [alert_id]
        with self._lock:
            conn = self._get_conn()
            conn.execute(f"UPDATE trade_monitors SET {sets} WHERE alert_id=?", vals)
            conn.commit()

    def query_candles_after(self, symbol: str, timeframe: str, after_ts: int,
                             limit: int = 100) -> List[Dict]:
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT timestamp, open, high, low, close FROM fx_candles "
                "WHERE symbol=? AND timeframe=? AND timestamp>? "
                "ORDER BY timestamp ASC LIMIT ?",
                (symbol, timeframe, after_ts, limit),
            ).fetchall()
        return [{"timestamp": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4]}
                for r in rows]

    def get_gold_params(self, strategy: str, symbol: str = None) -> dict:
        """Return dict keyed by symbol → {param_set_id, win_rate, ev_1_2, trade_count, set_at}."""
        with self._lock:
            conn = self._get_conn()
            if symbol:
                rows = conn.execute(
                    "SELECT symbol, param_set_id, win_rate, ev_1_2, trade_count, set_at "
                    "FROM gold_params WHERE strategy=? AND symbol=?",
                    (strategy, symbol),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT symbol, param_set_id, win_rate, ev_1_2, trade_count, set_at "
                    "FROM gold_params WHERE strategy=?",
                    (strategy,),
                ).fetchall()
        return {
            r[0]: {"param_set_id": r[1], "win_rate": r[2], "ev_1_2": r[3],
                   "trade_count": r[4], "set_at": r[5]}
            for r in rows
        }

    # ------------------------------------------------------------------
    # API credit tracking
    # ------------------------------------------------------------------

    def increment_api_calls(self, n: int = 1) -> int:
        """Increment today's API call counter (UTC date). Returns new total."""
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """
                INSERT INTO api_daily_usage (date, calls_used) VALUES (?, ?)
                ON CONFLICT(date) DO UPDATE SET calls_used = calls_used + ?
                """,
                (today, n, n),
            )
            conn.commit()
            row = conn.execute(
                "SELECT calls_used FROM api_daily_usage WHERE date = ?", (today,)
            ).fetchone()
            return row[0] if row else n

    def get_api_calls_today(self) -> int:
        """Return number of API calls made today (UTC date)."""
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT calls_used FROM api_daily_usage WHERE date = ?", (today,)
            ).fetchone()
            return row[0] if row else 0

    def mark_api_limit_alerted(self) -> None:
        """Record that the daily-limit Telegram alert has already been sent today."""
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """
                INSERT INTO api_daily_usage (date, calls_used, limit_alerted) VALUES (?, 0, 1)
                ON CONFLICT(date) DO UPDATE SET limit_alerted = 1
                """,
                (today,),
            )
            conn.commit()

    def api_limit_already_alerted(self) -> bool:
        """Return True if the daily-limit alert has already been sent today."""
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT limit_alerted FROM api_daily_usage WHERE date = ?", (today,)
            ).fetchone()
            return bool(row and row[0])
