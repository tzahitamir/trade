from typing import Dict, List, Optional

from .telegram_notifier import TelegramNotifier
from analysis.smc_analyzer import SMCAnalyzer
from db.local_db import LocalDB
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import tempfile
import json
from typing import Any

class AlertManager:
    def __init__(self, settings):
        self.settings = settings
        self.notifier: Optional[TelegramNotifier] = None
        bot_token = getattr(settings, "telegram_bot_token", "")
        chat_id = getattr(settings, "telegram_chat_id", "")
        if bot_token and chat_id:
            self.notifier = TelegramNotifier(bot_token, chat_id)
        # local DB for storing alerts
        self.db = LocalDB(getattr(settings, "db_path"))
        self.analyzer = SMCAnalyzer()

    def evaluate(self, symbol: str, timeframe: str, candles: List[Dict]) -> List[Dict]:
        """Evaluate candles for SMC alerts.

        Placeholder method for future SMC logic. It should return alerts when
        price action matches the configured SMC structure.
        """
        alerts: List[Dict] = []
        # Only run BOS detection for now
        params = {"symbol": symbol, "timeframe": timeframe}
        bos_events = self.analyzer.detect_bos(candles, params=params)
        for ev in bos_events:
            # create a simple chart
            try:
                fig_path = self._render_bos_chart(candles, ev)
            except Exception as exc:
                fig_path = None
            message = self._format_bos_message(ev)
            alert = {
                "symbol": symbol,
                "timeframe": timeframe,
                "event": ev,
                "message": message,
                "image_path": fig_path,
            }
            # persist alert
            try:
                alert_id = self.db.insert_alert(symbol, timeframe, ev.get("breakout_ts", 0), "BOS", message, fig_path, json.dumps(ev.get("params_used", {})))
                alert["alert_id"] = alert_id
            except Exception:
                alert["alert_id"] = None
            alerts.append(alert)

        return alerts

    def _format_bos_message(self, ev: Dict) -> str:
        dir = ev.get("direction")
        lvl = ev.get("broken_level")
        strength = ev.get("break_strength")
        return f"SMC ALERT: BOS {dir.upper()} at {lvl:.5f} (strength={strength:.2f})"

    def _render_bos_chart(self, candles: List[Dict], ev: Dict) -> str:
        # plot last N candles with broken level
        N = 60
        c = list(reversed(candles))[-N:]
        times = [bar["timestamp"] for bar in c]
        opens = [bar["open"] for bar in c]
        highs = [bar["high"] for bar in c]
        lows = [bar["low"] for bar in c]
        closes = [bar["close"] for bar in c]
        fig, ax = plt.subplots(figsize=(8,4))
        ax.plot(closes, color="black", label="close")
        broken = ev.get("broken_level")
        if broken is not None:
            ax.hlines(broken, 0, len(closes)-1, colors="red", linestyles="--", label="broken level")
        ax.set_title(f"{ev.get('symbol')} {ev.get('timeframe')} BOS {ev.get('direction')}")
        ax.legend()
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        fig.tight_layout()
        fig.savefig(tmp.name)
        plt.close(fig)
        return tmp.name

    def format_alert(self, alert: Dict) -> str:
        return f"SMC alert for {alert.get('symbol')} {alert.get('timeframe')}: {alert.get('message')}"

    def send_alert(self, message: str) -> None:
        # message can be str or dict with image
        if isinstance(message, dict):
            text = message.get("message")
            img = message.get("image_path")
            if self.notifier:
                if img:
                    self.notifier.send_photo(img, caption=text)
                else:
                    self.notifier.send_message(text)
            else:
                print(text)
        else:
            if self.notifier:
                self.notifier.send_message(message)
            else:
                print(message)

    def send_fetch_error(self, symbol: str, timeframe: str, error_message: str) -> None:
        message = f"[trade] fetch error for {symbol} {timeframe}: {error_message}"
        self.send_alert(message)

    def send_test_alert(self) -> None:
        self.send_alert("[trade] test alert: Telegram notifications are configured and working.")
