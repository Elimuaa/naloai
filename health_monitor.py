"""
Health monitoring system for CryptoBot.
Runs automated checks every 30 minutes to verify all subsystems are working:
- Trading execution (bot loops running, no stuck trades)
- AI learning system (Anthropic API reachable)
- API responses (all endpoints healthy)
- Live vs Demo consistency
- Database integrity
"""

import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from database import AsyncSessionLocal, User, Trade
from sqlalchemy import select, func
from bot_engine import bot_states, _bot_tasks, _risk_managers
from ws_manager import ws_manager
import notifications

logger = logging.getLogger(__name__)

# Track health history for the dashboard
_health_history: list[dict] = []
MAX_HISTORY = 48  # Keep 24 hours of 30-min checks


async def check_bot_loops() -> dict:
    """Verify all active bot loops are running and not stuck."""
    issues = []
    active_count = 0
    stuck_count = 0

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.bot_active == True))
        active_users = result.scalars().all()

    for user in active_users:
        uid = user.id
        active_count += 1

        # Check if task exists and is running
        if uid not in _bot_tasks or _bot_tasks[uid].done():
            issues.append(f"Bot task missing/dead for user {uid[:8]}. Attempting restart...")
            try:
                from bot_engine import start_bot
                await start_bot(uid)
                issues[-1] += " RESTARTED OK"
            except Exception as e:
                issues[-1] += f" RESTART FAILED: {e}"
            continue

        # Check if state is being updated (not stuck)
        state = bot_states.get(uid)
        if state and state.last_update:
            try:
                last = datetime.fromisoformat(state.last_update)
                age_minutes = (datetime.now(timezone.utc) - last).total_seconds() / 60
                # If no update in 10 minutes for demo (6s interval) or 5 minutes for live (60s interval)
                max_age = 5 if not state.demo_mode else 10
                if age_minutes > max_age:
                    stuck_count += 1
                    issues.append(
                        f"Bot for {uid[:8]} may be stuck — last update {age_minutes:.0f}m ago"
                    )
            except (ValueError, TypeError):
                pass

        # Check for excessive errors
        if state and state.error_count > 5:
            issues.append(f"Bot for {uid[:8]} has {state.error_count} errors")

    return {
        "status": "ok" if not issues else "warning",
        "active_bots": active_count,
        "stuck_bots": stuck_count,
        "issues": issues,
    }


async def check_stuck_trades() -> dict:
    """Find trades that have been open too long (potential stuck trades)."""
    issues = []
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Trade).where(Trade.state == "open")
        )
        all_open = result.scalars().all()

    stuck_trades = []
    for t in all_open:
        opened = t.opened_at.replace(tzinfo=timezone.utc) if t.opened_at and t.opened_at.tzinfo is None else t.opened_at
        if opened and opened < cutoff:
            stuck_trades.append(t)

    for t in stuck_trades:
        opened = t.opened_at.replace(tzinfo=timezone.utc) if t.opened_at and t.opened_at.tzinfo is None else t.opened_at
        age_hours = (now - opened).total_seconds() / 3600 if opened else 0
        issues.append(
            f"Trade {t.id[:8]} ({t.symbol} {t.side}) open for {age_hours:.0f}h"
        )

    return {
        "status": "ok" if not issues else "warning",
        "stuck_trades": len(stuck_trades),
        "issues": issues,
    }


async def check_duplicate_open_trades() -> dict:
    """Verify no user has multiple open trades (data integrity)."""
    issues = []

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Trade.user_id, func.count(Trade.id).label("cnt"))
            .where(Trade.state == "open")
            .group_by(Trade.user_id)
        )
        counts = result.all()

    for user_id, count in counts:
        if count > 1:
            issues.append(f"User {user_id[:8]} has {count} open trades (expected max 1)")

    return {
        "status": "ok" if not issues else "critical",
        "issues": issues,
    }


async def check_ai_system() -> dict:
    """Check if the AI learning system (Anthropic API) is reachable."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"status": "unconfigured", "issues": ["ANTHROPIC_API_KEY not set"]}

    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                }
            )
            # 405 Method Not Allowed = API is reachable (GET not allowed, but server responded)
            # 401 = bad key
            if r.status_code in (200, 405):
                return {"status": "ok", "issues": []}
            elif r.status_code == 401:
                return {"status": "error", "issues": ["Anthropic API key is invalid (401)"]}
            else:
                return {"status": "warning", "issues": [f"Anthropic API returned {r.status_code}"]}
    except Exception as e:
        return {"status": "error", "issues": [f"Cannot reach Anthropic API: {str(e)[:100]}"]}


async def check_database() -> dict:
    """Verify database is accessible and tables exist."""
    try:
        async with AsyncSessionLocal() as db:
            # Simple query to test connectivity
            result = await db.execute(select(func.count(User.id)))
            user_count = result.scalar()
            result2 = await db.execute(select(func.count(Trade.id)))
            trade_count = result2.scalar()
        return {
            "status": "ok",
            "users": user_count,
            "trades": trade_count,
            "issues": [],
        }
    except Exception as e:
        return {"status": "critical", "issues": [f"Database error: {str(e)[:100]}"]}


async def check_live_demo_consistency() -> dict:
    """Verify live/demo state is consistent — no trades marked wrong."""
    issues = []

    async with AsyncSessionLocal() as db:
        # Check for users with API keys who have recent demo trades (might indicate fallback)
        result = await db.execute(
            select(User).where(User.rh_api_key != None, User.bot_active == True)
        )
        live_users = result.scalars().all()

        for user in live_users:
            uid = user.id
            state = bot_states.get(uid)
            if state and state.demo_mode and not state.force_demo:
                issues.append(
                    f"User {uid[:8]} has API keys but is running in demo mode (possible key issue)"
                )
            if state and state.key_invalid:
                issues.append(
                    f"User {uid[:8]} has invalid API key flagged"
                )

    return {
        "status": "ok" if not issues else "warning",
        "issues": issues,
    }


async def check_websocket_connections() -> dict:
    """Check WebSocket connection health."""
    connected = len(ws_manager.connections) if hasattr(ws_manager, 'connections') else 0
    return {
        "status": "ok",
        "connected_users": connected,
        "issues": [],
    }


async def run_full_health_check() -> dict:
    """Run all health checks and return comprehensive status."""
    start = datetime.now(timezone.utc)

    checks = await asyncio.gather(
        check_bot_loops(),
        check_stuck_trades(),
        check_duplicate_open_trades(),
        check_ai_system(),
        check_database(),
        check_live_demo_consistency(),
        check_websocket_connections(),
        return_exceptions=True,
    )

    check_names = [
        "bot_loops", "stuck_trades", "duplicate_trades",
        "ai_system", "database", "live_demo_consistency", "websocket",
    ]

    results = {}
    all_issues = []
    overall_status = "healthy"

    for name, result in zip(check_names, checks):
        if isinstance(result, Exception):
            results[name] = {"status": "error", "issues": [str(result)]}
            all_issues.append(f"{name}: {str(result)[:80]}")
            overall_status = "critical"
        else:
            results[name] = result
            if result.get("status") == "critical":
                overall_status = "critical"
            elif result.get("status") in ("error", "warning") and overall_status != "critical":
                overall_status = "degraded"
            all_issues.extend(result.get("issues", []))

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()

    report = {
        "status": overall_status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(elapsed, 2),
        "checks": results,
        "total_issues": len(all_issues),
        "issues_summary": all_issues[:20],  # Cap at 20 issues
    }

    # Store in history
    _health_history.append(report)
    if len(_health_history) > MAX_HISTORY:
        _health_history.pop(0)

    # Send alert via Telegram if critical
    if overall_status == "critical" and notifications.is_configured():
        alert_text = (
            "\u26a0\ufe0f <b>CryptoBot Health Alert: CRITICAL</b>\n\n"
            + "\n".join(f"- {i}" for i in all_issues[:5])
        )
        asyncio.create_task(notifications.send_message(alert_text))

    # Broadcast health status to all connected WS clients
    await ws_manager.broadcast({
        "type": "health_check",
        "status": overall_status,
        "total_issues": len(all_issues),
        "timestamp": report["timestamp"],
    })

    logger.info(
        f"Health check: {overall_status} | {len(all_issues)} issues | {elapsed:.1f}s"
    )

    return report


def get_health_history() -> list[dict]:
    """Return recent health check history."""
    return _health_history[-10:]
