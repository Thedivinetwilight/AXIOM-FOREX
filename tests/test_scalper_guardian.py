"""AX-GUARDIAN: the account-level authority.

Two properties matter more than any single limit:

* entry suspension and position management are **separate**, so a limit that trips
  can never lock the desk out of an exit;
* limits are **not** writable by the agents they constrain.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from axiom_forex.risk.aggregate import CorrelationMatrix
from axiom_forex.scalper.config import fixture_config
from axiom_forex.scalper.guardian import (
    CORRELATED_EXPOSURE,
    CURRENCY_CONCENTRATION,
    DAILY_LOSS_LIMIT,
    DRAWDOWN_LIMIT,
    EMERGENCY_STOPPED,
    ENTRIES_SUSPENDED,
    EQUITY_UNKNOWN,
    LEVERAGE_LIMIT,
    LIMITS_NOT_AGENT_CONFIGURABLE,
    LOSS_STREAK_LIMIT,
    MAX_OPEN_POSITIONS,
    PER_TRADE_RISK,
    QUOTE_STALE,
    SPREAD_TOO_WIDE,
    Guardian,
    GuardianLimits,
    OpenPositionRisk,
    Proposal,
)

CONFIG = fixture_config()


def guardian(**kwargs) -> Guardian:
    instance = Guardian(
        GuardianLimits.from_config(CONFIG),
        declared_slippage_points=CONFIG.cost.slippage_points,
        **kwargs,
    )
    instance.observe_account(
        equity=Decimal("10000"), balance=Decimal("10000"), at_ms=0, day_key="2026-03-02"
    )
    return instance


def proposal(**overrides) -> Proposal:
    arguments = dict(
        symbol="EURUSD.pro",
        pair="EUR/USD",
        side="BUY",
        risk_account=Decimal("10"),
        notional_account=Decimal("1000"),
        stop_points=Decimal("25"),
        spread_points=Decimal("8"),
        quote_age_ms=10,
        decision_latency_ms=20,
        fill_latency_ms=30,
        slippage_points=Decimal("1"),
        currencies=("EUR", "USD"),
    )
    arguments.update(overrides)
    return Proposal(**arguments)


def open_position(risk: str = "10", notional: str = "1000", **overrides) -> OpenPositionRisk:
    arguments = dict(
        position_id="p1",
        symbol="EURUSD.pro",
        pair="EUR/USD",
        side="BUY",
        risk_account=Decimal(risk),
        notional_account=Decimal(notional),
        currencies=("EUR", "USD"),
        protected=True,
    )
    arguments.update(overrides)
    return OpenPositionRisk(**arguments)


# ---------------------------------------------------------------------------
# limits are not writable by agents
# ---------------------------------------------------------------------------


def test_limits_are_frozen():
    limits = GuardianLimits.from_config(CONFIG)
    with pytest.raises(Exception):
        limits.max_daily_loss_fraction = Decimal("0.9")  # type: ignore[misc]


def test_an_agent_asking_for_a_wider_limit_is_refused_and_recorded():
    answer = guardian().request_limit_change(
        requested_by="momentum_continuation", max_daily_loss_fraction=Decimal("0.5")
    )
    assert answer["accepted"] is False
    assert answer["reason"] == LIMITS_NOT_AGENT_CONFIGURABLE
    assert "No strategy" in answer["detail"]


def test_invalid_limits_prevent_construction():
    limits = GuardianLimits.from_config(CONFIG)
    broken = GuardianLimits(**{**limits.__dict__, "max_daily_loss_fraction": Decimal("0")})
    with pytest.raises(ValueError):
        Guardian(broken)


# ---------------------------------------------------------------------------
# the checks
# ---------------------------------------------------------------------------


def test_a_clean_proposal_is_allowed_and_says_why():
    decision = guardian().evaluate(proposal())
    assert decision.allowed is True
    assert decision.reasons == ()
    names = {check.name for check in decision.checks}
    assert {"per_trade_risk", "aggregate_risk", "leverage", "spread", "quote_freshness"} <= names


def test_per_trade_risk_above_the_cap_is_refused():
    decision = guardian().evaluate(proposal(risk_account=Decimal("500")))
    assert decision.allowed is False
    assert PER_TRADE_RISK in decision.reasons


def test_aggregate_open_risk_is_capped():
    instance = guardian()
    for index in range(3):
        instance.record_open(open_position(risk="40", position_id=f"p{index}"))
    decision = instance.evaluate(proposal(risk_account=Decimal("10")))
    assert "aggregate_open_risk_exceeded" in decision.reasons


def test_the_open_position_count_is_capped():
    instance = guardian()
    for index in range(CONFIG.max_open_positions):
        instance.record_open(
            open_position(risk="1", notional="100", position_id=f"p{index}", pair="EUR/GBP")
        )
    decision = instance.evaluate(proposal())
    assert MAX_OPEN_POSITIONS in decision.reasons


def test_leverage_is_capped():
    instance = guardian()
    decision = instance.evaluate(proposal(notional_account=Decimal("60000")))
    assert LEVERAGE_LIMIT in decision.reasons


def test_currency_concentration_is_capped():
    instance = guardian()
    decision = instance.evaluate(proposal(notional_account=Decimal("40000")))
    assert CURRENCY_CONCENTRATION in decision.reasons


def test_missing_correlation_information_fails_closed():
    instance = guardian()
    instance.record_open(open_position(risk="1", notional="1000", pair="EUR/GBP"))
    decision = instance.evaluate(proposal())
    assert decision.allowed is False
    assert CORRELATED_EXPOSURE in decision.reasons
    detail = " ".join(check.detail for check in decision.checks if check.name == "correlated_exposure")
    assert "cannot be checked" in detail


def test_a_loaded_correlation_matrix_lets_uncorrelated_pairs_through():
    matrix = CorrelationMatrix(
        pairs=frozenset({"EUR/USD", "EUR/GBP"}),
        values={
            ("EUR/USD", "EUR/USD"): Decimal("1"),
            ("EUR/GBP", "EUR/GBP"): Decimal("1"),
            ("EUR/USD", "EUR/GBP"): Decimal("0.10"),
            ("EUR/GBP", "EUR/USD"): Decimal("0.10"),
        },
        source="TEST_MATRIX",
        version="v1",
    )
    instance = guardian(correlations=matrix)
    instance.record_open(open_position(risk="1", notional="1000", pair="EUR/GBP"))
    decision = instance.evaluate(proposal())
    assert decision.allowed is True


def test_highly_correlated_exposure_is_refused_even_with_a_matrix():
    matrix = CorrelationMatrix(
        pairs=frozenset({"EUR/USD", "GBP/USD"}),
        values={
            ("EUR/USD", "EUR/USD"): Decimal("1"),
            ("GBP/USD", "GBP/USD"): Decimal("1"),
            ("EUR/USD", "GBP/USD"): Decimal("0.92"),
            ("GBP/USD", "EUR/USD"): Decimal("0.92"),
        },
        source="TEST_MATRIX",
        version="v1",
    )
    instance = guardian(correlations=matrix)
    for index in range(CONFIG.max_correlated_positions):
        instance.record_open(
            open_position(risk="1", notional="100", pair="GBP/USD", position_id=f"p{index}")
        )
    decision = instance.evaluate(proposal())
    assert CORRELATED_EXPOSURE in decision.reasons


def test_a_wide_spread_stops_the_entry():
    decision = guardian().evaluate(proposal(spread_points=Decimal("40")))
    assert SPREAD_TOO_WIDE in decision.reasons


def test_an_unobserved_spread_stops_the_entry():
    decision = guardian().evaluate(proposal(spread_points=None))
    assert "spread_not_observed" in decision.reasons


def test_a_stale_quote_stops_the_entry():
    decision = guardian().evaluate(proposal(quote_age_ms=5000))
    assert QUOTE_STALE in decision.reasons


def test_unknown_equity_stops_the_entry():
    instance = Guardian(GuardianLimits.from_config(CONFIG), declared_slippage_points=Decimal("1"))
    decision = instance.evaluate(proposal())
    assert EQUITY_UNKNOWN in decision.reasons


def test_no_fill_latency_is_stated_not_invented():
    decision = guardian().evaluate(proposal(fill_latency_ms=None))
    check = next(item for item in decision.checks if item.name == "fill_latency")
    assert check.passed is True
    assert "no fill observed yet" in check.detail


def test_no_slippage_falls_back_to_the_declared_estimate():
    decision = guardian().evaluate(proposal(slippage_points=None))
    check = next(item for item in decision.checks if item.name == "slippage")
    assert check.passed is True
    assert "declared estimate" in check.detail


def test_an_observed_slippage_above_the_limit_is_refused():
    decision = guardian().evaluate(proposal(slippage_points=Decimal("50")))
    assert "slippage_limit_exceeded" in decision.reasons


def test_losses_in_a_row_suspend_entries():
    instance = guardian()
    for index in range(CONFIG.max_loss_streak):
        instance.record_open(open_position(position_id=f"p{index}"))
        instance.record_close(f"p{index}", realised=Decimal("-5"), at_ms=index)
    allowed, reasons = instance.may_open()
    assert allowed is False
    assert LOSS_STREAK_LIMIT in reasons
    assert LOSS_STREAK_LIMIT in instance.suspended_reasons


def test_a_win_clears_the_loss_streak():
    instance = guardian()
    for index in range(2):
        instance.record_open(open_position(position_id=f"p{index}"))
        instance.record_close(f"p{index}", realised=Decimal("-5"), at_ms=index)
    instance.record_open(open_position(position_id="p9"))
    instance.record_close("p9", realised=Decimal("8"), at_ms=9)
    assert instance.loss_streak == 0


def test_a_drawdown_suspends_entries():
    instance = guardian()  # peak equity 10,000 on 2026-03-02
    # A new day resets the daily baseline, so this is a drawdown and not a daily loss.
    instance.observe_account(equity=Decimal("9300"), at_ms=10, day_key="2026-03-03")
    allowed, reasons = instance.may_open()
    assert allowed is False
    assert DRAWDOWN_LIMIT in reasons


def test_a_daily_loss_suspends_entries_for_the_day():
    instance = guardian()
    instance.observe_account(equity=Decimal("9700"), at_ms=10, day_key="2026-03-02")
    allowed, reasons = instance.may_open()
    assert allowed is False
    assert DAILY_LOSS_LIMIT in reasons


def test_a_new_day_resets_the_daily_baseline():
    instance = guardian()
    instance.observe_account(equity=Decimal("9700"), at_ms=10, day_key="2026-03-03")
    allowed, _reasons = instance.may_open()
    assert allowed is True


def test_resuming_needs_a_named_operator_and_a_cleared_streak():
    instance = guardian()
    for index in range(CONFIG.max_loss_streak):
        instance.record_open(open_position(position_id=f"p{index}"))
        instance.record_close(f"p{index}", realised=Decimal("-5"), at_ms=index)
    ok, detail = instance.resume_entries(reason=LOSS_STREAK_LIMIT, operator="  ")
    assert ok is False
    ok, detail = instance.resume_entries(reason=LOSS_STREAK_LIMIT, operator="risk-desk")
    assert ok is False
    assert "still at the limit" in detail


def test_a_daily_loss_suspension_can_be_resumed_by_an_operator():
    instance = guardian()
    instance.observe_account(equity=Decimal("9600"), at_ms=10, day_key="2026-03-02")
    instance.record_open(open_position(position_id="p1"))
    instance.record_close("p1", realised=Decimal("-400"), at_ms=11)
    ok, detail = instance.resume_entries(reason=DAILY_LOSS_LIMIT, operator="risk-desk")
    assert ok is True
    assert "risk-desk" in detail
    assert instance.may_open()[0] is True


# ---------------------------------------------------------------------------
# suspension versus management
# ---------------------------------------------------------------------------


def test_entries_can_be_suspended_without_losing_the_ability_to_manage():
    instance = guardian()
    instance.suspend_entries("operator_hold")
    assert instance.may_open()[0] is False
    assert instance.may_manage() is True
    decision = instance.evaluate(proposal())
    assert ENTRIES_SUSPENDED in decision.reasons


def test_an_emergency_stop_records_the_positions_it_does_not_abandon():
    instance = guardian()
    instance.record_open(open_position(risk="12", notional="1200"))
    plan = instance.emergency_stop(reason="operator emergency", at_ms=1000)
    assert plan.entries_suspended is True
    assert plan.position_management_allowed is True
    assert len(plan.protected_positions) == 1
    assert plan.protected_positions[0]["position_id"] == "p1"
    assert instance.may_open()[0] is False
    assert EMERGENCY_STOPPED in instance.may_open()[1]
    assert instance.may_manage() is True
    assert "never sends an order" in plan.flatten_instruction
    assert "not abandoned" in plan.note


def test_an_emergency_stop_lists_unprotected_positions_by_name():
    instance = guardian()
    instance.record_open(open_position(protected=False))
    plan = instance.emergency_stop(reason="protection lost", at_ms=1000)
    assert plan.unprotected_positions == ("p1",)


def test_an_emergency_stop_needs_an_operator_to_clear():
    instance = guardian()
    instance.emergency_stop(reason="test", at_ms=1)
    ok, _ = instance.clear_emergency(operator="")
    assert ok is False
    ok, detail = instance.clear_emergency(operator="risk-desk")
    assert ok is True
    assert "risk-desk" in detail
    assert instance.may_open()[0] is True


def test_status_reports_the_state_a_reader_needs():
    instance = guardian()
    instance.record_open(open_position())
    status = instance.status()
    assert status["entries_allowed"] is True
    assert status["position_management_allowed"] is True
    assert status["open_risk"] == "10"
    assert status["limits"]["immutable"] is True
    assert status["correlations_loaded"] is False
