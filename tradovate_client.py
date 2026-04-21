"""
tradovate_client.py — Live Tradovate REST API client.

Duck-typed interface matching RobinhoodCryptoClient:
  get_current_price(symbol) → float
  get_portfolio_cash() → float
  get_holdings() → dict
  get_account() → dict
  place_market_order(symbol, side, quantity) → dict
  cancel_order(order_id) → dict

Tradovate API docs: https://api.tradovate.com/
Auth: POST /auth/accessTokenRequest → Bearer token (90 min TTL)
Futures contracts expire quarterly — auto-detect front-month (NQZ24, GCZ24).
Position size: always integer contracts (minimum 1).
"""

import asyncio
import logging
import time
import httpx
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# CME month codes for quarterly contracts
# NQ trades: H (Mar), M (Jun), U (Sep), Z (Dec)
# GC trades every month: F G H J K M N Q U V X Z
NQ_MONTHS = ["H", "M", "U", "Z"]
GC_MONTHS = ["F", "G", "H", "J", "K", "M", "N", "Q", "U", "V", "X", "Z"]
MONTH_CODES = "FGHJKMNQUVXZ"  # 1-indexed: Jan=F, Feb=G, Mar=H, ...
MONTH_MAP = {i + 1: c for i, c in enumerate(MONTH_CODES)}


def _next_quarterly_symbol(root: str) -> str:
    """
    Build the front-month symbol for NQ or GC.
    E.g. NQZ24, GCZ24 for Dec 2024.
    Falls back to simple calculation — Tradovate's /contract/find is preferred.
    """
    now = datetime.now(timezone.utc)
    year2 = str(now.year)[-2:]
    month = now.month
    day = now.day

    valid_months = NQ_MONTHS if root == "NQ" else GC_MONTHS

    # Find next expiry month (contracts expire around the 15th)
    candidate_month = month if day < 15 else (month % 12 + 1)
    candidate_year = now.year if (day < 15 or month < 12) else now.year + 1

    for _ in range(12):
        code = MONTH_MAP.get(candidate_month, "Z")
        if code in valid_months:
            y2 = str(candidate_year)[-2:]
            return f"{root}{code}{y2}"
        candidate_month = candidate_month % 12 + 1
        if candidate_month == 1:
            candidate_year += 1

    return f"{root}Z{year2}"


class TradovateClient:
    DEMO_BASE = "https://demo.tradovateapi.com/v1"
    LIVE_BASE = "https://live.tradovateapi.com/v1"
    MD_BASE = "https://md.tradovateapi.com/v1"

    def __init__(
        self,
        username: str,
        password: str,
        account_id: int,
        demo: bool = False,
    ):
        self.base = self.DEMO_BASE if demo else self.LIVE_BASE
        self.username = username
        self.password = password
        self.account_id = account_id
        self._token: str | None = None
        self._token_expires: float = 0
        self._login_lock = asyncio.Lock()
        # Contract cache: root → (symbol, expiry_ts)
        self._contract_cache: dict[str, tuple[str, float]] = {}

    # ── Auth ──────────────────────────────────────────────────────────────────

    async def _login(self):
        """Authenticate and store access token (valid 90 min)."""
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{self.base}/auth/accessTokenRequest",
                json={
                    "name": self.username,
                    "password": self.password,
                    "appId": "nalo.ai",
                    "appVersion": "1.0",
                    "cid": 0,
                    "sec": "",
                },
            )
            if r.status_code not in (200, 201):
                raise ValueError(f"Tradovate login failed: {r.status_code} {r.text[:200]}")
            body = r.json()
            token = body.get("accessToken")
            if not token:
                raise ValueError(f"Tradovate login: no accessToken in response: {body}")
            self._token = token
            self._token_expires = time.time() + 85 * 60  # refresh 5 min before expiry
            logger.info("Tradovate access token obtained")

    async def _ensure_auth(self):
        """Re-login if token is missing or expired."""
        async with self._login_lock:
            if not self._token or time.time() > self._token_expires:
                await self._login()

    async def _headers(self) -> dict:
        await self._ensure_auth()
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    # ── Contract resolution ───────────────────────────────────────────────────

    async def _front_month_symbol(self, root: str) -> str:
        """Find active front-month contract for NQ or GC."""
        cache_key = root
        if cache_key in self._contract_cache:
            sym, expires = self._contract_cache[cache_key]
            if time.time() < expires:
                return sym

        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(
                    f"{self.base}/contract/find",
                    headers=await self._headers(),
                    params={"name": root},
                )
                contracts = r.json()
                if isinstance(contracts, list) and contracts:
                    sym = contracts[0].get("name", _next_quarterly_symbol(root))
                else:
                    sym = _next_quarterly_symbol(root)
        except Exception as e:
            logger.warning(f"Tradovate contract lookup failed for {root}: {e}")
            sym = _next_quarterly_symbol(root)

        # Cache for 1 hour
        self._contract_cache[cache_key] = (sym, time.time() + 3600)
        return sym

    def _to_root(self, symbol: str) -> str:
        """Map user-facing symbol to CME root: US100/NQ → NQ, GOLD/GC → GC."""
        s = symbol.upper()
        if s in ("US100", "NAS100", "NDX") or s.startswith("NQ"):
            return "NQ"
        return "GC"

    # ── Price ─────────────────────────────────────────────────────────────────

    async def get_current_price(self, symbol: str) -> float:
        """Get latest bar close price from Tradovate market data feed."""
        root = self._to_root(symbol)
        contract = await self._front_month_symbol(root)
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(
                    f"{self.MD_BASE}/chart/getchart",
                    headers=await self._headers(),
                    params={
                        "symbol": contract,
                        "chartDescription": {
                            "underlyingType": "MinuteBar",
                            "elementSize": 1,
                            "elementSizeUnit": "UnderlyingUnits",
                            "withHistogram": False,
                        },
                        "timeRange": {"asMuchAsElements": 2},
                    },
                )
                data = r.json()
                # The response has bars in data["bars"] or data["charts"][0]["bars"]
                bars = data.get("bars") or []
                if not bars and isinstance(data, dict):
                    for key in ("charts", "data"):
                        if data.get(key):
                            bars = data[key][0].get("bars", []) if isinstance(data[key][0], dict) else []
                            break
                if bars:
                    return float(bars[-1].get("close", 0))
        except Exception as e:
            logger.warning(f"Tradovate price fetch failed for {contract}: {e}")

        return 0.0

    # ── Account ───────────────────────────────────────────────────────────────

    async def get_portfolio_cash(self) -> float:
        """GET /cashbalance/getcashbalancesnapshot → totalCashValue."""
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(
                    f"{self.base}/cashbalance/getcashbalancesnapshot",
                    headers=await self._headers(),
                    params={"accountId": self.account_id},
                )
                data = r.json()
                return float(data.get("totalCashValue", 0) or 0)
        except Exception as e:
            logger.error(f"Tradovate get_portfolio_cash failed: {e}")
            return 0.0

    async def get_account(self) -> dict:
        """GET /account/item → raw account object."""
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(
                    f"{self.base}/account/item",
                    headers=await self._headers(),
                    params={"id": self.account_id},
                )
                return r.json()
        except Exception as e:
            logger.error(f"Tradovate get_account failed: {e}")
            return {}

    # ── Holdings ──────────────────────────────────────────────────────────────

    async def get_holdings(self) -> dict:
        """GET /position/list → Robinhood-style {results: [...]}."""
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{self.base}/position/list", headers=await self._headers())
                positions = r.json() or []
                results = []
                for p in positions:
                    if p.get("accountId") != self.account_id:
                        continue
                    net_pos = abs(p.get("netPos", 0))
                    if net_pos == 0:
                        continue
                    results.append({
                        "asset_code": str(p.get("contractId", "")),
                        "total_quantity": str(net_pos),
                        "quantity_available_for_trading": str(net_pos),
                        "position_id": str(p.get("id", "")),
                        "net_price": str(p.get("netPrice", 0)),
                    })
                return {"results": results}
        except Exception as e:
            logger.error(f"Tradovate get_holdings failed: {e}")
            return {"results": []}

    # ── Orders ────────────────────────────────────────────────────────────────

    async def place_market_order(self, symbol: str, side: str, quantity: str) -> dict:
        """
        POST /order/placeorder → returns {id, state, average_price}.
        Quantity is always whole contracts (integer).
        """
        root = self._to_root(symbol)
        contract = await self._front_month_symbol(root)
        action = "Buy" if side.lower() == "buy" else "Sell"
        qty = max(1, int(float(quantity)))

        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(
                    f"{self.base}/order/placeorder",
                    headers=await self._headers(),
                    json={
                        "accountSpec": self.username,
                        "accountId": self.account_id,
                        "action": action,
                        "symbol": contract,
                        "orderQty": qty,
                        "orderType": "Market",
                        "isAutomated": True,
                    },
                )
                body = r.json()

            ord_status = body.get("ordStatus", "")
            state = "filled" if ord_status not in ("Rejected", "Canceled", "error") else "rejected"
            avg_px = str(body.get("avgPx", 0))
            order_id = str(body.get("orderId", ""))

            logger.info(f"Tradovate order {action} {qty} {contract}: {state} @ {avg_px}")
            return {"id": order_id, "state": state, "average_price": avg_px}

        except Exception as e:
            logger.error(f"Tradovate place_market_order failed: {e}")
            return {"state": "error", "reason": str(e), "id": ""}

    async def cancel_order(self, order_id: str) -> dict:
        """
        Close/liquidate a position.
        order_id here is the positionId from get_holdings.
        """
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(
                    f"{self.base}/order/liquidateposition",
                    headers=await self._headers(),
                    json={
                        "accountId": self.account_id,
                        "positionId": int(order_id),
                        "orderType": "Market",
                        "isAutomated": True,
                    },
                )
                return r.json()
        except Exception as e:
            logger.error(f"Tradovate cancel_order failed: {e}")
            return {"error": str(e)}
