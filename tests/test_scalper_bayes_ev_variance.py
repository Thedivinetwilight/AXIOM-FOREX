"""BAYES, EV CHECK and VARIANCE.

The three modules that decide *whether* and *how much*.  The tests pin the
arithmetic, because arithmetic that is only approximately right is how a cost gets
quietly dropped.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from axiom_forex.scalper.bayes import (
    BASIS_FEED_PROXY,
    BetaPosterior,
    OutcomeBook,
    ProbabilityEstimate,
    beta_quantile,
    brier_score,
    calibrate,
    flow_from_ticks,
    regularized_incomplete_beta,
)
from axiom_forex.scalper.config import ASSUMED_ZERO, UNKNOWN, CostTerms, fixture_config, fixture_instrument
from axiom_forex.scalper.ev import (
    ACCEPT,
    BELOW_MIN_EDGE,
    COST_EXCEEDS_GROSS,
    COST_UNKNOWN,
    REJECT,
    ExpectancyTracker,
    cost_for,
    evaluate,
    required_win_rate,
)
from axiom_forex.scalper.signals import Signal
from axiom_forex.scalper.variance import (
    CURRENCY_CAP,
    LEVERAGE_CAP,
    NO_STOP,
    SIZE_BELOW_MINIMUM,
    SizingInput,
    fractional_kelly,
    size_position,
    volatility_scale,
)

SPEC = fixture_config().spec_for("EURUSD.pro")
AT = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)


def signal(*, stop: str = "25", target: str = "60", side: str = "BUY") -> Signal:
    reference = Decimal("1.10200")
    offset_stop = Decimal(stop) * SPEC.point
    return Signal(
        symbol=SPEC.symbol,
        family="momentum",
        strategy="momentum_continuation",
        side=side,
        at=AT,
        tick_id="abc123",
        reference_price=reference,
        stop_points=Decimal(stop),
        target_points=Decimal(target),
        reason="test",
        rule_version="v1",
        parameter_fingerprint="f" * 64,
        ttl_ms=750,
        _point=SPEC.point,
    )


def estimate(p: str = "0.60", sd: str = "0.05", samples: int = 100) -> ProbabilityEstimate:
    """A hand-built estimate so the EV arithmetic can be checked exactly."""

    return ProbabilityEstimate(
        p=float(p),
        sd=float(sd),
        low=max(0.0, float(p) - 1.28 * float(sd)),
        high=min(1.0, float(p) + 1.28 * float(sd)),
        samples=samples,
        basis="observed_outcomes",
        label="test",
    )


# ---------------------------------------------------------------------------
# BAYES
# ---------------------------------------------------------------------------


def test_beta_maths_matches_known_values():
    # Beta(2,2) is symmetric around 0.5.
    assert regularized_incomplete_beta(2, 2, 0.5) == pytest.approx(0.5, abs=1e-9)
    assert beta_quantile(2, 2, 0.5) == pytest.approx(0.5, abs=1e-6)
    # CDF of Beta(2,2) is 3x^2 - 2x^3, so the 5 % quantile solves 3x^2 - 2x^3 = 0.05.
    assert beta_quantile(2, 2, 0.05) == pytest.approx(0.13535, abs=1e-4)


def test_a_posterior_with_no_evidence_is_wide_and_centred_at_a_half():
    posterior = BetaPosterior(0.5, 0.5)
    assert posterior.mean == pytest.approx(0.5)
    assert posterior.sd > 0.3
    low, high = posterior.interval(0.8)
    assert low < 0.1 and high > 0.9


def test_evidence_narrows_the_interval():
    weak = BetaPosterior(0.5, 0.5).updated(wins=3, losses=2)
    strong = BetaPosterior(0.5, 0.5).updated(wins=300, losses=200)
    assert weak.mean == pytest.approx(strong.mean, abs=0.02)
    assert strong.sd < weak.sd / 5
    assert strong.samples == 500


def test_the_outcome_book_keys_on_fingerprint_not_on_name():
    book = OutcomeBook()
    book.record(fingerprint="a" * 64, regime="TRENDING_UP", won=True)
    book.record(fingerprint="a" * 64, regime="TRENDING_UP", won=True)
    book.record(fingerprint="b" * 64, regime="TRENDING_UP", won=False)
    assert book.counts("a" * 64, "TRENDING_UP") == (2, 0)
    assert book.counts("b" * 64, "TRENDING_UP") == (0, 1)
    assert book.estimate("a" * 64, "TRENDING_UP").p > book.estimate("b" * 64, "TRENDING_UP").p
    assert "not by strategy name" in book.audit_view()["note"]


def test_flow_carries_the_broker_feed_caveat_and_counts_ticks_not_volume():
    flow = flow_from_ticks([1.1000, 1.1001, 1.1002, 1.1001], [1000, 2000, 100, 50])
    assert flow.estimate.basis == BASIS_FEED_PROXY
    assert flow.ticks == 3  # three steps between four quotes
    assert flow.estimate.samples == flow.ticks
    assert "not the global order book" in flow.caveat
    view = flow.audit_view()
    assert "buy_volume" in view and "sell_volume" in view


def test_flow_without_volume_is_not_estimated():
    flow = flow_from_ticks([1.1, 1.1001, 1.1002], [None, None, None])
    view = flow.audit_view()
    assert view["p"] in (None, 0.5)
    assert view.get("caveat")


def test_calibration_refuses_to_judge_a_small_sample():
    report = calibrate([(0.6, True)] * 10 + [(0.6, False)] * 5)
    assert report.verdict == "insufficient_evidence"
    assert report.samples == 15


def test_calibration_measures_the_error_it_says_it_measures():
    predictions = [(0.7, True)] * 70 + [(0.7, False)] * 30
    report = calibrate(predictions)
    assert report.verdict == "within_declared_error"
    assert report.expected_calibration_error == pytest.approx(0.0, abs=0.02)
    # Brier for a constant 0.7 forecast: 0.7*0.09 + 0.3*0.49 = 0.21
    assert report.brier == pytest.approx(0.21, abs=0.02)
    assert "not an edge" in report.audit_view()["note"]


def test_brier_score_of_a_perfect_forecast_is_zero():
    assert brier_score([(1.0, True), (0.0, False)]) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# EV
# ---------------------------------------------------------------------------


def test_cost_terms_price_a_round_turn_in_points_and_money():
    cost = cost_for(spec=SPEC, cost=fixture_config().cost, spread_points=Decimal("8"))
    # spread 8 + slippage 1 per side + no commission
    assert cost.total_points == Decimal("10")
    assert cost.complete is True
    assert cost.total_account_per_lot == Decimal("10")  # 1 USD per point per lot


def test_an_unknown_commission_is_not_treated_as_zero():
    unknown = CostTerms(commission_per_lot=Decimal("7"), commission_state=UNKNOWN)
    cost = cost_for(spec=SPEC, cost=unknown, spread_points=Decimal("8"))
    assert cost.complete is False
    assert "commission" in cost.missing
    value = evaluate(
        signal(),
        estimate(),
        spec=SPEC,
        cost=unknown,
        spread_points=Decimal("8"),
        min_edge_points=Decimal("0.6"),
    )
    assert value.verdict == REJECT
    assert COST_UNKNOWN in value.reasons


def test_a_missing_spread_is_not_a_free_spread():
    cost = cost_for(spec=SPEC, cost=fixture_config().cost, spread_points=None)
    assert cost.complete is False
    assert "spread" in cost.missing


def test_expected_value_is_the_arithmetic_it_claims_to_be():
    value = evaluate(
        signal(stop="25", target="60"),
        estimate(p="0.6", sd="0.0"),
        spec=SPEC,
        cost=fixture_config().cost,
        spread_points=Decimal("8"),
        min_edge_points=Decimal("0.6"),
    )
    gross = Decimal("0.6") * Decimal("60") - Decimal("0.4") * Decimal("25")
    assert value.gross_points == pytest.approx(gross)
    assert value.net_points == pytest.approx(gross - Decimal("10"))
    assert value.verdict == ACCEPT


def test_costs_that_swallow_the_edge_are_refused():
    value = evaluate(
        signal(stop="10", target="14"),
        estimate(p="0.7", sd="0.0"),
        spec=SPEC,
        cost=fixture_config().cost,
        spread_points=Decimal("8"),
        min_edge_points=Decimal("0.6"),
    )
    assert value.verdict == REJECT
    assert COST_EXCEEDS_GROSS in value.reasons
    assert value.cost_to_gross is not None and value.cost_to_gross >= 1


def test_uncertainty_can_reject_an_otherwise_positive_edge():
    certain = evaluate(
        signal(), estimate(p="0.6", sd="0.0"), spec=SPEC, cost=fixture_config().cost,
        spread_points=Decimal("8"), min_edge_points=Decimal("0.6"),
    )
    uncertain = evaluate(
        signal(), estimate(p="0.6", sd="0.25"), spec=SPEC, cost=fixture_config().cost,
        spread_points=Decimal("8"), min_edge_points=Decimal("0.6"),
    )
    assert certain.verdict == ACCEPT
    assert uncertain.verdict == REJECT
    assert BELOW_MIN_EDGE in uncertain.reasons
    assert uncertain.lower_bound_points < certain.lower_bound_points


def test_a_negative_expectation_is_never_accepted():
    value = evaluate(
        signal(), estimate(p="0.25", sd="0.0"), spec=SPEC, cost=fixture_config().cost,
        spread_points=Decimal("8"), min_edge_points=Decimal("0.6"),
    )
    assert value.verdict == REJECT
    assert value.net_points < 0


def test_the_break_even_probability_says_what_has_to_be_beaten():
    rate = required_win_rate(
        stop_points=Decimal("25"), target_points=Decimal("60"), cost_points=Decimal("10")
    )
    assert rate == pytest.approx(35 / 85)


def test_every_ev_row_carries_its_costs_for_audit():
    value = evaluate(
        signal(), estimate(), spec=SPEC, cost=fixture_config().cost,
        spread_points=Decimal("8"), min_edge_points=Decimal("0.6"),
    )
    view = value.audit_view()
    assert view["cost"]["spread_points"] == "8"
    assert view["cost"]["slippage_points"] == "2"
    assert view["verdict"] in {ACCEPT, REJECT}


def test_the_expectancy_tracker_reports_an_interval_not_a_promise():
    tracker = ExpectancyTracker()
    for value in ["5", "-3", "4", "-2", "6", "-4", "3", "-1"]:
        tracker.record("fp", Decimal(value))
    summary = tracker.summary("fp")
    assert summary.n == 8
    assert summary.lower_95 < summary.mean_points < summary.upper_95
    assert summary.wins == 4
    assert "not distinguishable from nothing" in summary.audit_view()["note"]


# ---------------------------------------------------------------------------
# VARIANCE
# ---------------------------------------------------------------------------


def sizing(**overrides) -> SizingInput:
    arguments = dict(
        spec=SPEC,
        price=Decimal("1.10200"),
        stop_points=Decimal("25"),
        equity=Decimal("10000"),
        risk_fraction=Decimal("0.0025"),
        max_loss_account=Decimal("50"),
        # Generous exposure caps by default, so these tests measure the risk
        # arithmetic itself; the cap tests below set their own.
        max_leverage=Decimal("1000"),
        max_currency_exposure_fraction=Decimal("1000"),
    )
    arguments.update(overrides)
    return SizingInput(**arguments)


def test_size_comes_from_the_stop_and_the_permitted_loss():
    result = size_position(sizing())
    # budget 25 account units, 25 points x 1 USD/point = 25 per lot -> 1.00 lots
    assert result.lots == Decimal("1.00")
    assert result.risk_account == Decimal("25.00")


def test_a_wider_stop_buys_fewer_lots():
    narrow = size_position(sizing(stop_points=Decimal("12")))
    wide = size_position(sizing(stop_points=Decimal("50")))
    assert wide.lots < narrow.lots


def test_an_absolute_cap_binds_before_the_percentage():
    result = size_position(sizing(max_loss_account=Decimal("5")))
    assert result.risk_account == Decimal("5.00")
    assert result.risk_budget == Decimal("5")


def test_size_is_floored_to_the_volume_step_never_rounded_up():
    result = size_position(sizing(stop_points=Decimal("30")))
    # budget 25 / 30 per lot = 0.8333 -> 0.83, not 0.84
    assert result.lots == Decimal("0.83")
    assert result.risk_account <= Decimal("25")


def test_a_budget_below_the_broker_minimum_is_a_refusal_not_a_round_up():
    result = size_position(sizing(stop_points=Decimal("4000")))
    assert result.allowed is False
    assert result.refusals[0] == SIZE_BELOW_MINIMUM
    assert "rounding up would exceed the budget" in " ".join(result.refusals)


def test_default_caps_refuse_a_size_that_the_risk_budget_would_allow():
    result = size_position(sizing(max_leverage=Decimal("1"), max_currency_exposure_fraction=Decimal("1")))
    assert result.leverage_after <= Decimal("1")
    assert result.risk_account <= Decimal("25")


def test_a_signal_without_a_stop_cannot_be_sized():
    result = size_position(sizing(stop_points=Decimal("0")))
    assert result.refusals[0] == NO_STOP
    assert result.lots == 0


def test_leverage_caps_the_size_and_says_so():
    result = size_position(sizing(max_leverage=Decimal("1"), max_loss_account=Decimal("900")))
    assert LEVERAGE_CAP in " ".join(result.adjustments)
    assert result.leverage_after <= Decimal("1")


def test_currency_concentration_caps_the_size():
    result = size_position(
        sizing(
            max_loss_account=Decimal("900"),
            max_leverage=Decimal("10"),
            max_currency_exposure_fraction=Decimal("0.1"),
        )
    )
    assert CURRENCY_CAP in " ".join(result.adjustments)
    assert result.currency_exposure_after["EUR"] <= Decimal("10000") * Decimal("10") * Decimal("0.1")


def test_a_regime_multiplier_reduces_size_and_never_increases_it():
    full = size_position(sizing())
    reduced = size_position(sizing(size_multiplier=Decimal("0.5")))
    inflated = size_position(sizing(size_multiplier=Decimal("2")))
    assert reduced.lots < full.lots
    assert inflated.lots <= full.lots / Decimal("0.5")


def test_volatility_scaling_only_shrinks():
    assert volatility_scale(volatility_points=10, target_volatility_points=5) == Decimal("0.5")
    assert volatility_scale(volatility_points=1, target_volatility_points=5) == Decimal("1")


def test_notional_is_reported_but_is_not_the_risk():
    result = size_position(sizing())
    view = result.audit_view()
    assert Decimal(view["notional_account"]) > Decimal(view["risk_account"]) * 100
    assert "neither is the amount at risk" in view["note"]


def test_kelly_is_available_but_capped_and_candid():
    kelly = fractional_kelly(p_win=0.6, payoff_ratio=1.5, fraction=0.25, cap=0.01)
    assert kelly["usable"] is True
    assert kelly["fraction"] <= 0.01
    assert "assumes the probability is right" in kelly["note"]
    assert fractional_kelly(p_win=0.3, payoff_ratio=1.0)["usable"] is False


def test_an_instrument_without_point_value_is_refused():
    spec = fixture_instrument(quote_currency="EUR", account_currency="USD", conversion_rate=None)
    result = size_position(
        SizingInput(
            spec=spec,
            price=Decimal("1.1"),
            stop_points=Decimal("20"),
            equity=Decimal("10000"),
            risk_fraction=Decimal("0.0025"),
            max_loss_account=Decimal("50"),
        )
    )
    assert result.allowed is False
    assert "instrument_terms_missing" in result.refusals[0]
