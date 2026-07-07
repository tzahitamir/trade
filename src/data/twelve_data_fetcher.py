import calendar
from datetime import datetime
from typing import Dict, List

import requests

from config.settings import Settings


class TwelveDataFetcher:
    BASE_URL = "https://api.twelvedata.com/time_series"
    INTERVAL_MAP = {
        "5m": "5min",
        "15m": "15min",
        "30m": "30min",
        "1h": "1h",
        "h1": "1h",
        "4h": "4h",
        "h4": "4h",
        "1d": "1day",
        "1day": "1day",
    }

    def _map_interval(self, timeframe: str) -> str:
        interval = self.INTERVAL_MAP.get(timeframe.lower())
        if not interval:
            raise ValueError(f"Unsupported timeframe for Twelve Data: {timeframe}")
        return interval

    def __init__(self, settings: Settings):
        self.settings = settings
        self.api_key = settings.provider_api_key
        if not self.api_key:
            raise ValueError("Twelve Data API key must be provided in settings.provider_api_key")

    def _normalize_symbol(self, symbol: str) -> str:
        if "/" in symbol:
            return symbol
        # Only add slash for 6-char all-alpha FX pairs (e.g. EURUSD → EUR/USD)
        # Indices and other symbols (DE40, US30) pass through unchanged
        if len(symbol) == 6 and symbol.isalpha():
            return symbol[:3] + "/" + symbol[3:]
        return symbol

    def fetch_historical(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 500,
        end_date: str = None,
    ) -> List[Dict]:
        symbol_value = self._normalize_symbol(symbol)
        interval_value = self._map_interval(timeframe)
        params = {
            "symbol": symbol_value,
            "interval": interval_value,
            "outputsize": min(limit, 5000),
            "format": "JSON",
            "apikey": self.api_key,
            "timezone": "UTC",
        }
        if end_date:
            params["end_date"] = end_date

        response = requests.get(self.BASE_URL, params=params, timeout=30)
        if response.status_code != 200:
            raise RuntimeError(
                f"Twelve Data request failed with status {response.status_code}: {response.text}"
            )

        payload = response.json()
        if payload.get("status") == "error":
            raise RuntimeError(
                f"Twelve Data API error: {payload.get('message', 'unknown error')}"
            )

        values = payload.get("values") or []
        candles = []
        for item in reversed(values):
            try:
                fmt = "%Y-%m-%d %H:%M:%S" if " " in item["datetime"] else "%Y-%m-%d"
                timestamp = calendar.timegm(
                    datetime.strptime(item["datetime"], fmt).timetuple()
                )
                candles.append(
                    {
                        "timestamp": int(timestamp),
                        "open": float(item["open"]),
                        "high": float(item["high"]),
                        "low": float(item["low"]),
                        "close": float(item["close"]),
                        "volume": float(item.get("volume", 0)) if item.get("volume") is not None else None,
                    }
                )
            except Exception as exc:
                raise RuntimeError(f"Failed to parse Twelve Data candle: {item}") from exc

        return candles
