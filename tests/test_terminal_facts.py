"""What the terminal reports, what it does not, and how each is labelled.

Pinned here:

* a value read from the connected terminal is ``OBSERVED``; the offline sample is
  ``SIMULATED``; anything the terminal did not answer is ``UNKNOWN`` with the
  reason recorded -- never a zero, never a guess;
* a pair is derived only when the terminal reported *both* legs as real currency
  codes, never by splitting a symbol name;
* a pip is derived from the broker's own digits and point, and labelled derived;
* swap is usable only in the modes this build can price; interest modes and a
  missing mode stay unknown, with the effect stated;
* reading never raises: a broken reader, a broken status, or a broken symbol
  record each produce a labelled result;
* no credential, login, balance, or equity appears anywhere in the result.

Every value below comes from a fake reader.  No terminal was contacted.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from axiom_forex.config.terminal_facts import (
    OBSERVED,
    SIMULATED,
    UNKNOWN,
    UNKNOWN_NOTES,
    TerminalFacts,
    observe_terminal,
    symbol_facts,
)


class Info:
    """A broker symbol record, as the terminal's symbol_info returns it."""

    def __init__(self, **overrides) -> None:
        values = dict(
            name="EURUSD.pro",
            description="Euro vs US Dollar",
            currency_base="EUR",
            currency_profit="USD",
            currency_margin="USD",
            digits=5,
            point=Decimal("0.00001"),
            trade_contract_size=Decimal("100000"),
            volume_min=Decimal("0.01"),
            volume_max=Decimal("100"),
            volume_step=Decimal("0.01"),
            stops_level=20,
            spread=12,
            trade_mode=4,
            swap_long=Decimal("-0.62"),
            swap_short=Decimal("0.18"),
            swap_mode=1,
            swap_rollover3days=3,
        )
        values.update(overrides)
        for name, value in values.items():
            setattr(self, name, value)


class Status:
    def __init__(self, **overrides) -> None:
        values = dict(
            connected=True,
            company="Example Broker",
            account_server="Example-Server",
            account_fingerprint="f00dbabe",
            account_currency="USD",
            leverage=200,
            account_mode="DEMO",
        )
        values.update(overrides)
        for name, value in values.items():
            setattr(self, name, value)


class Reader:
    def __init__(self, infos=None, status=None, fail_symbols=False) -> None:
        self.kind = "MT5_TERMINAL"
        self.simulated = False
        self._status = status or Status()
        self._infos = infos if infos is not None else {"EURUSD.pro": Info()}
        self.fail_symbols = fail_symbols

    def status(self):
        return self._status

    def symbol_names(self):
        if self.fail_symbols:
            raise RuntimeError("no symbols")
        return list(self._infos)

    def symbol_info(self, name):
        return self._infos[name]


# -- symbols ---------------------------------------------------------------


def test_a_complete_symbol_record_is_observed_field_by_field():
    facts = symbol_facts(Info())
    assert facts.state == OBSERVED
    assert facts.broker_symbol == "EURUSD.pro"
    assert facts.pair == "EUR/USD"
    assert facts.point == Decimal("0.00001")
    assert facts.contract_size == Decimal("100000")
    assert facts.pip_size == Decimal("0.0001")  # derived from 5 digits
    assert facts.missing() == ()
    assert facts.swap_state == "POINTS"
    view = facts.audit_view()
    assert view["swap_state"] == "POINTS"
    assert Decimal(view["pip_size"]) == Decimal("0.0001")


def test_a_pair_is_never_split_out_of_a_symbol_name():
    facts = symbol_facts(Info(name="EURUSD.pro", currency_base="", currency_profit=""))
    assert facts.pair == ""
    assert "pair" in facts.missing()
    assert "quote_currencies" in facts.missing()

    odd = symbol_facts(Info(currency_base="EUR", currency_profit="XBT"))
    assert odd.pair == ""
    assert "pair" in odd.missing()


def test_missing_fields_stay_unknown_instead_of_defaulting_to_zero():
    facts = symbol_facts(
        Info(point=None, trade_contract_size=None, volume_min=None, swap_mode=None)
    )
    assert facts.point is None
    assert facts.contract_size is None
    assert facts.pip_size is None
    assert facts.swap_state == UNKNOWN
    for name in ("point", "contract_size", "volume_min", "swap_mode"):
        assert name in facts.missing()


@pytest.mark.parametrize(
    "mode,expected",
    [
        (1, "POINTS"),
        (2, "CURRENCY"),
        (3, "CURRENCY"),
        (4, "CURRENCY"),
        (5, "INTEREST_MODE"),
        (6, "INTEREST_MODE"),
        (0, "DISABLED"),
        (None, UNKNOWN),
    ],
)
def test_swap_mode_decides_whether_swap_can_be_priced(mode, expected):
    assert symbol_facts(Info(swap_mode=mode)).swap_state == expected


def test_swap_without_reported_rates_is_unknown_even_in_a_priceable_mode():
    facts = symbol_facts(Info(swap_long=None, swap_short=None, swap_mode=1))
    assert facts.swap_state == UNKNOWN
    assert "swap_rates" in facts.missing()


def test_pip_size_follows_the_broker_digits():
    assert symbol_facts(Info(digits=5)).pip_size == Decimal("0.0001")
    assert symbol_facts(Info(digits=3, point=Decimal("0.001"))).pip_size == Decimal("0.01")
    assert symbol_facts(Info(digits=2, point=Decimal("0.01"))).pip_size == Decimal("0.01")
    assert symbol_facts(Info(digits=4, point=Decimal("0.0001"))).pip_size == Decimal("0.0001")


# -- one read of the terminal ----------------------------------------------


def test_observe_terminal_reads_account_facts_and_symbols():
    facts = observe_terminal(Reader())
    assert facts.connected is True
    assert facts.state == OBSERVED
    assert facts.currency.state == OBSERVED
    assert facts.currency.value == "USD"
    assert facts.leverage.value == "200"
    assert facts.account_mode.value == "DEMO"
    assert facts.symbol("EURUSD.pro") is not None
    assert facts.pairs() == ("EUR/USD",)
    assert facts.ready_for_research() == ()
    assert facts.notes == UNKNOWN_NOTES


def test_the_result_holds_no_login_password_balance_or_equity():
    status = Status()
    status.login = 12345678
    status.password = "hunter2"
    status.balance = 10000.0
    status.equity = 10123.45
    facts = observe_terminal(Reader(status=status))
    text = repr(facts.audit_view()).lower()
    for forbidden in ("12345678", "hunter2", "balance", "equity", "password"):
        assert forbidden not in text


def test_a_simulated_reader_is_marked_simulated_everywhere():
    reader = Reader()
    reader.simulated = True
    facts = observe_terminal(reader)
    assert facts.simulated is True
    assert facts.state == SIMULATED
    assert facts.currency.state == SIMULATED
    assert facts.symbol("EURUSD.pro").state == SIMULATED


def test_a_missing_terminal_is_reported_not_raised():
    facts = observe_terminal(None)
    assert facts.connected is False
    assert facts.state == UNKNOWN
    assert facts.error == "no_terminal"
    assert facts.ready_for_research()


def test_a_broken_status_read_is_reported_by_type_only():
    class Broken(Reader):
        def status(self):
            raise ValueError("connection reset by peer")

    facts = observe_terminal(Broken())
    assert facts.error == "ValueError"
    assert "reset" not in repr(facts.audit_view())


def test_a_broken_symbol_list_keeps_the_account_facts_it_already_read():
    facts = observe_terminal(Reader(fail_symbols=True))
    assert facts.connected is True
    assert facts.symbols == ()
    assert facts.error.startswith("symbols_unavailable")
    assert facts.currency.value == "USD"


def test_one_unreadable_symbol_does_not_lose_the_others():
    class Partial(Reader):
        def symbol_info(self, name):
            if name == "GBPUSD.pro":
                raise RuntimeError("nope")
            return Info(name=name)

    reader = Partial(infos={"EURUSD.pro": Info(), "GBPUSD.pro": Info()})
    facts = observe_terminal(reader)
    names = {item.broker_symbol: item for item in facts.symbols}
    assert names["EURUSD.pro"].state == OBSERVED
    assert names["GBPUSD.pro"].state == UNKNOWN
    assert "symbol_info" in names["GBPUSD.pro"].missing()


def test_symbol_reading_is_bounded():
    infos = {f"SYM{index}": Info(name=f"SYM{index}") for index in range(30)}
    facts = observe_terminal(Reader(infos=infos), symbol_limit=5)
    assert len(facts.symbols) == 5


def test_research_blockers_name_what_is_missing():
    facts = TerminalFacts(connected=True, symbols=())
    blockers = facts.ready_for_research()
    assert any("no symbols" in item for item in blockers)

    no_pair = TerminalFacts(
        connected=True, symbols=(symbol_facts(Info(currency_base="", currency_profit="")),)
    )
    assert any("pair" in item for item in no_pair.ready_for_research())

    offline = TerminalFacts()
    assert any("no terminal is connected" in item for item in offline.ready_for_research())
