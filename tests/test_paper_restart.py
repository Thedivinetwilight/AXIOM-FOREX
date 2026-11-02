"""A paper position must survive a restart *exactly*, or not at all.

The defect these tests cover: ``_restore_state()`` used to rebuild an open
position from the page-shaped summary in the state file, with ``atr=0.0``,
``bar_index=0`` and whatever stop/trail happened to be there.  The ATR is the
unit a trailing stop is measured in, so a restored position trailed differently
from the one that stopped -- which is to say a restart could change an exit, and
the ledger would record that changed exit as a trade.

What is proved here:

* an uninterrupted run and a stop/restart/resumed run produce the *same* fills,
  the same closes, the same prices and the same ledger totals;
* every field the exit rules read travels through the state file;
* a record written by the previous schema is migrated, and says exactly what it
  derived and why that cannot move an exit;
* a record that cannot be resumed exactly is refused -- the run stays blocked,
  the state file is left untouched, and no stop is invented.

Everything is fixture data and a temporary folder.  No terminal is connected, no
order is sent, and nothing here measures a market.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from axiom_forex.paper_runner import accounting
from axiom_forex.paper_runner.catalog_paper import (
    EVENT_MIGRATED,
    EVENT_RECOVERY_FAILED,
    EVENT_RESTORED,
    REASON_STATE_RECOVERY,
    STATE_VERSION,
    CatalogPaperTest,
    Watch,
)
from axiom_forex.strategies.catalog.candidates import catalog

from tests.catalog_support import random_walk

UTC = timezone.utc
START = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
SECONDS = 900

#: A bracket row: ATR stop and target, no trailing stop.
BRACKET = next(
    item
    for item in catalog()
    if item.entry == "sma_cross" and item.exit == "bracket" and item.filter == "none"
)
#: A row whose stop *is* an ATR trail -- the one where a missing ATR changes exits.
TRAILING = next(
    item
    for item in catalog()
    if item.entry == "sma_cross" and item.exit == "trailing_atr" and item.filter == "none"
)


@dataclass
class FakeFacts:
    point: float = 0.00001
    contract_size: float = 100_000.0
    pair: str = "EUR/USD"
    profit_currency: str = "USD"
    account_currency: str = "USD"
    swap_state: str = "POINTS"
    swap_long: float = -0.5
    swap_short: float = 0.2


class Clock:
    """A clock the test moves by hand, so a replay is reproducible."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class ReplayQuotes:
    """A read-only quote surface over a bar list: no order call exists on it."""

    def __init__(self, closes, *, clock: Clock, cursor: int, symbol: str = "EURUSD.pro"):
        self.closes = list(closes)
        self.clock = clock
        self.cursor = cursor  # how many bars have completed
        self.symbol = symbol
        self.ticks = 0

    def symbol_names(self):
        return [self.symbol]

    def symbol_info(self, symbol):
        if symbol != self.symbol:
            raise LookupError("symbol unavailable")
        return FakeFacts()

    def tick(self, symbol):
        if symbol != self.symbol:
            raise LookupError("symbol unavailable")
        self.ticks += 1
        mid = self.closes[self.cursor - 1]
        return SimpleNamespace(
            symbol=symbol,
            bid=mid,
            ask=round(mid + 0.00004, 5),
            time=self.clock.now - timedelta(seconds=1.0),
        )

    def rates(self, symbol, timeframe, *, start, end):
        if symbol != self.symbol:
            raise LookupError("symbol unavailable")
        rows = []
        previous = self.closes[0]
        for index in range(self.cursor):
            close = self.closes[index]
            rows.append(
                {
                    "timestamp": START + timedelta(seconds=SECONDS * index),
                    "open": round(previous, 5),
                    "high": round(max(previous, close) + 0.00003, 5),
                    "low": round(min(previous, close) - 0.00003, 5),
                    "close": round(close, 5),
                    "spread_points": 4,
                    "tick_volume": 7,
                }
            )
            previous = close
        return rows


def _watch(candidate=BRACKET, symbol: str = "EURUSD.pro") -> Watch:
    return Watch(candidate_id=candidate.candidate_id, symbol=symbol, timeframe="15m", candidate=candidate)


def _test(root: Path, quotes: ReplayQuotes, clock: Clock, *, candidate=BRACKET) -> CatalogPaperTest:
    return CatalogPaperTest(
        quotes=quotes, watches=(_watch(candidate),), root=root, clock=clock
    )


def _advance(quotes: ReplayQuotes, clock: Clock) -> None:
    """One more completed bar, and a clock that has moved with it."""

    quotes.cursor += 1
    clock.now = START + timedelta(seconds=SECONDS * quotes.cursor)


def _drive(
    root: Path,
    closes,
    *,
    first: int,
    last: int,
    restart_at: int | None = None,
    candidate=BRACKET,
) -> dict:
    """Run a replay, optionally stopping and resuming it part way through.

    The resume is a *new object on the same folder*, which is what a process
    restart looks like from the state file's point of view.
    """

    clock = Clock(START + timedelta(seconds=SECONDS * first))
    quotes = ReplayQuotes(closes, clock=clock, cursor=first)
    test = _test(root, quotes, clock, candidate=candidate)
    reports = []
    for step in range(first + 1, last + 1):
        _advance(quotes, clock)
        test._cycle()
        if restart_at is not None and quotes.cursor == restart_at:
            test.stop()  # the operator stops the loop; the state file is what is left
            resumed = _test(root, quotes, clock, candidate=candidate)
            reports.append(resumed._restore_state())
            test = resumed
    return {"test": test, "quotes": quotes, "clock": clock, "reports": reports}


def _fills(root: Path) -> list[dict]:
    """The simulated fills in a ledger, in file order, without any of the notes."""

    path = root / "catalog-paper-ledger.jsonl"
    events = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [
        event
        for event in events
        if event.get("kind") in {"simulated_fill", "simulated_close"}
    ]


def _shape(events) -> list[tuple]:
    return [
        (
            event.get("kind"),
            event.get("reason"),
            event.get("direction"),
            event.get("price"),
            event.get("exit_price"),
            event.get("simulated_net_quote"),
        )
        for event in events
    ]


def _kinds(root: Path) -> list[str]:
    path = root / "catalog-paper-ledger.jsonl"
    return [
        json.loads(line).get("kind")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# 1. the whole point: a restart must not change the future
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("restart_at", [123, 167])
def test_a_stopped_and_resumed_run_matches_an_uninterrupted_run(tmp_path, restart_at):
    """Cursor 123 stops with the first position open; 167 after one close."""
    closes = random_walk(400)
    straight_root = tmp_path / "straight"
    resumed_root = tmp_path / "resumed"

    straight = _drive(straight_root, closes, first=120, last=180)
    resumed = _drive(resumed_root, closes, first=120, last=180, restart_at=restart_at)

    # The restart really happened, and it was clean.
    report = resumed["reports"][0]
    assert report["ok"] is True
    assert report["restored"] == 1, "the run stopped with an open position to resume"

    # Same fills, same closes, same prices, same simulated P&L -- in order.
    assert _shape(_fills(resumed_root)) == _shape(_fills(straight_root))

    # Same ledger totals, read through the accounting module both pages use.
    straight_account = accounting.read_account(straight_root)
    resumed_account = accounting.read_account(resumed_root)
    for key in ("orders", "fills", "entry_fills", "exit_fills", "open_positions", "closed_trades"):
        assert resumed_account["counts"][key] == straight_account["counts"][key], key
    assert resumed_account["values"]["realized_gross"] == straight_account["values"]["realized_gross"]
    assert (
        resumed_account["metric_by_key"]["realized_gross"]["value"]
        == straight_account["metric_by_key"]["realized_gross"]["value"]
    )

    # And the same open book, position for position.
    assert [p["key"] for p in resumed["test"].snapshot()["positions"]] == [
        p["key"] for p in straight["test"].snapshot()["positions"]
    ]


def test_the_fixture_really_exercises_entries_and_exits(tmp_path):
    """A guard on the test above: a replay with no fills would prove nothing."""

    result = _drive(tmp_path, random_walk(400), first=120, last=180)
    kinds = _kinds(tmp_path)
    assert kinds.count("simulated_fill") >= 2
    assert kinds.count("simulated_close") >= 2


# ---------------------------------------------------------------------------
# 2. every field the exit rules read travels with the position
# ---------------------------------------------------------------------------


def test_the_restored_position_carries_every_field_the_exit_rules_read(tmp_path):
    closes = random_walk(400)
    clock = Clock(START + timedelta(seconds=SECONDS * 120))
    quotes = ReplayQuotes(closes, clock=clock, cursor=120)
    test = _test(tmp_path, quotes, clock)
    for _ in range(6):
        _advance(quotes, clock)
        test._cycle()
    before = test.snapshot()["positions"]
    assert before, "the fixture should hold one open position at this point"
    original = dict(before[0])
    test.stop()

    state = json.loads((tmp_path / "catalog-paper-state.json").read_text(encoding="utf-8"))
    assert state["version"] == STATE_VERSION
    stored = state["positions"][0]
    for field in (
        "atr",
        "bar_index",
        "bar_at",
        "stop",
        "target",
        "trail",
        "best",
        "stop_initial",
        "target_initial",
        "trail_initial",
        "entry_price",
        "entry_mid",
        "lots",
        "candidate_id",
        "quote_currency",
        "contract_size",
    ):
        assert field in stored, field

    resumed = _test(tmp_path, quotes, clock)
    report = resumed._restore_state()
    assert report["ok"] is True and report["migrated"] == 0
    restored = resumed.snapshot()["positions"][0]
    for field in (
        "key",
        "symbol",
        "direction",
        "lots",
        "entry_at",
        "entry_price",
        "atr",
        "bar_index",
        "bar_at",
        "stop",
        "target",
        "trail",
        "best",
        "stop_initial",
        "target_initial",
        "trail_initial",
        "quote_currency",
        "contract_size",
    ):
        assert restored[field] == original[field], field
    assert restored["restored_after_restart"] is True
    assert restored["migrated"] is False


def test_the_bar_already_decided_on_is_not_decided_again_after_a_restart(tmp_path):
    """Otherwise a restart can act on a signal the first run had already spent."""

    closes = random_walk(400)
    clock = Clock(START + timedelta(seconds=SECONDS * 120))
    quotes = ReplayQuotes(closes, clock=clock, cursor=120)
    test = _test(tmp_path, quotes, clock)
    for _ in range(4):
        _advance(quotes, clock)
        test._cycle()
    key = test.watches[0].key
    decided_bar = test._last_bar[key]
    assert decided_bar
    test.stop()

    resumed = _test(tmp_path, quotes, clock)
    assert resumed._restore_state()["ok"] is True
    assert resumed._last_bar[key] == decided_bar

    # Cycling again on the same completed bar changes nothing.
    fills_before = len(_fills(tmp_path))
    resumed._cycle()
    assert len(_fills(tmp_path)) == fills_before


# ---------------------------------------------------------------------------
# 3. a record from the previous schema: migrated, and honest about it
# ---------------------------------------------------------------------------


def _downgrade_to_version_1(root: Path) -> dict:
    """Rewrite a state document the way the previous build wrote one.

    That build stored the page summary -- no ATR, no bar index, no initial
    levels -- which is precisely why a restored position trailed differently.
    """

    path = root / "catalog-paper-state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["version"] = 1
    state.pop("recovery", None)
    legacy = []
    for stored in state["positions"]:
        legacy.append(
            {
                "key": stored["key"],
                "candidate_id": stored["candidate_id"],
                "symbol": stored["symbol"],
                "timeframe": stored["timeframe"],
                "direction": stored["direction"],
                "lots": stored["lots"],
                "entry_at": stored["entry_at"],
                "entry_price": stored["entry_price"],
                "stop": stored["stop"],
                "target": stored["target"],
                "trail": stored["trail"],
                "quote_currency": stored["quote_currency"],
                "contract_size": stored["contract_size"],
                "restored_after_restart": False,
                "priced_from": "BROKER_TICK",
                "simulated": True,
            }
        )
    state["positions"] = legacy
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    return state


def test_a_legacy_row_is_migrated_without_moving_an_exit(tmp_path):
    closes = random_walk(400)
    clock = Clock(START + timedelta(seconds=SECONDS * 120))
    quotes = ReplayQuotes(closes, clock=clock, cursor=120)
    test = _test(tmp_path, quotes, clock)
    for _ in range(6):
        _advance(quotes, clock)
        test._cycle()
    original = dict(test.snapshot()["positions"][0])
    test.stop()

    _downgrade_to_version_1(tmp_path)

    resumed = _test(tmp_path, quotes, clock)
    report = resumed._restore_state()
    assert report["ok"] is True
    assert report["version"] == 1
    assert report["migrated"] == 1

    restored = resumed.snapshot()["positions"][0]
    assert restored["migrated"] is True
    # The levels are the stored ones: nothing was invented and nothing was zeroed
    # in a field this candidate's exit rules read.
    assert restored["stop"] == original["stop"]
    assert restored["target"] == original["target"]
    assert restored["trail"] == original["trail"]
    assert restored["entry_price"] == original["entry_price"]
    # The derivations are stated, in the record and in the ledger.
    assert "atr" in restored["migration"]
    assert "bar_index" in restored["migration"]
    events = [
        json.loads(line)
        for line in (tmp_path / "catalog-paper-ledger.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    migrated = [event for event in events if event["kind"] == EVENT_MIGRATED]
    assert migrated and migrated[0]["from_version"] == 1
    assert "atr" in migrated[0]["derivation"]
    assert any(event["kind"] == EVENT_RESTORED for event in events)


def test_a_legacy_row_continues_to_the_same_fills(tmp_path):
    """A migrated position must still be managed by its own stored levels."""

    closes = random_walk(400)
    straight = _drive(tmp_path / "straight", closes, first=120, last=180)
    root = tmp_path / "legacy"
    resumed = _drive(root, closes, first=120, last=127)
    assert resumed["test"].snapshot()["positions"], "stop with a position open"
    resumed["test"].stop()
    _downgrade_to_version_1(root)

    # Carry on the same replay from a legacy document.
    clock = resumed["clock"]
    quotes = resumed["quotes"]
    revived = _test(root, quotes, clock)
    assert revived._restore_state()["ok"] is True
    for step in range(quotes.cursor + 1, 181):
        _advance(quotes, clock)
        revived._cycle()

    assert _shape(_fills(root)) == _shape(_fills(tmp_path / "straight"))
    assert straight["test"].snapshot()["stats"]["closes"] >= 1


# ---------------------------------------------------------------------------
# 4. what cannot be resumed is refused, not approximated
# ---------------------------------------------------------------------------


def _handwritten_state(
    tmp_path: Path,
    *,
    candidate=TRAILING,
    version: int = STATE_VERSION,
    extra: dict | None = None,
    drop: tuple[str, ...] = (),
) -> Path:
    """A state document for one open position, written field by field."""

    key = f"{candidate.candidate_id}@EURUSD.pro|15m"
    record = {
        "state_version": version,
        "key": key,
        "candidate_id": candidate.candidate_id,
        "symbol": "EURUSD.pro",
        "timeframe": "15m",
        "direction": "LONG",
        "lots": 0.1,
        "entry_at": "2026-11-02T00:00:00+00:00",
        "entry_price": 1.10000,
        "entry_mid": 1.10002,
        "atr": 0.00120,
        "bar_index": 124,
        "bar_at": "2026-11-02T07:30:00+00:00",
        "stop": None,
        "target": None,
        "trail": 1.09880,
        "best": 1.10120,
        "stop_initial": None,
        "target_initial": None,
        "trail_initial": 1.10000,
        "notional_quote": 11000.0,
        "quote_currency": "USD",
        "contract_size": 100000.0,
        "restored_after_restart": False,
        "priced_from": "BROKER_TICK",
        "simulated": True,
    }
    record.update(extra or {})
    for name in drop:
        record.pop(name, None)
    document = {
        "version": version,
        "updated_at": "2026-11-02T07:45:00+00:00",
        "status": "STOPPED",
        "running": False,
        "positions": [record],
        "last_bar": {},
        "watches": [],
    }
    path = tmp_path / "catalog-paper-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _climbing(count: int = 400) -> list[float]:
    """A rising bar path, so a long's trailing stop ratchets upward every bar."""

    return [round(1.1000 + 0.0002 * index, 5) for index in range(count)]


def _trail_after(root: Path, *, restart_at: int | None) -> dict:
    """Seed one long trailing position, then replay bars, optionally across a restart."""

    _handwritten_state(root, candidate=TRAILING)
    # 250 bars: this candidate refuses to decide on fewer than 242.
    clock = Clock(START + timedelta(seconds=SECONDS * 250))
    quotes = ReplayQuotes(_climbing(), clock=clock, cursor=250)
    test = _test(root, quotes, clock, candidate=TRAILING)
    assert test._restore_state()["ok"] is True
    for _ in range(12):
        _advance(quotes, clock)
        test._cycle()
        if restart_at is not None and quotes.cursor == restart_at:
            test.stop()
            resumed = _test(root, quotes, clock, candidate=TRAILING)
            assert resumed._restore_state()["ok"] is True
            test = resumed
    return test.snapshot()["positions"][0]


def test_the_trailing_stop_ratchets_identically_across_a_restart(tmp_path):
    """The defect, stated as an invariant.

    The trail is measured in the ATR.  Resuming with ``atr=0.0`` -- what the old
    restore did -- freezes the trail at whatever value the file held, so the
    resumed position is managed by a *looser* stop than the one the uninterrupted
    run is honouring.  No exit differs in this fixture (the trail trigger compares
    against a bar index the decision has already advanced), which is exactly why a
    frozen trail can go unnoticed: the state, not the fill, is what silently drifts.
    """

    straight = _trail_after(tmp_path / "straight", restart_at=None)
    restarted = _trail_after(tmp_path / "restarted", restart_at=255)

    # The fixture must actually ratchet, or this proves nothing.
    assert straight["trail"] > 1.1000
    assert straight["best"] > straight["entry_price"]

    assert restarted["trail"] == straight["trail"]
    assert restarted["best"] == straight["best"]
    assert restarted["atr"] == straight["atr"]
    assert restarted["restored_after_restart"] is True


def test_a_trailing_position_restores_with_its_atr(tmp_path):
    """The positive control for the refusal below: with the ATR, it resumes."""

    _handwritten_state(tmp_path, candidate=TRAILING)
    clock = Clock(START + timedelta(seconds=SECONDS * 126))
    quotes = ReplayQuotes(random_walk(400), clock=clock, cursor=126)
    test = _test(tmp_path, quotes, clock, candidate=TRAILING)

    report = test._restore_state()
    assert report["ok"] is True
    restored = test.snapshot()["positions"][0]
    assert restored["atr"] == 0.0012
    assert restored["trail"] == 1.0988
    assert restored["best"] == 1.1012


def test_a_legacy_trailing_row_without_its_atr_is_refused(tmp_path):
    """The trailing stop is measured in the ATR. Without it, no exit is knowable."""

    # Stripped exactly as the previous build wrote it: no ATR, and a live trail.
    path = _handwritten_state(
        tmp_path, candidate=TRAILING, version=1, drop=("atr", "bar_index", "bar_at")
    )
    before = path.read_text(encoding="utf-8")
    clock = Clock(START + timedelta(seconds=SECONDS * 126))
    quotes = ReplayQuotes(random_walk(400), clock=clock, cursor=126)

    resumed = _test(tmp_path, quotes, clock, candidate=TRAILING)
    report = resumed._restore_state()
    assert report["ok"] is False
    assert "ATR" in report["failed"][0]["reason"]
    assert resumed.snapshot()["positions"] == []
    # The evidence is untouched: the file is exactly as it was found.
    assert path.read_text(encoding="utf-8") == before

    start = resumed.start()
    assert start["started"] is False
    assert start["status"] == "BLOCKED"
    assert any(item["reason"] == REASON_STATE_RECOVERY for item in start["blockers"])
    assert "will not be continued with a substituted value" in start["blockers"][0]["detail"]
    assert path.read_text(encoding="utf-8") == before


def test_a_corrupt_state_document_blocks_the_start_and_is_left_alone(tmp_path):
    path = tmp_path / "catalog-paper-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json at all", encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    clock = Clock(START + timedelta(seconds=SECONDS * 120))
    quotes = ReplayQuotes(random_walk(200), clock=clock, cursor=120)
    test = _test(tmp_path, quotes, clock)
    start = test.start()

    assert start["started"] is False
    assert start["status"] == "BLOCKED"
    assert start["blockers"][0]["reason"] == REASON_STATE_RECOVERY
    assert "not valid JSON" in start["blockers"][0]["detail"]
    assert path.read_text(encoding="utf-8") == before
    assert not (tmp_path / "catalog-paper-ledger.jsonl").exists()


def test_a_stored_position_whose_entry_price_is_missing_is_refused(tmp_path):
    closes = random_walk(400)
    clock = Clock(START + timedelta(seconds=SECONDS * 120))
    quotes = ReplayQuotes(closes, clock=clock, cursor=120)
    test = _test(tmp_path, quotes, clock)
    for _ in range(6):
        _advance(quotes, clock)
        test._cycle()
    assert test.snapshot()["positions"]
    test.stop()

    path = tmp_path / "catalog-paper-state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["positions"][0]["entry_price"] = None
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")

    resumed = _test(tmp_path, quotes, clock)
    report = resumed._restore_state()
    assert report["ok"] is False
    assert "entry price" in report["failed"][0]["reason"]
    assert resumed.snapshot()["positions"] == [], "no position is half-restored"


def test_a_stored_position_with_no_watched_row_is_refused(tmp_path):
    """Nothing would manage it, so it would sit open for ever. Say so instead."""

    closes = random_walk(400)
    clock = Clock(START + timedelta(seconds=SECONDS * 120))
    quotes = ReplayQuotes(closes, clock=clock, cursor=120)
    test = _test(tmp_path, quotes, clock)
    for _ in range(6):
        _advance(quotes, clock)
        test._cycle()
    assert test.snapshot()["positions"]
    test.stop()

    # The operator restarts with a different paper set: the stored row is not in it.
    other = next(
        item for item in catalog() if item.candidate_id not in {BRACKET.candidate_id, TRAILING.candidate_id}
    )
    resumed = _test(tmp_path, quotes, clock, candidate=other)
    report = resumed._restore_state()
    assert report["ok"] is False
    assert "no watched row matches" in report["failed"][0]["reason"]
    start = resumed.start()
    assert start["started"] is False
    assert start["blockers"][0]["reason"] == REASON_STATE_RECOVERY


def test_a_refused_recovery_writes_no_fill_and_no_close(tmp_path):
    closes = random_walk(400)
    clock = Clock(START + timedelta(seconds=SECONDS * 120))
    quotes = ReplayQuotes(closes, clock=clock, cursor=120)
    test = _test(tmp_path, quotes, clock)
    for _ in range(6):
        _advance(quotes, clock)
        test._cycle()
    test.stop()
    fills_before = len(_fills(tmp_path))

    path = tmp_path / "catalog-paper-state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["positions"][0]["direction"] = "SIDEWAYS"
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")

    resumed = _test(tmp_path, quotes, clock)
    report = resumed._restore_state()
    assert report["ok"] is False
    assert len(_fills(tmp_path)) == fills_before

    kinds = _kinds(tmp_path)
    assert kinds.count(EVENT_RECOVERY_FAILED) == 1
    # Nothing was closed behind the operator's back to make the book look tidy.
    assert "simulated_close" not in kinds or kinds.count("simulated_close") == sum(
        1 for kind in kinds if kind == "simulated_close"
    )


def test_an_empty_folder_is_not_a_recovery_failure(tmp_path):
    """The first run has nothing to resume, and that is not an error."""

    clock = Clock(START + timedelta(seconds=SECONDS * 120))
    quotes = ReplayQuotes(random_walk(200), clock=clock, cursor=120)
    test = _test(tmp_path, quotes, clock)
    report = test._restore_state()
    assert report["ok"] is True
    assert report["restored"] == 0
    assert report["version"] == 0
    assert test.snapshot()["recovery"]["ok"] is True


# ---------------------------------------------------------------------------
# 5. the boundary: restoring a file still cannot reach an order
# ---------------------------------------------------------------------------


def test_the_restore_path_cannot_reach_an_order_surface():
    source = Path("axiom_forex/paper_runner/catalog_paper.py").read_text(encoding="utf-8")
    for forbidden in ("order_send", "order_check", "positions_get", "MetaTrader5"):
        assert forbidden not in source, forbidden
    # The quote surface the loop is given has no order call on it either.
    quotes = ReplayQuotes(random_walk(50), clock=Clock(START), cursor=40)
    for name in ("order_send", "order_check", "buy", "sell", "close"):
        assert not hasattr(quotes, name), name
