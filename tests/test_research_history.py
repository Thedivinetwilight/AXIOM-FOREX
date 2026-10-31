"""Terminal history: read it, label it, store it, and never invent it.

Pinned here:

* history is read through the terminal's read-only ``rates()`` -- nothing is
  downloaded, generated, or substituted;
* a refusal from the terminal, an empty window, or an oversized request is
  reported as a named status, not as an empty success;
* a stored file records where it came from, and a sample can only be loaded when
  the caller asks for a sample by name;
* the round trip through JSON preserves the bars, the spread, and the origin, and
  a file with the wrong schema is ignored rather than guessed at;
* no stored payload carries a login, a password, or a server host.

All bars in this file are synthetic fixtures.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from axiom_forex.research.history import (
    MAX_ROWS,
    ORIGIN_SAMPLE,
    ORIGIN_TERMINAL,
    HistoryStore,
    fetch_series,
)
from axiom_forex.research.series import BID_PLUS_SPREAD

NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)


class FakeTerminal:
    """A fake reader with the terminal's read-only rates method."""

    def __init__(self, count: int = 9000, step: int = 3600, fail: bool = False) -> None:
        self.count = count
        self.step = step
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def rates(self, symbol: str, timeframe: str, *, start, end):
        self.calls.append((symbol, timeframe))
        if self.fail:
            raise RuntimeError("terminal said no")
        rows = []
        stamp = end - timedelta(seconds=self.step * self.count)
        price = 1.10000
        for index in range(self.count):
            open_ = price
            close = round(price + (0.00004 if index % 2 else -0.00004), 5)
            rows.append(
                {
                    "timestamp": stamp + timedelta(seconds=self.step * index),
                    "open": Decimal(str(open_)),
                    "high": Decimal(str(round(max(open_, close) + 0.00002, 5))),
                    "low": Decimal(str(round(min(open_, close) - 0.00002, 5))),
                    "close": Decimal(str(close)),
                    "spread_points": 12,
                    "tick_volume": 4,
                    "source": "mt5_copy_rates_range",
                }
            )
            price = close
        return rows


def fetch(reader=None, **overrides):
    values = dict(
        symbol="EURUSD.pro",
        timeframe="1h",
        pair="EUR/USD",
        point=0.00001,
        now=NOW,
    )
    values.update(overrides)
    return fetch_series(reader or FakeTerminal(), **values)


def test_history_is_read_from_the_terminal_and_labelled_two_sided():
    reader = FakeTerminal(count=8760)
    result = fetch(reader)
    assert reader.calls == [("EURUSD.pro", "1h")]
    assert result.status == "OK"
    assert result.origin == ORIGIN_TERMINAL
    assert result.bars == 8760
    assert result.series.price_basis == BID_PLUS_SPREAD
    assert result.coverage is not None and result.coverage.usable
    row = result.summary_row()
    assert row["usable"] is True
    assert row["bars"] == 8760
    assert row["origin"] == ORIGIN_TERMINAL


def test_a_terminal_refusal_is_reported_without_raw_error_text():
    result = fetch(FakeTerminal(fail=True))
    assert result.status == "ERROR"
    assert result.series is None
    assert "RuntimeError" in " ".join(result.notes)
    assert "terminal said no" not in " ".join(result.notes)
    assert result.summary_row()["usable"] is False


def test_an_empty_window_is_reported_as_empty_not_as_success():
    result = fetch(FakeTerminal(count=0))
    assert result.status == "EMPTY"
    assert result.reason == "no_bars_for_this_window"
    assert result.series is None


def test_an_unknown_timeframe_is_refused_before_the_terminal_is_asked():
    reader = FakeTerminal()
    result = fetch(reader, timeframe="3m")
    assert result.status == "REFUSED"
    assert result.reason == "unknown_timeframe"
    assert reader.calls == []


def test_an_oversized_request_is_refused_rather_than_truncated():
    result = fetch(FakeTerminal(count=MAX_ROWS + 10))
    assert result.status == "REFUSED"
    assert result.reason == "too_many_rows"
    assert result.series is None


def test_stored_history_round_trips_with_its_origin(tmp_path):
    result = fetch()
    store = HistoryStore(tmp_path / "history")
    saved = store.save(result)
    assert saved["bars"] == result.bars
    assert saved["usable_for_research"] is True

    loaded = store.load("EURUSD.pro", "1h")
    assert loaded is not None
    assert loaded.origin == ORIGIN_TERMINAL
    assert loaded.usable_for_research is True
    assert len(loaded.series) == len(result.series)
    assert loaded.series.point == pytest.approx(result.series.point)
    assert loaded.series.c[0] == pytest.approx(result.series.c[0])
    assert loaded.series.spread[0] == pytest.approx(result.series.spread[0])
    assert loaded.window_days == 365


def test_a_sample_cannot_be_loaded_unless_it_is_asked_for_by_name(tmp_path):
    from dataclasses import replace

    result = replace(fetch(), origin=ORIGIN_SAMPLE)
    store = HistoryStore(tmp_path / "history")
    saved = store.save(result)
    assert saved["usable_for_research"] is False
    assert store.load("EURUSD.pro", "1h") is None
    allowed = store.load("EURUSD.pro", "1h", allow_sample=True)
    assert allowed is not None and allowed.origin == ORIGIN_SAMPLE
    assert allowed.usable_for_research is False


def test_an_unlabelled_or_older_file_is_ignored(tmp_path):
    store = HistoryStore(tmp_path / "history")
    store.save(fetch())
    path = store.path_for("EURUSD.pro", "1h")
    payload = json.loads(path.read_text())
    payload["schema"] = "axiom-history-v0"
    path.write_text(json.dumps(payload))
    assert store.load("EURUSD.pro", "1h") is None
    # The inventory still reports the file honestly, schema version or not.
    assert store.inventory()[0]["bars"] == 9000


def test_a_stored_file_carries_no_credential_fields(tmp_path):
    store = HistoryStore(tmp_path / "history")
    store.save(fetch())
    text = store.path_for("EURUSD.pro", "1h").read_text().lower()
    for forbidden in ("password", "login", "token", "secret", "server="):
        assert forbidden not in text
    assert "origin" in text


def test_inventory_lists_what_exists_with_its_origin(tmp_path):
    store = HistoryStore(tmp_path / "history")
    store.save(fetch())
    store.save(fetch(symbol="GBPUSD.pro", pair="GBP/USD", timeframe="4h"))
    items = {item["symbol"]: item for item in store.inventory()}
    assert set(items) == {"EURUSD.pro", "GBPUSD.pro"}
    assert items["GBPUSD.pro"]["timeframe"] == "4h"
    assert items["EURUSD.pro"]["usable_for_research"] is True
    loaded = store.load_all()
    assert set(loaded) == {("EURUSD.pro", "1h"), ("GBPUSD.pro", "4h")}


def test_saving_something_that_was_never_fetched_is_refused(tmp_path):
    store = HistoryStore(tmp_path / "history")
    empty = fetch(FakeTerminal(count=0))
    with pytest.raises(ValueError):
        store.save(empty)
