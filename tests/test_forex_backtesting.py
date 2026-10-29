from decimal import Decimal

import pytest

from axiom_forex.backtesting.validation import run_oos
from axiom_forex.strategies.base import Signal, Strategy
from axiom_forex.validation.split import LockedTest, chronological_split
from axiom_forex.validation.walk_forward import walk_forward


class HoldStrategy(Strategy):
    name = "hold"
    history_limit = 1

    def generate_signal(self, bars):
        return Signal.HOLD


class BuyThenExit(Strategy):
    name = "buy_then_exit"
    history_limit = 1

    def generate_signal(self, bars):
        if len(bars) == 1:
            return Signal.BUY
        if len(bars) == 2:
            return Signal.EXIT
        return Signal.HOLD


class BuyAndFinalExit(Strategy):
    def __init__(self, total_bars: int):
        self.total_bars = total_bars
        self.name = "buy_and_final_exit"
        self.history_limit = 1

    def generate_signal(self, bars):
        if len(bars) == 1:
            return Signal.BUY
        if len(bars) == self.total_bars:
            return Signal.EXIT
        return Signal.HOLD


def test_backtest_fills_buy_at_ask_and_exit_at_bid(platform):
    result = platform.new_engine().run(
        platform.bars, BuyThenExit(), run_id="bid-ask"
    )
    assert result.usable
    assert result.quality.classification == "VALID"
    assert len(result.trades) == 2

    entry = result.trades[0]["fill"]
    exit_fill = result.trades[1]["fill"]
    assert entry["action"] == "OPEN"
    assert entry["side"] == "BUY"
    assert entry["price"] == platform.bars[1].ask.open
    assert exit_fill["action"] == "CLOSE"
    assert exit_fill["side"] == "SELL"
    assert exit_fill["price"] == platform.bars[2].bid.open
    assert result.cost_total_account > 0
    assert result.rollover_total_account == 0
    assert result.rejections == ()


def test_final_bar_exit_signal_is_cancelled_without_forced_liquidation(platform):
    strategy = BuyAndFinalExit(len(platform.bars))
    result = platform.new_engine().run(
        platform.bars, strategy, run_id="final-bar"
    )
    assert result.usable
    assert result.final_bar_signal_cancelled
    assert len(result.trades) == 1
    assert len(result.open_positions) == 1
    assert result.trades[0]["fill"]["action"] == "OPEN"
    assert result.cost_total_account > 0


def test_missing_bar_gap_blocks_backtest_without_interpolation(platform):
    bars = platform.bars[:5] + platform.bars[6:]
    result = platform.new_engine().run(
        bars, HoldStrategy(), run_id="missing-bar"
    )
    assert not result.usable
    assert result.quality.classification == "MISSING"
    assert result.trades == ()
    assert any(
        rejection["code"] == "DATA_QUALITY_REJECTED"
        for rejection in result.rejections
    )


def test_oos_uses_locked_test_once_and_does_not_mutate_platform(platform):
    bars = platform.bars
    validation = platform.run_oos(
        HoldStrategy(),
        train_bars=bars[:20],
        validation_bars=bars[20:30],
        test_bars=bars[30:],
    )
    assert validation.test_used
    assert validation.result.out_of_sample
    assert validation.result.quality.usable
    assert platform.paper_execution.records() == ()
    assert isinstance(validation.passed, bool)


def test_run_oos_rejects_test_reuse(platform):
    bars = platform.bars
    locked = LockedTest(bars[:10])
    assert locked.use_once() == bars[:10]
    assert locked.used
    with pytest.raises(ValueError, match="already been used"):
        locked.use_once()
    validation = run_oos(
        platform.new_engine,
        HoldStrategy(),
        train_bars=bars[:20],
        validation_bars=bars[20:30],
        test_bars=bars[30:],
    )
    assert validation.test_used


def test_chronological_split_and_walk_forward_do_not_leak_future(platform):
    bars = platform.bars
    split = chronological_split(bars, (Decimal("0.5"), Decimal("0.25"), Decimal("0.25")))
    assert sum(split.counts().values()) == len(bars)
    assert split.train[-1].timestamp < split.validation[0].timestamp
    assert split.validation[-1].timestamp < split.test[0].timestamp

    folds = list(
        walk_forward(
            bars,
            train_size=10,
            validation_size=5,
            test_size=5,
            step_size=5,
        )
    )
    assert folds
    for fold in folds:
        assert fold.train[-1].timestamp < fold.validation[0].timestamp
        assert fold.validation[-1].timestamp < fold.test[0].timestamp


def test_engine_reuse_fails_closed_without_state_leakage(platform):
    engine = platform.new_engine()
    first = engine.run(platform.bars, HoldStrategy(), run_id="first")
    assert first.usable
    with pytest.raises(ValueError, match="engine_already_used"):
        engine.run(platform.bars, HoldStrategy(), run_id="second")
    second = platform.new_engine().run(
        platform.bars, HoldStrategy(), run_id="fresh-engine"
    )
    assert second.usable
    assert second.initial_equity_account == first.initial_equity_account


def test_backtest_equity_curve_covers_every_bar_in_order(platform):
    result = platform.new_engine().run(
        platform.bars, HoldStrategy(), run_id="equity-curve"
    )
    indices = [point["index"] for point in result.equity_curve]
    assert indices == list(range(-1, len(platform.bars)))
    assert result.final_equity_account == result.equity_curve[-1]["equity_account"]
    assert result.max_drawdown_account >= 0
