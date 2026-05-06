"""
momentum_search.py — Test if momentum/breakout has edge where mean-reversion doesn't.

Same z-score signal, INVERTED direction:
  - Mean-reversion: z >= +1.3 → SELL  (fade the rally)
  - Momentum:       z >= +1.3 → BUY   (ride the breakout)

Also tests Donchian-channel breakout as a separate strategy.
"""
import asyncio, json, os, time, statistics, math
from itertools import product
from datetime import datetime, timezone

from backtester import fetch_coinbase_bars

CACHE = "/tmp/btc_bars_180d.json"


async def get_bars():
    if os.path.exists(CACHE):
        with open(CACHE) as f:
            return json.load(f)
    bars = await fetch_coinbase_bars("BTC-USD", 180)
    with open(CACHE, "w") as f:
        json.dump(bars, f)
    return bars


def momentum_backtest(
    bars,
    starting_balance=10000.0,
    lookback=20,
    entry_z=1.3,
    sl_pct=0.025,
    tp_pct=0.05,
    risk_per_trade=0.02,
    slippage_bps=3.5,
    fee_pct=0.001,
    use_trail=True,
    trail_pct=0.015,
):
    """z-score breakout: BUY on z >= +entry_z, SELL on z <= -entry_z. Trail stop."""
    balance = starting_balance
    open_trade = None
    trades = []
    prices = []
    peak_balance = balance
    max_dd = 0
    equity_curve = [balance]

    for i, bar in enumerate(bars):
        close = bar["close"]
        t = bar["time"]
        prices.append(close)
        if len(prices) < lookback + 5:
            continue

        # Manage open trade
        if open_trade:
            tr = open_trade
            ep = tr["entry_price"]
            side = tr["side"]
            sl = ep * (1 - sl_pct) if side == "buy" else ep * (1 + sl_pct)
            tp = ep * (1 + tp_pct) if side == "buy" else ep * (1 - tp_pct)

            # Trail stop logic — moves with price for momentum
            if use_trail:
                if side == "buy":
                    tr["high_water"] = max(tr.get("high_water", close), close)
                    trail = tr["high_water"] * (1 - trail_pct)
                    sl = max(sl, trail)
                else:
                    tr["low_water"] = min(tr.get("low_water", close), close)
                    trail = tr["low_water"] * (1 + trail_pct)
                    sl = min(sl, trail)

            exit_reason = None
            if side == "buy":
                if close <= sl: exit_reason = "stop"
                elif close >= tp: exit_reason = "tp"
            else:
                if close >= sl: exit_reason = "stop"
                elif close <= tp: exit_reason = "tp"
            if (t - tr["entry_time"]) >= 5 * 3600:
                exit_reason = exit_reason or "time"

            if exit_reason:
                slip = slippage_bps / 10000
                fill = close * (1 - slip) if side == "buy" else close * (1 + slip)
                diff = (fill - ep) if side == "buy" else (ep - fill)
                pnl = diff * tr["qty"] - (close * tr["qty"] * fee_pct)
                balance += pnl
                tr["pnl"] = pnl
                tr["exit_reason"] = exit_reason
                trades.append(tr)
                open_trade = None
                peak_balance = max(peak_balance, balance)
                dd = (peak_balance - balance) / peak_balance
                max_dd = max(max_dd, dd)
                equity_curve.append(balance)

        # Entry
        if open_trade is None:
            window = prices[-lookback:]
            mean = statistics.mean(window)
            std = statistics.stdev(window) if len(window) > 1 else 0
            if std == 0: continue
            z = (close - mean) / std

            side = None
            # MOMENTUM direction: z>=+entry_z BUY (ride up), z<=-entry_z SELL (ride down)
            if z >= entry_z:
                side = "buy"
            elif z <= -entry_z:
                side = "sell"
            if side is None: continue

            risk_amt = balance * risk_per_trade
            qty = risk_amt / (close * sl_pct) if sl_pct > 0 else 0
            qty = max(0.0001, round(qty, 6))
            if qty * close > balance: continue

            slip = slippage_bps / 10000
            fill = close * (1 + slip) if side == "buy" else close * (1 - slip)
            balance -= close * qty * fee_pct
            open_trade = {
                "entry_time": t,
                "entry_price": fill,
                "side": side,
                "qty": qty,
            }

    if not trades:
        return {"error": "no trades", "trade_count": 0}

    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    pf = sum(wins) / abs(sum(losses)) if losses else 999
    wr = len(wins) / len(trades)
    sharpe = 0
    rets = [t["pnl"] / starting_balance for t in trades]
    if len(rets) > 1 and statistics.stdev(rets) > 0:
        sharpe = statistics.mean(rets) / statistics.stdev(rets) * math.sqrt(252 * 6.5)
    return {
        "trade_count": len(trades),
        "win_rate": wr,
        "profit_factor": pf,
        "return_pct": (balance - starting_balance) / starting_balance * 100,
        "max_drawdown_pct": max_dd * 100,
        "sharpe": sharpe,
        "avg_win": statistics.mean(wins) if wins else 0,
        "avg_loss": statistics.mean(losses) if losses else 0,
    }


def donchian_backtest(bars, lookback=20, sl_pct=0.025, trail_pct=0.015,
                      starting_balance=10000.0, slippage_bps=3.5, fee_pct=0.001,
                      risk_per_trade=0.02):
    """Classic Donchian breakout: BUY on N-bar high, SELL on N-bar low. Trail exit."""
    balance = starting_balance
    open_trade = None
    trades = []
    highs, lows, closes = [], [], []
    peak = balance
    max_dd = 0

    for bar in bars:
        h, l, c, t = bar["high"], bar["low"], bar["close"], bar["time"]
        highs.append(h); lows.append(l); closes.append(c)
        if len(closes) < lookback + 2:
            continue

        if open_trade:
            tr = open_trade
            ep = tr["entry_price"]
            side = tr["side"]
            if side == "buy":
                tr["hw"] = max(tr.get("hw", c), c)
                trail = tr["hw"] * (1 - trail_pct)
                sl = max(ep * (1 - sl_pct), trail)
                if c <= sl: exit_reason = "stop"
                else: exit_reason = None
            else:
                tr["lw"] = min(tr.get("lw", c), c)
                trail = tr["lw"] * (1 + trail_pct)
                sl = min(ep * (1 + sl_pct), trail)
                if c >= sl: exit_reason = "stop"
                else: exit_reason = None
            if (t - tr["entry_time"]) >= 24 * 3600:
                exit_reason = exit_reason or "time"
            if exit_reason:
                slip = slippage_bps/10000
                fill = c * (1 - slip) if side == "buy" else c * (1 + slip)
                diff = (fill - ep) if side == "buy" else (ep - fill)
                pnl = diff * tr["qty"] - (c * tr["qty"] * fee_pct)
                balance += pnl
                tr["pnl"] = pnl
                trades.append(tr)
                open_trade = None
                peak = max(peak, balance)
                max_dd = max(max_dd, (peak - balance) / peak if peak > 0 else 0)

        if open_trade is None:
            recent_high = max(highs[-lookback-1:-1])
            recent_low = min(lows[-lookback-1:-1])
            side = None
            if c > recent_high:
                side = "buy"
            elif c < recent_low:
                side = "sell"
            if side is None: continue
            qty = (balance * risk_per_trade) / (c * sl_pct)
            qty = max(0.0001, round(qty, 6))
            if qty * c > balance: continue
            slip = slippage_bps/10000
            fill = c * (1 + slip) if side == "buy" else c * (1 - slip)
            balance -= c * qty * fee_pct
            open_trade = {"entry_time": t, "entry_price": fill, "side": side, "qty": qty}

    if not trades:
        return {"error": "no trades", "trade_count": 0}
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    pf = sum(wins) / abs(sum(losses)) if losses else 999
    rets = [t["pnl"] / starting_balance for t in trades]
    sh = (statistics.mean(rets)/statistics.stdev(rets) * math.sqrt(252 * 6.5)) if len(rets)>1 and statistics.stdev(rets)>0 else 0
    return {
        "trade_count": len(trades),
        "win_rate": len(wins)/len(trades),
        "profit_factor": pf,
        "return_pct": (balance - starting_balance)/starting_balance*100,
        "max_drawdown_pct": max_dd * 100,
        "sharpe": sh,
    }


def fmt(r):
    return f"PF={r.get('profit_factor',0):.2f}  WR={r.get('win_rate',0)*100:.1f}%  Ret={r.get('return_pct',0):+.1f}%  Sharpe={r.get('sharpe',0):+.2f}  DD={r.get('max_drawdown_pct',0):.1f}%  N={r.get('trade_count',0)}"


async def main():
    bars = await get_bars()
    print(f"Bars loaded: {len(bars)}")

    # ── Z-MOMENTUM SWEEP ──
    print("\n" + "="*80)
    print("Z-SCORE MOMENTUM (inverted from current strategy)")
    print("="*80)
    grid = list(product(
        [10, 20, 30, 50],   # lookback
        [1.0, 1.3, 1.5, 1.8, 2.2],  # entry_z
        [0.015, 0.025, 0.04],  # sl
        [1.5, 2.0, 2.5, 3.0],  # rr
    ))
    print(f"Testing {len(grid)} configs...")
    results = []
    for lb, ez, sl, rr in grid:
        r = momentum_backtest(bars, lookback=lb, entry_z=ez, sl_pct=sl, tp_pct=sl*rr)
        if "error" not in r:
            r["cfg"] = (lb, ez, sl, rr)
            results.append(r)
    results.sort(key=lambda r: r["profit_factor"], reverse=True)
    print("Top 10 momentum configs:")
    for r in results[:10]:
        lb, ez, sl, rr = r["cfg"]
        print(f"  lb={lb:<3} z={ez:<4} sl={sl:<6} rr={rr:<4}  →  {fmt(r)}")
    profitable = [r for r in results if r["profit_factor"] >= 1.3]
    print(f"\n>>> Momentum configs with PF >= 1.3: {len(profitable)} / {len(results)}")

    # ── DONCHIAN BREAKOUT SWEEP ──
    print("\n" + "="*80)
    print("DONCHIAN-CHANNEL BREAKOUT")
    print("="*80)
    grid2 = list(product(
        [20, 50, 100, 200],   # lookback
        [0.015, 0.025, 0.04, 0.06],  # sl
        [0.01, 0.02, 0.03],  # trail
    ))
    print(f"Testing {len(grid2)} configs...")
    results2 = []
    for lb, sl, tr in grid2:
        r = donchian_backtest(bars, lookback=lb, sl_pct=sl, trail_pct=tr)
        if "error" not in r:
            r["cfg"] = (lb, sl, tr)
            results2.append(r)
    results2.sort(key=lambda r: r["profit_factor"], reverse=True)
    print("Top 10 Donchian configs:")
    for r in results2[:10]:
        lb, sl, tr = r["cfg"]
        print(f"  lb={lb:<3} sl={sl:<6} trail={tr:<6}  →  {fmt(r)}")
    profitable2 = [r for r in results2 if r["profit_factor"] >= 1.3]
    print(f"\n>>> Donchian configs with PF >= 1.3: {len(profitable2)} / {len(results2)}")

    print("\n" + "="*80)
    print("VERDICT")
    print("="*80)
    if profitable or profitable2:
        print(f"FOUND PROFITABLE STRATEGIES: {len(profitable)} momentum + {len(profitable2)} Donchian")
    else:
        print("NO profitable configs found across both strategies.")
        print("Implies: BTC 15m is too efficient for these classical strategies.")
        print("Next step: try 1h or 4h timeframe, or different asset (ETH, SOL).")


if __name__ == "__main__":
    asyncio.run(main())
