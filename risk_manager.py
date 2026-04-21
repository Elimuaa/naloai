"""
Risk management module for CryptoBot.
Provides drawdown protection, stoploss guard, cooldown, exposure limits,
and dynamic position sizing.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from indicators import atr_from_prices

logger = logging.getLogger(__name__)


class RiskManager:
    """Per-user risk manager that tracks drawdown, consecutive stops, and cooldowns."""

    def __init__(
        self,
        max_drawdown_pct: float = 8.0,       # Auto-pause at -8% daily drawdown
        max_stops_before_pause: int = 3,       # Pause after 3 stop-losses in window
        stop_guard_window_hours: float = 4.0,  # Window for stoploss guard
        cooldown_ticks: int = 5,               # Skip N ticks after a stop-loss
        max_exposure_pct: float = 40.0,        # Max 40% of balance per position → $4k on $10k
        risk_per_trade_pct: float = 2.0,       # Risk 2% per trade → $200 risk on $10k ($200 win at 2:1)
    ):
        self.max_drawdown_pct = max_drawdown_pct
        self.max_stops_before_pause = max_stops_before_pause
        self.stop_guard_window = timedelta(hours=stop_guard_window_hours)
        self.cooldown_ticks = cooldown_ticks
        self.max_exposure_pct = max_exposure_pct
        self.risk_per_trade_pct = risk_per_trade_pct

        # State
        self.daily_starting_balance: float = 0.0
        self.daily_pnl: float = 0.0
        self.daily_reset_date: Optional[str] = None
        self.stop_loss_times: list[datetime] = []
        self.cooldown_remaining: int = 0
        self.is_paused: bool = False
        self.pause_reason: str = ""

    def reset_daily(self, balance: float):
        """Reset daily tracking. Call at start of each trading day."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.daily_reset_date != today:
            self.daily_starting_balance = balance
            self.daily_pnl = 0.0
            self.daily_reset_date = today
            self.is_paused = False
            self.pause_reason = ""
            # Prune old stop-loss records
            cutoff = datetime.now(timezone.utc) - self.stop_guard_window
            self.stop_loss_times = [t for t in self.stop_loss_times if t > cutoff]

    def record_trade_close(self, pnl: float, exit_reason: str):
        """Record a closed trade for risk tracking."""
        self.daily_pnl += pnl

        if exit_reason == "stop_loss":
            self.stop_loss_times.append(datetime.now(timezone.utc))
            self.cooldown_remaining = self.cooldown_ticks

        # Check drawdown
        if self.daily_starting_balance > 0:
            drawdown_pct = abs(self.daily_pnl) / self.daily_starting_balance * 100
            if self.daily_pnl < 0 and drawdown_pct >= self.max_drawdown_pct:
                self.is_paused = True
                self.pause_reason = f"Max daily drawdown reached ({drawdown_pct:.1f}% >= {self.max_drawdown_pct}%)"
                logger.warning(f"Risk: {self.pause_reason}")

        # Check stoploss guard
        cutoff = datetime.now(timezone.utc) - self.stop_guard_window
        recent_stops = [t for t in self.stop_loss_times if t > cutoff]
        self.stop_loss_times = recent_stops
        if len(recent_stops) >= self.max_stops_before_pause:
            self.is_paused = True
            self.pause_reason = f"StoplossGuard: {len(recent_stops)} stop-losses in {self.stop_guard_window.total_seconds()/3600:.0f}h"
            logger.warning(f"Risk: {self.pause_reason}")

    def can_trade(self) -> tuple[bool, str]:
        """Check if trading is allowed. Returns (allowed, reason)."""
        if self.is_paused:
            return False, self.pause_reason

        if self.cooldown_remaining > 0:
            self.cooldown_remaining -= 1
            return False, f"Cooldown: {self.cooldown_remaining + 1} ticks remaining after stop-loss"

        return True, ""

    def calculate_position_size(
        self,
        balance: float,
        entry_price: float,
        stop_loss_pct: float,
        prices: list[float],
        min_qty: float = 0.0001,
    ) -> float:
        """Calculate position size based on ATR and risk-per-trade.

        Uses the smaller of:
        1. Risk-based: (balance * risk_per_trade%) / (entry_price * stop_loss_distance)
        2. Exposure-based: (balance * max_exposure%) / entry_price
        3. ATR-adjusted: tightens position when ATR is high relative to price
        """
        if balance <= 0 or entry_price <= 0:
            return min_qty

        # Risk-based sizing
        risk_amount = balance * (self.risk_per_trade_pct / 100.0)
        stop_distance = entry_price * stop_loss_pct
        if stop_distance > 0:
            risk_qty = risk_amount / stop_distance
        else:
            risk_qty = min_qty

        # Exposure-based cap
        max_exposure_amount = balance * (self.max_exposure_pct / 100.0)
        exposure_qty = max_exposure_amount / entry_price

        # ATR adjustment — reduce size in high volatility
        current_atr = atr_from_prices(prices, 14)
        atr_multiplier = 1.0
        if current_atr and entry_price > 0:
            atr_pct = current_atr / entry_price
            # If ATR% > 2x stop_loss_pct, reduce position proportionally
            if atr_pct > stop_loss_pct * 2:
                atr_multiplier = stop_loss_pct * 2 / atr_pct

        qty = min(risk_qty, exposure_qty) * atr_multiplier
        # Enforce minimum and round to 4 decimals
        qty = max(min_qty, round(qty, 4))
        return qty

    def get_status(self) -> dict:
        """Return current risk manager state for UI display."""
        return {
            "is_paused": self.is_paused,
            "pause_reason": self.pause_reason,
            "daily_pnl": round(self.daily_pnl, 4),
            "daily_drawdown_pct": round(
                abs(self.daily_pnl) / self.daily_starting_balance * 100, 2
            ) if self.daily_starting_balance > 0 and self.daily_pnl < 0 else 0.0,
            "cooldown_remaining": self.cooldown_remaining,
            "recent_stops": len(self.stop_loss_times),
            "max_drawdown_pct": self.max_drawdown_pct,
            "max_stops": self.max_stops_before_pause,
        }

    def resume(self):
        """Manually resume trading after pause."""
        self.is_paused = False
        self.pause_reason = ""
        self.cooldown_remaining = 0
        logger.info("Risk: Trading resumed manually")
