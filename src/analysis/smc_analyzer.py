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
        # simple ATR as SMA of TRs
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

    def detect_bos(self, candles: List[Dict], params: Dict = None) -> List[Dict]:
        """
        Detect Break Of Structure (BOS) events in the provided candles list.
        Returns list of BOS events (may be empty).

        Expected `candles`: list of dicts with keys timestamp, open, high, low, close, volume
        """
        params = params or {}
        swing_lookback = params.get("swing_lookback", 20)
        min_break_candles = params.get("min_break_candles", 1)
        confirmation_candles = params.get("confirmation_candles", 1)
        volume_multiplier = params.get("volume_multiplier", 1.0)
        use_atr = params.get("use_atr", True)
        min_break_distance_param = params.get("min_break_distance")

        if not candles or len(candles) < 5:
            return []

        c = SMCAnalyzer._chronological(candles)
        atr = self.calculate_atr(candles) or 0.0
        last = c[-1]
        price = last["close"]

        swings = self._find_last_swing(candles, lookback=3, search_back=swing_lookback)
        events = []

        # determine threshold distance
        if min_break_distance_param is not None:
            min_break_distance = min_break_distance_param
        else:
            # default: 0.5% of price or 0.5 * ATR, whichever is larger
            pct = 0.005 * price
            min_break_distance = max(pct, 0.5 * atr)

        # bullish BOS: close(s) above swing_high + threshold
        sh = swings.get("swing_high")
        if sh is not None:
            threshold = sh + min_break_distance
            # check last min_break_candles closes
            ok = True
            for i in range(1, min_break_candles + 1):
                if len(c) - i < 0:
                    ok = False
                    break
                if c[-i]["close"] <= threshold:
                    ok = False
                    break
            if ok:
                # confirmation: at least one of next confirmation_candles shows follow-through or retest
                follow = 0
                for j in range(1, confirmation_candles + 1):
                    if len(c) - 1 - j < 0:
                        continue
                    # check that subsequent candles continue higher
                    if c[-1]["close"] < c[-1 - j]["close"]:
                        follow += 1
                break_strength = 0.0
                if atr > 0:
                    break_strength = (c[-1]["close"] - sh) / atr * (1 + follow / 5)
                events.append({
                    "symbol": params.get("symbol"),
                    "timeframe": params.get("timeframe"),
                    "direction": "bullish",
                    "broken_level": sh,
                    "breakout_ts": last["timestamp"],
                    "break_strength": break_strength,
                    "params_used": {
                        "swing_lookback": swing_lookback,
                        "min_break_distance": min_break_distance,
                        "min_break_candles": min_break_candles,
                        "confirmation_candles": confirmation_candles,
                    },
                })

        # bearish BOS: close(s) below swing_low - threshold
        sl = swings.get("swing_low")
        if sl is not None:
            threshold = sl - min_break_distance
            ok = True
            for i in range(1, min_break_candles + 1):
                if len(c) - i < 0:
                    ok = False
                    break
                if c[-i]["close"] >= threshold:
                    ok = False
                    break
            if ok:
                follow = 0
                for j in range(1, confirmation_candles + 1):
                    if len(c) - 1 - j < 0:
                        continue
                    if c[-1]["close"] > c[-1 - j]["close"]:
                        follow += 1
                break_strength = 0.0
                if atr > 0:
                    break_strength = (sl - c[-1]["close"]) / atr * (1 + follow / 5)
                events.append({
                    "symbol": params.get("symbol"),
                    "timeframe": params.get("timeframe"),
                    "direction": "bearish",
                    "broken_level": sl,
                    "breakout_ts": last["timestamp"],
                    "break_strength": break_strength,
                    "params_used": {
                        "swing_lookback": swing_lookback,
                        "min_break_distance": min_break_distance,
                        "min_break_candles": min_break_candles,
                        "confirmation_candles": confirmation_candles,
                    },
                })

        return events


__all__ = ["SMCAnalyzer"]
