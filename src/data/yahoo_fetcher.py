import calendar
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import yfinance as yf

INTERVAL_MAP = {
    "5m":  "5m",
    "15m": "15m",
    "30m": "30m",
    "1h":  "60m",
    "4h":  "1h",   # yfinance has no 4h; caller aggregates if needed
    "1d":  "1d",
}

SYMBOL_MAP = {
    "DAX":   "^GDAXI",
    "US100": "NQ=F",    # NASDAQ 100 futures — tracks FTMO US100 CFD price
}

# yfinance limits for intraday history (days back)
MAX_DAYS = {
    "5m":  59,
    "15m": 59,
    "30m": 59,
    "60m": 729,
    "1d":  3650,
}


def _to_yf_symbol(symbol: str) -> str:
    return SYMBOL_MAP.get(symbol.upper(), symbol)


def fetch_yahoo(symbol: str, timeframe: str, days: int = 365) -> List[Dict]:
    """Fetch OHLCV candles from Yahoo Finance and return newest-first list."""
    yf_sym      = _to_yf_symbol(symbol)
    interval    = INTERVAL_MAP.get(timeframe.lower())
    if not interval:
        raise ValueError(f"Unsupported timeframe for Yahoo fetcher: {timeframe}")

    max_days = MAX_DAYS.get(interval, 59)
    fetch_days = min(days, max_days)
    if fetch_days < days:
        import logging
        logging.warning(
            "Yahoo Finance caps %s history at %d days (requested %d)",
            interval, max_days, days
        )

    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=fetch_days)

    ticker = yf.Ticker(yf_sym)
    df = ticker.history(start=start.strftime("%Y-%m-%d"),
                        end=(end + timedelta(days=1)).strftime("%Y-%m-%d"),
                        interval=interval,
                        auto_adjust=True)

    if df is None or df.empty:
        raise RuntimeError(f"Yahoo Finance returned no data for {yf_sym} {interval}")

    candles = []
    for ts, row in df.iterrows():
        # ts is a pandas Timestamp; convert to UTC unix int
        if hasattr(ts, "to_pydatetime"):
            dt = ts.to_pydatetime()
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            unix_ts = int(dt.timestamp())
        else:
            unix_ts = int(ts)
        candles.append({
            "timestamp": unix_ts,
            "open":   float(row["Open"]),
            "high":   float(row["High"]),
            "low":    float(row["Low"]),
            "close":  float(row["Close"]),
            "volume": float(row.get("Volume", 0) or 0),
        })

    # Return newest-first (same convention as TwelveDataFetcher)
    candles.sort(key=lambda c: c["timestamp"], reverse=True)
    return candles
