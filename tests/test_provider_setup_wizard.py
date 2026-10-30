"""The guided provider setup: what it collects, what it refuses, what it writes.

The rule this file defends: a setup document contains only terms the operator
supplied or the terminal proved, it is validated by the same loader the runner
uses, it is never able to carry a credential, and it is local.

Everything here is synthetic: a fake MetaTrader 5 module supplies the account and
symbol facts, and the values in the forms are made up for the test.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from axiom_forex.brokers.mt5.catalogue import SymbolRequest, build_catalogue
from axiom_forex.brokers.mt5.terminal import MT5Terminal
from axiom_forex.config import provider_setup_wizard as wizard
from axiom_forex.config.mt5 import MT5Settings
from axiom_forex.config.provider_setup import (
    SCHEMA,
    ProviderSetupError,
    load_provider_setup,
)
from axiom_forex.workstation.app import WorkstationApp
from axiom_forex.workstation.session import ObservationSession
from axiom_forex.workstation.setup import SetupWizard
from tests.fake_mt5 import FakeMT5
from tests.test_workstation_ui import offline_environment, simulated_wizard

PAIR = "EUR/USD"


def fake_terminal() -> MT5Terminal:
    terminal = MT5Terminal(
        module=FakeMT5(),
        settings=MT5Settings(
            terminal_path="terminal64.exe",
            server="Broker-Demo",
            account_login=55500123,
            symbol_suffix=".pro",
        ),
    )
    terminal.connect()
    return terminal


class TerminalSource:
    source_id = "mt5-terminal"
    kind = "LIVE_TERMINAL"
    label = "MetaTrader 5 terminal"

    def __init__(self, terminal):
        self.terminal = terminal

    def connect(self) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def snapshot(self) -> dict:
        return {
            "connected": True,
            "account": {
                "currency": "USD",
                "leverage": 100,
                "server": "Broker-Demo",
                "margin_mode": "RETAIL_HEDGING",
            },
            "symbols": [{"name": "EURUSD.pro", "tradable": True, "digits": 5}],
        }

    def market_data(self, freshness=None):
        return "market"


@pytest.fixture
def terminal():
    instance = fake_terminal()
    try:
        yield instance
    finally:
        instance.disconnect()


@pytest.fixture
def session(terminal):
    instance = ObservationSession(source=TerminalSource(terminal))
    instance.connect()
    return instance


def complete_form() -> dict[str, str]:
    """A form a competent operator would submit.  Every value is made up."""

    return {
        "account.account_id": "test-account-label",
        "account.initial_capital": "10000",
        "account.conversion_source": "OPERATOR_DECLARED_RATES",
        "account.entry_exposure_limit.amount": "1000",
        "account.entry_exposure_limit.currency": "USD",
        "account.entry_exposure_limit.version": "cap-v1",
        "account.entry_exposure_limit.source": "OPERATOR_PROVIDER_SETUP",
        "costs.model_id": "test-costs",
        "costs.version": "v1",
        "costs.commission_rate": "0",
        "costs.slippage_pips": "0.2",
        "costs.financing_rate_daily": "0",
        "costs.source": "BROKER_SCHEDULE",
        "rollover.schedule_id": "test-swap",
        "rollover.version": "v1",
        "rollover.daily_cutoff_utc": "21:00",
        "rollover.triple_rollover_weekday": "2",
        "rollover.source": "BROKER_SCHEDULE",
        "risk.max_order_notional_account": "1000",
        "risk.max_gross_exposure_account": "2000",
        "risk.max_margin_account": "1000",
        "risk.max_abs_currency_exposure_account": "1500",
        "risk.max_correlation_group_exposure_account": "1500",
        "risk.max_open_positions": "1",
        "risk.max_daily_loss_account": "200",
        "risk.max_trades_per_day": "3",
        "risk.max_spread_pips": "1.5",
        "risk.max_leverage": "30",
        "risk.limits_version": "limits-v1",
        "calendar.calendar_id": "test-calendar",
        "calendar.version": "v1",
        "calendar.source": "BROKER_CONTRACT_SPEC",
        "calendar.timezone_name": "UTC",
        "calendar.valid_from": "2026-01-01T00:00:00Z",
        "calendar.valid_until": "2026-12-31T23:59:59Z",
        "calendar.windows": (
            f"{PAIR} | 2026-01-05T00:00:00Z | 2026-01-09T23:59:59Z | week one"
        ),
        "calendar.holidays": f"{PAIR} | 2026-12-25",
        "execution.quantity": "0.10",
        "execution.max_open_positions": "1",
        "execution.allow_short": "no",
        "symbols": f"{PAIR} | EURUSD.pro | 0.00001 | majors | 1 | spec-v1",
        "swap": f"{PAIR} | 1.5 | -3.2 | BROKER_SCHEDULE | v1",
        "conversion_rates": "",
    }


# ---------------------------------------------------------------------------
# 1. what is missing, and exactly which field
# ---------------------------------------------------------------------------


def test_an_empty_form_names_every_missing_term(session):
    payload, problems = wizard.validate({}, session=session)
    paths = {item.path for item in problems}
    for expected in (
        "account.account_id",
        "account.initial_capital",
        "costs.commission_rate",
        "costs.slippage_pips",
        "rollover.daily_cutoff_utc",
        "risk.max_order_notional_account",
        "calendar.valid_from",
        "calendar.windows",
        "symbols",
        "execution.quantity",
    ):
        assert expected in paths, expected
    assert all(item.kind == wizard.MISSING for item in problems)
    # The document is still returned, and carries no invented value: an absent
    # commission is absent, not zero.
    assert payload["costs"]["commission_rate"] is None


def test_the_terminal_fills_only_the_two_fields_it_can_prove(session):
    form = complete_form()
    del form["account.initial_capital"]  # operator-supplied, must stay missing
    payload, problems = wizard.build_payload(form, session=session)
    assert payload["account"]["currency"] == "USD"
    assert payload["account"]["leverage"] == "100"
    assert [item.path for item in problems] == ["account.initial_capital"]


def test_a_currency_that_contradicts_the_terminal_is_refused(session):
    form = complete_form()
    form["account.currency"] = "EUR"
    payload, problems = wizard.build_payload(form, session=session)
    assert any(item.path == "account.currency" for item in problems)
    assert "terminal wins" in problems[0].message
    assert payload["account"]["currency"] == "USD"


def test_without_a_terminal_the_terminal_fields_are_missing_not_guessed():
    payload, problems = wizard.build_payload(complete_form(), session=None)
    paths = {item.path for item in problems}
    assert "account.currency" in paths
    assert "account.leverage" in paths
    assert payload["account"]["currency"] == ""


def test_a_symbol_the_terminal_does_not_quote_is_refused(session):
    form = complete_form()
    form["symbols"] = "USD/TRY | | | majors | 1 | spec-v1"
    payload, problems = wizard.build_payload(form, session=session)
    assert any(item.path.startswith("symbols[0].broker_symbol") for item in problems)
    assert payload["symbols"] == []


def test_a_blank_broker_symbol_is_filled_from_the_terminal(session):
    form = complete_form()
    form["symbols"] = f"{PAIR} | EURUSD.pro | | majors | 1 | spec-v1"
    payload, problems = wizard.build_payload(form, session=session)
    assert problems == []
    assert payload["symbols"][0]["broker_symbol"] == "EURUSD.pro"
    assert payload["symbols"][0]["pip_size"] == "0.00001"
    assert payload["symbols"][0]["suffix"] == ".pro"


@pytest.mark.parametrize(
    "path,value,needle",
    [
        ("costs.commission_rate", "abc", "number"),
        ("risk.max_open_positions", "", "required"),
        ("calendar.valid_from", "2026-01-01", "timezone"),
        ("rollover.triple_rollover_weekday", "funday", "0-6"),
    ],
)
def test_invalid_values_are_named_rather_than_accepted(session, path, value, needle):
    form = complete_form()
    form[path] = value
    payload, problems = wizard.validate(form, session=session)
    assert problems, path
    assert any(item.path == path for item in problems), [item.path for item in problems]
    assert any(needle in item.message.lower() for item in problems)


# ---------------------------------------------------------------------------
# 2. credentials are refused, both ways
# ---------------------------------------------------------------------------


def test_a_credential_field_in_the_form_is_refused(session):
    form = complete_form()
    form["password"] = "hunter2"
    payload, problems = wizard.build_payload(form, session=session)
    assert any(item.path == "credentials" for item in problems)


def test_a_credential_in_the_document_is_never_written(tmp_path):
    payload, _ = wizard.build_payload(complete_form(), session=None)
    payload["account"]["password"] = "hunter2"
    target = tmp_path / wizard.SETUP_FILENAME
    with pytest.raises(wizard.SetupIncomplete):
        wizard.write_payload(payload, target)
    assert not target.exists()


# ---------------------------------------------------------------------------
# 3. the file is local, atomic, and never replaces anything by accident
# ---------------------------------------------------------------------------


def test_the_setup_file_is_written_atomically_and_only_once(tmp_path, session):
    payload, problems = wizard.validate(complete_form(), session=session)
    assert problems == []
    target = wizard.setup_path(tmp_path)
    assert target.name == "provider_setup.local.json"
    wizard.write_payload(payload, target)
    first = target.read_text(encoding="utf-8")
    assert json.loads(first)["schema"] == SCHEMA

    with pytest.raises(FileExistsError):
        wizard.write_payload({**payload, "execution": {"quantity": "9"}}, target)
    assert target.read_text(encoding="utf-8") == first

    wizard.write_payload({**payload, "execution": {"quantity": "9"}}, target, overwrite=True)
    assert json.loads(target.read_text(encoding="utf-8"))["execution"]["quantity"] == "9"


def test_the_mt5_settings_file_is_never_read_or_written(tmp_path, session):
    settings = tmp_path / "mt5_settings.local.json"
    settings.write_text('{"server": "Broker-Demo"}', encoding="utf-8")
    before = settings.read_bytes()
    payload, problems = wizard.validate(complete_form(), session=session)
    assert problems == []
    wizard.write_payload(payload, wizard.setup_path(tmp_path))
    assert settings.read_bytes() == before
    # And the generated document carries no trace of it.
    assert "Broker-Demo" not in (tmp_path / wizard.SETUP_FILENAME).read_text(encoding="utf-8")


def test_the_generated_file_is_ignored_by_git():
    ignore = Path(".gitignore").read_text(encoding="utf-8").splitlines()
    assert "*.local.json" in ignore
    assert wizard.SETUP_FILENAME.endswith(".local.json")


# ---------------------------------------------------------------------------
# 4. the written document really does build the provider lane
# ---------------------------------------------------------------------------


def test_a_complete_form_builds_a_loadable_document(tmp_path, session, terminal):
    payload, problems = wizard.validate(complete_form(), session=session)
    assert problems == []
    target = wizard.write_payload(payload, wizard.setup_path(tmp_path))
    setup = load_provider_setup(target)

    assert setup.account.account_currency == "USD"
    assert setup.account.leverage == Decimal("100")
    assert setup.pairs() == (PAIR,)
    assert setup.terms_for(PAIR).broker_symbol == "EURUSD.pro"
    assert setup.execution.quantity == Decimal("0.10")
    assert setup.entry_limit is not None

    terms = setup.terms_for(PAIR)
    report = build_catalogue(
        terminal,
        [
            SymbolRequest(
                pair=terms.pair,
                broker_symbol=terms.broker_symbol,
                suffix=terms.suffix,
                pip_size=terms.pip_size,
                calendar_id=terms.calendar_id,
                calendar_version=terms.calendar_version,
                correlation_group=terms.correlation_group,
                margin_multiplier=terms.margin_multiplier,
                spec_version=terms.spec_version,
                source=terms.source,
            )
        ],
    )
    assert report.complete, [item.audit_view() for item in report.refused]
    resolved = report.resolved_for(PAIR)
    assert resolved is not None
    assert resolved.broker_symbol == "EURUSD.pro"


def test_the_runner_loader_is_the_validator(tmp_path, session):
    payload, problems = wizard.build_payload(complete_form(), session=session)
    payload["costs"]["commission_rate"] = "not-a-number"
    assert wizard.check_payload(payload), "the runner's own loader must reject this"


# ---------------------------------------------------------------------------
# 5. the example is an example, and is not the onboarding template
# ---------------------------------------------------------------------------


def test_the_example_documents_the_real_schema_and_is_not_runnable():
    path = Path(wizard.EXAMPLE_PATH)
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == SCHEMA
    assert "NOT RUNNABLE" in str(payload.get("_status", ""))
    with pytest.raises(ProviderSetupError):
        load_provider_setup(path)
    # It is a different document from the provider onboarding template.
    template = json.loads(Path("profiles/provider_onboarding_template.json").read_text())
    assert template.get("format") != SCHEMA
    assert template.get("schema") != SCHEMA
    assert template.get("schema_version") != SCHEMA
    # The operator is sent to the page that actually exists, and nothing else.
    assert "provider-setup" in str(payload["_readme"])
    assert "axiom_forex.cli" not in path.read_text(encoding="utf-8")
    # No key anywhere is credential-shaped (the prose may *warn* about secrets,
    # but a field named like one would be refused by the loader).
    for name in _every_key(payload):
        assert name.lower() not in wizard.CREDENTIAL_KEYS


def _every_key(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _every_key(child)
    elif isinstance(value, list):
        for child in value:
            yield from _every_key(child)


# ---------------------------------------------------------------------------
# 6. the page
# ---------------------------------------------------------------------------


def setup_app(tmp_path: Path, session=None) -> WorkstationApp:
    return WorkstationApp(
        wizard=SetupWizard(
            environment=offline_environment(),
            session=session or simulated_wizard().session,
        ),
        platform=None,
        version="0.1.0",
        project_root=str(tmp_path),
        provider_setup_path="",
        runner_source="terminal",
    )


def test_the_page_renders_and_says_what_it_will_not_do(tmp_path):
    app = setup_app(tmp_path)
    status, html = app.page("/provider-setup")
    assert int(status) == 200
    assert "provider_setup.local.json" in html
    assert "mt5_settings.local.json" in html
    assert "Not connected" in html
    assert "you supply" in html
    assert "from terminal" in html


def test_saving_needs_the_nonce(tmp_path):
    app = setup_app(tmp_path)
    status, _, _ = app.act("provider-setup-save", {"account.account_id": ["x"]})
    assert int(status) == 403
    assert not (tmp_path / wizard.SETUP_FILENAME).exists()


def test_an_incomplete_save_writes_nothing_and_names_the_field(tmp_path):
    app = setup_app(tmp_path)
    status, redirect, message = app.act(
        "provider-setup-save", {"nonce": [app.nonce], "account.account_id": ["label"]}
    )
    assert int(status) == 303
    assert redirect == "/provider-setup"
    assert "Nothing was written" in message
    assert any(token in message for token in ("account.", "costs.", "calendar.", "risk."))
    assert not (tmp_path / wizard.SETUP_FILENAME).exists()
    _, html = app.page("/provider-setup")
    assert "Nothing was written" in html


def test_a_complete_save_writes_the_file_and_reports_the_next_steps(tmp_path, session):
    app = setup_app(tmp_path, session)
    form = {"nonce": [app.nonce], **complete_form()}
    status, redirect, message = app.act("provider-setup-save", form)
    assert int(status) == 303
    assert redirect == "/provider-setup"
    assert "Saved" in message
    target = tmp_path / wizard.SETUP_FILENAME
    assert target.exists()
    assert load_provider_setup(target) is not None
    assert app.provider_setup_path == str(target)
    _, html = app.page("/provider-setup")
    assert "valid" in html
    # The remaining blockers are the data ones, not the setup file.
    assert "no_imported_history" in html or "strategy_not_evaluated" in html


def test_a_second_save_needs_the_explicit_replace_flag(tmp_path, session):
    app = setup_app(tmp_path, session)
    form = {"nonce": [app.nonce], **complete_form()}
    app.act("provider-setup-save", form)
    first = (tmp_path / wizard.SETUP_FILENAME).read_text(encoding="utf-8")
    _, _, message = app.act("provider-setup-save", form)
    assert "already exists" in message
    assert (tmp_path / wizard.SETUP_FILENAME).read_text(encoding="utf-8") == first
    _, _, replaced = app.act("provider-setup-save", {**form, "overwrite": ["yes"]})
    assert "Saved" in replaced
