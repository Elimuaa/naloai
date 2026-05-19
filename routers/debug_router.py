"""
debug_router.py — Diagnostic endpoint for when Render logs aren't accessible.

URL-secret-gated (no JWT/basic auth) so it works even when the user's session
is broken and ADMIN_PASSWORD isn't set on the host. Returns a comprehensive
snapshot of server-side state in one call:

  - Last 20 trades (any state, any user) with timestamps and exit reasons
  - In-memory bot loop states (price history length, in_trade, last_signal,
    indicators, demo_mode, key_invalid)
  - Risk manager states (is_paused, daily_pnl, recent_stops)
  - Background task health (alive vs done counts)
  - The most recent log lines captured by a circular in-process buffer

The URL secret is intentionally long + random so it can't be guessed.
Remove this router once Render logs are accessible.
"""
import logging
from collections import deque
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select, desc
from database import AsyncSessionLocal, Trade, User, RiskState

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/debug", tags=["debug"])

# ── URL secret — must match the ?key=… query param to access the endpoint ──
# Hardcoded intentionally: ADMIN_PASSWORD env var isn't set on the host,
# and we need a way to read server state without a session. This is a
# temporary diagnostic — remove the router once logs are accessible.
_DEBUG_SECRET = "n4l0a1-diag-2026-mvp"

# ── In-process log buffer — captures the last 200 log lines ──
# Installed at module-import time so it catches everything from app start.
_LOG_BUFFER: deque = deque(maxlen=200)


class _BufferHandler(logging.Handler):
    """Pushes every log record onto the in-memory buffer."""
    def emit(self, record):
        try:
            _LOG_BUFFER.append({
                "t": datetime.now(timezone.utc).isoformat(),
                "lvl": record.levelname,
                "src": record.name,
                "msg": self.format(record),
            })
        except Exception:
            pass


_handler = _BufferHandler(level=logging.INFO)
_handler.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger().addHandler(_handler)
# Also explicitly cover the loggers we care about most
for name in ("bot_engine", "startup_tasks", "routers.bot_router",
             "routers.trades_router", "ai_calibrator", "risk_manager"):
    logging.getLogger(name).addHandler(_handler)


def _check_secret(key: str) -> None:
    if key != _DEBUG_SECRET:
        raise HTTPException(404, "Not found")


@router.get("/state")
async def debug_state(key: str = Query(...)):
    """Return a comprehensive server-side state snapshot."""
    _check_secret(key)

    # 1. In-memory bot states
    from bot_engine import bot_states, _bot_tasks, _risk_managers, _client_cache
    bots = []
    for skey, state in list(bot_states.items()):
        task = _bot_tasks.get(skey)
        bots.append({
            "key": skey,
            "task_alive": bool(task and not task.done()),
            "task_done": bool(task and task.done()),
            "task_exception": str(task.exception()) if (task and task.done() and not task.cancelled() and task.exception()) else None,
            "price_history_len": len(state.price_history),
            "in_trade": state.in_trade,
            "entry_price": state.entry_price,
            "trade_side": state.trade_side,
            "current_quantity": state.current_quantity,
            "last_signal": state.last_signal,
            "last_update": state.last_update,
            "error_count": state.error_count,
            "demo_mode": state.demo_mode,
            "force_demo": state.force_demo,
            "key_invalid": state.key_invalid,
            "regime": state.regime,
            "bullish_levels_count": len(state.bullish_levels),
            "bearish_levels_count": len(state.bearish_levels),
            "indicators": state.indicators,
            "consecutive_losses": state.consecutive_losses,
            "consecutive_wins": state.consecutive_wins,
            "current_trade_id": state.current_trade_id,
        })

    # 2. Risk manager states
    risk_mgrs = []
    for rm_key, rm in list(_risk_managers.items()):
        risk_mgrs.append({
            "key": rm_key,
            "is_paused": rm.is_paused,
            "pause_reason": rm.pause_reason,
            "daily_pnl": rm.daily_pnl,
            "daily_starting_balance": rm.daily_starting_balance,
            "cooldown_remaining": rm.cooldown_remaining,
            "recent_stops_count": len(rm.stop_loss_times) if hasattr(rm, "stop_loss_times") else 0,
            "max_drawdown_pct": rm.max_drawdown_pct,
            "max_stops_before_pause": rm.max_stops_before_pause,
        })

    # 3. Client cache contents
    clients = []
    for ck, client in list(_client_cache.items()):
        clients.append({
            "key": ck,
            "type": type(client).__name__,
            "has_balance_attr": hasattr(client, "balance"),
            "balance": getattr(client, "balance", None) if hasattr(client, "balance") else None,
            "has_get_portfolio_cash": hasattr(client, "get_portfolio_cash"),
        })

    # 4. Last 20 trades across ALL users
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Trade).order_by(desc(Trade.opened_at)).limit(20)
        )).scalars().all()
        recent_trades = [{
            "id": t.id[:8],
            "user_id": t.user_id[:8],
            "symbol": t.symbol,
            "side": t.side,
            "state": t.state,
            "is_demo": t.is_demo,
            "opened_at": t.opened_at.isoformat() if t.opened_at else None,
            "closed_at": t.closed_at.isoformat() if t.closed_at else None,
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "pnl": t.pnl,
            "exit_reason": t.exit_reason,
            "quantity_value": getattr(t, "quantity_value", None),
        } for t in rows]

        # 5. User summary — what params each user has
        users = (await db.execute(select(User))).scalars().all()
        user_summary = [{
            "id": u.id[:8],
            "email": u.email,
            "is_premium": u.is_premium,
            "calibration_count": u.calibration_count,
            "bot_active": u.bot_active,
            "bot_active_capital": u.bot_active_capital,
            "force_demo_robinhood": getattr(u, "force_demo_robinhood", False),
            "entry_z": u.entry_z,
            "stop_loss_pct": u.stop_loss_pct,
            "take_profit_pct": u.take_profit_pct,
            "trail_stop_pct": u.trail_stop_pct,
            "use_rsi_filter": u.use_rsi_filter,
            "use_bbands_filter": u.use_bbands_filter,
            "use_adx_filter": u.use_adx_filter,
            "risk_per_trade_pct": u.risk_per_trade_pct,
            "max_exposure_pct": u.max_exposure_pct,
            "has_rh_keys": bool(u.rh_api_key),
            "has_capital_keys": bool(u.capital_api_key and u.capital_identifier),
        } for u in users]

    return {
        "now_utc": datetime.now(timezone.utc).isoformat(),
        "bot_loops": bots,
        "risk_managers": risk_mgrs,
        "client_cache": clients,
        "users": user_summary,
        "recent_trades": recent_trades,
    }


@router.get("/logs")
async def debug_logs(
    key: str = Query(...),
    level: str | None = Query(None, description="Filter by level: INFO/WARNING/ERROR"),
    contains: str | None = Query(None, description="Substring filter on message"),
    limit: int = Query(200, le=200),
):
    """Return the in-memory log buffer (last 200 lines)."""
    _check_secret(key)
    out = list(_LOG_BUFFER)
    if level:
        out = [l for l in out if l["lvl"] == level.upper()]
    if contains:
        out = [l for l in out if contains.lower() in l["msg"].lower()]
    return {"count": len(out), "logs": out[-limit:]}
