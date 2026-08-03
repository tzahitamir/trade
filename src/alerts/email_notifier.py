import logging
import re
import requests

_log = logging.getLogger(__name__)

_RESEND_URL = "https://api.resend.com/emails"
_FROM = "Trade Alerts <onboarding@resend.dev>"


def _telegram_to_html(body: str) -> str:
    """Wrap Telegram-flavored HTML in a minimal email-safe template."""
    body = body.replace("&amp;", "&")
    body = body.replace("\n", "<br>\n")
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  body {{ font-family: Arial, sans-serif; font-size: 14px; color: #222; background: #fff; padding: 20px; }}
  b {{ color: #111; }} i {{ color: #555; }}
  hr {{ border: none; border-top: 1px solid #ddd; margin: 16px 0; }}
  small {{ color: #999; }}
</style>
</head><body>
{body}
<hr><small>trade-alerts → tzahitamir@gmail.com</small>
</body></html>"""


def _strip_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    return text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


class EmailNotifier:
    def __init__(self, api_key: str, recipient: str):
        self.api_key = api_key
        self.recipient = recipient

    def is_configured(self) -> bool:
        return bool(self.api_key and self.recipient)

    def send(self, subject: str, body: str) -> bool:
        if not self.is_configured():
            _log.warning("[EMAIL] not configured — skipping")
            return False
        try:
            resp = requests.post(
                _RESEND_URL,
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json={
                    "from": _FROM,
                    "to": [self.recipient],
                    "subject": subject,
                    "html": _telegram_to_html(body),
                    "text": _strip_tags(body),
                },
                timeout=15,
            )
            resp.raise_for_status()
            _log.info("[EMAIL] sent '%s' to %s", subject, self.recipient)
            return True
        except Exception as exc:
            _log.exception("[EMAIL] failed '%s': %s", subject, exc)
            return False
