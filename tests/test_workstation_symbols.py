"""The Account & symbols picker: what may be selected, and why not.

The picker's whole claim is that a selection is *verified*: a symbol is only
selectable when the terminal described it with both currency legs, its broker name
is preserved exactly (suffixes included), and anything else is listed with a reason
instead of being guessed at from the ticker.  These tests hold that line, and check
that the two bulk controls go through the same save path as the checkboxes.
"""

from __future__ import annotations

import re

import pytest

from axiom_forex.workstation.setup import SetupChoices
from axiom_forex.workstation.symbols import (
    REASON_NO_SPEC_REPORTED,
    REASON_TRADE_MODE_NOT_REPORTED,
    build_catalogue,
    records_from_terminal,
)
from axiom_forex.workstation.symbols import MAX_EXCLUDED_SHOWN
from axiom_forex.workstation.ui.theme import BREAKPOINTS, STYLES
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

    read = records_from_terminal(Silent(), 10)

    assert read.records == ()
    assert read.reported == 0 and read.examined == 0


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


# ---------------------------------------------------------------------------
# 3. layout: three full-width sections in the redesign's own vocabulary
# ---------------------------------------------------------------------------
#
# The page is the redesign's single-column layout, top to bottom: the account as
# a fact list, the picker on the full width, and the confirmation closing the
# page.  These tests read the *new* theme: the section order and ids, the facts
# list, the picker grid and its bounded refusal list, and the breakpoints the
# layout is built against.


def many_excluded_app(excluded: int = 113):
    """Two verified pairs and a broker-sized pile of instruments that are not."""

    specs = [spec("EURUSD.a", "EUR", "USD"), spec("GBPUSD", "GBP", "USD")]
    specs += [spec(f"SYNTH{i:03d}") for i in range(excluded)]
    session = live_session(specs=tuple(specs))
    app = app_for(live_wizard(session=session))
    app.act("connect", {"nonce": [app.nonce]})
    return app


def _content(html: str) -> str:
    """Everything inside the single-column layout wrapper."""

    return html.split("<div class='layout layout--wide'>", 1)[1]


def _section_ids(html: str) -> list[str]:
    return re.findall(r"<h2 id='([^']+)'", html)


def _style_block(marker: str) -> str:
    """The body of one CSS rule or at-rule, braces balanced."""

    start = STYLES.index(marker)
    opening = STYLES.index("{", start)
    depth = 0
    for index in range(opening, len(STYLES)):
        if STYLES[index] == "{":
            depth += 1
        elif STYLES[index] == "}":
            depth -= 1
            if depth == 0:
                return STYLES[opening + 1 : index]
    raise AssertionError(f"unbalanced stylesheet at {marker}")


def test_the_account_page_is_three_full_width_sections_in_order():
    html = render(many_excluded_app(), "/account")
    body = _content(html)

    titles = re.findall(r"<h2[^>]*>([^<]*)</h2>", body)
    assert titles == ["Detected account", "Symbols to observe", "Confirm configuration"]
    assert _section_ids(body) == ["account", "symbols", "confirm"]
    # One section per step, each a direct child of the single-column layout: no
    # two-column wrapper and no side column for the picker to be squeezed into.
    assert body.count("<section class='section'") == 3
    assert "col--aside" not in body
    # The only h1 on the page is the page title.
    assert html.count("<h1>") == 1


def test_the_detected_account_summary_is_a_labelled_fact_list():
    html = render(rich_live_app(), "/account")
    body = _content(html)

    assert "<dl class='facts'>" in body
    account = body.split("id='account'", 1)[1].split("id='symbols'", 1)[0]
    assert account.count("<dt>") == 8
    assert account.count("<dd>") == 8
    # A long value wraps under its own label instead of inside a fixed tile; the
    # narrow breakpoint folds the two columns into one.
    rule = _style_block(".facts{")
    assert "grid-template-columns:minmax(120px" in rule
    narrow = _style_block("@media (max-width:860px){")
    assert ".facts{grid-template-columns:minmax(0,1fr)" in narrow


def test_the_pair_grid_is_left_to_span_the_page():
    body = _content(render(many_excluded_app(), "/account"))

    grid = body.split("<div class='pick-grid'", 1)[1]
    # The grid's own container is the only thing wrapping it: it is not nested
    # inside a narrower column or a second card, and it is one labelled group.
    assert grid.index("</section>") > grid.index("EURUSD.a")
    assert "role='group' aria-label='Verified forex pairs'" in grid
    rule = _style_block(".pick-grid{")
    assert "repeat(auto-fill,minmax(200px,1fr))" in rule
    assert "max-height:360px" in rule
    # Every tile carries its input and its broker name exactly once.
    assert body.count("name='symbol'") == 2  # two verified pairs


def test_the_excluded_symbols_are_listed_once_and_scroll_inside_a_bounded_box():
    html = render(many_excluded_app(), "/account")
    page = parse(html)

    assert page.has_text("113 reported symbol(s) not selectable")
    # The names and their reasons appear on the page exactly once: as a list,
    # rather than a tile that only says "not selectable" *and* a list of reasons.
    assert "class='pick pick--off'" not in html
    assert html.count("not selectable") == 1
    listed = html.split("aria-label='Reported symbols that cannot be selected", 1)[1]
    listed = listed.split("</ul>", 1)[0]
    assert listed.count("<li><code>") == 113  # every name is listed, none dropped

    # A hundred rows must not stretch the page and push the confirmation out of
    # reach: the list scrolls on its own and is bounded.
    assert "<ul class='scroll-list small' tabindex='0'" in html
    rule = _style_block(".scroll-list{")
    assert "max-height:220px" in rule
    assert "overflow:auto" in rule
    # The list is a real group with a name, reachable without a mouse.
    assert "role='group' aria-label='Reported symbols that cannot be selected" in html
    # The list is capped: past the cap the page says how many rows it left out
    # and still accounts for them, rather than silently dropping them.
    assert "not listed here" not in html  # 113 rows is under the cap
    huge = parse(render(many_excluded_app(MAX_EXCLUDED_SHOWN + 30), "/account"))
    assert huge.has_text(
        f"The other 30 excluded symbol(s) are counted but not listed here"
    )
    assert huge.has_text("Every reported symbol was classified")


def test_a_narrow_viewport_stacks_every_section_instead_of_squeezing_it():
    assert BREAKPOINTS == (620, 860, 1080)
    tablet = _style_block("@media (max-width:1080px){")
    assert ".layout{grid-template-columns:minmax(0,1fr)}" in tablet
    narrow = _style_block("@media (max-width:620px){")
    # The picker and the candidate lists fold to one column, and every form field
    # is allowed the full width of the screen.
    assert ".pick-grid{grid-template-columns:minmax(0,1fr);max-height:none}" in narrow
    assert ".candidate-list{grid-template-columns:minmax(0,1fr)}" in narrow
    assert ".field,fieldset{max-width:none}" in narrow


def test_the_page_adds_no_post_target_or_field():
    html = render(many_excluded_app(), "/account")
    body = _content(html)
    actions = set(re.findall(r"<form method='post' action='([^']+)'", body))
    forms = re.findall(r"<form\b[^>]*>.*?</form>", body, re.S)

    # The same two targets as any other build: the save form and the confirmation.
    # Disconnect lives in the shared top bar, outside this body.
    assert actions == {"/actions/symbols", "/actions/confirm"}
    assert len(forms) == 3  # the checkbox form, the bulk controls, the confirm
    assert all("name='nonce'" in form for form in forms)
    assert "name='symbol'" in body
    # ...and the top bar's own controls are the shared shell's, not this page's.
    assert "/actions/disconnect" in html


def test_reading_the_new_layout_changes_nothing():
    app = rich_live_app()
    app.act("symbols", {"nonce": [app.nonce], "symbol": ["GBPUSD"]})
    app.act("confirm", {"nonce": [app.nonce]})
    before = app.wizard.view()
    connects = app.wizard.session.connect_count
    mode = app.mode_view()["mode"]

    render(app, "/account")
    render(app, "/account")

    after = app.wizard.view()
    assert list(after["symbols_selected"]) == list(before["symbols_selected"]) == ["GBPUSD"]
    assert after["confirmed"] is True
    assert app.wizard.session.connect_count == connects
    assert app.mode_view()["mode"] == mode
