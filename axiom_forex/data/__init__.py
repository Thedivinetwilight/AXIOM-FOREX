"""Provider-neutral forex data normalization, pipeline, quality, and local fixtures."""

from __future__ import annotations

from typing import Any

__all__ = [
    "ALL_DATA_CLASSES",
    "DATA_CLASS_SYNTHETIC_FIXTURE",
    "DATA_CLASS_USER_SUPPLIED_DEMO",
    "DATA_CLASS_USER_SUPPLIED_HISTORICAL",
    "DATA_CLASS_USER_SUPPLIED_PRODUCTION",
    "IMPORT_DATA_CLASSES",
    "DataNormalizer",
    "DataPipelineError",
    "ForexDataPipeline",
    "LoadedDataset",
    "NormalizedResult",
    "QualityFinding",
    "QualityReport",
    "SyntheticForexProvider",
    "assess_bars",
    "build_synthetic_bars",
    "data_class_view",
    "is_execution_eligible",
    "is_imported",
    "is_production_declared",
    "is_synthetic",
    "make_backtest_conversion_book",
    "synthetic_bar_payloads",
    "synthetic_quote_payloads",
]

#: Public name -> (submodule, attribute) resolved on first access.
#:
#: Imported lazily because the submodules import each other through this package:
#: ``pipeline`` imports ``axiom_forex.research.datasets``, which imports
#: ``axiom_forex.data.classes``, which would re-enter this ``__init__`` and ask
#: for a ``pipeline`` that is still half-built.  Resolving names on demand ends
#: that cycle without changing what ``from axiom_forex.data import X`` means.
_LAZY: dict[str, tuple[str, str]] = {}
for _name in (
    "ALL_DATA_CLASSES",
    "DATA_CLASS_SYNTHETIC_FIXTURE",
    "DATA_CLASS_USER_SUPPLIED_DEMO",
    "DATA_CLASS_USER_SUPPLIED_HISTORICAL",
    "DATA_CLASS_USER_SUPPLIED_PRODUCTION",
    "IMPORT_DATA_CLASSES",
    "data_class_view",
    "is_execution_eligible",
    "is_imported",
    "is_production_declared",
    "is_synthetic",
):
    _LAZY[_name] = (".classes", _name)
for _name in (
    "SyntheticForexProvider",
    "build_synthetic_bars",
    "make_backtest_conversion_book",
    "synthetic_bar_payloads",
    "synthetic_quote_payloads",
):
    _LAZY[_name] = (".fixtures", _name)
for _name in ("DataNormalizer", "NormalizedResult"):
    _LAZY[_name] = (".normalization", _name)
for _name in ("DataPipelineError", "ForexDataPipeline", "LoadedDataset"):
    _LAZY[_name] = (".pipeline", _name)
for _name in ("QualityFinding", "QualityReport", "assess_bars"):
    _LAZY[_name] = (".quality", _name)
del _name


def __getattr__(name: str) -> Any:
    """Resolve a public name on first access (PEP 562)."""

    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    module_name, attribute = target
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value  # cached: the second access is a plain lookup
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))
