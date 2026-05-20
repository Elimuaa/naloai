"""
startup_tasks.py — All one-time startup operations run inside FastAPI lifespan.

Extracted from main.py to keep the app entry-point clean and readable.
Runs in order on every deploy:

  1. apply_profit_params()   — scalp-mode defaults for uncalibrated users
  2. apply_pro_boost()       — aggressive sizing for premium users
  3. recover_corrupt_data()  — detect & fix data corruption (idempotent)
  4. restore_active_bots()   — restart bots that were running before shutdown
"""

import logging
from sqlalchemy import select, update, or_
from database import AsyncSessionLocal, User, Trade, RiskState, DailyReport

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Scalp-mode profit parameters
# ─────────────────────────────────────────────────────────────────────────────

# Only applied to users with calibration_count == 0 (never AI-tuned).
# Preserves hard-won calibrated parameters across deploys.
#
# Target: $200–300/day on a $10k account
#   SL 0.5%, TP 2.0% → 4:1 R/R
#   Z-revert fires when unrealised ≥ $15 → avg $15-35 win
#   60% exposure on $10k = $6k deployed → meaningful position size
SCALP_PARAMS = dict(
    entry_z=1.1,
    stop_loss_pct=0.005,
    take_profit_pct=0.020,
    trail_stop_pct=0.005,
    use_rsi_filter=True,
    use_ema_filter=False,
    use_adx_filter=True,
    use_bbands_filter=True,
    use_macd_filter=False,
    max_drawdown_pct=8.0,
    max_stops_before_pause=5,
    cooldown_ticks=2,
    risk_per_trade_pct=2.5,
    max_exposure_pct=60.0,
    position_size_mode="dynamic",
)


async def apply_profit_params() -> int:
    """Apply scalp-mode defaults to uncalibrated users. Returns rows affected."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(User)
            .where(User.calibration_count == 0)
            .values(**SCALP_PARAMS)
        )
        await db.commit()
    n = result.rowcount
    logger.info(
        f"Startup: SCALP MODE → {n} uncalibrated users | "
        "SL=0.5% TP=2% trail=0.5% z=1.1 risk=2.5% exposure=60%"
    )
    return n


# ─────────────────────────────────────────────────────────────────────────────
# 2. PRO BOOST — aggressive sizing + signal reset for premium users
# ─────────────────────────────────────────────────────────────────────────────

# PRO users have proven 65-69% win rate. Bump risk/exposure for larger P&L.
# Math: 65% win × 30 trades/day × $2,500 notional × 1% net edge ≈ $487/day
#
# CRITICAL: entry_z is reset to 1.3 every deploy to prevent the AI calibrator
# from over-tightening it after a bad session. After 11 losses the calibrator
# pushed entry_z toward 3.5 — signals became so rare the bot stopped trading.
# 1.3 gives ~6-12 signal opportunities per day per symbol; calibrator fine-tunes
# from there but startup always pulls it back to a tradeable baseline.
#
# BBands filter disabled: the breakout/retest signal fires when price is ABOVE
# the mean (positive z) — exactly where BBands %B > 0.8 lives. Keeping BBands
# ON blocks the very entries the signal generator is designed to catch.
PRO_BOOST = dict(
    entry_z=1.3,
    risk_per_trade_pct=3.5,
    max_exposure_pct=70.0,
    stop_loss_pct=0.005,
    take_profit_pct=0.020,   # 4:1 R/R vs SL → realistic daily target
    trail_stop_pct=0.004,
    cooldown_ticks=1,
    use_rsi_filter=False,    # RSI > 70 was blocking strong upside breakouts
    use_bbands_filter=False, # BBands contradicts breakout-retest signal logic
    use_adx_filter=True,     # keep: blocks entries during extreme trends
    use_ema_filter=False,
    use_macd_filter=False,
    max_stops_before_pause=5,
)


async def apply_pro_boost() -> int:
    """Apply PRO sizing + reset signal params for premium users. Returns rows affected."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(User).where(User.is_premium == True).values(**PRO_BOOST)
        )
        await db.commit()
    n = result.rowcount
    logger.info(
        f"Startup: PRO BOOST → {n} premium users | "
        "entry_z=1.3 risk=3.5% exposure=70% TP=2% trail=0.4% RSI/BB filters OFF"
    )
    return n


# ─────────────────────────────────────────────────────────────────────────────
# 3. Data corruption recovery (idempotent)
# ─────────────────────────────────────────────────────────────────────────────

# Previous mock-client short bug credited free proceeds on every sell-to-open,
# compounding into quintillion-dollar balances and absurd quantities.

_CORRUPT_BALANCE   = 1_000_000.0   # $1M+ → impossible on a $10k demo seed
_CORRUPT_NOTIONAL  = 500_000.0     # entry × qty > $500k for OPEN trade → user-level reset
_ABSURD_PNL        = 100_000.0     # |P&L| > $100k → fake
_ABSURD_NOTIONAL   = 50_000.0      # entry × qty > $50k → trade-level scrub only

# NOTE: was using raw quantity_value > 10000 to detect "corrupt" trades, but
# that flags any legit DOGE position (10k+ units at $0.10 = $1000 — normal).
# Switched to notional ($500k = 70x normal max position) so only truly absurd
# values trigger the destructive user-level reset that wipes ALL open trades
# and sets bot_active=False.


async def recover_corrupt_data() -> None:
    """Detect and fix data corruption. Fully idempotent — safe to run every deploy."""
    async with AsyncSessionLocal() as db:

        # 1 — Find users with corrupt balance or absurd open-trade quantities
        corrupt_users: list[User] = []

        bal_q = await db.execute(select(User).where(User.demo_balance > _CORRUPT_BALANCE))
        corrupt_users.extend(bal_q.scalars().all())

        # Notional-based check (entry × qty > $500k) — units alone are misleading
        # because a $7k DOGE position at $0.10/unit is 70,000 units (over the old
        # 10k threshold) but completely legitimate. Only flag the user when the
        # OPEN position is impossibly large by dollar value.
        bad_open_q = await db.execute(
            select(Trade)
            .where(Trade.state == "open", Trade.is_demo == True, Trade.quantity_value > 0.0)
        )
        bad_uids = set()
        for t in bad_open_q.scalars().all():
            ep = float(t.entry_price or 0)
            qv = float(t.quantity_value or 0)
            if ep > 0 and (ep * qv) > _CORRUPT_NOTIONAL:
                bad_uids.add(t.user_id)
        if bad_uids:
            existing = {u.id for u in corrupt_users}
            extra_q = await db.execute(select(User).where(User.id.in_(bad_uids - existing)))
            corrupt_users.extend(extra_q.scalars().all())

        for cu in corrupt_users:
            await db.execute(
                Trade.__table__.update()
                .where(Trade.user_id == cu.id, Trade.state == "open")
                .values(state="closed", exit_reason="corruption_recovery", pnl=0.0, pnl_pct=0.0)
            )
            await db.execute(
                update(User).where(User.id == cu.id)
                .values(demo_balance=10000.0, bot_active=False)
            )
            await db.execute(RiskState.__table__.delete().where(RiskState.user_id == cu.id))
            logger.warning(f"CORRUPTION RECOVERY: user {cu.id[:8]} reset (was ${cu.demo_balance:,.0f})")

        if corrupt_users:
            await db.commit()

        # 2 — Zero absurd P&L rows
        await db.execute(
            Trade.__table__.update()
            .where(or_(Trade.pnl > _ABSURD_PNL, Trade.pnl < -_ABSURD_PNL))
            .values(pnl=0.0, pnl_pct=0.0, partial_pnl=0.0)
        )

        # 3 — Zero absurd-notional trade quantities
        cands = (await db.execute(select(Trade).where(Trade.quantity_value > 0.0))).scalars().all()
        scrub_ids = [
            t.id for t in cands
            if (ep := float(t.entry_price or 0)) > 0
            and (float(t.quantity_value or 0) * ep) > _ABSURD_NOTIONAL
        ]
        if scrub_ids:
            CHUNK = 500
            for i in range(0, len(scrub_ids), CHUNK):
                await db.execute(
                    Trade.__table__.update()
                    .where(Trade.id.in_(scrub_ids[i:i + CHUNK]))
                    .values(
                        quantity="0", quantity_value=0.0, initial_quantity=0.0,
                        pnl=0.0, pnl_pct=0.0, partial_pnl=0.0,
                        exit_reason="data_corruption_scrubbed",
                    )
                )

        # 4 — Residue: qty=0 closed trades with leftover P&L
        await db.execute(
            Trade.__table__.update()
            .where(
                Trade.quantity_value == 0.0,
                Trade.state == "closed",
                or_(Trade.pnl != 0.0, Trade.pnl_pct != 0.0, Trade.partial_pnl != 0.0),
            )
            .values(pnl=0.0, pnl_pct=0.0, partial_pnl=0.0)
        )

        # 5 — Scrub corrupt daily_reports aggregates
        await db.execute(
            DailyReport.__table__.update()
            .where(or_(DailyReport.total_pnl > _ABSURD_PNL, DailyReport.total_pnl < -_ABSURD_PNL))
            .values(total_pnl=0.0)
        )

        # NOTE: Step 6 (reset ALL balances to $10k) was a ONE-TIME recovery op.
        # It must NOT run on every deploy — it wipes real trading gains.
        # Removed: update(User).where(User.demo_balance != 10000.0)...

        await db.commit()

    n_corrupt = len(corrupt_users)
    n_scrub   = len(scrub_ids)
    logger.info(
        f"Startup: corruption recovery done — "
        f"{n_corrupt} users reset, {n_scrub} trades scrubbed"
    )

    # ── One-shot recovery: the OLD (broken) corruption_recovery wrongly set
    # bot_active=False on premium users with legitimate cheap-unit positions
    # (e.g. DOGE at 70k units). Now that the threshold is notional-based, those
    # users were never actually corrupt — restore their Robinhood bot to active
    # so loops start again on this deploy. Capital.com bot was unaffected
    # (uses bot_active_capital column), so we use it as the "was active" signal.
    async with AsyncSessionLocal() as db2:
        res = await db2.execute(
            update(User)
            .where(
                User.is_premium == True,
                User.bot_active == False,
                User.bot_active_capital == True,
            )
            .values(bot_active=True)
        )
        if res.rowcount:
            await db2.commit()
            logger.warning(
                f"Startup: re-enabled bot_active=True for {res.rowcount} premium "
                f"user(s) wrongly disabled by old corruption_recovery"
            )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Restore previously active bots
# ─────────────────────────────────────────────────────────────────────────────

async def resume_stuck_risk_managers() -> int:
    """
    Auto-resume any risk managers paused for > 8 hours.

    The StopLossGuard pauses trading after N stops in a 4h window.
    After a deploy/restart the 4h window has long passed but is_paused
    stays True in the DB — permanently blocking trading. This clears
    stale pauses on startup so bots can trade again immediately.
    """
    from datetime import datetime, timezone, timedelta

    STALE_HOURS = 8

    cleared = 0
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(RiskState).where(RiskState.is_paused == True))).scalars().all()
        for rs in rows:
            # Check age of pause via updated_at
            age = datetime.now(timezone.utc) - rs.updated_at.replace(tzinfo=timezone.utc)
            if age < timedelta(hours=STALE_HOURS):
                logger.info(f"Startup: respecting recent pause for {rs.user_id[:12]} (age {age})")
                continue

            await db.execute(
                update(RiskState)
                .where(RiskState.user_id == rs.user_id)
                .values(is_paused=False, pause_reason=None, cooldown_remaining=0)
            )
            cleared += 1
            logger.info(f"Startup: cleared stale pause for {rs.user_id[:12]} (paused {age} ago)")

        if cleared:
            await db.commit()

    logger.info(f"Startup: auto-resumed {cleared} stale risk pause(s)")
    return cleared


async def restore_active_bots() -> int:
    """Restart bots that were running before the last shutdown. Returns count."""
    from bot_engine import start_bot
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User))
        users = result.scalars().all()

    count = 0
    for user in users:
        if getattr(user, 'bot_active', False):
            logger.info(f"Startup: restoring Robinhood bot for user {user.id[:8]}")
            await start_bot(user.id, broker='robinhood')
            count += 1
        if getattr(user, 'bot_active_capital', False):
            logger.info(f"Startup: restoring Capital.com bot for user {user.id[:8]}")
            await start_bot(user.id, broker='capital')
            count += 1

    logger.info(f"Startup: restored {count} active bot loop(s)")
    return count


# ─────────────────────────────────────────────────────────────────────────────
# Master runner — call this once from main.py lifespan
# ─────────────────────────────────────────────────────────────────────────────

async def run_all() -> None:
    """Run all startup tasks in order."""
    await apply_profit_params()
    await apply_pro_boost()
    await recover_corrupt_data()
    await resume_stuck_risk_managers()   # clear stale pauses before bots start
    await restore_active_bots()
