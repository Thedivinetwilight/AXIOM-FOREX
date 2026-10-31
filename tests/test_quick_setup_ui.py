"""The ``/quick-setup`` page, its four actions, and what it refuses to say.

Pinned here:

* the page exists at ``/quick-setup``, is in the navigation, and the oversized
  provider-terms page is kept but moved after it -- live execution still needs it;
* the page shows what the terminal answered with provenance, asks only the five
  things a terminal cannot answer, and shows research and live readiness
  separately;
* it lists stored history with a per-series usable/not-usable verdict, and the
  evaluation results with the locked test window measured only for the variant
  chosen on validation;
* it never claims profit, never shows raw exception text, and offers paper
  trading only through the existing runner action;
* the four new POST actions reach the panel and nothing else can.

The panel here is a fake; no terminal, account, or order is involved.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

from axiom_forex.facade import build_fixture_platform
from axiom_forex.workstation import views
from axiom_forex.workstation.app import GET_ROUTES, POST_ACTIONS, WorkstationApp
from axiom_forex.workstation.probes import probe_environment
from axiom_forex.workstation.session import ObservationSession, locked_session
from axiom_forex.workstation.setup import SetupWizard


class FakePanel:
    """A research panel that records calls and returns a fixed view."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, Any]]] = []
        self.view_payload: dict[str, Any] = {
            "available": True,
            "facts": {"state": "OBSERVED", "connected": True, "simulated": False},
            "symbols": [
                {
                    "broker_symbol": "EURUSD.pro",
                    "pair": "EUR/USD",
                    "tradeable": True,
                    "volume_min": "0.01",
                    "volume_step": "0.01",
                    "stops_level": 20,
                    "swap_state": "POINTS",
                    "digits": 5,
                    "missing": [],
                }
            ],
            "observed_rows": [
                {
                    "label": "Account currency",
                    "value": "USD",
                    "state": "OBSERVED",
                    "detail": "read from the connected terminal",
                },
                {
                    "label": "Leverage",
                    "value": "unknown",
                    "state": "UNKNOWN",
                    "detail": "the terminal did not report leverage",
                },
            ],
            "choices": {
                "symbols": ["EURUSD.pro"],
                "lot_size": "0.10",
                "max_open_positions": 1,
                "max_daily_loss_account": "50",
                "max_order_notional_account": "2000",
                "max_spread_pips": "2",
                "window_days": 365,
            },
            "saved": {"path": "quick_setup.local.json", "fingerprint": "abc123", "created_at": ""},
            "readiness": {
                "research": {"ready": True, "blockers": [], "note": "needs observed symbols"},
                "live": {
                    "ready": False,
                    "blockers": ["commission, session windows, rollover cutoff and conversion"],
                    "note": "live still passes the broker-terms document",
                },
            },
            "timeframes": ["1m", "5m", "15m", "1h", "4h", "1d"],
            "default_timeframes": ["1h", "4h", "1d"],
            "history": [
                {
                    "symbol": "EURUSD.pro",
                    "timeframe": "1h",
                    "bars": 6200,
                    "first_at": "2025-10-31T00:00:00+00:00",
                    "last_at": "2026-10-30T23:00:00+00:00",
                    "usable_for_research": True,
                    "reason": "",
                },
                {
                    "symbol": "EURUSD.pro",
                    "timeframe": "1m",
                    "bars": 400,
                    "first_at": "2026-10-30T00:00:00+00:00",
                    "last_at": "2026-10-30T06:39:00+00:00",
                    "usable_for_research": False,
                    "reason": "too_few_bars (400 < 500)",
                },
            ],
            "window_days": 365,
            "progress": {"running": False, "fraction": 1.0, "label": "COMPLETE"},
            "running": False,
            "report": {
                "summary": {
                    "status": "COMPLETE",
                    "series": 1,
                    "series_with_usable_history": 1,
                    "variants_measured": 42,
                    "variants_total": 42,
                    "not_run": 0,
                    "failures": 0,
                    "elapsed_seconds": 61.0,
                    "budget_seconds": 540.0,
                    "claim": "Historical research estimate. Not a forecast.",
                },
                "table": [
                    {
                        "symbol": "EURUSD.pro",
                        "timeframe": "1h",
                        "strategy": "trend_sma_cross::fast_period=8-slow_period=21",
                        "validation_trades": 14,
                        "validation_net_return": 0.0042,
                        "validation_cost_return": 0.0011,
                        "validation_max_drawdown": 0.0035,
                        "test_trades": 6,
                        "test_net_return": -0.0012,
                        "test_max_drawdown": 0.0021,
                        "selected_for_test": True,
                    },
                    {
                        "symbol": "EURUSD.pro",
                        "timeframe": "1h",
                        "strategy": "breakout_donchian::lookback=20-exit_lookback=10",
                        "validation_trades": 9,
                        "validation_net_return": -0.0008,
                        "validation_cost_return": 0.0009,
                        "validation_max_drawdown": 0.0022,
                        "selected_for_test": False,
                        "test_note": "not measured on test",
                    },
                ],
                "row_count": 2,
                "coverage": [
                    {
                        "symbol": "EURUSD.pro",
                        "timeframe": "1h",
                        "bars": 6200,
                        "coverage_fraction": 0.97,
                        "gaps": 4,
                        "usable": True,
                        "reason": "",
                    }
                ],
                "not_run": [
                    {
                        "symbol": "EURUSD.pro",
                        "timeframe": "1d",
                        "reason": "budget_exhausted",
                        "detail": "540s of 540s used",
                    }
                ],
                "failures": [],
                "walk_forward": [
                    {
                        "symbol": "EURUSD.pro",
                        "timeframe": "1h",
                        "variant": "trend_sma_cross::fast_period=8-slow_period=21",
                        "folds": [
                            {
                                "fold": 1,
                                "from": "2025-10-31T00:00:00+00:00",
                                "to": "2026-02-01T00:00:00+00:00",
                                "trades": 5,
                                "net_return": 0.0012,
                                "max_drawdown": 0.0009,
                            }
                        ],
                        "positive_folds": 1,
                        "positive_fraction": 1.0,
                        "median_net_return": 0.0012,
                        "note": "later chunks of the same history",
                    }
                ],
                "sensitivity": [
                    {
                        "symbol": "EURUSD.pro",
                        "timeframe": "1h",
                        "variant": "trend_sma_cross::fast_period=8-slow_period=21",
                        "runs": [
                            {
                                "cost_multiplier": 1.0,
                                "slippage_pips": 0.2,
                                "commission_per_lot": 0.0,
                                "trades": 5,
                                "net_return": 0.0012,
                                "window": "validation",
                            },
                            {
                                "cost_multiplier": 2.0,
                                "slippage_pips": 0.4,
                                "commission_per_lot": 0.0,
                                "trades": 5,
                                "net_return": 0.0004,
                                "window": "validation",
                            },
                        ],
                    }
                ],
                "notes": ["Fills are the next bar's open on the side the trade pays."],
                "claim": "Historical research estimate. Not a forecast.",
            },
            "paper": {
                "ready": False,
                "blockers": [
                    {
                        "reason": "no_strategy_selected",
                        "message": "No strategy has been selected by the operator.",
                        "hint": "Select one on the strategy library page.",
                    }
                ],
                "selection": None,
                "runner": {"available": True, "running": False},
                "note": "Paper fills are simulated locally.",
            },
        }

    def view(self) -> dict[str, Any]:
        return dict(self.view_payload)

    def act(self, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append((action, dict(payload)))
        if action == "quick-setup-save":
            return {"ok": True, "message": "Setup recorded.", "path": "/quick-setup"}
        if action == "research-run":
            return {"ok": True, "message": "Research started.", "path": "/quick-setup"}
        return {
            "ok": False,
            "reason": "refused_for_the_test",
            "message": "that step was refused",
            "path": "/quick-setup",
        }


def offline_environment():
    return probe_environment(
        settings_path=None,
        system="Linux",
        release="6.1.0",
        python="3.11.9",
        finder=lambda name: None,
        version_of=lambda name: None,
    )


def app_with_panel(panel: Any = None) -> WorkstationApp:
    session = locked_session("no_settings_document")
    wizard = SetupWizard(environment=offline_environment(), session=session)
    app = WorkstationApp(wizard=wizard, platform=build_fixture_platform())
    app.research = panel if panel is not None else FakePanel()
    return app


def page_html(app: WorkstationApp) -> str:
    ctx = app._context("quick-setup", "Quick setup")
    return views.render_page(ctx)


# -- routes and navigation -------------------------------------------------


def test_quick_setup_is_a_route_and_a_navigation_entry():
    assert GET_ROUTES["/quick-setup"] == ("quick-setup", "Quick setup")
    labels = [label for _key, _path, label in views.NAV]
    assert "Quick setup" in labels
    # The provider-terms document is kept, and moved after the short setup.
    provider = labels.index("Broker terms (advanced)")
    assert provider > labels.index("Quick setup")
    assert "Provider setup" not in labels


def test_the_page_renders_without_a_research_panel():
    app = app_with_panel()
    app.research = None
    html = page_html(app)
    assert "not wired into this workstation" in html


# -- what the page says ----------------------------------------------------


def test_the_page_shows_observed_facts_with_their_provenance():
    html = page_html(app_with_panel())
    assert "What the terminal answered" in html
    assert "EURUSD.pro" in html
    assert "EUR/USD" in html
    assert "OBSERVED" in html
    # An unknown value says what it affects instead of showing a zero.
    assert "UNKNOWN" in html
    assert "never replaced by a zero" in html


def test_the_page_asks_only_the_five_things_and_labels_the_assumptions():
    html = page_html(app_with_panel())
    assert "The five things only you can answer" in html
    for name in (
        "name='symbols'",
        "name='lot_size'",
        "name='max_daily_loss_account'",
        "name='max_order_notional_account'",
        "name='max_spread_pips'",
    ):
        assert name in html
    # The cost assumptions the terminal cannot report are on the page, labelled.
    assert "name='commission_per_lot'" in html
    assert "assumption you state" in html
    # No credential field is offered anywhere on this page.
    for forbidden in ("type='password'", "name='password'", "name='login'", "name='token'"):
        assert forbidden not in html
    assert "No credential, no login, no account number." in html


def test_readiness_is_shown_separately_for_research_and_live():
    html = page_html(app_with_panel())
    assert "Research" in html and "Live execution" in html
    assert "commission, session windows, rollover cutoff" in html
    assert "protective-stop requirement" in html


def test_stored_history_shows_which_series_are_usable():
    html = page_html(app_with_panel())
    assert "Stored history, and whether it is usable" in html
    assert "too_few_bars (400 &lt; 500)" in html or "too_few_bars (400 < 500)" in html
    assert "not usable" in html
    assert "usable" in html


def test_the_results_show_the_locked_test_only_for_the_chosen_variant():
    html = page_html(app_with_panel())
    assert "chosen on validation" in html
    assert "not measured" in html
    assert "By strategy, symbol and timeframe" in html
    assert "Out of sample, in later chunks" in html
    assert "Would a dearer cost assumption change it?" in html
    assert "What did not run, and why" in html
    assert "budget_exhausted" in html


def test_the_page_never_claims_profit_and_says_so_once_plainly():
    html = page_html(app_with_panel())
    assert "No profitability claim is made anywhere on this page" in html
    lowered = html.lower()
    for forbidden in ("guaranteed", "risk-free", "sure profit", "will make money"):
        assert forbidden not in lowered
    assert "Not a forecast" in html


def test_paper_trading_is_offered_through_the_existing_runner_action_only():
    html = page_html(app_with_panel())
    assert "Paper trading, one click" in html
    assert "/actions/runner-start" in html
    assert "no_strategy_selected" in html
    assert "/actions/paper-start" not in html


def test_no_raw_exception_text_reaches_the_page():
    panel = FakePanel()

    def explode(action, payload):
        raise RuntimeError("connection reset by peer")

    panel.act = explode  # type: ignore[assignment]
    app = app_with_panel(panel)
    status, redirect, message = app.act("quick-setup-save", {"nonce": [app.nonce]})
    assert redirect == "/quick-setup"
    assert "connection reset" not in message
    assert "RuntimeError" not in message
    html = page_html(app)
    assert "connection reset" not in html


# -- actions ---------------------------------------------------------------


def test_the_four_actions_reach_the_panel_with_the_forms_values():
    panel = FakePanel()
    app = app_with_panel(panel)

    status, redirect, _ = app.act(
        "quick-setup-save",
        {
            "nonce": [app.nonce],
            "symbols": ["EURUSD.pro", "GBPUSD.pro"],
            "lot_size": ["0.20"],
            "max_daily_loss_account": ["100"],
            "max_order_notional_account": ["5000"],
            "max_spread_pips": ["1.5"],
        },
    )
    assert int(status) == 303 and redirect == "/quick-setup"
    action, payload = panel.calls[-1]
    assert action == "quick-setup-save"
    # Repeated names stay lists so a multi-select is not silently truncated.
    assert payload["symbols"] == ["EURUSD.pro", "GBPUSD.pro"]
    assert payload["lot_size"] == "0.20"

    app.act("research-fetch", {"nonce": [app.nonce], "timeframes": ["1h", "4h"]})
    assert panel.calls[-1][0] == "research-fetch"
    assert panel.calls[-1][1]["timeframes"] == ["1h", "4h"]

    app.act("research-run", {"nonce": [app.nonce]})
    assert panel.calls[-1][0] == "research-run"

    app.act("research-cancel", {"nonce": [app.nonce]})
    assert panel.calls[-1][0] == "research-cancel"


def test_a_refused_action_reports_the_panels_own_message():
    panel = FakePanel()
    app = app_with_panel(panel)
    status, redirect, message = app.act("research-fetch", {"nonce": [app.nonce]})
    assert int(status) == 409
    assert redirect == "/quick-setup"
    assert message == "that step was refused"


def test_an_unknown_action_is_still_not_found():
    app = app_with_panel()
    status, _redirect, message = app.act("research-delete-everything", {"nonce": [app.nonce]})
    assert int(status) == 404
    assert message == "unknown action"


def test_a_stale_form_is_refused_before_the_panel_is_touched():
    panel = FakePanel()
    app = app_with_panel(panel)
    status, _redirect, _message = app.act("quick-setup-save", {"nonce": ["stale"]})
    assert int(status) == 403
    assert panel.calls == []


def test_a_credential_in_the_form_is_refused_outright():
    panel = FakePanel()
    app = app_with_panel(panel)
    status, _redirect, message = app.act(
        "quick-setup-save", {"nonce": [app.nonce], "password": ["hunter2"]}
    )
    assert int(status) == 400
    assert "must not carry credentials" in message
    assert panel.calls == []


def test_the_new_actions_cannot_arm_or_trade():
    for action in ("quick-setup-save", "research-fetch", "research-run", "research-cancel"):
        assert action in POST_ACTIONS
        lowered = action.lower()
        for forbidden in ("order", "arm", "live", "mode", "account"):
            assert forbidden not in lowered


def test_the_panel_view_is_only_read_for_its_own_page():
    app = app_with_panel()
    overview = app._context("overview", "Overview")
    assert (overview.extra or {}).get("research") == {}
    quick = app._context("quick-setup", "Quick setup")
    assert (quick.extra or {}).get("research", {}).get("available") is True
