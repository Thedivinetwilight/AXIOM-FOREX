"""How much of the broker's symbol list the picker actually looks at.

The reported symptom was that Account & symbols verified only seven pairs, all of
them AUD-base.  The cause was not classification: the symbol list was cut to its
first ``MAX_SYMBOLS`` names *before* any specification was read, and on a real
broker the equity CFDs sort ahead of the forex board, so the walk stopped shortly
after the ``AUD`` block.  ``EURUSD`` and everything after it were never examined.

These tests hold the fix in place:

* every reported name is classified, whatever its position in the list;
* reading a specification writes nothing -- no ``symbol_select``, no Market Watch
  change;
* when an adapter cannot return specifications in bulk the read says how many names
  it did not reach, instead of implying the list ended there;
* only the *drawn* rows are capped, and the page states the cap;
* broker names stay byte-exact, metals stay metals, and every excluded symbol keeps
  a stated reason.

Everything here runs against the repository's fake MT5 module and in-memory
sources.  No terminal, account, credential, or order is involved.
"""

from __future__ import annotations

import re

from axiom_forex.brokers.mt5.terminal import MT5Terminal
from axiom_forex.config.mt5 import MT5ExecutionMode, MT5Settings
from axiom_forex.workstation.session import MT5ObservationSource, ObservationSession
from axiom_forex.workstation.symbols import (
    MAX_EXCLUDED_SHOWN,
    REASON_NO_SPEC_REPORTED,
    SymbolRead,
    build_catalogue,
    catalogue_message,
    records_from_terminal,
)
from tests.fake_mt5 import FakeMT5
from tests.test_workstation_ui import app_for, live_session, live_wizard, render

# ---------------------------------------------------------------------------
# a broker-shaped symbol list
# ---------------------------------------------------------------------------

#: A CFD record: the broker reports no currency legs, so it is not a pair.
CFD = {"currency_base": "", "currency_profit": "", "trade_mode": 4, "digits": 2, "point": 0.01}

#: Pairs that must be found however far down the list they sort.
LATER_PAIRS = (
    ("EUR", "USD"),
    ("GBP", "USD"),
    ("USD", "JPY"),
    ("USD", "CHF"),
    ("USD", "CAD"),
    ("EUR", "JPY"),
    ("EUR", "GBP"),
    ("GBP", "JPY"),
    ("NZD", "USD"),
    ("USD", "SEK"),
    ("USD", "NOK"),
    ("USD", "SGD"),
    ("USD", "MXN"),
    ("USD", "ZAR"),
)

AUD_PAIRS = ("USD", "CAD", "CHF", "JPY", "NOK", "NZD", "SEK")


def broker_symbols(*, lead: int = 100, trail: int = 140) -> dict[str, dict]:
    """CFDs that sort before ``AUD``, the AUD block, the rest of forex, more CFDs."""

    specs: dict[str, dict] = {}
    for index in range(lead):
        specs[f"AA{index:02d}USD"] = dict(CFD)
    for leg in AUD_PAIRS:
        specs[f"AUD{leg}"] = {"currency_base": "AUD", "currency_profit": leg}
    for base, quote in LATER_PAIRS:
        specs[f"{base}{quote}.pro"] = {"currency_base": base, "currency_profit": quote}
    specs["XAUUSD"] = {
        "currency_base": "XAU",
        "currency_profit": "USD",
        "point": 0.01,
        "trade_contract_size": 100.0,
    }
    for index in range(trail):
        specs[f"ZZ{index:03d}USD"] = dict(CFD)
    return specs


def expected_pairs(specs: dict[str, dict]) -> set[str]:
    return {
        name
        for name, values in specs.items()
        if values.get("currency_base")
        and values.get("currency_profit")
        and values["currency_base"] != values["currency_profit"]
        and "XAU" not in (values["currency_base"], values["currency_profit"])
    }


def mt5_settings() -> MT5Settings:
    return MT5Settings(
        terminal_path="terminal64.exe",
        server="Broker-Demo",
        account_login=55500123,
        mode=MT5ExecutionMode.TRADING_ENABLED,
        symbol_suffix=".pro",
        magic=26081001,
        deviation_points=10,
        max_price_drift_points=20,
    )


def connected_terminal(fake: FakeMT5) -> MT5Terminal:
    terminal = MT5Terminal(mt5_settings(), module=fake)
    terminal.connect()
    return terminal


def catalogue_of(read: SymbolRead) -> object:
    return build_catalogue(
        read.records,
        reported_total=read.reported,
        examined=read.examined,
        specs_reported=read.bulk,
    )


# ---------------------------------------------------------------------------
# 1. the whole list is classified, not its alphabetical head
# ---------------------------------------------------------------------------


def test_every_pair_is_verified_even_when_cfds_sort_ahead_of_it():
    specs = broker_symbols()
    fake = FakeMT5(symbols=specs, default_symbol=False)
    read = records_from_terminal(connected_terminal(fake), 120)
    catalogue = catalogue_of(read)

    names = {pair.broker_symbol for pair in catalogue.pairs}
    assert names == expected_pairs(specs)
    assert "EURUSD.pro" in names  # far past the old 120-name window
    assert len(names) == len(AUD_PAIRS) + len(LATER_PAIRS)
    assert read.reported == len(specs)
    assert read.examined == len(specs)
    assert catalogue.truncated is False


def test_the_seven_aud_pairs_are_not_the_whole_answer_any_more():
    """The exact reported symptom: seven pairs, all AUD-base."""

    specs = broker_symbols()
    read = records_from_terminal(
        connected_terminal(FakeMT5(symbols=specs, default_symbol=False)), 120
    )
    catalogue = catalogue_of(read)
    bases = {pair.pair.split("/")[0] for pair in catalogue.pairs if pair.pair}

    assert bases != {"AUD"}
    assert {"EUR", "GBP", "USD", "NZD"} <= bases
    assert len(catalogue.pairs) > len(AUD_PAIRS)


def test_a_pair_sorting_last_in_a_long_list_is_still_found():
    specs = {f"AAA{index:04d}": dict(CFD) for index in range(1500)}
    specs["ZZZUSD.pro"] = {"currency_base": "ZZZ", "currency_profit": "USD"}
    specs["EURUSD.pro"] = {"currency_base": "EUR", "currency_profit": "USD"}
    fake = FakeMT5(symbols=specs, default_symbol=False)
    read = records_from_terminal(connected_terminal(fake), 120)
    catalogue = catalogue_of(read)

    names = {pair.broker_symbol for pair in catalogue.pairs}
    assert read.bulk is True
    assert read.examined == 1502
    assert "EURUSD.pro" in names
    # ``ZZZ`` is not a currency code, so the pair is not derived from the ticker.
    assert "ZZZUSD.pro" not in names
    assert any(row["broker_symbol"] == "ZZZUSD.pro" for row in catalogue.excluded)


def test_the_bulk_read_is_one_terminal_call_and_no_writes():
    specs = broker_symbols()
    fake = FakeMT5(symbols=specs, default_symbol=False)
    terminal = connected_terminal(fake)
    calls = {"symbol_info": 0}
    original = terminal.symbol_info

    def counting(symbol):
        calls["symbol_info"] += 1
        return original(symbol)

    terminal.symbol_info = counting  # type: ignore[method-assign]
    read = records_from_terminal(terminal, 120)

    assert read.bulk is True
    assert calls["symbol_info"] == 0
    assert fake.symbol_select_calls == []


def test_reading_a_specification_never_selects_it_in_market_watch():
    fake = FakeMT5()
    terminal = connected_terminal(fake)

    info = terminal.symbol_info("EURUSD.pro")

    assert info.name == "EURUSD.pro"
    assert fake.symbol_select_calls == []


def test_the_whole_read_path_leaves_market_watch_alone():
    fake = FakeMT5(symbols=broker_symbols(), default_symbol=False)
    terminal = connected_terminal(fake)
    fake.symbol_select_calls.clear()

    records_from_terminal(terminal, 120)
    records_from_terminal(terminal, None)

    assert fake.symbol_select_calls == []


# ---------------------------------------------------------------------------
# 2. a bounded read says what it did not reach
# ---------------------------------------------------------------------------


def test_a_name_only_adapter_reports_the_names_it_did_not_reach():
    specs = broker_symbols()
    fake = FakeMT5(symbols=specs, default_symbol=False)
    fake.symbols_get_names_only = True
    read = records_from_terminal(connected_terminal(fake), 120)
    catalogue = catalogue_of(read)

    assert read.bulk is False
    assert read.reported == len(specs)
    assert read.examined == 120
    assert read.truncated is True
    assert catalogue.truncated is True
    assert catalogue.counts["not_examined"] == len(specs) - 120
    message = catalogue_message(catalogue)
    assert f"{len(specs)} symbol(s) reported, 120 examined" in message
    assert "not reached" in message
    assert fake.symbol_select_calls == []


def test_an_unbounded_fallback_read_reaches_every_name():
    specs = broker_symbols()
    fake = FakeMT5(symbols=specs, default_symbol=False)
    fake.symbols_get_names_only = True
    read = records_from_terminal(connected_terminal(fake), None)
    catalogue = catalogue_of(read)

    assert read.truncated is False
    assert {pair.broker_symbol for pair in catalogue.pairs} == expected_pairs(specs)


def test_a_failing_bulk_call_falls_back_to_the_names():
    specs = {"EURUSD.pro": {"currency_base": "EUR", "currency_profit": "USD"}}
    fake = FakeMT5(symbols=specs, default_symbol=False)
    terminal = connected_terminal(fake)

    def refuse(group=None):
        raise RuntimeError("terminal said no")

    fake.symbols_get = refuse  # type: ignore[assignment]
    read = records_from_terminal(terminal, 10)

    assert read.records == ()
    assert read.reported == 0
    assert read.truncated is False


def test_a_silent_terminal_yields_nothing_and_claims_nothing():
    class Silent:
        def symbol_names(self):
            raise RuntimeError("terminal said no")

    read = records_from_terminal(Silent(), 10)
    catalogue = build_catalogue(read.records)

    assert read.records == ()
    assert catalogue.available is False
    assert catalogue.reason == "no_terminal_symbols"


# ---------------------------------------------------------------------------
# 3. every reported name is accounted for, with a reason
# ---------------------------------------------------------------------------


def test_pairs_plus_excluded_cover_every_reported_name_exactly_once():
    specs = broker_symbols()
    catalogue = catalogue_of(
        records_from_terminal(
            connected_terminal(FakeMT5(symbols=specs, default_symbol=False)), 120
        )
    )

    seen = [pair.broker_symbol for pair in catalogue.pairs] + [
        row["broker_symbol"] for row in catalogue.excluded
    ]
    assert len(seen) == len(set(seen)) == len(specs)
    assert set(seen) == set(specs)
    assert catalogue.counts["excluded"] == len(catalogue.excluded)


def test_every_excluded_symbol_states_a_reason():
    catalogue = catalogue_of(
        records_from_terminal(
            connected_terminal(FakeMT5(symbols=broker_symbols(), default_symbol=False)), 120
        )
    )

    assert catalogue.excluded
    for row in catalogue.excluded:
        assert row["reason"].strip(), row
        assert row["broker_symbol"].strip(), row


def test_metals_and_bare_cfds_are_excluded_for_their_own_reasons():
    catalogue = build_catalogue(
        [
            {"name": "EURUSD.pro", "currency_base": "EUR", "currency_profit": "USD", "trade_mode": 4},
            {"name": "XAUUSD", "currency_base": "XAU", "currency_profit": "USD", "trade_mode": 4},
            {"name": "AAOIUSD", "currency_base": "", "currency_profit": "", "trade_mode": 4},
        ]
    )
    by_name = {row["broker_symbol"]: row for row in catalogue.excluded}

    assert [pair.broker_symbol for pair in catalogue.pairs] == ["EURUSD.pro"]
    assert by_name["XAUUSD"]["kind"] == "METAL"
    assert by_name["AAOIUSD"]["reason"] == REASON_NO_SPEC_REPORTED


def test_a_broker_suffix_is_never_stripped_to_invent_a_pair():
    catalogue = build_catalogue(
        [
            {"name": "EURUSD.a", "currency_base": "EUR", "currency_profit": "USD", "trade_mode": 4},
            {"name": "EURUSD", "currency_base": "EUR", "currency_profit": "USD", "trade_mode": 4},
        ]
    )

    assert {pair.broker_symbol for pair in catalogue.pairs} == {"EURUSD.a", "EURUSD"}
    assert {pair.label for pair in catalogue.pairs} == {"EURUSD.a · EUR/USD", "EURUSD · EUR/USD"}


# ---------------------------------------------------------------------------
# 4. only the drawn rows are capped, and the page says so
# ---------------------------------------------------------------------------


def app_with_specs(specs) -> object:
    """A connected workstation app whose terminal double reports ``specs``."""

    session = live_session()
    session.source.specs = tuple(specs)
    app = app_for(live_wizard(session=session))
    app.act("connect", {"nonce": [app.nonce]})
    return app


def test_the_page_draws_a_bounded_number_of_excluded_rows_and_says_how_many():
    total = MAX_EXCLUDED_SHOWN + 300
    html = render(
        app_with_specs(
            [
                {
                    "name": f"CFD{index:04d}",
                    "currency_base": "",
                    "currency_profit": "",
                    "trade_mode": 4,
                    "digits": 2,
                    "point": 0.01,
                }
                for index in range(total)
            ]
        ),
        "/account",
    )

    assert len(re.findall(r"<li><span class='mono'>CFD", html)) == MAX_EXCLUDED_SHOWN
    assert f"{total} reported symbol(s) not selectable" in html
    assert f"first {MAX_EXCLUDED_SHOWN} shown" in html
    assert f"The other {total - MAX_EXCLUDED_SHOWN} excluded symbol(s)" in html


def test_the_page_shows_a_pair_that_sorts_past_the_old_window():
    specs = [
        {
            "name": f"AA{index:02d}USD",
            "currency_base": "",
            "currency_profit": "",
            "trade_mode": 4,
            "digits": 2,
            "point": 0.01,
        }
        for index in range(150)
    ]
    specs.append(
        {
            "name": "EURUSD.pro",
            "currency_base": "EUR",
            "currency_profit": "USD",
            "trade_mode": 4,
            "digits": 5,
            "point": 0.00001,
            "trade_contract_size": 100000,
        }
    )
    html = render(app_with_specs(specs), "/account")

    assert "EURUSD.pro · EUR/USD" in html
    assert "value='EURUSD.pro'" in html
    assert "151 symbol(s) reported: 1 verified forex pair(s), 150 excluded." in html


def test_the_status_snapshot_cap_does_not_gate_the_catalogue():
    from axiom_forex.workstation import session as session_module

    assert session_module.MAX_SYMBOLS < session_module.MAX_SYMBOL_READS



# ---------------------------------------------------------------------------
# 5. end to end: the real adapter, the session, and the page
# ---------------------------------------------------------------------------


def live_app_from_mt5(specs: dict[str, dict]):
    """An app whose read-only source *is* the MT5 adapter, over the fake module."""

    fake = FakeMT5(symbols=specs, default_symbol=False)
    source = MT5ObservationSource(settings=mt5_settings(), module=fake)
    app = app_for(live_wizard(session=ObservationSession(source)))
    app.act("connect", {"nonce": [app.nonce]})
    return app, fake


def test_the_mt5_source_examines_the_whole_list_and_writes_nothing():
    specs = broker_symbols()
    app, fake = live_app_from_mt5(specs)
    read = app.wizard.session.symbol_read()

    assert read.bulk is True
    assert read.reported == read.examined == len(specs)
    assert read.truncated is False
    assert fake.symbol_select_calls == []
    assert app.wizard.session.symbol_records() == read.records


def test_the_account_page_offers_every_verified_pair_from_a_real_adapter():
    specs = broker_symbols()
    app, _fake = live_app_from_mt5(specs)
    html = render(app, "/account")

    for name in expected_pairs(specs):
        assert f"value='{name}'" in html, name
    assert (
        f"{len(specs)} symbol(s) reported: {len(expected_pairs(specs))} verified "
        f"forex pair(s), {len(specs) - len(expected_pairs(specs))} excluded."
    ) in html
    # A metal is still a metal, and a CFD is still excluded with its reason.
    assert "XAUUSD" in html
    assert REASON_NO_SPEC_REPORTED in html


def test_a_broker_with_no_forex_is_classified_not_guessed():
    specs = {f"AAA{index:04d}": dict(CFD) for index in range(800)}
    fake = FakeMT5(symbols=specs, default_symbol=False)
    read = records_from_terminal(connected_terminal(fake), 120)
    catalogue = catalogue_of(read)

    assert read.bulk is True
    assert read.examined == 800
    assert catalogue.pairs == ()
    assert catalogue.reason == "no_verified_forex_pair"
    assert len(catalogue.excluded) == 800
    assert {row["reason"] for row in catalogue.excluded} == {REASON_NO_SPEC_REPORTED}
    assert "no symbol AXIOM can verify as a currency pair" in catalogue_message(catalogue)


# ---------------------------------------------------------------------------
# 6. the catalog offer (strategies/catalog/panel.py) reads the whole list
# ---------------------------------------------------------------------------

from axiom_forex.config.terminal_facts import observe_terminal
from axiom_forex.strategies.catalog.panel import (
    MAX_CHOICES_SHOWN,
    CatalogPanel,
)
from axiom_forex.strategies.catalog.panel import MAX_EXCLUDED_SHOWN as CATALOG_MAX_EXCLUDED_SHOWN


def full_pair(name: str, base: str, quote: str) -> dict:
    """A complete pair record, as a broker-shaped snapshot would report it."""

    return {
        "name": name,
        "currency_base": base,
        "currency_profit": quote,
        "currency_margin": base,
        "digits": 5,
        "point": 0.00001,
        "trade_contract_size": 100000.0,
        "volume_min": 0.01,
        "volume_max": 100.0,
        "volume_step": 0.01,
        "trade_mode": 4,
        "trade_allowed": True,
        "spread": 4,
        "swap_mode": 1,
        "swap_long": -0.5,
        "swap_short": 0.2,
    }


def full_cfd(name: str) -> dict:
    """A share CFD: the broker reported the fields, and no currency legs."""

    return {
        "name": name,
        "currency_base": "",
        "currency_profit": "",
        "digits": 2,
        "point": 0.01,
        "trade_contract_size": 1.0,
        "volume_min": 1.0,
        "volume_max": 1000.0,
        "volume_step": 1.0,
        "trade_mode": 4,
        "trade_allowed": True,
    }


class BulkTerminal:
    """A read-only terminal whose symbol list arrives with specifications."""

    def __init__(self, specs: dict[str, dict]) -> None:
        self.specs = specs
        self.bulk_calls = 0
        self.info_calls = 0
        self.select_calls: list[str] = []

    def symbol_snapshots(self):
        self.bulk_calls += 1
        return [dict(values) for values in self.specs.values()]

    def symbol_names(self):
        return sorted(self.specs)

    def symbol_info(self, name):
        self.info_calls += 1
        return self.specs.get(name)

    def symbol_select(self, name, enable=True):  # pragma: no cover - must not be called
        self.select_calls.append(name)
        return True


class NameOnlyTerminal(BulkTerminal):
    """A wrapper that returns bare names, so the bounded fallback is exercised."""

    symbol_snapshots = None  # type: ignore[assignment]


def catalog_specs(*, cfds: int, pairs: list[tuple[str, str, str]]) -> dict[str, dict]:
    specs: dict[str, dict] = {}
    for index in range(cfds):
        name = f"AA{index:04d}"
        specs[name] = full_cfd(name)
    for broker_symbol, base, quote in pairs:
        specs[broker_symbol] = full_pair(broker_symbol, base, quote)
    specs["XAUUSD"] = {
        **full_cfd("XAUUSD"),
        "currency_base": "XAU",
        "currency_profit": "USD",
        "point": 0.01,
        "trade_contract_size": 100.0,
    }
    return specs


LATE_PAIRS = [
    ("AUDUSD", "AUD", "USD"),
    ("EURUSD.pro", "EUR", "USD"),
    ("GBPUSD.pro", "GBP", "USD"),
    ("USDJPY.pro", "USD", "JPY"),
]


def test_the_catalog_offer_finds_pairs_past_the_old_name_window(tmp_path):
    specs = catalog_specs(cfds=1500, pairs=LATE_PAIRS)
    terminal = BulkTerminal(specs)
    panel = CatalogPanel(root=tmp_path, terminal_provider=lambda: terminal)

    offer = panel.fx_choices()

    assert offer["available"] is True
    assert offer["bulk"] is True
    assert offer["truncated"] is False
    assert offer["counts"]["reported"] == len(specs)
    assert offer["counts"]["examined"] == len(specs)
    assert offer["counts"]["not_examined"] == 0
    assert offer["counts"]["forex_pairs"] == len(LATE_PAIRS)
    found = {item["pair"] for item in offer["choices"]}
    assert found == {"AUD/USD", "EUR/USD", "GBP/USD", "USD/JPY"}
    assert all(item["usable"] for item in offer["choices"])
    # One bulk call, no per-name read, and no write anywhere.
    assert terminal.bulk_calls == 1
    assert terminal.info_calls == 0
    assert terminal.select_calls == []
    # A metal in the currency fields is still not a pair, and is still counted even
    # though the drawn rows stop before it.
    assert offer["counts"]["excluded"] == len(specs) - len(LATE_PAIRS)
    assert "XAUUSD" not in {item["broker_symbol"] for item in offer["choices"]}


def test_the_catalog_offer_states_a_bounded_fallback_honestly(tmp_path):
    """A names-only wrapper: the read stops early and says how much it missed."""

    # The CFDs sort *after* the pairs here, so the pairs are inside the names the
    # bounded read reaches -- and the read still says it stopped early.
    specs = {name: full_pair(name, base, quote) for name, base, quote in LATE_PAIRS}
    specs["XAUUSD"] = {**full_cfd("XAUUSD"), "currency_base": "XAU", "currency_profit": "USD"}
    for index in range(400):
        specs[f"ZZ{index:04d}"] = full_cfd(f"ZZ{index:04d}")
    panel = CatalogPanel(root=tmp_path, terminal_provider=lambda: NameOnlyTerminal(specs))

    offer = panel.fx_choices()

    counts = offer["counts"]
    assert offer["bulk"] is False
    assert offer["truncated"] is True
    assert counts["reported"] == len(specs)
    assert counts["examined"] == 300
    assert counts["not_examined"] == len(specs) - 300
    # The pairs sort inside the names that were actually read.
    assert counts["forex_pairs"] == len(LATE_PAIRS)


def test_the_catalog_offer_reads_the_real_adapter_without_writing(tmp_path):
    specs = broker_symbols()
    fake = FakeMT5(symbols=specs, default_symbol=False)
    terminal = connected_terminal(fake)
    fake.symbol_select_calls.clear()
    panel = CatalogPanel(root=tmp_path, terminal_provider=lambda: terminal)

    offer = panel.fx_choices()

    assert offer["counts"]["reported"] == len(specs)
    assert offer["counts"]["not_examined"] == 0
    assert {item["broker_symbol"] for item in offer["choices"]} == expected_pairs(specs)
    assert "EURUSD.pro" in {item["broker_symbol"] for item in offer["choices"]}
    assert fake.symbol_select_calls == []


def test_the_catalog_offer_caps_only_what_it_draws(tmp_path):
    pairs = [(f"PAIR{index:03d}", "EUR", "USD") for index in range(100)]
    specs = catalog_specs(cfds=250, pairs=pairs)
    panel = CatalogPanel(root=tmp_path, terminal_provider=lambda: BulkTerminal(specs))

    offer = panel.fx_choices()
    counts = offer["counts"]

    assert counts["forex_pairs"] == 100  # every pair is counted
    assert len(offer["choices"]) == MAX_CHOICES_SHOWN  # only 60 are drawn
    assert counts["choices_shown"] == MAX_CHOICES_SHOWN
    assert counts["excluded"] == 251  # 250 CFDs and one metal
    assert len(offer["excluded"]) == CATALOG_MAX_EXCLUDED_SHOWN
    assert counts["excluded_shown"] == CATALOG_MAX_EXCLUDED_SHOWN


def test_the_catalog_page_says_how_many_pairs_it_is_not_drawing(tmp_path):
    from axiom_forex.workstation import views
    from tests.test_catalog_view import _app as catalog_app

    pairs = [(f"PAIR{index:03d}", "EUR", "USD") for index in range(100)]
    specs = catalog_specs(cfds=250, pairs=pairs)
    panel = CatalogPanel(root=tmp_path, terminal_provider=lambda: BulkTerminal(specs))
    app = catalog_app(panel)

    html = views.render_page(app._context("quick-setup", "Quick setup"))
    text = " ".join(html.split())

    assert "100 currency pair(s) of 351 reported symbol(s)" in text
    assert "first 12 of the 60 this page carries" in text
    assert "and 241 more, all counted" in text


# ---------------------------------------------------------------------------
# 7. the generic reader (config/terminal_facts.py) reads the whole list
# ---------------------------------------------------------------------------


class BulkReader:
    """A read-only terminal that answers status() and one bulk symbol call."""

    kind = "MT5_TERMINAL"
    simulated = False

    def __init__(self, specs: dict[str, dict]) -> None:
        self.specs = specs
        self.bulk_calls = 0

    def status(self):
        from types import SimpleNamespace

        return SimpleNamespace(
            connected=True,
            company="Example Broker",
            account_server="Example-Server",
            account_fingerprint="f00dbabe",
            account_currency="USD",
            leverage=200,
            account_mode="DEMO",
        )

    def symbol_snapshots(self):
        self.bulk_calls += 1
        return [dict(values) for values in self.specs.values()]

    def symbol_names(self):
        return sorted(self.specs)

    def symbol_info(self, name):
        raise AssertionError("a bulk answer must not be followed by per-name reads")


def test_the_research_reader_reads_every_reported_symbol_in_one_call():
    specs = catalog_specs(cfds=1500, pairs=LATE_PAIRS)
    reader = BulkReader(specs)

    facts = observe_terminal(reader)

    assert facts.symbols_bulk is True
    assert reader.bulk_calls == 1
    assert facts.symbols_seen == len(specs)
    assert facts.symbols_examined == len(specs)
    assert facts.not_examined == 0
    assert facts.symbols_truncated is False
    late = facts.symbol("EURUSD.pro")
    assert late is not None and late.pair == "EUR/USD"
    assert facts.symbol("AUDUSD").pair == "AUD/USD"
    view = facts.audit_view()
    assert view["symbols_reported"] == len(specs)
    assert view["symbols_examined"] == len(specs)
    assert view["symbols_truncated"] is False


def test_the_research_reader_says_what_a_names_only_wrapper_did_not_reach():
    specs = catalog_specs(cfds=400, pairs=LATE_PAIRS)

    class NamesOnly(BulkReader):
        def symbol_info(self, name):
            return self.specs[name]

    NamesOnly.symbol_snapshots = None  # type: ignore[assignment]

    facts = observe_terminal(NamesOnly(specs))

    assert facts.symbols_bulk is False
    assert facts.symbols_seen == len(specs)
    assert facts.symbols_examined == 300
    assert facts.not_examined == len(specs) - 300
    assert facts.symbols_truncated is True
    assert any("only 300 of" in note and "were read" in note for note in facts.notes)


def test_an_explicit_limit_is_honoured_on_the_bulk_path_and_reported():
    """A caller bound still works, and the count of what was reported stays true."""

    specs = catalog_specs(cfds=1500, pairs=LATE_PAIRS)
    facts = observe_terminal(BulkReader(specs), symbol_limit=10)

    assert facts.symbols_bulk is True  # one call, not ten
    assert facts.symbols_examined == 10
    assert facts.symbols_seen == len(specs)
    assert facts.not_examined == len(specs) - 10
    assert facts.symbols_truncated is True
    assert any("limited to 10 of" in note for note in facts.notes)


def test_the_research_reader_reads_the_real_adapter_without_writing():
    specs = broker_symbols()
    fake = FakeMT5(symbols=specs, default_symbol=False)
    terminal = connected_terminal(fake)
    fake.symbol_select_calls.clear()

    facts = observe_terminal(terminal)

    assert facts.symbols_bulk is True
    assert facts.symbols_seen == len(specs)
    assert facts.not_examined == 0
    assert {item.broker_symbol for item in facts.symbols} == set(specs)
    assert facts.symbol("EURUSD.pro").pair == "EUR/USD"
    assert fake.symbol_select_calls == []


# ---------------------------------------------------------------------------
# 8. the status snapshot counts what the terminal reported, not what it carried
# ---------------------------------------------------------------------------


def test_the_status_snapshot_reports_the_true_symbol_count():
    from axiom_forex.workstation.session import MAX_SYMBOLS

    specs = {f"AA{index:04d}": full_cfd(f"AA{index:04d}") for index in range(300)}
    source = MT5ObservationSource(settings=mt5_settings(), module=FakeMT5(symbols=specs, default_symbol=False))
    session = ObservationSession(source)
    session.connect()

    snapshot = source.snapshot()

    assert snapshot["symbols_reported"] == 300
    assert len(snapshot["symbols"]) == MAX_SYMBOLS
    assert snapshot["symbols_carried"] == MAX_SYMBOLS
    assert session.symbols_reported() == 300
    assert len(session.symbols()) == MAX_SYMBOLS


def test_the_status_snapshot_of_a_disconnected_source_claims_no_symbols():
    source = MT5ObservationSource(settings=mt5_settings(), module=FakeMT5())
    assert ObservationSession(source).symbols_reported() == 0


def test_the_provider_page_shows_the_reported_count_not_the_carried_names():
    from tests.test_workstation_ui import FakeLiveTerminalSource

    session = live_session()

    class ManySymbols(FakeLiveTerminalSource):
        def snapshot(self):
            snapshot = dict(super().snapshot())
            snapshot["symbols_reported"] = 2048
            snapshot["symbols_carried"] = len(snapshot.get("symbols") or ())
            return snapshot

    session.source = ManySymbols()
    app = app_for(live_wizard(session=session))
    app.act("connect", {"nonce": [app.nonce]})

    html = render(app, "/provider-setup")
    text = " ".join(html.split())

    assert "Symbols in Market Watch" in text
    assert "2048" in text
    assert "2 names carried here" in text
