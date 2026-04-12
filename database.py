import os
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Boolean, Float, Text, DateTime, Integer, text
import uuid

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
    entry_z: Mapped[float] = mapped_column(Float, default=2.0)
    exit_z: Mapped[float] = mapped_column(Float, default=0.5)
    lookback: Mapped[str] = mapped_column(String, default="20")
    # Research-backed defaults: 2.5% SL / 5% TP = 2:1 R/R; 1.5% trail avoids micro-volatility exits
    stop_loss_pct: Mapped[float] = mapped_column(Float, default=0.025)
    take_profit_pct: Mapped[float] = mapped_column(Float, default=0.05)
    trail_stop_pct: Mapped[float] = mapped_column(Float, default=0.015)


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


async def init_db():
    os.makedirs("data", exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA busy_timeout=30000"))
        await conn.execute(text("PRAGMA synchronous=NORMAL"))
        await conn.execute(text("PRAGMA foreign_keys=ON"))
        # Migrate: add is_demo column if missing
        try:
            await conn.execute(text("ALTER TABLE trades ADD COLUMN is_demo BOOLEAN DEFAULT 0"))
        except Exception:
            pass  # Column already exists


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
