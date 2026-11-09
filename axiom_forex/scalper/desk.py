"""The AX-SCALPER desk: one tick in, at most one position out.

The desk is the only place in the package that knows about *all* the layers.  It
is deliberately boring: it takes a quote, asks the tick core whether the quote may
be used, asks the regime what kind of market it is, asks each strategy for a
signal, asks the probability model and the EV engine whether a signal is worth
anything after costs, asks the guardian and the venue for permission, books what
comes back, and writes every step to the ledger.

Three properties matter more than any of the mechanics:

* **Management is never blocked by entry controls.**  An emergency stop, a
  suspension, a loss limit or a data-quality fault stops *new* entries and leaves
  stops, targets, trailing and time exits running.
* **Nothing is silent.**  A refusal is written with its reason; an opportunity that
  produces no trade is still an opportunity in the ledger.
* **A restart inherits nothing.**  Recovered state is re-read from the ledger,
  re-verified against the venue, and a position whose protection cannot be verified
  blocks entries until an operator resolves it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping

from app.execution.broker import TradingBlocked

from . import bayes, config as config_module, execution, positions, scribe as scribe_module
from .config import PAPER, ScalperConfig
from .ev import ExpectancyTracker, cost_for, evaluate as evaluate_ev
from .execution import ExecutionReport, PaperVenue, ScalperVenue, Trader
from .guardian import Guardian, GuardianLimits, OpenPositionRisk, ShutdownPlan
from .positions import ClosedTrade, ManagementAction, ManagementSettings, PositionBook
from .regime import ABNORMAL, RegimeState, RegimeThresholds, classify
from .scribe import Scribe
from .skeptic import session_of
from .signals import Evaluation, Signal, SignalContext, Strategy, default_strategies
from .ticks import Tick, TickCore
from .variance import SizingInput, size_position

#: Why the desk declined to look for entries on a tick.  Distinct from why a
#: strategy declined a setup: these are desk-level gates.
WARMING_UP = "warming_up"
RESYNCING = "resynchronising_after_clock_reset"
NO_TRADE_REGIME = "regime_says_no_trade"
ENTRIES_SUSPENDED = "entries_suspended"
COOLDOWN = "cooldown_after_entry"
UNRESOLVED_ORDER = "unresolved_order_blocks_entries"
NO_QUOTE = "no_quote_for_symbol"
RISK_STATE_UNKNOWN = "risk_state_unavailable"

#: Ledger-free default thresholds.  Passed explicitly so a reader never has to guess.
DEFAULT_MIN_PROBABILITY_SAMPLES = 30


def _ms(moment: datetime) -> int:
    return int(round(moment.timestamp() * 1000))


@dataclass(frozen=True)
class DeskThresholds:
    """Desk-level gates that are policy, not instrument terms."""

    min_probability_samples: int = DEFAULT_MIN_PROBABILITY_SAMPLES
    #: Minimum quotes before a regime label is trusted for a decision.
    min_regime_ticks: int = 30
    #: Re-evaluate a symbol at most this often, in milliseconds.  Zero means every tick.
    decision_interval_ms: int = 0


@dataclass(frozen=True)
class TickOutcome:
    """What one quote produced.  Empty tuples are a result, not a failure."""

    accepted: bool
    tick: Tick | None
    rejection: str = ""
    regime: str | None = None
    blocked: str | None = None
    evaluations: tuple[Evaluation, ...] = ()
    signals: tuple[Signal, ...] = ()
    executions: tuple[ExecutionReport, ...] = ()
    actions: tuple[ManagementAction, ...] = ()
    trades: tuple[ClosedTrade, ...] = ()

    def audit_view(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "rejection": self.rejection or None,
            "tick_id": None if self.tick is None else self.tick.tick_id,
            "regime": self.regime,
            "blocked": self.blocked,
            "evaluations": [item.audit_view() for item in self.evaluations],
            "signals": [item.audit_view() for item in self.signals],
            "executions": [item.audit_view() for item in self.executions],
            "actions": [item.audit_view() for item in self.actions],
            "trades": [item.audit_view() for item in self.trades],
        }


class ScalperDesk:
    """The paper desk.  Construct one, feed it quotes, read the ledger."""

    def __init__(
        self,
        *,
        config: ScalperConfig,
        strategies: Iterable[Strategy] | None = None,
        venue: ScalperVenue | None = None,
        scribe: Scribe | None = None,
        guardian: Guardian | None = None,
        correlations: Any | None = None,
        outcome_book: bayes.OutcomeBook | None = None,
        probability_source: Callable[[Signal, str], bayes.ProbabilityEstimate] | None = None,
        thresholds: DeskThresholds | None = None,
        regime_thresholds: RegimeThresholds | None = None,
        starting_equity: Decimal = Decimal("10000"),
        run_id: str = "scalper-paper",
        engine: str = "scalper",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        problems = config.validate()
        if problems:
            raise config_module.ScalperConfigError(problems[0], "; ".join(problems))
        if config.mode != PAPER:
            raise TradingBlocked(config_module.CONFIG_NOT_PAPER)

        self._config = config
        self._engine = engine
        self._run_id = run_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._thresholds = thresholds or DeskThresholds()
        self._regime_thresholds = regime_thresholds or RegimeThresholds(
            min_ticks=self._thresholds.min_regime_ticks
        )

        self._strategies = tuple(strategies) if strategies is not None else default_strategies()
        self._specs = {spec.symbol: spec for spec in config.instruments}

        self._core = TickCore(
            instruments=config.instruments,
            max_quote_age_ms=config.max_quote_age_ms,
            max_spread_points=config.max_spread_points,
            gap_threshold_ms=config.gap_threshold_ms,
            spread_expansion_multiple=config.spread_expansion_multiple,
            tick_window=config.tick_window,
            min_window_ticks=config.min_window_ticks,
            duplicate_window=config.duplicate_window,
            clock_reset_threshold_ms=config.clock_reset_threshold_ms,
            resync_ticks=config.resync_ticks,
            clock=self._clock,
        )
        self._venue: ScalperVenue = venue or PaperVenue(cost=config.cost)
        if getattr(self._venue, "mode", PAPER) != PAPER:
            raise TradingBlocked(execution.PAPER_ONLY)
        self._scribe = scribe or Scribe(run_id=run_id, mode=config.mode)
        self._guardian = guardian or Guardian(
            GuardianLimits.from_config(config),
            correlations=correlations,
            declared_slippage_points=config.cost.slippage_points,
            engine=engine,
        )
        self._trader = Trader(
            venue=self._venue,
            guardian=self._guardian,
            cost=config.cost,
            max_quote_age_ms=config.max_quote_age_ms,
            specs=self._specs,
            signal_ttl_ms=max((strategy.ttl_ms for strategy in self._strategies), default=750),
            engine=engine,
        )
        self._book = PositionBook(
            venue=self._venue,
            settings=ManagementSettings.from_config(config),
            specs=self._specs,
        )
        self._outcomes = outcome_book or bayes.OutcomeBook()
        self._probability_source = probability_source
        self._expectancy = ExpectancyTracker()

        self._equity = Decimal(starting_equity)
        self._balance = Decimal(starting_equity)
        self._starting_equity = Decimal(starting_equity)
        self._peak_equity = Decimal(starting_equity)
        self._cost_by_position: dict[str, Decimal] = {}
        self._fingerprint_by_position: dict[str, str] = {}
        self._strategy_by_position: dict[str, str] = {}
        self._regime_by_position: dict[str, str] = {}
        self._entered_spread: dict[str, Decimal] = {}
        self._last_entry_ms: int | None = None
        self._last_decision_ms: dict[str, int] = {}
        self._last_regimes: dict[str, str] = {}
        self._initial_stop: dict[str, Decimal] = {}
        self._settled: set[str] = set()
        self._trades: list[dict[str, Any]] = []
        self._seen_events = 0
        self._pending_approval_cache: dict[str, str] = {}
        self._suspended_by_desk: set[str] = set()
        self._last_tick_at: datetime | None = None
        self.stats: dict[str, Any] = {
            "ticks": 0,
            "accepted": 0,
            "refused": {},
            "quality": {},
            "regimes": {},
            "opportunities": 0,
            "no_setup": 0,
            "signals": 0,
            "probability_refused": 0,
            "ev_accept": 0,
            "ev_reject": 0,
            "size_refused": 0,
            "sized": 0,
            "risk_refused": 0,
            "orders": 0,
            "fills": 0,
            "rejects": 0,
            "unknown": 0,
            "partials": 0,
            "blocked": {},
            "exits": 0,
            "trades": 0,
            "entries_suspended": 0,
        }
        # Reason-level detail lives beside the counters, never inside them: a
        # counter is a number a reader can compare, a breakdown is a diagnosis.
        self._ev_rejections: dict[str, int] = {}
        self._risk_refusals: dict[str, int] = {}
        self._size_refusals: dict[str, int] = {}
        self._tick_refusals: dict[str, int] = {}
        self._slippages: list[Decimal] = []
        self._latencies: list[int] = []
        self._day_key: str | None = None

    # -- read-only views -------------------------------------------------

    @property
    def config(self) -> ScalperConfig:
        return self._config

    @property
    def core(self) -> TickCore:
        return self._core

    @property
    def book(self) -> PositionBook:
        return self._book

    @property
    def guardian(self) -> Guardian:
        return self._guardian

    @property
    def trader(self) -> Trader:
        return self._trader

    @property
    def venue(self) -> ScalperVenue:
        return self._venue

    @property
    def scribe(self) -> Scribe:
        return self._scribe

    @property
    def outcomes(self) -> bayes.OutcomeBook:
        return self._outcomes

    @property
    def equity(self) -> Decimal:
        return self._equity

    @property
    def trades(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(item) for item in self._trades)

    # -- the loop --------------------------------------------------------

    def on_tick(
        self,
        *,
        symbol: str,
        bid: Any,
        ask: Any,
        source_time: datetime,
        received_at: datetime | None = None,
        volume: Any = None,
        source: str = "MT5_POLL",
        equity: Any = None,
        balance: Any = None,
        day_key: str | None = None,
    ) -> TickOutcome:
        """Process one quote.  Management runs first and is never skipped.

        ``equity``/``balance``/``day_key`` are the *caller's* account figures.  When
        they are supplied they replace the desk's paper arithmetic for that tick, so a
        live-account reading can never be silently replaced by a simulated one, and a
        simulated figure can never be presented as a broker's.
        """

        received = received_at or self._clock()
        self.stats["ticks"] += 1
        result = self._core.ingest(
            symbol=symbol,
            bid=bid,
            ask=ask,
            source_time=source_time,
            received_at=received,
            source=source,
            volume=volume,
        )
        self._drain_quality_events(received)
        if not result.accepted or result.tick is None:
            key = result.reason
            self.stats["refused"][key] = self.stats["refused"].get(key, 0) + 1
            self._tick_refusals[key] = self._tick_refusals.get(key, 0) + 1
            self._scribe.reject(
                at_ms=_ms(received),
                stage="tick_core",
                reason=key,
                detail=result.detail,
                symbol=symbol,
                source_time=source_time.isoformat(),
            )
            return TickOutcome(accepted=False, tick=None, rejection=key)

        tick = result.tick
        self.stats["accepted"] += 1
        self._last_tick_at = received
        now_ms = _ms(received)
        if equity is not None:
            self._equity = Decimal(str(equity))
        if balance is not None:
            self._balance = Decimal(str(balance))
        if day_key is not None:
            self._day_key = day_key

        spec = self._specs.get(tick.symbol)
        spread_points = tick.spread_points(spec.point) if spec is not None else None
        self._guardian.observe_quote(
            symbol=tick.symbol,
            spread_points=spread_points,
            quote_age_ms=max(0, int(tick.ingest_latency_ms)),
        )

        actions: list[ManagementAction] = []
        closed: list[ClosedTrade] = []
        managed = self._manage(tick=tick, now_ms=now_ms)
        actions.extend(managed[0])
        closed.extend(managed[1])
        for trade in closed:
            self._settle(trade, now_ms=now_ms)

        blocked = self._entry_block(tick.symbol, now_ms)
        if blocked is not None:
            self.stats["blocked"][blocked] = self.stats["blocked"].get(blocked, 0) + 1
            return TickOutcome(
                accepted=True,
                tick=tick,
                regime=self._last_regime(tick.symbol),
                blocked=blocked,
                actions=tuple(actions),
                trades=tuple(closed),
            )

        evaluations, reports = self._decide(tick=tick, now_ms=now_ms)
        return TickOutcome(
            accepted=True,
            tick=tick,
            regime=self._last_regime(tick.symbol),
            evaluations=tuple(evaluations),
            signals=tuple(item.signal for item in evaluations if item.signal is not None),
            executions=tuple(reports),
            actions=tuple(actions),
            trades=tuple(closed),
        )

    def run(
        self,
        ticks: Iterable[Mapping[str, Any]],
        *,
        source: str = "HISTORICAL_TICK_REPLAY",
    ) -> dict[str, Any]:
        """Drive the desk from a sequence of quote dicts.  Replay, not live data."""

        processed = 0
        for row in ticks:
            self.on_tick(
                symbol=row["symbol"],
                bid=row["bid"],
                ask=row["ask"],
                source_time=row["source_time"],
                received_at=row.get("received_at"),
                volume=row.get("volume"),
                source=source,
            )
            processed += 1
        return {"ticks": processed, "stats": dict(self.stats)}

    # -- management ------------------------------------------------------

    def _manage(self, *, tick: Tick, now_ms: int) -> tuple[list[ManagementAction], list[ClosedTrade]]:
        actions: list[ManagementAction] = []
        closed: list[ClosedTrade] = []
        for position in list(self._book.open_positions):
            if position.symbol != tick.symbol:
                continue
            before = len(self._book.closed)
            produced = self._book.on_tick(
                position_id=position.position_id, tick=tick, now_ms=now_ms
            )
            for action in produced:
                actions.append(action)
                if action.kind == positions.MODIFY_STOP:
                    self._scribe.record(
                        scribe_module.POSITION_MODIFICATION,
                        at_ms=now_ms,
                        actor="position_manager",
                        position_id=action.position_id,
                        reason=action.reason,
                        stop_price=None if action.stop_price is None else str(action.stop_price),
                        detail=action.detail,
                    )
                elif action.kind == positions.PARTIAL_EXIT:
                    self._scribe.record(
                        scribe_module.POSITION_MODIFICATION,
                        at_ms=now_ms,
                        actor="position_manager",
                        position_id=action.position_id,
                        reason=action.reason,
                        detail=action.detail,
                        exit_price=None if action.exit_price is None else str(action.exit_price),
                    )
                elif action.kind == positions.HOLD and action.reason == positions.PROTECTION_MISSING:
                    self._guardian.suspend_entries(execution.PROTECTION_UNVERIFIED)
                    self._scribe.error(
                        at_ms=now_ms,
                        error=execution.PROTECTION_UNVERIFIED,
                        detail=action.detail,
                        position_id=action.position_id,
                    )
            closed.extend(self._book.closed[before:])
        return actions, closed

    def _settle(self, trade: ClosedTrade, *, now_ms: int) -> None:
        """Turn a closed position into an auditable, cost-bearing record.

        The book owns the gross arithmetic; the desk owns the cost model, so the net
        figure is written here and nowhere else.  A settlement happens once.
        """

        if trade.position_id in self._settled:
            return
        self._settled.add(trade.position_id)
        cost_points = self._cost_by_position.pop(trade.position_id, Decimal(0))
        net = trade.gross_points - cost_points
        spec = self._specs.get(trade.symbol)
        gross_account = trade.gross_account
        net_account = None
        if spec is not None and spec.point_value_per_lot is not None:
            net_account = net * spec.point_value_per_lot * trade.lots
        if net_account is not None:
            self._equity += net_account
            self._balance += net_account
            self._peak_equity = max(self._peak_equity, self._equity)
        fingerprint = self._fingerprint_by_position.pop(trade.position_id, trade.strategy_fingerprint)
        strategy = self._strategy_by_position.pop(trade.position_id, "")
        regime_name = self._regime_by_position.pop(trade.position_id, "")
        spread = self._entered_spread.pop(trade.position_id, Decimal(0))
        if fingerprint:
            self._outcomes.record(fingerprint=fingerprint, regime=regime_name, won=net > 0)
            self._expectancy.record(fingerprint, net)
        stop_points = self._initial_stop.pop(trade.position_id, Decimal(0))
        r_multiple = None if stop_points <= 0 else float(net / stop_points)
        row = {
            "index": self.stats["trades"] + 1,
            "position_id": trade.position_id,
            "fingerprint": fingerprint,
            "strategy": strategy,
            "regime": regime_name,
            "side": trade.side,
            "lots": str(trade.lots),
            "entry_price": str(trade.entry_price),
            "exit_price": str(trade.exit_price),
            "reason": trade.reason,
            "gross_points": trade.gross_points,
            "cost_points": cost_points,
            "net_points": net,
            "gross_account": gross_account,
            "net_account": net_account,
            "spread_points_at_entry": spread,
            "stop_points": stop_points,
            "r_multiple": r_multiple,
            "mae_points": trade.mae_points,
            "mfe_points": trade.mfe_points,
            "holding_ms": trade.holding_ms,
            "opened_at_ms": trade.opened_at_ms,
            "closed_at_ms": now_ms,
            "session": session_of(self._clock()),
            "won": net > 0,
        }
        self._trades.append(row)
        self.stats["trades"] += 1
        self.stats["exits"] += 1
        self._scribe.exit_record(
            at_ms=now_ms,
            position_id=trade.position_id,
            reason=trade.reason,
            gross_points=str(trade.gross_points),
            gross_account=None if gross_account is None else str(gross_account),
            strategy=strategy,
            fingerprint=fingerprint,
            cost_points=str(cost_points),
            net_points=str(net),
            won=bool(net > 0),
            net_account=None if net_account is None else str(net_account),
            spread_points_at_entry=str(spread),
            holding_ms=trade.holding_ms,
        )
        self._guardian.record_close(
            position_id=trade.position_id,
            realised=Decimal(0) if net_account is None else net_account,
            at_ms=now_ms,
        )
        self._observe_account(now_ms)

    def _observe_account(self, now_ms: int) -> None:
        day_key = self._day_key or self._clock().date().isoformat()
        self._guardian.observe_account(
            equity=self._equity, balance=self._balance, at_ms=now_ms, day_key=day_key
        )

    # -- entries ---------------------------------------------------------

    def _entry_block(self, symbol: str, now_ms: int) -> str | None:
        if self._core.resyncing(symbol):
            return RESYNCING
        quality = self._core.quality(symbol, now=self._now())
        if quality["usable"] is False:
            reasons = [str(reason) for reason in quality["reasons"]]
            if "resynchronising" in reasons:
                return RESYNCING
            if "insufficient_window" in reasons:
                return WARMING_UP
            if "no_tick" in reasons or "stale_quote" in reasons:
                return NO_QUOTE
            return "data_quality:" + ",".join(reasons)
        if self._trader.has_unresolved:
            return UNRESOLVED_ORDER
        allowed, reasons = self._guardian.may_open()
        if not allowed:
            self.stats["entries_suspended"] += 1
            return ENTRIES_SUSPENDED + ":" + ",".join(reasons)
        if self._last_entry_ms is not None and now_ms - self._last_entry_ms < self._config.cooldown_ms:
            return COOLDOWN
        interval = self._thresholds.decision_interval_ms
        if interval:
            previous = self._last_decision_ms.get(symbol)
            if previous is not None and now_ms - previous < interval:
                return COOLDOWN
        if len(self._book.open_positions) >= self._config.max_open_positions:
            return "max_open_positions_reached"
        if self._equity <= 0:
            return RISK_STATE_UNKNOWN
        self._observe_account(now_ms)
        allowed, reasons = self._guardian.may_open()
        if not allowed:
            self.stats["entries_suspended"] += 1
            return ENTRIES_SUSPENDED + ":" + ",".join(reasons)
        return None

    def _decide(
        self, *, tick: Tick, now_ms: int
    ) -> tuple[list[Evaluation], list[ExecutionReport]]:
        spec = self._specs[tick.symbol]
        window = self._core.window(tick.symbol)
        state = classify(
            window,
            spec=spec,
            thresholds=self._regime_thresholds,
            spread_limit_points=self._config.max_spread_points,
        )
        self._record_regime(state, now_ms)
        self._last_decision_ms[tick.symbol] = now_ms
        if not state.tradeable:
            self.stats["blocked"][NO_TRADE_REGIME] = self.stats["blocked"].get(NO_TRADE_REGIME, 0) + 1
            return [], []
        if len(window) < self._thresholds.min_regime_ticks:
            self.stats["blocked"][WARMING_UP] = self.stats["blocked"].get(WARMING_UP, 0) + 1
            return [], []

        context = SignalContext(
            ticks=window,
            spec=spec,
            regime=state,
            spread_limit_points=self._config.max_spread_points,
            max_quote_age_ms=self._config.max_quote_age_ms,
        )
        evaluations: list[Evaluation] = []
        reports: list[ExecutionReport] = []
        decision_started = time.perf_counter()
        for strategy in self._strategies:
            self.stats["opportunities"] += 1
            evaluation = strategy.evaluate(context)
            evaluations.append(evaluation)
            self._scribe.record(
                scribe_module.OPPORTUNITY,
                at_ms=now_ms,
                actor="signal_generator",
                strategy=evaluation.strategy,
                family=evaluation.family,
                outcome=evaluation.outcome,
                detail=evaluation.detail,
                regime=state.regime,
                tick_id=tick.tick_id,
            )
            if evaluation.signal is None:
                self.stats["no_setup"] += 1
                continue
            signal = evaluation.signal
            self.stats["signals"] += 1
            self._scribe.record(
                scribe_module.SIGNAL,
                at_ms=now_ms,
                actor="signal_generator",
                strategy=signal.strategy,
                fingerprint=signal.parameter_fingerprint,
                side=signal.side,
                family=signal.family,
                reference_price=str(signal.reference_price),
                stop_points=str(signal.stop_points),
                target_points=str(signal.target_points),
                reason=signal.reason,
                regime=state.regime,
                tick_id=signal.tick_id,
            )

            probability = self._probability(strategy, state, signal)
            if probability is None:
                continue
            self._scribe.record(
                scribe_module.PROBABILITY,
                at_ms=now_ms,
                actor="bayes",
                strategy=signal.strategy,
                fingerprint=signal.parameter_fingerprint,
                p=round(probability.p, 6),
                sd=round(probability.sd, 6),
                samples=probability.samples,
                basis=probability.basis,
                label=probability.label,
            )
            spread_points = tick.spread_points(spec.point)
            trade_cost = cost_for(spec=spec, cost=self._config.cost, spread_points=spread_points)
            expected = evaluate_ev(
                signal,
                probability,
                spec=spec,
                cost=self._config.cost,
                spread_points=spread_points,
                min_edge_points=self._config.min_edge_points,
                uncertainty_z=self._config.uncertainty_z,
            )
            self._scribe.record(
                scribe_module.EVALUATION,
                at_ms=now_ms,
                actor="ev_check",
                strategy=signal.strategy,
                fingerprint=signal.parameter_fingerprint,
                verdict=expected.verdict,
                reasons=list(expected.reasons),
                net_points=str(round(expected.net_points, 4)),
                lower_bound_points=str(round(expected.lower_bound_points, 4)),
                cost_points=str(trade_cost.total_points),
                cost_complete=trade_cost.complete,
                cost_missing=list(trade_cost.missing),
            )
            if not expected.accepted:
                self.stats["ev_reject"] += 1
                self._ev_rejections[expected.verdict] = (
                    self._ev_rejections.get(expected.verdict, 0) + 1
                )
                for reason in expected.reasons:
                    self._ev_rejections[reason] = self._ev_rejections.get(reason, 0) + 1
                self._scribe.reject(
                    at_ms=now_ms,
                    stage="ev_check",
                    reason=expected.reasons[0] if expected.reasons else "expected_value_rejected",
                    strategy=signal.strategy,
                    verdict=expected.verdict,
                )
                continue
            self.stats["ev_accept"] += 1

            sizing = self._size(signal, state, spec, tick)
            if not sizing.allowed:
                reason = sizing.refusals[0] if sizing.refusals else "size_refused"
                self.stats["size_refused"] += 1
                self._size_refusals[reason] = self._size_refusals.get(reason, 0) + 1
                self._scribe.reject(
                    at_ms=now_ms, stage="variance", reason=reason, strategy=signal.strategy
                )
                continue

            self.stats["sized"] += 1
            decision_latency = int((time.perf_counter() - decision_started) * 1000)
            request_id = self._trader.request_id_for(signal)
            self._scribe.record(
                scribe_module.ORDER_REQUEST,
                at_ms=now_ms,
                actor="trader",
                request_id=request_id,
                strategy=signal.strategy,
                fingerprint=signal.parameter_fingerprint,
                side=signal.side,
                lots=str(sizing.lots),
                risk_account=str(sizing.risk_account),
                notional_account=str(sizing.notional_account),
                stop_price=str(signal.stop_price),
                target_price=str(signal.target_price),
                decision_latency_ms=decision_latency,
                tick_id=tick.tick_id,
                quote_source=tick.source,
                quote_source_time=tick.source_time.isoformat(),
                quote_received_at=tick.received_at.isoformat(),
                quote_bid=str(tick.bid),
                quote_ask=str(tick.ask),
            )
            self.stats["orders"] += 1
            report = self._trader.execute(
                signal=signal,
                lots=sizing.lots,
                risk_account=sizing.risk_account,
                notional_account=sizing.notional_account,
                quote=tick,
                now_ms=now_ms,
                decision_latency_ms=decision_latency,
                currencies=self._currencies(spec),
            )
            reports.append(report)
            self._record_execution(report, now_ms, tick)
            if report.fill is not None and report.fill.filled_lots > 0 and report.fill.ticket:
                self._open(
                    trade_report=report,
                    signal=signal,
                    sizing=sizing,
                    spec=spec,
                    state=state,
                    tick=tick,
                    now_ms=now_ms,
                )
        return evaluations, reports

    def _probability(
        self, strategy: Strategy, state: RegimeState, signal: Signal
    ) -> bayes.ProbabilityEstimate | None:
        if self._probability_source is not None:
            estimate = self._probability_source(signal, state.regime)
            if estimate.samples < self._thresholds.min_probability_samples:
                # A source that returns a thin posterior is reported as thin, not used
                # as if it were evidence.
                self.stats["probability_refused"] += 1
                return estimate
            return estimate
        estimate = self._outcomes.estimate(strategy.fingerprint, state.regime)
        if estimate.samples < self._thresholds.min_probability_samples:
            self.stats["probability_refused"] += 1
            self._scribe.reject(
                at_ms=_ms(self._current_time()),
                stage="bayes",
                reason="insufficient_probability_samples",
                strategy=strategy.name,
                samples=estimate.samples,
                required=self._thresholds.min_probability_samples,
                basis=estimate.basis,
            )
            return None
        return estimate

    def _size(self, signal: Signal, state: RegimeState, spec: Any, tick: Tick):
        open_positions = self._book.open_positions
        open_risk = Decimal(0)
        open_notional = Decimal(0)
        exposure: dict[str, Decimal] = {}
        for position in open_positions:
            point_value = position.point_value_per_lot or Decimal(0)
            open_risk += position.initial_stop_points * point_value * position.lots
            conversion = spec.conversion_rate if spec.conversion_rate is not None else Decimal(1)
            notional = spec.contract_size * position.entry_price * conversion * position.lots
            open_notional += notional
            for currency in self._currencies(spec):
                exposure[currency] = exposure.get(currency, Decimal(0)) + notional
        return size_position(
            SizingInput(
                spec=spec,
                price=tick.ask if signal.side == "BUY" else tick.bid,
                stop_points=signal.stop_points,
                equity=self._equity,
                risk_fraction=self._config.risk_fraction,
                max_loss_account=self._config.max_loss_per_trade_account,
                size_multiplier=state.size_multiplier,
                open_risk_account=open_risk,
                open_notional_account=open_notional,
                max_aggregate_risk_fraction=self._config.max_aggregate_risk_fraction,
                max_leverage=self._config.max_leverage,
                max_currency_exposure_fraction=self._config.max_currency_exposure_fraction,
                currency_exposure=exposure,
                max_open_positions=self._config.max_open_positions,
                open_positions=len(open_positions),
            )
        )

    def _currencies(self, spec: Any) -> tuple[str, ...]:
        pair = getattr(spec, "pair", "") or ""
        parts = [part for part in pair.replace("-", "/").split("/") if part]
        return tuple(parts)

    def _record_execution(self, report: ExecutionReport, now_ms: int, quote: Tick) -> None:
        self._scribe.record(
            scribe_module.EXECUTION_RESULT,
            at_ms=now_ms,
            actor="trader",
            request_id=report.request_id,
            status=report.status,
            reasons=list(report.reasons),
            filled_lots=None if report.fill is None else str(report.fill.filled_lots),
            price=None if report.fill is None or report.fill.price is None else str(report.fill.price),
            ticket=None if report.fill is None else report.fill.ticket,
            latency_ms=report.latency_ms,
            slippage_points=None
            if report.fill is None
            else str(report.fill.slippage_points),
            simulated=None if report.fill is None else report.fill.simulated,
            tick_id=quote.tick_id,
            quote_source=quote.source,
            quote_source_time=quote.source_time.isoformat(),
            quote_received_at=quote.received_at.isoformat(),
            quote_bid=str(quote.bid),
            quote_ask=str(quote.ask),
        )
        if report.fill is not None:
            self._guardian.observe_execution(
                slippage_points=report.fill.slippage_points,
                fill_latency_ms=report.fill.latency_ms,
            )
            if report.fill.filled_lots > 0:
                self.stats["fills"] += 1
                if report.fill.partial:
                    self.stats["partials"] += 1
        if report.status == execution.REJECTED:
            self.stats["rejects"] += 1
        if report.status == execution.UNKNOWN_OUTCOME:
            self.stats["unknown"] += 1
            self._guardian.suspend_entries(execution.UNRESOLVED_ORDER)
        if report.fill is not None:
            self._slippages.append(report.fill.slippage_points)
            self._latencies.append(int(report.fill.latency_ms))
        if report.risk is not None and not report.risk.allowed:
            self.stats["risk_refused"] += 1
            for reason in report.risk.reasons:
                self._risk_refusals[reason] = self._risk_refusals.get(reason, 0) + 1

    def _open(
        self,
        *,
        trade_report: ExecutionReport,
        signal: Signal,
        sizing: Any,
        spec: Any,
        state: RegimeState,
        tick: Tick,
        now_ms: int,
    ) -> None:
        fill = trade_report.fill
        if fill is None or fill.ticket is None or fill.price is None:
            return
        position = self._book.open_from_fill(
            fill=fill,
            symbol=signal.symbol,
            side=signal.side,
            stop_price=signal.stop_price or fill.price,
            target_price=signal.target_price,
            strategy_fingerprint=signal.parameter_fingerprint,
            now_ms=now_ms,
        )
        spread_points = tick.spread_points(spec.point)
        trade_cost = cost_for(spec=spec, cost=self._config.cost, spread_points=spread_points)
        self._cost_by_position[position.position_id] = trade_cost.total_points
        self._initial_stop[position.position_id] = position.initial_stop_points
        self._fingerprint_by_position[position.position_id] = signal.parameter_fingerprint
        self._strategy_by_position[position.position_id] = signal.strategy
        self._regime_by_position[position.position_id] = state.regime
        self._entered_spread[position.position_id] = spread_points
        self._last_entry_ms = now_ms
        self._guardian.record_open(
            OpenPositionRisk(
                position_id=position.position_id,
                symbol=spec.symbol,
                pair=spec.pair,
                side=signal.side,
                risk_account=sizing.risk_account,
                notional_account=sizing.notional_account,
                currencies=self._currencies(spec),
                protected=position.protection_verified,
            )
        )
        self._scribe.record(
            scribe_module.POSITION_OPENED,
            at_ms=now_ms,
            actor="position_manager",
            position_id=position.position_id,
            strategy=signal.strategy,
            fingerprint=signal.parameter_fingerprint,
            side=signal.side,
            lots=str(position.lots),
            entry_price=str(position.entry_price),
            stop_price=str(position.stop_price),
            target_price=None if position.target_price is None else str(position.target_price),
            protection_verified=position.protection_verified,
            regime=state.regime,
            cost_points=str(trade_cost.total_points),
            cost_complete=trade_cost.complete,
        )

    # -- regime bookkeeping ----------------------------------------------

    def _record_regime(self, state: RegimeState, now_ms: int) -> None:
        self.stats["regimes"][state.regime] = self.stats["regimes"].get(state.regime, 0) + 1
        previous = self._last_regime(state.symbol)
        if previous == state.regime:
            return
        self._last_regimes[state.symbol] = state.regime
        self._scribe.record(
            scribe_module.STATE,
            at_ms=now_ms,
            actor="regime",
            symbol=state.symbol,
            state="no_trade" if not state.tradeable else state.regime,
            regime=state.regime,
            previous=previous,
            no_trade_reason=state.no_trade_reason or None,
            eligible=sorted(state.eligible),
            size_multiplier=str(state.size_multiplier),
        )

    def _last_regime(self, symbol: str) -> str | None:
        return self._last_regimes.get(symbol)

    # -- quality ---------------------------------------------------------

    def _drain_quality_events(self, received: datetime) -> None:
        events = self._core.events(limit=10_000)
        for event in events[self._seen_events :]:
            self.stats["quality"][event.kind] = self.stats["quality"].get(event.kind, 0) + 1
            self._scribe.record(
                scribe_module.QUALITY,
                at_ms=_ms(received),
                actor="tick_core",
                event_kind=event.kind,
                symbol=event.symbol,
                severity=event.severity,
                detail=event.detail,
            )
        self._seen_events = max(self._seen_events, len(events))

    # -- public controls -------------------------------------------------

    def emergency_stop(self, *, reason: str, at_ms: int | None = None) -> ShutdownPlan:
        """Stop new entries.  Never closes a position and never disables management."""

        stamp = at_ms if at_ms is not None else _ms(self._current_time())
        plan = self._guardian.emergency_stop(reason=reason, at_ms=stamp)
        self._scribe.record(
            scribe_module.STATE,
            at_ms=stamp,
            actor="operator",
            state="emergency_stop",
            reason=reason,
            entries_suspended=plan.entries_suspended,
            position_management_allowed=plan.position_management_allowed,
            unprotected_positions=list(plan.unprotected_positions),
        )
        self._suspended_by_desk.add(execution.UNRESOLVED_ORDER)
        return plan

    def clear_emergency(self, *, operator: str) -> tuple[bool, str]:
        ok, detail = self._guardian.clear_emergency(operator=operator)
        self._scribe.record(
            scribe_module.STATE,
            at_ms=_ms(self._current_time()),
            actor=operator,
            state="emergency_cleared" if ok else "emergency_clear_refused",
            detail=detail,
        )
        return ok, detail

    def flatten_all(self, *, at_ms: int | None = None, reason: str = "emergency_close") -> list[ManagementAction]:
        """The deliberate operator step.  Positions without a quote stay open."""

        stamp = at_ms if at_ms is not None else _ms(self._current_time())
        ticks = {
            symbol: tick
            for symbol in {position.symbol for position in self._book.open_positions}
            if (tick := self._core.last(symbol)) is not None
        }
        actions = self._book.flatten_all(ticks=ticks, now_ms=stamp, reason=reason)
        for trade in self._book.closed:
            self._settle(trade, now_ms=stamp)
        return actions

    def resolve_unresolved(self, *, at_ms: int | None = None) -> dict[str, Any]:
        """Query every ambiguous submission.  Never resend it."""

        stamp = at_ms if at_ms is not None else _ms(self._current_time())
        resolved: dict[str, Any] = {}
        for request_id in list(self._trader.unresolved):
            status, fill = self._trader.resolve(request_id)
            resolved[request_id] = {
                "status": status,
                "price": None if fill is None or fill.price is None else str(fill.price),
            }
            self._scribe.record(
                scribe_module.EXECUTION_RESULT,
                at_ms=stamp,
                actor="trader",
                request_id=request_id,
                status=f"recovered_{status}",
                price=None if fill is None or fill.price is None else str(fill.price),
                ticket=None if fill is None else fill.ticket,
            )
            if status == "not_found":
                self._scribe.error(
                    at_ms=stamp,
                    error="unresolved_order_not_found",
                    detail="the venue has no record; a resend requires an operator decision",
                    request_id=request_id,
                )
        if not self._trader.has_unresolved:
            self._guardian.resume_entries(reason="unresolved orders resolved", operator="desk")
        return resolved

    def reconcile(self) -> dict[str, Any]:
        venue_orders = getattr(self._venue, "orders", {})
        tickets = {
            fill.ticket
            for fill in venue_orders.values()
            if fill.ticket is not None and fill.status == execution.ACCEPTED
        }
        result = self._book.reconcile(venue_tickets=tickets)
        result["trader"] = self._trader.reconcile(tickets=tickets)
        result["protection"] = self._book.verify_protection()
        return result

    # -- reporting -------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        open_positions = [
            {
                **position.audit_view(),
                "stop_verified": position.protection_verified,
                "cost_points": str(self._cost_by_position.get(position.position_id, Decimal(0))),
            }
            for position in self._book.open_positions
        ]
        return {
            "run_id": self._run_id,
            "mode": self._config.mode,
            "sequence": self._core.sequence(),
            "equity": str(self._equity),
            "starting_equity": str(self._starting_equity),
            "peak_equity": str(self._peak_equity),
            "trades": self.stats["trades"],
            "open_positions": open_positions,
            "unresolved_orders": sorted(self._trader.unresolved),
            "scribe_head": self._scribe.verify()["head"],
            "ledger_chain": self._scribe.verify(),
            "guardian": self._guardian.status(),
            "quality": {
                symbol: self._core.quality(symbol, now=self._now())
                for symbol in self._specs
            },
            "note": (
                "A snapshot is a paper ledger plus the venue's own answers. Restoring it "
                "re-reads both and re-verifies protection against the venue; a position "
                "whose protection cannot be verified blocks entries until an operator "
                "resolves it. Nothing is inherited by assuming it was still true."
            ),
        }

    def status(self) -> dict[str, Any]:
        """A reader-facing view.  Nothing here is inferred; it is all counted."""

        return {
            "mode": self._config.mode,
            "run_id": self._run_id,
            "engine": self._engine,
            "equity": str(self._equity),
            "starting_equity": str(self._starting_equity),
            "open_positions": len(self._book.open_positions),
            "closed_trades": len(self._book.closed),
            "unresolved": len(self._trader.unresolved),
            "ticks": {
                "received": self.stats["ticks"],
                "accepted": self.stats["accepted"],
                "refused": sum(self._tick_refusals.values()),
            },
            "tick_refusals": dict(self._tick_refusals),
            "stats": {
                key: (dict(value) if isinstance(value, dict) else value)
                for key, value in self.stats.items()
                if key not in {"ticks", "accepted", "refused", "quality", "regimes", "blocked"}
            },
            "ev_rejections": dict(self._ev_rejections),
            "size_refusals": dict(self._size_refusals),
            "risk_refusals": dict(self._risk_refusals),
            "regimes": dict(self.stats["regimes"]),
            "blocked": dict(self.stats["blocked"]),
            "quality": {
                symbol: self._core.quality(symbol, now=self._now())
                for symbol in self._specs
            },
            "trader": self._trader.execution_stats(),
            "guardian": self._guardian.status(),
            "ledger": self._scribe.summary(),
            "core": self._core.describe(),
        }

    def _now(self) -> datetime:
        """The clock a decision is taken against.

        In replay that is the last quote's receive stamp, not the wall clock: a
        historical feed would otherwise be permanently stale, and "stale" would stop
        meaning anything.  When no tick has arrived, the process clock is used.
        """

        return self._last_tick_at or self._clock()

    def _current_time(self) -> datetime:
        return self._now()
