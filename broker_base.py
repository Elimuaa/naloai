"""
broker_base.py — Asset class detection and per-asset strategy presets.

Imported by bot_engine, routers, and broker clients.
"""


def get_asset_class(symbol: str) -> str:
    """Return 'gold', 'index', or 'crypto' based on symbol."""
    s = symbol.upper()
    if s in ("GOLD", "XAU_USD", "XAUUSD", "XAU/USD") or s.startswith("GC"):
        return "gold"
    if s in ("US100", "NAS100", "NAS100_USD", "USTEC", "NDX", "NQ") or s.startswith("NQ"):
        return "index"
    return "crypto"


ASSET_CLASS_PRESETS = {
    "crypto": {
        # Time of day filter: avoid 4-8 AM UTC (low volume)
        "dead_zone_hours": {4, 5, 6, 7},
        # Correlate with ETH for divergence filter
        "use_eth_correlation": True,
        # Position quantity precision
        "qty_step": 0.0001,
        "qty_precision": 4,
        # Strategy defaults
        "default_entry_z": 1.5,
        "default_lookback": 20,
        "default_stop_loss_pct": 0.025,
        "default_take_profit_pct": 0.05,
        "default_trail_stop_pct": 0.015,
        # Filter defaults
        "default_use_rsi_filter": True,
        "default_use_ema_filter": False,
        "default_use_adx_filter": True,
        "default_use_bbands_filter": True,
        "default_use_macd_filter": False,
    },
    "gold": {
        # Gold trades 24/5 (Sun-Fri), no dead zone
        "dead_zone_hours": set(),
        "use_eth_correlation": False,
        # CFD lots: 0.01 minimum step
        "qty_step": 0.01,
        "qty_precision": 2,
        # Strategy defaults (tighter due to lower volatility)
        "default_entry_z": 1.8,
        "default_lookback": 30,
        "default_stop_loss_pct": 0.008,
        "default_take_profit_pct": 0.016,
        "default_trail_stop_pct": 0.005,
        # Filter defaults
        "default_use_rsi_filter": True,
        "default_use_ema_filter": False,
        "default_use_adx_filter": True,
        "default_use_bbands_filter": True,
        "default_use_macd_filter": False,
    },
    "index": {
        # NAS100 trades NYSE hours 13:30–20:00 UTC only
        # Block: midnight–13:29 UTC and 20:00–23:59 UTC
        "dead_zone_hours": set(range(0, 14)) | {20, 21, 22, 23},
        "use_eth_correlation": False,
        # Whole units only (1 contract per order)
        "qty_step": 1,
        "qty_precision": 0,
        # Strategy defaults (tight due to high notional value)
        "default_entry_z": 1.6,
        "default_lookback": 25,
        "default_stop_loss_pct": 0.005,
        "default_take_profit_pct": 0.012,
        "default_trail_stop_pct": 0.004,
        # Filter defaults
        "default_use_rsi_filter": True,
        "default_use_ema_filter": False,
        "default_use_adx_filter": True,
        "default_use_bbands_filter": True,
        "default_use_macd_filter": False,
    },
}
