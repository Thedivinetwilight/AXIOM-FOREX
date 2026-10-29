"""MT5 local settings, credential storage, and operator surfaces.

Everything here runs offline: a fake vault stands in for the OS credential
store, the settings document is a temporary file, and no terminal is opened.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from axiom_forex.brokers.mt5 import (
    MASKED_DISPLAY,
    MT5PasswordStore,
    MT5Settings,
    MT5SettingsError,
    MT5SettingsFileError,
    MT5SettingsSurface,
    load_settings_file,
    make_password_store,
    mt5_settings_document,
)
from axiom_forex.config.vault import CredentialVault, SecureStorageUnavailable
from axiom_forex.config.mt5 import MT5ExecutionMode

PASSWORD = "S3cret-Terminal-Passw0rd!"


@dataclass
class FakeVault:
    """Minimal OS-vault stand-in that records every operation."""

    available_flag: bool = True
    values: dict[str, str] = field(default_factory=dict)
    writes: list[str] = field(default_factory=list)
    erases: list[str] = field(default_factory=list)

    def describe(self):
        return {
            "backend": "fake_os_vault" if self.available_flag else "unavailable",
            "secure": True,
            "plaintext_fallback": False,
            "service": "AXIOM-FOREX-MT5",
        }

    def available(self) -> bool:
        return self.available_flag

    def read(self, entry: str) -> str | None:
        return self.values.get(entry)

    def write(self, entry: str, value: str) -> None:
        if not self.available_flag:
            raise SecureStorageUnavailable("secure_storage_unavailable")
        self.writes.append(entry)
        self.values[entry] = value

    def erase(self, entry: str) -> bool:
        self.erases.append(entry)
        return self.values.pop(entry, None) is not None


@dataclass
class FakeAuditLog:
    events: list[dict] = field(default_factory=list)

    def record(self, event):
        self.events.append(dict(event))


def settings(**overrides) -> MT5Settings:
    values = dict(
        terminal_path="terminal64.exe",
        server="Broker-Demo",
        account_login=55500123,
        mode=MT5ExecutionMode.OBSERVE_ONLY,
        symbol_suffix=".pro",
    )
    values.update(overrides)
    return MT5Settings(**values)


@pytest.fixture
def vault():
    return FakeVault()


@pytest.fixture
def audit():
    return FakeAuditLog()


@pytest.fixture
def store(settings_unused=None):
    pass


# -- settings validation ----------------------------------------------------


def test_settings_are_valid_and_carry_no_secret():
    view = settings().audit_view()
    assert settings().validate().valid
    assert "password" not in view
    assert view["credential_storage"] == "os_credential_vault"
    assert view["plaintext_fallback"] is False
    assert view["trading_enabled"] is False


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"terminal_path": ""}, "invalid_terminal_path"),
        ({"terminal_path": "notes.txt"}, "invalid_terminal_path"),
        ({"server": ""}, "missing_server"),
        ({"server": "   "}, "missing_server"),
        ({"account_login": 0}, "invalid_account_login"),
        ({"account_login": "55500123"}, "invalid_account_login"),
        ({"mode": "SOMETIMES"}, "invalid_execution_mode"),
        ({"symbol_suffix": "pro"}, "symbol_suffix_must_start_with_dot"),
        ({"magic": -1}, "invalid_magic"),
        ({"deviation_points": -5}, "invalid_deviation"),
        ({"max_price_drift_points": -1}, "invalid_max_price_drift"),
        ({"timeout_ms": 0}, "invalid_timeout"),
    ],
)
def test_settings_validation_is_explicit(overrides, reason):
    result = settings(**overrides).validate()
    assert not result.valid
    assert reason in result.errors


# -- broker server identifiers ----------------------------------------------
#
# Real broker servers contain internal spaces ("Broker Name - Live").  The
# validator accepts those while still refusing values that could never match
# what a terminal reports: blanks, surrounding whitespace, and control or
# invisible characters.


@pytest.mark.parametrize(
    "value",
    [
        "Broker Demo",
        "Broker Name - Live",
        "Broker  Demo",  # two spaces is a different identifier; kept as written
        "A B C",
        "Broker-Demo",
        "Broker_Demo.1",
    ],
)
def test_server_with_internal_spaces_is_valid(value):
    candidate = settings(server=value)
    result = candidate.validate()
    assert result.valid, result.errors
    # The accepted string is preserved exactly: no trimming, collapsing, or
    # case folding, so it still describes the account the operator chose.
    assert candidate.server == value


@pytest.mark.parametrize(
    "value, reason",
    [
        ("", "missing_server"),
        (" ", "missing_server"),
        ("\t", "missing_server"),
        ("  Broker Demo", "server_padded"),
        ("Broker Demo  ", "server_padded"),
        ("\tBroker Demo", "server_padded"),
        ("Broker Demo\n", "server_padded"),
        ("Broker\tDemo", "server_control_character"),
        ("Broker\nDemo", "server_control_character"),
        ("Broker\rDemo", "server_control_character"),
        ("Broker\x00Demo", "server_control_character"),
        ("Broker\x1bDemo", "server_control_character"),
        ("Broker\u200bDemo", "server_control_character"),  # zero-width space
        ("Broker\u00a0Demo", "server_invalid_whitespace"),  # non-breaking space
        ("Broker\u2003Demo", "server_invalid_whitespace"),  # em space
    ],
)
def test_invalid_server_identifiers_are_refused(value, reason):
    result = settings(server=value).validate()
    assert not result.valid
    assert reason in result.errors


def test_server_must_be_a_string():
    for value in (None, 123, 55500123, ["Broker Demo"]):
        result = settings(server=value).validate()
        assert not result.valid
        assert "missing_server" in result.errors


def test_a_spaced_server_loads_from_the_local_document_exactly(tmp_path):
    document = {
        "terminal_path": "C:/Program Files/MetaTrader 5/terminal64.exe",
        "server": "Broker Name - Live",
        "account_login": 55500123,
        "mode": "OBSERVE_ONLY",
    }
    path = tmp_path / "mt5_settings.local.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    loaded = load_settings_file(path)
    assert loaded is not None
    assert loaded.server == "Broker Name - Live"
    assert loaded.validate().valid
    # The boundary spaces of a two-space identifier survive the round trip.
    document["server"] = "Broker  Demo"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert load_settings_file(path).server == "Broker  Demo"


def test_a_padded_server_in_the_document_fails_to_load(tmp_path):
    path = tmp_path / "mt5_settings.local.json"
    path.write_text(
        json.dumps(
            {
                "terminal_path": "C:/Program Files/MetaTrader 5/terminal64.exe",
                "server": " Broker Demo",
                "account_login": 55500123,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(MT5SettingsFileError) as caught:
        load_settings_file(path)
    assert "server_padded" in str(caught.value)


def test_spaced_server_matches_only_the_same_server_at_the_terminal():
    """Account matching compares the declared value; it never rewrites it."""

    from axiom_forex.brokers.mt5.terminal import MT5Terminal, MT5TerminalUnavailable
    from tests.fake_mt5 import FakeMT5

    declared = settings(server="Broker Name - Live")
    matching = MT5Terminal(declared, module=FakeMT5(account_server="Broker Name - Live"))
    status = matching.connect()
    assert status.account_server == "Broker Name - Live"
    assert declared.server == "Broker Name - Live"

    for reported in ("Broker-Name-Live", "Broker Name Live", "Broker Name - Live 2"):
        mismatched = MT5Terminal(
            declared, module=FakeMT5(account_server=reported)
        )
        with pytest.raises(MT5TerminalUnavailable) as caught:
            mismatched.connect()
        assert caught.value.reason == "mt5_server_mismatch"
        # A refused connection leaves nothing open.
        assert mismatched.connected is False


def test_credential_entry_is_still_keyed_by_server_and_login_for_spaced_names():
    first = settings(server="Broker Name - Live").credential_entry()
    assert first == "terminal_login:broker name - live:55500123"
    assert settings(server="Broker Name Live").credential_entry() != first
    assert settings(server="Broker Name - Live", account_login=55500999).credential_entry() != first


def test_local_terminal_path_is_redacted_in_reports(tmp_path):
    local = tmp_path / "terminal64.exe"
    local.write_text("")
    view = settings(terminal_path=str(local)).audit_view()
    assert view["terminal_path"] == "<local-terminal-path>"
    assert str(tmp_path) not in json.dumps(view)


def test_credential_entry_is_bound_to_server_and_login():
    first = settings().credential_entry()
    assert first == "terminal_login:broker-demo:55500123"
    assert settings(account_login=55500999).credential_entry() != first
    assert settings(server="Other-Server").credential_entry() != first


def test_trading_mode_gate_fails_closed():
    with pytest.raises(Exception) as observe:
        settings(mode=MT5ExecutionMode.OBSERVE_ONLY).assert_trading_allowed()
    assert getattr(observe.value, "reason", "") == "mt5_trading_not_enabled"
    settings(mode=MT5ExecutionMode.TRADING_ENABLED).assert_trading_allowed()


# -- password store ---------------------------------------------------------


def test_password_round_trip_never_exposes_the_value(vault, audit):
    store = MT5PasswordStore(settings=settings(), vault=vault, audit_log=audit)
    assert store.configured() is False
    status = store.save(PASSWORD)
    assert status["configured"] is True
    assert status["display"] == MASKED_DISPLAY
    assert status["placeholder"] == "Configured"
    assert PASSWORD not in json.dumps(status, default=str)
    assert PASSWORD not in json.dumps(audit.events, default=str)
    assert vault.values[store.settings.credential_entry()] == PASSWORD
    cleared = store.clear()
    assert cleared["configured"] is False
    assert cleared["display"] == ""
    assert PASSWORD not in json.dumps(cleared, default=str)


def test_password_audit_events_do_not_carry_credentials(vault, audit):
    store = MT5PasswordStore(settings=settings(), vault=vault, audit_log=audit)
    store.save(PASSWORD)
    store.clear()
    kinds = [event["event_type"] for event in audit.events]
    assert kinds == ["MT5_PASSWORD_SAVED", "MT5_PASSWORD_CLEARED"]
    for event in audit.events:
        assert event["storage"] == "os_credential_vault"
        assert event["live"] is False
        assert PASSWORD not in json.dumps(event, default=str)
        # The field is named; no field *value* is ever a credential.
        assert "value" not in event and "password" not in event


def test_unavailable_vault_refuses_before_validating(vault_settings=None):
    broken = FakeVault(available_flag=False)
    audit = FakeAuditLog()
    store = MT5PasswordStore(settings=settings(), vault=broken, audit_log=audit)
    with pytest.raises(SecureStorageUnavailable) as excinfo:
        store.save(PASSWORD)
    assert excinfo.value.reason == "secure_storage_unavailable"
    assert store.configured() is False
    assert store.status()["configured"] is False
    assert store.status()["secure_storage"]["available"] is False
    assert PASSWORD not in json.dumps(store.status(), default=str)
    assert audit.events[-1]["event_type"] == "MT5_PASSWORD_REFUSED"
    assert broken.writes == []


def test_clear_on_unavailable_vault_is_refused_too():
    broken = FakeVault(available_flag=False)
    store = MT5PasswordStore(settings=settings(), vault=broken)
    with pytest.raises(SecureStorageUnavailable):
        store.clear()


@pytest.mark.parametrize(
    "value, reason",
    [
        ("", "empty_password"),
        (None, "invalid_password_type"),
        ("short\ninjected", "password_contains_control_characters"),
        ("x" * 2000, "password_too_long"),
    ],
)
def test_invalid_passwords_are_refused(vault, value, reason):
    store = MT5PasswordStore(settings=settings(), vault=vault)
    with pytest.raises(MT5SettingsError) as excinfo:
        store.save(value)
    assert excinfo.value.reason == reason
    assert vault.values == {}


def test_replace_reports_replace_action(vault, audit):
    store = MT5PasswordStore(settings=settings(), vault=vault, audit_log=audit)
    store.save("first-password")
    store.save("second-password")
    assert audit.events[-1]["action"] == "replace"
    assert store.secret_for_login() == "second-password"
    login_events = [e for e in audit.events if e["event_type"] == "MT5_PASSWORD_USED_FOR_LOGIN"]
    assert len(login_events) == 1
    assert "second-password" not in json.dumps(login_events, default=str)


def test_secret_for_login_requires_a_configured_password(vault, audit):
    store = MT5PasswordStore(settings=settings(), vault=vault, audit_log=audit)
    with pytest.raises(MT5SettingsError) as excinfo:
        store.secret_for_login()
    assert excinfo.value.reason == "mt5_password_not_configured"
    assert audit.events[-1]["event_type"] == "MT5_PASSWORD_USE_BLOCKED"


def test_vault_protocol_is_satisfied_by_the_fake():
    assert isinstance(FakeVault(), CredentialVault)


# -- local settings document ------------------------------------------------


def test_settings_document_round_trips(tmp_path):
    path = tmp_path / "mt5_settings.local.json"
    path.write_text(json.dumps(mt5_settings_document(settings())), encoding="utf-8")
    loaded = load_settings_file(path)
    assert loaded is not None
    assert loaded.audit_view()["account_login"] == 55500123
    assert loaded.symbol_suffix == ".pro"


def test_settings_document_missing_is_not_an_error(tmp_path):
    assert load_settings_file(tmp_path / "absent.json") is None


def test_settings_document_with_a_password_is_refused(tmp_path):
    path = tmp_path / "mt5_settings.local.json"
    path.write_text(
        json.dumps(
            {
                "terminal_path": "terminal64.exe",
                "server": "Broker-Demo",
                "account_login": 55500123,
                "password": PASSWORD,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(MT5SettingsFileError) as excinfo:
        load_settings_file(path)
    assert excinfo.value.reason == "mt5_settings_file_contains_secret"
    assert PASSWORD not in str(excinfo.value)


def test_settings_document_unknown_key_is_refused(tmp_path):
    path = tmp_path / "mt5_settings.local.json"
    path.write_text(
        json.dumps({"server": "Broker-Demo", "account_login": 1, "surprise": 1}),
        encoding="utf-8",
    )
    with pytest.raises(MT5SettingsFileError) as excinfo:
        load_settings_file(path)
    assert excinfo.value.reason == "mt5_settings_file_unknown_key"


def test_settings_document_invalid_content_is_refused(tmp_path):
    path = tmp_path / "mt5_settings.local.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(MT5SettingsFileError) as excinfo:
        load_settings_file(path)
    assert excinfo.value.reason == "mt5_settings_file_unreadable"


def test_settings_document_invalid_settings_are_refused(tmp_path):
    path = tmp_path / "mt5_settings.local.json"
    path.write_text(
        json.dumps({"terminal_path": "", "server": "", "account_login": 0}),
        encoding="utf-8",
    )
    with pytest.raises(MT5SettingsFileError) as excinfo:
        load_settings_file(path)
    assert excinfo.value.reason == "mt5_settings_invalid"


# -- operator surface (bearer-gated) ---------------------------------------


def _surface(vault, token="operator-token"):
    return MT5SettingsSurface(
        settings=settings(),
        password_store=MT5PasswordStore(settings=settings(), vault=vault),
        api_bearer_token=token,
    )


@pytest.mark.parametrize(
    "presented, reason",
    [
        (None, "settings_locked_missing_token"),
        ("wrong", "settings_locked_invalid_token"),
    ],
)
def test_surface_requires_the_operator_bearer(vault, presented, reason):
    surface = _surface(vault)
    with pytest.raises(MT5SettingsError) as excinfo:
        surface.status(presented_token=presented)
    assert excinfo.value.reason == reason


def test_surface_locks_when_no_token_is_configured(vault):
    surface = _surface(vault, token="")
    with pytest.raises(MT5SettingsError) as excinfo:
        surface.status(presented_token="anything")
    assert excinfo.value.reason == "settings_locked_no_operator_token"


def test_surface_status_declares_no_broker_token(vault):
    payload = _surface(vault).status(presented_token="operator-token")
    assert payload["token_required_by_broker"] is False
    assert "no separate api token" in payload["token_note"].lower()
    assert payload["password"]["configured"] is False
    assert payload["valid"] is True


def test_surface_sets_and_clears_the_password(vault):
    surface = _surface(vault)
    payload = surface.set_password(PASSWORD, presented_token="operator-token")
    assert payload["configured"] is True
    assert PASSWORD not in json.dumps(payload, default=str)
    cleared = surface.clear_password(presented_token="operator-token")
    assert cleared["configured"] is False


def test_make_password_store_uses_the_supplied_vault(vault):
    store = make_password_store(settings(), vault=vault)
    assert store.vault is vault


# -- CLI --------------------------------------------------------------------


def test_cli_reports_not_configured_without_a_settings_file(tmp_path, capsys):
    from axiom_forex.cli import main

    code = main(["mt5", "--mt5-settings-path", str(tmp_path / "absent.json")])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == "NOT_CONFIGURED"
    assert payload["password_storage"] == "os_credential_vault"


def test_cli_set_password_reads_a_prompt_and_prints_no_secret(tmp_path, capsys, monkeypatch):
    import getpass

    from axiom_forex import cli
    from axiom_forex.cli import main

    path = tmp_path / "mt5_settings.local.json"
    path.write_text(json.dumps(mt5_settings_document(settings())), encoding="utf-8")
    vault = FakeVault()
    monkeypatch.setattr(cli, "default_vault", lambda: vault)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": PASSWORD)

    code = main(["mt5", "--mt5-settings-path", str(path), "--mt5-action", "set-password"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == "OK"
    assert payload["password"]["configured"] is True
    assert PASSWORD not in json.dumps(payload, default=str)
    assert list(vault.values.values()) == [PASSWORD]


def test_cli_set_password_refuses_mismatched_confirmation(tmp_path, capsys, monkeypatch):
    import getpass

    from axiom_forex import cli
    from axiom_forex.cli import main

    path = tmp_path / "mt5_settings.local.json"
    path.write_text(json.dumps(mt5_settings_document(settings())), encoding="utf-8")
    vault = FakeVault()
    monkeypatch.setattr(cli, "default_vault", lambda: vault)
    answers = iter([PASSWORD, "different"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))

    code = main(["mt5", "--mt5-settings-path", str(path), "--mt5-action", "set-password"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["reason"] == "password_confirmation_mismatch"
    assert vault.writes == []


def test_cli_clear_password(tmp_path, capsys, monkeypatch):
    from axiom_forex import cli
    from axiom_forex.cli import main

    path = tmp_path / "mt5_settings.local.json"
    path.write_text(json.dumps(mt5_settings_document(settings())), encoding="utf-8")
    vault = FakeVault()
    vault.values[settings().credential_entry()] = PASSWORD
    monkeypatch.setattr(cli, "default_vault", lambda: vault)

    code = main(["mt5", "--mt5-settings-path", str(path), "--mt5-action", "clear-password"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["password"]["configured"] is False
    assert PASSWORD not in json.dumps(payload, default=str)


def test_cli_refuses_a_settings_file_with_a_secret(tmp_path, capsys):
    from axiom_forex.cli import main

    path = tmp_path / "mt5_settings.local.json"
    path.write_text(json.dumps({"server": "S", "account_login": 1, "password": "leak"}))
    code = main(["mt5", "--mt5-settings-path", str(path)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["reason"] == "mt5_settings_file_contains_secret"
