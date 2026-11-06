"""The ``/quick-setup`` page, with the catalog card rendered from real panel data."""

from __future__ import annotations

import time

from axiom_forex.facade import build_fixture_platform
from axiom_forex.strategies.catalog.panel import ACTION_EVALUATE, CatalogPanel
from axiom_forex.workstation import views
from axiom_forex.workstation.app import POST_ACTIONS, WorkstationApp
from axiom_forex.workstation.setup import SetupWizard

from tests.test_catalog_panel import _panel
from tests.test_quick_setup_ui import offline_environment


def _app(panel: CatalogPanel) -> WorkstationApp:
    from tests.test_quick_setup_ui import FakePanel
    from axiom_forex.workstation.session import locked_session

    wizard = SetupWizard(
        environment=offline_environment(), session=locked_session("no_settings_document")
    )
    app = WorkstationApp(wizard=wizard, platform=build_fixture_platform())
    app.research = FakePanel()
    app.catalog = panel
    return app


def _page(app: WorkstationApp) -> str:
    ctx = app._context("quick-setup", "Quick setup")
    return views.render_page(ctx)


def test_the_catalog_card_offers_only_forex_and_one_start_button(tmp_path):
    panel = _panel(tmp_path)
    html = _page(_app(panel))
    assert "Catalog and the Monday paper test" in html
    assert "Start live-market paper test" in html
    # Stop and the flatten act on a built test.  With none built they could only
    # answer "no paper test is built", so they are not drawn at all -- and the page
    # says why, and that they appear once the test exists.
    assert "Stop the paper test" not in html
    assert "Simulate closing open positions" not in html
    assert "There is nothing to stop or close yet" in html
    assert "EUR/USD" in html
    assert "XAU/USD" not in html
    assert "catalog-evaluate" in html
    assert "no claim" in html.lower() or "makes no claim" in html


def test_the_stop_and_close_controls_appear_once_the_test_is_built(tmp_path):
    """The other half of the rule: a control is drawn when it can work."""

    from axiom_forex.strategies.catalog.panel import CatalogPaperTest

    panel = _panel(tmp_path)
    app = _app(panel)
    paper = CatalogPaperTest(quotes=panel._terminal(), watches=(), root=panel.paper_root())
    assert panel.adopt_paper(paper, ()) is True
    html = _page(app)
    assert "Stop the paper test" in html
    assert "There is nothing to stop or close yet" not in html


def test_the_card_shows_the_evaluation_and_the_paper_set(tmp_path):
    panel = _panel(tmp_path)
    app = _app(panel)
    assert panel.act(ACTION_EVALUATE, {"symbols": "EURUSD.pro", "timeframe": "15m"})["ok"] is True
    deadline = time.monotonic() + 60
    while panel.evaluation_view()["running"] and time.monotonic() < deadline:
        time.sleep(0.02)
    html = _page(app)
    assert "Evaluation on record" in html
    assert "passed the screen" in html


def test_the_actions_are_allowlisted_and_reach_only_the_catalog_panel(tmp_path):
    panel = _panel(tmp_path)
    app = _app(panel)
    for action in ("catalog-evaluate", "catalog-paper-start", "catalog-paper-stop", "catalog-paper-flat"):
        assert action in POST_ACTIONS
    # A start with no record is refused, and the message is the panel's, not an exception.
    status, path, message = app.act("catalog-paper-start", {"nonce": [app.nonce]})
    assert int(status) == 409
    assert path == "/quick-setup"
    assert "evaluation" in message.lower()
    assert "Traceback" not in message


def test_the_card_is_absent_when_no_catalog_panel_is_wired(tmp_path):
    app = _app(_panel(tmp_path))
    app.catalog = None
    html = _page(app)
    # No panel, no card, and no half-rendered controls that could post nowhere.
    assert "Catalog and the Monday paper test" not in html
    assert "catalog-paper-start" not in html
