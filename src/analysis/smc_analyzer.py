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
        min_break_strength = params.get("min_break_strength", 0.7)  # filter weak BOS signals
        min_swing_age = params.get("min_swing_age_candles", 5)  # swing must be this many candles old
        require_sweep = params.get("require_liquidity_sweep", False)
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
        sh_idx = swings.get("swing_high_idx")
        if sh_idx is not None and (len(c) - 1 - sh_idx) < min_swing_age:
            sh = None  # swing too recent to be valid structure
        if sh is not None:
            threshold = sh + min_break_distance
            ok = True
            for i in range(1, min_break_candles + 1):
                if len(c) - i < 0 or c[-i]["close"] <= threshold:
                    ok = False
                    break

            sweep = self._has_preceding_sweep(c, "bullish", sweep_lookback, sweep_level_lookback)
            if ok and require_sweep:
                ok = sweep is not None

            if ok:
                follow = sum(
                    1 for j in range(1, confirmation_candles + 1)
                    if len(c) - 1 - j >= 0 and c[-1]["close"] < c[-1 - j]["close"]
                )
                break_strength = (c[-1]["close"] - sh) / atr * (1 + follow / 5) if atr > 0 else 0.0
                if break_strength < min_break_strength:
                    ok = False
            if ok:
                events.append({
                    "symbol": params.get("symbol"),
                    "timeframe": params.get("timeframe"),
                    "direction": "bullish",
                    "broken_level": sh,
                    "breakout_ts": last["timestamp"],
                    "break_strength": break_strength,
                    "liquidity_sweep": sweep,
                    "swing_age_candles": len(c) - 1 - sh_idx,
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
        sl_idx = swings.get("swing_low_idx")
        if sl_idx is not None and (len(c) - 1 - sl_idx) < min_swing_age:
            sl = None  # swing too recent to be valid structure
        if sl is not None:
            threshold = sl - min_break_distance
            ok = True
            for i in range(1, min_break_candles + 1):
                if len(c) - i < 0 or c[-i]["close"] >= threshold:
                    ok = False
                    break

            sweep = self._has_preceding_sweep(c, "bearish", sweep_lookback, sweep_level_lookback)
            if ok and require_sweep:
                ok = sweep is not None

            if ok:
                follow = sum(
                    1 for j in range(1, confirmation_candles + 1)
                    if len(c) - 1 - j >= 0 and c[-1]["close"] > c[-1 - j]["close"]
                )
                break_strength = (sl - c[-1]["close"]) / atr * (1 + follow / 5) if atr > 0 else 0.0
                if break_strength < min_break_strength:
                    ok = False
            if ok:
                events.append({
                    "symbol": params.get("symbol"),
                    "timeframe": params.get("timeframe"),
                    "direction": "bearish",
                    "broken_level": sl,
                    "breakout_ts": last["timestamp"],
                    "break_strength": break_strength,
                    "liquidity_sweep": sweep,
                    "swing_age_candles": len(c) - 1 - sl_idx,
                    "params_used": {
                        "swing_lookback": swing_lookback,
                        "min_break_distance": min_break_distance,
                        "min_break_candles": min_break_candles,
                        "confirmation_candles": confirmation_candles,
                        "require_liquidity_sweep": require_sweep,
                    },
                })

        return events

    def detect_fvg_doji(self, candles: List[Dict], params: Dict = None) -> List[Dict]:
        """
        Detect FVG + deep retrace + doji/rejection setup.

        Sequence:
          1. A Fair Value Gap (3-candle gap) formed within fvg_lookback candles.
          2. Price retraced >= min_retrace_pct into the FVG zone.
          3. The current (last) candle is a doji: body <= max_doji_body_pct * range.
          4. Doji closed in the FVG origin direction (rejection confirmed).

        Returns at most one signal per call (the strongest match by retrace depth).
        """
        params = params or {}
        fvg_lookback      = params.get("fvg_lookback", 20)
        min_fvg_size_atr  = params.get("min_fvg_size_atr", 0.3)
        min_retrace_pct   = params.get("min_retrace_pct", 0.5)
        max_doji_body_pct = params.get("max_doji_body_pct", 0.35)
        min_atr_pct       = params.get("min_atr_pct", 0.0003)

        if not candles or len(candles) < 5:
            return []

        c   = self._chronological(candles)
        atr = self.calculate_atr(candles) or 0.0
        if atr == 0:
            return []

        cur  = c[-1]
        body = abs(cur["close"] - cur["open"])
        rng  = cur["high"] - cur["low"]

        # ATR gate: skip dead-market candles
        if rng < min_atr_pct * cur["close"]:
            return []

        # Must be a doji (small body relative to full range)
        if rng == 0 or body / rng > max_doji_body_pct:
            return []

        # Search last fvg_lookback candles (exclude current and 1 gap) for a FVG
        search_start = max(0, len(c) - 2 - fvg_lookback)
        search = c[search_start : len(c) - 2]

        candidates = []
        for i in range(len(search) - 2):
            c1, c2, c3 = search[i], search[i + 1], search[i + 2]

            # --- Bullish FVG: gap above c1.high below c3.low ---
            if c3["low"] > c1["high"]:
                fvg_lo, fvg_hi = c1["high"], c3["low"]
                fvg_size = fvg_hi - fvg_lo
                if fvg_size < min_fvg_size_atr * atr:
                    continue
                retrace_level = fvg_hi - min_retrace_pct * fvg_size
                if cur["low"] > retrace_level:
                    continue  # not deep enough
                if cur["low"] < fvg_lo - atr * 0.3:
                    continue  # blew through — FVG invalidated
                mid = (cur["low"] + cur["high"]) / 2
                if cur["close"] < mid:
                    continue  # closed weak, not a bullish rejection
                depth = (fvg_hi - cur["low"]) / fvg_size
                candidates.append({
                    "symbol":        params.get("symbol"),
                    "timeframe":     params.get("timeframe"),
                    "direction":     "bullish",
                    "fvg_low":       fvg_lo,
                    "fvg_high":      fvg_hi,
                    "fvg_ts":        c2["timestamp"],
                    "breakout_ts":   cur["timestamp"],
                    "fvg_size_atr":  round(fvg_size / atr, 2),
                    "retrace_depth": round(depth, 2),
                    "doji_body_pct": round(body / rng, 2),
                })

            # --- Bearish FVG: gap below c1.low above c3.high ---
            if c1["low"] > c3["high"]:
                fvg_hi, fvg_lo = c1["low"], c3["high"]
                fvg_size = fvg_hi - fvg_lo
                if fvg_size < min_fvg_size_atr * atr:
                    continue
                retrace_level = fvg_lo + min_retrace_pct * fvg_size
                if cur["high"] < retrace_level:
                    continue
                if cur["high"] > fvg_hi + atr * 0.3:
                    continue
                mid = (cur["low"] + cur["high"]) / 2
                if cur["close"] > mid:
                    continue  # closed strong, not a bearish rejection
                depth = (cur["high"] - fvg_lo) / fvg_size
                candidates.append({
                    "symbol":        params.get("symbol"),
                    "timeframe":     params.get("timeframe"),
                    "direction":     "bearish",
                    "fvg_low":       fvg_lo,
                    "fvg_high":      fvg_hi,
                    "fvg_ts":        c2["timestamp"],
                    "breakout_ts":   cur["timestamp"],
                    "fvg_size_atr":  round(fvg_size / atr, 2),
                    "retrace_depth": round(depth, 2),
                    "doji_body_pct": round(body / rng, 2),
                })

        if not candidates:
            return []
        # Return the best match (deepest retrace = most significant rejection)
        return [max(candidates, key=lambda x: x["retrace_depth"])]

    def get_htf_bias(self, candles_htf: List[Dict], bos_ts: int) -> str:
        """
        Determine the HTF (4h) directional bias at the time of a lower-TF BOS.

        Uses the swing high and swing low of the 4h candles that were available
        at bos_ts (i.e. candles whose timestamp <= bos_ts).

        Logic:
          - price above swing_high  → bullish (already broke HTF structure up)
          - price below swing_low   → bearish (already broke HTF structure down)
          - price between the two   → whichever side of the midpoint price sits on

        Returns 'bullish', 'bearish', or 'neutral' (insufficient data).
        """
        available = [c for c in candles_htf if c["timestamp"] <= bos_ts]
        if len(available) < 10:
            return "neutral"

        c = self._chronological(available)  # oldest → newest
        price = c[-1]["close"]

        swings = self._find_last_swing(available, lookback=2, search_back=20)
        sh = swings.get("swing_high")
        sl = swings.get("swing_low")

        if sh is None or sl is None:
            return "neutral"

        if price > sh:
            return "bullish"
        if price < sl:
            return "bearish"
        mid = (sh + sl) / 2
        return "bullish" if price >= mid else "bearish"


__all__ = ["SMCAnalyzer"]
