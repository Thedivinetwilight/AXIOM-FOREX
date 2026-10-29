"""An explicit, versioned entry-exposure limit -- never a guessed default.

The reservation book can only hold an entry inside a cap it was *given*.  This
module is that limit: a declared number, in a declared currency, under a
declared version, carrying its own provenance so a synthetic fixture limit can
never be mistaken for an operator's risk policy.

Two rules follow from the project-wide "fail closed" constraint:

* no limit declared -> new entries are refused (``ENTRY_LIMIT_NOT_CONFIGURED``);
* a limit declared in a currency other than the account's -> refused
  (``ENTRY_LIMIT_INCOMPATIBLE``), because comparing an account-currency cap to a
  notional in some other currency would be arithmetic on unrelated numbers.

Closes are unaffected: they reduce exposure, so they never need a cap.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.common.validation import ValidationResult, parse_decimal, validate_currency_code

#: A limit written into the synthetic fixtures.  It exists so the offline
#: paper workflow can run at all, and is labeled everywhere it appears.
DATA_CLASS_SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"
#: A limit an operator declared in a real profile.
DATA_CLASS_OPERATOR_DECLARED = "OPERATOR_DECLARED"


@dataclass(frozen=True)
class EntryExposureLimit:
    """A hard cap on entry notional *reserved* in flight, in account currency."""

    limit_account: Decimal | int | float | str
    account_currency: str
    version: str
    source: str
    #: Provenance label: ``SYNTHETIC_FIXTURE`` or ``OPERATOR_DECLARED``.
    data_class: str = DATA_CLASS_OPERATOR_DECLARED

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        value, error = parse_decimal(self.limit_account, "entry_exposure_limit")
        if error or value is None or value <= 0:
            errors.append(error or "invalid_entry_exposure_limit")
        _, currency_error = validate_currency_code(
            self.account_currency, "entry_exposure_limit_currency"
        )
        if currency_error:
            errors.append(currency_error)
        if not isinstance(self.version, str) or not self.version.strip():
            errors.append("missing_entry_exposure_limit_version")
        if not isinstance(self.source, str) or not self.source.strip():
            errors.append("missing_entry_exposure_limit_source")
        if self.data_class not in {
            DATA_CLASS_SYNTHETIC_FIXTURE,
            DATA_CLASS_OPERATOR_DECLARED,
        }:
            errors.append("invalid_entry_exposure_limit_data_class")
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def decimal(self) -> Decimal:
        value, error = parse_decimal(self.limit_account, "entry_exposure_limit")
        if error or value is None or value <= 0:
            raise ValueError(error or "invalid_entry_exposure_limit")
        return value

    def compatible_with(self, account_currency: str | None) -> bool:
        """True only when the cap and the account are denominated the same."""

        declared = str(self.account_currency or "").strip().upper()
        actual = str(account_currency or "").strip().upper()
        return bool(declared) and declared == actual

    @property
    def synthetic(self) -> bool:
        return self.data_class == DATA_CLASS_SYNTHETIC_FIXTURE

    def audit_view(self) -> dict[str, Any]:
        return {
            "limit_account": self.limit_account,
            "account_currency": self.account_currency,
            "version": self.version,
            "source": self.source,
            "data_class": self.data_class,
            "synthetic": self.synthetic,
        }
