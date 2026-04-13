"""
Mock Robinhood client for UI testing without real API credentials.
Simulates realistic price movements and order execution.
"""
import asyncio
import random
import uuid
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Simulated base prices per symbol
BASE_PRICES = {
    "BTC-USD": 67500.0,
    "ETH-USD": 3520.0,
    "SOL-USD": 145.0,
    "DOGE-USD": 0.38,
}

# Per-symbol price state (shared across calls to simulate continuity)
_price_state: dict[str, float] = {}


def _get_simulated_price(symbol: str) -> float:
    base = BASE_PRICES.get(symbol, 100.0)
    if symbol not in _price_state:
        _price_state[symbol] = base
    # Random walk: ±0.15% per tick
    change_pct = (random.random() - 0.5) * 0.003
    _price_state[symbol] *= (1 + change_pct)
    # Mean-revert gently back toward base
    _price_state[symbol] += (base - _price_state[symbol]) * 0.01
    return round(_price_state[symbol], 2)


class MockRobinhoodClient:
    """Drop-in replacement for RobinhoodCryptoClient that returns fake data."""

    def __init__(self, symbol: str = "BTC-USD", balance: float = 10000.0):
        self.symbol = symbol
        self.balance = balance
        self._holdings: dict[str, float] = {}  # symbol -> quantity
        logger.info(f"🎭 MockRobinhoodClient initialized — demo balance: ${balance:,.2f}")

    async def get_account(self) -> dict:
        return {"buying_power": str(round(self.balance, 4)), "buying_power_currency": "USD"}

    async def get_holdings(self) -> dict:
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
        price = _get_simulated_price(symbol)
        spread = price * 0.0002
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
        price = _get_simulated_price(symbol)
        qty = float(asset_quantity)
        order_id = str(uuid.uuid4())

        if side == "buy":
            cost = price * qty
            self.balance -= cost
            self._holdings[symbol] = self._holdings.get(symbol, 0) + qty
        else:
            proceeds = price * qty
            self.balance += proceeds
            self._holdings[symbol] = max(0, self._holdings.get(symbol, 0) - qty)

        logger.info(f"🎭 Mock order: {side} {asset_quantity} {symbol} @ ${price:.2f} | balance: ${self.balance:,.2f}")
        await asyncio.sleep(0.1)  # simulate network latency
        return {
            "id": order_id,
            "symbol": symbol,
            "side": side,
            "type": "market",
            "state": "filled",
            "average_price": str(price),
            "quantity": asset_quantity,
        }

    async def place_limit_order(self, symbol: str, side: str, quantity: str, limit_price: str) -> dict:
        order_id = str(uuid.uuid4())
        return {"id": order_id, "state": "open"}

    async def cancel_order(self, order_id: str) -> dict:
        return {"id": order_id, "state": "cancelled"}

    async def get_current_price(self, symbol: str) -> float:
        return _get_simulated_price(symbol)

    async def get_portfolio_cash(self) -> float:
        return round(self.balance, 4)
