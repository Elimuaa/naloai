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
    broker = getattr(user, 'broker_type', 'robinhood') or 'robinhood'
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


# Auto-optimization interval (run quantum optimizer every N ticks)
AUTO_OPTIMIZE_INTERVAL = 200

# Dead zone hours (UTC) — data-driven from 14-month BTC audit (RC Quantum Signal Engine)
# CONSERVATIVE blacklist: only the WORST 4 hours where edge is statistically negative.
# Was {1,6,9,11,13,14,17,18} (8h) — too restrictive, was cutting volume in half vs RC Quantum.
# Now {1,11,13,18} — kept the 4 hours with strongest negative edge; opens up 4 more trading hours/day.
DEAD_ZONE_HOURS = {1, 11, 13, 18}

# Minimum cooldown after stop loss (seconds)
MIN_COOLDOWN_SECONDS = 600  # 10 minutes — re-enter faster after a loss (was 15)

bot_states: dict[str, BotState] = {}
_bot_tasks: dict[str, asyncio.Task] = {}
_client_cache: dict[str, object] = {}
_risk_managers: dict[str, RiskManager] = {}


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


async def start_bot(user_id: str, force_demo: bool = False):
    if user_id in _bot_tasks and not _bot_tasks[user_id].done():
        return {"status": "already_running"}
    state = BotState(force_demo=force_demo)

    # Recover open trade state from DB to prevent duplicate entries after restart.
    # If multiple open trades exist (from prior restarts), force-close the extras.
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Trade).where(Trade.user_id == user_id, Trade.state == "open")
                .order_by(Trade.opened_at.desc())
            )
            all_open = result.scalars().all()

            if len(all_open) > 1:
                # Force-close older duplicate trades — bot can only manage one at a time
                extras = all_open[1:]
                extra_ids = [t.id for t in extras]
                from sqlalchemy import update as sa_update
                await db.execute(
                    sa_update(Trade).where(Trade.id.in_(extra_ids)).values(
                        state="closed",
                        exit_reason="force_closed_duplicate",
                        closed_at=datetime.now(timezone.utc),
                        pnl=0.0,
                        pnl_pct=0.0,
                    )
                )
                await db.commit()
                logger.warning(f"Force-closed {len(extras)} duplicate open trades for {user_id}")

            open_trade = all_open[0] if all_open else None
            if open_trade and open_trade.entry_price:
                state.in_trade = True
                state.entry_price = float(open_trade.entry_price)
                state.trade_side = open_trade.side
                state.current_trade_id = open_trade.id
                state.current_quantity = open_trade.quantity_value or float(open_trade.quantity)
                # Set trailing stop based on entry price (updated with live price in loop)
                user_conf = await db.execute(select(User).where(User.id == user_id))
                u = user_conf.scalar_one_or_none()
                trail_pct = u.trail_stop_pct if u else 0.015
                ep = state.entry_price
                state.trail_stop_price = ep * (1 - trail_pct) if open_trade.side == "buy" else ep * (1 + trail_pct)
                logger.info(f"Restored open trade {open_trade.id[:8]} for {user_id}: {open_trade.side} @ {open_trade.entry_price}")
    except Exception as e:
        logger.error(f"Failed to restore open trade for {user_id}: {e}")
        state.in_trade = False
        state.entry_price = None
        state.trade_side = None
        state.current_trade_id = None

    bot_states[user_id] = state
    task = asyncio.create_task(_bot_loop(user_id), name=f"bot-{user_id}")
    _bot_tasks[user_id] = task
    return {"status": "started"}


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
    if user_id in _bot_tasks:
        _bot_tasks[user_id].cancel()
        try:
            await asyncio.wait_for(asyncio.shield(_bot_tasks[user_id]), timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        del _bot_tasks[user_id]
    for k in list(_client_cache.keys()):
        if k.startswith(user_id):
            del _client_cache[k]
    async with AsyncSessionLocal() as db:
        await db.execute(update(User).where(User.id == user_id).values(bot_active=False))
        await db.commit()
    await notifications.notify_bot_stopped()
    return {"status": "stopped"}


def get_bot_status(user_id: str) -> dict:
    running = user_id in _bot_tasks and not _bot_tasks[user_id].done()
    state = bot_states.get(user_id, BotState())
    risk_mgr = _risk_managers.get(user_id)
    return {
        "running": running,
        "in_trade": state.in_trade,
        "entry_price": state.entry_price,
        "trade_side": state.trade_side,
        "trail_stop": state.trail_stop_price,
        "last_signal": state.last_signal,
        "last_update": state.last_update,
        "error_count": state.error_count,
        "demo_mode": state.demo_mode,
        "key_invalid": state.key_invalid,
        "position_size": state.current_quantity,
        "risk": risk_mgr.get_status() if risk_mgr else None,
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
    state: 'BotState', z_score: float
) -> tuple[bool, list[str]]:
    """Apply indicator filters + new advanced filters. Returns (passed, reasons_rejected)."""
    reasons = []
    current_price = prices[-1]

    # ── TIME-OF-DAY FILTER (asset-class aware) ──
    from broker_base import get_asset_class, ASSET_CLASS_PRESETS
    _asset_preset = ASSET_CLASS_PRESETS[get_asset_class(getattr(user, 'trading_symbol', 'BTC-USD'))]
    _dead_zone = _asset_preset["dead_zone_hours"]
    _use_eth_corr = _asset_preset["use_eth_correlation"]
    current_hour = datetime.now(timezone.utc).hour
    if current_hour in _dead_zone:
        reasons.append(f"Time filter: {current_hour}:00 UTC is outside trading hours for {user.trading_symbol}")

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
    asyncio.create_task(_run_ai_analysis(trade_id, {
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
    }, user_id))


async def _run_ai_analysis(trade_id: str, trade_data: dict, user_id: str):
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
        state = bot_states.get(user_id)
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


async def _bot_loop(user_id: str):
    logger.info(f"Bot started for user {user_id}")
    state = bot_states.get(user_id, BotState())

    while True:
        try:
            user = await _get_user_config(user_id)
            if not user or not user.bot_active:
                logger.info(f"Bot disabled for user {user_id}, stopping")
                break

            broker = getattr(user, 'broker_type', 'robinhood') or 'robinhood'
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
            risk_mgr.reset_daily(balance)

            symbol = user.trading_symbol
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

            # Fetch ETH price for correlation filter (crypto only)
            from broker_base import get_asset_class, ASSET_CLASS_PRESETS as _ACP
            _use_eth_for_symbol = _ACP[get_asset_class(symbol)]["use_eth_correlation"]
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
            # 20% chance per tick × 10 ticks/min = ~2 signal chances/min when not in trade
            if is_demo and not state.in_trade and len(state.price_history) >= lookback:
                if random.random() < 0.20:
                    if random.random() > 0.5:
                        bullish_retest = True
                        state.bullish_levels.append(current_price)
                    else:
                        bearish_retest = True
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

            if state.in_trade and state.entry_price:
                if state.trade_side == "buy" and state.trail_stop_price:
                    state.trail_stop_price = max(
                        state.trail_stop_price,
                        current_price * (1 - adaptive_trail)
                    )
                elif state.trade_side == "sell" and state.trail_stop_price:
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
                    sl_dist = ep * (user.stop_loss_pct or 0.025)
                    if sl_dist > 0:
                        if state.trade_side == "buy":
                            r_now = (current_price - ep) / sl_dist
                        else:
                            r_now = (ep - current_price) / sl_dist
                    else:
                        r_now = 0.0

                    # 1) Z-REVERSION — signal premise fulfilled, take the win
                    if abs(z_score) < 0.3 and r_now > 0:
                        _time_limit_exit = True
                        _smart_exit_reason = "z_reverted"
                        logger.info(
                            f"Z-reversion exit {user_id}: z={z_score:.2f}, r={r_now:.2f}, "
                            f"elapsed={_elapsed_hours:.1f}h"
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

                    # Track consecutive losses/wins and cooldown
                    if pnl < 0:
                        state.consecutive_losses += 1
                        state.consecutive_wins = 0
                        if exit_reason == "stop_loss":
                            state.last_stop_loss_time = time.time()
                    else:
                        state.consecutive_losses = 0
                        state.consecutive_wins += 1

                    # Record pattern for AI memory
                    record_pattern(user_id, {
                        "side": state.trade_side,
                        "pnl": pnl,
                        "exit_reason": exit_reason,
                        "z_score": z_score,
                        "regime": state.regime,
                    })

                    # Persist updated demo balance to DB
                    if is_demo and hasattr(client, 'balance'):
                        async with AsyncSessionLocal() as db2:
                            await db2.execute(
                                update(User).where(User.id == user_id).values(demo_balance=client.balance)
                            )
                            await db2.commit()

                    # Daily target progress — compounds with balance
                    _live_balance = client.balance if is_demo and hasattr(client, 'balance') else balance
                    _daily_target = max(200.0, _live_balance * 0.02)   # 2% of current balance, min $200
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
                        asyncio.create_task(notifications.notify_trade_closed(
                            symbol, state.trade_side, state.entry_price, current_price,
                            pnl, pnl_pct, exit_reason, is_demo
                        ))

                    # Check if risk manager paused trading
                    if risk_mgr.is_paused:
                        await ws_manager.send_to_user(user_id, {
                            "type": "risk_pause",
                            "message": risk_mgr.pause_reason,
                        })
                        if getattr(user, 'telegram_enabled', False):
                            asyncio.create_task(notifications.notify_risk_pause(risk_mgr.pause_reason))

                    state.in_trade = False
                    state.entry_price = None
                    state.trade_side = None
                    state.trail_stop_price = None
                    state.current_trade_id = None
                    state.entry_z_score = None
                    state.breakeven_moved = False
                    state.trade_open_time = None
                    state.partial_exit_done = False
                    state.initial_quantity = 0.0
                    state.partial_pnl_booked = 0.0
                    state.adaptive_tp_pct = None
                    from broker_base import get_asset_class, ASSET_CLASS_PRESETS as _FQP
                    state.current_quantity = _FQP[get_asset_class(symbol)]["qty_step"]

            # ── Entry logic ──
            elif not state.in_trade:
                # ── DAILY PROFIT TARGET STOP: protect locked-in gains ──
                _cur_bal_entry = client.balance if hasattr(client, 'balance') else balance
                _daily_tgt_entry = max(200.0, _cur_bal_entry * 0.02)
                _daily_target_hit = risk_mgr.daily_pnl >= _daily_tgt_entry
                if _daily_target_hit:
                    state.last_signal = (
                        f"🎯 Daily target ${_daily_tgt_entry:,.0f} hit "
                        f"(+${risk_mgr.daily_pnl:,.2f}) — protecting gains until tomorrow"
                    )
                    # Bot stays active but won't enter new trades today
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
                            # Apply indicator filters (now includes multi-TF, regime, time, correlation)
                            passed, filter_reasons = _check_signal_filters(
                                state.price_history, entry_side, user, state, z_score
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

                            if not passed and not is_demo:
                                state.last_signal = f"Signal filtered: {filter_reasons[0]}"
                                logger.info(f"Signal filtered for {user_id}: {filter_reasons}")
                            elif not passed and is_demo and random.random() > 0.3:
                                state.last_signal = f"Signal filtered: {filter_reasons[0]}"
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
                                            state.regime, signal_strength
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
                                            if _long_std > 0 and _short_std / _long_std > 2.0:
                                                strength_mult *= 0.50
                                                logger.info(f"Vol-spike guard: short/long σ = {_short_std/_long_std:.2f} → halving size")
                                        quantity = round(base_qty * strength_mult, 8)

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
                                    state.current_trade_id = trade_id
                                    state.trade_open_time = time.time()   # ← time-limit tracking
                                    state.breakeven_moved = False          # ← reset for new trade
                                    state.partial_exit_done = False        # ← reset partial-profit flag
                                    state.initial_quantity = quantity      # remember full size
                                    # ── ADAPTIVE R/R: lock TP based on current regime at entry ──
                                    _sl_adj, _tp_adj = _adaptive_rr(stop_loss_pct, take_profit_pct, state.regime, entry_side)
                                    state.adaptive_tp_pct = _tp_adj
                                    logger.info(f"Adaptive R/R for {user_id}: regime={state.regime} SL={_sl_adj:.3f} TP={_tp_adj:.3f}")
                                    state.trail_stop_price = (
                                        current_price * (1 - adaptive_trail) if entry_side == "buy"
                                        else current_price * (1 + adaptive_trail)
                                    )
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
                                        asyncio.create_task(notifications.notify_trade_opened(
                                            symbol, entry_side, current_price, quantity, is_demo
                                        ))

            # Daily target progress for UI progress bar
            _cur_balance = client.balance if hasattr(client, 'balance') else balance
            _daily_target_now = max(200.0, _cur_balance * 0.02)  # 2% of current balance
            _daily_progress = min(100.0, max(0.0, (risk_mgr.daily_pnl / _daily_target_now) * 100)) if _daily_target_now > 0 else 0.0

            # Send status update — hide strategy internals (z_score, indicators) from clients
            await ws_manager.send_to_user(user_id, {
                "type": "status_update",
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
                await ws_manager.send_to_user(user_id, {
                    "type": "bot_error",
                    "message": "Bot stopped after too many errors. Check your settings and restart.",
                })
                break

        await asyncio.sleep(POLL_INTERVAL)

    logger.info(f"Bot loop ended for user {user_id}")
