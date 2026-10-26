from datetime import date, datetime, timedelta, timezone

from agent.strategy import StrategyConfig, evaluate_daily_breakout
from data.market_data import DailyCandle, validate_candles


NOW = datetime(2025, 1, 1, 12, tzinfo=timezone.utc)


def make_candles(count=60):
    candles = []
    start = date(2024, 10, 1)
    for i in range(count):
        close = 100 + i * 0.25 + (0.5 if i % 2 == 0 else -0.5)
        candles.append(DailyCandle(
            session_date=start + timedelta(days=i), open=close - 0.2,
            high=close + 0.4, low=close - 0.5, close=close,
            volume=1000, completed=True, received_at=NOW,
        ))
    return candles


def test_incomplete_or_stale_candles_rejected():
    candles = make_candles()
    candles[-1] = DailyCandle(**{**candles[-1].__dict__, "completed": False}) if hasattr(candles[-1], "__dict__") else DailyCandle(
        session_date=candles[-1].session_date, open=candles[-1].open,
        high=candles[-1].high, low=candles[-1].low, close=candles[-1].close,
        volume=candles[-1].volume, completed=False, received_at=NOW)
    assert not validate_candles(candles, min_count=50, as_of=NOW, max_age_minutes=30)[0]


def test_strategy_returns_no_trade_when_breakout_filter_fails():
    candles = make_candles()
    signal = evaluate_daily_breakout(symbol="abc", candles=candles,
        config=StrategyConfig(minimum_volume_ratio=2.0), as_of=NOW)
    assert signal.status == "NO_TRADE"
    assert "volume" in signal.reason


def test_strategy_can_emit_signal_without_using_incomplete_candle():
    candles = make_candles()
    last = candles[-1]
    candles[-1] = DailyCandle(last.session_date, 116.0, 118.0, 115.5, 117.5,
                             5000, True, NOW)
    signal = evaluate_daily_breakout(symbol="abc", candles=candles,
        config=StrategyConfig(minimum_volume_ratio=1.5), as_of=NOW)
    assert signal.status == "LONG"
    assert signal.entry_price == 117.5
    assert signal.stop_loss < signal.entry_price < signal.target_price
    assert signal.reward_to_risk >= 2


def test_stale_data_returns_no_trade():
    candles = make_candles()
    signal = evaluate_daily_breakout(symbol="ABC", candles=candles,
        config=StrategyConfig(max_data_age_minutes=5),
        as_of=NOW + timedelta(minutes=10))
    assert signal.status == "NO_TRADE"
    assert "stale" in signal.reason
