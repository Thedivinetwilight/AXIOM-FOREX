"""One canonical path from versioned specs and raw bid/ask payloads to a dataset.

Every dataset carries an explicit data class and provenance label.  Synthetic
fixtures default to ``SYNTHETIC_FIXTURE``; imported datasets must pass their own
class through, and no code path may relabel one as the other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from app.calendar.trading import TradingCalendar
from app.data.models import ForexQuote, ForexQuoteBar, QuoteFreshness
from app.universe.specs import CurrencyPairSpec, InstrumentCatalog

from axiom_forex.data.classes import DATA_CLASS_SYNTHETIC_FIXTURE, data_class_view
from axiom_forex.research.datasets import DatasetManifest, build_manifest, verify_manifest
from axiom_forex.universe.manager import InstrumentCatalogManager, spec_signature

from .fixtures import BAR_SOURCE, SyntheticForexProvider
from .normalization import DataNormalizer
from .quality import QualityReport, assess_bars


class DataPipelineError(Exception):
    """Raised when raw data cannot be normalized or validated without invention."""


@dataclass(frozen=True)
class LoadedDataset:
    pair: str
    spec: CurrencyPairSpec
    catalog: InstrumentCatalog
    catalog_signature: str
    spec_signature: str
    bars: tuple[ForexQuoteBar, ...]
    quotes: tuple[ForexQuote, ...]
    quality: QualityReport
    manifest: DatasetManifest
    provider: SyntheticForexProvider
    normalization_count: int
    data_class: str = DATA_CLASS_SYNTHETIC_FIXTURE
    provenance: Mapping[str, str] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.quality.usable

    def verify(self) -> bool:
        return verify_manifest(self.bars, self.manifest)

    def audit_view(self) -> dict[str, Any]:
        return {
            "pair": self.pair,
            "catalog_signature": self.catalog_signature,
            "spec_signature": self.spec_signature,
            "bar_count": len(self.bars),
            "quote_count": len(self.quotes),
            "normalization_count": self.normalization_count,
            "quality": self.quality.audit_view(),
            "manifest": self.manifest.audit_view(),
            "provider_id": self.provider.provider_id,
            "source": self.manifest.source,
            "data_class": self.data_class,
            "data_class_view": data_class_view(self.data_class),
            "provenance": {str(key): str(value) for key, value in self.provenance.items()},
            "digest_valid": self.verify(),
        }


@dataclass(frozen=True)
class ForexDataPipeline:
    catalog_manager: InstrumentCatalogManager
    calendar: TradingCalendar
    provider_id: str
    quote_freshness: QuoteFreshness
    dataset_id: str
    pair: str
    source: str = BAR_SOURCE
    data_class: str = DATA_CLASS_SYNTHETIC_FIXTURE
    source_version: str = ""
    timezone_basis: str = "UTC"
    provenance: Mapping[str, str] = field(default_factory=dict)

    def load(
        self,
        raw_bar_payloads: Iterable[Mapping[str, Any]],
        raw_quote_payloads: Iterable[Mapping[str, Any]] = (),
    ) -> LoadedDataset:
        catalog = self.catalog_manager.catalog
        catalog_validation = catalog.validate()
        if not catalog_validation.valid:
            raise DataPipelineError("invalid_catalog:" + catalog_validation.reason)
        spec = catalog.get(self.pair)
        if spec is None or not spec.validate().valid:
            raise DataPipelineError("missing_or_invalid_pair_spec")
        normalizer = DataNormalizer(catalog, self.provider_id)
        bar_results = normalizer.normalize_bars(raw_bar_payloads)
        quote_results = normalizer.normalize_quotes(raw_quote_payloads)
        errors: list[str] = []
        for index, result in enumerate(bar_results):
            errors.extend(f"bar[{index}]:{error}" for error in result.errors)
        for index, result in enumerate(quote_results):
            errors.extend(f"quote[{index}]:{error}" for error in result.errors)
        if errors:
            raise DataPipelineError("normalization_failed:" + ";".join(errors))
        bars = tuple(
            result.value
            for result in bar_results
            if isinstance(result.value, ForexQuoteBar)
        )
        quotes = tuple(
            result.value
            for result in quote_results
            if isinstance(result.value, ForexQuote)
        )
        if not bars or len(bars) != len(bar_results):
            raise DataPipelineError("normalized_bar_set_incomplete")
        if len(quotes) != len(quote_results):
            raise DataPipelineError("normalized_quote_set_incomplete")
        quality = assess_bars(
            bars,
            spec=spec,
            calendar=self.calendar,
            freshness=self.quote_freshness,
            evaluated_at=bars[-1].end_at,
        )
        if not quality.usable:
            raise DataPipelineError("data_quality_unusable:" + quality.classification)
        manifest = build_manifest(
            bars,
            dataset_id=self.dataset_id,
            spec_version=spec.spec_version,
            calendar_signature=self.calendar.signature(),
            source=self.source,
            data_class=self.data_class,
            timezone_basis=self.timezone_basis,
            source_version=self.source_version,
            validation_classification=quality.classification,
        )
        provider = SyntheticForexProvider(
            bars=bars,
            catalog=catalog,
            provider_id=self.provider_id,
            source=self.source,
            quotes=quotes,
            supported_timeframes=frozenset({bars[0].timeframe}),
        )
        return LoadedDataset(
            pair=self.pair,
            spec=spec,
            catalog=catalog,
            catalog_signature=self.catalog_manager.signature(),
            spec_signature=spec_signature(spec),
            bars=bars,
            quotes=quotes,
            quality=quality,
            manifest=manifest,
            provider=provider,
            normalization_count=len(bar_results) + len(quote_results),
            data_class=self.data_class,
            provenance=dict(self.provenance),
        )
