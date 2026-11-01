"""The guided "Start paper trading" page: five steps from cold start to PAPER.

The page is only useful if it tells the truth, so these tests drive the real
catalog panel with a fake terminal and fixture bars: connect, select all, read the
terminal's history, evaluate, tick a screened row, and start the paper session.
Nothing here contacts MetaTrader, sends an order, arms live trading, or changes an
execution mode -- and every test that renders the page also proves that.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from axiom_forex.facade import build_fixture_platform
from axiom_forex.strategies.catalog.panel import (
    ACTION_PAPER_START,
    ACTION_PAPER_STOP,
    CatalogPanel,
)
from axiom_forex.workstation import guide
from axiom_forex.workstation.app import GET_ROUTES, POST_ACTIONS, WorkstationApp
from axiom_forex.workstation.setup import SetupWizard
from axiom_forex.workstation.views import render_page

from tests.test_catalog_panel import (
    NOW,
    FakeTerminal,
    _bars,
    _symbol_record,
    _wait_for,
)
from tests.test_workstation_ui import _Page, live_session, parse, render

# ---------------------------------------------------------------------------
# fixtures: a fake terminal that answers for the names the session reports
# ---------------------------------------------------------------------------


class PanelTerminal(FakeTerminal):
    """The catalog's fake terminal, renamed to match the wizard's test session."""

    NAMES = ("EURUSD", "GBPUSD", "XAUUSD")

    def symbol_names(self):
        return list(self.NAMES)

    def symbol_info(self, name):
        self.calls.append(f"symbol_info:{name}")
        table = {
            "EURUSD": _symbol_record(name, "EUR", "USD"),
            "GBPUSD": _symbol_record(name, "GBP", "USD"),
            # A metal: reported, but it is not a currency pair.
            "XAUUSD": _symbol_record(name, "XAU", "USD"),
        }
        record = table.get(name)
        if record is None:
            raise LookupError("symbol not found")
        return [record]


def _panel(root: Path, terminal=None) -> CatalogPanel:
    return CatalogPanel(
        root=root,
        terminal_provider=lambda: terminal or PanelTerminal(),
        bars_provider=_bars,
        budget_seconds=30.0,
        clock=lambda: NOW,
    )


def _wizard() -> SetupWizard:
    from tests.test_quick_setup_ui import offline_environment

    return SetupWizard(environment=offline_environment(), session=live_session())


def _app(root: Path, *, connect: bool = False, symbols: bool = False) -> WorkstationApp:
    app = WorkstationApp(wizard=_wizard(), platform=build_fixture_platform())
    app.catalog = _panel(root)
    if connect:
        app.act("connect", {"nonce": [app.nonce]})
    if symbols:
        app.act(
            "symbols",
            {"nonce": [app.nonce], "bulk": ["all"], "return_to": ["/"]},
        )
    return app


def _page(app: WorkstationApp) -> str:
    return render(app, "/")


def _text(html: str) -> str:
    page = _Page()
    page.feed(html)
    return " ".join(page.text_content.split())


def _steps(html: str) -> dict[str, dict[str, str]]:
    """Each guided step, keyed by its id, with its state class and status chip."""

    found: dict[str, dict[str, str]] = {}
    for match in re.finditer(
        r"<li class='guide-step guide-step--(?P<state>[a-z]+)' id='step-(?P<key>[a-z]+)'>(?P<body>.*?)</li>",
        html,
        re.S,
    ):
        key = match.group("key")
        status = re.search(r"<span class='chip[^']*'>([^<]*)</span>", match.group("body"))
        found[key] = {
            "state": match.group("state"),
            "status": status.group(1) if status else "",
            "body": match.group("body"),
        }
    return found


# ---------------------------------------------------------------------------
# 1. a cold start: five steps, one obvious first action, nothing claimed
# ---------------------------------------------------------------------------


def test_a_cold_start_shows_the_five_steps_and_one_next_action(tmp_path):
    app = _app(tmp_path)
    html = _page(app)
    steps = _steps(html)

    assert list(steps) == ["connect", "symbols", "data", "evaluate", "paper"]
    assert steps["connect"]["state"] == "next"
    # Nothing is trading, and the page says so in one line rather than a warning wall.
    text = _text(html)
    assert "Not trading yet" in text
    assert "Step 1 — Connect to MetaTrader 5" in text
    assert "0 of 5 steps complete" in text
    # PAPER is named, and LIVE is explicitly not started here.
    assert "PAPER · simulated fills" in text
    assert "LIVE is never started here" in text
    # The only action on the page is the connect form.
    assert "action='/actions/connect'" in html
    assert "catalog-paper-start" not in html


def test_the_summary_reports_every_fact_the_operator_needs(tmp_path):
    app = _app(tmp_path, connect=True, symbols=True)
    text = _text(_page(app))

    assert "Where things stand" in text
    tiles = [
        "MetaTrader 5",
        "Paper session",
        "Symbols",
        "Strategy",
        "Market data",
        "Latest signal",
        "Simulated orders and fills",
        "Open paper positions",
    ]
    for label in tiles:
        assert label in text, label
    assert "2 selected" in text
    assert "EURUSD, GBPUSD" in text
    # Nothing is invented: no signal, no order, no fill, no position has happened.
    assert "None yet" in text
    assert "Nothing open" in text


# ---------------------------------------------------------------------------
# 2. steps 1 and 2: connect, then select the verified pairs
# ---------------------------------------------------------------------------


def test_connect_then_select_all_completes_the_first_two_steps(tmp_path):
    app = _app(tmp_path)
    app.act("connect", {"nonce": [app.nonce]})
    steps = _steps(_page(app))

    assert steps["connect"]["state"] == "done"
    assert steps["symbols"]["state"] == "next"
    # The pair count comes from the terminal, and a metal is not offered.
    assert "2 verified forex pair(s) of 3 reported symbol(s)" in steps["symbols"]["body"]

    status, back, _message = app.act(
        "symbols", {"nonce": [app.nonce], "bulk": ["all"], "return_to": ["/"]}
    )
    assert int(status) == 303
    assert back == "/"  # the guided page keeps the operator on the guided page
    steps = _steps(_page(app))
    assert steps["symbols"]["state"] == "done"
    assert "EURUSD, GBPUSD" in steps["symbols"]["body"]


def test_nothing_offered_can_be_selected_until_a_pair_is_verified(tmp_path):
    from tests.test_workstation_ui import connected_simulator_app

    # The simulator reports sample symbols with no specification at all, so none of
    # them can be verified as a pair: the step says so and offers no bulk button.
    app = connected_simulator_app()
    app.catalog = CatalogPanel(
        root=tmp_path,
        terminal_provider=lambda: None,
        bars_provider=_bars,
        clock=lambda: NOW,
    )
    steps = _steps(_page(app))

    assert steps["symbols"]["state"] == "blocked"
    assert "No reported symbol could be verified" in steps["symbols"]["body"]
    assert "bulk' value='all'" not in steps["symbols"]["body"]


# ---------------------------------------------------------------------------
# 3. step 3: the terminal's own history, per symbol, with reasons
# ---------------------------------------------------------------------------


def test_the_history_check_reports_bars_and_the_reason_for_a_refusal(tmp_path):
    panel = _panel(tmp_path)

    check = panel.data_check(symbols=["EURUSD", "GBPUSD"], timeframe="1h")

    assert check["usable"] == 2
    rows = {row["symbol"]: row for row in check["rows"]}
    assert rows["EURUSD"]["usable"] is True
    assert rows["EURUSD"]["bars"] > 200
    assert rows["EURUSD"]["last"]
    assert rows["EURUSD"]["reason"] == ""

    refused = panel.data_check(symbols=["EURUSD", "NOT_A_SYMBOL"], timeframe="1h")
    why = {row["symbol"]: row["reason"] for row in refused["rows"] if not row["usable"]}
    assert "NOT_A_SYMBOL" in why and why["NOT_A_SYMBOL"]


def test_the_history_check_is_cached_and_never_asks_for_more_than_three_symbols(tmp_path):
    terminal = PanelTerminal()
    panel = _panel(tmp_path, terminal=terminal)

    panel.data_check(symbols=["EURUSD"], timeframe="1h")
    reads = len([call for call in terminal.calls if call.startswith("rates:")])
    panel.data_check(symbols=["EURUSD"], timeframe="1h")
    assert len([call for call in terminal.calls if call.startswith("rates:")]) == reads

    panel.data_check(symbols=["EURUSD", "GBPUSD", "XAUUSD"], timeframe="1h")
    assert panel.data_check(symbols=["EURUSD"], timeframe="1h")["requested"] == 1


def test_step_three_is_blocked_until_a_symbol_is_selected(tmp_path):
    steps = _steps(_page(_app(tmp_path, connect=True)))
    assert steps["data"]["state"] == "blocked"
    assert "WAITING FOR A SYMBOL" in steps["data"]["status"]


# ---------------------------------------------------------------------------
# 4. step 4: evaluate, and show what passed, failed, or could not run
# ---------------------------------------------------------------------------


def test_evaluating_from_the_guided_page_stays_on_it_and_reports_the_screen(tmp_path):
    app = _app(tmp_path, connect=True, symbols=True)
    panel = app.catalog

    status, back, _message = app.act(
        "catalog-evaluate",
        {
            "nonce": [app.nonce],
            "symbols": ["EURUSD", "GBPUSD"],
            "timeframe": ["1h"],
            "return_to": ["/"],
        },
    )
    assert int(status) == 303
    assert back == "/"
    _wait_for(lambda: not panel.evaluation_view()["running"])

    html = _page(app)
    text = _text(html)
    steps = _steps(html)
    assert steps["evaluate"]["state"] == "done"
    assert "PASSED THE SCREEN" in steps["evaluate"]["status"]
    assert "row(s) tested" in steps["evaluate"]["body"]
    assert "data origin" in steps["evaluate"]["body"]
    assert "See every row and reason" in steps["evaluate"]["body"]
    # A screened row is not a profit claim.
    assert "not a promise of profit" in text


def test_evaluation_on_history_the_terminal_cannot_supply_says_so(tmp_path):
    app = _app(tmp_path, connect=True, symbols=True)
    app.catalog.bars_provider = lambda symbol, timeframe, count: []

    status, _back, message = app.act(
        "catalog-evaluate",
        {
            "nonce": [app.nonce],
            "symbols": ["EURUSD"],
            "timeframe": ["1h"],
            "return_to": ["/"],
        },
    )
    assert int(status) == 409
    assert "no usable history" in message.lower() or "nothing to evaluate" in message.lower()
    assert "Traceback" not in message


# ---------------------------------------------------------------------------
# 5. step 5: tick the screened rows, start PAPER, stop it
# ---------------------------------------------------------------------------


def _evaluate(app: WorkstationApp) -> None:
    app.act(
        "catalog-evaluate",
        {
            "nonce": [app.nonce],
            "symbols": ["EURUSD", "GBPUSD"],
            "timeframe": ["1h"],
            "return_to": ["/"],
        },
    )
    _wait_for(lambda: not app.catalog.evaluation_view()["running"])


def test_starting_paper_trading_only_follows_the_ticked_rows(tmp_path):
    app = _app(tmp_path, connect=True, symbols=True)
    _evaluate(app)
    panel = app.catalog
    rows = panel.evaluation_view()["paper_set"]
    assert rows, "the fixture must produce at least one screened row"
    chosen = f"{rows[0]['symbol']}|{rows[0]['timeframe']}|{rows[0]['candidate_id']}"

    html = _page(app)
    assert "name='paper' value='" + chosen + "'" in html
    assert "Start PAPER trading" in html

    status, back, _message = app.act(
        "catalog-paper-start",
        {"nonce": [app.nonce], "paper": [chosen], "return_to": ["/"]},
    )
    try:
        assert int(status) == 303
        assert back == "/"
        assert panel.paper_view()["running"] is True
        assert {watch["symbol"] for watch in panel.paper_view()["watches"]} == {rows[0]["symbol"]}
        text = _text(_page(app))
        assert "Paper trading is running" in text
        assert "RUNNING · PAPER" in text
        assert "Stop the paper session" in text
        assert "no order is ever sent" in text
    finally:
        app.act("catalog-paper-stop", {"nonce": [app.nonce], "return_to": ["/"]})

    # Stopped is not an error: the page says it is not running, and offers the
    # same screened rows to start again.
    html = _page(app)
    steps = _steps(html)
    assert steps["paper"]["state"] == "next"
    assert "READY TO START" in steps["paper"]["status"]
    assert "Not trading yet" in _text(html)


def test_a_ticked_row_that_is_not_in_the_record_is_refused_not_widened(tmp_path):
    app = _app(tmp_path, connect=True, symbols=True)
    _evaluate(app)

    status, back, message = app.act(
        "catalog-paper-start",
        {"nonce": [app.nonce], "paper": ["EURUSD|1h|not_a_candidate"], "return_to": ["/"]},
    )
    assert int(status) == 409
    assert back == "/"
    assert "nothing was widened" in message.lower()
    assert app.catalog.paper_view()["running"] is False


def test_starting_paper_without_an_evaluation_is_refused_with_the_reason(tmp_path):
    app = _app(tmp_path, connect=True, symbols=True)

    status, back, message = app.act(
        "catalog-paper-start", {"nonce": [app.nonce], "return_to": ["/"]}
    )

    assert int(status) == 409
    assert back == "/"
    assert "evaluation" in message.lower()
    assert app.catalog.paper_view()["running"] is False


# ---------------------------------------------------------------------------
# 6. the page cannot trade, change mode, or send the operator to LIVE
# ---------------------------------------------------------------------------


def test_the_guided_page_offers_no_live_action_and_no_mode_switch(tmp_path):
    app = _app(tmp_path, connect=True, symbols=True)
    _evaluate(app)
    html = _page(app)

    for forbidden in (
        "/actions/mode-live",
        "/actions/mode-paper",
        "/actions/live-arm",
        "/actions/live-start",
        "/actions/runner-start",
        "order_send",
        "/actions/buy",
        "/actions/sell",
    ):
        assert forbidden not in html, forbidden
    posts = set(re.findall(r"<form method='post' action='([^']+)'", html))
    assert posts <= {
        f"/actions/{name}"
        for name in ("connect", "disconnect", "catalog-evaluate", "catalog-paper-start")
    }
    assert "PAPER · simulated fills" in _text(html)


def test_reading_the_guided_page_changes_nothing(tmp_path):
    app = _app(tmp_path, connect=True, symbols=True)
    before = (
        list(app.wizard.view()["symbols_selected"]),
        app.wizard.choices.confirmed,
        app.wizard.session.connect_count,
        app.mode_view()["mode"],
    )

    _page(app)
    _page(app)

    after = (
        list(app.wizard.view()["symbols_selected"]),
        app.wizard.choices.confirmed,
        app.wizard.session.connect_count,
        app.mode_view()["mode"],
    )
    assert after == before
    assert app.catalog.paper_view()["running"] is False


def test_a_panel_that_fails_is_a_state_not_a_traceback(tmp_path):
    app = _app(tmp_path, connect=True, symbols=True)

    def explode(*_args, **_kwargs):
        raise RuntimeError("connection reset by peer")

    app.catalog.view = explode  # type: ignore[assignment]

    html = _page(app)
    text = _text(html)
    assert "Traceback" not in html
    assert "connection reset by peer" not in html
    # The step says it cannot run here, and the page still renders every other step.
    steps = _steps(html)
    assert steps["evaluate"]["state"] in {"unavailable", "blocked"}
    assert steps["connect"]["state"] == "done"


def test_the_advanced_pages_are_collapsed_behind_one_disclosure(tmp_path):
    html = _page(_app(tmp_path, connect=True, symbols=True))

    assert "<details class='advanced'>" in html
    assert "Advanced — every other page" in html
    for href in ("/account", "/quick-setup", "/provider-setup", "/mode", "/live"):
        assert f"href='{href}'" in html
    # Collapsed means collapsed: no open attribute on the disclosure.
    block = html.split("<details class='advanced'>", 1)[1].split("</details>", 1)[0]
    assert "open" not in block.split(">", 1)[0]


# ---------------------------------------------------------------------------
# 7. the model itself, without a page
# ---------------------------------------------------------------------------


def _terminal(**overrides):
    class Terminal:
        connected = True
        simulated = False
        server = "SAMPLE-Broker-Demo"
        mode = "DEMO"

    terminal = Terminal()
    for name, value in overrides.items():
        setattr(terminal, name, value)
    return terminal


def test_the_guide_names_the_first_incomplete_step_as_the_reason():
    model = guide.build(
        terminal=_terminal(connected=False, server="", mode=""),
        wizard_view={"symbols_selected": []},
        symbols={"available": False, "reported": 0, "select_all": 0},
        catalog={},
        data_check={},
    )

    summary = model["summary"]
    assert summary["ready"] is False
    assert summary["next_key"] == "connect"
    assert summary["reason"].startswith("Step 1 — Connect to MetaTrader 5")
    assert [step.state for step in model["steps"]][0] == guide.NEXT


def test_the_guide_calls_a_running_session_ready_and_gives_it_a_stop_action():
    model = guide.build(
        terminal=_terminal(),
        wizard_view={"symbols_selected": ["EURUSD"]},
        symbols={"available": True, "reported": 3, "select_all": 2},
        catalog={
            "available": True,
            "symbols": {"available": True, "counts": {"reported": 3, "usable": 2}},
            "evaluation": {
                "on_record": True,
                "counts": {"tested": 40, "skipped": 1, "failed": 0, "screen": {"PASSED_SCREEN": 3}},
                "paper_set": [{"symbol": "EURUSD", "timeframe": "1h", "candidate_id": "trend_v1"}],
            },
            "paper": {
                "built": True,
                "running": True,
                "snapshot": {
                    "status": "RUNNING",
                    "running": True,
                    "stats": {"cycles": 4, "signals": 1, "fills": 1},
                    "quotes": [
                        {
                            "symbol": "EURUSD",
                            "fresh": True,
                            "age_seconds": 2,
                            "checked_at": "2026-11-02T09:20:00+00:00",
                        }
                    ],
                    "positions": [{"symbol": "EURUSD", "side": "LONG"}],
                    "orders": [{"kind": "FILL", "symbol": "EURUSD"}],
                    "fills": [],
                    "signals": [{"symbol": "EURUSD", "decision": "ENTER_LONG"}],
                },
            },
        },
        data_check={"available": True, "rows": [], "message": ""},
    )

    summary = model["summary"]
    assert summary["ready"] is True
    assert summary["headline"] == "Paper trading is running."
    assert summary["reason"] == ""
    tiles = {tile["key"]: tile for tile in summary["tiles"]}
    assert tiles["terminal"]["value"] == "Connected"
    assert tiles["session"]["value"] == "RUNNING"
    assert tiles["symbols"]["detail"] == "EURUSD"
    assert tiles["strategy"]["value"] == "trend_v1"
    assert tiles["data"]["value"] == "FRESH"
    assert tiles["signal"]["value"] == "ENTER_LONG"
    assert tiles["positions"]["value"] == "1"
    paper_step = {step.key: step for step in model["steps"]}["paper"]
    assert paper_step.state == guide.RUNNING
    assert paper_step.action_target == "catalog-paper-stop"


def test_the_guided_page_is_the_home_page_and_its_actions_are_allowlisted():
    from axiom_forex.workstation import views

    assert GET_ROUTES["/"] == ("start", "Start paper trading")
    # The sidebar leads with the guided page; everything else is grouped after it.
    assert views.NAV[0][0] == "start"
    assert [title for title, _keys in views.NAV_GROUPS] == [
        "Start here",
        "Steps in detail",
        "Advanced",
    ]
    # The classic overview is still reachable, and reachable on purpose.
    assert GET_ROUTES["/overview"] == ("overview", "Overview")
    # The page only posts actions the workstation already had.
    for action in ("connect", "symbols", "catalog-evaluate", "catalog-paper-start", "catalog-paper-stop"):
        assert action in POST_ACTIONS
    assert "live-arm" not in {
        step.action_target for step in guide.build(
            terminal=_terminal(),
            wizard_view={"symbols_selected": ["EURUSD"]},
            symbols={"available": True, "reported": 1, "select_all": 1},
            catalog={},
            data_check={},
        )["steps"]
    }


@pytest.mark.parametrize("page", ["/", "/positions", "/activity", "/account", "/mode", "/live"])
def test_every_page_still_renders_with_the_new_home_page(tmp_path, page):
    app = _app(tmp_path, connect=True, symbols=True)
    html = render(app, page)
    assert "Start paper trading" in html  # the navigation entry
    assert "<main" in html


# ---------------------------------------------------------------------------
# 8. PAPER vs LIVE, and the older paper runner
# ---------------------------------------------------------------------------


def test_an_operator_in_live_mode_is_told_the_guided_flow_only_starts_paper(tmp_path):
    app = _app(tmp_path, connect=True, symbols=True)
    status, _back, _message = app.act(
        "mode-live",
        {
            "nonce": [app.nonce],
            "confirmation": ["ENABLE LIVE TRADING"],
            "account_confirmation": ["0123456789ab"],
        },
    )
    assert int(status) == 303
    assert app.mode_view()["mode"] == "LIVE"

    text = _text(_page(app))

    assert "The execution mode is LIVE" in text
    assert "starts the PAPER session" in text
    assert "nothing here can do it" in text
    # The paper button is still the paper button; no live control is offered.
    assert "Start PAPER trading" in text or "Evaluate the strategies" in text
    assert "/actions/live-arm" not in _page(app)


def test_a_running_paper_runner_is_reported_as_running_not_as_not_trading(tmp_path):
    class Runner:
        running = True
        state = "RUNNING"

        def view(self):
            return {"state": "RUNNING", "running": True, "available": True}

    app = _app(tmp_path, connect=True, symbols=True)
    app.runner = Runner()

    html = _page(app)
    text = _text(html)
    steps = _steps(html)

    assert "RUNNING (PAPER RUNNER)" in text
    assert steps["paper"]["state"] == "running"
    # Its own page owns the stop button; this page does not post a stop it cannot do.
    assert "Open the paper runner" in steps["paper"]["body"]
    assert "catalog-paper-stop" not in steps["paper"]["body"]


def test_a_blocked_paper_runner_states_its_own_reason_on_the_start_page(tmp_path):
    """A runner that refused to start must say why, not just "not started"."""

    class Runner:
        running = False
        state = "BLOCKED"

        def view(self):
            return {
                "state": "BLOCKED",
                "running": False,
                "available": True,
                "blockers": [
                    {
                        "reason": "missing_risk_limit",
                        "message": "No entry-exposure limit is declared.",
                        "hint": "Declare one in your broker-terms profile.",
                    }
                ],
            }

    app = _app(tmp_path, connect=True, symbols=True)
    app.runner = Runner()

    html = _page(app)
    text = _text(html)
    steps = _steps(html)

    assert "BLOCKED (PAPER RUNNER)" in text
    assert "No entry-exposure limit is declared." in text
    # The hint is the direct action, so it is on the page next to the reason.
    assert "Declare one in your broker-terms profile." in text
    assert steps["paper"]["state"] == "blocked"


def test_every_navigation_link_is_grouped_rendered_and_has_an_icon():
    from axiom_forex.workstation import views

    keys = [key for key, _href, _label in views.NAV]
    grouped = [key for _title, group in views.NAV_GROUPS for key in group]
    # A link that is not grouped would silently disappear from the sidebar.
    assert sorted(grouped) == sorted(keys)
    assert len(set(grouped)) == len(grouped)
    assert all(key in views.ICONS and key in views.PAGES for key in keys)
