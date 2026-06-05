import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import requests

_ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.endpoint = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    @staticmethod
    def _is_quiet_time() -> bool:
        """True if current Israel time is between 23:00 and 07:00 (no alerts)."""
        hour = datetime.now(tz=_ISRAEL_TZ).hour
        return hour >= 23 or hour < 7

    def send_message(self, message: str) -> bool:
        if not self.is_configured():
            logging.warning("Telegram notifier is not configured.")
            return False
        if self._is_quiet_time():
            logging.info("Telegram quiet time (23:00–07:00 IL) — message suppressed")
            return False

        payload = {
            "chat_id": self.chat_id,
            "text": message,
        }

        try:
            response = requests.post(self.endpoint, data=payload, timeout=15)
            response.raise_for_status()
        except requests.RequestException as exc:
            logging.exception("Failed to send Telegram message: %s", exc)
            return False

        body = response.json()
        if not body.get("ok"):
            logging.error("Telegram API returned error: %s", body)
            return False

        return True

    def send_photo(self, image_path: str, caption: str = "") -> bool:
        if not self.is_configured():
            logging.warning("Telegram notifier is not configured.")
            return False
        if self._is_quiet_time():
            logging.info("Telegram quiet time (23:00–07:00 IL) — photo suppressed")
            return False

        endpoint = f"https://api.telegram.org/bot{self.bot_token}/sendPhoto"
        files = {"photo": open(image_path, "rb")}
        data = {"chat_id": self.chat_id, "caption": caption}
        try:
            response = requests.post(endpoint, data=data, files=files, timeout=30)
            response.raise_for_status()
        except requests.RequestException as exc:
            logging.exception("Failed to send Telegram photo: %s", exc)
            return False

        body = response.json()
        if not body.get("ok"):
            logging.error("Telegram API returned error on sendPhoto: %s", body)
            return False

        return True
