from decimal import Decimal

import pytest

from app.calendar.trading import MarketStatus
from axiom_forex.universe.manager import InstrumentCatalogManager, spec_signature
from fixtures.m1 import (
    CALENDAR_ID,
    PAIR,
    PROVIDER_ID,
    local_utc,
    make_calendar,
    make_spec,
)


def _record(spec, **overrides):
    record = dict(spec.audit_view())
    record["pair"] = spec.canonical_pair
    record["quantity_unit"] = spec.quantity_unit_value
    record["status"] = spec.status_value
    record["provider_aliases"] = dict(spec.provider_aliases)
    record.update(overrides)
    return record


def _manager():
    return InstrumentCatalogManager.from_specs(
        [make_spec()], source="SYNTHETIC_INSTRUMENT_SOURCE", version="catalog-v1"
    )


def test_catalog_manager_hashes_versions_and_sync_changes():
    manager = _manager()
    status = manager.status()
    assert status["spec_count"] == 1
    assert status["pairs"] == [PAIR]
    assert status["signature"] == manager.signature()
    assert spec_signature(make_spec()) == manager.hashes[PAIR]

    changed = _record(make_spec(), spec_version="spec-v2")
    result = manager.sync([changed], version="catalog-v2")
    assert result.changed == (PAIR,)
    assert result.version == "catalog-v2"
    assert manager.hashes[PAIR] != status["spec_hashes"][PAIR]
    assert result.signature == manager.signature()


def test_catalog_manager_adds_and_removes_specs_without_network():
    manager = _manager()
    second = _record(
        make_spec(),
        pair="CCC/DDD",
        provider_aliases={PROVIDER_ID: "CCC_DDD"},
    )
    added = manager.sync([_record(make_spec()), second], version="catalog-v2")
    assert added.added == ("CCC/DDD",)
    assert manager.catalog.resolve(PROVIDER_ID, "CCC_DDD") is not None

    removed = manager.sync([_record(make_spec())], version="catalog-v3")
    assert removed.removed == ("CCC/DDD",)
    assert manager.catalog.resolve(PROVIDER_ID, "CCC_DDD") is None


def test_catalog_manager_rejects_missing_spec_fields():
    record = _record(make_spec())
    del record["pip_size"]
    with pytest.raises(ValueError, match="missing_pip_size"):
        InstrumentCatalogManager(
            [record], source="SYNTHETIC_INSTRUMENT_SOURCE", version="catalog-v1"
        )


def test_calendar_handles_dst_holiday_daily_break_and_expiry():
    calendar = make_calendar()
    assert local_utc(2026, 3, 6, 17, 0).hour == 22  # EST
    assert local_utc(2026, 3, 8, 17, 0).hour == 21  # EDT
    assert calendar.status(PAIR, local_utc(2026, 3, 8, 17, 0)).is_open
    assert calendar.status(PAIR, local_utc(2026, 3, 9, 10, 0)).status == MarketStatus.HOLIDAY
    assert (
        calendar.status(PAIR, local_utc(2026, 3, 10, 17, 30)).status
        == MarketStatus.DAILY_BREAK
    )
    assert (
        calendar.status(PAIR, local_utc(2026, 3, 14, 10, 0)).status
        == MarketStatus.CALENDAR_EXPIRED
    )


def test_calendar_signature_changes_with_version():
    first = make_calendar()
    second = make_calendar(version="calendar-v2")
    assert first.signature() != second.signature()
    assert first.calendar_id == CALENDAR_ID
    assert second.version == "calendar-v2"
