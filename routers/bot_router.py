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


def _get_live_demo_balance(user: User) -> float | None:
    """Get live demo balance from running client if available.
    Checks broker-aware key first, then legacy key as fallback."""
    try:
        from bot_engine import _client_cache
        broker = getattr(user, 'broker_type', 'robinhood') or 'robinhood'
        # Try broker-aware key (current format), then legacy key
        client = (
            _client_cache.get(f"{user.id}:{broker}:demo")
            or _client_cache.get(f"{user.id}:demo")
        )
        if client and hasattr(client, 'balance'):
            return round(client.balance, 2)
    except Exception:
        pass
    return None


class KeysPayload(BaseModel):
    rh_api_key: str


class AnthropicKeyPayload(BaseModel):
    anthropic_api_key: str


class StartPayload(BaseModel):
    mode: str = "auto"


class DemoBalancePayload(BaseModel):
    balance: float = 10000.0


class SettingsPayload(BaseModel):
    trading_symbol: str = "BTC-USD"
    entry_z: float = 2.0
    exit_z: float = 0.5
    lookback: int = 20
    stop_loss_pct: float = 0.02
    take_profit_pct: float = 0.04
    trail_stop_pct: float = 0.01
    # Indicator filters
    use_rsi_filter: bool = True
    use_ema_filter: bool = True
    use_adx_filter: bool = True
    use_bbands_filter: bool = True
    use_macd_filter: bool = False
    use_volume_filter: bool = False
    # Risk management
    max_drawdown_pct: float = 5.0
    max_stops_before_pause: int = 3
    cooldown_ticks: int = 5
    risk_per_trade_pct: float = 1.0
    max_exposure_pct: float = 20.0
    # Position sizing
    position_size_mode: str = "dynamic"
    fixed_quantity: float = 0.0001
    # Telegram
    telegram_enabled: bool = False
    # Broker
    broker_type: str = "robinhood"


class CapitalKeysPayload(BaseModel):
    capital_api_key: str
    capital_identifier: str   # Capital.com login email
    capital_password: str     # Capital.com login password


class TradovateKeysPayload(BaseModel):
    tradovate_username: str
    tradovate_password: str
    tradovate_account_id: int


class TelegramPayload(BaseModel):
    bot_token: str
    chat_id: str


class OptimizePayload(BaseModel):
    mode: str = "quick"  # "quick" or "full"


@router.post("/keys")
async def save_keys(
    payload: KeysPayload,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    api_key = payload.rh_api_key.strip()
    if not api_key:
        raise HTTPException(400, "API key cannot be empty")

    await db.execute(
        update(User).where(User.id == current_user.id).values(
            rh_api_key=api_key,
            rh_private_key=current_user.ed25519_private_key,
        )
    )
    await db.commit()

    from bot_engine import _bot_tasks, bot_states, _client_cache
    for k in list(_client_cache.keys()):
        if k.startswith(current_user.id):
            del _client_cache[k]
    if current_user.id in bot_states:
        bot_states[current_user.id].key_invalid = False
        bot_states[current_user.id].force_demo = False

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
            use_rsi_filter=payload.use_rsi_filter,
            use_ema_filter=payload.use_ema_filter,
            use_adx_filter=payload.use_adx_filter,
            use_bbands_filter=payload.use_bbands_filter,
            use_macd_filter=payload.use_macd_filter,
            use_volume_filter=payload.use_volume_filter,
            max_drawdown_pct=payload.max_drawdown_pct,
            max_stops_before_pause=payload.max_stops_before_pause,
            cooldown_ticks=payload.cooldown_ticks,
            risk_per_trade_pct=payload.risk_per_trade_pct,
            max_exposure_pct=payload.max_exposure_pct,
            position_size_mode=payload.position_size_mode,
            fixed_quantity=payload.fixed_quantity,
            telegram_enabled=payload.telegram_enabled,
            broker_type=payload.broker_type,
        )
    )
    await db.commit()

    # Clear client cache when broker or symbol changes (force new client)
    from bot_engine import _client_cache
    for k in list(_client_cache.keys()):
        if k.startswith(current_user.id):
            del _client_cache[k]

    # Update risk manager if it exists
    from bot_engine import _risk_managers
    if current_user.id in _risk_managers:
        rm = _risk_managers[current_user.id]
        rm.max_drawdown_pct = payload.max_drawdown_pct
        rm.max_stops_before_pause = payload.max_stops_before_pause
        rm.cooldown_ticks = payload.cooldown_ticks
        rm.risk_per_trade_pct = payload.risk_per_trade_pct
        rm.max_exposure_pct = payload.max_exposure_pct

    return {"message": "Settings saved"}


@router.post("/start")
async def bot_start(
    payload: StartPayload = StartPayload(),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    mode = payload.mode
    broker = getattr(current_user, 'broker_type', 'robinhood') or 'robinhood'

    if mode == "live":
        if broker == 'robinhood' and not (current_user.rh_api_key and current_user.ed25519_private_key):
            raise HTTPException(400, "Add your Robinhood API key in Settings before going live")
        elif broker == 'capital' and not (current_user.capital_api_key and current_user.capital_identifier):
            raise HTTPException(400, "Add your Capital.com API key in Settings before going live")
        elif broker == 'tradovate' and not (current_user.tradovate_username and current_user.tradovate_password):
            raise HTTPException(400, "Add your Tradovate credentials in Settings before going live")

    from bot_engine import bot_states
    if mode == "live" and broker == 'robinhood' and current_user.id in bot_states and bot_states[current_user.id].key_invalid:
        raise HTTPException(400, "Your Robinhood API key is invalid — paste a new one in Settings first")

    force_demo = (mode == "demo")
    await db.execute(update(User).where(User.id == current_user.id).values(bot_active=True))
    await db.commit()
    result = await start_bot(current_user.id, force_demo=force_demo)

    import notifications
    if getattr(current_user, 'telegram_enabled', False):
        import asyncio
        asyncio.create_task(notifications.notify_bot_started(
            "demo" if force_demo else "live", current_user.trading_symbol
        ))

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
    _broker = getattr(current_user, 'broker_type', 'robinhood') or 'robinhood'
    if _broker == 'capital':
        _has_keys = bool(current_user.capital_api_key and current_user.capital_identifier)
    elif _broker == 'tradovate':
        _has_keys = bool(current_user.tradovate_username and current_user.tradovate_password)
    else:
        _has_keys = bool(current_user.rh_api_key)

    return {
        "trading_symbol": current_user.trading_symbol,
        "stop_loss_pct": current_user.stop_loss_pct,
        "take_profit_pct": current_user.take_profit_pct,
        "trail_stop_pct": current_user.trail_stop_pct,
        "has_api_keys": _has_keys,
        "demo_mode": not _has_keys,
        "public_key": current_user.ed25519_public_key or "",
        "demo_balance": _get_live_demo_balance(current_user) or current_user.demo_balance or 10000.0,
        # Broker info
        "broker_type": _broker,
        "has_capital_keys": bool(getattr(current_user, 'capital_api_key', None) and getattr(current_user, 'capital_identifier', None)),
        "has_tradovate_keys": bool(getattr(current_user, 'tradovate_username', None) and getattr(current_user, 'tradovate_password', None)),
        # Risk management (user-facing)
        "max_drawdown_pct": getattr(current_user, 'max_drawdown_pct', 5.0) or 5.0,
        "max_stops_before_pause": getattr(current_user, 'max_stops_before_pause', 3) or 3,
        "cooldown_ticks": getattr(current_user, 'cooldown_ticks', 5) or 5,
        "risk_per_trade_pct": getattr(current_user, 'risk_per_trade_pct', 1.0) or 1.0,
        "max_exposure_pct": getattr(current_user, 'max_exposure_pct', 20.0) or 20.0,
        # Position sizing
        "position_size_mode": getattr(current_user, 'position_size_mode', 'dynamic') or 'dynamic',
        "fixed_quantity": getattr(current_user, 'fixed_quantity', 0.0001) or 0.0001,
        # Telegram
        "telegram_enabled": getattr(current_user, 'telegram_enabled', False),
        "telegram_configured": bool(os.getenv("TELEGRAM_BOT_TOKEN")) and bool(os.getenv("TELEGRAM_CHAT_ID")),
        # Premium
        "is_premium": getattr(current_user, 'is_premium', False),
    }


@router.get("/balance")
async def get_balance(current_user: User = Depends(get_current_user)):
    broker = getattr(current_user, 'broker_type', 'robinhood') or 'robinhood'
    has_live_creds = (
        bool(current_user.rh_api_key) if broker == 'robinhood'
        else bool(current_user.capital_api_key and current_user.capital_identifier) if broker == 'capital'
        else bool(current_user.tradovate_username and current_user.tradovate_password)
    )
    if not has_live_creds:
        # Try to get live balance from running client first
        from bot_engine import _client_cache
        client = (
            _client_cache.get(f"{current_user.id}:{broker}:demo")
            or _client_cache.get(f"{current_user.id}:demo")
        )
        if client and hasattr(client, 'balance'):
            demo_bal = round(client.balance, 2)
        else:
            demo_bal = current_user.demo_balance or 10000.0
        return {"available": demo_bal, "holdings": [], "is_demo": True}

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
    import re
    key = payload.anthropic_api_key.strip()
    # Validate: API keys should only contain safe characters (alphanumeric, hyphens, underscores)
    if not re.match(r'^[a-zA-Z0-9_\-]+$', key) or len(key) < 10:
        raise HTTPException(400, "Invalid API key format")
    # Prevent newline injection into .env
    if '\n' in key or '\r' in key:
        raise HTTPException(400, "Invalid API key format")
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")

    try:
        with open(env_path, "r") as f:
            content = f.read()
    except FileNotFoundError:
        content = ""

    if re.search(r"^ANTHROPIC_API_KEY=", content, re.MULTILINE):
        content = re.sub(r"^ANTHROPIC_API_KEY=.*$", f"ANTHROPIC_API_KEY={key}", content, flags=re.MULTILINE)
    else:
        content = content.rstrip("\n") + f"\nANTHROPIC_API_KEY={key}\n"

    with open(env_path, "w") as f:
        f.write(content)

    os.environ["ANTHROPIC_API_KEY"] = key
    return {"message": "Anthropic API key saved and activated"}


@router.get("/ai-status")
async def get_ai_status(current_user: User = Depends(get_current_user)):
    key = os.getenv("ANTHROPIC_API_KEY", "")
    return {"configured": bool(key), "key_preview": f"...{key[-4:]}" if len(key) > 4 else ""}


@router.post("/test-connection")
async def test_connection(current_user: User = Depends(get_current_user)):
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
                "2. You registered YOUR public key (shown above) on Robinhood\n"
                "3. The key hasn't been revoked on Robinhood"
            )
        elif "403" in err:
            detail = (
                "Not authorized (403). Go to robinhood.com > Account > Crypto API, "
                "make sure your key has 'Crypto Trading' permission enabled."
            )
        else:
            detail = f"Connection failed: {err}"
        return {"ok": False, "error": detail}


@router.post("/demo-balance")
async def set_demo_balance(
    payload: DemoBalancePayload,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    bal = max(0, payload.balance)
    await db.execute(update(User).where(User.id == current_user.id).values(demo_balance=bal))
    await db.commit()

    from bot_engine import _client_cache, _bot_tasks
    _broker = getattr(current_user, 'broker_type', 'robinhood') or 'robinhood'
    for k in list(_client_cache.keys()):
        if k.startswith(current_user.id):
            del _client_cache[k]

    if current_user.id in _bot_tasks and not _bot_tasks[current_user.id].done():
        await stop_bot(current_user.id)
        await db.execute(update(User).where(User.id == current_user.id).values(bot_active=True))
        await db.commit()
        await start_bot(current_user.id, force_demo=True)
        return {"message": f"Demo balance set to ${bal:,.2f} — bot restarted", "balance": bal}

    return {"message": f"Demo balance set to ${bal:,.2f}", "balance": bal}


@router.post("/demo-balance/clear")
async def clear_demo_balance(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    default_bal = 10000.0
    await db.execute(update(User).where(User.id == current_user.id).values(demo_balance=default_bal))
    await db.commit()

    from database import Trade
    from sqlalchemy import delete
    await db.execute(delete(Trade).where(Trade.user_id == current_user.id, Trade.is_demo == True))
    await db.commit()

    from bot_engine import _client_cache, _bot_tasks
    _client_cache.pop(f"{current_user.id}:demo", None)

    if current_user.id in _bot_tasks and not _bot_tasks[current_user.id].done():
        await stop_bot(current_user.id)
        await db.execute(update(User).where(User.id == current_user.id).values(bot_active=True))
        await db.commit()
        await start_bot(current_user.id, force_demo=True)

    return {"message": "Demo balance reset to $10,000 and demo trades cleared", "balance": default_bal}


@router.post("/telegram")
async def save_telegram(
    payload: TelegramPayload,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Save Telegram bot token and chat ID to .env."""
    import re
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    try:
        with open(env_path, "r") as f:
            content = f.read()
    except FileNotFoundError:
        content = ""

    for key, value in [("TELEGRAM_BOT_TOKEN", payload.bot_token.strip()), ("TELEGRAM_CHAT_ID", payload.chat_id.strip())]:
        # Validate: prevent newline injection into .env
        if '\n' in value or '\r' in value or not re.match(r'^[a-zA-Z0-9_:\-]+$', value):
            raise HTTPException(400, f"Invalid {key} format")
        if re.search(rf"^{key}=", content, re.MULTILINE):
            content = re.sub(rf"^{key}=.*$", f"{key}={value}", content, flags=re.MULTILINE)
        else:
            content = content.rstrip("\n") + f"\n{key}={value}\n"
        os.environ[key] = value

    with open(env_path, "w") as f:
        f.write(content)

    await db.execute(update(User).where(User.id == current_user.id).values(telegram_enabled=True))
    await db.commit()

    return {"message": "Telegram configured"}


@router.post("/telegram/test")
async def test_telegram(current_user: User = Depends(get_current_user)):
    import notifications
    result = await notifications.test_connection()
    return result


@router.post("/risk/resume")
async def resume_trading(current_user: User = Depends(get_current_user)):
    """Resume trading after risk manager pause."""
    from bot_engine import _risk_managers
    rm = _risk_managers.get(current_user.id)
    if rm:
        rm.resume()
        return {"message": "Trading resumed"}
    return {"message": "No active risk manager"}


@router.get("/premium/status")
async def premium_status(current_user: User = Depends(get_current_user)):
    """Get the user's premium subscription status."""
    from ai_calibrator import get_calibration_history
    is_premium = getattr(current_user, 'is_premium', False)
    history = await get_calibration_history(current_user.id, limit=5) if is_premium else []
    return {
        "is_premium": is_premium,
        "premium_since": current_user.premium_since.isoformat() if getattr(current_user, 'premium_since', None) else None,
        "calibration_count": getattr(current_user, 'calibration_count', 0),
        "last_calibration_at": current_user.last_calibration_at.isoformat() if getattr(current_user, 'last_calibration_at', None) else None,
        "recent_calibrations": history,
        "price": "$199/month",
        "features": [
            "AI-powered trade analysis on every trade",
            "Auto-calibration after every closed trade",
            "Strategy parameters adapt to market conditions",
            "Bot gets smarter with every trade",
            "Priority support",
        ],
    }


@router.post("/premium/activate")
async def activate_premium(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Activate premium for the user. In production, integrate Stripe here."""
    from datetime import datetime, timezone
    await db.execute(
        update(User).where(User.id == current_user.id).values(
            is_premium=True,
            premium_since=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return {"message": "Premium activated! AI auto-calibration is now active.", "is_premium": True}


@router.post("/premium/deactivate")
async def deactivate_premium(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Deactivate premium for the user."""
    await db.execute(
        update(User).where(User.id == current_user.id).values(is_premium=False)
    )
    await db.commit()
    return {"message": "Premium deactivated.", "is_premium": False}


@router.get("/premium/calibrations")
async def get_calibrations(
    current_user: User = Depends(get_current_user),
):
    """Get calibration history for premium users."""
    if not getattr(current_user, 'is_premium', False):
        raise HTTPException(403, "Premium subscription required")
    from ai_calibrator import get_calibration_history
    return await get_calibration_history(current_user.id, limit=20)


@router.post("/capital-keys")
async def save_capital_keys(
    payload: CapitalKeysPayload,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Save Capital.com credentials and switch broker_type to 'capital'."""
    api_key = payload.capital_api_key.strip()
    identifier = payload.capital_identifier.strip()
    password = payload.capital_password

    if not api_key or not identifier or not password:
        raise HTTPException(400, "All three Capital.com fields are required")

    await db.execute(
        update(User).where(User.id == current_user.id).values(
            capital_api_key=api_key,
            capital_identifier=identifier,
            capital_password=password,
            broker_type="capital",
        )
    )
    await db.commit()

    # Clear client cache so next start uses fresh client
    from bot_engine import _client_cache
    for k in list(_client_cache.keys()):
        if k.startswith(current_user.id):
            del _client_cache[k]

    return {"message": "Capital.com credentials saved. Broker set to Capital.com."}


@router.post("/test-capital-connection")
async def test_capital_connection(current_user: User = Depends(get_current_user)):
    """Test Capital.com live credentials and return account balance."""
    if not current_user.capital_api_key or not current_user.capital_identifier:
        return {"ok": False, "error": "No Capital.com credentials saved yet"}
    try:
        from capital_client import CapitalComClient
        client = CapitalComClient(
            api_key=current_user.capital_api_key,
            identifier=current_user.capital_identifier,
            password=current_user.capital_password or "",
            demo=False,
        )
        cash = await client.get_portfolio_cash()
        return {"ok": True, "balance": cash, "message": f"Connected! Balance: ${cash:,.2f}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/tradovate-keys")
async def save_tradovate_keys(
    payload: TradovateKeysPayload,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Save Tradovate credentials and switch broker_type to 'tradovate'."""
    username = payload.tradovate_username.strip()
    password = payload.tradovate_password
    account_id = payload.tradovate_account_id

    if not username or not password or not account_id:
        raise HTTPException(400, "Username, password, and account ID are all required")

    await db.execute(
        update(User).where(User.id == current_user.id).values(
            tradovate_username=username,
            tradovate_password=password,
            tradovate_account_id=account_id,
            broker_type="tradovate",
        )
    )
    await db.commit()

    from bot_engine import _client_cache
    for k in list(_client_cache.keys()):
        if k.startswith(current_user.id):
            del _client_cache[k]

    return {"message": "Tradovate credentials saved. Broker set to Tradovate."}


@router.post("/test-tradovate-connection")
async def test_tradovate_connection(current_user: User = Depends(get_current_user)):
    """Test Tradovate live credentials and return account balance."""
    if not current_user.tradovate_username or not current_user.tradovate_password:
        return {"ok": False, "error": "No Tradovate credentials saved yet"}
    try:
        from tradovate_client import TradovateClient
        client = TradovateClient(
            username=current_user.tradovate_username,
            password=current_user.tradovate_password,
            account_id=current_user.tradovate_account_id or 0,
            demo=False,
        )
        cash = await client.get_portfolio_cash()
        return {"ok": True, "balance": cash, "message": f"Connected! Balance: ${cash:,.2f}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/optimize")
async def optimize_params(
    payload: OptimizePayload = OptimizePayload(),
    current_user: User = Depends(get_current_user),
):
    """Run quantum-inspired parameter optimization on collected price history."""
    from bot_engine import bot_states
    state = bot_states.get(current_user.id)
    if not state or len(state.price_history) < 50:
        raise HTTPException(400, "Need at least 50 price ticks. Let the bot run longer before optimizing.")

    from quantum_optimizer import quick_optimize, full_optimize
    if payload.mode == "full":
        result = full_optimize(state.price_history)
    else:
        result = quick_optimize(state.price_history)

    return result
