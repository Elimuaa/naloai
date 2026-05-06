"""
Mock Robinhood client for demo trading without real API credentials.
Uses REAL market prices from Coinbase/Kraken for accurate strategy execution,
but simulates order fills with virtual balance.
"""
import asyncio
import logging
import uuid
import httpx

logger = logging.getLogger(__name__)

# Cache real prices with short TTL to avoid hammering APIs
_price_cache: dict[str, tuple[float, float]] = {}  # symbol -> (price, timestamp)
CACHE_TTL = 3.0  # seconds


async def _fetch_real_price(symbol: str) -> float:
    """Fetch real market price from public APIs."""
    import time
    now = time.time()

    # Check cache
    if symbol in _price_cache:
        cached_price, cached_at = _price_cache[symbol]
        if now - cached_at < CACHE_TTL:
            return cached_price

    base = symbol.split("-")[0].upper()

    # 1. Coinbase (primary)
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"https://api.coinbase.com/v2/prices/{symbol}/spot")
            r.raise_for_status()
            price = float(r.json()["data"]["amount"])
            _price_cache[symbol] = (price, now)
            return price
    except Exception as e:
        logger.debug(f"Coinbase price fetch failed for {symbol}: {e}")

    # 2. Kraken (fallback)
    try:
        kraken_pair = "XBTUSD" if base == "BTC" else f"{base}USD"
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"https://api.kraken.com/0/public/Ticker?pair={kraken_pair}")
            r.raise_for_status()
            result = r.json().get("result", {})
            ticker = next(iter(result.values()), None)
            if ticker:
                price = float(ticker["c"][0])
                _price_cache[symbol] = (price, now)
                return price
    except Exception as e:
        logger.debug(f"Kraken price fetch failed for {symbol}: {e}")

    # 3. CryptoCompare (last resort)
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"https://min-api.cryptocompare.com/data/price?fsym={base}&tsyms=USD")
            r.raise_for_status()
            price = float(r.json()["USD"])
            _price_cache[symbol] = (price, now)
            return price
    except Exception as e:
        logger.warning(f"All 3 price sources failed for {symbol} (last: CryptoCompare {e})")

    # If all fail, return cached price if available (even if stale)
    if symbol in _price_cache:
        return _price_cache[symbol][0]

    logger.error(f"All price sources failed for {symbol}, no cached price available")
    return 0.0


class MockRobinhoodClient:
    """Drop-in replacement for RobinhoodCryptoClient that uses real market prices
    but simulates order execution with virtual balance."""

    def __init__(self, symbol: str = "BTC-USD", balance: float = 10000.0):
        self.symbol = symbol
        self.balance = balance
        # Signed quantity: positive = long, negative = short. Tracking shorts as
        # negative is what keeps balance honest when the strategy opens a sell-side
        # position — without this, sell-to-open silently credits proceeds and the
        # balance grows unboundedly on every short cycle.
        self._holdings: dict[str, float] = {}
        # Average entry price per open position. Required so that closing a short
        # can compute realized P&L = (entry - fill) * qty without needing a prior
        # cash debit. Pop on flat.
        self._avg_entry: dict[str, float] = {}
        self._lock = asyncio.Lock()  # prevents concurrent balance mutations across 4 symbol loops
        logger.info(f"MockRobinhoodClient initialized — demo balance: ${balance:,.2f} (using real market prices)")

    async def get_account(self) -> dict:
        return {"buying_power": str(round(self.balance, 4)), "buying_power_currency": "USD"}

    async def get_holdings(self) -> dict:
        # Only surface long positions to the existing get_holdings consumers
        # (display in dashboard) — shorts are tracked internally but not shown
        # as "holdings" since they're synthetic, not custodied assets.
        results = []
        for sym, qty in self._holdings.items():
            if qty > 0:
                results.append({
                    "asset_code": sym.replace("-USD", ""),
                    "total_quantity": str(qty),
                    "quantity_available_for_trading": str(qty),
                })
        return {"results": results}

    async def get_best_bid_ask(self, symbol: str) -> dict:
        price = await _fetch_real_price(symbol)
        spread = price * 0.0002  # Simulate tight spread
        return {
            "results": [{
                "symbol": symbol,
                "bid_inclusive_of_sell_spread": str(round(price - spread, 2)),
                "ask_inclusive_of_buy_spread": str(round(price + spread, 2)),
            }]
        }

    async def get_orders(self) -> dict:
        return {"results": []}

    async def get_order(self, order_id: str) -> dict:
        return {"id": order_id, "state": "filled"}

    async def place_market_order(self, symbol: str, side: str, asset_quantity: str) -> dict:
        import random
        mid_price = await _fetch_real_price(symbol)
        qty = float(asset_quantity)
        order_id = str(uuid.uuid4())

        # ── REALISTIC EXECUTION: slippage + fees ──
        # Slippage: 2-5 bps uniform-random adverse fill (buys fill higher, sells lower)
        # Fees: 0.10% per side (Robinhood crypto taker fee approximation)
        SLIPPAGE_BPS = 2 + random.random() * 3   # 2-5 bps
        FEE_PCT = 0.001                           # 10 bps per side
        slip_frac = SLIPPAGE_BPS / 10000.0
        if side == "buy":
            fill_price = mid_price * (1 + slip_frac)  # pay higher
        else:
            fill_price = mid_price * (1 - slip_frac)  # receive lower

        # Lock prevents two concurrent loops (e.g. BTC + ETH) both passing the balance
        # check before either deducts, which could overdraw the account silently.
        async with self._lock:
            held = self._holdings.get(symbol, 0.0)
            basis = self._avg_entry.get(symbol, 0.0)
            notional = fill_price * qty
            fee = notional * FEE_PCT

            if side == "buy":
                if held < 0:
                    # Closing/reducing a short position — realize P&L into balance.
                    close_qty = min(qty, -held)
                    close_frac = close_qty / qty if qty > 0 else 0.0
                    realized = (basis - fill_price) * close_qty - fee * close_frac
                    self.balance += realized
                    new_held = held + close_qty
                    remaining = qty - close_qty

                    if remaining > 0:
                        # Flip into a long for the leftover quantity.
                        rem_frac = remaining / qty
                        cost = fill_price * remaining + fee * rem_frac
                        if cost > self.balance:
                            logger.warning(
                                f"Insufficient demo balance to flip into long: need ${cost:,.2f}, have ${self.balance:,.2f}"
                            )
                            self._holdings[symbol] = new_held
                            if abs(new_held) < 1e-12:
                                self._avg_entry.pop(symbol, None)
                            return {"id": order_id, "state": "rejected", "reason": "insufficient_balance"}
                        self.balance -= cost
                        new_held += remaining
                        self._avg_entry[symbol] = fill_price
                    else:
                        if abs(new_held) < 1e-12:
                            self._avg_entry.pop(symbol, None)

                    self._holdings[symbol] = new_held
                else:
                    # Opening or extending a long.
                    cost = notional + fee
                    if cost > self.balance:
                        logger.warning(f"Insufficient demo balance: need ${cost:,.2f}, have ${self.balance:,.2f}")
                        return {"id": order_id, "state": "rejected", "reason": "insufficient_balance"}
                    self.balance -= cost
                    new_held = held + qty
                    # Weighted-average entry price.
                    self._avg_entry[symbol] = (
                        ((basis * held) + (fill_price * qty)) / new_held
                        if new_held > 0 else fill_price
                    )
                    self._holdings[symbol] = new_held
            else:  # sell
                if held > 0:
                    # Closing/reducing a long.
                    close_qty = min(qty, held)
                    close_frac = close_qty / qty if qty > 0 else 0.0
                    proceeds = fill_price * close_qty - fee * close_frac
                    self.balance += proceeds
                    new_held = held - close_qty
                    remaining = qty - close_qty

                    if remaining > 0:
                        # Flip into a short for the leftover — no cash change on
                        # short open; basis tracks where we shorted.
                        new_held -= remaining
                        self._avg_entry[symbol] = fill_price
                    else:
                        if abs(new_held) < 1e-12:
                            self._avg_entry.pop(symbol, None)

                    self._holdings[symbol] = new_held
                else:
                    # Opening or extending a short — no balance movement; we
                    # bookmark the entry so closing the short can compute P&L.
                    short_qty = -held  # current short magnitude (>=0)
                    new_held = held - qty  # more negative
                    if held == 0:
                        self._avg_entry[symbol] = fill_price
                    else:
                        total_short = short_qty + qty
                        self._avg_entry[symbol] = (
                            (basis * short_qty + fill_price * qty) / total_short
                            if total_short > 0 else fill_price
                        )
                    self._holdings[symbol] = new_held

        logger.info(
            f"Mock order: {side} {asset_quantity} {symbol} @ ${fill_price:,.2f} "
            f"(mid=${mid_price:,.2f}, slip={SLIPPAGE_BPS:.1f}bps, fee=${fee:.2f}) | balance: ${self.balance:,.2f}"
        )
        await asyncio.sleep(0.05)  # Minimal simulated latency
        return {
            "id": order_id,
            "symbol": symbol,
            "side": side,
            "type": "market",
            "state": "filled",
            "average_price": str(fill_price),
            "quantity": asset_quantity,
        }

    async def place_limit_order(self, symbol: str, side: str, quantity: str, limit_price: str) -> dict:
        order_id = str(uuid.uuid4())
        return {"id": order_id, "state": "open"}

    async def cancel_order(self, order_id: str) -> dict:
        return {"id": order_id, "state": "cancelled"}

    async def get_current_price(self, symbol: str) -> float:
        return await _fetch_real_price(symbol)

    async def get_portfolio_cash(self) -> float:
        return round(self.balance, 4)
