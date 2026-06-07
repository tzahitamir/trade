from typing import Dict, List, Optional


def find_trigger_candle(
    ev: Dict,
    lookahead_chron: List[Dict],
    pre_bos_chron: List[Dict],
    atr: float,
) -> Optional[int]:
    """
    Return the index (into lookahead_chron) of the first candle that confirms
    a confluence condition — i.e. the candle where the alert would fire.
    Returns None if no confirming candle is found.
    """
    direction = ev.get("direction", "bullish")
    broken = ev.get("broken_level", 0.0)
    bullish = direction == "bullish"
    snap = atr * 0.5 if atr > 0 else broken * 0.0005
    ob = _find_order_block(pre_bos_chron, direction)

    for i, c in enumerate(lookahead_chron):
        if (bullish and c["close"] > c["open"]) or (not bullish and c["close"] < c["open"]):
            return i
        if bullish and c["low"] <= broken + snap and c["close"] > broken:
            return i
        if not bullish and c["high"] >= broken - snap and c["close"] < broken:
            return i
        if ob is not None:
            if bullish and c["low"] <= ob["high"] and c["low"] >= ob["low"] - snap:
                return i
            if not bullish and c["high"] >= ob["low"] and c["high"] <= ob["high"] + snap:
                return i
    return None


_LABEL_MAP = {
    "CONF_CANDLE": "Confirmation Candle",
    "BRT": "Break+Retest",
    "OB_RETRACE": "OB Retrace",
    "FVG": "FVG Fill",
    "HTF_LEVEL": "HTF Level",
}

_CONF_DESCRIPTIONS = {
    "CONF_CANDLE": "First candle after BOS closes in trade direction",
    "BRT":         "Price retested the broken level before continuing",
    "OB_RETRACE":  "Price pulled back into the last opposing order block",
    "FVG":         "Price filled a fair-value gap left by the BOS impulse",
    "HTF_LEVEL":   "Broken 15m swing aligns with a 4h structural level",
}


def confluence_labels(confluences: List[str]) -> str:
    return "  |  ".join(_LABEL_MAP.get(c, c) for c in confluences)


def confluence_description_text(confluences: List[str]) -> str:
    lines = [f"✓ {_LABEL_MAP.get(c, c)}: {_CONF_DESCRIPTIONS.get(c, '')}" for c in confluences]
    return "\n".join(lines)


def _find_order_block(pre_bos_chron: List[Dict], direction: str) -> Optional[Dict]:
    """Last opposing candle before the BOS impulse (the Order Block origin)."""
    search = pre_bos_chron[-20:]
    for candle in reversed(search):
        if direction == "bullish" and candle["close"] < candle["open"]:
            return candle
        if direction == "bearish" and candle["close"] > candle["open"]:
            return candle
    return None


def detect_confluences(
    ev: Dict,
    lookahead_chron: List[Dict],
    pre_bos_chron: List[Dict],
    candles_4h_desc: List[Dict],
    atr: float,
) -> List[str]:
    """
    Check which confluence criteria are met in the candles after the BOS.

    ev               — BOS event dict from detect_bos()
    lookahead_chron  — candles after BOS, chronological (oldest→newest)
    pre_bos_chron    — candles up to and including BOS, chronological
    candles_4h_desc  — 4h candles newest-first (for HTF_LEVEL check)
    atr              — 14-period ATR of the 15m window

    Returns list of confirmed confluence names. Empty = no confirmation.
    """
    found: List[str] = []
    direction = ev.get("direction", "bullish")
    broken = ev.get("broken_level", 0.0)
    bos_ts = ev.get("breakout_ts", 0)
    bullish = direction == "bullish"
    snap = atr * 0.5 if atr > 0 else broken * 0.0005

    if not lookahead_chron:
        return found

    # 1. Confirmation candle — first lookahead candle aligned with BOS direction
    for c in lookahead_chron:
        if (bullish and c["close"] > c["open"]) or (not bullish and c["close"] < c["open"]):
            found.append("CONF_CANDLE")
            break

    # 2. Break + Retest — price returns to broken level then holds on the new side
    for c in lookahead_chron:
        if bullish:
            if c["low"] <= broken + snap and c["close"] > broken:
                found.append("BRT")
                break
        else:
            if c["high"] >= broken - snap and c["close"] < broken:
                found.append("BRT")
                break

    # 3. Order Block retrace — price retraces into origin (last opposing) candle
    ob = _find_order_block(pre_bos_chron, direction)
    if ob is not None:
        for c in lookahead_chron:
            if bullish:
                if c["low"] <= ob["high"] and c["low"] >= ob["low"] - snap:
                    found.append("OB_RETRACE")
                    break
            else:
                if c["high"] >= ob["low"] and c["high"] <= ob["high"] + snap:
                    found.append("OB_RETRACE")
                    break

    # 4. Fair Value Gap fill — price retraces into a 3-candle gap from the impulse
    impulse = pre_bos_chron[-10:]
    fvg_found = False
    for i in range(2, len(impulse)):
        if bullish:
            gap_lo, gap_hi = impulse[i - 2]["high"], impulse[i]["low"]
        else:
            gap_lo, gap_hi = impulse[i]["high"], impulse[i - 2]["low"]
        if gap_hi > gap_lo:
            for c in lookahead_chron:
                if c["low"] <= gap_hi and c["high"] >= gap_lo:
                    found.append("FVG")
                    fvg_found = True
                    break
        if fvg_found:
            break

    # 5. HTF level — broken 15m swing coincides with a 4h structural swing
    if candles_4h_desc and atr > 0:
        from analysis.smc_analyzer import SMCAnalyzer
        avail = [c for c in candles_4h_desc if c["timestamp"] <= bos_ts]
        if len(avail) >= 5:
            swings = SMCAnalyzer._find_last_swing(avail, lookback=2, search_back=20)
            ref = swings.get("swing_high") if bullish else swings.get("swing_low")
            if ref is not None and abs(broken - ref) <= atr * 2:
                found.append("HTF_LEVEL")

    return found
