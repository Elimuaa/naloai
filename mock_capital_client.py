"""
mock_capital_client.py — Demo Capital.com client.

Uses real prices from Yahoo Finance (GC=F for Gold, ^NDX for NAS100).
Simulates fills locally — no real money, no real API calls.
Mirrors the interface of CapitalComClient.
"""

import asyncio
import logging
import time
import uuid
import httpx

logger = logging.getLogger(__name__)


YAHOO_SOURCES = {
    "GOLD": [
        "https://query1.finance.yahoo.com/v8/finance/chart/GC%3DF",
        "https://query2.finance.yahoo.com/v8/finance/chart/GC%3DF",
    ],
    "US100": [
        "https://query1.finance.yahoo.com/v8/finance/chart/%5ENDX",
        "https://query2.finance.yahoo.com/v8/finance/chart/%5ENDX",
    ],
    # Fallback crypto prices
    "BITCOIN": [
        "https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD",
        "https://query2.finance.yahoo.com/v8/finance/chart/BTC-USD",
    ],
}


class MockCapitalClient:
    """
    Paper-trading Capital.com client.

    balance tracks available cash.
    _holdings tracks open positions: {epic: {"size": float, "entry_price": float, "deal_id": str, "direction": str}}
    """

    def __init__(self, symbol: str = "GOLD", balance: float = 10000.0):
        self.symbol = symbol
        self.balance = balance
        self._holdings: dict[str, dict] = {}
        self._price_cache: dict[str, tuple[float, float]] = {}  # symbol → (price, ts)

    # ── Price ─────────────────────────────────────────────────────────────────

    async def _fetch_yahoo_price(self, epic: str) -> float:
        """Fetch real market price from Yahoo Finance with 3-second cache."""
        sym = epic.upper()
        now = time.time()
        if sym in self._price_cache:
            price, ts = self._price_cache[sym]
            if now - ts < 3:
                return price

        urls = YAHOO_SOURCES.get(sym, [])
        last_err = None
        for url in urls:
            try:
                async with httpx.AsyncClient(timeout=8, headers={"User-Agent": "Mozilla/5.0"}) as c:
                    r = await c.get(url)
                    data = r.json()
                    price = float(data["chart"]["result"][0]["meta"]["regularMarketPrice"])
                    self._price_cache[sym] = (price, now)
                    return price
            except Exception as e:
                last_err = e
                logger.debug(f"Yahoo price fetch failed for {sym} via {url}: {e}")
                continue

        if last_err is not None:
            logger.warning(f"All Yahoo Finance sources failed for {sym} (last: {last_err}); using cached if available")

        # Return cached price even if stale
        if sym in self._price_cache:
            return self._price_cache[sym][0]
        return 0.0

    async def get_current_price(self, symbol: str) -> float:
        """Get current price for any symbol."""
        sym = self._map_to_yahoo_key(symbol)
        return await self._fetch_yahoo_price(sym)

    # ── Account ───────────────────────────────────────────────────────────────

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

    # ── Holdings ──────────────────────────────────────────────────────────────

    async def get_holdings(self) -> dict:
        results = []
        for epic, pos in self._holdings.items():
            results.append({
                "asset_code": epic,
                "total_quantity": str(pos["size"]),
                "quantity_available_for_trading": str(pos["size"]),
                "deal_id": pos["deal_id"],
                "direction": pos["direction"],
            })
        return {"results": results}

    # ── Orders ────────────────────────────────────────────────────────────────

    async def place_market_order(self, symbol: str, side: str, quantity: str) -> dict:
        """Simulate a market order fill at current price."""
        epic = self._map_to_yahoo_key(symbol)
        price = await self._fetch_yahoo_price(epic)
        if price <= 0:
            return {"state": "rejected", "reason": "price_unavailable", "id": ""}

        qty = float(quantity)
        direction = "BUY" if side.lower() == "buy" else "SELL"
        cost = price * qty

        if direction == "BUY":
            if self.balance < cost:
                # Scale down to what we can afford
                qty = max(0.01, (self.balance * 0.95) / price)
                qty = round(qty, 2)
                cost = price * qty
            self.balance -= cost
        else:
            # Opening a short: hold margin, record as negative size
            if self.balance < cost * 0.1:  # 10% margin requirement
                return {"state": "rejected", "reason": "insufficient_margin", "id": ""}
            self.balance -= cost * 0.1

        deal_id = str(uuid.uuid4())[:8]
        self._holdings[deal_id] = {
            "epic": epic,
            "size": qty,
            "entry_price": price,
            "deal_id": deal_id,
            "direction": direction,
        }

        logger.info(f"[DEMO Capital] {direction} {qty} {epic} @ {price:.4f}, balance={self.balance:.2f}")
        return {"id": deal_id, "state": "filled", "average_price": str(price)}

    async def cancel_order(self, order_id: str) -> dict:
        """Close position and return cash."""
        if order_id not in self._holdings:
            return {"error": "position_not_found"}

        pos = self._holdings.pop(order_id)
        epic = pos["epic"]
        qty = pos["size"]
        entry = pos["entry_price"]
        direction = pos["direction"]

        current_price = await self._fetch_yahoo_price(epic)
        if current_price <= 0:
            current_price = entry

        if direction == "BUY":
            proceeds = qty * current_price
            self.balance += proceeds
        else:
            # Short close: return margin + pnl
            margin = qty * entry * 0.1
            pnl = qty * (entry - current_price)  # profit if price fell
            self.balance += margin + pnl

        logger.info(f"[DEMO Capital] Closed {order_id} {direction} {qty} {epic} @ {current_price:.4f}")
        return {"dealId": order_id, "status": "CLOSED"}

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _map_to_yahoo_key(symbol: str) -> str:
        """Map symbol to Yahoo Finance key used in YAHOO_SOURCES."""
        mapping = {
            "GOLD": "GOLD",
            "XAU/USD": "GOLD",
            "XAUUSD": "GOLD",
            "US100": "US100",
            "NAS100": "US100",
            "USTEC": "US100",
            "BTC-USD": "BITCOIN",
            "BITCOIN": "BITCOIN",
        }
        return mapping.get(symbol.upper(), symbol.upper())
