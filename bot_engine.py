import asyncio
import logging
import json
import os
import random
import time
from datetime import datetime, timezone
from typing import Optional
import numpy as np
from database import AsyncSessionLocal, User, Trade
from ws_manager import ws_manager
from post_trade_ai_learner import analyze_trade
from ai_calibrator import calibrate_after_trade
from ai_screener import screen_trade, classify_regime, record_pattern, get_pattern_insights
from indicators import (
    rsi, ema, adx, bollinger_bands, macd, atr_from_prices, compute_all_indicators
)
from risk_manager import RiskManager
import notifications
from sqlalchemy import select, update

logger = logging.getLogger(__name__)

def _get_client(user: User, force_demo: bool = False):
    """Return broker client based on user's broker_type setting.
    Routes to Capital.com, Tradovate, or Robinhood. Falls back to matching mock client.
    Caches the client to preserve internal state (e.g. demo balance)."""
    broker = (getattr(user, 'broker_type', 'robinhood') or 'robinhood').lower().strip()
    mode_key = 'demo' if force_demo else 'live'
    cache_key = f"{user.id}:{broker}:{mode_key}"

    if cache_key in _client_cache:
        return _client_cache[cache_key]

    if not force_demo:
        # ── Capital.com live client ──
        if broker == 'capital':
            if user.capital_api_key and user.capital_identifier:
                try:
                    from capital_client import CapitalComClient
                    client = CapitalComClient(
                        api_key=user.capital_api_key,
                        identifier=user.capital_identifier,
                        password=user.capital_password or "",
                        demo=False,
                    )
                    logger.info(f"Using LIVE Capital.com client for user {user.id}")
                    _client_cache[cache_key] = client
                    return client
                except Exception as e:
                    logger.error(f"Capital.com client creation failed for {user.id}: {e}")
            else:
                logger.info(f"No Capital.com keys for {user.id}, using mock")

        # ── Tradovate live client ──
        elif broker == 'tradovate':
            if user.tradovate_username and user.tradovate_password:
                try:
                    from tradovate_client import TradovateClient
                    client = TradovateClient(
                        username=user.tradovate_username,
                        password=user.tradovate_password,
                        account_id=user.tradovate_account_id or 0,
                        demo=False,
                    )
                    logger.info(f"Using LIVE Tradovate client for user {user.id}")
                    _client_cache[cache_key] = client
                    return client
                except Exception as e:
                    logger.error(f"Tradovate client creation failed for {user.id}: {e}")
            else:
                logger.info(f"No Tradovate credentials for {user.id}, using mock")

        # ── Robinhood live client ──
        else:
            private_key = user.ed25519_private_key or user.rh_private_key
            if user.rh_api_key and private_key:
                from robinhood import create_client
                client = create_client(user.rh_api_key, private_key)
                if client:
                    logger.info(f"Using LIVE Robinhood client for user {user.id}")
                    _client_cache[cache_key] = client
                    return client
                logger.error(f"Failed to create Robinhood client for {user.id}, falling back to mock")
            else:
                logger.info(f"No Robinhood keys for {user.id}, using mock")
    else:
        logger.info(f"force_demo=True for user {user.id} (broker={broker}), using mock")

    # ── Demo/mock fallback — pick matching mock client ──
    if broker == 'capital':
        from mock_capital_client import MockCapitalClient
        client = MockCapitalClient(symbol=user.trading_symbol, balance=user.demo_balance or 10000.0)
    elif broker == 'tradovate':
        from mock_tradovate_client import MockTradovateClient
        client = MockTradovateClient(symbol=user.trading_symbol, balance=user.demo_balance or 10000.0)
    else:
        from mock_robinhood import MockRobinhoodClient
        client = MockRobinhoodClient(symbol=user.trading_symbol, balance=user.demo_balance or 10000.0)

    _client_cache[cache_key] = client
    return client


class BotState:
    def __init__(self, force_demo: bool = False):
        self.price_history: list[float] = []
        self.eth_price_history: list[float] = []  # ETH correlation filter
        self.bullish_levels: list[float] = []
        self.bearish_levels: list[float] = []
        self.in_trade: bool = False
        self.entry_price: Optional[float] = None
        self.trade_side: Optional[str] = None
        self.entry_z_score: Optional[float] = None
        self.current_trade_id: Optional[str] = None
        self.trail_stop_price: Optional[float] = None
        self.last_signal: Optional[str] = None
        self.last_update: Optional[str] = None
        self.error_count: int = 0
        self.demo_mode: bool = True
        self.force_demo: bool = force_demo
        self.key_invalid: bool = False
        self.indicators: dict = {}
        self.current_quantity: float = 0.0001
        self.last_optimize_tick: int = 0
        self.last_calibration_tick: int = 0
        # Multi-timeframe
        self.regime: str = "ranging"  # trending_up, trending_down, ranging, volatile
        self.slow_z_score: Optional[float] = None
        # Cooldown
        self.last_stop_loss_time: Optional[float] = None  # time.time() of last stop loss
        self.consecutive_losses: int = 0
        # AI screening
        self.last_ai_screen: Optional[dict] = None
        self.trades_since_optimize: int = 0
        # Strategy memory — last bucket lookup result, surfaced on dashboard
        self.last_setup_score: Optional[dict] = None
        # Signal strength at the moment the trade was opened (0.0–1.0).
        # Captured here because by the time the trade closes the live `signal_strength`
        # has long since changed. Required for correct strategy_memory bucketing.
        self.entry_signal_strength: Optional[float] = None
        # ── Profitability enhancements ──
        self.breakeven_moved: bool = False          # True once SL moved to entry
        self.trade_open_time: Optional[float] = None  # time.time() when trade opened
        self.consecutive_wins: int = 0             # reset on loss, increment on win
        self.bb_width_history: list[float] = []    # rolling BB-width for squeeze filter
        # Partial profit taking
        self.partial_exit_done: bool = False        # True once 50% has been closed at 1R
        self.initial_quantity: float = 0.0          # full size at entry; remaining = current_quantity
        self.partial_pnl_booked: float = 0.0        # P&L locked during the trade (added to total at close)
        # Adaptive R/R per trade
        self.adaptive_tp_pct: Optional[float] = None  # locked at entry based on regime
        # ── Option A: second concurrent position (when signal_strength ≥ 0.70) ──
        # Stores a lightweight dict for a second open trade on the same symbol.
        # Keys: trade_id, entry_price, side, entry_z, quantity, trail_stop_price,
        #       trade_open_time, adaptive_tp_pct, breakeven_moved
        self.second_slot: Optional[dict] = None


# Auto-optimization interval (run quantum optimizer every N ticks)
AUTO_OPTIMIZE_INTERVAL = 200

# Dead zone hours (UTC) — data-driven from 14-month BTC audit (RC Quantum Signal Engine)
# CONSERVATIVE blacklist: only the WORST 4 hours where edge is statistically negative.
# Was {1,6,9,11,13,14,17,18} (8h) — too restrictive, was cutting volume in half vs RC Quantum.
# Now {1,11,13,18} — kept the 4 hours with strongest negative edge; opens up 4 more trading hours/day.
DEAD_ZONE_HOURS = {1, 11, 13, 18}

# Minimum cooldown after stop loss (seconds)
MIN_COOLDOWN_SECONDS = 600  # 10 minutes — re-enter faster after a loss (was 15)

# Symbols where new entries are blocked but existing open positions still get managed.
# Audit (298 closed trades, May 2026): ETH-USD posted 0/28 z-revert exits — the core
# mean-reversion premise (z>=1.1 → return to z=0) never fired once. Net result was
# −$18.37 of pure time-decay losses. Re-add once we have momentum-mode parameters.
NO_NEW_ENTRY_SYMBOLS: set[str] = {"ETH-USD"}

bot_states: dict[str, BotState] = {}
# Background task registry — prevents asyncio.create_task() results from being GC'd
# before they complete. Tasks are removed on completion via the done-callback.
_background_tasks: set = set()
_bot_tasks: dict[str, asyncio.Task] = {}
_client_cache: dict[str, object] = {}
_risk_managers: dict[str, RiskManager] = {}
_risk_state_loaded: set[str] = set()  # user_ids whose risk state has been hydrated from DB


async def _load_risk_state(user_id: str, rm: RiskManager) -> None:
    """One-shot hydrate of RiskManager from the persisted snapshot.

    Called once per process per user. Without this, `daily_pnl`, the Kelly
    rolling window, and the stop-loss cooldown all reset on every redeploy —
    which can re-trigger full-size mode after the day's target was already hit
    or bypass cooldown right after a stop. Both are profitability leaks.
    """
    if user_id in _risk_state_loaded:
        return
    _risk_state_loaded.add(user_id)
    try:
        from database import RiskState
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(RiskState).where(RiskState.user_id == user_id)
            )).scalar_one_or_none()
        if row is None:
            return
        import json as _json
        snap = {
            "daily_pnl": row.daily_pnl,
            "daily_starting_balance": row.daily_starting_balance,
            "daily_reset_date": row.daily_reset_date,
            "is_paused": row.is_paused,
            "pause_reason": row.pause_reason,
            "cooldown_remaining": row.cooldown_remaining,
            "stop_loss_times": _json.loads(row.stop_loss_times_json) if row.stop_loss_times_json else [],
            "recent_trades": _json.loads(row.recent_trades_json) if row.recent_trades_json else [],
        }
        rm.restore_from_dict(snap)
    except Exception as e:
        logger.error(f"Risk state load failed for {user_id}: {e}", exc_info=True)


async def _persist_risk_state(user_id: str, rm: RiskManager) -> None:
    """Write the RiskManager snapshot to DB. Called after every trade close."""
    try:
        from database import RiskState
        import json as _json
        snap = rm.to_persisted_dict()
        async with AsyncSessionLocal() as db:
            existing = (await db.execute(
                select(RiskState).where(RiskState.user_id == user_id)
            )).scalar_one_or_none()
            now = datetime.now(timezone.utc)
            if existing is None:
                db.add(RiskState(
                    user_id=user_id,
                    daily_pnl=snap["daily_pnl"],
                    daily_starting_balance=snap["daily_starting_balance"],
                    daily_reset_date=snap["daily_reset_date"],
                    is_paused=snap["is_paused"],
                    pause_reason=snap["pause_reason"],
                    cooldown_remaining=snap["cooldown_remaining"],
                    stop_loss_times_json=_json.dumps(snap["stop_loss_times"]),
                    recent_trades_json=_json.dumps(snap["recent_trades"]),
                    updated_at=now,
                ))
            else:
                existing.daily_pnl = snap["daily_pnl"]
                existing.daily_starting_balance = snap["daily_starting_balance"]
                existing.daily_reset_date = snap["daily_reset_date"]
                existing.is_paused = snap["is_paused"]
                existing.pause_reason = snap["pause_reason"]
                existing.cooldown_remaining = snap["cooldown_remaining"]
                existing.stop_loss_times_json = _json.dumps(snap["stop_loss_times"])
                existing.recent_trades_json = _json.dumps(snap["recent_trades"])
                existing.updated_at = now
            await db.commit()
    except Exception as e:
        logger.error(f"Risk state persist failed for {user_id}: {e}", exc_info=True)


def _get_risk_manager(user: User) -> RiskManager:
    """Get or create a risk manager for a user. Always syncs live settings from DB."""
    rm = _risk_managers.get(user.id)
    if rm is None:
        rm = RiskManager(
            max_drawdown_pct=getattr(user, 'max_drawdown_pct', 8.0) or 8.0,
            max_stops_before_pause=getattr(user, 'max_stops_before_pause', 3) or 3,
            cooldown_ticks=getattr(user, 'cooldown_ticks', 5) or 5,
            max_exposure_pct=getattr(user, 'max_exposure_pct', 40.0) or 40.0,
            risk_per_trade_pct=getattr(user, 'risk_per_trade_pct', 2.0) or 2.0,
        )
        _risk_managers[user.id] = rm
    else:
        # Sync settings every tick so admin changes take effect immediately
        rm.max_drawdown_pct = getattr(user, 'max_drawdown_pct', 8.0) or 8.0
        rm.max_stops_before_pause = getattr(user, 'max_stops_before_pause', 3) or 3
        rm.cooldown_ticks = getattr(user, 'cooldown_ticks', 5) or 5
        rm.max_exposure_pct = getattr(user, 'max_exposure_pct', 40.0) or 40.0
        rm.risk_per_trade_pct = getattr(user, 'risk_per_trade_pct', 2.0) or 2.0
    return rm


async def _recover_state_for_symbol(user_id: str, symbol: str, state: 'BotState'):
    """Restore open-trade state from DB for a given symbol on restart.
    Supports up to 2 open trades (primary + second_slot). Does NOT force-close extras;
    leaves them in DB as-is so they can be managed by the running loop.
    """
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Trade).where(
                    Trade.user_id == user_id,
                    Trade.symbol == symbol,
                    Trade.state == "open",
                ).order_by(Trade.opened_at.asc())
            )
            open_trades = result.scalars().all()

            user_conf = await db.execute(select(User).where(User.id == user_id))
            u = user_conf.scalar_one_or_none()
            trail_pct = u.trail_stop_pct if u else 0.020

            if open_trades:
                # Restore primary slot (oldest open trade)
                t = open_trades[0]
                if t.entry_price:
                    state.in_trade = True
                    state.entry_price = float(t.entry_price)
                    state.trade_side = t.side
                    state.current_trade_id = t.id
                    state.current_quantity = t.quantity_value or float(t.quantity)
                    # Trail starts None — arms at +0.5R same as fresh entry (consistent with 07d339f)
                    state.trail_stop_price = None
                    # Restore trade_open_time from DB so the 5h time-cap still fires after restarts.
                    # Without this, trade_open_time stays None → time-cap never fires → trades
                    # can stay open indefinitely if price drifts between SL and TP.
                    if t.opened_at:
                        _oa = t.opened_at
                        if _oa.tzinfo is None:
                            _oa = _oa.replace(tzinfo=timezone.utc)
                        state.trade_open_time = _oa.timestamp()
                    else:
                        state.trade_open_time = time.time() - 3600  # assume 1h old if no timestamp
                    logger.info(f"Restored primary trade {t.id[:8]} for {user_id}/{symbol}: {t.side} @ {t.entry_price}")

            if len(open_trades) >= 2:
                # Restore second slot
                t2 = open_trades[1]
                if t2.entry_price:
                    ep2 = float(t2.entry_price)
                    _oa2 = t2.opened_at
                    if _oa2 and _oa2.tzinfo is None:
                        _oa2 = _oa2.replace(tzinfo=timezone.utc)
                    state.second_slot = {
                        "trade_id": t2.id,
                        "entry_price": ep2,
                        "side": t2.side,
                        "entry_z": 0.0,
                        "entry_signal_strength": None,  # unknown after restart — recorder uses fallback
                        "quantity": t2.quantity_value or float(t2.quantity),
                        "trail_stop_price": None,  # arms at +0.5R, consistent with 07d339f
                        "trade_open_time": _oa2.timestamp() if _oa2 else time.time() - 3600,
                        "adaptive_tp_pct": None,
                        "breakeven_moved": False,
                    }
                    logger.info(f"Restored second-slot trade {t2.id[:8]} for {user_id}/{symbol}")

    except Exception as e:
        logger.error(f"Failed to restore open trades for {user_id}/{symbol}: {e}")


async def start_bot(user_id: str, force_demo: bool = False):
    """Launch bot for a user. Starts 4 parallel loops for crypto users:
    BTC-USD, ETH-USD, SOL-USD, DOGE-USD — giving ~12-16 trades/day per user.
    Each loop has its own BotState so they manage positions independently.
    For Gold/NAS100 brokers (Capital.com / Tradovate), only the primary symbol runs.
    """
    # Check if any loop is already running for this user
    running_keys = [k for k, t in _bot_tasks.items() if k.startswith(f"{user_id}:") and not t.done()]
    if running_keys:
        return {"status": "already_running"}

    # Fetch user config to determine broker / asset class
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()

    primary_symbol = (user.trading_symbol if user else "BTC-USD") or "BTC-USD"

    from broker_base import get_asset_class
    _broker_cls = (getattr(user, 'broker_type', 'robinhood') or 'robinhood').lower().strip()
    _asset_cls = get_asset_class(primary_symbol)

    if _asset_cls == "crypto":
        # All 4 loops still spawn so existing open positions on any symbol are managed
        # to close. New entries on ETH-USD are blocked at the entry gate (see
        # NO_NEW_ENTRY_SYMBOLS below) — 298-trade audit showed 0/28 z_reverts on ETH,
        # i.e. mean-reversion premise never fired. Once existing ETH positions close
        # the loop idles cheaply.
        symbols_to_run = ["BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD"]
    else:
        # Gold / NAS100 futures — single asset, no cross-symbol diversification
        symbols_to_run = [primary_symbol]

    # Hydrate the RiskManager from the persisted snapshot ONCE before any loop fires.
    # This protects daily_pnl, Kelly history, and stop-loss cooldowns across restarts.
    if user is not None:
        _rm_for_load = _get_risk_manager(user)
        await _load_risk_state(user_id, _rm_for_load)

    for sym in symbols_to_run:
        _state_key = f"{user_id}:{sym}"
        state = BotState(force_demo=force_demo)
        await _recover_state_for_symbol(user_id, sym, state)
        bot_states[_state_key] = state

        # Rebuild mock client holdings from restored open trades so that sell orders
        # on exit correctly credit the balance (mock client._holdings is in-memory only
        # and is wiped on every process restart — without this, exit sells execute for
        # qty=0 and the position value silently disappears from the demo balance).
        if state.in_trade and state.current_quantity > 0:
            client_now = _get_client(user, force_demo=force_demo)
            if hasattr(client_now, '_holdings'):
                current_held = client_now._holdings.get(sym, 0)
                client_now._holdings[sym] = current_held + state.current_quantity
                logger.info(
                    f"Restored holdings for {user_id}/{sym}: +{state.current_quantity} "
                    f"(total held: {client_now._holdings[sym]:.6f})"
                )
        if state.second_slot and state.second_slot.get("quantity", 0) > 0:
            client_now = _get_client(user, force_demo=force_demo)
            if hasattr(client_now, '_holdings'):
                s2_qty = state.second_slot["quantity"]
                client_now._holdings[sym] = client_now._holdings.get(sym, 0) + s2_qty
                logger.info(f"Restored second-slot holdings for {user_id}/{sym}: +{s2_qty:.6f}")

        task = asyncio.create_task(_bot_loop(user_id, sym), name=f"bot-{user_id}-{sym}")
        _bot_tasks[_state_key] = task
        logger.info(f"Launched bot loop for {user_id} symbol={sym}")

    return {"status": "started", "symbols": symbols_to_run}


async def stop_bot(user_id: str):
    # Persist demo balance before clearing client cache
    for k in list(_client_cache.keys()):
        if k.startswith(user_id):
            client = _client_cache[k]
            if hasattr(client, 'balance'):
                try:
                    async with AsyncSessionLocal() as db2:
                        await db2.execute(
                            update(User).where(User.id == user_id).values(demo_balance=round(client.balance, 2))
                        )
                        await db2.commit()
                except Exception as _e:
                    logger.warning(f"stop_bot: failed to persist demo_balance for {user_id}: {_e}")

    # Cancel ALL symbol-keyed tasks for this user
    task_keys = [k for k in list(_bot_tasks.keys()) if k.startswith(f"{user_id}:")]
    for tk in task_keys:
        task = _bot_tasks[tk]
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        del _bot_tasks[tk]

    # Clear state and client cache
    state_keys = [k for k in list(bot_states.keys()) if k.startswith(f"{user_id}:")]
    for sk in state_keys:
        del bot_states[sk]

    for k in list(_client_cache.keys()):
        if k.startswith(user_id):
            del _client_cache[k]

    async with AsyncSessionLocal() as db:
        await db.execute(update(User).where(User.id == user_id).values(bot_active=False))
        await db.commit()
    await notifications.notify_bot_stopped()
    return {"status": "stopped"}


def get_bot_status(user_id: str) -> dict:
    # Aggregate running status across all symbol loops for this user
    running_tasks = {k: t for k, t in _bot_tasks.items()
                     if k.startswith(f"{user_id}:") and not t.done()}
    running = len(running_tasks) > 0

    # Primary state: find most active (in-trade, or most recently updated)
    user_states = {k: v for k, v in bot_states.items() if k.startswith(f"{user_id}:")}
    # Prefer the state that is currently in trade
    primary_state = None
    for st in user_states.values():
        if st.in_trade:
            primary_state = st
            break
    if primary_state is None:
        # Fall back to any state (last alphabetically for determinism)
        if user_states:
            primary_state = list(user_states.values())[-1]
        else:
            primary_state = BotState()

    risk_mgr = _risk_managers.get(user_id)

    # Count total open positions across all symbol loops
    open_position_count = sum(
        (1 if st.in_trade else 0) + (1 if st.second_slot is not None else 0)
        for st in user_states.values()
    )

    return {
        "running": running,
        "in_trade": primary_state.in_trade,
        "entry_price": primary_state.entry_price,
        "trade_side": primary_state.trade_side,
        "trail_stop": primary_state.trail_stop_price,
        "last_signal": primary_state.last_signal,
        "last_update": primary_state.last_update,
        "error_count": primary_state.error_count,
        "demo_mode": primary_state.demo_mode,
        "key_invalid": primary_state.key_invalid,
        "position_size": primary_state.current_quantity,
        "risk": risk_mgr.get_status() if risk_mgr else None,
        "open_position_count": open_position_count,
        "active_symbols": list(running_tasks.keys()),
        "last_setup_score": primary_state.last_setup_score,
    }


def _adaptive_rr(base_sl_pct: float, base_tp_pct: float, regime: str, side: str) -> tuple[float, float]:
    """Adjust SL/TP based on current market regime.

    Ranging markets: widen TP to let mean-reversion run (2.5× SL).
    Trending with us: let it ride (3.0× SL).
    Trending against us: tight exit (1.5× SL) — don't fight the trend.
    Volatile: standard (2.0× SL).

    Returns (sl_pct, tp_pct).
    """
    if regime == "ranging":
        # Mean-reversion sweet spot — widen TP
        return base_sl_pct, base_sl_pct * 2.5
    if regime == "trending_up":
        if side == "buy":
            return base_sl_pct, base_sl_pct * 3.0   # ride the trend
        return base_sl_pct, base_sl_pct * 1.5       # against trend — exit fast
    if regime == "trending_down":
        if side == "sell":
            return base_sl_pct, base_sl_pct * 3.0
        return base_sl_pct, base_sl_pct * 1.5
    # volatile / unknown — use user's setting
    return base_sl_pct, base_tp_pct


def _calculate_zscore(prices: list[float], lookback: int) -> Optional[float]:
    if len(prices) < lookback:
        return None
    window = prices[-lookback:]
    mean = np.mean(window)
    std = np.std(window)
    if std == 0:
        return 0.0
    return float((prices[-1] - mean) / std)


def _calculate_signal_strength(
    z_score: float, slow_z: Optional[float], regime: str,
    indicators: dict, side: str
) -> float:
    """Calculate signal strength from 0.0 to 1.0 based on multiple confirmations."""
    score = 0.0
    checks = 0

    # Z-score strength (stronger deviation = stronger signal)
    abs_z = abs(z_score)
    if abs_z >= 2.5:
        score += 1.0
    elif abs_z >= 2.0:
        score += 0.7
    elif abs_z >= 1.5:
        score += 0.4
    checks += 1

    # Multi-timeframe alignment
    if slow_z is not None:
        if side == "buy" and slow_z < 0:
            score += 1.0  # Slow also says buy
        elif side == "sell" and slow_z > 0:
            score += 1.0
        elif side == "buy" and slow_z > 0:
            score += 0.2  # Conflicting
        elif side == "sell" and slow_z < 0:
            score += 0.2
        checks += 1

    # Regime alignment
    if regime == "ranging":
        score += 0.8  # Mean reversion loves ranges
    elif regime in ("trending_up", "trending_down"):
        score += 0.2  # Risky for mean reversion
    checks += 1

    # RSI confirmation
    rsi_val = indicators.get("rsi")
    if rsi_val is not None:
        if side == "buy" and rsi_val < 40:
            score += 0.8
        elif side == "sell" and rsi_val > 60:
            score += 0.8
        elif side == "buy" and rsi_val > 60:
            score += 0.1
        elif side == "sell" and rsi_val < 40:
            score += 0.1
        else:
            score += 0.5
        checks += 1

    # Bollinger band position
    bb = indicators.get("bb_pct_b")
    if bb is not None:
        if side == "buy" and bb < 0.2:
            score += 1.0
        elif side == "sell" and bb > 0.8:
            score += 1.0
        else:
            score += 0.3
        checks += 1

    return score / checks if checks > 0 else 0.5


def _check_signal_filters(
    prices: list[float], side: str, user: User,
    state: 'BotState', z_score: float, symbol: str = None
) -> tuple[bool, list[str]]:
    """Apply indicator filters + new advanced filters. Returns (passed, reasons_rejected)."""
    reasons = []
    current_price = prices[-1]
    _effective_symbol = symbol or getattr(user, 'trading_symbol', 'BTC-USD')

    # ── TIME-OF-DAY FILTER (asset-class aware) ──
    from broker_base import get_asset_class, ASSET_CLASS_PRESETS
    _asset_preset = ASSET_CLASS_PRESETS[get_asset_class(_effective_symbol)]
    _dead_zone = _asset_preset["dead_zone_hours"]
    _use_eth_corr = _asset_preset["use_eth_correlation"] and _effective_symbol.upper() != "ETH-USD"
    current_hour = datetime.now(timezone.utc).hour
    if current_hour in _dead_zone:
        reasons.append(f"Time filter: {current_hour}:00 UTC is outside trading hours for {_effective_symbol}")

    # ── CONSECUTIVE LOSS COOLDOWN (time-based) ──
    if state.last_stop_loss_time is not None:
        elapsed = time.time() - state.last_stop_loss_time
        if elapsed < MIN_COOLDOWN_SECONDS and state.consecutive_losses >= 3:
            remaining = int((MIN_COOLDOWN_SECONDS - elapsed) / 60)
            reasons.append(f"Cooldown: {remaining}m remaining after {state.consecutive_losses} consecutive losses")

    # ── MULTI-TIMEFRAME CONFIRMATION ──
    slow_lookback = min(60, len(prices))
    if slow_lookback >= 30:
        slow_z = _calculate_zscore(prices, slow_lookback)
        state.slow_z_score = slow_z
        if slow_z is not None:
            # Buy signal but slow timeframe says sell (or vice versa)
            if side == "buy" and slow_z > 2.0:
                reasons.append(f"Multi-TF filter: slow Z={slow_z:.2f} says overbought (conflicting)")
            elif side == "sell" and slow_z < -2.0:
                reasons.append(f"Multi-TF filter: slow Z={slow_z:.2f} says oversold (conflicting)")

    # ── MARKET REGIME FILTER ──
    if state.regime in ("trending_up", "trending_down"):
        adx_val = adx(prices, 14)
        if adx_val and adx_val > 35:
            # Strong trend — block mean reversion unless very strong signal
            signal_strength = _calculate_signal_strength(
                z_score, state.slow_z_score, state.regime, state.indicators, side
            )
            if signal_strength < 0.7:
                reasons.append(f"Regime filter: strong {state.regime} (ADX={adx_val:.0f}), signal only {signal_strength:.0%}")

    # ── ETH CORRELATION FILTER (crypto only) ──
    if _use_eth_corr and len(state.eth_price_history) >= 10 and len(prices) >= 10:
        btc_change = (prices[-1] - prices[-10]) / prices[-10]
        eth_change = (state.eth_price_history[-1] - state.eth_price_history[-10]) / state.eth_price_history[-10]
        # If BTC and ETH are diverging significantly, market is uncertain
        if abs(btc_change - eth_change) > 0.03:  # >3% divergence
            reasons.append(f"Correlation filter: BTC/ETH diverging (BTC {btc_change:+.2%}, ETH {eth_change:+.2%})")

    # ── ORIGINAL FILTERS (EMA, RSI, ADX, BB, MACD) ──
    if getattr(user, 'use_ema_filter', False):
        ema_50 = ema(prices, 50)
        if ema_50 is not None:
            if side == "buy" and current_price < ema_50:
                reasons.append(f"EMA-50 filter: price ${current_price:,.0f} < EMA ${ema_50:,.0f} (downtrend)")
            elif side == "sell" and current_price > ema_50:
                reasons.append(f"EMA-50 filter: price ${current_price:,.0f} > EMA ${ema_50:,.0f} (uptrend)")

    if getattr(user, 'use_rsi_filter', True):
        rsi_val = rsi(prices, 14)
        if rsi_val is not None:
            if side == "buy" and rsi_val > 70:
                reasons.append(f"RSI filter: RSI {rsi_val:.1f} > 70 (overbought)")
            elif side == "sell" and rsi_val < 30:
                reasons.append(f"RSI filter: RSI {rsi_val:.1f} < 30 (oversold)")

    if getattr(user, 'use_adx_filter', True):
        adx_val = adx(prices, 14)
        if adx_val is not None and adx_val > 25 and state.regime == "ranging":
            reasons.append(f"ADX filter: ADX {adx_val:.1f} > 25 (strong trend, skip mean-reversion)")

    if getattr(user, 'use_bbands_filter', True):
        bb = bollinger_bands(prices, int(getattr(user, 'lookback', 20)))
        if bb is not None:
            if side == "buy" and bb["pct_b"] > 0.8:
                reasons.append(f"BB filter: %B {bb['pct_b']:.2f} > 0.8 (not near lower band)")
            elif side == "sell" and bb["pct_b"] < 0.2:
                reasons.append(f"BB filter: %B {bb['pct_b']:.2f} < 0.2 (not near upper band)")

    if getattr(user, 'use_macd_filter', False):
        macd_data = macd(prices)
        if macd_data is not None:
            if side == "buy" and macd_data["histogram"] < 0 and macd_data["macd"] < macd_data["signal"]:
                reasons.append(f"MACD filter: bearish (histogram {macd_data['histogram']:.4f})")
            elif side == "sell" and macd_data["histogram"] > 0 and macd_data["macd"] > macd_data["signal"]:
                reasons.append(f"MACD filter: bullish (histogram {macd_data['histogram']:.4f})")

    return (len(reasons) == 0, reasons)


async def _get_user_config(user_id: str) -> Optional[User]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()


async def _save_trade(user_id: str, trade_data: dict) -> str:
    async with AsyncSessionLocal() as db:
        trade = Trade(user_id=user_id, **trade_data)
        db.add(trade)
        await db.commit()
        await db.refresh(trade)
        return trade.id


async def _close_trade(
    user_id: str, trade_id: str, exit_price: float, exit_reason: str,
    entry_price: float, side: str, entry_z: float, current_z: float,
    symbol: str = "BTC-USD", quantity: float = 1.0,
    partial_pnl: float = 0.0,
):
    """Close a trade in DB. `quantity` is the REMAINING qty after any partial.
    `partial_pnl` is profit already booked during the trade (50% close at 1R).
    Stored Trade.pnl is the TOTAL = close-leg pnl + partial_pnl.
    """
    if not entry_price or entry_price <= 0:
        entry_price = exit_price  # Safeguard: avoids division by zero
    price_diff = (exit_price - entry_price) if side == "buy" else (entry_price - exit_price)
    close_leg_pnl = price_diff * quantity
    pnl = close_leg_pnl + partial_pnl                      # ← TRUE total profit
    pnl_pct = (price_diff / entry_price) * 100 if entry_price > 0 else 0.0
    # Retry once on transient DB errors — a failed close leaves an orphan open trade
    for _attempt in range(2):
        try:
            async with AsyncSessionLocal() as db:
                await db.execute(
                    update(Trade).where(Trade.id == trade_id).values(
                        exit_price=str(exit_price),
                        pnl=pnl,
                        pnl_pct=pnl_pct,
                        partial_pnl=partial_pnl,                   # ← persist for analytics
                        state="closed",
                        exit_reason=exit_reason,
                        closed_at=datetime.now(timezone.utc)
                    )
                )
                await db.commit()
            break  # success
        except Exception as _dbe:
            logger.error(f"_close_trade DB commit failed (attempt {_attempt+1}) for {trade_id}: {_dbe}")
            if _attempt == 1:
                logger.critical(
                    f"TRADE CLOSE LOST for user {user_id} trade {trade_id}: "
                    f"{side} {symbol} exit={exit_price} pnl={pnl:.2f} reason={exit_reason}. "
                    f"Manually set state=closed in DB to fix."
                )
    _t = asyncio.create_task(_run_ai_analysis(trade_id, {
        "symbol": symbol,
        "side": side,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "exit_reason": exit_reason,
        "entry_z_score": entry_z,
        "exit_z_score": current_z,
        "duration_minutes": 0,
    }, user_id, symbol=symbol))
    _background_tasks.add(_t)
    _t.add_done_callback(_background_tasks.discard)


async def _run_ai_analysis(trade_id: str, trade_data: dict, user_id: str, symbol: str = "BTC-USD"):
    try:
        analysis = await analyze_trade(trade_data)
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(Trade).where(Trade.id == trade_id).values(
                    ai_grade=analysis.get("grade"),
                    ai_entry_quality=analysis.get("entry_quality"),
                    ai_exit_quality=analysis.get("exit_quality"),
                    ai_what_went_well=json.dumps(analysis.get("what_went_well", [])),
                    ai_what_went_wrong=json.dumps(analysis.get("what_went_wrong", [])),
                    ai_improvements=json.dumps(analysis.get("improvements", [])),
                    ai_confidence=analysis.get("confidence", 0.0),
                    ai_analyzed=True,
                )
            )
            await db.commit()
        await ws_manager.send_to_user(user_id, {
            "type": "ai_analysis_ready",
            "trade_id": trade_id,
            "analysis": analysis,
        })
    except Exception as e:
        logger.error(f"AI analysis failed for trade {trade_id}: {e}")

    # Premium auto-calibration — runs after AI analysis
    try:
        calibration = await calibrate_after_trade(user_id)
        # Mark calibration tick to prevent optimizer from overwriting immediately
        _state_key = f"{user_id}:{symbol}"
        state = bot_states.get(_state_key)
        if state:
            state.last_calibration_tick = len(state.price_history)
        if calibration and calibration.get("applied_changes"):
            changes = calibration["applied_changes"]
            param_summary = ", ".join(
                f"{k}: {v['old']}→{v['new']}" for k, v in changes.items()
            )
            await ws_manager.send_to_user(user_id, {
                "type": "calibration_applied",
                "changes": changes,
                "reasoning": calibration.get("reasoning", ""),
                "projected_impact": calibration.get("projected_impact", ""),
                "summary": param_summary,
            })
            logger.info(f"Auto-calibration applied for {user_id}: {param_summary}")
    except Exception as e:
        logger.error(f"Auto-calibration failed for {user_id}: {e}")


async def _bot_loop(user_id: str, symbol: str):
    _state_key = f"{user_id}:{symbol}"
    logger.info(f"Bot started for user {user_id} symbol={symbol}")
    state = bot_states.get(_state_key, BotState())

    while True:
        try:
            user = await _get_user_config(user_id)
            if not user or not user.bot_active:
                logger.info(f"Bot disabled for user {user_id} ({symbol}), stopping")
                break

            broker = (getattr(user, 'broker_type', 'robinhood') or 'robinhood').lower().strip()
            if broker == 'capital':
                has_live_creds = bool(user.capital_api_key and user.capital_identifier)
            elif broker == 'tradovate':
                has_live_creds = bool(user.tradovate_username and user.tradovate_password)
            else:
                has_live_creds = bool(user.rh_api_key and user.ed25519_private_key)
            is_demo = state.force_demo or not has_live_creds
            state.demo_mode = is_demo
            POLL_INTERVAL = 6 if is_demo else 15  # 15s live for faster reaction

            client = _get_client(user, force_demo=state.force_demo)
            if not client:
                await asyncio.sleep(30)
                continue

            # Initialize risk manager with user settings
            risk_mgr = _get_risk_manager(user)
            balance = client.balance if hasattr(client, 'balance') else (user.demo_balance or 10000.0)
            _prev_reset_date = risk_mgr.daily_reset_date
            risk_mgr.reset_daily(balance)
            # If a UTC-day rollover just happened, flush the fresh snapshot to DB so
            # a restart at 00:01 doesn't show stale yesterday counters in the UI.
            if risk_mgr.daily_reset_date != _prev_reset_date:
                _rpr = asyncio.create_task(_persist_risk_state(user_id, risk_mgr))
                _background_tasks.add(_rpr); _rpr.add_done_callback(_background_tasks.discard)

            # symbol is passed as parameter — do not override with user.trading_symbol
            # (allows ETH-USD loop to run alongside BTC-USD loop independently)
            lookback = int(user.lookback)
            stop_loss_pct = user.stop_loss_pct
            take_profit_pct = user.take_profit_pct
            trail_stop_pct = user.trail_stop_pct
            tolerance_pct = 0.005 if is_demo else 0.01

            # ── Golden hour scaling (data-driven from 14-month BTC audit) ──
            # Best UTC hours by avg P/L: 8($80), 20($76), 15($55), 19($55), 7($36)
            # During these hours: lower entry threshold + allow up to 25% larger position
            _now_hour = datetime.now(timezone.utc).hour
            GOLDEN_HOURS = {7, 8, 15, 19, 20}
            if _now_hour in GOLDEN_HOURS:
                entry_z_thresh = max(0.9, user.entry_z * 0.80)  # 20% lower threshold
                _golden_boost = 1.25   # 25% larger position
            else:
                entry_z_thresh = user.entry_z
                _golden_boost = 1.0

            current_price = await client.get_current_price(symbol)
            if not is_demo and state.key_invalid:
                state.key_invalid = False
                await ws_manager.send_to_user(user_id, {
                    "type": "status_update", "key_invalid": False,
                })
            if current_price <= 0 and not is_demo:
                try:
                    from routers.market_router import _fetch_price
                    fallback = await _fetch_price(symbol)
                    if fallback:
                        current_price = fallback
                except Exception as _e:
                    logger.warning(f"Price fetch fallback failed for {user_id}/{symbol}: {_e}")
            if current_price <= 0:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            state.price_history.append(current_price)
            if len(state.price_history) > 2000:
                state.price_history = state.price_history[-2000:]

            # Fetch ETH price for correlation filter (BTC/Robinhood only — skip for ETH loop
            # and for Capital.com/Tradovate where ETH-USD is not a valid symbol)
            from broker_base import get_asset_class, ASSET_CLASS_PRESETS as _ACP
            _use_eth_for_symbol = (
                _ACP[get_asset_class(symbol)]["use_eth_correlation"]
                and symbol.upper() != "ETH-USD"
                and broker == "robinhood"
            )
            if _use_eth_for_symbol:
                try:
                    eth_price = await client.get_current_price("ETH-USD")
                    if eth_price > 0:
                        state.eth_price_history.append(eth_price)
                        if len(state.eth_price_history) > 200:
                            state.eth_price_history = state.eth_price_history[-200:]
                except Exception as _e:
                    logger.debug(f"ETH correlation fetch skipped for {user_id}: {_e}")

            # Auto-optimize: run quantum optimizer every N ticks (non-blocking)
            # Skip if AI calibration ran recently (within 50 ticks) to avoid param conflicts
            tick_count = len(state.price_history)
            if (tick_count >= 100
                    and tick_count - state.last_optimize_tick >= AUTO_OPTIMIZE_INTERVAL
                    and tick_count - state.last_calibration_tick >= 50
                    and not state.in_trade):
                state.last_optimize_tick = tick_count
                try:
                    from quantum_optimizer import quick_optimize
                    result = quick_optimize(list(state.price_history))
                    if result and result.get("optimal_params") and result.get("score", 0) > 0:
                        params = result["optimal_params"]
                        async with AsyncSessionLocal() as opt_db:
                            updates = {}
                            if "entry_z" in params:
                                updates["entry_z"] = round(float(params["entry_z"]), 4)
                            if "lookback" in params:
                                updates["lookback"] = str(int(params["lookback"]))
                            if "stop_loss_pct" in params:
                                updates["stop_loss_pct"] = round(float(params["stop_loss_pct"]), 4)
                            if "take_profit_pct" in params:
                                updates["take_profit_pct"] = round(float(params["take_profit_pct"]), 4)
                            if "trail_stop_pct" in params:
                                updates["trail_stop_pct"] = round(float(params["trail_stop_pct"]), 4)
                            if updates:
                                await opt_db.execute(
                                    update(User).where(User.id == user_id).values(**updates)
                                )
                                await opt_db.commit()
                                logger.info(
                                    f"Auto-optimized params for {user_id[:8]}: "
                                    f"score={result['score']:.4f} params={updates}"
                                )
                except Exception as e:
                    logger.debug(f"Auto-optimize skipped: {e}")

            z_score = _calculate_zscore(state.price_history, lookback)

            # Compute indicators for UI display and signal filtering
            indicators = compute_all_indicators(state.price_history, lookback)
            state.indicators = {
                k: (round(v, 4) if isinstance(v, float) else v)
                for k, v in indicators.items() if v is not None
            }

            if z_score is None:
                await ws_manager.send_to_user(user_id, {
                    "type": "status_update",
                    "symbol": symbol,
                    "price": current_price,
                    "z_score": 0.0,
                    "in_trade": False,
                    "entry_price": None,
                    "trade_side": None,
                    "trail_stop": None,
                    "last_signal": f"Warming up\u2026 {len(state.price_history)}/{lookback} ticks",
                    "demo_mode": is_demo,
                    "indicators": state.indicators,
                })
                await asyncio.sleep(POLL_INTERVAL)
                continue

            state.last_update = datetime.now(timezone.utc).isoformat()

            # Track bullish/bearish retest levels
            MAX_LEVELS = 10
            if len(state.price_history) >= 2:
                prev_prices = state.price_history[:-1]
                prev_z = _calculate_zscore(prev_prices, min(lookback, len(prev_prices)))
                if prev_z is not None:
                    if prev_z < entry_z_thresh and z_score >= entry_z_thresh:
                        state.bullish_levels.append(current_price)
                        if len(state.bullish_levels) > MAX_LEVELS:
                            state.bullish_levels.pop(0)
                    elif prev_z > -entry_z_thresh and z_score <= -entry_z_thresh:
                        state.bearish_levels.append(current_price)
                        if len(state.bearish_levels) > MAX_LEVELS:
                            state.bearish_levels.pop(0)

            bullish_retest = any(
                abs(current_price - lvl) <= lvl * tolerance_pct
                for lvl in state.bullish_levels
            )
            bearish_retest = any(
                abs(current_price - lvl) <= lvl * tolerance_pct
                for lvl in state.bearish_levels
            )

            # Demo: inject synthetic signals to ensure enough trades per day (~4-6 trades)
            # 20% chance per tick × 10 ticks/min = ~2 signal chances/min when not in trade.
            # Signals are mutually exclusive: injecting bullish clears any stale bearish and vice versa.
            # ── CRITICAL: only inject when z-score is elevated in the correct direction ──
            # Without this gate, signals fire at z≈0 → z immediately "reverts" back → $0.02 exits.
            # Requiring |z| >= 0.60 ensures the z-reversion exit fires meaningfully after real movement.
            if is_demo and not state.in_trade and len(state.price_history) >= lookback:
                if random.random() < 0.20 and z_score is not None and abs(z_score) >= 0.60:
                    if z_score > 0:
                        # Positive z → oversold relative to mean → buy the retest
                        bullish_retest = True
                        bearish_retest = False   # prevent conflicting signal on same tick
                        state.bullish_levels.append(current_price)
                    else:
                        # Negative z → overbought relative to mean → sell the retest
                        bearish_retest = True
                        bullish_retest = False   # prevent conflicting signal on same tick
                        state.bearish_levels.append(current_price)

            # Classify market regime (runs every tick, cached for 5min by ai_screener)
            state.regime = await classify_regime(user_id, state.price_history, state.indicators)

            # ATR-adaptive trailing stop — widens in volatile markets, tightens in calm
            current_atr = atr_from_prices(state.price_history, 14)
            if current_atr and current_price > 0:
                atr_pct = current_atr / current_price
                # Adaptive trail: use max of user setting and 1.5x ATR%
                adaptive_trail = max(trail_stop_pct, atr_pct * 1.5)
                # Cap at 5% to prevent runaway
                adaptive_trail = min(adaptive_trail, 0.05)
            else:
                adaptive_trail = trail_stop_pct

            # ── PROGRESSIVE TRAIL TIGHTENING: lock in more of the winner as profit grows ──
            # At 1.5R captured: trail tightens to 60% of base
            # At 2.0R captured: trail tightens to 40% of base (very protective)
            # Past partial-profit trades benefit most — they already locked 0.5R, this lets
            # the runner ride further with tighter protection.
            if state.in_trade and state.entry_price and state.entry_price > 0:
                _r_distance = stop_loss_pct  # 1R in %
                _profit_pct_now = (
                    (current_price - state.entry_price) / state.entry_price if state.trade_side == "buy"
                    else (state.entry_price - current_price) / state.entry_price
                )
                _r_multiple = _profit_pct_now / _r_distance if _r_distance > 0 else 0
                if _r_multiple >= 2.0:
                    adaptive_trail = max(adaptive_trail * 0.40, 0.003)   # min 0.3%
                elif _r_multiple >= 1.5:
                    adaptive_trail = max(adaptive_trail * 0.60, 0.005)   # min 0.5%

            # ── TRAIL ACTIVATION GATE ──
            # The previous behavior set the trail stop at entry-trail% on tick #1, so any
            # normal price wobble hit it before the trade had a cushion. That cost
            # avg −$9.64 per trail-stop exit across 46 trades (= −$215.79 net leak).
            # New rule: trail stays None until profit reaches +0.5R, then it arms at
            # current_price - adaptive_trail. After arming, it ratchets toward price
            # exactly like before (one-way ladder, never widens).
            if state.in_trade and state.entry_price:
                _ep = state.entry_price
                _profit_r = 0.0
                if stop_loss_pct > 0:
                    if state.trade_side == "buy":
                        _profit_r = ((current_price - _ep) / _ep) / stop_loss_pct
                    else:
                        _profit_r = ((_ep - current_price) / _ep) / stop_loss_pct

                if state.trail_stop_price is None:
                    # Arm only after the trade is genuinely in profit (≥0.5R).
                    if _profit_r >= 0.5:
                        state.trail_stop_price = (
                            current_price * (1 - adaptive_trail) if state.trade_side == "buy"
                            else current_price * (1 + adaptive_trail)
                        )
                        logger.info(
                            f"Trail armed for {user_id}/{symbol} at {_profit_r:.2f}R: "
                            f"price={current_price:.4f} trail={state.trail_stop_price:.4f}"
                        )
                else:
                    # Already armed — ratchet toward price (never loosen).
                    if state.trade_side == "buy":
                        state.trail_stop_price = max(
                            state.trail_stop_price,
                            current_price * (1 - adaptive_trail)
                        )
                    else:
                        state.trail_stop_price = min(
                            state.trail_stop_price,
                            current_price * (1 + adaptive_trail)
                        )

            # ── Exit logic ──
            if state.in_trade and state.entry_price and state.current_trade_id:
                ep = state.entry_price
                # Use regime-adaptive TP locked at entry (falls back to user setting if not set)
                _tp_pct_active = state.adaptive_tp_pct if state.adaptive_tp_pct else take_profit_pct
                sl = ep * (1 - stop_loss_pct) if state.trade_side == "buy" else ep * (1 + stop_loss_pct)
                tp = ep * (1 + _tp_pct_active) if state.trade_side == "buy" else ep * (1 - _tp_pct_active)

                # ── PARTIAL PROFIT AT 1.5R: close 50% of position once 1.5× SL distance in profit ──
                # Moved from 1R → 1.5R: firing at 1R was too early — BTC noise clips winners
                # before they mature. 1.5R ensures the move is real before locking half profit.
                # Remaining 50% runs to full TP (2× SL) or trail stop.
                if not state.partial_exit_done and state.initial_quantity > 0:
                    one_r_move = stop_loss_pct * 1.5  # 1.5× stop distance in %
                    profit_pct_now = (
                        (current_price - ep) / ep if state.trade_side == "buy"
                        else (ep - current_price) / ep
                    )
                    if profit_pct_now >= one_r_move:
                        # Close 50% of the position
                        half_qty = round(state.initial_quantity * 0.50, 8)
                        from broker_base import get_asset_class, ASSET_CLASS_PRESETS as _PP
                        _pres = _PP[get_asset_class(symbol)]
                        half_qty = max(_pres["qty_step"], round(round(half_qty / _pres["qty_step"]) * _pres["qty_step"], _pres["qty_precision"]))
                        if _pres["qty_precision"] == 0:
                            half_qty = int(half_qty)

                        close_side = "sell" if state.trade_side == "buy" else "buy"
                        try:
                            # Execute partial close — same on demo and live
                            await client.place_market_order(symbol, close_side, str(half_qty))

                            # Record partial P&L (doesn't close the trade — position continues)
                            partial_diff = (
                                (current_price - ep) if state.trade_side == "buy"
                                else (ep - current_price)
                            )
                            partial_pnl = partial_diff * half_qty
                            risk_mgr.daily_pnl += partial_pnl   # book the locked profit
                            state.partial_pnl_booked = partial_pnl  # remember for final-close accounting
                            # Persist updated daily_pnl — partial gains must survive a restart
                            _rpp = asyncio.create_task(_persist_risk_state(user_id, risk_mgr))
                            _background_tasks.add(_rpp); _rpp.add_done_callback(_background_tasks.discard)

                            # Reduce remaining quantity
                            state.current_quantity = max(
                                _pres["qty_step"],
                                round(state.initial_quantity - half_qty, 8)
                            )
                            if _pres["qty_precision"] == 0:
                                state.current_quantity = int(state.current_quantity)
                            state.partial_exit_done = True

                            # Persist partial_pnl on the Trade record (so analytics see total profit)
                            try:
                                async with AsyncSessionLocal() as db_p:
                                    await db_p.execute(
                                        update(Trade).where(Trade.id == state.current_trade_id).values(
                                            partial_pnl=partial_pnl
                                        )
                                    )
                                    await db_p.commit()
                            except Exception as _pe:
                                logger.warning(f"Persist partial_pnl failed for {user_id}: {_pe}")

                            # Persist demo balance after partial close
                            if is_demo and hasattr(client, 'balance'):
                                async with AsyncSessionLocal() as db2:
                                    await db2.execute(
                                        update(User).where(User.id == user_id).values(demo_balance=round(client.balance, 2))
                                    )
                                    await db2.commit()

                            logger.info(
                                f"Partial exit {user_id}: closed {half_qty} @ {current_price}, "
                                f"booked +${partial_pnl:.2f}, remaining {state.current_quantity}"
                            )
                            await ws_manager.send_to_user(user_id, {
                                "type": "partial_exit",
                                "symbol": symbol,
                                "price": current_price,
                                "closed_qty": half_qty,
                                "remaining_qty": state.current_quantity,
                                "partial_pnl": round(partial_pnl, 2),
                                "message": f"💰 Locked +${partial_pnl:.2f} (50% sold at 1R). Runner on remainder.",
                            })
                        except Exception as _e:
                            logger.warning(f"Partial-exit order failed for {user_id}: {_e}")

                # ── BREAKEVEN STOP: move SL to entry once 50% of TP distance is captured ──
                if not state.breakeven_moved and ep > 0:
                    tp_dist = abs(tp - ep)
                    profit_captured = (
                        (current_price - ep) if state.trade_side == "buy"
                        else (ep - current_price)
                    )
                    if profit_captured >= tp_dist * 0.50:
                        # Slide the static SL to entry (lock-in breakeven)
                        if state.trade_side == "buy":
                            sl = max(sl, ep)  # SL can only move up
                        else:
                            sl = min(sl, ep)  # SL can only move down
                        state.breakeven_moved = True
                        logger.info(f"Breakeven stop activated for {user_id} @ entry={ep:.4f}")
                        await ws_manager.send_to_user(user_id, {
                            "type": "status_update_minor",
                            "message": f"🔒 Breakeven locked — SL moved to entry ${ep:,.2f}",
                        })
                elif state.breakeven_moved and ep > 0:
                    # Keep enforcing breakeven floor on subsequent ticks
                    if state.trade_side == "buy":
                        sl = max(sl, ep)
                    else:
                        sl = min(sl, ep)

                # ── SMART EXIT: z-reversion (primary) + hard time cap (fallback) ──
                # Mean-reversion thesis: entered at |z|>=1.3 expecting return to 0.
                # Once |z|<0.3 with profit, signal is fulfilled — exit, redeploy capital.
                # Hard cap at 5h = one full 20-bar lookback. Past that, edge is decayed
                # below noise floor; we'd be praying not trading. SL/TP/trail handle
                # everything in between (don't curve-fit asymmetric loser/winner timers
                # for a strategy that already has SL/trail doing outcome management).
                _time_limit_exit = False
                _smart_exit_reason = None
                if state.trade_open_time is not None and ep > 0:
                    _elapsed_hours = (time.time() - state.trade_open_time) / 3600

                    # R-multiple (only used to gate z-reversion exit on profitable trades)
                    sl_dist = ep * (user.stop_loss_pct if user.stop_loss_pct is not None else 0.015)
                    if sl_dist > 0:
                        if state.trade_side == "buy":
                            r_now = (current_price - ep) / sl_dist
                        else:
                            r_now = (ep - current_price) / sl_dist
                    else:
                        r_now = 0.0

                    # 1) Z-REVERSION — signal premise fulfilled, take the win
                    # Minimum dollar P&L gate — prevents the $0.05/$0.24 exits that plagued
                    # the old strategy. Fires only when the position has built real profit.
                    # DEMO: $15 minimum unrealised PnL before z-revert can close the trade.
                    #        At 0.5% SL and 0.079 BTC, $15 = ~0.25% price move — realistic.
                    # LIVE: R-multiple gate (0.50R = half of 1R captured).
                    _MIN_ZREVERT_PNL = 15.0   # $15 minimum profit to z-revert in demo
                    _unrealised_pnl = (
                        (current_price - ep) * state.current_quantity
                        if state.trade_side == "buy"
                        else (ep - current_price) * state.current_quantity
                    )
                    if abs(z_score) < 0.3:
                        if is_demo and _unrealised_pnl >= _MIN_ZREVERT_PNL:
                            _time_limit_exit = True
                            _smart_exit_reason = "z_reverted"
                            logger.info(
                                f"Z-reversion exit (demo) {user_id}: z={z_score:.2f}, "
                                f"pnl=${_unrealised_pnl:.2f}, elapsed={_elapsed_hours:.1f}h"
                            )
                        elif not is_demo and r_now >= 0.50:
                            _time_limit_exit = True
                            _smart_exit_reason = "z_reverted"
                            logger.info(
                                f"Z-reversion exit (live) {user_id}: z={z_score:.2f}, "
                                f"r={r_now:.2f}, elapsed={_elapsed_hours:.1f}h"
                            )
                    # 2) HARD TIME CAP — one full lookback window (5h on 15m bars)
                    elif _elapsed_hours >= 5.0:
                        _time_limit_exit = True
                        _smart_exit_reason = "time_limit"
                        logger.info(
                            f"Time-cap exit {user_id}: elapsed={_elapsed_hours:.1f}h, r={r_now:.2f}"
                        )
                    # Otherwise: SL / TP / trail stop handle the trade.

                exit_reason = None
                if _time_limit_exit:
                    exit_reason = _smart_exit_reason or "time_limit"
                elif state.trade_side == "buy":
                    if current_price <= sl:
                        exit_reason = "stop_loss"
                    elif current_price >= tp:
                        exit_reason = "take_profit"
                    elif state.trail_stop_price and current_price <= state.trail_stop_price:
                        exit_reason = "trailing_stop"
                else:
                    if current_price >= sl:
                        exit_reason = "stop_loss"
                    elif current_price <= tp:
                        exit_reason = "take_profit"
                    elif state.trail_stop_price and current_price >= state.trail_stop_price:
                        exit_reason = "trailing_stop"

                if exit_reason:
                    logger.info(f"Exiting {user_id}: {exit_reason} @ {current_price}")
                    close_side = "sell" if state.trade_side == "buy" else "buy"
                    if not is_demo:
                        try:
                            await client.place_market_order(symbol, close_side, str(state.current_quantity))
                        except Exception as e:
                            logger.error(f"Close order error for {user_id}: {e}")
                            await ws_manager.send_to_user(user_id, {
                                "type": "bot_error",
                                "message": f"Close order failed: {str(e)[:120]}. Trade remains open.",
                            })
                            continue
                    else:
                        # Demo mode: execute close order on mock client to update balance
                        await client.place_market_order(symbol, close_side, str(state.current_quantity))

                    price_diff = (
                        (current_price - state.entry_price) if state.trade_side == "buy"
                        else (state.entry_price - current_price)
                    )
                    close_leg_pnl = price_diff * state.current_quantity
                    # TOTAL trade P&L = profit booked during partial + close-leg P&L
                    pnl = close_leg_pnl + state.partial_pnl_booked
                    pnl_pct = (price_diff / state.entry_price) * 100 if state.entry_price > 0 else 0.0

                    await _close_trade(
                        user_id, state.current_trade_id, current_price, exit_reason,
                        state.entry_price, state.trade_side, state.entry_z_score or 0, z_score,
                        symbol=symbol, quantity=state.current_quantity,
                        partial_pnl=state.partial_pnl_booked,
                    )

                    # Update risk manager: daily_pnl gets close-leg only (partial already booked),
                    # but Kelly tracker uses TOTAL to correctly grade the trade.
                    risk_mgr.record_trade_close(close_leg_pnl, exit_reason, total_pnl=pnl)
                    # Persist the snapshot — survives redeploys (daily_pnl, Kelly, cooldowns)
                    _rp = asyncio.create_task(_persist_risk_state(user_id, risk_mgr))
                    _background_tasks.add(_rp); _rp.add_done_callback(_background_tasks.discard)

                    # Track consecutive losses/wins and cooldown
                    if pnl < 0:
                        state.consecutive_losses += 1
                        state.consecutive_wins = 0
                        if exit_reason == "stop_loss":
                            state.last_stop_loss_time = time.time()
                    else:
                        state.consecutive_losses = 0
                        state.consecutive_wins += 1

                    # Record pattern for AI memory (lightweight per-hour failure tracker)
                    record_pattern(user_id, {
                        "side": state.trade_side,
                        "pnl": pnl,
                        "exit_reason": exit_reason,
                        "z_score": z_score,
                        "regime": state.regime,
                    })

                    # ── PERSISTENT STRATEGY MEMORY ──
                    # Update aggregated bucket stats — this is the system's permanent
                    # knowledge base. Demo and live both contribute (real prices, real
                    # indicators); is_demo is tagged so live can be weighted later.
                    # Storage is bounded: every trade increments existing buckets.
                    try:
                        from strategy_memory import record_strategy_outcome as _rec_strat
                        _entry_z_for_mem = state.entry_z_score if state.entry_z_score is not None else z_score
                        # Signal strength is captured at the trade-open moment (0.0-1.0).
                        # Falling back to the live recompute or last AI confidence (rescaled
                        # from 0-100 → 0-1) keeps recording even on legacy in-flight trades
                        # that were opened before this field existed.
                        if state.entry_signal_strength is not None:
                            _signal_strength_at_entry = float(state.entry_signal_strength)
                        else:
                            _ai_conf_raw = (state.last_ai_screen or {}).get("confidence")
                            if _ai_conf_raw is not None:
                                # AI screener returns 0-100; bucketing expects 0-1
                                _ai_conf = float(_ai_conf_raw)
                                _signal_strength_at_entry = _ai_conf / 100.0 if _ai_conf > 1.0 else _ai_conf
                            else:
                                _signal_strength_at_entry = 0.5
                            logger.warning(
                                f"entry_signal_strength missing for {user_id[:8]} {symbol} — "
                                f"using fallback {_signal_strength_at_entry:.2f}"
                            )
                        _duration_min = (
                            (time.time() - state.trade_open_time) / 60
                            if state.trade_open_time else 0.0
                        )
                        _hour_at_entry = (
                            datetime.fromtimestamp(state.trade_open_time, tz=timezone.utc).hour
                            if state.trade_open_time else datetime.now(timezone.utc).hour
                        )
                        _mt = asyncio.create_task(_rec_strat(
                            user_id=user_id,
                            symbol=symbol,
                            side=state.trade_side,
                            hour_utc=_hour_at_entry,
                            regime=state.regime,
                            signal_strength=float(_signal_strength_at_entry or 0.5),
                            z_score=float(_entry_z_for_mem or 0.0),
                            pnl=float(pnl),
                            pnl_pct=float(pnl_pct),
                            duration_minutes=float(_duration_min),
                            is_demo=bool(is_demo),
                            exit_reason=str(exit_reason),
                        ))
                        _background_tasks.add(_mt); _mt.add_done_callback(_background_tasks.discard)
                    except Exception as _me:
                        logger.warning(f"Strategy memory update failed for {user_id}: {_me}")

                    # Persist updated demo balance to DB
                    if is_demo and hasattr(client, 'balance'):
                        async with AsyncSessionLocal() as db2:
                            await db2.execute(
                                update(User).where(User.id == user_id).values(demo_balance=client.balance)
                            )
                            await db2.commit()

                    # Daily target progress — compounds with balance
                    _live_balance = client.balance if is_demo and hasattr(client, 'balance') else balance
                    # Daily MINIMUM target: $200 on small accounts, 2.5% on larger → compounds with balance.
                    # On $10k → $250. On $20k → $500. NOT a cap — bot keeps trading past it for max profit.
                    _daily_target = max(200.0, _live_balance * 0.025)
                    _daily_pnl = risk_mgr.daily_pnl
                    _progress_pct = min(100.0, max(0.0, (_daily_pnl / _daily_target) * 100)) if _daily_target > 0 else 0.0

                    await ws_manager.send_to_user(user_id, {
                        "type": "trade_closed",
                        "symbol": symbol,
                        "exit_price": current_price,
                        "exit_reason": exit_reason,
                        "pnl": round(pnl, 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "demo_mode": is_demo,
                        "demo_balance": round(_live_balance, 2) if is_demo else None,
                        "risk": risk_mgr.get_status(),
                        # Daily compounding target
                        "daily_pnl": round(_daily_pnl, 2),
                        "daily_target": round(_daily_target, 2),
                        "daily_progress_pct": round(_progress_pct, 1),
                        "daily_target_hit": _daily_pnl >= _daily_target,
                    })

                    # Telegram notification
                    if getattr(user, 'telegram_enabled', False):
                        _nt = asyncio.create_task(notifications.notify_trade_closed(
                            symbol, state.trade_side, state.entry_price, current_price,
                            pnl, pnl_pct, exit_reason, is_demo
                        ))
                        _background_tasks.add(_nt); _nt.add_done_callback(_background_tasks.discard)

                    # Check if risk manager paused trading
                    if risk_mgr.is_paused:
                        await ws_manager.send_to_user(user_id, {
                            "type": "risk_pause",
                            "message": risk_mgr.pause_reason,
                        })
                        if getattr(user, 'telegram_enabled', False):
                            _nt = asyncio.create_task(notifications.notify_risk_pause(risk_mgr.pause_reason))
                            _background_tasks.add(_nt); _nt.add_done_callback(_background_tasks.discard)

                    state.in_trade = False
                    state.entry_price = None
                    state.trade_side = None
                    state.trail_stop_price = None
                    state.current_trade_id = None
                    state.entry_z_score = None
                    state.entry_signal_strength = None
                    state.breakeven_moved = False
                    state.trade_open_time = None
                    state.partial_exit_done = False
                    state.initial_quantity = 0.0
                    state.partial_pnl_booked = 0.0
                    state.adaptive_tp_pct = None
                    from broker_base import get_asset_class, ASSET_CLASS_PRESETS as _FQP
                    state.current_quantity = _FQP[get_asset_class(symbol)]["qty_step"]

            # ── Option A: Second concurrent position exit ──
            # Manages exit for the second simultaneous slot (opened when signal_strength ≥ 0.70).
            # Uses simplified exit: SL/TP/trail/time-cap only (no partial exit on second slot).
            if state.second_slot is not None:
                s2 = state.second_slot
                ep2 = s2["entry_price"]
                side2 = s2["side"]
                qty2 = s2["quantity"]
                tp_pct2 = s2.get("adaptive_tp_pct") or take_profit_pct
                sl2 = ep2 * (1 - stop_loss_pct) if side2 == "buy" else ep2 * (1 + stop_loss_pct)
                tp2 = ep2 * (1 + tp_pct2) if side2 == "buy" else ep2 * (1 - tp_pct2)

                # Second-slot trail: same activation gate as primary — arm only at +0.5R profit
                _profit_r2 = 0.0
                if stop_loss_pct > 0:
                    if side2 == "buy":
                        _profit_r2 = ((current_price - ep2) / ep2) / stop_loss_pct
                    else:
                        _profit_r2 = ((ep2 - current_price) / ep2) / stop_loss_pct

                if not s2.get("trail_stop_price"):
                    if _profit_r2 >= 0.5:
                        s2["trail_stop_price"] = (
                            current_price * (1 - adaptive_trail) if side2 == "buy"
                            else current_price * (1 + adaptive_trail)
                        )
                else:
                    if side2 == "buy":
                        s2["trail_stop_price"] = max(s2["trail_stop_price"], current_price * (1 - adaptive_trail))
                    else:
                        s2["trail_stop_price"] = min(s2["trail_stop_price"], current_price * (1 + adaptive_trail))

                # Breakeven on second slot
                if not s2.get("breakeven_moved") and ep2 > 0:
                    _tp2_dist = abs(tp2 - ep2)
                    _profit2 = (current_price - ep2) if side2 == "buy" else (ep2 - current_price)
                    if _profit2 >= _tp2_dist * 0.50:
                        if side2 == "buy":
                            sl2 = max(sl2, ep2)
                        else:
                            sl2 = min(sl2, ep2)
                        s2["breakeven_moved"] = True
                elif s2.get("breakeven_moved"):
                    if side2 == "buy":
                        sl2 = max(sl2, ep2)
                    else:
                        sl2 = min(sl2, ep2)

                # Check exit conditions for second slot
                exit_reason2 = None
                _elapsed2 = (time.time() - s2["trade_open_time"]) / 3600
                _profit_pct2 = (current_price - ep2) / ep2 if side2 == "buy" else (ep2 - current_price) / ep2
                # Second slot z-revert: same dollar gate as primary slot
                _sl_dist2 = ep2 * (user.stop_loss_pct if user.stop_loss_pct is not None else 0.005)
                _r_now2 = (_profit_pct2 * ep2) / _sl_dist2 if _sl_dist2 > 0 else 0.0
                _unrealised_pnl2 = _profit_pct2 * ep2 * qty2
                if abs(z_score) < 0.3:
                    if is_demo and _unrealised_pnl2 >= 15.0:
                        exit_reason2 = "z_reverted"
                    elif not is_demo and _r_now2 >= 0.50:
                        exit_reason2 = "z_reverted"
                elif _elapsed2 >= 5.0:
                    exit_reason2 = "time_limit"
                elif side2 == "buy":
                    if current_price <= sl2:
                        exit_reason2 = "stop_loss"
                    elif current_price >= tp2:
                        exit_reason2 = "take_profit"
                    elif s2.get("trail_stop_price") and current_price <= s2["trail_stop_price"]:
                        exit_reason2 = "trailing_stop"
                else:
                    if current_price >= sl2:
                        exit_reason2 = "stop_loss"
                    elif current_price <= tp2:
                        exit_reason2 = "take_profit"
                    elif s2.get("trail_stop_price") and current_price >= s2["trail_stop_price"]:
                        exit_reason2 = "trailing_stop"

                if exit_reason2:
                    close_side2 = "sell" if side2 == "buy" else "buy"
                    if not is_demo:
                        try:
                            await client.place_market_order(symbol, close_side2, str(qty2))
                        except Exception as _e2:
                            logger.error(f"Close slot2 order failed for {user_id}: {_e2}")
                    else:
                        await client.place_market_order(symbol, close_side2, str(qty2))

                    _diff2 = (current_price - ep2) if side2 == "buy" else (ep2 - current_price)
                    _pnl2 = _diff2 * qty2
                    _pnl_pct2 = (_diff2 / ep2) * 100 if ep2 > 0 else 0.0

                    await _close_trade(
                        user_id, s2["trade_id"], current_price, exit_reason2,
                        ep2, side2, s2.get("entry_z", 0.0), z_score,
                        symbol=symbol, quantity=qty2,
                    )
                    risk_mgr.record_trade_close(_pnl2, exit_reason2, total_pnl=_pnl2)
                    _rp2 = asyncio.create_task(_persist_risk_state(user_id, risk_mgr))
                    _background_tasks.add(_rp2); _rp2.add_done_callback(_background_tasks.discard)

                    if _pnl2 < 0:
                        state.consecutive_losses += 1
                        state.consecutive_wins = 0
                    else:
                        state.consecutive_losses = 0
                        state.consecutive_wins += 1

                    if is_demo and hasattr(client, 'balance'):
                        async with AsyncSessionLocal() as _db2:
                            await _db2.execute(
                                update(User).where(User.id == user_id).values(demo_balance=round(client.balance, 2))
                            )
                            await _db2.commit()

                    # ── Feed the strategy_memory bucket for the second slot too ──
                    # Without this, ~half of high-confidence trades never enter the
                    # learning system and the AI calibrator gets a biased sample.
                    try:
                        from strategy_memory import record_strategy_outcome as _rec_strat_s2
                        _s2_dur_min = (
                            (time.time() - s2["trade_open_time"]) / 60
                            if s2.get("trade_open_time") else 0.0
                        )
                        _s2_hour = (
                            datetime.fromtimestamp(s2["trade_open_time"], tz=timezone.utc).hour
                            if s2.get("trade_open_time") else datetime.now(timezone.utc).hour
                        )
                        _s2_ss = float(s2.get("entry_signal_strength") or 0.7)
                        _s2_ez = float(s2.get("entry_z") or z_score)
                        _t2 = asyncio.create_task(_rec_strat_s2(
                            user_id=user_id, symbol=symbol, side=side2,
                            hour_utc=_s2_hour, regime=state.regime,
                            signal_strength=_s2_ss, z_score=_s2_ez,
                            pnl=float(_pnl2), pnl_pct=float(_pnl_pct2),
                            duration_minutes=float(_s2_dur_min),
                            is_demo=bool(is_demo), exit_reason=str(exit_reason2),
                        ))
                        _background_tasks.add(_t2); _t2.add_done_callback(_background_tasks.discard)
                    except Exception as _se2:
                        logger.warning(f"Second-slot strategy memory record failed for {user_id}: {_se2}")

                    state.second_slot = None
                    logger.info(f"Second slot closed for {user_id}/{symbol}: {exit_reason2} @ {current_price}, P&L ${_pnl2:.2f}")
                    await ws_manager.send_to_user(user_id, {
                        "type": "trade_closed",
                        "symbol": symbol,
                        "exit_price": current_price,
                        "exit_reason": exit_reason2,
                        "pnl": round(_pnl2, 2),
                        "pnl_pct": round(_pnl_pct2, 2),
                        "demo_mode": is_demo,
                        "demo_balance": round(client.balance, 2) if is_demo and hasattr(client, 'balance') else None,
                        "slot": "second",
                    })

            # ── Entry logic ──
            elif not state.in_trade:
                # ── DAILY PROFIT TARGET STOP: protect locked-in gains ──
                _cur_bal_entry = client.balance if hasattr(client, 'balance') else balance
                _daily_tgt_entry = max(200.0, _cur_bal_entry * 0.025)
                _daily_target_hit = risk_mgr.daily_pnl >= _daily_tgt_entry
                # NOTE: target hit is NOT a stop — it's a milestone. Bot keeps trading to
                # maximize daily profit, but later sizing applies a "house-money" ratchet
                # (smaller positions) so locked gains aren't given back on a single bad trade.
                if False:
                    pass
                else:
                    # Check risk manager
                    can_trade, risk_reason = risk_mgr.can_trade()
                    if not can_trade:
                        state.last_signal = f"Paused: {risk_reason}"
                    else:
                        entry_side = None
                        signal = None
                        if bullish_retest:
                            entry_side = "buy"
                            signal = f"Bullish retest @ ${current_price:,.2f} (Z={z_score:.2f})"
                        elif bearish_retest:
                            entry_side = "sell"
                            signal = f"Bearish retest @ ${current_price:,.2f} (Z={z_score:.2f})"

                        if entry_side:
                            # ── SYMBOL-LEVEL ENTRY GATE ──
                            # Audit on 298 closed trades found ETH-USD posted 0/28 z-reverts
                            # (mean-reversion premise never fires) → −$18.37 net of pure decay.
                            # Block new entries on disabled symbols; existing open positions
                            # still get managed to close by the same loop.
                            if symbol in NO_NEW_ENTRY_SYMBOLS:
                                state.last_signal = f"{symbol} entries paused (0% z-revert hist) — Z={z_score:.2f}"
                                entry_side = None
                                # Skip the rest of the entry pipeline
                                pass
                            # Apply indicator filters (now includes multi-TF, regime, time, correlation)
                            passed, filter_reasons = (False, ["symbol disabled"]) if not entry_side else _check_signal_filters(
                                state.price_history, entry_side, user, state, z_score, symbol=symbol
                            )
                            is_premium_user = getattr(user, 'is_premium', False)

                            # ── VOLATILITY SQUEEZE FILTER: skip entries when BB too narrow ──
                            # Low BB width = market coiled / indecisive → signals are low quality
                            if passed:
                                _bb_now = bollinger_bands(state.price_history, int(getattr(user, 'lookback', 20)))
                                if _bb_now is not None and current_price > 0:
                                    _bb_w = (_bb_now["upper"] - _bb_now["lower"]) / current_price
                                    state.bb_width_history.append(_bb_w)
                                    if len(state.bb_width_history) > 50:
                                        state.bb_width_history.pop(0)
                                    if len(state.bb_width_history) >= 20:
                                        _avg_bb_w = sum(state.bb_width_history) / len(state.bb_width_history)
                                        if _bb_w < _avg_bb_w * 0.50:  # width < 50% of avg = squeeze
                                            passed = False
                                            filter_reasons = [f"Volatility squeeze: BB width {_bb_w:.4f} < 50% of avg {_avg_bb_w:.4f} — market coiling"]

                            # Gate: allow entry if filters passed, OR demo 30% bypass for trade volume.
                            # The bypass still runs the signal quality gate inside — only the
                            # indicator filters (RSI/ADX/BB) are relaxed, not the strength floor.
                            _allow_entry = passed
                            if not passed and is_demo and random.random() < 0.30:
                                _allow_entry = True  # 30% bypass for demo volume
                            if not _allow_entry:
                                if filter_reasons:
                                    state.last_signal = f"Signal filtered: {filter_reasons[0]}"
                                if not is_demo:
                                    logger.info(f"Signal filtered for {user_id}: {filter_reasons}")
                            else:
                                # Calculate signal strength for position sizing
                                signal_strength = _calculate_signal_strength(
                                    z_score, state.slow_z_score, state.regime,
                                    state.indicators, entry_side
                                )

                                # ── SIGNAL QUALITY GATE (all users) ──
                                # Minimum strength 0.45 for everyone — blocks weakest 25% of signals
                                # that historically contribute most losses. High-confidence signals
                                # (0.75+) get position doubled — user's "duplicate best setups" logic.
                                _min_strength = 0.55 if is_premium_user else 0.45
                                if signal_strength < _min_strength:
                                    state.last_signal = f"Quality gate: signal too weak ({signal_strength:.0%} < {_min_strength:.0%}), waiting"
                                    logger.info(f"Quality gate blocked {entry_side} for {user_id}: strength={signal_strength:.2f}")
                                    continue

                                # ── STRATEGY MEMORY GATE — query the system's permanent knowledge ──
                                # Looks up the historical outcome bucket matching this exact setup
                                # (symbol/side/hour/regime/strength/z). If the bucket has been seen
                                # ≥ 10 times with < 35% win rate AND negative avg P&L, BLOCK the trade.
                                # Demo + live both feed this — every trade makes the system smarter.
                                # Premium users get cross-user fallback when their personal samples
                                # are thin → benefit from collective learning faster.
                                try:
                                    from strategy_memory import score_setup
                                    _now_hour = datetime.now(timezone.utc).hour
                                    _setup = await score_setup(
                                        user_id=user_id, symbol=symbol, side=entry_side,
                                        hour_utc=_now_hour, regime=state.regime,
                                        signal_strength=signal_strength, z_score=z_score,
                                    )
                                    state.last_setup_score = _setup
                                    if _setup["recommendation"] == "skip":
                                        state.last_signal = f"Strategy memory: {_setup['reason']}"
                                        logger.info(
                                            f"Memory-gated entry blocked for {user_id} "
                                            f"({entry_side}@{symbol}): {_setup['reason']}"
                                        )
                                        continue
                                except Exception as _se:
                                    logger.debug(f"Strategy memory check skipped: {_se}")

                                # ── PREMIUM ADVANTAGE 2: AI Pre-trade screening ──
                                ai_approved = True
                                ai_confidence = 0
                                if is_premium_user and os.getenv("ANTHROPIC_API_KEY"):
                                    try:
                                        async with AsyncSessionLocal() as screen_db:
                                            recent_r = await screen_db.execute(
                                                select(Trade).where(
                                                    Trade.user_id == user_id,
                                                    Trade.state == "closed"
                                                ).order_by(Trade.closed_at.desc()).limit(10)
                                            )
                                            recent_trades = [{
                                                "side": t.side, "pnl": t.pnl or 0,
                                                "exit_reason": t.exit_reason
                                            } for t in recent_r.scalars().all()]

                                        screen_result = await screen_trade(
                                            user_id, entry_side, current_price,
                                            z_score, state.indicators, recent_trades,
                                            state.regime, signal_strength,
                                            symbol=symbol,
                                        )
                                        state.last_ai_screen = screen_result
                                        ai_confidence = screen_result.get("confidence", 50)
                                        if not screen_result.get("take", True):
                                            ai_approved = False
                                            state.last_signal = f"AI blocked: {screen_result.get('reasoning', 'low confidence')}"
                                            logger.info(f"AI screen blocked trade for {user_id}: {screen_result}")

                                        # ── PREMIUM ADVANTAGE 3: AI-adjusted risk params ──
                                        # If AI suggests tighter SL/TP, use them
                                        if ai_approved and screen_result.get("adjusted_sl"):
                                            suggested_sl = float(screen_result["adjusted_sl"])
                                            if 0.005 < suggested_sl < stop_loss_pct:
                                                stop_loss_pct = suggested_sl
                                                logger.info(f"AI tightened SL to {suggested_sl:.3f} for {user_id}")
                                        if ai_approved and screen_result.get("adjusted_tp"):
                                            suggested_tp = float(screen_result["adjusted_tp"])
                                            if suggested_tp > take_profit_pct:
                                                take_profit_pct = suggested_tp
                                                logger.info(f"AI widened TP to {suggested_tp:.3f} for {user_id}")
                                    except Exception as e:
                                        logger.debug(f"AI screen error: {e}")

                                # ── PREMIUM ADVANTAGE 4: Pattern memory filter ──
                                if is_premium_user and ai_approved:
                                    try:
                                        insights = get_pattern_insights(user_id)
                                        current_hour = datetime.now(timezone.utc).hour
                                        # Check if this hour+side combo has >65% loss rate
                                        if insights.get("bad_hours") and current_hour in insights["bad_hours"]:
                                            state.last_signal = f"Pattern memory: hour {current_hour} UTC historically loses"
                                            logger.info(f"Pattern memory blocked {entry_side} at hour {current_hour} for {user_id}")
                                            continue
                                    except Exception as _e:
                                        logger.debug(f"Pattern memory skipped for {user_id}: {_e}")

                                if not ai_approved:
                                    pass  # Skip entry — AI blocked it
                                else:
                                    logger.info(f"Entering {entry_side} for {user_id} @ {current_price} (strength={signal_strength:.0%}, regime={state.regime}, premium={is_premium_user})")
                                    state.last_signal = signal

                                    # Signal-strength position sizing
                                    pos_mode = getattr(user, 'position_size_mode', 'dynamic') or 'dynamic'
                                    if pos_mode == "fixed":
                                        quantity = getattr(user, 'fixed_quantity', 0.0001) or 0.0001
                                    else:
                                        base_qty = risk_mgr.calculate_position_size(
                                            balance, current_price, stop_loss_pct, state.price_history
                                        )
                                        # ── CONFIDENCE-BASED POSITION SIZING ──
                                        # High-confidence setups get doubled ("duplicate the best" logic).
                                        # Weak signals that pass the gate get normal size.
                                        # This concentrates capital on the best setups without changing trade count.
                                        if signal_strength >= 0.80:
                                            strength_mult = 2.5   # Strongest setups: 2.5× — full "duplicate"
                                            logger.info(f"High-confidence entry {user_id}: {signal_strength:.0%} → 2.5× size")
                                        elif signal_strength >= 0.70:
                                            strength_mult = 2.0   # Strong: 2× size
                                            logger.info(f"Strong entry {user_id}: {signal_strength:.0%} → 2× size")
                                        elif signal_strength >= 0.60:
                                            strength_mult = 1.5   # Medium: 1.5×
                                        else:
                                            strength_mult = 1.0   # Baseline (just cleared quality gate)
                                        # ── AI confidence overlay (premium) ──
                                        if is_premium_user and ai_confidence > 70:
                                            strength_mult = min(3.0, strength_mult * 1.2)
                                        # ── Golden hour boost: up to 25% larger during best hours ──
                                        strength_mult = min(3.0, strength_mult * _golden_boost)
                                        # ── LOSING STREAK REDUCTION: size down after 2 consecutive losses ──
                                        if state.consecutive_losses >= 2:
                                            _loss_mult = 0.60
                                            strength_mult *= _loss_mult
                                            logger.info(f"Streak reduction ({state.consecutive_losses} losses): sizing at {_loss_mult:.0%}")
                                        # ── WINNER STREAK BOOST: size up slightly after 3 consecutive wins ──
                                        elif state.consecutive_wins >= 3:
                                            _win_mult = min(1.10, 1.0 + (state.consecutive_wins - 2) * 0.03)
                                            strength_mult = min(1.5, strength_mult * _win_mult)
                                            logger.info(f"Streak boost ({state.consecutive_wins} wins): sizing at {strength_mult:.0%}")
                                        # ── KELLY-ADJUSTED SIZING: auto-defend when edge shrinks ──
                                        # Shrinks position when rolling win-rate/RR deteriorates.
                                        _kelly_mult = risk_mgr.kelly_fraction()
                                        strength_mult *= _kelly_mult
                                        if _kelly_mult < 0.7 or _kelly_mult > 1.2:
                                            logger.info(f"Kelly adjustment: {_kelly_mult:.2f}× (edge signal)")
                                        # ── REALIZED-VOL GUARD: cut size 50% during volatility spikes ──
                                        # If recent realized vol > 2× the longer-window average, market is in
                                        # an unstable regime → halve the position regardless of signal strength.
                                        if len(state.price_history) >= 50:
                                            _short = state.price_history[-10:]
                                            _long = state.price_history[-50:]
                                            _short_std = (sum((p - sum(_short)/10)**2 for p in _short) / 10) ** 0.5
                                            _long_std = (sum((p - sum(_long)/50)**2 for p in _long) / 50) ** 0.5
                                            import math as _math
                                            if (_long_std > 0
                                                    and not _math.isnan(_short_std)
                                                    and not _math.isnan(_long_std)
                                                    and _short_std / _long_std > 2.0):
                                                strength_mult *= 0.50
                                                logger.info(f"Vol-spike guard: short/long σ = {_short_std/_long_std:.2f} → halving size")
                                        # ── HOUSE-MONEY RATCHET: after daily target is hit, keep trading
                                        # but with smaller risk so locked-in gains don't bleed out.
                                        # Profit is NEVER capped — we just press lighter on the gas.
                                        if _daily_target_hit:
                                            strength_mult *= 0.65
                                            logger.info(
                                                f"House-money ratchet {user_id[:8]}: target +${risk_mgr.daily_pnl:,.0f} "
                                                f"hit → 0.65× size (still hunting more profit)"
                                            )
                                        # ── BUCKET-CONFIDENCE BOOST: concentrate capital on proven winners ──
                                        # When this exact bucket (symbol+side+hour+regime+strength+z) has a
                                        # historically high win rate over a meaningful sample, push 1.25× size.
                                        # When it's historically poor but didn't fully fail the gate, cut to 0.7×.
                                        try:
                                            _ss = state.last_setup_score
                                            if _ss and isinstance(_ss, dict):
                                                _wr = float(_ss.get("win_rate") or 0.0)
                                                _n = int(_ss.get("sample_count") or 0)
                                                if _n >= 20 and _wr >= 0.60:
                                                    strength_mult = min(3.0, strength_mult * 1.25)
                                                    logger.info(
                                                        f"Bucket boost {user_id[:8]}: {_n} samples @ {_wr:.0%} win → 1.25× size"
                                                    )
                                                elif _n >= 20 and _wr < 0.45:
                                                    strength_mult *= 0.70
                                                    logger.info(
                                                        f"Bucket caution {user_id[:8]}: {_n} samples @ {_wr:.0%} win → 0.70× size"
                                                    )
                                        except Exception as _e:
                                            logger.debug(f"Bucket-confidence sizing skipped: {_e}")
                                        quantity = round(base_qty * strength_mult, 8)

                                        # ── HARD EXPOSURE CAP (post-multiplier) ──
                                        # strength_mult × Kelly can exceed the exposure cap set in
                                        # calculate_position_size. Enforce it absolutely here so no
                                        # combination of multipliers can bust the user's risk limit.
                                        if balance > 0 and current_price > 0:
                                            _abs_max = (balance * (risk_mgr.max_exposure_pct / 100.0)) / current_price
                                            if quantity > _abs_max:
                                                logger.info(
                                                    f"Exposure cap enforced for {user_id[:8]}/{symbol}: "
                                                    f"{quantity:.4f} → {_abs_max:.4f} "
                                                    f"(multipliers exceeded {risk_mgr.max_exposure_pct:.0f}% limit)"
                                                )
                                                quantity = _abs_max

                                    # ── Asset-class quantity rounding ──
                                    from broker_base import get_asset_class, ASSET_CLASS_PRESETS as _QP
                                    _preset = _QP[get_asset_class(symbol)]
                                    _step = _preset["qty_step"]
                                    _prec = _preset["qty_precision"]
                                    quantity = max(_step, round(round(quantity / _step) * _step, _prec))
                                    if _prec == 0:
                                        quantity = int(quantity)

                                    state.current_quantity = quantity
                                    qty_str = f"{quantity:.8f}".rstrip('0').rstrip('.') if isinstance(quantity, float) else str(quantity)

                                    rh_order_id = ""
                                    if not is_demo:
                                        try:
                                            order = await client.place_market_order(symbol, entry_side, qty_str)
                                            rh_order_id = order.get("id", "")
                                        except Exception as e:
                                            logger.error(f"Live order failed for {user_id}: {e}")
                                            err_msg = str(e)
                                            if "403" in err_msg or "401" in err_msg:
                                                await ws_manager.send_to_user(user_id, {
                                                    "type": "bot_error",
                                                    "message": f"Order rejected by Robinhood: {err_msg[:120]}. Check your API key permissions.",
                                                })
                                            else:
                                                await ws_manager.send_to_user(user_id, {
                                                    "type": "bot_error",
                                                    "message": f"Order failed: {err_msg[:120]}",
                                                })
                                            continue

                                    # In demo mode, execute mock order and check for rejection
                                    if is_demo:
                                        demo_order = await client.place_market_order(symbol, entry_side, qty_str)
                                        if demo_order.get("state") == "rejected":
                                            logger.warning(f"Demo order rejected for {user_id[:8]}: {demo_order.get('reason')}")
                                            state.last_signal = f"Order rejected: {demo_order.get('reason', 'insufficient balance')}"
                                            continue

                                    # Save indicators snapshot with trade
                                    ind_snapshot = json.dumps({
                                        k: (round(v, 4) if isinstance(v, (int, float)) else v)
                                        for k, v in state.indicators.items()
                                    })

                                    trade_id = await _save_trade(user_id, {
                                        "symbol": symbol,
                                        "side": entry_side,
                                        "quantity": qty_str,
                                        "quantity_value": quantity,
                                        "initial_quantity": quantity,   # full size at entry
                                        "entry_price": str(current_price),
                                        "state": "open",
                                        "is_demo": is_demo,
                                        "rh_order_id": rh_order_id,
                                        "indicators_snapshot": ind_snapshot,
                                        "opened_at": datetime.now(timezone.utc),
                                    })
                                    state.in_trade = True
                                    state.entry_price = current_price
                                    state.trade_side = entry_side
                                    state.entry_z_score = z_score
                                    state.entry_signal_strength = float(signal_strength)
                                    state.current_trade_id = trade_id
                                    state.trade_open_time = time.time()   # ← time-limit tracking
                                    state.breakeven_moved = False          # ← reset for new trade
                                    state.partial_exit_done = False        # ← reset partial-profit flag
                                    state.initial_quantity = quantity      # remember full size
                                    # ── ADAPTIVE R/R: lock TP based on current regime at entry ──
                                    _sl_adj, _tp_adj = _adaptive_rr(stop_loss_pct, take_profit_pct, state.regime, entry_side)
                                    state.adaptive_tp_pct = _tp_adj
                                    logger.info(f"Adaptive R/R for {user_id}: regime={state.regime} SL={_sl_adj:.3f} TP={_tp_adj:.3f}")
                                    # Leave trail unset at entry — arming gate above
                                    # turns it on after +0.5R profit. Eliminates false
                                    # trail-stop exits on early adverse wobble.
                                    state.trail_stop_price = None
                                    # Persist demo balance after entry deduction
                                    if is_demo and hasattr(client, 'balance'):
                                        async with AsyncSessionLocal() as db2:
                                            await db2.execute(
                                                update(User).where(User.id == user_id).values(demo_balance=round(client.balance, 2))
                                            )
                                            await db2.commit()

                                    await ws_manager.send_to_user(user_id, {
                                        "type": "trade_opened",
                                        "symbol": symbol,
                                        "side": entry_side,
                                        "entry_price": current_price,
                                        "demo_mode": is_demo,
                                        "quantity": quantity,
                                        "demo_balance": round(client.balance, 2) if is_demo and hasattr(client, 'balance') else None,
                                    })

                                    # Telegram notification
                                    if getattr(user, 'telegram_enabled', False):
                                        _nt = asyncio.create_task(notifications.notify_trade_opened(
                                            symbol, entry_side, current_price, quantity, is_demo
                                        ))
                                        _background_tasks.add(_nt); _nt.add_done_callback(_background_tasks.discard)

            # ── Option A: Second concurrent position entry ──
            # When the primary slot is occupied AND a new high-confidence signal fires,
            # open a second independent position on the same symbol.
            # Requires signal_strength ≥ 0.70 and risk manager not paused.
            elif state.in_trade and state.second_slot is None:
                _cur_bal_s2 = client.balance if hasattr(client, 'balance') else balance
                _daily_tgt_s2 = max(200.0, _cur_bal_s2 * 0.025)
                # Always allow second-slot entry — never cap profit. House-money ratchet handles risk.
                _s2_can_trade, _ = risk_mgr.can_trade()
                if _s2_can_trade and (bullish_retest or bearish_retest):
                    _s2_side = "buy" if bullish_retest else "sell"
                    # Only enter second slot in same direction as primary (don't hedge)
                    if _s2_side == state.trade_side and symbol not in NO_NEW_ENTRY_SYMBOLS:
                        _s2_passed, _ = _check_signal_filters(
                            state.price_history, _s2_side, user, state, z_score, symbol=symbol
                        )
                        if _s2_passed or is_demo:
                            _s2_strength = _calculate_signal_strength(
                                z_score, state.slow_z_score, state.regime,
                                state.indicators, _s2_side
                            )
                            if _s2_strength >= 0.70:
                                # Position size for second slot: half of normal (risk control)
                                _s2_base_qty = risk_mgr.calculate_position_size(
                                    _cur_bal_s2, current_price, stop_loss_pct, state.price_history
                                )
                                _s2_qty = round(_s2_base_qty * 0.5 * min(3.0, _s2_strength * 2.0), 8)
                                from broker_base import get_asset_class, ASSET_CLASS_PRESETS as _S2QP
                                _s2_preset = _S2QP[get_asset_class(symbol)]
                                _s2_qty = max(_s2_preset["qty_step"], round(round(_s2_qty / _s2_preset["qty_step"]) * _s2_preset["qty_step"], _s2_preset["qty_precision"]))
                                if _s2_preset["qty_precision"] == 0:
                                    _s2_qty = int(_s2_qty)
                                _s2_qty_str = f"{_s2_qty:.8f}".rstrip('0').rstrip('.') if isinstance(_s2_qty, float) else str(_s2_qty)

                                _s2_order_ok = True
                                if not is_demo:
                                    try:
                                        await client.place_market_order(symbol, _s2_side, _s2_qty_str)
                                    except Exception as _e2:
                                        logger.warning(f"Second slot entry order failed for {user_id}: {_e2}")
                                        _s2_order_ok = False
                                else:
                                    _s2_demo_order = await client.place_market_order(symbol, _s2_side, _s2_qty_str)
                                    if _s2_demo_order.get("state") == "rejected":
                                        _s2_order_ok = False

                                if _s2_order_ok:
                                    _s2_ind_snap = json.dumps({k: (round(v, 4) if isinstance(v, (int, float)) else v) for k, v in state.indicators.items()})
                                    _s2_trade_id = await _save_trade(user_id, {
                                        "symbol": symbol,
                                        "side": _s2_side,
                                        "quantity": _s2_qty_str,
                                        "quantity_value": _s2_qty,
                                        "initial_quantity": _s2_qty,
                                        "entry_price": str(current_price),
                                        "state": "open",
                                        "is_demo": is_demo,
                                        "indicators_snapshot": _s2_ind_snap,
                                        "opened_at": datetime.now(timezone.utc),
                                    })
                                    _s2_sl_adj, _s2_tp_adj = _adaptive_rr(stop_loss_pct, take_profit_pct, state.regime, _s2_side)
                                    # Trail starts disarmed — second-slot trail-update block above
                                    # arms it once profit ≥ 0.5R, mirroring the primary-slot rule.
                                    _s2_trail = None
                                    state.second_slot = {
                                        "trade_id": _s2_trade_id,
                                        "entry_price": current_price,
                                        "side": _s2_side,
                                        "entry_z": z_score,
                                        "entry_signal_strength": float(_s2_strength),
                                        "quantity": _s2_qty,
                                        "trail_stop_price": _s2_trail,
                                        "trade_open_time": time.time(),
                                        "adaptive_tp_pct": _s2_tp_adj,
                                        "breakeven_moved": False,
                                    }
                                    logger.info(f"Second slot opened for {user_id}/{symbol}: {_s2_side} @ {current_price} qty={_s2_qty} strength={_s2_strength:.0%}")
                                    await ws_manager.send_to_user(user_id, {
                                        "type": "trade_opened",
                                        "symbol": symbol,
                                        "side": _s2_side,
                                        "entry_price": current_price,
                                        "demo_mode": is_demo,
                                        "quantity": _s2_qty,
                                        "slot": "second",
                                        "demo_balance": round(client.balance, 2) if is_demo and hasattr(client, 'balance') else None,
                                    })

            # Daily target progress for UI progress bar
            _cur_balance = client.balance if hasattr(client, 'balance') else balance
            _daily_target_now = max(200.0, _cur_balance * 0.025)  # 2.5% of current balance, min $200
            _daily_progress = min(100.0, max(0.0, (risk_mgr.daily_pnl / _daily_target_now) * 100)) if _daily_target_now > 0 else 0.0

            # Send status update — hide strategy internals (z_score, indicators) from clients
            await ws_manager.send_to_user(user_id, {
                "type": "status_update",
                "symbol": symbol,
                "price": current_price,
                "in_trade": state.in_trade,
                "entry_price": state.entry_price,
                "trade_side": state.trade_side,
                "trail_stop": state.trail_stop_price,
                "last_signal": state.last_signal,
                "demo_mode": is_demo,
                "demo_balance": round(_cur_balance, 2) if is_demo and hasattr(client, 'balance') else None,
                "position_size": state.current_quantity,
                "risk": risk_mgr.get_status(),
                # Daily compounding target
                "daily_pnl": round(risk_mgr.daily_pnl, 2),
                "daily_target": round(_daily_target_now, 2),
                "daily_progress_pct": round(_daily_progress, 1),
            })

        except asyncio.CancelledError:
            logger.info(f"Bot task cancelled for user {user_id}")
            break
        except Exception as e:
            err_str = str(e)
            logger.error(f"Bot loop error for {user_id}: {err_str}")

            if ("401" in err_str or "403" in err_str) and "trading.robinhood.com" in err_str:
                if "401" in err_str:
                    msg = "Robinhood API key rejected (401). Paste your key again in Settings and click 'Test Connection'."
                else:
                    msg = ("Robinhood API key not authorized (403). "
                           "Make sure 'Crypto Trading' permission is enabled on your key at robinhood.com \u2192 Account \u2192 Crypto API.")
                logger.warning(f"Robinhood auth error for {user_id}: {err_str}")
                state.force_demo = True
                state.demo_mode = True
                state.key_invalid = True
                state.error_count = 0
                await ws_manager.send_to_user(user_id, {"type": "key_invalid", "message": msg})
                await asyncio.sleep(30)
                continue

            state.error_count += 1
            await ws_manager.send_to_user(user_id, {"type": "bot_error", "message": err_str})
            if state.error_count > 10:
                # Auto-restart after a back-off pause instead of dying permanently.
                # A dead loop leaves open positions unmanaged (no stop-loss checks).
                # Reset error count, sleep 120s, then resume — covers transient API blips.
                logger.warning(
                    f"Bot for {user_id}/{symbol}: {state.error_count} errors, pausing 120s then auto-restarting"
                )
                await ws_manager.send_to_user(user_id, {
                    "type": "bot_error",
                    "message": f"Too many errors on {symbol}. Auto-restarting in 2 minutes...",
                })
                state.error_count = 0
                await asyncio.sleep(120)
                continue

        await asyncio.sleep(POLL_INTERVAL)

    logger.info(f"Bot loop ended for user {user_id} symbol={symbol}")
