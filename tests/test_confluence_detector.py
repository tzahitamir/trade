"""Tests for confluence detection and description text."""
from analysis.confluence_detector import (
    detect_confluences,
    confluence_description_text,
    confluence_labels,
    _LABEL_MAP,
    _CONF_DESCRIPTIONS,
)
from conftest import make_candle


def _ev(direction="bullish", broken=1.1050, ts=1_000_000):
    return {"direction": direction, "broken_level": broken, "breakout_ts": ts}


def _bull_candle(ts, base=1.1050, body=0.0010):
    return make_candle(ts, base, base + body * 1.5, base - body * 0.2, base + body)


def _bear_candle(ts, base=1.1050, body=0.0010):
    return make_candle(ts, base + body, base + body * 1.2, base - body * 0.2, base)


# --- CONF_CANDLE ---

def test_conf_candle_bullish_first_candle_closes_up():
    ev = _ev("bullish", broken=1.1050)
    lookahead = [_bull_candle(1_001_000)]
    pre_bos = [make_candle(i * 900, 1.1000, 1.1010, 1.0990, 1.1005) for i in range(20)]
    result = detect_confluences(ev, lookahead, pre_bos, [], atr=0.0010)
    assert "CONF_CANDLE" in result


def test_conf_candle_bearish_first_candle_closes_down():
    ev = _ev("bearish", broken=1.1050)
    lookahead = [_bear_candle(1_001_000, base=1.1050)]
    pre_bos = [make_candle(i * 900, 1.1000, 1.1010, 1.0990, 1.1005) for i in range(20)]
    result = detect_confluences(ev, lookahead, pre_bos, [], atr=0.0010)
    assert "CONF_CANDLE" in result


def test_no_conf_candle_when_empty_lookahead():
    ev = _ev("bullish", broken=1.1050)
    pre_bos = [make_candle(i * 900, 1.1000, 1.1010, 1.0990, 1.1005) for i in range(20)]
    result = detect_confluences(ev, [], pre_bos, [], atr=0.0010)
    assert result == []


# --- confluence_description_text ---

def test_description_text_contains_all_confluences():
    confs = ["CONF_CANDLE", "BRT", "HTF_LEVEL"]
    text = confluence_description_text(confs)
    for c in confs:
        assert _LABEL_MAP[c] in text
        assert _CONF_DESCRIPTIONS[c] in text


def test_description_text_one_line_per_confluence():
    confs = ["CONF_CANDLE", "BRT"]
    text = confluence_description_text(confs)
    lines = [l for l in text.splitlines() if l.strip()]
    assert len(lines) == 2


def test_description_text_empty_for_no_confluences():
    assert confluence_description_text([]) == ""


def test_description_text_checkmark_prefix():
    text = confluence_description_text(["CONF_CANDLE"])
    assert text.startswith("✓")


# --- confluence_labels ---

def test_labels_uses_label_map():
    text = confluence_labels(["CONF_CANDLE", "BRT"])
    assert "Confirmation Candle" in text
    assert "Break+Retest" in text


def test_labels_separator():
    text = confluence_labels(["CONF_CANDLE", "BRT"])
    assert "|" in text


def test_unknown_confluence_passed_through():
    text = confluence_description_text(["UNKNOWN_CONF"])
    assert "UNKNOWN_CONF" in text


# --- all known confluences have descriptions ---

def test_all_label_map_keys_have_descriptions():
    for key in _LABEL_MAP:
        assert key in _CONF_DESCRIPTIONS, f"{key} missing from _CONF_DESCRIPTIONS"
