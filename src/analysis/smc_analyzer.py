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
                # Count candles between swing formation and break that touched the level
                touch_tol = 0.3 * atr
                sh_test_count = sum(
                    1 for i in range(sh_idx + 1, len(c) - 1)
                    if c[i]["high"] >= sh - touch_tol and c[i]["high"] <= sh + touch_tol * 3
                )
                brk = c[-1]
                brk_rng = brk["high"] - brk["low"]
                break_body_pct = round(abs(brk["close"] - brk["open"]) / brk_rng, 2) if brk_rng > 0 else 0.0
                events.append({
                    "symbol": params.get("symbol"),
                    "timeframe": params.get("timeframe"),
                    "direction": "bullish",
                    "broken_level": sh,
                    "breakout_ts": last["timestamp"],
                    "break_strength": break_strength,
                    "liquidity_sweep": sweep,
                    "swing_age_candles": len(c) - 1 - sh_idx,
                    "swing_test_count": sh_test_count,
                    "break_body_pct": break_body_pct,
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
                touch_tol = 0.3 * atr
                sl_test_count = sum(
                    1 for i in range(sl_idx + 1, len(c) - 1)
                    if c[i]["low"] <= sl + touch_tol and c[i]["low"] >= sl - touch_tol * 3
                )
                brk = c[-1]
                brk_rng = brk["high"] - brk["low"]
                break_body_pct = round(abs(brk["close"] - brk["open"]) / brk_rng, 2) if brk_rng > 0 else 0.0
                events.append({
                    "symbol": params.get("symbol"),
                    "timeframe": params.get("timeframe"),
                    "direction": "bearish",
                    "broken_level": sl,
                    "breakout_ts": last["timestamp"],
                    "break_strength": break_strength,
                    "liquidity_sweep": sweep,
                    "swing_age_candles": len(c) - 1 - sl_idx,
                    "swing_test_count": sl_test_count,
                    "break_body_pct": break_body_pct,
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
        Detect FVG + retrace + pin-bar doji rejection setup.

        Sequence:
          1. A Fair Value Gap (3-candle gap) formed by a stacked momentum move:
             the candle immediately before the gap (c1) AND the one before it (c0)
             must both be full-bodied in the FVG direction (body >= min_momentum_body_pct * range).
          2. Price retraced >= min_retrace_pct into the FVG zone.
          3. The current (last) candle is a pin-bar doji:
             - body <= max_doji_body_pct * range
             - rejection wick (lower for bullish, upper for bearish) >= min_rejection_wick_pct * range
          4. Doji closes back inside the zone (confirms rejection):
             Bullish: close >= fvg_low  |  Bearish: close <= fvg_high

        Returns at most one signal per call (the strongest match by retrace depth).
        """
        params = params or {}
        fvg_lookback            = params.get("fvg_lookback", 20)
        max_bars_after_fvg      = params.get("max_bars_after_fvg", 8)
        min_fvg_size_atr        = params.get("min_fvg_size_atr", 1.5)
        min_retrace_pct         = params.get("min_retrace_pct", 0.5)
        max_doji_body_pct       = params.get("max_doji_body_pct", 0.10)
        min_rejection_wick_pct  = params.get("min_rejection_wick_pct", 0.40)
        min_momentum_body_pct   = params.get("min_momentum_body_pct", 0.60)
        min_atr_pct             = params.get("min_atr_pct", 0.0003)

        if not candles or len(candles) < 6:
            return []

        c   = self._chronological(candles)
        atr = self.calculate_atr(candles) or 0.0
        if atr == 0:
            return []

        cur  = c[-1]
        body = abs(cur["close"] - cur["open"])
        rng  = cur["high"] - cur["low"]

        # ATR gate
        if rng < min_atr_pct * cur["close"]:
            return []

        # Pin-bar doji: small body
        if rng == 0 or body / rng > max_doji_body_pct:
            return []

        # Pre-compute rejection wicks
        lower_wick = min(cur["open"], cur["close"]) - cur["low"]
        upper_wick = cur["high"] - max(cur["open"], cur["close"])

        # Search within fvg_lookback candles (need c0 before c1, so search starts 1 earlier)
        search_start = max(0, len(c) - 2 - fvg_lookback)
        search = c[search_start : len(c) - 2]

        candidates = []
        for i in range(1, len(search) - 2):   # i >= 1 so c0 = search[i-1] exists
            fvg_c3_abs_idx = search_start + i + 2
            bars_since_fvg = (len(c) - 1) - fvg_c3_abs_idx
            if bars_since_fvg > max_bars_after_fvg:
                continue

            c0, c1, c2, c3 = search[i - 1], search[i], search[i + 1], search[i + 2]

            def _body_pct(candle):
                r = candle["high"] - candle["low"]
                return abs(candle["close"] - candle["open"]) / r if r > 0 else 0.0

            # --- Bullish FVG ---
            if c3["low"] > c1["high"]:
                fvg_lo, fvg_hi = c1["high"], c3["low"]
                fvg_size = fvg_hi - fvg_lo
                if fvg_size < min_fvg_size_atr * atr:
                    continue
                # Stacked momentum: c0 and c1 both bullish full-body candles
                if not (c0["close"] > c0["open"] and _body_pct(c0) >= min_momentum_body_pct):
                    continue
                if not (c1["close"] > c1["open"] and _body_pct(c1) >= min_momentum_body_pct):
                    continue
                retrace_level = fvg_hi - min_retrace_pct * fvg_size
                if cur["low"] > retrace_level:
                    continue
                if cur["low"] < fvg_lo - atr * 0.3:
                    continue
                if cur["close"] < fvg_lo:
                    continue
                # Rejection wick must poke down into the FVG (lower wick)
                if rng > 0 and lower_wick / rng < min_rejection_wick_pct:
                    continue
                depth = (fvg_hi - cur["low"]) / fvg_size
                candidates.append({
                    "symbol":                   params.get("symbol"),
                    "timeframe":                params.get("timeframe"),
                    "direction":                "bullish",
                    "fvg_low":                  fvg_lo,
                    "fvg_high":                 fvg_hi,
                    "fvg_ts":                   c2["timestamp"],
                    "breakout_ts":              cur["timestamp"],
                    "fvg_size_atr":             round(fvg_size / atr, 2),
                    "retrace_depth":            round(depth, 2),
                    "doji_body_pct":            round(body / rng, 2),
                    "rejection_wick_pct":       round(lower_wick / rng, 2),
                    "bars_since_fvg":           bars_since_fvg,
                })

            # --- Bearish FVG ---
            if c1["low"] > c3["high"]:
                fvg_hi, fvg_lo = c1["low"], c3["high"]
                fvg_size = fvg_hi - fvg_lo
                if fvg_size < min_fvg_size_atr * atr:
                    continue
                # Stacked momentum: c0 and c1 both bearish full-body candles
                if not (c0["close"] < c0["open"] and _body_pct(c0) >= min_momentum_body_pct):
                    continue
                if not (c1["close"] < c1["open"] and _body_pct(c1) >= min_momentum_body_pct):
                    continue
                retrace_level = fvg_lo + min_retrace_pct * fvg_size
                if cur["high"] < retrace_level:
                    continue
                if cur["high"] > fvg_hi + atr * 0.3:
                    continue
                if cur["close"] > fvg_hi:
                    continue
                # Rejection wick must poke up into the FVG (upper wick)
                if rng > 0 and upper_wick / rng < min_rejection_wick_pct:
                    continue
                depth = (cur["high"] - fvg_lo) / fvg_size
                candidates.append({
                    "symbol":                   params.get("symbol"),
                    "timeframe":                params.get("timeframe"),
                    "direction":                "bearish",
                    "fvg_low":                  fvg_lo,
                    "fvg_high":                 fvg_hi,
                    "fvg_ts":                   c2["timestamp"],
                    "breakout_ts":              cur["timestamp"],
                    "fvg_size_atr":             round(fvg_size / atr, 2),
                    "retrace_depth":            round(depth, 2),
                    "doji_body_pct":            round(body / rng, 2),
                    "rejection_wick_pct":       round(upper_wick / rng, 2),
                    "bars_since_fvg":           bars_since_fvg,
                })

        if not candidates:
            return []
        return [max(candidates, key=lambda x: x["retrace_depth"])]

    def detect_liquidity_sweep(self, candles: List[Dict], params: Dict = None) -> List[Dict]:
        """
        Detect liquidity sweep + pin-bar rejection setup.

        Pool sources:
          EQH/EQL — equal highs/lows (2+ swing pivots within eq_tolerance_atr of each other)
          PDH/PDL — previous-day high/low (UTC boundary)

        A sweep: wick exceeds pool by >= min_sweep_atr * ATR, candle closes back inside.
        Rejection: sweeping candle opposing wick >= min_rejection_wick_pct * range.

        Stored metadata per signal (for post-scan filtering):
          pool_touch_count — number of swing points that formed the pool (EQH/EQL only)
          pool_age_bars    — candles since the most recent pool touch
        """
        from datetime import datetime as _dt, timezone as _tz

        params = params or {}
        eq_tolerance_atr       = params.get("eq_tolerance_atr", 0.20)
        swing_lookback         = params.get("swing_lookback", 30)
        min_swing_strength     = params.get("min_swing_strength", 2)
        min_sweep_atr          = params.get("min_sweep_atr", 0.0)
        min_rejection_wick_pct = params.get("min_rejection_wick_pct", 0.0)
        use_pdh_pdl            = params.get("use_pdh_pdl", True)
        use_eq_pools           = params.get("use_eq_pools", True)

        if len(candles) < 30:
            return []

        c   = candles   # desc (newest first)
        cur = c[0]      # the potential sweep candle

        # Kill zone gate — fast exit if sweep candle is outside all kill zones
        kill_zones = params.get("kill_zones_utc")   # None = all hours; or [(7,10),(13,16)]
        if kill_zones is not None:
            cur_hour = _dt.fromtimestamp(cur["timestamp"], tz=_tz.utc).hour
            if not any(start <= cur_hour < end for start, end in kill_zones):
                return []

        atr = self.calculate_atr(candles)
        if not atr:
            return []

        rng = cur["high"] - cur["low"]
        if rng == 0:
            return []

        lower_wick = min(cur["open"], cur["close"]) - cur["low"]
        upper_wick = cur["high"] - max(cur["open"], cur["close"])

        # chronological lookback (excludes current candle)
        chron_lb = list(reversed(c[1: swing_lookback + 1]))

        pools = []  # (level, pool_type, touch_count, age_bars)

        # --- EQH / EQL -------------------------------------------------------
        if use_eq_pools and len(chron_lb) >= 5:
            tol = eq_tolerance_atr * atr
            n   = len(chron_lb)

            # collect (level, chron_lb index) for swing pivots
            sh_with_idx = []
            for i in range(min_swing_strength, n - min_swing_strength):
                h = chron_lb[i]["high"]
                if (all(chron_lb[j]["high"] <= h for j in range(i - min_swing_strength, i)) and
                        all(chron_lb[j]["high"] <= h for j in range(i + 1, i + min_swing_strength + 1))):
                    sh_with_idx.append((h, i))

            sl_with_idx = []
            for i in range(min_swing_strength, n - min_swing_strength):
                l = chron_lb[i]["low"]
                if (all(chron_lb[j]["low"] >= l for j in range(i - min_swing_strength, i)) and
                        all(chron_lb[j]["low"] >= l for j in range(i + 1, i + min_swing_strength + 1))):
                    sl_with_idx.append((l, i))

            def _cluster(items):
                # items = [(level, chron_idx), ...]
                # returns [(avg_level, touch_count, age_bars), ...]
                # age_bars = bars since most recent touch (0 = just before current candle)
                used   = [False] * len(items)
                groups = []
                for i in range(len(items)):
                    if used[i]:
                        continue
                    lvl_i, idx_i = items[i]
                    grp = [(lvl_i, idx_i)]
                    for j in range(i + 1, len(items)):
                        if not used[j] and abs(items[j][0] - lvl_i) <= tol:
                            grp.append(items[j])
                            used[j] = True
                    if len(grp) >= 2:
                        avg_lvl        = sum(l for l, _ in grp) / len(grp)
                        most_recent    = max(idx for _, idx in grp)  # highest idx = most recent
                        age_bars       = (n - 1) - most_recent       # bars before current candle
                        groups.append((avg_lvl, len(grp), age_bars))
                return groups

            for lvl, touches, age in _cluster(sh_with_idx):
                pools.append((lvl, "EQH", touches, age))
            for lvl, touches, age in _cluster(sl_with_idx):
                pools.append((lvl, "EQL", touches, age))

        # --- PDH / PDL -------------------------------------------------------
        if use_pdh_pdl and chron_lb:
            cur_date = _dt.fromtimestamp(cur["timestamp"], tz=_tz.utc).date()
            prev_day = [
                (i, c2) for i, c2 in enumerate(chron_lb)
                if _dt.fromtimestamp(c2["timestamp"], tz=_tz.utc).date() < cur_date
            ]
            if prev_day:
                n = len(chron_lb)
                pdh_idx, pdh_c = max(prev_day, key=lambda x: x[1]["high"])
                pdl_idx, pdl_c = min(prev_day, key=lambda x: x[1]["low"])
                pools.append((pdh_c["high"], "PDH", 1, (n - 1) - pdh_idx))
                pools.append((pdl_c["low"],  "PDL", 1, (n - 1) - pdl_idx))

        if not pools:
            return []

        # --- Evaluate each pool for a sweep ----------------------------------
        events     = []
        seen_types = set()

        for pool_level, pool_type, touch_count, age_bars in pools:
            if pool_type in seen_types:
                continue

            bullish = pool_type in ("EQL", "PDL")

            if bullish:
                sweep_size = (pool_level - cur["low"]) / atr
                if sweep_size < min_sweep_atr:
                    continue
                if cur["close"] <= pool_level:
                    continue
                if rng > 0 and lower_wick / rng < min_rejection_wick_pct:
                    continue
                events.append({
                    "direction":          "bullish",
                    "pool_type":          pool_type,
                    "pool_level":         pool_level,
                    "sweep_size_atr":     round(sweep_size, 2),
                    "rejection_wick_pct": round(lower_wick / rng, 2),
                    "pool_touch_count":   touch_count,
                    "pool_age_bars":      age_bars,
                    "breakout_ts":        cur["timestamp"],
                    "symbol":             params.get("symbol"),
                    "timeframe":          params.get("timeframe"),
                })
            else:
                sweep_size = (cur["high"] - pool_level) / atr
                if sweep_size < min_sweep_atr:
                    continue
                if cur["close"] >= pool_level:
                    continue
                if rng > 0 and upper_wick / rng < min_rejection_wick_pct:
                    continue
                events.append({
                    "direction":          "bearish",
                    "pool_type":          pool_type,
                    "pool_level":         pool_level,
                    "sweep_size_atr":     round(sweep_size, 2),
                    "rejection_wick_pct": round(upper_wick / rng, 2),
                    "pool_touch_count":   touch_count,
                    "pool_age_bars":      age_bars,
                    "breakout_ts":        cur["timestamp"],
                    "symbol":             params.get("symbol"),
                    "timeframe":          params.get("timeframe"),
                })
            seen_types.add(pool_type)

        return events

    def detect_dax_session_setup(
        self,
        candles_15m_session: List[Dict],   # session-window 15m candles, oldest→newest
        candles_5m_full: List[Dict],        # 5m candles covering session + lookahead, oldest→newest
        params: Dict = None,
        candles_15m_presession: List[Dict] = None,
    ) -> List[Dict]:
        """
        Counter-trend DAX Frankfurt Open session strategy.

        1. Detect session expansion via 15m BOS against pre-session structure.
        2. Find the expansion peak (session high for bull, session low for bear).
        3. From the peak, scan 5m for a CHoCH/BOS COUNTER-TREND (bearish for bull
           expansion, bullish for bear expansion).
        4. Accept signal only while price is still in premium zone (above eq_level
           for bearish counter-trade; below eq_level for bullish counter-trade).
        5. TP = origin + tp_pct × expansion_range (mean reversion target).
        6. SL = recent 5m swing extreme + sl_atr_mult × ATR_5m.

        Returns at most one signal per session (the first qualifying counter-trend entry).
        """
        params = params or {}
        tp_pct         = params.get("tp_pct", 0.5)
        sl_atr_mult    = params.get("sl_atr_mult", 0.5)
        min_exp_atr    = params.get("min_expansion_atr", 1.0)
        entry_zone_pct = params.get("entry_zone_min_pct", 0.5)
        swing_lb_5m    = params.get("swing_lookback_5m", 12)
        min_str_5m     = params.get("min_break_str_5m", 0.3)

        if len(candles_15m_session) < 3:
            return []

        session_start_ts = candles_15m_session[0]["timestamp"]
        pre      = (candles_15m_presession or [])[-16:]
        combined = pre + candles_15m_session

        if len(combined) < 6:
            return []

        atr_15m = self.calculate_atr(list(reversed(combined))) or 1.0

        # Step 1 — detect expansion direction via first session BOS against pre-session structure
        first_bos             = None
        first_bos_combined_idx = None
        for i in range(5, len(combined)):
            if combined[i]["timestamp"] < session_start_ts:
                continue
            window_desc = list(reversed(combined[:i + 1]))
            events = self.detect_bos(
                window_desc,
                params={
                    "min_break_strength":       0.3,
                    "min_swing_age_candles":    2,
                    "swing_lookback":           min(16, i),
                    "min_break_distance_atr_mult": 0.1,
                    "min_atr_pct":              0.0001,
                    "require_liquidity_sweep":  False,
                    "symbol":                   params.get("symbol"),
                    "timeframe":                "15m",
                },
            )
            for ev in events:
                if ev["breakout_ts"] >= session_start_ts:
                    first_bos              = ev
                    first_bos_combined_idx = i
                    break
            if first_bos:
                break

        if first_bos is None:
            return []

        direction   = first_bos["direction"]
        ct_dir      = "bearish" if direction == "bullish" else "bullish"
        bullish_exp = direction == "bullish"
        bos_ts      = first_bos["breakout_ts"]

        # Step 2 — key expansion levels
        pre_bos  = combined[:first_bos_combined_idx + 1]
        post_bos = combined[first_bos_combined_idx:]

        if bullish_exp:
            origin      = min(c["low"]  for c in pre_bos[-16:]) if pre_bos else combined[0]["low"]
            peak        = max(c["high"] for c in post_bos)      if post_bos else first_bos["broken_level"]
            peak_candle = max(post_bos, key=lambda c: c["high"])
        else:
            origin      = max(c["high"] for c in pre_bos[-16:]) if pre_bos else combined[0]["high"]
            peak        = min(c["low"]  for c in post_bos)      if post_bos else first_bos["broken_level"]
            peak_candle = min(post_bos, key=lambda c: c["low"])

        expansion_range = abs(peak - origin)
        if expansion_range < min_exp_atr * atr_15m:
            return []

        peak_ts = peak_candle["timestamp"]

        eq_level    = (origin + 0.5          * expansion_range) if bullish_exp else (origin - 0.5          * expansion_range)
        tp_level    = (origin + tp_pct       * expansion_range) if bullish_exp else (origin - tp_pct       * expansion_range)
        entry_floor = (origin + entry_zone_pct * expansion_range) if bullish_exp else (origin - entry_zone_pct * expansion_range)

        # Step 3 — scan 5m COUNTER-TREND starting from the expansion peak
        post_peak_5m = [c for c in candles_5m_full if c["timestamp"] >= peak_ts]
        if len(post_peak_5m) < 5:
            return []

        atr_5m = self.calculate_atr(list(reversed(post_peak_5m[:60]))) or (expansion_range * 0.005)

        for i in range(4, min(len(post_peak_5m), 120)):
            cur = post_peak_5m[i]

            # Stop if price has moved through eq_level — mean reversion already playing out
            if bullish_exp  and cur["close"] < eq_level:
                break
            if not bullish_exp and cur["close"] > eq_level:
                break

            # Skip if price hasn't pulled back into the premium entry zone yet
            if bullish_exp  and cur["close"] < entry_floor:
                continue
            if not bullish_exp and cur["close"] > entry_floor:
                continue

            window_5m      = post_peak_5m[max(0, i - swing_lb_5m + 1): i + 1]
            window_5m_desc = list(reversed(window_5m))
            if len(window_5m) < 5:
                continue

            # detect_bos computes ATR internally, but the 5m window is often < 15
            # candles, making calculate_atr return None → atr=0 → break_strength=0.
            # Fix: pass min_break_distance as an absolute value from the wider atr_5m,
            # and disable the relative strength filter (min_break_strength=0.0).
            events_5m = self.detect_bos(
                window_5m_desc,
                params={
                    "min_break_strength":       0.0,
                    "min_break_distance":       min_str_5m * atr_5m,
                    "min_swing_age_candles":    2,
                    "swing_lookback":           len(window_5m),
                    "min_break_distance_atr_mult": 0.0,
                    "min_atr_pct":              0.0,
                    "require_liquidity_sweep":  False,
                    "symbol":                   params.get("symbol"),
                    "timeframe":                "5m",
                },
            )

            for ev in events_5m:
                if ev["direction"] != ct_dir:
                    continue

                entry_candle = next((c for c in post_peak_5m if c["timestamp"] == ev["breakout_ts"]), cur)
                entry = entry_candle["close"]

                # Confirm entry is still in premium zone at the signal moment
                if bullish_exp  and (entry <= eq_level or entry < entry_floor):
                    continue
                if not bullish_exp and (entry >= eq_level or entry > entry_floor):
                    continue

                recent = post_peak_5m[max(0, i - 3): i + 1]
                sl = (max(c["high"] for c in recent) + sl_atr_mult * atr_5m) if bullish_exp \
                    else (min(c["low"] for c in recent) - sl_atr_mult * atr_5m)

                risk      = abs(entry - sl) or atr_5m
                retrace   = abs(peak - entry) / expansion_range if expansion_range else 0.0
                entry_pct = abs(entry - origin) / expansion_range if expansion_range else 0.0

                return [{
                    "symbol":                params.get("symbol"),
                    "timeframe":             "15m+5m",
                    "direction":             ct_dir,
                    "expansion_dir":         direction,
                    "breakout_ts":           ev["breakout_ts"],
                    "bos_15m_ts":            bos_ts,
                    "peak_ts":               peak_ts,
                    "origin":                round(origin, 2),
                    "peak":                  round(peak, 2),
                    "eq_level":              round(eq_level, 2),
                    "expansion_range":       round(expansion_range, 2),
                    "entry":                 round(entry, 2),
                    "sl":                    round(sl, 2),
                    "tp":                    round(tp_level, 2),
                    "risk":                  round(risk, 2),
                    "bos_5m_level":          round(ev["broken_level"], 2),
                    "retrace_depth_pct":     round(retrace, 2),
                    "entry_pct_from_origin": round(entry_pct, 2),
                }]

        return []

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
