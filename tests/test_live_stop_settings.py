"""Per-symbol broker-side stop distances: the document, the control, the guard.

What these tests pin down:

* the local settings document stores one distance **per pair**, written with its
  unit, and only that section is ever rewritten;
* a value is judged against the connected broker's own symbol record before it is
  written, and refused when it is missing a unit, unreadable, or inside the
  broker's minimum distance;
* the write is refused while trading is armed or a session is running, because the
  adapter reads its settings when it is built;
* the page never shows a saved-but-unloaded value as if it were the enforced one;
* the arming check refuses a plan whose symbol has no usable distance.

No terminal, account, or order is involved.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from types import SimpleNamespace

import pytest

from axiom_forex.config.mt5 import (
    MT5Settings,
    MT5SettingsFileError,
    load_settings_file,
    write_protective_stop,
)
from axiom_forex.paper_runner.live_workflow import LiveTrading
from axiom_forex.workstation.views import _broker_stop_settings_card
from tests.test_mt5_adapter import make_spec

SPEC = make_spec()  # EUR/USD, point 0.00001, pip 0.0001, stops_level 0


def settings_document(tmp_path, **overrides):
    document = {
        "terminal_path": "terminal64.exe",
        "server": "Broker-Demo",
        "account_login": 55500123,
        "mode": "TRADING_ENABLED",
        "symbol_suffix": ".pro",
        "magic": 26081001,
        "protective_stops": {"GBP/USD": "30 pips"},
    }
    document.update(overrides)
    path = tmp_path / "mt5_settings.local.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path, document


# -- the document -----------------------------------------------------------


def test_a_distance_is_written_per_pair_and_everything_else_is_kept(tmp_path):
    path, before = settings_document(tmp_path)
    write_protective_stop(path, pair="eurusd", distance="20 pips")
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["protective_stops"] == {"GBP/USD": "30 pips", "EUR/USD": "20 pips"}
    for key, value in before.items():
        if key != "protective_stops":
            assert written[key] == value, key
    # And the file still loads through the launcher's own validator.
    settings = load_settings_file(path)
    assert settings is not None
    assert settings.stop_distance_for("EUR/USD") == (Decimal("20"), "pips")


def test_a_second_spelling_of_one_pair_replaces_the_first(tmp_path):
    path, _ = settings_document(tmp_path)
    write_protective_stop(path, pair="GBP/USD", distance="25 pips")
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["protective_stops"] == {"GBP/USD": "25 pips"}


@pytest.mark.parametrize(
    ("distance", "reason"),
    [
        ("20", "invalid_protective_stop_value"),
        ("0 pips", "invalid_protective_stop_value"),
        ("-5 points", "invalid_protective_stop_value"),
        ("20 bananas", "invalid_protective_stop_value"),
    ],
)
def test_an_unusable_value_is_refused_and_the_file_is_untouched(
    tmp_path, distance, reason
):
    path, _ = settings_document(tmp_path)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(MT5SettingsFileError) as excinfo:
        write_protective_stop(path, pair="EUR/USD", distance=distance)
    assert excinfo.value.reason == reason
    assert path.read_text(encoding="utf-8") == before


def test_a_missing_document_is_never_created(tmp_path):
    with pytest.raises(MT5SettingsFileError) as excinfo:
        write_protective_stop(tmp_path / "absent.json", pair="EUR/USD", distance="20 pips")
    assert excinfo.value.reason == "mt5_settings_file_missing"
    assert not (tmp_path / "absent.json").exists()


def test_a_document_with_a_secret_is_refused_rather_than_rewritten(tmp_path):
    path, _ = settings_document(tmp_path, investor_password="hunter2")
    before = path.read_text(encoding="utf-8")
    with pytest.raises(MT5SettingsFileError) as excinfo:
        write_protective_stop(path, pair="EUR/USD", distance="20 pips")
    assert excinfo.value.reason == "mt5_settings_file_contains_secret"
    assert path.read_text(encoding="utf-8") == before


def test_the_canonical_text_is_what_lands_in_the_file(tmp_path):
    path, _ = settings_document(tmp_path)
    write_protective_stop(path, pair="EUR/USD", distance="200 Points")
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["protective_stops"]["EUR/USD"] == "200 points"


# -- the live workflow's own write path -------------------------------------


class StubAdapter:
    """Just enough adapter to answer ``stop_requirement`` for the arming checks."""

    def __init__(self, *, declared: str = "20 pips", points: int = 200, valid: bool = True,
                 reason: str = "", min_points: int = 0):
        self.settings = MT5Settings(
            terminal_path="terminal64.exe",
            server="Broker-Demo",
            account_login=1,
            protective_stops={"EUR/USD": declared} if declared else {},
        )
        self._declared = declared
        self._points = points
        self._valid = valid
        self._reason = reason
        self._min_points = min_points
        self.armed = False
        self.connected = True

    def stop_requirement(self, spec, *, text=None, symbol=None, info=None):
        declared = self._declared if text is None else str(text)
        return {
            "pair": "EUR/USD",
            "broker_symbol": "EURUSD.pro",
            "declared": declared,
            "unit": "pips" if declared.endswith("pips") else "points",
            "distance_points": self._points,
            "distance_price": "0.002",
            "point": "0.00001",
            "pip_size": "0.0001",
            "broker_min_points": self._min_points,
            "broker_min_price": str(Decimal("0.00001") * self._min_points),
            "valid": self._valid,
            "reason": self._reason,
            "detail": "" if self._valid else "the broker's minimum is larger",
        }


def controller(tmp_path, *, adapter=None, plan_pair: str = "EUR/USD"):
    instance = LiveTrading(
        adapter=adapter or StubAdapter(),
        stack_builder=lambda pair: SimpleNamespace(spec=SPEC),
        settings_path=tmp_path / "mt5_settings.local.json",
    )
    instance.plan = {"pair": plan_pair}
    return instance


def test_saving_writes_the_pair_and_reports_that_it_is_not_in_force_yet(tmp_path):
    path, _ = settings_document(tmp_path)
    instance = controller(tmp_path)
    result = instance.save_stop_setting("EUR/USD", "20 pips")
    assert result["saved"] is True
    assert result["pending_reconnect"] is True
    assert "reconnect" in result["message"].lower()
    assert json.loads(path.read_text(encoding="utf-8"))["protective_stops"]["EUR/USD"] == (
        "20 pips"
    )


def test_saving_is_refused_while_armed(tmp_path):
    path, _ = settings_document(tmp_path)
    before = path.read_text(encoding="utf-8")
    instance = controller(tmp_path)
    instance.armed_strategy = SimpleNamespace(armed=True)
    result = instance.save_stop_setting("EUR/USD", "20 pips")
    assert result["saved"] is False
    assert result["reason"] == "live_trading_armed"
    assert path.read_text(encoding="utf-8") == before


def test_saving_is_refused_while_a_session_is_running(tmp_path):
    path, _ = settings_document(tmp_path)
    before = path.read_text(encoding="utf-8")
    instance = controller(tmp_path)
    instance.session = SimpleNamespace(view=lambda: {"running": True})
    result = instance.save_stop_setting("EUR/USD", "20 pips")
    assert result["saved"] is False
    assert result["reason"] == "live_session_running"
    assert path.read_text(encoding="utf-8") == before


def test_saving_is_refused_when_the_workstation_has_no_settings_file(tmp_path):
    instance = LiveTrading(
        adapter=StubAdapter(),
        stack_builder=lambda pair: SimpleNamespace(spec=SPEC),
        settings_path=None,
    )
    result = instance.save_stop_setting("EUR/USD", "20 pips")
    assert result["saved"] is False
    assert result["reason"] == "live_settings_path_unknown"


def test_a_distance_below_the_broker_minimum_is_refused_before_the_write(tmp_path):
    path, _ = settings_document(tmp_path)
    before = path.read_text(encoding="utf-8")
    adapter = StubAdapter(
        valid=False, reason="mt5_protective_stop_below_broker_minimum", min_points=500
    )
    instance = controller(tmp_path, adapter=adapter)
    result = instance.save_stop_setting("EUR/USD", "20 pips")
    assert result["saved"] is False
    assert result["reason"] == "mt5_protective_stop_below_broker_minimum"
    assert path.read_text(encoding="utf-8") == before


def test_a_saved_value_the_running_adapter_does_not_use_is_shown_as_pending(tmp_path):
    settings_document(tmp_path)  # GBP/USD only
    write_protective_stop(
        tmp_path / "mt5_settings.local.json", pair="EUR/USD", distance="25 pips"
    )
    instance = controller(tmp_path)  # the adapter still enforces 20 pips
    setting = instance.stop_setting()
    assert setting["declared"] == "20 pips"
    assert setting["file_declared"] == "25 pips"
    assert setting["pending"] is True


# -- the arming guard -------------------------------------------------------


def test_arming_is_refused_when_the_symbol_has_no_declared_distance():
    instance = LiveTrading(adapter=StubAdapter(declared="", valid=False,
                                              reason="mt5_protective_stop_not_configured_for_symbol"))
    blockers = instance._broker_stop_blockers(SPEC)
    assert [item.reason for item in blockers] == ["broker_stop_not_configured"]
    assert "EUR/USD" in blockers[0].message


def test_arming_is_refused_when_the_distance_is_inside_the_broker_minimum():
    instance = LiveTrading(
        adapter=StubAdapter(
            valid=False, reason="mt5_protective_stop_below_broker_minimum", min_points=500
        )
    )
    blockers = instance._broker_stop_blockers(SPEC)
    assert [item.reason for item in blockers] == ["broker_stop_below_broker_minimum"]
    assert "500" in blockers[0].message


def test_a_usable_distance_blocks_nothing():
    instance = LiveTrading(adapter=StubAdapter())
    assert instance._broker_stop_blockers(SPEC) == []


def test_an_unreadable_adapter_is_reported_not_assumed():
    class Broken(StubAdapter):
        def stop_requirement(self, *args, **kwargs):
            raise RuntimeError("terminal gone")

    instance = LiveTrading(adapter=Broken())
    blockers = instance._broker_stop_blockers(SPEC)
    assert [item.reason for item in blockers] == ["broker_stop_unverifiable"]


# -- the page ---------------------------------------------------------------


def page(setting: dict, **view) -> str:
    return _broker_stop_settings_card(
        {"stop_setting": setting, **view}, "nonce-1"
    )


def usable_setting(**overrides) -> dict:
    setting = {
        "available": True,
        "pair": "EUR/USD",
        "broker_symbol": "EURUSD.pro",
        "declared": "20 pips",
        "file_declared": "20 pips",
        "pending": False,
        "unit": "pips",
        "distance": "20",
        "distance_points": 200,
        "distance_price": "0.002",
        "point": "0.00001",
        "pip_size": "0.0001",
        "broker_min_points": 0,
        "broker_min_price": "0",
        "valid": True,
        "reason": "",
        "detail": "",
    }
    setting.update(overrides)
    return setting


def test_the_page_shows_the_symbol_units_point_size_and_broker_minimum():
    html = page(usable_setting(broker_min_points=100, broker_min_price="0.001"))
    assert "EURUSD.pro" in html
    assert "20 pips" in html
    assert "unit: pips" in html
    assert "0.00001" in html  # point size
    assert "0.0001" in html  # pip size
    assert "100 point(s)" in html
    assert "0.001" in html


def test_the_page_never_pre_fills_a_distance_or_preselects_a_unit():
    html = page(usable_setting())
    assert "placeholder='e.g. 20'" in html
    assert "<option value=''>Choose points or pips…</option>" in html
    assert "<option value='pips'>" in html and "<option value='points'>" in html


def test_the_page_hides_the_form_while_armed():
    html = page(usable_setting(), armed=True, session_running=False)
    assert "Stop and disarm to change the distance" in html
    assert "Save the distance" not in html


def test_the_page_states_a_saved_value_is_not_yet_enforced():
    html = page(usable_setting(file_declared="25 pips", pending=True))
    assert "Saved, not yet in force" in html
    assert "25 pips" in html
    assert "Reconnect" in html


def test_the_page_says_which_reason_made_a_value_unusable():
    html = page(
        usable_setting(
            valid=False,
            reason="mt5_protective_stop_below_broker_minimum",
            detail="150 point(s) is inside the broker's minimum of 500",
        )
    )
    assert "not usable" in html
    assert "mt5_protective_stop_below_broker_minimum" in html
    assert "inside the broker&#x27;s minimum of 500" in html or "minimum of 500" in html


# -- the POST action --------------------------------------------------------


def workstation():
    """A real workstation object with a stubbed live workflow attached."""

    from axiom_forex.facade import build_fixture_platform
    from axiom_forex.workstation.app import WorkstationApp
    from axiom_forex.workstation.session import locked_session
    from axiom_forex.workstation.setup import SetupWizard

    from tests.test_quick_setup_ui import offline_environment

    wizard = SetupWizard(
        environment=offline_environment(), session=locked_session("no_settings_document")
    )
    return WorkstationApp(wizard=wizard, platform=build_fixture_platform())


class RecordingLive:
    def __init__(self, result=None):
        self.calls: list[tuple[str, str]] = []
        self.result = result or {"saved": True, "pair": "EUR/USD", "distance": "20 pips",
                                 "message": "Saved 20 pips for EUR/USD. Reconnect to apply."}

    def save_stop_setting(self, pair, text):
        self.calls.append((pair, text))
        return dict(self.result)


def form(app, **fields):
    payload = {"nonce": [app.nonce]}
    payload.update({key: [value] for key, value in fields.items()})
    return payload


def test_the_action_refuses_without_an_explicit_unit():
    app = workstation()
    app.live = RecordingLive()
    status, path, message = app.act(
        "live-stop-settings", form(app, pair="EUR/USD", distance="20", unit="")
    )
    assert status == 303 and path == "/live"
    assert "points or pips" in message
    assert app.live.calls == []


def test_the_action_refuses_an_empty_distance():
    app = workstation()
    app.live = RecordingLive()
    status, path, message = app.act(
        "live-stop-settings", form(app, pair="EUR/USD", distance=" ", unit="pips")
    )
    assert status == 303
    assert "Enter a distance" in message
    assert app.live.calls == []


def test_the_action_reports_a_refusal_verbatim_and_saves_nothing():
    app = workstation()
    app.live = RecordingLive(
        {"saved": False, "reason": "mt5_protective_stop_below_broker_minimum",
         "message": "150 point(s) is inside the broker's minimum of 500"}
    )
    status, path, message = app.act(
        "live-stop-settings", form(app, pair="EUR/USD", distance="15", unit="pips")
    )
    assert status == 303
    assert message.startswith("Not saved:")
    assert "minimum of 500" in message
    # The unit is part of the value the workflow was asked to judge.
    assert app.live.calls == [("EUR/USD", "15 pips")]


def test_the_action_passes_the_unit_to_the_workflow_and_reports_the_message():
    app = workstation()
    app.live = RecordingLive()
    status, path, message = app.act(
        "live-stop-settings", form(app, pair="EUR/USD", distance="200", unit="points")
    )
    assert status == 303 and path == "/live"
    assert app.live.calls == [("EUR/USD", "200 points")]
    assert "Reconnect" in message


def test_the_action_needs_a_live_workflow_at_all():
    app = workstation()
    app.live = None
    status, path, message = app.act(
        "live-stop-settings", form(app, pair="EUR/USD", distance="20", unit="pips")
    )
    assert status == 400
    assert "no live trading adapter" in message


def test_the_page_states_how_many_broker_points_one_pip_is():
    html = page(usable_setting())
    assert "one pip is 10 point(s)" in html


def test_the_page_says_so_when_it_cannot_work_out_pip_size():
    html = page(usable_setting(pip_size="", point=""))
    assert "one pip is not reported" in html


def test_a_unit_that_is_a_typo_is_not_read_as_a_unit(tmp_path):
    """``"20 pipss"`` must not become 20 pips: a guessed unit is a guessed risk."""

    path, _ = settings_document(tmp_path)
    with pytest.raises(MT5SettingsFileError) as excinfo:
        write_protective_stop(path, pair="EUR/USD", distance="20 pipss")
    assert excinfo.value.reason == "invalid_protective_stop_value"


def test_the_note_about_the_watched_limit_never_denies_the_broker_stop():
    """One banner, two numbers. It may not say the account has no broker stop."""

    from axiom_forex.paper_runner.live_plan import PROTECTIVE_STOP_NOTE

    text = PROTECTIVE_STOP_NOTE.lower()
    assert "sends no" not in text
    assert "broker-side stop" in text
    assert "separate" in text


# -- the control is reachable before a plan exists --------------------------


def test_without_a_plan_the_control_offers_the_declared_symbols():
    """Arming needs a stop; the stop must not need an armed plan first."""

    html = page(usable_setting(pair="", pairs=["EUR/USD", "GBP/USD"]))
    assert "not chosen yet" in html
    assert "<select name='pair' required>" in html
    assert "<option value=''>Choose the symbol…</option>" in html
    assert "<option value='EUR/USD'>EUR/USD</option>" in html
    # Nothing is pre-selected, and no pair is smuggled in as a hidden field.
    assert "selected='selected'" not in html
    assert not re.search(r"<option[^>]*\bselected\b", html)
    assert "name='pair' value=" not in html


def test_with_a_plan_the_symbol_is_fixed_to_it():
    html = page(usable_setting(pairs=["EUR/USD", "GBP/USD"]))
    assert "<input type='hidden' name='pair' value='EUR/USD'>" in html
    assert "<select name='pair'" not in html


def test_with_neither_a_plan_nor_a_declared_symbol_the_control_says_so():
    html = page(usable_setting(pair="", pairs=[]))
    assert "No symbol to configure yet" in html
    assert "action='/actions/live-stop-settings'" not in html


def test_a_stop_can_be_saved_before_any_plan_exists(tmp_path):
    """The order was: arm needs a stop, the stop needed a plan. Now it does not."""

    path, _ = settings_document(tmp_path)
    instance = LiveTrading(
        adapter=StubAdapter(),
        stack_builder=lambda pair: SimpleNamespace(spec=SPEC),
        settings_path=tmp_path / "mt5_settings.local.json",
        paired_symbols=["EUR/USD", "GBP/USD"],
    )
    assert instance.plan == {}
    assert instance.stop_setting()["pairs"] == ["EUR/USD", "GBP/USD"]

    result = instance.save_stop_setting("EUR/USD", "20 pips")

    assert result["saved"] is True
    assert json.loads(path.read_text(encoding="utf-8"))["protective_stops"]["EUR/USD"] == (
        "20 pips"
    )


def test_the_action_refuses_an_empty_symbol_without_touching_the_file(tmp_path):
    path, _ = settings_document(tmp_path)
    before = path.read_text(encoding="utf-8")
    app = workstation()
    recording = RecordingLive()
    recording.save_stop_setting = lambda pair, text: (_ for _ in ()).throw(
        AssertionError("the workflow must not be asked to save an unnamed symbol")
    )
    app.live = recording
    status, path_part, message = app.act(
        "live-stop-settings", form(app, pair="", distance="20", unit="pips")
    )
    assert status == 303 and path_part == "/live"
    assert "symbol" in message.lower()
    assert path.read_text(encoding="utf-8") == before
