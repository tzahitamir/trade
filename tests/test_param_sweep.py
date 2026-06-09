"""Tests for _apply_param_filter and _pset_label."""
import json
from main import _apply_param_filter, _pset_label


def _sig(**kwargs):
    """Build a minimal raw_signal dict."""
    defaults = {
        "symbol": "EURUSD",
        "direction": "bullish",
        "break_strength": 1.0,
        "confluences": json.dumps(["CONF_CANDLE"]),
        "htf_bias": "bullish",
        "has_liquidity_sweep": False,
        "session": "london",
        "dow": 1,            # Monday
        "hour": 10,
        "swing_age_candles": 10,
        "swing_test_count": 0,
        "break_body_pct": 0.5,
    }
    defaults.update(kwargs)
    return defaults


BASE_PARAMS = {
    "min_break_strength": 0.0,
    "require_brt_confluence": False,
    "require_liquidity_sweep": False,
    "min_conf_count": 1,
    "htf_aligned_only": False,
    "session": "all",
    "exclude_pairs": [],
    "min_swing_age_candles": 0,
    "min_swing_test_count": 0,
    "min_break_body_pct": 0.0,
    "max_confluences": None,
    "exclude_weekend": False,
    "dead_hours_utc": [],
    "max_break_strength": None,
}


# ── min_break_strength ─────────────────────────────────────────────────────────

def test_below_min_break_strength_filtered():
    sigs = [_sig(break_strength=0.4)]
    result = _apply_param_filter(sigs, {**BASE_PARAMS, "min_break_strength": 0.5})
    assert result == []


def test_above_min_break_strength_passes():
    sigs = [_sig(break_strength=0.6)]
    result = _apply_param_filter(sigs, {**BASE_PARAMS, "min_break_strength": 0.5})
    assert len(result) == 1


# ── max_break_strength cap ─────────────────────────────────────────────────────

def test_above_max_break_strength_filtered():
    sigs = [_sig(break_strength=5.0)]
    result = _apply_param_filter(sigs, {**BASE_PARAMS, "max_break_strength": 4.0})
    assert result == []


def test_below_max_break_strength_passes():
    sigs = [_sig(break_strength=3.9)]
    result = _apply_param_filter(sigs, {**BASE_PARAMS, "max_break_strength": 4.0})
    assert len(result) == 1


# ── max_confluences cap ────────────────────────────────────────────────────────

def test_over_max_confluences_filtered():
    sigs = [_sig(confluences=json.dumps(["A", "B", "C"]))]
    result = _apply_param_filter(sigs, {**BASE_PARAMS, "max_confluences": 2})
    assert result == []


def test_at_max_confluences_passes():
    sigs = [_sig(confluences=json.dumps(["A", "B"]))]
    result = _apply_param_filter(sigs, {**BASE_PARAMS, "max_confluences": 2})
    assert len(result) == 1


# ── exclude_weekend ────────────────────────────────────────────────────────────

def test_weekend_signal_filtered_when_exclude_weekend():
    sigs = [_sig(dow=5)]  # Saturday
    result = _apply_param_filter(sigs, {**BASE_PARAMS, "exclude_weekend": True})
    assert result == []


def test_weekday_signal_passes_exclude_weekend():
    sigs = [_sig(dow=1)]  # Monday
    result = _apply_param_filter(sigs, {**BASE_PARAMS, "exclude_weekend": True})
    assert len(result) == 1


# ── dead_hours_utc ─────────────────────────────────────────────────────────────

def test_dead_hour_signal_filtered():
    sigs = [_sig(hour=0)]  # midnight
    result = _apply_param_filter(sigs, {**BASE_PARAMS, "dead_hours_utc": [0, 1, 2, 3]})
    assert result == []


def test_non_dead_hour_passes():
    sigs = [_sig(hour=10)]
    result = _apply_param_filter(sigs, {**BASE_PARAMS, "dead_hours_utc": [0, 1, 2, 3]})
    assert len(result) == 1


# ── htf_aligned_only ──────────────────────────────────────────────────────────

def test_htf_misaligned_filtered():
    sigs = [_sig(htf_bias="bearish", direction="bullish")]
    result = _apply_param_filter(sigs, {**BASE_PARAMS, "htf_aligned_only": True})
    assert result == []


def test_htf_aligned_passes():
    sigs = [_sig(htf_bias="bullish", direction="bullish")]
    result = _apply_param_filter(sigs, {**BASE_PARAMS, "htf_aligned_only": True})
    assert len(result) == 1


# ── require_liquidity_sweep ────────────────────────────────────────────────────

def test_require_sweep_no_sweep_filtered():
    sigs = [_sig(has_liquidity_sweep=False)]
    result = _apply_param_filter(sigs, {**BASE_PARAMS, "require_liquidity_sweep": True})
    assert result == []


def test_require_sweep_with_sweep_passes():
    sigs = [_sig(has_liquidity_sweep=True)]
    result = _apply_param_filter(sigs, {**BASE_PARAMS, "require_liquidity_sweep": True})
    assert len(result) == 1


# ── confluences preserved as list in output ────────────────────────────────────

def test_confluences_deserialized_in_output():
    sigs = [_sig(confluences='["CONF_CANDLE","BRT"]')]
    result = _apply_param_filter(sigs, BASE_PARAMS)
    assert isinstance(result[0]["confluences"], list)


# ── _pset_label ────────────────────────────────────────────────────────────────

def test_pset_label_default_has_strength():
    label = _pset_label({"min_break_strength": 0.7})
    assert "str0.7" in label


def test_pset_label_nobrt_when_brt_disabled():
    label = _pset_label({"min_break_strength": 0.5, "require_brt_confluence": False})
    assert "nobrt" in label


def test_pset_label_brt_required_not_in_label():
    label = _pset_label({"min_break_strength": 0.5, "require_brt_confluence": True})
    assert "nobrt" not in label


def test_pset_label_htf_present_when_set():
    label = _pset_label({"min_break_strength": 0.5, "htf_aligned_only": True})
    assert "htf" in label


def test_pset_label_nowe_present_for_exclude_weekend():
    label = _pset_label({"min_break_strength": 0.5, "exclude_weekend": True})
    assert "nowe" in label


def test_pset_label_nodh_present_for_dead_hours():
    label = _pset_label({"min_break_strength": 0.5, "dead_hours_utc": [0, 1]})
    assert "nodh" in label


def test_pset_label_max_confluences_cap():
    label = _pset_label({"min_break_strength": 0.5, "max_confluences": 3})
    assert "mx3" in label


def test_pset_label_max_str_cap():
    label = _pset_label({"min_break_strength": 0.5, "max_break_strength": 5.0})
    assert "mxs5" in label


def test_pset_label_parts_joined_with_plus():
    label = _pset_label({"min_break_strength": 0.5, "htf_aligned_only": True, "exclude_weekend": True})
    parts = label.split("+")
    assert len(parts) >= 3
