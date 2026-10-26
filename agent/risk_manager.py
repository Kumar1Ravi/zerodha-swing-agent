"""Position sizing and pre-trade risk checks for the paper-trading agent."""

from __future__ import annotations

from dataclasses import dataclass
from math import floor, isfinite


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Configured account and per-trade risk limits (all monetary values in INR)."""

    planned_risk_per_trade: float = 200.0
    hard_risk_ceiling_per_trade: float = 300.0
    max_open_positions: int = 1
    daily_loss_limit: float = 300.0
    gap_slippage_buffer_percent: float = 1.0
    allow_leverage: bool = False
    allow_averaging_down: bool = False

    def __post_init__(self) -> None:
        _require_positive_finite("planned_risk_per_trade", self.planned_risk_per_trade)
        _require_positive_finite("hard_risk_ceiling_per_trade", self.hard_risk_ceiling_per_trade)
        _require_positive_finite("daily_loss_limit", self.daily_loss_limit)
        if self.planned_risk_per_trade > self.hard_risk_ceiling_per_trade:
            raise ValueError("planned risk cannot exceed the hard risk ceiling")
        if isinstance(self.max_open_positions, bool) or self.max_open_positions < 1:
            raise ValueError("max_open_positions must be a positive integer")
        if not isinstance(self.max_open_positions, int):
            raise ValueError("max_open_positions must be a positive integer")
        if not isfinite(self.gap_slippage_buffer_percent) or self.gap_slippage_buffer_percent < 0:
            raise ValueError("gap_slippage_buffer_percent must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class SizingResult:
    accepted: bool
    quantity: int
    entry_price: float
    stop_loss: float
    target_price: float
    planned_loss_per_share: float
    planned_risk: float
    required_capital: float
    reward_to_risk: float
    reason: str


def _require_positive_finite(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")


def size_long_position(
    *,
    entry_price: float,
    stop_loss: float,
    target_price: float,
    available_capital: float,
    limits: RiskLimits,
    estimated_cost_per_share: float = 0.0,
) -> SizingResult:
    """Size a cash-market long position, rounding down to whole shares.

    Costs are added to per-share risk for conservative sizing. The configured gap
    and slippage buffer increases the stop distance used for planned-risk sizing.
    Brokerage and statutory costs must be estimated by the caller; no fees are
    inferred here.
    """
    for name, value in (
        ("entry_price", entry_price),
        ("stop_loss", stop_loss),
        ("target_price", target_price),
        ("available_capital", available_capital),
    ):
        _require_positive_finite(name, value)
    if isinstance(estimated_cost_per_share, bool) or not isinstance(estimated_cost_per_share, (int, float)):
        raise ValueError("estimated_cost_per_share must be finite and non-negative")
    if not isfinite(estimated_cost_per_share) or estimated_cost_per_share < 0:
        raise ValueError("estimated_cost_per_share must be finite and non-negative")

    if stop_loss >= entry_price:
        return _rejected(entry_price, stop_loss, target_price, "stop-loss must be below entry for a long position")
    if target_price <= entry_price:
        return _rejected(entry_price, stop_loss, target_price, "target must be above entry for a long position")

    raw_risk_per_share = entry_price - stop_loss
    buffered_risk_per_share = raw_risk_per_share * (1 + limits.gap_slippage_buffer_percent / 100)
    risk_per_share = buffered_risk_per_share + estimated_cost_per_share
    if risk_per_share <= 0 or not isfinite(risk_per_share):
        return _rejected(entry_price, stop_loss, target_price, "computed per-share risk is invalid")

    reward_to_risk = (target_price - entry_price) / raw_risk_per_share
    if reward_to_risk < 1:
        return _rejected(entry_price, stop_loss, target_price, "target reward is below stop-loss risk")

    # Enforce both planned budget and hard ceiling in case configuration changes.
    risk_budget = min(limits.planned_risk_per_trade, limits.hard_risk_ceiling_per_trade)
    qty_by_risk = floor(risk_budget / risk_per_share)
    qty_by_capital = floor(available_capital / entry_price)
    quantity = min(qty_by_risk, qty_by_capital)
    required_capital = quantity * entry_price
    planned_risk = quantity * risk_per_share

    if quantity <= 0:
        reason = "insufficient capital for one share" if qty_by_capital <= 0 else "risk budget is too small for one share"
        return SizingResult(False, 0, entry_price, stop_loss, target_price, risk_per_share, 0.0, 0.0, reward_to_risk, reason)
    if planned_risk > limits.hard_risk_ceiling_per_trade:
        return _rejected(entry_price, stop_loss, target_price, "planned loss exceeds hard per-trade risk ceiling")

    return SizingResult(
        accepted=True,
        quantity=quantity,
        entry_price=entry_price,
        stop_loss=stop_loss,
        target_price=target_price,
        planned_loss_per_share=risk_per_share,
        planned_risk=planned_risk,
        required_capital=required_capital,
        reward_to_risk=reward_to_risk,
        reason="accepted",
    )


def _rejected(entry: float, stop: float, target: float, reason: str) -> SizingResult:
    return SizingResult(False, 0, entry, stop, target, 0.0, 0.0, 0.0, 0.0, reason)


def check_trade_allowed(
    *,
    proposed_risk: float,
    available_capital: float,
    required_capital: float,
    open_positions: int,
    realized_daily_pnl: float,
    limits: RiskLimits,
) -> tuple[bool, str]:
    """Apply account-level gates before accepting a sized trade."""
    for name, value in (
        ("proposed_risk", proposed_risk),
        ("available_capital", available_capital),
        ("required_capital", required_capital),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
            return False, f"{name} must be finite and non-negative"
    if isinstance(open_positions, bool) or not isinstance(open_positions, int) or open_positions < 0:
        return False, "open_positions must be a non-negative integer"
    if not isfinite(realized_daily_pnl):
        return False, "realized_daily_pnl must be finite"
    if open_positions >= limits.max_open_positions:
        return False, "maximum open positions reached"
    if realized_daily_pnl <= -limits.daily_loss_limit:
        return False, "daily loss limit reached"
    if proposed_risk > limits.hard_risk_ceiling_per_trade:
        return False, "proposed risk exceeds hard per-trade ceiling"
    if proposed_risk > limits.planned_risk_per_trade:
        return False, "proposed risk exceeds configured planned risk"
    if required_capital > available_capital:
        return False, "required capital exceeds available capital"
    if not limits.allow_leverage and required_capital > available_capital:
        return False, "trade would require leverage"
    return True, "allowed"
