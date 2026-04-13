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
from bot_engine import start_bot, _bot_tasks
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
    # Restore previously active bots
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.bot_active == True))
        active_users = result.scalars().all()
        for user in active_users:
            logger.info(f"Restoring bot for user {user.id}")
            await start_bot(user.id)
    yield
    # Shutdown all bots
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
