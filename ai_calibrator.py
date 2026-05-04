"""
AI Auto-Calibrator for Nalo.Ai Premium.
Analyzes trade history after each closed trade and adjusts strategy parameters
to improve future profitability. Uses Claude to identify patterns and recommend
parameter changes, then applies them automatically.
"""

import os
import json
import logging
from datetime import datetime, timezone
from anthropic import AsyncAnthropic
from database import AsyncSessionLocal, User, Trade, CalibrationLog
from sqlalchemy import select, update

logger = logging.getLogger(__name__)


def _get_client():
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    return AsyncAnthropic(api_key=key)


# Parameter bounds — prevents the AI from setting dangerous values
PARAM_BOUNDS = {
    "entry_z": (1.2, 3.5),
    "lookback": (10, 60),
    "stop_loss_pct": (0.01, 0.08),
    "take_profit_pct": (0.02, 0.15),
    "trail_stop_pct": (0.005, 0.04),
    "risk_per_trade_pct": (0.5, 3.0),
    "max_exposure_pct": (5.0, 30.0),
    "cooldown_ticks": (2, 15),
}

CALIBRATION_PROMPT = """You are Nalo.Ai's auto-calibration engine. Your job is to analyze recent trade history AND the system's permanent strategy memory to recommend parameter adjustments that improve future profitability.

CURRENT PARAMETERS:
{current_params}

RECENT TRADE HISTORY (last {trade_count} trades):
{trade_history}

PERFORMANCE SUMMARY:
- Total trades: {total_trades}
- Wins: {wins} | Losses: {losses}
- Win rate: {win_rate:.1f}%
- Total P&L: ${total_pnl:.4f}
- Average P&L per trade: ${avg_pnl:.4f}
- Avg winning trade: ${avg_win:.4f}
- Avg losing trade: ${avg_loss:.4f}
- Largest win: ${best_trade:.4f}
- Largest loss: ${worst_trade:.4f}
- Stop loss exits: {stop_losses}
- Take profit exits: {take_profits}
- Trailing stop exits: {trailing_stops}

PERMANENT STRATEGY MEMORY (aggregated across ALL trades this user has ever made):
The system bucketed every closed trade by (symbol/side/hour/regime/signal_strength/z_band) and tracked win-rate + avg P&L per bucket. These are the user's HIGHEST-EDGE and LOWEST-EDGE setups:

TOP 5 PROFITABLE BUCKETS (keep doing these):
{best_recipes}

BOTTOM 5 LOSING BUCKETS (avoid or filter these):
{worst_recipes}

When choosing parameters, consider:
- If losing buckets cluster in a hour, suggest tightening dead_zone via a comment.
- If winning buckets fire at high z-scores (≥2.0), it's safe to RAISE entry_z.
- If losing buckets fire at low z-scores (<1.5), entry_z is too low — raise it.
- If best buckets exit on take_profit consistently, current TP is well-tuned.
- If best buckets exit on trailing_stop, the runner is profitable — widen TP further.

PARAMETER BOUNDS (you MUST stay within these):
{param_bounds}

RULES:
1. Only suggest changes that are supported by the data. Don't change parameters that are already performing well.
2. Make incremental adjustments — never change more than 3 parameters at once.
3. Each change must have a data-driven reason based on the trade history above.
4. If the current strategy is already performing well (win rate > 55% and positive P&L), make minimal or no changes.
5. Focus on the weakest aspect: if stop losses trigger too often, adjust SL%. If trades miss profits, adjust TP%.
6. All values MUST be within the parameter bounds.

Return ONLY valid JSON in this exact format:
{{
  "changes": {{
    "param_name": {{
      "old": <current_value>,
      "new": <recommended_value>,
      "reason": "one sentence explanation based on the data"
    }}
  }},
  "reasoning": "2-3 sentence summary of what the data shows and why these changes should help",
  "projected_impact": "one sentence on expected improvement",
  "confidence": 0.0-1.0
}}

If no changes are needed, return:
{{
  "changes": {{}},
  "reasoning": "explanation of why current params are good",
  "projected_impact": "maintain current performance",
  "confidence": 0.9
}}"""


async def calibrate_after_trade(user_id: str) -> dict | None:
    """
    Analyze the user's recent trades and auto-adjust parameters for better performance.
    Called after every trade close for premium users.
    Returns the calibration result or None if not applicable.
    """
    client = _get_client()
    if not client:
        logger.warning("No Anthropic API key — skipping calibration")
        return None

    async with AsyncSessionLocal() as db:
        # Get user
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user or not user.is_premium:
            return None

        # Demo and live BOTH count. Prices, indicators, regimes, and SL/TP/trail
        # exits are computed from the real live market — only the fill is simulated.
        # Treating demo trades as "not real learning" throws away thousands of valid
        # market observations. Strategy memory tags origin separately for queries
        # that need to weight live higher.
        from sqlalchemy import func as _f
        total_closed = (await db.execute(
            select(_f.count(Trade.id)).where(
                Trade.user_id == user_id,
                Trade.state == "closed",
            )
        )).scalar() or 0

        # Get last 30 closed trades (demo + live)
        result = await db.execute(
            select(Trade)
            .where(Trade.user_id == user_id, Trade.state == "closed")
            .order_by(Trade.closed_at.desc())
            .limit(30)
        )
        trades = result.scalars().all()

    # Over-fit guard: need 20+ trades minimum (was 5 — caused calibrator to drift on tiny samples).
    # Pro users with 14-16 trades were getting calibrated 18× and drifting AWAY from optimal.
    if total_closed < 20:
        logger.info(f"Calibration deferred for user {user_id}: {total_closed}/20 trades min")
        return None

    # Throttle: only recalibrate every 10 trades, not after every close.
    # 14 trades × 18 calibrations was over-fitting; now max 1 per 10 trades.
    if total_closed % 10 != 0:
        logger.info(f"Calibration throttled: only fires every 10th trade ({total_closed} total)")
        return None

    # Build trade history for the prompt
    trade_data = []
    wins = []
    losses = []
    stop_losses = 0
    take_profits = 0
    trailing_stops = 0

    for t in trades:
        pnl = t.pnl or 0
        entry = float(t.entry_price) if t.entry_price else 0
        exit_p = float(t.exit_price) if t.exit_price else 0
        duration = ""
        if t.opened_at and t.closed_at:
            mins = (t.closed_at - t.opened_at).total_seconds() / 60
            duration = f"{mins:.0f}min"

        trade_data.append({
            "side": t.side,
            "entry": entry,
            "exit": exit_p,
            "pnl": round(pnl, 6),
            "pnl_pct": round(t.pnl_pct or 0, 4),
            "exit_reason": t.exit_reason,
            "duration": duration,
            "is_demo": t.is_demo,
        })

        if pnl > 0:
            wins.append(pnl)
        else:
            losses.append(pnl)

        if t.exit_reason == "stop_loss":
            stop_losses += 1
        elif t.exit_reason == "take_profit":
            take_profits += 1
        elif t.exit_reason == "trailing_stop":
            trailing_stops += 1

    total_pnl = sum(t.pnl or 0 for t in trades)
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    avg_pnl = total_pnl / len(trades) if trades else 0
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    best_trade = max(wins) if wins else 0
    worst_trade = min(losses) if losses else 0

    current_params = {
        "entry_z": user.entry_z,
        "lookback": int(user.lookback),
        "stop_loss_pct": user.stop_loss_pct,
        "take_profit_pct": user.take_profit_pct,
        "trail_stop_pct": user.trail_stop_pct,
        "risk_per_trade_pct": getattr(user, 'risk_per_trade_pct', 1.0) or 1.0,
        "max_exposure_pct": getattr(user, 'max_exposure_pct', 20.0) or 20.0,
        "cooldown_ticks": getattr(user, 'cooldown_ticks', 5) or 5,
    }

    # Pull aggregated bucket stats from the permanent strategy memory.
    # This is THE knowledge base — every trade this user has ever made,
    # condensed into win-rate per condition. Far richer than 30 raw trades.
    try:
        from strategy_memory import top_recipes
        recipes = await top_recipes(user_id, n=5, min_samples=10)
        # Fall back to global cross-user stats if user is too new for own buckets
        if not recipes["best"] and not recipes["worst"]:
            recipes = await top_recipes(None, n=5, min_samples=10)
    except Exception as _e:
        logger.debug(f"Strategy memory unavailable for calibration: {_e}")
        recipes = {"best": [], "worst": []}

    prompt = CALIBRATION_PROMPT.format(
        current_params=json.dumps(current_params, indent=2),
        trade_count=len(trades),
        trade_history=json.dumps(trade_data[:20], indent=2),  # Last 20 for context window
        total_trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        win_rate=win_rate,
        total_pnl=total_pnl,
        avg_pnl=avg_pnl,
        avg_win=avg_win,
        avg_loss=avg_loss,
        best_trade=best_trade,
        worst_trade=worst_trade,
        stop_losses=stop_losses,
        take_profits=take_profits,
        trailing_stops=trailing_stops,
        best_recipes=json.dumps(recipes["best"], indent=2) if recipes["best"] else "(insufficient data — need 10+ trades per bucket)",
        worst_recipes=json.dumps(recipes["worst"], indent=2) if recipes["worst"] else "(insufficient data)",
        param_bounds=json.dumps({k: {"min": v[0], "max": v[1]} for k, v in PARAM_BOUNDS.items()}, indent=2),
    )

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system="You are a trading strategy calibration AI. Respond ONLY with valid JSON. No preamble.",
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        if "```" in raw:
            parts = raw.split("```")
            for part in parts[1:]:
                stripped = part.strip()
                if stripped.startswith("json"):
                    stripped = stripped[4:].strip()
                if stripped:
                    raw = stripped
                    break
        result = json.loads(raw.strip())
    except Exception as e:
        logger.error(f"AI calibration failed: {e}")
        return None

    changes = result.get("changes", {})
    if not changes:
        logger.info(f"AI calibration: no changes needed for user {user_id}")
        # Still log it
        async with AsyncSessionLocal() as db:
            log = CalibrationLog(
                user_id=user_id,
                param_changes=json.dumps({}),
                trade_count_analyzed=len(trades),
                win_rate_before=win_rate,
                projected_improvement=result.get("projected_impact", ""),
                ai_reasoning=result.get("reasoning", ""),
            )
            db.add(log)
            await db.execute(
                update(User).where(User.id == user_id).values(
                    last_calibration_at=datetime.now(timezone.utc)
                )
            )
            await db.commit()
        return result

    # Validate and apply changes
    validated_changes = {}
    db_updates = {}

    for param, change in changes.items():
        if param not in PARAM_BOUNDS:
            logger.warning(f"AI suggested unknown param: {param}, skipping")
            continue

        new_val = change.get("new")
        if new_val is None:
            continue

        low, high = PARAM_BOUNDS[param]
        # Clamp to bounds
        if param == "lookback":
            new_val = int(max(low, min(high, new_val)))
        elif param == "cooldown_ticks":
            new_val = int(max(low, min(high, new_val)))
        else:
            new_val = round(max(low, min(high, float(new_val))), 4)

        old_val = current_params.get(param)
        if old_val == new_val:
            continue

        validated_changes[param] = {
            "old": old_val,
            "new": new_val,
            "reason": change.get("reason", "AI-recommended adjustment"),
        }

        # Map to DB column (lookback stored as string in DB)
        if param == "lookback":
            db_updates["lookback"] = str(int(new_val))
        else:
            db_updates[param] = new_val

    if not db_updates:
        logger.info(f"No valid parameter changes after validation for user {user_id}")
        return result

    # Apply to database
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(User).where(User.id == user_id).values(
                **db_updates,
                calibration_count=User.calibration_count + 1,
                last_calibration_at=datetime.now(timezone.utc),
            )
        )

        log = CalibrationLog(
            user_id=user_id,
            param_changes=json.dumps(validated_changes),
            trade_count_analyzed=len(trades),
            win_rate_before=win_rate,
            projected_improvement=result.get("projected_impact", ""),
            ai_reasoning=result.get("reasoning", ""),
        )
        db.add(log)
        await db.commit()

    # Update in-memory risk manager if applicable
    from bot_engine import _risk_managers
    if user_id in _risk_managers:
        rm = _risk_managers[user_id]
        if "risk_per_trade_pct" in db_updates:
            rm.risk_per_trade_pct = db_updates["risk_per_trade_pct"]
        if "max_exposure_pct" in db_updates:
            rm.max_exposure_pct = db_updates["max_exposure_pct"]
        if "cooldown_ticks" in db_updates:
            rm.cooldown_ticks = db_updates["cooldown_ticks"]

    logger.info(f"AI calibration applied for user {user_id}: {list(validated_changes.keys())}")
    result["applied_changes"] = validated_changes
    return result


async def get_calibration_history(user_id: str, limit: int = 10) -> list[dict]:
    """Get recent calibration logs for a user."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(CalibrationLog)
            .where(CalibrationLog.user_id == user_id)
            .order_by(CalibrationLog.created_at.desc())
            .limit(limit)
        )
        logs = result.scalars().all()

    return [
        {
            "id": log.id,
            "created_at": log.created_at.isoformat() if log.created_at else None,
            "param_changes": json.loads(log.param_changes) if log.param_changes else {},
            "trade_count_analyzed": log.trade_count_analyzed,
            "win_rate_before": log.win_rate_before,
            "projected_improvement": log.projected_improvement,
            "ai_reasoning": log.ai_reasoning,
        }
        for log in logs
    ]
