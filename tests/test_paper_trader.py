from datetime import datetime, timezone

import pytest

from execution.paper_trader import CostModel, PaperTrader


T0 = datetime(2025, 1, 2, 10, tzinfo=timezone.utc)
T1 = datetime(2025, 1, 3, 10, tzinfo=timezone.utc)


def trader(tmp_path, **kwargs):
    return PaperTrader(tmp_path / "paper.sqlite3", starting_capital=10_000, **kwargs)


def open_trade(paper, **overrides):
    args = dict(symbol="abc", quantity=10, entry_price=100, stop_loss=90,
                target_price=120, signal_key="signal-1", timestamp=T0)
    args.update(overrides)
    return paper.open_position(**args)


def test_entry_persists_and_restart_restores_open_position(tmp_path):
    paper = trader(tmp_path)
    opened = open_trade(paper)
    assert opened.status == "OPEN"
    assert opened.symbol == "ABC"
    assert opened.entry_price == pytest.approx(100.1)
    reopened = PaperTrader(tmp_path / "paper.sqlite3", starting_capital=1)
    assert reopened.open_positions()[0].id == opened.id
    assert reopened.available_capital() == pytest.approx(8_999)


def test_duplicate_signal_key_rejected(tmp_path):
    paper = trader(tmp_path)
    open_trade(paper)
    with pytest.raises(ValueError, match="duplicate"):
        open_trade(paper)


def test_stop_exit_accounts_for_slippage_and_costs(tmp_path):
    paper = trader(tmp_path, cost_model=CostModel(slippage_percent=0.1, brokerage_inr=5))
    opened = open_trade(paper)
    closed = paper.close_position(opened.id, reference_price=90, reason="stop", timestamp=T1)
    assert closed.status == "CLOSED"
    assert closed.exit_price == pytest.approx(89.91)
    assert closed.gross_pnl == pytest.approx((89.91 - 100.1) * 10)
    assert closed.costs == pytest.approx(10)
    assert closed.net_pnl == pytest.approx(closed.gross_pnl - 10)


def test_stop_first_when_stop_and_target_touched_same_candle(tmp_path):
    paper = trader(tmp_path)
    opened = open_trade(paper)
    closed = paper.evaluate_exit(opened.id, candle_open=100, candle_high=125,
        candle_low=89, candle_close=110, timestamp=T1)
    assert closed.exit_reason == "stop_loss"
    assert closed.exit_price < opened.stop_loss


def test_gap_below_stop_uses_open_price(tmp_path):
    paper = trader(tmp_path)
    opened = open_trade(paper)
    closed = paper.evaluate_exit(opened.id, candle_open=85, candle_high=95,
        candle_low=84, candle_close=90, timestamp=T1)
    assert closed.exit_reason == "stop_loss_gap"
    assert closed.exit_price < opened.stop_loss


def test_target_exit_and_daily_summary(tmp_path):
    paper = trader(tmp_path)
    opened = open_trade(paper)
    paper.evaluate_exit(opened.id, candle_open=115, candle_high=121,
        candle_low=114, candle_close=120, timestamp=T1)
    summary = paper.daily_summary("2025-01-03")
    assert summary.closed_trades == 1
    assert summary.gross_pnl > 0
    assert summary.net_pnl == pytest.approx(summary.gross_pnl - summary.costs)
    assert summary.win_rate == 1
    assert summary.drawdown == 0


def test_daily_risk_pnl_includes_open_position_mark(tmp_path):
    paper = trader(tmp_path)
    open_trade(paper)
    assert paper.daily_risk_pnl("2025-01-02", marks={"ABC": 80}) == pytest.approx(-201)


def test_insufficient_cash_and_invalid_position_rejected(tmp_path):
    paper = PaperTrader(tmp_path / "small.sqlite3", starting_capital=100)
    with pytest.raises(ValueError, match="insufficient"):
        open_trade(paper)
    with pytest.raises(ValueError, match="stop_loss"):
        open_trade(paper, stop_loss=101)


def test_invalid_cost_configuration_rejected():
    with pytest.raises(ValueError):
        CostModel(slippage_percent=-1)
