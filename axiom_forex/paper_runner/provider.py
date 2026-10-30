"""A paper platform for a *real*, terminal-resolved symbol.

The fixture platform exists to test the machinery.  To paper-trade a broker's own
symbol, the same components are assembled from the broker's terms -- read through
the instrument catalogue -- plus the operator's setup document: calendar, account,
costs, swap, risk limits, and size.

Nothing is defaulted.  If a term is missing the builder returns a
:class:`StackRefusal` naming it, and the workstation shows that as a setup
blocker, not as a runner that quietly does nothing.  In particular it refuses when:

* the pair has no session windows in the calendar (when the market is open would
  otherwise be unknowable);
* the pair has no swap card (overnight financing would be unmodelled, which
  understates cost);
* a conversion rate needed for the pair or its exposure cannot be derived from
  the terminal's own quotes, or a declared static rate is stale;
* the operator's cost model does not validate.

The stack is **paper only**: a local ledger, the local paper engine, and the
operator's declared risk limits.  It holds no MT5 adapter and no order-sending
surface.  The live leg lives separately in :mod:`axiom_forex.paper_runner.live`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.common.validation import ValidationResult, canonical_pair
from app.config.settings import SafetySettings, SystemState
from app.data.models import QuoteFreshness
from app.execution.broker import BrokerCapabilities
from app.risk.pretrade import PreTradeGate
from app.universe.specs import InstrumentCatalog

from axiom_forex.brokers.local_paper import make_local_paper_broker
from axiom_forex.config.provider_setup import ProviderSetup
from axiom_forex.database.audit import DurableAuditLog
from axiom_forex.database.repository import ForexRepository
from axiom_forex.execution.config import ExecutionConfig
from axiom_forex.execution.paper_engine import PaperExecutionEngine
from axiom_forex.execution.planner import ForexExecutionPlanner
from axiom_forex.execution.reservations import OrderReservationBook
from axiom_forex.portfolio.costs import ForexCostModel
from axiom_forex.portfolio.ledger import ForexPortfolio
from axiom_forex.portfolio.rollover import RolloverEngine, SwapRateCard
from axiom_forex.risk.context import ForexExecutionContextFactory

from .conversion import LiveConversionRates

NO_CALENDAR_WINDOWS = "no_calendar_windows_for_pair"
MISSING_SWAP_TERMS = "missing_swap_terms_for_pair"
MISSING_CONVERSION_RATE = "missing_conversion_rate"
MISSING_ENTRY_LIMIT = "missing_entry_exposure_limit"
SPEC_UNAVAILABLE = "resolved_spec_unavailable"
SETUP_INVALID = "provider_setup_invalid"

#: The paper lane's data class.  Quotes are the broker's own ticks; the fills,
#: however, are simulated locally and are never presented as broker fills.
QUOTE_DATA_CLASS = "LIVE_BROKER_TICK"


@dataclass(frozen=True)
class StackRefusal:
    reason: str
    detail: str = ""
    pair: str = ""

    def audit_view(self) -> dict[str, Any]:
        return {"reason": self.reason, "detail": self.detail, "pair": self.pair}


@dataclass
class ProviderPaperPlatform:
    """Everything one paper lane needs for one resolved broker symbol.

    The attribute names match the fixture platform's, so the runner's
    ``lane_from_platform`` and journal wiring work unchanged.
    """

    setup: ProviderSetup
    spec: Any
    resolved: Any
    calendar: Any
    account: Any
    repository: ForexRepository
    audit_log: DurableAuditLog
    portfolio: ForexPortfolio
    paper_execution: PaperExecutionEngine
    planner: ForexExecutionPlanner
    context_factory: ForexExecutionContextFactory
    cost_model: ForexCostModel
    rollover_engine: RolloverEngine
    execution_config: ExecutionConfig
    quote_freshness: QuoteFreshness
    conversion: Any
    gate: Any = None
    terminal: Any = None
    capabilities: Any = None
    system_state: Any = None

    @property
    def pair(self) -> str:
        return str(getattr(self.spec, "canonical_pair", "") or getattr(self.spec, "pair", ""))

    @property
    def broker_symbol(self) -> str:
        return str(getattr(self.resolved, "broker_symbol", "") or "")

    @property
    def data_class(self) -> str:
        return QUOTE_DATA_CLASS

    @property
    def execution_eligible(self) -> bool:
        """Paper fills are locally simulated; the data class is the broker's."""

        return False

    def new_engine(self) -> Any:
        """A fresh, single-use backtest engine on this platform's own terms.

        Evaluation and backtesting must use exactly the terms the paper lane
        would: same spec, calendar, cost model, account, conversion basis, and
        risk limits.  The engine is single-use by construction, so every run
        gets new objects; nothing here touches the live lane's portfolio.
        """

        from axiom_forex.backtesting.engine import ForexBacktestEngine
        from axiom_forex.brokers.local_paper import make_local_paper_broker
        from axiom_forex.execution.paper_engine import PaperExecutionEngine
        from axiom_forex.execution.planner import ForexExecutionPlanner
        from axiom_forex.execution.reservations import OrderReservationBook
        from axiom_forex.portfolio.ledger import ForexPortfolio
        from axiom_forex.risk.context import ForexExecutionContextFactory

        audit = self.audit_log or DurableAuditLog(repository=self.repository)
        gate = PreTradeGate(
            InstrumentCatalog((self.spec,)), self.calendar, audit
        )
        capabilities = self.capabilities or BrokerCapabilities(
            broker_id="PROVIDER_LOCAL_PAPER",
            capability_version="capabilities-v1",
            execution_mode="LOCAL_PAPER",
            supports_live_orders=False,
            supports_partial_fills=False,
            order_types=frozenset({"SIMULATED_INTENT"}),
            calendar_id=self.calendar.calendar_id,
        )
        portfolio = ForexPortfolio(
            account=self.account,
            spec=self.spec,
            conversion_rates=self.conversion,
            cost_model=self.cost_model,
            calendar=self.calendar,
        )
        paper_execution = PaperExecutionEngine(
            broker=make_local_paper_broker(
                gate=gate, capabilities=capabilities, audit_log=audit
            ),
            portfolio=portfolio,
            audit_log=audit,
            reservations=OrderReservationBook(
                self.repository,
                entry_limit=self.setup.entry_limit,
                account_currency=self.account.account_currency_code,
            ),
        )
        context_factory = ForexExecutionContextFactory(
            safety_settings=SafetySettings(),
            system_state=self.system_state
            or SystemState(
                emergency_stop=False, kill_switch_active=False, execution_paused=False
            ),
            account=self.account,
            risk_limits=self.setup.risk,
            cost_model=self.cost_model,
            rollover_policy=self.setup.rollover,
            conversion_rates=self.conversion,
            quote_freshness=self.quote_freshness,
            portfolio=portfolio,
            paper_execution=paper_execution,
        )
        planner = ForexExecutionPlanner(
            spec=self.spec,
            calendar=self.calendar,
            config=self.execution_config,
            context_factory=context_factory,
        )
        return ForexBacktestEngine(
            spec=self.spec,
            calendar=self.calendar,
            portfolio=portfolio,
            paper_execution=paper_execution,
            audit_log=audit,
            config=self.execution_config,
            planner=planner,
        )

    def audit_view(self) -> dict[str, Any]:
        return {
            "pair": self.pair,
            "broker_symbol": self.broker_symbol,
            "spec_version": self.spec.spec_version,
            "account_currency": self.account.account_currency_code,
            "leverage": str(self.account.leverage),
            "calendar_id": self.calendar.calendar_id,
            "calendar_version": self.calendar.version,
            "cost_model": f"{self.setup.costs.model_id}/{self.setup.costs.version}",
            "commission_rate": str(self.setup.costs.commission_rate),
            "slippage_pips": str(self.setup.costs.slippage_pips),
            "swap_modelled": bool(getattr(self.rollover_engine, "cards", {})),
            "quantity": str(self.execution_config.quantity),
            "quote_data_class": QUOTE_DATA_CLASS,
            "fills": "SIMULATED_LOCALLY",
            "paper_only": True,
            "live": False,
        }


def _default_freshness(max_age: Decimal = Decimal("5")) -> QuoteFreshness:
    return QuoteFreshness(
        policy_id="provider-paper-freshness",
        version="v1",
        max_age_seconds=max_age,
        max_future_skew_seconds=Decimal("2"),
        max_feed_latency_seconds=max_age,
    )


def build_provider_platform(
    *,
    setup: ProviderSetup,
    resolved: Any,
    repository: ForexRepository,
    spec: Any | None = None,
    audit_log: DurableAuditLog | None = None,
    market: Any = None,
    terminal: Any = None,
    symbol_of_pair: dict[tuple[str, str], str] | None = None,
    symbols: tuple[str, ...] = (),
    freshness: QuoteFreshness | None = None,
    system_state: SystemState | None = None,
    now: datetime | None = None,
) -> tuple[ProviderPaperPlatform | None, StackRefusal | None]:
    """Assemble the paper platform for one catalogue-resolved symbol."""

    spec = spec if spec is not None else getattr(resolved, "spec", None)
    if spec is None:
        return None, StackRefusal(SPEC_UNAVAILABLE, "no resolved specification")
    pair = str(getattr(spec, "canonical_pair", "") or getattr(spec, "pair", ""))
    canonical, error = canonical_pair(pair)
    if error or canonical is None:
        return None, StackRefusal(SETUP_INVALID, f"invalid_pair:{pair}", pair)

    spec_check: ValidationResult = spec.validate()
    if not spec_check.valid:
        return None, StackRefusal(
            SETUP_INVALID, "spec_validation:" + ",".join(spec_check.errors or ()), pair
        )

    terms = setup.terms_for(canonical)
    if terms is None:
        return None, StackRefusal(
            SETUP_INVALID,
            f"the setup has no symbol terms for {canonical}",
            pair,
        )
    if str(getattr(terms, "broker_symbol", "") or "") != str(
        getattr(resolved, "broker_symbol", "") or ""
    ):
        return None, StackRefusal(
            SETUP_INVALID,
            f"the setup's broker_symbol ({terms.broker_symbol!r}) is not the symbol the "
            f"catalogue resolved ({getattr(resolved, 'broker_symbol', '')!r}); "
            "re-resolve rather than renaming it here",
            pair,
        )

    windows = setup.calendar.windows_by_pair.get(canonical) or ()
    if not windows:
        return None, StackRefusal(
            NO_CALENDAR_WINDOWS,
            f"the setup calendar {setup.calendar.calendar_id} has no session windows "
            f"for {canonical}; when the market is open is not knowable",
            pair,
        )

    swap = setup.swap_for(canonical)
    if swap is None:
        return None, StackRefusal(
            MISSING_SWAP_TERMS,
            f"no swap card for {canonical}: overnight financing would be unmodelled",
            pair,
        )
    card = SwapRateCard(
        pair=canonical,
        long_rate=swap.long_rate,
        short_rate=swap.short_rate,
        source=swap.source,
        version=swap.version,
    )

    if setup.entry_limit is None:
        return None, StackRefusal(
            MISSING_ENTRY_LIMIT,
            "the setup declares no entry_exposure_limit; without a declared cap the "
            "reservation book refuses new entries",
            pair,
        )

    account = setup.account
    freshness = freshness or setup.quote_freshness or _default_freshness()
    if market is not None:
        rates: Any = LiveConversionRates(
            source=account.conversion_source,
            freshness=freshness,
            market=market,
            symbols=symbols,
            symbol_of_pair=symbol_of_pair or {},
        )
    else:
        rates = setup.rate_book(source=account.conversion_source)
        # Presence only: whether a declared rate is *still* valid is judged when it
        # is used, at the instant of that use, by the book's own freshness policy.
        # Judging it here against the wall clock would reject a correctly declared
        # historical basis (a replay lives on its own timeline).
        declared = set(getattr(rates, "rates", {}) or {})
        for currency in {spec.quote_currency, spec.base_currency}:
            if currency == account.account_currency_code:
                continue
            key = (currency.upper(), account.account_currency_code.upper())
            reciprocal = (key[1], key[0])
            if key not in declared and reciprocal not in declared:
                return None, StackRefusal(
                    MISSING_CONVERSION_RATE,
                    f"{currency}->{account.account_currency_code}: the setup declares no "
                    "rate and no reciprocal rate",
                    pair,
                )

    rollover_engine = RolloverEngine(
        policy=setup.rollover,
        cards={canonical: card},
        conversion_rates=rates,
        account=account,
        calendar_id=setup.calendar.calendar_id,
    )
    cost_model = ForexCostModel(
        terms=setup.costs,
        spec=spec,
        account=account,
        conversion_rates=rates,
        rollover_engine=rollover_engine,
    )
    validation = cost_model.validate()
    if not validation.valid:
        return None, StackRefusal(
            SETUP_INVALID, "cost_model:" + ",".join(validation.errors or ()), pair
        )

    audit = audit_log or DurableAuditLog(repository=repository)
    state = system_state or SystemState(
        emergency_stop=False, kill_switch_active=False, execution_paused=False
    )
    catalog = InstrumentCatalog((spec,))
    gate = PreTradeGate(catalog, setup.calendar, audit)
    capabilities = BrokerCapabilities(
        broker_id="PROVIDER_LOCAL_PAPER",
        capability_version="capabilities-v1",
        execution_mode="LOCAL_PAPER",
        supports_live_orders=False,
        supports_partial_fills=False,
        order_types=frozenset({"SIMULATED_INTENT"}),
        calendar_id=setup.calendar.calendar_id,
    )
    portfolio = ForexPortfolio(
        account=account,
        spec=spec,
        conversion_rates=rates,
        cost_model=cost_model,
        calendar=setup.calendar,
    )
    reservations = OrderReservationBook(
        repository,
        entry_limit=setup.entry_limit,
        account_currency=account.account_currency_code,
    )
    paper_execution = PaperExecutionEngine(
        broker=make_local_paper_broker(
            gate=gate, capabilities=capabilities, audit_log=audit
        ),
        portfolio=portfolio,
        audit_log=audit,
        reservations=reservations,
    )
    context_factory = ForexExecutionContextFactory(
        safety_settings=SafetySettings(),
        system_state=state,
        account=account,
        risk_limits=setup.risk,
        cost_model=cost_model,
        rollover_policy=setup.rollover,
        conversion_rates=rates,
        quote_freshness=freshness,
        portfolio=portfolio,
        paper_execution=paper_execution,
    )
    execution_config = ExecutionConfig(
        dataset_id=f"provider:{spec.spec_version}:{canonical}",
        quantity=setup.execution.quantity,
        # The catalogue records the terminal symbol under the ``mt5`` alias; the
        # request must use the same pair so the pre-trade gate can resolve it.
        provider_id="mt5",
        provider_symbol=getattr(resolved, "broker_symbol", "") or canonical,
        strategy_id="",
        max_open_positions=max(int(setup.execution.max_open_positions), 1),
        allow_short=bool(setup.execution.allow_short),
    )
    planner = ForexExecutionPlanner(
        spec=spec,
        calendar=setup.calendar,
        config=execution_config,
        context_factory=context_factory,
    )
    return (
        ProviderPaperPlatform(
            setup=setup,
            spec=spec,
            resolved=resolved,
            calendar=setup.calendar,
            account=account,
            repository=repository,
            audit_log=audit,
            portfolio=portfolio,
            paper_execution=paper_execution,
            planner=planner,
            context_factory=context_factory,
            cost_model=cost_model,
            rollover_engine=rollover_engine,
            execution_config=execution_config,
            quote_freshness=freshness,
            conversion=rates,
            gate=gate,
            terminal=terminal,
            capabilities=capabilities,
            system_state=state,
        ),
        None,
    )


def build_provider_feed(
    *,
    platform: ProviderPaperPlatform,
    market: Any = None,
    terminal: Any = None,
    interval_seconds: int = 60,
    max_quote_age_seconds: Decimal = Decimal("5"),
    max_gap_windows: int = 0,
) -> Any:
    """A terminal tick feed bound to the *resolved* broker symbol.

    The feed never re-resolves the symbol: the catalogue decided it, the operator
    confirmed it in the setup, and a later rename or a second suffix variant must
    not silently redirect the runner to a different instrument.
    """

    from .feed import FeedPolicy, TerminalBarFeed

    policy = FeedPolicy(
        interval_seconds=max(int(interval_seconds), 1),
        max_quote_age_seconds=max_quote_age_seconds,
        max_gap_windows=max_gap_windows,
        timeframe=f"{max(int(interval_seconds), 1)}s",
    )
    return TerminalBarFeed(
        market=_ResolvedSymbolMarket(
            symbol=platform.broker_symbol, market=market, terminal=terminal
        ),
        spec=platform.spec,
        policy=policy,
    )


@dataclass
class _ResolvedSymbolMarket:
    """Use the exact symbol the catalogue resolved, and nothing else."""

    symbol: str
    market: Any = None
    terminal: Any = None

    def symbol_for(self, spec: Any) -> str:
        if not self.symbol:
            raise ValueError("resolved_symbol_missing")
        return self.symbol

    def quote(self, spec: Any) -> Any:
        if self.terminal is not None:
            return self.terminal.quote(spec, self.symbol)
        quote_symbol = getattr(self.market, "quote_symbol", None)
        if callable(quote_symbol):
            return quote_symbol(spec, self.symbol)
        if self.market is None:
            raise ValueError("market_data_unavailable")
        return self.market.quote(spec)

    def tick(self, symbol: str) -> Any:
        for holder in (self.terminal, self.market):
            getter = getattr(holder, "tick", None)
            if callable(getter):
                return getter(symbol)
        raise ValueError("terminal_tick_unavailable")
