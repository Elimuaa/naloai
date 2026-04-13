import os
import secrets
import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import select, func, update
from database import get_db, User, Trade, AsyncSession
from auth import get_current_user
from datetime import datetime, timezone

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
    """Admin overview: all users with signup date, balance, and P&L."""
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    users = result.scalars().all()

    user_data = []
    for u in users:
        # Get closed trades for P&L
        trades_result = await db.execute(
            select(
                func.count(Trade.id).label("total_trades"),
                func.sum(Trade.pnl).label("total_pnl"),
            ).where(Trade.user_id == u.id, Trade.state == "closed")
        )
        row = trades_result.one()
        total_trades = row.total_trades or 0
        total_pnl = round(row.total_pnl if row.total_pnl is not None else 0, 4)

        # Win count
        wins_result = await db.execute(
            select(func.count(Trade.id)).where(
                Trade.user_id == u.id, Trade.state == "closed", Trade.pnl > 0
            )
        )
        wins = wins_result.scalar() or 0

        # Open trades count
        open_result = await db.execute(
            select(func.count(Trade.id)).where(
                Trade.user_id == u.id, Trade.state == "open"
            )
        )
        open_trades = open_result.scalar() or 0

        # Live balance from Robinhood (if keys exist) — skip actual API call, show demo balance
        has_keys = bool(u.rh_api_key)
        demo_balance = u.demo_balance or 10000.0

        user_data.append({
            "id": u.id,
            "email": u.email,
            "signed_up": u.created_at.isoformat() if u.created_at else None,
            "has_api_keys": has_keys,
            "bot_active": u.bot_active,
            "trading_symbol": u.trading_symbol,
            "demo_balance": round(demo_balance, 2),
            "total_trades": total_trades,
            "open_trades": open_trades,
            "wins": wins,
            "losses": total_trades - wins,
            "win_rate": round(wins / total_trades * 100, 1) if total_trades > 0 else 0,
            "total_pnl": total_pnl,
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
