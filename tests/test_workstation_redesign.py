"""Full-workstation UI redesign: routes, controls, feedback and narrow layout.

This is an audit in executable form.  Every route is rendered in three session
states; every visible form, link and control is traced to a real handler; the
one-shot outcome of an action is proved to reach the page it redirects to; and
PAPER, LIVE and sample data are checked in the shared chrome.  Nothing here
connects to MetaTrader 5, arms LIVE, sends an order, or touches local user data.
"""

from __future__ import annotations

import http.client
import re
import threading
from collections import Counter
from html.parser import HTMLParser
from http import HTTPStatus
from pathlib import Path

import pytest

from axiom_forex.workstation import launch, views
from axiom_forex.workstation.app import (
    GET_ROUTES,
    POST_ACTIONS,
    WorkstationApp,
    make_server,
)
from axiom_forex.workstation.context import PAGE_PATHS
from tests.test_autopilot_ui import _autopilot
from tests.test_catalog_panel import _panel
from tests.test_catalog_view import _app as _catalog_app
from tests.test_scalper_autopilot_ui import FakeScalper
from tests.test_workstation_revamp import _chrome_apps
from tests.test_workstation_ui import app_for, simulated_wizard


REPO = Path(__file__).resolve().parents[1]


class _ControlAudit(HTMLParser):
    """Parse one rendered page without a browser or a JavaScript runtime."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict] = []
        self._form: dict | None = None
        self.controls: list[dict] = []
        self.buttons: list[dict] = []
        self.links: list[dict] = []
        self.ids: Counter[str] = Counter()
        self.headings: list[str] = []
        self.landmarks: list[str] = []
        self.tables: list[dict] = []
        self._table: dict | None = None
        self._label_stack: list[dict] = []
        self.label_fors: set[str] = set()
        self._button_stack: list[dict] = []
        self.aria_current = 0
        self.main_scope = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {key: value or "" for key, value in attrs}
        if "id" in a:
            self.ids[a["id"]] += 1
        if a.get("aria-current") == "page":
            self.aria_current += 1
        if tag in {"main", "nav", "header", "footer", "aside"}:
            self.landmarks.append(tag)
        if tag == "main":
            self.main_scope = a.get("data-scope", "")
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.headings.append(tag)
        if tag == "form":
            self._form = {"action": a.get("action", ""), "method": a.get("method", ""), "fields": []}
            self.forms.append(self._form)
        elif tag in {"input", "select", "textarea"}:
            item = {
                "tag": tag,
                "name": a.get("name", ""),
                "type": a.get("type", ""),
                "id": a.get("id", ""),
                "aria_label": a.get("aria-label", ""),
                "labelled": bool(self._label_stack),
                "disabled": "disabled" in a or a.get("aria-disabled") == "true",
            }
            self.controls.append(item)
            if self._form is not None:
                self._form["fields"].append(item)
        elif tag == "label":
            self._label_stack.append(a)
            if a.get("for"):
                self.label_fors.add(a["for"])
        elif tag == "button":
            item = {"aria_label": a.get("aria-label", ""), "text": "", "disabled": "disabled" in a}
            self.buttons.append(item)
            self._button_stack.append(item)
        elif tag == "a":
            self.links.append({"href": a.get("href", ""), "aria_label": a.get("aria-label", "")})
        elif tag == "table":
            self._table = {"caption": False, "scoped_head": False}
            self.tables.append(self._table)
        elif tag == "caption" and self._table is not None:
            self._table["caption"] = True
        elif tag == "th" and self._table is not None and a.get("scope") == "col":
            self._table["scoped_head"] = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._form = None
        elif tag == "label" and self._label_stack:
            self._label_stack.pop()
        elif tag == "button" and self._button_stack:
            self._button_stack.pop()
        elif tag == "table":
            self._table = None

    def handle_data(self, data: str) -> None:
        if self._button_stack:
            self._button_stack[-1]["text"] += data


def _audit(html: str) -> _ControlAudit:
    parser = _ControlAudit()
    parser.feed(html)
    parser.close()
    return parser


def _app_states(tmp_path: Path) -> dict[str, WorkstationApp]:
    """Bare, wired, observed, catalog, autopilot and LIVE render states."""

    from tests.test_workstation_live_overview import _wired_app
    from tests.test_workstation_ui import live_wizard

    states = dict(_chrome_apps())
    states["catalog"] = _catalog_app(_panel(tmp_path / "catalog"))
    states["autopilot"] = _autopilot(tmp_path / "autopilot")[1]
    states["scalper"] = WorkstationApp(
        wizard=simulated_wizard(), scalper_paper=FakeScalper()
    )
    # A live terminal whose account has been observed but not yet confirmed: the
    # one state in which the confirmation form is on the page.
    observed = app_for(live_wizard())
    observed.act("connect", {"nonce": [observed.nonce]})
    observed.act("symbols", {"nonce": [observed.nonce], "symbol": ["EURUSD", "GBPUSD"]})
    states["observed"] = observed
    # A wired LIVE adapter and typed-intent bridge, rendered only. Nothing here
    # connects, arms or sends anything.
    states["wired-live"] = _wired_app()
    return states


def _internal_links(html: str) -> set[str]:
    return {
        href.split("#", 1)[0].split("?", 1)[0]
        for href in re.findall(r"href='(/[^']*)'", html)
        if not href.startswith("//")
    }


# ---------------------------------------------------------------------------
# every route, every visible control, one accessible frame
# ---------------------------------------------------------------------------


def test_every_route_renders_one_consistent_accessible_frame(tmp_path):
    states = _app_states(tmp_path)
    for state, app in states.items():
        for path in sorted(GET_ROUTES):
            status, html = app.page(path)
            assert int(status) == HTTPStatus.OK, (state, path, status)
            assert html.count("<style>") == 1, (state, path)
            assert "<script" not in html.lower(), (state, path)
            audit = _audit(html)
            assert audit.headings.count("h1") == 1, (state, path, audit.headings)
            assert "main" in audit.landmarks and "nav" in audit.landmarks
            assert audit.main_scope, (state, path)
            assert audit.aria_current == 1, (state, path)
            assert any(link["href"] == "#main" for link in audit.links)
            assert not [key for key, count in audit.ids.items() if count > 1], (state, path)
            for table in audit.tables:
                assert table["caption"] and table["scoped_head"], (state, path, table)
            for form in audit.forms:
                assert form["method"].lower() == "post", (state, path, form)
                assert form["action"].startswith("/actions/"), (state, path, form)
                action = form["action"].rsplit("/", 1)[-1]
                assert action in POST_ACTIONS, (state, path, action)
                assert any(field["name"] == "nonce" for field in form["fields"]), (state, path, form)
            for control in audit.controls:
                if control["type"] == "hidden" or control["disabled"]:
                    continue
                assert (
                    control["aria_label"]
                    or control["labelled"]
                    or (control["id"] and control["id"] in audit.label_fors)
                ), (state, path, control)
            for button in audit.buttons:
                assert button["aria_label"] or button["text"].strip(), (state, path, button)
            for href in _internal_links(html):
                assert href in GET_ROUTES, (state, path, href)


def test_all_literal_and_builder_form_targets_are_registered_actions():
    source = (REPO / "axiom_forex/workstation/views.py").read_text(encoding="utf-8")
    literal_targets = set(re.findall(r"/actions/([a-z0-9-]+)", source))
    builder_targets = {
        match
        for match in re.findall(r"action_form\(\s*['\"]([a-z0-9-]+)['\"]", source)
    }
    assert literal_targets
    assert builder_targets
    assert literal_targets <= POST_ACTIONS
    assert builder_targets <= POST_ACTIONS
    # The route table, page map and navigation cannot drift apart.
    assert set(PAGE_PATHS) == {key for key, _title in GET_ROUTES.values()}
    assert {key for key, _href, _label in views.NAV} == set(PAGE_PATHS)
    assert len(views.PAGES) == len(GET_ROUTES) == 16
    assert set(views.PAGE_SCOPES) == set(PAGE_PATHS)


# Every declared action that no render state above can show, with the state that
# reveals it. A control that is neither rendered nor named here would be a dead
# button, so this table is pinned rather than described.
_STATE_GATED: dict[str, str] = {
    "autopilot-flat": "autopilot book holds a PAPER position to flatten",
    "autopilot-live-start": "LIVE plan present and armed",
    "autopilot-live-stop": "a LIVE session is running",
    "autopilot-stop": "the PAPER autopilot is running",
    "catalog-paper-close-all": "catalog PAPER book holds a position",
    "catalog-paper-flat": "catalog PAPER position exists",
    "catalog-paper-stop": "catalog PAPER runner is running",
    "dataset-evaluate": "an imported dataset is selected",
    "imports-refresh": "a data folder is configured",
    "kronos-benchmark": "the Kronos panel is wired in",
    "kronos-forecast": "the Kronos panel is wired in",
    "kronos-inspect-weights": "the Kronos panel is wired in",
    "kronos-install": "the Kronos panel is wired in",
    "kronos-install-cancel": "the Kronos panel is wired in",
    "kronos-install-check": "the Kronos panel is wired in",
    "kronos-install-cleanup": "the Kronos panel is wired in",
    "kronos-load": "the Kronos panel is wired in",
    "kronos-preset-load": "the Kronos panel is wired in",
    "kronos-preset-save": "the Kronos panel is wired in",
    "kronos-reconcile": "the Kronos panel is wired in",
    "kronos-save": "the Kronos panel is wired in",
    "kronos-schedule-start": "the Kronos panel is wired in",
    "kronos-schedule-stop": "the Kronos panel is wired in",
    "kronos-stop": "a Kronos job is running",
    "live-disarm": "LIVE is armed",
    "live-start": "LIVE plan present and armed",
    "live-stop": "a LIVE session is running",
    "live-stop-settings": "a LIVE session has run",
    "mode-paper": "the effective execution mode is LIVE",
    "research-cancel": "a research job is running",
    "runner-stop": "a strategy runner is running",
    "scalper-paper-acknowledge-recovery": "the scalper PAPER loop reported recovery",
    "scalper-paper-observe": "a scalper symbol is selected",
    "scalper-paper-select-symbol": "the scalper symbol picker is drawn",
    "scalper-paper-start": "a scalper symbol is selected",
    "scalper-paper-stop": "the scalper PAPER loop is running",
    "strategy-deselect": "a strategy is enabled in the library",
    "strategy-evaluate": "a strategy is selected in the library",
    "strategy-monitor": "a strategy is enabled in the library",
    "strategy-screen": "the strategy library is wired in",
    "strategy-select": "the strategy library is wired in",
    "ticks-convert": "a tick source is configured",
}


def test_every_declared_action_is_visible_or_names_the_state_that_reveals_it(tmp_path):
    states = _app_states(tmp_path)
    rendered: dict[str, set[str]] = {}
    for state, app in states.items():
        for path in sorted(GET_ROUTES):
            status, html = app.page(path)
            assert int(status) == HTTPStatus.OK, (state, path, status)
            for action in set(re.findall(r"action='/actions/([a-z0-9-]+)'", html)):
                rendered.setdefault(action, set()).add(f"{state} {path}")
    assert set(rendered) <= POST_ACTIONS, set(rendered) - POST_ACTIONS
    assert set(rendered) | set(_STATE_GATED) == POST_ACTIONS, (
        POST_ACTIONS - set(rendered) - set(_STATE_GATED),
        (set(rendered) | set(_STATE_GATED)) - POST_ACTIONS,
    )
    assert not set(rendered) & set(_STATE_GATED), set(rendered) & set(_STATE_GATED)
    assert set(_STATE_GATED) <= POST_ACTIONS
    assert all(reason.strip() for reason in _STATE_GATED.values())
    # The LIVE lane's own controls are on /live and nowhere else.
    live_only = {
        "scalper-live-register", "scalper-live-review", "scalper-live-approve",
        "scalper-live-arm", "scalper-live-submit", "scalper-live-reconcile",
    }
    for action in live_only:
        assert rendered[action] == {"wired-live /live"}, (action, rendered[action])
    # And PAPER's command-center controls are on /autopilot, never on /live.
    for action in ("autopilot-start", "autopilot-kronos"):
        assert all(path.startswith("autopilot /autopilot") for path in rendered[action])
    assert "/live" not in " ".join(rendered["autopilot-start"])


def test_every_registered_action_has_a_dispatcher_and_a_specific_outcome():
    """A dead control is one whose POST has no branch or no reported result."""

    result_fields = (
        "live_result",
        "strategies_result",
        "research_result",
        "catalog_result",
        "catalog_audit",
        "autopilot_result",
        "autopilot_live_result",
        "scalper_paper_result",
        "scalper_live_result",
        "kronos_result",
        "data_result",
    )
    for action in sorted(POST_ACTIONS):
        app = app_for(simulated_wizard())
        status, path, message = app.act(action, {"nonce": [app.nonce]})
        assert message != "unknown action", action
        assert path in GET_ROUTES, (action, path)
        reported = bool(str(message).strip()) or any(
            bool(getattr(app, field, None)) for field in result_fields
        )
        assert reported, (action, status, path, message)


# ---------------------------------------------------------------------------
# action outcomes: one click, one notice, on the page the click came from
# ---------------------------------------------------------------------------


def test_a_redirected_action_reports_its_outcome_once_on_its_destination():
    app = app_for(simulated_wizard())
    status, path, message = app.act("connect", {"nonce": [app.nonce]})
    assert int(status) == HTTPStatus.SEE_OTHER and path == "/setup"
    assert message

    elsewhere = app.page("/")[1]
    assert message not in elsewhere
    assert "SIMULATED / SAMPLE DATA" in elsewhere

    destination = app.page(path)[1]
    assert message in destination
    assert "data-action='connect'" in destination
    assert "notice--warn" in destination
    assert "SIMULATED / SAMPLE DATA" in destination, "the sample warning must not hide feedback"

    refreshed = app.page(path)[1]
    assert message not in refreshed, "a one-shot outcome must not repeat"
    assert "data-action='connect'" not in refreshed


def test_a_refused_action_names_its_reason_and_links_back_to_its_form():
    app = app_for(simulated_wizard())
    server = make_server(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        body = f"nonce={app.nonce}&amount=10&currency=X".encode()
        connection = http.client.HTTPConnection(str(host), int(port), timeout=10)
        connection.request(
            "POST",
            "/actions/paper-capital",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        payload = response.read().decode("utf-8", "replace")
        connection.close()
        assert int(response.status) == HTTPStatus.CONFLICT
        assert "Request refused" in payload
        assert "Not saved" in payload
        assert "Back to Paper performance" in payload
        assert "href='/performance'" in payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_no_action_outcome_is_invented_from_a_colour_or_another_panel():
    """Tone comes from the action's own sentence, not from page state."""

    from axiom_forex.workstation.app import _feedback_tone

    assert _feedback_tone(303, "Connected to the terminal.") == "ok"
    assert _feedback_tone(303, "Could not connect: terminal_unavailable") == "warn"
    assert _feedback_tone(303, "Nothing was armed: review required") == "warn"
    assert _feedback_tone(409, "The action was refused.") == "error"


# ---------------------------------------------------------------------------
# PAPER / LIVE / sample distinction and responsive chrome
# ---------------------------------------------------------------------------


def test_the_shared_chrome_names_the_paper_live_and_sample_lanes():
    app = app_for(simulated_wizard())
    autopilot = app.page("/autopilot")[1]
    live = app.page("/live")[1]
    mode = app.page("/mode")[1]
    assert "PAPER COMMAND CENTER" in autopilot
    assert "data-scope='paper-command'" in autopilot
    assert "LIVE · REAL BROKER" not in autopilot
    assert "LIVE · REAL BROKER" in live
    assert "data-scope='live-execution'" in live
    assert "PAPER COMMAND CENTER" not in live
    assert "EXECUTION MODE" in mode
    for html, scope, tone in (
        (autopilot, "paper-command", "paper"),
        (live, "live-execution", "danger"),
        (mode, "execution-mode", "warn"),
    ):
        assert "SIMULATED / SAMPLE DATA" in html
        assert "id='page-scope'" in html
        assert f"data-scope='{scope}'" in html
        assert f"page-scope--{tone}" in html


def test_the_stylesheet_has_one_design_system_and_three_narrow_breakpoints():
    styles = views.STYLES
    for marker in (
        "@media (max-width:1120px)",
        "@media (max-width:860px)",
        "@media (max-width:560px)",
        "prefers-reduced-motion",
        ".page-scope{",
        ".notice--danger",
        ".notice--info",
        ".notice[data-action]",
        "grid-template-columns:minmax(0,1fr)",
    ):
        assert marker in styles, marker
    assert styles.count(":root{") == 1


def test_the_launcher_opens_the_paper_command_center(monkeypatch, capsys, tmp_path):
    opened: list[str] = []

    class _FakeServer:
        server_address = ("127.0.0.1", 8765)

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def shutdown(self) -> None:
            return None

        def server_close(self) -> None:
            return None

    monkeypatch.setattr(launch, "make_server", lambda app, **kwargs: _FakeServer())
    monkeypatch.setattr(launch, "_fixture_platform", lambda: None)
    monkeypatch.setattr(launch.webbrowser, "open", opened.append)
    exit_code = launch.main(
        [
            "--simulate",
            "--settings",
            str(tmp_path / "missing-settings.json"),
        ]
    )
    assert exit_code == 0
    assert opened == ["http://127.0.0.1:8765/autopilot"]
    out = capsys.readouterr().out
    assert "PAPER command center" in out
    assert "http://127.0.0.1:8765/" in out
    assert "five-step PAPER flow" in out
    assert launch.LAUNCH_PATH == "/autopilot"
    assert '/live' in (REPO / "axiom_forex/workstation/launch.py").read_text(encoding="utf-8")


def test_the_readme_and_windows_launcher_name_the_same_landing_page():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    script = (REPO / "scripts/start-workstation.cmd").read_text(encoding="utf-8")
    assert "http://127.0.0.1:8765/autopilot" in readme
    assert "/autopilot" in script
    assert "PAPER command center" in script
