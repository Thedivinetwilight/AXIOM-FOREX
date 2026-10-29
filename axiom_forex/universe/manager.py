"""Versioned explicit instrument catalogs; no public/vendor instrument fetch."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Mapping

from app.universe.specs import (
    CurrencyPairSpec,
    InstrumentCatalog,
    InstrumentStatus,
    QuantityUnit,
)


def _decimal(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"invalid_{name}") from exc
    if not result.is_finite():
        raise ValueError(f"non_finite_{name}")
    return result


def record_to_spec(record: Mapping[str, Any], *, source: str) -> CurrencyPairSpec:
    def get(name: str) -> Any:
        if name not in record:
            raise ValueError(f"missing_{name}")
        return record[name]

    max_quantity = record.get("max_quantity")
    return CurrencyPairSpec(
        pair=str(get("pair")),
        pip_size=_decimal(get("pip_size"), "pip_size"),
        price_tick_size=_decimal(get("price_tick_size"), "price_tick_size"),
        quote_precision=int(get("quote_precision")),
        contract_size=_decimal(get("contract_size"), "contract_size"),
        min_quantity=_decimal(get("min_quantity"), "min_quantity"),
        quantity_step=_decimal(get("quantity_step"), "quantity_step"),
        max_quantity=None if max_quantity is None else _decimal(max_quantity, "max_quantity"),
        quantity_unit=QuantityUnit(str(get("quantity_unit"))),
        margin_multiplier=_decimal(get("margin_multiplier"), "margin_multiplier"),
        correlation_group=str(get("correlation_group")),
        trading_calendar_id=str(get("trading_calendar_id")),
        trading_calendar_version=str(get("trading_calendar_version")),
        status=InstrumentStatus(str(get("status"))),
        spec_version=str(get("spec_version")),
        source=source,
        provider_aliases=dict(get("provider_aliases")),
    )


def spec_signature(spec: CurrencyPairSpec) -> str:
    payload = json.dumps(spec.audit_view(), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CatalogSyncResult:
    added: tuple[str, ...]
    changed: tuple[str, ...]
    removed: tuple[str, ...]
    version: str
    signature: str

    def audit_view(self) -> dict[str, Any]:
        return {
            "added": list(self.added),
            "changed": list(self.changed),
            "removed": list(self.removed),
            "version": self.version,
            "signature": self.signature,
        }


class InstrumentCatalogManager:
    def __init__(
        self,
        records: Iterable[Mapping[str, Any]],
        *,
        source: str,
        version: str,
    ):
        self.source = source
        self.version = version
        self.catalog = self._build(records)
        self.hashes = self._hash_catalog(self.catalog)

    def _build(self, records: Iterable[Mapping[str, Any]]) -> InstrumentCatalog:
        specs = tuple(record_to_spec(record, source=self.source) for record in records)
        catalog = InstrumentCatalog(specs)
        validation = catalog.validate()
        if not validation.valid:
            raise ValueError("Invalid catalog: " + validation.reason)
        for spec in specs:
            spec_validation = spec.validate()
            if not spec_validation.valid:
                raise ValueError("Invalid spec: " + spec_validation.reason)
        return catalog

    @staticmethod
    def _hash_catalog(catalog: InstrumentCatalog) -> dict[str, str]:
        return {
            spec.canonical_pair or spec.pair: spec_signature(spec)
            for spec in catalog.specs
        }

    @classmethod
    def from_specs(
        cls,
        specs: Iterable[CurrencyPairSpec],
        *,
        source: str,
        version: str,
    ) -> "InstrumentCatalogManager":
        records = []
        for spec in specs:
            record = dict(spec.audit_view())
            record["pair"] = spec.canonical_pair
            record["quantity_unit"] = spec.quantity_unit_value
            record["status"] = spec.status_value
            record["provider_aliases"] = dict(spec.provider_aliases)
            records.append(record)
        return cls(records, source=source, version=version)

    def sync(
        self,
        records: Iterable[Mapping[str, Any]],
        *,
        version: str | None = None,
    ) -> CatalogSyncResult:
        new_catalog = self._build(records)
        new_hashes = self._hash_catalog(new_catalog)
        old_hashes = self.hashes
        added = tuple(sorted(set(new_hashes) - set(old_hashes)))
        removed = tuple(sorted(set(old_hashes) - set(new_hashes)))
        changed = tuple(
            sorted(
                pair
                for pair in set(new_hashes) & set(old_hashes)
                if new_hashes[pair] != old_hashes[pair]
            )
        )
        self.catalog = new_catalog
        self.hashes = new_hashes
        self.version = version or self.version
        return CatalogSyncResult(
            added, changed, removed, self.version, self.signature()
        )

    def signature(self) -> str:
        payload = json.dumps(self.hashes, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def status(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "version": self.version,
            "signature": self.signature(),
            "spec_count": len(self.catalog.specs),
            "pairs": list(self.catalog.pairs),
            "spec_hashes": dict(self.hashes),
        }
