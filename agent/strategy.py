"""Configurable long-only daily breakout strategy with explicit NO TRADE outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Sequence

from data.market_data import DailyCandle, validate_candles


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    ema_fast_period: int = 20
    ema_slow_period: int = 50
    rsi_period: int = 14
    volume_average_period: int = 20
    breakout_lookback_days: int = 20
    minimum_reward_to_risk: float = 2.0
    minimum_volume_ratio: float = 1.5
    maximum_rsi: float = 80.0
    stop_atr_multiple: float = 2.0
    max_data_age_minutes: int = 30

    def __post_init__(self) -> None:
        for name in ("ema_fast_period", "ema_slow_period", "rsi_period", "volume_average_period", "breakout_lookback_days"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 2:
                raise ValueError(f"{name} must be an integer of at least 2")
        if self.ema_fast_period >= self.ema_slow_period:
            raise ValueError("ema_fast_period must be less than ema_slow_period")
        for name in ("minimum_reward_to_risk", "minimum_volume_ratio", "maximum_rsi", "stop_atr_multiple"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if isinstance(self.max_data_age_minutes, bool) or not isinstance(self.max_data_age_minutes, int) or self.max_data_age_minutes < 0:
            raise ValueError("max_data_age_minutes must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class Signal:
    status: str
    symbol: str
    reason: str
    as_of: datetime
    entry_price: float | None = None
    stop_loss: float | None = None
    target_price: float | None = None
    reward_to_risk: float | None = None
    rsi: float | None = None
    volume_ratio: float | None = None


def _ema(values: Sequence[float], period: int) -> list[float]:
    alpha = 2 / (period + 1)
    result: list[float] = []
    current = values[0]
    for value in values:
        current = alpha * value + (1 - alpha) * current
        result.append(current)
    return result


def _rsi(values: Sequence[float], period: int) -> float:
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    if len(changes) < period:
        raise ValueError("not enough prices to calculate RSI")
    gains = [max(change, 0.0) for change in changes[-period:]]
    losses = [max(-change, 0.0) for change in changes[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    relative_strength = avg_gain / avg_loss
    return 100 - (100 / (1 + relative_strength))


def _atr(candles: Sequence[DailyCandle], period: int) -> float:
    true_ranges = []
    for i, candle in enumerate(candles):
        previous_close = candles[i - 1].close if i else candle.close
        true_ranges.append(max(candle.high - candle.low, abs(candle.high - previous_close), abs(candle.low - previous_close)))
    return sum(true_ranges[-period:]) / period


def evaluate_daily_breakout(
    *,
    symbol: str,
    candles: Sequence[DailyCandle],
    config: StrategyConfig,
    as_of: datetime,
) -> Signal:
    """Evaluate the latest completed candle; returns NO TRADE for any failed gate.

    Breakout resistance is computed from candles preceding the latest candle, so
    the signal does not use the current close to define its own resistance.
    """
    normalized_symbol = symbol.strip().upper()
    if not normalized_symbol:
        return Signal("NO_TRADE", symbol, "symbol is empty", as_of)
    required = max(config.ema_slow_period + 1, config.rsi_period + 1,
                   config.volume_average_period + 1, config.breakout_lookback_days + 1,
                   15)
    valid, reason = validate_candles(candles, min_count=required, as_of=as_of,
                                     max_age_minutes=config.max_data_age_minutes)
    if not valid:
        return Signal("NO_TRADE", normalized_symbol, reason, as_of)

    closes = [float(c.close) for c in candles]
    latest = candles[-1]
    fast = _ema(closes, config.ema_fast_period)
    slow = _ema(closes, config.ema_slow_period)
    rsi_value = _rsi(closes, config.rsi_period)
    prior_volumes = [c.volume for c in candles[-config.volume_average_period - 1:-1]]
    average_volume = sum(prior_volumes) / len(prior_volumes)
    volume_ratio = latest.volume / average_volume if average_volume > 0 else 0.0
    resistance = max(c.high for c in candles[-config.breakout_lookback_days - 1:-1])
    atr_value = _atr(candles[:-1], min(14, len(candles) - 1))

    failures = []
    if latest.close <= slow[-1]:
        failures.append("close is not above the slow EMA")
    if fast[-1] <= fast[-2]:
        failures.append("fast EMA is not rising")
    if latest.close <= resistance:
        failures.append("completed close did not break prior resistance")
    if volume_ratio < config.minimum_volume_ratio:
        failures.append("breakout volume is below the configured threshold")
    if rsi_value >= config.maximum_rsi:
        failures.append("RSI is too high; setup is extended")
    if failures:
        return Signal("NO_TRADE", normalized_symbol, "; ".join(failures), as_of,
                      rsi=rsi_value, volume_ratio=volume_ratio)

    entry = float(latest.close)
    stop = entry - config.stop_atr_multiple * atr_value
    if stop <= 0 or stop >= entry:
        return Signal("NO_TRADE", normalized_symbol, "calculated stop-loss is invalid", as_of,
                      rsi=rsi_value, volume_ratio=volume_ratio)
    risk = entry - stop
    target = entry + config.minimum_reward_to_risk * risk
    return Signal("LONG", normalized_symbol, "completed daily breakout meets configured filters", as_of,
                  entry_price=entry, stop_loss=stop, target_price=target,
                  reward_to_risk=(target - entry) / risk, rsi=rsi_value,
                  volume_ratio=volume_ratio)
