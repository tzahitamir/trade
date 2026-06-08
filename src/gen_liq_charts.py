"""Generate candlestick chart images for LIQ sweep strategy examples."""
import sys, os, sqlite3
from datetime import datetime, timezone

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

sys.path.insert(0, '.')
from analysis.smc_analyzer import SMCAnalyzer
from db.local_db import LocalDB

OUT_DIR = '/home/tzahi/repo/trade/local_dev'
DB_PATH = 'data/trade.db'


def load_candles(db, symbol):
    return db.query_recent(symbol, '30m', limit=20000)


def compute_outcome(candles, idx, direction, atr):
    bullish = direction == 'bullish'
    entry   = candles[idx]['close']
    sl      = (entry - 0.7 * atr) if bullish else (entry + 0.7 * atr)
    risk    = abs(entry - sl) or atr * 0.5
    tp      = (entry + 3.0 * risk) if bullish else (entry - 3.0 * risk)

    # candles are newest-first, so lower indices = newer
    lookahead = list(reversed(candles[max(0, idx - 101): idx]))  # chrono order
    outcome = 'OPEN'
    for bar in lookahead:
        if bullish:
            if bar['low'] <= sl:   outcome = 'LOSS'; break
            if bar['high'] >= tp:  outcome = 'WIN';  break
        else:
            if bar['high'] >= sl:  outcome = 'LOSS'; break
            if bar['low'] <= tp:   outcome = 'WIN';  break
    return entry, sl, tp, outcome


def plot_chart(candles, idx, sig, entry, sl, tp, outcome, out_path):
    symbol    = sig['symbol']
    direction = sig['direction']
    pool_type = sig['pool_type']
    pool_level= sig['broken_level']
    sweep_atr = sig['fvg_size_atr']
    wick_pct  = sig['rejection_wick_pct']
    bts       = sig['breakout_ts']
    atr       = abs(entry - sl) / 0.7

    dt_label = datetime.fromtimestamp(bts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
    bullish  = direction == 'bullish'

    # 25 candles before (context), sweep candle, 50 candles after
    before = list(reversed(candles[idx + 1: idx + 26]))  # chrono
    after  = list(reversed(candles[max(0, idx - 50): idx]))  # chrono
    display = before + [candles[idx]] + after
    sweep_pos = len(before)
    n = len(display)

    fig, ax = plt.subplots(figsize=(14, 7))
    fig.patch.set_facecolor('#0d1117')
    ax.set_facecolor('#0d1117')

    UP    = '#26a69a'
    DOWN  = '#ef5350'
    POOL  = '#ffeb3b'
    ENT   = '#29b6f6'
    SLC   = '#ef5350'
    TPC   = '#66bb6a'

    for i, c in enumerate(display):
        o, h, l, cl = c['open'], c['high'], c['low'], c['close']
        color = UP if cl >= o else DOWN
        ax.plot([i, i], [l, h], color=color, linewidth=1.2)
        body_lo = min(o, cl)
        body_hi = max(o, cl)
        height  = max(body_hi - body_lo, (h - l) * 0.005)
        ax.add_patch(plt.Rectangle(
            (i - 0.35, body_lo), 0.7, height,
            facecolor=color, edgecolor=color, alpha=1.0, zorder=3
        ))

    # Highlight sweep candle
    ax.axvspan(sweep_pos - 0.5, sweep_pos + 0.5, color='#ffeb3b', alpha=0.10, zorder=1)

    # Pool level
    ax.hlines(pool_level, 0, n - 1, colors=POOL, linewidth=1.5, linestyle='--', alpha=0.9, zorder=5)
    ax.text(n - 0.3, pool_level, f' {pool_type}', color=POOL, fontsize=8, va='center', fontweight='bold')

    # Entry / SL / TP from sweep candle onward
    ax.hlines(entry, sweep_pos, n - 1, colors=ENT, linewidth=1.5, linestyle='-', alpha=0.9, zorder=5)
    ax.hlines(sl,    sweep_pos, n - 1, colors=SLC, linewidth=1.2, linestyle=':', alpha=0.9, zorder=5)
    ax.hlines(tp,    sweep_pos, n - 1, colors=TPC, linewidth=1.2, linestyle=':', alpha=0.9, zorder=5)

    ax.text(sweep_pos + 0.5, entry, f' Entry {entry:.5f}',             color=ENT, fontsize=8, va='bottom')
    ax.text(sweep_pos + 0.5, sl,    f' SL {sl:.5f}  (−0.7 ATR)',       color=SLC, fontsize=8, va='top')
    ax.text(sweep_pos + 0.5, tp,    f' TP {tp:.5f}  (3R={3*abs(entry-sl):.5f})', color=TPC, fontsize=8, va='bottom')

    # Outcome badge
    oc_color = '#66bb6a' if outcome == 'WIN' else '#ef5350'
    ax.text(0.98, 0.96, outcome, transform=ax.transAxes,
            color=oc_color, fontsize=22, fontweight='bold', ha='right', va='top',
            bbox=dict(boxstyle='round,pad=0.3', facecolor='#1a1a2e', edgecolor=oc_color, alpha=0.9))

    # x-axis ticks every 5 candles
    tick_pos = list(range(0, n, 5))
    tick_lab = [datetime.fromtimestamp(display[i]['timestamp'], tz=timezone.utc).strftime('%m/%d %H:%M')
                if i < n else '' for i in tick_pos]
    ax.set_xticks(tick_pos)
    ax.set_xticklabels(tick_lab, color='#aaaaaa', fontsize=7, rotation=30, ha='right')
    ax.tick_params(colors='#555555')
    ax.yaxis.tick_right()
    ax.yaxis.set_tick_params(labelcolor='#aaaaaa', labelsize=8)
    ax.grid(axis='y', color='#1e2730', linewidth=0.5)
    ax.set_xlim(-1, n)

    legend_items = [
        Line2D([0], [0], color=POOL, linestyle='--', linewidth=1.5, label=f'{pool_type} level = {pool_level:.5f}'),
        Line2D([0], [0], color=ENT,  linewidth=1.5,  label=f'Entry = {entry:.5f}'),
        Line2D([0], [0], color=SLC,  linestyle=':',  linewidth=1.2, label=f'SL = {sl:.5f}  (entry {"−" if bullish else "+"} 0.7 ATR)'),
        Line2D([0], [0], color=TPC,  linestyle=':',  linewidth=1.2, label=f'TP = {tp:.5f}  (3R)'),
    ]
    ax.legend(handles=legend_items, loc='upper left', framealpha=0.3,
              facecolor='#1a1a2e', edgecolor='#333333', fontsize=8, labelcolor='white')

    info = f"Sweep {sweep_atr:.2f} ATR  |  Wick {wick_pct:.0%}  |  Risk = {abs(entry - sl):.5f}  |  ATR = {atr:.5f}"
    ax.set_title(
        f"{symbol} 30m  ·  {'BULLISH' if bullish else 'BEARISH'} {pool_type} Sweep  ·  {dt_label} UTC\n{info}",
        color='white', fontsize=11, pad=10
    )
    for spine in ax.spines.values():
        spine.set_edgecolor('#333333')

    plt.tight_layout()
    plt.savefig(out_path, dpi=130, bbox_inches='tight', facecolor='#0d1117')
    plt.close()
    print(f"  Saved: {os.path.basename(out_path)}")


def main():
    db   = LocalDB(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    analyzer = SMCAnalyzer()

    run_id = conn.execute("SELECT MAX(scan_run_id) FROM raw_signals WHERE strategy='LIQ30m'").fetchone()[0]
    rows = conn.execute('''
        SELECT symbol, breakout_ts, direction, broken_level, fvg_size_atr, rejection_wick_pct, pool_type
        FROM raw_signals
        WHERE strategy='LIQ30m' AND scan_run_id=?
          AND pool_type IN ('PDH','PDL')
          AND hour IN (13,14,15)
          AND fvg_size_atr >= 0.2
          AND htf_bias IS NOT NULL AND htf_bias = direction
          AND symbol IN ('EURUSD','USDCAD')
        ORDER BY breakout_ts
    ''', (run_id,)).fetchall()

    col_names = ['symbol', 'breakout_ts', 'direction', 'broken_level', 'fvg_size_atr', 'rejection_wick_pct', 'pool_type']
    all_sigs = [dict(zip(col_names, r)) for r in rows]
    print(f"Total filtered signals: {len(all_sigs)}")

    # Load candle cache
    cache = {}
    for sym in ('EURUSD', 'USDCAD'):
        cache[sym] = db.query_recent(sym, '30m', limit=20000)
        print(f"  {sym}: {len(cache[sym])} candles loaded")

    # Evaluate outcomes
    evaluated = []
    for sig in all_sigs:
        sym   = sig['symbol']
        bts   = sig['breakout_ts']
        candles = cache[sym]
        idx   = next((i for i, c in enumerate(candles) if c['timestamp'] == bts), None)
        if idx is None:
            continue
        det   = candles[idx: idx + 50]
        if len(det) < 15:
            continue
        atr = analyzer.calculate_atr(det)
        if not atr:
            continue
        entry, sl, tp, outcome = compute_outcome(candles, idx, sig['direction'], atr)
        evaluated.append({**sig, 'idx': idx, 'atr': atr, 'entry': entry, 'sl': sl, 'tp': tp, 'outcome': outcome})

    wins_eu  = [r for r in evaluated if r['symbol'] == 'EURUSD' and r['outcome'] == 'WIN']
    wins_ca  = [r for r in evaluated if r['symbol'] == 'USDCAD' and r['outcome'] == 'WIN']
    loss_eu  = [r for r in evaluated if r['symbol'] == 'EURUSD' and r['outcome'] == 'LOSS']
    loss_ca  = [r for r in evaluated if r['symbol'] == 'USDCAD' and r['outcome'] == 'LOSS']

    print(f"WIN  EURUSD: {len(wins_eu)}  USDCAD: {len(wins_ca)}")
    print(f"LOSS EURUSD: {len(loss_eu)}  USDCAD: {len(loss_ca)}")

    # Pick 3 wins (prefer higher wick_pct = cleaner setup), 2 losses
    wins_eu_sorted  = sorted(wins_eu,  key=lambda r: r['rejection_wick_pct'], reverse=True)
    wins_ca_sorted  = sorted(wins_ca,  key=lambda r: r['rejection_wick_pct'], reverse=True)

    picks = wins_eu_sorted[:2] + wins_ca_sorted[:1] + loss_eu[:1] + loss_ca[:1]
    print(f"Generating {len(picks)} charts...")

    for sig in picks:
        sym     = sig['symbol']
        bts     = sig['breakout_ts']
        outcome = sig['outcome']
        direction = sig['direction']
        pool_type = sig['pool_type']
        dt = datetime.fromtimestamp(bts, tz=timezone.utc).strftime('%Y-%m-%d_%H-%M')
        fname = f"liq_{outcome}_{sym}_{direction[:4].upper()}_{pool_type}_{dt}.png"
        out_path = os.path.join(OUT_DIR, fname)
        candles  = cache[sym]
        plot_chart(candles, sig['idx'], sig, sig['entry'], sig['sl'], sig['tp'], outcome, out_path)

    print("Done.")


if __name__ == '__main__':
    main()
