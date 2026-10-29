"""The live preflight: all the blockers, each with the way out.

Two things matter here and are asserted directly:

1. **nothing is hidden** -- in particular ``mt5_account_mismatch``, which the
   terminal reports when MetaTrader 5 is signed in to a different account than
   the settings document names.  It reaches the operator as a named blocker with
   the sign-in-and-reconnect step, and the account check is *not* relaxed.
2. **nothing is guessed** -- a missing fact produces a blocker, never a pass.
   The preflight reads only what was already gathered, so it can be run on any
   machine without a terminal.
"""

from __future__ import annotations

import json

from axiom_forex.config.mt5 import MT5ExecutionMode, MT5Settings
from axiom_forex.workstation.preflight import (
    ORDER_REQUIREMENTS,
    LivePreflight,
    StrategyBinding,
    collect_blockers,
    dedupe,
    facts_from,
)
from axiom_forex.workstation.probes import probe_environment
from axiom_forex.workstation.session import MT5ObservationSource, ObservationSession
from tests.fake_mt5 import FakeMT5

PHRASE = "ENABLE LIVE TRADING"


def settings(**overrides):
    values = dict(
        terminal_path="C:/Program Files/MetaTrader 5/terminal64.exe",
        server="SAMPLE-Broker-Demo",
        account_login=99999999,
        mode=MT5ExecutionMode.TRADING_ENABLED,
    )
    values.update(overrides)
    return MT5Settings(**values)


def windows_report(settings_value=None):
    return probe_environment(
        settings=settings_value,
        system="Windows",
        release="10",
        python="3.12.4",
        finder=lambda name: "C:/Program Files/MetaTrader 5/terminal64.exe"
        if name == "MetaTrader5"
        else None,
        version_of=lambda name: "5.0.45" if name == "MetaTrader5" else "25.0",
        exists=lambda path: path.endswith("terminal64.exe"),
    )


def linux_report(settings_value=None):
    return probe_environment(
        settings=settings_value,
        system="Linux",
        release="6.1.0",
        python="3.11.9",
        finder=lambda name: None,
        version_of=lambda name: None,
    )


def connected_session_view(
    *,
    server="SAMPLE-Broker-Demo",
    mode="DEMO",
    mode_confirmed=True,
    trade_allowed=True,
):
    snapshot = {
        "connected": True,
        "source_kind": "LIVE_TERMINAL",
        "market_data": "TERMINAL_TICKS",
        "terminal": {"name": "MetaTrader 5", "build": 4755, "connected": True},
        "account": {
            "login": "<redacted>",
            "fingerprint": "0123456789ab",
            "server": server,
            "company": "Sample Broker",
            "currency": "EUR",
            "leverage": 30,
            "mode": mode,
            "mode_confirmed": mode_confirmed,
            "margin_mode": "RETAIL_NETTING",
            "trade_allowed": trade_allowed,
            "balance_reporting": "withheld",
        },
        "symbols": [{"name": "EURUSD"}],
    }
    return {
        "state": "CONNECTED",
        "connected": True,
        "source_kind": "LIVE_TERMINAL",
        "snapshot": snapshot,
        "error": None,
        "market_data": "TERMINAL_TICKS",
    }


def disconnected_view(**extra):
    view = {
        "state": "DISCONNECTED",
        "connected": False,
        "source_kind": "LIVE_TERMINAL",
        "snapshot": {},
        "error": None,
        "market_data": "NONE",
    }
    view.update(extra)
    return view


def issue(facts) -> dict[str, object]:
    return {blocker.reason: blocker for blocker in collect_blockers(facts)}


def ready_facts(**overrides):
    values = dict(
        settings=settings(),
        environment=windows_report(settings()),
        session=connected_session_view(),
        confirmed=True,
        selected_symbols=("EURUSD",),
        strategy=StrategyBinding("strat-ok", "APPROVED", approved_active=True),
    )
    values.update(overrides)
    return facts_from(**values)


# ---------------------------------------------------------------------------
# the clear case
# ---------------------------------------------------------------------------


def test_everything_lined_up_means_no_blockers():
    blockers = collect_blockers(ready_facts())
    assert blockers == []
    preflight = LivePreflight(provider=lambda: ready_facts())
    view = preflight.view()
    assert view["ready"] is True
    assert view["count"] == 0
    assert "Every live check passed" in view["summary"]
    json.dumps(view, default=str)  # the view is audit-safe


# ---------------------------------------------------------------------------
# one blocker per fact, with a resolution
# ---------------------------------------------------------------------------


def test_missing_settings_document_is_named_with_the_template_step():
    blockers = issue(
        facts_from(
            settings=None,
            settings_reason="no_settings_document",
            environment=windows_report(None),
            session=disconnected_view(),
        )
    )
    assert "no_settings_document" in blockers
    assert "template on the Setup screen" in blockers["no_settings_document"].resolution


def test_observe_only_settings_block_live_execution():
    blockers = issue(ready_facts(settings=settings(mode=MT5ExecutionMode.OBSERVE_ONLY)))
    assert "trading_not_enabled_in_settings" in blockers
    assert "TRADING_ENABLED" in blockers["trading_not_enabled_in_settings"].resolution


def test_invalid_settings_are_reported_with_their_reason():
    blockers = issue(ready_facts(settings=settings(server="")))
    assert "mt5_settings_invalid" in blockers


def test_a_simulated_session_can_never_reach_live():
    simulated = {
        "state": "CONNECTED",
        "connected": True,
        "source_kind": "SIMULATED",
        "snapshot": {"connected": True, "account": {"mode": "DEMO", "sample": True}},
        "error": None,
    }
    blockers = issue(ready_facts(session=simulated))
    assert "simulated_session_only" in blockers
    assert "without --simulate" in blockers["simulated_session_only"].resolution


def test_an_unconfigured_workstation_is_named():
    unconfigured = {
        "state": "DISCONNECTED",
        "connected": False,
        "source_kind": "UNCONFIGURED",
        "snapshot": {},
        "error": None,
    }
    blockers = issue(ready_facts(session=unconfigured))
    assert "workstation_unconfigured" in blockers


def test_not_connected_is_a_blocker_with_the_connect_step():
    blockers = issue(ready_facts(session=disconnected_view()))
    assert "not_connected" in blockers
    assert "Press Connect" in blockers["not_connected"].resolution


def test_the_environment_checks_are_reported_separately():
    report = linux_report(settings())
    blockers = issue(ready_facts(environment=report))
    assert "not_windows" in blockers
    assert "metatrader5_package_missing" in blockers
    assert "mt5_terminal_not_found" in blockers
    for blocker in blockers.values():
        assert blocker.resolution


def test_an_unconfirmed_account_type_blocks_live():
    blockers = issue(ready_facts(session=connected_session_view(mode_confirmed=False)))
    assert "account_mode_unconfirmed" in blockers
    assert "demo or real" in blockers["account_mode_unconfirmed"].message


def test_a_read_only_terminal_session_blocks_live():
    blockers = issue(ready_facts(session=connected_session_view(trade_allowed=False)))
    assert "terminal_trading_not_allowed" in blockers
    assert "Algo Trading" in blockers["terminal_trading_not_allowed"].resolution


def test_missing_symbols_and_confirmation_are_named():
    blockers = issue(ready_facts(selected_symbols=(), confirmed=False))
    assert "symbols_not_selected" in blockers
    assert "configuration_not_confirmed" in blockers


# ---------------------------------------------------------------------------
# account identity: surfaced, never relaxed
# ---------------------------------------------------------------------------


def test_a_different_account_signing_in_is_surfaced_not_accepted():
    """The exact scenario behind ``mt5_account_mismatch``.

    A fake module reports an account the settings document does not name.  The
    session refuses to connect (unchanged behaviour), and the preflight turns
    that refusal into a named blocker with the fix -- rather than losing it.
    """

    fake = FakeMT5(account_login=55500123, account_server="SAMPLE-Broker-Demo")
    misconfigured = settings(account_login=99999999, server="SAMPLE-Broker-Demo")
    source = MT5ObservationSource(settings=misconfigured, module=fake)
    session = ObservationSession(source)
    view = session.connect()

    assert view["connected"] is False
    assert view["error"]["reason"] == "mt5_account_mismatch"
    hint = view["error"]["hint"]
    assert "different account" in hint
    assert "will not accept a different account" in hint

    blockers = issue(
        facts_from(
            settings=misconfigured,
            environment=windows_report(misconfigured),
            session=view,
            confirmed=True,
            selected_symbols=("EURUSD",),
        )
    )
    blocker = blockers["mt5_account_mismatch"]
    assert "different account" in blocker.message
    assert "Sign in to the configured account inside MetaTrader 5" in blocker.resolution
    assert blocker.where == "in MetaTrader 5"
    assert blocker.category == "account"
    # The generic "not connected" line is not stacked on top of the real reason.
    assert "not_connected" not in blockers
    # ... and the account check itself is unchanged: nothing was learned as valid.
    assert fake.shutdown_calls == 1


def test_a_server_mismatch_detected_after_connecting_is_a_blocker():
    blockers = issue(ready_facts(session=connected_session_view(server="OTHER-Server")))
    assert "mt5_server_mismatch" in blockers
    assert "different server" in blockers["mt5_server_mismatch"].message


def test_one_entry_per_reason():
    facts = ready_facts(session=connected_session_view(server="OTHER-Server"))
    blockers = collect_blockers(facts)
    reasons = [blocker.reason for blocker in blockers]
    assert reasons.count("mt5_server_mismatch") == 1
    assert dedupe(blockers) == tuple(blockers)


# ---------------------------------------------------------------------------
# a strategy is never invented
# ---------------------------------------------------------------------------


def test_no_attached_strategy_is_not_a_blocker_but_the_order_checks_still_list_it():
    facts = ready_facts(strategy=None)
    assert collect_blockers(facts) == []
    reasons = {item["reason"] for item in ORDER_REQUIREMENTS}
    assert "strategy_approved_active" in reasons
    assert "pretrade_gate" in reasons
    assert "account_identity" in reasons
    assert "arming" in reasons
    view = LivePreflight(provider=lambda: facts).view()
    assert view["strategy_attached"] is False


def test_an_unapproved_attached_strategy_is_a_blocker():
    blockers = issue(
        ready_facts(strategy=StrategyBinding("strat-x", "TESTING", approved_active=False))
    )
    assert "strategy_not_approved" in blockers
    assert "does not approve strategies for you" in blockers["strategy_not_approved"].resolution


# ---------------------------------------------------------------------------
# shape
# ---------------------------------------------------------------------------


def test_blocker_order_follows_the_workflow_and_every_item_has_a_fix():
    facts = facts_from(
        settings=None,
        environment=linux_report(None),
        session=disconnected_view(),
        confirmed=False,
        selected_symbols=(),
    )
    blockers = collect_blockers(facts)
    reasons = [blocker.reason for blocker in blockers]
    assert reasons.index("not_windows") < reasons.index("not_connected")
    assert reasons.index("not_connected") < reasons.index("configuration_not_confirmed")
    for blocker in blockers:
        assert blocker.title and blocker.message and blocker.resolution
        assert blocker.category and blocker.where
        payload = blocker.audit_view()
        json.dumps(payload)


def test_the_preflight_recomputes_from_its_provider():
    state = {"facts": ready_facts(session=disconnected_view())}
    preflight = LivePreflight(provider=lambda: state["facts"])
    assert preflight.view()["ready"] is False
    state["facts"] = ready_facts()
    assert preflight.view()["ready"] is True
