import logging
from fastapi import APIRouter, Depends
from database import get_db, Trade, AsyncSession
from auth import get_current_user, User
from sqlalchemy import select, desc
import json

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/trades", tags=["trades"])

# These exit reasons are system/maintenance events — not real trade decisions.
# Excluded from win/loss stats so they don't distort the user's performance metrics.
SYSTEM_EXIT_REASONS = {"graceful_shutdown", "corruption_recovery", "data_corruption_scrubbed"}


def _safe_json(data: str | None, default=None):
    """Safely parse JSON, returning default on failure."""
    if not data:
        return default if default is not None else []
    try:
        return json.loads(data)
    except (json.JSONDecodeError, TypeError):
        logger.warning(f"Failed to parse JSON: {data[:100] if data else 'None'}")
        return default if default is not None else []


def serialize_trade(t: Trade) -> dict:
    is_system = t.exit_reason in SYSTEM_EXIT_REASONS if t.exit_reason else False
    return {
        "id": t.id,
        "symbol": t.symbol,
        "side": t.side,
        "quantity": t.quantity,
        "entry_price": t.entry_price,
        "exit_price": t.exit_price,
        "pnl": t.pnl,
        "pnl_pct": t.pnl_pct,
        "state": t.state,
        "is_demo": getattr(t, 'is_demo', True),
        "exit_reason": t.exit_reason,
        "is_system_close": is_system,   # flag so UI can grey these out
        "quantity_value": getattr(t, 'quantity_value', None),
        "opened_at": t.opened_at.isoformat() if t.opened_at else None,
        "closed_at": t.closed_at.isoformat() if t.closed_at else None,
        "ai": {
            "grade": t.ai_grade,
            "entry_quality": t.ai_entry_quality,
            "exit_quality": t.ai_exit_quality,
            "what_went_well": _safe_json(t.ai_what_went_well),
            "what_went_wrong": _safe_json(t.ai_what_went_wrong),
            "improvements": _safe_json(t.ai_improvements),
            "confidence": t.ai_confidence,
            "analyzed": t.ai_analyzed
        } if t.ai_analyzed else None
    }


CAPITAL_SYMS = ("GOLD", "US100")


def _apply_filters(query, mode: str, broker: str):
    """Apply mode (all/live/demo) and broker (all/robinhood/capital) filters."""
    mode = mode.lower().strip()
    if mode == "live":
        query = query.where(Trade.is_demo == False)
    elif mode == "demo":
        query = query.where(Trade.is_demo == True)
    broker = broker.lower().strip()
    if broker == "capital":
        query = query.where(Trade.symbol.in_(CAPITAL_SYMS))
    elif broker == "robinhood":
        query = query.where(Trade.symbol.not_in(CAPITAL_SYMS))
    return query


@router.get("")
async def get_trades(
    limit: int = 50,
    mode: str = "all",
    broker: str = "all",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    query = select(Trade).where(Trade.user_id == current_user.id)
    query = _apply_filters(query, mode, broker)
    result = await db.execute(query.order_by(desc(Trade.opened_at)).limit(limit))
    return [serialize_trade(t) for t in result.scalars().all()]


@router.get("/open")
async def get_open_trades(
    broker: str = "all",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    query = select(Trade).where(Trade.user_id == current_user.id, Trade.state == "open")
    query = _apply_filters(query, "all", broker)
    result = await db.execute(query)
    return [serialize_trade(t) for t in result.scalars().all()]


@router.get("/stats")
async def get_stats(
    mode: str = "all",
    broker: str = "all",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    query = select(Trade).where(
        Trade.user_id == current_user.id,
        Trade.state == "closed"
    )
    query = _apply_filters(query, mode, broker)
    all_trades = (await db.execute(query)).scalars().all()

    if not all_trades:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0, "total_pnl": 0, "avg_pnl": 0, "pnl_chart": []}

    # Exclude system/maintenance closes from win-rate and counts —
    # graceful_shutdown and corruption_recovery are server events, not decisions.
    real_trades = [t for t in all_trades if t.exit_reason not in SYSTEM_EXIT_REASONS]
    system_count = len(all_trades) - len(real_trades)

    wins   = [t for t in real_trades if t.pnl is not None and t.pnl > 0]
    losses = [t for t in real_trades if t.pnl is None  or  t.pnl <= 0]

    # Total P&L includes everything (system closes may have small real P&L)
    total_pnl = sum(t.pnl if t.pnl is not None else 0 for t in all_trades)

    return {
        "total": len(real_trades),            # real trading decisions only
        "system_closes": system_count,         # restart/recovery artifacts
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(real_trades) * 100, 1) if real_trades else 0,
        "total_pnl": round(total_pnl, 4),
        "avg_pnl": round(total_pnl / len(real_trades), 4) if real_trades else 0,
        "pnl_chart": [
            {
                "date": t.closed_at.strftime("%m/%d") if t.closed_at else "",
                "pnl": round(t.pnl if t.pnl is not None else 0, 4)
            }
            for t in sorted(all_trades, key=lambda x: x.closed_at or x.opened_at)[-30:]
        ]
    }
