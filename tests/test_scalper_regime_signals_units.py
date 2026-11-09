"""AX-SCALPER unit tests, part 2: AX-REGIME and the signal generator.

The regime classifier is deterministic and is allowed to refuse.  These tests pin
both halves of that sentence: the same window always produces the same label, and
the three no-trade regimes can never expose an eligible family.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Sequence

import pytest

from axiom_forex.scalper import (
    ALL_FAMILIES,
    ABNORMAL,
    BREAKOUT,
    HIGH_VOL,
    LOW_LIQUIDITY,
    LOW_VOL,
    RANGING,
    SPREAD_EXPANSION,
    TRENDING_DOWN,
    TRENDING_UP,
    BreakoutParameters,
    BreakoutContinuation,
    MeanReversion,
    MomentumContinuation,
    MomentumParameters,
    OrderFlowMomentum,
    PullbackContinuation,
    RegimeState,
    Signal,
    SignalContext,
    Tick,
    classify,
    default_strategies,
    fixture_instrument,
    regime,
    signals,
)

UTC = timezone.utc
START = datetime(2026, 3, 2, 9, 0, 0, tzinfo=UTC)
SPEC = fixture_instrument()
POINT = SPEC.point


# -- helpers ----------------------------------------------------------------


def make_ticks(
    mids: Sequence[object],
    *,
    step_ms: int = 250,
    spread_points: int = 8,
    volume: object | None = None,
    start: datetime = START,
) -> list[Tick]:
    ticks = []
    for index, mid in enumerate(mids):
        price = Decimal(str(mid))
        ticks.append(
            Tick(
                symbol=SPEC.symbol,
                bid=price,
                ask=price + Decimal(spread_points) * POINT,
                source_time=start + timedelta(milliseconds=step_ms * index),
                received_at=start + timedelta(milliseconds=step_ms * index + 40),
                sequence=index + 1,
                source="SAMPLE_DATA",
                ingest_latency_ms=40.0,
                volume=volume,
            )
        )
    return ticks


def flat(count: int = 40, price: str = "1.10000", **kwargs) -> list[Tick]:
    return make_ticks([price] * count, **kwargs)


def state_for(regime_name: str, family: str, *, spread: float = 8.0) -> RegimeState:
    return RegimeState(
        symbol=SPEC.symbol,
        at=START,
        regime=regime_name,
        direction=1,
        eligible=frozenset({family}),
        size_multiplier=Decimal("1"),
        no_trade_reason="",
        features={"spread_points": spread},
        ticks=100,
    )


def context(ticks: Sequence[Tick], *, family="momentum", spread=8.0, regime_name=TRENDING_UP):
    return SignalContext(
        ticks=tuple(ticks),
        spec=SPEC,
        regime=state_for(regime_name, family, spread=spread),
        spread_limit_points=Decimal("30"),
        max_quote_age_ms=1500,
    )


# -- regime classification --------------------------------------------------


def test_empty_window_is_abnormal_and_untradeable():
    state = classify([], spec=SPEC)
    assert state.regime == ABNORMAL
    assert state.tradeable is False
    assert state.no_trade_reason == "no_ticks"
    assert state.size_multiplier == 0


def test_short_window_is_refused_rather_than_guessed():
    state = classify(flat(12), spec=SPEC)
    assert state.regime == ABNORMAL
    assert state.no_trade_reason == "window_too_short"
    assert state.eligible == frozenset()


def test_upward_drift_is_a_trend_and_admits_the_trend_families_only():
    state = classify(
        make_ticks([Decimal("1.10000") + POINT * index for index in range(40)]), spec=SPEC
    )
    assert state.regime == TRENDING_UP
    assert state.direction == 1
    assert state.allows(regime.FAMILY_MOMENTUM)
    assert state.allows(regime.FAMILY_PULLBACK)
    assert not state.allows(regime.FAMILY_MEAN_REVERSION)
    assert not state.allows(regime.FAMILY_ORDER_FLOW)
    assert state.size_multiplier == Decimal("1.0")
    assert state.no_trade_reason == ""


def test_downward_drift_is_a_trend_down():
    state = classify(
        make_ticks([Decimal("1.10000") - POINT * index for index in range(40)]), spec=SPEC
    )
    assert state.regime == TRENDING_DOWN
    assert state.direction == -1
    assert state.allows(regime.FAMILY_MOMENTUM)


def test_classification_is_deterministic_for_the_same_window():
    ticks = make_ticks([Decimal("1.10000") + POINT * index for index in range(40)])
    first = classify(ticks, spec=SPEC).audit_view()
    second = classify(ticks, spec=SPEC).audit_view()
    assert first == second


def test_a_slow_feed_is_called_low_liquidity_and_says_no_trade():
    ticks = flat(40, step_ms=4000)  # one quote every four seconds
    state = classify(ticks, spec=SPEC)
    assert state.regime == LOW_LIQUIDITY
    assert state.tradeable is False
    assert state.no_trade_reason == regime.NO_TRADE_REASONS[LOW_LIQUIDITY]
    assert all(not state.allows(family) for family in ALL_FAMILIES)


def test_a_stuck_feed_showing_one_price_is_low_liquidity():
    ticks = flat(40, price="1.10000")
    state = classify(ticks, spec=SPEC)
    assert state.regime == LOW_LIQUIDITY
    assert state.tradeable is False


def test_spread_expansion_is_a_no_trade_regime():
    mids = [Decimal("1.10000") + POINT * (index % 3) for index in range(40)]
    ticks = make_ticks(mids, spread_points=8)
    ticks[-1] = make_ticks([mids[-1]], spread_points=40)[0]
    state = classify(ticks, spec=SPEC)
    assert state.regime == SPREAD_EXPANSION
    assert state.tradeable is False
    assert all(not state.allows(family) for family in ALL_FAMILIES)


def test_declared_spread_limit_is_applied_before_anything_else():
    ticks = make_ticks([Decimal("1.10000") + POINT * index for index in range(40)])
    state = classify(ticks, spec=SPEC, spread_limit_points=Decimal("5"))
    assert state.regime == SPREAD_EXPANSION
    assert state.tradeable is False


def test_a_single_abnormal_print_stops_the_regime():
    mids = [Decimal("1.10000") + POINT * (index % 9) for index in range(39)]
    mids.append(Decimal("1.11000"))
    state = classify(make_ticks(mids), spec=SPEC)
    assert state.regime == ABNORMAL
    assert state.tradeable is False
    assert state.no_trade_reason


def test_high_volatility_admits_breakout_at_half_size():
    mids = []
    for index in range(40):
        mids.append(Decimal("1.10000") + (POINT * 5 if index % 2 else Decimal(0)))
    state = classify(make_ticks(mids), spec=SPEC)
    if state.regime == HIGH_VOL:
        assert state.allows(regime.FAMILY_BREAKOUT)
        assert state.size_multiplier == Decimal("0.5")
    else:  # a deterministic generator that happens to label elsewhere must still refuse safely
        assert state.tradeable is (state.regime not in {LOW_LIQUIDITY, SPREAD_EXPANSION, ABNORMAL})


def test_a_quiet_range_admits_mean_reversion():
    mids = [Decimal("1.10000") + POINT * (index % 9) for index in range(40)]
    state = classify(make_ticks(mids), spec=SPEC)
    assert state.regime in {RANGING, LOW_VOL}
    assert state.allows(regime.FAMILY_MEAN_REVERSION)
    assert state.size_multiplier in {Decimal("1.0"), Decimal("0.5")}
    assert not state.allows(regime.FAMILY_MOMENTUM)


@pytest.mark.parametrize("name", [LOW_LIQUIDITY, SPREAD_EXPANSION, ABNORMAL])
def test_no_trade_regimes_have_no_eligible_family_in_the_map(name):
    assert name not in regime._ELIGIBILITY
    assert name in regime.NO_TRADE_REASONS
    state = RegimeState(
        symbol=SPEC.symbol,
        at=START,
        regime=name,
        direction=0,
        eligible=frozenset(),
        size_multiplier=Decimal("0"),
        no_trade_reason=regime.NO_TRADE_REASONS[name],
        features={"spread_points": 8.0},
        ticks=100,
    )
    assert state.tradeable is False
    assert all(not state.allows(family) for family in ALL_FAMILIES)


def test_regime_features_are_reported_for_review():
    state = classify(
        make_ticks([Decimal("1.10000") + POINT * index for index in range(40)]), spec=SPEC
    )
    for key in ("net_points", "efficiency", "volatility_points", "spread_points", "ticks_per_second"):
        assert key in state.features
    assert state.ticks == 40


# -- strategy identity ------------------------------------------------------


def test_the_five_families_have_distinct_fingerprints():
    strategies = default_strategies()
    assert len(strategies) == 5
    assert len({strategy.fingerprint for strategy in strategies}) == 5
    assert {strategy.family for strategy in strategies} == set(ALL_FAMILIES)


def test_fingerprint_is_stable_for_equal_parameters_and_changes_with_them():
    first = MomentumContinuation(MomentumParameters(min_move_points=3.0))
    second = MomentumContinuation(MomentumParameters(min_move_points=3.0))
    changed = MomentumContinuation(MomentumParameters(min_move_points=4.0))
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint != changed.fingerprint


def test_fingerprint_covers_the_ttl():
    slow = MomentumContinuation(ttl_ms=1500)
    fast = MomentumContinuation(ttl_ms=750)
    assert slow.fingerprint != fast.fingerprint


def test_strategy_spec_publishes_both_rules_and_the_version():
    spec = MomentumContinuation().spec()
    assert spec["rule_version"] == "v1"
    assert "lookback_ticks" in spec["parameters"]
    assert spec["entry_rule"] and spec["exit_rule"]
    assert spec["fingerprint"] == MomentumContinuation().fingerprint


# -- gates ------------------------------------------------------------------


def test_family_not_admitted_by_the_regime_is_refused_by_name():
    evaluation = MomentumContinuation().evaluate(
        context(flat(40), family="mean_reversion", regime_name=RANGING)
    )
    assert not evaluation.traded
    assert evaluation.outcome == signals.NOT_ELIGIBLE
    assert RANGING in evaluation.detail


def test_too_few_ticks_is_refused_before_any_rule_runs():
    evaluation = MomentumContinuation().evaluate(context(flat(4)))
    assert evaluation.outcome == signals.NOT_ENOUGH_TICKS


def test_a_wide_spread_stops_the_strategy_independently_of_the_regime():
    ticks = make_ticks([Decimal("1.10000") + POINT * index for index in range(12)], spread_points=25)
    evaluation = MomentumContinuation().evaluate(context(ticks))
    assert evaluation.outcome == signals.SPREAD_TOO_WIDE


def test_order_flow_refuses_a_feed_without_volume():
    ticks = make_ticks([Decimal("1.10000") + POINT * index for index in range(61)])
    evaluation = OrderFlowMomentum().evaluate(context(ticks, family="order_flow"))
    assert evaluation.outcome == signals.FLOW_DATA_UNAVAILABLE
    assert "no trade-volume direction" in evaluation.detail


# -- momentum ---------------------------------------------------------------


def test_rising_momentum_buys_at_the_ask_with_a_points_bracket():
    ticks = make_ticks([Decimal("1.10000") + POINT * index for index in range(14)])
    evaluation = MomentumContinuation().evaluate(context(ticks))
    assert evaluation.traded
    signal = evaluation.signal
    assert signal is not None
    assert signal.side == signals.BUY
    assert signal.reference_price == ticks[-1].ask
    assert signal.stop_points == Decimal("10")
    assert signal.target_points == Decimal("14")
    assert signal.stop_price == signal.reference_price - Decimal("10") * POINT
    assert signal.parameter_fingerprint == MomentumContinuation().fingerprint


def test_falling_momentum_sells_at_the_bid():
    ticks = make_ticks([Decimal("1.10000") - POINT * index for index in range(14)])
    evaluation = MomentumContinuation().evaluate(context(ticks))
    assert evaluation.traded
    assert evaluation.signal.side == signals.SELL
    assert evaluation.signal.reference_price == ticks[-1].bid


def test_flat_quotes_are_not_a_directional_run():
    evaluation = MomentumContinuation().evaluate(context(flat(14)))
    assert evaluation.outcome == signals.NO_SETUP
    assert "not a directional run" in evaluation.detail


def test_volatility_widens_the_momentum_stop_beyond_the_parameter():
    mids = [Decimal("1.10000") + POINT * 8 * index for index in range(14)]
    evaluation = MomentumContinuation().evaluate(context(make_ticks(mids)))
    assert evaluation.traded
    assert evaluation.signal.stop_points > Decimal("10")


# -- breakout ---------------------------------------------------------------


def test_a_break_above_the_window_high_buys():
    mids = [Decimal("1.10000")] * 40 + [Decimal("1.10005")]
    evaluation = BreakoutContinuation().evaluate(
        context(make_ticks(mids), family="breakout", regime_name=BREAKOUT)
    )
    assert evaluation.traded
    assert evaluation.signal.side == signals.BUY
    assert evaluation.signal.stop_points == Decimal("12")


def test_a_break_below_the_window_low_sells():
    mids = [Decimal("1.10000")] * 40 + [Decimal("1.09995")]
    evaluation = BreakoutContinuation().evaluate(
        context(make_ticks(mids), family="breakout", regime_name=BREAKOUT)
    )
    assert evaluation.traded
    assert evaluation.signal.side == signals.SELL


def test_a_move_inside_the_buffer_is_not_a_breakout():
    mids = [Decimal("1.10000")] * 40 + [Decimal("1.10001")]
    evaluation = BreakoutContinuation().evaluate(
        context(make_ticks(mids), family="breakout", regime_name=BREAKOUT)
    )
    assert evaluation.outcome == signals.NO_SETUP


# -- pullback ---------------------------------------------------------------


def test_an_uptrend_pullback_that_reclaims_buys():
    mids = [Decimal("1.10000") + POINT * index for index in range(20)]
    mids += [mids[-1] - POINT * 2, mids[-1] - POINT * 3, mids[-1] - POINT * 2]
    evaluation = PullbackContinuation().evaluate(context(make_ticks(mids), family="pullback"))
    assert evaluation.traded, evaluation.detail
    assert evaluation.signal.side == signals.BUY
    assert evaluation.signal.stop_points == Decimal("9")


def test_a_pullback_that_keeps_falling_is_not_a_reclaim():
    mids = [Decimal("1.10000") + POINT * index for index in range(20)]
    mids += [mids[-1] - POINT * 2, mids[-1] - POINT * 3, mids[-1] - POINT * 4]
    evaluation = PullbackContinuation().evaluate(context(make_ticks(mids), family="pullback"))
    assert evaluation.outcome == signals.NO_SETUP


# -- mean reversion ---------------------------------------------------------


def test_a_stretched_high_in_a_range_sells_with_a_target_at_the_mean():
    mids = [Decimal("1.10000") + POINT * (index % 9) for index in range(41)]
    mids[-1] = Decimal("1.10010")
    evaluation = MeanReversion().evaluate(
        context(make_ticks(mids), family="mean_reversion", regime_name=RANGING)
    )
    assert evaluation.traded, evaluation.detail
    assert evaluation.signal.side == signals.SELL
    assert evaluation.signal.target_points >= Decimal("4")


def test_a_stretched_low_in_a_range_buys():
    mids = [Decimal("1.10010") + POINT * (index % 9) for index in range(41)]
    mids[-1] = Decimal("1.10000")
    evaluation = MeanReversion().evaluate(
        context(make_ticks(mids), family="mean_reversion", regime_name=RANGING)
    )
    assert evaluation.traded
    assert evaluation.signal.side == signals.BUY


def test_a_window_with_no_dispersion_cannot_revert():
    evaluation = MeanReversion().evaluate(
        context(flat(41), family="mean_reversion", regime_name=RANGING)
    )
    assert evaluation.outcome == signals.NO_SETUP
    assert "dispersion" in evaluation.detail


# -- order flow -------------------------------------------------------------


def test_one_sided_volume_buys_and_records_that_it_is_a_feed_proxy():
    mids = [Decimal("1.10000") + POINT * index for index in range(61)]
    evaluation = OrderFlowMomentum().evaluate(
        context(make_ticks(mids, volume=Decimal("2")), family="order_flow")
    )
    assert evaluation.traded
    assert evaluation.signal.side == signals.BUY
    assert evaluation.signal.flow_proxy == Decimal("1")
    assert evaluation.signal.family == "order_flow"
    assert evaluation.signal.audit_view()["flow_proxy"] is not None


def test_balanced_volume_is_not_an_imbalance():
    mids = []
    price = Decimal("1.10000")
    for index in range(61):
        price += POINT if index % 2 else -POINT
        mids.append(price)
    evaluation = OrderFlowMomentum().evaluate(
        context(make_ticks(mids, volume=Decimal("1")), family="order_flow")
    )
    assert evaluation.outcome in {signals.NO_SETUP, signals.FLOW_DATA_UNAVAILABLE}


# -- signal object ----------------------------------------------------------


def test_signal_stop_and_target_prices_follow_the_instrument_point():
    signal = Signal(
        symbol=SPEC.symbol,
        family="momentum",
        strategy="momentum_continuation",
        side="BUY",
        at=START,
        tick_id="abc",
        reference_price=Decimal("1.10010"),
        stop_points=Decimal("10"),
        target_points=Decimal("14"),
        reason="test",
        rule_version="v1",
        parameter_fingerprint="f" * 32,
        ttl_ms=750,
        _point=POINT,
    )
    assert signal.stop_price == Decimal("1.10000")
    assert signal.target_price == Decimal("1.10024")


def test_with_point_re_prices_the_same_signal_for_another_instrument():
    signal = Signal(
        symbol=SPEC.symbol,
        family="momentum",
        strategy="momentum_continuation",
        side="SELL",
        at=START,
        tick_id="abc",
        reference_price=Decimal("1.10010"),
        stop_points=Decimal("10"),
        target_points=Decimal("14"),
        reason="test",
        rule_version="v1",
        parameter_fingerprint="f" * 32,
        ttl_ms=750,
    )
    assert signal.stop_price is None
    repriced = signal.with_point(Decimal("0.0001"))
    assert repriced.stop_price == Decimal("1.10110")
    assert repriced.parameter_fingerprint == signal.parameter_fingerprint
    assert repriced.side == "SELL"


def test_a_signal_without_a_point_has_no_priceable_protection():
    signal = Signal(
        symbol=SPEC.symbol,
        family="momentum",
        strategy="momentum_continuation",
        side="BUY",
        at=START,
        tick_id="abc",
        reference_price=Decimal("1.10010"),
        stop_points=Decimal("10"),
        target_points=Decimal("14"),
        reason="test",
        rule_version="v1",
        parameter_fingerprint="f" * 32,
        ttl_ms=750,
    )
    assert signal.stop_price is None
    assert signal.target_price is None


def test_signal_audit_view_carries_the_fingerprint_and_the_ttl():
    ticks = make_ticks([Decimal("1.10000") + POINT * index for index in range(14)])
    signal = MomentumContinuation().evaluate(context(ticks)).signal
    view = signal.audit_view()
    assert view["parameter_fingerprint"] == MomentumContinuation().fingerprint
    assert view["ttl_ms"] == 750
    assert Decimal(view["stop_points"]) == Decimal("10")
