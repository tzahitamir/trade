from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .telegram_notifier import TelegramNotifier
from analysis.smc_analyzer import SMCAnalyzer
from analysis.confluence_detector import confluence_labels
from db.local_db import LocalDB
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import json


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
        htf_bias: Optional[str] = None,
    ) -> List[Dict]:
        """Evaluate candles for SMC alerts.

        candles: newest-first list (as returned by query_recent / sliding window).
        lookahead_candles: optional newest-first list of candles after the BOS candle,
            used in dev_mode to show the outcome on the chart.
        htf_bias: 'bullish', 'bearish', or 'neutral' — the HTF (4h) directional
            bias at the time of the BOS, printed on the chart image.
        """
        alerts: List[Dict] = []
        params = {"symbol": symbol, "timeframe": timeframe}
        bos_events = self.analyzer.detect_bos(candles, params=params)
        for ev in bos_events:
            alert_id = self._generate_alert_id(symbol, ev.get("breakout_ts", 0))
            try:
                fig_path, _ = self._render_bos_chart(candles, ev, alert_id, lookahead_candles, htf_bias)
            except Exception:
                fig_path = None
            message = self._format_bos_message(ev, htf_bias)
            alert = {
                "alert_id": alert_id,
                "symbol": symbol,
                "timeframe": timeframe,
                "event": ev,
                "message": message,
                "image_path": fig_path,
                "htf_bias": htf_bias,
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

    def _format_bos_message(self, ev: Dict, htf_bias: Optional[str] = None, confluences: Optional[List[str]] = None) -> str:
        direction = ev.get("direction", "")
        lvl = ev.get("broken_level")
        strength = ev.get("break_strength")
        sweep = ev.get("liquidity_sweep") or {}
        sweep_info = ""
        if sweep:
            sweep_ts = datetime.fromtimestamp(sweep["timestamp"], tz=timezone.utc).strftime("%H:%M")
            sweep_info = f" | sweep@{sweep_ts}"
        bias_info = f" | 4H {htf_bias.upper()}" if htf_bias and htf_bias != "neutral" else ""
        conf_info = f" | conf: {','.join(confluences)}" if confluences else ""
        return f"SMC BOS {direction.upper()} @ {lvl:.5f} (str={strength:.2f}){sweep_info}{bias_info}{conf_info}"

    @staticmethod
    def evaluate_bos_outcome(
        candles: List[Dict],
        ev: Dict,
        lookahead_chron: List[Dict],
        candles_4h: Optional[List[Dict]] = None,
        trigger_ts: Optional[int] = None,
    ) -> str:
        """Compute WIN/LOSS/HTF WIN/HTF LOSS/OPEN without rendering a chart."""
        from analysis.smc_analyzer import SMCAnalyzer as _SA
        PRE_BOS = 50
        c = list(reversed(candles))[-(PRE_BOS + 1):]
        direction = ev.get("direction", "bullish")
        bullish = direction == "bullish"
        _atr = _SA.calculate_atr(candles) or 1e-6
        _swings = _SA._find_last_swing(candles, lookback=3, search_back=20)
        sweep = ev.get("liquidity_sweep") or {}
        if bullish:
            sl_anchor = sweep.get("wick_low") or _swings.get("swing_low") or min(b["low"] for b in c[-10:])
            sl = sl_anchor - _atr * 0.3
        else:
            sl_anchor = sweep.get("wick_high") or _swings.get("swing_high") or max(b["high"] for b in c[-10:])
            sl = sl_anchor + _atr * 0.3
        entry = c[-1]["close"] if c else ev.get("broken_level", 0.0)
        if trigger_ts and lookahead_chron:
            for _b in lookahead_chron:
                if _b["timestamp"] == trigger_ts:
                    entry = _b["close"]
                    break
        risk = abs(entry - sl) or _atr
        tp = (entry + risk * 2) if bullish else (entry - risk * 2)
        outcome = "OPEN"
        if lookahead_chron:
            past_trigger = trigger_ts is None
            for _b in lookahead_chron:
                if not past_trigger:
                    if _b["timestamp"] == trigger_ts:
                        past_trigger = True
                    continue
                if bullish:
                    if _b["low"] <= sl:  outcome = "LOSS"; break
                    if _b["high"] >= tp: outcome = "WIN";  break
                else:
                    if _b["high"] >= sl: outcome = "LOSS"; break
                    if _b["low"] <= tp:  outcome = "WIN";  break
        if outcome == "OPEN" and candles_4h:
            bos_ts = ev.get("breakout_ts", 0)
            future_4h = [_b for _b in reversed(candles_4h) if _b["timestamp"] > bos_ts]
            for _b in future_4h:
                if bullish:
                    if _b["low"] <= sl:  outcome = "HTF LOSS"; break
                    if _b["high"] >= tp: outcome = "HTF WIN";  break
                else:
                    if _b["high"] >= sl: outcome = "HTF LOSS"; break
                    if _b["low"] <= tp:  outcome = "HTF WIN";  break
        return outcome

    @staticmethod
    def _draw_candles(
        ax,
        bars: List[Dict],
        x_offset: int = 0,
        alpha: float = 1.0,
    ) -> None:
        """Draw OHLC candlesticks on ax starting at x_offset."""
        for i, bar in enumerate(bars):
            x = x_offset + i
            o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
            color = "#26a69a" if c >= o else "#ef5350"  # teal up / red down
            body_bottom = min(o, c)
            body_height = abs(c - o) or abs(h - l) * 0.05
            ax.add_patch(
                mpatches.Rectangle(
                    (x - 0.35, body_bottom), 0.7, body_height,
                    color=color, alpha=alpha, zorder=2,
                )
            )
            ax.plot([x, x], [l, h], color=color, linewidth=0.8, alpha=alpha, zorder=1)

    def render_alert(
        self,
        symbol: str,
        timeframe: str,
        ev: Dict,
        candles: List[Dict],
        lookahead_candles: Optional[List[Dict]] = None,
        htf_bias: Optional[str] = None,
        confluences: Optional[List[str]] = None,
        trigger_ts: Optional[int] = None,
        candles_4h: Optional[List[Dict]] = None,
        param_set_id: Optional[int] = None,
        skip_db: bool = False,
    ) -> Dict:
        """Generate chart and DB record for a confirmed BOS event. Returns alert dict."""
        alert_id = self._generate_alert_id(symbol, ev.get("breakout_ts", 0))
        outcome = "OPEN"
        try:
            fig_path, outcome = self._render_bos_chart(
                candles, ev, alert_id, lookahead_candles, htf_bias, confluences, trigger_ts, candles_4h
            )
        except Exception:
            import logging, traceback
            logging.error("Chart render failed for %s: %s", alert_id, traceback.format_exc())
            fig_path = None
        message = self._format_bos_message(ev, htf_bias, confluences)
        alert = {
            "alert_id": alert_id,
            "symbol": symbol,
            "timeframe": timeframe,
            "event": ev,
            "message": message,
            "image_path": fig_path,
            "htf_bias": htf_bias,
            "confluences": confluences or [],
            "outcome": outcome,
            "params_used": ev.get("params_used", {}),
            "param_set_id": param_set_id,
        }
        if not skip_db:
            try:
                db_id = self.db.insert_alert(
                    symbol, timeframe, ev.get("breakout_ts", 0), "BOS",
                    message, fig_path,
                    json.dumps(ev.get("params_used", {})),
                    alert_id=alert_id,
                    param_set_id=param_set_id,
                )
                alert["db_id"] = db_id
            except Exception:
                alert["db_id"] = None
        return alert

    def _render_bos_chart(
        self,
        candles: List[Dict],
        ev: Dict,
        alert_id: str,
        lookahead_candles: Optional[List[Dict]] = None,
        htf_bias: Optional[str] = None,
        confluences: Optional[List[str]] = None,
        trigger_ts: Optional[int] = None,
        candles_4h: Optional[List[Dict]] = None,
    ) -> tuple:
        """
        Render BOS candlestick chart and save to data/charts/{alert_id}.png.

        candles: newest-first. Reversed to oldest-first for plotting.
        lookahead_candles: newest-first future candles (dev mode only),
            drawn muted after the BOS candle to show outcome.

        Annotations:
          - Orange arrow → BOS candle, labelled "BOS"
          - Blue arrow → liquidity sweep candle, labelled "SSL" or "BSL"
        """
        PRE_BOS = 50
        c = list(reversed(candles))[-(PRE_BOS + 1):]  # 50 pre-BOS candles + BOS candle

        c_ahead: List[Dict] = []
        if lookahead_candles and self.dev_mode:
            c_ahead = list(reversed(lookahead_candles))   # oldest → newest

        fig, ax = plt.subplots(figsize=(10, 4))
        self._draw_candles(ax, c, x_offset=0, alpha=1.0)
        if c_ahead:
            self._draw_candles(ax, c_ahead, x_offset=len(c), alpha=0.45)

        # scale axes manually (add_patch doesn't auto-scale)
        all_bars = c + c_ahead
        ax.set_xlim(-1, len(all_bars))
        all_lows  = [b["low"]  for b in all_bars]
        all_highs = [b["high"] for b in all_bars]
        pad = (max(all_highs) - min(all_lows)) * 0.08
        ax.set_ylim(min(all_lows) - pad, max(all_highs) + pad)

        # broken level line
        broken = ev.get("broken_level")
        if broken is not None:
            ax.hlines(broken, 0, len(all_bars) - 1, colors="red",
                      linestyles="--", linewidth=0.8, label="broken level", zorder=3)

        # arrow offset: 15% of visible range
        price_range = max(all_highs) - min(all_lows) or all_highs[0] * 0.001
        arrow_offset = price_range * 0.15
        direction = ev.get("direction", "bullish")
        bullish = direction == "bullish"
        arrow_props = dict(arrowstyle="-|>", lw=1.3)

        # BOS arrow — points at the broken level on the BOS candle (lines up with dashed line)
        bos_x = len(c) - 1
        bos_tip_y  = broken if broken is not None else (c[-1]["low"] if bullish else c[-1]["high"])
        bos_text_y = bos_tip_y - arrow_offset if bullish else bos_tip_y + arrow_offset
        ax.annotate(
            "BOS",
            xy=(bos_x, bos_tip_y),
            xytext=(bos_x, bos_text_y),
            fontsize=8, fontweight="bold", color="darkorange", ha="center", zorder=5,
            arrowprops={**arrow_props, "color": "darkorange"},
        )

        # Sweep arrow — points at the wick that swept liquidity
        sweep = ev.get("liquidity_sweep") or {}
        sweep_ts = sweep.get("timestamp")
        if sweep_ts:
            sweep_idx = next((i for i, bar in enumerate(c) if bar["timestamp"] == sweep_ts), None)
            if sweep_idx is not None:
                # SSL grab wicks LOW; BSL grab wicks HIGH
                sweep_tip_y  = c[sweep_idx]["low"]  if bullish else c[sweep_idx]["high"]
                sweep_text_y = sweep_tip_y - arrow_offset if bullish else sweep_tip_y + arrow_offset
                ax.annotate(
                    sweep.get("type", "sweep"),
                    xy=(sweep_idx, sweep_tip_y),
                    xytext=(sweep_idx, sweep_text_y),
                    fontsize=8, fontweight="bold", color="royalblue", ha="center", zorder=5,
                    arrowprops={**arrow_props, "color": "royalblue"},
                )

        # ALERT arrow — first lookahead candle where confluence was confirmed
        if trigger_ts and c_ahead:
            trigger_idx = next((i for i, bar in enumerate(c_ahead) if bar["timestamp"] == trigger_ts), None)
            if trigger_idx is not None:
                alert_x = len(c) + trigger_idx
                alert_bar = c_ahead[trigger_idx]
                alert_tip_y  = alert_bar["low"]  if bullish else alert_bar["high"]
                alert_text_y = alert_tip_y - arrow_offset if bullish else alert_tip_y + arrow_offset
                action = "BUY" if bullish else "SELL"
                ax.annotate(
                    action,
                    xy=(alert_x, alert_tip_y),
                    xytext=(alert_x, alert_text_y),
                    fontsize=8, fontweight="bold", color="green", ha="center", zorder=5,
                    arrowprops={**arrow_props, "color": "green"},
                )

        # SL / TP / outcome
        from analysis.smc_analyzer import SMCAnalyzer as _SA
        _atr = _SA.calculate_atr(candles) or (price_range * 0.05)
        _swings = _SA._find_last_swing(candles, lookback=3, search_back=20)
        sweep = ev.get("liquidity_sweep") or {}

        if bullish:
            sl_anchor = sweep.get("wick_low") or _swings.get("swing_low") or min(b["low"] for b in c[-10:])
            sl = sl_anchor - _atr * 0.3
        else:
            sl_anchor = sweep.get("wick_high") or _swings.get("swing_high") or max(b["high"] for b in c[-10:])
            sl = sl_anchor + _atr * 0.3

        # Entry from trigger candle; fall back to BOS close
        entry = c[-1]["close"]
        trigger_x_chart = len(c) - 1
        if trigger_ts and c_ahead:
            for _i, _b in enumerate(c_ahead):
                if _b["timestamp"] == trigger_ts:
                    entry = _b["close"]
                    trigger_x_chart = len(c) + _i
                    break

        risk = abs(entry - sl) or _atr
        tp = (entry + risk * 2) if bullish else (entry - risk * 2)

        # Extend ylim to include SL and TP
        new_lo = min(ax.get_ylim()[0], sl - _atr * 0.5)
        new_hi = max(ax.get_ylim()[1], tp + _atr * 0.5)
        ax.set_ylim(new_lo, new_hi)

        # Swing level (structural SL zone)
        ax.hlines(sl_anchor, trigger_x_chart, len(all_bars) - 1,
                  colors="mediumpurple", linestyles=":", linewidth=1.0, zorder=3)

        # SL and TP lines from trigger point onward
        ax.hlines(sl, trigger_x_chart, len(all_bars) - 1,
                  colors="red", linestyles="--", linewidth=1.2, zorder=3)
        ax.hlines(tp, trigger_x_chart, len(all_bars) - 1,
                  colors="limegreen", linestyles="--", linewidth=1.2, zorder=3)
        ax.text(len(all_bars) - 0.3, sl, " SL", fontsize=7, color="red",
                va="center", fontweight="bold")
        ax.text(len(all_bars) - 0.3, tp, " TP", fontsize=7, color="limegreen",
                va="center", fontweight="bold")

        # Check outcome: did price hit TP or SL first in the 15m lookahead?
        outcome = "OPEN"
        if c_ahead:
            past_trigger = False
            for _b in c_ahead:
                if not past_trigger:
                    if trigger_ts and _b["timestamp"] == trigger_ts:
                        past_trigger = True
                    continue
                if bullish:
                    if _b["low"] <= sl:
                        outcome = "LOSS"; break
                    if _b["high"] >= tp:
                        outcome = "WIN"; break
                else:
                    if _b["high"] >= sl:
                        outcome = "LOSS"; break
                    if _b["low"] <= tp:
                        outcome = "WIN"; break

        # If still OPEN, check 4h candles beyond the BOS to resolve HTF outcome
        if outcome == "OPEN" and candles_4h:
            bos_ts = ev.get("breakout_ts", 0)
            future_4h = [_b for _b in reversed(candles_4h) if _b["timestamp"] > bos_ts]
            for _b in future_4h:
                if bullish:
                    if _b["low"] <= sl:
                        outcome = "HTF LOSS"; break
                    if _b["high"] >= tp:
                        outcome = "HTF WIN"; break
                else:
                    if _b["high"] >= sl:
                        outcome = "HTF LOSS"; break
                    if _b["low"] <= tp:
                        outcome = "HTF WIN"; break

        if outcome in ("WIN", "HTF WIN"):
            outcome_color = "#26a69a"
        elif outcome in ("LOSS", "HTF LOSS"):
            outcome_color = "#ef5350"
        else:
            outcome_color = "gray"
        ax.text(
            0.98, 0.03, outcome,
            transform=ax.transAxes, fontsize=11, fontweight="bold",
            color=outcome_color, ha="right", va="bottom",
            bbox=dict(boxstyle="round,pad=0.4", facecolor="white", alpha=0.9, edgecolor=outcome_color),
        )

        # legend patches
        legend_items = [
            mpatches.Patch(color="#26a69a", label="bullish candle"),
            mpatches.Patch(color="#ef5350", label="bearish candle"),
        ]
        if c_ahead:
            legend_items.append(mpatches.Patch(color="gray", alpha=0.45, label="next 50 candles"))
        ax.legend(handles=legend_items, fontsize=7, loc="upper left")

        # HTF bias label — top-right corner
        if htf_bias and htf_bias != "neutral":
            tf = ev.get("timeframe", "15m").upper()
            bias_color = "#26a69a" if htf_bias == "bullish" else "#ef5350"
            bias_text = f"{tf} {direction.upper()}  |  4H {htf_bias.upper()}"
            ax.text(
                0.98, 0.97, bias_text,
                transform=ax.transAxes, fontsize=9, fontweight="bold",
                color=bias_color, ha="right", va="top",
                bbox=dict(boxstyle="round,pad=0.3", facecolor="white", alpha=0.8, edgecolor=bias_color),
            )

        # confluence label — bottom left
        if confluences:
            ax.text(
                0.02, 0.03, f"✓ {confluence_labels(confluences)}",
                transform=ax.transAxes, fontsize=8, fontweight="bold",
                color="#26a69a", ha="left", va="bottom",
                bbox=dict(boxstyle="round,pad=0.3", facecolor="white", alpha=0.85, edgecolor="#26a69a"),
            )

        mode_tag = " [DEV]" if self.dev_mode and c_ahead else ""
        ax.set_title(
            f"{ev.get('symbol')} {ev.get('timeframe')} BOS {direction.upper()}{mode_tag}  |  {alert_id}",
            fontsize=9,
        )
        ax.tick_params(labelsize=7)

        chart_path = self.charts_dir / f"{alert_id}.png"
        fig.tight_layout()
        fig.savefig(str(chart_path), dpi=100)
        plt.close(fig)
        return str(chart_path), outcome

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
