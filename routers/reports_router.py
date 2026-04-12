from fastapi import APIRouter, Depends
from database import get_db, DailyReport, AsyncSession
from auth import get_current_user, User
from sqlalchemy import select, desc
import json

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("")
async def get_reports(
    limit: int = 10,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(DailyReport)
        .where(DailyReport.user_id == current_user.id)
        .order_by(desc(DailyReport.created_at))
        .limit(limit)
    )
    reports = result.scalars().all()
    return [{
        "id": r.id,
        "report_date": r.report_date,
        "total_trades": r.total_trades,
        "wins": r.wins,
        "losses": r.losses,
        "total_pnl": r.total_pnl,
        "win_rate": r.win_rate,
        "summary": r.summary,
        "top_improvement": r.top_improvement,
        "full_report": json.loads(r.full_report_json) if r.full_report_json else {}
    } for r in reports]


@router.get("/latest")
async def get_latest_report(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(DailyReport)
        .where(DailyReport.user_id == current_user.id)
        .order_by(desc(DailyReport.created_at))
        .limit(1)
    )
    r = result.scalar_one_or_none()
    if not r:
        return None
    return {
        "report_date": r.report_date,
        "total_trades": r.total_trades,
        "wins": r.wins,
        "losses": r.losses,
        "total_pnl": r.total_pnl,
        "win_rate": r.win_rate,
        "summary": r.summary,
        "top_improvement": r.top_improvement,
        "full_report": json.loads(r.full_report_json) if r.full_report_json else {}
    }
