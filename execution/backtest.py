"""Small, conservative historical evaluation for completed daily CSV candles.

This is research tooling, not a prediction. Results depend on CSV source quality,
survivorship, corporate actions, and simplified execution assumptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from agent.strategy import StrategyConfig, evaluate_daily_breakout
from data.csv_provider import CsvDailyDataProvider
from data.market_data import DailyCandle
from execution.paper_trader import CostModel


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    symbol: str
    signal_date: str
    entry_date: str
    exit_date: str
    quantity: int
    entry_price: float
    exit_price: float
    reason: str
    gross_pnl: float
    costs: float
    net_pnl: float


@dataclass(frozen=True, slots=True)
class BacktestSummary:
    source_directory: str
    trades: tuple[BacktestTrade, ...]
    gross_pnl: float
    costs: float
    net_pnl: float
    win_rate: float
    max_drawdown: float


def run_backtest(*, symbol: str, csv_directory: str | Path, strategy: StrategyConfig,
                 costs: CostModel, starting_capital: float = 10_000,
                 planned_risk: float = 200, gap_slippage_buffer_percent: float = 1.0) -> BacktestSummary:
    provider = CsvDailyDataProvider(csv_directory)
    candles = provider.get_daily_candles(symbol, 1_000_000)
    required = max(strategy.ema_slow_period + 1, strategy.rsi_period + 1,
                   strategy.volume_average_period + 1, strategy.breakout_lookback_days + 1, 15)
    output: list[BacktestTrade] = []
    index = required - 1
    cash = float(starting_capital)
    while index < len(candles) - 1:
        history = candles[:index + 1]
        signal_bar = history[-1]
        as_of = datetime.combine(signal_bar.session_date, time(23, 59),
                                 tzinfo=ZoneInfo("Asia/Kolkata"))
        signal = evaluate_daily_breakout(symbol=symbol, candles=history,
                                         config=strategy, as_of=as_of)
        if signal.status != "LONG":
            index += 1
            continue
        next_bar = candles[index + 1]
        entry_ref = max(next_bar.open, signal.entry_price)
        entry = entry_ref * (1 + costs.slippage_percent / 100)
        stop = signal.stop_loss
        risk_per_share = (entry - stop) * (1 + gap_slippage_buffer_percent / 100)
        quantity = min(int(planned_risk // risk_per_share), int(cash // entry))
        if quantity < 1:
            index += 1
            continue
        entry_notional = entry * quantity
        entry_cost = _cost(costs, entry_notional, is_exit=False)
        cash -= entry_notional + entry_cost
        exit_reference = next_bar.close
        exit_day = next_bar.session_date.isoformat()
        reason = "end_of_data"
        exit_index = index + 1
        for j in range(index + 1, len(candles)):
            bar = candles[j]
            if bar.open <= stop:
                exit_reference, reason = bar.open, "stop_loss_gap"
            elif bar.low <= stop:
                exit_reference, reason = stop, "stop_loss"
            elif bar.high >= signal.target_price:
                exit_reference, reason = signal.target_price, "target"
            else:
                continue
            exit_day = bar.session_date.isoformat()
            exit_index = j
            break
        exit = exit_reference * (1 - costs.slippage_percent / 100)
        exit_notional = exit * quantity
        exit_cost = _cost(costs, exit_notional, is_exit=True)
        gross = (exit - entry) * quantity
        total_cost = entry_cost + exit_cost
        net = gross - total_cost
        cash += exit_notional - exit_cost
        output.append(BacktestTrade(symbol.upper(), signal_bar.session_date.isoformat(),
            next_bar.session_date.isoformat(), exit_day, quantity, entry, exit, reason,
            gross, total_cost, net))
        index = exit_index + 1

    equity = peak = float(starting_capital)
    max_drawdown = 0.0
    for trade in output:
        equity += trade.net_pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    count = len(output)
    gross_pnl = sum(trade.gross_pnl for trade in output)
    total_costs = sum(trade.costs for trade in output)
    return BacktestSummary(str(Path(csv_directory).resolve()), tuple(output), gross_pnl,
        total_costs, gross_pnl - total_costs,
        sum(trade.net_pnl > 0 for trade in output) / count if count else 0.0,
        max_drawdown)


def _cost(model: CostModel, notional: float, *, is_exit: bool) -> float:
    result = model.brokerage_inr
    result += notional * (model.stt_rate + model.exchange_transaction_rate + model.stamp_duty_rate)
    result += notional * model.exchange_transaction_rate * model.gst_rate
    if is_exit:
        result += model.dp_charge_inr
    return result
