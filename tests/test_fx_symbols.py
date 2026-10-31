"""Which symbols are forex, which are not, and how the offer is ordered."""

from __future__ import annotations

from decimal import Decimal

from axiom_forex.config.fx_symbols import (
    FX_PAIR,
    METAL,
    UNCLASSIFIED,
    classify,
    excluded,
    forex_symbols,
    recommended,
    resolve_choice,
)
from axiom_forex.config.terminal_facts import SymbolFacts

BASE = dict(
    description="",
    digits=5,
    point=Decimal("0.00001"),
    contract_size=Decimal("100000"),
    volume_min=Decimal("0.01"),
    volume_max=Decimal("100"),
    volume_step=Decimal("0.01"),
    stops_level=0,
    spread_points=4,
    trade_mode=4,
    swap_long=Decimal("-0.5"),
    swap_short=Decimal("0.2"),
)


def facts(symbol: str, base: str, profit: str, *, tradable: bool = True) -> SymbolFacts:
    pair = f"{base}/{profit}" if base and profit else ""
    return SymbolFacts(
        broker_symbol=symbol,
        base_currency=base,
        profit_currency=profit,
        margin_currency=base,
        pair=pair,
        tradable=tradable,
        **BASE,
    )


def test_a_pair_is_named_from_the_reported_legs_only():
    assert classify(facts("EURUSD.pro", "EUR", "USD")).kind == FX_PAIR
    assert classify(facts("EURUSD.pro", "EUR", "USD")).pair == "EUR/USD"


def test_a_share_cfd_is_not_a_pair_and_is_never_guessed_from_the_ticker():
    choice = classify(facts("AAOIUSD", "", "USD"))
    assert choice.kind == UNCLASSIFIED
    assert choice.pair == ""
    assert choice.usable is False
    assert choice.reason


def test_a_metal_is_excluded_even_though_its_legs_look_like_currencies():
    choice = classify(facts("XAUUSD", "XAU", "USD"))
    assert choice.kind == METAL
    assert choice.usable is False
    assert choice.reason


def test_an_untradable_pair_stays_listed_with_its_reason():
    choice = classify(facts("GBPUSD.pro", "GBP", "USD", tradable=False))
    assert choice.kind == FX_PAIR
    assert choice.usable is False
    assert choice.reason


def test_majors_come_first_and_nothing_is_dropped():
    symbols = [
        facts("EURJPY", "EUR", "JPY"),
        facts("GBPUSD", "GBP", "USD"),
        facts("AAOIUSD", "", "USD"),
        facts("XAUUSD", "XAU", "USD"),
        facts("EURUSD", "EUR", "USD"),
    ]
    offered = forex_symbols(symbols)
    assert [item.broker_symbol for item in offered] == ["EURUSD", "GBPUSD", "EURJPY"]
    assert all(item.kind == FX_PAIR for item in offered)
    assert {item.broker_symbol for item in excluded(symbols)} == {"AAOIUSD", "XAUUSD"}
    assert recommended(symbols)[0] == "EURUSD"


def test_resolve_choice_returns_only_what_was_asked_for():
    symbols = [facts("EURUSD", "EUR", "USD"), facts("GBPUSD", "GBP", "USD")]
    chosen = resolve_choice(symbols, ["GBPUSD", "AAOIUSD"])
    assert [item.broker_symbol for item in chosen] == ["GBPUSD"]
