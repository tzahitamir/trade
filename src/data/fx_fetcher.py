from typing import Dict, List

from config.settings import Settings
from .twelve_data_fetcher import TwelveDataFetcher

class FXFetcher:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._provider = self._build_provider()

    def _build_provider(self):
        if self.settings.provider_name == "twelve_data":
            return TwelveDataFetcher(self.settings)
        raise ValueError(f"Unsupported provider: {self.settings.provider_name}")

    def fetch_historical(self, symbol: str, timeframe: str, limit: int = 500, end_date: str = None) -> List[Dict]:
        return self._provider.fetch_historical(symbol, timeframe, limit=limit, end_date=end_date)
