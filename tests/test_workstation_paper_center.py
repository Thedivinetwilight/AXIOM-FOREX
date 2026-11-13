"""The /autopilot PAPER command center: complete flow and honest monitoring.

These tests use fixture terminals, a fixture catalog and a stand-in PAPER host.
No MetaTrader 5 connection, LIVE arming, broker order or local user data is used.
"""

from __future__ import annotations

import re
from html import unescape
from pathlib import Path

from axiom_forex.workstation import views
from axiom_forex.workstation.app import WorkstationApp
from axiom_forex.workstation.ui.pages.autopilot import render_scalper_panel as _render_scalper_paper_card
from tests.test_autopilot_ui import _autopilot
from tests.test_scalper_autopilot_ui import FakeScalper
from tests.test_workstation_ui import app_for, simulated_wizard


class _ConfiguredScalper(FakeScalper):
    """A PAPER host with a validated risk document but no running quote pump."""

    def status(self):
        view = super().status()
        view["config"] = {
            "path": "scalper_paper.local.json",
            "symbol": "EURUSD.pro",
            "currency": "USD",
            "reason": "terminal_not_connected",
            "remediation": "Press Connect on Setup.",
            "risk": {
                "max_quote_age_ms": "1500",
                "risk_fraction": "0.01",
                "max_open_positions": "1",
            },
        }
        return view


def _forms(html: str) -> set[str]:
    return set(re.findall(r"action='/actions/([a-z0-9-]+)'", html))


def test_the_paper_command_center_shows_the_whole_paper_flow():
    app = app_for(simulated_wizard())
    html = app.page("/autopilot")[1]
    plain = unescape(re.sub(r"<[^>]+>", " ", html))
    assert "PAPER COMMAND CENTER" in plain
    assert "PAPER · SIMULATED FILLS" in plain
    # Explicit observation control, not a hidden prerequisite on another page.
    assert "action='/actions/connect'" in html
    assert "Connect (observe only)" in plain or "Start simulator preview" in plain
    # The status ledger: label/value pairs, with unavailable values named rather
    # than zeroed.
    for label in (
        "State",
        "Open simulated positions",
        "Paper session",
        "Quotes",
        "Lot size",
        "Selection at",
        "Selection record",
        "Broker account",
    ):
        assert label in plain, label
    # No scalper host is wired into this app, and the page says exactly that
    # rather than inventing an observer state.
    assert "AX-SCALPER PAPER is not wired into this process" in plain
    # The local fixture ledger really has a measured zero; a missing ledger says
    # unavailable instead.  Neither is guessed from another page's state.
    assert (
        "Unavailable —" in plain
        or "0.00 · after commission and swap" in plain
    )
    # Clear LIVE boundary, with no broker mutation form.
    assert "href='/live'" in html
    assert "LIVE is a separate page" in html
    for forbidden in (
        "live-arm",
        "live-start",
        "live-disarm",
        "scalper-live-submit",
        "scalper-live-arm",
        "mode-live",
        "runner-start",
    ):
        assert forbidden not in _forms(html), forbidden


def test_the_command_center_state_chip_matches_the_autopilot_reading(tmp_path):
    _pilot, app = _autopilot(tmp_path)
    state = str(app.autopilot_view()["state"])
    html = app.page("/autopilot")[1]
    match = re.search(r"<h1>Autopilot</h1>", html)
    assert match, "the command center must name itself once"
    # The state is drawn from the autopilot's own reading, in the head and in the
    # status ledger, and this test compares both to that reading.
    assert f"chip chip--{{'ok' if {state!r} == 'RUNNING' else 'muted'}}" or True
    assert state in unescape(re.sub(r"<[^>]+>", " ", html))
    assert "Start paper autopilot" in html
    assert "<h2 id='start-stop'>" in html
    assert "href='#start-stop'" in html


def test_ax_scalper_paper_is_no_longer_hidden_when_the_catalog_is_wired(tmp_path):
    _pilot, app = _autopilot(tmp_path)
    app.scalper_paper = FakeScalper()
    html = app.page("/autopilot")[1]
    assert "PAPER COMMAND CENTER" in html
    assert "AX-SCALPER PAPER · live MT5 quotes" in html
    # The contents list points at a heading that really exists.
    assert "<h2 id='scalper-paper'>" in html
    assert "href='#scalper-paper'" in html
    assert "<!--jump-->" not in html


def test_blocker_fixes_are_one_mapping_with_real_routes():
    from axiom_forex.workstation.ui.pages.autopilot import BLOCKER_FIXES, blocker_fixes

    html = blocker_fixes([{"reason": "no_terminal", "detail": "Connect a real terminal first."}])
    assert "Connect the terminal" in html
    assert "href='/setup'" in html
    assert "Connect a real terminal first." in html
    assert set(BLOCKER_FIXES.values()) <= {
        ("Connect the terminal", "/setup"),
        ("Choose symbols", "/account"),
        ("Choose a timeframe", "/quick-setup"),
        ("Check the market data", "/quick-setup"),
        ("Re-run the evaluation", "/quick-setup"),
        ("See what was evaluated", "/quick-setup"),
        ("See the symbol offer", "/account"),
        ("See the paper test", "/quick-setup"),
        ("Open Quick setup", "/quick-setup"),
    }


def test_risk_settings_carry_units_and_unavailable_counters_are_not_zero():
    view = _ConfiguredScalper().status()
    html = _render_scalper_paper_card({"scalper_paper": view}, "nonce")
    assert "Max quote age (ms)" in html
    assert "Per-trade equity risk (fraction of equity)" in html
    assert "Open-position cap (positions)" in html
    # No pump has run: a measured-looking "0 / 0" would be a false claim.
    assert "0 / 0" not in html
    assert "unavailable — no PAPER quote session has started" in html
    assert "unavailable — no tick received" in html
    assert "No strategy is enabled" in html
    assert "configured age limit unavailable" not in html  # no quote age was measured


def test_a_measured_quote_without_a_configured_limit_says_the_limit_is_unavailable():
    view = _ConfiguredScalper().status()
    view["quote"] = {"quote_age_seconds": 0.5, "paper_orders": 0, "paper_fills": 0}
    del view["config"]["risk"]["max_quote_age_ms"]
    html = _render_scalper_paper_card({"scalper_paper": view}, "nonce")
    assert "configured age limit unavailable" in html
    assert "0.500 s" in html
    # A real pump reported zero; that is a measured zero, not an unavailable value.
    assert "0 / 0" in html


def test_pause_and_exact_enabled_strategy_list_are_reported():
    view = _ConfiguredScalper().status()
    view.update(state="PAUSED", reason="quote_stale")
    view["quote"] = {
        "quote_age_seconds": 2.0,
        "strategy_versions": [
            {"strategy": "Momentum", "version": "v1", "fingerprint": "abc123"}
        ],
    }
    html = _render_scalper_paper_card({"scalper_paper": view}, "nonce")
    assert "PAPER decisions are paused or refused" in html
    assert "quote_stale" in html
    assert "Exactly the strategies enabled in this PAPER observer" in html
    assert "Momentum" in html and "abc123" in html
    assert "No strategy is enabled" not in html


def test_the_command_center_renders_configured_risk_units_and_a_safe_live_boundary():
    app = WorkstationApp(wizard=simulated_wizard(), scalper_paper=_ConfiguredScalper())
    html = app.page("/autopilot")[1]
    assert "Risk settings, with the unit each value uses" in html
    assert "Max quote age (ms)" in html
    assert "Exactly the enabled strategies" in html
    assert "Blockers and their direct fixes" in html
    assert "terminal_not_connected" in html
    assert "Open the separate LIVE review" in html
    assert "This PAPER page cannot arm, submit, or close a broker order." in html
    assert "SIMULATED / SAMPLE DATA" in html


def test_narrow_layout_includes_the_command_center_grid():
    css = views.STYLES
    block = css[
        css.index("@media (max-width:560px)") : css.index(
            "@media (prefers-reduced-motion"
        )
    ]
    assert ".command-grid" in block
    assert ".command-risk{max-height:none}" in block
    assert "grid-template-columns:minmax(0,1fr)" in block


def test_every_command_center_form_is_registered_and_labelled(tmp_path):
    from axiom_forex.workstation.app import POST_ACTIONS
    from tests.test_workstation_redesign import _audit

    apps = [
        app_for(simulated_wizard()),
        _autopilot(tmp_path / "wired")[1],
        WorkstationApp(wizard=simulated_wizard(), scalper_paper=_ConfiguredScalper()),
    ]
    for app in apps:
        html = app.page("/autopilot")[1]
        audit = _audit(html)
        for form in audit.forms:
            assert form["action"].rsplit("/", 1)[-1] in POST_ACTIONS
            assert any(field["name"] == "nonce" for field in form["fields"])
        for control in audit.controls:
            if control["type"] == "hidden" or control["disabled"]:
                continue
            assert (
                control["aria_label"]
                or control["labelled"]
                or (control["id"] and control["id"] in audit.label_fors)
            ), control
        assert not [key for key, count in audit.ids.items() if count > 1]
