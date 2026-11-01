"""/autopilot: one start, one stop, and a guarded live bridge that cannot arm.

The page is rendered from a fixture workstation: a fake terminal, fixture bars, a
declared stand-in evaluation, and no broker.  Nothing here connects, reads an
account, arms anything, or sends an order.  The live session the bridge can start
is the one /live built; the tests below drive it with a stand-in and assert which
prerequisite each control refuses on.
"""

from __future__ import annotations

import re
from datetime import timedelta
from http import HTTPStatus
from pathlib import Path

from axiom_forex.strategies.catalog.autopilot import BLOCKED, Autopilot
from axiom_forex.strategies.catalog.selection import validation_boundaries
from axiom_forex.workstation import views
from tests.test_catalog_autopilot import (
    FakeReport,
    TIMEFRAME,
    _candidate_id,
    _passing_row,
)
from tests.test_catalog_panel import _panel
from tests.test_catalog_view import _app


def _autopilot(tmp_path: Path, report: bool = False) -> tuple[Autopilot, object]:
    panel = _panel(tmp_path)
    app = _app(panel)
    autopilot = Autopilot(panel)
    if report:

        def fake_evaluate(slots):
            slots = tuple(slots)
            boundaries = validation_boundaries(slots)
            return FakeReport(
                [_passing_row(boundaries[("EURUSD.pro", TIMEFRAME)] - timedelta(hours=1), _candidate_id())],
                slots,
            )

        autopilot._evaluate = fake_evaluate  # type: ignore[assignment]
    app.autopilot = autopilot
    return autopilot, app


def _page(app) -> str:
    return views.render_page(app._context("autopilot", "Autopilot"))


def _text(html: str) -> str:
    body = html[html.index("<main") :] if "<main" in html else html
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


def test_the_page_is_a_route_and_renders_without_an_autopilot():
    from axiom_forex.workstation.app import GET_ROUTES

    assert GET_ROUTES["/autopilot"][0] == "autopilot"
    panel_app = _app(_panel(Path("/tmp/autopilot-ui-check")))
    status, html = panel_app.page("/autopilot")
    assert int(status) == HTTPStatus.OK
    assert "not wired into this workstation" in _text(html)


def test_the_page_offers_one_start_and_one_stop_and_a_blocked_live_card(tmp_path):
    _autopilot_instance, app = _autopilot(tmp_path)
    text = _text(_page(app))
    assert "Start paper autopilot" in text
    assert "Stop the paper loop" not in text, "nothing is running, so there is nothing to stop"
    assert "LIVE is never automatic" in text
    # No bridge is wired in this fixture, and the card says so instead of guessing.
    assert "The live bridge is not wired into this workstation" in text
    # The only actions the page can post are the autopilot's own six.
    posted = set(re.findall(r"action='/actions/([a-z-]+)'", _page(app)))
    assert posted <= {
        "autopilot-start",
        "autopilot-stop",
        "autopilot-flat",
        "autopilot-kronos",
        "autopilot-live-start",
        "autopilot-live-stop",
    }
    for forbidden in ("live-arm", "live-start", "catalog-paper-start", "mode-live"):
        assert forbidden not in posted
    # Arming is never offered here: that stays on /live with its own phrase.
    assert "live-arm" not in posted


def test_the_forecast_input_is_off_by_default_and_says_it_can_only_veto(tmp_path):
    _autopilot_instance, app = _autopilot(tmp_path)
    text = _text(_page(app))
    assert "Mode: OFF" in text
    assert "Can create a trade no" in text
    assert "Can only veto yes" in text
    assert "never required" in text


def test_a_running_page_shows_the_reasons_the_positions_and_the_measured_latency(tmp_path):
    autopilot, app = _autopilot(tmp_path, report=True)
    autopilot.start(symbols=["EURUSD.pro"], timeframes=[TIMEFRAME])
    autopilot.wait(timeout=180.0)
    try:
        assert autopilot.status()["state"] == "RUNNING"
        text = _text(_page(app))
        assert "Stop the paper loop" in text
        assert "Why every candidate was selected or left out" in text
        assert "The test window took no part" in text
        assert "Active strategies" in text
        assert "Feed health" in text
        assert "Open simulated positions" in text
        assert "Observed latency" in text
        assert "Taking this to LIVE: what is missing" in text
        assert "no order was sent" in text.lower() or "No order is sent" in text
        # Nothing on the page promises a result.
        lowered = text.lower()
        for promise in ("guaranteed", "risk-free", "sure profit", "best strategy"):
            assert promise not in lowered
    finally:
        autopilot.stop()


def test_the_start_action_is_refused_without_an_autopilot(tmp_path):
    app = _app(_panel(tmp_path))
    status, redirect, message = app.act("autopilot-start", {"nonce": [app.nonce]})
    assert int(status) == HTTPStatus.CONFLICT
    assert redirect == "/autopilot"
    assert "no autopilot" in message


def test_the_actions_go_through_the_page_and_report_back(tmp_path):
    autopilot, app = _autopilot(tmp_path, report=True)
    status, redirect, _message = app.act(
        "autopilot-start", {"nonce": [app.nonce], "symbols": "EURUSD.pro", "timeframes": TIMEFRAME}
    )
    assert int(status) == HTTPStatus.SEE_OTHER
    assert redirect == "/autopilot"
    autopilot.wait(timeout=180.0)
    try:
        status, redirect, _message = app.act("autopilot-stop", {"nonce": [app.nonce]})
        assert int(status) == HTTPStatus.SEE_OTHER
        assert autopilot.status()["state"] == "STOPPED"
        # The result is shown once, and it says nothing was closed.
        text = _text(_page(app))
        assert "The last autopilot action" in text
        assert "No simulated position is open." in text or "still open" in text
    finally:
        autopilot.stop()


def test_the_forecast_switch_validates_its_mode(tmp_path):
    autopilot, app = _autopilot(tmp_path)
    status, _redirect, _message = app.act(
        "autopilot-kronos", {"nonce": [app.nonce], "kronos_mode": "VETO"}
    )
    assert int(status) == HTTPStatus.SEE_OTHER
    assert autopilot.gate.mode == "VETO"
    assert autopilot.gate.required is False
    status, _redirect, message = app.act(
        "autopilot-kronos", {"nonce": [app.nonce], "kronos_mode": "SIGNAL"}
    )
    assert int(status) == HTTPStatus.CONFLICT
    assert "'SIGNAL' is not one of OFF, VETO." in message
    assert autopilot.gate.mode == "VETO", "a refused mode leaves the current one alone"


def test_a_stale_form_is_refused_like_every_other_action(tmp_path):
    _autopilot_instance, app = _autopilot(tmp_path)
    status, _redirect, message = app.act("autopilot-stop", {"nonce": ["stale"]})
    assert int(status) == HTTPStatus.FORBIDDEN
    assert "stale" in message


def test_the_page_never_names_an_order_function(tmp_path):
    _autopilot_instance, app = _autopilot(tmp_path)
    html = _page(app)
    for forbidden in ("order_send", "order_check", "initialize(", "subprocess", "socket"):
        assert forbidden not in html


def test_the_blocked_state_shows_which_step_stopped_it(tmp_path):
    autopilot = Autopilot(_panel(tmp_path))
    autopilot._evaluate = lambda slots: None  # type: ignore[assignment]

    class _App:
        pass

    autopilot.start(symbols=["EURUSD.pro"], timeframes=[TIMEFRAME])
    autopilot.wait(timeout=120.0)
    status = autopilot.status()
    assert status["state"] == BLOCKED
    assert status["blockers"]


# -- the bridge to the existing LIVE path ------------------------------------
#
# The live workflow here is a stand-in with the same surface the workstation
# wires: a plan, a view, a start and a stop.  Rendering these pages starts
# nothing, and the "live" text below is a fixture's string, not a broker report.


def _bridge(tmp_path, *, state: str = "RUNNING", **trading: object):
    from axiom_forex.strategies.catalog.live_autopilot import build_live_autopilot
    from tests.test_catalog_live_autopilot import (
        CANDIDATE,
        FakeAutopilot,
        FakeTrading,
        plan as live_plan,
    )

    chosen = str(trading.pop("candidate_id", CANDIDATE))
    trading.setdefault("plan_doc", live_plan(candidate_id=chosen))
    return build_live_autopilot(
        autopilot=FakeAutopilot(state=state, selected=(chosen,)),
        trading=FakeTrading(**trading),  # type: ignore[arg-type]
    )


def test_a_blocked_bridge_shows_the_reason_and_offers_no_start(tmp_path):
    from tests.test_catalog_live_autopilot import CANDIDATE

    _autopilot_instance, app = _autopilot(tmp_path)
    app.autopilot_live = _bridge(
        tmp_path,
        candidate_id=CANDIDATE,
        mode="PAPER",
        blockers=({"reason": "mode_not_live", "message": "Not LIVE."},),
    )
    html = _page(app)
    text = _text(html)

    assert "The selected strategy on the existing LIVE path" in text
    assert "Start is disabled" in text
    assert "mode_not_live" in text
    assert "action='/actions/autopilot-live-start'" not in html
    assert "SENDING REAL ORDERS" not in text


def test_a_ready_bridge_offers_the_start_and_states_what_it_preserves(tmp_path):
    _autopilot_instance, app = _autopilot(tmp_path)
    app.autopilot_live = _bridge(tmp_path)
    html = _page(app)
    text = _text(html)

    assert "action='/actions/autopilot-live-start'" in html
    assert "not sending" in text
    assert "unresolved-order lockout" in text
    assert "Plan matches the selection yes" in text


def test_a_running_bridge_shows_the_stop_the_history_and_the_timings(tmp_path):
    from tests.test_catalog_live_autopilot import CANDIDATE

    _autopilot_instance, app = _autopilot(tmp_path)
    pilot = _bridge(tmp_path, candidate_id=CANDIDATE)
    pilot.start()
    app.autopilot_live = pilot
    html = _page(app)
    text = _text(html)

    assert "action='/actions/autopilot-live-stop'" in html
    assert "SENDING REAL ORDERS" in text
    assert "req-1" in text
    assert "Orders the live path recorded" in text
    assert "Measured latency" in text
    assert "not a promise about execution speed" in text
    assert "session_feed" in text


def test_the_live_start_action_reports_the_first_blocker(tmp_path):
    autopilot, app = _autopilot(tmp_path)
    app.autopilot_live = _bridge(tmp_path, state="IDLE")

    status, redirect, message = app.act("autopilot-live-start", {"nonce": [app.nonce]})

    assert int(status) == HTTPStatus.CONFLICT
    assert redirect == "/autopilot"
    assert "paper test" in message


def test_the_live_start_action_without_a_bridge_says_so(tmp_path):
    _autopilot_instance, app = _autopilot(tmp_path)
    app.autopilot_live = None

    status, _redirect, message = app.act("autopilot-live-start", {"nonce": [app.nonce]})

    assert int(status) == HTTPStatus.CONFLICT
    assert "no live bridge" in message


def test_the_live_actions_go_through_the_page_and_report_back(tmp_path):
    _autopilot_instance, app = _autopilot(tmp_path)
    pilot = _bridge(tmp_path)
    app.autopilot_live = pilot

    status, redirect, _message = app.act("autopilot-live-start", {"nonce": [app.nonce]})
    assert int(status) == HTTPStatus.SEE_OTHER
    assert redirect == "/autopilot"
    assert pilot.state == "RUNNING"
    assert "The live path says" in _text(_page(app))

    status, redirect, _message = app.act("autopilot-live-stop", {"nonce": [app.nonce]})
    assert int(status) == HTTPStatus.SEE_OTHER
    assert pilot.state == "STOPPED"
    assert "Stopped and disarmed" in _text(_page(app))


def test_a_stale_live_form_is_refused_like_every_other_action(tmp_path):
    _autopilot_instance, app = _autopilot(tmp_path)
    app.autopilot_live = _bridge(tmp_path)

    status, _redirect, message = app.act("autopilot-live-start", {"nonce": ["stale"]})

    assert int(status) == HTTPStatus.FORBIDDEN
    assert "stale" in message


def test_the_live_card_never_names_an_order_function(tmp_path):
    _autopilot_instance, app = _autopilot(tmp_path)
    app.autopilot_live = _bridge(tmp_path)

    html = _page(app)
    for forbidden in ("order_send", "order_check", "initialize(", "submit_plan("):
        assert forbidden not in html


def test_a_session_started_on_the_live_page_is_shown_not_restarted(tmp_path):
    _autopilot_instance, app = _autopilot(tmp_path)
    pilot = _bridge(tmp_path, session_running=True)
    app.autopilot_live = pilot
    html = _page(app)

    # The same session, started from /live, is visible here and can be stopped.
    assert pilot.trading.started == 0
    assert "action='/actions/autopilot-live-stop'" in html
    assert "SENDING REAL ORDERS" in _text(html)
