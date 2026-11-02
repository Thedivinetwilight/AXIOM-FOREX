"""The trailing stop must be able to *act*, on both sides, and only when it should.

The defect these tests cover: ``_manage`` guarded the trailing exit with

    index > position.bar_index

but ``_decide`` sets ``position.bar_index = index`` on the line above the call.  The
comparison was therefore ``index > index`` -- permanently false.  The trail ratcheted
and was displayed, persisted and reconciled, but it could never close a position,
before or after a restart.  The intent (and the rule the evaluation applies in
``simulate.py``) is ``index > entry_index``: a stop or trail may act from the *next*
bar, never on the bar that opened the trade.

Paper cycles cannot compare raw indices: the series is rebuilt from a sliding rate
request every cycle, so index 300 this cycle is not index 300 next cycle.  The guard
now compares the bars' own stamps, and the entry bar travels in the state file.

Everything here is fixture data -- a hand-written bar path, a synthetic ATR, and a
temporary folder.  No terminal is connected, no order is sent, and nothing measured
here says anything about a real broker or about profitability.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from axiom_forex.paper_runner import accounting
from axiom_forex.paper_runner.catalog_paper import (
    EXIT_TRAIL,
    STATE_VERSION,
    CatalogPaperTest,
    Watch,
)
from axiom_forex.strategies.catalog.candidates import catalog

from tests.test_paper_accounting import _account_app, _mark, _text
from tests.test_paper_restart import (
    SECONDS,
    START,
    Clock,
    ReplayQuotes,
    _advance,
    _test,
)
from tests.test_workstation_ui import render

#: A row whose only stop *is* an ATR trail (stop_atr and target_atr are None).
TRAILING = next(
    item
    for item in catalog()
    if item.entry == "sma_cross" and item.exit == "trailing_atr" and item.filter == "none"
)

#: The candidate needs this many completed bars before it will decide at all.
WARMUP = 250


def _watch() -> Watch:
    return Watch(
        candidate_id=TRAILING.candidate_id,
        symbol="EURUSD.pro",
        timeframe="15m",
        candidate=TRAILING,
    )


def _flat(count: int = WARMUP, price: float = 1.1000) -> list[float]:
    """Warm-up bars. The ATR window needs history; nothing else uses them."""

    return [round(price + (0.00001 if index % 2 else -0.00001), 5) for index in range(count)]


def _state(
    root: Path,
    *,
    direction: str = "LONG",
    entry_price: float = 1.10000,
    best: float = 1.10120,
    trail: float = 1.09880,
    atr: float = 0.00120,
    version: int = STATE_VERSION,
    drop: tuple[str, ...] = (),
) -> Path:
    """One open trailing position, as the runner itself would have written it."""

    watch = _watch()
    record = {
        "state_version": version,
        "key": watch.key,
        "candidate_id": watch.candidate_id,
        "symbol": watch.symbol,
        "timeframe": watch.timeframe,
        "direction": direction,
        "lots": 0.1,
        "entry_at": "2026-11-02T00:00:00+00:00",
        "entry_price": entry_price,
        "entry_mid": entry_price,
        "atr": atr,
        "bar_index": WARMUP - 1,
        "bar_at": "2026-11-04T14:15:00+00:00",
        "entry_bar_at": "2026-11-04T14:15:00+00:00",
        "entry_bar_index": WARMUP - 1,
        "stop": None,
        "target": None,
        "trail": trail,
        "best": best,
        "stop_initial": None,
        "target_initial": None,
        "trail_initial": entry_price,
        "notional_quote": 11000.0,
        "quote_currency": "USD",
        "contract_size": 100000.0,
        "restored_after_restart": False,
        "priced_from": "BROKER_TICK",
        "simulated": True,
    }
    for name in drop:
        record.pop(name, None)
    document = {
        "version": version,
        "updated_at": "2026-11-04T14:30:00+00:00",
        "status": "STOPPED",
        "running": False,
        "positions": [record],
        "last_bar": {},
        "watches": [],
    }
    path = root / "catalog-paper-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _run(root: Path, closes, *, steps: int, quotes_cls=ReplayQuotes, **quote_kwargs):
    """Resume the seeded position and walk ``steps`` completed bars."""

    clock = Clock(START + timedelta(seconds=SECONDS * WARMUP))
    quotes = quotes_cls(closes, clock=clock, cursor=WARMUP, **quote_kwargs)
    test = _test(root, quotes, clock, candidate=TRAILING)
    report = test._restore_state()
    assert report["ok"] is True, report
    trail_seen: list[float | None] = []
    for _ in range(steps):
        _advance(quotes, clock)
        test._cycle()
        positions = test.snapshot()["positions"]
        trail_seen.append(positions[0]["trail"] if positions else None)
        if test.snapshot()["exit_fills"]:
            break
    return test, quotes, clock, trail_seen


def _ledger(root: Path) -> list[dict]:
    path = root / "catalog-paper-ledger.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _closes(root: Path) -> list[dict]:
    return [event for event in _ledger(root) if event.get("kind") == "simulated_close"]


# ---------------------------------------------------------------------------
# 1. a trailing stop ratchets, and then acts -- long and short
# ---------------------------------------------------------------------------


def test_a_long_trail_ratchets_up_and_closes_when_a_later_bar_crosses_it(tmp_path):
    _state(tmp_path, direction="LONG", best=1.10120, trail=1.09880)
    # Four rising bars ratchet the trail up, then a fall crosses it.
    closes = _flat() + [1.1010, 1.1020, 1.1030, 1.1040, 1.1000, 1.0960, 1.0920]

    test, _quotes, _clock, trails = _run(tmp_path, closes, steps=10)

    # The trail really ratcheted upward while price made higher highs.
    assert trails[0] is not None
    assert max(t for t in trails if t) > trails[0]
    # ...and then it closed the position, at the level the previous bar left.
    closes_written = _closes(tmp_path)
    assert len(closes_written) == 1
    event = closes_written[0]
    carried = [t for t in trails if t is not None][-1]
    assert event["reason"] == EXIT_TRAIL
    assert event["exit_basis"] == "LEVEL"
    # A level-filled exit carries no spread or slippage: the price *is* the level.
    assert abs(float(event["exit_price"]) - round(carried, 8)) < 1e-9
    assert test.snapshot()["positions"] == []
    assert test.snapshot()["stats"]["closes"] == 1


def test_a_short_trail_ratchets_down_and_closes_when_a_later_bar_crosses_it(tmp_path):
    _state(tmp_path, direction="SHORT", best=1.09880, trail=1.10120)
    # Falling bars ratchet the trail down, then a rally crosses it.
    closes = _flat() + [1.0990, 1.0980, 1.0970, 1.0960, 1.1000, 1.1040, 1.1080]

    test, _quotes, _clock, trails = _run(tmp_path, closes, steps=10)

    assert trails[0] is not None
    assert min(t for t in trails if t) < trails[0]
    closes_written = _closes(tmp_path)
    assert len(closes_written) == 1
    event = closes_written[0]
    carried = [t for t in trails if t is not None][-1]
    assert event["reason"] == EXIT_TRAIL
    assert abs(float(event["exit_price"]) - round(carried, 8)) < 1e-9
    assert test.snapshot()["positions"] == []


def test_the_trail_does_not_close_while_price_has_not_crossed_it(tmp_path):
    """The control for the two tests above: same seed, a path that never crosses."""

    _state(tmp_path, direction="LONG", best=1.10120, trail=1.09880)
    closes = _flat() + [1.1010, 1.1020, 1.1030, 1.1040, 1.1050, 1.1060]

    test, _quotes, _clock, trails = _run(tmp_path, closes, steps=6)

    assert test.snapshot()["positions"], "the position must still be open"
    assert test.snapshot()["exit_fills"] == []
    assert all(t is not None for t in trails)


# ---------------------------------------------------------------------------
# 2. never on the entry bar -- the guard's whole purpose
# ---------------------------------------------------------------------------


def _entry_bar_scenario(tmp_path: Path):
    """A position whose trail sits *at* the fill, as a real entry starts it.

    That is how the runner opens an ATR trail (``trail_initial = entry price``), so
    the entry bar's own range always crosses it.  Acting on it would stop the trade
    out at the price it entered at, on the bar it entered on.
    """

    watch = _watch()
    _state(tmp_path, direction="LONG", best=1.10000, trail=1.10000)
    clock = Clock(START + timedelta(seconds=SECONDS * WARMUP))
    # The next bar trades straight through the level.
    closes = _flat() + [1.0940, 1.0940]
    quotes = ReplayQuotes(closes, clock=clock, cursor=WARMUP)
    test = _test(tmp_path, quotes, clock, candidate=TRAILING)
    assert test._restore_state()["ok"] is True
    return watch, test, quotes, clock


def test_a_trail_at_the_fill_does_not_stop_the_trade_out_on_the_entry_bar(tmp_path):
    watch, test, quotes, clock = _entry_bar_scenario(tmp_path)

    # Re-decide the *entry bar itself* -- the case the guard exists for.  It happens
    # for real when a restart resumes a position whose last decided bar was the bar
    # that opened it: ``_last_bar`` is empty after a restore, so that bar is decided
    # again.  No bar is advanced here, so the bar being managed *is* the entry bar.
    test._cycle()
    test._last_bar.clear()
    test._cycle()

    position = test._positions[watch.key]
    snapshot = test.snapshot()
    assert snapshot["positions"], "the entry bar must never arm the trail"
    assert snapshot["exit_fills"] == []
    # The bar being managed is the entry bar, so the guard is false by identity...
    assert position.entry_bar_at == position.bar_at
    # ...even though that bar's own range is already through the level.  Without the
    # guard these two numbers alone would have stopped the trade out here, at the
    # price it entered at.
    assert snapshot["positions"][0]["trail"] == 1.10000

    # One bar later the same level, unchanged, is allowed to act.
    _advance(quotes, clock)
    test._cycle()
    closed = test.snapshot()["exit_fills"]
    assert closed, "from the next bar the trail must be able to close the trade"
    assert closed[0]["reason"] == EXIT_TRAIL
    assert abs(float(closed[0]["exit_price"]) - 1.10000) < 1e-9


def test_the_guard_is_false_on_the_entry_bar_and_true_after_it(tmp_path):
    """The narrow unit-level statement of the fix, without the runner around it."""

    from axiom_forex.paper_runner.catalog_paper import _after_entry_bar

    entry = "2026-11-04T14:15:00+00:00"
    assert _after_entry_bar(entry, entry) is False
    assert _after_entry_bar(entry, "2026-11-04T14:30:00+00:00") is True
    assert _after_entry_bar(entry, "2026-11-04T14:00:00+00:00") is False
    # An unknown identity never arms a stop: delay, never fabricate.
    assert _after_entry_bar("", entry) is False
    assert _after_entry_bar(entry, "") is False
    assert _after_entry_bar("not a timestamp", entry) is False


def _decline_then_rise(down: int = 250, up: int = 30, fall: int = 30) -> list[float]:
    """A path the runner really enters: a downtrend, a cross up, then a fall."""

    closes, price = [], 1.1400
    for _ in range(down):
        price = round(price - 0.00008, 5)
        closes.append(price)
    for _ in range(up):
        price = round(price + 0.00030, 5)
        closes.append(price)
    for _ in range(fall):
        price = round(price - 0.00050, 5)
        closes.append(price)
    return closes


def _run_to_entry(root: Path, closes, *, limit: int = 60):
    """Cycle until the runner itself opens the trailing position."""

    clock = Clock(START + timedelta(seconds=SECONDS * WARMUP))
    quotes = ReplayQuotes(closes, clock=clock, cursor=WARMUP)
    test = _test(root, quotes, clock, candidate=TRAILING)
    assert test._restore_state()["ok"] is True
    for _ in range(limit):
        _advance(quotes, clock)
        test._cycle()
        snapshot = test.snapshot()
        if snapshot["entry_fills"]:
            return test, quotes, clock, snapshot
    raise AssertionError("the fixture should open a trailing position")


def test_a_position_the_runner_enters_survives_its_entry_bar_then_trails_out(tmp_path):
    """End to end, with no hand-seeded state: the entry bar, then the trail acting."""

    test, quotes, clock, at_entry = _run_to_entry(tmp_path, _decline_then_rise())

    # The fill is on the ledger, and the bar that opened it is recorded.
    entry = at_entry["entry_fills"][0]
    position = at_entry["positions"][0]
    assert entry["price"] == position["entry_price"]
    assert position["entry_bar_at"] == position["bar_at"], "same bar: the guard is false"
    # A trailing row starts its trail *at* the fill, so this bar's own range is
    # already through it -- and the trade is still open.
    assert position["trail_initial"] == pytest.approx(position["entry_price"], abs=1e-9)
    assert position["trail"] == pytest.approx(position["entry_price"], abs=1e-9)

    # From the next completed bar the same level is allowed to act.
    _advance(quotes, clock)
    test._cycle()
    closed = test.snapshot()["exit_fills"]
    assert closed, "the trail must be able to close the trade from the next bar"
    event = closed[0]
    assert event["reason"] == EXIT_TRAIL
    assert event["exit_basis"] == "LEVEL"
    assert event["entry_bar_at"] == position["entry_bar_at"]
    assert abs(float(event["exit_price"]) - float(event["trail"])) < 1e-9


# ---------------------------------------------------------------------------
# 3. a restart must not change what the trail does
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("restart_after", [2, 4])
def test_a_trailing_exit_survives_a_stop_and_restart_unchanged(tmp_path, restart_after):
    closes = _flat() + [1.1010, 1.1020, 1.1030, 1.1040, 1.1000, 1.0960, 1.0920]
    straight_root = tmp_path / "straight"
    resumed_root = tmp_path / "resumed"
    _state(straight_root, best=1.10120, trail=1.09880)
    _state(resumed_root, best=1.10120, trail=1.09880)

    straight, _q, _c, straight_trails = _run(straight_root, closes, steps=10)

    clock = Clock(START + timedelta(seconds=SECONDS * WARMUP))
    quotes = ReplayQuotes(closes, clock=clock, cursor=WARMUP)
    test = _test(resumed_root, quotes, clock, candidate=TRAILING)
    assert test._restore_state()["ok"] is True
    resumed_trails: list[float | None] = []
    for step in range(1, 11):
        _advance(quotes, clock)
        test._cycle()
        positions = test.snapshot()["positions"]
        resumed_trails.append(positions[0]["trail"] if positions else None)
        if step == restart_after:
            test.stop()
            resumed = _test(resumed_root, quotes, clock, candidate=TRAILING)
            report = resumed._restore_state()
            assert report["ok"] is True
            assert report["restored"] == 1
            test = resumed
        if test.snapshot()["exit_fills"]:
            break

    # Identical ratchet, identical exit, identical ledger totals.
    assert [t for t in resumed_trails if t is not None] == [t for t in straight_trails if t is not None]
    assert _shape(_closes(resumed_root)) == _shape(_closes(straight_root))
    assert _closes(straight_root), "the straight run must have trailed out"

    straight_account = accounting.read_account(straight_root)
    resumed_account = accounting.read_account(resumed_root)
    for key in ("orders", "fills", "entry_fills", "exit_fills", "open_positions", "closed_trades"):
        assert resumed_account["counts"][key] == straight_account["counts"][key], key
    assert (
        resumed_account["values"]["realized_gross"] == straight_account["values"]["realized_gross"]
    )


def _shape(events) -> list[tuple]:
    return [
        (event.get("reason"), event.get("exit_price"), event.get("simulated_net_quote"), event.get("trail"))
        for event in events
    ]


def test_a_resumed_position_keeps_the_bar_its_trade_opened_on(tmp_path):
    _state(tmp_path, best=1.10120, trail=1.09880)
    clock = Clock(START + timedelta(seconds=SECONDS * WARMUP))
    quotes = ReplayQuotes(_flat() + [1.1010], clock=clock, cursor=WARMUP)
    test = _test(tmp_path, quotes, clock, candidate=TRAILING)
    assert test._restore_state()["ok"] is True
    before = test.snapshot()["positions"][0]["entry_bar_at"]
    assert before

    resumed = _test(tmp_path, quotes, clock, candidate=TRAILING)
    assert resumed._restore_state()["ok"] is True
    assert resumed.snapshot()["positions"][0]["entry_bar_at"] == before


def test_a_schema_two_record_derives_the_entry_bar_from_its_last_managed_bar(tmp_path):
    """Records written by the previous build resume, and say what was derived."""

    _state(
        tmp_path,
        best=1.10120,
        trail=1.09880,
        version=2,
        drop=("state_version", "entry_bar_at", "entry_bar_index"),
    )
    clock = Clock(START + timedelta(seconds=SECONDS * WARMUP))
    quotes = ReplayQuotes(_flat() + [1.1010, 1.1020, 1.0940, 1.0940], clock=clock, cursor=WARMUP)
    test = _test(tmp_path, quotes, clock, candidate=TRAILING)

    report = test._restore_state()
    assert report["ok"] is True
    assert report["version"] == 2
    assert report["migrated"] == 1
    position = test.snapshot()["positions"][0]
    # Derived from the last managed bar, which is never before the entry bar, so
    # it can delay a trail trigger by a bar and never advance one.
    assert position["entry_bar_at"] == "2026-11-04T14:15:00+00:00"
    assert "entry_bar_at" in position["migration"]
    events = [e for e in _ledger(tmp_path) if e.get("kind") == "state_migrated"]
    assert events and "entry_bar_at" in events[0]["derivation"]

    # And the migrated position still trails out on the same bar path.
    for _ in range(10):
        _advance(quotes, clock)
        test._cycle()
        if test.snapshot()["exit_fills"]:
            break
    assert test.snapshot()["exit_fills"], "a migrated trail must still be able to act"


def test_a_live_trail_with_no_knowable_entry_bar_is_refused(tmp_path):
    """No anchor means no arming rule: refuse rather than resume an inert trail."""

    _state(
        tmp_path,
        best=1.10120,
        trail=1.09880,
        version=1,
        drop=("state_version", "bar_at", "entry_bar_at", "entry_bar_index"),
    )
    clock = Clock(START + timedelta(seconds=SECONDS * WARMUP))
    quotes = ReplayQuotes(_flat(), clock=clock, cursor=WARMUP)
    test = _test(tmp_path, quotes, clock, candidate=TRAILING)

    report = test._restore_state()
    assert report["ok"] is False
    assert "trailing stop" in report["failed"][0]["reason"]
    assert test.snapshot()["positions"] == []
    start = test.start()
    assert start["started"] is False
    assert start["status"] == "BLOCKED"


# ---------------------------------------------------------------------------
# 4. a stale quote cannot move a trail, let alone fill one
# ---------------------------------------------------------------------------


class StaleQuotes(ReplayQuotes):
    """The same quotes, aged past the allowance, with the age under test control."""

    def __init__(self, *args, age_seconds: float = 600.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.age = age_seconds

    def tick(self, symbol):
        quote = super().tick(symbol)
        quote.time = self.clock.now - timedelta(seconds=self.age)
        return quote


def test_a_stale_quote_neither_triggers_nor_fabricates_a_trailing_exit(tmp_path):
    _state(tmp_path, direction="LONG", best=1.10120, trail=1.09880)
    # A path that *would* cross the trail on its first bar.
    closes = _flat() + [1.0940] * 6

    clock = Clock(START + timedelta(seconds=SECONDS * WARMUP))
    quotes = StaleQuotes(closes, clock=clock, cursor=WARMUP, age_seconds=600.0)
    test = _test(tmp_path, quotes, clock, candidate=TRAILING)
    assert test._restore_state()["ok"] is True

    for _ in range(3):
        _advance(quotes, clock)
        test._cycle()

    snapshot = test.snapshot()
    assert snapshot["positions"], "a stale quote must not close anything"
    assert snapshot["exit_fills"] == []
    assert snapshot["stats"]["stale_quotes_skipped"] >= 3
    assert snapshot["positions"][0]["trail"] == 1.0988, "the trail must not move on a stale quote"
    kinds = [event.get("kind") for event in _ledger(tmp_path)]
    assert kinds.count("stale_quote") == 3
    assert "simulated_close" not in kinds

    # The page agrees: paused, and it says why.
    app = _account_app(tmp_path, marks={"EURUSD": _mark("EURUSD", 1.0940, age_seconds=600)})
    account = app.paper_account()
    assert account["trading"]["paused"] is True
    assert "PAUSED" in _text(render(app, "/positions"))

    # And with a fresh quote the very same bar gap does close it, so the stale
    # reading above was a pause and not a change in the rules.
    quotes.age = 1.0
    _advance(quotes, clock)
    test._cycle()
    assert test.snapshot()["exit_fills"], "fresh quotes must let the trail act"
    assert test.snapshot()["exit_fills"][0]["reason"] == EXIT_TRAIL


# ---------------------------------------------------------------------------
# 5. the exit reaches the ledger, and the pages agree with it
# ---------------------------------------------------------------------------


def test_a_trailing_exit_updates_the_ledger_and_the_dashboard_counts(tmp_path):
    test, quotes, clock, at_entry = _run_to_entry(tmp_path, _decline_then_rise())
    for _ in range(10):
        _advance(quotes, clock)
        test._cycle()
        if test.snapshot()["exit_fills"]:
            break

    ledger = _ledger(tmp_path)
    closes_written = [e for e in ledger if e.get("kind") == "simulated_close"]
    notes = [e for e in ledger if e.get("kind") == "close_recorded"]
    assert len(closes_written) == 1, "one close, written once"
    assert len(notes) == 1, "and a separate note, not a second close"
    event = closes_written[0]
    # The ledger carries what the exit was decided on.
    assert event["reason"] == EXIT_TRAIL
    assert event["trail"] is not None
    assert event["entry_bar_at"]
    # The price P&L is the level move on the contract, in the quote currency.
    expected = (float(event["exit_price"]) - float(event["entry_price"])) * 100000.0 * 0.1
    assert abs(float(event["simulated_net_quote"]) - expected) < 1e-4

    account = accounting.read_account(tmp_path)
    assert account["counts"]["closed_trades"] == 1
    assert account["counts"]["exit_fills"] == 1
    assert account["counts"]["entry_fills"] == 1
    assert account["counts"]["orders"] == account["counts"]["fills"] == 2
    assert account["counts"]["open_positions"] == 0
    assert account["integrity"]["ok"] is True
    assert test.snapshot()["positions"] == []

    # The workstation shows the same numbers: the ledger is the only source.
    app = _account_app(tmp_path, marks={"EURUSD": _mark("EURUSD", 1.0960)})
    page_account = app.paper_account()
    assert page_account["counts"] == account["counts"]
    positions_text = _text(render(app, "/positions"))
    assert "Nothing open" in positions_text
    assert "1 trade(s)" in positions_text
    performance_text = _text(render(app, "/performance"))
    assert "1 trade(s)" in performance_text
    # The closed trade is presented with its reason and its level basis.
    assert "trailing stop" in performance_text.lower()


def test_the_open_book_shows_the_trail_and_the_bar_that_arms_it(tmp_path):
    """A level that can now close the trade must not be hidden from the page."""

    test, _quotes, _clock, at_entry = _run_to_entry(tmp_path, _decline_then_rise())
    position = at_entry["positions"][0]
    assert position["trail"] is not None

    mark = float(position["entry_price"])
    app = _account_app(tmp_path, marks={position["symbol"]: _mark(position["symbol"], mark)})
    text = _text(render(app, "/positions"))
    assert "Nothing open" not in text
    assert "Trailing stop" in text
    assert "armed after" in text
    # The column is the runner's own level, not a page-side recomputation.
    assert f"{float(position['trail']):.5f}" in text


def test_the_runner_is_still_unable_to_reach_an_order_surface():
    source = Path("axiom_forex/paper_runner/catalog_paper.py").read_text(encoding="utf-8")
    for forbidden in ("order_send", "order_check", "positions_get", "MetaTrader5", "live_arm"):
        assert forbidden not in source, forbidden
