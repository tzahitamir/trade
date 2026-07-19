"""
Weekly Break-and-Retest live scanner.

Runs every Sunday and finds S&P 500 + Nasdaq 100 stocks where:
  - A swing high broke out with ≥4 consecutive weekly closes above it (BOS confirmed)
  - Price has since pulled back to retest the level (last 4 weeks)
  - The trade has not been stopped out

SL is reported at 0.25×ATR below the swing level (practical vs the research's fixed 0.2%).
"""

import io
import json
import logging
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

_log = logging.getLogger(__name__)

# ── Parameters ─────────────────────────────────────────────────────────────────
SWING_N        = 5      # bars each side to confirm swing high
MIN_CANDLES    = 4      # consecutive closes above level to confirm BOS
ENTRY_PCT      = 0.015  # entry limit = level × 1.015 (1.5% above)
ATR_FRACTION   = 0.25   # SL = level − 0.25 × ATR14
ATR_PERIOD     = 14
RETEST_MAX_PCT = 0.02   # retest zone top = level × 1.02
MAX_BARS_HELD  = 52     # stop watching after 52 weeks
SCAN_WEEKS     = 4      # look-back window for "recent" retests

CACHE_DIR      = Path("data/weekly_cache")
WATCHLIST_PATH = Path("data/weekly_retest_watchlist.json")

# Watchlist thresholds
WATCHLIST_MAX_PCT_ABOVE = 15.0   # only watch stocks within 15% of their BOS level
WATCHLIST_ALERT_PCT     = 3.0    # alert when price ≤ level × (1 + 3%)
WATCHLIST_MIN_CONSEC    = 15     # must have held above level for ≥15 weeks
WATCHLIST_MIN_PEAK      = 15.0   # peak expansion after BOS must be ≥15%
WATCHLIST_MIN_CONSOL    = 1.0    # min close during BOS confirmation ≥1% above level
WATCHLIST_MIN_BOS_VOL   = 1.25   # BOS bar volume ≥1.25× 20w average

# ── Universe ───────────────────────────────────────────────────────────────────

def get_universe() -> list[str]:
    """S&P 500 + Nasdaq 100, deduplicated."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _get(cache_file, fetch_fn):
        cache = CACHE_DIR / cache_file
        if cache.exists() and (time.time() - cache.stat().st_mtime) < 86400 * 6:
            data = json.loads(cache.read_text())
            if data:
                return data
        tickers = fetch_fn()
        cache.write_text(json.dumps(tickers))
        return tickers

    def _sp500():
        r = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers={"User-Agent": "Mozilla/5.0 (research script)"}, timeout=15,
        )
        r.raise_for_status()
        return pd.read_html(io.StringIO(r.text))[0]["Symbol"].str.replace(".", "-", regex=False).tolist()

    def _nasdaq100():
        r = requests.get(
            "https://slickcharts.com/nasdaq100",
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}, timeout=15,
        )
        r.raise_for_status()
        return pd.read_html(io.StringIO(r.text))[0]["Symbol"].str.replace(".", "-", regex=False).tolist()

    sp500   = _get("sp500_tickers.json",   _sp500)
    nasdaq  = _get("nasdaq100_tickers.json", _nasdaq100)
    combined = list(dict.fromkeys(sp500 + nasdaq))   # preserve order, deduplicate
    _log.info("[WEEKLY_SCAN] Universe: %d tickers (S&P500 + Nasdaq100)", len(combined))
    return combined


# ── Data fetching ──────────────────────────────────────────────────────────────

def _fetch_weekly(ticker: str) -> pd.DataFrame | None:
    cache = CACHE_DIR / f"{ticker}_weekly.csv"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if cache.exists() and (time.time() - cache.stat().st_mtime) < 86400:
        try:
            return pd.read_csv(cache, index_col=0, parse_dates=True)
        except Exception:
            pass
    try:
        df = yf.download(ticker, interval="1wk", period="max", progress=False, auto_adjust=True)
        if df.empty or len(df) < 60:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
        df.to_csv(cache)
        return df
    except Exception as e:
        _log.debug("[WEEKLY_SCAN] fetch error %s: %s", ticker, e)
        return None


# ── Setup detection ────────────────────────────────────────────────────────────

def _find_setups(df: pd.DataFrame, ticker: str) -> list[dict]:
    highs  = df["High"].values
    lows   = df["Low"].values
    closes = df["Close"].values
    dates  = df.index
    n      = len(df)
    setups = []
    used_levels: list[float] = []

    atr14 = pd.Series(highs - lows).rolling(ATR_PERIOD, min_periods=ATR_PERIOD).mean().values

    for i in range(SWING_N, n - SWING_N):
        level  = highs[i]
        window = highs[i - SWING_N : i + SWING_N + 1]
        if level < max(window):
            continue
        if any(abs(level - ul) / ul < 0.01 for ul in used_levels):
            continue

        bos_idx = None
        for j in range(i + 1, n):
            if closes[j] > level:
                bos_idx = j
                break
        if bos_idx is None:
            continue

        consec = 0
        for k in range(bos_idx, min(bos_idx + MIN_CANDLES + 30, n)):
            if closes[k] > level:
                consec += 1
                if consec >= MIN_CANDLES:
                    break
            else:
                break
        if consec < MIN_CANDLES:
            continue

        confirm_end = bos_idx + consec - 1
        entry       = level * (1 + ENTRY_PCT)

        for m in range(confirm_end + 1, n):
            if closes[m] < level:
                break
            if lows[m] <= level * (1 + RETEST_MAX_PCT):
                used_levels.append(level)
                atr_m = atr14[m] if not pd.isna(atr14[m]) else (highs[m] - lows[m])
                sl    = level - ATR_FRACTION * atr_m
                risk  = entry - sl
                if risk <= 0:
                    break

                # Simulate from bar after retest
                outcome   = "TIMEOUT"
                bars_held = 0
                for p in range(m + 1, min(m + MAX_BARS_HELD + 1, n)):
                    bars_held += 1
                    if lows[p] <= sl:
                        outcome = "SL"
                        break

                cur_price = float(closes[-1]) if m < n - 1 else None

                setups.append({
                    "ticker":      ticker,
                    "level":       round(level, 4),
                    "entry":       round(entry, 4),
                    "sl":          round(sl, 4),
                    "risk_pct":    round(risk / entry * 100, 2),
                    "retest_date": str(dates[m].date()),
                    "confirm_end": str(dates[confirm_end].date()),
                    "outcome":     outcome,
                    "bars_held":   bars_held,
                    "cur_price":   round(cur_price, 4) if cur_price else None,
                })
                break

    return setups


# ── Watchlist ──────────────────────────────────────────────────────────────────

def _pre_score(consec_total: int, peak_gain_pct: float,
               consol_min_pct: float, bos_vol_ratio: float) -> int:
    """Score 0-5: pre-retest criteria knowable at scan time."""
    pts = 0
    if consec_total >= 6:               pts += 1
    if consec_total >= WATCHLIST_MIN_CONSEC: pts += 1
    if peak_gain_pct >= WATCHLIST_MIN_PEAK:  pts += 1
    if consol_min_pct >= WATCHLIST_MIN_CONSOL: pts += 1
    if bos_vol_ratio >= WATCHLIST_MIN_BOS_VOL: pts += 1
    return pts


def _find_watchlist_candidate(df: pd.DataFrame, ticker: str) -> dict | None:
    """
    Find the most recent confirmed-but-not-yet-retested BOS structure for ticker.
    Requirements:
      - ≥ MIN_CANDLES consecutive closes above level after BOS (confirmed)
      - ALL closes from BOS bar to today remain above level (unbroken)
      - No weekly low has touched the 3% retest zone since confirmation
      - Current price within WATCHLIST_MAX_PCT_ABOVE % of level
    Returns the most recently confirmed candidate, or None.
    """
    highs  = df["High"].values
    lows   = df["Low"].values
    closes = df["Close"].values
    opens  = df["Open"].values
    vols   = df["Volume"].values.astype(float)
    dates  = df.index
    n      = len(df)

    atr14 = pd.Series(highs - lows).rolling(ATR_PERIOD, min_periods=ATR_PERIOD).mean().values
    vol20 = pd.Series(vols).rolling(20, min_periods=5).mean().values

    best: dict | None = None
    used_levels: list[float] = []

    for i in range(SWING_N, n - SWING_N):
        level  = highs[i]
        window = highs[i - SWING_N : i + SWING_N + 1]
        if level < max(window):
            continue
        if any(abs(level - ul) / ul < 0.01 for ul in used_levels):
            continue

        # Find BOS bar
        bos_idx = None
        for j in range(i + 1, n):
            if closes[j] > level:
                bos_idx = j
                break
        if bos_idx is None:
            continue

        # Require ≥ MIN_CANDLES consecutive closes above level
        consec = 0
        for k in range(bos_idx, min(bos_idx + MIN_CANDLES + 30, n)):
            if closes[k] > level:
                consec += 1
                if consec >= MIN_CANDLES:
                    break
            else:
                break
        if consec < MIN_CANDLES:
            continue

        confirm_end = bos_idx + consec - 1

        # Verify unbroken: no close below level AND no retest low since confirmation
        broken = False
        retested = False
        for k in range(bos_idx, n):
            if closes[k] <= level:
                broken = True
                break
            if k > confirm_end and lows[k] <= level * (1 + WATCHLIST_ALERT_PCT / 100):
                retested = True
                break
        if broken or retested:
            used_levels.append(level)
            continue

        # ── Compute watchlist features ──
        consec_total = n - 1 - bos_idx

        peak_high     = float(highs[bos_idx:].max())
        peak_gain_pct = round((peak_high / level - 1) * 100, 1)

        # peak bar index (post-confirm phase only)
        post_confirm_slice = highs[confirm_end:]
        peak_abs_idx = confirm_end + int(post_confirm_slice.argmax())

        consol_closes  = closes[bos_idx : confirm_end + 1]
        consol_min_pct = round((float(consol_closes.min()) / level - 1) * 100, 2)

        ref_vol       = (float(vol20[bos_idx - 1])
                         if bos_idx > 0 and not pd.isna(vol20[bos_idx - 1])
                         else float(vol20[bos_idx]))
        bos_vol_ratio = (round(float(vols[bos_idx]) / ref_vol, 2)
                         if ref_vol and ref_vol > 0 else 1.0)

        # Panic-sell: any pullback bar (peak → now) that is red with vol > 2× avg
        has_panic_sell = any(
            closes[p] < opens[p]
            and vols[p] > 2.0 * (float(vol20[p]) if not pd.isna(vol20[p]) else float(vols[p]))
            for p in range(peak_abs_idx, n)
        )

        atr_last = (atr14[-1] if not pd.isna(atr14[-1]) else float(highs[-1] - lows[-1]))
        current_price    = float(closes[-1])
        current_pct_above = round((current_price / level - 1) * 100, 2)

        used_levels.append(level)
        best = {
            "ticker":           ticker,
            "level":            round(float(level), 4),
            "entry":            round(level * (1 + ENTRY_PCT), 4),
            "sl":               round(level - ATR_FRACTION * atr_last, 4),
            "bos_date":         str(dates[bos_idx].date()),
            "consec_total":     consec_total,
            "peak_gain_pct":    peak_gain_pct,
            "consol_min_pct":   consol_min_pct,
            "bos_vol_ratio":    bos_vol_ratio,
            "has_panic_sell":   has_panic_sell,
            "current_pct_above": current_pct_above,
        }
        # keep searching — a later BOS (higher i) will overwrite best

    return best


def build_watchlist() -> list[dict]:
    """
    Weekly rebuild: scan universe for score-5 pre-retest BOS candidates.
    Only includes stocks currently within WATCHLIST_MAX_PCT_ABOVE % of their level.
    Saves to WATCHLIST_PATH and returns the list.
    """
    tickers = get_universe()
    _log.info("[WATCHLIST] Scanning %d tickers for score-5 pre-retest candidates...", len(tickers))

    candidates: list[dict] = []
    for ticker in tickers:
        df = _fetch_weekly(ticker)
        if df is None:
            continue
        cand = _find_watchlist_candidate(df, ticker)
        if cand is None:
            continue
        score = _pre_score(
            cand["consec_total"], cand["peak_gain_pct"],
            cand["consol_min_pct"], cand["bos_vol_ratio"],
        )
        if score < 5:
            continue
        pct = cand["current_pct_above"]
        if pct < 0 or pct > WATCHLIST_MAX_PCT_ABOVE:
            continue
        cand["pre_score"] = score
        candidates.append(cand)

    candidates.sort(key=lambda c: c["current_pct_above"])

    WATCHLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    WATCHLIST_PATH.write_text(json.dumps(candidates, indent=2))
    _log.info("[WATCHLIST] %d score-5 candidates saved to %s", len(candidates), WATCHLIST_PATH)
    return candidates


def check_watchlist_alerts() -> list[dict]:
    """
    Load saved watchlist, fetch current daily prices, return entries
    where price has dropped to within WATCHLIST_ALERT_PCT % of BOS level.
    """
    if not WATCHLIST_PATH.exists():
        return []
    watchlist: list[dict] = json.loads(WATCHLIST_PATH.read_text())
    if not watchlist:
        return []

    tickers = [e["ticker"] for e in watchlist]
    prices: dict[str, float] = {}
    try:
        raw = yf.download(tickers, period="5d", interval="1d", progress=False, auto_adjust=True)
        close_col = raw["Close"] if "Close" in raw else raw
        if isinstance(close_col, pd.Series):
            close_col = close_col.to_frame(tickers[0])
        for t in tickers:
            if t in close_col.columns:
                series = close_col[t].dropna()
                if not series.empty:
                    prices[t] = float(series.iloc[-1])
    except Exception as e:
        _log.warning("[WATCHLIST] Daily price fetch error: %s", e)

    alerts: list[dict] = []
    for entry in watchlist:
        price = prices.get(entry["ticker"])
        if price is None:
            continue
        level    = entry["level"]
        pct_now  = (price / level - 1) * 100
        if 0 <= pct_now <= WATCHLIST_ALERT_PCT:
            e = dict(entry)
            e["alert_price"]     = round(price, 4)
            e["alert_pct_above"] = round(pct_now, 2)
            alerts.append(e)
    return alerts


def format_watchlist_update(candidates: list[dict]) -> str:
    today = date.today().strftime("%b %d %Y")
    lines = [f"<b>📋 BOS Watchlist — {today}</b>",
             "Score-5 stocks with unretested BOS, approaching support\n"]

    if not candidates:
        lines.append("No score-5 candidates within 15% of support this week.")
        return "\n".join(lines)

    for c in candidates:
        pct  = c["current_pct_above"]
        icon = "🔴" if pct <= WATCHLIST_ALERT_PCT else ("🟡" if pct <= 8 else "🟢")
        panic = " | 🚨 panic-sell absorbed" if c["has_panic_sell"] else ""
        lines.append(
            f"{icon} <b>{c['ticker']}</b>  "
            f"Support ${c['level']:.2f}  |  "
            f"Now ${c['level'] * (1 + pct/100):.2f} (+{pct:.1f}%)  |  "
            f"{c['consec_total']}w  |  "
            f"Peak +{c['peak_gain_pct']:.0f}%"
            f"{panic}"
        )

    lines += [
        "",
        f"<i>{len(candidates)} stocks on watch  |  "
        f"Alert fires when price ≤ support +{WATCHLIST_ALERT_PCT:.0f}%</i>",
    ]
    return "\n".join(lines)


def format_watchlist_alert(alerts: list[dict]) -> str:
    lines = ["<b>⚡ WATCHLIST ALERT — BOS support retest in progress</b>\n"]
    for a in alerts:
        panic = "\n  🚨 Panic-sell candle absorbed during pullback" if a.get("has_panic_sell") else ""
        lines.append(
            f"<b>{a['ticker']}</b>\n"
            f"  Support: ${a['level']:.2f}  |  "
            f"Price: ${a['alert_price']:.2f} (+{a['alert_pct_above']:.1f}%)\n"
            f"  Entry limit: ${a['entry']:.2f}  |  SL: ${a['sl']:.2f}\n"
            f"  Above support {a['consec_total']} weeks since BOS {a['bos_date']}\n"
            f"  Peak expansion: +{a['peak_gain_pct']:.0f}%"
            f"{panic}\n"
        )
    return "\n".join(lines)


# ── Live scan ──────────────────────────────────────────────────────────────────

def scan() -> dict:
    """
    Returns {"pending": [...], "open": [...], "n_tickers": int, "n_failed": int}
    pending = price in zone, entry not yet triggered
    open    = entry triggered, not stopped out
    """
    tickers = get_universe()
    cutoff  = (date.today() - timedelta(weeks=SCAN_WEEKS)).isoformat()

    all_candidates = []
    n_failed = 0

    for ticker in tickers:
        df = _fetch_weekly(ticker)
        if df is None:
            n_failed += 1
            continue
        for s in _find_setups(df, ticker):
            if s["retest_date"] >= cutoff and s["outcome"] != "SL":
                all_candidates.append(s)

    # Fetch current prices in one batch call
    unique_tickers = list({s["ticker"] for s in all_candidates})
    cur_prices: dict[str, float] = {}
    if unique_tickers:
        try:
            raw = yf.download(unique_tickers, period="5d", interval="1wk",
                              progress=False, auto_adjust=True)
            closes = raw["Close"] if "Close" in raw else raw
            if isinstance(closes, pd.Series):
                closes = closes.to_frame(unique_tickers[0])
            for t in unique_tickers:
                if t in closes.columns:
                    last = closes[t].dropna()
                    if not last.empty:
                        cur_prices[t] = float(last.iloc[-1])
        except Exception as e:
            _log.warning("[WEEKLY_SCAN] batch price fetch error: %s", e)

    pending, open_ = [], []
    for s in sorted(all_candidates, key=lambda x: x["retest_date"], reverse=True):
        cur = cur_prices.get(s["ticker"])
        s["cur_price"] = round(cur, 2) if cur else None
        if cur is None:
            continue
        if cur <= s["sl"]:
            continue          # stopped out (intraweek; weekly close might disagree)
        if cur <= s["entry"]:
            pending.append(s)
        else:
            open_.append(s)

    return {
        "pending":   pending,
        "open":      open_,
        "n_tickers": len(tickers),
        "n_failed":  n_failed,
    }


# ── Telegram formatting ────────────────────────────────────────────────────────

def format_message(result: dict) -> str:
    today     = date.today().strftime("%b %d %Y")
    pending   = result["pending"]
    open_     = result["open"]
    n_tick    = result["n_tickers"]
    n_failed  = result["n_failed"]
    total     = len(pending) + len(open_)

    lines = [
        f"<b>📊 Weekly Retest Scanner — {today}</b>",
        f"S&amp;P 500 + Nasdaq 100 | {n_tick - n_failed} tickers scanned",
        f"Total live candidates: {total}",
        "",
    ]

    # ── PENDING section ──
    if pending:
        lines += [
            f"<b>🎯 PENDING ({len(pending)}) — set a buy limit</b>",
            "<i>Price is in the retest zone, entry not triggered yet</i>",
            "",
        ]
        for s in pending:
            cur  = s["cur_price"]
            dist = (cur - s["entry"]) / s["entry"] * 100
            lines += [
                f"<b>{s['ticker']}</b>  |  Level: ${s['level']:.2f}",
                f"  Entry: ${s['entry']:.2f}  |  SL: ${s['sl']:.2f}  |  Risk: {s['risk_pct']:.1f}%",
                f"  Current: ${cur:.2f}  ({dist:+.1f}% from entry)",
                f"  Retest: {s['retest_date']}  |  Confirmed: {s['confirm_end']}",
                "",
            ]
    else:
        lines += ["<b>🎯 PENDING</b> — none this week", ""]

    # ── OPEN section ──
    if open_:
        lines += [
            f"<b>✅ OPEN ({len(open_)}) — trailing stop, target 20-30%</b>",
            "<i>Entry already triggered, trade running</i>",
            "",
        ]
        for s in open_:
            cur   = s["cur_price"]
            gain  = (cur - s["entry"]) / s["entry"] * 100
            held  = (date.today() - date.fromisoformat(s["retest_date"])).days
            lines += [
                f"<b>{s['ticker']}</b>  |  Entry: ${s['entry']:.2f}  |  SL: ${s['sl']:.2f}",
                f"  Current: ${cur:.2f}  ({gain:+.1f}%)  |  ~{held}d since retest",
                "",
            ]
    else:
        lines += ["<b>✅ OPEN</b> — none this week", ""]

    lines.append("<i>SL = 0.25×ATR below swing level | Strategy: weekly BOS retest (bull)</i>")
    return "\n".join(lines)
