"""
mock_tradovate_client.py — Demo Tradovate client.

Uses real prices from Yahoo Finance (NQ=F for NAS100, GC=F for Gold).
Simulates fills locally — no real money, no real API calls.
Position size: always integer contracts.

Notional value per contract (for P&L math):
  NQ (E-mini NASDAQ-100): $20 × price
  GC (Gold futures): 100 oz × price
"""

import logging
import time
import uuid
import httpx

logger = logging.getLogger(__name__)


YAHOO_SOURCES = {
    "NQ": [
        "https://query1.finance.yahoo.com/v8/finance/chart/NQ%3DF",
        "https://query2.finance.yahoo.com/v8/finance/chart/NQ%3DF",
    ],
    "GC": [
        "https://query1.finance.yahoo.com/v8/finance/chart/GC%3DF",
        "https://query2.finance.yahoo.com/v8/finance/chart/GC%3DF",
    ],
}

# Contract multipliers (points → USD)
CONTRACT_MULTIPLIER = {
    "NQ": 20,    # E-mini: $20/point
    "GC": 100,   # Gold: 100 oz/contract
}


class MockTradovateClient:
    """
    Paper-trading Tradovate client.

    balance tracks available cash.
    _positions: {position_id: {root, qty, entry_price, direction}}
    """

    def __init__(self, symbol: str = "NQ", balance: float = 10000.0):
        self.symbol = symbol
        self.balance = balance
        self._positions: dict[str, dict] = {}
        self._price_cache: dict[str, tuple[float, float]] = {}

    # ── Price ─────────────────────────────────────────────────────────────────

    def _to_root(self, symbol: str) -> str:
        s = symbol.upper()
        if s in ("US100", "NAS100", "NDX") or s.startswith("NQ"):
            return "NQ"
        return "GC"

    async def _fetch_yahoo_price(self, root: str) -> float:
        now = time.time()
        if root in self._price_cache:
            price, ts = self._price_cache[root]
            if now - ts < 3:
                return price

        for url in YAHOO_SOURCES.get(root, []):
            try:
                async with httpx.AsyncClient(timeout=8, headers={"User-Agent": "Mozilla/5.0"}) as c:
                    r = await c.get(url)
                    data = r.json()
                    price = float(data["chart"]["result"][0]["meta"]["regularMarketPrice"])
                    self._price_cache[root] = (price, now)
                    return price
            except Exception:
                continue

        if root in self._price_cache:
            return self._price_cache[root][0]
        return 0.0

    async def get_current_price(self, symbol: str) -> float:
        root = self._to_root(symbol)
        return await self._fetch_yahoo_price(root)

    # ── Account ───────────────────────────────────────────────────────────────

    async def get_portfolio_cash(self) -> float:
        return round(self.balance, 2)

    async def get_account(self) -> dict:
        return {
            "id": 0,
            "name": "Demo Account",
            "accountType": "Customer",
            "active": True,
            "balance": self.balance,
        }

    # ── Holdings ──────────────────────────────────────────────────────────────

    async def get_holdings(self) -> dict:
        results = []
        for pos_id, pos in self._positions.items():
            results.append({
                "asset_code": pos["root"],
                "total_quantity": str(pos["qty"]),
                "quantity_available_for_trading": str(pos["qty"]),
                "position_id": pos_id,
                "net_price": str(pos["entry_price"]),
            })
        return {"results": results}

    # ── Orders ────────────────────────────────────────────────────────────────

    async def place_market_order(self, symbol: str, side: str, quantity: str) -> dict:
        """Simulate a futures market order."""
        root = self._to_root(symbol)
        price = await self._fetch_yahoo_price(root)
        if price <= 0:
            return {"state": "rejected", "reason": "price_unavailable", "id": ""}

        qty = max(1, int(float(quantity)))
        direction = "Buy" if side.lower() == "buy" else "Sell"
        multiplier = CONTRACT_MULTIPLIER.get(root, 1)
        notional = price * qty * multiplier

        # Require 10% margin (simplified)
        margin = notional * 0.1
        if self.balance < margin:
            qty = max(1, int(self.balance * 0.9 / (price * multiplier * 0.1)))
            margin = price * qty * multiplier * 0.1

        self.balance -= margin

        pos_id = str(uuid.uuid4())[:8]
        self._positions[pos_id] = {
            "root": root,
            "qty": qty,
            "entry_price": price,
            "direction": direction,
            "margin": margin,
            "multiplier": multiplier,
        }

        logger.info(f"[DEMO Tradovate] {direction} {qty} {root} @ {price:.2f}, margin={margin:.2f}, balance={self.balance:.2f}")
        return {"id": pos_id, "state": "filled", "average_price": str(price)}

    async def cancel_order(self, order_id: str) -> dict:
        """Close position and settle P&L."""
        if order_id not in self._positions:
            return {"error": "position_not_found"}

        pos = self._positions.pop(order_id)
        root = pos["root"]
        qty = pos["qty"]
        entry = pos["entry_price"]
        direction = pos["direction"]
        multiplier = pos["multiplier"]
        margin = pos["margin"]

        current_price = await self._fetch_yahoo_price(root)
        if current_price <= 0:
            current_price = entry

        if direction == "Buy":
            pnl = (current_price - entry) * qty * multiplier
        else:
            pnl = (entry - current_price) * qty * multiplier

        self.balance += margin + pnl

        logger.info(f"[DEMO Tradovate] Closed {order_id} {direction} {qty} {root} @ {current_price:.2f}, P&L={pnl:+.2f}")
        return {"positionId": order_id, "status": "CLOSED", "pnl": pnl}
