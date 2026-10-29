"""Dataset manifests with explicit data class and provenance.

The manifest is the durable record of *what* a dataset is: its identity, pair,
timeframe, coverage, digests, spec/calendar versions, declared data class, and
the source label that produced it.  It contains no credentials, no local file
paths, and no data values beyond timestamps and digests.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from app.data.models import ForexQuote, ForexQuoteBar

from axiom_forex.data.classes import DATA_CLASS_SYNTHETIC_FIXTURE

DATASET_MANIFEST_SCHEMA_VERSION = "dataset-manifest-v1"


@dataclass(frozen=True)
class DatasetManifest:
    dataset_id: str
    pair: str
    timeframe: str
    bar_count: int
    first_timestamp: str
    last_timestamp: str
    sha256: str
    spec_version: str
    calendar_signature: str
    source: str
    data_class: str = DATA_CLASS_SYNTHETIC_FIXTURE
    timezone_basis: str = "UTC"
    source_version: str = ""
    validation_classification: str = ""
    schema_version: str = DATASET_MANIFEST_SCHEMA_VERSION

    def audit_view(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "dataset_id": self.dataset_id,
            "pair": self.pair,
            "timeframe": self.timeframe,
            "bar_count": self.bar_count,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "sha256": self.sha256,
            "spec_version": self.spec_version,
            "calendar_signature": self.calendar_signature,
            "source": self.source,
            "source_version": self.source_version,
            "data_class": self.data_class,
            "timezone_basis": self.timezone_basis,
            "validation_classification": self.validation_classification,
        }


def _payload(bars: Sequence[ForexQuoteBar]) -> str:
    rows = []
    for bar in bars:
        rows.append(
            {
                "timestamp": bar.timestamp.isoformat(),
                "bid": {
                    point: str(getattr(bar.bid, point))
                    for point in ("open", "high", "low", "close")
                },
                "ask": {
                    point: str(getattr(bar.ask, point))
                    for point in ("open", "high", "low", "close")
                },
                "quote_count": bar.quote_count,
                "complete": bar.complete,
            }
        )
    return json.dumps(rows, sort_keys=True, separators=(",", ":"))


def _quote_payload(quotes: Sequence[ForexQuote]) -> str:
    rows = [
        {
            "timestamp": quote.timestamp_utc().isoformat(),
            "bid": str(quote.bid),
            "ask": str(quote.ask),
            "quote_id": quote.quote_id,
        }
        for quote in quotes
    ]
    return json.dumps(rows, sort_keys=True, separators=(",", ":"))


def dataset_digest(bars: Sequence[ForexQuoteBar]) -> str:
    return hashlib.sha256(_payload(bars).encode("utf-8")).hexdigest()


def quote_digest(quotes: Sequence[ForexQuote]) -> str:
    return hashlib.sha256(_quote_payload(quotes).encode("utf-8")).hexdigest()


def payload_digest(payload: str | bytes) -> str:
    """Stable SHA-256 of a raw import payload (bytes or text)."""

    data = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    return hashlib.sha256(data).hexdigest()


def build_manifest(
    bars: Sequence[ForexQuoteBar],
    *,
    dataset_id: str,
    spec_version: str,
    calendar_signature: str,
    source: str,
    data_class: str = DATA_CLASS_SYNTHETIC_FIXTURE,
    timezone_basis: str = "UTC",
    source_version: str = "",
    validation_classification: str = "",
) -> DatasetManifest:
    if not bars:
        raise ValueError("Empty dataset")
    timeframe = bars[0].timeframe
    pair = bars[0].canonical_pair or ""
    if any(bar.timeframe != timeframe or (bar.canonical_pair or "") != pair for bar in bars):
        raise ValueError("Dataset must contain one pair and one timeframe")
    if not str(source).strip():
        raise ValueError("Dataset source label is required")
    return DatasetManifest(
        dataset_id=dataset_id,
        pair=pair,
        timeframe=timeframe,
        bar_count=len(bars),
        first_timestamp=bars[0].timestamp.isoformat(),
        last_timestamp=bars[-1].end_at.isoformat(),
        sha256=dataset_digest(bars),
        spec_version=spec_version,
        calendar_signature=calendar_signature,
        source=str(source),
        data_class=str(data_class),
        timezone_basis=str(timezone_basis),
        source_version=str(source_version),
        validation_classification=str(validation_classification),
    )


def verify_manifest(bars: Sequence[ForexQuoteBar], manifest: DatasetManifest) -> bool:
    return (
        bool(bars)
        and len(bars) == manifest.bar_count
        and dataset_digest(bars) == manifest.sha256
        and (bars[0].canonical_pair or "") == manifest.pair
        and bars[0].timeframe == manifest.timeframe
    )


def manifest_provenance_view(manifest: Mapping[str, Any] | DatasetManifest) -> dict[str, Any]:
    """Provenance-only view used by reports; never includes local paths."""

    view = manifest.audit_view() if isinstance(manifest, DatasetManifest) else dict(manifest)
    keys = (
        "schema_version",
        "dataset_id",
        "pair",
        "timeframe",
        "bar_count",
        "first_timestamp",
        "last_timestamp",
        "source",
        "source_version",
        "data_class",
        "timezone_basis",
        "validation_classification",
        "spec_version",
        "calendar_signature",
        "sha256",
    )
    return {key: view.get(key) for key in keys}
