"""Workstation controls for the REAL-only AX-SCALPER run.

The execution-boundary test uses a fake terminal deliberately: it proves that a
DEMO identity is refused before the order adapter is armed.  It is not broker or
strategy evidence.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from axiom_forex.brokers.mt5.identity import account_fingerprint
from axiom_forex.paper_runner.scalper_live_host import ARM_PHRASE, ScalperLiveAutonomousHost
from axiom_forex.scalper.live_config import LIVE_SCHEMA, RISK_KEYS
from axiom_forex.scalper.signals import default_strategies
from axiom_forex.workstation.ui.pages.live import render_live


SYMBOL = "EURUSD.pro"
SERVER = "Broker-Real"
LOGIN = 12345678


def _settings(path: Path) -> None:
    risk = {
        "risk_fraction": "0.001", "max_loss_per_trade_account": "10",
        "max_aggregate_risk_fraction": "0.005", "max_leverage": "3",
        "max_currency_exposure_fraction": "0.5", "max_correlated_positions": 1,
        "max_daily_loss_fraction": "0.01", "max_drawdown_fraction": "0.03",
        "max_loss_streak": 3, "max_spread_points_hard": "40",
        "max_slippage_points": "4", "max_fill_latency_ms": 500,
        "max_open_positions": 1, "max_quote_age_ms": 1000,
        "max_spread_points": "25", "max_hold_ms": 600000,
        "break_even_trigger_points": "8", "trail_after_points": "12",
        "trail_distance_points": "6",
    }
    assert set(risk) == set(RISK_KEYS)
    payload = {
        "schema": LIVE_SCHEMA,
        "symbol": SYMBOL,
        "margin_rate": "0.02",
        "costs": {
            "commission_per_lot": "7", "commission_state": "OBSERVED",
            "slippage_points": "1", "slippage_state": "ESTIMATE",
            "swap_state": "OBSERVED",
        },
        "risk": risk,
        "account": {
            "server": SERVER, "login": LOGIN, "currency": "USD",
            "fingerprint": account_fingerprint(LOGIN),
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


class _Session:
    connected = True

    def account(self):
        return {"currency": "USD", "mode": "DEMO"}

    def symbols(self):
        return ({
            "name": SYMBOL, "currency_base": "EUR", "currency_profit": "USD",
            "currency_margin": "EUR", "digits": 5, "point": Decimal("0.00001"),
            "trade_contract_size": Decimal("100000"), "volume_min": Decimal("0.01"),
            "volume_step": Decimal("0.01"), "volume_max": Decimal("10"),
            "trade_mode": 4, "stops_level": 10, "spread": 10,
            "swap_long": Decimal("-1"), "swap_short": Decimal("-1"),
            "swap_mode": 0, "swap_rollover3days": 3,
        },)

    def market_data_source(self):
        return self

    def symbol_info(self, symbol):
        assert symbol == SYMBOL
        return SimpleNamespace(
            name=symbol, tradable=True, point=Decimal("0.00001"), digits=5,
            trade_contract_size=Decimal("100000"), volume_min=Decimal("0.01"),
            volume_step=Decimal("0.01"), volume_max=Decimal("10"), stops_level=10,
            currency_base="EUR", currency_profit="USD",
        )


class _Adapter:
    armed = False

    def __init__(self):
        self.arm_calls = 0
        self.send_calls = 0

    def status(self):
        return {
            "connected": True, "account_mode": "DEMO", "account_mode_confirmed": True,
            "account_fingerprint": account_fingerprint(LOGIN),
            "redacted_account": {"server": SERVER, "currency": "USD"},
            "trading_enabled_setting": True,
            "terminal": {"balance": "10000", "equity": "10000"},
            "armed": self.armed,
        }

    def refresh_account(self):
        return None

    def arm_trading(self, **_kwargs):
        self.arm_calls += 1
        self.armed = True
        return {"armed": True}

    def disarm(self, **_kwargs):
        self.armed = False

    def order_send(self, *_args, **_kwargs):
        self.send_calls += 1
        raise AssertionError("the workstation DEMO refusal must happen before submission")


class _Admission:
    def __init__(self, artifact: Path, policy: Path):
        self.artifact = artifact
        self.policy = policy
        strategy = default_strategies()[0]
        from axiom_forex.scalper.admission import evidence_digest, load_policy

        digest = evidence_digest(json.loads(artifact.read_text(encoding="utf-8")),
                                 load_policy(policy, expected_currency="USD"))
        self.review = {
            "candidates": [{
                "fingerprint": strategy.fingerprint, "candidate_id": strategy.fingerprint,
                "name": strategy.name, "family": strategy.family,
                "eligible": True,
                "evidence": {"evidence_digest": digest},
                "artifact_path": str(artifact),
            }],
            "policy_path": str(policy),
        }

    def _strategy_review(self, **_kwargs):
        return self.review


class _Mode:
    mode = "LIVE"
    live_effective = True


class _Registry:
    def register_delegate(self, *_args):
        raise AssertionError("a DEMO account must be refused before lifecycle registration")

    def unregister_delegate(self, *_args):
        return None


class _Workflow:
    def __init__(self):
        self.session = SimpleNamespace(view=lambda: {"running": False})
        self.armed_strategy = _Registry()


def _host(tmp_path: Path):
    settings = tmp_path / "scalper_live_settings.local.json"
    _settings(settings)
    artifact = tmp_path / "signed-evidence.fixture.json"
    raw = tmp_path / "raw.fixture.csv"
    raw.write_text("synthetic data only\n", encoding="utf-8")
    from axiom_forex.scalper.raw_replay import source_digest
    artifact.write_text(json.dumps({"fixture": True, "data": {
        "file": raw.name, "sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
        "end": datetime.now(timezone.utc).isoformat(),
    }, "replay_config": {"strategy_source_digest": source_digest()}}), encoding="utf-8")
    policy = tmp_path / "policy.fixture.json"
    from tests.test_scalper_admission import policy_payload
    policy.write_text(json.dumps(policy_payload(bytes(32))), encoding="utf-8")
    adapter = _Adapter()
    platform_calls = []
    host = ScalperLiveAutonomousHost(
        session=_Session(), adapter=adapter, mode=_Mode(),
        platform_factory=lambda pair: platform_calls.append(pair),
        admission_host=_Admission(artifact, policy), live_workflow=_Workflow(),
        settings_path=settings, state_path=tmp_path / "run-state.local.json",
        poll_seconds=0.1,
    )
    return host, adapter, platform_calls


def test_demo_identity_refuses_before_platform_adapter_or_order_mutation(tmp_path):
    host, adapter, platform_calls = _host(tmp_path)
    try:
        candidate_id = default_strategies()[0].fingerprint
        result = host.arm(
            candidate_id=candidate_id,
            confirmation=ARM_PHRASE,
            account_confirmation=account_fingerprint(LOGIN),
        )
        assert result["ok"] is False
        assert result["reason"] == "real_account_required"
        assert adapter.arm_calls == 0
        assert adapter.send_calls == 0
        assert platform_calls == []
    finally:
        host.close()


def test_workstation_arm_action_reaches_runner_and_preserves_demo_refusal(tmp_path):
    from axiom_forex.workstation.app import WorkstationApp
    from tests.test_workstation_ui import live_wizard

    host, adapter, platform_calls = _host(tmp_path)
    app = WorkstationApp(wizard=live_wizard(), live=SimpleNamespace(
        view=lambda: {"configured": True, "positions": [], "account": {}},
        scalper_autonomous=host,
    ))
    try:
        result, redirect, message = app.act("ax-scalper-live-arm", {
            "nonce": [app.nonce],
            "candidate_id": [default_strategies()[0].fingerprint],
            "confirmation": [ARM_PHRASE],
            "account_confirmation": [account_fingerprint(LOGIN)],
        })
        assert int(result) == 409
        assert redirect == "/live"
        assert "real_account_required" in message
        assert adapter.arm_calls == 0
        assert adapter.send_calls == 0
        assert platform_calls == []
    finally:
        host.close()


def test_workstation_shutdown_stops_entries_disarms_and_never_closes_positions(tmp_path):
    host, adapter, _platform_calls = _host(tmp_path)

    class Runtime:
        state = "RUNNING"

        def __init__(self):
            self.stop_kwargs = None
            self.close_calls = 0
            self.venue = SimpleNamespace(
                position_details=lambda: [{"ticket": "broker-ticket-1"}]
            )

        def stop(self, **kwargs):
            self.stop_kwargs = kwargs
            self.state = "STOPPED"
            return {"ok": True, "state": "STOPPED",
                    "open_positions": [{"position_id": "position-1"}],
                    "message": "Entries stopped; position remains open."}

    runtime = Runtime()
    try:
        host.view()  # start only the owner thread; this performs read-only checks
        host._runtime = runtime
        host._armed_by_host = True
        host._strategy_id = "ax-scalper-live:test"
        adapter.armed = True
        result = host.close()
        assert result["ok"] is True
        assert runtime.stop_kwargs == {"reason": "operator_stop", "close_positions": False}
        assert runtime.close_calls == 0
        assert adapter.armed is False
        assert adapter.send_calls == 0
    finally:
        host.close()


def test_live_page_is_one_real_only_autopilot_and_has_no_demo_or_legacy_start():
    ctx = SimpleNamespace(
        action_nonce="test-nonce",
        extra={"live": {
            "configured": True,
            "scalper_autonomous": {
                "available": True, "state": "STOPPED", "account_mode": "DEMO",
                "connected": True, "mode": "LIVE", "live_effective": True,
                "settings_path": "scalper_live_settings.local.json",
                "state_path": "scalper_live_run.local.json",
                "blockers": [{"reason": "real_account_required",
                               "message": "The selected terminal account is DEMO.",
                               "hint": "Sign in to the declared REAL account in MT5."}],
                "candidates": [], "eligible_candidates": [], "config": {}, "run": {},
            },
        }},
    )
    html = render_live(ctx)
    assert "Autonomous scalping" in html
    assert "REAL ONLY" in html
    assert "The selected terminal account is DEMO." in html
    assert "action='/actions/demo-test-submit'" not in html
    assert "action='/actions/live-start'" not in html
    assert "action='/actions/autopilot-live-start'" not in html
    assert "action='/actions/scalper-live-submit'" not in html
    assert "Only a terminal-confirmed REAL account" in html


def test_live_page_renders_explicit_arm_and_start_forms_only_for_ready_admission():
    admitted = {
        "fingerprint": "candidate-exact-1", "name": "Scalper candidate",
        "family": "momentum", "timeframe": "M1", "eligible": True,
    }
    base = {
        "available": True, "state": "STOPPED", "account_mode": "REAL",
        "connected": True, "mode": "LIVE", "live_effective": True,
        "account_fingerprint": "fingerprint-exact", "adapter_armed": False,
        "candidates": [admitted], "eligible_candidates": [admitted],
        "config": {"symbol": SYMBOL}, "run": {}, "blockers": [],
        "settings_path": "scalper_live_settings.local.json",
        "state_path": "scalper_live_run.local.json",
    }
    ctx = SimpleNamespace(action_nonce="nonce", extra={"live": {"scalper_autonomous": base}})
    html = render_live(ctx)
    assert "ax-scalper-live-arm" in html
    assert "ARM AX-SCALPER LIVE" in html
    assert "value='candidate-exact-1'" in html
    armed = {**base, "state": "ARMED", "adapter_armed": True,
             "armed": True, "candidate_id": "candidate-exact-1"}
    ctx.extra["live"]["scalper_autonomous"] = armed
    html = render_live(ctx)
    assert "ax-scalper-live-start" in html
    assert "START AX-SCALPER LIVE" in html


def test_timed_out_queued_action_is_cancelled_before_it_can_run(tmp_path):
    import threading

    host, _adapter, _ = _host(tmp_path)
    entered, release = threading.Event(), threading.Event()
    executed = []

    def busy():
        entered.set()
        assert release.wait(5)
        return {"ok": True}

    host._do_busy = busy
    host._do_probe = lambda: executed.append(True) or {"ok": True}
    caller = threading.Thread(target=lambda: host._call("busy", {}, timeout=5))
    try:
        caller.start()
        assert entered.wait(2)
        result = host._call("probe", {}, timeout=0.01)
        assert result["reason"] == "live_runner_busy"
        release.set()
        caller.join(2)
        # FIFO barrier proves that the cancelled command was processed/skipped.
        assert host._call("view", {}, timeout=5)["ok"] is True
        assert executed == []
    finally:
        release.set()
        caller.join(2)
        host.close()


def test_timed_out_started_action_reports_unknown_in_progress(tmp_path):
    import threading

    host, _adapter, _ = _host(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def busy():
        entered.set()
        assert release.wait(5)
        return {"ok": True}

    host._do_busy = busy
    try:
        result = host._call("busy", {}, timeout=0.1)
        assert entered.is_set()
        assert result["reason"] == "live_runner_action_in_progress"
        assert "do not retry" in result["message"]
    finally:
        release.set()
        host.close()


def test_runner_accepts_production_admission_digest_not_raw_file_hash(tmp_path):
    host, _adapter, _ = _host(tmp_path)
    candidate = default_strategies()[0].fingerprint
    ok, prepared = host._prepare_candidate(candidate)
    assert ok, prepared
    semantic_digest = prepared[4]
    artifact = Path(host.admission_host.review["candidates"][0]["artifact_path"])
    assert semantic_digest != hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert prepared[6][str(artifact)] == hashlib.sha256(artifact.read_bytes()).hexdigest()


def test_runner_refuses_changed_evidence_after_admission_review(tmp_path):
    host, _adapter, _ = _host(tmp_path)
    artifact = Path(host.admission_host.review["candidates"][0]["artifact_path"])
    artifact.write_text('{"fixture":"changed"}', encoding="utf-8")
    ok, refusal = host._prepare_candidate(default_strategies()[0].fingerprint)
    assert not ok
    assert refusal["reason"] == "evidence_digest_changed"


def test_admission_lifecycle_revokes_changed_raw_data_or_expired_evidence(tmp_path):
    from datetime import timedelta
    from axiom_forex.paper_runner.scalper_live_host import _AdmissionLifecycle

    host, _adapter, _ = _host(tmp_path)
    candidate = default_strategies()[0].fingerprint
    ok, prepared = host._prepare_candidate(candidate)
    assert ok, prepared
    lifecycle = _AdmissionLifecycle(host, "strategy", prepared[6], **prepared[7])
    host._candidate_id = candidate
    host._strategy_id = "strategy"
    host._lifecycle = lifecycle
    host._armed_by_host = True
    assert host._lifecycle_active(lifecycle)
    lifecycle.data_path.write_text("changed synthetic data\n", encoding="utf-8")
    assert not host._lifecycle_active(lifecycle)
    lifecycle.data_path.write_text("synthetic data only\n", encoding="utf-8")
    assert host._lifecycle_active(lifecycle)
    lifecycle.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert not host._lifecycle_active(lifecycle)


def test_candidate_binding_refuses_tampered_raw_data(tmp_path):
    host, _adapter, _ = _host(tmp_path)
    (tmp_path / "raw.fixture.csv").write_text("tampered\n", encoding="utf-8")
    ok, refusal = host._prepare_candidate(default_strategies()[0].fingerprint)
    assert not ok
    assert refusal["reason"] == "evidence_data_binding_invalid"


def test_broker_monitor_reports_account_bound_readback_and_unavailable_profit(tmp_path):
    host, adapter, _ = _host(tmp_path)
    adapter.status = lambda: {"connected": True, "account_mode": "REAL", "account_mode_confirmed": True,
                              "account_fingerprint": account_fingerprint(LOGIN),
                              "redacted_account": {"server": SERVER, "currency": "USD"}}
    adapter.positions = lambda: [SimpleNamespace(audit_view=lambda: {
        "ticket": 7, "symbol": SYMBOL, "side": "BUY", "volume": Decimal("0.1"),
        "stop_loss": Decimal("1.09"), "take_profit": None, "profit": None,
    })]
    adapter.repository = SimpleNamespace(list_orders=lambda: [
        {"broker_id": "mt5_terminal", "request_id": "ours", "state": "FILLED",
         "account_fingerprint": account_fingerprint(LOGIN), "account_server": SERVER},
        {"broker_id": "mt5_terminal", "request_id": "other", "state": "FILLED",
         "account_fingerprint": "another", "account_server": SERVER},
    ])
    adapter._record_account_fingerprint = lambda row: row.get("account_fingerprint", "")
    preview = host._configuration_review()
    monitor = host._broker_monitor(preview)
    assert monitor["available"]
    assert monitor["positions"][0]["stop_loss"] == Decimal("1.09")
    assert not monitor["pnl"]["available"]
    assert monitor["pnl"]["reason"] == "broker_profit_unavailable"
    assert [row["request_id"] for row in monitor["orders"]] == ["ours"]
    assert "account_login" not in monitor["orders"][0]
    assert adapter.send_calls == 0


def test_broker_monitor_reports_incomplete_position_read_as_unavailable(tmp_path):
    from axiom_forex.brokers.mt5.orders import parse_positions
    from tests.fake_mt5 import make_position

    host, adapter, _ = _host(tmp_path)
    adapter.status = lambda: {"connected": True, "account_mode": "REAL", "account_mode_confirmed": True,
                              "account_fingerprint": account_fingerprint(LOGIN),
                              "redacted_account": {"server": SERVER, "currency": "USD"}}
    valid = make_position(ticket=1, symbol=SYMBOL, side="BUY", volume=0.1)
    invalid = make_position(ticket=2, symbol=SYMBOL, side="SELL", volume=0.1)
    invalid["volume"] = None
    adapter.positions = lambda: parse_positions([valid, invalid])
    adapter.repository = SimpleNamespace(list_orders=lambda: [])
    monitor = host._broker_monitor(host._configuration_review())
    assert monitor["positions"] == []
    assert monitor["positions_reason"] == "broker_positions_unavailable"
    assert not monitor["pnl"]["available"]
    assert monitor["pnl"]["reason"] == "broker_positions_unavailable"
    assert adapter.send_calls == 0


def test_broker_monitor_does_not_report_other_account_as_real(tmp_path):
    host, adapter, _ = _host(tmp_path)
    # The fixture remains DEMO even if it happens to have matching identity.
    monitor = host._broker_monitor(host._configuration_review())
    assert not monitor["available"]
    assert not monitor["pnl"]["available"]
    assert monitor["positions_reason"] == "verified_real_account_required"
    assert adapter.send_calls == 0


def test_stored_requests_visible_without_adoption_or_orders(tmp_path):
    from axiom_forex.workstation.ui.pages.live import autonomous_scalper_section

    host, adapter, _ = _host(tmp_path)
    state = {"pending_submission": {"request_id": "entry-original"},
             "position_book": {"pending_closes": [{"request_id": "close-original", "position_id": "77"}],
                               "pending_protection_modifications": [{"request_id": "protect-original", "position_id": "77"}]}}
    host.state_store.path.write_text(json.dumps(state), encoding="utf-8")
    before = host.state_store.path.read_bytes()
    try:
        view = host.view()
        assert [item["request_id"] for item in view["saved_recovery"]["requests"]] == [
            "entry-original", "close-original", "protect-original"]
        html = autonomous_scalper_section({"scalper_autonomous": view}, "nonce")
        assert all(value in html for value in ("entry-original", "close-original", "protect-original"))
        assert "Unverified stored requests" in html
        assert host.state_store.path.read_bytes() == before
        assert adapter.send_calls == 0
        assert host._runtime is None
    finally:
        host.close()


def test_damaged_stored_recovery_is_visible_and_preserved(tmp_path):
    host, adapter, _ = _host(tmp_path)
    host.state_store.path.write_text("{damaged", encoding="utf-8")
    before = host.state_store.path.read_bytes()
    try:
        view = host.view()
        assert view["saved_recovery"]["error"] == "stored_run_state_unreadable"
        assert host.state_store.path.read_bytes() == before
        assert adapter.send_calls == 0
    finally:
        host.close()


def test_partially_damaged_recovery_does_not_hide_readable_request_ids(tmp_path):
    host, adapter, _ = _host(tmp_path)
    host.state_store.path.write_text(json.dumps({
        "pending_submission": {"request_id": "original-entry"},
        "position_book": {"pending_closes": [None, {"request_id": "original-close"}],
                          "pending_protection_modifications": "damaged"},
    }), encoding="utf-8")
    before = host.state_store.path.read_bytes()
    try:
        stored = host.view()["saved_recovery"]
        assert stored["error"] == "stored_run_state_unreadable"
        assert [row["request_id"] for row in stored["requests"]] == ["original-entry", "original-close"]
        assert host.state_store.path.read_bytes() == before
        assert adapter.send_calls == 0
    finally:
        host.close()
