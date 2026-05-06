"""
walk_forward.py — Out-of-sample validation of the 4h BTC mean-reversion edge.

The in-sample backtest found:
    returns_z  lb=10  z=2.2  sl=0.02  rr=2.0  →  PF=1.73  N=31  over 360 days

N=31 is too small to trust. This script does a walk-forward test:
    - Split 360 days into 6 windows of ~60 days each
    - Optimize on windows {1..k}, test on window {k+1}
    - Report in-sample vs out-of-sample PF for each fold
    - If OOS PF holds, edge is real. If OOS collapses, edge was luck.

Also reports:
    - BTC buy-and-hold return over same period (the real benchmark)
    - Per-fold trade count and drawdown
"""
import asyncio, json, os, time, statistics, math
from itertools import product

# Reuse machinery from rc_setup_search
from rc_setup_search import (
    get_bars_4h, _returns_z, _logret_z, _price_z, _atr_z, _vwap_z,
    _rsi, _atr, backtest, fmt
)

Z_FUNCS = {
    "returns_z": _returns_z,
    "logret_z":  _logret_z,
    "price_z":   _price_z,
    "atr_z":     _atr_z,
    "vwap_z":    _vwap_z,
}


def hodl_return(bars):
    if len(bars) < 2: return 0.0
    return (bars[-1]["close"] - bars[0]["close"]) / bars[0]["close"] * 100


async def main():
    bars = await get_bars_4h()
    print(f"Total 4h bars: {len(bars)}")
    print(f"BTC HODL over full period: {hodl_return(bars):+.1f}%\n")

    # 6 folds of equal size
    n_folds = 6
    fold_size = len(bars) // n_folds
    folds = [bars[i*fold_size:(i+1)*fold_size] for i in range(n_folds)]
    print(f"Fold size: {fold_size} bars (~{fold_size*4/24:.0f} days each)")

    # Param grid (smaller — focused on the winner's neighborhood)
    grid = list(product(
        ["returns_z", "logret_z"],   # top performers
        [10, 20, 30],                 # lookback
        [1.8, 2.0, 2.2],              # entry_z
        [0.02, 0.03],                 # sl
        [1.5, 2.0, 2.5],              # rr
    ))
    print(f"Optimizing over {len(grid)} configs per fold\n")

    print("="*100)
    print(f"{'Fold':<6}{'IS bars':<10}{'OOS bars':<10}{'IS best PF':<12}{'OOS PF':<10}{'OOS Ret':<10}{'OOS N':<8}{'HODL':<10}{'Config'}")
    print("="*100)

    fold_results = []
    for k in range(1, n_folds):
        in_sample  = sum(folds[:k], [])
        out_sample = folds[k]

        # Optimize on in-sample
        best = None
        for zname, lb, ez, sl, rr in grid:
            r = backtest(in_sample, z_variant=zname, lookback=lb, entry_z=ez,
                         sl_pct=sl, tp_pct=sl*rr)
            if "error" in r or r.get("trade_count", 0) < 10: continue
            if best is None or r["profit_factor"] > best["profit_factor"]:
                best = r
                best["cfg"] = (zname, lb, ez, sl, rr)

        if best is None:
            print(f"{k:<6}{len(in_sample):<10}{len(out_sample):<10}no profitable IS config")
            continue

        # Test on out-of-sample
        zname, lb, ez, sl, rr = best["cfg"]
        oos = backtest(out_sample, z_variant=zname, lookback=lb, entry_z=ez,
                       sl_pct=sl, tp_pct=sl*rr)
        hodl = hodl_return(out_sample)

        cfg_str = f"{zname} lb={lb} z={ez} sl={sl} rr={rr}"
        oos_pf  = oos.get("profit_factor", 0) if "error" not in oos else 0
        oos_ret = oos.get("return_pct", 0) if "error" not in oos else 0
        oos_n   = oos.get("trade_count", 0) if "error" not in oos else 0
        print(f"{k:<6}{len(in_sample):<10}{len(out_sample):<10}{best['profit_factor']:<12.2f}{oos_pf:<10.2f}{oos_ret:<+10.1f}{oos_n:<8}{hodl:<+10.1f}{cfg_str}")
        fold_results.append({"is_pf": best["profit_factor"], "oos_pf": oos_pf,
                              "oos_ret": oos_ret, "oos_n": oos_n, "hodl": hodl})

    print("="*100)

    if not fold_results:
        print("No folds produced a usable config.")
        return

    avg_is_pf  = statistics.mean(r["is_pf"]  for r in fold_results)
    avg_oos_pf = statistics.mean(r["oos_pf"] for r in fold_results)
    avg_oos_ret = statistics.mean(r["oos_ret"] for r in fold_results)
    total_oos_n = sum(r["oos_n"] for r in fold_results)
    avg_hodl   = statistics.mean(r["hodl"]   for r in fold_results)
    pf_decay   = avg_oos_pf / avg_is_pf if avg_is_pf > 0 else 0

    print(f"\nFold averages:")
    print(f"  In-sample PF (optimistic):     {avg_is_pf:.2f}")
    print(f"  Out-of-sample PF (honest):     {avg_oos_pf:.2f}")
    print(f"  OOS / IS ratio:                {pf_decay:.2f}  (1.0 = no decay; <0.7 = curve-fit)")
    print(f"  Avg OOS return per fold:       {avg_oos_ret:+.1f}%")
    print(f"  Avg HODL return per fold:      {avg_hodl:+.1f}%")
    print(f"  Total OOS trades:              {total_oos_n}")

    print("\n" + "="*60)
    print("VERDICT")
    print("="*60)
    if avg_oos_pf >= 1.3 and pf_decay >= 0.7 and total_oos_n >= 30:
        print(f"✓ Edge holds out-of-sample. Real strategy.")
    elif avg_oos_pf >= 1.0 and pf_decay >= 0.5:
        print(f"~ Edge weakens OOS but isn't dead. Marginal — needs more data.")
    else:
        print(f"✗ Edge collapses out-of-sample. The PF 1.73 result was curve-fit luck.")

    if avg_oos_ret < avg_hodl:
        gap = avg_hodl - avg_oos_ret
        print(f"\n⚠️  Even if edge holds: strategy returns {avg_oos_ret:+.1f}% vs HODL {avg_hodl:+.1f}% per fold.")
        print(f"   Buy-and-hold beats this by {gap:.1f}%/fold. Active trading is destroying value.")


if __name__ == "__main__":
    asyncio.run(main())
