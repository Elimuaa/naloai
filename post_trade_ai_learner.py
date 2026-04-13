import os
import json
import logging
from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)


def _get_client():
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None, None
    return AsyncAnthropic(api_key=key), key

SYSTEM_PROMPT = """You are a professional crypto trading analyst. Your job is to analyze closed trades and provide structured, actionable feedback.

Rules:
- Base ALL analysis strictly on the data provided. Never speculate beyond what the data shows.
- Be concise and specific — avoid generic advice.
- Grade scale: A (excellent), B (good), C (average), D (poor), F (failed)
- Entry quality: how well the entry aligned with the Z-score signal
- Exit quality: how well the exit matched stop/target logic
- Confidence 0.0-1.0: your confidence in this analysis given available data

IMPORTANT: Respond ONLY with valid JSON. No preamble, no markdown, no explanation outside the JSON."""


async def analyze_trade(trade_data: dict) -> dict:
    """
    Analyze a single closed trade and return structured AI feedback.
    trade_data should contain: symbol, side, entry_price, exit_price, pnl, pnl_pct,
    exit_reason, duration_minutes, entry_z_score, exit_z_score, market_conditions
    """
    client, api_key = _get_client()
    if not client:
        return _fallback_analysis()

    prompt = f"""Analyze this closed crypto trade:

Symbol: {trade_data.get('symbol', 'Unknown')}
Side: {trade_data.get('side', 'Unknown')}
Entry Price: ${trade_data.get('entry_price', 0)}
Exit Price: ${trade_data.get('exit_price', 0)}
P&L: ${trade_data.get('pnl', 0):.4f} ({trade_data.get('pnl_pct', 0):.2f}%)
Exit Reason: {trade_data.get('exit_reason', 'Unknown')}
Duration: {trade_data.get('duration_minutes', 0)} minutes
Z-Score at Entry: {trade_data.get('entry_z_score', 0):.2f}
Z-Score at Exit: {trade_data.get('exit_z_score', 0):.2f}

Return this exact JSON structure:
{{
  "grade": "A|B|C|D|F",
  "entry_quality": "one sentence",
  "exit_quality": "one sentence",
  "what_went_well": ["item1", "item2"],
  "what_went_wrong": ["item1"],
  "improvements": ["specific suggestion 1", "specific suggestion 2"],
  "confidence": 0.85
}}"""

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        # Strip markdown if present
        if "```" in raw:
            parts = raw.split("```")
            for part in parts[1:]:
                stripped = part.strip()
                if stripped.startswith("json"):
                    stripped = stripped[4:].strip()
                if stripped:
                    raw = stripped
                    break
        return json.loads(raw.strip())
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse AI response: {e}")
        return _fallback_analysis()
    except Exception as e:
        logger.error(f"AI analysis error: {e}")
        return _fallback_analysis()


async def generate_daily_report(user_id: str, trades: list[dict]) -> dict:
    """Generate a daily summary report from a list of closed trades."""
    client, _ = _get_client()
    if not client or not trades:
        return _fallback_daily_report(trades)

    wins = [t for t in trades if (t.get('pnl') or 0) > 0]
    losses = [t for t in trades if (t.get('pnl') or 0) <= 0]
    total_pnl = sum(t.get('pnl', 0) for t in trades)
    win_rate = len(wins) / len(trades) * 100 if trades else 0

    prompt = f"""Generate a daily trading report for {len(trades)} trades:

Total Trades: {len(trades)}
Wins: {len(wins)} | Losses: {len(losses)}
Win Rate: {win_rate:.1f}%
Total P&L: ${total_pnl:.4f}

Trade summaries: {json.dumps([{
    'symbol': t.get('symbol'),
    'side': t.get('side'),
    'pnl_pct': t.get('pnl_pct', 0),
    'exit_reason': t.get('exit_reason'),
    'ai_grade': t.get('ai_grade', 'N/A')
} for t in trades], indent=2)}

Return this exact JSON:
{{
  "summary": "2-3 sentence overview of the day",
  "top_improvement": "The single most important thing to improve tomorrow",
  "patterns_noticed": ["pattern1", "pattern2"],
  "risk_assessment": "low|medium|high",
  "recommendation": "continue|adjust_parameters|pause"
}}"""

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
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
        report_data = json.loads(raw.strip())
        report_data.update({
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "total_pnl": total_pnl,
            "win_rate": win_rate,
        })
        return report_data
    except Exception as e:
        logger.error(f"Daily report error: {e}")
        return _fallback_daily_report(trades)


def _fallback_analysis() -> dict:
    return {
        "grade": "N/A",
        "entry_quality": "Analysis unavailable",
        "exit_quality": "Analysis unavailable",
        "what_went_well": [],
        "what_went_wrong": [],
        "improvements": ["Configure Anthropic API key for AI analysis"],
        "confidence": 0.0
    }


def _fallback_daily_report(trades: list) -> dict:
    wins = [t for t in trades if (t.get('pnl') or 0) > 0]
    total_pnl = sum(t.get('pnl', 0) for t in trades)
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    return {
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(trades) - len(wins),
        "total_pnl": total_pnl,
        "win_rate": win_rate,
        "summary": f"Completed {len(trades)} trades with {win_rate:.1f}% win rate and ${total_pnl:.4f} total P&L.",
        "top_improvement": "Enable AI analysis by configuring ANTHROPIC_API_KEY",
        "patterns_noticed": [],
        "risk_assessment": "medium",
        "recommendation": "continue"
    }
