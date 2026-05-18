"""
market_router.py — Price endpoints for the frontend.

Uses the shared price_feed module (3-second cache, multi-source fallback).
Supports all symbols: BTC-USD, ETH-USD, SOL-USD, DOGE-USD, GOLD, US100.
"""

from fastapi import APIRouter, HTTPException
from price_feed import get_price, get_prices, cache_snapshot

router = APIRouter(prefix="/api/market", tags=["market"])


@router.get("/price")
async def price(symbol: str = "BTC-USD"):
    """Get current price for a single symbol."""
    p = await get_price(symbol)
    if p <= 0:
        raise HTTPException(503, f"Unable to fetch price for {symbol} — all sources failed")
    return {"symbol": symbol, "price": p}


@router.get("/prices")
async def prices(symbols: str = "BTC-USD,ETH-USD,GOLD,US100"):
    """Get current prices for multiple symbols (comma-separated). Fetches concurrently."""
    sym_list = [s.strip() for s in symbols.split(",") if s.strip()]
    result = await get_prices(sym_list)
    return {"prices": result}


@router.get("/cache")
async def price_cache():
    """Return current price cache state (for debugging/admin)."""
    return {"cache": cache_snapshot()}
