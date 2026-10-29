"""Versioned provider-profile contracts and safe operator views.

A profile is an onboarding document, not an execution configuration.  It can
describe market data, broker, instrument, calendar, account/risk, cost, and
recovery terms, but it never contains credentials and never enables external
order placement.  Only an explicitly synthetic local-paper fixture profile can
be activated by the offline workflow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from app.audit.log import sanitize_audit_value

PROFILE_FORMAT = "AXIOM_FOREX_PROVIDER_PROFILE"
PROFILE_SCHEMA_VERSION = "1.0.0"
SUPPORTED_SCHEMA_VERSIONS = frozenset({"1.0.0"})
LOCAL_PAPER_ACTIVATION_MODE = "LOCAL_PAPER"
NO_RESUBMIT_POLICY = "NO_RESUBMIT_UNTIL_RECONCILED"
PROFILE_VALUE_CLASSES = frozenset({"FIXTURE", "DEMO", "PRODUCTION"})
PROFILE_ENVIRONMENTS = frozenset({"FIXTURE", "DEMO", "PRODUCTION", "UNSPECIFIED"})

REQUIRED_SECTIONS = (
    "market_data",
    "broker",
    "instrument",
    "calendar",
    "account_risk",
    "cost_financing",
    "recovery",
)

# Every key in REQUIRED_PROVIDER_VALUES appears in at least one section.  The
# validation layer verifies coverage against the source-of-truth list.
DEFAULT_SECTION_KEYS: dict[str, tuple[str, ...]] = {
    "market_data": (
        "market.provider_id",
        "market.capability_version",
        "market.source",
        "market.supported_pairs",
        "market.provider_symbols",
        "market.supported_timeframes",
        "market.quote_timestamp_basis",
        "market.clock_source",
        "market.bid_ask_semantics",
        "market.quote_id_required",
        "market.received_at_required",
        "market.quote_correction_policy",
        "market.bar_timestamp_basis",
        "market.bar_completion_rule",
        "market.bar_ohlc_semantics",
        "market.gap_policy",
        "market.history_bounds",
        "market.freshness",
        "market.calendar",
        "market.instrument_spec",
        "market.max_spread_pips",
    ),
    "broker": (
        "broker.broker_id",
        "broker.capability_version",
        "broker.execution_mode",
        "broker.external_orders_supported",
        "broker.order_types",
        "broker.partial_fills",
        "broker.cancellation",
        "broker.reconciliation",
        "broker.idempotency",
        "broker.rejection_codes",
        "broker.sizing",
        "broker.trading_hours",
        "broker.costs",
        "broker.account_currency",
        "broker.margin",
        "broker.recovery",
    ),
    "instrument": (
        "market.supported_pairs",
        "market.provider_symbols",
        "market.instrument_spec",
        "market.max_spread_pips",
        "broker.sizing",
    ),
    "calendar": (
        "market.calendar",
        "market.history_bounds",
        "market.supported_timeframes",
        "broker.trading_hours",
        "broker.costs",
    ),
    "account_risk": (
        "market.freshness",
        "market.max_spread_pips",
        "broker.account_currency",
        "broker.margin",
    ),
    "cost_financing": (
        "market.clock_source",
        "market.quote_timestamp_basis",
        "market.bar_timestamp_basis",
        "broker.costs",
        "broker.trading_hours",
    ),
    "recovery": (
        "broker.order_types",
        "broker.partial_fills",
        "broker.cancellation",
        "broker.reconciliation",
        "broker.idempotency",
        "broker.rejection_codes",
        "broker.recovery",
    ),
}

_SENSITIVE_SOURCE_PARTS = (
    "token",
    "secret",
    "password",
    "credential",
    "private_key",
    "api_key",
    "access_key",
    "account_id",
    "balance",
    "equity",
    "capital",
    "leverage",
    "margin",
)


class ProfileError(Exception):
    """Raised when a profile source cannot be read or decoded."""


def _safe_source(value: Any) -> str:
    text = "" if value is None else str(value)
    lowered = text.lower()
    if not text.strip():
        return "[MISSING]"
    if "://" in text or any(part in lowered for part in _SENSITIVE_SOURCE_PARTS):
        return "[PROVENANCE_REDACTED]"
    return text


def _clean(view: dict[str, Any]) -> dict[str, Any]:
    result = sanitize_audit_value(view)
    return result if isinstance(result, dict) else {}


@dataclass(frozen=True)
class ParseIssue:
    field: str
    code: str
    detail: str

    def audit_view(self) -> dict[str, Any]:
        return {"field": self.field, "code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class ProfileProvenance:
    source: str = ""
    source_version: str = ""
    effective_date: str = ""
    value_class: str = ""
    expires_at: str = ""
    documented_at: str = ""
    review_by: str = ""

    @classmethod
    def from_mapping(cls, view: Any) -> "ProfileProvenance":
        data = view if isinstance(view, Mapping) else {}
        return cls(
            source=str(data.get("source", "") or ""),
            source_version=str(data.get("source_version", "") or ""),
            effective_date=str(data.get("effective_date", "") or ""),
            value_class=str(data.get("value_class", "") or "").upper(),
            expires_at=str(data.get("expires_at", "") or ""),
            documented_at=str(data.get("documented_at", "") or ""),
            review_by=str(data.get("review_by", "") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_version": self.source_version,
            "effective_date": self.effective_date,
            "value_class": self.value_class,
            "expires_at": self.expires_at,
            "documented_at": self.documented_at,
            "review_by": self.review_by,
        }

    def safe_view(self) -> dict[str, Any]:
        return {
            "source": _safe_source(self.source),
            "source_version": self.source_version,
            "effective_date": self.effective_date,
            "value_class": self.value_class,
            "expires_at": self.expires_at,
            "documented_at": self.documented_at,
            "review_by": self.review_by,
        }


@dataclass(frozen=True)
class ProfileTerm:
    key: str
    value: Any
    provenance: ProfileProvenance

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "provenance": self.provenance.to_dict()}


@dataclass(frozen=True)
class ProviderProfile:
    format: str
    schema_version: str
    profile_id: str
    profile_version: str
    environment: str
    label: str
    synthetic: bool
    external_execution_enabled: bool
    activation_mode: str
    evaluated_at: str
    effective_from: str
    expires_at: str
    review_by: str
    sections: Mapping[str, tuple[str, ...]]
    terms: Mapping[str, ProfileTerm]
    parse_issues: tuple[ParseIssue, ...] = ()

    def identity_view(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "environment": self.environment,
            "label": self.label,
            "synthetic": self.synthetic,
            "external_execution_enabled": self.external_execution_enabled,
            "activation_mode": self.activation_mode,
            "evaluated_at": self.evaluated_at,
            "effective_from": self.effective_from,
            "expires_at": self.expires_at,
            "review_by": self.review_by,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "schema_version": self.schema_version,
            "profile": {
                "profile_id": self.profile_id,
                "profile_version": self.profile_version,
                "environment": self.environment,
                "label": self.label,
                "synthetic": self.synthetic,
                "external_execution_enabled": self.external_execution_enabled,
                "activation_mode": self.activation_mode,
                "evaluated_at": self.evaluated_at,
                "effective_from": self.effective_from,
                "expires_at": self.expires_at,
                "review_by": self.review_by,
            },
            "sections": {
                str(name): list(keys) for name, keys in self.sections.items()
            },
            "terms": {
                str(key): term.to_dict() for key, term in self.terms.items()
            },
        }


@dataclass(frozen=True)
class ProfileFinding:
    field: str
    code: str
    category: str
    detail: str

    @property
    def ok(self) -> bool:
        return False

    def audit_view(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "code": self.code,
            "category": self.category,
            "detail": self.detail,
            "ok": False,
        }


@dataclass(frozen=True)
class ProfileCheck:
    name: str
    passed: bool
    detail: str

    def audit_view(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class ProviderProfileReport:
    profile: ProviderProfile
    status: str
    passed: bool
    activation_allowed: bool
    activation_status: str
    activation_reason: str
    evaluated_at: datetime
    missing_fields: tuple[str, ...] = ()
    invalid_fields: tuple[str, ...] = ()
    stale_fields: tuple[str, ...] = ()
    contradictory_fields: tuple[str, ...] = ()
    compatibility_failures: tuple[str, ...] = ()
    findings: tuple[ProfileFinding, ...] = ()
    checks: tuple[ProfileCheck, ...] = ()
    term_status: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    note: str = (
        "Profiles are onboarding artifacts only. This view excludes term values, "
        "credentials, and sensitive account values. External execution remains disabled."
    )

    @property
    def check_count(self) -> int:
        return len(self.checks)

    @property
    def error_count(self) -> int:
        return len(self.findings)

    def audit_view(self) -> dict[str, Any]:
        payload = {
            "status": self.status,
            "passed": self.passed,
            "activation_allowed": self.activation_allowed,
            "profile": self.profile.identity_view(),
            "validation": {
                "status": self.status,
                "passed": self.passed,
                "check_count": self.check_count,
                "error_count": self.error_count,
                "evaluated_at": self.evaluated_at,
            },
            "activation": {
                "allowed": self.activation_allowed,
                "status": self.activation_status,
                "reason": self.activation_reason,
                "mode": self.profile.activation_mode,
                "external_execution_enabled": self.profile.external_execution_enabled,
            },
            "missing_fields": list(self.missing_fields),
            "invalid_fields": list(self.invalid_fields),
            "stale_fields": list(self.stale_fields),
            "contradictory_fields": list(self.contradictory_fields),
            "compatibility_failures": list(self.compatibility_failures),
            "findings": [item.audit_view() for item in self.findings],
            "checks": [item.audit_view() for item in self.checks],
            "required_terms": [dict(item) for item in self.term_status],
            "sections": {
                str(name): list(keys) for name, keys in self.profile.sections.items()
            },
            "parse_issues": [item.audit_view() for item in self.profile.parse_issues],
            "note": self.note,
        }
        return _clean(payload)

    def safe_operator_view(self) -> dict[str, Any]:
        return self.audit_view()
