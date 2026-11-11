"""Guarded AX-SCALPER LIVE bridge tests with synthetic evidence and a fake MT5.

The successful route uses the *real* LiveLeg, PreTradeGate, ExecutionRouter and
MT5TradingAdapter, backed only by FakeMT5. It proves the existing single order
path and the durable write-ahead record. The evidence digest, reviewer key and
approval are synthetic test fixtures, not claims about a real candidate.
"""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.audit.log import InMemoryAuditLog
from axiom_forex.brokers.mt5.terminal import MT5Terminal
from axiom_forex.config.mt5 import MT5Settings
from axiom_forex.execution.mode_router import ExecutionRouter
from axiom_forex.paper_runner.live import LiveLeg
from axiom_forex.paper_runner.live_host import OwnerThreadLeg, OwnerThreadPlatform
from axiom_forex.paper_runner.live_workflow import (
    ARM_CONFIRMATION_PHRASE,
    ArmedStrategy,
    LiveTrading,
)
from axiom_forex.scalper.live_approval import (
    APPROVAL_PHRASE,
    APPROVAL_SCHEMA,
    approval_identity,
    canonical_approval_payload,
)
from axiom_forex.scalper.live_bridge import (
    REVIEW_PHRASE,
    EvidenceVerdict,
    ScalperLiveIntentBridge,
)
from axiom_forex.scalper.live_intent import SCHEMA_V3, ScalperLiveIntent
from axiom_forex.scalper.signals import default_strategies
from tests.fake_mt5 import FakeMT5
from tests.paper_provider_support import (
    BROKER_SYMBOL,
    PAIR,
    START,
    Mode,
    write_setup,
)
from tests.test_forex_live_runner import build_platform
from tests.test_mt5_adapter import build_harness
from tests.test_scalper_admission import policy_payload, setup_policy

DIGEST = "e" * 64
REVIEWER = "independent-reviewer"


@pytest.fixture
def world(tmp_path):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy_payload(public)), encoding="utf-8")
    policy = setup_policy(tmp_path, private)
    strategy = default_strategies()[0]
    fake = FakeMT5(symbols={BROKER_SYMBOL: {
        "time": int(START.timestamp()), "bid": 1.1, "ask": 1.1002,
    }})
    harness = build_harness(fake=fake, settings_overrides={"max_price_drift_points": 5000})
    terminal = MT5Terminal(
        module=FakeMT5(),
        settings=MT5Settings(
            terminal_path="terminal64.exe", server="Broker-Demo",
            account_login=55500123, symbol_suffix=".pro",
        ),
    )
    terminal.connect()
    setup = write_setup(tmp_path)
    platform = build_platform(setup, terminal, repository=harness.repository)
    account = harness.adapter.account_fingerprint()
    mode = Mode("LIVE", True, session_account=account)
    stack = SimpleNamespace(
        spec=platform.spec, broker_symbol=BROKER_SYMBOL,
        symbol_info={"volume_min": "0.01", "volume_step": "0.01",
                     "volume_max": "100", "digits": 5, "trade_mode": 4},
    )
    live = LiveTrading(
        adapter=harness.adapter, mode=mode,
        plan_path=tmp_path / "live_plan.local.json",
        setup_path=setup.path, stack_builder=lambda pair: stack,
        candidates=(), armed_strategy=ArmedStrategy(),
        clock=lambda: START, audit_log=harness.audit,
        live_interval_seconds=60,
    )
    router = ExecutionRouter(mode=mode, paper=None, live=harness.adapter, audit=harness.audit)
    leg = LiveLeg(
        router=router, adapter=harness.adapter, gate=platform.gate,
        mode=mode, spec=platform.spec, strategy_id=strategy.fingerprint,
        repository=harness.repository, audit_log=harness.audit,
        lifecycle=live.armed_strategy, portfolio=platform.portfolio,
        clock=lambda: START,
    )
    approval_path = tmp_path / "approval.json"

    def evidence_gate(candidate_fingerprint, *, now=None):
        return EvidenceVerdict(True, "synthetic_test_evidence", DIGEST,
                               strategy.spec(), "synthetic-reviewer")

    bridge = ScalperLiveIntentBridge(
        repository=harness.repository, live=live, leg=leg, platform=platform,
        policy=policy, approval_path=approval_path,
        evidence_gate=evidence_gate, audit_log=harness.audit,
        clock=lambda: START,
    )
    live.scalper_bridge = bridge
    try:
        yield {
            "tmp_path": tmp_path, "private": private, "policy": policy,
            "strategy": strategy, "harness": harness, "terminal": terminal,
            "setup": setup, "platform": platform, "mode": mode, "live": live,
            "leg": leg, "bridge": bridge, "approval_path": approval_path,
            "account": account,
        }
    finally:
        terminal.disconnect()
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


def exact_payload(w, **overrides):
    base = {
        "schema": SCHEMA_V3, "intent_id": "scalper-live-intent-1",
        "candidate_fingerprint": w["strategy"].fingerprint,
        "evidence_fingerprint": DIGEST, "account_fingerprint": "pending",
        "instrument_fingerprint": "pending", "symbol": PAIR,
        "broker_symbol": BROKER_SYMBOL, "timeframe": "M1", "side": "BUY",
        "quantity": "0.10", "quantity_unit": "lots",
        "stop_price": "1.09900", "target_price": "1.10200",
        "quote_bid": "1.10000", "quote_ask": "1.10020",
        "quote_timestamp": (START - timedelta(seconds=2)).isoformat(),
        "quote_received_at": (START - timedelta(seconds=1)).isoformat(),
        "quote_source": "MT5_READ_ONLY",
        "expires_at": (START + timedelta(seconds=30)).isoformat(),
        "risk_fingerprint": "pending", "config_fingerprint": "pending",
    }
    base.update(overrides)
    return base


def bound_payload(w, **overrides):
    raw = exact_payload(w, **overrides)
    dummy = ScalperLiveIntent.parse({**raw, "account_fingerprint": "pending",
                                    "instrument_fingerprint": "pending",
                                    "risk_fingerprint": "pending",
                                    "config_fingerprint": "pending"}, now=START)
    identity, reason = w["bridge"]._runtime_identity(dummy)
    assert identity is not None, reason
    raw.update({
        "account_fingerprint": identity.account_fingerprint,
        "instrument_fingerprint": identity.instrument_fingerprint,
        "risk_fingerprint": identity.risk_fingerprint,
        "config_fingerprint": identity.config_fingerprint,
    })
    return raw


def write_exact_approval(w, raw, fingerprint: str) -> None:
    document = {
        "schema": APPROVAL_SCHEMA,
        "plan_phrase": APPROVAL_PHRASE,
        "plan_fingerprint": fingerprint,
        "reviewer_id": REVIEWER,
        "strategy_developers": ["strategy-author"],
        "reviewed_at": (START - timedelta(hours=1)).isoformat(),
        "expires_at": (START + timedelta(days=1)).isoformat(),
        **approval_identity(raw, evidence_digest=DIGEST),
    }
    document["signature"] = w["private"].sign(canonical_approval_payload(document)).hex()
    w["approval_path"].write_text(json.dumps(document), encoding="utf-8")


def full_workflow(w, raw):
    bridge = w["bridge"]
    account = w["account"]
    registered = bridge.register(raw)
    assert registered["ok"], registered
    write_exact_approval(w, raw, registered["plan_fingerprint"])
    reviewed = bridge.review(raw["intent_id"], reviewed=True, phrase=REVIEW_PHRASE,
                             account_confirmation=account)
    assert reviewed["ok"], reviewed
    approved = bridge.approve(raw["intent_id"],
                              plan_fingerprint_value=registered["plan_fingerprint"],
                              phrase=APPROVAL_PHRASE, account_confirmation=account)
    assert approved["ok"], approved
    armed = bridge.arm(raw["intent_id"], confirmation=ARM_CONFIRMATION_PHRASE,
                       account_confirmation=account)
    assert armed["ok"], armed
    return raw


def test_exact_guarded_proposal_reaches_the_single_fake_broker_order_path(world):
    raw = bound_payload(world)
    full_workflow(world, raw)
    outcome = world["bridge"].submit(raw["intent_id"])
    assert outcome["reason"] in {"FILLED", "PARTIAL"}, outcome
    assert outcome["broker"]["submitted"] is True
    assert len(world["harness"].fake.send_requests) == 1
    assert world["harness"].fake.send_requests[0]["symbol"] == BROKER_SYMBOL

    # Corrected repository test: the real API is list_orders(), never orders().
    records = world["harness"].repository.list_orders()
    assert len(records) == 1
    assert records[0]["request_id"] == raw["intent_id"]
    assert not hasattr(world["harness"].repository, "orders")

    stored = world["bridge"]._load(raw["intent_id"])
    assert stored["status"] in {"FILLED", "PARTIAL"}
    assert stored["broker"]["request_id"] == raw["intent_id"]
    assert stored["reconciliation"]["request_id"] == raw["intent_id"]
    events = world["harness"].audit.find(proposal_id=raw["intent_id"])
    assert {"AX_SCALPER_LIVE_PROPOSAL_RECEIVED", "AX_SCALPER_LIVE_PROPOSAL_REVIEWED",
            "AX_SCALPER_LIVE_PLAN_APPROVED", "AX_SCALPER_LIVE_PLAN_ARMED",
            "AX_SCALPER_LIVE_SUBMISSION_STARTED", "AX_SCALPER_LIVE_BROKER_RESPONSE",
            "AX_SCALPER_LIVE_RECONCILED"} <= {item["event_type"] for item in events}
    view = world["live"].view()
    assert view["scalper_live"]["selected"]["status"] in {"FILLED", "PARTIAL"}
    assert view["scalper_live"]["one_shot_submission_available"] is False


def test_default_evidence_gate_keeps_real_live_submission_unavailable(world):
    from axiom_forex.scalper.live_bridge import blocked_evidence_gate

    world["bridge"].evidence_gate = blocked_evidence_gate("no_genuine_replay_evidence")
    raw = bound_payload(world)
    result = world["bridge"].register(raw)
    assert not result["ok"]
    assert "no_genuine_replay_evidence" in result["reason"]
    assert world["harness"].fake.send_requests == []


def test_stale_or_identity_mismatched_intents_never_reach_the_live_leg(world):
    raw = bound_payload(world)
    full_workflow(world, raw)
    bridge = world["bridge"]
    state = bridge._load(raw["intent_id"])
    state["status"] = "ARMED"
    # An exact account change is a refusal, not a substitution.
    mismatched = {**raw, "account_fingerprint": "another-account"}
    state["intent"] = mismatched
    bridge._write(state, "TEST_IDENTITY_CHANGED")
    result = bridge.submit(raw["intent_id"])
    assert not result["ok"]
    assert "mismatch" in result["reason"] or "invalid" in result["reason"]
    assert world["harness"].fake.send_requests == []


class FakeOutcomeLeg:
    """Synthetic broker outcome leg; it has no terminal and sends nothing."""

    def __init__(self, outcome):
        self.outcome = dict(outcome)
        self.executions = 0
        self.reconciliations = 0

    def status(self):
        return {"blockers": [], "ready": True, "configured": True}

    def execute(self, _plan):
        self.executions += 1
        return dict(self.outcome)

    def reconcile(self):
        self.reconciliations += 1
        return {"available": True, "reconciled": True, "recovered": []}


class RefusingGate:
    def __init__(self, code):
        self.code = code

    def evaluate(self, _request, _context):
        return SimpleNamespace(
            allowed=False, code=self.code,
            audit_view=lambda: {"code": self.code.value, "allowed": False},
        )


@pytest.mark.parametrize(("outcome", "expected"), [
    ({"submitted": True, "booked": True, "state": "FILLED",
      "volume_requested": "0.10", "volume_filled": "0.05"}, "PARTIAL"),
    ({"submitted": True, "booked": False, "state": "REJECTED",
      "volume_requested": "0.10", "volume_filled": "0"}, "REJECTED"),
    ({"submitted": True, "unknown": True, "state": "UNRESOLVED",
      "volume_requested": "0.10", "volume_filled": "0"}, "UNRESOLVED"),
])
def test_partial_rejected_and_ambiguous_broker_outcomes_are_recorded(world, outcome, expected):
    raw = bound_payload(world)
    full_workflow(world, raw)
    fake_leg = FakeOutcomeLeg({**outcome, "request_id": raw["intent_id"]})
    world["bridge"].leg = fake_leg
    result = world["bridge"].submit(raw["intent_id"])
    assert result["reason"] == expected
    assert world["bridge"]._load(raw["intent_id"])["status"] == expected
    assert fake_leg.executions == 1
    assert fake_leg.reconciliations == 1
    assert world["harness"].fake.send_requests == []
    # An ambiguous outcome is queried by the same identity, never sent again.
    again = world["bridge"].submit(raw["intent_id"])
    assert not again["ok"]
    assert fake_leg.executions == 1


def test_stale_quote_expired_intent_and_duplicate_submission_are_refused(world):
    stale = bound_payload(
        world,
        quote_timestamp=(START - timedelta(seconds=10)).isoformat(),
        quote_received_at=(START - timedelta(seconds=9)).isoformat(),
    )
    full_workflow(world, stale)
    result = world["bridge"].submit(stale["intent_id"])
    assert not result["ok"]
    assert result["reason"] == "stale_quote"
    assert world["harness"].fake.send_requests == []
    assert world["live"].armed is False
    assert world["bridge"]._load(stale["intent_id"])["status"] == "REFUSED"

    expired = bound_payload(world, intent_id="scalper-live-intent-expired")
    full_workflow(world, expired)
    world["bridge"].clock = lambda: START + timedelta(seconds=31)
    result = world["bridge"].submit(expired["intent_id"])
    assert not result["ok"]
    assert "expired" in result["reason"]
    assert world["harness"].fake.send_requests == []

    fresh = bound_payload(world, intent_id="scalper-live-intent-duplicate")
    world["bridge"].clock = lambda: START
    full_workflow(world, fresh)
    first = world["bridge"].submit(fresh["intent_id"])
    assert first["reason"] in {"FILLED", "PARTIAL"}, first
    second = world["bridge"].submit(fresh["intent_id"])
    assert not second["ok"]
    assert second["reason"] == "proposal_already_submitted"
    assert second["order_record"]["request_id"] == fresh["intent_id"]
    assert len(world["harness"].fake.send_requests) == 1


def test_missing_signed_exact_plan_approval_and_gate_risk_refusal(world):
    raw = bound_payload(world, intent_id="scalper-live-intent-no-approval")
    bridge = world["bridge"]
    registered = bridge.register(raw)
    assert registered["ok"], registered
    assert bridge.review(raw["intent_id"], reviewed=True, phrase=REVIEW_PHRASE,
                         account_confirmation=world["account"])["ok"]
    refused = bridge.approve(raw["intent_id"],
                             plan_fingerprint_value=registered["plan_fingerprint"],
                             phrase=APPROVAL_PHRASE,
                             account_confirmation=world["account"])
    assert not refused["ok"]
    assert "approval_missing" in refused["reason"]
    assert world["harness"].fake.send_requests == []

    risk_raw = bound_payload(world, intent_id="scalper-live-intent-risk-refused")
    full_workflow(world, risk_raw)
    from app.risk.pretrade import RejectionReason
    world["leg"].gate = RefusingGate(RejectionReason.MAX_DAILY_LOSS_EXCEEDED)
    result = bridge.submit(risk_raw["intent_id"])
    assert result["reason"] == "REFUSED"
    assert result["broker"]["submitted"] is False
    assert result["broker"]["gate"]["code"] == "MAX_DAILY_LOSS_EXCEEDED"
    assert world["harness"].fake.send_requests == []


def test_archived_v1_intent_and_bridge_source_have_no_second_broker_route(world):
    raw = bound_payload(world)
    archived = {**raw, "schema": "axiom-scalper-live-intent-v1"}
    result = world["bridge"].register(archived)
    assert not result["ok"]
    assert result["reason"] == "intent_schema_not_live_ready"
    source = world["bridge"].__class__.__module__.replace(".", "/") + ".py"
    text = (world["tmp_path"].parents[0] / source).read_text(encoding="utf-8") if False else None
    from pathlib import Path
    module_source = Path(__file__).resolve().parents[1] / "axiom_forex/scalper/live_bridge.py"
    text = module_source.read_text(encoding="utf-8")
    for forbidden in ("order_send(", "order_check(", "positions_close(",
                      "arm_trading(", "disarm_trading(", "send_order("):
        assert forbidden not in text
    assert "self.leg.execute(execution)" in text
    assert world["harness"].fake.send_requests == []


def test_bridge_and_live_views_share_authoritative_facts_without_recursion(world):
    raw = bound_payload(world)
    full_workflow(world, raw)
    direct = world["bridge"].view()
    host = world["live"].view()
    assert "live_view_unreadable" not in [item["reason"] for item in direct["blockers"]]
    assert direct["connected"] == host["connected"] is True
    assert direct["armed"] == host["armed"] is True
    assert direct["account_fingerprint"] == host["account_fingerprint"]
    assert direct["session_running"] == host["session_running"] is False
    assert direct["continuous_session_sending"] is False
    assert direct["one_shot_submission_available"] is True
    assert host["scalper_live"]["one_shot_submission_available"] is True


def test_exact_recovery_after_restart_queries_same_identity_without_resending(world):
    raw = bound_payload(world)
    full_workflow(world, raw)
    state = world["bridge"]._load(raw["intent_id"])
    state["status"] = "SUBMITTING"
    state["submission"] = {"at": START.isoformat(), "request_id": raw["intent_id"]}
    world["bridge"]._write(state, "TEST_RECOVERY_IN_DOUBT", request_id=raw["intent_id"])
    restarted = ScalperLiveIntentBridge(
        repository=world["harness"].repository,
        live=world["live"], leg=world["leg"], platform=world["platform"],
        policy=world["policy"], approval_path=world["approval_path"],
        evidence_gate=world["bridge"].evidence_gate,
        audit_log=world["harness"].audit, clock=lambda: START,
    )
    world["live"].scalper_bridge = restarted
    result = restarted.submit(raw["intent_id"])
    assert result["reason"] == "submission_in_doubt"
    assert result["order_record"] == {}
    assert result["state"]["status"] in {"SUBMITTING", "RECONCILED"}
    assert result["state"]["reconciliation"]["request_id"] == raw["intent_id"]
    assert world["harness"].fake.send_requests == []
    assert world["live"].armed is False


def test_scalper_host_policy_loader_and_paper_evidence_gate_fail_closed(tmp_path):
    from axiom_forex.scalper.live_bridge import paper_host_evidence_gate, scalper_host_policy

    host = SimpleNamespace(
        admission_policy_path=tmp_path / "missing.local.json",
        status=lambda: {"config": {"currency": "USD"}},
    )
    assert scalper_host_policy(host)() is None
    gate = paper_host_evidence_gate(SimpleNamespace(status=lambda: {}))
    assert gate("candidate") .eligible is False
    eligible_host = SimpleNamespace(status=lambda: {
        "strategy_review": {"candidates": [{
            "fingerprint": "candidate", "eligible": True, "name": "Strategy",
            "strategy_id": "strategy", "rule_version": "v1",
            "evidence": {"evidence_digest": DIGEST, "reviewer": "independent"},
        }]}
    })
    verdict = paper_host_evidence_gate(eligible_host)("candidate")
    assert verdict.eligible is True
    assert verdict.evidence_digest == DIGEST


class ImmediateHost:
    """Executes owner-thread tasks synchronously; no broker or terminal."""

    alive = True

    def __init__(self):
        self.platform = SimpleNamespace(
            setup=SimpleNamespace(name="setup"),
            quote_freshness=SimpleNamespace(max_age_seconds=Decimal("5")),
            planner=SimpleNamespace(
                plan_at_quote=lambda **kwargs: "planned",
                audit_view=lambda: {"ok": True},
            ),
            repository=SimpleNamespace(close=lambda: None),
        )
        self.leg = SimpleNamespace(
            execute=lambda plan: {"submitted": True, "request_id": plan},
            status=lambda: {"configured": True, "ready": True, "blockers": []},
            reconcile=lambda: {"available": True, "reconciled": True, "recovered": []},
        )
        self.closed = False

    def submit(self, task):
        return task()

    def close(self):
        self.closed = True

    def one_shot_stack(self):
        return OwnerThreadPlatform(self), OwnerThreadLeg(self)


def test_owner_thread_proxies_keep_platform_access_on_the_owner_thread():
    from axiom_forex.paper_runner.live_host import OwnerThreadLeg, OwnerThreadPlatform

    host = ImmediateHost()
    platform = OwnerThreadPlatform(host)
    assert platform.setup.name == "setup"
    assert platform.quote_freshness.max_age_seconds == Decimal("5")
    assert platform.planner.audit_view() == {"ok": True}
    assert platform.planner.plan_at_quote(signal="BUY") == "planned"
    leg = OwnerThreadLeg(host)
    assert leg.one_shot_ready is True
    assert leg.execute("request-1")["request_id"] == "request-1"
    assert leg.status()["ready"] is True
    assert leg.reconcile()["reconciled"] is True
    leg.close()
    assert host.closed is True


def test_one_shot_stack_factory_distinguishes_planning_and_owner_stacks():
    from axiom_forex.paper_runner.live_assembly import (
        OneShotStackFactory, UnavailableOneShotLeg,
    )

    host = ImmediateHost()
    live = SimpleNamespace(armed=True, session=None, plan={"scalper_live": {}}, view=lambda: {})
    factory = OneShotStackFactory(
        live=live,
        platform_factory=lambda pair: SimpleNamespace(
            repository=SimpleNamespace(close=lambda: None), pair=pair,
        ),
        session_factory=lambda plan: host,
    )
    factory._owner_ready = lambda state, intent: True
    state = {"plan_fingerprint": "fingerprint-1", "evidence": {}, "values": {}}
    intent = SimpleNamespace(symbol="EURUSD", as_payload=lambda: {})
    planning_platform, planning_leg = factory(state, intent, prepare=False)
    assert planning_platform.pair == "EURUSD"
    assert isinstance(planning_leg, UnavailableOneShotLeg)
    assert planning_leg.one_shot_ready is False
    owner_platform, owner_leg = factory(state, intent, prepare=True)
    assert isinstance(owner_platform, OwnerThreadPlatform)
    assert owner_leg.one_shot_ready is True
    assert owner_leg.status()["ready"] is True
    factory.release()
    assert host.closed is True


class FakeBridge:
    """UI-only bridge stub; it records actions and renders a blocked state."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.result = {"ok": True, "reason": "proposal_received",
                       "message": "Exact proposal recorded.", "blockers": []}

    def _record(self, name, value=""):
        self.calls.append((name, value))
        return dict(self.result)

    def register(self, raw):
        return self._record("register", str(raw))

    def review(self, intent_id, **kwargs):
        return self._record("review", intent_id)

    def approve(self, intent_id, **kwargs):
        return self._record("approve", intent_id)

    def arm(self, intent_id, **kwargs):
        return self._record("arm", intent_id)

    def submit(self, intent_id):
        return self._record("submit", intent_id)

    def reconcile(self, intent_id):
        self.calls.append(("reconcile", intent_id))
        return {"status": "RECONCILED", "intent": {"intent_id": intent_id},
                "reconciliation": {"request_id": intent_id}}

    def view(self, **_kwargs):
        return {
            "available": True,
            "one_shot_submission_available": False,
            "submission_available": False,
            "submission_reason": "proposal_missing",
            "session_running": False,
            "continuous_session_sending": False,
            "account_fingerprint": "account-1",
            "approval_file": "scalper_live_approval.local.json",
            "review_phrase": REVIEW_PHRASE,
            "approval_phrase": APPROVAL_PHRASE,
            "arm_phrase": ARM_CONFIRMATION_PHRASE,
            "selected": {"intent_id": "intent-1", "status": "RECEIVED",
                         "plan_fingerprint": "fingerprint-1", "reviewed": False,
                         "approved": False, "armed": False},
            "proposals": [],
            "positions": [],
            "unresolved_orders": [],
            "pnl": {"available": False, "basis": "broker-reported open position profit only"},
            "blockers": [{"reason": "proposal_missing", "message": "No exact proposal.", "hint": ""}],
            "note": "PAPER results cannot enter this bridge.",
        }


class FakeLiveWithBridge:
    def __init__(self, bridge):
        self.scalper_bridge = bridge

    def view(self):
        return {
            "configured": True, "armed": False, "connected": True,
            "mode": "LIVE", "live_effective": True,
            "session_running": False, "next_signal_sends_real_order": False,
            "plan": {"present": False}, "review": {}, "blockers": [],
            "candidates": [], "symbols": [], "positions": [],
            "stop_setting": {}, "confirmation_phrase": ARM_CONFIRMATION_PHRASE,
            "account_fingerprint": "account-1", "account": {},
            "scalper_live": self.scalper_bridge.view(),
        }

    def summary(self):
        return {"sentence": "No real order can be sent right now."}

    def confirmed_account_fingerprint(self):
        return "account-1"


def test_guarded_live_actions_dispatch_to_bridge_and_live_page_renders_card():
    from tests.test_workstation_ui import app_for, live_wizard

    app = app_for(live_wizard())
    bridge = FakeBridge()
    app.live = FakeLiveWithBridge(bridge)
    html = app.page("/live")[1]
    assert "Guarded AX-SCALPER one-shot LIVE intent" in html
    for action in ("scalper-live-register", "scalper-live-review",
                   "scalper-live-approve", "scalper-live-arm",
                   "scalper-live-submit", "scalper-live-reconcile"):
        assert f"/actions/{action}" in html
    assert "One-shot submission BLOCKED" in html

    status, path, _ = app.act("scalper-live-register", {
        "nonce": [app.nonce], "intent_json": ["{\"schema\":\"v3\"}"],
    })
    assert int(status) == 303
    assert path == "/live"
    assert bridge.calls == [("register", "{\"schema\":\"v3\"}")]
    assert "Last guarded AX-SCALPER action" in app.page("/live")[1]

    status, _, message = app.act("scalper-live-register", {
        "nonce": [app.nonce], "password": ["must-not-be-read"],
    })
    assert int(status) == 400
    assert "credential" in message
    assert bridge.calls == [("register", "{\"schema\":\"v3\"}")]

    status, _, _ = app.act("scalper-live-reconcile", {
        "nonce": [app.nonce], "intent_id": ["intent-1"],
    })
    assert int(status) == 303
    assert bridge.calls[-1] == ("reconcile", "intent-1")
