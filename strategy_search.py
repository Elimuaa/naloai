"""
strategy_search.py — Systematic search for ANY profitable config.

Uses the existing backtester. Three rounds:
  1. Parameter sweep on z-score mean-reversion (current strategy)
  2. Filter ablation (which filters help, which hurt)
  3. Strategy inversion test (momentum/breakout — flip signal direction)

Caches bars to /tmp/btc_bars_180d.json so repeated runs are fast.
"""
import asyncio, json, os, time, statistics, math
from itertools import product
from datetime import datetime, timezone

from backtester import fetch_coinbase_bars, backtest

CACHE = "/tmp/btc_bars_180d.json"
DAYS = 180


async def get_bars():
    if os.path.exists(CACHE):
        age = time.time() - os.path.getmtime(CACHE)
        if age < 86400:  # 24h cache
            with open(CACHE) as f:
                bars = json.load(f)
            print(f"Using cached {len(bars)} bars (age {age/3600:.1f}h)")
            return bars
    print(f"Fetching {DAYS} days of BTC-USD bars...")
    bars = await fetch_coinbase_bars("BTC-USD", DAYS)
    with open(CACHE, "w") as f:
        json.dump(bars, f)
    print(f"Cached {len(bars)} bars")
    return bars


def fmt(r):
    return f"PF={r.get('profit_factor',0):.2f}  WR={r.get('win_rate',0)*100:.1f}%  Ret={r.get('return_pct',0):+.1f}%  Sharpe={r.get('sharpe',0):+.2f}  DD={r.get('max_drawdown_pct',0):.1f}%  N={r.get('trade_count',0)}"


def round1_param_sweep(bars):
    print("\n" + "="*80)
    print("ROUND 1 — Parameter sweep on current strategy")
    print("="*80)

    grid = {
        "lookback":     [10, 20, 30, 50],
        "entry_z":      [1.0, 1.3, 1.5, 1.8, 2.2],
        "sl_pct":       [0.015, 0.025, 0.04],
        "rr_ratio":     [1.5, 2.0, 2.5, 3.0],
    }
    keys = list(grid.keys())
    combos = list(product(*[grid[k] for k in keys]))
    print(f"Testing {len(combos)} configs...")

    results = []
    for i, combo in enumerate(combos):
        params = dict(zip(keys, combo))
        sl = params["sl_pct"]
        tp = sl * params["rr_ratio"]
        r = backtest(
            bars,
            lookback=params["lookback"],
            entry_z=params["entry_z"],
            sl_pct=sl,
            tp_pct=tp,
        )
        if "error" not in r:
            r["config"] = params
            results.append(r)
        if (i+1) % 20 == 0:
            print(f"  ...{i+1}/{len(combos)}")

    results.sort(key=lambda r: r["profit_factor"], reverse=True)
    print(f"\nTop 10 by profit factor:")
    for r in results[:10]:
        c = r["config"]
        print(f"  z={c['entry_z']:<4} lb={c['lookback']:<3} sl={c['sl_pct']:<6} rr={c['rr_ratio']:<4}  →  {fmt(r)}")
    print(f"\nBottom 3:")
    for r in results[-3:]:
        c = r["config"]
        print(f"  z={c['entry_z']:<4} lb={c['lookback']:<3} sl={c['sl_pct']:<6} rr={c['rr_ratio']:<4}  →  {fmt(r)}")

    profitable = [r for r in results if r["profit_factor"] >= 1.3]
    print(f"\n>>> Configs with PF >= 1.3 (live-ready threshold): {len(profitable)} / {len(results)}")
    return results[0] if results else None


def round2_ablation(bars, best_config):
    print("\n" + "="*80)
    print("ROUND 2 — Filter ablation (does each feature actually help?)")
    print("="*80)
    if not best_config:
        return
    c = best_config["config"]
    base = dict(
        lookback=c["lookback"],
        entry_z=c["entry_z"],
        sl_pct=c["sl_pct"],
        tp_pct=c["sl_pct"] * c["rr_ratio"],
    )

    print(f"Base config: {base}")
    full = backtest(bars, **base)
    print(f"\nALL filters ON:    {fmt(full)}")

    flags = ["enable_partial", "enable_breakeven", "enable_kelly", "enable_adaptive_tp", "enable_squeeze_filter"]
    print(f"\nLeave-one-out ablation:")
    for flag in flags:
        kwargs = dict(base)
        kwargs[flag] = False
        r = backtest(bars, **kwargs)
        delta = r["profit_factor"] - full["profit_factor"]
        verdict = "HURTS" if delta > 0.05 else ("HELPS" if delta < -0.05 else "neutral")
        print(f"  Without {flag:<25}: PF={r['profit_factor']:.3f} ({delta:+.3f})  [{verdict}]")

    # All filters off
    naked = backtest(bars,
        enable_partial=False, enable_breakeven=False, enable_kelly=False,
        enable_adaptive_tp=False, enable_squeeze_filter=False,
        **base
    )
    print(f"\nAll filters OFF:   {fmt(naked)}")
    print(f"Net effect of stack: PF {naked['profit_factor']:.3f} → {full['profit_factor']:.3f}  ({full['profit_factor']-naked['profit_factor']:+.3f})")


def round3_inversion(bars):
    """Mean-reversion inverted = momentum. If MR has -PF, MR-inverted should have +PF (minus fees)."""
    print("\n" + "="*80)
    print("ROUND 3 — Strategy inversion (test if momentum/breakout works on same data)")
    print("="*80)
    print("Strategy inversion is implemented inline in backtest by flipping signal direction.")
    print("Skipping for now — needs separate momentum.py to do properly.")


async def main():
    bars = await get_bars()
    best = round1_param_sweep(bars)
    round2_ablation(bars, best)
    round3_inversion(bars)
    print("\n" + "="*80)
    print("DONE.")
    print("="*80)


if __name__ == "__main__":
    asyncio.run(main())
