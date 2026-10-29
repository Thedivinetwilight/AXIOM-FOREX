"""Profile, catalog, and calendar compatibility checks for imports.

Imported data is only accepted when it is consistent with the explicit provider
profile terms, the versioned pair spec, and the trading calendar that the
offline workflow already uses.  A missing or unrecognized term fails the check;
nothing is defaulted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from app.calendar.trading import TradingCalendar
from app.common.validation import canonical_pair, parse_decimal
from app.universe.specs import CurrencyPairSpec

from axiom_forex.data.classes import (
    DATA_CLASS_SYNTHETIC_FIXTURE,
    IMPORT_DATA_CLASSES,
    is_production_declared,
)
from axiom_forex.profiles.contracts import ProviderProfile

from .contracts import CompatibilityCheck, ImportRequest


@dataclass(frozen=True)
class ProfileRequirements:
    """Terms the record validator needs, extracted from the profile."""

    provider_id: str
    provider_symbol: str
    supported_pairs: tuple[str, ...]
    supported_timeframes: tuple[str, ...]
    quote_id_required: bool
    received_at_required: bool
    correction_policy: str
    bar_completion_rule: str
    gap_policy: str
    bid_ask_semantics: str
    max_spread_pips: Decimal | None
    calendar_id: str
    calendar_version: str
    calendar_signature: str
    history_start: datetime | None
    history_end: datetime | None
    profile_environment: str
    profile_synthetic: bool
    profile_id: str
    profile_version: str


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _term_value(profile: ProviderProfile, key: str) -> Any:
    term = profile.terms.get(key)
    return term.value if term is not None else None


def _instant(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _decimal(value: Any) -> Decimal | None:
    parsed, error = parse_decimal(value, "value")
    return None if error else parsed


def _term_requirements(
    profile: ProviderProfile,
    spec: CurrencyPairSpec,
    pair: str,
) -> ProfileRequirements:
    values = {key: term.value for key, term in profile.terms.items()}
    symbols = _mapping(values.get("market.provider_symbols"))
    provider_id = _text(values.get("market.provider_id"))
    calendar = _mapping(values.get("market.calendar"))
    history = _mapping(values.get("market.history_bounds"))
    provider_symbol = _text(symbols.get(pair)) or _text(spec.alias_for(provider_id))
    return ProfileRequirements(
        provider_id=provider_id,
        provider_symbol=provider_symbol,
        supported_pairs=tuple(str(item) for item in _sequence(values.get("market.supported_pairs"))),
        supported_timeframes=tuple(
            str(item) for item in _sequence(values.get("market.supported_timeframes"))
        ),
        quote_id_required=values.get("market.quote_id_required") is True,
        received_at_required=values.get("market.received_at_required") is True,
        correction_policy=_text(values.get("market.quote_correction_policy")),
        bar_completion_rule=_text(values.get("market.bar_completion_rule")),
        gap_policy=_text(values.get("market.gap_policy")),
        bid_ask_semantics=_text(values.get("market.bid_ask_semantics")),
        max_spread_pips=_decimal(values.get("market.max_spread_pips")),
        calendar_id=_text(calendar.get("calendar_id")),
        calendar_version=_text(calendar.get("version")),
        calendar_signature=_text(calendar.get("signature")),
        history_start=_instant(history.get("start")),
        history_end=_instant(history.get("end")),
        profile_environment=profile.environment,
        profile_synthetic=profile.synthetic,
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
    )


def check_import_compatibility(
    *,
    request: ImportRequest,
    profile: ProviderProfile,
    profile_passed: bool,
    spec: CurrencyPairSpec,
    calendar: TradingCalendar,
) -> tuple[tuple[CompatibilityCheck, ...], ProfileRequirements]:
    """Return explicit compatibility checks plus the extracted requirements."""

    requirements = _term_requirements(profile, spec, request.pair)
    checks: list[CompatibilityCheck] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append(CompatibilityCheck(name=name, passed=bool(passed), detail=detail))

    check(
        "profile_validated",
        profile_passed,
        "profile must pass strict validation before it can govern an import",
    )
    check(
        "profile_identity",
        bool(requirements.profile_id) and bool(requirements.profile_version),
        "profile id and version must be declared",
    )
    check(
        "pair_supported",
        request.pair in requirements.supported_pairs,
        "pair must be listed in the profile's supported pairs",
    )
    check(
        "pair_alias",
        bool(requirements.provider_symbol),
        "a provider symbol alias for the pair is required; no alias is invented",
    )
    check(
        "provider_id",
        bool(requirements.provider_id)
        and (not request.provider_id or request.provider_id == requirements.provider_id),
        "provider id must match the profile unless explicitly identical",
    )
    check(
        "timeframe_supported",
        request.timeframe in requirements.supported_timeframes,
        "timeframe must be listed in the profile's supported timeframes",
    )
    instrument = _mapping(_term_value(profile, "market.instrument_spec"))
    check(
        "instrument_spec",
        _text(instrument.get("pair")) == request.pair
        and _text(instrument.get("spec_version")) == spec.spec_version
        and _decimal(instrument.get("price_tick_size")) == spec.price_tick_size_decimal(),
        "pair spec version and tick size must match the profile instrument terms",
    )
    check(
        "calendar_identity",
        requirements.calendar_id == calendar.calendar_id
        and requirements.calendar_version == calendar.version
        and requirements.calendar_signature == calendar.signature(),
        "profile calendar id, version, and signature must match the active calendar",
    )
    calendar_term = _mapping(_term_value(profile, "market.calendar"))
    calendar_valid_from = _instant(calendar_term.get("valid_from"))
    calendar_valid_until = _instant(calendar_term.get("valid_until"))
    check(
        "calendar_validity",
        calendar_valid_from is not None
        and calendar_valid_until is not None
        and calendar_valid_from <= request.valid_from
        and request.valid_until <= calendar_valid_until,
        "declared validity bounds must fall inside the profile calendar validity",
    )
    check(
        "history_bounds",
        requirements.history_start is not None
        and requirements.history_end is not None
        and requirements.history_start <= request.valid_from
        and request.valid_until <= requirements.history_end,
        "declared validity bounds must fall inside the profile history bounds",
    )
    quote_basis = _text(_term_value(profile, "market.quote_timestamp_basis")).upper()
    bar_basis = _text(_term_value(profile, "market.bar_timestamp_basis")).upper()
    check(
        "timestamp_basis",
        "UTC" in quote_basis and "UTC" in bar_basis,
        "quote and bar timestamp bases must be UTC-based for canonical import",
    )
    check(
        "quote_semantics",
        requirements.bid_ask_semantics == "SEPARATE_BID_ASK"
        and requirements.quote_id_required
        and requirements.received_at_required
        and requirements.correction_policy == "EXPLICIT_REPLACEMENT_BY_QUOTE_ID",
        "separate bid/ask, quote ids, receive times, and explicit corrections are required",
    )
    check(
        "bar_semantics",
        requirements.bar_completion_rule == "COMPLETE_ONLY"
        and requirements.gap_policy == "REPORT_NO_INTERPOLATION"
        and _text(_term_value(profile, "market.bar_ohlc_semantics"))
        == "SEPARATE_BID_ASK_OHLC",
        "completed bid/ask bars with no interpolation are required",
    )
    check(
        "data_class",
        request.data_class in IMPORT_DATA_CLASSES
        and not (
            is_production_declared(request.data_class)
            and (
                requirements.profile_synthetic
                or requirements.profile_environment
                in {DATA_CLASS_SYNTHETIC_FIXTURE, "FIXTURE", "DEMO"}
            )
        ),
        "production data cannot be validated against a synthetic or demo profile",
    )
    check(
        "spread_limit",
        requirements.max_spread_pips is not None and requirements.max_spread_pips > 0,
        "an explicit maximum spread in pips is required",
    )
    return tuple(checks), requirements


def canonical_request_pair(value: str) -> str | None:
    pair, error = canonical_pair(value)
    if error or pair is None:
        return None
    return pair
