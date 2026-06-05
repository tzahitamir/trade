from typing import List, Dict, Optional
from statistics import mean


class SMCAnalyzer:
    def __init__(self):
        pass

    @staticmethod
    def _chronological(candles: List[Dict]) -> List[Dict]:
        # ensure oldest -> newest
        return list(reversed(candles))

    @staticmethod
    def calculate_atr(candles: List[Dict], period: int = 14) -> Optional[float]:
        candles = SMCAnalyzer._chronological(candles)
        if len(candles) < period + 1:
            return None
        trs = []
        for i in range(1, len(candles)):
            high = candles[i]["high"]
            low = candles[i]["low"]
            prev_close = candles[i - 1]["close"]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)
        if len(trs) < period:
            return mean(trs)
        return mean(trs[-period:])

    @staticmethod
    def _find_last_swing(candles: List[Dict], lookback: int = 3, search_back: int = 50) -> Dict:
        """
        Find the most recent swing high and swing low using a simple neighborhood comparison.
        Returns dict with keys: 'swing_high', 'swing_high_idx', 'swing_low', 'swing_low_idx'
        """
        c = SMCAnalyzer._chronological(candles)
        n = len(c)
        start = max(lookback, n - search_back)
        swing_high = None
        swing_high_idx = None
        swing_low = None
        swing_low_idx = None
        for i in range(start, n - lookback):
            window_highs = [bar["high"] for bar in c[i - lookback : i + lookback + 1]]
            window_lows = [bar["low"] for bar in c[i - lookback : i + lookback + 1]]
            if c[i]["high"] == max(window_highs):
                swing_high = c[i]["high"]
                swing_high_idx = i
            if c[i]["low"] == min(window_lows):
                swing_low = c[i]["low"]
                swing_low_idx = i
        return {
            "swing_high": swing_high,
            "swing_high_idx": swing_high_idx,
            "swing_low": swing_low,
            "swing_low_idx": swing_low_idx,
        }

    @staticmethod
    def _has_preceding_sweep(
        c: List[Dict],
        direction: str,
        sweep_lookback: int = 30,
        level_lookback: int = 10,
    ) -> Optional[Dict]:
        """
        Check if a liquidity sweep precedes the last candle in c (chronological).

        Bullish BOS needs an SSL grab: a candle that wicked below the recent
        swing low and closed back above it (stop hunt below support).
        Bearish BOS needs a BSL grab: a candle that wicked above the recent
        swing high and closed back below it (stop hunt above resistance).

        Returns the sweep event dict if found, None otherwise.
        c must be chronological (oldest → newest).
        """
        # candles before the BOS candle
        pre_bos = c[-(sweep_lookback + 1):-1]
        if len(pre_bos) < level_lookback + 1:
            return None

        for i in range(level_lookback, len(pre_bos)):
            candle = pre_bos[i]
            prior = pre_bos[i - level_lookback:i]

            if direction == "bullish":
                level = min(b["low"] for b in prior)
                if candle["low"] < level and candle["close"] > level:
                    return {
                        "type": "SSL",
                        "level": level,
                        "timestamp": candle["timestamp"],
                        "wick_low": candle["low"],
                        "close": candle["close"],
                    }
            else:
                level = max(b["high"] for b in prior)
                if candle["high"] > level and candle["close"] < level:
                    return {
                        "type": "BSL",
                        "level": level,
                        "timestamp": candle["timestamp"],
                        "wick_high": candle["high"],
                        "close": candle["close"],
                    }

        return None

    def detect_bos(self, candles: List[Dict], params: Dict = None) -> List[Dict]:
        """
        Detect Break Of Structure (BOS) events in the provided candles list.

        Filters applied (both must pass):
          1. ATR gate: if ATR < min_atr_pct * price the market is too quiet
             (overnight chop) and no signal is generated.
          2. Break size: close must exceed swing ± (min_break_distance_atr_mult * ATR).

        When require_liquidity_sweep=True (default), a BOS is only valid when
        a liquidity sweep (stop hunt) preceded it in the last sweep_lookback candles:
          - Bullish BOS requires a prior SSL grab (wick below swing low, close above).
          - Bearish BOS requires a prior BSL grab (wick above swing high, close below).

        Returns list of BOS event dicts (may be empty).
        """
        params = params or {}
        swing_lookback = params.get("swing_lookback", 20)
        min_break_candles = params.get("min_break_candles", 1)
        confirmation_candles = params.get("confirmation_candles", 1)
        min_break_distance_param = params.get("min_break_distance")
        min_break_distance_atr_mult = params.get("min_break_distance_atr_mult", 0.3)
        min_atr_pct = params.get("min_atr_pct", 0.0003)   # ATR gate: skip if mkt too quiet
        require_sweep = params.get("require_liquidity_sweep", True)
        sweep_lookback = params.get("sweep_lookback", 30)
        sweep_level_lookback = params.get("sweep_level_lookback", 10)

        if not candles or len(candles) < 5:
            return []

        c = SMCAnalyzer._chronological(candles)
        atr = self.calculate_atr(candles) or 0.0
        last = c[-1]
        price = last["close"]

        # --- Activity gate: use the current candle's true range, not the lagging ATR.
        # The 14-period ATR is slow to respond when the market transitions from quiet
        # overnight to an active session, causing the gate to block valid early breaks.
        prev_close = c[-2]["close"] if len(c) >= 2 else last["close"]
        last_tr = max(
            last["high"] - last["low"],
            abs(last["high"] - prev_close),
            abs(last["low"] - prev_close),
        )
        if last_tr < min_atr_pct * price:
            return []

        swings = self._find_last_swing(candles, lookback=3, search_back=swing_lookback)
        events = []

        if min_break_distance_param is not None:
            min_break_distance = min_break_distance_param
        else:
            # 0.3 × ATR: screens out micro-noise breaks; the activity gate above
            # already handles the dead-market case so this can stay small
            min_break_distance = min_break_distance_atr_mult * atr

        # --- Bullish BOS: close(s) above swing_high + threshold ---
        sh = swings.get("swing_high")
        if sh is not None:
            threshold = sh + min_break_distance
            ok = True
            for i in range(1, min_break_candles + 1):
                if len(c) - i < 0 or c[-i]["close"] <= threshold:
                    ok = False
                    break

            if ok and require_sweep:
                sweep = self._has_preceding_sweep(c, "bullish", sweep_lookback, sweep_level_lookback)
                ok = sweep is not None
            else:
                sweep = None

            if ok:
                follow = sum(
                    1 for j in range(1, confirmation_candles + 1)
                    if len(c) - 1 - j >= 0 and c[-1]["close"] < c[-1 - j]["close"]
                )
                break_strength = (c[-1]["close"] - sh) / atr * (1 + follow / 5) if atr > 0 else 0.0
                events.append({
                    "symbol": params.get("symbol"),
                    "timeframe": params.get("timeframe"),
                    "direction": "bullish",
                    "broken_level": sh,
                    "breakout_ts": last["timestamp"],
                    "break_strength": break_strength,
                    "liquidity_sweep": sweep,
                    "params_used": {
                        "swing_lookback": swing_lookback,
                        "min_break_distance": min_break_distance,
                        "min_break_candles": min_break_candles,
                        "confirmation_candles": confirmation_candles,
                        "require_liquidity_sweep": require_sweep,
                    },
                })

        # --- Bearish BOS: close(s) below swing_low - threshold ---
        sl = swings.get("swing_low")
        if sl is not None:
            threshold = sl - min_break_distance
            ok = True
            for i in range(1, min_break_candles + 1):
                if len(c) - i < 0 or c[-i]["close"] >= threshold:
                    ok = False
                    break

            if ok and require_sweep:
                sweep = self._has_preceding_sweep(c, "bearish", sweep_lookback, sweep_level_lookback)
                ok = sweep is not None
            else:
                sweep = None

            if ok:
                follow = sum(
                    1 for j in range(1, confirmation_candles + 1)
                    if len(c) - 1 - j >= 0 and c[-1]["close"] > c[-1 - j]["close"]
                )
                break_strength = (sl - c[-1]["close"]) / atr * (1 + follow / 5) if atr > 0 else 0.0
                events.append({
                    "symbol": params.get("symbol"),
                    "timeframe": params.get("timeframe"),
                    "direction": "bearish",
                    "broken_level": sl,
                    "breakout_ts": last["timestamp"],
                    "break_strength": break_strength,
                    "liquidity_sweep": sweep,
                    "params_used": {
                        "swing_lookback": swing_lookback,
                        "min_break_distance": min_break_distance,
                        "min_break_candles": min_break_candles,
                        "confirmation_candles": confirmation_candles,
                        "require_liquidity_sweep": require_sweep,
                    },
                })

        return events


__all__ = ["SMCAnalyzer"]
