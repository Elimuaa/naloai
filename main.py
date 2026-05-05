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
