"""One execution path per mode -- chosen by the operator, never guessed.

The router is the only place an approved order is dispatched. These tests pin
the two properties that matter:

* the paper path cannot reach the live submitter, and the live path cannot reach
  the paper engine;
* there is no fallback: a refused live order is an error, never a quiet paper
  fill, and vice versa.

The live submitter here is a fake that records calls.  No terminal, no package,
no order, and no credential is involved.
"""

from __future__ import annotations

import pytest

from app.execution.broker import TradingBlocked
from axiom_forex.execution.mode_router import (
    LIVE_MODE_REQUIRED,
    LIVE_NOT_ARMED,
    PAPER_MODE_REQUIRED,
    PAPER_UNAVAILABLE,
    ExecutionRouter,
)


class Bomb:
    """Explodes on any use: proves a code path did not touch its neighbour."""

    def __getattr__(self, name):  # noqa: ANN001
        raise AssertionError(f"the {name!r} attribute was used on the wrong path")


class FlatMode:
    def __init__(self, mode: str, confirmed: bool = False) -> None:
        self.mode = mode
        self.live_effective = confirmed


class FakePaper:
    def __init__(self) -> None:
        self.calls: list[tuple[object, object]] = []
        self.record = type("Record", (), {"state": "SIMULATED", "simulation_id": "sim-1"})()

    def submit(self, request, context):
        self.calls.append((request, context))
        return self.record


class FakeLive:
    def __init__(self, *, armed: bool = True) -> None:
        self.armed = armed
        self.calls: list[dict] = []
        self.receipt = type("Receipt", (), {"state": "FILLED", "request_id": "req-1"})()

    def submit(self, decision, intent, *, spec=None):
        self.calls.append({"decision": decision, "intent": intent, "spec": spec})
        return self.receipt


class Audit:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, event):
        self.events.append(dict(event))
        return dict(event)


def make_router(mode: str, *, confirmed: bool = False, armed: bool = True, audit=None):
    paper = FakePaper()
    live = FakeLive(armed=armed)
    return (
        ExecutionRouter(
            mode=FlatMode(mode, confirmed), paper=paper, live=live, audit=audit or Audit()
        ),
        paper,
        live,
    )


# ---------------------------------------------------------------------------
# paper
# ---------------------------------------------------------------------------


def test_paper_routes_to_the_paper_engine_and_says_no_order_was_sent():
    audit = Audit()
    router, paper, live = make_router("PAPER", audit=audit)
    record = router.route_paper("request", "context")
    assert record is paper.record
    assert paper.calls == [("request", "context")]
    assert live.calls == []
    assert audit.events[-1]["event_type"] == "EXECUTION_ROUTED"
    assert audit.events[-1]["route"] == "PAPER"
    assert audit.events[-1]["live"] is False
    assert audit.events[-1]["order_sent"] is False


def test_paper_never_touches_the_live_submitter_even_when_it_is_a_bomb():
    router = ExecutionRouter(mode=FlatMode("PAPER"), paper=FakePaper(), live=Bomb())
    assert router.route_paper("request", "context").state == "SIMULATED"


def test_paper_is_refused_while_live_is_active():
    router, paper, _ = make_router("LIVE", confirmed=True)
    with pytest.raises(TradingBlocked) as caught:
        router.route_paper("request", "context")
    assert caught.value.reason == PAPER_MODE_REQUIRED
    assert paper.calls == []


def test_paper_without_an_engine_fails_closed():
    router = ExecutionRouter(mode=FlatMode("PAPER"), paper=None, live=FakeLive())
    with pytest.raises(TradingBlocked) as caught:
        router.route_paper("request", "context")
    assert caught.value.reason == PAPER_UNAVAILABLE


def test_an_unknown_mode_routes_nothing():
    router, paper, live = make_router("", audit=Audit())
    with pytest.raises(TradingBlocked):
        router.route_paper("request", "context")
    with pytest.raises(TradingBlocked):
        router.route_live("decision", "intent")
    assert paper.calls == [] and live.calls == []


def test_a_missing_mode_object_routes_nothing():
    router = ExecutionRouter(mode=None, paper=FakePaper(), live=FakeLive())
    with pytest.raises(TradingBlocked):
        router.route_paper("request", "context")
    with pytest.raises(TradingBlocked):
        router.route_live("decision", "intent")


# ---------------------------------------------------------------------------
# live
# ---------------------------------------------------------------------------


def test_live_is_refused_in_paper_mode_and_does_not_fall_back():
    router, paper, live = make_router("PAPER")
    with pytest.raises(TradingBlocked) as caught:
        router.route_live("decision", "intent")
    assert caught.value.reason == LIVE_MODE_REQUIRED
    assert live.calls == []
    assert paper.calls == []  # no silent paper fill


def test_live_needs_the_confirmation_in_this_process():
    router, paper, live = make_router("LIVE", confirmed=False)
    with pytest.raises(TradingBlocked) as caught:
        router.route_live("decision", "intent")
    assert caught.value.reason == "live_mode_not_confirmed"
    assert live.calls == [] and paper.calls == []


def test_live_needs_an_armed_session():
    router, _, live = make_router("LIVE", confirmed=True, armed=False)
    with pytest.raises(TradingBlocked) as caught:
        router.route_live("decision", "intent")
    assert caught.value.reason == LIVE_NOT_ARMED
    assert live.calls == []

    missing = ExecutionRouter(
        mode=FlatMode("LIVE", True),
        paper=FakePaper(),
        live=type("NoArming", (), {"submit": lambda self, *a, **k: pytest.fail("submitted")})(),
    )
    with pytest.raises(TradingBlocked) as caught:
        missing.route_live("decision", "intent")
    assert caught.value.reason == LIVE_NOT_ARMED


def test_a_confirmed_armed_live_mode_submits_exactly_once():
    audit = Audit()
    router, paper, live = make_router("LIVE", confirmed=True, audit=audit)
    receipt = router.route_live("decision", "intent", spec="spec")
    assert receipt is live.receipt
    assert len(live.calls) == 1
    assert live.calls[0] == {"decision": "decision", "intent": "intent", "spec": "spec"}
    assert paper.calls == []
    assert audit.events[-1]["live"] is True
    assert audit.events[-1]["order_sent"] is True


def test_a_refused_route_is_audited():
    audit = Audit()
    router, _, _ = make_router("PAPER", audit=audit)
    with pytest.raises(TradingBlocked):
        router.route_live("decision", "intent")
    refusal = audit.events[-1]
    assert refusal["event_type"] == "EXECUTION_ROUTE_REFUSED"
    assert refusal["reason"] == LIVE_MODE_REQUIRED
    assert refusal["order_sent"] is False


def test_a_submitter_failure_is_not_retried_or_rerouted():
    class Exploding(FakeLive):
        def submit(self, decision, intent, *, spec=None):
            self.calls.append({"decision": decision, "intent": intent, "spec": spec})
            raise RuntimeError("terminal disconnected")

    live = Exploding()
    paper = FakePaper()
    router = ExecutionRouter(mode=FlatMode("LIVE", True), paper=paper, live=live)
    with pytest.raises(RuntimeError):
        router.route_live("decision", "intent")
    assert len(live.calls) == 1
    assert paper.calls == []


def test_describe_states_there_is_no_fallback():
    router, _, _ = make_router("PAPER")
    described = router.describe()
    assert described == {
        "mode": "PAPER",
        "live_effective": False,
        "paper_path_available": True,
        "live_path_available": True,
        "fallback": "none",
    }


def test_the_mode_controller_satisfies_the_router_contract():
    from axiom_forex.workstation.mode import ExecutionModeController

    controller = ExecutionModeController()
    router = ExecutionRouter(mode=controller, paper=FakePaper(), live=FakeLive())
    assert router.mode_name == "PAPER"
    with pytest.raises(TradingBlocked):
        router.route_live("decision", "intent")
    assert controller.enable_live(confirmation="ENABLE LIVE TRADING").changed is True
    assert router.live_effective is True
    assert router.route_live("decision", "intent").state == "FILLED"
