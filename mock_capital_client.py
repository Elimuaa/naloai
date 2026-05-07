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

    One instance is shared across BOTH the GOLD and US100 bot loops since they
    trade the same Capital.com account. The asyncio.Lock prevents concurrent
    balance mutations when both loops fire simultaneously.

    balance tracks available cash.
    _holdings tracks open positions keyed by deal_id:
        {deal_id: {"epic": str, "size": float, "entry_price": float, "direction": str}}
    _holdings_by_symbol tracks symbol → deal_id for the bot_engine restoration path.
    """

    def __init__(self, symbol: str = "GOLD", balance: float = 10000.0):
        self.symbol = symbol
        self.balance = balance
        self._holdings: dict[str, dict] = {}          # deal_id → position info
        self._holdings_by_symbol: dict[str, str] = {} # symbol  → deal_id (latest open)
        self._price_cache: dict[str, tuple[float, float]] = {}  # symbol → (price, ts)
        self._lock = asyncio.Lock()  # prevent concurrent balance mutations (GOLD + US100 loops)
        logger.info(f"MockCapitalClient initialised — demo balance: ${balance:,.2f} (GOLD + US100 loops share this account)")

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
        """Simulate a market order fill at current price. Lock prevents GOLD+US100 race."""
        epic = self._map_to_yahoo_key(symbol)
        price = await self._fetch_yahoo_price(epic)
        if price <= 0:
            return {"state": "rejected", "reason": "price_unavailable", "id": ""}

        qty = float(quantity)
        direction = "BUY" if side.lower() == "buy" else "SELL"

        async with self._lock:
            cost = price * qty
            if direction == "BUY":
                if self.balance < cost:
                    # Scale down to what we can afford
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
                "epic": epic,
                "size": qty,
                "entry_price": price,
                "deal_id": deal_id,
                "direction": direction,
                "symbol": symbol.upper(),
            }
            self._holdings[deal_id] = pos
            # Also index by symbol so bot_engine restoration path works
            self._holdings_by_symbol[symbol.upper()] = deal_id

        logger.info(f"[DEMO Capital] {direction} {qty} {epic} @ {price:,.4f} | balance=${self.balance:,.2f}")
        return {"id": deal_id, "state": "filled", "average_price": str(price)}

    async def cancel_order(self, order_id: str) -> dict:
        """Close position by deal_id and realise P&L. Lock prevents GOLD+US100 race."""
        async with self._lock:
            # Try direct deal_id lookup first, then symbol lookup
            pos = self._holdings.get(order_id)
            if pos is None:
                # order_id might be a symbol if called from restoration path
                fallback_deal_id = self._holdings_by_symbol.get(order_id.upper())
                if fallback_deal_id:
                    pos = self._holdings.get(fallback_deal_id)
                    order_id = fallback_deal_id or order_id
            if pos is None:
                logger.warning(f"[DEMO Capital] cancel_order: position {order_id} not found")
                return {"error": "position_not_found"}

            epic = pos["epic"]
            qty = pos["size"]
            entry = pos["entry_price"]
            direction = pos["direction"]
            symbol = pos.get("symbol", epic)

            # Fetch close price outside lock is OK since price is read-only
            # but we need it before computing P&L — re-fetch inside lock is fine for demo
            current_price = await self._fetch_yahoo_price(epic)
            if current_price <= 0:
                current_price = entry

            if direction == "BUY":
                proceeds = qty * current_price
                self.balance += proceeds
            else:
                margin = qty * entry * 0.1
                pnl = qty * (entry - current_price)
                self.balance += margin + pnl

            self._holdings.pop(order_id, None)
            # Remove from symbol index if it points to this deal
            if self._holdings_by_symbol.get(symbol) == order_id:
                self._holdings_by_symbol.pop(symbol, None)

        pnl_realised = (qty * (current_price - entry)) if direction == "BUY" else (qty * (entry - current_price))
        logger.info(f"[DEMO Capital] Closed {order_id} {direction} {qty} {epic} @ {current_price:,.4f} | P&L=${pnl_realised:,.2f} | balance=${self.balance:,.2f}")
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
