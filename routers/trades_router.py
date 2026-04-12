from fastapi import APIRouter, Depends
from database import get_db, Trade, AsyncSession
from auth import get_current_user, User
from sqlalchemy import select, desc
import json

router = APIRouter(prefix="/api/trades", tags=["trades"])


def serialize_trade(t: Trade) -> dict:
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
        "opened_at": t.opened_at.isoformat() if t.opened_at else None,
        "closed_at": t.closed_at.isoformat() if t.closed_at else None,
        "ai": {
            "grade": t.ai_grade,
            "entry_quality": t.ai_entry_quality,
            "exit_quality": t.ai_exit_quality,
            "what_went_well": json.loads(t.ai_what_went_well) if t.ai_what_went_well else [],
            "what_went_wrong": json.loads(t.ai_what_went_wrong) if t.ai_what_went_wrong else [],
            "improvements": json.loads(t.ai_improvements) if t.ai_improvements else [],
            "confidence": t.ai_confidence,
            "analyzed": t.ai_analyzed
        } if t.ai_analyzed else None
    }


@router.get("")
async def get_trades(
    limit: int = 50,
    mode: str = "all",  # "all", "live", "demo"
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    query = select(Trade).where(Trade.user_id == current_user.id)
    if mode == "live":
        query = query.where(Trade.is_demo == False)
    elif mode == "demo":
        query = query.where(Trade.is_demo == True)
    result = await db.execute(query.order_by(desc(Trade.opened_at)).limit(limit))
    trades = result.scalars().all()
    return [serialize_trade(t) for t in trades]


@router.get("/open")
async def get_open_trades(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(Trade)
        .where(Trade.user_id == current_user.id, Trade.state == "open")
    )
    return [serialize_trade(t) for t in result.scalars().all()]


@router.get("/stats")
async def get_stats(
    mode: str = "all",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    query = select(Trade).where(
        Trade.user_id == current_user.id,
        Trade.state == "closed"
    )
    if mode == "live":
        query = query.where(Trade.is_demo == False)
    elif mode == "demo":
        query = query.where(Trade.is_demo == True)
    result = await db.execute(query)
    trades = result.scalars().all()

    if not trades:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0, "total_pnl": 0, "avg_pnl": 0, "pnl_chart": []}

    wins = [t for t in trades if (t.pnl or 0) > 0]
    total_pnl = sum(t.pnl or 0 for t in trades)
    return {
        "total": len(trades),
        "wins": len(wins),
        "losses": len(trades) - len(wins),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "total_pnl": round(total_pnl, 4),
        "avg_pnl": round(total_pnl / len(trades), 4),
        "pnl_chart": [
            {"date": t.closed_at.strftime("%m/%d") if t.closed_at else "", "pnl": round(t.pnl or 0, 4)}
            for t in sorted(trades, key=lambda x: x.closed_at or x.opened_at)[-30:]
        ]
    }
