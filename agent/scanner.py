"""Scan a supplied universe through the configured daily-candle adapter."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from agent.config import load_config
from agent.strategy import StrategyConfig, evaluate_daily_breakout
from data.csv_provider import CsvDailyDataProvider


def scan_symbols(symbols: list[str], *, config_path: str | Path = "config.yaml",
                 as_of: datetime | None = None):
    config = load_config(config_path)
    strategy_cfg = StrategyConfig(**config.get("strategy", {}),
        max_data_age_minutes=config["market_data"].get("max_data_age_minutes", 30))
    provider_cfg = config["market_data"]
    if provider_cfg.get("provider") != "csv":
        raise ValueError("no supported market-data provider is configured")
    directory = Path(provider_cfg.get("csv_directory", "data/market"))
    if not directory.is_absolute():
        directory = Path(config_path).resolve().parent / directory
    provider = CsvDailyDataProvider(directory)
    now = as_of or datetime.now().astimezone()
    required = max(strategy_cfg.ema_slow_period + 1, strategy_cfg.rsi_period + 1,
                   strategy_cfg.volume_average_period + 1,
                   strategy_cfg.breakout_lookback_days + 1, 15)
    output = []
    for symbol in symbols:
        try:
            candles = provider.get_daily_candles(symbol, required)
            output.append(evaluate_daily_breakout(symbol=symbol, candles=candles,
                                                  config=strategy_cfg, as_of=now))
        except (OSError, ValueError) as exc:
            from agent.strategy import Signal
            output.append(Signal("NO_TRADE", symbol.strip().upper(),
                                 f"market data unavailable: {exc}", now))
    return output
