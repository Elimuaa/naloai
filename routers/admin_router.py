import os
import secrets
import logging
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import select, func, update, Float
from database import get_db, User, Trade, AsyncSession
from auth import get_current_user
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])
security = HTTPBasic()

ADMIN_USER = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASSWORD", "")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").lower().strip()


def verify_admin_basic(credentials: HTTPBasicCredentials = Depends(security)):
    if not ADMIN_PASS:
        raise HTTPException(503, "Admin password not configured. Set ADMIN_PASSWORD in .env")
    user_ok = secrets.compare_digest(credentials.username.encode(), ADMIN_USER.encode())
    pass_ok = secrets.compare_digest(credentials.password.encode(), ADMIN_PASS.encode())
    if not (user_ok and pass_ok):
        raise HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


async def verify_admin_jwt(current_user: User = Depends(get_current_user)):
    if not ADMIN_EMAIL or current_user.email.lower().strip() != ADMIN_EMAIL:
        raise HTTPException(403, "Admin access required")
    return current_user


@router.get("/users")
async def admin_users(
    admin: User = Depends(verify_admin_jwt),
    db: AsyncSession = Depends(get_db)
):
    """Admin overview: all users with signup date, balance, P&L, and period breakdowns."""
    now = datetime.now(timezone.utc)
    today_start  = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago     = now - timedelta(days=7)
    month_ago    = now - timedelta(days=30)

    result = await db.execute(select(User).order_by(User.created_at.desc()))
    users = result.scalars().all()

    async def period_pnl(user_id: str, since: datetime | None) -> float:
        q = select(func.sum(Trade.pnl)).where(
            Trade.user_id == user_id, Trade.state == "closed"
        )
        if since:
            q = q.where(Trade.closed_at >= since)
        val = (await db.execute(q)).scalar()
        return round(val or 0.0, 2)

    user_data = []
    for u in users:
        # All-time aggregates
        trades_result = await db.execute(
            select(
                func.count(Trade.id).label("total_trades"),
                func.sum(Trade.pnl).label("total_pnl"),
            ).where(Trade.user_id == u.id, Trade.state == "closed")
        )
        row = trades_result.one()
        total_trades = row.total_trades or 0
        total_pnl = round(row.total_pnl if row.total_pnl is not None else 0, 4)

        wins_result = await db.execute(
            select(func.count(Trade.id)).where(
                Trade.user_id == u.id, Trade.state == "closed", Trade.pnl > 0
            )
        )
        wins = wins_result.scalar() or 0

        open_result = await db.execute(
            select(
                func.count(Trade.id).label("cnt"),
                func.sum(Trade.quantity_value * func.cast(Trade.entry_price, Float)).label("position_value")
            ).where(Trade.user_id == u.id, Trade.state == "open")
        )
        open_row = open_result.one()
        open_trades = open_row.cnt or 0
        open_position_value = round(open_row.position_value or 0.0, 2)

        # Period P&L breakdowns
        daily_pnl   = await period_pnl(u.id, today_start)
        weekly_pnl  = await period_pnl(u.id, week_ago)
        monthly_pnl = await period_pnl(u.id, month_ago)

        has_keys = bool(u.rh_api_key)
        cash_balance = round(u.demo_balance or 10000.0, 2)
        # Total equity = cash on hand + value of open positions (at entry price)
        demo_balance = round(cash_balance + open_position_value, 2)

        user_data.append({
            "id": u.id,
            "email": u.email,
            "signed_up": u.created_at.isoformat() if u.created_at else None,
            "has_api_keys": has_keys,
            "bot_active": u.bot_active,
            "trading_symbol": u.trading_symbol,
            "demo_balance": demo_balance,
            "cash_balance": cash_balance,
            "open_position_value": open_position_value,
            "total_trades": total_trades,
            "open_trades": open_trades,
            "wins": wins,
            "losses": total_trades - wins,
            "win_rate": round(wins / total_trades * 100, 1) if total_trades > 0 else 0,
            "total_pnl": total_pnl,
            "daily_pnl": daily_pnl,
            "weekly_pnl": weekly_pnl,
            "monthly_pnl": monthly_pnl,
            "is_premium": getattr(u, 'is_premium', False),
            "premium_since": u.premium_since.isoformat() if getattr(u, 'premium_since', None) else None,
            "calibration_count": getattr(u, 'calibration_count', 0),
        })

    return {
        "total_users": len(users),
        "active_bots": sum(1 for u in user_data if u["bot_active"]),
        "users_with_keys": sum(1 for u in user_data if u["has_api_keys"]),
        "users": user_data,
    }


@router.get("/summary")
async def admin_summary(
    admin: User = Depends(verify_admin_jwt),
    db: AsyncSession = Depends(get_db)
):
    """Quick summary stats."""
    user_count = (await db.execute(select(func.count(User.id)))).scalar()
    trade_count = (await db.execute(select(func.count(Trade.id)))).scalar()
    closed_count = (await db.execute(
        select(func.count(Trade.id)).where(Trade.state == "closed")
    )).scalar()
    total_pnl = (await db.execute(
        select(func.sum(Trade.pnl)).where(Trade.state == "closed")
    )).scalar() or 0
    active_bots = (await db.execute(
        select(func.count(User.id)).where(User.bot_active == True)
    )).scalar()
    premium_users = (await db.execute(
        select(func.count(User.id)).where(User.is_premium == True)
    )).scalar()

    return {
        "total_users": user_count,
        "active_bots": active_bots,
        "premium_users": premium_users,
        "total_trades": trade_count,
        "closed_trades": closed_count,
        "platform_pnl": round(total_pnl, 4),
        "monthly_premium_revenue": (premium_users or 0) * 199,
    }


async def _btc_hodl_return(since_ts: float, symbol: str = "BTC-USD") -> float:
    """
    Fetch BTC return (%) from `since_ts` (unix) to now using Coinbase candles.
    Uses 1h granularity — good enough for daily/weekly comparison.
    Returns 0.0 on any error so the dashboard never breaks.
    """
    try:
        now_ts = int(__import__('time').time())
        gran = 3600  # 1h candles
        url = f"https://api.exchange.coinbase.com/products/{symbol}/candles"
        params = {
            "start": datetime.fromtimestamp(since_ts, timezone.utc).isoformat(),
            "end":   datetime.fromtimestamp(min(since_ts + gran * 2, now_ts), timezone.utc).isoformat(),
            "granularity": gran,
        }
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(url, params=params)
        candles = r.json() if r.status_code == 200 else []
        if not candles:
            return 0.0
        open_price = float(candles[-1][3])  # oldest candle open

        # Current price — latest 1h candle
        params2 = {
            "start": datetime.fromtimestamp(now_ts - gran * 2, timezone.utc).isoformat(),
            "end":   datetime.fromtimestamp(now_ts, timezone.utc).isoformat(),
            "granularity": gran,
        }
        async with httpx.AsyncClient(timeout=5) as c:
            r2 = await c.get(url, params=params2)
        candles2 = r2.json() if r2.status_code == 200 else []
        if not candles2 or open_price == 0:
            return 0.0
        current_price = float(candles2[0][4])  # newest candle close
        return round((current_price - open_price) / open_price * 100, 2)
    except Exception:
        return 0.0


@router.get("/today-stats")
async def admin_today_stats(
    admin: User = Depends(verify_admin_jwt),
    db: AsyncSession = Depends(get_db)
):
    """Real-time platform performance: trades + P&L today, 7d, all-time + BTC HODL benchmark."""
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)

    async def stats_for(since: datetime | None):
        """Aggregate trades closed since `since`. None = all time."""
        q_count = select(func.count(Trade.id)).where(Trade.state == "closed")
        q_pnl = select(func.sum(Trade.pnl)).where(Trade.state == "closed")
        q_wins = select(func.count(Trade.id)).where(Trade.state == "closed", Trade.pnl > 0)
        q_losses = select(func.count(Trade.id)).where(Trade.state == "closed", Trade.pnl <= 0)
        q_partial = select(func.sum(Trade.partial_pnl)).where(Trade.state == "closed")
        q_demo_pnl = select(func.sum(Trade.pnl)).where(
            Trade.state == "closed", Trade.is_demo == True
        )
        q_live_pnl = select(func.sum(Trade.pnl)).where(
            Trade.state == "closed", Trade.is_demo == False
        )
        if since is not None:
            q_count = q_count.where(Trade.closed_at >= since)
            q_pnl = q_pnl.where(Trade.closed_at >= since)
            q_wins = q_wins.where(Trade.closed_at >= since)
            q_losses = q_losses.where(Trade.closed_at >= since)
            q_partial = q_partial.where(Trade.closed_at >= since)
            q_demo_pnl = q_demo_pnl.where(Trade.closed_at >= since)
            q_live_pnl = q_live_pnl.where(Trade.closed_at >= since)

        cnt = (await db.execute(q_count)).scalar() or 0
        pnl = (await db.execute(q_pnl)).scalar() or 0.0
        wins = (await db.execute(q_wins)).scalar() or 0
        losses = (await db.execute(q_losses)).scalar() or 0
        partial = (await db.execute(q_partial)).scalar() or 0.0
        demo_pnl = (await db.execute(q_demo_pnl)).scalar() or 0.0
        live_pnl = (await db.execute(q_live_pnl)).scalar() or 0.0
        return {
            "trades": cnt,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round((wins / cnt * 100) if cnt else 0, 1),
            "total_pnl": round(pnl, 2),
            "demo_pnl": round(demo_pnl, 2),
            "live_pnl": round(live_pnl, 2),
            "partial_pnl_locked": round(partial, 2),
            "avg_pnl_per_trade": round(pnl / cnt, 2) if cnt else 0.0,
        }

    today = await stats_for(today_start)
    week = await stats_for(week_ago)
    all_time = await stats_for(None)

    # ── BTC HODL benchmark for each period ──
    # Fetch in parallel — don't block if Coinbase is slow
    import asyncio as _asyncio
    # Find oldest trade for all-time HODL start
    oldest_trade = (await db.execute(
        select(Trade.opened_at).where(Trade.state == "closed").order_by(Trade.opened_at.asc()).limit(1)
    )).scalar()
    alltime_since = oldest_trade.timestamp() if oldest_trade else (now - timedelta(days=365)).timestamp()

    hodl_today, hodl_week, hodl_alltime = await _asyncio.gather(
        _btc_hodl_return(today_start.timestamp()),
        _btc_hodl_return(week_ago.timestamp()),
        _btc_hodl_return(alltime_since),
        return_exceptions=True,
    )
    hodl_today   = hodl_today   if isinstance(hodl_today,   float) else 0.0
    hodl_week    = hodl_week    if isinstance(hodl_week,    float) else 0.0
    hodl_alltime = hodl_alltime if isinstance(hodl_alltime, float) else 0.0

    # Currently open positions across the platform
    open_count = (await db.execute(
        select(func.count(Trade.id)).where(Trade.state == "open")
    )).scalar() or 0

    # Active bots
    active_bots = (await db.execute(
        select(func.count(User.id)).where(User.bot_active == True)
    )).scalar() or 0

    # Trades opened today (entries — different from closes)
    opened_today = (await db.execute(
        select(func.count(Trade.id)).where(Trade.opened_at >= today_start)
    )).scalar() or 0

    return {
        "as_of_utc": now.isoformat(),
        "active_bots": active_bots,
        "open_positions": open_count,
        "trades_opened_today": opened_today,
        "today": {**today, "btc_hodl_pct": hodl_today},
        "last_7_days": {**week, "btc_hodl_pct": hodl_week},
        "all_time": {**all_time, "btc_hodl_pct": hodl_alltime},
    }


@router.post("/users/{user_id}/premium")
async def admin_toggle_premium(
    user_id: str,
    admin: User = Depends(verify_admin_jwt),
    db: AsyncSession = Depends(get_db)
):
    """Admin: toggle premium status for a user."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")
    new_status = not getattr(user, 'is_premium', False)
    values = {"is_premium": new_status}
    if new_status:
        values["premium_since"] = datetime.now(timezone.utc)
    await db.execute(update(User).where(User.id == user_id).values(**values))
    await db.commit()
    return {"user_id": user_id, "is_premium": new_status}


@router.post("/reset-all")
async def admin_reset_all(
    admin: User = Depends(verify_admin_jwt),
    db: AsyncSession = Depends(get_db)
):
    """Admin: reset all users' demo balances, trades, and strategy params."""
    from bot_engine import stop_bot, start_bot, _bot_tasks

    # Stop all bots
    result = await db.execute(select(User))
    users = result.scalars().all()
    for user in users:
        if user.bot_active:
            try:
                await stop_bot(user.id)
            except Exception as e:
                logger.error(f"admin reset: stop_bot({user.id}) failed: {e}")

    # Delete all trades
    await db.execute(Trade.__table__.delete())

    # Reset all users with profit-optimized strategy parameters
    await db.execute(
        update(User).values(
            demo_balance=10000.0,
            entry_z=1.3,
            lookback="20",
            stop_loss_pct=0.025,
            take_profit_pct=0.05,
            trail_stop_pct=0.015,
            use_rsi_filter=True,
            use_ema_filter=False,
            use_adx_filter=True,
            use_bbands_filter=True,
            use_macd_filter=False,
            max_drawdown_pct=8.0,
            max_stops_before_pause=3,
            cooldown_ticks=5,
            risk_per_trade_pct=2.0,
            max_exposure_pct=40.0,
            position_size_mode="dynamic",
            bot_active=False,
        )
    )
    await db.commit()

    # Restart all bots in demo mode
    result2 = await db.execute(select(User))
    users2 = result2.scalars().all()
    for user in users2:
        await db.execute(update(User).where(User.id == user.id).values(bot_active=True))
        await db.commit()
        await start_bot(user.id)

    return {
        "message": "All users reset",
        "users_reset": len(users),
        "demo_balance": 10000.0,
        "entry_z": 1.5,
    }


@router.post("/apply-optimal-settings")
async def admin_apply_optimal_settings(
    admin: User = Depends(verify_admin_jwt),
    db: AsyncSession = Depends(get_db)
):
    """Admin: apply profit-optimized strategy parameters to all users without resetting balances or trades."""
    OPTIMAL = dict(
        entry_z=1.3,            # slightly lower threshold → more trade opportunities
        lookback="20",
        stop_loss_pct=0.025,    # 2.5% SL
        take_profit_pct=0.05,   # 5.0% TP → 2:1 R/R
        trail_stop_pct=0.015,   # 1.5% trail
        use_rsi_filter=True,
        use_ema_filter=False,
        use_adx_filter=True,
        use_bbands_filter=True,
        use_macd_filter=False,
        max_drawdown_pct=8.0,
        max_stops_before_pause=3,
        cooldown_ticks=5,           # 5 ticks cooldown after loss (down from 10)
        risk_per_trade_pct=2.0,     # risk $200 per trade on $10k → $200/win
        max_exposure_pct=40.0,      # 40% max position → $4k on $10k account
        position_size_mode="dynamic",
    )
    await db.execute(update(User).values(**OPTIMAL))
    await db.commit()

    # Push changes to in-memory risk managers immediately (no restart required)
    from bot_engine import _risk_managers
    for rm in _risk_managers.values():
        rm.max_drawdown_pct = OPTIMAL["max_drawdown_pct"]
        rm.max_stops_before_pause = OPTIMAL["max_stops_before_pause"]
        rm.cooldown_ticks = OPTIMAL["cooldown_ticks"]
        rm.risk_per_trade_pct = OPTIMAL["risk_per_trade_pct"]
        rm.max_exposure_pct = OPTIMAL["max_exposure_pct"]

    return {
        "message": "Profit-optimized settings applied to all users",
        "params": {
            "entry_z": 1.3, "sl": "2.5%", "tp": "5.0%", "trail": "1.5%",
            "rr_ratio": "2:1", "risk_per_trade": "2%", "max_exposure": "40%",
            "expected_win_value": "$200 at $10k account",
            "expected_loss_value": "$100 at $10k account",
            "break_even_win_rate": "34%",
            "daily_target": "$200 min or 2.5% of balance (compounds, never caps profit)",
            "filters": "RSI+ADX+BBands ON, EMA OFF",
        },
    }


@router.post("/users/{user_id}/reset")
async def admin_reset_user(
    user_id: str,
    admin: User = Depends(verify_admin_jwt),
    db: AsyncSession = Depends(get_db)
):
    """Admin: reset a single user's balance, trades, and params."""
    from bot_engine import stop_bot, start_bot

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")

    # Stop bot
    if user.bot_active:
        try:
            await stop_bot(user_id)
        except Exception as e:
            logger.error(f"admin delete-user: stop_bot({user_id}) failed: {e}")

    # Delete user's trades
    await db.execute(Trade.__table__.delete().where(Trade.user_id == user_id))

    # Reset user settings
    await db.execute(
        update(User).where(User.id == user_id).values(
            demo_balance=10000.0,
            entry_z=1.5,
            bot_active=True,
        )
    )
    await db.commit()

    # Restart bot
    await start_bot(user_id)

    return {"user_id": user_id, "message": "User reset", "demo_balance": 10000.0}
