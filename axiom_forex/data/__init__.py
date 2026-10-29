"""Provider-neutral forex data normalization, pipeline, quality, and local fixtures."""

from .classes import (
    ALL_DATA_CLASSES,
    DATA_CLASS_SYNTHETIC_FIXTURE,
    DATA_CLASS_USER_SUPPLIED_DEMO,
    DATA_CLASS_USER_SUPPLIED_HISTORICAL,
    DATA_CLASS_USER_SUPPLIED_PRODUCTION,
    IMPORT_DATA_CLASSES,
    data_class_view,
    is_execution_eligible,
    is_imported,
    is_production_declared,
    is_synthetic,
)
from .fixtures import (
    SyntheticForexProvider,
    build_synthetic_bars,
    make_backtest_conversion_book,
    synthetic_bar_payloads,
    synthetic_quote_payloads,
)
from .normalization import DataNormalizer, NormalizedResult
from .pipeline import DataPipelineError, ForexDataPipeline, LoadedDataset
from .quality import QualityFinding, QualityReport, assess_bars

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
