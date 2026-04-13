import asyncio
import logging
import json
import os
import random
from datetime import datetime, timezone
from typing import Optional
import numpy as np
from database import AsyncSessionLocal, User, Trade
from ws_manager import ws_manager
from post_trade_ai_learner import analyze_trade
from sqlalchemy import select, update

logger = logging.getLogger(__name__)

def _get_client(user: User, force_demo: bool = False):
    """Return real or mock Robinhood client depending on key availability and mode.
    Caches the client to preserve internal state (e.g. _market_data_forbidden)."""
    cache_key = f"{user.id}:{'demo' if force_demo else 'live'}"
    if cache_key in _client_cache:
        return _client_cache[cache_key]

    if not force_demo:
        private_key = user.ed25519_private_key or user.rh_private_key
        if user.rh_api_key and private_key:
            from robinhood import create_client
            client = create_client(user.rh_api_key, private_key)
            if client:
                logger.info(f"Using LIVE Robinhood client for user {user.id}")
                _client_cache[cache_key] = client
                return client
            logger.error(f"Failed to create real client for user {user.id}, falling back to mock")
        else:
            logger.info(f"No keys for user {user.id} (api_key={bool(user.rh_api_key)}, priv={bool(private_key)}), using mock")
    else:
        logger.info(f"force_demo=True for user {user.id}, using mock")
    from mock_robinhood import MockRobinhoodClient
    client = MockRobinhoodClient(symbol=user.trading_symbol, balance=user.demo_balance or 10000.0)
    _client_cache[cache_key] = client
    return client


class BotState:
    def __init__(self, force_demo: bool = False):
        self.price_history: list[float] = []
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
        self.key_invalid: bool = False  # set True on 401; cleared when user saves a new key


bot_states: dict[str, BotState] = {}
_bot_tasks: dict[str, asyncio.Task] = {}
_client_cache: dict[str, object] = {}  # user_id -> cached RobinhoodCryptoClient or MockRobinhoodClient


async def start_bot(user_id: str, force_demo: bool = False):
    if user_id in _bot_tasks and not _bot_tasks[user_id].done():
        return {"status": "already_running"}
    state = BotState(force_demo=force_demo)
    bot_states[user_id] = state
    task = asyncio.create_task(_bot_loop(user_id), name=f"bot-{user_id}")
    _bot_tasks[user_id] = task
    return {"status": "started"}


async def stop_bot(user_id: str):
    if user_id in _bot_tasks:
        _bot_tasks[user_id].cancel()
        try:
            await asyncio.wait_for(asyncio.shield(_bot_tasks[user_id]), timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        del _bot_tasks[user_id]
    # Clear cached client so next start uses fresh credentials
    for k in list(_client_cache.keys()):
        if k.startswith(user_id):
            del _client_cache[k]
    async with AsyncSessionLocal() as db:
        await db.execute(update(User).where(User.id == user_id).values(bot_active=False))
        await db.commit()
    return {"status": "stopped"}


def get_bot_status(user_id: str) -> dict:
    running = user_id in _bot_tasks and not _bot_tasks[user_id].done()
    state = bot_states.get(user_id, BotState())
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
    }


def _calculate_zscore(prices: list[float], lookback: int) -> Optional[float]:
    if len(prices) < lookback:
        return None
    window = prices[-lookback:]
    mean = np.mean(window)
    std = np.std(window)
    if std == 0:
        return 0.0
    return float((prices[-1] - mean) / std)


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
    entry_price: float, side: str, entry_z: float, current_z: float
):
    pnl = (exit_price - entry_price) if side == "buy" else (entry_price - exit_price)
    pnl_pct = (pnl / entry_price) * 100
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(Trade).where(Trade.id == trade_id).values(
                exit_price=str(exit_price),
                pnl=pnl,
                pnl_pct=pnl_pct,
                state="closed",
                exit_reason=exit_reason,
                closed_at=datetime.now(timezone.utc)
            )
        )
        await db.commit()
    asyncio.create_task(_run_ai_analysis(trade_id, {
        "symbol": "BTC-USD",
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


async def _bot_loop(user_id: str):
    logger.info(f"Bot started for user {user_id}")
    state = bot_states.get(user_id, BotState())

    while True:
        try:
            user = await _get_user_config(user_id)
            if not user or not user.bot_active:
                logger.info(f"Bot disabled for user {user_id}, stopping")
                break

            is_demo = state.force_demo or not user.rh_api_key
            state.demo_mode = is_demo
            # Poll faster in demo mode so UI shows activity quickly
            POLL_INTERVAL = 6 if is_demo else 60

            client = _get_client(user, force_demo=state.force_demo)
            if not client:
                await asyncio.sleep(30)
                continue

            symbol = user.trading_symbol
            entry_z_thresh = user.entry_z
            lookback = int(user.lookback)
            stop_loss_pct = user.stop_loss_pct
            take_profit_pct = user.take_profit_pct
            trail_stop_pct = user.trail_stop_pct
            tolerance_pct = 0.005 if is_demo else 0.01

            current_price = await client.get_current_price(symbol)
            # Successful Robinhood call — clear any previous key_invalid flag
            if not is_demo and state.key_invalid:
                state.key_invalid = False
                await ws_manager.send_to_user(user_id, {
                    "type": "status_update",
                    "key_invalid": False,
                })
            # If live client returns 0/None, fall back to public market API
            if current_price <= 0 and not is_demo:
                try:
                    from routers.market_router import _fetch_price
                    fallback = await _fetch_price(symbol)
                    if fallback:
                        current_price = fallback
                except Exception:
                    pass
            if current_price <= 0:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            state.price_history.append(current_price)
            if len(state.price_history) > 200:
                state.price_history = state.price_history[-200:]

            z_score = _calculate_zscore(state.price_history, lookback)
            if z_score is None:
                await ws_manager.send_to_user(user_id, {
                    "type": "status_update",
                    "price": current_price,
                    "z_score": 0.0,
                    "in_trade": False,
                    "entry_price": None,
                    "trade_side": None,
                    "trail_stop": None,
                    "last_signal": f"Warming up… {len(state.price_history)}/{lookback} ticks",
                    "demo_mode": is_demo,
                })
                await asyncio.sleep(POLL_INTERVAL)
                continue

            state.last_update = datetime.now(timezone.utc).isoformat()

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

            # Demo: occasionally inject a synthetic signal so UI shows live trades
            if is_demo and not state.in_trade and len(state.price_history) >= lookback:
                if random.random() < 0.12:
                    if random.random() > 0.5:
                        bullish_retest = True
                        state.bullish_levels.append(current_price)
                    else:
                        bearish_retest = True
                        state.bearish_levels.append(current_price)

            # Trailing stop update
            if state.in_trade and state.entry_price:
                if state.trade_side == "buy" and state.trail_stop_price:
                    state.trail_stop_price = max(
                        state.trail_stop_price,
                        current_price * (1 - trail_stop_pct)
                    )
                elif state.trade_side == "sell" and state.trail_stop_price:
                    state.trail_stop_price = min(
                        state.trail_stop_price,
                        current_price * (1 + trail_stop_pct)
                    )

            # Exit logic
            if state.in_trade and state.entry_price and state.current_trade_id:
                ep = state.entry_price
                sl = ep * (1 - stop_loss_pct) if state.trade_side == "buy" else ep * (1 + stop_loss_pct)
                tp = ep * (1 + take_profit_pct) if state.trade_side == "buy" else ep * (1 - take_profit_pct)

                exit_reason = None
                if state.trade_side == "buy":
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
                    if not is_demo:
                        try:
                            close_side = "sell" if state.trade_side == "buy" else "buy"
                            await client.place_market_order(symbol, close_side, "0.0001")
                        except Exception as e:
                            logger.error(f"Close order error for {user_id}: {e}")
                            await ws_manager.send_to_user(user_id, {
                                "type": "bot_error",
                                "message": f"Close order failed: {str(e)[:120]}. Trade remains open.",
                            })
                            continue  # Don't mark trade as closed if order wasn't placed

                    await _close_trade(
                        user_id, state.current_trade_id, current_price, exit_reason,
                        state.entry_price, state.trade_side, state.entry_z_score or 0, z_score
                    )
                    pnl = (
                        (current_price - state.entry_price) if state.trade_side == "buy"
                        else (state.entry_price - current_price)
                    )
                    # Persist updated demo balance to DB
                    if is_demo and hasattr(client, 'balance'):
                        async with AsyncSessionLocal() as db2:
                            await db2.execute(
                                update(User).where(User.id == user_id).values(demo_balance=client.balance)
                            )
                            await db2.commit()

                    await ws_manager.send_to_user(user_id, {
                        "type": "trade_closed",
                        "symbol": symbol,
                        "exit_price": current_price,
                        "exit_reason": exit_reason,
                        "pnl": pnl,
                        "pnl_pct": (pnl / state.entry_price) * 100,
                        "demo_mode": is_demo,
                        "demo_balance": client.balance if is_demo and hasattr(client, 'balance') else None,
                    })
                    state.in_trade = False
                    state.entry_price = None
                    state.trade_side = None
                    state.trail_stop_price = None
                    state.current_trade_id = None
                    state.entry_z_score = None

            elif not state.in_trade:
                entry_side = None
                signal = None
                if bullish_retest:
                    entry_side = "buy"
                    signal = f"Bullish retest @ ${current_price:,.2f} (Z={z_score:.2f})"
                elif bearish_retest:
                    entry_side = "sell"
                    signal = f"Bearish retest @ ${current_price:,.2f} (Z={z_score:.2f})"

                if entry_side:
                    logger.info(f"Entering {entry_side} for {user_id} @ {current_price}")
                    state.last_signal = signal
                    rh_order_id = ""
                    if not is_demo:
                        try:
                            order = await client.place_market_order(symbol, entry_side, "0.0001")
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
                            continue  # Don't record a trade that was never placed

                    trade_id = await _save_trade(user_id, {
                        "symbol": symbol,
                        "side": entry_side,
                        "quantity": "0.0001",
                        "entry_price": str(current_price),
                        "state": "open",
                        "is_demo": is_demo,
                        "rh_order_id": rh_order_id,
                        "opened_at": datetime.now(timezone.utc),
                    })
                    state.in_trade = True
                    state.entry_price = current_price
                    state.trade_side = entry_side
                    state.entry_z_score = z_score
                    state.current_trade_id = trade_id
                    state.trail_stop_price = (
                        current_price * (1 - trail_stop_pct) if entry_side == "buy"
                        else current_price * (1 + trail_stop_pct)
                    )
                    await ws_manager.send_to_user(user_id, {
                        "type": "trade_opened",
                        "symbol": symbol,
                        "side": entry_side,
                        "entry_price": current_price,
                        "z_score": z_score,
                        "signal": signal,
                        "demo_mode": is_demo,
                    })

            await ws_manager.send_to_user(user_id, {
                "type": "status_update",
                "price": current_price,
                "z_score": round(z_score, 3),
                "in_trade": state.in_trade,
                "entry_price": state.entry_price,
                "trade_side": state.trade_side,
                "trail_stop": state.trail_stop_price,
                "last_signal": state.last_signal,
                "demo_mode": is_demo,
                "demo_balance": round(client.balance, 2) if is_demo and hasattr(client, 'balance') else None,
            })

        except asyncio.CancelledError:
            logger.info(f"Bot task cancelled for user {user_id}")
            break
        except Exception as e:
            err_str = str(e)
            logger.error(f"Bot loop error for {user_id}: {err_str}")

            # 401/403 from Robinhood → switch to demo, show clear user message
            if ("401" in err_str or "403" in err_str) and "trading.robinhood.com" in err_str:
                if "401" in err_str:
                    msg = "Robinhood API key rejected (401). Paste your key again in Settings and click 'Test Connection'."
                else:
                    msg = ("Robinhood API key not authorized (403). "
                           "Make sure 'Crypto Trading' permission is enabled on your key at robinhood.com → Account → Crypto API.")
                logger.warning(f"Robinhood auth error for {user_id}: {err_str}")
                state.force_demo = True
                state.demo_mode = True
                state.key_invalid = True
                state.error_count = 0
                await ws_manager.send_to_user(user_id, {
                    "type": "key_invalid",
                    "message": msg,
                })
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
