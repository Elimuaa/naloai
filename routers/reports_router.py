from fastapi import APIRouter, Depends
from database import get_db, DailyReport, AsyncSession
from auth import get_current_user, User
from sqlalchemy import select, desc
import json
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/reports", tags=["reports"])


def _safe_json_report(data: str | None) -> dict:
    if not data or not data.strip():
        return {}
    try:
        return json.loads(data)
    except (json.JSONDecodeError, TypeError):
        logger.warning(f"Failed to parse report JSON: {data[:100]}")
        return {}


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
        "full_report": _safe_json_report(r.full_report_json)
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
        "full_report": _safe_json_report(r.full_report_json)
    }
