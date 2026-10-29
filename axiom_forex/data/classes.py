"""Explicit data classes so synthetic fixtures can never be mistaken for market data.

Every dataset in this repository carries one data class.  Synthetic fixtures are
generated locally for tests and the offline workflow; imported datasets come
from a user-supplied canonical file and are always labelled as user-supplied.
There is no automatic promotion between classes: an imported file can never be
relabelled as a fixture, and fixture data can never be relabelled as imported or
production data.
"""

from __future__ import annotations

from typing import Any

DATA_CLASS_SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"
DATA_CLASS_USER_SUPPLIED_DEMO = "USER_SUPPLIED_DEMO"
DATA_CLASS_USER_SUPPLIED_HISTORICAL = "USER_SUPPLIED_HISTORICAL"
DATA_CLASS_USER_SUPPLIED_PRODUCTION = "USER_SUPPLIED_PRODUCTION"

SYNTHETIC_DATA_CLASSES = frozenset({DATA_CLASS_SYNTHETIC_FIXTURE})

# Data classes an offline importer is allowed to declare.  ``SYNTHETIC_FIXTURE``
# is intentionally absent: imports can never masquerade as local fixtures.
IMPORT_DATA_CLASSES = frozenset(
    {
        DATA_CLASS_USER_SUPPLIED_DEMO,
        DATA_CLASS_USER_SUPPLIED_HISTORICAL,
        DATA_CLASS_USER_SUPPLIED_PRODUCTION,
    }
)

ALL_DATA_CLASSES = frozenset(SYNTHETIC_DATA_CLASSES | IMPORT_DATA_CLASSES)

# Only the local synthetic fixture dataset may feed the local paper execution
# path.  Imported data is research/backtest material in this repository.
EXECUTION_ELIGIBLE_DATA_CLASSES = SYNTHETIC_DATA_CLASSES

#: Data classes that describe externally supplied (not locally generated) data.
PRODUCTION_DATA_CLASSES = frozenset(
    {
        DATA_CLASS_USER_SUPPLIED_PRODUCTION,
    }
)


def is_synthetic(data_class: Any) -> bool:
    return str(data_class) in SYNTHETIC_DATA_CLASSES


def is_imported(data_class: Any) -> bool:
    return str(data_class) in IMPORT_DATA_CLASSES


def is_execution_eligible(data_class: Any) -> bool:
    """Return True only for locally generated synthetic fixture data."""

    return str(data_class) in EXECUTION_ELIGIBLE_DATA_CLASSES


def is_production_declared(data_class: Any) -> bool:
    return str(data_class) in PRODUCTION_DATA_CLASSES


def data_class_view(data_class: Any) -> dict[str, Any]:
    """Safe operator view for a data class; contains no data values."""

    value = str(data_class)
    return {
        "data_class": value,
        "synthetic": is_synthetic(value),
        "imported": is_imported(value),
        "production_declared": is_production_declared(value),
        "execution_eligible": is_execution_eligible(value),
        "recognized": value in ALL_DATA_CLASSES,
    }
