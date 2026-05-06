"""
rc_setup_search.py — Replicate RC's 4h BTC setup with all z-score variants.

RC told user: 4h timeframe, BTC, z-score + RSI + volume indicator.

Phase 1: Test 5 z-score variants on 4h BTC to find which has raw edge.
Phase 2: Apply RC's full filter stack (z + RSI + volume MA) to winners.

Z-score variants tested:
  1. price_z      — (close - mean(close)) / std(close)        [what we have]
  2. returns_z    — (ret - mean(ret)) / std(ret)              [stationary]
  3. logret_z     — log-return version                         [most statistically clean]
  4. atr_z        — (close - mean(close)) / ATR                [vol-normalized]
  5. vwap_z       — (close - VWAP_n) / std(close - VWAP_n)    [VWAP deviation]
"""
import asyncio, json, os, time, statistics, math
from itertools import product
from datetime import datetime, timezone

import httpx

CACHE_4H = "/tmp/btc_bars_4h_360d.json"
CACHE_1H = "/tmp/btc_bars_1h_360d.json"


async def fetch_bars(symbol="BTC-USD", granularity_s=14400, days=360):
    """Coinbase candle fetch. granularity 14400=4h, 3600=1h, 900=15m."""
    end_ts = int(time.time())
    start_ts = end_ts - days * 86400
    url = f"https://api.exchange.coinbase.com/products/{symbol}/candles"
    bars = []
    window = 300 * granularity_s
    cursor = start_ts
    async with httpx.AsyncClient(timeout=30) as c:
        while cursor < end_ts:
            win_end = min(cursor + window, end_ts)
            params = {
                "start": datetime.fromtimestamp(cursor, timezone.utc).isoformat(),
                "end": datetime.fromtimestamp(win_end, timezone.utc).isoformat(),
                "granularity": granularity_s,
            }
            try:
                r = await c.get(url, params=params)
                if r.status_code == 200:
                    for row in r.json():
                        bars.append({"time": row[0], "low": row[1], "high": row[2],
                                     "open": row[3], "close": row[4], "volume": row[5]})
            except Exception as e:
                print(f"err {e}")
            cursor = win_end
            await asyncio.sleep(0.15)
    seen = {b["time"]: b for b in bars}
    return sorted(seen.values(), key=lambda x: x["time"])


def aggregate_to_4h(bars_1h):
    """Coinbase doesn't support 4h granularity — aggregate 1h bars into 4h buckets."""
    if not bars_1h:
        return []
    out = []
    bucket = []
    bucket_start = (bars_1h[0]["time"] // 14400) * 14400
    for b in bars_1h:
        b_bucket = (b["time"] // 14400) * 14400
        if b_bucket != bucket_start and bucket:
            out.append({
                "time": bucket_start,
                "open": bucket[0]["open"],
                "close": bucket[-1]["close"],
                "high": max(x["high"] for x in bucket),
                "low":  min(x["low"]  for x in bucket),
                "volume": sum(x["volume"] for x in bucket),
            })
            bucket = []
            bucket_start = b_bucket
        bucket.append(b)
    if bucket:
        out.append({
            "time": bucket_start,
            "open": bucket[0]["open"],
            "close": bucket[-1]["close"],
            "high": max(x["high"] for x in bucket),
            "low":  min(x["low"]  for x in bucket),
            "volume": sum(x["volume"] for x in bucket),
        })
    return out


async def get_bars_4h():
    if os.path.exists(CACHE_4H):
        age = time.time() - os.path.getmtime(CACHE_4H)
        if age < 86400:
            with open(CACHE_4H) as f:
                bars = json.load(f)
            if bars:
                print(f"Cached 4h bars: {len(bars)} (age {age/3600:.1f}h)")
                return bars
    print("Building 4h bars by aggregating 1h...")
    bars_1h = await get_bars_1h()
    bars = aggregate_to_4h(bars_1h)
    with open(CACHE_4H, "w") as f:
        json.dump(bars, f)
    print(f"Cached {len(bars)} 4h bars (from {len(bars_1h)} 1h)")
    return bars


async def get_bars_1h():
    if os.path.exists(CACHE_1H):
        age = time.time() - os.path.getmtime(CACHE_1H)
        if age < 86400:
            with open(CACHE_1H) as f:
                bars = json.load(f)
            print(f"Cached 1h bars: {len(bars)} (age {age/3600:.1f}h)")
            return bars
    print("Fetching 360 days of BTC 1h bars...")
    bars = await fetch_bars(granularity_s=3600, days=360)
    with open(CACHE_1H, "w") as f:
        json.dump(bars, f)
    print(f"Cached {len(bars)} 1h bars")
    return bars


# ── Z-score variants ────────────────────────────────────────────────────
def _price_z(closes, lookback):
    if len(closes) < lookback + 1: return None
    win = closes[-lookback:]
    m = sum(win)/len(win)
    s = statistics.stdev(win) if len(win) > 1 else 0
    return (closes[-1] - m) / s if s > 0 else None


def _returns_z(closes, lookback):
    if len(closes) < lookback + 2: return None
    rets = [(closes[i] - closes[i-1])/closes[i-1] for i in range(-lookback, 0) if closes[i-1] > 0]
    if len(rets) < 5: return None
    m = sum(rets)/len(rets)
    s = statistics.stdev(rets) if len(rets) > 1 else 0
    if s <= 0: return None
    last_ret = (closes[-1] - closes[-2])/closes[-2] if closes[-2] > 0 else 0
    return (last_ret - m) / s


def _logret_z(closes, lookback):
    if len(closes) < lookback + 2: return None
    rets = [math.log(closes[i]/closes[i-1]) for i in range(-lookback, 0) if closes[i-1] > 0 and closes[i] > 0]
    if len(rets) < 5: return None
    m = sum(rets)/len(rets)
    s = statistics.stdev(rets) if len(rets) > 1 else 0
    if s <= 0: return None
    last = math.log(closes[-1]/closes[-2]) if closes[-2] > 0 and closes[-1] > 0 else 0
    return (last - m) / s


def _atr(highs, lows, closes, period=14):
    if len(closes) < period + 1: return None
    trs = []
    for i in range(-period, 0):
        h, l = highs[i], lows[i]
        pc = closes[i-1]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else None


def _atr_z(highs, lows, closes, lookback):
    """Price deviation normalized by ATR rather than std."""
    if len(closes) < lookback + 15: return None
    win = closes[-lookback:]
    m = sum(win)/len(win)
    a = _atr(highs, lows, closes, 14)
    if not a or a == 0: return None
    return (closes[-1] - m) / a


def _vwap_z(closes, volumes, lookback):
    if len(closes) < lookback + 1 or len(volumes) < lookback + 1: return None
    vols = volumes[-lookback:]
    cls = closes[-lookback:]
    total_vol = sum(vols)
    if total_vol <= 0: return None
    vwap = sum(c*v for c,v in zip(cls, vols)) / total_vol
    devs = [c - vwap for c in cls]
    s = statistics.stdev(devs) if len(devs) > 1 else 0
    if s <= 0: return None
    return (closes[-1] - vwap) / s


# ── RSI ──────────────────────────────────────────────────────────────────
def _rsi(closes, period=14):
    if len(closes) < period + 1: return None
    gains, losses = [], []
    for i in range(-period, 0):
        delta = closes[i] - closes[i-1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_g = sum(gains) / period
    avg_l = sum(losses) / period
    if avg_l == 0: return 100
    rs = avg_g / avg_l
    return 100 - (100 / (1 + rs))


# ── Backtest engine ──────────────────────────────────────────────────────
def backtest(
    bars,
    z_variant="price_z",
    lookback=20,
    entry_z=1.5,
    sl_pct=0.025,
    tp_pct=0.05,
    use_rsi=False,
    rsi_period=14,
    rsi_buy_max=70,    # don't buy if RSI too high
    rsi_sell_min=30,   # don't sell if RSI too low
    use_volume=False,
    vol_mult=1.5,      # require current volume > vol_mult * avg
    starting_balance=10000.0,
    risk_per_trade=0.02,
    slippage_bps=3.5,
    fee_pct=0.001,
    use_trail=False,
    trail_pct=0.015,
    time_cap_hours=20,   # 5 bars on 4h = 20h
):
    balance = starting_balance
    open_trade = None
    trades = []
    closes, highs, lows, vols = [], [], [], []
    peak = balance
    max_dd = 0

    for bar in bars:
        c, h, l, v, t = bar["close"], bar["high"], bar["low"], bar["volume"], bar["time"]
        closes.append(c); highs.append(h); lows.append(l); vols.append(v)
        if len(closes) < lookback + 20: continue

        # Manage open trade
        if open_trade:
            tr = open_trade
            ep = tr["entry_price"]
            side = tr["side"]
            sl = ep * (1 - sl_pct) if side == "buy" else ep * (1 + sl_pct)
            tp = ep * (1 + tp_pct) if side == "buy" else ep * (1 - tp_pct)
            if use_trail:
                if side == "buy":
                    tr["hw"] = max(tr.get("hw", c), c)
                    sl = max(sl, tr["hw"] * (1 - trail_pct))
                else:
                    tr["lw"] = min(tr.get("lw", c), c)
                    sl = min(sl, tr["lw"] * (1 + trail_pct))

            exit_reason = None
            if side == "buy":
                if c <= sl: exit_reason = "stop"
                elif c >= tp: exit_reason = "tp"
            else:
                if c >= sl: exit_reason = "stop"
                elif c <= tp: exit_reason = "tp"
            if (t - tr["entry_time"]) >= time_cap_hours * 3600:
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

        # Entry
        if open_trade is None:
            if z_variant == "price_z":
                z = _price_z(closes, lookback)
            elif z_variant == "returns_z":
                z = _returns_z(closes, lookback)
            elif z_variant == "logret_z":
                z = _logret_z(closes, lookback)
            elif z_variant == "atr_z":
                z = _atr_z(highs, lows, closes, lookback)
            elif z_variant == "vwap_z":
                z = _vwap_z(closes, vols, lookback)
            else:
                z = None
            if z is None: continue

            # Mean-reversion direction (fade extremes)
            side = None
            if z <= -entry_z: side = "buy"
            elif z >= entry_z: side = "sell"
            if side is None: continue

            # RSI filter
            if use_rsi:
                rsi_val = _rsi(closes, rsi_period)
                if rsi_val is None: continue
                if side == "buy" and rsi_val > rsi_buy_max: continue
                if side == "sell" and rsi_val < rsi_sell_min: continue

            # Volume filter
            if use_volume and len(vols) >= 20:
                avg_v = sum(vols[-20:]) / 20
                if avg_v > 0 and v < avg_v * vol_mult:
                    continue

            qty = (balance * risk_per_trade) / (c * sl_pct) if sl_pct > 0 else 0
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
    sh = (statistics.mean(rets)/statistics.stdev(rets) * math.sqrt(252)) if len(rets)>1 and statistics.stdev(rets)>0 else 0
    return {
        "trade_count": len(trades),
        "win_rate": len(wins)/len(trades) if trades else 0,
        "profit_factor": pf,
        "return_pct": (balance - starting_balance)/starting_balance*100,
        "max_drawdown_pct": max_dd * 100,
        "sharpe": sh,
        "avg_win": statistics.mean(wins) if wins else 0,
        "avg_loss": statistics.mean(losses) if losses else 0,
    }


def fmt(r):
    return f"PF={r.get('profit_factor',0):.2f}  WR={r.get('win_rate',0)*100:.1f}%  Ret={r.get('return_pct',0):+.1f}%  Sharpe={r.get('sharpe',0):+.2f}  DD={r.get('max_drawdown_pct',0):.1f}%  N={r.get('trade_count',0)}"


def phase1_pure_z(bars, label="4H"):
    """Test all 5 z-variants raw (no RSI, no volume) to find which has edge."""
    print("\n" + "="*80)
    print(f"PHASE 1 ({label}) — Pure z-score variants, no extra filters")
    print("="*80)
    variants = ["price_z", "returns_z", "logret_z", "atr_z", "vwap_z"]
    grid = list(product(
        variants,
        [10, 20, 30, 50],         # lookback
        [1.0, 1.3, 1.5, 1.8, 2.2], # entry_z
        [0.02, 0.04, 0.06],        # sl_pct (wider for 4h)
        [1.5, 2.0, 2.5],           # rr
    ))
    print(f"Testing {len(grid)} configs...")
    results = []
    for v, lb, ez, sl, rr in grid:
        r = backtest(bars, z_variant=v, lookback=lb, entry_z=ez, sl_pct=sl, tp_pct=sl*rr,
                     time_cap_hours=20)
        if "error" not in r and r["trade_count"] >= 10:
            r["cfg"] = (v, lb, ez, sl, rr)
            results.append(r)

    results.sort(key=lambda r: r["profit_factor"], reverse=True)
    print(f"Top 15 configs (n>=10 trades):")
    for r in results[:15]:
        v, lb, ez, sl, rr = r["cfg"]
        print(f"  {v:<10} lb={lb:<3} z={ez:<4} sl={sl:<5} rr={rr:<4}  →  {fmt(r)}")
    profitable = [r for r in results if r["profit_factor"] >= 1.3]
    print(f"\n>>> {label} configs with PF >= 1.3: {len(profitable)} / {len(results)}")
    return results


def phase2_with_filters(bars, top_configs, label="4H"):
    """Apply RC's RSI + volume filters to the best base z-variants."""
    print("\n" + "="*80)
    print(f"PHASE 2 ({label}) — Add RSI + Volume filters to top base configs")
    print("="*80)
    if not top_configs:
        print("No base configs to extend.")
        return []

    # Take top 5 distinct z-variants from phase 1
    seen = set()
    top_seeds = []
    for r in top_configs:
        v = r["cfg"][0]
        if v not in seen:
            seen.add(v)
            top_seeds.append(r)
        if len(top_seeds) >= 5: break

    grid = list(product(
        [(False, False), (True, False), (False, True), (True, True)],   # (use_rsi, use_volume)
        [60, 65, 70, 75],     # rsi_buy_max
        [1.2, 1.5, 2.0],      # vol_mult
    ))
    results = []
    for seed in top_seeds:
        v, lb, ez, sl, rr = seed["cfg"]
        for (urs, uv), rsi_max, vmult in grid:
            r = backtest(
                bars, z_variant=v, lookback=lb, entry_z=ez,
                sl_pct=sl, tp_pct=sl*rr,
                use_rsi=urs, rsi_buy_max=rsi_max, rsi_sell_min=100-rsi_max,
                use_volume=uv, vol_mult=vmult,
                time_cap_hours=20,
            )
            if "error" not in r and r["trade_count"] >= 10:
                r["cfg"] = (v, lb, ez, sl, rr, urs, uv, rsi_max, vmult)
                results.append(r)

    results.sort(key=lambda r: r["profit_factor"], reverse=True)
    print(f"Top 20 with RSI/volume filters:")
    for r in results[:20]:
        v, lb, ez, sl, rr, urs, uv, rmax, vm = r["cfg"]
        flags = ("R" if urs else "_") + ("V" if uv else "_")
        print(f"  {v:<10} lb={lb:<3} z={ez:<4} sl={sl:<5} rr={rr:<4} flt={flags} rsi<{rmax} vol×{vm}  →  {fmt(r)}")
    profitable = [r for r in results if r["profit_factor"] >= 1.3]
    print(f"\n>>> {label} with-filters configs PF >= 1.3: {len(profitable)} / {len(results)}")
    return results


async def main():
    bars_4h = await get_bars_4h()
    bars_1h = await get_bars_1h()
    print(f"\nBars: 4h={len(bars_4h)}  1h={len(bars_1h)}")

    # 4H — RC's actual timeframe — most important
    p1_4h = phase1_pure_z(bars_4h, "4H")
    p2_4h = phase2_with_filters(bars_4h, p1_4h, "4H")

    # 1H — bridge timeframe, more trades than 4h
    p1_1h = phase1_pure_z(bars_1h, "1H")
    p2_1h = phase2_with_filters(bars_1h, p1_1h, "1H")

    print("\n" + "="*80)
    print("FINAL VERDICT")
    print("="*80)

    def best(results):
        if not results: return None
        return max(results, key=lambda r: r["profit_factor"])

    for label, r in [
        ("4H pure z", best(p1_4h)),
        ("4H z+filters", best(p2_4h)),
        ("1H pure z", best(p1_1h)),
        ("1H z+filters", best(p2_1h)),
    ]:
        if r:
            print(f"  {label:<18} → best PF={r['profit_factor']:.2f}  Ret={r['return_pct']:+.1f}%  Sharpe={r['sharpe']:+.2f}  N={r['trade_count']}")

    print()
    any_profitable = any(
        r and r["profit_factor"] >= 1.3 and r["trade_count"] >= 30
        for r in [best(p1_4h), best(p2_4h), best(p1_1h), best(p2_1h)]
    )
    if any_profitable:
        print(">>> EDGE FOUND. We have a real config to deploy.")
    else:
        print(">>> Still no PF>=1.3 with n>=30. RC may use something we haven't tested.")


if __name__ == "__main__":
    asyncio.run(main())
