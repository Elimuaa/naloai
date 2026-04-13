"""
AI-powered pre-trade screening and regime classification using Claude.
Premium feature: Claude reviews every signal before entry.
"""
import os
import json
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Cache regime classification to avoid excessive API calls
_regime_cache: dict[str, tuple[str, float]] = {}  # user_id -> (regime, timestamp)
REGIME_CACHE_TTL = 300  # 5 minutes

# Pattern memory: store failure patterns per user
_pattern_memory: dict[str, list[dict]] = {}  # user_id -> list of pattern observations
MAX_PATTERNS = 50


async def screen_trade(
    user_id: str,
    side: str,
    current_price: float,
    z_score: float,
    indicators: dict,
    recent_trades: list[dict],
    regime: str,
    signal_strength: float,
) -> dict:
    """
    Claude pre-trade screening. Asks Claude whether to take the trade.
    Returns: {take: bool, confidence: float, reasoning: str, adjusted_sl: float|None, adjusted_tp: float|None}
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"take": True, "confidence": 0.5, "reasoning": "No API key — skipping AI screen"}

    # Build context for Claude
    recent_summary = ""
    if recent_trades:
        wins = sum(1 for t in recent_trades if t.get("pnl", 0) > 0)
        losses = len(recent_trades) - wins
        total_pnl = sum(t.get("pnl", 0) for t in recent_trades)
        recent_summary = f"Last {len(recent_trades)} trades: {wins}W/{losses}L, total P&L: ${total_pnl:.2f}"

    # Check pattern memory for known failure conditions
    patterns = _pattern_memory.get(user_id, [])
    pattern_warnings = []
    hour = datetime.now(timezone.utc).hour
    for p in patterns[-10:]:
        if p.get("side") == side and p.get("hour") == hour and p.get("outcome") == "loss":
            pattern_warnings.append(f"Historical loss pattern: {side} trades at {hour}:00 UTC tend to lose")

    ind_str = ", ".join(f"{k}={v}" for k, v in indicators.items() if isinstance(v, (int, float)))

    prompt = f"""You are a crypto trading advisor. Evaluate this trade signal and respond with JSON only.

Signal: {side.upper()} BTC at ${current_price:,.2f}
Z-score: {z_score:.3f}
Signal strength: {signal_strength:.0%}
Market regime: {regime}
Indicators: {ind_str}
{recent_summary}
{chr(10).join(pattern_warnings) if pattern_warnings else 'No pattern warnings.'}
Current hour (UTC): {hour}

Rules:
- In "ranging" regime, mean-reversion trades are good
- In "trending" regime, mean-reversion is dangerous — only take if signal strength > 80%
- After 3+ consecutive losses, be more conservative
- Avoid trades during 4-8 AM UTC (low volume)
- If indicators conflict with the signal, reduce confidence

Respond with ONLY this JSON:
{{"take": true/false, "confidence": 0.0-1.0, "reasoning": "one sentence", "adjusted_sl": null or float, "adjusted_tp": null or float}}"""

    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-6-20250514",
                    "max_tokens": 200,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            r.raise_for_status()
            raw = r.json()["content"][0]["text"].strip()
            # Strip markdown if present
            if "```" in raw:
                for part in raw.split("```")[1:]:
                    stripped = part.strip()
                    if stripped.startswith("json"):
                        stripped = stripped[4:].strip()
                    if stripped:
                        raw = stripped
                        break
            result = json.loads(raw)
            return {
                "take": bool(result.get("take", True)),
                "confidence": float(result.get("confidence", 0.5)),
                "reasoning": str(result.get("reasoning", "")),
                "adjusted_sl": result.get("adjusted_sl"),
                "adjusted_tp": result.get("adjusted_tp"),
            }
    except Exception as e:
        logger.debug(f"AI screen failed: {e}")
        return {"take": True, "confidence": 0.5, "reasoning": f"AI screen unavailable: {str(e)[:50]}"}


async def classify_regime(
    user_id: str,
    prices: list[float],
    indicators: dict,
) -> str:
    """
    Claude-powered market regime classification.
    Returns: 'trending_up', 'trending_down', 'ranging', or 'volatile'
    """
    # Check cache first
    now = time.time()
    if user_id in _regime_cache:
        cached_regime, cached_at = _regime_cache[user_id]
        if now - cached_at < REGIME_CACHE_TTL:
            return cached_regime

    # Quick heuristic fallback (no API needed)
    adx_val = indicators.get("adx")
    rsi_val = indicators.get("rsi")
    ema_50 = indicators.get("ema_50")
    current_price = prices[-1] if prices else 0

    # Heuristic regime detection
    regime = "ranging"
    if adx_val is not None and adx_val > 25:
        if ema_50 and current_price > ema_50:
            regime = "trending_up"
        elif ema_50 and current_price < ema_50:
            regime = "trending_down"
        else:
            regime = "trending_up" if rsi_val and rsi_val > 50 else "trending_down"
    elif adx_val is not None and adx_val > 40:
        regime = "volatile"

    # Try Claude for more nuanced classification (premium only)
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if api_key and len(prices) >= 50:
        try:
            # Calculate price change stats for Claude
            p50 = prices[-50:]
            pct_changes = [(p50[i] - p50[i-1]) / p50[i-1] * 100 for i in range(1, len(p50))]
            avg_change = sum(pct_changes) / len(pct_changes)
            max_up = max(pct_changes)
            max_down = min(pct_changes)
            volatility = (sum((c - avg_change) ** 2 for c in pct_changes) / len(pct_changes)) ** 0.5

            prompt = f"""Classify the current BTC market regime. Respond with ONE word only.

Stats (last 50 ticks):
- Price range: ${min(p50):,.0f} - ${max(p50):,.0f}
- Avg tick change: {avg_change:.4f}%
- Max up: +{max_up:.3f}%, Max down: {max_down:.3f}%
- Volatility (std): {volatility:.4f}%
- ADX: {adx_val}, RSI: {rsi_val}
- Price vs EMA-50: {'above' if ema_50 and current_price > ema_50 else 'below'}

Options: trending_up, trending_down, ranging, volatile
Answer:"""

            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-sonnet-4-6-20250514",
                        "max_tokens": 20,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                r.raise_for_status()
                ai_regime = r.json()["content"][0]["text"].strip().lower().replace(" ", "_")
                if ai_regime in ("trending_up", "trending_down", "ranging", "volatile"):
                    regime = ai_regime
        except Exception as e:
            logger.debug(f"AI regime classification failed, using heuristic: {e}")

    _regime_cache[user_id] = (regime, now)
    return regime


def record_pattern(user_id: str, trade_data: dict):
    """Record a trade pattern for Claude's pattern memory."""
    if user_id not in _pattern_memory:
        _pattern_memory[user_id] = []

    pattern = {
        "side": trade_data.get("side"),
        "hour": datetime.now(timezone.utc).hour,
        "weekday": datetime.now(timezone.utc).weekday(),
        "outcome": "win" if trade_data.get("pnl", 0) > 0 else "loss",
        "pnl": trade_data.get("pnl", 0),
        "exit_reason": trade_data.get("exit_reason"),
        "z_score": trade_data.get("z_score"),
        "regime": trade_data.get("regime"),
    }
    _pattern_memory[user_id].append(pattern)
    if len(_pattern_memory[user_id]) > MAX_PATTERNS:
        _pattern_memory[user_id] = _pattern_memory[user_id][-MAX_PATTERNS:]


def get_pattern_insights(user_id: str) -> dict:
    """Analyze stored patterns and return actionable insights."""
    patterns = _pattern_memory.get(user_id, [])
    if len(patterns) < 10:
        return {"sufficient_data": False}

    # Analyze by hour
    hour_stats: dict[int, dict] = {}
    for p in patterns:
        h = p.get("hour", 0)
        if h not in hour_stats:
            hour_stats[h] = {"wins": 0, "losses": 0, "total_pnl": 0}
        if p["outcome"] == "win":
            hour_stats[h]["wins"] += 1
        else:
            hour_stats[h]["losses"] += 1
        hour_stats[h]["total_pnl"] += p.get("pnl", 0)

    # Find worst hours
    bad_hours = []
    for h, stats in hour_stats.items():
        total = stats["wins"] + stats["losses"]
        if total >= 3 and stats["losses"] / total > 0.7:
            bad_hours.append(h)

    # Analyze by side
    side_stats: dict[str, dict] = {}
    for p in patterns:
        s = p.get("side", "unknown")
        if s not in side_stats:
            side_stats[s] = {"wins": 0, "losses": 0}
        if p["outcome"] == "win":
            side_stats[s]["wins"] += 1
        else:
            side_stats[s]["losses"] += 1

    return {
        "sufficient_data": True,
        "bad_hours": bad_hours,
        "side_stats": side_stats,
        "total_patterns": len(patterns),
    }
