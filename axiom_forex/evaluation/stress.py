"""Deterministic forex stress and sensitivity suite for the research evaluation.

Every scenario is *explicitly configured* by the operator.  This module never
invents a "realistic" shock size and never treats a fixture value as a provider
fact: a scenario that lacks its parameters, its evidence class, or its evidence
reference is rejected before anything runs.

Semantics
---------
* A scenario reruns the **same** portfolio evaluation over the **same** pinned
  train/validation/test windows and the **same** walk-forward folds, with the
  same strategy parameters and the same research sizes.  Only the stressed input
  changes.
* A stress rerun is a sensitivity measurement, not new evidence.  It never claims
  an out-of-sample window, never qualifies a strategy, never activates anything,
  and never feeds a selection decision.
* Results stay labelled: each run records its evidence class
  (``HYPOTHETICAL_ASSUMPTION``, ``IMPORTED_DATA_BACKED`` or
  ``DOCUMENTED_PROVIDER_TERMS``) and its explicit parameter set.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.accounting.fx import ConversionRate, ConversionRateBook
from app.common.validation import canonical_pair
from app.data.models import QuoteSideBar

from axiom_forex.risk.aggregate import CorrelationMatrix

from .contracts import (
    EvaluationConfig,
    EvaluationError,
    PairSeries,
    ResearchEvaluationReport,
)
from .research import run_research_evaluation
from .splits import FoldPlan, SplitPlan

STRESS_FORMAT = "AXIOM_FOREX_STRESS_SCENARIOS"
STRESS_SCHEMA_VERSION = "1.0.0"
STRESS_MODE = "HYPOTHETICAL_STRESS"

EVIDENCE_HYPOTHETICAL = "HYPOTHETICAL_ASSUMPTION"
EVIDENCE_IMPORTED = "IMPORTED_DATA_BACKED"
EVIDENCE_DOCUMENTED = "DOCUMENTED_PROVIDER_TERMS"
EVIDENCE_CLASSES = frozenset(
    {EVIDENCE_HYPOTHETICAL, EVIDENCE_IMPORTED, EVIDENCE_DOCUMENTED}
)

KIND_SPREAD = "SPREAD_WIDENING"
KIND_SLIPPAGE = "SLIPPAGE_INCREASE"
KIND_COMMISSION = "COMMISSION_INCREASE"
KIND_SWAP = "ADVERSE_SWAP"
KIND_QUOTE_DELAY = "QUOTE_DELAY"
KIND_MISSING_QUOTES = "MISSING_QUOTES"
KIND_WEEKEND_GAP = "WEEKEND_GAP"
KIND_CONVERSION = "CONVERSION_SHOCK"
KIND_CORRELATION = "CORRELATION_STRESS"
SCENARIO_KINDS = frozenset(
    {
        KIND_SPREAD,
        KIND_SLIPPAGE,
        KIND_COMMISSION,
        KIND_SWAP,
        KIND_QUOTE_DELAY,
        KIND_MISSING_QUOTES,
        KIND_WEEKEND_GAP,
        KIND_CONVERSION,
        KIND_CORRELATION,
    }
)

#: Parameters each kind accepts.  Anything else is rejected, so a typo cannot
#: silently produce an unstressed run.
KIND_PARAMETERS: Mapping[str, frozenset[str]] = {
    KIND_SPREAD: frozenset({"extra_spread_pips", "spread_multiplier"}),
    KIND_SLIPPAGE: frozenset({"extra_slippage_pips", "slippage_multiplier"}),
    KIND_COMMISSION: frozenset({"extra_commission_rate", "commission_multiplier"}),
    KIND_SWAP: frozenset({"swap_rate_multiplier"}),
    KIND_QUOTE_DELAY: frozenset({"delay_seconds", "max_staleness_seconds", "pairs"}),
    KIND_MISSING_QUOTES: frozenset({"drop_every_nth", "pairs"}),
    KIND_WEEKEND_GAP: frozenset({"gap_start", "gap_end", "pairs"}),
    KIND_CONVERSION: frozenset({"rate_shocks"}),
    KIND_CORRELATION: frozenset({"correlation_overrides"}),
}

ZERO = Decimal(0)

#: Comparison fields shared by the portfolio and per-pair views.
COMPARISON_KEYS: tuple[str, ...] = (
    "trades",
    "win_ratio",
    "gross_pnl_account",
    "costs_account",
    "net_pnl_account",
    "turnover_account",
    "max_drawdown_account",
    "max_drawdown_fraction",
    "max_exposure_account",
    "max_margin_account",
    "min_coverage_ratio",
    "rejection_count",
)


class StressScenarioError(EvaluationError):
    """Raised when a scenario is missing, malformed, or unsupported."""


@dataclass(frozen=True)
class StressScenario:
    """One explicitly configured stress or sensitivity assumption."""

    scenario_id: str
    kind: str
    label: str
    description: str
    evidence_class: str
    evidence_reference: str
    parameters: Mapping[str, Any]
    version: str = "stress-scenario-v1"

    @property
    def is_hypothetical(self) -> bool:
        return self.evidence_class == EVIDENCE_HYPOTHETICAL

    def validate(self) -> tuple[str, ...]:
        errors: list[str] = []
        if not str(self.scenario_id).strip():
            errors.append("missing_scenario_id")
        if not str(self.label).strip():
            errors.append("missing_scenario_label")
        if self.kind not in SCENARIO_KINDS:
            return tuple(errors + [f"unknown_scenario_kind:{self.kind}"])
        if self.evidence_class not in EVIDENCE_CLASSES:
            errors.append(f"unknown_evidence_class:{self.evidence_class}")
        reference = str(self.evidence_reference or "").strip()
        if self.evidence_class == EVIDENCE_DOCUMENTED and not reference:
            errors.append("documented_terms_require_evidence_reference")
        if self.evidence_class == EVIDENCE_IMPORTED and not reference:
            errors.append("imported_data_requires_evidence_reference")
        if self.is_hypothetical and not str(self.description).strip():
            errors.append("hypothetical_scenario_requires_description")
        errors.extend(_validate_parameters(self.kind, self.parameters))
        return tuple(errors)

    def audit_view(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "kind": self.kind,
            "label": self.label,
            "description": self.description,
            "evidence_class": self.evidence_class,
            "evidence_reference": self.evidence_reference,
            "hypothetical": self.is_hypothetical,
            "parameters": _json_safe(dict(self.parameters)),
            "version": self.version,
        }


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return value


def _decimal(
    parameters: Mapping[str, Any], key: str
) -> tuple[Decimal | None, str | None]:
    if key not in parameters:
        return None, None
    try:
        value = Decimal(str(parameters[key]))
    except (InvalidOperation, TypeError, ValueError):
        return None, f"invalid_parameter:{key}"
    if not value.is_finite():
        return None, f"invalid_parameter:{key}"
    return value, None


def _integer(
    parameters: Mapping[str, Any], key: str
) -> tuple[int | None, str | None]:
    if key not in parameters:
        return None, None
    value = parameters[key]
    if isinstance(value, bool) or not isinstance(value, int):
        return None, f"invalid_parameter:{key}"
    return value, None


def _instant(
    parameters: Mapping[str, Any], key: str
) -> tuple[datetime | None, str | None]:
    if key not in parameters:
        return None, None
    text = str(parameters[key]).strip()
    if not text:
        return None, f"invalid_parameter:{key}"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None, f"invalid_parameter:{key}"
    if parsed.tzinfo is None:
        return None, f"invalid_parameter:{key}"
    return parsed.astimezone(timezone.utc), None


def _pairs(parameters: Mapping[str, Any]) -> tuple[tuple[str, ...], str | None]:
    raw = parameters.get("pairs")
    if raw is None:
        return (), None
    if isinstance(raw, str):
        return (raw.upper(),), None
    if not isinstance(raw, (list, tuple)):
        return (), "invalid_parameter:pairs"
    pairs = tuple(str(item).strip().upper() for item in raw if str(item).strip())
    return pairs, None


def _validate_parameters(kind: str, parameters: Mapping[str, Any]) -> list[str]:
    """Explicit, per-kind parameter validation.  Nothing is defaulted."""

    errors: list[str] = []
    if not isinstance(parameters, Mapping):
        return ["missing_scenario_parameters"]
    allowed = KIND_PARAMETERS[kind]
    unknown = sorted(set(parameters) - set(allowed))
    if unknown:
        errors.append("unknown_parameter:" + ",".join(unknown))
    if kind in {KIND_SPREAD, KIND_SLIPPAGE, KIND_COMMISSION}:
        absolute, relative = {
            KIND_SPREAD: ("extra_spread_pips", "spread_multiplier"),
            KIND_SLIPPAGE: ("extra_slippage_pips", "slippage_multiplier"),
            KIND_COMMISSION: ("extra_commission_rate", "commission_multiplier"),
        }[kind]
        first, first_error = _decimal(parameters, absolute)
        second, second_error = _decimal(parameters, relative)
        errors.extend(item for item in (first_error, second_error) if item)
        if first is None and second is None:
            errors.append(f"missing_parameter:{absolute}|{relative}")
        if first is not None and first < 0:
            errors.append(f"invalid_parameter:{absolute}")
        if second is not None and second <= 0:
            errors.append(f"invalid_parameter:{relative}")
        if first is not None and second is not None:
            errors.append("conflicting_parameters")
        if first is not None and first == 0:
            errors.append("no_effect_parameter")
        if second is not None and second == 1:
            errors.append("no_effect_parameter")
    elif kind == KIND_SWAP:
        multiplier, error = _decimal(parameters, "swap_rate_multiplier")
        if error:
            errors.append(error)
        if multiplier is None:
            errors.append("missing_parameter:swap_rate_multiplier")
        elif multiplier <= 0:
            errors.append("invalid_parameter:swap_rate_multiplier")
        elif multiplier == 1:
            errors.append("no_effect_parameter")
    elif kind == KIND_QUOTE_DELAY:
        delay, delay_error = _integer(parameters, "delay_seconds")
        stale, stale_error = _integer(parameters, "max_staleness_seconds")
        errors.extend(item for item in (delay_error, stale_error) if item)
        if delay is None:
            errors.append("missing_parameter:delay_seconds")
        elif delay <= 0:
            errors.append("invalid_parameter:delay_seconds")
        if stale is None:
            errors.append("missing_parameter:max_staleness_seconds")
        elif stale <= 0:
            errors.append("invalid_parameter:max_staleness_seconds")
        _, pairs_error = _pairs(parameters)
        if pairs_error:
            errors.append(pairs_error)
        if not parameters.get("pairs"):
            errors.append("missing_parameter:pairs")
    elif kind == KIND_MISSING_QUOTES:
        every, every_error = _integer(parameters, "drop_every_nth")
        if every_error:
            errors.append(every_error)
        if every is None:
            errors.append("missing_parameter:drop_every_nth")
        elif every < 2:
            errors.append("invalid_parameter:drop_every_nth")
        _pairs_error = _pairs(parameters)[1]
        if _pairs_error:
            errors.append(_pairs_error)
    elif kind == KIND_WEEKEND_GAP:
        start, start_error = _instant(parameters, "gap_start")
        end, end_error = _instant(parameters, "gap_end")
        errors.extend(item for item in (start_error, end_error) if item)
        if start is None:
            errors.append("missing_parameter:gap_start")
        if end is None:
            errors.append("missing_parameter:gap_end")
        if start is not None and end is not None and end <= start:
            errors.append("invalid_gap_window")
        _pairs_error = _pairs(parameters)[1]
        if _pairs_error:
            errors.append(_pairs_error)
        if not parameters.get("pairs"):
            errors.append("missing_parameter:pairs")
    elif kind == KIND_CONVERSION:
        shocks = parameters.get("rate_shocks")
        if not isinstance(shocks, Mapping) or not shocks:
            errors.append("missing_parameter:rate_shocks")
        else:
            seen_legs: set[str] = set()
            for key, value in shocks.items():
                leg, leg_error = canonical_pair(key)
                if leg_error or leg is None:
                    # "AAA/BBB/CCC" or "/BBB" would otherwise reach the runner
                    # and surface as a bare ValueError instead of a refusal.
                    errors.append(f"invalid_rate_leg:{key}")
                    continue
                if leg in seen_legs:
                    # Two spellings of one leg with different multipliers make
                    # the applied shock depend on dict order.
                    errors.append(f"duplicate_rate_leg:{leg}")
                    continue
                seen_legs.add(leg)
                try:
                    multiplier = Decimal(str(value))
                except (InvalidOperation, TypeError, ValueError):
                    errors.append(f"invalid_parameter:rate_shocks.{key}")
                    continue
                if not multiplier.is_finite() or multiplier <= 0:
                    errors.append(f"invalid_parameter:rate_shocks.{key}")
                elif multiplier == 1:
                    errors.append(f"no_effect_parameter:rate_shocks.{key}")
    elif kind == KIND_CORRELATION:
        overrides = parameters.get("correlation_overrides")
        if not isinstance(overrides, Mapping) or not overrides:
            errors.append("missing_parameter:correlation_overrides")
        else:
            seen_pairs: set[str] = set()
            for key, value in overrides.items():
                pair, pair_error = canonical_pair(key)
                if pair_error or pair is None:
                    errors.append(f"invalid_correlation_pair:{key}")
                    continue
                if pair in seen_pairs:
                    errors.append(f"duplicate_correlation_pair:{pair}")
                    continue
                seen_pairs.add(pair)
                try:
                    correlation = Decimal(str(value))
                except (InvalidOperation, TypeError, ValueError):
                    errors.append(f"invalid_parameter:correlation_overrides.{key}")
                    continue
                if not correlation.is_finite() or not (
                    Decimal(-1) <= correlation <= Decimal(1)
                ):
                    errors.append(f"invalid_parameter:correlation_overrides.{key}")
    return errors


@dataclass(frozen=True)
class ScenarioApplication:
    """The stressed inputs produced by one scenario, plus what changed."""

    scenario: StressScenario
    config: EvaluationConfig
    series: Mapping[str, PairSeries]
    notes: tuple[str, ...] = ()
    derived_series: bool = False

    def audit_view(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario.audit_view(),
            "notes": list(self.notes),
            "derived_series": self.derived_series,
        }


def validate_scenarios(
    scenarios: Sequence[StressScenario],
) -> tuple[str, ...]:
    errors: list[str] = []
    if not scenarios:
        return ("no_scenarios_configured",)
    seen: set[str] = set()
    for scenario in scenarios:
        if scenario.scenario_id in seen:
            errors.append(f"duplicate_scenario_id:{scenario.scenario_id}")
        seen.add(scenario.scenario_id)
        errors.extend(
            f"{scenario.scenario_id}:{error}" for error in scenario.validate()
        )
    return tuple(errors)


def _widen_bars(
    series: PairSeries,
    *,
    extra_pips: Decimal | None,
    multiplier: Decimal | None,
) -> PairSeries:
    spec = series.spec
    pip = spec.pip_size_decimal()
    extra_price = (extra_pips * pip) / Decimal(2) if extra_pips is not None else None

    def widen(bar: Any) -> Any:
        bid_values: dict[str, Decimal] = {}
        ask_values: dict[str, Decimal] = {}
        for name in ("open", "high", "low", "close"):
            bid_price = Decimal(str(getattr(bar.bid, name)))
            ask_price = Decimal(str(getattr(bar.ask, name)))
            mid = (bid_price + ask_price) / Decimal(2)
            if multiplier is not None:
                bid_price = mid - (mid - bid_price) * multiplier
                ask_price = mid + (ask_price - mid) * multiplier
            if extra_price is not None:
                bid_price = bid_price - extra_price
                ask_price = ask_price + extra_price
            if bid_price <= 0 or ask_price <= 0:
                raise StressScenarioError("spread_shock_makes_price_non_positive")
            bid_values[name] = bid_price
            ask_values[name] = ask_price
        return replace(bar, bid=QuoteSideBar(**bid_values), ask=QuoteSideBar(**ask_values))

    return replace(series, bars=tuple(widen(bar) for bar in series.bars))


def _delay_bars(
    series: PairSeries, *, delay_seconds: int, max_staleness_seconds: int
) -> tuple[PairSeries, int, int]:
    kept: list[Any] = []
    dropped = 0
    stale = 0
    delay = timedelta(seconds=delay_seconds)
    for bar in series.bars:
        # A delayed feed means the confirming quote arrived *later* than the bar,
        # so the last usable quote inside the bar is older than recorded.
        shifted = bar.last_quote_at - delay
        if shifted < bar.timestamp:
            shifted = bar.timestamp
        staleness = (bar.end_at - shifted).total_seconds()
        if staleness > max_staleness_seconds:
            stale += 1
            dropped += 1
            continue
        kept.append(replace(bar, last_quote_at=shifted))
    return replace(series, bars=tuple(kept)), dropped, stale


def _drop_every_nth(
    series: PairSeries, *, every: int
) -> tuple[PairSeries, int]:
    kept = tuple(
        bar for index, bar in enumerate(series.bars) if (index + 1) % every != 0
    )
    return replace(series, bars=kept), len(series.bars) - len(kept)


def _drop_window(
    series: PairSeries, *, start: datetime, end: datetime
) -> tuple[PairSeries, int]:
    kept = tuple(
        bar for bar in series.bars if not (start <= bar.timestamp < end)
    )
    return replace(series, bars=kept), len(series.bars) - len(kept)


def _scaled_costs(terms: Any, scenario: StressScenario) -> Any:
    parameters = scenario.parameters
    commission_multiplier = _decimal(parameters, "commission_multiplier")[0]
    extra_commission = _decimal(parameters, "extra_commission_rate")[0]
    slippage_multiplier = _decimal(parameters, "slippage_multiplier")[0]
    extra_slippage = _decimal(parameters, "extra_slippage_pips")[0]
    swap_multiplier = _decimal(parameters, "swap_rate_multiplier")[0]

    commission = terms.decimal("commission_rate")
    slippage = terms.decimal("slippage_pips")
    financing = terms.decimal("financing_rate_daily")
    if commission_multiplier is not None:
        commission = commission * commission_multiplier
    if extra_commission is not None:
        commission = commission + extra_commission
    if slippage_multiplier is not None:
        slippage = slippage * slippage_multiplier
    if extra_slippage is not None:
        slippage = slippage + extra_slippage
    if swap_multiplier is not None:
        financing = financing * swap_multiplier
    return replace(
        terms,
        commission_rate=commission,
        slippage_pips=slippage,
        financing_rate_daily=financing,
        model_id=f"{terms.model_id}:stress:{scenario.scenario_id}",
        version=f"{terms.version}+stress",
        source=f"{terms.source}:STRESS_ASSUMPTION",
    )


def _scaled_swap_cards(
    cards: Mapping[str, Any], multiplier: Decimal
) -> dict[str, Any]:
    return {
        pair: replace(
            card,
            long_rate=card.long_rate * multiplier,
            short_rate=card.short_rate * multiplier,
            source=f"{card.source}:STRESS_ASSUMPTION",
            version=f"{card.version}+stress",
        )
        for pair, card in cards.items()
    }


def _shocked_conversion_rates(
    book: ConversionRateBook, shocks: Mapping[str, Any], scenario_id: str
) -> ConversionRateBook:
    rates: dict[tuple[str, str], ConversionRate] = {}
    applied: set[tuple[str, str]] = set()
    parsed_shocks: dict[tuple[str, str], Any] = {}
    for raw_leg, raw_value in shocks.items():
        canonical, leg_error = canonical_pair(raw_leg)
        if leg_error or canonical is None:
            raise StressScenarioError(
                f"invalid_conversion_shock_leg:{raw_leg}"
            )
        from_currency, to_currency = canonical.split("/")
        parsed_shocks[(from_currency, to_currency)] = raw_value
    for key, rate in book.rates.items():
        leg = (str(key[0]).upper(), str(key[1]).upper())
        multiplier = None
        for shock_leg, raw_value in parsed_shocks.items():
            if shock_leg == leg:
                multiplier = Decimal(str(raw_value))
        if multiplier is None:
            rates[leg] = rate
            continue
        applied.add(leg)
        # A rate book requires every rate source to equal the book source, so
        # the hypothetical marker lives in the rate id and the scenario record.
        rates[leg] = replace(
            rate,
            rate=rate.rate * multiplier,
            rate_id=f"{rate.rate_id or 'rate'}:stress:{scenario_id}",
        )
    missing = set(parsed_shocks) - applied
    if missing:
        raise StressScenarioError(
            "conversion_shock_leg_not_in_rate_book:"
            + ",".join(f"{a}/{b}" for a, b in sorted(missing))
        )
    # The book source identifies the rate *provider* and is matched against the
    # account profile, so it stays unchanged.  The hypothetical nature of the
    # shock is carried by the per-rate source/id, the scenario record, and the
    # report purpose label.
    return ConversionRateBook(
        source=book.source,
        freshness=book.freshness,
        rates=rates,
    )


def _stressed_correlations(
    matrix: CorrelationMatrix,
    overrides: Mapping[str, Any],
    scenario_id: str,
) -> CorrelationMatrix:
    values = dict(matrix.values)
    for raw_pair, raw_value in overrides.items():
        canonical, pair_error = canonical_pair(raw_pair)
        if pair_error or canonical is None:
            raise StressScenarioError(
                f"invalid_correlation_pair:{raw_pair}"
            )
        pair = canonical
        correlation = Decimal(str(raw_value))
        values[(pair, pair)] = Decimal(1)
        for other in sorted(matrix.pairs):
            if other == pair:
                continue
            values[(pair, other)] = correlation
            values[(other, pair)] = correlation
    return CorrelationMatrix(
        pairs=matrix.pairs,
        values=values,
        source=f"{matrix.source}:STRESS_ASSUMPTION",
        version=f"{matrix.version}+stress:{scenario_id}",
    )


def apply_scenario(
    scenario: StressScenario,
    *,
    series: Mapping[str, PairSeries],
    config: EvaluationConfig,
) -> ScenarioApplication:
    """Derive stressed inputs.  Only declared parameters are applied."""

    errors = scenario.validate()
    if errors:
        raise StressScenarioError("invalid_scenario:" + ",".join(errors))

    parameters = scenario.parameters
    notes: list[str] = [f"kind:{scenario.kind}", f"evidence:{scenario.evidence_class}"]
    stressed_config = config
    stressed_series: Mapping[str, PairSeries] = series
    derived = False

    if scenario.kind in {KIND_SPREAD, KIND_SLIPPAGE, KIND_COMMISSION, KIND_SWAP}:
        cost_terms = _scaled_costs(config.cost_terms, scenario)
        stressed_config = replace(stressed_config, cost_terms=cost_terms)
        notes.append(
            "cost_terms:"
            + f"commission={cost_terms.commission_rate},"
            + f"slippage_pips={cost_terms.slippage_pips},"
            + f"financing_rate_daily={cost_terms.financing_rate_daily}"
        )
        multiplier = _decimal(parameters, "swap_rate_multiplier")[0]
        if multiplier is not None:
            stressed_config = replace(
                stressed_config,
                swap_cards=_scaled_swap_cards(config.swap_cards, multiplier),
            )
            notes.append(f"swap_rate_multiplier:{multiplier}")
        if scenario.kind == KIND_SPREAD:
            extra_pips = _decimal(parameters, "extra_spread_pips")[0]
            spread_multiplier = _decimal(parameters, "spread_multiplier")[0]
            stressed_series = {
                pair: _widen_bars(
                    item, extra_pips=extra_pips, multiplier=spread_multiplier
                )
                for pair, item in series.items()
            }
            derived = True
            notes.append(
                "bar_spread_widened:"
                + (
                    f"extra_pips={extra_pips}"
                    if extra_pips is not None
                    else f"multiplier={spread_multiplier}"
                )
            )

    elif scenario.kind == KIND_CONVERSION:
        stressed_config = replace(
            stressed_config,
            conversion_rates=_shocked_conversion_rates(
                config.conversion_rates,
                parameters["rate_shocks"],
                scenario.scenario_id,
            ),
        )
        notes.append("conversion_rate_shock:" + ",".join(sorted(parameters["rate_shocks"])))

    elif scenario.kind == KIND_CORRELATION:
        stressed_config = replace(
            stressed_config,
            correlation_matrix=_stressed_correlations(
                config.correlation_matrix,
                parameters["correlation_overrides"],
                scenario.scenario_id,
            ),
        )
        notes.append(
            "correlation_override:" + ",".join(sorted(parameters["correlation_overrides"]))
        )

    elif scenario.kind in {KIND_QUOTE_DELAY, KIND_MISSING_QUOTES, KIND_WEEKEND_GAP}:
        selected, _ = _pairs(parameters)
        if not selected:
            # Data-removal scenarios always declare which pairs they apply to.
            raise StressScenarioError("missing_parameter:pairs")
        unknown = sorted(set(selected) - set(series))
        if unknown:
            raise StressScenarioError(
                "scenario_pair_not_evaluated:" + ",".join(unknown)
            )
        updated: dict[str, PairSeries] = dict(series)
        for pair in selected:
            item = updated[pair]
            if scenario.kind == KIND_QUOTE_DELAY:
                item, dropped, stale = _delay_bars(
                    item,
                    delay_seconds=int(parameters["delay_seconds"]),
                    max_staleness_seconds=int(parameters["max_staleness_seconds"]),
                )
                notes.append(f"quote_delay:{pair}:dropped={dropped}:stale={stale}")
            elif scenario.kind == KIND_MISSING_QUOTES:
                item, dropped = _drop_every_nth(
                    item, every=int(parameters["drop_every_nth"])
                )
                notes.append(f"missing_quotes:{pair}:dropped={dropped}")
            else:
                start = _instant(parameters, "gap_start")[0]
                end = _instant(parameters, "gap_end")[0]
                item, dropped = _drop_window(item, start=start, end=end)
                notes.append(f"gap_window:{pair}:dropped={dropped}")
            provenance = dict(item.provenance)
            provenance.update(
                {
                    "stress_scenario_id": scenario.scenario_id,
                    "stress_derivation": scenario.kind,
                    "derived_from_dataset_sha256": provenance.get(
                        "dataset_sha256", ""
                    )
                    or str(getattr(item.dataset_manifest, "sha256", "")),
                }
            )
            updated[pair] = replace(item, provenance=provenance)
        stressed_series = updated
        derived = True

    return ScenarioApplication(
        scenario=scenario,
        config=stressed_config,
        series=stressed_series,
        notes=tuple(notes),
        derived_series=derived,
    )


# -- comparison ------------------------------------------------------------


def _max_margin(report: ResearchEvaluationReport) -> Decimal:
    values = [
        point.margin_account
        for phase in report.phases.values()
        for point in phase.equity_curve
    ]
    return max(values, default=ZERO)


def _rejection_counts(report: ResearchEvaluationReport) -> dict[str, int]:
    counts: dict[str, int] = {}
    for phase in report.phases.values():
        for rejection in phase.rejections:
            code = str(rejection.get("code", "UNKNOWN"))
            counts[code] = counts.get(code, 0) + 1
    return dict(sorted(counts.items()))


def _min_coverage(report: ResearchEvaluationReport) -> Decimal:
    values = [coverage.coverage_ratio for coverage in report.alignment.pairs]
    return min(values, default=ZERO)


def _portfolio_row(report: ResearchEvaluationReport) -> dict[str, Any]:
    metrics = report.portfolio_metrics
    return {
        "trades": metrics.trades,
        "win_ratio": metrics.win_ratio,
        "gross_pnl_account": metrics.gross_pnl_account,
        "costs_account": metrics.costs_account,
        "net_pnl_account": metrics.net_pnl_account,
        "turnover_account": metrics.turnover_account,
        "max_drawdown_account": metrics.max_drawdown_account,
        "max_drawdown_fraction": metrics.max_drawdown_fraction,
        "max_exposure_account": metrics.exposure_account,
        "max_margin_account": _max_margin(report),
        "min_coverage_ratio": _min_coverage(report),
        "rejection_count": sum(_rejection_counts(report).values()),
    }


def _pair_row(report: ResearchEvaluationReport, pair: str) -> dict[str, Any]:
    metrics = next(
        (metric for metric in report.pair_metrics if metric.key == pair), None
    )
    phase_rows = [
        phase.pair_summary.get(pair, {}) for phase in report.phases.values()
    ]
    max_exposure = max(
        (row.get("max_exposure_account", ZERO) for row in phase_rows), default=ZERO
    )
    min_coverage = min(
        (row.get("coverage_ratio", ZERO) for row in phase_rows), default=ZERO
    )
    rejections = sum(
        1
        for phase in report.phases.values()
        for rejection in phase.rejections
        if rejection.get("pair") == pair
    )
    return {
        "trades": metrics.trades if metrics else 0,
        "win_ratio": metrics.win_ratio if metrics else ZERO,
        "gross_pnl_account": metrics.gross_pnl_account if metrics else ZERO,
        "costs_account": metrics.costs_account if metrics else ZERO,
        "net_pnl_account": metrics.net_pnl_account if metrics else ZERO,
        "turnover_account": metrics.turnover_account if metrics else ZERO,
        "max_drawdown_account": metrics.max_drawdown_account if metrics else ZERO,
        "max_drawdown_fraction": metrics.max_drawdown_fraction if metrics else ZERO,
        "max_exposure_account": max_exposure,
        "max_margin_account": max(
            (
                point.margin_account
                for phase in report.phases.values()
                for point in phase.equity_curve
            ),
            default=ZERO,
        ),
        "min_coverage_ratio": min_coverage,
        "rejection_count": rejections,
    }


def _delta(baseline: Any, stressed: Any) -> Any:
    if isinstance(baseline, Decimal) and isinstance(stressed, Decimal):
        return stressed - baseline
    if isinstance(baseline, int) and isinstance(stressed, int):
        return stressed - baseline
    if isinstance(baseline, Decimal) and isinstance(stressed, int):
        return Decimal(stressed) - baseline
    if isinstance(baseline, int) and isinstance(stressed, Decimal):
        return stressed - Decimal(baseline)
    return None


@dataclass(frozen=True)
class StressComparison:
    scope: str
    key: str
    baseline: Mapping[str, Any]
    stressed: Mapping[str, Any]
    deltas: Mapping[str, Any]
    status: str

    def audit_view(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "key": self.key,
            "status": self.status,
            "baseline": _json_safe(dict(self.baseline)),
            "stressed": _json_safe(dict(self.stressed)),
            "deltas": _json_safe(dict(self.deltas)),
        }


def _comparison(
    *,
    scope: str,
    key: str,
    baseline: Mapping[str, Any],
    stressed: Mapping[str, Any],
    status: str,
) -> StressComparison:
    deltas = {
        name: _delta(baseline.get(name, ZERO), stressed.get(name, ZERO))
        for name in COMPARISON_KEYS
    }
    return StressComparison(
        scope=scope,
        key=key,
        baseline=dict(baseline),
        stressed=dict(stressed),
        deltas={name: value for name, value in deltas.items() if value is not None},
        status=status,
    )


@dataclass(frozen=True)
class StressRun:
    scenario: StressScenario
    status: str
    report: ResearchEvaluationReport
    comparisons: tuple[StressComparison, ...]
    notes: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()
    rejections: Mapping[str, int] = field(default_factory=dict)

    def audit_view(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario.audit_view(),
            "status": self.status,
            "reason": self.report.reason,
            "blocked_reasons": list(self.blocked_reasons),
            "rejections": dict(self.rejections),
            "notes": list(self.notes),
            "comparisons": [item.audit_view() for item in self.comparisons],
            "report": {
                "evaluation_id": self.report.evaluation_id,
                "purpose": self.report.purpose,
                "selection_allowed": self.report.selection_allowed,
                "data_class": self.report.evaluation_data_class,
                "qualification": self.report.qualification.audit_view(),
                "eligibility": self.report.eligibility.audit_view(),
                "phases": {
                    label: phase.audit_view()
                    for label, phase in self.report.phases.items()
                },
                "provenance": [dict(item) for item in self.report.provenance],
                "execution_isolation": self.report.audit_view()["execution_isolation"],
            },
        }


@dataclass(frozen=True)
class StressSuiteReport:
    baseline: ResearchEvaluationReport
    scenarios: tuple[StressRun, ...]
    status: str
    passed: bool
    reasons: tuple[str, ...]
    generated_at: datetime
    mode: str = STRESS_MODE
    format: str = STRESS_FORMAT
    schema_version: str = STRESS_SCHEMA_VERSION
    note: str = (
        "Stress and sensitivity results are hypothetical or documented-input "
        "reconstructions of the baseline evaluation. They never qualify or "
        "activate a strategy, never claim an out-of-sample window, create no "
        "orders, and are not profitability or live-readiness claims."
    )

    @property
    def hypothetical_scenarios(self) -> tuple[str, ...]:
        return tuple(
            run.scenario.scenario_id
            for run in self.scenarios
            if run.scenario.is_hypothetical
        )

    def audit_view(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "schema_version": self.schema_version,
            "mode": self.mode,
            "status": self.status,
            "passed": self.passed,
            "reasons": list(self.reasons),
            "generated_at": self.generated_at,
            "baseline": {
                "evaluation_id": self.baseline.evaluation_id,
                "status": self.baseline.status,
                "purpose": self.baseline.purpose,
                "data_class": self.baseline.evaluation_data_class,
            },
            "hypothetical_scenarios": list(self.hypothetical_scenarios),
            "scenarios": [run.audit_view() for run in self.scenarios],
            "execution_isolation": {
                "creates_orders": False,
                "touches_paper_order_stream": False,
                "external_execution_enabled": False,
                "paper_broker_used": False,
                "qualifies_strategy": False,
                "activates_strategy": False,
                "claims_out_of_sample_window": False,
            },
            "note": self.note,
        }


def _plan_from_report(report: ResearchEvaluationReport) -> SplitPlan | None:
    if not report.split:
        return None
    return SplitPlan(timeline=(), phases=dict(report.split))


def _folds_from_report(
    report: ResearchEvaluationReport,
) -> tuple[FoldPlan, ...]:
    return tuple(
        FoldPlan(
            fold=fold.fold,
            train=fold.train.window,
            validation=fold.validation.window,
            test=fold.test.window,
        )
        for fold in report.folds
    )


def run_stress_suite(
    *,
    series: Mapping[str, PairSeries],
    config: EvaluationConfig,
    calendar: Any,
    scenarios: Sequence[StressScenario],
    baseline: ResearchEvaluationReport | None = None,
    now: datetime | None = None,
) -> StressSuiteReport:
    """Rerun the pinned baseline evaluation once per explicit scenario."""

    generated_at = now or datetime.now(timezone.utc)
    errors = validate_scenarios(scenarios)
    if errors:
        raise StressScenarioError("invalid_scenarios:" + ",".join(errors))

    baseline_report = baseline or run_research_evaluation(
        series=series,
        config=config,
        calendar=calendar,
        now=generated_at,
        purpose="BASELINE",
    )
    if baseline_report.status != "PASS":
        return StressSuiteReport(
            baseline=baseline_report,
            scenarios=(),
            status="BLOCKED",
            passed=False,
            reasons=("baseline_not_evaluable:" + baseline_report.reason,),
            generated_at=generated_at,
        )

    plan = _plan_from_report(baseline_report)
    folds = _folds_from_report(baseline_report)
    runs: list[StressRun] = []
    for scenario in scenarios:
        application = apply_scenario(scenario, series=series, config=config)
        report = run_research_evaluation(
            series=application.series,
            config=application.config,
            calendar=calendar,
            evaluation_id=f"{baseline_report.evaluation_id}:{scenario.scenario_id}",
            now=generated_at,
            fixed_plan=plan,
            fixed_folds=folds,
            selection=False,
            purpose=f"STRESS:{scenario.scenario_id}",
        )
        pairs = sorted({item["pair"] for item in report.datasets}) or sorted(series)
        comparisons = [
            _comparison(
                scope="PORTFOLIO",
                key="PORTFOLIO",
                baseline=_portfolio_row(baseline_report),
                stressed=_portfolio_row(report),
                status=report.status,
            )
        ]
        comparisons.extend(
            _comparison(
                scope="PAIR",
                key=pair,
                baseline=_pair_row(baseline_report, pair),
                stressed=_pair_row(report, pair),
                status=report.status,
            )
            for pair in pairs
        )
        blocked = (
            tuple(
                sorted(
                    set(report.eligibility.reasons)
                    | {report.reason}
                )
            )
            if report.status != "PASS"
            else ()
        )
        runs.append(
            StressRun(
                scenario=scenario,
                status=report.status,
                report=report,
                comparisons=tuple(comparisons),
                notes=application.notes,
                blocked_reasons=tuple(item for item in blocked if item),
                rejections=_rejection_counts(report),
            )
        )

    evaluated = sum(1 for run in runs if run.status == "PASS")
    status = "PASS" if evaluated else "BLOCKED"
    reasons: list[str] = []
    if not evaluated:
        reasons.append("all_scenarios_fail_closed")
    reasons.extend(
        f"{run.scenario.scenario_id}:{reason}"
        for run in runs
        for reason in run.blocked_reasons
    )
    return StressSuiteReport(
        baseline=baseline_report,
        scenarios=tuple(runs),
        status=status,
        passed=status == "PASS",
        reasons=tuple(reasons),
        generated_at=generated_at,
    )


# -- loading explicit scenarios -------------------------------------------


def load_stress_scenarios(source: Mapping[str, Any] | str | Path) -> tuple[StressScenario, ...]:
    """Load scenario definitions; nothing is defaulted or invented."""

    payload: Any = source
    if isinstance(source, (str, Path)):
        text = Path(source).read_text(encoding="utf-8")
        payload = json.loads(text)
    if not isinstance(payload, Mapping):
        raise StressScenarioError("scenario_document_must_be_an_object")
    if payload.get("format") != STRESS_FORMAT:
        raise StressScenarioError("unknown_scenario_document_format")
    if str(payload.get("schema_version")) != STRESS_SCHEMA_VERSION:
        raise StressScenarioError("unsupported_scenario_schema_version")
    raw = payload.get("scenarios")
    if not isinstance(raw, list) or not raw:
        raise StressScenarioError("no_scenarios_configured")
    scenarios: list[StressScenario] = []
    allowed = {
        "scenario_id",
        "kind",
        "label",
        "description",
        "evidence_class",
        "evidence_reference",
        "parameters",
        "version",
    }
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise StressScenarioError(f"scenario_{index}_must_be_an_object")
        unknown = sorted(set(item) - allowed)
        if unknown:
            raise StressScenarioError(
                f"scenario_{index}_unknown_fields:" + ",".join(unknown)
            )
        parameters = item.get("parameters")
        if not isinstance(parameters, Mapping):
            raise StressScenarioError(f"scenario_{index}_missing_parameters")
        scenario = StressScenario(
            scenario_id=str(item.get("scenario_id", "")),
            kind=str(item.get("kind", "")),
            label=str(item.get("label", "")),
            description=str(item.get("description", "")),
            evidence_class=str(item.get("evidence_class", "")),
            evidence_reference=str(item.get("evidence_reference", "")),
            parameters=dict(parameters),
            version=str(item.get("version", "stress-scenario-v1")),
        )
        errors = scenario.validate()
        if errors:
            raise StressScenarioError(
                f"scenario_{index}_invalid:" + ",".join(errors)
            )
        scenarios.append(scenario)
    errors = validate_scenarios(scenarios)
    if errors:
        raise StressScenarioError("invalid_scenarios:" + ",".join(errors))
    return tuple(scenarios)


__all__ = [
    "COMPARISON_KEYS",
    "EVIDENCE_CLASSES",
    "EVIDENCE_DOCUMENTED",
    "EVIDENCE_HYPOTHETICAL",
    "EVIDENCE_IMPORTED",
    "KIND_COMMISSION",
    "KIND_CONVERSION",
    "KIND_CORRELATION",
    "KIND_MISSING_QUOTES",
    "KIND_QUOTE_DELAY",
    "KIND_SLIPPAGE",
    "KIND_SPREAD",
    "KIND_SWAP",
    "KIND_WEEKEND_GAP",
    "SCENARIO_KINDS",
    "STRESS_FORMAT",
    "STRESS_MODE",
    "STRESS_SCHEMA_VERSION",
    "ScenarioApplication",
    "StressComparison",
    "StressRun",
    "StressScenario",
    "StressScenarioError",
    "StressSuiteReport",
    "apply_scenario",
    "load_stress_scenarios",
    "run_stress_suite",
    "validate_scenarios",
]
