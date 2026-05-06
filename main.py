import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dotenv import load_dotenv
load_dotenv()  # Load .env before anything else reads env vars
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from database import init_db, AsyncSessionLocal, User
from sqlalchemy import select
from auth import get_current_user_ws
from ws_manager import ws_manager
from bot_engine import start_bot, _bot_tasks, graceful_shutdown_close_all_demo_positions
from scheduler import start_scheduler
from routers import auth_router, bot_router, trades_router, reports_router, market_router, admin_router, stripe_router
from health_monitor import run_full_health_check, get_health_history

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from robinhood import sync_clock_offset
    await sync_clock_offset()  # Correct for system clock drift before any Robinhood calls
    await init_db()
    start_scheduler()

    # ── Startup migration: apply profit-optimised settings to all users ──────
    # Runs on every deploy. Upgrades any user still on old conservative values.
    # Target: $200-300/day on a $10k account via 40% max exposure + 2% risk/trade.
    from sqlalchemy import update as _up
    # ── SCALP MODE: $5–20 z-revert wins + occasional TP at 2% ──────────────────
    # Math: 0.5% SL, 2% TP = 4:1 R/R on TP hits.
    # Z-revert fires when unrealised PnL >= $15 → avg $15-35 win.
    # With 60% exposure on $10k = $6k deployed → 0.079 BTC position:
    #   SL loss  = 0.079 × $76k × 0.005 = $30
    #   TP win   = 0.079 × $76k × 0.020 = $120
    #   Z-revert = $15–40 (fires after meaningful move, not $0.24 noise)
    # Target: 15 z-revert wins × $20 + 2 TP hits × $120 − 4 SL × $30 = $420/day
    PROFIT_PARAMS = dict(
        entry_z=1.1,                # lower threshold → more entries (was 1.3)
        stop_loss_pct=0.005,        # 0.5% SL — tight enough to protect, wide enough for BTC noise
        take_profit_pct=0.020,      # 2.0% TP — reachable intraday (was 5% — rarely hit)
        trail_stop_pct=0.005,       # 0.5% trail — locks in wins as price moves (was 2%)
        use_rsi_filter=True,
        use_ema_filter=False,
        use_adx_filter=True,
        use_bbands_filter=True,
        use_macd_filter=False,
        max_drawdown_pct=8.0,
        max_stops_before_pause=5,   # allow 5 stops before pause (more room at tighter SL)
        cooldown_ticks=2,           # re-enter fast — scalp needs quick reloading
        risk_per_trade_pct=2.5,     # 2.5% risk per trade (was 2%)
        max_exposure_pct=60.0,      # 60% max exposure → bigger positions (was 40%)
        position_size_mode="dynamic",
    )
    async with AsyncSessionLocal() as db:
        # Only apply defaults to users who have NEVER been AI-calibrated.
        # calibration_count == 0 means the AI optimizer hasn't tuned them yet.
        # This preserves hard-won calibrated parameters across deploys — previously
        # every redeploy silently wiped AI-calibrated settings for every user.
        result = await db.execute(
            _up(User)
            .where(User.calibration_count == 0)
            .values(**PROFIT_PARAMS)
        )
        affected = result.rowcount
        await db.commit()
    logger.info(
        f"Startup: SCALP MODE applied to {affected} uncalibrated users "
        "(calibrated users preserved) — "
        "SL=0.5%, TP=2% (4:1 R/R), trail=0.5%, z-revert min=$15, "
        "entry_z=1.1, risk=2.5%, exposure=60%, target=$200+/day"
    )

    # ── One-shot corruption recovery ────────────────────────────────────────
    # The previous mock-client short bug (d713744) credited free proceeds on
    # every sell-to-open, compounding into quintillion-dollar demo balances and
    # quintillion-coin "open" trades. Detect and reset any account where the
    # numbers are obviously impossible: balance > $1M, or an open trade with
    # absurd quantity / size. Force-close those trades, reset balance to $10k,
    # and wipe risk_state so position sizing starts clean.
    from database import Trade as _Trade, RiskState as _RiskState
    from sqlalchemy import or_ as _or
    CORRUPT_BALANCE_THRESHOLD = 1_000_000.0  # $1M — well above any plausible demo gain
    CORRUPT_QTY_THRESHOLD = 10_000.0          # >10k units of any symbol is impossible from $10k seed
    async with AsyncSessionLocal() as db:
        # 1) Users with corrupt demo balance
        corrupt_users_q = await db.execute(
            select(User).where(User.demo_balance > CORRUPT_BALANCE_THRESHOLD)
        )
        corrupt_users = list(corrupt_users_q.scalars().all())

        # 2) Users with absurd-quantity open demo trades (catch corruption that
        #    didn't yet reflect in demo_balance — e.g. mid-trade snapshot)
        bad_trade_users_q = await db.execute(
            select(_Trade.user_id).where(
                _Trade.state == "open",
                _Trade.is_demo == True,
                _Trade.quantity_value > CORRUPT_QTY_THRESHOLD,
            ).distinct()
        )
        bad_trade_user_ids = {row[0] for row in bad_trade_users_q.all()}
        if bad_trade_user_ids:
            extra_q = await db.execute(select(User).where(User.id.in_(bad_trade_user_ids)))
            existing_ids = {u.id for u in corrupt_users}
            for u in extra_q.scalars().all():
                if u.id not in existing_ids:
                    corrupt_users.append(u)

        for cu in corrupt_users:
            # Force-close all open trades (demo or live) for this user — any
            # leftover open trades with corrupt quantities will crash the loop.
            await db.execute(
                _Trade.__table__.update()
                .where(_Trade.user_id == cu.id, _Trade.state == "open")
                .values(state="closed", exit_reason="corruption_recovery", pnl=0.0, pnl_pct=0.0)
            )
            # Reset balance + bot flag so user has a clean slate.
            await db.execute(
                _up(User).where(User.id == cu.id).values(
                    demo_balance=10000.0,
                    bot_active=False,  # require user to manually restart so they see it
                )
            )
            # Wipe risk-state snapshot so daily P&L / cooldown start fresh.
            await db.execute(
                _RiskState.__table__.delete().where(_RiskState.user_id == cu.id)
            )
            logger.warning(
                f"CORRUPTION RECOVERY: user {cu.id[:8]} reset — "
                f"old_balance=${cu.demo_balance:,.2f}, bot stopped, balance=$10000.00"
            )
        if corrupt_users:
            await db.commit()
            logger.warning(f"Reset {len(corrupt_users)} corrupted demo accounts")

        # 3) Scrub absurd historical P&L values on closed trades. The corruption
        #    bug stamped phantom-trillion P&L into Trade.pnl rows that are now
        #    polluting platform-wide aggregates (all-time / 7d / 30d).
        #    Any single trade with |pnl| > $100k on a $10k demo seed is
        #    definitionally corrupt — zero those out (preserve trade record for
        #    audit, but neutralize the fake numbers in aggregates).
        from sqlalchemy import or_ as __or
        ABSURD_PNL_THRESHOLD = 100_000.0
        scrub = await db.execute(
            _Trade.__table__.update()
            .where(__or(
                _Trade.pnl > ABSURD_PNL_THRESHOLD,
                _Trade.pnl < -ABSURD_PNL_THRESHOLD,
            ))
            .values(pnl=0.0, pnl_pct=0.0, partial_pnl=0.0)
        )
        if scrub.rowcount:
            await db.commit()
            logger.warning(
                f"Scrubbed {scrub.rowcount} corrupted historical trade P&L rows "
                f"(|pnl| > ${ABSURD_PNL_THRESHOLD:,.0f})"
            )

        # 4) Daily reports may have already aggregated the corrupt P&L — wipe
        #    daily_reports rows so they regenerate from clean data on next run.
        from database import DailyReport as _DailyReport
        dr_scrub = await db.execute(
            _DailyReport.__table__.update()
            .where(__or(
                _DailyReport.total_pnl > ABSURD_PNL_THRESHOLD,
                _DailyReport.total_pnl < -ABSURD_PNL_THRESHOLD,
            ))
            .values(total_pnl=0.0)
        )
        if dr_scrub.rowcount:
            await db.commit()
            logger.warning(f"Scrubbed {dr_scrub.rowcount} corrupted daily_reports rows")

        # 5) Operator-requested clean slate: reset *every* user's demo balance
        #    to $10k so all accounts start from the same baseline post-recovery.
        #    Idempotent — already-$10k users are unchanged.
        reset_all = await db.execute(
            _up(User).where(User.demo_balance != 10000.0).values(demo_balance=10000.0)
        )
        if reset_all.rowcount:
            await db.commit()
            logger.warning(f"Reset {reset_all.rowcount} users to $10,000 demo balance (clean slate)")

    # Restore previously active bots
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.bot_active == True))
        active_users = result.scalars().all()
        for user in active_users:
            logger.info(f"Restoring bot for user {user.id}")
            await start_bot(user.id)
    yield
    # ── Graceful shutdown: close all open demo positions before cancelling ───
    # Render gives ~30s on SIGTERM before SIGKILL. Without this, deploy windows
    # leave demo positions exposed for 5–7 min unmanaged → stop-loss/time-cap
    # fires on resume against a stale price → preventable losses.
    try:
        await asyncio.wait_for(
            graceful_shutdown_close_all_demo_positions(),
            timeout=20.0,
        )
    except asyncio.TimeoutError:
        logger.warning("Graceful shutdown timed out at 20s — proceeding to cancel tasks")
    except Exception as _e:
        logger.error(f"Graceful shutdown errored: {_e}", exc_info=True)

    # Cancel bot tasks now that positions are flushed
    for task in _bot_tasks.values():
        task.cancel()


app = FastAPI(title="Nalo.Ai", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(auth_router.router)
app.include_router(bot_router.router)
app.include_router(trades_router.router)
app.include_router(reports_router.router)
app.include_router(market_router.router)
app.include_router(admin_router.router)
app.include_router(stripe_router.router)


# WebSocket
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(...)):
    await websocket.accept()  # Must accept before closing, otherwise starlette returns 403
    async with AsyncSessionLocal() as db:
        user = await get_current_user_ws(token, db)
    if not user:
        await websocket.close(code=4002)  # 4002 = invalid auth
        return
    await ws_manager.connect(user.id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(user.id, websocket)
    except Exception as e:
        logger.warning(f"WebSocket error for user {user.id}: {e}")
        ws_manager.disconnect(user.id, websocket)


# Health monitoring endpoints
@app.get("/api/health")
async def health_check():
    """Run a full health check across all subsystems."""
    report = await run_full_health_check()
    return report


@app.get("/api/health/history")
async def health_history():
    """Get recent health check history."""
    return get_health_history()


@app.get("/api/health/quick")
async def health_quick():
    """Quick liveness check for uptime monitors."""
    return {"status": "ok", "timestamp": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()}


# Serve React frontend
if os.path.exists("frontend/dist"):
    app.mount("/assets", StaticFiles(directory="frontend/dist/assets"), name="assets")

    @app.get("/{path:path}")
    async def serve_spa(path: str):
        index = "frontend/dist/index.html"
        if os.path.exists(index):
            return FileResponse(index)
        return {"error": "Frontend not built. Run: cd frontend && npm run build"}
