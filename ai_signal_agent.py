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
- get_instrument_profile(symbol): asset class, expected behaviour, ideal conditions, hostile conditions — READ THIS FIRST
- get_indicators(symbol): RSI, ADX, BBands %B, MACD, EMA, regime, slow-z
- get_recent_trades(symbol, limit): the last N closed trades for this symbol with side, pnl, exit_reason
- get_strategy_memory(symbol, side, hour_utc): historical win rate for this exact bucket
- get_market_context(symbol): current price, current UTC hour, dead-zone status, recent volatility

INSTRUMENT-SPECIFIC GUIDANCE — apply different reasoning per asset class:

CRYPTO (BTC-USD, SOL-USD, DOGE-USD, ETH-USD):
- 24/7 market; mean-reversion strategy works well
- Be moderately aggressive — z ≥ 1.3 with confluence is enough to enter
- Watch for unusual ADX > 40 (suggests news/whale event → SKIP, momentum will overwhelm)
- Volume thin during dead zones (1, 11, 13, 18 UTC) — be extra selective
- Recent losing streak (3+ in a row) on the same side suggests regime change → SKIP
- For SELL signals: prefer when RSI > 70 AND BB %B > 0.85 (real exhaustion)
- For BUY signals: prefer when RSI < 35 AND BB %B < 0.15

GOLD:
- Mean-reverting commodity, lower volatility than crypto (~0.3-0.6%/day ATR)
- Be MORE selective — require z ≥ 1.5 AND at least one indicator confirmation
- Best hours: 8-10 UTC (London open) and 13-15 UTC (NY open) — better fills, more volume
- Hostile: strong DXY moves, geopolitical news days — skip if regime shows strong trend
- For BUY signals: weak retest signal at z=1.3 should be SKIPPED on GOLD (too noisy)
- Sample size matters more than crypto — require ≥ 8 trades in the bucket for memory to influence

US100 (NASDAQ-100):
- TRENDING index, not mean-reverting — apply reversion logic carefully
- Strong trend (ADX 25-40) is NORMAL for US100, do NOT reflexively reject like you would on GOLD
- For BUY signals: STRONGLY prefer when slow-z is also positive (trend continuation > reversion)
- For SELL signals: only on very strong z (≥ 1.8) AND conflicting indicators (RSI > 75 + BB%B > 0.95)
- HARD SKIP outside 14:00-19:59 UTC (no liquidity, signals are noise)
- ETH-USD-style mean-reversion failure mode applies here — if pattern memory shows < 40% on SELL signals, skip almost all SELL setups

GENERAL DECISION CRITERIA (apply ALONGSIDE the above):
- Skip if historical win rate for this exact bucket is < 40% AND sample size > 10
- Skip if recent trades on this symbol are 3+ losses in a row
- ENTER if z-score is strong (>= 1.5) AND no strong conflicting signal (and asset-class checks above pass)
- ENTER if pattern memory shows >= 55% win rate with sample size >= 5 (and asset-class checks above pass)

CRITICAL: start every decision by calling get_instrument_profile(symbol). The profile tells you which rules above apply. Then 2-3 more tool calls maximum.

Respond with ONLY a JSON object on your final turn:
{
  "decision": "enter" or "skip",
  "reason": "one sentence explaining why, mentioning the asset class consideration that drove it",
  "confidence": 0.0 to 1.0
}
No prose around the JSON. No code blocks. Just the JSON."""


# ── Tool definitions (the JSON-schema the agent sees) ─────────────────────────
TOOLS = [
    {
        "name": "get_instrument_profile",
        "description": "Return the instrument's asset-class profile: behaviour, ideal conditions, hostile conditions, suggested entry_z. CALL THIS FIRST — it tells you which decision rules apply.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    },
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


# ── Static instrument profiles — pure data, no DB lookup needed ──────────────
# Hand-authored from the strategy notes in INSTRUMENT_OVERRIDES + the per-asset
# guidance in the system prompt. Returning structured data (not free text) lets
# the agent reason cleanly about which rules apply.
_INSTRUMENT_PROFILES = {
    # ── Crypto ──
    "BTC-USD": {
        "asset_class": "crypto",
        "behaviour": "mean-reverting, 24/7 trading, occasionally trending on news",
        "ideal_conditions": "ranging market (ADX 15-25), z >= 1.3 with RSI/BB confluence",
        "hostile_conditions": "ADX > 40 (news/whale event), dead zones (1,11,13,18 UTC), >3 recent losses",
        "suggested_entry_z": 1.3,
        "sl_pct": 0.005, "tp_pct": 0.02,
        "session_window_utc": "24/7 except dead zones {1,11,13,18}",
        "notes": "Most-active symbol on the platform. Be selective during low-volume hours.",
    },
    "ETH-USD": {
        "asset_class": "crypto",
        "behaviour": "mean-reversion premise has FAILED historically (0/28 z-reverts in audit)",
        "ideal_conditions": "almost none — entries are blocked at the bot level (NO_NEW_ENTRY_SYMBOLS)",
        "hostile_conditions": "always — z-reversion doesn't fire on this symbol",
        "suggested_entry_z": 9.99,
        "sl_pct": 0.005, "tp_pct": 0.02,
        "session_window_utc": "blocked",
        "notes": "If you're seeing a candidate for ETH-USD, SKIP. Existing positions still get managed to close.",
    },
    "SOL-USD": {
        "asset_class": "crypto",
        "behaviour": "mean-reverting, more volatile than BTC, news-sensitive",
        "ideal_conditions": "ranging market, z >= 1.4, RSI confluence",
        "hostile_conditions": "ADX > 40, breakout days, low volume hours",
        "suggested_entry_z": 1.3,
        "sl_pct": 0.005, "tp_pct": 0.02,
        "session_window_utc": "24/7 except dead zones {1,11,13,18}",
        "notes": "Has produced the most wins in recent sessions. Treat like BTC but tighter on confirmation.",
    },
    "DOGE-USD": {
        "asset_class": "crypto",
        "behaviour": "mean-reverting, EXTREMELY volatile, high ADX days are common",
        "ideal_conditions": "low ADX (< 25), z >= 1.3, no recent meme-trigger news",
        "hostile_conditions": "ADX > 30 (DOGE rallies are violent), >2 recent losses",
        "suggested_entry_z": 1.3,
        "sl_pct": 0.005, "tp_pct": 0.02,
        "session_window_utc": "24/7 except dead zones {1,11,13,18}",
        "notes": "Lowest-priced asset (~$0.10) → position sizes are 70k+ units. Slippage matters more.",
    },
    # ── Commodities ──
    "GOLD": {
        "asset_class": "commodity",
        "behaviour": "mean-reverting, low volatility (0.3-0.6%/day ATR), session-sensitive",
        "ideal_conditions": "z >= 1.5 + RSI/BB confirmation, 8-10 UTC (London) or 13-15 UTC (NY)",
        "hostile_conditions": "strong DXY move days, geopolitical news, ADX > 25 (rare but bad)",
        "suggested_entry_z": 1.7,
        "sl_pct": 0.004, "tp_pct": 0.014,
        "session_window_utc": "Sun 21 UTC – Fri 21 UTC (no dead zones inside)",
        "notes": "Best money-maker per trade on the platform when conditions align. Be selective.",
    },
    # ── Indices ──
    "US100": {
        "asset_class": "index",
        "behaviour": "TRENDING (not mean-reverting), 0.8-1.8% daily ATR, momentum-driven",
        "ideal_conditions": "BUY signals with slow-z positive (trend continuation), 14-19 UTC only",
        "hostile_conditions": "SELL signals during US rallies, Fed days, NFP days, outside 14:00-19:59 UTC",
        "suggested_entry_z": 1.2,
        "sl_pct": 0.009, "tp_pct": 0.030,
        "session_window_utc": "14:00 - 19:59 UTC only (HARD constraint)",
        "notes": "Mean-reversion logic backfires here. Strongly prefer BUYs in uptrends, treat SELLs with extreme skepticism.",
    },
}


async def _tool_get_instrument_profile(symbol: str) -> dict:
    """Return the hand-authored asset-class profile for this symbol."""
    profile = _INSTRUMENT_PROFILES.get(symbol)
    if not profile:
        # Fallback — synthesize a minimal profile from broker mapping
        from bot_engine import _broker_for_symbol
        broker = _broker_for_symbol(symbol)
        return {
            "asset_class": "unknown",
            "behaviour": f"no specific profile — falls back to default {broker} strategy",
            "suggested_entry_z": 1.3,
            "notes": "No tailored guidance available. Apply general decision criteria from the system prompt.",
        }
    return profile


async def _dispatch_tool(name: str, tool_input: dict, user_id: str) -> str:
    """Run a single tool and return its JSON-serialised result."""
    try:
        if name == "get_instrument_profile":
            result = await _tool_get_instrument_profile(tool_input["symbol"])
        elif name == "get_indicators":
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

# Fallback decision when the agent can't be reached (API down, credits depleted,
# network error, parse failure). We FAIL OPEN — return "enter" so the trade
# proceeds based on the hardcoded filters that already approved it. The agent
# is a SECOND-LAYER veto, not a required gate; agent unavailability shouldn't
# punish trade volume. The reason string is preserved so the operator can see
# WHY the agent didn't weigh in, and the confidence is set to 0.0 so downstream
# position-sizing logic doesn't size up on an un-vetted entry.
DEFAULT_DECISION = {
    "decision": "enter",
    "reason": "agent unavailable — falling back to hardcoded decision",
    "confidence": 0.0,
}


# Process-wide backoff: when the Anthropic API rejects us (credits depleted,
# rate-limited), suppress all agent calls for a cooldown window. Without this,
# the bot hammers the API every poll-tick across all 6 symbols, burning logs
# and CPU on doomed requests. Once the operator tops up credits, the next call
# after the cooldown will succeed and the agent resumes normal operation.
import time as _time
_AGENT_BLOCKED_UNTIL: float = 0.0
_AGENT_LAST_BLOCK_REASON: str = ""


async def decide_entry(
    user_id: str,
    symbol: str,
    side: str,
    z_score: float,
    entry_price: float,
    max_turns: int = 8,   # 5 tools + final → allow some headroom
) -> dict:
    """Ask the agent whether to take this candidate entry.

    Returns {decision: 'enter'|'skip', reason: str, confidence: float}.
    FAILS OPEN — on any error returns the DEFAULT_DECISION (enter) so the
    hardcoded-filter decision (which had already passed) stands. The agent
    is a SECOND-LAYER veto, not a required gate.
    """
    global _AGENT_BLOCKED_UNTIL, _AGENT_LAST_BLOCK_REASON
    now = _time.time()
    if now < _AGENT_BLOCKED_UNTIL:
        wait_left = int(_AGENT_BLOCKED_UNTIL - now)
        return {
            "decision": "enter",
            "reason": f"agent in cooldown ({wait_left}s) — {_AGENT_LAST_BLOCK_REASON}",
            "confidence": 0.0,
        }

    client = _get_client()
    if client is None:
        logger.debug("No ANTHROPIC_API_KEY set — agent fail-open (hardcoded decision stands)")
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
        err_str = str(e)
        # Set cooldown on hard errors that won't resolve on retry:
        #   - credit_balance_too_low (billing issue, operator action required)
        #   - rate_limit_error (429 — wait for the limit window)
        # Other transient errors (network, parse) get a shorter retry-soon path.
        if "credit balance is too low" in err_str.lower():
            _AGENT_BLOCKED_UNTIL = _time.time() + 300.0  # 5 min — wait for operator top-up
            _AGENT_LAST_BLOCK_REASON = "Anthropic credits depleted"
            logger.warning(f"Agent backoff 5min: credits depleted ({user_id[:8]}/{symbol})")
        elif "rate_limit" in err_str.lower() or "429" in err_str:
            _AGENT_BLOCKED_UNTIL = _time.time() + 60.0   # 1 min
            _AGENT_LAST_BLOCK_REASON = "Anthropic rate-limited"
            logger.warning(f"Agent backoff 60s: rate-limited ({user_id[:8]}/{symbol})")
        else:
            logger.warning(f"Agent decide_entry failed for {user_id[:8]}/{symbol}: {err_str[:200]}")
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
