from datetime import datetime, timezone

import mongomock
import pytest

from execution.mongo_paper_trader import MongoPaperTrader


def trader():
    client = mongomock.MongoClient()
    return MongoPaperTrader("mongodb://unused", database_name="paper_trading_test",
                            starting_capital=10_000, client=client)


def test_mongo_adapter_persists_open_close_and_daily_summary():
    paper = trader()
    opened = paper.open_position(symbol="abc", quantity=10, entry_price=100,
        stop_loss=90, target_price=120, signal_key="unique-1",
        timestamp=datetime(2025, 1, 2, tzinfo=timezone.utc))
    assert opened.id == 1
    assert paper.available_capital() == pytest.approx(8_999)
    closed = paper.close_position(opened.id, reference_price=110, reason="target",
        timestamp=datetime(2025, 1, 3, tzinfo=timezone.utc))
    assert closed.status == "CLOSED"
    assert closed.net_pnl == pytest.approx(97.9)
    summary = paper.daily_summary("2025-01-03")
    assert summary.closed_trades == 1
    assert summary.net_pnl == pytest.approx(97.9)


def test_mongo_adapter_duplicate_and_insufficient_cash_rejected():
    paper = trader()
    args = dict(symbol="ABC", quantity=1, entry_price=100, stop_loss=95,
                target_price=110, signal_key="dup")
    paper.open_position(**args)
    with pytest.raises(ValueError, match="duplicate"):
        paper.open_position(**args)
    with pytest.raises(ValueError, match="insufficient"):
        paper.open_position(symbol="HIGH", quantity=1_000, entry_price=100,
            stop_loss=95, target_price=110, signal_key="large")
