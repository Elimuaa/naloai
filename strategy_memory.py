"""
Strategy Memory — the system's permanent, bounded knowledge base.

PROBLEM
-------
Storing every individual trade for learning would balloon to millions of rows
within months at high trade volumes, and Claude can no longer fit recent
history in its context window.

SOLUTION
--------
Aggregate by bucket. Every trade falls into a unique combination of conditions:
    (symbol, side, hour_utc, regime, signal_strength_band, z_score_band)

We update running totals on the matching bucket — sample_count, total_pnl,
win_count, exit-reason counts. Storage grows ONLY when a new unique combination
appears; the table converges to a fixed size as buckets fill up.

Every trade — demo OR live — feeds the system. Demo prices, indicators,
regimes, and SL/TP/trail exits come from real markets; only the fill is
simulated. We tag the source so live can be weighted higher when needed,
but we never throw away the observation.

USAGE
-----
- After every trade close → record_strategy_outcome(trade_data)
- Before every trade entry → score_setup(...) returns expected_pnl, win_rate,
  confidence (sample_count). Block if win_rate < 35% with N >= 10 samples.
- For the calibrator → top_recipes(user_id) returns the user's best/worst
  buckets for Claude to inspect.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from sqlalchemy import select, update, and_, or_, func
from sqlalchemy.exc import IntegrityError
from database import AsyncSessionLocal, StrategyMemory

logger = logging.getLogger(__name__)


def _signal_strength_bucket(s: float) -> str:
    """Quantize signal strength (0.0-1.0) into discrete buckets."""
    if s is None:
        return "unknown"
    if s < 0.2:  return "0.0-0.2"
    if s < 0.4:  return "0.2-0.4"
    if s < 0.6:  return "0.4-0.6"
    if s < 0.7:  return "0.6-0.7"
    if s < 0.8:  return "0.7-0.8"
    if s < 0.9:  return "0.8-0.9"
    return "0.9-1.0"


def _z_bucket(z: float) -> str:
    """Quantize abs(z-score) into bands. Sign is captured by `side` already."""
    if z is None:
        return "unknown"
    a = abs(z)
    if a < 1.0:  return "0.0-1.0"
    if a < 1.5:  return "1.0-1.5"
    if a < 2.0:  return "1.5-2.0"
    if a < 2.5:  return "2.0-2.5"
    if a < 3.0:  return "2.5-3.0"
    return "3.0+"


async def record_strategy_outcome(
    user_id: str,
    symbol: str,
    side: str,
    hour_utc: int,
    regime: str,
    signal_strength: float,
    z_score: float,
    pnl: float,
    pnl_pct: float,
    duration_minutes: float,
    is_demo: bool,
    exit_reason: str,
):
    """Update aggregated bucket stats after a trade closes.

    Writes BOTH a per-user bucket and a global bucket (user_id=None) so
    premium users can query cross-user statistics for higher sample sizes.
    Idempotent under retries — uses INSERT-or-UPDATE pattern.
    """
    s_bucket = _signal_strength_bucket(signal_strength)
    z_b = _z_bucket(z_score)
    is_win = pnl > 0
    now = datetime.now(timezone.utc)

    # Upsert per-user AND global bucket (user_id NULL).
    # SEPARATE transactions per row so a race on one bucket can't roll back the other.
    # On IntegrityError (concurrent insert collision), we retry once as an update.
    for uid in (user_id, None):
        for attempt in (1, 2):
            try:
                async with AsyncSessionLocal() as db:
                    existing = await db.execute(
                        select(StrategyMemory).where(
                            StrategyMemory.user_id.is_(None) if uid is None else StrategyMemory.user_id == uid,
                            StrategyMemory.symbol == symbol,
                            StrategyMemory.side == side,
                            StrategyMemory.hour_utc == hour_utc,
                            StrategyMemory.regime == regime,
                            StrategyMemory.signal_strength_bucket == s_bucket,
                            StrategyMemory.z_bucket == z_b,
                        )
                    )
                    row = existing.scalar_one_or_none()

                    if row:
                        row.sample_count += 1
                        row.win_count += 1 if is_win else 0
                        row.loss_count += 0 if is_win else 1
                        row.total_pnl += pnl
                        row.total_pnl_pct += pnl_pct
                        row.total_duration_minutes += duration_minutes
                        if is_demo:
                            row.demo_count += 1
                        else:
                            row.live_count += 1
                        if exit_reason == "take_profit":
                            row.tp_count += 1
                        elif exit_reason == "trailing_stop":
                            row.trail_count += 1
                        elif exit_reason == "stop_loss":
                            row.sl_count += 1
                        elif exit_reason in ("z_reverted", "time_limit"):
                            row.zrevert_count += 1
                        row.last_updated = now
                    else:
                        row = StrategyMemory(
                            user_id=uid,
                            symbol=symbol,
                            side=side,
                            hour_utc=hour_utc,
                            regime=regime,
                            signal_strength_bucket=s_bucket,
                            z_bucket=z_b,
                            sample_count=1,
                            win_count=1 if is_win else 0,
                            loss_count=0 if is_win else 1,
                            total_pnl=pnl,
                            total_pnl_pct=pnl_pct,
                            total_duration_minutes=duration_minutes,
                            live_count=0 if is_demo else 1,
                            demo_count=1 if is_demo else 0,
                            tp_count=1 if exit_reason == "take_profit" else 0,
                            trail_count=1 if exit_reason == "trailing_stop" else 0,
                            sl_count=1 if exit_reason == "stop_loss" else 0,
                            zrevert_count=1 if exit_reason in ("z_reverted", "time_limit") else 0,
                            first_seen=now,
                            last_updated=now,
                        )
                        db.add(row)

                    await db.commit()
                break  # success
            except IntegrityError:
                # Concurrent INSERT race — on retry, the row exists, we'll take the UPDATE branch
                if attempt == 2:
                    logger.error(
                        f"Strategy memory persistent race for user={uid} symbol={symbol} "
                        f"side={side} hr={hour_utc} — sample LOST"
                    )
            except Exception as e:
                logger.error(
                    f"Strategy memory write failed for user={uid} symbol={symbol}: {e}",
                    exc_info=True,
                )
                break


async def score_setup(
    user_id: str,
    symbol: str,
    side: str,
    hour_utc: int,
    regime: str,
    signal_strength: float,
    z_score: float,
    min_samples: int = 10,
) -> dict:
    """Look up the user's bucket stats for this setup. Falls back to global
    stats if the user lacks samples. Returns:
        {
          win_rate: float (0.0-1.0),
          avg_pnl: float,
          sample_count: int,
          confidence: "high" | "medium" | "low" | "none",
          source: "user" | "global" | "none",
          recommendation: "take" | "skip" | "neutral",
          reason: str,
        }
    """
    s_bucket = _signal_strength_bucket(signal_strength)
    z_b = _z_bucket(z_score)

    async with AsyncSessionLocal() as db:
        # 1) Try user-specific bucket first — captures personal patterns
        user_row = (await db.execute(
            select(StrategyMemory).where(
                StrategyMemory.user_id == user_id,
                StrategyMemory.symbol == symbol,
                StrategyMemory.side == side,
                StrategyMemory.hour_utc == hour_utc,
                StrategyMemory.regime == regime,
                StrategyMemory.signal_strength_bucket == s_bucket,
                StrategyMemory.z_bucket == z_b,
            )
        )).scalar_one_or_none()

        chosen_row = None
        source = "none"
        if user_row and user_row.sample_count >= min_samples:
            chosen_row = user_row
            source = "user"
        else:
            # 2) Fall back to global cross-user stats — higher N
            global_row = (await db.execute(
                select(StrategyMemory).where(
                    StrategyMemory.user_id.is_(None),
                    StrategyMemory.symbol == symbol,
                    StrategyMemory.side == side,
                    StrategyMemory.hour_utc == hour_utc,
                    StrategyMemory.regime == regime,
                    StrategyMemory.signal_strength_bucket == s_bucket,
                    StrategyMemory.z_bucket == z_b,
                )
            )).scalar_one_or_none()
            if global_row and global_row.sample_count >= min_samples:
                chosen_row = global_row
                source = "global"

    if chosen_row is None:
        return {
            "win_rate": None,
            "avg_pnl": None,
            "sample_count": 0,
            "confidence": "none",
            "source": "none",
            "recommendation": "neutral",
            "reason": "No prior data for this setup — let signal filters decide.",
        }

    n = chosen_row.sample_count
    wr = chosen_row.win_count / n if n > 0 else 0.0
    avg_pnl = chosen_row.total_pnl / n if n > 0 else 0.0

    # Confidence by sample size
    if n >= 50:
        conf = "high"
    elif n >= 20:
        conf = "medium"
    else:
        conf = "low"

    # Recommendation: hard block on known-bad, green-light on known-good
    if n >= min_samples and wr < 0.35 and avg_pnl < 0:
        rec = "skip"
        reason = (
            f"Bucket history is bad: {wr:.0%} win rate, avg ${avg_pnl:+.2f} over "
            f"{n} samples ({source})."
        )
    elif n >= min_samples and wr >= 0.60 and avg_pnl > 0:
        rec = "take"
        reason = (
            f"Bucket history is strong: {wr:.0%} win rate, avg ${avg_pnl:+.2f} over "
            f"{n} samples ({source})."
        )
    else:
        rec = "neutral"
        reason = (
            f"Bucket history is mixed: {wr:.0%} win rate, avg ${avg_pnl:+.2f} over "
            f"{n} samples ({source})."
        )

    return {
        "win_rate": round(wr, 3),
        "avg_pnl": round(avg_pnl, 2),
        "sample_count": n,
        "confidence": conf,
        "source": source,
        "recommendation": rec,
        "reason": reason,
    }


async def top_recipes(user_id: Optional[str], n: int = 5, min_samples: int = 10) -> dict:
    """Return the most profitable and least profitable buckets for context.

    Used by the calibrator (in its prompt) and the dashboard. If user_id is
    given, returns user-specific recipes. If None, returns global cross-user.
    """
    async with AsyncSessionLocal() as db:
        base = select(StrategyMemory).where(
            StrategyMemory.sample_count >= min_samples,
        )
        if user_id is None:
            base = base.where(StrategyMemory.user_id.is_(None))
        else:
            base = base.where(StrategyMemory.user_id == user_id)

        # Best by avg PnL per trade
        best = (await db.execute(
            base.order_by((StrategyMemory.total_pnl / StrategyMemory.sample_count).desc()).limit(n)
        )).scalars().all()

        worst = (await db.execute(
            base.order_by((StrategyMemory.total_pnl / StrategyMemory.sample_count).asc()).limit(n)
        )).scalars().all()

    def _summarize(r: StrategyMemory) -> dict:
        wr = r.win_count / r.sample_count if r.sample_count else 0
        avg = r.total_pnl / r.sample_count if r.sample_count else 0
        return {
            "symbol": r.symbol,
            "side": r.side,
            "hour_utc": r.hour_utc,
            "regime": r.regime,
            "signal_strength": r.signal_strength_bucket,
            "z_bucket": r.z_bucket,
            "samples": r.sample_count,
            "win_rate": round(wr, 3),
            "avg_pnl": round(avg, 2),
            "total_pnl": round(r.total_pnl, 2),
            "live_share": round(r.live_count / r.sample_count, 2) if r.sample_count else 0,
        }

    return {
        "best": [_summarize(r) for r in best],
        "worst": [_summarize(r) for r in worst],
    }


async def memory_stats(user_id: Optional[str] = None) -> dict:
    """Return aggregate stats about the knowledge base — for the dashboard."""
    async with AsyncSessionLocal() as db:
        q = select(
            func.count(StrategyMemory.id),
            func.sum(StrategyMemory.sample_count),
            func.sum(StrategyMemory.win_count),
            func.sum(StrategyMemory.total_pnl),
        )
        if user_id is None:
            q = q.where(StrategyMemory.user_id.is_(None))
        else:
            q = q.where(StrategyMemory.user_id == user_id)

        row = (await db.execute(q)).first()
        n_buckets, total_samples, total_wins, total_pnl = row or (0, 0, 0, 0)
        n_buckets = n_buckets or 0
        total_samples = total_samples or 0
        total_wins = total_wins or 0
        total_pnl = total_pnl or 0.0

    wr = (total_wins / total_samples) if total_samples else 0
    return {
        "buckets_known": n_buckets,
        "trades_observed": total_samples,
        "overall_win_rate": round(wr, 3),
        "overall_pnl": round(total_pnl, 2),
        "memory_bytes_estimate": n_buckets * 256,  # rough — each row ~256 bytes
    }


async def prune_stale(days: int = 180):
    """Optional janitor: remove buckets unseen in N days with low sample count.

    Buckets with >= 50 samples are kept forever — that's hard-won knowledge.
    Only fast-decaying low-sample buckets are pruned to keep the table tidy
    if market conditions shift dramatically. Safe to never call.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    async with AsyncSessionLocal() as db:
        from sqlalchemy import delete
        result = await db.execute(
            delete(StrategyMemory).where(
                StrategyMemory.last_updated < cutoff,
                StrategyMemory.sample_count < 50,
            )
        )
        await db.commit()
        return result.rowcount or 0
