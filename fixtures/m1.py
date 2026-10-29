"""Synthetic M1 fixtures with a DST-aware 24/5-style calendar.

The calendar is explicitly labeled as a fixture.  It demonstrates weekday
operation, a daily break, a holiday, weekend gaps, and a US DST transition; it
is not a broker calendar and must never be used as a production default.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.accounting.costs import CostModelTerms, StaticCostModel
from app.accounting.fx import ConversionRate, ConversionRateBook
from app.accounting.rollover import RolloverPolicy
from app.audit.log import InMemoryAuditLog
from app.calendar.trading import CalendarWindow, ExplicitTradingCalendar
from app.common.types import ExecutionMode, OrderSide, TradingMode
from app.config.settings import AccountProfile, RiskLimits, SafetySettings, SystemState
from app.data.models import ForexQuote, ForexQuoteBar, QuoteFreshness, QuoteSideBar
from app.execution.broker import BrokerCapabilities, LocalPaperBroker
from app.risk.models import (
    CloseIntent,
    GateContext,
    OrderRequest,
    PositionSnapshot,
    RiskContext,
    UnresolvedOrder,
)
from app.risk.pretrade import PreTradeGate
from axiom_forex.risk.entry_limits import (
    DATA_CLASS_SYNTHETIC_FIXTURE,
    EntryExposureLimit,
)
from app.universe.specs import CurrencyPairSpec, InstrumentCatalog, InstrumentStatus, QuantityUnit

UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")

PAIR = "AAA/BBB"
PROVIDER_ID = "SYNTHETIC_FEED"
PROVIDER_SYMBOL = "AAA_BBB"
ACCOUNT_CURRENCY = "CCC"
CALENDAR_ID = "synthetic-24x5-v1"
CALENDAR_VERSION = "calendar-v1"
CORRELATION_GROUP = "SYNTHETIC_RISK_GROUP"
COST_MODEL_ID = "cost-model-v1"
COST_MODEL_VERSION = "cost-v1"
ROLLOVER_SCHEDULE_ID = "rollover-schedule-v1"
ROLLOVER_SCHEDULE_VERSION = "rollover-policy-v1"
CONVERSION_SOURCE = "SYNTHETIC_FX_SOURCE"

BID = Decimal("1.10000")
ASK = Decimal("1.10020")
QUOTE_RATE = Decimal("0.9")       # 1 BBB = 0.9 CCC
BASE_RATE = Decimal("1.1")        # 1 AAA = 1.1 CCC
QUANTITY = Decimal("0.10")        # lots; 0.10 * 1000 = 100 base units

EVALUATED_AT = datetime(2026, 3, 10, 10, 0, tzinfo=NEW_YORK).astimezone(UTC)


def local_utc(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=NEW_YORK).astimezone(UTC)


def make_calendar_windows() -> tuple[CalendarWindow, ...]:
    """Explicit UTC windows: 24/5 shape with 17:00-18:00 local daily breaks."""
    windows: list[CalendarWindow] = []

    def add(
        start: tuple[int, int, int, int, int],
        end: tuple[int, int, int, int, int],
        label: str,
    ) -> None:
        windows.append(
            CalendarWindow(
                start=local_utc(*start),
                end=local_utc(*end),
                label=label,
            )
        )

    # Pre-DST Friday; 17:00 local is 22:00 UTC.
    add((2026, 3, 6, 0, 0), (2026, 3, 6, 17, 0), "fri_pre_dst_core")
    add((2026, 3, 6, 18, 0), (2026, 3, 7, 0, 0), "fri_pre_dst_evening")
    # Saturday is intentionally absent: a weekend gap.
    # DST starts 2026-03-08. Sunday 17:00 local is already 21:00 UTC.
    add((2026, 3, 8, 17, 0), (2026, 3, 9, 0, 0), "sun_open")
    for day in (9, 10, 11, 12):
        add((2026, 3, day, 0, 0), (2026, 3, day, 17, 0), f"mar_{day}_core")
        add((2026, 3, day, 18, 0), (2026, 3, day + 1, 0, 0), f"mar_{day}_evening")
    # Friday close; no evening session, so the weekend gap starts here.
    add((2026, 3, 13, 0, 0), (2026, 3, 13, 17, 0), "fri_close")
    return tuple(windows)


def make_calendar(*, version: str = CALENDAR_VERSION) -> ExplicitTradingCalendar:
    return ExplicitTradingCalendar(
        calendar_id=CALENDAR_ID,
        version=version,
        source="LOCAL_FIXTURE_NOT_A_BROKER_CALENDAR",
        timezone_name="America/New_York",
        valid_from=local_utc(2026, 3, 6, 0, 0),
        valid_until=local_utc(2026, 3, 13, 17, 0),
        windows_by_pair={PAIR: make_calendar_windows()},
        holidays_by_pair={PAIR: frozenset({date(2026, 3, 9)})},
    )


def make_spec(**overrides: Any) -> CurrencyPairSpec:
    values: dict[str, Any] = {
        "pair": PAIR,
        "pip_size": Decimal("0.0001"),
        "price_tick_size": Decimal("0.00001"),
        "quote_precision": 5,
        "contract_size": Decimal("1000"),
        "min_quantity": Decimal("0.01"),
        "quantity_step": Decimal("0.01"),
        "max_quantity": Decimal("5"),
        "quantity_unit": QuantityUnit.LOTS,
        "margin_multiplier": Decimal("1"),
        "correlation_group": CORRELATION_GROUP,
        "trading_calendar_id": CALENDAR_ID,
        "trading_calendar_version": CALENDAR_VERSION,
        "status": InstrumentStatus.ACTIVE,
        "spec_version": "spec-v1",
        "source": "SYNTHETIC_INSTRUMENT_SOURCE",
        "provider_aliases": {PROVIDER_ID: PROVIDER_SYMBOL},
    }
    values.update(overrides)
    return CurrencyPairSpec(**values)


def make_catalog(spec: CurrencyPairSpec | None = None) -> InstrumentCatalog:
    return InstrumentCatalog((spec or make_spec(),))


def make_account(**overrides: Any) -> AccountProfile:
    values: dict[str, Any] = {
        "account_id": "SYNTHETIC_PAPER_ACCOUNT",
        "account_currency": ACCOUNT_CURRENCY,
        "leverage": Decimal("5"),
        "initial_capital": Decimal("10000"),
        "conversion_source": CONVERSION_SOURCE,
        "cost_model_id": COST_MODEL_ID,
        "cost_model_version": COST_MODEL_VERSION,
        "rollover_schedule_id": ROLLOVER_SCHEDULE_ID,
        "rollover_schedule_version": ROLLOVER_SCHEDULE_VERSION,
    }
    values.update(overrides)
    return AccountProfile(**values)


def make_entry_limit(**overrides: Any) -> EntryExposureLimit:
    """A clearly synthetic entry-exposure cap for the offline fixtures.

    ``250 CCC`` covers two of the fixture's orders in flight (about 99 CCC
    each) but not three.  The provenance label is the point: this number exists
    so the offline workflow can run, and never stands in for an operator's risk
    policy.
    """

    values: dict[str, Any] = {
        "limit_account": Decimal("250"),
        "account_currency": ACCOUNT_CURRENCY,
        "version": "synthetic-entry-limit-v1",
        "source": "OFFLINE_FIXTURE_PROFILE",
        "data_class": DATA_CLASS_SYNTHETIC_FIXTURE,
    }
    values.update(overrides)
    return EntryExposureLimit(**values)


def make_risk_limits(**overrides: Any) -> RiskLimits:
    values: dict[str, Any] = {
        "max_order_notional_account": Decimal("500"),
        "max_gross_exposure_account": Decimal("1000"),
        "max_margin_account": Decimal("100"),
        "max_abs_currency_exposure_account": Decimal("800"),
        "max_correlation_group_exposure_account": Decimal("800"),
        "max_open_positions": 2,
        "max_daily_loss_account": Decimal("100"),
        "max_trades_per_day": 5,
        "max_spread_pips": Decimal("5"),
        "max_leverage": Decimal("10"),
        "limits_version": "limits-v1",
    }
    values.update(overrides)
    return RiskLimits(**values)


def make_quote_freshness(**overrides: Any) -> QuoteFreshness:
    values: dict[str, Any] = {
        "policy_id": "quote-freshness-v1",
        "version": "freshness-v1",
        "max_age_seconds": Decimal("5"),
        "max_future_skew_seconds": Decimal("1"),
        "max_feed_latency_seconds": Decimal("1"),
    }
    values.update(overrides)
    return QuoteFreshness(**values)


def make_cost_model(**overrides: Any) -> StaticCostModel:
    values: dict[str, Any] = {
        "model_id": COST_MODEL_ID,
        "version": COST_MODEL_VERSION,
        "commission_rate": Decimal("0.0001"),
        "slippage_pips": Decimal("0.2"),
        "financing_rate_daily": Decimal("0.00001"),
        "source": "SYNTHETIC_COST_SOURCE",
    }
    values.update(overrides)
    return StaticCostModel(CostModelTerms(**values))


def make_rollover_policy(**overrides: Any) -> RolloverPolicy:
    values: dict[str, Any] = {
        "schedule_id": ROLLOVER_SCHEDULE_ID,
        "version": ROLLOVER_SCHEDULE_VERSION,
        "daily_cutoff_utc": time(17, 0),
        # None is an explicit fixture choice: no triple-rollover rule is assumed.
        "triple_rollover_weekday": None,
        "calendar_id": CALENDAR_ID,
        "source": "SYNTHETIC_ROLLOVER_SOURCE",
    }
    values.update(overrides)
    return RolloverPolicy(**values)


def make_conversion_rates(
    timestamp: datetime | None = None,
    *,
    source: str = CONVERSION_SOURCE,
    quote_rate: Decimal = QUOTE_RATE,
    base_rate: Decimal = BASE_RATE,
    include_quote_rate: bool = True,
    include_base_rate: bool = True,
) -> ConversionRateBook:
    ts = timestamp or (EVALUATED_AT - timedelta(seconds=1))
    rates: dict[tuple[str, str], ConversionRate] = {}
    if include_quote_rate:
        rates[("BBB", "CCC")] = ConversionRate(
            "BBB", "CCC", quote_rate, ts, source, "bbb-ccc"
        )
    if include_base_rate:
        rates[("AAA", "CCC")] = ConversionRate(
            "AAA", "CCC", base_rate, ts, source, "aaa-ccc"
        )
    return ConversionRateBook(
        source=source,
        freshness=make_quote_freshness(),
        rates=rates,
    )


def make_quote(
    evaluated_at: datetime = EVALUATED_AT,
    *,
    bid: Decimal | str | float | None = BID,
    ask: Decimal | str | float | None = ASK,
    timestamp: datetime | None = None,
    received_at: datetime | None = None,
    include_received_at: bool = True,
    source: str = "SYNTHETIC_QUOTE_SOURCE",
) -> ForexQuote:
    ts = timestamp or (evaluated_at - timedelta(seconds=1))
    if include_received_at:
        received = received_at or (ts + timedelta(milliseconds=100))
    else:
        received = None
    return ForexQuote(
        pair=PAIR,
        timestamp=ts,
        bid=bid,
        ask=ask,
        source=source,
        quote_id="synthetic-quote-1",
        received_at=received,
        metadata={"fixture": "m1"},
    )


def make_quote_bar(
    evaluated_at: datetime = EVALUATED_AT,
    *,
    complete: bool = True,
) -> ForexQuoteBar:
    timestamp = evaluated_at - timedelta(minutes=1)
    return ForexQuoteBar(
        pair=PAIR,
        timeframe="1m",
        timestamp=timestamp,
        duration_seconds=60,
        bid=QuoteSideBar(
            open=Decimal("1.09990"),
            high=Decimal("1.10010"),
            low=Decimal("1.09980"),
            close=BID,
        ),
        ask=QuoteSideBar(
            open=Decimal("1.10010"),
            high=Decimal("1.10030"),
            low=Decimal("1.10000"),
            close=ASK,
        ),
        source="SYNTHETIC_BAR_SOURCE",
        quote_count=12,
        last_quote_at=timestamp + timedelta(seconds=59),
        complete=complete,
    )


def make_risk_context(**overrides: Any) -> RiskContext:
    values: dict[str, Any] = {
        "equity_account": Decimal("10000"),
        "gross_exposure_account": Decimal("0"),
        "currency_exposures_account": {},
        "correlation_group_exposures_account": {},
        "open_positions": 0,
        "realized_pnl_today_account": Decimal("0"),
        "trades_today": 0,
    }
    values.update(overrides)
    return RiskContext(**values)


def make_position_snapshot(**overrides: Any) -> PositionSnapshot:
    """One open ledger position, as the gate receives it from the book."""

    values: dict[str, Any] = {
        "position_id": "fixture-position",
        "account_id": "SYNTHETIC_PAPER_ACCOUNT",
        "pair": PAIR,
        "side": OrderSide.BUY,
        "quantity": QUANTITY,
        "quantity_unit": "LOTS",
        "base_units": Decimal("100"),
        "opened_at": EVALUATED_AT - timedelta(minutes=5),
        "pending_close_quantity": Decimal("0"),
    }
    values.update(overrides)
    return PositionSnapshot(**values)


def make_close_intent(**overrides: Any) -> CloseIntent:
    values: dict[str, Any] = {
        "position_id": "fixture-position",
        "account_id": "SYNTHETIC_PAPER_ACCOUNT",
        "quantity": QUANTITY,
        "pair": PAIR,
        "quantity_unit": "LOTS",
    }
    values.update(overrides)
    return CloseIntent(**values)


def make_capabilities(**overrides: Any) -> BrokerCapabilities:
    values: dict[str, Any] = {
        "broker_id": "SYNTHETIC_LOCAL_PAPER",
        "capability_version": "capabilities-v1",
        "execution_mode": ExecutionMode.LOCAL_PAPER,
        "supports_live_orders": False,
        "supports_partial_fills": False,
        "order_types": frozenset({"SIMULATED_INTENT"}),
        "calendar_id": CALENDAR_ID,
    }
    values.update(overrides)
    return BrokerCapabilities(**values)


def make_request(
    evaluated_at: datetime = EVALUATED_AT,
    *,
    quote: ForexQuote | None = None,
    **overrides: Any,
) -> OrderRequest:
    values: dict[str, Any] = {
        "request_id": "m1-fixture-request",
        "provider_id": PROVIDER_ID,
        "provider_symbol": PROVIDER_SYMBOL,
        "side": OrderSide.BUY,
        "quantity": QUANTITY,
        "target_mode": TradingMode.PAPER,
        "evaluated_at": evaluated_at,
        "quote": quote if quote is not None else make_quote(evaluated_at),
        "strategy_id": "fixture-strategy-m1",
    }
    values.update(overrides)
    return OrderRequest(**values)


@dataclass(frozen=True)
class M1Environment:
    settings: SafetySettings
    system_state: SystemState
    account: AccountProfile
    risk_limits: RiskLimits
    cost_model: StaticCostModel
    rollover_policy: RolloverPolicy
    conversion_rates: ConversionRateBook
    quote_freshness: QuoteFreshness
    risk_context: RiskContext
    catalog: InstrumentCatalog
    calendar: ExplicitTradingCalendar
    audit_log: InMemoryAuditLog
    request: OrderRequest
    capabilities: BrokerCapabilities

    def context(self, **overrides: Any) -> GateContext:
        values: dict[str, Any] = {
            "settings": self.settings,
            "system_state": self.system_state,
            "account": self.account,
            "risk_limits": self.risk_limits,
            "cost_model": self.cost_model,
            "rollover_policy": self.rollover_policy,
            "conversion_rates": self.conversion_rates,
            "quote_freshness": self.quote_freshness,
            "risk_context": self.risk_context,
            "unresolved_orders": (),
        }
        values.update(overrides)
        return GateContext(**values)

    def gate(self) -> PreTradeGate:
        return PreTradeGate(self.catalog, self.calendar, self.audit_log)

    def broker(self) -> LocalPaperBroker:
        return LocalPaperBroker(self.gate(), self.capabilities, self.audit_log)


def make_environment(
    evaluated_at: datetime = EVALUATED_AT,
    *,
    quote: ForexQuote | None = None,
    rate_timestamp: datetime | None = None,
    spec: CurrencyPairSpec | None = None,
    calendar: ExplicitTradingCalendar | None = None,
) -> M1Environment:
    effective_spec = spec or make_spec()
    effective_calendar = calendar or make_calendar()
    effective_quote = quote or make_quote(evaluated_at)
    effective_rate_timestamp = rate_timestamp or (evaluated_at - timedelta(seconds=1))
    return M1Environment(
        settings=SafetySettings(),
        system_state=SystemState(
            emergency_stop=False,
            kill_switch_active=False,
            execution_paused=False,
        ),
        account=make_account(),
        risk_limits=make_risk_limits(),
        cost_model=make_cost_model(),
        rollover_policy=make_rollover_policy(),
        conversion_rates=make_conversion_rates(effective_rate_timestamp),
        quote_freshness=make_quote_freshness(),
        risk_context=make_risk_context(),
        catalog=make_catalog(effective_spec),
        calendar=effective_calendar,
        audit_log=InMemoryAuditLog(),
        request=make_request(evaluated_at, quote=effective_quote),
        capabilities=make_capabilities(),
    )


def with_unresolved_order(context: GateContext, state: str = "SUBMITTED") -> GateContext:
    return replace(
        context,
        unresolved_orders=(
            UnresolvedOrder("fixture-unresolved", state, PAIR, EVALUATED_AT),
        ),
    )
