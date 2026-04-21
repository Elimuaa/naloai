"""
capital_client.py — Live Capital.com REST API client.

Duck-typed interface matching RobinhoodCryptoClient:
  get_current_price(symbol) → float
  get_portfolio_cash() → float
  get_holdings() → dict
  get_account() → dict
  place_market_order(symbol, side, quantity) → dict
  cancel_order(order_id) → dict

Capital.com API docs: https://open-api.capital.com/
Auth: POST /session → CST + X-SECURITY-TOKEN headers
Session expires after 10 min of inactivity → keep-alive via GET /ping every 9 min
"""

import asyncio
import logging
import httpx

logger = logging.getLogger(__name__)


class CapitalComClient:
    LIVE_BASE = "https://api-capital.backend-capital.com/api/v1"
    DEMO_BASE = "https://demo-api-capital.backend-capital.com/api/v1"

    def __init__(
        self,
        api_key: str,
        identifier: str,   # Capital.com login email
        password: str,     # Capital.com login password
        demo: bool = False,
    ):
        self.base = self.DEMO_BASE if demo else self.LIVE_BASE
        self.api_key = api_key
        self.identifier = identifier
        self.password = password
        self._cst: str | None = None
        self._security_token: str | None = None
        self._ping_task: asyncio.Task | None = None
        self._login_lock = asyncio.Lock()

    # ── Auth ──────────────────────────────────────────────────────────────────

    async def _login(self):
        """Create session, store CST + X-SECURITY-TOKEN headers."""
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{self.base}/session",
                headers={
                    "X-CAP-API-KEY": self.api_key,
                    "Content-Type": "application/json",
                },
                json={
                    "identifier": self.identifier,
                    "password": self.password,
                    "encryptedPassword": False,
                },
            )
            if r.status_code not in (200, 201):
                raise ValueError(f"Capital.com login failed: {r.status_code} {r.text[:200]}")
            self._cst = r.headers.get("CST") or r.headers.get("cst")
            self._security_token = r.headers.get("X-SECURITY-TOKEN") or r.headers.get("x-security-token")
            if not self._cst or not self._security_token:
                raise ValueError(f"Capital.com login: missing auth headers. Response: {r.text[:300]}")
            logger.info("Capital.com session created")

        # Start background ping to keep session alive
        if self._ping_task is None or self._ping_task.done():
            self._ping_task = asyncio.create_task(self._ping_loop())

    async def _ensure_session(self):
        """Login if not already authenticated."""
        async with self._login_lock:
            if not self._cst:
                await self._login()

    async def _headers(self) -> dict:
        await self._ensure_session()
        return {
            "CST": self._cst,
            "X-SECURITY-TOKEN": self._security_token,
            "Content-Type": "application/json",
        }

    async def _ping_loop(self):
        """Keep session alive every 9 minutes (session expires at 10 min)."""
        while True:
            await asyncio.sleep(540)  # 9 minutes
            try:
                async with httpx.AsyncClient(timeout=10) as c:
                    r = await c.get(f"{self.base}/ping", headers=await self._headers())
                    if r.status_code == 401:
                        # Session expired — force re-login on next call
                        logger.warning("Capital.com session expired, will re-login")
                        self._cst = None
                        self._security_token = None
            except Exception as e:
                logger.warning(f"Capital.com ping failed: {e}")
                self._cst = None  # Force re-login on next request

    # ── Price ─────────────────────────────────────────────────────────────────

    async def get_current_price(self, symbol: str) -> float:
        """GET /markets?epics=SYMBOL → mid of bid + offer."""
        # Normalize symbol
        sym = self._normalize_symbol(symbol)
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(
                    f"{self.base}/markets",
                    headers=await self._headers(),
                    params={"epics": sym},
                )
                if r.status_code == 401:
                    self._cst = None
                    return 0.0
                data = r.json()
                details = data.get("marketDetails", [])
                if not details:
                    return 0.0
                snap = details[0].get("snapshot", {})
                bid = float(snap.get("bid", 0) or 0)
                offer = float(snap.get("offer", 0) or snap.get("ofr", 0) or 0)
                if bid and offer:
                    return (bid + offer) / 2
                return bid or offer
        except Exception as e:
            logger.warning(f"Capital.com price fetch failed for {sym}: {e}")
            return 0.0

    # ── Account ───────────────────────────────────────────────────────────────

    async def get_portfolio_cash(self) -> float:
        """GET /accounts → preferred account available balance."""
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{self.base}/accounts", headers=await self._headers())
                data = r.json()
                accounts = data.get("accounts", [])
                if not accounts:
                    return 0.0
                # Find preferred account, fall back to first
                preferred = next((a for a in accounts if a.get("preferred")), accounts[0])
                return float(preferred.get("balance", {}).get("available", 0) or 0)
        except Exception as e:
            logger.error(f"Capital.com get_portfolio_cash failed: {e}")
            return 0.0

    async def get_account(self) -> dict:
        """GET /accounts → raw account data."""
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{self.base}/accounts", headers=await self._headers())
                return r.json()
        except Exception as e:
            logger.error(f"Capital.com get_account failed: {e}")
            return {}

    # ── Holdings ──────────────────────────────────────────────────────────────

    async def get_holdings(self) -> dict:
        """GET /positions → translated to Robinhood-style {results: [...]}."""
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{self.base}/positions", headers=await self._headers())
                data = r.json()
                positions = data.get("positions", [])
                results = []
                for p in positions:
                    pos = p.get("position", {})
                    size = str(pos.get("size", 0))
                    epic = pos.get("epic", "")
                    results.append({
                        "asset_code": epic,
                        "total_quantity": size,
                        "quantity_available_for_trading": size,
                        "deal_id": pos.get("dealId", ""),
                        "direction": pos.get("direction", ""),
                    })
                return {"results": results}
        except Exception as e:
            logger.error(f"Capital.com get_holdings failed: {e}")
            return {"results": []}

    # ── Orders ────────────────────────────────────────────────────────────────

    async def place_market_order(self, symbol: str, side: str, quantity: str) -> dict:
        """
        POST /positions → confirm fill via GET /confirms/{dealReference}.
        Returns {id, state, average_price} matching Robinhood shape.
        """
        sym = self._normalize_symbol(symbol)
        direction = "BUY" if side.lower() == "buy" else "SELL"
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(
                    f"{self.base}/positions",
                    headers=await self._headers(),
                    json={
                        "epic": sym,
                        "direction": direction,
                        "size": float(quantity),
                        "guaranteedStop": False,
                    },
                )
                body = r.json()

            reason = body.get("reason", "")
            if reason != "SUCCESS":
                logger.warning(f"Capital.com order rejected: {reason} — {body}")
                return {"state": "rejected", "reason": reason, "id": ""}

            deal_ref = body.get("dealReference", "")
            if not deal_ref:
                return {"state": "rejected", "reason": "No dealReference", "id": ""}

            # Wait for fill confirmation (Capital.com uses 2-step flow)
            await asyncio.sleep(0.8)
            async with httpx.AsyncClient(timeout=10) as c:
                conf_r = await c.get(
                    f"{self.base}/confirms/{deal_ref}",
                    headers=await self._headers(),
                )
                conf = conf_r.json()

            deal_status = conf.get("dealStatus", "")
            state = "filled" if deal_status == "ACCEPTED" else "rejected"
            avg_price = str(conf.get("level", 0))

            affected = conf.get("affectedDeals", [])
            deal_id = affected[0].get("dealId", deal_ref) if affected else deal_ref

            logger.info(f"Capital.com order {direction} {sym} x{quantity}: {state} @ {avg_price}")
            return {"id": deal_id, "state": state, "average_price": avg_price}

        except Exception as e:
            logger.error(f"Capital.com place_market_order failed: {e}")
            return {"state": "error", "reason": str(e), "id": ""}

    async def cancel_order(self, order_id: str) -> dict:
        """Close an open position by dealId via DELETE /positions/{dealId}."""
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.delete(
                    f"{self.base}/positions/{order_id}",
                    headers=await self._headers(),
                )
                return r.json()
        except Exception as e:
            logger.error(f"Capital.com cancel_order failed: {e}")
            return {"error": str(e)}

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        """Map internal symbols to Capital.com epic codes."""
        mapping = {
            "BTC-USD": "BITCOIN",
            "ETH-USD": "ETHEREUM",
            "SOL-USD": "SOLUSD",
            "DOGE-USD": "DOGECOIN",
            "US100": "US100",
            "NAS100": "US100",
            "GOLD": "GOLD",
            "XAU/USD": "GOLD",
            "XAUUSD": "GOLD",
        }
        return mapping.get(symbol.upper(), symbol.upper())
