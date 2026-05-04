import base64
import json
import uuid
import time
import logging
from typing import Optional
import nacl.signing
import httpx
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)

# Clock offset to correct for system clock drift (critical for Robinhood timestamp validation)
_CLOCK_OFFSET: int = 0


async def sync_clock_offset():
    """Measure difference between local clock and a trusted server. Call once at startup."""
    global _CLOCK_OFFSET
    try:
        async with httpx.AsyncClient() as client:
            r = await client.head("https://www.google.com", timeout=5)
            date_hdr = r.headers.get("date", "")
            if date_hdr:
                server_ts = parsedate_to_datetime(date_hdr).timestamp()
                _CLOCK_OFFSET = int(server_ts - time.time())
                if _CLOCK_OFFSET != 0:
                    logger.info(f"Clock offset corrected by {_CLOCK_OFFSET}s (system clock was off)")
    except Exception as e:
        logger.warning(f"Could not sync clock offset: {e}")


class RobinhoodCryptoClient:
    BASE = "https://trading.robinhood.com"

    def __init__(self, api_key: str, private_key_b64: str):
        self.api_key = api_key
        raw = base64.b64decode(private_key_b64)
        self.signing_key = nacl.signing.SigningKey(raw[:32])
        self._market_data_forbidden = False  # Cache 403 to avoid hammering

    def _sign(self, method: str, path: str, body: str = "") -> dict:
        ts = int(time.time()) + _CLOCK_OFFSET
        msg = f"{self.api_key}{ts}{path}{method}{body}"
        signed = self.signing_key.sign(msg.encode())
        signature = base64.b64encode(signed.signature).decode()
        return {
            "x-api-key": self.api_key,
            "x-timestamp": str(ts),
            "x-signature": signature,
            "Content-Type": "application/json"
        }

    async def _get(self, path: str) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.BASE}{path}",
                headers=self._sign("GET", path),
                timeout=15
            )
            if not resp.is_success:
                try:
                    detail = resp.json()
                except Exception:
                    detail = resp.text
                raise httpx.HTTPStatusError(
                    f"{resp.status_code} {resp.reason_phrase} — {detail}",
                    request=resp.request,
                    response=resp,
                )
            return resp.json()

    async def _post(self, path: str, body: dict) -> dict:
        b = json.dumps(body)
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.BASE}{path}",
                headers=self._sign("POST", path, b),
                content=b,
                timeout=15
            )
            if not resp.is_success:
                try:
                    detail = resp.json()
                except Exception:
                    detail = resp.text
                raise httpx.HTTPStatusError(
                    f"{resp.status_code} {resp.reason_phrase} — {detail}",
                    request=resp.request,
                    response=resp,
                )
            return resp.json()

    async def get_account(self) -> dict:
        return await self._get("/api/v1/crypto/trading/accounts/")

    async def get_holdings(self) -> dict:
        return await self._get("/api/v1/crypto/trading/holdings/")

    async def get_best_bid_ask(self, symbol: str) -> dict:
        return await self._get(f"/api/v1/crypto/marketdata/best_bid_ask/?symbol={symbol}")

    async def get_orders(self) -> dict:
        return await self._get("/api/v1/crypto/trading/orders/")

    async def get_order(self, order_id: str) -> dict:
        return await self._get(f"/api/v1/crypto/trading/orders/{order_id}/")

    async def place_market_order(self, symbol: str, side: str, asset_quantity: str) -> dict:
        return await self._post("/api/v1/crypto/trading/orders/", {
            "client_order_id": str(uuid.uuid4()),
            "side": side,
            "type": "market",
            "symbol": symbol,
            "market_order_config": {"asset_quantity": asset_quantity}
        })

    async def place_limit_order(self, symbol: str, side: str, quantity: str, limit_price: str) -> dict:
        return await self._post("/api/v1/crypto/trading/orders/", {
            "client_order_id": str(uuid.uuid4()),
            "side": side,
            "type": "limit",
            "symbol": symbol,
            "limit_order_config": {
                "asset_quantity": quantity,
                "limit_price": limit_price,
                "time_in_force": "gtc"
            }
        })

    async def cancel_order(self, order_id: str) -> dict:
        return await self._post(f"/api/v1/crypto/trading/orders/{order_id}/cancel/", {})

    async def get_current_price(self, symbol: str) -> float:
        # Try Robinhood market data first (skip if previously got 403)
        if not self._market_data_forbidden:
            try:
                data = await self.get_best_bid_ask(symbol)
                results = data.get("results", [{}])
                if results:
                    bid = float(results[0].get("bid_inclusive_of_sell_spread", 0))
                    ask = float(results[0].get("ask_inclusive_of_buy_spread", 0))
                    if bid > 0 and ask > 0:
                        return (bid + ask) / 2
            except Exception as e:
                err_str = str(e)
                if "403" in err_str or "401" in err_str or "not active" in err_str.lower():
                    self._market_data_forbidden = True
                    logger.info(f"Robinhood market data not available ({err_str[:80]}) — using Coinbase for prices")
                else:
                    logger.warning(f"best_bid_ask failed ({e}), trying Coinbase")
        # Fallback: Coinbase public API (reliable, no auth needed)
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"https://api.coinbase.com/v2/prices/{symbol}/spot", timeout=10)
                if r.is_success:
                    return float(r.json()["data"]["amount"])
                logger.warning(f"Coinbase price fallback non-success for {symbol}: HTTP {r.status_code}")
        except Exception as e:
            logger.warning(f"Coinbase price fallback FAILED for {symbol}: {e}. Returning 0.0 (caller should retry).")
        return 0.0

    async def get_portfolio_cash(self) -> float:
        try:
            data = await self.get_account()
            logger.info(f"Robinhood account response: {json.dumps(data, indent=2)[:500]}")
            # Robinhood may return {"results": [...]} or a flat object
            if "results" in data and isinstance(data["results"], list):
                acct = data["results"][0] if data["results"] else {}
            else:
                acct = data
            # Try multiple possible field names
            for field in ("buying_power", "cash_available_for_trading", "cash", "portfolio_cash"):
                val = acct.get(field)
                if val is not None:
                    return float(val)
            logger.warning(f"No buying_power field found in account data. Keys: {list(acct.keys())}")
            return 0.0
        except Exception as e:
            logger.error(f"Error getting portfolio cash: {e}")
            return 0.0


def create_client(api_key: str, private_key_b64: str) -> Optional[RobinhoodCryptoClient]:
    try:
        return RobinhoodCryptoClient(api_key, private_key_b64)
    except Exception as e:
        logger.error(f"Failed to create RH client: {e}")
        return None
