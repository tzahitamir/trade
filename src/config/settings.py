import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

@dataclass
class Settings:
    db_path: str = str(Path("data/trade.db").resolve())
    fx_pairs: List[str] = field(default_factory=lambda: ["EURUSD", "GBPUSD", "USDJPY"])
    timeframes: List[str] = field(default_factory=lambda: ["M5", "15m", "30m", "1h", "4h"])
    provider_name: str = "twelve_data"
    provider_api_key: str = ""
    alert_threshold: float = 0.0
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @classmethod
    def load_from_yaml(cls, path: str) -> "Settings":
        import yaml

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        settings = cls(
            db_path=data.get("db_path", cls().db_path),
            fx_pairs=data.get("fx_pairs", cls().fx_pairs),
            timeframes=data.get("timeframes", cls().timeframes),
            provider_name=data.get("provider", {}).get("name", cls().provider_name),
            provider_api_key=data.get("provider", {}).get("api_key", cls().provider_api_key),
            alert_threshold=data.get("alert_threshold", cls().alert_threshold),
            telegram_bot_token=data.get("telegram", {}).get("bot_token", ""),
            telegram_chat_id=data.get("telegram", {}).get("chat_id", ""),
        )

        settings.provider_api_key = os.environ.get("TWELVE_DATA_API_KEY", settings.provider_api_key)
        settings.telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", settings.telegram_bot_token)
        settings.telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", settings.telegram_chat_id)
        settings.provider_name = os.environ.get("TRADE_PROVIDER_NAME", settings.provider_name)
        settings.alert_threshold = float(os.environ.get("TRADE_ALERT_THRESHOLD", settings.alert_threshold))
        settings.db_path = os.environ.get("TRADE_DB_PATH", settings.db_path)

        return settings
