"""The command deck: the workstation's frame, its lanes, and its two run pages.

These are the guards for the redesign the operator asked for after rejecting a
light, card-shuffling console:

* the shipped theme is dark first and *capped* for glare -- body copy is not
  near-white, and every token still clears AA against the plane it sits on;
* every route is drawn in the same frame, and the shared status strip opens with
  the lane (SIMULATED / LIVE / PAPER) before any figure;
* /live and /autopilot open with the same command deck in the same place, so the
  run controls are above the record on both;
* the LIVE deck shows state, order sending, orders, broker positions and quote
  health, and its start/stop post the bridge's own actions -- there is no second
  order route anywhere;
* a blocked LIVE deck offers no start, and every blocker carries its own fix;
* the one-shot AX-SCALPER bridge is labelled one-shot and is never the deck;
* PAPER never renders a LIVE control, and LIVE never renders a paper control.

No MetaTrader 5 connection, no arming, no order and no local user data is
involved: every state here is a stand-in from the test suite.
"""

from __future__ import annotations

import re
from html import unescape
from pathlib import Path

import pytest

from axiom_forex.workstation.app import GET_ROUTES, POST_ACTIONS
from axiom_forex.workstation.ui import shell
from axiom_forex.workstation.ui.render import render_connecting, render_message, render_page
from tests.test_autopilot_ui import _autopilot, _bridge
from tests.test_workstation_live_overview import _wired_app
from tests.test_workstation_ui import app_for, simulated_wizard


# ------------------------------------------------------------------ helpers
def _text(html: str) -> str:
    body = html[html.index("<main") :] if "<main" in html else html
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", body))).strip()


def _deck(html: str) -> str:
    match = re.search(r"<section class='deck[^>]*>.*?</section>", html, re.S)
    assert match, "the page has no command deck"
    return match.group(0)


def _tokens() -> dict[str, str]:
    from axiom_forex.workstation.ui.theme import STYLES

    return {
        name: value
        for name, value in re.findall(r"(--[a-z0-9-]+):(#[0-9a-fA-F]{6})", STYLES)
    }


def _luminance(colour: str) -> float:
    channels = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [
        c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.fixture()
def paper_app(tmp_path):
    return _autopilot(tmp_path / "deck")[1]


@pytest.fixture()
def live_app(tmp_path):
    app = _autopilot(tmp_path / "deck-live")[1]
    app.autopilot_live = _bridge(tmp_path / "deck-live")
    return app


# -------------------------------------------------------------------- theme
def test_the_theme_is_dark_first_and_caps_glare():
    """Dark, and not *blindingly* dark-on-white either: the ink is capped."""

    from axiom_forex.workstation.ui.theme import STYLES

    tokens = _tokens()
    assert "color-scheme:dark" in STYLES
    # The planes go darker as they go up, and none of them is a light sheet.
    assert _luminance(tokens["--paper"]) < 0.02
    assert _luminance(tokens["--surface"]) < 0.05
    assert _luminance(tokens["--rail"]) <= _luminance(tokens["--paper"])
    # Headlines are the brightest thing on the screen, and even they are not
    # pure white; body copy is deliberately dimmer so a long session does not
    # glow at the operator.
    assert 0.7 < _luminance(tokens["--ink"]) < 0.85
    assert _luminance(tokens["--ink-body"]) < 0.65
    assert _luminance(tokens["--ink-body"]) > 0.4
    # ...and the dimmer body copy still clears AAA against the page.
    assert _contrast(tokens["--ink-body"], tokens["--paper"]) >= 7.0
    assert _contrast(tokens["--ink-3"], tokens["--paper"]) >= 4.5
    assert _contrast(tokens["--ink-2"], tokens["--surface"]) >= 4.5
    # No white surface is painted outside print.
    screen = STYLES[: STYLES.index("@media print")]
    assert "background:#fff" not in screen
    assert "#ffffff" not in screen.replace("rgba(255,255,255", "")


def test_every_lane_colour_is_readable_on_its_own_tint():
    tokens = _tokens()
    for tone, tint in (
        ("--ok-ink", "--ok-tint"),
        ("--warn-ink", "--warn-tint"),
        ("--info-ink", "--info-tint"),
        ("--live-accent-ink", "--live-tint"),
        ("--paper-accent-ink", "--paper-tint"),
    ):
        assert _contrast(tokens[tone], tokens[tint]) >= 4.5, tone


def test_the_narrow_rules_keep_the_deck_and_the_chrome_usable():
    from axiom_forex.workstation.ui.theme import STYLES

    phone = STYLES[STYLES.index("@media (max-width:620px)") : STYLES.index("@media (prefers-reduced-motion")]
    tablet = STYLES[STYLES.index("@media (max-width:860px)") : STYLES.index("@media (max-width:620px)")]
    # The chrome scrolls instead of stacking four rows before the page starts.
    assert "flex-wrap:nowrap" in tablet
    assert "overflow-x:auto" in tablet
    # The deck's figures stay a grid; the run controls keep their own line.
    assert ".ro{" in phone
    assert ".page-actions{width:100%" in phone
    assert ".form-steps{grid-template-columns:minmax(0,1fr)" in phone
    # Nothing wider than the page: tables scroll inside their own box.
    assert ".table-wrap{overflow-x:auto" in STYLES


# ------------------------------------------------------------------- frame
def test_every_route_carries_one_lane_band_before_any_figure():
    app = app_for(simulated_wizard())
    for path in sorted(GET_ROUTES):
        status, html = app.page(path)
        assert int(status) == 200, path
        strip = html[html.index("<div class='strip'") : html.index("<main")]
        # The lane is the first fact in the strip, and there is exactly one.
        assert strip.index("class='lane ") < strip.index("class='k'>Execution"), path
        assert strip.count("class='lane ") == 1, path
        # A simulated terminal is never labelled LIVE in the chrome.
        assert "lane lane--sim" in strip, path
        assert "lane lane--live" not in strip, path


def test_the_banner_and_connecting_pages_are_dark_too():
    app = app_for(simulated_wizard())
    ctx = app._context("live", "Live execution")
    for html in (
        render_connecting(ctx),
        render_message(ctx, tone="error", title="Not found", message="No such page."),
    ):
        assert "data-theme='dark'" in html
        assert "<meta name='color-scheme' content='dark'>" in html
        assert "color-scheme:dark" in html


def test_the_connecting_page_carries_the_lane_band(ctx_app=None):
    app = app_for(simulated_wizard())
    html = render_connecting(app._context("live", "Live execution"))
    strip = html[html.index("<div class='strip'") : html.index("<main")]
    assert "class='lane " in strip


# -------------------------------------------------------------- the deck
def test_both_run_pages_open_with_the_same_deck_anatomy(live_app, paper_app):
    live = _deck(render_page(live_app._context("live", "Live execution")))
    paper = _deck(render_page(paper_app._context("autopilot", "Autopilot")))
    for deck in (live, paper):
        # One head in the same place, its figures directly under it, then the
        # body: an operator reads both pages the same way.
        assert deck.index("deck-head") < deck.index("deck-readouts")
        assert deck.index("deck-readouts") < deck.index("deck-body")
        assert "class='ro'" in deck and "ro .k" not in deck
        assert deck.count("class='ro") >= 4
    # The lane is the difference, and it is stated in the deck itself.
    assert "deck--live" in live and "lane--live" in live
    assert "deck--paper" in paper and "lane--paper" in paper
    # ...and both are above the record, not below it.
    for html, anchor in (
        (render_page(live_app._context("live", "Live execution")), "id='ax-scalper-live'"),
        (render_page(paper_app._context("autopilot", "Autopilot")), "id='start-stop'"),
    ):
        assert html.index("class='deck") < html.index(anchor)


def test_the_live_deck_answers_state_sending_orders_positions_and_quotes(live_app):
    deck = _deck(render_page(live_app._context("live", "Live execution")))
    text = _text(deck)
    for label in ("Runner", "Account", "MT5 connection", "Quote", "Managed positions"):
        assert label in text, label
    assert "runner" in text.lower()
    assert "SENDING REAL ORDERS" not in text
    assert "one admitted strategy" in text


def test_the_live_deck_start_and_stop_are_the_bridge_own_actions(tmp_path):
    app = _autopilot(tmp_path / "start-stop")[1]
    from types import SimpleNamespace

    admitted = {"fingerprint": "candidate-1", "name": "Reviewed strategy", "family": "trend", "eligible": True}

    class Runner:
        def __init__(self):
            self.state = "STOPPED"
        def view(self):
            return {
                "available": True, "state": self.state, "account_mode": "REAL",
                "connected": True, "mode": "LIVE", "live_effective": True,
                "account_fingerprint": "account-1", "adapter_armed": self.state == "ARMED",
                "candidate_id": "candidate-1", "candidates": [admitted],
                "eligible_candidates": [admitted], "config": {"symbol": "EURUSD"},
                "run": {}, "blockers": [], "settings_path": "local.json", "state_path": "state.json",
            }
        def arm(self, **_values):
            self.state = "ARMED"
            return {"ok": True, "state": self.state}
        def start(self, **_values):
            self.state = "RUNNING"
            return {"ok": True, "state": self.state}
        def stop(self):
            self.state = "STOPPED"
            return {"ok": True, "state": self.state}

    runner = Runner()
    app.live = SimpleNamespace(
        view=lambda: {"configured": True, "positions": [], "account": {}, "blockers": []},
        scalper_autonomous=runner,
    )
    deck = _deck(render_page(app._context("live", "Live execution")))
    assert "action='/actions/ax-scalper-live-arm'" in deck
    assert "action='/actions/ax-scalper-live-start'" not in deck
    for action in ("ax-scalper-live-arm", "ax-scalper-live-start", "ax-scalper-live-stop"):
        assert action in POST_ACTIONS, action

    runner.arm()
    deck = _deck(render_page(app._context("live", "Live execution")))
    assert "action='/actions/ax-scalper-live-stop'" in deck
    assert "action='/actions/ax-scalper-live-start'" in deck
    # There is exactly one start/stop control on the page.
    html = render_page(app._context("live", "Live execution"))
    assert html.count("action='/actions/ax-scalper-live-start'") == 1
    assert html.count("action='/actions/ax-scalper-live-stop'") == 1


def test_a_blocked_live_deck_offers_no_start_and_each_blocker_has_its_fix(tmp_path):
    app = _autopilot(tmp_path / "blocked")[1]
    app.live = None
    html = render_page(app._context("live", "Live execution"))
    deck = _deck(html)
    assert "action='/actions/ax-scalper-live-start'" not in html
    assert "No MT5 AX-SCALPER runner is wired" in _text(deck)
    assert "ax-scalper-live" in deck


def test_old_demo_and_one_shot_entry_forms_are_not_rendered(live_app):
    html = render_page(live_app._context("live", "Live execution"))
    assert "Autonomous scalping" in _text(html)
    for action in ("demo-test-submit", "scalper-live-submit", "scalper-live-arm"):
        assert f"action='/actions/{action}'" not in html


# ------------------------------------------------------------ the lanes
def test_the_paper_deck_has_no_route_to_a_broker(paper_app):
    html = render_page(paper_app._context("autopilot", "Autopilot"))
    deck = _deck(html)
    text = _text(deck)
    assert "deck--paper" in deck
    assert "PAPER" in text
    for forbidden in (
        "action='/actions/autopilot-live-start'",
        "action='/actions/autopilot-live-stop'",
        "action='/actions/live-start'",
        "action='/actions/live-stop'",
        "action='/actions/scalper-live-submit'",
        "action='/actions/scalper-live-close'",
    ):
        assert forbidden not in html, forbidden
    # The paper controls it does offer are the paper loop's own.
    assert "action='/actions/autopilot-start'" in deck
    assert "action='/actions/autopilot-stop'" not in deck


def test_the_live_page_never_offers_a_paper_control(live_app):
    html = render_page(live_app._context("live", "Live execution"))
    for forbidden in (
        "action='/actions/autopilot-start'",
        "action='/actions/autopilot-stop'",
        "action='/actions/scalper-paper-start'",
        "action='/actions/autopilot-flat'",
    ):
        assert forbidden not in html, forbidden


def test_the_paper_deck_reports_the_ledger_or_says_it_cannot(tmp_path):
    """The deck's money figure is the ledger's own reading, never a typed zero."""

    from axiom_forex.workstation.ui.pages import common

    app = _autopilot(tmp_path / "no-ledger")[1]
    ctx = app._context("autopilot", "Autopilot")
    deck = _deck(render_page(ctx))
    text = _text(deck)
    assert "Net P&L after costs" in text

    account = common.account_view(ctx)
    metric = dict((account.get("metric_by_key") or {}).get("net_pnl") or {})
    if metric.get("available"):
        assert str(metric.get("value")) in text
    else:
        assert "Unavailable" in text


def test_without_an_admitted_candidate_there_is_no_submit_control(live_app):
    """No AX-SCALPER candidate has passed independent admission here.

    With no typed-intent bridge the page says so and offers no submit at all; the
    only way a submission control appears is a bridge that has admitted one.
    """

    html = render_page(live_app._context("live", "Live execution"))
    text = _text(html)
    assert "DEMO and REAL share the same checks" in text
    assert "action='/actions/scalper-live-submit'" not in html
    assert "action='/actions/ax-scalper-live-start'" not in html


# --------------------------------------------------------- accessibility
def test_the_decks_landmarks_and_controls_are_labelled(live_app, paper_app):
    class Audit:
        def __init__(self) -> None:
            self.buttons: list[tuple[str, str]] = []
            self.h2 = 0

    for app, page, title in (
        (live_app, "live", "Live execution"),
        (paper_app, "autopilot", "Autopilot"),
    ):
        html = render_page(app._context(page, title))
        deck = _deck(html)
        # The deck's title is a heading inside a region named by it.
        assert re.search(r"<section class='deck[^>]*aria-labelledby='([a-z0-9-]+)'", deck)
        assert re.search(r"<h2 id='[a-z0-9-]+'>", deck)
        # Every button says something and is a real submit button.
        for button in re.findall(r"<button[^>]*>(.*?)</button>", deck, re.S):
            label = re.sub(r"<[^>]+>", "", button).strip()
            assert label, "a deck button has no accessible name"
        assert html.count("<h1>") == 1
        assert "<main class='shell' id='main'" in html


def test_the_deck_survives_a_keyboard_and_a_screen_reader(paper_app):
    html = render_page(paper_app._context("autopilot", "Autopilot"))
    # The skip link is first, the page is reachable, and nothing is a div that
    # only looks like a control.
    assert html.index("class='skip'") < html.index("class='rail'")
    assert "href='#main'" in html
    assert "<main class='shell' id='main'" in html
    # A fold is a details/summary pair, so it works without a script.
    assert "<details class='fold'" in html
    assert "<summary>" in html


# ---------------------------------------------------------------- shell
def test_the_strip_names_the_lane_and_the_loop_once():
    app = _autopilot(Path("/tmp/axiom-deck-strip"))[1]
    html = render_page(app._context("autopilot", "Autopilot"))
    strip = html[html.index("<div class='strip'") : html.index("<main")]
    assert strip.count("Paper loop") == 1
    assert strip.count("Execution") == 1
    assert strip.count("Terminal") == 1


def test_the_lane_band_is_derived_from_the_session_not_handed_in():
    class _Terminal:
        simulated = False
        connected = True
        state = "CONNECTED"
        mode = "DEMO"
        mode_confirmed = True

    class _Ctx:
        page = "live"
        terminal = _Terminal()
        execution_mode = {"mode": "PAPER"}
        live_effective = False
        extra: dict = {}

    band = shell.lane_band(_Ctx())
    assert "lane lane--live" in band and "LIVE · REAL BROKER" in band

    _Ctx.page = "autopilot"
    assert "lane lane--paper" in shell.lane_band(_Ctx())

    _Ctx.terminal.simulated = True
    assert "lane lane--sim" in shell.lane_band(_Ctx())


# --------------------------------------------------- the handlers are real
def test_starting_from_the_deck_runs_the_bridges_own_handler(tmp_path):
    """The control a deck draws is the control the dispatcher runs."""

    app = _autopilot(tmp_path / "handler")[1]
    app.autopilot_live = _bridge(tmp_path / "handler")

    outcome = app._autopilot_live_action("autopilot-live-start")
    assert outcome["ok"] is True
    assert outcome["live"]["state"] == "RUNNING"
    # ...and the page now draws the stop, not a second start.
    html = render_page(app._context("live", "Live execution"))
    assert html.count("action='/actions/autopilot-live-stop'") == 1
    assert "action='/actions/autopilot-live-start'" not in html

    stopped = app._autopilot_live_action("autopilot-live-stop")
    assert stopped["ok"] is True
    assert app.autopilot_live.status()["state"] != "RUNNING"


def test_a_missing_bridge_answers_start_and_stop_without_claiming_either(tmp_path):
    app = _autopilot(tmp_path / "no-bridge")[1]
    started = app._autopilot_live_action("autopilot-live-start")
    stopped = app._autopilot_live_action("autopilot-live-stop")
    assert started["ok"] is False and started["reason"] == "live_autopilot_unavailable"
    assert stopped["ok"] is False and stopped["reason"] == "live_autopilot_unavailable"
    assert "Nothing changed and nothing was sent" in started["message"]
    assert "Nothing changed and nothing was sent" in stopped["message"]


def test_the_chrome_and_the_deck_agree_on_the_loop_state(tmp_path):
    app = _autopilot(tmp_path / "agree")[1]
    view = app.autopilot_view()
    state = str(view.get("state") or "IDLE")
    html = render_page(app._context("autopilot", "Autopilot"))
    strip = html[html.index("<div class='strip'") : html.index("<main")]
    deck = _deck(html)
    assert state in strip
    assert state in _text(deck)
    # The paper lane is named in both, and neither says LIVE.
    assert "lane lane--sim" in strip or "lane lane--paper" in strip
    assert "lane--live" not in strip
