"""The ``/kronos`` page: a separate control panel with an honest status.

What is pinned here:

* the page exists on its own route, and is not reachable from the live or mode
  pages -- a forecast is never an order path;
* every state the runtime can be in is worded as what it is, including "this
  deployment has no forecasting component";
* the model-file card never offers a download, and the licence/version are shown
  before anything is used;
* a stale nonce is refused, and every action that changes something is on the
  POST allowlist;
* the page can be rendered with no bar source, no model and no torch present --
  which is exactly this sandbox -- and still says something true.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app.data.models import ForexQuoteBar, QuoteSideBar
from axiom_forex.kronos.panel import KronosPanel
from axiom_forex.kronos.settings import KronosSettings
from axiom_forex.workstation.app import CREDENTIAL_FIELDS, GET_ROUTES, POST_ACTIONS, WorkstationApp
from tests.test_workstation_ui import app_for, simulated_wizard

UTC = timezone.utc
START = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)
PIP = Decimal("0.0001")


def make_bar(mid: Decimal, timestamp: datetime) -> ForexQuoteBar:
    half = PIP / Decimal(2)
    bid, ask = mid - half, mid + half
    return ForexQuoteBar(
        pair="EURUSD",
        timeframe="1m",
        timestamp=timestamp,
        duration_seconds=60,
        bid=QuoteSideBar(open=bid, high=bid + half, low=bid - half, close=bid),
        ask=QuoteSideBar(open=ask, high=ask + half, low=ask - half, close=ask),
        source="KRONOS_PAGE_TEST_BARS",
        quote_count=7,
        last_quote_at=timestamp + timedelta(seconds=59),
        complete=True,
    )


def bars(count: int = 200) -> list[ForexQuoteBar]:
    series = []
    mid = Decimal("1.1000")
    for index in range(count):
        series.append(make_bar(mid, START + timedelta(minutes=index)))
        mid += PIP / Decimal(4)
    return series


SERIES = bars()


class FakePredictor:
    device = "cpu"

    def predict(self, **kwargs):
        rows = []
        base = 1.1000
        for index in range(int(kwargs["pred_len"])):
            close = base + 0.0001 * index
            rows.append(
                {"open": close, "high": close + 0.00002, "low": close - 0.00002, "close": close}
            )
        return rows


def fake_factory(settings):
    from axiom_forex.kronos.forecast import LocalKronosForecaster

    return LocalKronosForecaster(
        settings,
        predictor_factory=lambda _settings: FakePredictor(),
        frame_builder=lambda data: ([tuple(row.values()) for row in data.rows], None, None),
    )


def panel(tmp_path: Path, **kwargs) -> KronosPanel:
    params = dict(
        root=tmp_path,
        bars_provider=lambda symbol, timeframe, count: SERIES[-count:],
        forecaster_factory=fake_factory,
    )
    params.update(kwargs)
    instance = KronosPanel(**params)
    instance.save(
        {
            "symbol": ["EURUSD"],
            "timeframe": ["1m"],
            "lookback": ["120"],
            "pred_len": ["4"],
            "rollouts": ["2"],
        }
    )
    return instance


def app_with_kronos(tmp_path, **kwargs) -> WorkstationApp:
    app = app_for(simulated_wizard())
    app.kronos = panel(tmp_path, **kwargs)
    return app


# ---------------------------------------------------------------------------
# routing and the allowlist
# ---------------------------------------------------------------------------


def test_kronos_has_its_own_route_and_a_navigation_entry():
    assert GET_ROUTES["/kronos"] == ("kronos", "Kronos forecast")
    from axiom_forex.workstation.views import NAV

    assert any(key == "kronos" and href == "/kronos" for key, href, _label in NAV)
    # It is a separate page from provider setup and from the live path.
    assert GET_ROUTES["/provider-setup"][0] == "provider-setup"
    assert GET_ROUTES["/live"][0] == "live"


def test_every_kronos_action_is_on_the_allowlist_and_none_can_trade():
    kronos_actions = {name for name in POST_ACTIONS if name.startswith("kronos-")}
    assert kronos_actions == {
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
    }
    for name in kronos_actions:
        for forbidden in ("order", "trade", "arm", "mode", "risk", "live"):
            assert forbidden not in name


def test_a_stale_nonce_is_refused_before_anything_runs(tmp_path):
    app = app_with_kronos(tmp_path)
    status, _, message = app.act("kronos-load", {"nonce": ["stale"]})
    assert int(status) == 403
    assert "stale" in message
    assert app.kronos is not None
    assert app.kronos.runtime().view()["state"] == "NOT_LOADED"


def test_an_unknown_kronos_action_is_refused_not_guessed(tmp_path):
    app = app_with_kronos(tmp_path)
    status, _, message = app.act("kronos-trade-now", {"nonce": [app.nonce]})
    assert int(status) == 404


def test_kronos_actions_never_accept_a_credential(tmp_path):
    app = app_with_kronos(tmp_path)
    for name in CREDENTIAL_FIELDS:
        status, _, message = app.act("kronos-save", {"nonce": [app.nonce], name: ["x"]})
        assert int(status) == 400
        assert "credential" in message


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def test_the_page_renders_with_no_component_and_says_so():
    app = app_for(simulated_wizard())
    assert app.kronos is None
    status, html = app.page("/kronos")
    assert int(status) == 200
    assert "not wired into this workstation" in html
    assert "optional" in html


def test_the_page_renders_in_this_sandbox_where_torch_is_absent(tmp_path):
    app = app_with_kronos(tmp_path)
    status, html = app.page("/kronos")
    assert int(status) == 200
    # Forecast-only is stated on the page, not implied.
    assert "Forecast only" in html
    assert "cannot place an order" in html
    # The model-file card says outright that nothing is fetched for you.
    assert "no automatic download" in html
    assert "AXIOM does not download weights" in html
    # Every dependency the sandbox lacks is listed with its requirement line.
    assert "torch&gt;=2.0.0" in html or "torch>=2.0.0" in html
    # The model limits come from the variant, and the unsupported settings are
    # documented rather than offered.
    assert "Settings Kronos does not support" in html
    assert "beam_search" in html
    # Nothing on the page invites an order.
    assert "order_send" not in html
    assert "Send order" not in html


def test_the_settings_form_only_exposes_supported_controls(tmp_path):
    app = app_with_kronos(tmp_path)
    _, html = app.page("/kronos")
    for name in ("temperature", "top_k", "top_p", "rollouts", "pred_len", "max_context", "clip"):
        assert f"name='{name}'" in html
    # Not a Kronos argument, so it is not a control.
    for invented in ("name='dtype'", "name='num_beams'", "name='batch_size'"):
        assert invented not in html


def test_a_successful_save_redirects_and_reports_on_the_next_render(tmp_path):
    app = app_with_kronos(tmp_path)
    status, redirect, message = app.act(
        "kronos-save",
        {"nonce": [app.nonce], "symbol": ["EURUSD"], "lookback": ["120"], "rollouts": ["3"]},
    )
    assert int(status) == 303
    assert redirect == "/kronos"
    assert message == ""
    _, html = app.page("/kronos")
    assert "Settings saved locally" in html
    # The line is shown once, not forever.
    _, again = app.page("/kronos")
    assert "Settings saved locally" not in again


def test_an_invalid_setting_is_refused_with_the_reason_and_nothing_written(tmp_path):
    app = app_with_kronos(tmp_path)
    before = (tmp_path / "kronos_settings.local.json").read_text()
    status, _, message = app.act(
        "kronos-save",
        {"nonce": [app.nonce], "symbol": [""], "lookback": ["5"], "rollouts": ["2"]},
    )
    assert int(status) == 409
    assert "kronos.symbol" in message
    assert "kronos.lookback" in message
    assert (tmp_path / "kronos_settings.local.json").read_text() == before


def test_loading_a_model_here_reports_the_missing_runtime_not_a_fake_success(tmp_path):
    # No forecaster is injected, so the default factory is used: it finds torch
    # absent and refuses. That refusal must reach the operator.
    instance = KronosPanel(
        root=tmp_path,
        bars_provider=lambda symbol, timeframe, count: SERIES[-count:],
    )
    instance.save({"symbol": ["EURUSD"], "lookback": ["120"], "pred_len": ["4"]})
    app = app_for(simulated_wizard())
    app.kronos = instance
    status, _, message = app.act("kronos-load", {"nonce": [app.nonce]})
    assert int(status) == 409
    assert "torch_missing" in message
    assert "Nothing was downloaded or installed" in message


def test_a_forecast_is_recorded_and_shown_without_creating_anything(tmp_path):
    app = app_with_kronos(tmp_path)
    app.act("kronos-load", {"nonce": [app.nonce]})
    status, redirect, _ = app.act("kronos-forecast", {"nonce": [app.nonce]})
    assert int(status) == 303
    assert redirect == "/kronos"
    _, html = app.page("/kronos")
    assert "The last forecast" in html
    assert "forecast only" in html
    # The history row exists, and it is waiting for its horizon.
    assert "waiting for its horizon" in html
    # No live object was created or touched by forecasting.
    assert app.live is None


def test_the_history_survives_a_restart_of_the_page_object(tmp_path):
    app = app_with_kronos(tmp_path)
    app.act("kronos-load", {"nonce": [app.nonce]})
    app.act("kronos-forecast", {"nonce": [app.nonce]})

    reopened = KronosPanel(
        root=tmp_path,
        bars_provider=lambda symbol, timeframe, count: SERIES[-count:],
        forecaster_factory=fake_factory,
    )
    app2 = app_for(simulated_wizard())
    app2.kronos = reopened
    _, html = app2.page("/kronos")
    assert "Recorded forecasts and what happened" in html
    assert "kronos-small" in html


def test_presets_are_saved_beside_the_launcher_and_listed(tmp_path):
    app = app_with_kronos(tmp_path)
    status, _, _ = app.act(
        "kronos-preset-save", {"nonce": [app.nonce], "preset_name": ["quiet"]}
    )
    assert int(status) == 303
    assert (tmp_path / "kronos_presets.local.json").exists()
    _, html = app.page("/kronos")
    assert "quiet" in html


def test_the_benchmark_runs_against_the_baselines_and_labels_itself(tmp_path):
    app = app_with_kronos(tmp_path)
    status, _, _ = app.act(
        "kronos-benchmark",
        {"nonce": [app.nonce], "symbol": ["EURUSD"], "timeframe": ["1m"], "include_kronos": ["no"]},
    )
    assert int(status) == 303
    _, html = app.page("/kronos")
    assert "Forecast error and spread-adjusted direction. Not profit." in html
    assert "hold" in html and "drift" in html and "momentum" in html


def test_without_a_bar_source_the_page_refuses_to_forecast(tmp_path):
    instance = KronosPanel(root=tmp_path, forecaster_factory=fake_factory)
    instance.save({"symbol": ["EURUSD"], "lookback": ["120"], "pred_len": ["4"]})
    app = app_for(simulated_wizard())
    app.kronos = instance
    app.act("kronos-load", {"nonce": [app.nonce]})
    status, _, message = app.act("kronos-forecast", {"nonce": [app.nonce]})
    assert int(status) == 409
    assert "no_bar_source" in message
    _, html = app.page("/kronos")
    assert "No bar source is wired in" in html
