"""Market-data contracts and validation for completed daily OHLCV candles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from math import isfinite
from typing import Protocol, Sequence


@dataclass(frozen=True, slots=True)
class DailyCandle:
    session_date: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    completed: bool = True
    received_at: datetime | None = None


class MarketDataProvider(Protocol):
    """Adapter boundary; implementations must document source and limitations."""

    def get_daily_candles(self, symbol: str, limit: int) -> Sequence[DailyCandle]: ...


def validate_candles(
    candles: Sequence[DailyCandle],
    *,
    min_count: int,
    as_of: datetime,
    max_age_minutes: int,
) -> tuple[bool, str]:
    if min_count < 1:
        return False, "minimum candle count must be positive"
    if max_age_minutes < 0:
        return False, "maximum data age must be non-negative"
    if len(candles) < min_count:
        return False, f"need at least {min_count} candles; received {len(candles)}"
    dates = [c.session_date for c in candles]
    if dates != sorted(dates) or len(set(dates)) != len(dates):
        return False, "candles must be unique and ordered by session date"
    if not candles[-1].completed:
        return False, "latest candle is incomplete"
    for candle in candles:
        if not isinstance(candle.session_date, date):
            return False, "candle session date is invalid"
        vals = (candle.open, candle.high, candle.low, candle.close)
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v) or v <= 0 for v in vals):
            return False, "candle contains an invalid OHLC price"
        if candle.high < max(candle.open, candle.close, candle.low) or candle.low > min(candle.open, candle.close, candle.high):
            return False, "candle OHLC values are inconsistent"
        if isinstance(candle.volume, bool) or not isinstance(candle.volume, int) or candle.volume < 0:
            return False, "candle volume must be a non-negative integer"
    latest = candles[-1]
    if latest.received_at is None:
        return False, "latest candle has no receipt timestamp; freshness cannot be verified"
    received = latest.received_at
    current = as_of
    if received.tzinfo is None or current.tzinfo is None:
        return False, "timestamps must include a timezone"
    age_minutes = (current.astimezone(timezone.utc) - received.astimezone(timezone.utc)).total_seconds() / 60
    if age_minutes < 0:
        return False, "latest candle timestamp is in the future"
    if age_minutes > max_age_minutes:
        return False, f"market data is stale ({age_minutes:.1f} minutes old)"
    return True, "valid"
