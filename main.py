"""
main.py — Nalo.Ai application entry point.

Responsibilities:
  - FastAPI app + CORS
  - WebSocket endpoint
  - Lifespan: DB init, startup tasks, graceful shutdown
  - Static file serving for React SPA

All startup logic lives in startup_tasks.py.
All route logic lives in routers/.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from database import init_db, AsyncSessionLocal
from auth import get_current_user_ws
from ws_manager import ws_manager
from bot_engine import _bot_tasks, graceful_shutdown_close_all_demo_positions
from scheduler import start_scheduler
from startup_tasks import run_all
from health_monitor import run_full_health_check, get_health_history
from routers import (
    auth_router, bot_router, trades_router,
    reports_router, market_router, admin_router, stripe_router,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    from robinhood import sync_clock_offset
    await sync_clock_offset()
    await init_db()
    start_scheduler()
    await run_all()

    yield

    # Graceful shutdown — close demo positions before Render kills the process
    try:
        await asyncio.wait_for(graceful_shutdown_close_all_demo_positions(), timeout=20.0)
    except asyncio.TimeoutError:
        logger.warning("Graceful shutdown timed out at 20s")
    except Exception as e:
        logger.error(f"Graceful shutdown error: {e}", exc_info=True)

    for task in _bot_tasks.values():
        task.cancel()


# ── App ───────────────────────────────────────────────────────────────────────

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


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(...)):
    await websocket.accept()
    async with AsyncSessionLocal() as db:
        user = await get_current_user_ws(token, db)
    if not user:
        await websocket.close(code=4002)
        return
    await ws_manager.connect(user.id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(user.id, websocket)
    except Exception as e:
        logger.warning(f"WebSocket error for user {user.id[:8]}: {e}")
        ws_manager.disconnect(user.id, websocket)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health_check():
    return await run_full_health_check()


@app.get("/api/health/history")
async def health_history():
    return get_health_history()


@app.get("/api/health/quick")
async def health_quick():
    from datetime import datetime, timezone
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


# ── React SPA ─────────────────────────────────────────────────────────────────

if os.path.exists("frontend/dist"):
    app.mount("/assets", StaticFiles(directory="frontend/dist/assets"), name="assets")

    @app.get("/{path:path}")
    async def serve_spa(path: str):
        index = "frontend/dist/index.html"
        if os.path.exists(index):
            return FileResponse(index)
        return {"error": "Frontend not built — run: cd frontend && npm run build"}
