import asyncio
import logging
import json
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from database import AsyncSessionLocal, Trade, DailyReport, User
from post_trade_ai_learner import generate_daily_report
from sqlalchemy import select

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")


async def run_daily_reports():
    """Runs at midnight UTC for all users with trades today"""
    logger.info("Running daily report generation")
    today = datetime.now(timezone.utc).date()
    yesterday = (today - timedelta(days=1)).isoformat()

    async with AsyncSessionLocal() as db:
        users_result = await db.execute(select(User))
        users = users_result.scalars().all()

        for user in users:
            trades_result = await db.execute(
                select(Trade).where(
                    Trade.user_id == user.id,
                    Trade.state == "closed"
                )
            )
            trades = trades_result.scalars().all()

            today_trades = [
                t for t in trades
                if t.closed_at and t.closed_at.date().isoformat() == yesterday
            ]

            if not today_trades:
                continue

            trades_data = [{
                "symbol": t.symbol,
                "side": t.side,
                "pnl": t.pnl or 0,
                "pnl_pct": t.pnl_pct or 0,
                "exit_reason": t.exit_reason,
                "ai_grade": t.ai_grade
            } for t in today_trades]

            report = await generate_daily_report(user.id, trades_data)

            wins = [t for t in today_trades if (t.pnl or 0) > 0]
            daily_report = DailyReport(
                user_id=user.id,
                report_date=yesterday,
                total_trades=len(today_trades),
                wins=len(wins),
                losses=len(today_trades) - len(wins),
                total_pnl=sum(t.pnl or 0 for t in today_trades),
                win_rate=len(wins) / len(today_trades) * 100 if today_trades else 0,
                summary=report.get("summary", ""),
                top_improvement=report.get("top_improvement", ""),
                full_report_json=json.dumps(report)
            )
            db.add(daily_report)
        await db.commit()

    logger.info("Daily reports complete")


def start_scheduler():
    scheduler.add_job(run_daily_reports, "cron", hour=0, minute=5)
    scheduler.start()
    logger.info("Scheduler started")
