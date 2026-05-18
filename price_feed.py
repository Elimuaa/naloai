"""
price_feed.py — Unified price feed for all instruments.

Single source of truth for live prices across the whole platform.
Used by market_router, bot_engine, mock_capital_client, mock_robinhood.

Price source priority:
  Crypto   → Revolut X (live exchange) → Coinbase → Kraken → CryptoCompare
  Gold     → Yahoo Finance (GC=F)      → Metals-API fallback
  NAS100   → Yahoo Finance (^NDX)      → fallback

Caching: 3-second TTL to avoid hammering APIs when multiple loops request
the same symbol simultaneously (GOLD loop + US100 loop share one account).
"""

from __future__ import annotations

import asyncio
import logging
import time
import httpx
from typing import Optional

logger = logging.getLogger(__name__)

# ── In-process cache ─────────────────────────────────────────────────────────
_cache: dict[str, tuple[float, float]] = {}   # symbol → (price, timestamp)
_CACHE_TTL = 3.0                               # seconds


def _cache_get(key: str) -> Optional[float]:
    if key in _cache:
        price, ts = _cache[key]
        if time.time() - ts < _CACHE_TTL:
            return price
    return None


def _cache_set(key: str, price: float) -> None:
    _cache[key] = (price, time.time())


# ── Symbol normalisation ──────────────────────────────────────────────────────
_SYMBOL_MAP: dict[str, str] = {
    # Gold
    "GOLD":    "GOLD",
    "XAU/USD": "GOLD",
    "XAUUSD":  "GOLD",
    "XAU_USD": "GOLD",
    # NAS100
    "US100":   "US100",
    "NAS100":  "US100",
    "USTEC":   "US100",
    "NDX":     "US100",
    # Crypto (keep as-is, already normalised below)
}

_YAHOO_TICKERS: dict[str, str] = {
    "GOLD":   "GC%3DF",    # Gold futures
    "US100":  "%5ENDX",    # NASDAQ-100 index
    "BTC":    "BTC-USD",
    "ETH":    "ETH-USD",
    "SOL":    "SOL-USD",
    "DOGE":   "DOGE-USD",
}

# Revolut X symbol pairs (used for live crypto prices)
_REVOLUT_PAIRS: dict[str, str] = {
    "BTC-USD":  "BTC/USD",
    "ETH-USD":  "ETH/USD",
    "SOL-USD":  "SOL/USD",
    "DOGE-USD": "DOGE/USD",
}


def normalise(symbol: str) -> str:
    """Return canonical internal symbol (e.g. 'BTC-USD', 'GOLD', 'US100')."""
    s = symbol.upper().strip()
    return _SYMBOL_MAP.get(s, s)


# ── Price sources ─────────────────────────────────────────────────────────────

async def _yahoo(ticker: str) -> float | None:
    """Fetch from Yahoo Finance (dual-host failover)."""
    for base in ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"):
        try:
            async with httpx.AsyncClient(
                timeout=8,
                headers={"User-Agent": "Mozilla/5.0"},
            ) as c:
                r = await c.get(f"{base}/v8/finance/chart/{ticker}")
                r.raise_for_status()
                return float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
        except Exception as e:
            logger.debug(f"Yahoo {ticker} @ {base}: {e}")
    return None


async def _coinbase(symbol: str) -> float | None:
    """Fetch spot price from Coinbase (crypto only)."""
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"https://api.coinbase.com/v2/prices/{symbol}/spot")
            r.raise_for_status()
            return float(r.json()["data"]["amount"])
    except Exception as e:
        logger.debug(f"Coinbase {symbol}: {e}")
    return None


async def _kraken(base: str) -> float | None:
    """Fetch from Kraken (crypto only)."""
    try:
        pair = "XBTUSD" if base == "BTC" else f"{base}USD"
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"https://api.kraken.com/0/public/Ticker?pair={pair}")
            r.raise_for_status()
            result = r.json().get("result", {})
            ticker = next(iter(result.values()), None)
            if ticker:
                return float(ticker["c"][0])
    except Exception as e:
        logger.debug(f"Kraken {base}: {e}")
    return None


async def _cryptocompare(base: str) -> float | None:
    """CryptoCompare — last-resort crypto fallback."""
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(
                f"https://min-api.cryptocompare.com/data/price?fsym={base}&tsyms=USD"
            )
            r.raise_for_status()
            return float(r.json()["USD"])
    except Exception as e:
        logger.debug(f"CryptoCompare {base}: {e}")
    return None


# ── Main fetch function ───────────────────────────────────────────────────────

async def get_price(symbol: str) -> float:
    """
    Return the current price for any supported symbol.

    Never raises — returns 0.0 if all sources fail (caller should check).
    Uses a 3-second in-process cache to reduce API load.
    """
    sym = normalise(symbol)
    cached = _cache_get(sym)
    if cached is not None:
        return cached

    price: float | None = None

    # ── Commodities / Indices (Yahoo Finance) ─────────────────────────────
    if sym in ("GOLD", "US100"):
        ticker = _YAHOO_TICKERS[sym]
        price = await _yahoo(ticker)

    # ── Crypto ────────────────────────────────────────────────────────────
    else:
        base = sym.split("-")[0]   # "BTC-USD" → "BTC"

        # 1. Coinbase (fast, reliable, no auth needed)
        price = await _coinbase(sym)

        # 2. Kraken fallback
        if price is None:
            price = await _kraken(base)

        # 3. Yahoo Finance fallback (if ticker exists)
        if price is None and base in _YAHOO_TICKERS:
            price = await _yahoo(_YAHOO_TICKERS[base])

        # 4. CryptoCompare last resort
        if price is None:
            price = await _cryptocompare(base)

    if price is not None and price > 0:
        _cache_set(sym, price)
        return price

    # Return stale cache rather than 0.0 if it exists
    if sym in _cache:
        logger.warning(f"price_feed: all sources failed for {sym}, using stale cache")
        return _cache[sym][0]

    logger.error(f"price_feed: all sources failed for {sym}, returning 0.0")
    return 0.0


async def get_prices(symbols: list[str]) -> dict[str, float]:
    """Fetch multiple symbols concurrently."""
    results = await asyncio.gather(*[get_price(s) for s in symbols])
    return {s: p for s, p in zip(symbols, results)}


# ── Cache inspection (for admin/debug) ───────────────────────────────────────

def cache_snapshot() -> dict[str, dict]:
    """Return current cache state for debugging."""
    now = time.time()
    return {
        sym: {"price": price, "age_seconds": round(now - ts, 1)}
        for sym, (price, ts) in _cache.items()
    }
