"""Focused tests for the paper strategy runner.

What is pinned here, in the order the runner itself works:

* it only ever trades in paper mode, and never gains an order-sending surface;
* a decision is made from completed bars and filled at the *next* price, so the
  no-lookahead rule survives the live path;
* every refusal path -- live mode, unselected candidate, stale tick, data gap,
  disconnect, gate refusal, unresolved intent -- trades nothing and says why;
* a restart restores the book and the counters and then waits for an explicit
  Start instead of resuming on its own;
* the evaluation spends its locked test window once and cannot promote a
  candidate from fixture or demo data;
* the UI reports the runner's own state and never invents a green one.

All data here is synthetic fixture data.  Nothing in this file is evidence about
a real market, and no result here is a profitability claim.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app.config.settings import SystemState
from app.data.models import ForexQuote, ForexQuoteBar, QuoteSideBar
from axiom_forex.execution.planner import ExecutionRejection
from axiom_forex.paper_runner.feed import (
    BAR,
    DISCONNECTED,
    GAP,
    STALE,
    TICK,
    FeedEvent,
    ReplayBarFeed,
)
from axiom_forex.paper_runner.host import lane_from_platform
from axiom_forex.paper_runner.journal import RunnerJournal
from axiom_forex.paper_runner.runner import (
    BLOCKED,
    REASON_DATA_GAP,
    REASON_DISCONNECTED,
    REASON_GATE_REFUSED,
    REASON_HOLD,
    REASON_INTENT_UNMATCHED,
    REASON_LIVE_NOT_CONFIGURED,
    REASON_MISSING_RISK_LIMIT,
    REASON_MARKET_CLOSED,
    REASON_MODE_UNKNOWN,
    REASON_NO_EXECUTION_PRICE,
    REASON_STALE_QUOTE,
    REASON_STRATEGY_NOT_SELECTED,
    RUNNING,
    STOPPED,
    Lane,
    PaperStrategyRunner,
    RunnerConfig,
)
from axiom_forex.paper_runner.strategy import (
    STATUS_INSUFFICIENT,
    STATUS_REJECTED,
    STATUS_SELECTED,
    Candidate,
    HoldBaseline,
    TrendPullbackV1,
    candidate_family,
    status_of,
)
from fixtures.m1 import PAIR, make_spec

UTC = timezone.utc
PIP = Decimal("0.0001")
#: 2026-03-10 13:50 UTC is inside the fixture calendar's core window
#: (04:00-21:00 UTC) and inside the fixture conversion-rate book's validity, so
#: the gate can price the trade instead of refusing a future rate.
START = datetime(2026, 3, 10, 13, 50, tzinfo=UTC)

PROMOTING_EVIDENCE = {
    "evaluated": True,
    "data_class": "USER_SUPPLIED_HISTORICAL",
    "passed": True,
    "failed_criteria": [],
    "dataset_id": "test-dataset",
    "criteria_version": "paper-runner-criteria-v1",
}


# ---------------------------------------------------------------------------
# fixture data
# ---------------------------------------------------------------------------


def make_bar(mid: Decimal, timestamp: datetime, *, spec=None) -> ForexQuoteBar:
    pair_spec = spec or make_spec()
    half = PIP / Decimal(2)
    bid = mid - half
    ask = mid + half
    bar = ForexQuoteBar(
        pair=PAIR,
        timeframe="1m",
        timestamp=timestamp,
        duration_seconds=60,
        bid=QuoteSideBar(open=bid, high=bid + half, low=bid - half, close=bid),
        ask=QuoteSideBar(open=ask, high=ask + half, low=ask - half, close=ask),
        source="PAPER_RUNNER_TEST_BARS",
        quote_count=12,
        last_quote_at=timestamp + timedelta(seconds=59),
        complete=True,
    )
    validation = bar.validate(pair_spec)
    assert validation.valid, validation.reason
    return bar


def mid_series(n_up: int = 25, n_down: int = 20) -> list[Decimal]:
    values: list[Decimal] = []
    value = Decimal("1.1000")
    for _ in range(n_up):
        values.append(value)
        value += PIP
    for _ in range(n_down):
        value -= PIP
        values.append(value)
    return values


def make_bars(mids: list[Decimal], *, start: datetime = START) -> list[ForexQuoteBar]:
    return [make_bar(mid, start + timedelta(minutes=index)) for index, mid in enumerate(mids)]


class Mode:
    """Minimal stand-in for the execution-mode controller (read-only for us)."""

    def __init__(self, mode: str = "PAPER", live_effective: bool = False) -> None:
        self.mode = mode
        self.live_effective = live_effective


class FakeFeed:
    """A scripted feed: each ``poll`` returns the next event, then repeats it."""

    kind = "SCRIPTED"

    def __init__(self, events, *, data_class: str = "SYNTHETIC_FIXTURE") -> None:
        self.events = list(events)
        self.data_class = data_class
        self._index = 0

    def poll(self) -> FeedEvent:
        event = self.events[min(self._index, len(self.events) - 1)]
        self._index += 1
        return event

    def describe(self):
        return {
            "kind": self.kind,
            "data_class": self.data_class,
            "label": "scripted test feed",
            "events": len(self.events),
        }


def candidate_for(
    fast: int = 8, slow: int = 21, evidence: dict | None = None
) -> Candidate:
    base = next(
        item
        for item in candidate_family()
        if item.parameters == {"fast_period": fast, "slow_period": slow}
    )
    return replace(base, evidence=dict(evidence or PROMOTING_EVIDENCE))


def make_runner(
    platform,
    feed,
    *,
    candidate: Candidate | None = None,
    mode: Mode | None = None,
    run_id: str = "test-run",
    quantity: Decimal = Decimal("0.10"),
    max_cycles: int = 0,
    journal: bool = True,
) -> PaperStrategyRunner:
    lane = lane_from_platform(platform)
    chosen = candidate or candidate_for()
    runner = PaperStrategyRunner(
        lane=lane,
        feed=feed,
        strategy=chosen.build(),
        candidate=chosen,
        config=RunnerConfig(
            run_id=run_id,
            symbol=PAIR,
            quantity=quantity,
            pause_seconds=0,
            max_cycles=max_cycles,
        ),
        mode=mode or Mode(),
        journal=RunnerJournal(repository=platform.repository) if journal else None,
    )
    return runner


# ---------------------------------------------------------------------------
# 1. paper only, no order surface
# ---------------------------------------------------------------------------


def test_the_runner_opens_and_closes_paper_positions_in_replay(platform):
    bars = make_bars(mid_series())
    runner = make_runner(platform, ReplayBarFeed(bars=bars))
    runner.restore()
    view = runner.start()
    assert view["state"] == RUNNING, view

    final = runner.run(max_steps=200)
    assert final["state"] == STOPPED
    assert final["reason"] == "feed_exhausted"
    assert final["counters"]["orders"] >= 2
    assert final["counters"]["closes"] >= 1
    fills = platform.paper_execution.fills()
    assert fills, "the replay should have produced at least one simulated fill"
    assert all(fill["action"] in {"OPEN", "CLOSE"} for fill in fills)
    # Every order went to the local paper broker: its capabilities say so, and
    # the simulated fill is the only place it landed.
    for record in platform.repository.list_orders():
        capabilities = record["receipt"]["capabilities"]
        assert capabilities["supports_live_orders"] is False
        assert capabilities["execution_mode"] == "LOCAL_PAPER"
        assert capabilities["order_types"] == ["SIMULATED_INTENT"]
    assert final["paper_only"] is True
    assert final["order_sending"] is False


def test_only_the_live_modules_can_arm_and_nothing_here_can_send_an_order():
    """The order-sending surface is confined to the modules that exist for it.

    ``live.py``/``live_workflow.py``/``live_host.py``/``live_assembly.py`` are the
    live leg and the operator workflow behind it; every other module in this
    package stays paper-only and cannot arm anything.  ``order_send`` appears
    nowhere: only the MT5 adapter may call it.
    """

    root = Path(__file__).resolve().parents[1] / "axiom_forex" / "paper_runner"
    live_modules = {"live.py", "live_host.py", "live_workflow.py", "live_assembly.py"}
    paper_only_forbidden = {
        "submit_order",
        "place_order",
        "arm_trading",
        "disarm_trading",
        "MT5Adapter",
        "enable_live",
        "set_live",
        "set_paper",
        "ExecutionModeController",
    }
    arming: dict[str, set[str]] = {}
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        reachable: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                reachable.add(node.attr)
            elif isinstance(node, ast.Name):
                reachable.add(node.id)
            elif isinstance(node, ast.ImportFrom):
                reachable.update(alias.name for alias in node.names)
                reachable.add(node.module or "")
        # No module here sends an order directly, live or not.
        assert "order_send" not in reachable, path.name
        assert "order_check" not in reachable, path.name
        if path.name in live_modules:
            if reachable & {"arm_trading", "disarm_trading"}:
                arming[path.name] = reachable & {"arm_trading", "disarm_trading"}
            continue
        assert reachable & paper_only_forbidden == set(), (path.name, sorted(reachable))


# ---------------------------------------------------------------------------
# 2. no lookahead
# ---------------------------------------------------------------------------


def test_a_signal_is_filled_at_the_next_bars_price_not_the_decision_bars(platform):
    bars = make_bars(mid_series())
    feed = ReplayBarFeed(bars=bars)
    runner = make_runner(platform, feed)
    runner.restore()
    runner.start()
    runner.run(max_steps=200)

    fills = platform.paper_execution.fills()
    assert fills
    opens = [fill for fill in fills if fill["action"] == "OPEN"]
    assert opens
    entry = opens[0]
    # The fill price is an *ask*, i.e. the next bar's open ask, and never the
    # decision bar's own close.
    assert entry["price"] in {bar.ask.open for bar in bars}
    decision_bars = [
        bar
        for bar in bars
        if bar.ask.open == Decimal(str(entry["price"]))
    ]
    assert decision_bars
    assert entry["filled_at"] >= decision_bars[0].timestamp


def test_the_strategy_cannot_see_bars_that_have_not_happened():
    strategy = TrendPullbackV1()
    bars = make_bars(mid_series())
    prefix = tuple(bars[: strategy.history_limit])
    future = tuple(bars[strategy.history_limit :])
    assert strategy.generate_signal(prefix) == strategy.generate_signal(prefix + future)


def test_the_planner_refuses_a_quote_stamped_before_the_decision_bar_closed(platform):
    bars = make_bars(mid_series())
    lane = lane_from_platform(platform)
    decision_bar = bars[10]
    earlier = ForexQuote(
        pair=PAIR,
        timestamp=decision_bar.timestamp - timedelta(seconds=1),
        bid=Decimal("1.09990"),
        ask=Decimal("1.10010"),
        source="TEST",
        quote_id="earlier",
        received_at=decision_bar.timestamp,
    )
    from axiom_forex.strategies.base import Signal

    plan = lane.planner.plan_at_quote(
        signal=Signal.BUY,
        positions={},
        quote=earlier,
        decision_bar=decision_bar,
        decision_index=10,
        run_id="no-lookahead",
        strategy_id="test",
    )
    assert isinstance(plan, ExecutionRejection)
    assert plan.code == "STALE_QUOTE"
    assert plan.reason == "quote_precedes_decision"


# ---------------------------------------------------------------------------
# 3. fail-closed paths
# ---------------------------------------------------------------------------


def test_a_live_mode_runner_without_a_live_path_refuses_to_start(platform):
    """Live is a real path, but only when one is configured.

    With live selected and no live leg wired in, the runner must refuse rather
    than fall back to paper: a paper fill shown under a LIVE label would misstate
    what happened.
    """

    bars = make_bars(mid_series())
    runner = make_runner(
        platform, ReplayBarFeed(bars=bars), mode=Mode("LIVE", live_effective=True)
    )
    view = runner.start()
    assert view["state"] == BLOCKED
    assert view["reason"] == REASON_LIVE_NOT_CONFIGURED
    assert platform.paper_execution.fills() == ()
    assert view["counters"]["orders"] == 0
    assert view["live_configured"] is False
    assert view["trading_capability"] == "paper_simulation_only"


def test_an_unknown_execution_mode_is_refused(platform):
    runner = make_runner(platform, ReplayBarFeed(bars=make_bars(mid_series())), mode=Mode(""))
    view = runner.start()
    assert view["state"] == BLOCKED
    assert view["reason"] == REASON_MODE_UNKNOWN
    assert platform.paper_execution.fills() == ()


def test_an_unselected_or_rejected_candidate_cannot_run(platform):
    rejected = candidate_for(
        evidence={
            **PROMOTING_EVIDENCE,
            "passed": False,
            "failed_criteria": ["drawdown_limit"],
        }
    )
    assert status_of(rejected.candidate_id, rejected.evidence) == STATUS_REJECTED
    runner = make_runner(platform, ReplayBarFeed(bars=make_bars(mid_series())), candidate=rejected)
    view = runner.start()
    assert view["state"] == BLOCKED
    assert view["reason"] == REASON_STRATEGY_NOT_SELECTED
    assert platform.paper_execution.fills() == ()


def test_fixture_and_demo_evidence_can_never_select_a_candidate():
    evidence = {**PROMOTING_EVIDENCE, "data_class": "SYNTHETIC_FIXTURE"}
    assert status_of("x", evidence) == STATUS_INSUFFICIENT
    demo = {**PROMOTING_EVIDENCE, "data_class": "USER_SUPPLIED_DEMO"}
    assert status_of("x", demo) == STATUS_INSUFFICIENT
    assert status_of("x", {}) == "CANDIDATE_UNTESTED"
    assert status_of("x", PROMOTING_EVIDENCE) == STATUS_SELECTED


def test_a_stale_tick_trades_nothing_and_says_why(platform):
    quote = ForexQuote(
        pair=PAIR,
        timestamp=START,
        bid=Decimal("1.09990"),
        ask=Decimal("1.10010"),
        source="TEST",
        quote_id="stale",
        received_at=START,
    )
    feed = FakeFeed([FeedEvent(STALE, at=START, quote=quote, reason="stale_tick")])
    runner = make_runner(platform, feed)
    runner.restore()
    runner.start()
    view = runner.run(max_steps=3)
    assert view["reason"] == REASON_STALE_QUOTE
    assert platform.paper_execution.fills() == ()
    assert view["counters"]["refusals"] >= 1


def test_a_data_gap_is_reported_and_never_traded_through(platform):
    feed = FakeFeed([FeedEvent(GAP, at=START, reason="data_gap", detail="skipped_windows=3")])
    runner = make_runner(platform, feed)
    runner.restore()
    runner.start()
    view = runner.run(max_steps=2)
    assert view["reason"] == REASON_DATA_GAP
    assert platform.paper_execution.fills() == ()


def test_a_disconnected_terminal_stops_the_loop(platform):
    feed = FakeFeed([FeedEvent(DISCONNECTED, at=START, reason="terminal_gone")])
    runner = make_runner(platform, feed)
    runner.restore()
    runner.start()
    view = runner.run(max_steps=5)
    assert view["state"] == STOPPED
    assert view["reason"] == REASON_DISCONNECTED
    assert platform.paper_execution.fills() == ()


def test_ticks_alone_never_produce_a_decision(platform):
    quote = ForexQuote(
        pair=PAIR,
        timestamp=START,
        bid=Decimal("1.09990"),
        ask=Decimal("1.10010"),
        source="TEST",
        quote_id="tick",
        received_at=START,
    )
    feed = FakeFeed([FeedEvent(TICK, at=START, quote=quote)])
    runner = make_runner(platform, feed)
    runner.restore()
    runner.start()
    view = runner.run(max_steps=3)
    assert view["counters"]["decisions"] == 0
    assert view["counters"]["bars"] == 0
    assert platform.paper_execution.fills() == ()


def test_a_risk_gate_refusal_records_the_reason_and_places_nothing(platform):
    bars = make_bars(mid_series())
    lane = lane_from_platform(platform)
    frozen = replace(
        lane.planner,
        context_factory=replace(
            lane.planner.context_factory,
            system_state=SystemState(
                emergency_stop=True,
                kill_switch_active=False,
                execution_paused=False,
            ),
        ),
    )
    stuck = Lane(
        spec=lane.spec,
        planner=frozen,
        portfolio=lane.portfolio,
        paper_execution=lane.paper_execution,
        quote_freshness=lane.quote_freshness,
        audit_log=lane.audit_log,
        repository=lane.repository,
    )
    chosen = candidate_for()
    runner = PaperStrategyRunner(
        lane=stuck,
        feed=ReplayBarFeed(bars=bars),
        strategy=chosen.build(),
        candidate=chosen,
        config=RunnerConfig(run_id="gate", symbol=PAIR, quantity=Decimal("0.10"), pause_seconds=0),
        mode=Mode(),
        journal=RunnerJournal(repository=platform.repository),
    )
    runner.restore()
    runner.start()
    view = runner.run(max_steps=200)
    assert view["counters"]["refusals"] >= 1
    assert platform.paper_execution.fills() == ()
    assert any(
        item["reason"] in {REASON_GATE_REFUSED, REASON_MARKET_CLOSED} for item in view["recent"]
    )


def test_an_intent_without_an_order_blocks_the_next_start(platform):
    journal = RunnerJournal(repository=platform.repository)
    journal.write_intent(
        run_id="orphan",
        sequence=0,
        payload={"request_id": "orphan:0:OPEN", "action": "OPEN", "side": "BUY", "quantity": "0.10"},
    )
    runner = make_runner(
        platform, ReplayBarFeed(bars=make_bars(mid_series())), run_id="orphan"
    )
    runner.restore()
    view = runner.start()
    assert view["state"] == BLOCKED
    assert view["reason"] == REASON_INTENT_UNMATCHED
    assert platform.paper_execution.fills() == ()


def test_cycles_are_bounded_and_stop_with_a_reason(platform):
    runner = make_runner(
        platform, ReplayBarFeed(bars=make_bars(mid_series())), max_cycles=3
    )
    runner.restore()
    runner.start()
    view = runner.run(max_steps=50)
    assert view["state"] == STOPPED
    assert view["reason"] == "cycle_limit_reached"
    assert view["counters"]["cycles"] == 3


# ---------------------------------------------------------------------------
# 4. restart
# ---------------------------------------------------------------------------


def test_restart_restores_the_book_and_counters_and_waits_for_a_start(platform):
    bars = make_bars(mid_series())
    first = make_runner(platform, ReplayBarFeed(bars=bars))
    first.restore()
    first.start()
    before = first.run(max_steps=200)
    assert before["counters"]["fills"] >= 1
    positions = dict(platform.portfolio.positions)

    # A second runner over the same durable state: it must rebuild the book.
    second = make_runner(platform, ReplayBarFeed(bars=bars))
    second.restore()
    view = second.view()
    assert view["state"] in {STOPPED, BLOCKED}
    assert view["restored"] is True
    assert view["restored_from"]
    assert set(platform.portfolio.positions) == set(positions)
    assert view["counters"]["fills"] >= before["counters"]["fills"] - before["counters"]["orders"]
    # Restarting never trades by itself.
    assert view["counters"]["orders"] <= before["counters"]["orders"]
    journal_state = RunnerJournal(repository=platform.repository).load_state("test-run")
    assert journal_state is not None
    assert journal_state["paper_only"] is True


def test_restart_never_duplicates_a_request_id(platform):
    bars = make_bars(mid_series())
    first = make_runner(platform, ReplayBarFeed(bars=bars))
    first.restore()
    first.start()
    first.run(max_steps=200)

    second = make_runner(platform, ReplayBarFeed(bars=bars))
    second.restore()
    second.start()
    second.run(max_steps=200)
    request_ids = [row["request_id"] for row in platform.repository.list_orders()]
    assert len(request_ids) == len(set(request_ids))


def test_a_bar_with_no_later_price_is_never_traded(platform):
    """The final bar has no next price, so a signal there cannot become a fill."""

    bars = make_bars(mid_series())
    feed = FakeFeed([FeedEvent(BAR, at=START, bar=bars[0], quote=None)])
    chosen = candidate_for()
    runner = PaperStrategyRunner(
        lane=lane_from_platform(platform),
        feed=feed,
        strategy=_AlwaysBuy(),
        candidate=chosen,
        config=RunnerConfig(run_id="last-bar", symbol=PAIR, quantity=Decimal("0.10"), pause_seconds=0),
        mode=Mode(),
        journal=RunnerJournal(repository=platform.repository),
    )
    runner.restore()
    runner.start()
    view = runner.run(max_steps=5)
    assert view["reason"] == REASON_NO_EXECUTION_PRICE
    assert platform.paper_execution.fills() == ()
    assert view["counters"]["signals"] >= 1


class _AlwaysBuy:
    """A deliberate always-on signal, used only to reach the execution branch."""

    name = "always_buy"
    history_limit = 1

    def generate_signal(self, bars):
        from axiom_forex.strategies.base import Signal

        return Signal.BUY


def test_hold_baseline_never_trades(platform):
    runner = make_runner(
        platform,
        ReplayBarFeed(bars=make_bars(mid_series())),
        candidate=Candidate(
            candidate_id="baseline_hold",
            strategy_factory=lambda **_: HoldBaseline(),
            parameters={},
            rationale="zero-trade floor",
            evidence=dict(PROMOTING_EVIDENCE),
        ),
    )
    runner.restore()
    runner.start()
    view = runner.run(max_steps=200)
    assert platform.paper_execution.fills() == ()
    assert view["reason"] == "feed_exhausted"
    assert view["counters"]["decisions"] > 0
    reasons = {item["reason"] for item in view["recent"]}
    assert not reasons & {"paper_position_opened", "paper_position_closed"}
    assert reasons & {REASON_HOLD}


def test_a_paper_position_blocks_a_second_entry_and_nothing_stacks(platform):
    runner = make_runner(platform, ReplayBarFeed(bars=make_bars(mid_series())))
    runner.restore()
    runner.start()
    view = runner.run(max_steps=200)
    assert len(platform.portfolio.positions) <= 1
    assert view["counters"]["orders"] <= 2


# ---------------------------------------------------------------------------
# 5. the candidate family
# ---------------------------------------------------------------------------


def test_every_candidate_starts_unselected_and_carries_its_assumptions():
    for item in candidate_family():
        assert item.status == "CANDIDATE_UNTESTED"
        assert item.assumptions
        assert item.limitations
        assert item.parameters
        strategy = item.build()
        assert strategy.history_limit >= max(item.parameters.values())


# ---------------------------------------------------------------------------
# 6. the owner thread
# ---------------------------------------------------------------------------


def _host(platform_factory, bars, *, runner_id="host-run"):
    from axiom_forex import build_fixture_platform  # noqa: F401 (built on the thread)
    from axiom_forex.paper_runner.host import PaperRunnerHost

    chosen = candidate_for()

    def build_runner(platform, feed):
        return PaperStrategyRunner(
            lane=lane_from_platform(platform),
            feed=feed,
            strategy=chosen.build(),
            candidate=chosen,
            config=RunnerConfig(
                run_id=runner_id, symbol=PAIR, quantity=Decimal("0.10"), pause_seconds=0
            ),
            mode=Mode(),
            journal=RunnerJournal(repository=platform.repository),
        )

    return PaperRunnerHost(
        build_platform=platform_factory,
        build_feed=lambda platform: bars,
        build_runner=build_runner,
        pause_seconds=0.01,
    )


def test_the_host_builds_and_runs_everything_on_one_thread():
    import threading

    from axiom_forex import build_fixture_platform

    created: list[int] = []

    def factory():
        created.append(threading.get_ident())
        return build_fixture_platform()

    host = _host(factory, ReplayBarFeed(bars=make_bars(mid_series())))
    try:
        host.start()
        worker = host.submit(lambda platform: threading.get_ident())
        assert created == [worker]
        assert worker != threading.get_ident()
        assert host.view()["available"] is True
        assert host.view()["state"] in {"STOPPED", "BLOCKED"}
    finally:
        host.close()
    assert host.alive is False


def test_the_host_starts_and_stops_a_replay_runner_through_the_same_thread():
    from axiom_forex import build_fixture_platform

    bars = FakeFeed(
        [
            FeedEvent(
                TICK,
                at=START,
                quote=ForexQuote(
                    pair=PAIR,
                    timestamp=START,
                    bid=Decimal("1.09990"),
                    ask=Decimal("1.10010"),
                    source="TEST",
                    quote_id="t",
                    received_at=START,
                ),
            )
        ]
    )
    host = _host(build_fixture_platform, bars)
    try:
        host.start()
        started = host.start_runner()
        assert started["state"] == RUNNING, started
        assert host.view()["running"] is True
        stopped = host.stop_runner()
        assert stopped["state"] == STOPPED
        assert host.view()["running"] is False
    finally:
        host.close()


def test_a_host_whose_platform_cannot_be_built_reports_instead_of_raising():
    from axiom_forex.paper_runner.host import PaperRunnerHost

    def broken():
        raise RuntimeError("no database")

    host = PaperRunnerHost(
        build_platform=broken,
        build_feed=lambda platform: None,
        build_runner=lambda platform, feed: None,
    )
    try:
        view = host.start()
        # A failed build is a *blocker* the page can render, not exception text.
        # The reason code is stable and operator-facing; the exception message can
        # name internal objects, so it never reaches the UI.
        assert view["available"] is True
        assert view["blocked"] is True
        assert view["running"] is False
        assert view["reason"] == "runner_build_failed"
        assert view["error"] == ""
        assert "no database" not in repr(view)
        assert view["blockers"][0]["reason"] == "runner_build_failed"
    finally:
        host.close()


# ---------------------------------------------------------------------------
# 7. the data audit
# ---------------------------------------------------------------------------


def test_the_shipped_sample_file_is_flagged_as_a_format_example():
    from axiom_forex.paper_runner import data_audit

    report = data_audit.audit(directory="imports")
    names = {item["name"] for item in report["files"]}
    assert "SAMPLE_canonical_quote_bars.csv" in names
    sample = next(item for item in report["files"] if item["name"].startswith("SAMPLE"))
    assert sample["looks_like_sample"] is True
    assert sample["header_ok"] is True
    assert report["ready_for_evaluation"] is False
    assert report["ready_for_paper_runner"] is False
    assert any("no dataset has been imported" in problem for problem in report["problems"])
    assert "Historical data audit" in data_audit.markdown(report)


def test_demo_data_is_not_a_promoting_dataset(platform):
    from axiom_forex.paper_runner import data_audit

    datasets = [
        {
            "dataset_id": "demo-1",
            "pair": PAIR,
            "timeframe": "1m",
            "data_class": "USER_SUPPLIED_DEMO",
            "source": "DEMO",
            "source_version": "v1",
            "valid_from": START.isoformat(),
            "valid_until": (START + timedelta(minutes=5)).isoformat(),
            "bar_count": 5,
            "quote_count": 5,
            "imported_at": START.isoformat(),
            "promoting": False,
        }
    ]
    # The fixture platform registers only its own SYNTHETIC_FIXTURE dataset.
    report = data_audit.audit(repository=platform.repository, directory="imports")
    assert all(item["promoting"] is False for item in report["datasets"])
    assert data_audit.coverage(datasets)[0]["promoting"] is False
    assert any("can never select a strategy" in problem for problem in report["problems"])
    assert report["ready_for_evaluation"] is False


def test_the_audit_never_reads_a_path_outside_the_import_directory(tmp_path):
    from axiom_forex.paper_runner import data_audit

    report = data_audit.audit(directory=str(tmp_path))
    assert report["files"] == []
    assert report["ready_for_evaluation"] is False


# ---------------------------------------------------------------------------
# 8. the terminal feed's late-bound data surface
# ---------------------------------------------------------------------------


def test_the_terminal_feed_reports_disconnected_until_a_session_exists():
    from axiom_forex.paper_runner.feed import LateBoundMarketData, TerminalBarFeed

    class Refused(Exception):
        reason = "not_connected"

    def factory():
        raise Refused("no terminal session is open")

    feed = TerminalBarFeed(
        market=LateBoundMarketData(factory), spec=make_spec(), clock=lambda: START
    )
    event = feed.poll()
    assert event.kind == DISCONNECTED
    assert event.reason == "not_connected"


def test_the_terminal_feed_starts_polling_once_a_session_appears():
    from axiom_forex.paper_runner.feed import LateBoundMarketData, TerminalBarFeed

    quote = ForexQuote(
        pair=PAIR,
        timestamp=START,
        bid=Decimal("1.09990"),
        ask=Decimal("1.10010"),
        source="TEST",
        quote_id="late",
        received_at=START,
    )

    class Market:
        def symbol_for(self, spec):
            return "AAA_BBB"

        def quote(self, spec):
            return quote

    feed = TerminalBarFeed(
        market=LateBoundMarketData(lambda: Market()), spec=make_spec(), clock=lambda: START
    )
    event = feed.poll()
    assert event.kind == TICK
    assert event.quote is quote


def test_an_unresolved_paper_order_blocks_the_start(platform):
    lane = lane_from_platform(platform)

    class InDoubt:
        """Only ``unresolved_orders`` is asked of the engine by the guard."""

        def unresolved_orders(self):
            return (("request-in-doubt", "SUBMITTED"),)

    doubtful = Lane(
        spec=lane.spec,
        planner=lane.planner,
        portfolio=lane.portfolio,
        paper_execution=InDoubt(),
        quote_freshness=lane.quote_freshness,
        audit_log=lane.audit_log,
        repository=lane.repository,
    )
    chosen = candidate_for()
    runner = PaperStrategyRunner(
        lane=doubtful,
        feed=ReplayBarFeed(bars=make_bars(mid_series())),
        strategy=chosen.build(),
        candidate=chosen,
        config=RunnerConfig(run_id="in-doubt", symbol=PAIR, quantity=Decimal("0.10"), pause_seconds=0),
        mode=Mode(),
        journal=RunnerJournal(repository=platform.repository),
    )
    view = runner.start()
    assert view["state"] == BLOCKED
    assert view["reason"] == "unresolved_order_present"
    assert view["blockers"][0]["hint"]


# ---------------------------------------------------------------------------
# 7. an automated run needs a declared, finite risk ceiling before it starts
# ---------------------------------------------------------------------------


def _lane_with_limit(platform, limit, *, currency=None):
    """The same fixture lane, but with the entry cap this test chooses.

    The fixture platform is built fresh for every test, so replacing its
    reservation book here touches nothing another test can see.
    """

    from axiom_forex.execution.reservations import OrderReservationBook

    existing = platform.paper_execution.reservations
    book = OrderReservationBook(
        platform.repository,
        entry_limit=limit,
        account_currency=currency or existing.account_currency,
    )
    platform.paper_execution.reservations = book
    return lane_from_platform(platform)


def _runner_on(lane, *, candidate=None):
    chosen = candidate or candidate_for()
    return PaperStrategyRunner(
        lane=lane,
        feed=ReplayBarFeed(bars=make_bars(mid_series())),
        strategy=chosen.build(),
        candidate=chosen,
        config=RunnerConfig(
            run_id="risk-limit-run",
            symbol=PAIR,
            quantity=Decimal("0.10"),
            pause_seconds=0,
        ),
        mode=Mode(),
    )


def test_a_runner_with_no_declared_entry_limit_never_starts(platform):
    runner = _runner_on(_lane_with_limit(platform, None))

    view = runner.start()

    assert view["state"] == BLOCKED
    assert view["reason"] == REASON_MISSING_RISK_LIMIT
    assert view["running"] is False
    # The message says what is missing and what clears it -- not "unlimited".
    blocker = view["blockers"][0]
    assert "no entry-exposure limit is declared" in blocker["message"].lower()
    assert "never read as unlimited" in blocker["hint"].lower()
    # Starting refused, so not one cycle ran and nothing was filled.
    assert runner.counters["cycles"] == 0
    assert runner.counters["fills"] == 0
    assert view["fills"] == []


def test_a_runner_with_a_non_finite_entry_limit_never_starts(platform):
    from axiom_forex.risk.entry_limits import EntryExposureLimit

    limit = EntryExposureLimit(
        limit_account=Decimal("NaN"),
        account_currency=platform.paper_execution.reservations.account_currency,
        version="test-v1",
        source="TEST",
    )
    runner = _runner_on(_lane_with_limit(platform, limit))

    view = runner.start()

    assert view["state"] == BLOCKED
    assert view["reason"] == REASON_MISSING_RISK_LIMIT
    assert runner.counters["fills"] == 0


def test_a_runner_whose_entry_limit_is_in_another_currency_never_starts(platform):
    from axiom_forex.risk.entry_limits import EntryExposureLimit

    account = platform.paper_execution.reservations.account_currency
    other = "ZZZ" if account != "ZZZ" else "YYY"
    limit = EntryExposureLimit(
        limit_account=Decimal("250"),
        account_currency=other,
        version="test-v1",
        source="TEST",
    )
    runner = _runner_on(_lane_with_limit(platform, limit))

    view = runner.start()

    assert view["state"] == BLOCKED
    assert view["reason"] == REASON_MISSING_RISK_LIMIT
    assert "different currency" in view["blockers"][0]["message"].lower()


def test_a_declared_finite_limit_still_starts_the_runner(platform):
    runner = _runner_on(lane_from_platform(platform))

    view = runner.start()

    assert view["state"] == RUNNING
    assert view["running"] is True
