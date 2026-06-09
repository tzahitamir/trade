"""Shared fixtures for all tests."""
import sys
import os
from pathlib import Path

# Ensure src/ and tests/ are on the path
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

import pytest
from db.local_db import LocalDB


@pytest.fixture
def db(tmp_path):
    """In-memory-style LocalDB using a temp file — reset per test."""
    return LocalDB(str(tmp_path / "test.db"))


def make_candle(ts, open_, high, low, close, volume=1000):
    return {"timestamp": ts, "open": open_, "high": high, "low": low, "close": close, "volume": volume}


def bullish_candle(ts, base=1.1000, size=0.0010):
    return make_candle(ts, base, base + size, base - size * 0.2, base + size * 0.8)


def bearish_candle(ts, base=1.1000, size=0.0010):
    return make_candle(ts, base + size, base + size * 1.2, base - size * 0.2, base)


def doji_candle(ts, base=1.1000, size=0.0010):
    mid = base + size * 0.5
    return make_candle(ts, mid, base + size, base, mid + size * 0.02)
