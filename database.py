import os
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Boolean, Float, Text, DateTime, Integer, text
import uuid

os.makedirs("data", exist_ok=True)
DATABASE_URL = "sqlite+aiosqlite:///./data/app.db"

engine = create_async_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
    echo=False
)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Robinhood API keys
    rh_api_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    rh_private_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # legacy field

    # Auto-generated Ed25519 key pair (private key signs requests, public key registered on Robinhood)
    ed25519_private_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ed25519_public_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Bot settings
    bot_active: Mapped[bool] = mapped_column(Boolean, default=False)
    trading_symbol: Mapped[str] = mapped_column(String, default="BTC-USD")
    entry_z: Mapped[float] = mapped_column(Float, default=1.3)   # profit-optimized (was 1.5)
    exit_z: Mapped[float] = mapped_column(Float, default=0.5)
    lookback: Mapped[str] = mapped_column(String, default="20")
    # Research-backed defaults: 2.5% SL / 5% TP = 2:1 R/R; 1.5% trail avoids micro-volatility exits
    stop_loss_pct: Mapped[float] = mapped_column(Float, default=0.025)
    take_profit_pct: Mapped[float] = mapped_column(Float, default=0.05)
    trail_stop_pct: Mapped[float] = mapped_column(Float, default=0.020)

    # Demo balance — user-configurable starting balance for paper trading
    demo_balance: Mapped[float] = mapped_column(Float, default=10000.0)

    # Advanced strategy settings
    use_rsi_filter: Mapped[bool] = mapped_column(Boolean, default=True)
    use_ema_filter: Mapped[bool] = mapped_column(Boolean, default=False)  # profit-optimized (was True)
    use_adx_filter: Mapped[bool] = mapped_column(Boolean, default=True)
    use_bbands_filter: Mapped[bool] = mapped_column(Boolean, default=True)
    use_macd_filter: Mapped[bool] = mapped_column(Boolean, default=False)
    use_volume_filter: Mapped[bool] = mapped_column(Boolean, default=False)

    # Risk management settings
    max_drawdown_pct: Mapped[float] = mapped_column(Float, default=8.0)       # 8% daily drawdown limit
    max_stops_before_pause: Mapped[int] = mapped_column(Integer, default=4)  # 3->4: less aggressive pausing
    cooldown_ticks: Mapped[int] = mapped_column(Integer, default=3)  # 5->3: re-enter faster after stops
    risk_per_trade_pct: Mapped[float] = mapped_column(Float, default=2.0)     # 2% risk → $200/trade at $10k
    max_exposure_pct: Mapped[float] = mapped_column(Float, default=40.0)      # 40% max → $4k position at $10k

    # Position sizing
    position_size_mode: Mapped[str] = mapped_column(String, default="dynamic")  # "fixed" or "dynamic"
    fixed_quantity: Mapped[float] = mapped_column(Float, default=0.0001)

    # Telegram notifications
    telegram_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # Premium subscription
    is_premium: Mapped[bool] = mapped_column(Boolean, default=False)
    premium_since: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    calibration_count: Mapped[int] = mapped_column(Integer, default=0)  # Total calibrations run
    last_calibration_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Stripe
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    stripe_subscription_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Multi-broker support
    broker_type: Mapped[str] = mapped_column(String, default="robinhood")  # "robinhood" | "capital" | "tradovate"

    # Capital.com credentials
    capital_api_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    capital_identifier: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # Capital.com login email
    capital_password: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # Capital.com login password

    # Tradovate credentials
    tradovate_username: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    tradovate_password: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    tradovate_account_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String, index=True)
    symbol: Mapped[str] = mapped_column(String)
    side: Mapped[str] = mapped_column(String)  # buy / sell
    quantity: Mapped[str] = mapped_column(String)
    entry_price: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    exit_price: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    state: Mapped[str] = mapped_column(String, default="open")  # open / closed / cancelled
    rh_order_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)  # True = simulated, False = real Robinhood order
    exit_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # stop_loss / take_profit / trailing_stop
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Numeric quantity for position sizing
    quantity_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=0)
    # JSON snapshot of indicators at entry time
    indicators_snapshot: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Partial profit booked during the trade (e.g. 50% close at 1R) — pnl is the TOTAL incl. partial
    partial_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    # Original entry quantity before partial (used for accurate per-trade R/R analytics)
    initial_quantity: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=0)

    # AI analysis fields (populated by post_trade_ai_learner.py after close)
    ai_grade: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    ai_entry_quality: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    ai_exit_quality: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    ai_what_went_well: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON array
    ai_what_went_wrong: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON array
    ai_improvements: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON array
    ai_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ai_analyzed: Mapped[bool] = mapped_column(Boolean, default=False)


class DailyReport(Base):
    __tablename__ = "daily_reports"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String, index=True)
    report_date: Mapped[str] = mapped_column(String, index=True)  # YYYY-MM-DD
    total_trades: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    total_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    avg_rr: Mapped[float] = mapped_column(Float, default=0.0)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    top_improvement: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    full_report_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class CalibrationLog(Base):
    __tablename__ = "calibration_logs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    # What changed
    param_changes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON: {param: {old, new, reason}}
    # Context
    trade_count_analyzed: Mapped[int] = mapped_column(Integer, default=0)
    win_rate_before: Mapped[float] = mapped_column(Float, default=0.0)
    projected_improvement: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ai_reasoning: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


async def init_db():
    os.makedirs("data", exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA busy_timeout=30000"))
        await conn.execute(text("PRAGMA synchronous=NORMAL"))
        await conn.execute(text("PRAGMA foreign_keys=ON"))
        # Migrate: add columns if missing
        for stmt in [
            "ALTER TABLE trades ADD COLUMN is_demo BOOLEAN DEFAULT 0",
            "ALTER TABLE users ADD COLUMN demo_balance REAL DEFAULT 10000.0",
            "ALTER TABLE users ADD COLUMN use_rsi_filter BOOLEAN DEFAULT 1",
            "ALTER TABLE users ADD COLUMN use_ema_filter BOOLEAN DEFAULT 1",
            "ALTER TABLE users ADD COLUMN use_adx_filter BOOLEAN DEFAULT 1",
            "ALTER TABLE users ADD COLUMN use_bbands_filter BOOLEAN DEFAULT 1",
            "ALTER TABLE users ADD COLUMN use_macd_filter BOOLEAN DEFAULT 0",
            "ALTER TABLE users ADD COLUMN use_volume_filter BOOLEAN DEFAULT 0",
            "ALTER TABLE users ADD COLUMN max_drawdown_pct REAL DEFAULT 5.0",
            "ALTER TABLE users ADD COLUMN max_stops_before_pause INTEGER DEFAULT 3",
            "ALTER TABLE users ADD COLUMN cooldown_ticks INTEGER DEFAULT 5",
            "ALTER TABLE users ADD COLUMN risk_per_trade_pct REAL DEFAULT 1.0",
            "ALTER TABLE users ADD COLUMN max_exposure_pct REAL DEFAULT 20.0",
            "ALTER TABLE users ADD COLUMN position_size_mode TEXT DEFAULT 'dynamic'",
            "ALTER TABLE users ADD COLUMN fixed_quantity REAL DEFAULT 0.0001",
            "ALTER TABLE users ADD COLUMN telegram_enabled BOOLEAN DEFAULT 0",
            "ALTER TABLE trades ADD COLUMN quantity_value REAL DEFAULT 0",
            "ALTER TABLE trades ADD COLUMN indicators_snapshot TEXT DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN is_premium BOOLEAN DEFAULT 0",
            "ALTER TABLE users ADD COLUMN premium_since DATETIME DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN calibration_count INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN last_calibration_at DATETIME DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN stripe_customer_id TEXT DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN stripe_subscription_id TEXT DEFAULT NULL",
            # Multi-broker columns
            "ALTER TABLE users ADD COLUMN broker_type TEXT DEFAULT 'robinhood'",
            "ALTER TABLE users ADD COLUMN capital_api_key TEXT DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN capital_identifier TEXT DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN capital_password TEXT DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN tradovate_username TEXT DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN tradovate_password TEXT DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN tradovate_account_id INTEGER DEFAULT NULL",
            # Partial profit accounting
            "ALTER TABLE trades ADD COLUMN partial_pnl REAL DEFAULT 0.0",
            "ALTER TABLE trades ADD COLUMN initial_quantity REAL DEFAULT 0",
        ]:
            try:
                await conn.execute(text(stmt))
            except Exception as e:
                err_msg = str(e).lower()
                if "duplicate column" in err_msg or "already exists" in err_msg:
                    pass  # Column already exists, expected
                else:
                    import logging
                    logging.getLogger(__name__).warning(f"Migration warning: {stmt[:60]}... -> {e}")

        # One-time backfill: align EXISTING users with profit-optimized defaults.
        # Only updates users that still have the OLD default values (preserves customizations).
        backfills = [
            # entry_z: 1.5 (old) -> 1.3 (new) — looser entries, more trades
            "UPDATE users SET entry_z = 1.3 WHERE entry_z = 1.5",
            # use_ema_filter: True (old) -> False (new) — EMA filter rejects too many good signals
            "UPDATE users SET use_ema_filter = 0 WHERE use_ema_filter = 1",
            # cooldown_ticks: 5 (old) -> 3 (new) — re-enter faster after stops
            "UPDATE users SET cooldown_ticks = 3 WHERE cooldown_ticks = 5",
            # max_stops_before_pause: 3 (old) -> 4 (new) — don't pause as easily
            "UPDATE users SET max_stops_before_pause = 4 WHERE max_stops_before_pause = 3",
            # trail_stop_pct: 0.015 (old) -> 0.020 (new) — stop clipping winners on BTC noise
            "UPDATE users SET trail_stop_pct = 0.020 WHERE trail_stop_pct = 0.015",
        ]
        for stmt in backfills:
            try:
                await conn.execute(text(stmt))
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"Backfill warning: {stmt[:60]}... -> {e}")


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
