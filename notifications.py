"""
Telegram notification system for Nalo.Ai.
Sends trade alerts, risk warnings, and daily summaries to a Telegram chat.
"""

import os
import logging
import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


def _get_config() -> tuple[str, str]:
    """Get Telegram bot token and chat ID from environment."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    return token, chat_id


def is_configured() -> bool:
    token, chat_id = _get_config()
    return bool(token and chat_id)


async def send_message(text: str, parse_mode: str = "HTML") -> bool:
    """Send a message to the configured Telegram chat."""
    token, chat_id = _get_config()
    if not token or not chat_id:
        return False

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{TELEGRAM_API}/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            )
            if r.is_success:
                return True
            logger.warning(f"Telegram send failed: {r.status_code} {r.text[:200]}")
            return False
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return False


async def notify_trade_opened(symbol: str, side: str, entry_price: float, quantity: float, is_demo: bool):
    mode = "DEMO" if is_demo else "LIVE"
    emoji = "\U0001f7e2" if side == "buy" else "\U0001f534"
    text = (
        f"{emoji} <b>Trade Opened</b> [{mode}]\n"
        f"<b>{side.upper()}</b> {quantity} {symbol}\n"
        f"Entry: <code>${entry_price:,.2f}</code>"
    )
    await send_message(text)


async def notify_trade_closed(
    symbol: str, side: str, entry_price: float, exit_price: float,
    pnl: float, pnl_pct: float, exit_reason: str, is_demo: bool
):
    mode = "DEMO" if is_demo else "LIVE"
    emoji = "\U0001f4b0" if pnl >= 0 else "\U0001f4c9"
    sign = "+" if pnl >= 0 else ""
    text = (
        f"{emoji} <b>Trade Closed</b> [{mode}]\n"
        f"<b>{side.upper()}</b> {symbol}\n"
        f"Entry: <code>${entry_price:,.2f}</code> \u2192 Exit: <code>${exit_price:,.2f}</code>\n"
        f"P&L: <code>{sign}${pnl:.4f}</code> ({sign}{pnl_pct:.2f}%)\n"
        f"Reason: {exit_reason.replace('_', ' ')}"
    )
    await send_message(text)


async def notify_risk_pause(reason: str):
    text = (
        f"\u26a0\ufe0f <b>Risk Manager: Trading Paused</b>\n"
        f"{reason}\n\n"
        f"The bot has auto-paused to protect your capital. "
        f"You can resume from the dashboard."
    )
    await send_message(text)


async def notify_bot_started(mode: str, symbol: str):
    text = (
        f"\U0001f680 <b>Bot Started</b> [{mode.upper()}]\n"
        f"Symbol: {symbol}"
    )
    await send_message(text)


async def notify_bot_stopped():
    await send_message("\u23f9 <b>Bot Stopped</b>")


async def notify_daily_summary(
    total_trades: int, wins: int, losses: int,
    total_pnl: float, win_rate: float
):
    sign = "+" if total_pnl >= 0 else ""
    emoji = "\U0001f4ca"
    text = (
        f"{emoji} <b>Daily Summary</b>\n"
        f"Trades: {total_trades} (W: {wins} / L: {losses})\n"
        f"Win Rate: {win_rate:.1f}%\n"
        f"Total P&L: <code>{sign}${total_pnl:.4f}</code>"
    )
    await send_message(text)


async def test_connection() -> dict:
    """Test the Telegram connection by sending a test message."""
    token, chat_id = _get_config()
    if not token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not set"}
    if not chat_id:
        return {"ok": False, "error": "TELEGRAM_CHAT_ID not set"}

    success = await send_message("\u2705 Nalo.Ai connected! You'll receive trade alerts here.")
    return {"ok": success, "error": "" if success else "Failed to send message"}
