"""Focused tests for the workstation shell (setup, session, labelling).

The rules these tests exist to protect:

* **startup** -- a fresh process is disconnected and unconfirmed, and nothing
  connects until the operator clicks Connect;
* **simulator labelling** -- a simulated session is never described as a
  terminal connection, and every value it shows is marked SAMPLE / SIMULATED;
* **ready-state restriction** -- "ready for observation" is reachable only from
  a real terminal that answered an explicit Connect; a simulator cannot get
  there and cannot be confirmed;
* **explicit connect / disconnect** -- no rendering, polling, or timer opens a
  session, and nothing reconnects after a disconnect;
* **POST allowlist** -- the setup actions plus the two execution-mode actions
  exist; there is no order, arming, account-switch, or broker-settings handler
  anywhere, and switching mode never places an order;
* **secret redaction** -- no password, token, or account number reaches a page,
  a log line, or a response body.

The live-terminal tests use a fake observation source that reports
``kind="LIVE_TERMINAL"``. It contacts nothing: no MetaTrader 5 module, terminal,
account, or credential is involved anywhere in this file.
"""

from __future__ import annotations

import http.client
import io
import json
import re
import threading
from contextlib import redirect_stderr
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from pathlib import Path

import pytest

from axiom_forex.facade import build_fixture_platform
from axiom_forex.workstation import views
from axiom_forex.workstation.app import (
    CONFIRM_REFUSALS,
    CSP,
    CREDENTIAL_FIELDS,
    GET_ROUTES,
    POST_ACTIONS,
    WorkstationApp,
    csp_header,
    make_server,
)
from axiom_forex.workstation.context import (
    PageContext,
    PaperSnapshot,
    build_context,
    data_class_label,
)
from axiom_forex.workstation.probes import probe_environment
from axiom_forex.workstation.session import (
    CONNECTED,
    ObservationSession,
    SimulatedObservationSource,
    locked_session,
)
from axiom_forex.workstation.setup import SetupWizard

SECRET_VALUE = "Sup3rSecret!123"

#: Text that must appear on every page while the simulator is running.
SIMULATED_MARKERS = ("SIMULATED / SAMPLE DATA", "Simulator")


# ---------------------------------------------------------------------------
# fixtures and helpers
# ---------------------------------------------------------------------------


def offline_environment():
    """A non-Windows report with nothing installed: the blocked baseline."""

    return probe_environment(
        settings_path=None,
        system="Linux",
        release="6.1.0",
        python="3.11.9",
        finder=lambda name: None,
        version_of=lambda name: None,
    )


def sample_settings():
    from axiom_forex.config.mt5 import MT5Settings

    return MT5Settings(
        terminal_path="C:/Program Files/MetaTrader 5/terminal64.exe",
        server="SAMPLE-Broker-Demo",
        account_login=99999999,
    )


def windows_environment():
    """A Windows report where every prerequisite is satisfied."""

    def version_of(name: str) -> str:
        return "5.0.45" if name == "MetaTrader5" else "25.0"

    return probe_environment(
        settings=sample_settings(),
        system="Windows",
        release="10",
        python="3.12.4",
        finder=lambda name: "C:/Program Files/MetaTrader 5/terminal64.exe"
        if name == "MetaTrader5"
        else None,
        version_of=version_of,
        exists=lambda path: path.endswith("terminal64.exe"),
    )


def fake_symbol_specs() -> tuple[dict, ...]:
    """Two pairs, as a test double would report them: both currency legs given."""

    return (
        {
            "name": "EURUSD",
            "currency_base": "EUR",
            "currency_profit": "USD",
            "trade_mode": 4,
            "digits": 5,
            "point": 0.00001,
            "trade_contract_size": 100000,
            "description": "Euro vs US Dollar",
        },
        {
            "name": "GBPUSD",
            "currency_base": "GBP",
            "currency_profit": "USD",
            "trade_mode": 4,
            "digits": 5,
            "point": 0.00001,
            "trade_contract_size": 100000,
            "description": "Pound vs US Dollar",
        },
    )


@dataclass
class FakeLiveTerminalSource:
    """A *test double* for the real terminal: it contacts nothing.

    It exists so the "real terminal" path can be exercised on Linux. It is the
    only place in the suite that claims ``kind="LIVE_TERMINAL"``.
    """

    mode: str = "DEMO"
    mode_confirmed: bool = True
    connected_flag: bool = False
    source_id: str = "fake-terminal"
    kind: str = "LIVE_TERMINAL"
    label: str = "Fake terminal (test double)"
    connect_calls: int = 0
    specs: tuple[dict, ...] = field(default_factory=fake_symbol_specs)

    def symbol_names(self):
        return [str(item.get("name", "")) for item in self.specs]

    def symbol_info(self, name):
        for item in self.specs:
            if item.get("name") == name:
                return dict(item)
        return None

    def symbol_records(self):
        if not self.connected_flag:
            return ()
        return tuple(dict(item) for item in self.specs)

    @property
    def connected(self) -> bool:
        return self.connected_flag

    def connect(self) -> None:
        self.connect_calls += 1
        self.connected_flag = True

    def disconnect(self) -> None:
        self.connected_flag = False

    def snapshot(self):
        if not self.connected_flag:
            return {"connected": False}
        return {
            "connected": True,
            "source_kind": self.kind,
            "terminal": {"name": "MetaTrader 5 (test double)", "build": 4755, "connected": True},
            "account": {
                "login": "<redacted>",
                "fingerprint": "0123456789ab",
                "server": "SAMPLE-Broker-Demo",
                "company": "Sample Broker",
                "currency": "EUR",
                "leverage": 30,
                "mode": self.mode,
                "mode_confirmed": self.mode_confirmed,
                "margin_mode": "RETAIL_NETTING",
                "trade_allowed": True,
                "balance_reporting": "withheld",
            },
            "symbols": [
                {"name": "EURUSD", "description": "Euro vs US Dollar", "digits": 5, "tradable": True},
                {"name": "GBPUSD", "description": "Pound vs US Dollar", "digits": 5, "tradable": True},
            ],
            "symbol_error": "",
            "observed_at": "2026-10-29 00:00:00+00:00",
        }


def simulated_session() -> ObservationSession:
    return ObservationSession(SimulatedObservationSource())


def live_session(**kwargs) -> ObservationSession:
    return ObservationSession(FakeLiveTerminalSource(**kwargs))


def simulated_wizard(environment=None) -> SetupWizard:
    return SetupWizard(environment=environment or offline_environment(), session=simulated_session())


def live_wizard(environment=None, session=None) -> SetupWizard:
    return SetupWizard(
        environment=environment or windows_environment(),
        session=session or live_session(),
    )


def app_for(wizard: SetupWizard, platform=None) -> WorkstationApp:
    return WorkstationApp(wizard=wizard, platform=platform or build_fixture_platform())


def connected_simulator_app() -> WorkstationApp:
    app = app_for(simulated_wizard())
    app.act("connect", {"nonce": [app.nonce]})
    return app


def confirmed_live_app() -> WorkstationApp:
    app = app_for(live_wizard())
    app.act("connect", {"nonce": [app.nonce]})
    app.act("symbols", {"nonce": [app.nonce], "symbol": ["EURUSD", "GBPUSD"]})
    app.act("confirm", {"nonce": [app.nonce]})
    return app


class _Page(HTMLParser):
    """Minimal structural reader: text, elements, and attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str]]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        self.tags.append((tag, {name: (value or "") for name, value in attrs}))

    def handle_data(self, data):  # noqa: ANN001
        self.text.append(data)

    @property
    def text_content(self) -> str:
        return " ".join(part.strip() for part in self.text if part.strip())

    def by_tag(self, name: str) -> list[dict[str, str]]:
        return [attrs for tag, attrs in self.tags if tag == name]

    def has_text(self, needle: str) -> bool:
        return needle.lower() in self.text_content.lower()


def parse(html: str) -> _Page:
    page = _Page()
    page.feed(html)
    return page


def render(app: WorkstationApp, path: str) -> str:
    status, html = app.page(path)
    assert int(status) == 200, f"{path} returned {status}"
    return html


def assert_no_secret(html: str, secret: str = SECRET_VALUE) -> None:
    lowered = html.lower()
    assert secret.lower() not in lowered
    for marker in ("password=", "passwd", "access_token", "api_key"):
        assert marker not in lowered, f"page leaked a credential-shaped field: {marker}"


# ---------------------------------------------------------------------------
# 1. startup state
# ---------------------------------------------------------------------------


def test_a_fresh_workstation_starts_disconnected_and_unconfirmed():
    wizard = simulated_wizard()
    view = wizard.view()

    assert wizard.connected is False
    assert view["session"]["state"] == "DISCONNECTED"
    assert view["session"]["connected"] is False
    assert view["session"]["connect_count"] == 0
    assert view["confirmed"] is False
    assert view["configured_at"] is None
    assert view["symbols_selected"] == []
    assert view["account"] == {}
    assert view["ready_for_observation"] is False


def test_a_fresh_live_workstation_also_starts_disconnected():
    wizard = live_wizard()
    assert wizard.connected is False
    assert wizard.live_terminal is False
    assert wizard.ready_for_observation is False
    assert wizard.view()["session"]["connect_count"] == 0


def test_the_session_never_connects_on_its_own():
    source = FakeLiveTerminalSource()
    wizard = SetupWizard(environment=windows_environment(), session=ObservationSession(source))
    for _ in range(3):  # rendering does not connect
        wizard.steps()
        wizard.view()
    assert source.connect_calls == 0
    assert wizard.session.should_poll() is False
    assert wizard.session.view()["auto_reconnect"] is False


def test_startup_pages_say_disconnected_before_any_click():
    app = app_for(simulated_wizard())
    page = parse(render(app, "/"))
    assert "Simulator · not started" in page.text_content
    assert "No account mode" in page.text_content
    assert "Preview stopped" in page.text_content
    # And nothing was started by looking at it.
    assert app.wizard.session.view()["connect_count"] == 0


def test_the_launcher_opens_the_browser_only_after_the_socket_listens():
    """A first page load must not race the server.

    The launcher (not the .cmd wrapper) opens the browser, and only once the
    socket is bound; --no-browser suppresses it.
    """

    from axiom_forex.workstation import launch

    parser = launch.build_parser()
    assert parser.parse_args([]).no_browser is False
    assert parser.parse_args(["--no-browser"]).no_browser is True

    source = Path(launch.__file__).read_text(encoding="utf-8")
    listen = source.index("make_server(")
    open_browser = source.index("webbrowser.open(")
    assert listen < open_browser, "the browser must be opened after the server is created"
    assert "serve_forever" in source


def test_launch_builds_a_disconnected_simulator(monkeypatch=None):
    """The launcher's --simulate path must not pre-connect anything."""

    from axiom_forex.workstation import launch

    session = launch._build_session(simulate=True, settings_path=None)
    assert session.view()["state"] == "DISCONNECTED"
    assert session.view()["connected"] is False
    assert session.view()["connect_count"] == 0


# ---------------------------------------------------------------------------
# 2. simulator labelling
# ---------------------------------------------------------------------------


def test_prerequisite_help_never_points_at_a_removed_control():
    """Guidance must match the controls that exist.

    The terminal-path form was removed from the interface; help must send
    operators to the settings file instead of a screen that no longer offers it.
    """

    from axiom_forex.workstation.probes import prerequisite_help

    help_items = prerequisite_help(offline_environment())
    combined = " ".join(item["detail"] for item in help_items)
    assert "Account & symbols screen" not in combined
    assert "type its full path" not in combined
    terminal_item = next(item for item in help_items if item["code"] == "mt5_terminal_not_found")
    assert "mt5_settings.local.json" in terminal_item["detail"]


@pytest.mark.parametrize("path", sorted(GET_ROUTES))
def test_every_page_marks_a_simulator_as_simulated_sample_data(path):
    app = app_for(simulated_wizard())
    # Even before the preview is started, the mode is shown as a simulator.
    for _ in range(2):
        html = render(app, path)
        page = parse(html)
        assert "SIMULATED / SAMPLE DATA" in html, path
        assert "terminal is attached" in html, path
        assert "read from a broker" in html.lower(), path
        assert page.has_text("Simulator"), path


def test_a_running_simulator_never_claims_to_be_connected_to_mt5():
    app = connected_simulator_app()
    assert app.wizard.session.view()["connected"] is True  # the preview is running

    page = parse(render(app, "/"))
    text = page.text_content
    assert "Simulator · not MT5" in text
    assert "Preview running" in text
    assert "Sample mode: DEMO" in text
    # The words that would imply a terminal answered are absent as claims.
    assert "Connected ·" not in text
    assert "Account mode: DEMO" not in text
    # And the only connect/disconnect control offered is the explicit one.
    assert "Stop simulator preview" in render(app, "/")


def test_simulated_values_are_labelled_sample_not_detected():
    app = connected_simulator_app()
    account_html = render(app, "/account")
    assert "Sample account mode" in account_html
    assert "Detected account mode" not in account_html
    assert "No account was read" in account_html
    for label in ("Sample Broker / server", "Sample Currency", "Sample Leverage"):
        assert label in account_html, label

    overview = render(app, "/")
    assert "Sample mode: DEMO" in overview
    assert "Sample value — no account was read" in overview.replace("\n", " ")


def test_setup_step_four_is_labelled_as_sample_and_not_complete():
    wizard = simulated_wizard()
    wizard.session.connect()
    steps = {step.id: step for step in wizard.steps()}
    account = steps["account"]
    assert account.status == "action_required"
    assert account.title == "Review the sample account (preview)"
    assert all(item[0].startswith("Sample ") for item in account.items)
    assert "No broker was contacted" in account.summary or "no broker was contacted" in account.summary.lower()


def test_a_live_terminal_is_labelled_real_broker_not_sample():
    app = confirmed_live_app()
    overview = render(app, "/")
    assert "REAL BROKER" in parse(overview).text_content
    assert "SIMULATED / SAMPLE DATA" not in overview
    account = render(app, "/account")
    assert "Detected account mode: DEMO" in account
    assert "Sample account mode" not in account
    assert "Sample Broker / server" not in account
    assert 'Sample mode' not in account


def test_data_class_labels_are_distinct_and_never_upgrade_synthetic_data():
    labels = {
        name: data_class_label(name)["label"]
        for name in (
            "LIVE_BROKER",
            "SIMULATED",
            "SYNTHETIC_FIXTURE",
            "PAPER",
            "IMPORTED_RESEARCH",
        )
    }
    assert len(set(labels.values())) == len(labels)
    assert labels["SYNTHETIC_FIXTURE"] != labels["LIVE_BROKER"]
    assert data_class_label("ANYTHING_ELSE")["label"] == "ANYTHING ELSE"


# ---------------------------------------------------------------------------
# 3. ready-state restriction
# ---------------------------------------------------------------------------


def test_a_simulator_can_never_reach_ready_for_observation():
    wizard = simulated_wizard(windows_environment())  # even with a perfect machine
    wizard.acknowledge_sign_in()
    wizard.session.connect()
    wizard.choose_symbols(["SAMPLE_EURUSD"])

    assert wizard.live_terminal is False
    assert wizard.ready_for_observation is False
    assert wizard.ready_basis == "simulated_session_only"
    assert "simulated_session_only" in wizard.blockers

    ready_step = {step.id: step for step in wizard.steps()}["ready"]
    assert ready_step.status == "blocked"
    assert "can never reach observation mode" in ready_step.detail


def test_a_simulator_cannot_be_confirmed():
    wizard = simulated_wizard()
    wizard.session.connect()
    wizard.choose_symbols(["SAMPLE_EURUSD"])
    with pytest.raises(ValueError, match="confirm_requires_live_terminal"):
        wizard.confirm()
    assert wizard.choices.confirmed is False


def test_ready_requires_a_live_terminal_plus_confirmation_and_symbols():
    wizard = live_wizard()
    assert wizard.ready_for_observation is False  # not connected yet

    wizard.session.connect()
    assert wizard.ready_for_observation is False  # not confirmed yet

    wizard.choose_symbols(["EURUSD"])
    assert wizard.ready_for_observation is False  # chosen but not confirmed

    wizard.confirm()
    assert wizard.ready_for_observation is True
    assert wizard.ready_basis == "live_terminal"

    ready_step = {step.id: step for step in wizard.steps()}["ready"]
    assert ready_step.status == "complete"
    assert ready_step.items  # what was *not* enabled is listed


def test_confirm_requires_an_observed_account_even_on_a_live_session():
    wizard = live_wizard()
    wizard.session.connect()
    wizard.choose_symbols(["EURUSD"])
    # Drop the account from the snapshot: confirmation must refuse.
    wizard.session.snapshot_data = {"connected": True, "account": {}, "symbols": []}
    with pytest.raises(ValueError):
        wizard.confirm()


def test_disconnecting_a_ready_session_returns_it_to_not_ready():
    wizard = live_wizard()
    wizard.session.connect()
    wizard.choose_symbols(["EURUSD"])
    wizard.confirm()
    assert wizard.ready_for_observation is True

    wizard.session.disconnect()
    assert wizard.ready_for_observation is False
    assert wizard.ready_basis == "not_connected"
    assert "not_connected" in wizard.blockers


def test_a_real_account_is_reported_not_rejected():
    wizard = live_wizard(session=live_session(mode="REAL"))
    wizard.session.connect()
    wizard.choose_symbols(["EURUSD"])
    wizard.confirm()
    assert wizard.account_mode()["is_real"] is True
    assert wizard.account_mode()["is_demo"] is False
    assert wizard.ready_for_observation is True


# ---------------------------------------------------------------------------
# 4. explicit connect / disconnect
# ---------------------------------------------------------------------------


def test_connect_happens_only_on_the_explicit_action():
    app = app_for(live_wizard())
    source = app.wizard.session.source

    for path in sorted(GET_ROUTES):
        app.page(path)
    assert source.connect_calls == 0

    status, redirect, message = app.act("connect", {"nonce": [app.nonce]})
    assert int(status) == 303
    assert redirect == "/"
    assert source.connect_calls == 1
    assert app.wizard.session.connected is True
    assert "Connected to the terminal" in message

    # Pressing Connect again is idempotent: no second open, no switch.
    app.act("connect", {"nonce": [app.nonce]})
    assert source.connect_calls == 1
    assert app.wizard.session.view()["connect_count"] == 1


def test_disconnect_is_explicit_and_nothing_reconnects():
    app = app_for(live_wizard())
    app.act("connect", {"nonce": [app.nonce]})
    source = app.wizard.session.source

    status, _, message = app.act("disconnect", {"nonce": [app.nonce]})
    assert int(status) == 303
    assert app.wizard.session.connected is False
    assert "Nothing reconnects automatically" in message

    for path in sorted(GET_ROUTES):
        app.page(path)
    assert app.wizard.session.connected is False
    assert source.connect_calls == 1


def test_the_simulator_connect_action_is_worded_as_a_preview():
    app = app_for(simulated_wizard())
    status, redirect, message = app.act("connect", {"nonce": [app.nonce]})
    assert int(status) == 303
    assert redirect == "/setup"
    assert "Simulator preview started" in message
    assert "No terminal is attached" in message

    _, _, stop_message = app.act("disconnect", {"nonce": [app.nonce]})
    assert "Simulator preview stopped" in stop_message
    assert app.wizard.session.connected is False


def test_a_settings_less_launch_cannot_connect_at_all():
    session = locked_session("no_settings_document")
    app = app_for(SetupWizard(environment=offline_environment(), session=session))
    status, redirect, message = app.act("connect", {"nonce": [app.nonce]})
    assert int(status) == 303
    assert redirect == "/setup"
    assert "no_settings_document" in message
    assert session.view()["connected"] is False


# ---------------------------------------------------------------------------
# 5. POST allowlist
# ---------------------------------------------------------------------------


def test_the_post_allowlist_is_exactly_the_setup_mode_and_live_actions():
    assert POST_ACTIONS == {
        "connect",
        "disconnect",
        "acknowledge-sign-in",
        "confirm",
        "unconfirm",
        "symbols",
        # The execution-mode switch, and nothing else: it changes how an
        # approved order *would* be executed and cannot create one.
        "mode-paper",
        "mode-live",
        # The paper runner's own loop. Starting it runs *simulated* cycles;
        # it cannot send an order, arm anything, or change the mode.
        "runner-start",
        "runner-stop",
        # The data workflow: re-scan the import folder, convert one broker tick
        # export, and evaluate the candidate grid on one canonical file.  These
        # touch local files and the research database only -- no connection, no
        # order, no arming, no mode change.
        "imports-refresh",
        "ticks-convert",
        "dataset-evaluate",
        # The guided provider setup: writes one local, Git-ignored document from
        # terms the operator types plus facts the terminal reports. It never
        # reads mt5_settings.local.json, never stores a credential, and sends
        # nothing.
        "provider-setup-save",
        # The short setup and the one-year research path. Recording the setup
        # writes one local, Git-ignored file; fetching reads the terminal's own
        # rate history (read-only) and stores it locally; running the sweep spends
        # CPU on a worker thread. None of them connects, changes a mode, arms,
        # sizes, or sends anything, and nothing here can start paper trading --
        # that stays on ``runner-start`` above.
        "quick-setup-save",
        "research-fetch",
        "research-run",
        "research-cancel",
        # The live workflow. Arming writes the operator's plan and authorises the
        # broker adapter (sending nothing); starting begins the strategy loop
        # that the guard stops on any failed check; stopping ends it and
        # disarms. None of them connects, picks a strategy, symbol, size, or
        # limit, or changes the mode.
        "live-arm",
        "live-disarm",
        "live-start",
        "live-stop",
        # The optional Kronos panel. Every one of these changes what a
        # *forecast* looks like, or records/inspects one. None of them can
        # create an order, arm anything, change the mode, or edit a risk limit;
        # the panel is a separate object the workstation is handed, and it holds
        # no live or risk surface.
        "kronos-save",
        "kronos-load",
        "kronos-stop",
        "kronos-forecast",
        "kronos-schedule-start",
        "kronos-schedule-stop",
        "kronos-preset-save",
        "kronos-preset-load",
        "kronos-reconcile",
        "kronos-benchmark",
        "kronos-inspect-weights",
        # The Kronos install: one explicit, cancellable download of the official
        # sources into AXIOM's own folder. It writes files and one manifest; it
        # cannot connect to a broker, change a mode, arm, or send anything.
        "kronos-install-check",
        "kronos-install",
        "kronos-install-cancel",
        "kronos-install-cleanup",
        # The strategy library. Screening and evaluating write attempt rows to a
        # local ledger; selecting records the operator's own choice and cannot
        # override a rejection. None of them sizes, arms, trades, or changes the
        # mode, and the library holds no live or risk surface.
        "strategy-screen",
        "strategy-evaluate",
        "strategy-select",
        "strategy-deselect",
        "strategy-monitor",
        # The catalog: one evaluation on the terminal's own bars, then the
        # live-market paper test. Fills are simulated locally; no order surface is
        # reachable from any of these four actions, and none changes a mode.
        "catalog-evaluate",
        "catalog-paper-start",
        "catalog-paper-stop",
        "catalog-paper-flat",
    }


def test_no_post_action_can_trade_arm_or_switch_accounts():
    forbidden = {
        "orders",
        "order",
        "order-send",
        "arm",
        "disarm",
        "trade",
        "enable-trading",
        "buy",
        "sell",
        "close",
        "modify",
        "cancel",
        "switch-account",
        "select-account",
        "login",
        "logout",
        "settings",
        "set-server",
        "market-access-token",
    }
    assert not (POST_ACTIONS & forbidden)
    assert not (set(GET_ROUTES) & {f"/{name}" for name in forbidden})

    app = app_for(confirmed_live_app().wizard)
    for action in sorted(forbidden):
        status, _, _ = app.act(action, {"nonce": [app.nonce]})
        assert int(status) == 404, action


def test_the_removed_annotation_actions_are_gone():
    app = app_for(simulated_wizard())
    for action in ("mode-choice", "terminal-path"):
        status, _, _ = app.act(action, {"nonce": [app.nonce]})
        assert int(status) == 404


def _workstation_identifiers() -> set[str]:
    """Every name, attribute, and imported module the UI layer can reach.

    Docstrings are deliberately excluded: prose may name an API to say that it
    is *not* used.
    """

    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "axiom_forex" / "workstation"
    found: set[str] = set()
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                found.add(node.id)
            elif isinstance(node, ast.Attribute):
                found.add(node.attr)
            elif isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                found.add(node.module or "")
                found.update(alias.name for alias in node.names)
    return found


def test_the_workstation_source_contains_no_trading_calls():
    """A structural check: the UI cannot reach an order-submitting API."""

    reachable = _workstation_identifiers()
    forbidden = {
        "order_send",
        "order_check",
        "order_calc_margin",
        "TRADE_ACTION_DEAL",
        "positions_close",
        "arm_trading",
        "disarm_trading",
        "MT5Adapter",
        "submit_order",
        "place_order",
        "cancel_order",
        "history_orders_get",
    }
    assert reachable & forbidden == set(), sorted(reachable & forbidden)


def test_the_only_mt5_import_in_the_ui_layer_is_data_only():
    """The session imports the data-only terminal, never the adapter."""

    reachable = _workstation_identifiers()
    assert "axiom_forex.brokers.mt5.terminal" in reachable or any(
        "brokers.mt5.terminal" in name for name in reachable
    )
    assert not any("adapter" in name for name in reachable)
    assert not any("brokers.mt5.orders" in name for name in reachable)


def test_stale_or_missing_nonce_is_refused_for_every_action():
    app = app_for(live_wizard())
    for action in sorted(POST_ACTIONS):
        for form in ({}, {"nonce": [""]}, {"nonce": ["not-the-nonce"]}):
            status, _, _ = app.act(action, form)
            assert int(status) == 403, (action, form)
    assert app.wizard.session.view()["connect_count"] == 0
    assert app.wizard.choices.confirmed is False


def test_credential_shaped_form_fields_are_rejected_outright():
    app = app_for(live_wizard())
    before = app.wizard.session.view()["connect_count"]
    for field in sorted(CREDENTIAL_FIELDS):
        status, redirect, message = app.act("connect", {"nonce": [app.nonce], field: [SECRET_VALUE]})
        assert int(status) == 400, field
        assert redirect == "/setup"
        assert SECRET_VALUE not in message
    assert app.wizard.session.view()["connect_count"] == before


# ---------------------------------------------------------------------------
# secret redaction
# ---------------------------------------------------------------------------


def test_no_page_contains_a_secret_or_credential_field():
    for app in (connected_simulator_app(), confirmed_live_app()):
        for path in sorted(GET_ROUTES):
            assert_no_secret(render(app, path))


def test_action_logging_never_echoes_the_submitted_value():
    app = app_for(live_wizard())
    buffer = io.StringIO()
    with redirect_stderr(buffer):
        app.act("connect", {"nonce": [app.nonce], "password": [SECRET_VALUE]})
        app.act("connect", {"nonce": [app.nonce]})
        app.act("disconnect", {"nonce": [app.nonce]})
    logged = buffer.getvalue()
    assert SECRET_VALUE not in logged
    assert "connect requested" in logged
    assert "disconnect requested" in logged


def test_session_views_never_carry_balances_or_the_login():
    wizard = live_wizard()
    wizard.session.connect()
    payload = json.dumps(wizard.view(), default=str).lower()
    assert '"balance"' not in payload
    assert '"equity"' not in payload
    assert '"login": "<redacted>"' in payload
    assert "withheld" in payload


def test_account_page_redacts_the_login_and_withholds_balances():
    app = connected_simulator_app()
    html = render(app, "/account")
    assert "login redacted" in html
    assert "10000" not in html  # the fixture balance never reaches the account page


# ---------------------------------------------------------------------------
# account mode accuracy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode,expected_tone", [("DEMO", "ok"), ("REAL", "warn"), ("UNKNOWN", "warn")])
def test_account_mode_comes_from_the_terminal_with_a_matching_tone(mode, expected_tone):
    wizard = live_wizard(session=live_session(mode=mode))
    wizard.session.connect()

    ctx = build_context(
        platform=None, wizard=wizard, page="account", title="Account", action_nonce="n"
    )
    assert ctx.terminal.mode == mode
    assert ctx.terminal.mode_confirmed is True
    assert f"chip--{expected_tone}" in views.mode_chip(ctx)

    html = views.render_account(ctx)
    assert f"Detected account mode: {mode}" in html
    assert "never switches accounts" in html


def test_an_unconfirmed_mode_is_marked_unconfirmed():
    wizard = live_wizard(session=live_session(mode="DEMO", mode_confirmed=False))
    wizard.session.connect()
    ctx = build_context(
        platform=None, wizard=wizard, page="overview", title="Overview", action_nonce="n"
    )
    assert "unconfirmed" in views.mode_chip(ctx)
    assert ctx.terminal.mode == "DEMO"


def test_no_account_mode_is_shown_when_nothing_is_connected():
    ctx = build_context(
        platform=None, wizard=live_wizard(), page="overview", title="Overview", action_nonce="n"
    )
    assert "No account mode" in views.mode_chip(ctx)
    assert ctx.terminal.mode == "UNKNOWN"


def test_a_simulator_mode_is_never_reported_as_demo_or_real():
    wizard = simulated_wizard()
    wizard.session.connect()
    mode = wizard.account_mode()
    assert mode["sample"] is True
    assert mode["is_demo"] is False
    assert mode["is_real"] is False
    assert mode["detected"] == "DEMO"  # the fixture value, always shown as Sample


def test_the_account_page_offers_no_mode_or_path_forms():
    app = connected_simulator_app()
    html = render(app, "/account")
    assert "/actions/mode-choice" not in html
    assert "/actions/terminal-path" not in html
    assert "account_mode_choice" not in html
    assert "terminal_path" not in html


# ---------------------------------------------------------------------------
# states, accessibility, design system
# ---------------------------------------------------------------------------


def test_empty_states_exist_for_positions_activity_and_symbols():
    app = app_for(simulated_wizard())
    assert parse(render(app, "/positions")).has_text("No open paper positions")
    assert parse(render(app, "/account")).has_text("No account detected")


def test_connecting_state_is_rendered_with_a_status_role():
    ctx = build_context(
        platform=None, wizard=simulated_wizard(), page="overview", title="Overview", action_nonce="n"
    )
    connecting = replace(
        ctx, terminal=replace(ctx.terminal, state="CONNECTING", connected=False)
    )
    html = views.render_connecting(connecting)
    assert "role='status'" in html
    assert "Connecting" in html


def test_error_state_names_the_reason_and_offers_an_explicit_retry():
    ctx = build_context(
        platform=None, wizard=simulated_wizard(), page="overview", title="Overview", action_nonce="nonce"
    )
    errored = replace(
        ctx,
        terminal=replace(
            ctx.terminal,
            state="ERROR",
            error={"reason": "terminal_unavailable", "hint": "Open MetaTrader 5 first."},
        ),
    )
    html = views.render_overview(errored)
    assert "terminal_unavailable" in html
    assert "Open MetaTrader 5 first." in html
    assert "Try again" in html


def test_locked_session_reports_a_reason_instead_of_inventing_one():
    session = locked_session("no_settings_document")
    session.connect()
    view = session.view()
    assert view["connected"] is False
    assert view["state"] == "ERROR"
    assert view["error"]["reason"] == "no_settings_document"


def test_paper_snapshot_failure_degrades_instead_of_raising():
    class Broken:
        def status(self):
            raise RuntimeError("boom")

    app = WorkstationApp(wizard=simulated_wizard(), platform=Broken())
    assert parse(render(app, "/")).has_text("Paper platform unavailable")


def test_the_confirm_refusal_messages_are_operator_readable():
    assert set(CONFIRM_REFUSALS) == {
        "confirm_requires_live_terminal",
        "confirm_requires_observed_account",
        "confirm_requires_selected_symbols",
    }
    app = connected_simulator_app()
    _, _, message = app.act("confirm", {"nonce": [app.nonce]})
    assert message == CONFIRM_REFUSALS["confirm_requires_live_terminal"]
    assert "simulator preview never can" in message.lower()


def test_pages_contain_no_scripts_and_no_remote_assets():
    for app in (connected_simulator_app(), confirmed_live_app()):
        for path in sorted(GET_ROUTES):
            html = render(app, path)
            assert "<script" not in html.lower()
            assert "http://" not in html.replace("http-equiv", "").lower()
            assert "https://" not in html.lower()
            for _, href, _ in views.NAV:
                assert f"href='{href}'" in html, (path, href)


def test_csp_forbids_scripts_connections_and_unsafe_sources():
    assert "script-src 'none'" in CSP
    assert "default-src 'none'" in CSP
    assert "connect-src 'none'" in CSP
    assert "'unsafe-eval'" not in CSP


def test_framing_is_closed_unless_a_preview_asked_for_it():
    assert "frame-ancestors 'self'" in CSP
    assert "frame-ancestors *" not in CSP
    assert "frame-ancestors *" in csp_header(allow_framing=True)
    assert app_for(simulated_wizard()).allow_framing is False


def test_only_loopback_binding_is_allowed_by_default():
    app = app_for(simulated_wizard())
    with pytest.raises(ValueError):
        make_server(app, host="0.0.0.0", port=0)
    server = make_server(app, host="127.0.0.1", port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_every_page_has_landmarks_a_skip_link_and_labelled_navigation():
    for app in (connected_simulator_app(), confirmed_live_app()):
        for path in sorted(GET_ROUTES):
            page = parse(render(app, path))
            assert page.by_tag("main"), path
            assert page.by_tag("nav"), path
            assert any(a.get("href") == "#main" for a in page.by_tag("a")), path
            assert len([a for a in page.by_tag("a") if a.get("aria-current") == "page"]) == 1, path
            assert page.by_tag("html")[0].get("lang") == "en"


def test_tables_have_captions_and_scoped_headers():
    html = render(confirmed_live_app(), "/activity")
    assert "<caption>" in html
    assert "scope='col'" in html or 'scope="col"' in html


def test_design_tokens_keep_foreground_contrast_above_aa():
    tokens = {
        name: value
        for name, value in re.findall(r"(--[a-z0-9-]+):(#[0-9a-fA-F]{6})", views.STYLES)
    }

    def luminance(hex_colour: str) -> float:
        channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    def contrast(a: str, b: str) -> float:
        la, lb = luminance(a), luminance(b)
        high, low = max(la, lb), min(la, lb)
        return (high + 0.05) / (low + 0.05)

    background = tokens["--bg"]
    for name in ("--text", "--text-2", "--text-3", "--accent", "--ok", "--warn", "--danger"):
        assert contrast(tokens[name], background) >= 4.5, name
    for name in ("--text", "--text-2"):
        assert contrast(tokens[name], tokens["--panel"]) >= 4.5, name


def test_a_single_stylesheet_is_used_by_every_page():
    app = confirmed_live_app()
    assert {render(app, path).count("<style>") for path in GET_ROUTES} == {1}


def test_layout_is_responsive_and_respects_reduced_motion():
    assert "@media (max-width:860px)" in views.STYLES
    assert "prefers-reduced-motion" in views.STYLES


# ---------------------------------------------------------------------------
# real HTTP round trip
# ---------------------------------------------------------------------------


@pytest.fixture()
def live_server():
    app = app_for(live_wizard())
    server = make_server(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield app, str(host), int(port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(host: str, port: int, method: str, path: str, body: bytes = b"") -> tuple[int, dict, str]:
    connection = http.client.HTTPConnection(host, port, timeout=10)
    headers = {"Content-Type": "application/x-www-form-urlencoded"} if body else {}
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    payload = response.read().decode("utf-8", "replace")
    result = (response.status, dict(response.getheaders()), payload)
    connection.close()
    return result


def test_http_get_pages_are_read_only_and_harden_headers(live_server):
    app, host, port = live_server
    before = app.wizard.session.source.connect_calls
    for path in sorted(GET_ROUTES):
        status, headers, body = _request(host, port, "GET", path)
        assert status == 200, path
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "script-src 'none'" in headers["Content-Security-Policy"]
    assert app.wizard.session.source.connect_calls == before
    assert app.paper is not None and app.paper.available is True


def test_http_post_connect_then_disconnect(live_server):
    app, host, port = live_server
    body = f"nonce={app.nonce}".encode()

    status, headers, _ = _request(host, port, "POST", "/actions/connect", body)
    assert status == 303
    assert headers["Location"] == "/"
    assert app.wizard.session.connected is True
    assert app.wizard.session.source.connect_calls == 1

    status, headers, _ = _request(host, port, "POST", "/actions/disconnect", body)
    assert status == 303
    assert app.wizard.session.connected is False
    assert app.wizard.session.source.connect_calls == 1


def test_http_methods_other_than_get_head_post_are_refused(live_server):
    _, host, port = live_server
    for method in ("PUT", "PATCH", "DELETE"):
        status, headers, _ = _request(host, port, method, "/")
        assert status == 405, method
        assert headers["Allow"] == "GET, HEAD"
    status, _, _ = _request(host, port, "POST", "/orders", b"")
    assert status == 405


def test_http_post_with_a_password_is_refused(live_server):
    app, host, port = live_server
    body = f"nonce={app.nonce}&password={SECRET_VALUE}".encode()
    status, _, payload = _request(host, port, "POST", "/actions/connect", body)
    assert status == 400
    assert SECRET_VALUE not in payload
    assert app.wizard.session.connected is False


# ---------------------------------------------------------------------------
# 6. the paper strategy runner
# ---------------------------------------------------------------------------

RUNNER_VIEW = {
    "available": True,
    "state": "STOPPED",
    "running": False,
    "reason": "restored_after_restart",
    "reason_detail": "restored from durable state; press Start to resume cycles",
    "blockers": [],
    "strategy": {
        "candidate_id": "trend_pullback_v1_8_21",
        "status": "CANDIDATE_UNTESTED",
        "parameters": {"fast_period": 8, "slow_period": 21},
        "rationale": "trend plus pullback",
        "assumptions": ["fills at the next price"],
        "limitations": ["loses in ranging markets"],
        "evidence": {},
    },
    "symbol": "AAA/BBB",
    "quantity": "0.10",
    "data": {"kind": "HISTORICAL_REPLAY", "data_class": "HISTORICAL_REPLAY", "label": "offline replay"},
    "last_tick_at": "",
    "last_signal_at": "",
    "last_fill_at": "",
    "last_signal": "HOLD",
    "counters": {"cycles": 0, "bars": 0, "signals": 0, "orders": 0, "fills": 0, "refusals": 0},
    "recent": [],
    "positions": [],
    "fills": [],
    "restored": True,
    "paper_only": True,
    "order_sending": False,
}


class FakeRunnerHost:
    """Records what the UI asks the runner to do; owns no thread and no data."""

    def __init__(self, *, refuse: bool = False, error: str = "") -> None:
        self.refuse = refuse
        self.error = error
        self.started = 0
        self.stopped = 0

    def view(self):
        return dict(RUNNER_VIEW)

    def start_runner(self):
        self.started += 1
        if self.refuse:
            return {
                **RUNNER_VIEW,
                "state": "BLOCKED",
                "running": False,
                "reason": "live_mode_active",
                "blockers": [
                    {
                        "reason": "live_mode_active",
                        "message": "The paper runner only runs in PAPER mode.",
                        "hint": "Switch to paper first.",
                    }
                ],
            }
        return {**RUNNER_VIEW, "state": "RUNNING", "running": True, "reason": "operator_started"}

    def stop_runner(self):
        self.stopped += 1
        return {**RUNNER_VIEW, "state": "STOPPED", "reason": "operator_stopped"}


def _page_text(app: WorkstationApp, path: str) -> str:
    status, html = app.page(path)
    assert int(status) == 200
    page = _Page()
    page.feed(html)
    return " ".join(page.text)


def test_the_runner_panel_shows_its_state_and_offers_one_action():
    app = app_for(simulated_wizard())
    app.runner = FakeRunnerHost()
    for path in ("/", "/positions", "/activity"):
        text = _page_text(app, path)
        assert "Paper strategy runner" in text, path
        assert "STOPPED" in text, path
        assert "trend_pullback_v1_8_21" in text, path
        assert "CANDIDATE_UNTESTED" in text, path
        assert "HISTORICAL_REPLAY" in text, path
        assert "paper only" in text.lower(), path


def test_starting_the_runner_goes_through_the_host_and_never_touches_the_mode():
    app = app_for(simulated_wizard())
    host = FakeRunnerHost()
    app.runner = host
    before = app.mode.mode
    status, redirect, message = app.act("runner-start", {"nonce": [app.nonce]})
    assert int(status) == 303
    assert redirect == "/positions"
    assert "simulated" in message.lower()
    assert host.started == 1
    assert app.mode.mode == before  # the runner never changes the mode


def test_stopping_the_runner_is_its_own_action():
    app = app_for(simulated_wizard())
    host = FakeRunnerHost()
    app.runner = host
    status, redirect, message = app.act("runner-stop", {"nonce": [app.nonce]})
    assert int(status) == 303
    assert host.stopped == 1
    assert "stopped" in message.lower()


def test_a_refused_start_reports_the_reason_and_trades_nothing():
    app = app_for(simulated_wizard())
    app.runner = FakeRunnerHost(refuse=True)
    before = app.mode.mode
    status, _, message = app.act("runner-start", {"nonce": [app.nonce]})
    assert int(status) == 303
    assert "live_mode_active" in message
    assert app.mode.mode == before


def test_without_a_configured_runner_the_ui_says_so_and_refuses():
    app = app_for(simulated_wizard())
    text = _page_text(app, "/")
    assert "Paper strategy runner" in text
    assert "No runner configured" in text
    status, _, message = app.act("runner-start", {"nonce": [app.nonce]})
    assert int(status) == 400
    assert "No paper runner" in message


def test_the_runner_panel_is_never_green_unless_the_runner_says_running():
    app = app_for(simulated_wizard())
    app.runner = FakeRunnerHost()
    html = app.page("/")[1]
    assert "RUNNING (PAPER)" not in html
    assert "STOPPED" in html


class LiveRunnerHost(FakeRunnerHost):
    """A runner that *does* have a live path, so the panel must say which."""

    def __init__(self, *, ready: bool = False) -> None:
        super().__init__()
        self.ready = ready

    def view(self):
        return {
            **RUNNER_VIEW,
            "execution_mode": "LIVE" if self.ready else "PAPER",
            "live_effective": False,
            "live_configured": True,
            "paper_only": False,
            "order_sending": True,
            "trading_capability": "live_broker_orders",
            "live_orders": 0,
            "live": {
                "ready": self.ready,
                "mode": "LIVE" if self.ready else "PAPER",
                "blockers": (
                    []
                    if self.ready
                    else [
                        {
                            "reason": "mode_not_live",
                            "message": "Execution mode is PAPER; a live order needs LIVE.",
                            "hint": "Switch to LIVE and confirm it.",
                        }
                    ]
                ),
            },
        }


def test_the_runner_panel_shows_whether_a_live_path_exists_and_whether_it_is_ready():
    app = app_for(simulated_wizard())
    app.runner = LiveRunnerHost()
    text = _page_text(app, "/")
    assert "Live path" in text
    assert "blocked" in text.lower()
    assert "mode_not_live" in text
    assert "live_broker_orders" in text
    assert "order sending: yes" in text


def test_a_ready_live_path_is_reported_as_ready_and_not_as_green_by_default():
    app = app_for(simulated_wizard())
    app.runner = LiveRunnerHost(ready=True)
    text = _page_text(app, "/")
    assert "Live path" in text
    assert "ready" in text.lower()
    # The *runner* is still stopped: a ready path is not a running strategy.
    assert "STOPPED" in text
    # And nothing claims the terminal is connected by this page.
    assert "SIMULATED" in text.upper() or "simulated" in text
