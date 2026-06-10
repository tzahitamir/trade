import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .telegram_notifier import TelegramNotifier
from analysis.smc_analyzer import SMCAnalyzer
from analysis.confluence_detector import confluence_labels, confluence_description_text
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
        self.prod_charts_dir = self.charts_dir / "prod"
        self.prod_charts_dir.mkdir(parents=True, exist_ok=True)
        self.dev_mode: bool = getattr(settings, "dev_mode", True)

    @staticmethod
    def _generate_alert_id(symbol: str, timestamp: int, prefix: str = "bos") -> str:
        """Generate alert ID: {prefix}-mm-hh-day-month-year-fxpairname  e.g. bos15m-30-09-05-06-2026-eurusd"""
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        pair = symbol.lower().replace("/", "")
        return f"{prefix}-{dt.minute:02d}-{dt.hour:02d}-{dt.day:02d}-{dt.month:02d}-{dt.year}-{pair}"

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

    def evaluate_production(
        self,
        symbol: str,
        timeframe: str,
        candles: List[Dict],
        candles_4h: Optional[List[Dict]] = None,
        htf_bias: Optional[str] = None,
        gold_params: Optional[Dict] = None,
        min_breakout_ts: Optional[int] = None,
        id_prefix: str = "bos",
    ) -> List[Dict]:
        """Like evaluate() but applies gold param filters and computes TP/SL/R for each signal."""
        from analysis.confluence_detector import detect_confluences, find_trigger_candle

        gp = gold_params or {}
        min_str          = gp.get("min_break_strength", 0.7)
        req_brt          = gp.get("require_brt_confluence", False)
        htf_only         = gp.get("htf_aligned_only", False)
        sl_mode          = gp.get("sl_mode", "swing")
        req_liq          = gp.get("require_liquidity_sweep", False)
        excl_london_open = gp.get("exclude_london_open", False)
        excl_ny_open     = gp.get("exclude_ny_open", False)

        alerts: List[Dict] = []
        detect_params = {"symbol": symbol, "timeframe": timeframe, "min_break_strength": 0.0,
                         "require_liquidity_sweep": False}
        bos_events = self.analyzer.detect_bos(candles, params=detect_params)

        atr = self.analyzer.calculate_atr(candles) or 0.0
        pre_bos_chron = list(reversed(candles))

        seen: set = set()
        for ev in bos_events:
            # Skip events older than the live tick window (avoids chart-render spam)
            if min_breakout_ts and ev.get("breakout_ts", 0) < min_breakout_ts:
                continue

            key = (ev["direction"], round(ev.get("broken_level", 0), 5))
            if key in seen:
                continue

            # Gold param filters
            if ev.get("break_strength", 0.0) < min_str:
                continue
            if req_liq and not ev.get("liquidity_sweep"):
                continue
            if htf_only and htf_bias not in ("bullish" if ev["direction"] == "bullish" else ("bearish",)):
                if htf_bias != ("bullish" if ev["direction"] == "bullish" else "bearish"):
                    continue
            if excl_london_open or excl_ny_open:
                _ev_dt = datetime.fromtimestamp(ev.get("breakout_ts", 0), tz=timezone.utc)
                if excl_london_open and _ev_dt.hour == 8 and _ev_dt.minute < 30:
                    continue
                if excl_ny_open and _ev_dt.hour == 13 and _ev_dt.minute >= 30:
                    continue

            confluences = detect_confluences(ev, [], pre_bos_chron, candles_4h, atr)
            if req_brt and "BRT" not in confluences:
                continue

            seen.add(key)

            # Compute SL and TP using the gold sl_mode
            window_chron = list(reversed(candles))
            sl, tp, entry, swing_risk = self._compute_trade_levels(ev, window_chron, candles, sl_mode, atr)
            r_ratio = round(abs(tp - entry) / abs(entry - sl), 2) if abs(entry - sl) > 0 else None

            alert_id = self._generate_alert_id(symbol, ev.get("breakout_ts", 0), prefix=id_prefix)
            try:
                fig_path, _ = self._render_bos_chart(
                    candles, ev, alert_id, None, htf_bias,
                    confluences=confluences, output_dir=self.prod_charts_dir,
                    entry_price=entry, sl_price=sl, tp_price=tp,
                )
            except Exception:
                fig_path = None

            alert = {
                "alert_id": alert_id,
                "symbol": symbol,
                "timeframe": timeframe,
                "event": ev,
                "htf_bias": htf_bias,
                "confluences": confluences,
                "sl_mode": sl_mode,
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "r_ratio": r_ratio,
                "image_path": fig_path,
            }
            try:
                alert["db_id"] = self.db.insert_alert(
                    symbol, timeframe, ev.get("breakout_ts", 0), "BOS",
                    self._format_production_message(alert), fig_path,
                    json.dumps(gp), alert_id=alert_id,
                )
            except Exception:
                alert["db_id"] = None
            alerts.append(alert)

        return alerts

    def _compute_trade_levels(self, ev: Dict, candles_chron: List[Dict], candles_desc: List[Dict],
                               sl_mode: str, atr: float):
        """Return (sl, tp, entry, swing_risk) for a BOS event based on sl_mode."""
        bullish    = ev["direction"] == "bullish"
        entry      = ev.get("broken_level", 0.0)
        buf        = 0.3 * atr

        # Swing SL (always computed — used for TP anchor)
        swing_low  = min(c["low"]  for c in candles_chron[-30:]) if bullish else None
        swing_high = max(c["high"] for c in candles_chron[-30:]) if not bullish else None
        sl_swing   = (swing_low - buf) if bullish else (swing_high + buf)
        swing_risk = abs(entry - sl_swing) or atr

        if sl_mode == "swing":
            sl = sl_swing
        elif sl_mode == "broken_level":
            level = ev.get("broken_level", entry)
            sl = (level - buf) if bullish else (level + buf)
        else:  # break_candle
            bk_ts = ev.get("breakout_ts")
            bk_candle = next((c for c in candles_chron if c["timestamp"] == bk_ts), None)
            if bk_candle:
                sl = (bk_candle["low"] - buf) if bullish else (bk_candle["high"] + buf)
            else:
                sl = sl_swing

        tp = entry + swing_risk * 2 if bullish else entry - swing_risk * 2
        return sl, tp, entry, swing_risk

    @staticmethod
    def _fmt_symbol(symbol: str) -> str:
        """EURUSD → EUR/USD, XAUUSD → XAU/USD"""
        return f"{symbol[:3]}/{symbol[3:]}" if len(symbol) == 6 else symbol

    def _format_production_message(self, alert: Dict) -> str:
        ev        = alert["event"]
        direction = ev.get("direction", "bullish")
        bullish   = direction == "bullish"
        symbol    = alert["symbol"]
        timeframe = alert.get("timeframe", "15m")
        htf       = alert.get("htf_bias", "")
        confs     = alert.get("confluences") or []
        sl_mode   = alert.get("sl_mode", "swing")
        entry     = alert.get("entry")
        sl        = alert.get("sl")
        tp        = alert.get("tp")
        r         = alert.get("r_ratio")

        action    = "BUY" if bullish else "SELL"
        emoji     = "🟢" if bullish else "🔴"
        sym_fmt   = self._fmt_symbol(symbol)

        bias_str  = f" | 4H: {htf.upper()}" if htf and htf != "neutral" else ""
        conf_str  = f" | {','.join(confs)}" if confs else ""
        sl_str    = f" | SL: {sl:.5f}" if sl else ""
        tp_str    = f" | TP: {tp:.5f}" if tp else ""
        r_str     = f" | R: 1:{r}" if r else ""
        wr_str    = ""
        gold = self.db.get_gold_params("BOS15m", symbol).get(symbol)
        if gold:
            wr_str = f" | WR: {gold['win_rate']*100:.0f}%"
        ts_str = datetime.fromtimestamp(ev.get("breakout_ts", 0), tz=timezone.utc).strftime("%H:%M %d-%b UTC")

        # Staleness indicator: show how far current price is from entry
        stale_str = ""
        current_price = alert.get("current_price")
        if current_price is not None and entry:
            dist = current_price - entry
            dist_str = f"{dist:+.2f}" if symbol == "XAUUSD" else f"{dist:+.5f}"
            if abs(dist) > abs(entry - sl) * 0.5:
                stale_str = f"\n⚠️ Price now {current_price:.5f} ({dist_str} from entry)"
            else:
                stale_str = f"\nCurrent: {current_price:.5f} ({dist_str})"

        return (
            f"{emoji} {action} alert on {sym_fmt} {timeframe}\n"
            f"@ {entry:.5f}{sl_str}{tp_str}{r_str}{bias_str}{conf_str}{wr_str} [{ts_str}]{stale_str}"
        )

    def format_production_alert(self, alert: Dict) -> str:
        return self._format_production_message(alert)

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
        sl_mode: str = "swing",
    ) -> tuple:
        """Return (outcome, eff_r) for a BOS signal.

        sl_mode controls where the stop is placed:
          swing        — current default: prior swing low/high ± 0.3 ATR
          broken_level — SL just beyond the broken swing level ± 0.3 ATR
          break_candle — SL at the low/high of the BOS break candle itself

        TP is always entry + 2 × swing_risk (unchanged across modes).
        eff_r = (swing_risk × 2) / tight_risk for WIN, 1.0 for LOSS, None for OPEN.
        """
        from analysis.smc_analyzer import SMCAnalyzer as _SA
        PRE_BOS = 50
        c = list(reversed(candles))[-(PRE_BOS + 1):]
        direction = ev.get("direction", "bullish")
        bullish = direction == "bullish"
        _atr = _SA.calculate_atr(candles) or 1e-6
        _swings = _SA._find_last_swing(candles, lookback=3, search_back=20)
        sweep = ev.get("liquidity_sweep") or {}

        # Swing SL — always computed to anchor TP price
        if bullish:
            sl_anchor = sweep.get("wick_low") or _swings.get("swing_low") or min(b["low"] for b in c[-10:])
            sl_swing = sl_anchor - _atr * 0.3
        else:
            sl_anchor = sweep.get("wick_high") or _swings.get("swing_high") or max(b["high"] for b in c[-10:])
            sl_swing = sl_anchor + _atr * 0.3

        entry = c[-1]["close"] if c else ev.get("broken_level", 0.0)
        if trigger_ts and lookahead_chron:
            for _b in lookahead_chron:
                if _b["timestamp"] == trigger_ts:
                    entry = _b["close"]
                    break

        swing_risk = abs(entry - sl_swing) or _atr
        tp = (entry + swing_risk * 2) if bullish else (entry - swing_risk * 2)

        # Mode-specific SL (determines when to stop out)
        if sl_mode == "broken_level":
            broken = ev.get("broken_level", entry)
            sl = (broken - _atr * 0.3) if bullish else (broken + _atr * 0.3)
        elif sl_mode == "break_candle":
            sl = c[-1]["low"] if bullish else c[-1]["high"]
        else:
            sl = sl_swing

        tight_risk = abs(entry - sl) or swing_risk

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

        if "WIN" in outcome:
            eff_r = round((swing_risk * 2) / tight_risk, 3)
        elif "LOSS" in outcome:
            eff_r = 1.0
        else:
            eff_r = None

        return outcome, eff_r

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
        strategy: str = "bos",
        gold_wr: Optional[float] = None,
    ) -> Dict:
        """Generate chart and DB record for a confirmed BOS event. Returns alert dict."""
        alert_id = self._generate_alert_id(symbol, ev.get("breakout_ts", 0), prefix=strategy)
        outcome = "OPEN"
        try:
            fig_path, outcome = self._render_bos_chart(
                candles, ev, alert_id, lookahead_candles, htf_bias, confluences,
                trigger_ts, candles_4h, gold_wr=gold_wr,
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
        gold_wr: Optional[float] = None,
        output_dir=None,
        outcome_override: Optional[str] = None,
        entry_price: Optional[float] = None,
        sl_price: Optional[float] = None,
        tp_price: Optional[float] = None,
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

        fig, ax = plt.subplots(figsize=(16, 7))
        fig.patch.set_facecolor("white")
        ax.set_facecolor("#fafafa")

        self._draw_candles(ax, c, x_offset=0, alpha=1.0)
        if c_ahead:
            self._draw_candles(ax, c_ahead, x_offset=len(c), alpha=0.45)

        # Extra x-space on the right so level labels never get clipped
        all_bars = c + c_ahead
        LABEL_PAD = 12
        ax.set_xlim(-1, len(all_bars) + LABEL_PAD)

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
                trigger_dt = datetime.fromtimestamp(trigger_ts, tz=timezone.utc)
                trigger_label = f"{action}\n{trigger_dt.strftime('%d/%m %H:%M')}"
                ax.annotate(
                    trigger_label,
                    xy=(alert_x, alert_tip_y),
                    xytext=(alert_x, alert_text_y),
                    fontsize=7, fontweight="bold", color="green", ha="center", zorder=5,
                    arrowprops={**arrow_props, "color": "green"},
                )

        # SL / TP / Entry — use caller-supplied values when available (keeps chart
        # consistent with the Telegram message which uses gold sl_mode).
        from analysis.smc_analyzer import SMCAnalyzer as _SA
        _atr = _SA.calculate_atr(candles) or (price_range * 0.05)
        _swings = _SA._find_last_swing(candles, lookback=3, search_back=20)
        sweep = ev.get("liquidity_sweep") or {}

        trigger_x_chart = len(c) - 1
        if entry_price is not None and sl_price is not None and tp_price is not None:
            entry, sl, tp = entry_price, sl_price, tp_price
            sl_anchor = sl  # for the subtle purple dotted line
        else:
            if bullish:
                sl_anchor = sweep.get("wick_low") or _swings.get("swing_low") or min(b["low"] for b in c[-10:])
                sl = sl_anchor - _atr * 0.3
            else:
                sl_anchor = sweep.get("wick_high") or _swings.get("swing_high") or max(b["high"] for b in c[-10:])
                sl = sl_anchor + _atr * 0.3
            # Entry from trigger candle; fall back to BOS close
            entry = c[-1]["close"]
            if trigger_ts and c_ahead:
                for _i, _b in enumerate(c_ahead):
                    if _b["timestamp"] == trigger_ts:
                        entry = _b["close"]
                        trigger_x_chart = len(c) + _i
                        break
            risk = abs(entry - sl) or _atr
            tp = (entry + risk * 2) if bullish else (entry - risk * 2)

        # Extend ylim to include SL and TP with comfortable padding
        new_lo = min(ax.get_ylim()[0], sl - _atr * 0.8)
        new_hi = max(ax.get_ylim()[1], tp + _atr * 0.8)
        ax.set_ylim(new_lo, new_hi)

        line_end = len(all_bars) + LABEL_PAD - 1   # lines extend into label area
        label_x  = len(all_bars) + 0.8             # text starts just after last bar

        def _level_label(y, text, price, color, ls, lw=1.5):
            ax.hlines(y, trigger_x_chart, line_end,
                      colors=color, linestyles=ls, linewidth=lw, zorder=3)
            ax.text(label_x, y, f" {text}\n {price:.5f}",
                    fontsize=9, color=color, va="center", fontweight="bold",
                    clip_on=False,
                    bbox=dict(boxstyle="round,pad=0.35", facecolor="white",
                              alpha=1.0, edgecolor=color, linewidth=1.4))

        # Swing level (structural SL zone) — subtle dotted purple
        ax.hlines(sl_anchor, trigger_x_chart, line_end,
                  colors="mediumpurple", linestyles=":", linewidth=1.0, zorder=3)

        # Entry line — solid blue
        _level_label(entry, "ENTRY", entry, "dodgerblue", "-")
        # SL line — dashed red
        _level_label(sl, "SL", sl, "#d32f2f", "--")
        # TP line — dashed green
        _level_label(tp, "TP", tp, "#1b8a4e", "--")

        # Check outcome: did price hit TP or SL first in the 15m lookahead?
        # outcome_override lets callers (e.g. retroactive scans) inject the known result.
        outcome = outcome_override if outcome_override else "OPEN"
        if not outcome_override and c_ahead:
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
        if not outcome_override and outcome == "OPEN" and candles_4h:
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
            outcome_color = "#1b8a4e"
        elif outcome in ("LOSS", "HTF LOSS"):
            outcome_color = "#d32f2f"
        else:
            outcome_color = "#555555"

        # Outcome badge — bottom-right, large and clear
        ax.text(
            0.99, 0.04, outcome,
            transform=ax.transAxes, fontsize=14, fontweight="bold",
            color="white", ha="right", va="bottom",
            bbox=dict(boxstyle="round,pad=0.5", facecolor=outcome_color,
                      alpha=1.0, edgecolor=outcome_color),
        )

        # Legend — lower-left (away from info panel which occupies top-left)
        legend_items = [
            mpatches.Patch(color="#26a69a", label="Bullish"),
            mpatches.Patch(color="#ef5350", label="Bearish"),
        ]
        if c_ahead:
            legend_items.append(mpatches.Patch(color="gray", alpha=0.45, label="Future"))
        ax.legend(handles=legend_items, fontsize=8, loc="lower left",
                  framealpha=1.0, edgecolor="#aaaaaa")

        # Confluence labels — bottom-left, only if present
        if confluences:
            ax.text(
                0.01, 0.04, confluence_description_text(confluences),
                transform=ax.transAxes, fontsize=8,
                color="#1b6e4e", ha="left", va="bottom",
                bbox=dict(boxstyle="round,pad=0.4", facecolor="white",
                          alpha=1.0, edgecolor="#1b8a4e", linewidth=1.2),
            )

        # Alert time stamp on the BOS candle — vertical line only (time is in info panel)
        bos_ts = ev.get("breakout_ts", 0)
        bos_dt_str = (datetime.fromtimestamp(bos_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                      if bos_ts else "?")
        ax.axvline(x=bos_x, color="darkorange", linestyle=":", linewidth=1.2, alpha=0.7, zorder=2)

        # Trade info panel — top-left, multi-line; includes HTF bias (removes top-right badge)
        r_ratio = round(abs(tp - entry) / risk, 1) if risk else 2.0
        bias_str = htf_bias.upper() if htf_bias and htf_bias != "neutral" else "—"
        action = "BUY" if bullish else "SELL"
        info_lines = [
            f"{'Alert:':<7} {bos_dt_str}",
            f"{'Dir:':<7} {direction.upper()} ({action})  │  4H: {bias_str}",
            f"{'Entry:':<7} {entry:.5f}",
            f"{'SL:':<7} {sl:.5f}",
            f"{'TP:':<7} {tp:.5f}",
            f"{'R:':<7} 1:{r_ratio}",
        ]
        if gold_wr is not None:
            info_lines.append(f"{'WR:':<7} {gold_wr*100:.1f}%")
        ax.text(
            0.01, 0.97, "\n".join(info_lines),
            transform=ax.transAxes, fontsize=9.5,
            color="#111111", ha="left", va="top",
            family="monospace",
            bbox=dict(boxstyle="round,pad=0.5", facecolor="white",
                      alpha=1.0, edgecolor="#555555", linewidth=1.5),
            zorder=6,
        )

        # Title — symbol, timeframe, direction, alert ID
        mode_tag = " [DEV]" if self.dev_mode and c_ahead else ""
        sym  = ev.get("symbol", "?")
        tf   = ev.get("timeframe", "15m")
        ax.set_title(
            f"{sym} {tf} │ BOS {direction.upper()} ({action}){mode_tag} │ {bos_dt_str}",
            fontsize=11, fontweight="bold", pad=8,
        )
        ax.tick_params(labelsize=8)
        ax.grid(axis="y", color="#dddddd", linewidth=0.5, linestyle="-", zorder=0)

        outcome_tag = outcome.lower().replace(" ", "_")
        dest_dir = output_dir if output_dir is not None else self.charts_dir
        chart_path = dest_dir / f"{alert_id}-{outcome_tag}.png"
        fig.subplots_adjust(left=0.05, right=0.88, top=0.91, bottom=0.07)
        fig.savefig(str(chart_path), dpi=150, bbox_inches="tight")
        plt.close(fig)
        return str(chart_path), outcome

    @staticmethod
    def evaluate_fvg_outcome(
        candles: List[Dict],
        ev: Dict,
        lookahead_chron: List[Dict],
        candles_htf: Optional[List[Dict]] = None,
    ) -> str:
        """Compute WIN/LOSS/HTF WIN/HTF LOSS/OPEN for a FVG doji signal."""
        from analysis.smc_analyzer import SMCAnalyzer as _SA
        direction = ev.get("direction", "bullish")
        bullish   = direction == "bullish"
        atr       = _SA.calculate_atr(candles) or 1e-6
        fvg_lo    = ev["fvg_low"]
        fvg_hi    = ev["fvg_high"]
        entry     = list(reversed(candles))[0]["close"] if candles else ev.get("breakout_ts", 0)
        sl        = (fvg_lo - atr * 0.3) if bullish else (fvg_hi + atr * 0.3)
        risk      = abs(entry - sl) or atr
        tp        = (entry + risk * 2) if bullish else (entry - risk * 2)
        outcome   = "OPEN"
        for bar in lookahead_chron:
            if bullish:
                if bar["low"]  <= sl: outcome = "LOSS"; break
                if bar["high"] >= tp: outcome = "WIN";  break
            else:
                if bar["high"] >= sl: outcome = "LOSS"; break
                if bar["low"]  <= tp: outcome = "WIN";  break
        if outcome == "OPEN" and candles_htf:
            sig_ts   = ev.get("breakout_ts", 0)
            future   = [b for b in reversed(candles_htf) if b["timestamp"] > sig_ts]
            for bar in future:
                if bullish:
                    if bar["low"]  <= sl: outcome = "HTF LOSS"; break
                    if bar["high"] >= tp: outcome = "HTF WIN";  break
                else:
                    if bar["high"] >= sl: outcome = "HTF LOSS"; break
                    if bar["low"]  <= tp: outcome = "HTF WIN";  break
        return outcome

    @staticmethod
    def evaluate_liq_outcome(
        candles: List[Dict],
        ev: Dict,
        lookahead_chron: List[Dict],
        candles_htf: Optional[List[Dict]] = None,
        sl_buffer_atr: float = 0.1,
        rr: float = 2.0,
    ) -> str:
        """Compute WIN/LOSS/HTF WIN/HTF LOSS/OPEN for a liquidity sweep signal."""
        from analysis.smc_analyzer import SMCAnalyzer as _SA
        direction = ev.get("direction", "bullish")
        bullish   = direction == "bullish"
        atr       = _SA.calculate_atr(candles) or 1e-6
        entry     = candles[0]["close"]
        sl        = (candles[0]["low"]  - sl_buffer_atr * atr) if bullish \
                    else (candles[0]["high"] + sl_buffer_atr * atr)
        risk      = abs(entry - sl) or atr * 0.5
        tp        = (entry + rr * risk) if bullish else (entry - rr * risk)
        outcome   = "OPEN"
        for bar in lookahead_chron:
            if bullish:
                if bar["low"]  <= sl: outcome = "LOSS"; break
                if bar["high"] >= tp: outcome = "WIN";  break
            else:
                if bar["high"] >= sl: outcome = "LOSS"; break
                if bar["low"]  <= tp: outcome = "WIN";  break
        if outcome == "OPEN" and candles_htf:
            sig_ts = ev.get("breakout_ts", 0)
            future = [b for b in reversed(candles_htf) if b["timestamp"] > sig_ts]
            for bar in future:
                if bullish:
                    if bar["low"]  <= sl: outcome = "HTF LOSS"; break
                    if bar["high"] >= tp: outcome = "HTF WIN";  break
                else:
                    if bar["high"] >= sl: outcome = "HTF LOSS"; break
                    if bar["low"]  <= tp: outcome = "HTF WIN";  break
        return outcome

    def render_fvg_alert(
        self,
        symbol: str,
        timeframe: str,
        ev: Dict,
        candles: List[Dict],
        lookahead_candles: Optional[List[Dict]] = None,
        htf_bias: Optional[str] = None,
        candles_htf: Optional[List[Dict]] = None,
        param_set_id: Optional[int] = None,
        skip_db: bool = False,
    ) -> Dict:
        """Generate FVG doji chart + DB record. Returns alert dict."""
        alert_id = "fvg_" + self._generate_alert_id(symbol, ev.get("breakout_ts", 0))
        outcome  = "OPEN"
        fig_path = None
        try:
            fig_path, outcome = self._render_fvg_chart(
                candles, ev, alert_id, lookahead_candles, htf_bias, candles_htf
            )
        except Exception:
            import logging, traceback
            logging.error("FVG chart render failed for %s: %s", alert_id, traceback.format_exc())
        direction = ev.get("direction", "bullish")
        message = (
            f"FVG DOJI {direction.upper()} @ fvg=[{ev['fvg_low']:.5f}-{ev['fvg_high']:.5f}]"
            f"  size={ev.get('fvg_size_atr', 0):.2f}ATR"
            f"  retrace={ev.get('retrace_depth', 0):.0%}"
            f"  body={ev.get('doji_body_pct', 0):.0%}"
        )
        alert = {
            "alert_id":    alert_id,
            "symbol":      symbol,
            "timeframe":   timeframe,
            "event":       ev,
            "message":     message,
            "image_path":  fig_path,
            "htf_bias":    htf_bias,
            "confluences": [],
            "outcome":     outcome,
            "param_set_id": param_set_id,
        }
        if not skip_db:
            try:
                db_id = self.db.insert_alert(
                    symbol, timeframe, ev.get("breakout_ts", 0), "FVG",
                    message, fig_path, "{}", alert_id=alert_id, param_set_id=param_set_id,
                )
                alert["db_id"] = db_id
            except Exception:
                alert["db_id"] = None
        return alert

    def _render_fvg_chart(
        self,
        candles: List[Dict],
        ev: Dict,
        alert_id: str,
        lookahead_candles: Optional[List[Dict]] = None,
        htf_bias: Optional[str] = None,
        candles_htf: Optional[List[Dict]] = None,
    ) -> tuple:
        """Render FVG doji candlestick chart, save to data/charts/{alert_id}.png."""
        from analysis.smc_analyzer import SMCAnalyzer as _SA
        PRE  = 50
        c    = list(reversed(candles))[-(PRE + 1):]
        c_ahead: List[Dict] = []
        if lookahead_candles and self.dev_mode:
            c_ahead = list(reversed(lookahead_candles))

        fig, ax = plt.subplots(figsize=(10, 4))
        self._draw_candles(ax, c, x_offset=0, alpha=1.0)
        if c_ahead:
            self._draw_candles(ax, c_ahead, x_offset=len(c), alpha=0.45)

        all_bars = c + c_ahead
        ax.set_xlim(-1, len(all_bars))
        all_lows  = [b["low"]  for b in all_bars]
        all_highs = [b["high"] for b in all_bars]
        pad = (max(all_highs) - min(all_lows)) * 0.08
        ax.set_ylim(min(all_lows) - pad, max(all_highs) + pad)

        # FVG zone: shaded rectangle
        fvg_lo = ev["fvg_low"]
        fvg_hi = ev["fvg_high"]
        ax.axhspan(fvg_lo, fvg_hi, color="gold", alpha=0.25, zorder=1, label="FVG zone")

        # FVG impulse candle marker
        fvg_ts   = ev.get("fvg_ts")
        fvg_x    = next((i for i, b in enumerate(c) if b["timestamp"] == fvg_ts), None)
        direction = ev.get("direction", "bullish")
        bullish   = direction == "bullish"
        price_range = max(all_highs) - min(all_lows) or all_highs[0] * 0.001
        arrow_offset = price_range * 0.15
        arrow_props  = dict(arrowstyle="-|>", lw=1.3)

        if fvg_x is not None:
            tip_y  = c[fvg_x]["high"] if bullish else c[fvg_x]["low"]
            text_y = tip_y + arrow_offset if bullish else tip_y - arrow_offset
            ax.annotate(
                "FVG", xy=(fvg_x, tip_y), xytext=(fvg_x, text_y),
                fontsize=8, fontweight="bold", color="goldenrod", ha="center", zorder=5,
                arrowprops={**arrow_props, "color": "goldenrod"},
            )

        # Doji (entry signal) arrow
        doji_x  = len(c) - 1
        tip_y   = c[-1]["low"] if bullish else c[-1]["high"]
        text_y  = tip_y - arrow_offset if bullish else tip_y + arrow_offset
        action  = "BUY" if bullish else "SELL"
        ax.annotate(
            action, xy=(doji_x, tip_y), xytext=(doji_x, text_y),
            fontsize=8, fontweight="bold", color="green", ha="center", zorder=5,
            arrowprops={**arrow_props, "color": "green"},
        )

        # SL / TP
        atr   = _SA.calculate_atr(candles) or (price_range * 0.05)
        entry = c[-1]["close"]
        sl    = (fvg_lo - atr * 0.3) if bullish else (fvg_hi + atr * 0.3)
        risk  = abs(entry - sl) or atr
        tp    = (entry + risk * 2) if bullish else (entry - risk * 2)

        new_lo = min(ax.get_ylim()[0], sl - atr * 0.5)
        new_hi = max(ax.get_ylim()[1], tp + atr * 0.5)
        ax.set_ylim(new_lo, new_hi)

        ax.hlines(sl, doji_x, len(all_bars) - 1, colors="red",      linestyles="--", linewidth=1.2, zorder=3)
        ax.hlines(tp, doji_x, len(all_bars) - 1, colors="limegreen", linestyles="--", linewidth=1.2, zorder=3)
        ax.text(len(all_bars) - 0.3, sl, " SL", fontsize=7, color="red",      va="center", fontweight="bold")
        ax.text(len(all_bars) - 0.3, tp, " TP", fontsize=7, color="limegreen", va="center", fontweight="bold")

        # Outcome check
        outcome = self.evaluate_fvg_outcome(candles, ev, c_ahead, candles_htf)
        outcome_color = "#26a69a" if "WIN" in outcome else ("#ef5350" if "LOSS" in outcome else "gray")
        ax.text(
            0.98, 0.03, outcome, transform=ax.transAxes, fontsize=11, fontweight="bold",
            color=outcome_color, ha="right", va="bottom",
            bbox=dict(boxstyle="round,pad=0.4", facecolor="white", alpha=0.9, edgecolor=outcome_color),
        )

        # HTF bias label
        if htf_bias and htf_bias != "neutral":
            bias_color = "#26a69a" if htf_bias == "bullish" else "#ef5350"
            tf_label   = ev.get("timeframe", "30m").upper()
            ax.text(
                0.98, 0.97, f"{tf_label} {direction.upper()}  |  4H {htf_bias.upper()}",
                transform=ax.transAxes, fontsize=9, fontweight="bold", color=bias_color,
                ha="right", va="top",
                bbox=dict(boxstyle="round,pad=0.3", facecolor="white", alpha=0.8, edgecolor=bias_color),
            )

        # Stats label: FVG size / retrace / doji body
        stats_txt = (
            f"FVG {ev.get('fvg_size_atr', 0):.2f}ATR  "
            f"retrace {ev.get('retrace_depth', 0):.0%}  "
            f"body {ev.get('doji_body_pct', 0):.0%}"
        )
        ax.text(
            0.02, 0.03, stats_txt, transform=ax.transAxes, fontsize=8,
            color="goldenrod", ha="left", va="bottom",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="white", alpha=0.85, edgecolor="goldenrod"),
        )

        legend_items = [
            mpatches.Patch(color="#26a69a", label="bullish candle"),
            mpatches.Patch(color="#ef5350", label="bearish candle"),
            mpatches.Patch(color="gold",    alpha=0.4, label="FVG zone"),
        ]
        if c_ahead:
            legend_items.append(mpatches.Patch(color="gray", alpha=0.45, label="next 50 candles"))
        ax.legend(handles=legend_items, fontsize=7, loc="upper left")

        mode_tag = " [DEV]" if self.dev_mode and c_ahead else ""
        ax.set_title(
            f"{ev.get('symbol')} {ev.get('timeframe')} FVG DOJI {direction.upper()}{mode_tag}  |  {alert_id}",
            fontsize=9,
        )
        ax.tick_params(labelsize=7)
        outcome_tag = outcome.lower().replace(" ", "_")
        chart_path = self.charts_dir / f"{alert_id}-{outcome_tag}.png"
        fig.tight_layout()
        fig.savefig(str(chart_path), dpi=100)
        plt.close(fig)
        return str(chart_path), outcome

    def render_dax_alert(
        self,
        sig: Dict,
        candles_5m: List[Dict],
        outcome: str = "OPEN",
    ) -> str:
        """Render a DAX 5m counter-trend chart with premium/discount zone shading. Returns chart path."""
        try:
            from zoneinfo import ZoneInfo as _ZI
            _FFM_TZ = _ZI("Europe/Berlin")
        except Exception:
            from datetime import timedelta as _td
            _FFM_TZ = timezone(_td(hours=2))

        entry_ts    = sig["breakout_ts"]
        peak_ts     = sig.get("peak_ts", entry_ts)
        bos_15m_ts  = sig.get("bos_15m_ts", peak_ts)
        origin      = sig["origin"]
        peak        = sig["peak"]
        eq_level    = sig["eq_level"]
        entry       = sig["entry"]
        sl          = sig["sl"]
        tp          = sig["tp"]
        direction   = sig["direction"]
        exp_dir     = sig.get("expansion_dir", "")
        bullish_exp = exp_dir == "bullish"

        entry_idx = next(
            (i for i, c in enumerate(candles_5m) if c["timestamp"] >= entry_ts),
            len(candles_5m) - 1,
        )
        display_5m    = candles_5m[: entry_idx + 1]
        lookahead_bars = candles_5m[entry_idx + 1: entry_idx + 41]
        all_bars      = display_5m + lookahead_bars
        if not display_5m:
            return ""

        fig, ax = plt.subplots(figsize=(14, 5), constrained_layout=True)
        self._draw_candles(ax, display_5m, x_offset=0, alpha=1.0)
        if lookahead_bars:
            self._draw_candles(ax, lookahead_bars, x_offset=len(display_5m), alpha=0.40)

        x_total = len(all_bars)
        entry_x = len(display_5m) - 1
        lows    = [b["low"]  for b in all_bars]
        highs   = [b["high"] for b in all_bars]
        pad     = (max(highs) - min(lows)) * 0.06
        y_lo    = min(min(lows), sl, tp) - pad
        y_hi    = max(max(highs), sl, tp) + pad
        ax.set_xlim(-1, x_total)
        ax.set_ylim(y_lo, y_hi)

        # Premium / discount zone shading
        if bullish_exp:
            ax.axhspan(origin,   eq_level, alpha=0.07, color="green")
            ax.axhspan(eq_level, peak,     alpha=0.07, color="crimson")
        else:
            ax.axhspan(peak,     eq_level, alpha=0.07, color="crimson")
            ax.axhspan(eq_level, origin,   alpha=0.07, color="green")

        # Vertical event markers
        bos_x  = next((i for i, c in enumerate(all_bars) if c["timestamp"] >= bos_15m_ts), None)
        peak_x = next((i for i, c in enumerate(all_bars) if c["timestamp"] >= peak_ts),    None)
        if bos_x is not None:
            ax.axvline(bos_x, color="steelblue", linestyle=":", linewidth=1.2, alpha=0.8, zorder=1)
            ax.text(bos_x + 0.3, y_hi - pad * 0.3, "15m BOS", fontsize=6, color="steelblue", va="top")
        if peak_x is not None:
            ax.axvline(peak_x, color="darkorange", linestyle="--", linewidth=1.2, alpha=0.8, zorder=1)
            ax.text(peak_x + 0.3, y_hi - pad * 0.3, "PEAK", fontsize=6, color="darkorange", va="top")

        # Horizontal levels
        ax.hlines(origin,   0, x_total, colors="gray",       linestyles=":",  linewidth=1.0, zorder=2, label="origin")
        ax.hlines(peak,     0, x_total, colors="steelblue",  linestyles=":",  linewidth=1.0, zorder=2, label="peak")
        ax.hlines(eq_level, 0, x_total, colors="orange",     linestyles="--", linewidth=1.2, zorder=2, label="EQ 50%")
        ax.hlines(sl,  entry_x, x_total, colors="red",       linestyles="--", linewidth=1.3, zorder=3)
        ax.hlines(tp,  entry_x, x_total, colors="limegreen", linestyles="--", linewidth=1.3, zorder=3)
        for lvl, lbl, col in [
            (origin, "orig", "gray"), (peak, "peak", "steelblue"),
            (eq_level, "EQ", "orange"), (sl, "SL", "red"), (tp, "TP", "limegreen"),
        ]:
            ax.text(x_total - 0.2, lvl, f" {lbl}", fontsize=7, color=col, va="center", fontweight="bold")

        # Entry arrow
        action      = "SELL" if direction == "bearish" else "BUY"
        tip_y       = display_5m[-1]["high"] if direction == "bearish" else display_5m[-1]["low"]
        price_range = max(highs) - min(lows) or 1.0
        offset      = price_range * 0.10
        text_y      = tip_y + offset if direction == "bearish" else tip_y - offset
        entry_color = "crimson" if direction == "bearish" else "green"
        ax.annotate(
            action, xy=(entry_x, tip_y), xytext=(entry_x, text_y),
            fontsize=9, fontweight="bold", color="white", ha="center", zorder=6,
            arrowprops=dict(arrowstyle="-|>", color=entry_color, lw=1.5),
            bbox=dict(boxstyle="round,pad=0.25", facecolor=entry_color, alpha=0.9),
        )

        # Outcome badge — bottom right
        out_color = "#26a69a" if "WIN" in outcome else ("#ef5350" if "LOSS" in outcome else "gray")
        ax.text(
            0.98, 0.03, outcome, transform=ax.transAxes, fontsize=11, fontweight="bold",
            color=out_color, ha="right", va="bottom",
            bbox=dict(boxstyle="round,pad=0.4", facecolor="white", alpha=0.9, edgecolor=out_color),
        )

        # Trade info box — top left
        risk   = abs(entry - sl)
        r      = round(abs(tp - entry) / risk, 1) if risk else 0.0
        ep_pct = sig.get("entry_pct_from_origin", 0) * 100
        info   = f"Entry: {entry:.0f}  SL: {sl:.0f}  TP: {tp:.0f}  R: 1:{r}  zone: {ep_pct:.0f}%"
        ax.text(
            0.01, 0.98, info, transform=ax.transAxes, fontsize=7,
            color="black", ha="left", va="top", zorder=10,
            bbox=dict(boxstyle="round,pad=0.3", facecolor="lightyellow", alpha=0.95, edgecolor="gray"),
        )

        # X-axis: Frankfurt time at every :00 and :30
        tick_pos, tick_lbl = [], []
        for i, bar in enumerate(all_bars):
            dt_ff = datetime.fromtimestamp(bar["timestamp"], tz=_FFM_TZ)
            if dt_ff.minute % 30 == 0:
                tick_pos.append(i)
                tick_lbl.append(dt_ff.strftime("%H:%M"))
        ax.set_xticks(tick_pos)
        ax.set_xticklabels(tick_lbl, fontsize=6, rotation=45, ha="right")
        ax.set_xlabel("Frankfurt time", fontsize=7)
        ax.set_title(
            f"DAX  {direction.upper()} counter-trend (fades {exp_dir.upper()} expansion)"
            f"  |  {sig.get('symbol', 'DAX')}",
            fontsize=9,
        )
        ax.tick_params(axis="y", labelsize=7)
        ax.legend(fontsize=7, loc="lower left")

        bos_dt      = datetime.fromtimestamp(entry_ts, tz=timezone.utc)
        outcome_tag = outcome.lower().replace(" ", "_")
        alert_id    = (f"dax-{bos_dt.minute:02d}-{bos_dt.hour:02d}"
                       f"-{bos_dt.day:02d}-{bos_dt.month:02d}-{bos_dt.year}")
        chart_path  = self.charts_dir / f"{alert_id}-{outcome_tag}.png"
        fig.savefig(str(chart_path), dpi=100)
        plt.close(fig)
        logging.info("DAX chart saved: %s", chart_path.name)
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
