from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import update
from database import get_db, User, AsyncSession
from auth import get_current_user
from bot_engine import start_bot, stop_bot, get_bot_status
import logging
import os

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bot", tags=["bot"])

DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"


class KeysPayload(BaseModel):
    rh_api_key: str


class AnthropicKeyPayload(BaseModel):
    anthropic_api_key: str


class StartPayload(BaseModel):
    mode: str = "auto"  # "demo", "live", or "auto" (auto = live if keys exist, else demo)


class SettingsPayload(BaseModel):
    trading_symbol: str = "BTC-USD"
    entry_z: float = 2.0
    exit_z: float = 0.5
    lookback: int = 20
    stop_loss_pct: float = 0.02
    take_profit_pct: float = 0.04
    trail_stop_pct: float = 0.01


@router.post("/keys")
async def save_keys(
    payload: KeysPayload,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    api_key = payload.rh_api_key.strip()  # strip accidental whitespace
    if not api_key:
        raise HTTPException(400, "API key cannot be empty")

    await db.execute(
        update(User).where(User.id == current_user.id).values(
            rh_api_key=api_key,
            rh_private_key=current_user.ed25519_private_key,
        )
    )
    await db.commit()

    # Clear key_invalid flag and client cache so the user can attempt live mode again
    from bot_engine import _bot_tasks, bot_states, _client_cache
    # Invalidate cached clients so new keys are picked up
    for k in list(_client_cache.keys()):
        if k.startswith(current_user.id):
            del _client_cache[k]
    if current_user.id in bot_states:
        bot_states[current_user.id].key_invalid = False
        bot_states[current_user.id].force_demo = False

    # Restart bot immediately so it picks up the real client (no 6-60s wait)
    if current_user.id in _bot_tasks and not _bot_tasks[current_user.id].done():
        await stop_bot(current_user.id)
        await db.execute(update(User).where(User.id == current_user.id).values(bot_active=True))
        await db.commit()
        await start_bot(current_user.id)
        return {"message": "Keys saved — bot restarted in live mode"}

    return {"message": "Keys saved successfully"}


@router.post("/settings")
async def save_settings(
    payload: SettingsPayload,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    await db.execute(
        update(User).where(User.id == current_user.id).values(
            trading_symbol=payload.trading_symbol,
            entry_z=payload.entry_z,
            lookback=str(payload.lookback),
            stop_loss_pct=payload.stop_loss_pct,
            take_profit_pct=payload.take_profit_pct,
            trail_stop_pct=payload.trail_stop_pct,
        )
    )
    await db.commit()
    return {"message": "Settings saved"}


@router.post("/start")
async def bot_start(
    payload: StartPayload = StartPayload(),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    mode = payload.mode
    if mode == "live" and not (current_user.rh_api_key and current_user.ed25519_private_key):
        raise HTTPException(400, "Add your Robinhood API key in Settings before going live")

    from bot_engine import bot_states
    if mode == "live" and current_user.id in bot_states and bot_states[current_user.id].key_invalid:
        raise HTTPException(400, "Your Robinhood API key is invalid — paste a new one in Settings first")

    force_demo = (mode == "demo")
    await db.execute(update(User).where(User.id == current_user.id).values(bot_active=True))
    await db.commit()
    result = await start_bot(current_user.id, force_demo=force_demo)
    return {**result, "mode": "demo" if force_demo or not current_user.rh_api_key else "live"}


@router.post("/stop")
async def bot_stop(current_user: User = Depends(get_current_user)):
    result = await stop_bot(current_user.id)
    return result


@router.get("/status")
async def bot_status(current_user: User = Depends(get_current_user)):
    return get_bot_status(current_user.id)


@router.get("/settings")
async def get_settings(current_user: User = Depends(get_current_user)):
    return {
        "trading_symbol": current_user.trading_symbol,
        "entry_z": current_user.entry_z,
        "lookback": current_user.lookback,
        "stop_loss_pct": current_user.stop_loss_pct,
        "take_profit_pct": current_user.take_profit_pct,
        "trail_stop_pct": current_user.trail_stop_pct,
        "has_api_keys": bool(current_user.rh_api_key),
        "demo_mode": not bool(current_user.rh_api_key),
        "public_key": current_user.ed25519_public_key or "",
    }


@router.get("/balance")
async def get_balance(current_user: User = Depends(get_current_user)):
    if not current_user.rh_api_key:
        return {"available": None, "holdings": [], "error": "No API keys configured"}

    private_key = current_user.ed25519_private_key or current_user.rh_private_key
    if not private_key:
        return {"available": None, "holdings": [], "error": "No private key found"}

    from robinhood import create_client
    client = create_client(current_user.rh_api_key, private_key)
    if not client:
        return {"available": None, "holdings": [], "error": "Failed to create Robinhood client"}

    try:
        cash = await client.get_portfolio_cash()
        holdings_data = await client.get_holdings()
        logger.info(f"Holdings response keys for user {current_user.id}: {list(holdings_data.keys()) if isinstance(holdings_data, dict) else type(holdings_data)}")
        # Robinhood may return {"results": [...]} or a flat list or object
        if isinstance(holdings_data, dict) and "results" in holdings_data:
            raw_holdings = holdings_data["results"]
        elif isinstance(holdings_data, list):
            raw_holdings = holdings_data
        else:
            raw_holdings = []
        holdings = [
            {
                "asset_code": h.get("asset_code", h.get("currency_code", "")),
                "total_quantity": h.get("total_quantity", h.get("quantity", "0")),
                "quantity_available_for_trading": h.get("quantity_available_for_trading", h.get("quantity_available", "0")),
            }
            for h in raw_holdings
            if float(h.get("total_quantity", h.get("quantity", "0"))) > 0
        ]
        return {"available": cash, "holdings": holdings}
    except Exception as e:
        logger.error(f"Balance fetch failed for user {current_user.id}: {e}")
        err = str(e)
        if "401" in err or "Unauthorized" in err:
            msg = "Invalid API key — check your Robinhood API key in Settings"
        elif "403" in err or "Forbidden" in err:
            msg = "API key lacks permission — ensure Crypto Trading is enabled on Robinhood"
        elif "timeout" in err.lower() or "connect" in err.lower():
            msg = "Connection to Robinhood timed out — try again"
        else:
            msg = "Could not fetch balance — check your API key"
        return {"available": None, "holdings": [], "error": msg}


@router.post("/anthropic-key")
async def save_anthropic_key(
    payload: AnthropicKeyPayload,
    current_user: User = Depends(get_current_user)
):
    """Save the Anthropic API key to .env and activate it immediately."""
    import re
    key = payload.anthropic_api_key.strip()
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")

    # Read existing .env content or start fresh
    try:
        with open(env_path, "r") as f:
            content = f.read()
    except FileNotFoundError:
        content = ""

    # Replace or append ANTHROPIC_API_KEY
    if re.search(r"^ANTHROPIC_API_KEY=", content, re.MULTILINE):
        content = re.sub(r"^ANTHROPIC_API_KEY=.*$", f"ANTHROPIC_API_KEY={key}", content, flags=re.MULTILINE)
    else:
        content = content.rstrip("\n") + f"\nANTHROPIC_API_KEY={key}\n"

    with open(env_path, "w") as f:
        f.write(content)

    # Activate in-process immediately (no restart needed)
    os.environ["ANTHROPIC_API_KEY"] = key
    return {"message": "Anthropic API key saved and activated"}


@router.get("/ai-status")
async def get_ai_status(current_user: User = Depends(get_current_user)):
    key = os.getenv("ANTHROPIC_API_KEY", "")
    return {"configured": bool(key), "key_preview": f"...{key[-4:]}" if len(key) > 4 else ""}


@router.post("/test-connection")
async def test_connection(current_user: User = Depends(get_current_user)):
    """Test the Robinhood API key by making a lightweight authenticated call."""
    if not current_user.rh_api_key:
        return {"ok": False, "error": "No API key saved yet"}

    private_key = current_user.ed25519_private_key or current_user.rh_private_key
    if not private_key:
        return {"ok": False, "error": "No private key found — try logging out and back in"}

    from robinhood import create_client
    client = create_client(current_user.rh_api_key, private_key)
    if not client:
        return {"ok": False, "error": "Could not create client — private key may be corrupted"}

    try:
        data = await client.get_account()
        logger.info(f"Test connection account data for user {current_user.id}: {data}")
        # Robinhood may return {"results": [...]} or a flat object
        if "results" in data and isinstance(data["results"], list):
            acct = data["results"][0] if data["results"] else {}
        else:
            acct = data
        buying_power = 0.0
        for field in ("buying_power", "cash_available_for_trading", "cash", "portfolio_cash"):
            val = acct.get(field)
            if val is not None:
                buying_power = float(val)
                break
        # Reset key_invalid flag on success
        from bot_engine import bot_states
        if current_user.id in bot_states:
            bot_states[current_user.id].key_invalid = False
            bot_states[current_user.id].force_demo = False
        return {"ok": True, "buying_power": buying_power, "account_fields": list(acct.keys())}
    except Exception as e:
        err = str(e)
        if "401" in err:
            detail = (
                "Authentication failed (401). Make sure:\n"
                "1. You copied the correct Robinhood API key\n"
                "2. You registered YOUR public key (shown above) on Robinhood — not someone else's\n"
                "3. The key hasn't been revoked on Robinhood"
            )
        elif "403" in err:
            detail = (
                "Not authorized (403). Go to robinhood.com → Account → Crypto API, "
                "make sure your key has 'Crypto Trading' permission enabled. "
                "You may need to delete and recreate your API key."
            )
        else:
            detail = f"Connection failed: {err}"
        return {"ok": False, "error": detail}
