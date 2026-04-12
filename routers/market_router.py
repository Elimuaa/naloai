from fastapi import APIRouter
import httpx
import logging

router = APIRouter(prefix="/api/market", tags=["market"])
logger = logging.getLogger(__name__)


async def _fetch_price(symbol: str) -> float | None:
    base = symbol.split("-")[0].upper()  # BTC-USD → BTC

    # 1. Coinbase (primary)
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(f"https://api.coinbase.com/v2/prices/{symbol}/spot")
            r.raise_for_status()
            return float(r.json()["data"]["amount"])
    except Exception as e:
        logger.warning(f"Coinbase failed for {symbol}: {e}")

    # 2. Kraken (fallback)
    try:
        kraken_pair = "XBTUSD" if base == "BTC" else f"{base}USD"
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(f"https://api.kraken.com/0/public/Ticker?pair={kraken_pair}")
            r.raise_for_status()
            result = r.json().get("result", {})
            ticker = next(iter(result.values()), None)
            if ticker:
                return float(ticker["c"][0])
    except Exception as e:
        logger.warning(f"Kraken failed for {symbol}: {e}")

    # 3. CryptoCompare (last resort)
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(f"https://min-api.cryptocompare.com/data/price?fsym={base}&tsyms=USD")
            r.raise_for_status()
            return float(r.json()["USD"])
    except Exception as e:
        logger.error(f"All price sources failed for {symbol}: {e}")

    return None


@router.get("/price")
async def get_price(symbol: str = "BTC-USD"):
    price = await _fetch_price(symbol)
    if price is None:
        logger.error(f"All price sources exhausted for {symbol}")
        from fastapi import HTTPException
        raise HTTPException(503, f"Unable to fetch price for {symbol} — all sources failed")
    return {"symbol": symbol, "price": price}
