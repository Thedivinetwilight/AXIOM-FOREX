from dataclasses import replace
from decimal import Decimal

import pytest

from app.common.validation import canonical_pair
from app.universe.specs import InstrumentCatalog, QuantityUnit
from fixtures.m1 import PAIR, PROVIDER_ID, PROVIDER_SYMBOL, make_catalog, make_spec


def test_canonical_pair_accepts_explicit_separators():
    assert canonical_pair("aaa/bbb")[0] == PAIR
    assert canonical_pair("AAA-BBB")[0] == PAIR
    assert canonical_pair("AAA_BBB")[0] == PAIR
    assert canonical_pair("AAA.BBB")[0] == PAIR


@pytest.mark.parametrize(
    "value, expected_error",
    [
        ("AAABBB", "ambiguous_pair"),
        ("AA/BBB", "invalid_base_currency"),
        ("AAA/AAA", "identical_currencies"),
        ("AAA/BB/C", "invalid_pair_separator"),
        (None, "invalid_pair_type"),
    ],
)
def test_canonical_pair_rejects_ambiguous_or_invalid(value, expected_error):
    pair, error = canonical_pair(value)
    assert pair is None
    assert error == expected_error


def test_currency_pair_spec_properties_and_lot_conversion():
    spec = make_spec()
    assert spec.validate().valid
    assert spec.canonical_pair == PAIR
    assert spec.base_currency == "AAA"
    assert spec.quote_currency == "BBB"
    assert spec.to_base_units(Decimal("0.10")) == Decimal("100")
    unit_spec = replace(spec, quantity_unit=QuantityUnit.BASE_UNITS)
    assert unit_spec.to_base_units(Decimal("100")) == Decimal("100")


@pytest.mark.parametrize(
    "overrides",
    [
        {"pip_size": Decimal("0")},
        {"price_tick_size": Decimal("0")},
        {"contract_size": Decimal("-1")},
        {"quote_precision": -1},
        {"price_tick_size": Decimal("0.001"), "quote_precision": 2},
        {"min_quantity": Decimal("0.015")},
        {"status": "UNKNOWN"},
        {"spec_version": ""},
        {"provider_aliases": {PROVIDER_ID: ""}},
    ],
)
def test_currency_pair_spec_rejects_invalid_values(overrides):
    spec = replace(make_spec(), **overrides)
    result = spec.validate()
    assert not result.valid
    assert result.errors


def test_quantity_min_step_max_and_non_finite():
    spec = make_spec()
    assert spec.parse_quantity(Decimal("0.10"))[1].valid
    assert "quantity_below_minimum" in spec.parse_quantity(Decimal("0.001"))[1].errors
    assert "quantity_above_maximum" in spec.parse_quantity(Decimal("6"))[1].errors
    assert "quantity_not_step_aligned" in spec.parse_quantity(Decimal("0.105"))[1].errors
    assert spec.parse_quantity(float("nan"))[1].errors


def test_price_precision_and_tick_alignment():
    spec = make_spec()
    assert spec.validate_price(Decimal("1.10020")).valid
    assert "price_precision_exceeded" in spec.validate_price(Decimal("1.100201")).errors
    assert spec.validate_price(Decimal("1.100201")).errors


def test_catalog_strict_alias_resolution_has_no_fallback():
    catalog = make_catalog()
    assert catalog.resolve(PROVIDER_ID, PROVIDER_SYMBOL) is not None
    assert catalog.resolve(PROVIDER_ID, "WRONG_ALIAS") is None
    assert catalog.resolve("OTHER_FEED", PROVIDER_SYMBOL) is None
    assert catalog.get(PAIR) is not None
    assert catalog.validate().valid


def test_catalog_rejects_duplicate_pair_or_alias():
    spec = make_spec()
    with pytest.raises(ValueError, match="Duplicate pair"):
        InstrumentCatalog((spec, make_spec()))
    other_pair = replace(
        make_spec(),
        pair="DDD/EEE",
        provider_aliases={PROVIDER_ID: PROVIDER_SYMBOL},
    )
    with pytest.raises(ValueError, match="Duplicate provider alias"):
        InstrumentCatalog((spec, other_pair))


def test_catalog_identity_validation_rejects_bad_alias():
    catalog = make_catalog(replace(make_spec(), provider_aliases={PROVIDER_ID: ""}))
    result = catalog.validate()
    assert not result.valid
    assert any("invalid_provider_alias" in error for error in result.errors)
