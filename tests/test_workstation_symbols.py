"""The Account & symbols picker: what may be selected, and why not.

The picker's whole claim is that a selection is *verified*: a symbol is only
selectable when the terminal described it with both currency legs, its broker name
is preserved exactly (suffixes included), and anything else is listed with a reason
instead of being guessed at from the ticker.  These tests hold that line, and check
that the two bulk controls go through the same save path as the checkboxes.
"""

from __future__ import annotations

import pytest

from axiom_forex.workstation.setup import SetupChoices
from axiom_forex.workstation.symbols import (
    REASON_NO_SPEC_REPORTED,
    REASON_TRADE_MODE_NOT_REPORTED,
    build_catalogue,
    records_from_terminal,
)
from tests.test_workstation_ui import (
    app_for,
    live_session,
    live_wizard,
    parse,
    render,
)

# ---------------------------------------------------------------------------
# 1. classification: verified pairs, exact names, stated reasons
# ---------------------------------------------------------------------------


def spec(
    name: str,
    base: str = "",
    profit: str = "",
    *,
    trade_mode: int = 4,
    point: float = 0.00001,
    contract: float = 100000,
) -> dict:
    return {
        "name": name,
        "currency_base": base,
        "currency_profit": profit,
        "trade_mode": trade_mode,
        "digits": 5,
        "point": point,
        "trade_contract_size": contract,
    }


def test_only_a_pair_with_both_currency_legs_is_verified():
    catalogue = build_catalogue(
        [
            spec("EURUSD", "EUR", "USD"),
            spec("AAOIUSD"),  # a stock CFD: no currency legs reported
            spec("XAUUSD", "XAU", "USD"),  # a metal living in the same fields
        ]
    )

    assert [item.broker_symbol for item in catalogue.pairs] == ["EURUSD"]
    assert {item["broker_symbol"] for item in catalogue.excluded} == {"AAOIUSD", "XAUUSD"}
    reasons = {item["broker_symbol"]: item["reason"] for item in catalogue.excluded}
    assert "metal" in reasons["XAUUSD"]
    assert "both currency legs" in reasons["AAOIUSD"]


def test_broker_suffixes_are_preserved_exactly():
    catalogue = build_catalogue(
        [spec("EURUSD.a", "EUR", "USD"), spec("EURUSD.pro", "EUR", "USD")]
    )

    assert [item.broker_symbol for item in catalogue.pairs] == ["EURUSD.a", "EURUSD.pro"]
    # The pair is named from the terminal's legs, but the symbol is the broker's.
    assert all(item.pair == "EUR/USD" for item in catalogue.pairs)
    assert catalogue.select_all == ("EURUSD.a", "EURUSD.pro")


def test_a_symbol_reported_by_name_only_is_unverifiable_not_assumed_a_pair():
    catalogue = build_catalogue([{"name": "EURUSD"}])

    assert catalogue.pairs == ()
    assert catalogue.verified is False
    assert catalogue.reason == "symbol_facts_not_reported"
    assert catalogue.excluded[0]["reason"] == REASON_NO_SPEC_REPORTED


def test_select_all_leaves_out_pairs_the_account_cannot_trade():
    catalogue = build_catalogue(
        [
            spec("EURUSD", "EUR", "USD"),
            spec("GBPJPY", "GBP", "JPY", trade_mode=0),  # reported as not tradable
            spec("USDCHF", "USD", "CHF", point=None),
        ]
    )

    # Majors first, then the rest; the unusable pair stays listed, with a reason.
    assert {item.broker_symbol for item in catalogue.pairs} == {"EURUSD", "GBPJPY", "USDCHF"}
    assert catalogue.select_all == ("EURUSD",)
    skipped = {item["broker_symbol"] for item in catalogue.skipped_by_select_all}
    assert skipped == {"GBPJPY", "USDCHF"}


def test_an_unreported_trade_mode_is_never_read_as_tradable():
    record = spec("EURUSD", "EUR", "USD")
    record.pop("trade_mode")
    catalogue = build_catalogue([record])

    assert catalogue.select_all == ()
    assert catalogue.skipped_by_select_all[0]["reason"] == REASON_TRADE_MODE_NOT_REPORTED


def test_an_empty_report_is_not_a_verified_list():
    catalogue = build_catalogue([])

    assert catalogue.available is False
    assert catalogue.verified is False
    assert catalogue.reason == "no_terminal_symbols"
    assert catalogue.select_all == ()


def test_nothing_is_read_from_the_terminal_without_a_symbol_list():
    class Silent:
        def symbol_names(self):
            raise RuntimeError("terminal said no")

    assert records_from_terminal(Silent(), 10) == ()


# ---------------------------------------------------------------------------
# 2. the page: counts, reasons, and both bulk controls
# ---------------------------------------------------------------------------


class RecordingTerminal:
    """A read-only terminal double with one suffix, one metal, one CFD."""

    def __init__(self) -> None:
        self.specs = {
            "EURUSD.a": spec("EURUSD.a", "EUR", "USD"),
            "GBPUSD": spec("GBPUSD", "GBP", "USD"),
            "XAUUSD": spec("XAUUSD", "XAU", "USD", point=0.01, contract=100),
            "AAOIUSD": spec("AAOIUSD"),
        }

    def symbol_names(self):
        return list(self.specs)

    def symbol_info(self, name):
        return self.specs.get(name)


def rich_live_app():
    session = live_session()
    session.source.specs = tuple(RecordingTerminal().specs.values())
    app = app_for(live_wizard(session=session))
    app.act("connect", {"nonce": [app.nonce]})
    return app


def test_the_page_lists_verified_pairs_exactly_and_explains_each_exclusion():
    app = rich_live_app()
    html = render(app, "/account")

    assert "EURUSD.a · EUR/USD" in html
    assert "EURUSD.a" in html
    # The suffix is never stripped to invent a different broker symbol.
    assert "value='EURUSD'" not in html.replace("value='EURUSD.a'", "")
    assert "XAUUSD" in html and "metal" in html
    assert "AAOIUSD" in html and "both currency legs" in html
    assert "not selectable" in html


def test_the_page_offers_both_bulk_controls_and_shows_the_selected_count():
    app = rich_live_app()
    page = parse(render(app, "/account"))
    values = {attrs.get("value") for attrs in page.by_tag("button")}

    assert "all" in values
    assert "clear" in values
    assert page.has_text("0 selected")
    assert page.has_text("Select all takes 2 verified forex pair")

    app.act("symbols", {"nonce": [app.nonce], "bulk": ["all"]})
    assert parse(render(app, "/account")).has_text("2 selected")


def test_the_simulator_offers_nothing_selectable_and_says_why():
    from tests.test_workstation_ui import connected_simulator_app

    app = connected_simulator_app()
    html = render(app, "/account")
    page = parse(html)

    assert page.has_text("Sample symbols from the offline simulator")
    assert page.has_text("No symbol reported by the terminal could be verified")
    assert "disabled" in html  # Select all cannot be pressed
    app.act("symbols", {"nonce": [app.nonce], "bulk": ["all"]})
    assert app.wizard.choices.selected_symbols == ()


# ---------------------------------------------------------------------------
# 3. the action: one save path, exact names, confirmation withdrawn on change
# ---------------------------------------------------------------------------


def test_select_all_selects_every_verified_tradable_pair_by_broker_name():
    app = rich_live_app()
    status, location, message = app.act(
        "symbols", {"nonce": [app.nonce], "bulk": ["all"]}
    )

    assert (status, location) == (303, "/account")
    assert app.wizard.choices.selected_symbols == ("EURUSD.a", "GBPUSD")
    assert "2 verified forex pair(s)" in message


def test_select_all_includes_legacy_suffixed_pairs_not_the_base_name():
    app = rich_live_app()
    app.act("symbols", {"nonce": [app.nonce], "bulk": ["all"]})

    assert "EURUSD" not in app.wizard.choices.selected_symbols
    assert "EURUSD.a" in app.wizard.choices.selected_symbols


def test_a_reported_instrument_that_is_not_a_pair_cannot_be_saved():
    app = rich_live_app()
    status, _location, message = app.act(
        "symbols",
        {"nonce": [app.nonce], "symbol": ["EURUSD.a", "XAUUSD", "AAOIUSD"]},
    )

    assert status == 303
    assert app.wizard.choices.selected_symbols == ("EURUSD.a",)
    assert "not a verified, tradable currency pair" in message
    assert "XAUUSD" in message and "AAOIUSD" in message


def test_clear_empties_the_selection():
    app = rich_live_app()
    app.act("symbols", {"nonce": [app.nonce], "bulk": ["all"]})
    status, location, message = app.act(
        "symbols", {"nonce": [app.nonce], "bulk": ["clear"]}
    )

    assert (status, location) == (303, "/account")
    assert app.wizard.choices.selected_symbols == ()
    assert "cleared" in message.lower()


def test_a_name_the_terminal_did_not_report_is_ignored_not_substituted():
    app = rich_live_app()
    status, _location, message = app.act(
        "symbols",
        {"nonce": [app.nonce], "symbol": ["EURUSD", "EURUSD.a", "NOT_A_SYMBOL"]},
    )

    assert status == 303
    assert app.wizard.choices.selected_symbols == ("EURUSD.a",)
    assert "NOT_A_SYMBOL" in message
    assert "did not report" in message


def test_a_selection_change_withdraws_confirmation_and_says_so():
    app = rich_live_app()
    app.act("symbols", {"nonce": [app.nonce], "symbol": ["EURUSD.a"]})
    app.act("confirm", {"nonce": [app.nonce]})
    assert app.wizard.choices.confirmed is True

    _status, _location, message = app.act(
        "symbols", {"nonce": [app.nonce], "symbol": ["EURUSD.a", "GBPUSD"]}
    )

    assert app.wizard.choices.confirmed is False
    assert "unconfirmed" in message
    assert app.wizard.ready_for_observation is False


def test_re_saving_the_same_selection_keeps_the_confirmation():
    app = rich_live_app()
    app.act("symbols", {"nonce": [app.nonce], "symbol": ["EURUSD.a"]})
    app.act("confirm", {"nonce": [app.nonce]})

    app.act("symbols", {"nonce": [app.nonce], "symbol": ["EURUSD.a"]})

    assert app.wizard.choices.confirmed is True


def test_the_picker_never_opens_a_connection_or_touches_the_mode():
    app = app_for(live_wizard())
    connects = app.wizard.session.connect_count
    mode_before = app.wizard.session.source.kind

    # Not connected: the bulk control still posts, and still selects nothing.
    app.act("symbols", {"nonce": [app.nonce], "bulk": ["all"]})

    assert app.wizard.session.connect_count == connects
    assert app.wizard.session.connected is False
    assert app.wizard.session.source.kind == mode_before
    assert app.wizard.choices.selected_symbols == ()


def test_the_symbol_controls_are_not_on_quick_setup_or_the_mode_page():
    app = rich_live_app()

    for path in ("/quick-setup", "/mode"):
        html = render(app, path)
        assert "value='all'" not in html, path
        assert "Clear selection" not in html, path


def test_the_post_allowlist_still_carries_only_the_known_actions():
    from axiom_forex.workstation.app import POST_ACTIONS

    assert "symbols" in POST_ACTIONS
    assert "symbols-all" not in POST_ACTIONS


@pytest.mark.parametrize("names", [(), ("EURUSD.a",), ("GBPUSD", "EURUSD.a")])
def test_with_symbols_never_leaves_a_changed_set_confirmed(names):
    choices = SetupChoices(selected_symbols=("EURUSD.a",), confirmed=True)

    updated = choices.with_symbols(names)

    if tuple(sorted(set(names))) == ("EURUSD.a",):
        assert updated.confirmed is True
    else:
        assert updated.confirmed is False
