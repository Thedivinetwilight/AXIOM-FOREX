"""The dark workstation: the theme, the two-tier rail, and LIVE as one workflow.

These are the guards for the redesign the operator asked for:

* the shipped theme is dark first -- no light default anywhere a screen reads;
* the rail is one primary row with a labelled second tier, and every route is
  still reachable from it;
* Live execution owns the continuous guarded session, states whether it is
  sending, and gives every blocker the control that clears it;
* the one-shot AX-SCALPER intent says it is one-shot and never reads as a loop;
* PAPER pages cannot start a LIVE session, and the LIVE page cannot start the
  paper loop.  There is one order route and one control location.
"""

from __future__ import annotations

import re

import pytest

from axiom_forex.workstation.app import GET_ROUTES
from axiom_forex.workstation.ui.render import render_page
from axiom_forex.workstation.ui.shell import NAV, NAV_GROUPS, PRIMARY_GROUPS
from tests.test_autopilot_ui import _autopilot, _bridge


def _live(app) -> str:
    return render_page(app._context("live", "Live execution"))


def _paper(app) -> str:
    return render_page(app._context("autopilot", "Autopilot"))


def _text(html: str) -> str:
    body = html[html.index("<main") :] if "<main" in html else html
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


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


# --------------------------------------------------------------------- theme
def test_the_stylesheet_is_dark_first_and_says_so():
    from axiom_forex.workstation.ui.theme import STYLES

    assert "color-scheme:dark" in STYLES
    # The page plane, the panel above it and the rail above that all get darker,
    # not lighter: nothing a screen shows is a white sheet.
    tokens = _tokens()
    page = _luminance(tokens["--paper"])
    surface = _luminance(tokens["--surface"])
    rail = _luminance(tokens["--rail"])
    assert page < 0.02, page
    assert surface < 0.05, surface
    assert rail <= page
    assert _luminance(tokens["--ink"]) > 0.7
    # A light default would need a light page: there is none outside print.
    screen = STYLES[: STYLES.index("@media print")]
    assert "background:#fff" not in screen
    assert "background:#f4f6fa" not in screen
    assert "#ffffff" not in screen.replace("rgba(255,255,255", "")


def test_every_page_declares_the_dark_scheme_to_the_browser():
    _instance, app = _autopilot(__import__("pathlib").Path("/tmp/axiom-dark-scheme"))
    for path in sorted(GET_ROUTES):
        status, html = app.page(path)
        assert int(status) == 200, path
        assert "data-theme='dark'" in html, path
        assert "<meta name='color-scheme' content='dark'>" in html, path
        # The browser paints its own form controls dark too.
        assert "color-scheme:dark" in html, path


# ---------------------------------------------------------------------- rail
def test_the_rail_is_one_primary_row_and_one_labelled_second_tier():
    assert PRIMARY_GROUPS == ("Run", "Monitor")
    primary = [key for title, keys in NAV_GROUPS if title in PRIMARY_GROUPS for key in keys]
    secondary = [
        key for title, keys in NAV_GROUPS if title not in PRIMARY_GROUPS for key in keys
    ]
    assert primary and secondary
    assert primary + secondary == [page for page, _href, _label in NAV]
    # Six run/monitor entries is a row; sixteen was a wall.
    assert len(primary) == 6

    _instance, app = _autopilot(__import__("pathlib").Path("/tmp/axiom-dark-rail"))
    html = _paper(app)
    assert "<details class='rail-more" in html
    assert "<summary>Setup &amp; tools" in html
    assert "rail-group-label" in html


def test_every_route_is_still_reachable_and_one_page_is_current():
    _instance, app = _autopilot(__import__("pathlib").Path("/tmp/axiom-dark-routes"))
    for path in sorted(GET_ROUTES):
        html = render_page(app._context(GET_ROUTES[path][0], GET_ROUTES[path][1]))
        for _page, href, _label in NAV:
            assert f"href='{href}'" in html, (path, href)
        assert html.count("aria-current='page'") == 1, path


def test_a_second_tier_page_is_named_on_the_closed_disclosure():
    """A page behind the menu must not look like no page is current."""

    _instance, app = _autopilot(__import__("pathlib").Path("/tmp/axiom-dark-tier"))
    html = render_page(app._context("setup", "Setup steps"))

    assert "rail-more--current" in html
    assert "rail-more-current" in html
    assert "Setup steps" in html.split("<details class='rail-more")[1].split("</summary>")[0]


# ---------------------------------------------------------------- continuous
def test_live_execution_owns_the_continuous_session():
    _instance, app = _autopilot(__import__("pathlib").Path("/tmp/axiom-dark-cont"))
    app.autopilot_live = _bridge(__import__("pathlib").Path("/tmp/axiom-dark-cont"))
    html = _live(app)
    text = _text(html)

    assert "Continuous LIVE Autopilot" in text
    # The continuous lane is named before the one-shot bridge, in the document.
    assert html.index("id='continuous'") < html.index("id='one-shot'")
    # The index at the top of the page points at every part of the workflow.
    for anchor in ("#continuous", "#plan", "#session", "#one-shot", "#account"):
        assert f"href='{anchor}'" in html, anchor


def test_the_continuous_panel_states_whether_it_is_sending_and_why_not():
    from pathlib import Path

    _instance, app = _autopilot(Path("/tmp/axiom-dark-blocked"))
    app.autopilot_live = _bridge(
        Path("/tmp/axiom-dark-blocked"),
        mode="PAPER",
        blockers=({"reason": "mode_not_live", "message": "Not LIVE."},),
    )
    html = _live(app)
    text = _text(html)

    assert "not sending" in text
    assert "SENDING REAL ORDERS" not in text
    assert "What stops it, and where to fix it" in text
    # Every blocker carries the control that clears it, inline.
    assert "href='/mode'" in html
    assert "Switch the mode to LIVE" in text
    # A blocked bridge offers no start.
    assert "action='/actions/autopilot-live-start'" not in html


def test_a_ready_continuous_panel_offers_the_start_and_one_place_to_stop_it():
    from pathlib import Path

    _instance, app = _autopilot(Path("/tmp/axiom-dark-ready"))
    pilot = _bridge(Path("/tmp/axiom-dark-ready"))
    app.autopilot_live = pilot
    html = _live(app)
    text = _text(html)

    assert "action='/actions/autopilot-live-start'" in html
    assert "Every prerequisite the live path checks is satisfied" in text
    assert "not a statement about the market" not in text

    pilot.start()
    html = _live(app)
    assert "action='/actions/autopilot-live-stop'" in html
    assert "SENDING REAL ORDERS" in _text(html)


def test_a_blocker_with_no_known_fix_still_lands_somewhere_useful():
    from axiom_forex.workstation.ui.pages.live import FIXES, fix_for

    assert fix_for("mode_not_live") == FIXES["mode_not_live"]
    assert fix_for("something_nobody_mapped") == ("Open setup", "/setup")


# ------------------------------------------------------------------ one-shot
def test_the_scalper_bridge_is_labelled_one_shot_and_never_a_loop():
    from pathlib import Path

    _instance, app = _autopilot(Path("/tmp/axiom-dark-oneshot"))
    html = _live(app)
    text = _text(html)

    assert "Guarded AX-SCALPER one-shot LIVE intent (not continuous)" in text
    assert "ONE-SHOT" in text
    assert "One-shot, not a session" in text
    # What it is, said in the section itself: a single intent, not a loop.
    one_shot = html[html.index("id='one-shot'") : html.index("id='last-path'") if "id='last-path'" in html else None]
    assert "not continuous" in one_shot
    assert "One-shot, not a session" in one_shot
    assert "Nothing here loops" in one_shot


# --------------------------------------------------------------- separation
def test_only_one_page_starts_a_live_session():
    from pathlib import Path

    _instance, app = _autopilot(Path("/tmp/axiom-dark-separate"))
    app.autopilot_live = _bridge(Path("/tmp/axiom-dark-separate"))

    paper = _paper(app)
    live = _live(app)
    assert "action='/actions/autopilot-live-start'" not in paper
    assert "action='/actions/autopilot-live-stop'" not in paper
    assert "href='/live#continuous'" in paper
    # ...and the live page never posts the paper loop's own actions.
    for action in ("autopilot-start", "autopilot-stop", "scalper-paper-start"):
        assert f"action='/actions/{action}'" not in live, action


def test_the_paper_loop_can_not_be_started_from_the_live_page(tmp_path):
    from pathlib import Path

    _instance, app = _autopilot(Path("/tmp/axiom-dark-paper"))
    app.autopilot_live = _bridge(Path("/tmp/axiom-dark-paper"))
    html = _live(app)
    assert "Start paper autopilot" not in html
    assert "Start the paper loop" not in html


# -------------------------------------------------------------- the folds
def test_history_is_folded_rather_than_scrolled_past():
    from pathlib import Path

    from tests.test_workstation_paper_center import FakeScalper

    _instance, app = _autopilot(Path("/tmp/axiom-dark-fold"))
    app.scalper_paper = FakeScalper()
    html = _paper(app)
    from axiom_forex.workstation.ui.theme import STYLES

    assert ".fold{" in STYLES
    assert ".fold>summary" in STYLES
    assert "aria-label='Autopilot sections'" in html
    for anchor in ("#start-stop", "#pnl", "#live-handoff"):
        assert f"href='{anchor}'" in html, anchor

    # The paper lane keeps its heading and its state; its long dumps fold under it.
    assert html.count("id='scalper-paper'") == 1
    lane = html[html.index("id='scalper-paper'") :]
    assert "Strategy review — evidence and policy decision" in lane
    assert "Risk settings, with the unit each value uses" in lane
    assert "Terminal-verified Forex symbols (exact broker names)" in lane
    # Folded, not removed: the tables are still in the document.
    assert "class='fold'" in lane
    assert "Exactly the enabled strategies" in lane


@pytest.mark.parametrize("width", [620, 860, 1080])
def test_the_new_pieces_have_a_narrow_rule(width):
    from axiom_forex.workstation.ui.theme import STYLES

    block = STYLES[STYLES.index(f"@media (max-width:{width}px)") :]
    assert block
    if width == 860:
        assert ".rail-more-panel{position:static" in block
    if width == 620:
        assert ".kpi{grid-template-columns" in block
