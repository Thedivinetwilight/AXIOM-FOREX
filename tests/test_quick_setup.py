"""The short setup: five answers, observed facts, and two different readinesses.

Pinned here:

* research readiness needs observed symbols and specs plus the operator's own
  limits -- a missing commission or calendar does **not** block it, because those
  are labelled assumptions the results carry;
* live readiness is stricter: commission, calendar, rollover and conversion still
  come from the broker-terms document, and unpriceable swap blocks it;
* every recorded field keeps its provenance, unknown values stay unknown, and the
  saved record contains no credential-bearing field;
* the record round-trips through JSON (atomic write) and refuses the wrong schema;
* the operator's lot size is checked against the broker's own minimum.

The terminal here is a fake.  No account, terminal, or order is touched.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from axiom_forex.config.quick_setup import (
    ASSUMED,
    DEFAULT_WINDOW_DAYS,
    ESTIMATE,
    SCHEMA,
    CostAssumptions,
    QuickChoices,
    QuickSetupError,
    compose,
    load_quick_setup,
    summarise,
)
from axiom_forex.config.terminal_facts import UNKNOWN, TerminalFacts, symbol_facts


class Info:
    def __init__(self, **overrides) -> None:
        values = dict(
            name="EURUSD.pro",
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
        )
        values.update(overrides)
        for name, value in values.items():
            setattr(self, name, value)


def facts_for(*infos, **status) -> TerminalFacts:
    from axiom_forex.config.terminal_facts import Fact, OBSERVED

    return TerminalFacts(
        source_kind="MT5_TERMINAL",
        connected=status.get("connected", True),
        simulated=status.get("simulated", False),
        company="Example Broker",
        symbols=tuple(symbol_facts(info) for info in (infos or (Info(),))),
        currency=Fact("account_currency", "USD", OBSERVED, "terminal.account_info"),
        leverage=Fact("account_leverage", "200", OBSERVED, "terminal.account_info"),
        account_mode=Fact("account_mode", "DEMO", OBSERVED, "terminal.account_info"),
    )


def choices_for(**overrides) -> QuickChoices:
    values = dict(
        symbols=("EURUSD.pro",),
        lot_size=Decimal("0.10"),
        max_daily_loss_account=Decimal("50"),
        max_order_notional_account=Decimal("2000"),
        max_spread_pips=Decimal("2"),
    )
    values.update(overrides)
    return QuickChoices(**values)


def form(**overrides):
    values = {
        "symbols": ["EURUSD.pro"],
        "lot_size": "0.10",
        "max_daily_loss_account": "50",
        "max_order_notional_account": "2000",
        "max_spread_pips": "2",
    }
    values.update(overrides)
    return values


# -- what the operator answers ---------------------------------------------


def test_the_form_only_carries_what_the_terminal_could_not_observe():
    choices = QuickChoices.from_form(form())
    assert choices.symbols == ("EURUSD.pro",)
    assert choices.lot_size == Decimal("0.10")
    assert choices.max_open_positions == 1
    assert choices.window_days == DEFAULT_WINDOW_DAYS
    assert choices.problems() == ()


def test_the_form_refuses_an_incomplete_set_of_answers():
    empty = QuickChoices.from_form({})
    problems = empty.problems()
    assert any("symbol" in item for item in problems)
    assert any("lot size" in item for item in problems)
    assert any("daily loss" in item for item in problems)
    assert any("notional" in item for item in problems)


def test_the_window_is_kept_sane():
    assert QuickChoices.from_form(form(window_days="7")).window_days == 30
    assert QuickChoices.from_form(form(window_days="5000")).window_days == 1095
    assert QuickChoices.from_form(form(window_days="abc")).window_days == DEFAULT_WINDOW_DAYS


def test_duplicate_symbols_are_collapsed_and_blank_ones_dropped():
    choices = QuickChoices.from_form(form(symbols=["EURUSD.pro", " EURUSD.pro ", "", None]))
    assert choices.symbols == ("EURUSD.pro",)


# -- readiness -------------------------------------------------------------


def test_research_is_ready_from_observed_facts_without_the_broker_document():
    setup = compose(facts_for(), choices_for())
    readiness = setup.readiness()
    assert readiness["research"]["ready"] is True
    assert readiness["research"]["blockers"] == []
    # Live is not, and says exactly what is missing.
    assert readiness["live"]["ready"] is False
    assert any("commission" in item for item in readiness["live"]["blockers"])


def test_research_is_blocked_while_nothing_is_connected():
    setup = compose(facts_for(connected=False, simulate=None), choices_for())
    blockers = setup.research_blockers()
    assert any("connect to your terminal" in item for item in blockers)


def test_research_is_blocked_when_the_chosen_symbol_was_never_reported():
    setup = compose(facts_for(), choices_for(symbols=("XAUUSD.pro",)))
    assert any("none of the chosen symbols" in item for item in setup.research_blockers())


def test_research_is_blocked_when_the_pair_or_volume_grid_is_unknown():
    setup = compose(
        facts_for(Info(currency_base="", currency_profit="")), choices_for()
    )
    assert any("pair cannot be named" in item for item in setup.research_blockers())

    setup = compose(facts_for(Info(volume_min=None, volume_step=None)), choices_for())
    assert any("volume grid is unknown" in item for item in setup.research_blockers())


def test_a_lot_below_the_broker_minimum_is_refused():
    setup = compose(facts_for(), choices_for(lot_size=Decimal("0.001")))
    blockers = setup.research_blockers()
    assert any("below the broker minimum" in item for item in blockers)


def test_unpriceable_swap_blocks_live_but_not_research():
    setup = compose(facts_for(Info(swap_mode=5)), choices_for())
    assert setup.readiness()["research"]["ready"] is True
    assert any("swap is" in item for item in setup.live_blockers())


# -- the record ------------------------------------------------------------


def test_every_recorded_field_keeps_its_provenance():
    unknown_currency = TerminalFacts(connected=True, symbols=(symbol_facts(Info()),))
    setup = compose(unknown_currency, choices_for())
    rows = {label: (value, state) for label, value, state, _ in summarise(setup)}
    assert rows["Account currency"] == ("unknown", UNKNOWN)
    assert rows["Leverage"][1] == UNKNOWN

    observed = compose(facts_for(), choices_for())
    rows = {label: (value, state) for label, value, state, _ in summarise(observed)}
    assert rows["Account currency"] == ("USD", "OBSERVED")
    assert rows["Leverage"] == ("200", "OBSERVED")


def test_cost_assumptions_are_labelled_rather_than_presented_as_measured():
    view = CostAssumptions().audit_view()
    assert view["commission_per_lot"]["state"] == ASSUMED
    assert view["slippage_pips"]["state"] == ESTIMATE
    assert "optimistic" in view["commission_per_lot"]["effect"]
    assert [str(value) for value in CostAssumptions().sensitivity_multipliers] == ["1", "2"]


def test_the_saved_record_round_trips_and_carries_no_credential(tmp_path):
    setup = compose(facts_for(), choices_for(), CostAssumptions(commission_per_lot=Decimal("7")))
    saved = setup.save(tmp_path / "quick_setup.local.json")
    assert saved["fingerprint"] == setup.fingerprint()

    text = (tmp_path / "quick_setup.local.json").read_text().lower()
    for forbidden in ("password", "login", "token", "secret"):
        assert forbidden not in text
    payload = json.loads(text)
    assert payload["schema"] == SCHEMA

    loaded = load_quick_setup(tmp_path / "quick_setup.local.json")
    assert loaded.choices.symbols == ("EURUSD.pro",)
    assert loaded.choices.lot_size == Decimal("0.10")
    assert loaded.costs.commission_per_lot == Decimal("7")
    assert loaded.readiness()["research"]["ready"] is True
    assert loaded.symbol("EURUSD.pro").swap_state == "POINTS"
    assert loaded.path.endswith("quick_setup.local.json")


def test_a_recorded_setup_with_a_different_schema_is_refused(tmp_path):
    path = tmp_path / "quick_setup.local.json"
    path.write_text(json.dumps({"schema": "axiom-quick-setup-v0"}))
    with pytest.raises(QuickSetupError) as error:
        load_quick_setup(path)
    assert error.value.reason == "wrong_schema"


def test_a_missing_or_broken_file_is_refused_with_a_named_reason(tmp_path):
    with pytest.raises(QuickSetupError) as missing:
        load_quick_setup(tmp_path / "nope.json")
    assert missing.value.reason == "quick_setup_missing"

    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    with pytest.raises(QuickSetupError) as malformed:
        load_quick_setup(broken)
    assert malformed.value.reason == "quick_setup_malformed"


def test_the_fingerprint_changes_when_a_choice_changes():
    base = compose(facts_for(), choices_for())
    other = compose(facts_for(), choices_for(lot_size=Decimal("0.20")))
    assert base.fingerprint() != other.fingerprint()
    assert base.fingerprint() == compose(facts_for(), choices_for()).fingerprint()
