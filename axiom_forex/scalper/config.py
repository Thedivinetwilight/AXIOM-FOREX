"""AX-SCALPER configuration: frozen, validated, and paper only.

The configuration is the first of the two places paper-only is enforced.  The
other is the paper venue itself (:mod:`axiom_forex.scalper.execution`), because a
guarantee that lives only in a form field is not a guarantee.

Every number in here is *declared*: a threshold is written down before any
measurement is taken, and the same threshold is what the lab reports against.  No
value is derived from a backtest that looked good.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

PAPER = "PAPER"
LIVE = "LIVE"

#: Cost states, borrowed from the research cost model's vocabulary so a reader who
#: has met one has met the other.  An unknown cost is never silently zero.
OBSERVED = "OBSERVED"
ESTIMATE = "ESTIMATE"
ASSUMED_ZERO = "ASSUMED_ZERO"
UNKNOWN = "UNKNOWN"

#: Reason codes the configuration itself can refuse with.
CONFIG_NOT_PAPER = "scalper_is_paper_only"
CONFIG_NO_INSTRUMENTS = "no_instrument_declared"
CONFIG_INCOMPLETE_TERMS = "instrument_terms_incomplete"
CONFIG_UNKNOWN_COST = "cost_is_unknown"


class ScalperConfigError(ValueError):
    """Configuration was refused.  ``reason`` is stable; ``detail`` is for people."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _dec(value: Any) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


@dataclass(frozen=True)
class InstrumentSpec:
    """What the broker reported about one symbol, and nothing that it did not.

    ``point_value_per_lot`` is derived, never typed: contract size × point.  When
    the quote currency is not the account currency a conversion rate must be
    supplied, and without one the terms are *incomplete* rather than zero-cost.
    """

    symbol: str
    pair: str
    point: Decimal
    digits: int
    contract_size: Decimal
    volume_min: Decimal
    volume_step: Decimal
    volume_max: Decimal
    stops_level_points: Decimal
    margin_rate: Decimal
    quote_currency: str
    account_currency: str
    conversion_rate: Decimal | None = None
    source: str = "MT5_SYMBOL_INFO"
    defects: tuple[str, ...] = ()

    @property
    def point_value_per_lot(self) -> Decimal | None:
        """Account currency per one point, per one lot.  ``None`` when unknowable."""

        if self.conversion_rate is None:
            if self.quote_currency != self.account_currency:
                return None
            return self.contract_size * self.point
        return self.contract_size * self.point * self.conversion_rate

    @property
    def complete(self) -> bool:
        return not self.defects and self.point_value_per_lot is not None

    def validate(self) -> tuple[str, ...]:
        problems: list[str] = []
        if not self.symbol.strip():
            problems.append("symbol_missing")
        if self.point <= 0:
            problems.append("point_not_positive")
        if self.contract_size <= 0:
            problems.append("contract_size_not_positive")
        if self.volume_min <= 0 or self.volume_step <= 0:
            problems.append("volume_terms_not_positive")
        if self.volume_max < self.volume_min:
            problems.append("volume_max_below_min")
        if self.stops_level_points < 0:
            problems.append("stops_level_negative")
        if self.margin_rate <= 0:
            problems.append("margin_rate_not_positive")
        if self.point_value_per_lot is None:
            problems.append("no_conversion_between_quote_and_account_currency")
        if self.defects:
            problems.extend(self.defects)
        return tuple(problems)

    def audit_view(self) -> dict[str, Any]:
        value = self.point_value_per_lot
        return {
            "symbol": self.symbol,
            "pair": self.pair,
            "point": str(self.point),
            "contract_size": str(self.contract_size),
            "volume_min": str(self.volume_min),
            "volume_step": str(self.volume_step),
            "volume_max": str(self.volume_max),
            "stops_level_points": str(self.stops_level_points),
            "margin_rate": str(self.margin_rate),
            "point_value_per_lot": None if value is None else str(value),
            "account_currency": self.account_currency,
            "quote_currency": self.quote_currency,
            "conversion_rate": None if self.conversion_rate is None else str(self.conversion_rate),
            "complete": self.complete,
            "source": self.source,
        }


@dataclass(frozen=True)
class CostTerms:
    """What one round turn costs, and how well each part is known.

    ``commission_per_lot`` is account currency per round turn.  ``slippage_points``
    is per side, so a round turn pays it twice — stated here once so the EV engine
    does not have to guess what a caller meant.
    """

    commission_per_lot: Decimal = Decimal("0")
    commission_state: str = ASSUMED_ZERO
    slippage_points: Decimal = Decimal("0")
    slippage_state: str = ESTIMATE
    swap_state: str = UNKNOWN

    def validate(self) -> tuple[str, ...]:
        problems: list[str] = []
        if self.commission_per_lot < 0:
            problems.append("commission_negative")
        if self.slippage_points < 0:
            problems.append("slippage_negative")
        for name, state in (
            ("commission_state", self.commission_state),
            ("slippage_state", self.slippage_state),
        ):
            if state not in {OBSERVED, ESTIMATE, ASSUMED_ZERO, UNKNOWN}:
                problems.append(f"{name}_unrecognised")
        return tuple(problems)

    @property
    def commission_known(self) -> bool:
        """Assumed-zero is *declared* zero; unknown is not a number at all."""

        return self.commission_state in {OBSERVED, ESTIMATE, ASSUMED_ZERO}

    def audit_view(self) -> dict[str, Any]:
        return {
            "commission_per_lot": str(self.commission_per_lot),
            "commission_state": self.commission_state,
            "slippage_points": str(self.slippage_points),
            "slippage_state": self.slippage_state,
            "swap_state": self.swap_state,
        }


def default_cost_terms(slippage_points: str = "1") -> CostTerms:
    """A declared, conservative default: zero commission *assumed*, slippage estimated.

    Zero commission is written as ``ASSUMED_ZERO`` rather than ``OBSERVED``, so a
    report can say the number is an assumption.  Slippage defaults to one point per
    side: a positive number, because a market order that costs nothing to cross is
    not a thing.
    """

    return CostTerms(
        commission_per_lot=Decimal("0"),
        commission_state=ASSUMED_ZERO,
        slippage_points=Decimal(slippage_points),
        slippage_state=ESTIMATE,
    )


@dataclass(frozen=True)
class ScalperConfig:
    """Everything the desk needs, and every bound it must respect."""

    instruments: tuple[InstrumentSpec, ...]
    cost: CostTerms = field(default_factory=default_cost_terms)
    mode: str = PAPER

    # -- tick core ------------------------------------------------------
    max_quote_age_ms: int = 1500
    max_spread_points: Decimal = Decimal("30")
    spread_expansion_multiple: Decimal = Decimal("2.5")
    gap_threshold_ms: int = 5000
    clock_reset_threshold_ms: int = 60_000
    resync_ticks: int = 5
    tick_window: int = 240
    min_window_ticks: int = 40
    duplicate_window: int = 2048

    # -- decision -------------------------------------------------------
    cooldown_ms: int = 5_000
    decision_latency_budget_ms: int = 250
    min_edge_points: Decimal = Decimal("0.6")
    uncertainty_z: Decimal = Decimal("1.0")

    # -- position management --------------------------------------------
    max_open_positions: int = 2
    max_hold_ms: int = 900_000
    break_even_trigger_points: Decimal = Decimal("8")
    trail_after_points: Decimal = Decimal("10")
    trail_distance_points: Decimal = Decimal("6")

    # -- sizing ---------------------------------------------------------
    risk_fraction: Decimal = Decimal("0.0025")
    max_loss_per_trade_account: Decimal = Decimal("50")
    max_aggregate_risk_fraction: Decimal = Decimal("0.01")
    max_leverage: Decimal = Decimal("5")
    #: Ceiling for one currency's notional, as a share of the *leverage allowance*
    #: (equity x max_leverage), not of equity.  Equity-sized caps would refuse the
    #: first ordinary lot instead of constraining concentration.
    max_currency_exposure_fraction: Decimal = Decimal("0.60")
    max_correlated_positions: int = 2

    # -- guardian -------------------------------------------------------
    max_daily_loss_fraction: Decimal = Decimal("0.02")
    max_drawdown_fraction: Decimal = Decimal("0.05")
    max_loss_streak: int = 4
    max_spread_points_hard: Decimal = Decimal("45")
    max_slippage_points: Decimal = Decimal("6")
    max_fill_latency_ms: int = 750

    # -- desk -----------------------------------------------------------
    max_decisions_per_minute: int = 60
    state_version: int = 1

    # -- validation -----------------------------------------------------

    def validate(self) -> tuple[str, ...]:
        problems: list[str] = []
        if self.mode != PAPER:
            problems.append(CONFIG_NOT_PAPER)
        if not self.instruments:
            problems.append(CONFIG_NO_INSTRUMENTS)
        seen: set[str] = set()
        for spec in self.instruments:
            if spec.symbol in seen:
                problems.append(f"duplicate_instrument:{spec.symbol}")
            seen.add(spec.symbol)
            for problem in spec.validate():
                problems.append(f"{spec.symbol}:{problem}")
        for problem in self.cost.validate():
            problems.append(problem)
        if not self.cost.commission_known:
            problems.append(CONFIG_UNKNOWN_COST)
        if self.max_quote_age_ms <= 0:
            problems.append("max_quote_age_not_positive")
        if self.max_spread_points <= 0:
            problems.append("max_spread_not_positive")
        if self.max_spread_points_hard < self.max_spread_points:
            problems.append("hard_spread_below_soft_spread")
        if self.tick_window < self.min_window_ticks:
            problems.append("tick_window_below_min_window_ticks")
        if self.min_window_ticks < 10:
            problems.append("min_window_ticks_too_short")
        if not (Decimal(0) < self.risk_fraction <= Decimal("0.02")):
            problems.append("risk_fraction_outside_conservative_range")
        if not (Decimal(0) < self.max_aggregate_risk_fraction <= Decimal("0.05")):
            problems.append("aggregate_risk_outside_conservative_range")
        if not (Decimal(0) < self.max_daily_loss_fraction <= Decimal("0.10")):
            problems.append("daily_loss_limit_outside_conservative_range")
        if not (Decimal(0) < self.max_drawdown_fraction <= Decimal("0.20")):
            problems.append("drawdown_limit_outside_conservative_range")
        if not (Decimal(0) < self.uncertainty_z <= Decimal("3")):
            problems.append("uncertainty_z_outside_range")
        if self.max_hold_ms <= 0:
            problems.append("max_hold_not_positive")
        if self.max_open_positions < 1:
            problems.append("max_open_positions_not_positive")
        return tuple(problems)

    @property
    def instruments_by_symbol(self) -> dict[str, InstrumentSpec]:
        return {spec.symbol: spec for spec in self.instruments}

    def spec_for(self, symbol: str) -> InstrumentSpec | None:
        return self.instruments_by_symbol.get(symbol)

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(spec.symbol for spec in self.instruments)

    @property
    def ready(self) -> bool:
        return not self.validate()

    def with_mode(self, mode: str) -> "ScalperConfig":
        """Only useful for a test proving the refusal: anything but PAPER raises."""

        return replace(self, mode=mode)

    def audit_view(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "symbols": list(self.symbols),
            "instruments": [spec.audit_view() for spec in self.instruments],
            "cost": self.cost.audit_view(),
            "max_quote_age_ms": self.max_quote_age_ms,
            "max_spread_points": str(self.max_spread_points),
            "min_edge_points": str(self.min_edge_points),
            "risk_fraction": str(self.risk_fraction),
            "max_aggregate_risk_fraction": str(self.max_aggregate_risk_fraction),
            "max_daily_loss_fraction": str(self.max_daily_loss_fraction),
            "max_drawdown_fraction": str(self.max_drawdown_fraction),
            "max_open_positions": self.max_open_positions,
        }


def build_config(
    *,
    instruments: Sequence[InstrumentSpec],
    cost: CostTerms | None = None,
    **overrides: Any,
) -> ScalperConfig:
    """Construct a config and refuse it immediately if it is not usable.

    Refusing at construction rather than at first tick means a misconfigured desk
    cannot start, look healthy and then refuse every trade for an unstated reason.
    """

    # Overrides arrive from CLI flags, fixture helpers and tests as strings.  A
    # Decimal field holding a str is not a configuration error the dataclass can see,
    # so it is coerced here; an unknown or unparseable field is refused by name rather
    # than silently ignored.
    known = {item.name: item for item in dataclasses.fields(ScalperConfig)}
    coerced: dict[str, Any] = {}
    for key, value in overrides.items():
        item = known.get(key)
        if item is None:
            raise ScalperConfigError("unknown_config_field", key)
        annotation = item.type if isinstance(item.type, str) else getattr(item.type, "__name__", "")
        if annotation == "Decimal" and not isinstance(value, Decimal):
            parsed = _dec(value)
            if parsed is None:
                raise ScalperConfigError("config_value_not_a_number", f"{key}={value!r}")
            coerced[key] = parsed
        elif annotation == "int" and not isinstance(value, int):
            try:
                coerced[key] = int(value)
            except (TypeError, ValueError):
                raise ScalperConfigError("config_value_not_an_integer", f"{key}={value!r}") from None
        else:
            coerced[key] = value

    config = ScalperConfig(
        instruments=tuple(instruments),
        cost=cost or default_cost_terms(),
        **coerced,
    )
    problems = config.validate()
    if problems:
        raise ScalperConfigError(
            problems[0], "; ".join(problems[1:]) if len(problems) > 1 else problems[0]
        )
    return config


def fixture_instrument(
    symbol: str = "EURUSD.pro",
    *,
    pair: str = "EUR/USD",
    point: str = "0.00001",
    contract_size: str = "100000",
    stops_level_points: str = "20",
    account_currency: str = "USD",
    quote_currency: str = "USD",
    conversion_rate: str | None = None,
) -> InstrumentSpec:
    """A declared instrument for tests and offline labs.

    It is labelled ``SAMPLE_DECLARED_TERMS`` so no report can present it as a
    broker's real specification.
    """

    return InstrumentSpec(
        symbol=symbol,
        pair=pair,
        point=Decimal(point),
        digits=len(point.split(".")[1]) if "." in point else 0,
        contract_size=Decimal(contract_size),
        volume_min=Decimal("0.01"),
        volume_step=Decimal("0.01"),
        volume_max=Decimal("50"),
        stops_level_points=Decimal(stops_level_points),
        margin_rate=Decimal("0.01"),
        quote_currency=quote_currency,
        account_currency=account_currency,
        conversion_rate=None if conversion_rate is None else Decimal(conversion_rate),
        source="SAMPLE_DECLARED_TERMS",
    )


def fixture_config(
    *, cost: CostTerms | None = None, instruments: Sequence[InstrumentSpec] | None = None, **overrides: Any
) -> ScalperConfig:
    return build_config(
        instruments=list(instruments) if instruments is not None else [fixture_instrument()],
        cost=cost or default_cost_terms("1"),
        **overrides,
    )


__all__ = [
    "ASSUMED_ZERO",
    "CONFIG_INCOMPLETE_TERMS",
    "CONFIG_NOT_PAPER",
    "CONFIG_NO_INSTRUMENTS",
    "CONFIG_UNKNOWN_COST",
    "CostTerms",
    "ESTIMATE",
    "InstrumentSpec",
    "LIVE",
    "OBSERVED",
    "PAPER",
    "ScalperConfig",
    "ScalperConfigError",
    "UNKNOWN",
    "build_config",
    "default_cost_terms",
    "fixture_config",
    "fixture_instrument",
]
