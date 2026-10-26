"""CSV adapter for daily OHLCV data; source and limitations remain caller-owned."""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

from data.market_data import DailyCandle


class CsvDailyDataProvider:
    """Read completed daily candles from one CSV per symbol.

    Required headers: date, open, high, low, close, volume, completed,
    received_at (ISO-8601 timestamp with timezone). This adapter does not
    provide or validate the licensing/source of the CSV data.
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def get_daily_candles(self, symbol: str, limit: int) -> list[DailyCandle]:
        if limit < 1:
            raise ValueError("limit must be positive")
        path = self.directory / f"{symbol.strip().upper()}.csv"
        candles = []
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"date", "open", "high", "low", "close", "volume", "completed", "received_at"}
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise ValueError(f"CSV must include columns: {', '.join(sorted(required))}")
            for row in reader:
                timestamp = datetime.fromisoformat(row["received_at"])
                candles.append(DailyCandle(
                    session_date=date.fromisoformat(row["date"]),
                    open=float(row["open"]), high=float(row["high"]),
                    low=float(row["low"]), close=float(row["close"]),
                    volume=int(row["volume"]), completed=row["completed"].strip().lower() == "true",
                    received_at=timestamp,
                ))
        return candles[-limit:]
