"""The one-shot DEMO execution test, proved against a fake terminal.

Every test here builds ``tests.fake_mt5.FakeMT5`` -- no terminal, no account and
no broker is involved, and the workstation is never connected to anything.

What is proved, in the words the operator reads:

* a demo account accepts exactly one order, and the record it left names the
  same request id the adapter wrote ahead of ``order_send``;
* a REAL account, a CONTEST account and an account MT5 cannot classify are all
  refused before anything is queued, and the page says which one it is;
* a missing symbol, side, volume, stop, phrase or account signature is refused,
  and a volume or stop the broker's own terms would reject is refused with the
  broker's number;
* a stop that is not the stop that would actually be attached is refused rather
  than quietly substituted;
* a second submission is a duplicate, never a second order;
* an ambiguous outcome is never re-sent: the same request id is reconciled from
  the durable order ledger;
* PAPER cannot send one, and no paper page can even offer one.
"""

from __future__ import annotations

from datetime import timezone
from decimal import Decimal

import pytest

from axiom_forex.brokers.mt5.catalogue import build_catalogue
from axiom_forex.brokers.mt5.terminal import MT5Terminal
from axiom_forex.config.mt5 import MT5Settings
from axiom_forex.database.repository import ForexRepository
from axiom_forex.execution.mode_router import ExecutionRouter
from axiom_forex.paper_runner.live import (
    DEMO_ACCOUNT_REQUIRED,
    DEMO_ACCOUNT_UNCONFIRMED,
    DEMO_IDENTITY_REQUIRED,
    DEMO_TEST_STRATEGY_ID,
    LiveLeg,
)
from axiom_forex.paper_runner.live_demo_test import ALREADY_USED

from axiom_forex.workstation.ui.render import render_page
from tests.fake_mt5 import FakeMT5

from tests.paper_provider_support import (
    BROKER_SYMBOL,
    PAIR,
    START,
    Mode,
    symbol_request,
    write_setup,
)
from tests.test_mt5_adapter import build_harness

#: The fake terminal's tick time, aligned with the fixture's declared conversion
#: basis so the gate's context can be built at the quote's own instant.  A real
#: terminal answers with the current time; this is the same thing, pinned.
TICK = int(START.timestamp())


def demo_fake(**overrides) -> FakeMT5:
    symbols = {BROKER_SYMBOL: {"time": TICK, "bid": 1.10000, "ask": 1.10020}}
    symbols.update(overrides.pop("symbols", {}) or {})
    overrides.setdefault("trade_mode", 0)
    return FakeMT5(symbols=symbols, **overrides)
from tests.test_workstation_ui import simulated_wizard


# -- fixtures ---------------------------------------------------------------


def make_terminal(fake: FakeMT5) -> MT5Terminal:
    terminal = MT5Terminal(
        module=fake,
        settings=MT5Settings(
            terminal_path="terminal64.exe",
            server=fake.account_server,
            account_login=fake.account_login,
            symbol_suffix=".pro",
        ),
    )
    terminal.connect()
    return terminal


def make_platform(setup, terminal, repository):
    report = build_catalogue(terminal, [symbol_request(setup, PAIR)])
    assert report.complete, [item.audit_view() for item in report.refused]
    from axiom_forex.paper_runner.provider import build_provider_platform

    platform, refusal = build_provider_platform(
        setup=setup,
        resolved=report.resolved_for(PAIR),
        repository=repository,
        terminal=terminal,
    )
    assert refusal is None, refusal.audit_view()
    return platform


@pytest.fixture
def rig(tmp_path):
    """A demo terminal, a live mode, an armed adapter and one provider stack."""

    fake = demo_fake()
    repository = ForexRepository(":memory:")
    terminal = make_terminal(fake)
    setup = write_setup(tmp_path)
    platform = make_platform(setup, terminal, repository)
    harness = build_harness(fake=fake, repository=repository)
    harness.arm()
    try:
        yield {
            "fake": fake,
            "repository": repository,
            "terminal": terminal,
            "platform": platform,
            "harness": harness,
            "mode": Mode("LIVE", True),
            "tmp": tmp_path,
        }
    finally:
        terminal.disconnect()
        repository.close()


def controller(rig, *, pairs=(PAIR,), platform=None):
    from axiom_forex.paper_runner.live_demo_test import DemoExecutionTest

    chosen = platform or rig["platform"]
    return DemoExecutionTest(
        adapter=rig["harness"].adapter,
        mode=rig["mode"],
        platform_for=lambda pair: chosen,
        pairs=list(pairs),
        repository=rig["repository"],
        clock=lambda: START,
    )


def values(rig, **overrides):
    adapter = rig["harness"].adapter
    stop = adapter.stop_requirement(rig["platform"].spec)
    base = {
        "symbol": PAIR,
        "side": "BUY",
        "volume": "0.10",
        "stop_points": str(stop.get("distance_points") or 200),
        "confirmation": "SEND ONE DEMO ORDER",
        "account_confirmation": adapter.account_fingerprint(),
    }
    base.update(overrides)
    return base


# -- 1. the acceptance ------------------------------------------------------


def test_a_demo_account_accepts_exactly_one_order(rig):
    test = controller(rig)
    result = test.submit(values(rig))

    assert result["sent"] is True, result
    assert result["status"] == "FILLED", result
    assert len(rig["fake"].send_requests) == 1
    sent = rig["fake"].send_requests[0]
    assert sent["symbol"] == BROKER_SYMBOL
    assert sent["volume"] == 0.1
    assert float(sent["sl"]) > 0, "the order must carry the protective stop"

    # The durable record exists and names the same id the broker request carried.
    record = test.latest()
    assert record["kind"] == "axiom-demo-execution-test"
    # The comment the broker received *is* the idempotency id: no truncation to
    # unpick during a reconciliation.
    assert record["request_id"] == sent["comment"]
    assert len(record["request_id"]) <= 31
    assert record["status"] == "FILLED"
    assert record["not_a_strategy"] is True
    order = rig["repository"].get_order(record["request_id"])
    assert order is not None, "the adapter's write-ahead record must exist"

    # A smoke test never leaves trading armed behind it.
    assert rig["harness"].adapter.armed is False


def test_the_record_says_it_is_a_smoke_test_and_not_a_strategy(rig):
    test = controller(rig)
    test.submit(values(rig))
    record = test.latest()
    assert record["values"]["symbol"] == PAIR
    assert record["values"]["broker_symbol"] == BROKER_SYMBOL
    assert record["values"]["side"] == "BUY"
    assert record["values"]["volume_lots"] == "0.10"
    assert record["values"]["stop_points"] == 200
    assert record["account"]["mode"] == "DEMO"
    events = [item["event_type"] for item in record["events"]]
    assert events[0] == "DEMO_TEST_SUBMISSION_STARTED"
    assert "DEMO_TEST_BROKER_RESPONSE" in events
    assert events[-1] == "DEMO_TEST_DISARMED"


# -- 2. the account identity ------------------------------------------------


@pytest.mark.parametrize(
    ("trade_mode", "reason"),
    [
        (2, DEMO_ACCOUNT_REQUIRED),  # REAL
        (1, DEMO_ACCOUNT_REQUIRED),  # CONTEST
        (7, DEMO_ACCOUNT_UNCONFIRMED),  # MT5 did not say
    ],
)
def test_anything_that_is_not_positively_demo_is_refused(tmp_path, trade_mode, reason):
    fake = demo_fake(trade_mode=trade_mode)
    repository = ForexRepository(":memory:")
    terminal = make_terminal(fake)
    setup = write_setup(tmp_path)
    platform = make_platform(setup, terminal, repository)
    harness = build_harness(fake=fake, repository=repository)
    harness.arm()
    try:
        test = DemoExecutionTestFor(harness, platform, repository)
        result = test.submit(values({"harness": harness, "platform": platform}))
        assert result["sent"] is False
        assert result["reason"] == reason, result
        assert fake.send_requests == []
        assert test.latest() == {}
    finally:
        terminal.disconnect()
        repository.close()


def DemoExecutionTestFor(harness, platform, repository):
    from axiom_forex.paper_runner.live_demo_test import DemoExecutionTest

    return DemoExecutionTest(
        adapter=harness.adapter,
        mode=Mode("LIVE", True),
        platform_for=lambda pair: platform,
        pairs=[PAIR],
        repository=repository,
        clock=lambda: START,
    )


def test_the_page_never_shows_demo_as_ready_for_a_real_account(rig):
    test = controller(rig)
    rig["harness"].adapter.disconnect(reason="test")
    view = test.view()
    assert view["can_send"] is False
    assert view["account"]["is_demo"] is False


# -- 3. the fields the operator must fill in --------------------------------


def test_an_empty_form_names_every_missing_field(rig):
    test = controller(rig)
    checked = test.check({})
    reasons = {item["reason"] for item in checked["blockers"]}
    assert checked["ok"] is False
    assert {"demo_symbol_required", "demo_side_required", "demo_volume_required",
            "demo_stop_required", "demo_confirmation_required",
            "demo_account_confirmation_required"} <= reasons
    assert rig["fake"].send_requests == []


def test_a_volume_off_the_brokers_grid_is_refused_with_the_brokers_numbers(rig):
    test = controller(rig)
    checked = test.check(values(rig, volume="0.005"))
    assert checked["ok"] is False
    message = checked["blockers"][0]["message"]
    assert "demo_volume_invalid" == checked["blockers"][0]["reason"]
    assert "step" in message
    assert rig["fake"].send_requests == []


def test_a_stop_inside_the_brokers_minimum_is_refused(tmp_path):
    fake = demo_fake(symbols={BROKER_SYMBOL: {"time": TICK, "trade_stops_level": 300}})
    repository = ForexRepository(":memory:")
    terminal = make_terminal(fake)
    setup = write_setup(tmp_path)
    platform = make_platform(setup, terminal, repository)
    harness = build_harness(fake=fake, repository=repository)
    harness.arm()
    try:
        test = DemoExecutionTestFor(harness, platform, repository)
        checked = test.check(
            values(
                {"harness": harness, "platform": platform},
                stop_points="100",
            )
        )
        assert checked["ok"] is False
        reasons = {item["reason"] for item in checked["blockers"]}
        assert "demo_stop_invalid" in reasons
        assert "300 points" in " ".join(item["message"] for item in checked["blockers"])
        assert fake.send_requests == []
    finally:
        terminal.disconnect()
        repository.close()


def test_a_stop_that_is_not_the_one_in_force_is_refused_not_substituted(rig):
    test = controller(rig)
    checked = test.check(values(rig, stop_points="150"))
    assert checked["ok"] is False
    mismatch = [item for item in checked["blockers"] if item["reason"] == "demo_stop_mismatch"]
    assert mismatch, checked["blockers"]
    assert "200" in mismatch[0]["message"] and "150" in mismatch[0]["message"]
    assert rig["fake"].send_requests == []


def test_the_phrase_and_the_account_signature_are_both_required(rig):
    test = controller(rig)
    wrong_phrase = test.check(values(rig, confirmation="yes"))
    assert "demo_confirmation_mismatch" in {item["reason"] for item in wrong_phrase["blockers"]}

    wrong_account = test.check(values(rig, account_confirmation="deadbeef"))
    assert "demo_account_confirmation_mismatch" in {
        item["reason"] for item in wrong_account["blockers"]
    }
    assert rig["fake"].send_requests == []


def test_an_unverified_symbol_is_refused(rig):
    test = controller(rig)
    checked = test.check(values(rig, symbol="GBP/JPY"))
    assert checked["ok"] is False
    assert checked["blockers"][0]["reason"] == "demo_symbol_not_verified"


# -- 4. one request, ever ---------------------------------------------------


def test_a_second_submission_is_a_duplicate_not_a_second_order(rig):
    test = controller(rig)
    first = test.submit(values(rig))
    assert first["status"] == "FILLED"
    rig["harness"].arm()  # the operator arms again; it must still refuse

    second = test.submit(values(rig))
    assert second["sent"] is False
    assert second["reason"] == ALREADY_USED, second
    assert len(rig["fake"].send_requests) == 1


def test_an_ambiguous_outcome_is_never_resent_and_reconciles_by_the_same_id(rig):
    test = controller(rig)
    rig["fake"].send_script.insert(0, None)  # the terminal answers nothing at all
    result = test.submit(values(rig))
    assert result["sent"] is True
    assert result["status"] == "UNRESOLVED", result
    assert len(rig["fake"].send_requests) == 1
    record = test.latest()
    request_id = record["request_id"]
    assert rig["repository"].get_order(request_id) is not None
    # It disarmed, and the adapter itself refuses to re-arm while the order is
    # in doubt -- so even a determined operator cannot re-open the lane here.
    assert rig["harness"].adapter.armed is False
    with pytest.raises(Exception) as refused:
        rig["harness"].arm()
    assert "unresolved" in str(refused.value)
    again = test.submit(values(rig))
    assert again["sent"] is False
    assert again["reason"] in {ALREADY_USED, "demo_submission_in_doubt", "live_not_armed"}
    assert len(rig["fake"].send_requests) == 1
    # Reconciliation reads the same identity and resends nothing.
    reconciled = test.reconcile()
    assert reconciled["request_id"] == request_id
    assert reconciled["status"] in {"UNRESOLVED", "RECONCILED", "FILLED"}
    assert len(rig["fake"].send_requests) == 1


def test_a_refused_order_is_recorded_as_refused(rig):
    test = controller(rig)
    rig["fake"].send_script.insert(0, {"retcode": 10013, "comment": "invalid request"})
    result = test.submit(values(rig))
    assert result["sent"] is True
    assert result["status"] == "REJECTED", result
    assert test.latest()["status"] == "REJECTED"
    assert rig["harness"].adapter.armed is False


# -- 5. PAPER separation ----------------------------------------------------


def test_paper_mode_cannot_send_a_demo_order(rig):
    from axiom_forex.paper_runner.live_demo_test import DemoExecutionTest

    test = DemoExecutionTest(
        adapter=rig["harness"].adapter,
        mode=Mode("PAPER", False),
        platform_for=lambda pair: rig["platform"],
        pairs=[PAIR],
        repository=rig["repository"],
        clock=lambda: START,
    )
    result = test.submit(values(rig))
    assert result["sent"] is False
    assert result["reason"] == "mode_not_live"
    assert rig["fake"].send_requests == []


def test_the_live_leg_refuses_a_demo_test_that_carries_a_strategy_identity(rig):
    leg = LiveLeg(
        router=ExecutionRouter(
            mode=rig["mode"], paper=None, live=rig["harness"].adapter, audit=None
        ),
        adapter=rig["harness"].adapter,
        gate=rig["platform"].gate,
        mode=rig["mode"],
        spec=rig["platform"].spec,
        strategy_id="trend_pullback_v1_8_21",
        repository=rig["repository"],
        clock=lambda: START,
    )
    reasons = {item.reason for item in leg.blockers(demo_test=True)}
    assert DEMO_IDENTITY_REQUIRED in reasons
    assert DEMO_TEST_STRATEGY_ID not in {"trend_pullback_v1_8_21"}


def test_the_demo_test_cannot_close_a_position(rig):
    leg = LiveLeg(
        router=ExecutionRouter(
            mode=rig["mode"], paper=None, live=rig["harness"].adapter, audit=None
        ),
        adapter=rig["harness"].adapter,
        gate=rig["platform"].gate,
        mode=rig["mode"],
        spec=rig["platform"].spec,
        strategy_id=DEMO_TEST_STRATEGY_ID,
        repository=rig["repository"],
        clock=lambda: START,
    )

    class _Plan:
        request = type(
            "R",
            (),
            {
                "request_id": "demo-close",
                "close_intent": object(),
            },
        )()
        context = None

    outcome = leg.submit_plan(_Plan(), demo_test=True)
    assert outcome["submitted"] is False
    assert "demo_test_cannot_close" in outcome["reasons"]
    assert rig["fake"].send_requests == []


# -- 6. the pages -----------------------------------------------------------


def test_no_paper_page_offers_the_demo_test():
    from axiom_forex.workstation.app import WorkstationApp

    app = WorkstationApp(wizard=simulated_wizard())
    html = render_page(app._context("autopilot", "Autopilot"))
    assert "demo-test-submit" not in html
    assert "SEND ONE DEMO ORDER" not in html


def test_the_live_page_does_not_offer_a_demo_order_smoke_test():
    from axiom_forex.workstation.app import WorkstationApp

    app = WorkstationApp(wizard=simulated_wizard())
    html = render_page(app._context("live", "Live execution"))
    assert "Autonomous scalping" in html
    assert "DEMO and REAL share the same checks" in html
    assert "action='/actions/demo-test-submit'" not in html
    assert "DEMO execution test" not in html


# -- 7. the route -----------------------------------------------------------


def test_the_demo_test_route_refuses_when_no_adapter_is_wired():
    from http import HTTPStatus

    from axiom_forex.workstation.app import WorkstationApp

    app = WorkstationApp(wizard=simulated_wizard())
    status, path, message = app._dispatch("demo-test-submit", {})
    assert status == HTTPStatus.CONFLICT
    assert path == "/live"
    assert "DEMO trading is disabled" in message


def test_the_demo_test_route_refuses_without_calling_the_controller(rig):
    from http import HTTPStatus
    from types import SimpleNamespace

    from axiom_forex.workstation.app import WorkstationApp

    test = controller(rig)
    app = WorkstationApp(wizard=simulated_wizard())
    app.live = SimpleNamespace(demo_test=test)
    form = {name: [item] for name, item in values(rig).items()}
    form["nonce"] = [app.nonce]
    status, path, message = app._dispatch("demo-test-submit", form)
    assert status == HTTPStatus.CONFLICT
    assert path == "/live"
    assert "DEMO trading is disabled" in message
    assert len(rig["fake"].send_requests) == 0
    assert app.demo_test_result == {}


def test_the_demo_test_form_is_drawn_only_when_there_is_an_adapter():
    from types import SimpleNamespace

    from axiom_forex.workstation.ui.pages import live as live_page

    view = {
        "available": True,
        "confirmation": "SEND ONE DEMO ORDER",
        "account": {
            "mode": "DEMO",
            "confirmed": True,
            "is_demo": True,
            "armed": True,
            "fingerprint": "ab12cd34",
            "server": "Broker-Demo",
        },
        "execution": {"name": "LIVE", "live_effective": True},
        "symbols": [
            {
                "pair": "EUR/USD",
                "broker_symbol": "EURUSD.pro",
                "verified": True,
                "digits": 5,
                "point": "0.00001",
                "volume_min": "0.01",
                "volume_max": "100",
                "volume_step": "0.01",
                "stops_level": 0,
                "bid": "1.10000",
                "ask": "1.10020",
                "stop": {"points": 200, "declared": "200 points", "valid": True},
            }
        ],
        "blockers": [
            {"reason": "demo_symbol_required", "message": "Choose the broker symbol.",
             "hint": "None is guessed."}
        ],
        "record": {"status": "IDLE"},
    }
    ctx = SimpleNamespace(extra={"demo_test": view})
    html = live_page.demo_test_section(ctx, "nonce-1")
    text = " ".join(html.split())
    assert "action='/actions/demo-test-submit'" in html
    assert "Send one DEMO test order" in html
    assert "SEND ONE DEMO ORDER" in text
    assert "EUR/USD · EURUSD.pro" in html
    assert "broker stop level 0 points" in text
    assert "broker-side stop in force 200 points (200 points)" in text
    # Every field the operator must fill in is present, by name.
    for name in ("symbol", "side", "volume", "stop_points", "confirmation",
                 "account_confirmation"):
        assert f"name='{name}'" in html, name
    # The blocker is printed with its reason and its fix.
    assert "demo_symbol_required" in text and "None is guessed" in text
    # And the section says what it is not, and whose path it uses.
    assert "adds no second way to a broker" in text
    assert "Not a strategy result, not an AX-SCALPER trade, not a profitability claim" in text
