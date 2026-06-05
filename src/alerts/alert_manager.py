from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .telegram_notifier import TelegramNotifier
from analysis.smc_analyzer import SMCAnalyzer
from db.local_db import LocalDB
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import json
from typing import Any


class AlertManager:
    def __init__(self, settings, db: Optional[LocalDB] = None):
        self.settings = settings
        self.notifier: Optional[TelegramNotifier] = None
        bot_token = getattr(settings, "telegram_bot_token", "")
        chat_id = getattr(settings, "telegram_chat_id", "")
        if bot_token and chat_id:
            self.notifier = TelegramNotifier(bot_token, chat_id)
        self.db = db if db else LocalDB(getattr(settings, "db_path"))
        self.analyzer = SMCAnalyzer()
        self.charts_dir = Path(getattr(settings, "charts_dir", "data/charts"))
        self.charts_dir.mkdir(parents=True, exist_ok=True)
        self.dev_mode: bool = getattr(settings, "dev_mode", True)

    @staticmethod
    def _generate_alert_id(symbol: str, timestamp: int) -> str:
        """Generate alert ID: mm-hh-day-month-year-fxpairname  e.g. 30-09-05-06-2026-eurusd"""
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        pair = symbol.lower().replace("/", "")
        return f"{dt.minute:02d}-{dt.hour:02d}-{dt.day:02d}-{dt.month:02d}-{dt.year}-{pair}"

    def evaluate(
        self,
        symbol: str,
        timeframe: str,
        candles: List[Dict],
        lookahead_candles: Optional[List[Dict]] = None,
    ) -> List[Dict]:
        """Evaluate candles for SMC alerts.

        candles: newest-first list (as returned by query_recent / sliding window).
        lookahead_candles: optional newest-first list of candles after the BOS candle,
            used in dev_mode to show the outcome on the chart.
        """
        alerts: List[Dict] = []
        params = {"symbol": symbol, "timeframe": timeframe}
        bos_events = self.analyzer.detect_bos(candles, params=params)
        for ev in bos_events:
            alert_id = self._generate_alert_id(symbol, ev.get("breakout_ts", 0))
            try:
                fig_path = self._render_bos_chart(candles, ev, alert_id, lookahead_candles)
            except Exception:
                fig_path = None
            message = self._format_bos_message(ev)
            alert = {
                "alert_id": alert_id,
                "symbol": symbol,
                "timeframe": timeframe,
                "event": ev,
                "message": message,
                "image_path": fig_path,
            }
            try:
                db_id = self.db.insert_alert(
                    symbol, timeframe, ev.get("breakout_ts", 0), "BOS",
                    message, fig_path,
                    json.dumps(ev.get("params_used", {})),
                    alert_id=alert_id,
                )
                alert["db_id"] = db_id
            except Exception:
                alert["db_id"] = None
            alerts.append(alert)

        return alerts

    def _format_bos_message(self, ev: Dict) -> str:
        direction = ev.get("direction", "")
        lvl = ev.get("broken_level")
        strength = ev.get("break_strength")
        sweep = ev.get("liquidity_sweep") or {}
        sweep_info = ""
        if sweep:
            sweep_ts = datetime.fromtimestamp(sweep["timestamp"], tz=timezone.utc).strftime("%H:%M")
            sweep_info = f" | sweep@{sweep_ts}"
        return f"SMC BOS {direction.upper()} @ {lvl:.5f} (str={strength:.2f}){sweep_info}"

    def _render_bos_chart(
        self,
        candles: List[Dict],
        ev: Dict,
        alert_id: str,
        lookahead_candles: Optional[List[Dict]] = None,
    ) -> str:
        """
        Render BOS chart and save to data/charts/{alert_id}.png.

        candles: newest-first. Reversed to oldest-first for plotting.
        lookahead_candles: newest-first future candles (dev mode only).
            Plotted in gray after the BOS candle to show outcome.

        Annotations (dev mode):
          - Orange arrow pointing to the BOS candle labelled "BOS"
          - Blue arrow pointing to the liquidity sweep candle labelled "SSL" or "BSL"
        """
        N = 60
        # main window: last N candles up to and including BOS candle (oldest→newest)
        c = list(reversed(candles))[-N:]
        closes = [bar["close"] for bar in c]
        x_main = list(range(len(c)))

        # lookahead: future candles after BOS (oldest→newest)
        c_ahead: List[Dict] = []
        if lookahead_candles and self.dev_mode:
            c_ahead = list(reversed(lookahead_candles))
        closes_ahead = [bar["close"] for bar in c_ahead]
        x_ahead = list(range(len(c), len(c) + len(c_ahead)))

        fig, ax = plt.subplots(figsize=(10, 4))
        ax.plot(x_main, closes, color="black", linewidth=1, label="close")
        if closes_ahead:
            ax.plot(x_ahead, closes_ahead, color="gray", linewidth=1,
                    linestyle="--", label="next 10 candles")

        broken = ev.get("broken_level")
        if broken is not None:
            total_x = len(c) + len(c_ahead) - 1
            ax.hlines(broken, 0, total_x, colors="red", linestyles="--",
                      linewidth=0.8, label="broken level")

        # offset for arrows: 5% of the visible price range
        all_prices = closes + closes_ahead
        price_range = max(all_prices) - min(all_prices) or closes[-1] * 0.001
        arrow_offset = price_range * 0.12
        direction = ev.get("direction", "bullish")
        # arrows point UP for bullish (annotation text sits below the candle),
        # DOWN for bearish (annotation text sits above)
        sign = 1 if direction == "bullish" else -1

        arrow_props = dict(arrowstyle="-|>", lw=1.2)

        # BOS arrow
        bos_x = len(c) - 1
        bos_y = closes[-1]
        ax.annotate(
            "BOS",
            xy=(bos_x, bos_y),
            xytext=(bos_x, bos_y - sign * arrow_offset),
            fontsize=8, fontweight="bold", color="darkorange", ha="center",
            arrowprops={**arrow_props, "color": "darkorange"},
        )

        # Liquidity sweep arrow
        sweep = ev.get("liquidity_sweep") or {}
        sweep_ts = sweep.get("timestamp")
        if sweep_ts:
            sweep_idx = next((i for i, bar in enumerate(c) if bar["timestamp"] == sweep_ts), None)
            if sweep_idx is not None:
                sweep_y = c[sweep_idx]["close"]
                sweep_label = sweep.get("type", "sweep")
                ax.annotate(
                    sweep_label,
                    xy=(sweep_idx, sweep_y),
                    xytext=(sweep_idx, sweep_y - sign * arrow_offset),
                    fontsize=8, fontweight="bold", color="royalblue", ha="center",
                    arrowprops={**arrow_props, "color": "royalblue"},
                )

        mode_tag = " [DEV]" if self.dev_mode and c_ahead else ""
        ax.set_title(
            f"{ev.get('symbol')} {ev.get('timeframe')} BOS {direction.upper()}{mode_tag}  |  {alert_id}",
            fontsize=9,
        )
        ax.legend(fontsize=7)
        ax.tick_params(labelsize=7)

        chart_path = self.charts_dir / f"{alert_id}.png"
        fig.tight_layout()
        fig.savefig(str(chart_path), dpi=100)
        plt.close(fig)
        return str(chart_path)

    def format_alert(self, alert: Dict) -> str:
        return f"[{alert.get('alert_id')}] {alert.get('message')}"

    def send_alert(self, message) -> None:
        if isinstance(message, dict):
            text = message.get("message")
            img = message.get("image_path")
            alert_id = message.get("alert_id", "")
            caption = f"[{alert_id}]\n{text}" if alert_id else text
            if self.notifier:
                if img:
                    self.notifier.send_photo(img, caption=caption)
                else:
                    self.notifier.send_message(caption)
            else:
                print(caption)
        else:
            if self.notifier:
                self.notifier.send_message(message)
            else:
                print(message)

    def send_fetch_error(self, symbol: str, timeframe: str, error_message: str) -> None:
        self.send_alert(f"[trade] fetch error for {symbol} {timeframe}: {error_message}")

    def send_test_alert(self) -> None:
        self.send_alert("[trade] test alert: Telegram notifications are configured and working.")
