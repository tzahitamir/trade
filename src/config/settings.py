import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

_PROJECT_ROOT = Path(__file__).parents[2]

@dataclass
class Settings:
    db_path: str = str(Path("data/trade.db").resolve())
    charts_dir: str = str(_PROJECT_ROOT / "data" / "charts")
    fx_pairs: List[str] = field(default_factory=lambda: ["EURUSD", "EURJPY", "USDCAD", "USDCHF", "NZDUSD"])
    # alert_pairs: subset of fx_pairs that get live alerts. If None, defaults to all fx_pairs.
    alert_pairs: Optional[List[str]] = None
    timeframes: List[str] = field(default_factory=lambda: ["5m", "15m", "30m", "1h", "4h"])
    provider_name: str = "twelve_data"
    provider_api_key: str = ""
    alert_threshold: float = 0.0
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    dev_mode: bool = True
    dax_alerts_enabled: bool = False  # disabled until larger sample accumulated

    def should_alert(self, symbol: str) -> bool:
        """Return True if this symbol should generate live alerts."""
        pairs = self.alert_pairs if self.alert_pairs is not None else self.fx_pairs
        return symbol in pairs

    @classmethod
    def load_from_yaml(cls, path: str) -> "Settings":
        import yaml

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        defaults = cls()
        settings = cls(
            db_path=data.get("db_path", defaults.db_path),
            charts_dir=data.get("charts_dir", defaults.charts_dir),
            fx_pairs=data.get("fx_pairs", defaults.fx_pairs),
            alert_pairs=data.get("alert_pairs", None),
            timeframes=data.get("timeframes", defaults.timeframes),
            provider_name=data.get("provider", {}).get("name", defaults.provider_name),
            provider_api_key=data.get("provider", {}).get("api_key", defaults.provider_api_key),
            alert_threshold=data.get("alert_threshold", defaults.alert_threshold),
            telegram_bot_token=data.get("telegram", {}).get("bot_token", ""),
            telegram_chat_id=data.get("telegram", {}).get("chat_id", ""),
            dev_mode=data.get("dev_mode", defaults.dev_mode),
            dax_alerts_enabled=data.get("dax_alerts_enabled", defaults.dax_alerts_enabled),
        )

        settings.provider_api_key = os.environ.get("TWELVE_DATA_API_KEY", settings.provider_api_key)
        settings.telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", settings.telegram_bot_token)
        settings.telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", settings.telegram_chat_id)
        settings.provider_name = os.environ.get("TRADE_PROVIDER_NAME", settings.provider_name)
        settings.alert_threshold = float(os.environ.get("TRADE_ALERT_THRESHOLD", settings.alert_threshold))
        settings.db_path = os.environ.get("TRADE_DB_PATH", settings.db_path)
        settings.charts_dir = os.environ.get("TRADE_CHARTS_DIR", settings.charts_dir)
        dev_env = os.environ.get("TRADE_DEV_MODE")
        if dev_env is not None:
            settings.dev_mode = dev_env.lower() in ("true", "1")

        return settings
