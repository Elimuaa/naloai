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

        # Rolling trade stats for Kelly Criterion sizing
        # Stores last 30 closed trades as (pnl, was_win) tuples
        self.recent_trades: list[tuple[float, bool]] = []

    def kelly_fraction(self) -> float:
        """Compute Kelly-fraction based on recent trade history.

        Kelly = (p*b - (1-p)) / b
          p = win rate over last 30 trades
          b = avg_win / avg_loss ratio
        Uses half-Kelly (0.5× multiplier) for safety.
        Clamped to [0.25, 1.5] — never goes below 25% or above 150% of base size.

        When edge shrinks (low win-rate or small wins), fraction drops → auto-defense.
        When edge grows, fraction rises up to 1.5× → auto-offense.
        """
        if len(self.recent_trades) < 10:
            return 1.0  # Not enough data yet — use base size

        wins = [t[0] for t in self.recent_trades if t[1]]
        losses = [abs(t[0]) for t in self.recent_trades if not t[1] and t[0] < 0]

        if not wins or not losses:
            return 1.0

        p = len(wins) / len(self.recent_trades)          # win rate
        avg_win = sum(wins) / len(wins)
        avg_loss = sum(losses) / len(losses)
        if avg_loss <= 0:
            return 1.0
        b = avg_win / avg_loss                             # reward/risk ratio

        kelly = (p * b - (1 - p)) / b                     # raw Kelly
        half_kelly = max(0.0, kelly * 0.5)                # half-Kelly for safety

        # Express as multiplier vs base risk_per_trade_pct (e.g. 2%)
        # half_kelly of 0.02 = 2% risk = 1.0× base
        base = self.risk_per_trade_pct / 100.0
        if base <= 0 or half_kelly <= 0:
            return 1.0  # no edge data yet — trade at baseline
        multiplier = half_kelly / base
        return max(0.25, min(1.5, multiplier))

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

    def record_trade_close(self, pnl: float, exit_reason: str, total_pnl: Optional[float] = None):
        """Record a closed trade for risk tracking.

        `pnl` is the close-leg P&L (added to daily_pnl). If a partial-profit
        close fired earlier in the trade, that profit was already booked into
        daily_pnl at the time of the partial — passing close-leg only here
        avoids double-counting.

        `total_pnl` (optional) is the TRUE total profit (close-leg + partial).
        It's what the Kelly tracker uses to grade the trade as win/loss.
        Defaults to `pnl` when no partial occurred.
        """
        self.daily_pnl += pnl

        # Track for Kelly sizing — use TOTAL trade outcome (partial + close)
        kelly_pnl = total_pnl if total_pnl is not None else pnl
        self.recent_trades.append((kelly_pnl, kelly_pnl > 0))
        if len(self.recent_trades) > 30:
            self.recent_trades.pop(0)

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

    def to_persisted_dict(self) -> dict:
        """Serialize the volatile state required to survive a process restart.

        Static config (max_drawdown_pct etc) is reloaded from the User row each
        tick via _get_risk_manager — only the LIVE counters need persisting.
        """
        return {
            "daily_pnl": float(self.daily_pnl),
            "daily_starting_balance": float(self.daily_starting_balance),
            "daily_reset_date": self.daily_reset_date,
            "is_paused": bool(self.is_paused),
            "pause_reason": self.pause_reason or "",
            "cooldown_remaining": int(self.cooldown_remaining),
            "stop_loss_times": [t.isoformat() for t in self.stop_loss_times],
            "recent_trades": [[float(p), bool(w)] for (p, w) in self.recent_trades],
        }

    def restore_from_dict(self, data: dict) -> None:
        """Hydrate state from a persisted snapshot. Tolerant of partial/missing fields."""
        try:
            self.daily_pnl = float(data.get("daily_pnl") or 0.0)
            self.daily_starting_balance = float(data.get("daily_starting_balance") or 0.0)
            self.daily_reset_date = data.get("daily_reset_date")
            self.is_paused = bool(data.get("is_paused") or False)
            self.pause_reason = data.get("pause_reason") or ""
            self.cooldown_remaining = int(data.get("cooldown_remaining") or 0)
            slt = data.get("stop_loss_times") or []
            self.stop_loss_times = []
            for s in slt:
                try:
                    self.stop_loss_times.append(datetime.fromisoformat(s))
                except Exception:
                    continue
            rt = data.get("recent_trades") or []
            self.recent_trades = []
            for entry in rt:
                try:
                    self.recent_trades.append((float(entry[0]), bool(entry[1])))
                except Exception:
                    continue
            # If the persisted day is yesterday, the next tick's reset_daily() will
            # roll the counters cleanly. We still keep the values here so the UI
            # doesn't flash zeros for the brief window before the first tick.
            logger.info(
                f"Risk state restored: daily_pnl={self.daily_pnl:.2f}, "
                f"recent_trades={len(self.recent_trades)}, stops={len(self.stop_loss_times)}, "
                f"paused={self.is_paused}"
            )
        except Exception as e:
            logger.error(f"Risk state restore failed (starting fresh): {e}")

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
