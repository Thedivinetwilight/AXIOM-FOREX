"""Repository-local runtime settings; no environment or credential access.

``SafetySettings`` from the M1 kernel is the canonical paper-only boundary.
``ForexRuntimeSettings`` only adds higher-level explicit integration identity;
it never decides a broker, provider, account currency, leverage, or strategy
for production.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.common.validation import ValidationResult, parse_decimal, validate_currency_code
from app.config.settings import SafetySettings

from ..risk.entry_limits import EntryExposureLimit


def redact_local_path(value: str) -> str:
    """Describe a local path without disclosing its location.

    Reports and HTTP surfaces state *what* the storage is, never where the
    operator's filesystem keeps it: ``:memory:`` is meaningful on its own, any
    real file is reported as a generic marker.
    """

    text = str(value or "").strip()
    if text in {"", ":memory:"}:
        return text or ":memory:"
    return "<local-database-file>"


@dataclass(frozen=True)
class ForexRuntimeSettings:
    safety: SafetySettings = field(default_factory=SafetySettings)
    live_bridge_enabled: bool = False
    account_currency: str | None = None
    leverage: Decimal | None = None
    strategy_id: str | None = None
    broker_id: str | None = None
    data_provider_id: str | None = None
    calendar_id: str | None = None
    cost_model_id: str | None = None
    database_path: str = ":memory:"
    api_bearer_token: str = ""
    #: The declared hard cap on in-flight entry notional.  ``None`` is not
    #: "unlimited": the reservation book refuses new entries without one, while
    #: still accepting validated risk-reducing closes.
    entry_exposure_limit: EntryExposureLimit | None = None

    @property
    def research_only(self) -> bool:
        mode_value = (
            self.safety.mode.value
            if hasattr(self.safety.mode, "value")
            else str(self.safety.mode).upper()
        )
        return self.safety.validate().valid and mode_value in {"RESEARCH", "PAPER"}

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        safety_validation = self.safety.validate()
        errors.extend(safety_validation.errors)
        if not self.research_only:
            errors.append("forex_runtime_must_be_research_only")
        if not isinstance(self.live_bridge_enabled, bool) or self.live_bridge_enabled:
            errors.append("live_bridge_must_remain_disabled")
        if self.account_currency is not None:
            _, error = validate_currency_code(self.account_currency, "account_currency")
            if error:
                errors.append(error)
        if self.leverage is not None:
            leverage, error = parse_decimal(self.leverage, "leverage")
            if error or leverage is None or leverage <= 0:
                errors.append(error or "invalid_leverage")
        for name in ("strategy_id", "broker_id", "data_provider_id", "calendar_id", "cost_model_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                errors.append(f"invalid_{name}")
        if self.entry_exposure_limit is not None:
            if not isinstance(self.entry_exposure_limit, EntryExposureLimit):
                errors.append("invalid_entry_exposure_limit_type")
            else:
                errors.extend(self.entry_exposure_limit.validate().errors)
        if not isinstance(self.database_path, str) or not self.database_path.strip():
            errors.append("invalid_database_path")
        if not isinstance(self.api_bearer_token, str):
            errors.append("invalid_bearer_token_type")
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    @classmethod
    def for_local_fixture(cls, **overrides: object) -> "ForexRuntimeSettings":
        values: dict[str, object] = {
            "safety": SafetySettings(),
            "live_bridge_enabled": False,
            "account_currency": "CCC",
            "leverage": Decimal("5"),
            "strategy_id": "fixture-sma-cross",
            "broker_id": "SYNTHETIC_LOCAL_PAPER",
            "data_provider_id": "SYNTHETIC_FIXTURE",
            "calendar_id": "synthetic-24x5-v1",
            "cost_model_id": "cost-model-v1",
            "database_path": ":memory:",
            "api_bearer_token": "fixture-readonly-token",
            # A synthetic cap, labeled as such, so the offline workflow can
            # take an entry at all.  It is not a production risk limit.
            "entry_exposure_limit": EntryExposureLimit(
                limit_account=Decimal("250"),
                account_currency="CCC",
                version="synthetic-entry-limit-v1",
                source="OFFLINE_FIXTURE_PROFILE",
                data_class="SYNTHETIC_FIXTURE",
            ),
        }
        values.update(overrides)
        return cls(**values)  # type: ignore[arg-type]

    def audit_view(self) -> dict[str, object]:
        return {
            "research_only": self.research_only,
            "live_bridge_enabled": self.live_bridge_enabled,
            "safety": self.safety.signature(),
            "account_currency": self.account_currency,
            "leverage": self.leverage,
            "strategy_id": self.strategy_id,
            "broker_id": self.broker_id,
            "data_provider_id": self.data_provider_id,
            "calendar_id": self.calendar_id,
            "cost_model_id": self.cost_model_id,
            "database_path": redact_local_path(self.database_path),
            "bearer_token_configured": bool(self.api_bearer_token),
            "entry_exposure_limit": (
                self.entry_exposure_limit.audit_view()
                if self.entry_exposure_limit is not None
                else {"configured": False}
            ),
        }
