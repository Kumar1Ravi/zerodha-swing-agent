"""Run a local CSV historical evaluation. No network access or broker calls."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path

from agent.config import load_config
from agent.strategy import StrategyConfig
from execution.backtest import run_backtest
from execution.paper_trader import CostModel


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbol", help="NSE symbol matching SYMBOL.csv")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    config = load_config(args.config)
    strategy = StrategyConfig(**config.get("strategy", {}),
        max_data_age_minutes=config["market_data"].get("max_data_age_minutes", 30))
    execution = config["execution"]
    cost_model = CostModel(**{
        "slippage_percent": execution.get("simulated_slippage_percent", 0),
        "brokerage_inr": execution.get("brokerage_inr", 0),
        "stt_rate": execution.get("stt_rate", 0),
        "exchange_transaction_rate": execution.get("exchange_transaction_rate", 0),
        "gst_rate": execution.get("gst_rate", 0),
        "stamp_duty_rate": execution.get("stamp_duty_rate", 0),
        "dp_charge_inr": execution.get("dp_charge_inr", 0),
    })
    risk = config["risk"]
    summary = run_backtest(symbol=args.symbol,
        csv_directory=config["market_data"].get("csv_directory", "data/market"),
        strategy=strategy, costs=cost_model,
        starting_capital=risk["starting_capital_inr"],
        planned_risk=risk["planned_risk_per_trade_inr"],
        gap_slippage_buffer_percent=risk["gap_slippage_buffer_percent"])
    print(f"Source directory: {summary.source_directory}")
    print(f"Trades: {len(summary.trades)}")
    print(f"Gross P/L: INR {summary.gross_pnl:.2f}")
    print(f"Estimated costs: INR {summary.costs:.2f}")
    print(f"Net P/L: INR {summary.net_pnl:.2f}")
    print(f"Win rate: {summary.win_rate:.1%}")
    print(f"Max drawdown: INR {summary.max_drawdown:.2f}")
    print("Research result only; historical performance does not predict future results.")


if __name__ == "__main__":
    main()
