from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from app.common.types import OrderSide
from axiom_forex.portfolio.costs import ForexCostModel
from axiom_forex.portfolio.fx import MissingConversionRate, account_rate
from axiom_forex.portfolio.ledger import ForexPortfolio
from axiom_forex.portfolio.rollover import RolloverEngine, SwapRateCard
from axiom_forex.risk.aggregate import CorrelationMatrix, check_aggregate_exposure
from fixtures.m1 import (
    ACCOUNT_CURRENCY,
    BASE_RATE,
    CALENDAR_ID,
    CONVERSION_SOURCE,
    CORRELATION_GROUP,
    PAIR,
    PROVIDER_ID,
    PROVIDER_SYMBOL,
    QUANTITY,
    QUOTE_RATE,
    local_utc,
    make_account,
    make_calendar,
    make_cost_model,
    make_rollover_policy,
    make_spec,
)
from axiom_forex.data.fixtures import build_synthetic_bars, make_backtest_conversion_book


def _rollover_components(platform, *, triple_weekday, conversion_rates=None):
    rates = conversion_rates or platform.conversion_rates
    policy = make_rollover_policy(triple_rollover_weekday=triple_weekday)
    card = SwapRateCard(
        pair=PAIR,
        long_rate=Decimal("0.00001"),
        short_rate=Decimal("-0.00002"),
        source="TEST_SWAP_SOURCE",
        version="swap-test-v1",
    )
    engine = RolloverEngine(
        policy=policy,
        cards={PAIR: card},
        conversion_rates=rates,
        account=platform.account,
        calendar_id=CALENDAR_ID,
    )
    cost_model = ForexCostModel(
        terms=platform.m1_cost_model.terms,
        spec=platform.spec,
        account=platform.account,
        conversion_rates=rates,
        rollover_engine=engine,
    )
    portfolio = ForexPortfolio(
        account=platform.account,
        spec=platform.spec,
        conversion_rates=rates,
        cost_model=cost_model,
    )
    return engine, cost_model, portfolio


def test_account_rate_identity_explicit_and_missing_fail_closed(platform):
    at = platform.bars[0].timestamp
    assert account_rate(
        conversion_rates=platform.conversion_rates,
        account=platform.account,
        currency=ACCOUNT_CURRENCY,
        at=at,
    ) == Decimal(1)
    assert account_rate(
        conversion_rates=platform.conversion_rates,
        account=platform.account,
        currency="BBB",
        at=at,
    ) == QUOTE_RATE
    with pytest.raises(MissingConversionRate):
        account_rate(
            conversion_rates=platform.conversion_rates,
            account=platform.account,
            currency="DDD",
            at=at,
        )
    mismatched = replace(platform.conversion_rates, source="OTHER_SOURCE")
    with pytest.raises(MissingConversionRate):
        account_rate(
            conversion_rates=mismatched,
            account=platform.account,
            currency="BBB",
            at=at,
        )


def test_forex_cost_model_entry_and_exit_with_explicit_rollover(platform):
    bundle = platform.engine_factory()
    portfolio = bundle.portfolio
    first, second = platform.bars[0], platform.bars[1]
    position = portfolio.open(
        position_id="p1",
        side=OrderSide.BUY,
        quantity=QUANTITY,
        price=first.ask.open,
        at=first.timestamp,
    )
    assert position.costs.commission_account > 0
    assert position.costs.slippage_account > 0
    assert position.costs.financing_account == 0

    cutoff = first.timestamp + timedelta(minutes=1)
    exit_costs = platform.forex_cost_model.exit_costs(
        position=position,
        price=second.bid.open,
        at=second.timestamp,
        cutoffs=(cutoff,),
    )
    assert exit_costs.commission_account > 0
    assert exit_costs.slippage_account > 0
    assert exit_costs.financing_account > 0
    assert exit_costs.total_account > exit_costs.commission_account + exit_costs.slippage_account
    assert len(exit_costs.rollover_events) == 1
    assert exit_costs.rollover_events[0]["multiplier"] == 1
    assert exit_costs.rollover_events[0]["triple"] is False


def test_triple_rollover_uses_explicit_weekday_and_no_default_assumption(platform):
    spec = make_spec()
    calendar = make_calendar()
    bars = build_synthetic_bars(
        spec=spec,
        calendar=calendar,
        start_at=local_utc(2026, 3, 11, 10, 0),
        count=10,
    )
    rates = make_backtest_conversion_book(
        bars,
        spec=spec,
        account_currency=ACCOUNT_CURRENCY,
        quote_rate=QUOTE_RATE,
        base_rate=BASE_RATE,
        source=CONVERSION_SOURCE,
    )
    account = make_account()

    def components(triple_weekday):
        policy = make_rollover_policy(triple_rollover_weekday=triple_weekday)
        card = SwapRateCard(
            pair=PAIR,
            long_rate=Decimal("0.00001"),
            short_rate=Decimal("-0.00002"),
            source="TEST_SWAP_SOURCE",
            version="swap-test-v1",
        )
        engine = RolloverEngine(
            policy=policy,
            cards={PAIR: card},
            conversion_rates=rates,
            account=account,
            calendar_id=CALENDAR_ID,
        )
        cost_model = ForexCostModel(
            terms=make_cost_model().terms,
            spec=spec,
            account=account,
            conversion_rates=rates,
            rollover_engine=engine,
        )
        portfolio = ForexPortfolio(
            account=account,
            spec=spec,
            conversion_rates=rates,
            cost_model=cost_model,
        )
        return engine, portfolio

    triple_engine, triple_portfolio = components(2)
    plain_engine, plain_portfolio = components(None)
    position = triple_portfolio.open(
        position_id="triple",
        side=OrderSide.BUY,
        quantity=QUANTITY,
        price=bars[0].ask.open,
        at=bars[0].timestamp,
    )
    plain_position = plain_portfolio.open(
        position_id="plain",
        side=OrderSide.BUY,
        quantity=QUANTITY,
        price=bars[0].ask.open,
        at=bars[0].timestamp,
    )
    cutoff = bars[0].timestamp + timedelta(minutes=5)
    assert cutoff.weekday() == 2
    as_of = bars[-1].end_at
    triple = triple_engine.accrue(position, cutoffs=(cutoff,), as_of=as_of)
    plain = plain_engine.accrue(plain_position, cutoffs=(cutoff,), as_of=as_of)

    assert triple.events[0].multiplier == 3
    assert triple.events[0].triple is True
    assert plain.events[0].multiplier == 1
    assert plain.events[0].triple is False
    assert triple.total_account == plain.total_account * Decimal(3)


def test_rollover_missing_conversion_rate_fails_closed(platform):
    bad_rates = replace(platform.conversion_rates, source="OTHER_SOURCE")
    engine, _, _ = _rollover_components(
        platform, triple_weekday=None, conversion_rates=bad_rates
    )
    good_portfolio = platform.engine_factory().portfolio
    position = good_portfolio.open(
        position_id="p1",
        side=OrderSide.BUY,
        quantity=QUANTITY,
        price=platform.bars[0].ask.open,
        at=platform.bars[0].timestamp,
    )
    with pytest.raises(ValueError, match="missing_rollover_conversion_rate"):
        engine.accrue(
            position,
            cutoffs=(platform.bars[1].timestamp,),
            as_of=platform.bars[-1].end_at,
        )


def test_portfolio_marks_long_at_bid_and_short_at_ask(platform):
    portfolio = platform.engine_factory().portfolio
    first, second = platform.bars[0], platform.bars[1]
    long_position = portfolio.open(
        position_id="long",
        side=OrderSide.BUY,
        quantity=QUANTITY,
        price=first.ask.open,
        at=first.timestamp,
    )
    short_position = portfolio.open(
        position_id="short",
        side=OrderSide.SELL,
        quantity=QUANTITY,
        price=first.bid.open,
        at=first.timestamp,
    )
    assert portfolio._mark_price(long_position, second) == second.bid.close
    assert portfolio._mark_price(short_position, second) == second.ask.close


def test_portfolio_snapshot_and_risk_context_are_account_currency_normalized(platform):
    portfolio = platform.engine_factory().portfolio
    first, second = platform.bars[0], platform.bars[1]
    portfolio.open(
        position_id="p1",
        side=OrderSide.BUY,
        quantity=QUANTITY,
        price=first.ask.open,
        at=first.timestamp,
    )
    snapshot = portfolio.snapshot(second.end_at, second)
    assert snapshot["account_currency"] == ACCOUNT_CURRENCY
    assert snapshot["equity_account"] > 0
    assert snapshot["gross_exposure_account"] > 0
    assert snapshot["margin_account"] > 0
    assert snapshot["effective_leverage"] > 0
    assert ACCOUNT_CURRENCY not in snapshot["currency_exposures_account"]
    assert snapshot["correlation_group_exposures_account"][CORRELATION_GROUP] > 0
    context = portfolio.risk_context(second.end_at, second)
    assert context.validate().valid


def test_aggregate_risk_requires_explicit_correlations_and_limits(platform):
    matrix = platform.correlation_matrix
    limits = platform.aggregate_limits
    allowed = check_aggregate_exposure(
        matrix=matrix,
        limits=limits,
        pair_exposures_account={PAIR: Decimal("10")},
        currency_exposures_account={"AAA": Decimal("5")},
    )
    assert allowed.allowed
    assert allowed.adjusted_exposure_account == Decimal(10)

    missing = check_aggregate_exposure(
        matrix=matrix,
        limits=limits,
        pair_exposures_account={"DDD/EEE": Decimal("10")},
        currency_exposures_account={},
    )
    assert not missing.allowed
    assert any("missing_correlation" in reason for reason in missing.reasons)

    over_limit = check_aggregate_exposure(
        matrix=matrix,
        limits=limits,
        pair_exposures_account={PAIR: Decimal("2000")},
        currency_exposures_account={"AAA": Decimal("5")},
    )
    assert not over_limit.allowed
    assert "max_gross_exposure_exceeded" in over_limit.reasons

    invalid_matrix = replace(matrix, values={})
    invalid = check_aggregate_exposure(
        matrix=invalid_matrix,
        limits=limits,
        pair_exposures_account={PAIR: Decimal("10")},
        currency_exposures_account={},
    )
    assert not invalid.allowed
    assert "invalid_correlation_matrix" in invalid.reasons


def test_correlation_matrix_rejects_non_unit_diagonal():
    matrix = CorrelationMatrix(
        pairs=frozenset({PAIR}),
        values={(PAIR, PAIR): Decimal("0.90")},
        source="TEST_CORRELATION_SOURCE",
        version="correlation-test-v1",
    )
    assert not matrix.validate().valid
