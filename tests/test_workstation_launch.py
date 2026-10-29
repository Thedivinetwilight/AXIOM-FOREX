"""Regression tests for the workstation launcher's settings loading.

The bug these pin down: ``_load_settings`` imported ``load_mt5_settings``, which
does not exist, so the ``ImportError`` was swallowed and **every** launch fell
back to ``no_settings_document`` -- a valid settings file included. The tests
here prove the launcher now uses the real loader (``load_settings_file``) and
that a valid document produces the real observe-only session, while every bad
document stays blocked.

Nothing here contacts MetaTrader 5. The terminal entry point is replaced by a
tripwire, so any attempt to open a terminal during startup fails the test loudly
rather than reaching a broker.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom_forex.config.mt5 import MT5Settings
from axiom_forex.workstation import launch
from axiom_forex.workstation.session import ConnectionRefused

VALID_DOCUMENT = {
    "terminal_path": "C:/Program Files/MetaTrader 5/terminal64.exe",
    "server": "Broker Name - Live",
    "account_login": 55500123,
    "mode": "OBSERVE_ONLY",
    "use_existing_session": True,
}


def write_settings(tmp_path: Path, **overrides) -> Path:
    document = dict(VALID_DOCUMENT)
    document.update(overrides)
    path = tmp_path / "mt5_settings.local.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def no_terminal_contact(monkeypatch):
    """Fail loudly if startup tries to import or open a terminal."""

    def tripwire(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("the launcher must not contact MetaTrader 5 at startup")

    from axiom_forex.brokers.mt5 import terminal as terminal_module

    monkeypatch.setattr(terminal_module, "import_mt5", tripwire)
    yield


# ---------------------------------------------------------------------------
# the loader itself
# ---------------------------------------------------------------------------


def test_the_launcher_uses_the_loader_that_actually_exists():
    """The original bug: an import that could never succeed."""

    text = (Path(launch.__file__)).read_text(encoding="utf-8")
    assert "load_mt5_settings" not in text
    assert "load_settings_file" in text


def test_a_valid_document_loads(tmp_path):
    settings, failure = launch._load_settings(write_settings(tmp_path))

    assert failure == ""
    assert isinstance(settings, MT5Settings)
    assert settings.server == "Broker Name - Live"
    assert settings.account_login == 55500123
    assert settings.trading_enabled is False


def test_a_missing_document_is_reported_as_missing_not_as_an_import_error(tmp_path):
    settings, failure = launch._load_settings(tmp_path / "absent.json")

    assert settings is None
    assert failure == "no_settings_document"


@pytest.mark.parametrize(
    "contents, expected",
    [
        ("{not json", "mt5_settings_file_unreadable"),
        ("[]", "mt5_settings_file_not_an_object"),
        (
            json.dumps({**VALID_DOCUMENT, "password": "S3cret-Terminal-Passw0rd!"}),
            "mt5_settings_file_contains_secret",
        ),
        (
            json.dumps({**VALID_DOCUMENT, "account_login": 0}),
            "mt5_settings_invalid",
        ),
        (
            json.dumps({**VALID_DOCUMENT, "server": " Broker Name - Live"}),
            "mt5_settings_invalid",
        ),
        (
            json.dumps({**VALID_DOCUMENT, "unexpected_key": "x"}),
            "mt5_settings_file_unknown_key",
        ),
    ],
)
def test_bad_documents_are_refused_with_their_reason(tmp_path, contents, expected):
    path = tmp_path / "mt5_settings.local.json"
    path.write_text(contents, encoding="utf-8")

    settings, failure = launch._load_settings(path)

    assert settings is None
    assert failure == expected


def test_a_secret_in_the_document_is_never_echoed(capsys, tmp_path):
    secret = "S3cret-Terminal-Passw0rd!"
    path = write_settings(tmp_path)
    path.write_text(
        json.dumps({**VALID_DOCUMENT, "password": secret}), encoding="utf-8"
    )

    settings, failure = launch._load_settings(path)
    assert settings is None
    assert failure == "mt5_settings_file_contains_secret"
    assert secret not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# what startup builds from a valid document
# ---------------------------------------------------------------------------


def test_a_valid_document_constructs_the_real_observe_only_session(tmp_path):
    settings, failure = launch._load_settings(write_settings(tmp_path))

    session = launch._build_session(simulate=False, settings=settings, failure=failure)
    view = session.view()

    # The real terminal source -- not the simulator, not the locked stub.
    assert session.source.kind == "LIVE_TERMINAL"
    assert session.source.source_id == "mt5-terminal"
    assert session.source.settings.server == "Broker Name - Live"
    # Constructed, never connected: only an explicit Connect opens a terminal.
    assert view["state"] == "DISCONNECTED"
    assert view["connected"] is False
    assert view["connect_count"] == 0
    assert view["trading_capability"] == "none"
    assert view["auto_reconnect"] is False


def test_a_valid_document_also_works_through_the_path_argument(tmp_path):
    session = launch._build_session(simulate=False, settings_path=write_settings(tmp_path))

    assert session.source.kind == "LIVE_TERMINAL"
    assert session.view()["connected"] is False


def test_the_preview_ignores_settings_entirely(tmp_path):
    broken = tmp_path / "mt5_settings.local.json"
    broken.write_text("{not json", encoding="utf-8")

    session = launch._build_session(simulate=True, settings_path=broken)

    assert session.source.kind == "SIMULATED"
    assert session.view()["connected"] is False


# ---------------------------------------------------------------------------
# fail-closed behaviour for bad documents
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure, expected",
    [
        ("no_settings_document", "no_settings_document"),
        ("mt5_settings_file_contains_secret", "mt5_settings_file_contains_secret"),
        ("mt5_settings_invalid", "mt5_settings_invalid"),
        ("mt5_settings_file_unreadable", "mt5_settings_file_unreadable"),
        ("", "no_settings_document"),  # defensive: no settings and no reason
    ],
)
def test_a_bad_document_yields_a_locked_session_that_refuses_to_connect(failure, expected):
    session = launch._build_session(simulate=False, settings=None, failure=failure)

    assert session.source.kind == "UNCONFIGURED"
    result = session.connect()  # explicit attempt, as the Connect button would
    view = session.view()
    assert result["connected"] is False
    assert view["state"] == "ERROR"
    assert view["error"]["reason"] == expected
    assert view["connect_count"] == 0
    # The refusal explains itself without echoing any document value.
    assert view["error"]["detail"]
    assert view["error"]["hint"]
    assert session.source.label


def test_a_settings_file_with_a_secret_refuses_to_connect_and_says_why(tmp_path):
    path = write_settings(tmp_path)
    path.write_text(
        json.dumps({**VALID_DOCUMENT, "password": "S3cret-Terminal-Passw0rd!"}),
        encoding="utf-8",
    )
    settings, failure = launch._load_settings(path)

    session = launch._build_session(simulate=False, settings=settings, failure=failure)
    session.connect()

    error = session.view()["error"]
    assert error["reason"] == "mt5_settings_file_contains_secret"
    assert "credential" in error["detail"].lower()
    assert "vault" in error["hint"].lower()
    assert "S3cret" not in json.dumps(session.view(), default=str)


def test_a_padded_server_document_is_blocked_with_the_validator_reason(tmp_path):
    path = write_settings(tmp_path)
    path.write_text(
        json.dumps({**VALID_DOCUMENT, "server": " Broker Name - Live"}), encoding="utf-8"
    )
    settings, failure = launch._load_settings(path)

    assert (settings, failure) == (None, "mt5_settings_invalid")
    session = launch._build_session(simulate=False, settings=settings, failure=failure)
    session.connect()
    assert session.view()["connected"] is False


def test_the_locked_session_still_supports_the_old_call_shape():
    """Existing callers pass only a reason and keep the original wording."""

    from axiom_forex.workstation.session import locked_session

    session = locked_session("no_settings_document")
    with pytest.raises(ConnectionRefused):
        session.source.connect()
    session.connect()
    error = session.view()["error"]
    assert error["reason"] == "no_settings_document"
    assert "mt5_settings.local.json" in error["detail"]


# ---------------------------------------------------------------------------
# end to end: main() wiring, without starting a server or touching MT5
# ---------------------------------------------------------------------------


class _FakeServer:
    server_address = ("127.0.0.1", 8765)

    def __init__(self) -> None:
        self.served = False

    def serve_forever(self) -> None:
        self.served = True
        raise KeyboardInterrupt  # behave like an operator pressing Ctrl+C

    def shutdown(self) -> None:
        return None

    def server_close(self) -> None:
        return None


@pytest.fixture()
def harness(monkeypatch):
    """Capture the app main() builds, without binding a port."""

    captured: dict[str, object] = {}
    server = _FakeServer()

    def fake_make_server(app, **kwargs):  # noqa: ANN001, ANN003
        captured["app"] = app
        captured["kwargs"] = kwargs
        return server

    monkeypatch.setattr(launch, "make_server", fake_make_server)
    monkeypatch.setattr(launch, "_fixture_platform", lambda: None)
    monkeypatch.setattr(launch.webbrowser, "open", lambda url: None)
    return captured


def test_main_starts_with_the_real_session_when_the_document_is_valid(tmp_path, harness):
    path = write_settings(tmp_path)

    exit_code = launch.main(
        ["--settings", str(path), "--allow-non-windows", "--no-browser"]
    )

    assert exit_code == 0
    app = harness["app"]
    session = app.wizard.session
    assert session.source.kind == "LIVE_TERMINAL"
    assert session.source.settings.server == "Broker Name - Live"
    assert session.view()["connected"] is False
    # Step 1 now sees the settings that were actually loaded.
    assert app.wizard.environment.settings_present is True
    # And the preview-only framing escape hatch stays closed for a live session.
    assert app.allow_framing is False


def test_main_stays_locked_when_the_document_is_invalid(tmp_path, harness):
    path = write_settings(tmp_path)
    path.write_text("{not json", encoding="utf-8")

    exit_code = launch.main(
        ["--settings", str(path), "--allow-non-windows", "--no-browser"]
    )

    assert exit_code == 0  # the UI still opens; Connect is refused
    app = harness["app"]
    session = app.wizard.session
    assert session.source.kind == "UNCONFIGURED"
    session.connect()
    assert session.view()["connected"] is False
    assert session.view()["error"]["reason"] == "mt5_settings_file_unreadable"


def test_main_reports_a_missing_document_without_an_import_error(tmp_path, harness, capsys):
    missing = tmp_path / "nope.json"

    launch.main(["--settings", str(missing), "--allow-non-windows", "--no-browser"])

    err = capsys.readouterr().err
    assert "no_settings_document" in err
    assert "ImportError" not in err and "ModuleNotFoundError" not in err
    assert harness["app"].wizard.session.source.kind == "UNCONFIGURED"


def test_print_prerequisites_sees_a_valid_document(tmp_path, capsys):
    """The report and the session must agree about whether settings exist."""

    path = write_settings(tmp_path)
    exit_code = launch.main(["--settings", str(path), "--print-prerequisites"])

    out = capsys.readouterr().out
    # On a non-Windows box the prerequisite check still fails, but not for
    # settings: the document is found, parsed, and its values used.
    assert exit_code == 1
    assert "Everything needed to connect was found." not in out
    assert "Create mt5_settings.local.json" not in out
    assert "Local settings" not in out
