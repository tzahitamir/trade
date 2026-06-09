"""Tests for Settings: should_alert, YAML loading, env var overrides."""
import os
import textwrap
import pytest
from config.settings import Settings


# ── should_alert ──────────────────────────────────────────────────────────────

def test_should_alert_true_for_pair_in_alert_pairs():
    s = Settings(fx_pairs=["EURUSD", "USDJPY"], alert_pairs=["EURUSD"])
    assert s.should_alert("EURUSD") is True


def test_should_alert_false_for_pair_not_in_alert_pairs():
    s = Settings(fx_pairs=["EURUSD", "USDJPY"], alert_pairs=["EURUSD"])
    assert s.should_alert("USDJPY") is False


def test_should_alert_falls_back_to_fx_pairs_when_alert_pairs_none():
    s = Settings(fx_pairs=["EURUSD", "NZDUSD"], alert_pairs=None)
    assert s.should_alert("EURUSD") is True
    assert s.should_alert("USDJPY") is False


def test_usdjpy_not_alerted_in_production_config():
    """USDJPY is data-only; must never appear in alert_pairs."""
    s = Settings(
        fx_pairs=["NZDUSD", "EURJPY", "EURUSD", "USDCHF", "USDCAD", "XAUUSD", "USDJPY"],
        alert_pairs=["NZDUSD", "EURJPY", "EURUSD", "USDCHF", "USDCAD", "XAUUSD"],
    )
    assert s.should_alert("USDJPY") is False
    for pair in ["NZDUSD", "EURJPY", "EURUSD", "USDCHF", "USDCAD", "XAUUSD"]:
        assert s.should_alert(pair) is True


# ── YAML loading ──────────────────────────────────────────────────────────────

def test_yaml_loads_fx_pairs(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(textwrap.dedent("""\
        fx_pairs:
          - EURUSD
          - NZDUSD
        alert_pairs:
          - EURUSD
        timeframes:
          - 15m
          - 4h
    """))
    s = Settings.load_from_yaml(str(cfg))
    assert s.fx_pairs == ["EURUSD", "NZDUSD"]
    assert s.alert_pairs == ["EURUSD"]
    assert "15m" in s.timeframes


def test_yaml_loads_dev_mode_false(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("dev_mode: false\n")
    s = Settings.load_from_yaml(str(cfg))
    assert s.dev_mode is False


def test_yaml_loads_provider_name(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(textwrap.dedent("""\
        provider:
          name: twelve_data
          api_key: test_key
    """))
    # Unset env var so YAML value wins
    env_backup = os.environ.pop("TWELVE_DATA_API_KEY", None)
    try:
        s = Settings.load_from_yaml(str(cfg))
        assert s.provider_name == "twelve_data"
        assert s.provider_api_key == "test_key"
    finally:
        if env_backup is not None:
            os.environ["TWELVE_DATA_API_KEY"] = env_backup


# ── env var overrides ─────────────────────────────────────────────────────────

def test_env_var_overrides_api_key(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("provider:\n  api_key: yaml_key\n")
    os.environ["TWELVE_DATA_API_KEY"] = "env_key"
    try:
        s = Settings.load_from_yaml(str(cfg))
        assert s.provider_api_key == "env_key"
    finally:
        del os.environ["TWELVE_DATA_API_KEY"]


def test_env_var_overrides_dev_mode(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("dev_mode: true\n")
    os.environ["TRADE_DEV_MODE"] = "false"
    try:
        s = Settings.load_from_yaml(str(cfg))
        assert s.dev_mode is False
    finally:
        del os.environ["TRADE_DEV_MODE"]


def test_env_var_overrides_db_path(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("")
    os.environ["TRADE_DB_PATH"] = "/tmp/override.db"
    try:
        s = Settings.load_from_yaml(str(cfg))
        assert s.db_path == "/tmp/override.db"
    finally:
        del os.environ["TRADE_DB_PATH"]


def test_env_var_trade_dev_mode_true_string(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("dev_mode: false\n")
    os.environ["TRADE_DEV_MODE"] = "1"
    try:
        s = Settings.load_from_yaml(str(cfg))
        assert s.dev_mode is True
    finally:
        del os.environ["TRADE_DEV_MODE"]
