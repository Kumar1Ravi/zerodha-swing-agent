import csv
from datetime import date, timedelta, timezone, datetime

from agent.strategy import StrategyConfig
from execution.backtest import run_backtest
from execution.paper_trader import CostModel


def test_backtest_uses_local_csv_and_returns_summary(tmp_path):
    path = tmp_path / "ABC.csv"
    start = date(2024, 1, 1)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["date", "open", "high", "low", "close", "volume", "completed", "received_at"])
        writer.writeheader()
        for i in range(100):
            close = 100 + i * 0.2 + (0.5 if i % 2 == 0 else -0.5)
            writer.writerow({"date": (start + timedelta(days=i)).isoformat(), "open": close - 0.2,
                "high": close + 0.3, "low": close - 0.4, "close": close,
                "volume": 1000, "completed": "true",
                "received_at": datetime(2024, 5, 1, tzinfo=timezone.utc).isoformat()})
    summary = run_backtest(symbol="ABC", csv_directory=tmp_path,
        strategy=StrategyConfig(minimum_volume_ratio=0.5), costs=CostModel(),
        starting_capital=10_000, planned_risk=200)
    assert summary.source_directory == str(tmp_path.resolve())
    assert isinstance(summary.trades, tuple)
    assert summary.gross_pnl == sum(trade.gross_pnl for trade in summary.trades)
