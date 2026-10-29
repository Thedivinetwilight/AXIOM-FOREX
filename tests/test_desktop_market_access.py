"""Market Access Token setting: vault storage, masking, access control, redaction.

Every test uses a fake credential vault, so nothing here touches the machine's
real keychain and no real secret exists.  The token values used below are
obviously synthetic strings; they exist only to prove they never leak.

Offline and research-only: no network, no provider, no broker, no live path.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from axiom_forex import build_fixture_platform
from axiom_forex.api.wsgi import PROTECTED_PATHS
from desktop import DESKTOP_REQUESTS
from desktop.market_access import (
    MARKET_ACCESS_TOKEN_ENTRY,
    MASKED_DISPLAY,
    MarketAccessSettings,
    MarketAccessTokenError,
    MarketAccessTokenStore,
    SecureStorageUnavailable,
    SettingsAccessDenied,
)

#: Synthetic stand-ins.  Neither is, or could be, a real credential.
TOKEN_A = "fixture-token-aaaa-1111-not-a-real-secret"
TOKEN_B = "fixture-token-bbbb-2222-not-a-real-secret"


class FakeVault:
    """In-memory stand-in for the OS credential vault."""

    def __init__(
        self,
        *,
        available: bool = True,
        fail_write: bool = False,
        fail_read: bool = False,
        fail_erase: bool = False,
    ):
        self.entries: dict[str, str] = {}
        self.available_flag = available
        self.fail_write = fail_write
        self.fail_read = fail_read
        self.fail_erase = fail_erase
        self.calls: list[tuple[str, str]] = []

    # -- protocol -------------------------------------------------------
    def describe(self):
        return {
            "backend": "fake_vault" if self.available_flag else "unavailable",
            "secure": True,
            "plaintext_fallback": False,
            "service": "FAKE",
        }

    def available(self) -> bool:
        return self.available_flag

    def read(self, entry: str):
        self.calls.append(("read", entry))
        if self.fail_read:
            raise RuntimeError("read failed")
        return self.entries.get(entry)

    def write(self, entry: str, value: str) -> None:
        self.calls.append(("write", entry))
        if self.fail_write:
            raise SecureStorageUnavailable("secure_storage_write_failed", "Fake")
        if not self.available_flag:
            raise SecureStorageUnavailable("secure_storage_unavailable", "Fake")
        self.entries[entry] = value

    def erase(self, entry: str) -> bool:
        self.calls.append(("erase", entry))
        if self.fail_erase:
            raise SecureStorageUnavailable("secure_storage_erase_failed", "Fake")
        return self.entries.pop(entry, None) is not None

    # -- test helpers ---------------------------------------------------
    @property
    def writes(self) -> list[tuple[str, str]]:
        return [call for call in self.calls if call[0] == "write"]


def _store(vault: FakeVault, *, audit_log=None) -> MarketAccessTokenStore:
    return MarketAccessTokenStore(vault=vault, audit_log=audit_log)


def _settings(platform, vault: FakeVault | None = None) -> MarketAccessSettings:
    return platform.market_access_settings()


# ---------------------------------------------------------------------------
# save / replace / clear
# ---------------------------------------------------------------------------


def test_saving_stores_the_token_in_the_vault_and_only_reports_presence():
    vault = FakeVault()
    store = _store(vault)

    assert store.status()["configured"] is False
    assert store.status()["display"] == ""
    assert vault.writes == []

    status = store.save(TOKEN_A)

    assert vault.entries == {MARKET_ACCESS_TOKEN_ENTRY: TOKEN_A}
    assert status["configured"] is True
    assert status["display"] == MASKED_DISPLAY
    assert status["placeholder"] == "Configured"
    assert status["storage"] == "os_credential_vault"
    assert status["plaintext_fallback"] is False
    assert status["broker_trading_use"] is False
    assert status["used_for_requests"] is False
    # The value appears nowhere in the reported status.
    assert TOKEN_A not in json.dumps(status, default=str)


def test_replacing_overwrites_the_previous_value():
    vault = FakeVault()
    store = _store(vault)
    store.save(TOKEN_A)
    status = store.save(TOKEN_B)

    assert vault.entries == {MARKET_ACCESS_TOKEN_ENTRY: TOKEN_B}
    assert TOKEN_A not in vault.entries.values()
    assert status["configured"] is True
    assert TOKEN_B not in json.dumps(status, default=str)


def test_clearing_removes_the_token_and_is_idempotent():
    vault = FakeVault()
    store = _store(vault)
    store.save(TOKEN_A)

    status = store.clear()
    assert vault.entries == {}
    assert status["configured"] is False
    assert status["display"] == ""

    # Clearing again is the desired end state, not an error.
    again = store.clear()
    assert again["configured"] is False


def test_an_empty_or_unsafe_token_is_refused_without_touching_the_vault():
    vault = FakeVault()
    store = _store(vault)
    for bad, reason in (
        ("", "empty_token"),
        ("   ", "empty_token"),
        ("short", "token_too_short"),
        ("has space in it", "token_contains_whitespace"),
        ("tab\there-and-there", "token_contains_whitespace"),
        ("newline\nin-token-123", "token_contains_whitespace"),
        ("bell\x07in-token-123", "token_contains_control_characters"),
        ("x" * 5000, "token_too_long"),
        (None, "invalid_token_type"),
        (12345678, "invalid_token_type"),
        ("  padded-token-value  ", "token_has_surrounding_whitespace"),
    ):
        with pytest.raises(MarketAccessTokenError) as excinfo:
            store.save(bad)
        assert excinfo.value.reason == reason
    assert vault.writes == []
    assert store.status()["configured"] is False


# ---------------------------------------------------------------------------
# fail closed when secure storage is unavailable
# ---------------------------------------------------------------------------


def test_saving_is_refused_and_explained_when_no_vault_is_available():
    vault = FakeVault(available=False)
    store = _store(vault)

    status = store.status()
    assert status["configured"] is False
    assert status["secure_storage"]["available"] is False
    assert status["plaintext_fallback"] is False
    assert "credential vault" in status["secure_storage"]["reason"]

    with pytest.raises(SecureStorageUnavailable) as excinfo:
        store.save(TOKEN_A)
    assert excinfo.value.reason == "secure_storage_unavailable"
    # The explanation says why and offers a next step; it never echoes a value.
    assert "credential vault" in str(excinfo.value)
    assert TOKEN_A not in str(excinfo.value)

    assert vault.entries == {}
    assert vault.writes == []
    assert store.status()["configured"] is False


def test_a_failing_vault_write_is_refused_not_fallen_back(tmp_path):
    vault = FakeVault(fail_write=True)
    store = _store(vault)

    with pytest.raises(SecureStorageUnavailable, match="secure_storage_write_failed"):
        store.save(TOKEN_A)

    assert store.status()["configured"] is False
    # No plaintext escape hatch exists anywhere on disk or in the repository.
    assert not any(
        path.name.endswith((".env", ".json", ".txt", ".cfg", ".ini"))
        and "token" in path.name.lower()
        for path in Path.cwd().iterdir()
    )


def test_clearing_is_refused_when_the_vault_is_gone():
    vault = FakeVault()
    store = _store(vault)
    store.save(TOKEN_A)
    vault.available_flag = False

    with pytest.raises(SecureStorageUnavailable):
        store.clear()
    # The token is genuinely still there; pretending it was cleared would be a
    # lie about the security state.
    assert vault.entries == {MARKET_ACCESS_TOKEN_ENTRY: TOKEN_A}
    assert store.status()["configured"] is False  # fail closed on reporting


def test_a_vault_read_failure_reports_not_configured():
    vault = FakeVault()
    store = _store(vault)
    store.save(TOKEN_A)
    vault.fail_read = True
    status = store.status()
    assert status["configured"] is False
    assert status["display"] == ""


# ---------------------------------------------------------------------------
# access control
# ---------------------------------------------------------------------------


def test_the_settings_surface_requires_the_operator_token():
    vault = FakeVault()
    settings = MarketAccessSettings(store=_store(vault), api_bearer_token="operator-123")

    with pytest.raises(SettingsAccessDenied, match="settings_locked_missing_token"):
        settings.set_token(TOKEN_A, presented_token="")
    with pytest.raises(SettingsAccessDenied, match="settings_locked_invalid_token"):
        settings.set_token(TOKEN_A, presented_token="wrong")
    with pytest.raises(SettingsAccessDenied):
        settings.status(presented_token=None)
    with pytest.raises(SettingsAccessDenied):
        settings.clear_token(presented_token="operator-12")  # prefix is not enough

    assert vault.writes == []
    assert vault.entries == {}

    granted = settings.set_token(TOKEN_A, presented_token="operator-123")
    assert granted["configured"] is True
    assert settings.status(presented_token="operator-123")["configured"] is True
    assert settings.clear_token(presented_token="operator-123")["configured"] is False


def test_a_locked_settings_surface_cannot_be_used_at_all():
    settings = MarketAccessSettings(store=_store(FakeVault()), api_bearer_token="")
    with pytest.raises(SettingsAccessDenied, match="settings_locked_no_operator_token"):
        settings.status(presented_token="anything")


def test_the_http_setting_is_gated_by_the_api_bearer_and_accepts_no_writes(tmp_path):
    vault = FakeVault()
    platform = build_fixture_platform(
        database_path=str(tmp_path / "api.db"), credential_vault=vault
    )
    try:
        app = platform.create_wsgi_app()

        def call(method: str, path: str, *, auth: bool):
            captured: dict[str, object] = {}

            def start_response(status, headers):  # noqa: ANN001
                captured["status"] = status

            environ = {"REQUEST_METHOD": method, "PATH_INFO": path}
            if auth:
                environ["HTTP_AUTHORIZATION"] = (
                    "Bearer " + platform.runtime_settings.api_bearer_token
                )
            body = app(environ, start_response)
            return captured["status"], body[0].decode("utf-8")

        status, body = call("GET", "/settings/market-access", auth=False)
        assert status == "401 Unauthorized"
        assert "configured" not in body

        status, body = call("GET", "/settings/market-access", auth=True)
        assert status == "200 OK"
        payload = json.loads(body)
        assert payload["configured"] is False
        assert payload["display"] == ""

        # The read-only API cannot be used to deposit a secret.
        status, body = call("POST", "/settings/market-access", auth=True)
        assert status == "405 Method Not Allowed"
        assert json.loads(body)["error"] == "read_only_api"
        assert vault.writes == []

        assert "/settings/market-access" in PROTECTED_PATHS
        assert any(item.name == "market_access" for item in DESKTOP_REQUESTS)
    finally:
        platform.close()


# ---------------------------------------------------------------------------
# masked display
# ---------------------------------------------------------------------------


def test_the_display_is_a_fixed_mask_never_derived_from_the_value(tmp_path):
    vault = FakeVault()
    platform = build_fixture_platform(
        database_path=str(tmp_path / "mask.db"), credential_vault=vault
    )
    try:
        settings = platform.market_access_settings()
        settings.set_token(TOKEN_A, presented_token=platform.runtime_settings.api_bearer_token)
        first = platform.market_access_status()
        assert first["configured"] is True
        assert first["display"] == MASKED_DISPLAY == "*" * 8
        assert first["placeholder"] == "Configured"
        assert TOKEN_A[:4] not in first["display"]

        # A different token must produce exactly the same mask.
        settings.set_token(TOKEN_B, presented_token=platform.runtime_settings.api_bearer_token)
        second = platform.market_access_status()
        assert second["display"] == first["display"]
        assert second["configured"] is True
    finally:
        platform.close()


# ---------------------------------------------------------------------------
# redaction everywhere
# ---------------------------------------------------------------------------


def _all_surface_text(platform) -> dict[str, str]:
    app = platform.create_wsgi_app()
    token = platform.runtime_settings.api_bearer_token

    def get(path: str) -> str:
        def start_response(status, headers):  # noqa: ANN001
            return None

        body = app(
            {
                "REQUEST_METHOD": "GET",
                "PATH_INFO": path,
                "HTTP_AUTHORIZATION": "Bearer " + token,
            },
            start_response,
        )
        return body[0].decode("utf-8")

    return {
        "status": json.dumps(platform.status(), default=str),
        "desktop": get("/desktop"),
        "settings": get("/settings/market-access"),
        "status_api": get("/status"),
        "audit_api": get("/audit"),
        "dashboard": get("/dashboard"),
        "report": get("/report"),
        "viewmodel": json.dumps(platform.desktop_viewmodel(), default=str),
        "audit_events": json.dumps(platform.audit_events(limit=1000), default=str),
        "workflow": json.dumps(platform.workflow_status(), default=str),
    }


def test_the_token_value_never_reaches_an_api_surface_report_or_audit(tmp_path):
    vault = FakeVault()
    platform = build_fixture_platform(
        database_path=str(tmp_path / "redaction.db"), credential_vault=vault
    )
    try:
        platform.run_paper_workflow(workflow_id="market-access-redaction")
        settings = platform.market_access_settings()
        settings.set_token(TOKEN_A, presented_token=platform.runtime_settings.api_bearer_token)
        assert platform.market_access_status()["configured"] is True

        surfaces = _all_surface_text(platform)
        for name, text in surfaces.items():
            assert TOKEN_A not in text, name
            assert "fixture-token-aaaa" not in text, name

        # The setting is still described, just without the secret.
        assert "Market access setting" in surfaces["report"]
        assert "Not a broker trading credential" in surfaces["report"]
        assert json.loads(surfaces["settings"])["configured"] is True
        assert json.loads(surfaces["desktop"])["market_access"]["configured"] is True

        # Audit events record the action and the boolean only.
        saved = [
            event
            for event in platform.audit_events(limit=1000)
            if event.get("event_type") == "MARKET_ACCESS_TOKEN_SAVED"
        ]
        assert saved
        assert saved[0]["configured"] is True
        assert saved[0]["storage"] == "os_credential_vault"
        assert "purpose" in saved[0]
        assert TOKEN_A not in json.dumps(saved, default=str)

        # And the SQLite file itself holds no copy of the value.
        platform.repository.connection.commit()
        raw = (tmp_path / "redaction.db").read_bytes()
        assert TOKEN_A.encode() not in raw
        assert b"fixture-token-aaaa" not in raw
    finally:
        platform.close()


def test_clearing_and_blocked_use_are_audited_without_the_value(tmp_path):
    vault = FakeVault()
    platform = build_fixture_platform(
        database_path=str(tmp_path / "audit-clear.db"), credential_vault=vault
    )
    try:
        bearer = platform.runtime_settings.api_bearer_token
        settings = platform.market_access_settings()
        settings.set_token(TOKEN_A, presented_token=bearer)
        settings.clear_token(presented_token=bearer)
        with pytest.raises(MarketAccessTokenError, match="provider_not_selected"):
            platform.market_access.for_provider_request()

        events = platform.audit_events(limit=1000)
        types = [event.get("event_type") for event in events]
        assert "MARKET_ACCESS_TOKEN_SAVED" in types
        assert "MARKET_ACCESS_TOKEN_CLEARED" in types
        assert "MARKET_ACCESS_TOKEN_USE_BLOCKED" in types
        blocked = next(
            event
            for event in events
            if event.get("event_type") == "MARKET_ACCESS_TOKEN_USE_BLOCKED"
        )
        assert blocked["reason"] == "provider_not_selected"
        assert TOKEN_A not in json.dumps(events, default=str)

        # The token is gone from the vault and the mask is empty again.
        assert vault.entries == {}
        assert platform.market_access_status()["configured"] is False
    finally:
        platform.close()


def test_the_store_never_exposes_the_value_through_repr_or_errors():
    vault = FakeVault()
    store = _store(vault)
    store.save(TOKEN_A)
    assert TOKEN_A not in repr(store)
    assert TOKEN_A not in str(store.status())
    assert TOKEN_A not in json.dumps(store.status(), default=str)
    with pytest.raises(MarketAccessTokenError) as excinfo:
        store.for_provider_request()
    assert TOKEN_A not in str(excinfo.value)
    # There is no accessor that hands the secret back.
    assert not any(
        name in {"token", "value", "get_token", "read", "secret"}
        for name in dir(store)
    )


def test_a_credential_shaped_field_cannot_be_smuggled_into_an_audit_event():
    """Defence in depth: the audit builder copies only allow-listed fields."""

    class Recorder:
        def __init__(self):
            self.events = []

        def record(self, event):
            self.events.append(dict(event))
            return dict(event)

    recorder = Recorder()
    store = MarketAccessTokenStore(vault=FakeVault(), audit_log=recorder)
    store.save(TOKEN_A)
    # Even a careless caller cannot push the secret through the audit path.
    store._audit(
        "MARKET_ACCESS_TOKEN_TEST",
        token=TOKEN_A,
        value=TOKEN_A,
        secret=TOKEN_A,
        configured=True,
    )
    assert recorder.events
    assert TOKEN_A not in json.dumps(recorder.events, default=str)
    assert all(
        "token" not in json.dumps(event) or "market_access_token" in json.dumps(event)
        for event in recorder.events
    )
    # Only the known fields survive.
    assert set(recorder.events[-1]) <= {
        "event_type",
        "setting",
        "purpose",
        "broker_trading_use",
        "used_for_requests",
        "live",
        "action",
        "reason",
        "configured",
        "storage",
        "backend",
    }


# ---------------------------------------------------------------------------
# the setting is disconnected from provider traffic
# ---------------------------------------------------------------------------


def test_the_setting_never_reaches_a_data_provider_request(tmp_path):
    """A configured token changes nothing: workflows run, no vault read, no I/O."""

    vault = FakeVault()
    platform = build_fixture_platform(
        database_path=str(tmp_path / "disconnected.db"), credential_vault=vault
    )
    try:
        bearer = platform.runtime_settings.api_bearer_token
        platform.market_access_settings().set_token(TOKEN_A, presented_token=bearer)
        vault.calls.clear()

        result = platform.run_paper_workflow(workflow_id="market-access-disconnected")
        assert result.executed is True
        # The workflow never writes to the vault, and the only reads it makes are
        # this setting's own "is one configured?" display -- never a retrieval
        # for a request.
        assert [call for call in vault.calls if call[0] == "write"] == []
        assert {entry for call, entry in vault.calls if call == "read"} <= {
            MARKET_ACCESS_TOKEN_ENTRY
        }
        assert platform.market_access_status()["used_for_requests"] is False
        # Nothing on the data-provider side can even reach the credential.
        provider = platform.provider
        assert not hasattr(provider, "market_access")
        assert not hasattr(provider, "credential_vault")
        assert "market_access" not in getattr(provider, "__dict__", {})
    finally:
        platform.close()


def test_a_provider_selection_alone_still_does_not_enable_token_use(tmp_path):
    """Even with a provider selected, this milestone exposes no request path."""

    vault = FakeVault()
    store = MarketAccessTokenStore(
        vault=vault,
        provider_selected=True,
        provider_id="FUTURE_MARKET_DATA_PROVIDER",
    )
    store.save(TOKEN_A)
    with pytest.raises(MarketAccessTokenError, match="market_data_requests_disabled"):
        store.for_provider_request()


# ---------------------------------------------------------------------------
# the existing offline fixtures keep working without any token
# ---------------------------------------------------------------------------


def test_fixture_workflows_run_without_a_token(tmp_path):
    for name, vault in (
        ("empty", FakeVault()),
        ("unavailable", FakeVault(available=False)),
    ):
        platform = build_fixture_platform(
            database_path=str(tmp_path / f"no-token-{name}.db"), credential_vault=vault
        )
        try:
            assert platform.market_access_status()["configured"] is False
            result = platform.run_paper_workflow(workflow_id=f"no-token-{name}")
            assert result.executed is True
            assert result.health["status"] != "UNSAFE"
            assert platform.health().status == "HEALTHY"
            # Nothing was written to the vault by running a paper workflow.
            assert vault.writes == []
        finally:
            platform.close()


def test_the_default_vault_is_an_os_vault_without_a_plaintext_fallback():
    from desktop.market_access import KeyringCredentialVault

    vault = KeyringCredentialVault()
    described = vault.describe()
    assert described["secure"] is True
    assert described["plaintext_fallback"] is False
    assert described["backend"] in {"os_keyring", "unavailable"}

    store = MarketAccessTokenStore(vault=vault)
    status = store.status()
    assert status["storage"] == "os_credential_vault"
    assert status["plaintext_fallback"] is False
    # Whatever this machine offers, the store never claims a value it cannot
    # verify, and it never invents one.
    assert status["configured"] in {True, False}
    assert status["display"] in {"", MASKED_DISPLAY}
