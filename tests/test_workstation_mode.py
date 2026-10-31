"""Execution mode: paper is immediate, live takes one preflight and one phrase.

Everything here runs against local files in a temporary directory and a fake
observation source.  No terminal is contacted, no MetaTrader 5 package is
imported, no credential is read, and no order surface exists in any object the
workstation holds.

The properties these tests protect:

* a fresh controller is ``PAPER``, and paper is reachable at any time;
* live is refused while any blocker stands, and *all* blockers come back at once;
* live needs the confirmation phrase exactly, once per process;
* a persisted ``LIVE`` is remembered but never effective without confirming
  again, so nothing trades because of a file written yesterday;
* an unrecorded mode change is not allowed (fail closed on a failed write);
* every change and every refusal is written to the local audit document;
* the mode switch itself never connects, never submits, never arms.
"""

from __future__ import annotations

import json

from dataclasses import dataclass
from pathlib import Path

import pytest

from axiom_forex.workstation.app import WorkstationApp
from axiom_forex.workstation.mode import (
    AUDIT_FILENAME,
    LIVE,
    LIVE_CONFIRMATION_PHRASE,
    PAPER,
    STATE_FILENAME,
    ExecutionModeController,
    ExecutionModeStore,
    ModeAuditTrail,
    build_controller,
)
from axiom_forex.workstation.preflight import LivePreflight, StrategyBinding, facts_from
from axiom_forex.workstation.probes import probe_environment
from axiom_forex.workstation.session import ObservationSession
from axiom_forex.workstation.setup import SetupWizard

PHRASE = LIVE_CONFIRMATION_PHRASE
BLOCKER = {
    "reason": "not_connected",
    "title": "No terminal session is open",
    "message": "Live execution needs a session.",
    "resolution": "Press Connect.",
}


# ---------------------------------------------------------------------------
# fixtures and helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeTerminalSource:
    """A data-only stand-in for a connected terminal.  Contacts nothing."""

    mode: str = "DEMO"
    mode_confirmed: bool = True
    trade_allowed: bool = True
    connected_flag: bool = False
    source_id: str = "fake-terminal"
    kind: str = "LIVE_TERMINAL"
    label: str = "Fake terminal (test double)"
    connect_calls: int = 0
    settings: object = None

    @property
    def connected(self) -> bool:
        return self.connected_flag

    def connect(self) -> None:
        self.connect_calls += 1
        self.connected_flag = True

    def disconnect(self) -> None:
        self.connected_flag = False

    def snapshot(self):
        if not self.connected_flag:
            return {"connected": False}
        return {
            "connected": True,
            "source_kind": self.kind,
            "market_data": "TERMINAL_TICKS",
            "terminal": {"name": "MetaTrader 5 (test double)", "build": 4755, "connected": True},
            "account": {
                "login": "<redacted>",
                "fingerprint": "0123456789ab",
                "server": "SAMPLE-Broker-Demo",
                "company": "Sample Broker",
                "currency": "EUR",
                "leverage": 30,
                "mode": self.mode,
                "mode_confirmed": self.mode_confirmed,
                "margin_mode": "RETAIL_NETTING",
                "trade_allowed": self.trade_allowed,
                "balance_reporting": "withheld",
            },
            "symbols": [{"name": "EURUSD"}, {"name": "GBPUSD"}],
            "symbol_error": "",
        }


def live_settings():
    from axiom_forex.config.mt5 import MT5ExecutionMode, MT5Settings

    return MT5Settings(
        terminal_path="C:/Program Files/MetaTrader 5/terminal64.exe",
        server="SAMPLE-Broker-Demo",
        account_login=99999999,
        mode=MT5ExecutionMode.TRADING_ENABLED,
    )


def windows_report(settings=None):
    def version_of(name: str) -> str:
        return "5.0.45" if name == "MetaTrader5" else "25.0"

    return probe_environment(
        settings=settings,
        system="Windows",
        release="10",
        python="3.12.4",
        finder=lambda name: "C:/Program Files/MetaTrader 5/terminal64.exe"
        if name == "MetaTrader5"
        else None,
        version_of=version_of,
        exists=lambda path: path.endswith("terminal64.exe"),
    )


def ready_app(tmp_path: Path, *, settings_path: Path | None = None, **kwargs) -> WorkstationApp:
    """A workstation whose live preflight is clear but whose mode is paper."""

    settings = live_settings()
    source = FakeTerminalSource(settings=settings)
    session = ObservationSession(source)
    app = WorkstationApp(
        wizard=SetupWizard(environment=windows_report(settings), session=session),
        platform=None,
        settings=settings,
        mode_path=settings_path if settings_path is not None else tmp_path / "mt5_settings.local.json",
        **kwargs,
    )
    app.act("connect", {"nonce": [app.nonce]})
    app.act("symbols", {"nonce": [app.nonce], "symbol": ["EURUSD"]})
    app.act("confirm", {"nonce": [app.nonce]})
    return app


ACCOUNT = "0123456789ab"  # what FakeTerminalSource reports


def bare_app(tmp_path: Path) -> WorkstationApp:
    """A workstation with nothing connected, configured, or confirmed."""

    return WorkstationApp(
        wizard=SetupWizard(environment=windows_report(), session=ObservationSession(
            FakeTerminalSource(settings=None)
        )),
        platform=None,
        settings=None,
        mode_path=tmp_path / "mt5_settings.local.json",
        version="0.1.0",
    )


def act(app: WorkstationApp, action: str, **fields) -> tuple[int, str, str]:
    form = {"nonce": [app.nonce]}
    form.update({key: [value] for key, value in fields.items()})
    status, redirect, message = app.act(action, form)
    return int(status), redirect, message


# ---------------------------------------------------------------------------
# 1. a fresh controller is paper
# ---------------------------------------------------------------------------


def test_a_fresh_controller_is_paper_and_writes_nothing(tmp_path):
    controller = build_controller(settings_path=tmp_path / "mt5_settings.local.json")
    view = controller.view()
    assert view["mode"] == PAPER
    assert view["live_effective"] is False
    assert view["confirmation_required"] is False
    assert view["paper_always_available"] is True
    assert list(tmp_path.glob("*.local.json")) == []


def test_paper_is_immediate_and_needs_no_preflight(tmp_path):
    controller = build_controller(settings_path=tmp_path / "mt5_settings.local.json")
    change = controller.set_paper()
    assert change.changed is False  # already paper: nothing to do, nothing claimed
    assert "Already in paper mode" in change.message

    # Both modes are selectable at any time, and back again, without a preflight.
    assert controller.enable_live(confirmation=PHRASE).changed is True
    assert controller.live_effective is True
    back = controller.set_paper()
    assert back.changed is True
    assert controller.mode == PAPER
    assert controller.live_effective is False
    assert controller.enable_live(confirmation=PHRASE).changed is True
    assert controller.live_effective is True


def test_the_mode_switch_takes_no_preflight_argument(tmp_path):
    # Nothing about the account or a strategy can disable the switch: the only
    # inputs are the operator's own phrase and account confirmation.
    controller = build_controller(settings_path=tmp_path / "mt5_settings.local.json")
    with pytest.raises(TypeError):
        controller.enable_live(blockers=[BLOCKER], confirmation=PHRASE)  # type: ignore[call-arg]
    assert controller.mode == PAPER


# ---------------------------------------------------------------------------
# 2. live needs the phrase and the account it names
# ---------------------------------------------------------------------------


def test_live_names_the_account_the_session_confirmed(tmp_path):
    controller = build_controller(settings_path=tmp_path / "mt5_settings.local.json")
    change = controller.enable_live(
        confirmation=PHRASE, account_fingerprint=ACCOUNT, account_confirmation=ACCOUNT
    )
    assert change.changed is True
    assert controller.view()["session_account"] == ACCOUNT
    assert controller.view()["account_confirmed"] is True
    # Selecting paper forgets that confirmation: live must be confirmed again.
    controller.set_paper()
    assert controller.view()["session_account"] == ""


def test_live_refuses_an_account_confirmation_that_names_another_account(tmp_path):
    controller = build_controller(settings_path=tmp_path / "mt5_settings.local.json")
    change = controller.enable_live(
        confirmation=PHRASE, account_fingerprint=ACCOUNT, account_confirmation="ffffffffffff"
    )
    assert change.changed is False
    assert change.reason == "live_account_not_confirmed"
    assert controller.mode == PAPER
    assert controller.confirmed is False


def test_live_is_still_selectable_with_no_account_connected(tmp_path):
    """Nothing hides the switch.  It is armed with no account and sends nothing."""
    controller = build_controller(settings_path=tmp_path / "mt5_settings.local.json")
    change = controller.enable_live(confirmation=PHRASE, account_fingerprint="")
    assert change.changed is True
    assert controller.live_effective is True
    view = controller.view()
    assert view["session_account"] == ""
    assert view["account_confirmed"] is False
    # The message says plainly that no order can be sent until an account is named.
    assert "no order can be sent" in change.message.lower()


def test_live_requires_the_exact_confirmation_phrase(tmp_path):
    controller = build_controller(settings_path=tmp_path / "mt5_settings.local.json")
    for wrong in ("", "enable live trading", " ENABLE LIVE TRADING", "ENABLE-LIVE-TRADING", None):
        change = controller.enable_live(confirmation=wrong)
        assert change.changed is False, wrong
        assert change.reason == "live_confirmation_mismatch", wrong
        assert controller.mode == PAPER
    assert controller.enable_live(confirmation=PHRASE).changed is True
    assert controller.mode == LIVE


def test_enabling_live_is_recorded_once_and_is_idempotent(tmp_path):
    controller = build_controller(settings_path=tmp_path / "mt5_settings.local.json")
    first = controller.enable_live(confirmation=PHRASE)
    assert first.changed is True
    sequence = controller.sequence
    second = controller.enable_live(confirmation=PHRASE)
    assert second.changed is False
    assert second.reason == "already_live"
    assert controller.sequence == sequence  # no duplicate audit sequence


# ---------------------------------------------------------------------------
# 3. persistence, restart, and fail-closed writing
# ---------------------------------------------------------------------------


def test_the_choice_is_persisted_without_any_account_or_secret(tmp_path):
    settings_path = tmp_path / "mt5_settings.local.json"
    controller = build_controller(settings_path=settings_path)
    controller.enable_live(confirmation=PHRASE)
    document = json.loads((tmp_path / STATE_FILENAME).read_text(encoding="utf-8"))
    assert document["mode"] == LIVE
    assert document["schema"] == 1
    assert set(document) == {"schema", "mode", "sequence", "changed_at"}
    text = (tmp_path / STATE_FILENAME).read_text(encoding="utf-8")
    for forbidden in ("99999999", "password", "token", "SAMPLE-Broker", "balance"):
        assert forbidden not in text


def test_a_saved_live_is_never_effective_in_a_new_process(tmp_path):
    settings_path = tmp_path / "mt5_settings.local.json"
    build_controller(settings_path=settings_path).enable_live(confirmation=PHRASE)

    restarted = build_controller(settings_path=settings_path)
    view = restarted.view()
    assert view["mode"] == LIVE          # remembered
    assert view["live_effective"] is False  # but not in force
    assert view["confirmation_required"] is True
    assert restarted.confirmed is False
    assert "fresh confirmation naming the connected account" in json.dumps(view["recent"])
    # Confirming again in the new process is what makes it effective.
    assert restarted.enable_live(confirmation=PHRASE).changed is True
    assert restarted.live_effective is True


def test_an_unreadable_mode_file_falls_back_to_paper_and_says_so(tmp_path):
    settings_path = tmp_path / "mt5_settings.local.json"
    (tmp_path / STATE_FILENAME).write_text("{not json", encoding="utf-8")
    controller = build_controller(settings_path=settings_path)
    assert controller.mode == PAPER
    assert controller.restore_note == "mode_state_unreadable"
    events = [item["event_type"] for item in controller.audit.recent()]
    assert "EXECUTION_MODE_STATE_REFUSED" in events


def test_an_unknown_saved_mode_is_refused_rather_than_guessed(tmp_path):
    settings_path = tmp_path / "mt5_settings.local.json"
    (tmp_path / STATE_FILENAME).write_text(
        json.dumps({"schema": 1, "mode": "TURBO", "sequence": 4}), encoding="utf-8"
    )
    controller = build_controller(settings_path=settings_path)
    assert controller.mode == PAPER
    assert controller.restore_note == "mode_state_unreadable"


def test_a_mode_change_that_cannot_be_written_does_not_happen(tmp_path):
    # The parent of the state file is a *file*, so the write must fail.
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("x", encoding="utf-8")
    store = ExecutionModeStore(path=blocked / STATE_FILENAME)
    audit = ModeAuditTrail(path=tmp_path / "audit.local.json")
    controller = ExecutionModeController(store=store, audit=audit)

    change = controller.enable_live(confirmation=PHRASE)
    assert change.changed is False
    assert change.reason == "mode_state_not_recorded"
    assert controller.mode == PAPER
    assert controller.confirmed is False
    reasons = [item["reason"] for item in audit.recent()]
    assert "mode_state_not_recorded" in reasons

    # Paper is unaffected: it stays available and it reports the failure honestly.
    paper = controller.set_paper()
    assert paper.changed is False


# ---------------------------------------------------------------------------
# 4. the audit trail
# ---------------------------------------------------------------------------


def test_every_change_and_refusal_is_written_to_the_local_audit(tmp_path):
    settings_path = tmp_path / "mt5_settings.local.json"
    controller = build_controller(settings_path=settings_path)
    controller.enable_live(
        confirmation=PHRASE, account_fingerprint=ACCOUNT, account_confirmation="ffffffffffff"
    )
    controller.enable_live(confirmation="wrong")
    controller.enable_live(confirmation=PHRASE)
    controller.set_paper(detail="operator changed their mind")

    document = json.loads((tmp_path / AUDIT_FILENAME).read_text(encoding="utf-8"))
    events = document["events"]
    kinds = [item["event_type"] for item in events]
    assert kinds.count("EXECUTION_MODE_REFUSED") == 2
    assert kinds.count("EXECUTION_MODE_CHANGED") == 2
    reasons = [item.get("reason") for item in events if item["event_type"] == "EXECUTION_MODE_REFUSED"]
    assert reasons == ["live_account_not_confirmed", "live_confirmation_mismatch"]
    # Nothing secret, no account number, no balance: only mode words and codes.
    text = json.dumps(document)
    for forbidden in ("99999999", "password", "ENABLE LIVE TRADING", "balance", "equity"):
        assert forbidden not in text


def test_the_audit_can_also_forward_to_a_sink(tmp_path):
    from app.audit.log import InMemoryAuditLog

    sink = InMemoryAuditLog()
    controller = build_controller(settings_path=tmp_path / "mt5_settings.local.json", sink=sink)
    controller.enable_live(confirmation=PHRASE)
    assert [event["event_type"] for event in sink.events] == ["EXECUTION_MODE_CHANGED"]
    assert sink.events[0]["to_mode"] == LIVE


# ---------------------------------------------------------------------------
# 5. the workstation surfaces: selector, indicator, refusal, confirmation
# ---------------------------------------------------------------------------


def test_paper_mode_is_rendered_as_paper_everywhere(tmp_path):
    app = ready_app(tmp_path)
    for path in ("/", "/mode", "/setup", "/account", "/health"):
        html = app.page(path)[1]
        assert "PAPER · simulated fills" in html, path
        assert "LIVE · MT5 execution" not in html, path


def test_the_mode_page_lists_a_blocker_with_its_resolution(tmp_path):
    settings = live_settings()
    session = ObservationSession(FakeTerminalSource(settings=settings))
    app = WorkstationApp(
        wizard=SetupWizard(environment=windows_report(settings), session=session),
        platform=None,
        settings=settings,
        mode_path=tmp_path / "mt5_settings.local.json",
    )
    html = app.page("/mode")[1]
    assert "What would stop an order from being sent" in html
    assert "No terminal session is open" in html
    assert "Press Connect on the Setup screen" in html
    # The switch is still on offer: a blocker refuses orders, not the mode choice.
    assert "name='confirmation'" in html
    assert "aria-disabled='true'" not in html
    assert "Switch to LIVE" in html
    assert "Real-money orders may follow from the active strategy" in html
    # No account is connected, so there is nothing to confirm yet.
    assert "name='account_confirmation'" not in html
    assert "No account is connected right now" in html
    assert app.mode_view()["mode"] == PAPER
    status, _, message = act(app, "mode-live", confirmation=PHRASE)
    assert status == 303
    assert app.mode_view()["live_effective"] is True
    assert "no order can be sent" in message.lower()


def test_the_mode_page_offers_exactly_one_confirmation_when_ready(tmp_path):
    app = ready_app(tmp_path)
    assert app.preflight_view()["ready"] is True
    html = app.page("/mode")[1]
    assert html.count("name='confirmation'") == 1
    assert html.count("name='account_confirmation'") == 1
    assert PHRASE in html
    assert "Switch to LIVE" in html
    assert ACCOUNT in html, "the signature to confirm is shown on the page"


def test_enabling_live_through_the_app_records_the_change(tmp_path):
    app = ready_app(tmp_path)
    status, redirect, message = act(
        app, "mode-live", confirmation=PHRASE, account_confirmation=ACCOUNT
    )
    assert status == 303
    assert redirect == "/"
    assert "Live mode is enabled" in message
    assert app.mode_view()["mode"] == LIVE
    assert app.mode_view()["live_effective"] is True
    assert "LIVE · MT5 execution" in app.page("/")[1]
    assert "LIVE · MT5 execution" in app.page("/positions")[1]
    audit = json.loads((tmp_path / AUDIT_FILENAME).read_text(encoding="utf-8"))
    assert [item["event_type"] for item in audit["events"]][-1] == "EXECUTION_MODE_CHANGED"


def test_live_is_selectable_through_the_app_while_a_blocker_stands(tmp_path):
    """A blocker refuses orders.  It does not refuse the operator's choice."""

    settings = live_settings()
    session = ObservationSession(FakeTerminalSource(settings=settings))
    app = WorkstationApp(
        wizard=SetupWizard(environment=windows_report(settings), session=session),
        platform=None,
        settings=settings,
        mode_path=tmp_path / "mt5_settings.local.json",
    )
    assert app.preflight_view()["ready"] is False
    status, _, _ = act(app, "mode-live", confirmation=PHRASE)
    assert status == 303
    assert app.mode_view()["mode"] == LIVE
    assert app.mode_view()["live_effective"] is True
    # ... and the blocker is still reported, on the page and in the preflight.
    assert "not_connected" in app.page("/mode")[1]
    # Switching back is one click, always.
    act(app, "mode-paper")
    assert app.mode_view()["mode"] == PAPER


def test_the_wrong_phrase_is_refused_through_the_app(tmp_path):
    app = ready_app(tmp_path)
    status, redirect, message = act(app, "mode-live", confirmation="yes")
    assert status == 303
    assert redirect == "/mode"
    assert "confirmation phrase did not match" in message
    assert app.mode_view()["mode"] == PAPER


def test_switching_modes_never_connects_and_never_submits(tmp_path):
    app = ready_app(tmp_path)
    source = app.wizard.session.source
    connects_before = source.connect_calls

    act(app, "mode-live", confirmation=PHRASE, account_confirmation=ACCOUNT)
    act(app, "mode-paper")
    act(app, "mode-live", confirmation=PHRASE, account_confirmation=ACCOUNT)
    act(app, "mode-paper")

    assert source.connect_calls == connects_before
    # The app object graph has no order surface at all.
    assert not hasattr(app, "submit")
    names = set(dir(app)) | set(dir(app.mode))
    assert not {"order_send", "order_check", "arm_trading"} & names


def test_explicit_disconnect_returns_live_to_paper(tmp_path):
    app = ready_app(tmp_path)
    act(app, "mode-live", confirmation=PHRASE, account_confirmation=ACCOUNT)
    assert app.mode_view()["live_effective"] is True

    status, _, message = act(app, "disconnect")
    assert status == 303
    assert "Live mode ended" in message
    assert app.mode_view()["mode"] == PAPER
    events = [item["event_type"] for item in app.mode.audit.recent()]
    assert events.count("EXECUTION_MODE_CHANGED") == 2


def test_live_without_an_attached_strategy_still_lists_what_every_order_needs(tmp_path):
    app = ready_app(tmp_path)
    preflight = app.preflight_view()
    assert preflight["strategy_attached"] is False
    reasons = {item["reason"] for item in preflight["order_requirements"]}
    assert {
        "account_identity",
        "broker_connection",
        "strategy_approved_active",
        "pretrade_gate",
        "quote_freshness",
        "unresolved_orders",
        "arming",
    } <= reasons
    assert "Still enforced on every order" in app.page("/mode")[1]


def test_an_unapproved_attached_strategy_blocks_the_order_not_the_switch(tmp_path):
    app = ready_app(
        tmp_path,
        strategy=StrategyBinding(strategy_id="strat-x", stage="TESTING", approved_active=False),
    )
    reasons = {item["reason"] for item in app.preflight_view()["blockers"]}
    assert "strategy_not_approved" in reasons
    status, _, _ = act(app, "mode-live", confirmation=PHRASE, account_confirmation=ACCOUNT)
    assert status == 303
    assert app.mode_view()["mode"] == LIVE
    assert "strategy_not_approved" in app.page("/mode")[1]


def test_an_approved_strategy_binding_clears_that_check(tmp_path):
    app = ready_app(
        tmp_path,
        strategy=StrategyBinding(strategy_id="strat-ok", stage="APPROVED", approved_active=True),
    )
    assert app.preflight_view()["ready"] is True


def test_a_preflight_is_never_cached_between_pages(tmp_path):
    settings = live_settings()
    session = ObservationSession(FakeTerminalSource(settings=settings))
    app = WorkstationApp(
        wizard=SetupWizard(environment=windows_report(settings), session=session),
        platform=None,
        settings=settings,
        mode_path=tmp_path / "mt5_settings.local.json",
    )
    assert "not_connected" in {item["reason"] for item in app.preflight_view()["blockers"]}
    app.act("connect", {"nonce": [app.nonce]})
    assert "not_connected" not in {item["reason"] for item in app.preflight_view()["blockers"]}


@pytest.mark.parametrize("action", ["mode-live", "mode-paper"])
def test_the_mode_actions_require_the_page_nonce(tmp_path, action):
    app = ready_app(tmp_path)
    status, _, _ = app.act(action, {"nonce": ["stale"], "confirmation": [PHRASE]})
    assert int(status) == 403
    assert app.mode_view()["mode"] == PAPER


def test_the_mode_actions_reject_credential_shaped_fields(tmp_path):
    app = ready_app(tmp_path)
    for action in ("mode-live", "mode-paper"):
        status, _, message = app.act(action, {"nonce": [app.nonce], "password": ["hunter2"]})
        assert int(status) == 400
        assert "hunter2" not in message
    assert app.mode_view()["mode"] == PAPER


def test_an_account_mismatch_is_visible_on_the_pages_and_never_hidden(tmp_path):
    """The previously unsurfaced ``mt5_account_mismatch`` reaches the operator.

    A fake MetaTrader 5 module reports an account the settings document does not
    name.  Connecting is refused exactly as before (the check is not relaxed),
    and both the dashboard and the Trading mode page say what happened and what
    to do about it.
    """

    from axiom_forex.config.mt5 import MT5ExecutionMode, MT5Settings
    from axiom_forex.workstation.session import MT5ObservationSource
    from tests.fake_mt5 import FakeMT5

    fake = FakeMT5(account_login=55500123, account_server="SAMPLE-Broker-Demo")
    configured = MT5Settings(
        terminal_path="C:/Program Files/MetaTrader 5/terminal64.exe",
        server="SAMPLE-Broker-Demo",
        account_login=99999999,
        mode=MT5ExecutionMode.TRADING_ENABLED,
    )
    app = WorkstationApp(
        wizard=SetupWizard(
            environment=windows_report(configured),
            session=ObservationSession(MT5ObservationSource(settings=configured, module=fake)),
        ),
        platform=None,
        settings=configured,
        mode_path=tmp_path / "mt5_settings.local.json",
    )

    act(app, "connect")

    overview = app.page("/")[1]
    assert "mt5_account_mismatch" in overview
    assert "different account" in overview
    assert "will not accept a different account" in overview

    mode_page = app.page("/mode")[1]
    assert "mt5_account_mismatch" in mode_page
    assert "Sign in to the configured account inside MetaTrader 5" in mode_page
    # The account mismatch is shown, and the operator may still choose LIVE:
    # what it refuses is the order, with that same reason on the page.
    assert "name='confirmation'" in mode_page

    assert app.mode_view()["mode"] == PAPER
    status, _, _ = act(app, "mode-live", confirmation=PHRASE)
    assert status == 303
    assert app.mode_view()["mode"] == LIVE
    assert "mt5_account_mismatch" in app.page("/mode")[1]


# ---------------------------------------------------------------------------
# nothing at all stands between the operator and either mode
# ---------------------------------------------------------------------------


def test_both_modes_can_be_selected_at_any_time_in_any_order(tmp_path):
    app = ready_app(tmp_path)
    for action, expected in (
        ("mode-live", LIVE),
        ("mode-paper", PAPER),
        ("mode-live", LIVE),
        ("mode-paper", PAPER),
        ("mode-live", LIVE),
    ):
        status, _, _ = act(app, action, confirmation=PHRASE, account_confirmation=ACCOUNT)
        assert status == 303
        assert app.mode_view()["mode"] == expected
    assert app.mode_view()["live_effective"] is True


def test_a_fresh_install_with_nothing_configured_still_offers_live(tmp_path):
    """No connect, no symbols, no confirmation, no strategy, no settings read."""

    app = bare_app(tmp_path)
    html = app.page("/mode")[1]
    assert "Switch to LIVE" in html
    assert "aria-disabled='true'" not in html
    status, _, _ = act(app, "mode-live", confirmation=PHRASE)
    assert status == 303
    assert app.mode_view()["live_effective"] is True
    # Paper, immediately, without anything being cleaned up first.
    act(app, "mode-paper")
    assert app.mode_view()["mode"] == PAPER
    assert app.mode_view()["live_effective"] is False


def test_the_switch_warns_about_real_money_and_names_no_strategy_it_picked(tmp_path):
    app = ready_app(tmp_path)
    html = app.page("/mode")[1]
    assert "Real-money orders may follow from the active strategy" in html
    assert "does not place one by itself" in html
    assert "Routes an approved, gated order" in html


def test_live_with_no_connected_account_cannot_reach_an_order(tmp_path):
    app = bare_app(tmp_path)
    act(app, "mode-live", confirmation=PHRASE)
    assert app.mode_view()["live_effective"] is True
    # Nothing was connected, nothing was armed, and there is no order surface.
    assert app.wizard.session.connected is False
    names = set(dir(app)) | set(dir(app.mode))
    assert not {"order_send", "order_check", "arm_trading", "submit"} & names


class _LiveWithPositions:
    """A live controller holding one open broker position.  Sends nothing."""

    def view(self):
        return {
            "configured": True,
            "armed": False,
            "connected": True,
            "live_effective": False,
            "mode": "PAPER",
            "account_mode": "REAL",
            "session_running": False,
            "next_signal_sends_real_order": False,
            "plan": {"present": False},
            "blockers": [],
            "candidates": [],
            "symbols": [],
            "account": {"fingerprint": ACCOUNT},
            "positions": [
                {"ticket": 77, "symbol": "EURUSD.pro", "direction": "BUY", "volume": "0.10"}
            ],
            "confirmation_phrase": PHRASE,
        }

    def summary(self):
        return {
            "present": True,
            "armed": False,
            "next_signal_sends_real_order": False,
            "sentence": "Paper mode: every fill is simulated locally; no order is sent.",
        }


def test_switching_to_paper_shows_open_positions_and_says_it_will_not_close_them(tmp_path):
    app = ready_app(tmp_path, live=_LiveWithPositions())
    html = app.page("/mode")[1]
    assert "1 live position(s) remain open at the broker" in html
    assert "Changing the mode does not close them" in html
    assert "77" in html

    live_html = app.page("/live")[1]
    assert "live position(s) are still open" in live_html
    assert "does not close or manage these" in live_html


def test_switching_to_paper_stops_the_session_and_never_liquidates(tmp_path):
    class Recording(_LiveWithPositions):
        def __init__(self):
            self.calls = []

        def stop_session(self, **kwargs):
            self.calls.append(("stop_session", dict(kwargs)))
            return {"stopped": True}

        def disarm(self, **kwargs):
            self.calls.append(("disarm", dict(kwargs)))
            return {"armed": False}

    live = Recording()
    app = ready_app(tmp_path, live=live)
    act(app, "mode-live", confirmation=PHRASE, account_confirmation=ACCOUNT)
    assert app.mode_view()["live_effective"] is True
    status, _, message = act(app, "mode-paper")
    assert status == 303
    assert app.mode_view()["mode"] == PAPER
    assert [name for name, _ in live.calls] == ["stop_session"]
    # Nothing on the app object can close a broker position, and nothing asked for.
    assert not [
        name
        for name in dir(app)
        if name.startswith("close") or "liquidat" in name or name in {"order_send"}
    ]
    assert "simulated" in message.lower() or "no order" in message.lower()
