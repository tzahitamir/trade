"""
Bollinger Band scanner — two strategies:

bollinger_weekly (Friday after US close):
  Signal: weekly close ≤2% above lower BB (20w, 2σ) + 2-week MA crosses above 4-week MA
  Exit:   negative MA cross while close > mid band (dynamic exit, check each Friday)
  SL:     lower BB at entry, fixed
  Stats:  WR=34.6%, avgW=+13.3%, avgL=-1.2%, EV=+3.84%/trade, ~6 weeks hold

bollinger_earnings_gap (Mon-Fri after US close):
  Signal: daily close ≤2% above lower BB (20d, 2σ) + 2MA crosses above 4MA
           + earnings gap ≥7% within last 30 calendar days
  Exit:   upper BB at entry, fixed (TP)
  SL:     lower BB at entry, fixed (hard intraday stop)
  Stats:  WR=26.2%, avgW=+15.9%, avgL=-1.3%, EV=+3.22%/trade, ~17 days hold
"""

import logging
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from analysis.weekly_retest_scanner import get_universe

_log = logging.getLogger(__name__)

_CACHE_DIR = Path("data/bollinger_cache")

BB_PERIOD     = 20
BB_STD        = 2.0
MA_FAST       = 2
MA_SLOW       = 4
PROXIMITY_PCT = 2.0

EARNINGS_GAP_PCT  = 7.0
EARNINGS_LOOKBACK = 30


# ── Core signal detection ─────────────────────────────────────────────────────

def _bb_ma_cross(closes: np.ndarray) -> dict | None:
    """
    Check if the last bar in *closes* has all of:
      - close within PROXIMITY_PCT% above lower BB (or below it)
      - 2-bar MA just crossed above 4-bar MA (previous bar: 2MA ≤ 4MA, current: 2MA > 4MA)

    Returns a dict with signal levels, or None if no signal.
    """
    n = len(closes)
    if n < BB_PERIOD + MA_SLOW + 2:
        return None

    c    = pd.Series(closes)
    mid  = c.rolling(BB_PERIOD, min_periods=BB_PERIOD).mean()
    std  = c.rolling(BB_PERIOD, min_periods=BB_PERIOD).std()
    lower  = (mid - BB_STD * std).values
    upper  = (mid + BB_STD * std).values
    mid_v  = mid.values
    ma_f   = c.rolling(MA_FAST, min_periods=MA_FAST).mean().values
    ma_s   = c.rolling(MA_SLOW, min_periods=MA_SLOW).mean().values

    i = n - 1
    lb, ub, mb = lower[i], upper[i], mid_v[i]
    if any(np.isnan(x) for x in [lb, ub, mb, ma_f[i], ma_s[i], ma_f[i-1], ma_s[i-1]]):
        return None

    cl = float(closes[i])

    if cl > lb * (1 + PROXIMITY_PCT / 100):
        return None

    if not (ma_f[i-1] <= ma_s[i-1] and ma_f[i] > ma_s[i]):
        return None

    return {
        "close":            round(cl, 2),
        "lower_bb":         round(lb, 2),
        "mid_bb":           round(mb, 2),
        "upper_bb":         round(ub, 2),
        "pct_above_lower":  round((cl - lb) / lb * 100, 2),
    }


# ── Earnings gap detection ────────────────────────────────────────────────────

def _get_earnings_gap(ticker: str, closes: pd.Series) -> tuple[date | None, float]:
    """
    Fetch earnings dates for *ticker* and check whether any caused a gap-down
    ≥ EARNINGS_GAP_PCT within the last EARNINGS_LOOKBACK calendar days.

    Only called for tickers that already passed the BB+MA cross filter.
    Returns (gap_date, gap_pct) or (None, 0.0).
    """
    today   = date.today()
    cutoff  = today - timedelta(days=EARNINGS_LOOKBACK)
    idx_dates = closes.index.date

    try:
        ed = yf.Ticker(ticker).earnings_dates
        if ed is None or len(ed) == 0:
            return None, 0.0
        raw_idx = ed.index
        if raw_idx.tz is not None:
            raw_idx = raw_idx.tz_localize(None)
        past = sorted([d for d in raw_idx.date if d <= today], reverse=True)
    except Exception:
        return None, 0.0

    for ed_date in past:
        if ed_date < cutoff:
            break
        for offset in range(3):
            check = ed_date + timedelta(days=offset)
            positions = np.where(idx_dates == check)[0]
            if len(positions) == 0:
                continue
            p = int(positions[0])
            if p == 0:
                continue
            gap_pct = (closes.iloc[p] / closes.iloc[p - 1] - 1) * 100
            if gap_pct <= -EARNINGS_GAP_PCT:
                return check, round(gap_pct, 1)
            break

    return None, 0.0


# ── Public scan functions ─────────────────────────────────────────────────────

def bollinger_weekly_scan() -> list[dict]:
    """
    Scan S&P 500 + Nasdaq 100 on weekly bars for the bollinger_weekly setup.
    Run Friday after US market close (21:00 UTC) so the weekly bar is complete.
    Returns list of setup dicts sorted by proximity to lower BB (closest first).
    """
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tickers = get_universe()
    setups: list[dict] = []
    BATCH = 80

    for start in range(0, len(tickers), BATCH):
        batch = tickers[start : start + BATCH]
        try:
            raw = yf.download(
                batch,
                interval="1wk",
                period="5y",
                auto_adjust=True,
                group_by="ticker",
                progress=False,
                threads=True,
            )
            for tk in batch:
                try:
                    df = raw[tk] if len(batch) > 1 else raw
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    df = df[["Close"]].dropna()
                    if len(df) < BB_PERIOD + MA_SLOW + 2:
                        continue
                    sig = _bb_ma_cross(df["Close"].values)
                    if sig is None:
                        continue
                    bar_date = df.index[-1].date() if hasattr(df.index[-1], "date") else df.index[-1]
                    setups.append({"ticker": tk, "bar_date": bar_date, **sig})
                except Exception:
                    pass
        except Exception as exc:
            _log.warning("[BOLLINGER_WEEKLY] batch error: %s", exc)
        time.sleep(0.3)

    setups.sort(key=lambda x: x["pct_above_lower"])
    _log.info("[BOLLINGER_WEEKLY] %d setup(s) found", len(setups))
    return setups


def bollinger_earnings_gap_scan() -> list[dict]:
    """
    Scan S&P 500 + Nasdaq 100 on daily bars for the bollinger_earnings_gap setup.
    Run Mon-Fri after US market close.
    Returns list of setup dicts sorted by proximity to lower BB (closest first).
    """
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tickers = get_universe()
    setups: list[dict] = []
    today   = date.today()
    BATCH   = 80

    for start in range(0, len(tickers), BATCH):
        batch = tickers[start : start + BATCH]
        try:
            raw = yf.download(
                batch,
                interval="1d",
                period="3mo",
                auto_adjust=True,
                group_by="ticker",
                progress=False,
                threads=True,
            )
            for tk in batch:
                try:
                    df = raw[tk] if len(batch) > 1 else raw
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    df = df[["Close"]].dropna()
                    if len(df) < BB_PERIOD + MA_SLOW + 2:
                        continue
                    sig = _bb_ma_cross(df["Close"].values)
                    if sig is None:
                        continue
                    gap_date, gap_pct = _get_earnings_gap(tk, df["Close"])
                    if gap_date is None:
                        continue
                    bar_date = df.index[-1].date() if hasattr(df.index[-1], "date") else df.index[-1]
                    days_since = (today - gap_date).days
                    setups.append({
                        "ticker":        tk,
                        "bar_date":      bar_date,
                        "gap_date":      gap_date,
                        "gap_pct":       gap_pct,
                        "days_since_gap": days_since,
                        **sig,
                    })
                except Exception:
                    pass
        except Exception as exc:
            _log.warning("[BOLLINGER_EG] batch error: %s", exc)
        time.sleep(0.3)

    setups.sort(key=lambda x: x["pct_above_lower"])
    _log.info("[BOLLINGER_EG] %d setup(s) found", len(setups))
    return setups


# ── Alert message formatters ──────────────────────────────────────────────────

def format_bollinger_weekly(setups: list[dict]) -> str:
    today_str = date.today().strftime("%b %d %Y")
    n = len(setups)
    lines = [
        f"<b>📊 Bollinger Weekly — {today_str}</b>",
        f"<i>Strategy: bollinger_weekly | {n} setup{'s' if n != 1 else ''} found</i>",
        "<i>Weekly close ≤2% above lower BB (20w, 2σ) + 2MA crossed above 4MA</i>",
        "",
    ]

    if not setups:
        lines += [
            "No bollinger_weekly setups this week.",
            "",
            "<i>Stats: 34.6% WR | avg win +13.3% | avg loss -1.2% | EV +3.84%/trade</i>",
        ]
        return "\n".join(lines)

    for s in setups:
        tk   = s["ticker"]
        cl   = s["close"]
        lb   = s["lower_bb"]
        mb   = s["mid_bb"]
        ub   = s["upper_bb"]
        pct  = s["pct_above_lower"]

        risk_pct   = round((cl - lb) / cl * 100, 1) if cl != 0 else 0.0
        reward_pct = round((ub - cl) / cl * 100, 1) if cl != 0 else 0.0
        prox_str   = f"{pct:.1f}% above" if pct >= 0 else f"{abs(pct):.1f}% below"

        lines += [
            f"<b>{tk}</b>  |  {prox_str} lower BB  |  2MA crossed above 4MA",
            f"  📈 Close: ${cl:.2f}  |  Lower BB: ${lb:.2f}  |  Mid: ${mb:.2f}  |  Upper: ${ub:.2f}",
            f"  🛑 SL: ${lb:.2f}  (lower BB fixed — risk {risk_pct:.1f}%)",
            f"  🎯 TP: exit on negative MA cross above mid band  |  Upper BB ${ub:.2f} (+{reward_pct:.1f}%)",
            f"  ⏱ Avg hold ~6 weeks — check each Friday close",
            "",
        ]

    lines.append(
        "<i>bollinger_weekly: 34.6% WR | avg win +13.3% | avg loss -1.2% | EV +3.84%/trade</i>"
    )
    return "\n".join(lines)


def format_bollinger_earnings_gap(setups: list[dict]) -> str:
    today_str = date.today().strftime("%b %d %Y")
    n = len(setups)
    lines = [
        f"<b>🔻 Bollinger Earnings Gap — {today_str}</b>",
        f"<i>Strategy: bollinger_earnings_gap | {n} setup{'s' if n != 1 else ''} found</i>",
        "<i>Daily close ≤2% above lower BB + 2MA cross + earnings gap ≥7% within 30 days</i>",
        "",
    ]

    if not setups:
        lines += [
            "No bollinger_earnings_gap setups today.",
            "",
            "<i>Stats: 26.2% WR | avg win +15.9% | avg loss -1.3% | EV +3.22%/trade</i>",
        ]
        return "\n".join(lines)

    for s in setups:
        tk    = s["ticker"]
        cl    = s["close"]
        lb    = s["lower_bb"]
        mb    = s["mid_bb"]
        ub    = s["upper_bb"]
        pct   = s["pct_above_lower"]
        gdate = s["gap_date"]
        gpct  = s["gap_pct"]
        days  = s["days_since_gap"]

        risk_pct   = round((cl - lb) / cl * 100, 1) if cl != 0 else 0.0
        reward_pct = round((ub - cl) / cl * 100, 1) if cl != 0 else 0.0
        prox_str   = f"{pct:.1f}% above" if pct >= 0 else f"{abs(pct):.1f}% below"
        gap_label  = gdate.strftime("%b %d") if hasattr(gdate, "strftime") else str(gdate)

        lines += [
            f"<b>{tk}</b>  |  {prox_str} lower BB  |  2MA crossed above 4MA",
            f"  📉 Earnings gap: {gpct:.1f}% on {gap_label} ({days}d ago)",
            f"  📈 Close: ${cl:.2f}  |  Lower BB: ${lb:.2f}  |  Upper BB: ${ub:.2f}",
            f"  🛑 SL: ${lb:.2f}  (lower BB at entry fixed — risk {risk_pct:.1f}%)",
            f"  🎯 TP: ${ub:.2f}  (upper BB at entry fixed — +{reward_pct:.1f}%)",
            f"  ⏱ Avg hold ~17 days — set limit and hold",
            "",
        ]

    lines.append(
        "<i>bollinger_earnings_gap: 26.2% WR | avg win +15.9% | avg loss -1.3% | EV +3.22%/trade</i>"
    )
    return "\n".join(lines)
