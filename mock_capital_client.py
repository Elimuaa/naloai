"""
mock_capital_client.py — Demo Capital.com client.

Uses real prices via price_feed (Yahoo Finance for GOLD/US100, Coinbase for crypto).
Simulates fills locally — no real money, no real API calls.
Mirrors the interface of CapitalComClient.
"""

import asyncio
import logging
import uuid
from price_feed import get_price as _get_price

logger = logging.getLogger(__name__)


class MockCapitalClient:
    """
    Paper-trading Capital.com client.

    One instance is shared across BOTH the GOLD and US100 bot loops since they
    trade the same Capital.com account. The asyncio.Lock prevents concurrent
    balance mutations when both loops fire simultaneously.

    balance       — available cash
    _holdings     — open positions keyed by deal_id
    _holdings_by_symbol — symbol → deal_id (for bot_engine restoration path)
    """

    def __init__(self, symbol: str = "GOLD", balance: float = 10000.0):
        self.symbol = symbol
        self.balance = balance
        self._holdings: dict[str, dict] = {}           # deal_id → position info
        self._holdings_by_symbol: dict[str, str] = {}  # symbol  → deal_id
        self._lock = asyncio.Lock()
        logger.info(f"MockCapitalClient initialised — demo balance: ${balance:,.2f}")

    # ── Price ──────────────────────────────────────────────────────────────────

    async def get_current_price(self, symbol: str) -> float:
        """Delegate to shared price_feed (handles GOLD, US100, and crypto)."""
        return await _get_price(symbol)

    # ── Account ────────────────────────────────────────────────────────────────

    async def get_portfolio_cash(self) -> float:
        return round(self.balance, 2)

    async def get_account(self) -> dict:
        return {
            "accounts": [{
                "accountId": "DEMO",
                "accountName": "Demo Account",
                "preferred": True,
                "balance": {
                    "balance": self.balance,
                    "deposit": 0.0,
                    "profitLoss": 0.0,
                    "available": self.balance,
                },
                "currency": "USD",
                "status": "ENABLED",
                "accountType": "CFD",
            }]
        }

    # ── Holdings ───────────────────────────────────────────────────────────────

    async def get_holdings(self) -> dict:
        results = [
            {
                "asset_code": pos["epic"],
                "total_quantity": str(pos["size"]),
                "quantity_available_for_trading": str(pos["size"]),
                "deal_id": pos["deal_id"],
                "direction": pos["direction"],
            }
            for pos in self._holdings.values()
        ]
        return {"results": results}

    # ── Orders ─────────────────────────────────────────────────────────────────

    async def place_market_order(self, symbol: str, side: str, quantity: str) -> dict:
        """Simulate a market order fill. asyncio.Lock prevents GOLD+US100 race."""
        price = await _get_price(symbol)
        if price <= 0:
            return {"state": "rejected", "reason": "price_unavailable", "id": ""}

        qty = float(quantity)
        direction = "BUY" if side.lower() == "buy" else "SELL"

        async with self._lock:
            cost = price * qty
            if direction == "BUY":
                if self.balance < cost:
                    # Scale down to what we can afford (95% of balance)
                    qty = max(0.01, (self.balance * 0.95) / price)
                    qty = round(qty, 4)
                    cost = price * qty
                self.balance -= cost
            else:
                # Short: require 10% margin
                margin = cost * 0.1
                if self.balance < margin:
                    return {"state": "rejected", "reason": "insufficient_margin", "id": ""}
                self.balance -= margin

            deal_id = str(uuid.uuid4())[:8]
            pos = {
                "epic": symbol.upper(),
                "size": qty,
                "entry_price": price,
                "deal_id": deal_id,
                "direction": direction,
                "symbol": symbol.upper(),
            }
            self._holdings[deal_id] = pos
            self._holdings_by_symbol[symbol.upper()] = deal_id

        logger.info(f"[DEMO Capital] {direction} {qty} {symbol} @ {price:,.4f} | balance=${self.balance:,.2f}")
        return {"id": deal_id, "state": "filled", "average_price": str(price)}

    async def cancel_order(self, order_id: str) -> dict:
        """Close position by deal_id (or symbol) and realise P&L."""
        async with self._lock:
            pos = self._holdings.get(order_id)
            if pos is None:
                # Fallback: order_id might be a symbol (bot_engine restoration path)
                fallback = self._holdings_by_symbol.get(order_id.upper())
                if fallback:
                    pos = self._holdings.get(fallback)
                    order_id = fallback or order_id
            if pos is None:
                logger.warning(f"[DEMO Capital] cancel_order: position {order_id} not found")
                return {"error": "position_not_found"}

            epic      = pos["epic"]
            qty       = pos["size"]
            entry     = pos["entry_price"]
            direction = pos["direction"]
            symbol    = pos.get("symbol", epic)

            current_price = await _get_price(epic)
            if current_price <= 0:
                current_price = entry

            if direction == "BUY":
                self.balance += qty * current_price
            else:
                margin = qty * entry * 0.1
                pnl    = qty * (entry - current_price)
                self.balance += margin + pnl

            self._holdings.pop(order_id, None)
            if self._holdings_by_symbol.get(symbol) == order_id:
                self._holdings_by_symbol.pop(symbol, None)

        pnl_realised = (
            qty * (current_price - entry) if direction == "BUY"
            else qty * (entry - current_price)
        )
        logger.info(
            f"[DEMO Capital] Closed {order_id} {direction} {qty} {epic} "
            f"@ {current_price:,.4f} | P&L=${pnl_realised:,.2f} | balance=${self.balance:,.2f}"
        )
        return {"dealId": order_id, "status": "CLOSED"}
