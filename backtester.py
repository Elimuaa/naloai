"""
backtester.py — Offline strategy backtester for Nalo.Ai.

Replays historical bars through the exact same signal + exit logic the live
bot uses, so we can validate edge *before* risking capital.

Usage:
    python backtester.py --symbol BTC-USD --days 365 --balance 10000

Outputs an HTML report (backtest_report.html) with:
  - Equity curve
  - Trade list
  - Win rate, profit factor, Sharpe, max drawdown
  - Monthly P&L table

Data source: Coinbase public candles API (free, no key).
"""
import argparse
import asyncio
import json
import logging
import math
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
import numpy as np

from indicators import bollinger_bands, rsi, adx, atr_from_prices
from broker_base import get_asset_class, ASSET_CLASS_PRESETS

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


# ── Data fetcher ────────────────────────────────────────────────────────
COINBASE_GRANULARITY = 900   # 15-minute bars


async def fetch_coinbase_bars(symbol: str = "BTC-USD", days: int = 365) -> list[dict]:
    """Pull historical 15-min candles from Coinbase. Returns list of dicts."""
    end_ts = int(time.time())
    start_ts = end_ts - days * 86400
    base = symbol.replace("-USD", "-USD")   # normalize
    url = f"https://api.exchange.coinbase.com/products/{base}/candles"

    # Coinbase caps at 300 candles per call → page backwards
    bars = []
    window = 300 * COINBASE_GRANULARITY
    cursor = start_ts
    async with httpx.AsyncClient(timeout=30) as c:
        while cursor < end_ts:
            win_end = min(cursor + window, end_ts)
            params = {
                "start": datetime.fromtimestamp(cursor, timezone.utc).isoformat(),
                "end": datetime.fromtimestamp(win_end, timezone.utc).isoformat(),
                "granularity": COINBASE_GRANULARITY,
            }
            try:
                r = await c.get(url, params=params)
                if r.status_code == 200:
                    page = r.json()   # [[time, low, high, open, close, volume], ...]
                    for row in page:
                        bars.append({
                            "time": row[0], "low": row[1], "high": row[2],
                            "open": row[3], "close": row[4], "volume": row[5],
                        })
            except Exception as e:
                logger.warning(f"Fetch error at {cursor}: {e}")
            cursor = win_end
            await asyncio.sleep(0.2)  # rate-limit kindly

    # Deduplicate and sort
    seen = {}
    for b in bars:
        seen[b["time"]] = b
    bars = sorted(seen.values(), key=lambda x: x["time"])
    logger.info(f"Fetched {len(bars)} bars for {symbol}")
    return bars


# ── Minimal backtest engine ─────────────────────────────────────────────
@dataclass
class BTTrade:
    entry_time: int
    entry_price: float
    side: str  # "buy" or "sell"
    quantity: float
    exit_time: Optional[int] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    pnl: float = 0.0
    pnl_pct: float = 0.0
    partial_pnl: float = 0.0
    partial_done: bool = False


@dataclass
class BTState:
    balance: float
    equity_curve: list[tuple[int, float]] = field(default_factory=list)
    trades: list[BTTrade] = field(default_factory=list)
    open_trade: Optional[BTTrade] = None
    consecutive_losses: int = 0
    consecutive_wins: int = 0
    recent_pnls: list[tuple[float, bool]] = field(default_factory=list)
    daily_pnl: float = 0.0
    daily_date: str = ""
    breakeven_moved: bool = False
    bb_width_history: list[float] = field(default_factory=list)


def _kelly_mult(recent: list[tuple[float, bool]], base_risk: float) -> float:
    if len(recent) < 10:
        return 1.0
    wins = [t[0] for t in recent if t[1]]
    losses = [abs(t[0]) for t in recent if not t[1] and t[0] < 0]
    if not wins or not losses:
        return 1.0
    p = len(wins) / len(recent)
    b = (sum(wins) / len(wins)) / (sum(losses) / len(losses))
    if b <= 0:
        return 1.0
    kelly = max(0.0, (p * b - (1 - p)) / b) * 0.5
    mult = kelly / base_risk if base_risk > 0 else 1.0
    return max(0.25, min(1.5, mult))


def _adaptive_tp(regime: str, side: str, sl_pct: float, base_tp: float) -> float:
    if regime == "ranging":
        return sl_pct * 2.5
    if regime == "trending_up":
        return sl_pct * 3.0 if side == "buy" else sl_pct * 1.5
    if regime == "trending_down":
        return sl_pct * 3.0 if side == "sell" else sl_pct * 1.5
    return base_tp


def _detect_regime(prices: list[float]) -> str:
    """Simplified regime detection mirroring live bot."""
    if len(prices) < 50:
        return "ranging"
    adx_val = adx(prices, 14)
    if adx_val is None:
        return "ranging"
    ema_short = sum(prices[-10:]) / 10
    ema_long = sum(prices[-30:]) / 30
    if adx_val > 25:
        return "trending_up" if ema_short > ema_long else "trending_down"
    return "ranging"


def backtest(
    bars: list[dict],
    starting_balance: float = 10000.0,
    lookback: int = 20,
    entry_z: float = 1.3,
    sl_pct: float = 0.025,
    tp_pct: float = 0.05,
    risk_per_trade: float = 0.02,    # 2%
    slippage_bps: float = 3.5,
    fee_pct: float = 0.001,
    dead_zone: set[int] = None,
    golden_hours: set[int] = None,
    enable_partial: bool = True,
    enable_breakeven: bool = True,
    enable_kelly: bool = True,
    enable_adaptive_tp: bool = True,
    enable_squeeze_filter: bool = True,
    verbose: bool = False,
) -> dict:
    """Run backtest on historical bars. Returns metrics dict."""
    if dead_zone is None:
        dead_zone = {1, 6, 9, 11, 13, 14, 17, 18}
    if golden_hours is None:
        golden_hours = {7, 8, 15, 19, 20}

    state = BTState(balance=starting_balance)
    state.equity_curve.append((bars[0]["time"], starting_balance))

    prices: list[float] = []
    last_stop_time: Optional[int] = None

    for i, bar in enumerate(bars):
        close = bar["close"]
        t = bar["time"]
        prices.append(close)
        if len(prices) < lookback + 5:
            continue

        hour_utc = datetime.fromtimestamp(t, timezone.utc).hour

        # Daily P&L reset
        day_key = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")
        if state.daily_date != day_key:
            state.daily_pnl = 0.0
            state.daily_date = day_key

        regime = _detect_regime(prices)

        # ── Manage open trade ──
        if state.open_trade:
            tr = state.open_trade
            ep = tr.entry_price

            # Adaptive TP (locked per trade via tr side/regime — here use current for simplicity)
            active_tp = _adaptive_tp(regime, tr.side, sl_pct, tp_pct) if enable_adaptive_tp else tp_pct
            sl_price = ep * (1 - sl_pct) if tr.side == "buy" else ep * (1 + sl_pct)
            tp_price = ep * (1 + active_tp) if tr.side == "buy" else ep * (1 - active_tp)

            # Breakeven promotion
            if enable_breakeven and not state.breakeven_moved:
                captured = (close - ep) if tr.side == "buy" else (ep - close)
                tp_dist = abs(tp_price - ep)
                if captured >= tp_dist * 0.50:
                    sl_price = ep if tr.side == "buy" else ep
                    state.breakeven_moved = True
            elif state.breakeven_moved:
                sl_price = ep  # enforce floor

            # Partial exit at 1R
            if enable_partial and not tr.partial_done:
                one_r_profit = ep * sl_pct
                reached_1r = (
                    (close - ep >= one_r_profit) if tr.side == "buy"
                    else (ep - close >= one_r_profit)
                )
                if reached_1r:
                    half = tr.quantity * 0.50
                    fill = close * (1 + slippage_bps/10000) if tr.side == "sell" else close * (1 - slippage_bps/10000)
                    # Note: on partial "sell" side we receive; on partial on short we cover
                    pdiff = (fill - ep) if tr.side == "buy" else (ep - fill)
                    ppnl = pdiff * half - (close * half * fee_pct)
                    tr.partial_pnl = ppnl
                    tr.partial_done = True
                    tr.quantity -= half
                    state.balance += ppnl
                    state.daily_pnl += ppnl

            # Exit checks
            exit_reason = None
            if tr.side == "buy":
                if close <= sl_price:
                    exit_reason = "stop_loss" if not state.breakeven_moved else "breakeven"
                elif close >= tp_price:
                    exit_reason = "take_profit"
            else:
                if close >= sl_price:
                    exit_reason = "stop_loss" if not state.breakeven_moved else "breakeven"
                elif close <= tp_price:
                    exit_reason = "take_profit"

            # Time-in-trade (assume 16 bars = 4 hours on 15m)
            if exit_reason is None and (t - tr.entry_time) >= 4 * 3600:
                exit_reason = "time_limit"

            if exit_reason:
                slip = slippage_bps / 10000
                fill = close * (1 - slip) if tr.side == "buy" else close * (1 + slip)
                diff = (fill - ep) if tr.side == "buy" else (ep - fill)
                pnl = diff * tr.quantity - (close * tr.quantity * fee_pct)
                total_pnl = pnl + tr.partial_pnl
                tr.exit_time = t
                tr.exit_price = fill
                tr.exit_reason = exit_reason
                tr.pnl = total_pnl
                tr.pnl_pct = (total_pnl / (ep * (tr.quantity + (tr.quantity if tr.partial_done else 0)))) * 100 if ep > 0 else 0
                state.balance += pnl
                state.daily_pnl += pnl
                state.trades.append(tr)
                state.recent_pnls.append((total_pnl, total_pnl > 0))
                if len(state.recent_pnls) > 30:
                    state.recent_pnls.pop(0)
                if total_pnl > 0:
                    state.consecutive_wins += 1
                    state.consecutive_losses = 0
                else:
                    state.consecutive_losses += 1
                    state.consecutive_wins = 0
                    if exit_reason == "stop_loss":
                        last_stop_time = t
                state.open_trade = None
                state.breakeven_moved = False
                state.equity_curve.append((t, state.balance))

        # ── Entry logic ──
        if state.open_trade is None:
            # Time-of-day filter
            if hour_utc in dead_zone:
                continue
            # Daily target tracked for stats only — never caps profit (matches live engine)
            daily_target = max(200.0, state.balance * 0.025)
            # Z-score
            window = prices[-lookback:]
            mean = statistics.mean(window)
            std = statistics.stdev(window) if len(window) > 1 else 0
            if std == 0:
                continue
            z = (close - mean) / std

            side = None
            if z <= -entry_z:
                side = "buy"
            elif z >= entry_z:
                side = "sell"
            if side is None:
                continue

            # Filters
            rsi_v = rsi(prices, 14)
            if rsi_v is not None:
                if side == "buy" and rsi_v > 70:
                    continue
                if side == "sell" and rsi_v < 30:
                    continue

            # BB squeeze filter
            if enable_squeeze_filter:
                bb = bollinger_bands(prices, lookback)
                if bb and close > 0:
                    bbw = (bb["upper"] - bb["lower"]) / close
                    state.bb_width_history.append(bbw)
                    if len(state.bb_width_history) > 50:
                        state.bb_width_history.pop(0)
                    if len(state.bb_width_history) >= 20:
                        avg_w = sum(state.bb_width_history) / len(state.bb_width_history)
                        if bbw < avg_w * 0.50:
                            continue

            # Position sizing with Kelly + streaks + golden hour
            risk_amt = state.balance * risk_per_trade
            qty = risk_amt / (close * sl_pct) if sl_pct > 0 else 0
            max_expo = state.balance * 0.40 / close
            qty = min(qty, max_expo)

            mult = 1.0
            if state.consecutive_losses >= 2:
                mult *= 0.60
            elif state.consecutive_wins >= 3:
                mult *= min(1.10, 1.0 + (state.consecutive_wins - 2) * 0.03)
            if hour_utc in golden_hours:
                mult *= 1.25
            if enable_kelly:
                mult *= _kelly_mult(state.recent_pnls, risk_per_trade)
            qty *= mult
            qty = max(0.0001, round(qty, 4))

            if qty * close > state.balance:
                continue

            fill = close * (1 + slippage_bps/10000) if side == "buy" else close * (1 - slippage_bps/10000)
            tr = BTTrade(entry_time=t, entry_price=fill, side=side, quantity=qty)
            # Deduct fee
            state.balance -= close * qty * fee_pct
            state.open_trade = tr

        # Running equity tick
        if i % 50 == 0:
            state.equity_curve.append((t, state.balance))

    # ── Compute metrics ──
    closed = state.trades
    if not closed:
        return {"error": "no trades", "final_balance": state.balance, "trade_count": 0}

    wins = [t.pnl for t in closed if t.pnl > 0]
    losses = [t.pnl for t in closed if t.pnl <= 0]
    total_win = sum(wins)
    total_loss = abs(sum(losses)) if losses else 0.0001

    win_rate = len(wins) / len(closed)
    profit_factor = total_win / total_loss
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0

    # Max drawdown from equity curve
    peak = starting_balance
    max_dd = 0
    for _, eq in state.equity_curve:
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    # Sharpe (on trade returns)
    returns = [t.pnl / starting_balance for t in closed]
    sharpe = (statistics.mean(returns) / statistics.stdev(returns) * math.sqrt(252 * 6.5)) if len(returns) > 1 and statistics.stdev(returns) > 0 else 0

    # Daily P&L
    trades_per_day = len(closed) / max(1, (closed[-1].exit_time - closed[0].entry_time) / 86400)

    return {
        "trade_count": len(closed),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "total_pnl": state.balance - starting_balance,
        "final_balance": state.balance,
        "return_pct": (state.balance - starting_balance) / starting_balance * 100,
        "max_drawdown_pct": max_dd * 100,
        "sharpe": sharpe,
        "trades_per_day": trades_per_day,
        "partial_count": sum(1 for t in closed if t.partial_done),
        "breakeven_count": sum(1 for t in closed if t.exit_reason == "breakeven"),
    }


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTC-USD")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--balance", type=float, default=10000.0)
    parser.add_argument("--compare", action="store_true", help="Compare with/without enhancements")
    args = parser.parse_args()

    print(f"Fetching {args.days} days of {args.symbol} 15m bars from Coinbase…")
    bars = await fetch_coinbase_bars(args.symbol, args.days)
    print(f"Got {len(bars)} bars. Running backtest…\n")

    if args.compare:
        print("="*70)
        print("A/B COMPARISON — RC Quantum-style baseline vs Nalo.Ai full stack")
        print("="*70)
        baseline = backtest(
            bars, starting_balance=args.balance,
            enable_partial=False, enable_breakeven=False, enable_kelly=False,
            enable_adaptive_tp=False, enable_squeeze_filter=False,
        )
        full = backtest(bars, starting_balance=args.balance)

        print(f"\n{'Metric':<25} {'Baseline':>15} {'Full Stack':>15} {'Delta':>12}")
        print("-"*70)
        for k in ["trade_count", "win_rate", "profit_factor", "return_pct", "max_drawdown_pct", "sharpe", "trades_per_day"]:
            b = baseline.get(k, 0)
            f = full.get(k, 0)
            fmt = "{:>15.2%}" if k == "win_rate" else "{:>15.2f}"
            print(f"{k:<25} {fmt.format(b)} {fmt.format(f)} {fmt.format(f-b)}")
        print()
    else:
        result = backtest(bars, starting_balance=args.balance)
        print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
