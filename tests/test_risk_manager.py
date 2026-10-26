from agent.risk_manager import RiskLimits, check_trade_allowed, size_long_position


def test_position_size_respects_risk_budget_and_rounds_down():
    result = size_long_position(
        entry_price=100, stop_loss=95, target_price=110, available_capital=10_000,
        limits=RiskLimits(planned_risk_per_trade=200, hard_risk_ceiling_per_trade=300,
                          gap_slippage_buffer_percent=0),
    )
    assert result.accepted
    assert result.quantity == 40
    assert result.planned_risk == 200
    assert result.required_capital == 4_000


def test_capital_can_limit_quantity_before_risk_budget():
    result = size_long_position(
        entry_price=100, stop_loss=95, target_price=110, available_capital=1_250,
        limits=RiskLimits(gap_slippage_buffer_percent=0),
    )
    assert result.accepted
    assert result.quantity == 12
    assert result.required_capital == 1_200


def test_insufficient_capital_for_one_share_rejects():
    result = size_long_position(
        entry_price=500, stop_loss=490, target_price=520, available_capital=100,
        limits=RiskLimits(),
    )
    assert not result.accepted
    assert result.quantity == 0
    assert "capital" in result.reason


def test_invalid_long_stop_and_target_reject():
    limits = RiskLimits()
    assert not size_long_position(entry_price=100, stop_loss=100, target_price=110,
                                  available_capital=1000, limits=limits).accepted
    assert not size_long_position(entry_price=100, stop_loss=90, target_price=100,
                                  available_capital=1000, limits=limits).accepted


def test_bad_prices_raise_value_error():
    limits = RiskLimits()
    for bad in (0, -1, float("nan"), float("inf")):
        try:
            size_long_position(entry_price=bad, stop_loss=90, target_price=110,
                               available_capital=1000, limits=limits)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for entry={bad!r}")


def test_buffer_and_costs_reduce_quantity():
    limits = RiskLimits(planned_risk_per_trade=200, gap_slippage_buffer_percent=10)
    result = size_long_position(entry_price=100, stop_loss=95, target_price=110,
                                available_capital=10_000, limits=limits,
                                estimated_cost_per_share=1)
    assert result.accepted
    assert result.planned_loss_per_share == 6.5
    assert result.quantity == 30
    assert result.planned_risk <= 200


def test_risk_limits_reject_higher_than_planned_or_hard_ceiling():
    limits = RiskLimits(planned_risk_per_trade=200, hard_risk_ceiling_per_trade=300)
    assert check_trade_allowed(proposed_risk=201, available_capital=1000, required_capital=500,
                               open_positions=0, realized_daily_pnl=0, limits=limits) == (
                                   False, "proposed risk exceeds configured planned risk")
    assert check_trade_allowed(proposed_risk=301, available_capital=1000, required_capital=500,
                               open_positions=0, realized_daily_pnl=0, limits=limits) == (
                                   False, "proposed risk exceeds hard per-trade ceiling")


def test_position_and_daily_loss_limits_block_new_trade():
    limits = RiskLimits(max_open_positions=1, daily_loss_limit=300)
    common = dict(proposed_risk=100, available_capital=1000, required_capital=500,
                  realized_daily_pnl=0, limits=limits)
    assert not check_trade_allowed(open_positions=1, **common)[0]
    assert not check_trade_allowed(open_positions=0, realized_daily_pnl=-300,
                                   **{k: v for k, v in common.items() if k != "realized_daily_pnl"})[0]


def test_capital_shortfall_is_rejected():
    allowed, reason = check_trade_allowed(proposed_risk=100, available_capital=400,
        required_capital=500, open_positions=0, realized_daily_pnl=0, limits=RiskLimits())
    assert not allowed
    assert "exceeds available capital" in reason


def test_limits_validate_configuration():
    for kwargs in (
        {"planned_risk_per_trade": 301},
        {"max_open_positions": 0},
        {"gap_slippage_buffer_percent": -1},
    ):
        try:
            RiskLimits(**kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected invalid limits to fail: {kwargs}")
