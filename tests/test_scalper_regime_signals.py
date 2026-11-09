"""AX-REGIME and the five signal families.

The regime's most important output is the ability to say "no".  The strategies'
most important property is that their parameters are visible in a fingerprint, so
a change cannot ride on an old result.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from axiom_forex.scalper import regime as regime_module
from axiom_forex.scalper.config import fixture_config
from axiom_forex.scalper.regime import (
    ABNORMAL,
    BREAKOUT,
    LOW_LIQUIDITY,
    RANGING,
    SPREAD_EXPANSION,
    TRENDING_DOWN,
    TRENDING_UP,
    RegimeState,
    RegimeThresholds,
    classify,
)
from axiom_forex.scalper.signals import (
    FLOW_DATA_UNAVAILABLE,
    NO_SETUP,
    NOT_ELIGIBLE,
    SPREAD_TOO_WIDE,
    BreakoutContinuation,
    BreakoutParameters,
    MeanReversion,
    MeanReversionParameters,
    MomentumContinuation,
    MomentumParameters,
    OrderFlowMomentum,
    OrderFlowParameters,
    PullbackContinuation,
    PullbackParameters,
    SignalContext,
    default_strategies,
)
from axiom_forex.scalper.ticks import ReplayTick, TickCore, synthetic_ticks

SYMBOL = "EURUSD.pro"
START = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
SPEC = fixture_config().spec_for(SYMBOL)


def make_core(config=None):
    config = config or fixture_config()
    return TickCore(
        instruments=config.instruments,
        max_quote_age_ms=config.max_quote_age_ms,
        max_spread_points=config.max_spread_points,
        gap_threshold_ms=config.gap_threshold_ms,
        min_window_ticks=config.min_window_ticks,
        tick_window=config.tick_window,
    )


def ticks_from(replay, *, core=None):
    core = core or make_core()
    accepted = []
    for tick in replay:
        result = core.ingest(
            symbol=tick.symbol, bid=tick.bid, ask=tick.ask, source_time=tick.at, received_at=tick.at,
            volume=tick.volume,
        )
        if result.accepted:
            accepted.append(result.tick)
    return tuple(accepted)


def series(points, *, spread_points: str = "8", step_ms: int = 200, volume=None):
    """Build ticks from mid offsets in points, so a test can state its own shape."""

    point = Decimal("0.00001")
    spread = Decimal(spread_points) * point
    base = Decimal("1.10000")
    replay = []
    for index, offset in enumerate(points):
        mid = base + Decimal(str(offset)) * point
        half = spread / 2
        replay.append(
            ReplayTick(
                symbol=SYMBOL,
                bid=mid - half,
                ask=mid + half,
                at=START + timedelta(milliseconds=step_ms * index),
                volume=None if volume is None else Decimal(str(volume[index])),
            )
        )
    return replay


def state(regime: str, eligible=(), *, direction: int = 0, multiplier: str | None = None) -> RegimeState:
    multiplier = multiplier if multiplier is not None else ("1" if eligible else "0")
    return RegimeState(
        symbol=SYMBOL,
        at=START,
        regime=regime,
        direction=direction,
        eligible=frozenset(eligible),
        size_multiplier=Decimal(multiplier),
        no_trade_reason="" if eligible else "no_trade",
        features={"spread_points": 8.0},
        ticks=100,
    )


def context(ticks, regime_name, eligible, *, spread_limit: str = "30"):
    return SignalContext(
        ticks=tuple(ticks),
        spec=SPEC,
        regime=state(regime_name, eligible),
        spread_limit_points=Decimal(spread_limit),
        max_quote_age_ms=1500,
    )


# ---------------------------------------------------------------------------
# AX-REGIME
# ---------------------------------------------------------------------------


def test_a_clean_run_is_trending_and_admits_momentum_and_pullback():
    ticks = ticks_from(synthetic_ticks(count=120, drift_points=2.0, noise_points=0.4))
    result = classify(ticks, spec=SPEC)
    assert result.regime == TRENDING_UP
    assert result.direction == 1
    assert result.eligible == {regime_module.FAMILY_MOMENTUM, regime_module.FAMILY_PULLBACK}
    assert result.size_multiplier > 0


def test_a_clean_decline_is_trending_down():
    ticks = ticks_from(synthetic_ticks(count=120, drift_points=-2.0, noise_points=0.4))
    result = classify(ticks, spec=SPEC)
    assert result.regime == TRENDING_DOWN
    assert result.direction == -1


def test_an_oscillating_window_is_ranging_and_prefers_mean_reversion():
    ticks = ticks_from(synthetic_ticks(count=160, drift_points=0.0, noise_points=20.0))
    result = classify(ticks, spec=SPEC)
    assert result.regime == RANGING
    assert regime_module.FAMILY_MEAN_REVERSION in result.eligible
    assert regime_module.FAMILY_MOMENTUM not in result.eligible


def test_spread_expansion_is_a_no_trade_regime():
    replay = series([0] * 40 + [1] * 40, spread_points="8")
    ticks = ticks_from(replay)
    wide = ticks_from(series([0] * 40 + [1] * 40, spread_points="80"))
    result = classify(wide, spec=SPEC, spread_limit_points=Decimal("30"))
    assert result.regime in {SPREAD_EXPANSION, ABNORMAL}
    assert result.eligible == frozenset()
    assert result.no_trade_reason


def test_a_repeated_price_window_is_low_liquidity_not_a_quiet_trend():
    result = classify(ticks_from(series([0] * 60)), spec=SPEC)
    assert result.regime == LOW_LIQUIDITY
    assert result.eligible == frozenset()
    assert result.size_multiplier == 0


def test_an_empty_window_is_abnormal_and_tradeable_by_nobody():
    result = classify((), spec=SPEC)
    assert result.regime == ABNORMAL
    assert result.eligible == frozenset()
    assert result.no_trade_reason == "no_ticks"
    assert result.size_multiplier == 0


def test_undecided_between_trend_and_range_is_abnormal_not_a_guess():
    thresholds = RegimeThresholds(
        trend_efficiency=Decimal("0.90"), range_efficiency=Decimal("0.01")
    )
    ticks = ticks_from(synthetic_ticks(count=160, drift_points=0.0, noise_points=20.0))
    result = classify(ticks, spec=SPEC, thresholds=thresholds)
    assert result.regime == ABNORMAL
    assert result.no_trade_reason == "undecided_between_trend_and_range"
    assert result.eligible == frozenset()


def test_classification_is_deterministic():
    ticks = ticks_from(synthetic_ticks(count=120, drift_points=1.5, noise_points=1.2))
    first = classify(ticks, spec=SPEC)
    second = classify(ticks, spec=SPEC)
    assert first.regime == second.regime
    assert first.features == second.features


def test_no_trade_regimes_have_no_eligibility_entry():
    for name in (ABNORMAL, LOW_LIQUIDITY, SPREAD_EXPANSION):
        assert regime_module._ELIGIBILITY.get(name) is None
    state_ = state(ABNORMAL)
    assert state_.allows(regime_module.FAMILY_MOMENTUM) is False
    assert state_.size_multiplier == 0


# ---------------------------------------------------------------------------
# signal identity
# ---------------------------------------------------------------------------


def test_a_parameter_change_changes_the_fingerprint():
    default = MomentumContinuation()
    wider = MomentumContinuation(MomentumParameters(stop_points="25"))
    assert default.fingerprint != wider.fingerprint
    assert default.fingerprint == MomentumContinuation().fingerprint


def test_every_default_strategy_declares_its_rules():
    for strategy in default_strategies():
        spec = strategy.spec()
        assert spec["entry_rule"]
        assert spec["exit_rule"]
        assert spec["rule_version"]
        assert spec["fingerprint"] == strategy.fingerprint


def test_a_strategy_refuses_a_regime_that_does_not_admit_it():
    ticks = ticks_from(series(list(range(40))))
    evaluation = BreakoutContinuation().evaluate(context(ticks, RANGING, [regime_module.FAMILY_MEAN_REVERSION]))
    assert evaluation.outcome == NOT_ELIGIBLE
    assert evaluation.signal is None
    assert RANGING in evaluation.detail


def test_a_strategy_refuses_a_wide_spread_before_it_looks_for_a_setup():
    ticks = ticks_from(series(list(range(40)), spread_points="60"))
    evaluation = MomentumContinuation().evaluate(
        context(ticks, TRENDING_UP, [regime_module.FAMILY_MOMENTUM], spread_limit="20")
    )
    assert evaluation.outcome == SPREAD_TOO_WIDE


def test_a_signal_carries_a_priceable_stop_and_target():
    ticks = ticks_from(series([index * 2 for index in range(20)]))
    evaluation = MomentumContinuation().evaluate(
        context(ticks, TRENDING_UP, [regime_module.FAMILY_MOMENTUM])
    )
    assert evaluation.outcome == "SIGNAL"
    signal = evaluation.signal
    assert signal.stop_price is not None and signal.target_price is not None
    assert signal.side == "BUY"
    assert signal.stop_price < signal.reference_price < signal.target_price
    assert signal.parameter_fingerprint == MomentumContinuation().fingerprint


def test_a_declined_setup_says_why():
    ticks = ticks_from(series([0] * 40))
    evaluation = MomentumContinuation().evaluate(
        context(ticks, TRENDING_UP, [regime_module.FAMILY_MOMENTUM])
    )
    assert evaluation.outcome == NO_SETUP
    assert "net move" in evaluation.detail


# ---------------------------------------------------------------------------
# each family fires on its own shape
# ---------------------------------------------------------------------------


def test_breakout_fires_when_price_leaves_the_range():
    points = [0] * 60 + [3, 5, 7]
    ticks = ticks_from(series(points))
    evaluation = BreakoutContinuation().evaluate(
        context(ticks, BREAKOUT, [regime_module.FAMILY_BREAKOUT])
    )
    assert evaluation.outcome == "SIGNAL"
    assert evaluation.signal.side == "BUY"


def test_breakout_does_not_fire_inside_the_buffer():
    ticks = ticks_from(series([index % 4 for index in range(80)]))
    evaluation = BreakoutContinuation().evaluate(
        context(ticks, BREAKOUT, [regime_module.FAMILY_BREAKOUT])
    )
    assert evaluation.outcome == NO_SETUP


def test_pullback_fires_after_a_dip_that_is_reclaimed():
    points = [index * 0.5 for index in range(60)]
    last = points[-1]
    points += [last - 0.45, last - 0.90, last - 1.50]  # dip against the trend
    points += [last - 0.75, last - 0.15]  # reclaim
    ticks = ticks_from(series(points))
    evaluation = PullbackContinuation().evaluate(
        context(ticks, TRENDING_UP, [regime_module.FAMILY_PULLBACK])
    )
    assert evaluation.outcome == "SIGNAL"
    assert evaluation.signal.side == "BUY"


def test_mean_reversion_fires_on_a_stretched_window():
    points = [0.0] * 50 + [-12.0]
    ticks = ticks_from(series(points))
    evaluation = MeanReversion().evaluate(
        context(ticks, RANGING, [regime_module.FAMILY_MEAN_REVERSION])
    )
    assert evaluation.outcome == "SIGNAL"
    assert evaluation.signal.side == "BUY"
    assert "z_score" in evaluation.signal.reason


def test_order_flow_momentum_stays_off_without_volume():
    ticks = ticks_from(series([index * 0.2 for index in range(80)]))
    evaluation = OrderFlowMomentum().evaluate(
        context(ticks, TRENDING_UP, [regime_module.FAMILY_ORDER_FLOW])
    )
    assert evaluation.outcome == FLOW_DATA_UNAVAILABLE
    assert "carries no trade-volume direction" in evaluation.detail


def test_order_flow_momentum_fires_on_a_volume_imbalance():
    points = [0.0]
    volumes = [10]
    for index in range(80):
        points.append(points[-1] + (0.2 if index % 2 == 0 else -0.05))
        volumes.append(50 if index % 2 == 0 else 5)
    points.append(points[-1] + 0.2)  # the last step must agree with the imbalance
    volumes.append(50)
    replay = series(points, volume=volumes)
    ticks = ticks_from(replay)
    evaluation = OrderFlowMomentum().evaluate(
        context(ticks, TRENDING_UP, [regime_module.FAMILY_ORDER_FLOW])
    )
    assert evaluation.outcome == "SIGNAL"
    assert evaluation.signal.flow_proxy is not None
    assert evaluation.signal.side == "BUY"


def test_no_signal_is_constructed_without_a_positive_stop():
    for strategy in default_strategies():
        ticks = ticks_from(series([index * 3 for index in range(120)]))
        evaluation = strategy.evaluate(
            context(ticks, TRENDING_UP, [strategy.family])
        )
        if evaluation.signal is not None:
            assert evaluation.signal.stop_points > 0
            assert evaluation.signal.target_points > 0
