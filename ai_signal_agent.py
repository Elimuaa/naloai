"""
ai_signal_agent.py — Claude-driven signal decision agent.

Replaces the hardcoded filter cascade (ADX > 25, RSI > 70, BB %B,
time-of-day, pattern memory hourly blocks, etc.) with a Claude agent
that reads the full market context via tools and decides whether to
take each signal.

Loop:
  1. Bot loop fires a candidate entry (z-score crossed + retest)
  2. decide_entry() calls Claude with the candidate + a set of tools
  3. Claude reads indicators, recent trades, strategy memory via tools
  4. Claude returns a structured decision: {enter|skip, reason, confidence}
  5. Bot loop honours the decision

Designed to be feature-flagged per-user via User.use_ai_signal_agent so
it can be A/B tested against the hardcoded filters without ripping them
out. Hardcoded filters still run first (cheap fast rejects); the agent
only fires when those would have passed, so we don't waste API calls on
obvious rejects.

Cost: ~2-5 Claude calls per decision (tool-use loop). At Sonnet pricing
that's $0.01-0.03 per signal. Affordable at any sane volume.
"""
import os
import json
import logging
from datetime import datetime, timezone
from anthropic import AsyncAnthropic
from sqlalchemy import select, desc
from database import AsyncSessionLocal, Trade

logger = logging.getLogger(__name__)


def _get_client():
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    return AsyncAnthropic(api_key=key)


SYSTEM_PROMPT = """You are Nalo.Ai's signal-decision agent. The trading bot has just identified a candidate entry (z-score crossed and price retested). Your job is to read the full context via your tools and decide whether to take the trade.

You have these tools:
- get_indicators(symbol): RSI, ADX, BBands %B, MACD, EMA, regime, slow-z
- get_recent_trades(symbol, limit): the last N closed trades for this symbol with side, pnl, exit_reason
- get_strategy_memory(symbol, side, hour_utc): historical win rate for this exact bucket
- get_market_context(symbol): current price, current UTC hour, dead-zone status, recent volatility

DECISION CRITERIA — be conservative:
- Skip if the historical win rate for this bucket is < 40% AND sample size > 10
- Skip if the regime strongly conflicts with the entry direction (strong uptrend + SELL signal = skip)
- Skip if RSI is at the wrong extreme for the side (RSI > 75 + BUY = skip)
- Skip if recent trades on this symbol are 3+ losses in a row
- ENTER if z-score is strong (>= 1.5) AND no strong conflicting signal
- ENTER if pattern memory shows >= 55% win rate for this bucket with sample size >= 5

Use 2-4 tool calls maximum before deciding. Don't over-research — the bot needs a decision in seconds.

Respond with ONLY a JSON object on your final turn:
{
  "decision": "enter" or "skip",
  "reason": "one sentence explaining why",
  "confidence": 0.0 to 1.0
}
No prose around the JSON. No code blocks. Just the JSON."""


# ── Tool definitions (the JSON-schema the agent sees) ─────────────────────────
TOOLS = [
    {
        "name": "get_indicators",
        "description": "Return the latest technical indicators for a symbol's bot loop",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    },
    {
        "name": "get_recent_trades",
        "description": "Return the last N closed trades for this user+symbol",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "get_strategy_memory",
        "description": "Return historical win rate for this exact symbol+side+hour bucket",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "hour_utc": {"type": "integer"},
            },
            "required": ["symbol", "side", "hour_utc"],
        },
    },
    {
        "name": "get_market_context",
        "description": "Return current price, hour, dead-zone status, and recent volatility",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    },
]


# ── Tool implementations — pure-Python, read from existing systems ────────────

async def _tool_get_indicators(user_id: str, symbol: str) -> dict:
    from bot_engine import bot_states
    state = bot_states.get(f"{user_id}:{symbol}")
    if not state:
        return {"error": "no bot state for this symbol"}
    return {
        "regime": state.regime,
        "slow_z_score": state.slow_z_score,
        "indicators": state.indicators or {},
        "price_history_ticks": len(state.price_history),
        "in_trade_already": state.in_trade,
    }


async def _tool_get_recent_trades(user_id: str, symbol: str, limit: int = 10) -> dict:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Trade)
            .where(Trade.user_id == user_id, Trade.symbol == symbol, Trade.state == "closed")
            .order_by(desc(Trade.closed_at))
            .limit(limit)
        )).scalars().all()
    return {
        "trades": [
            {
                "side": t.side,
                "pnl": round(t.pnl or 0, 2),
                "pnl_pct": round(t.pnl_pct or 0, 3),
                "exit_reason": t.exit_reason,
                "closed_at": t.closed_at.isoformat() if t.closed_at else None,
            }
            for t in rows
        ]
    }


async def _tool_get_strategy_memory(user_id: str, symbol: str, side: str, hour_utc: int) -> dict:
    try:
        from strategy_memory import lookup_bucket
        result = await lookup_bucket(
            user_id=user_id, symbol=symbol, side=side, hour_utc=hour_utc,
        )
        return result or {"win_rate": None, "sample_size": 0, "note": "no data for this bucket"}
    except Exception as e:
        return {"error": f"strategy memory unavailable: {e}"}


async def _tool_get_market_context(user_id: str, symbol: str) -> dict:
    from bot_engine import bot_states, _broker_for_symbol
    from broker_base import get_asset_class, ASSET_CLASS_PRESETS
    state = bot_states.get(f"{user_id}:{symbol}")
    now_hour = datetime.now(timezone.utc).hour
    preset = ASSET_CLASS_PRESETS[get_asset_class(symbol)]
    in_dead_zone = now_hour in preset["dead_zone_hours"]
    ctx = {
        "current_hour_utc": now_hour,
        "in_dead_zone": in_dead_zone,
        "broker": _broker_for_symbol(symbol),
    }
    if state and state.price_history:
        ph = state.price_history
        ctx["current_price"] = ph[-1]
        if len(ph) >= 20:
            recent_high = max(ph[-20:])
            recent_low = min(ph[-20:])
            ctx["recent_range_pct"] = round(((recent_high - recent_low) / ph[-1]) * 100, 3)
    return ctx


async def _dispatch_tool(name: str, tool_input: dict, user_id: str) -> str:
    """Run a single tool and return its JSON-serialised result."""
    try:
        if name == "get_indicators":
            result = await _tool_get_indicators(user_id, tool_input["symbol"])
        elif name == "get_recent_trades":
            result = await _tool_get_recent_trades(
                user_id, tool_input["symbol"], tool_input.get("limit", 10)
            )
        elif name == "get_strategy_memory":
            result = await _tool_get_strategy_memory(
                user_id, tool_input["symbol"], tool_input["side"], tool_input["hour_utc"]
            )
        elif name == "get_market_context":
            result = await _tool_get_market_context(user_id, tool_input["symbol"])
        else:
            result = {"error": f"unknown tool: {name}"}
    except Exception as e:
        logger.warning(f"Agent tool {name} failed: {e}", exc_info=True)
        result = {"error": str(e)}
    return json.dumps(result)


# ── Main entry point ──────────────────────────────────────────────────────────

DEFAULT_DECISION = {"decision": "skip", "reason": "agent unavailable", "confidence": 0.0}


async def decide_entry(
    user_id: str,
    symbol: str,
    side: str,
    z_score: float,
    entry_price: float,
    max_turns: int = 6,
) -> dict:
    """Ask the agent whether to take this candidate entry.

    Returns {decision: 'enter'|'skip', reason: str, confidence: float}.
    If anything goes wrong (no API key, network error, parse failure) we
    return DEFAULT_DECISION (skip) — fail-safe, never enter on uncertainty.
    """
    client = _get_client()
    if client is None:
        logger.debug("No ANTHROPIC_API_KEY set — agent skipping (fail-safe)")
        return DEFAULT_DECISION

    user_msg = (
        f"Candidate signal:\n"
        f"  symbol: {symbol}\n"
        f"  side:   {side}\n"
        f"  z_score: {z_score:.3f}\n"
        f"  entry_price: {entry_price}\n"
        f"  utc_hour: {datetime.now(timezone.utc).hour}\n\n"
        f"Read the relevant context via your tools and decide. Aim for 2-4 tool "
        f"calls then respond with the JSON decision."
    )

    messages = [{"role": "user", "content": user_msg}]

    try:
        for turn in range(max_turns):
            resp = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )

            # If the model is done (no tool calls), parse its final JSON.
            if resp.stop_reason == "end_turn":
                text_blocks = [b.text for b in resp.content if b.type == "text"]
                final_text = "\n".join(text_blocks).strip()
                return _parse_decision(final_text)

            # Otherwise execute any tool_use blocks and feed the results back.
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    result_str = await _dispatch_tool(block.name, block.input, user_id)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_str,
                    })

            # Append assistant's full response + our tool results to the conversation
            messages.append({"role": "assistant", "content": resp.content})
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
            else:
                # Model didn't call tools AND didn't end the turn — bail out
                logger.warning(f"Agent turn {turn}: no tool calls and no end_turn — bailing")
                break

        logger.warning(f"Agent hit max_turns={max_turns} without a decision")
        return DEFAULT_DECISION

    except Exception as e:
        logger.warning(f"Agent decide_entry failed for {user_id[:8]}/{symbol}: {e}")
        return DEFAULT_DECISION


def _parse_decision(text: str) -> dict:
    """Best-effort parse of the agent's JSON decision."""
    text = text.strip()
    # Strip markdown fences if Claude added them despite instructions.
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()
    try:
        obj = json.loads(text)
        if obj.get("decision") not in ("enter", "skip"):
            return DEFAULT_DECISION
        return {
            "decision": obj["decision"],
            "reason": str(obj.get("reason", ""))[:200],
            "confidence": float(obj.get("confidence", 0.5)),
        }
    except Exception as e:
        logger.warning(f"Failed to parse agent decision: {text[:200]} — {e}")
        return DEFAULT_DECISION
