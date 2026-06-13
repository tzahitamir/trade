"""Unit tests for _5m_candle_quality_pass per-symbol candle filter."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import pytest
from main import _5m_candle_quality_pass


# ─── XAUUSD ────────────────────────────────────────────────────────────────

class TestXAUUSD:
    sym = "XAUUSD"

    def test_pass_c2_in_range(self):
        ok, _ = _5m_candle_quality_pass(self.sym, b1=0.5, b2=0.5, a1=True, a2=True)
        assert ok

    def test_pass_c2_lower_boundary(self):
        ok, _ = _5m_candle_quality_pass(self.sym, b1=0.5, b2=0.3, a1=True, a2=True)
        assert ok

    def test_pass_c2_near_upper_boundary(self):
        ok, _ = _5m_candle_quality_pass(self.sym, b1=1.0, b2=1.49, a1=False, a2=False)
        assert ok

    def test_block_c2_too_small(self):
        ok, reason = _5m_candle_quality_pass(self.sym, b1=0.5, b2=0.2, a1=True, a2=True)
        assert not ok
        assert "0.20" in reason

    def test_block_c2_exactly_zero(self):
        ok, _ = _5m_candle_quality_pass(self.sym, b1=0.5, b2=0.0, a1=True, a2=True)
        assert not ok

    def test_block_c2_at_upper_boundary(self):
        ok, reason = _5m_candle_quality_pass(self.sym, b1=0.5, b2=1.5, a1=True, a2=True)
        assert not ok
        assert "1.50" in reason

    def test_block_c2_too_large(self):
        ok, _ = _5m_candle_quality_pass(self.sym, b1=0.5, b2=2.0, a1=True, a2=True)
        assert not ok

    def test_alignment_irrelevant_for_xauusd(self):
        # XAUUSD filter only checks C2 body — alignment flags don't matter
        ok, _ = _5m_candle_quality_pass(self.sym, b1=2.0, b2=0.5, a1=False, a2=False)
        assert ok


# ─── EURUSD ────────────────────────────────────────────────────────────────

class TestEURUSD:
    sym = "EURUSD"

    def test_pass_clean_signal(self):
        ok, _ = _5m_candle_quality_pass(self.sym, b1=0.4, b2=0.4, a1=True, a2=True)
        assert ok

    def test_block_c1_medium(self):
        ok, reason = _5m_candle_quality_pass(self.sym, b1=0.7, b2=0.4, a1=True, a2=True)
        assert not ok
        assert "C1" in reason

    def test_block_c1_medium_upper_boundary(self):
        ok, _ = _5m_candle_quality_pass(self.sym, b1=1.49, b2=0.4, a1=True, a2=True)
        assert not ok

    def test_pass_c1_above_medium(self):
        ok, _ = _5m_candle_quality_pass(self.sym, b1=1.5, b2=0.4, a1=True, a2=True)
        assert ok

    def test_block_c2_medium(self):
        ok, reason = _5m_candle_quality_pass(self.sym, b1=0.4, b2=1.0, a1=True, a2=True)
        assert not ok
        assert "C2" in reason

    def test_block_c2_medium_lower_boundary(self):
        ok, _ = _5m_candle_quality_pass(self.sym, b1=0.4, b2=0.7, a1=True, a2=True)
        assert not ok

    def test_block_counter_aligned(self):
        # C1 counter (a1=False), C2 aligned (a2=True) → block
        ok, reason = _5m_candle_quality_pass(self.sym, b1=0.4, b2=0.4, a1=False, a2=True)
        assert not ok
        assert "counter" in reason.lower()

    def test_pass_aligned_aligned(self):
        ok, _ = _5m_candle_quality_pass(self.sym, b1=0.4, b2=0.4, a1=True, a2=True)
        assert ok

    def test_pass_aligned_counter(self):
        # C1 aligned, C2 counter — not in the block list
        ok, _ = _5m_candle_quality_pass(self.sym, b1=0.4, b2=0.4, a1=True, a2=False)
        assert ok

    def test_pass_counter_counter(self):
        # Both counter — not in the block list for EURUSD
        ok, _ = _5m_candle_quality_pass(self.sym, b1=0.4, b2=0.4, a1=False, a2=False)
        assert ok


# ─── EURJPY (same rules as EURUSD) ─────────────────────────────────────────

class TestEURJPY:
    sym = "EURJPY"

    def test_pass_clean_signal(self):
        ok, _ = _5m_candle_quality_pass(self.sym, b1=0.4, b2=0.4, a1=True, a2=True)
        assert ok

    def test_block_c1_medium(self):
        ok, _ = _5m_candle_quality_pass(self.sym, b1=0.8, b2=0.4, a1=True, a2=True)
        assert not ok

    def test_block_c2_medium(self):
        ok, _ = _5m_candle_quality_pass(self.sym, b1=0.4, b2=0.9, a1=True, a2=True)
        assert not ok

    def test_block_counter_aligned(self):
        ok, _ = _5m_candle_quality_pass(self.sym, b1=0.4, b2=0.4, a1=False, a2=True)
        assert not ok

    def test_pass_large_c1_aligned(self):
        ok, _ = _5m_candle_quality_pass(self.sym, b1=2.0, b2=0.4, a1=True, a2=True)
        assert ok

    def test_pass_large_c2_aligned(self):
        ok, _ = _5m_candle_quality_pass(self.sym, b1=0.4, b2=2.0, a1=True, a2=True)
        assert ok


# ─── USDCHF ────────────────────────────────────────────────────────────────

class TestUSDCHF:
    sym = "USDCHF"

    def test_pass_aligned_aligned(self):
        ok, _ = _5m_candle_quality_pass(self.sym, b1=0.5, b2=0.5, a1=True, a2=True)
        assert ok

    def test_block_c1_counter(self):
        ok, reason = _5m_candle_quality_pass(self.sym, b1=0.5, b2=0.5, a1=False, a2=True)
        assert not ok
        assert "C1" in reason or "aligned" in reason.lower()

    def test_block_c2_counter(self):
        ok, reason = _5m_candle_quality_pass(self.sym, b1=0.5, b2=0.5, a1=True, a2=False)
        assert not ok
        assert "C2" in reason or "aligned" in reason.lower()

    def test_block_both_counter(self):
        ok, _ = _5m_candle_quality_pass(self.sym, b1=0.5, b2=0.5, a1=False, a2=False)
        assert not ok

    def test_pass_regardless_of_body_size(self):
        # Any body size is fine for USDCHF as long as both aligned
        ok, _ = _5m_candle_quality_pass(self.sym, b1=3.0, b2=3.0, a1=True, a2=True)
        assert ok

    def test_block_large_body_counter(self):
        ok, _ = _5m_candle_quality_pass(self.sym, b1=3.0, b2=0.5, a1=False, a2=True)
        assert not ok


# ─── NZDUSD (no filter) ────────────────────────────────────────────────────

class TestNZDUSD:
    sym = "NZDUSD"

    def test_always_pass_aligned(self):
        ok, _ = _5m_candle_quality_pass(self.sym, b1=0.5, b2=0.5, a1=True, a2=True)
        assert ok

    def test_always_pass_counter(self):
        ok, _ = _5m_candle_quality_pass(self.sym, b1=0.5, b2=0.5, a1=False, a2=False)
        assert ok

    def test_always_pass_large_bodies(self):
        ok, _ = _5m_candle_quality_pass(self.sym, b1=5.0, b2=5.0, a1=False, a2=True)
        assert ok

    def test_always_pass_zero_bodies(self):
        ok, _ = _5m_candle_quality_pass(self.sym, b1=0.0, b2=0.0, a1=True, a2=False)
        assert ok


# ─── Unknown symbol fallthrough ─────────────────────────────────────────────

class TestUnknownSymbol:
    def test_unknown_always_passes(self):
        ok, _ = _5m_candle_quality_pass("GBPUSD", b1=0.5, b2=0.5, a1=True, a2=True)
        assert ok

    def test_unknown_counter_passes(self):
        ok, _ = _5m_candle_quality_pass("USDJPY", b1=2.0, b2=2.0, a1=False, a2=False)
        assert ok
