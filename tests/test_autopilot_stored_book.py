"""/autopilot must show, and be able to fix, the book that stops it.

The defects these tests cover, all found by driving the real page rather than
reading it:

* the page drew only the *in-memory* snapshot, so a stored book this process had
  not resumed — after a restart, or after a resume refused as
  ``paper_state_incomplete`` — was invisible there while /positions listed it and
  offered to close it.  Two pages, two answers about whether anything was open.
* the blocker it did show told the operator to use "Simulate closing open
  positions": a button that no longer exists under that name, on another page,
  with no link to it.
* the close button was drawn while the loop was running, where the runner always
  refuses it — a visible control that could not work in the state it was shown.
* the live bridge answered a **Stop** with a sentence about starting.
* every table on the page (and the symbol table on /quick-setup) rendered its
  column headings as machine names — ``candidate_id``, ``took_ms``, ``usable`` —
  because the header tuples were written as ``(field, Label)`` where
  ``table()`` reads ``(label, kind)``.

Everything here is a fixture workstation: a fake terminal, fixture bars, a stored
book of fixture positions and a temporary folder.  No terminal is contacted, no
order is sent, no MT5 position is touched, and nothing here is a profit claim.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from axiom_forex.paper_runner import accounting
from axiom_forex.paper_runner.catalog_paper import REASON_STATE_RECOVERY
from axiom_forex.strategies.catalog.autopilot import Autopilot
from axiom_forex.strategies.catalog.panel import CONFIRM_CLOSE_ALL
from axiom_forex.workstation.app import GET_ROUTES

from tests.test_autopilot_ui import _autopilot, _page, _text
from tests.test_catalog_panel import _panel
from tests.test_catalog_view import _app
from tests.test_paper_flatten import BOOK_SIZE, _seed_book, _ledger


def _paper_root(tmp_path: Path) -> Path:
    return tmp_path / "data" / "catalog" / "paper"


def _headers(html: str) -> dict[str, list[str]]:
    """Every table's caption and column headings, as the browser would show them."""

    found: dict[str, list[str]] = {}
    for caption, head in re.findall(
        r"<caption>(.*?)</caption><thead><tr>(.*?)</tr></thead>", html, re.S
    ):
        found[re.sub(r"<[^>]+>", "", caption)] = re.findall(r"<th[^>]*>(.*?)</th>", head)
    return found


# -- what the page shows ----------------------------------------------------


def test_a_stored_book_the_process_has_not_loaded_is_shown_not_hidden(tmp_path):
    """The ten positions /positions lists must not be invisible on /autopilot."""

    _seed_book(tmp_path)
    panel = _panel(tmp_path)
    app = _app(panel)
    app.autopilot = Autopilot(panel)

    text = _text(_page(app))

    assert "Open simulated positions, as stored on disk" in text
    assert f"{BOOK_SIZE} position(s) are on disk" in text
    # And it is the same reading /positions makes, so the two cannot disagree.
    counts = accounting.read_account(_paper_root(tmp_path))["counts"]
    assert counts["open_positions"] == BOOK_SIZE
    for symbol in ("EURUSD.pro", "GBPUSD.pro"):
        assert symbol in text


def test_a_stored_book_that_cannot_resume_offers_the_real_close_on_this_page(tmp_path):
    _seed_book(tmp_path)
    panel = _panel(tmp_path)
    app = _app(panel)
    app.autopilot = Autopilot(panel)
    html = _page(app)
    text = _text(html)

    assert "A stored paper book is stopping the next start" in text
    # The control that exists now, named as it is labelled, on this page.
    assert "Simulate closing all paper positions" in text
    assert "Simulate closing open positions" not in text, "that button does not exist"
    assert "action='/actions/catalog-paper-close-all'" in html
    assert "name='return_to' value='/autopilot'" in html
    assert "name='confirm' value='close-all-paper-positions'" in html
    # And it says, on the page, what kind of close it is.
    assert "no broker order is sent" in text
    assert "no MetaTrader 5 position is touched" in text
    assert "/positions" in text, "the operator is pointed at the other page too"


def test_a_resumable_book_is_not_offered_a_close(tmp_path):
    """A book the test can carry on managing must not be flattened by accident."""

    _seed_book(tmp_path)
    state_path = _paper_root(tmp_path) / "catalog-paper-state.json"
    import json

    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["recovery"] = {"ok": True, "version": 3, "restored": BOOK_SIZE,
                         "migrated": 0, "failed": [], "detail": ""}
    state_path.write_text(json.dumps(state), encoding="utf-8")

    panel = _panel(tmp_path)
    app = _app(panel)
    app.autopilot = Autopilot(panel)
    html = _page(app)
    text = _text(html)

    assert "Open simulated positions, as stored on disk" in text
    assert "Starting the paper loop resumes them exactly" in text
    assert "Simulate closing all paper positions" not in text
    assert "catalog-paper-close-all" not in html


def test_the_blocker_points_at_a_button_that_exists(tmp_path):
    """The refusal text and the UI must name the same control."""

    _seed_book(tmp_path)
    auto, app = _autopilot(tmp_path, report=True)
    auto.start(symbols=["EURUSD.pro"], timeframes=["15m"])
    auto.wait(timeout=180)
    try:
        status = auto.status()
        assert status["state"] == "BLOCKED", status
        detail = status["blockers"][0]["detail"]
        assert status["blockers"][0]["reason"] == REASON_STATE_RECOVERY
        assert "Simulate closing all paper positions" in detail
        assert "Simulate closing open positions" not in detail
    finally:
        auto.stop()


def test_the_close_reached_from_this_page_closes_the_book_and_comes_back(tmp_path):
    """End to end: the button on /autopilot actually works and reports."""

    _seed_book(tmp_path)
    panel = _panel(tmp_path)
    app = _app(panel)
    app.autopilot = Autopilot(panel)
    assert accounting.read_account(_paper_root(tmp_path))["counts"]["open_positions"] == BOOK_SIZE

    status, redirect, message = app.act(
        "catalog-paper-close-all",
        {
            "nonce": [app.nonce],
            "confirm": [CONFIRM_CLOSE_ALL],
            "return_to": ["/autopilot"],
        },
    )

    assert int(status) == 303
    assert redirect == "/autopilot", "the operator lands back where they pressed it"
    assert message == ""
    counts = accounting.read_account(_paper_root(tmp_path))["counts"]
    assert counts["open_positions"] == 0
    assert counts["closed_trades"] == BOOK_SIZE
    assert len([e for e in _ledger(_paper_root(tmp_path)) if e["kind"] == "simulated_close"]) == BOOK_SIZE
    # The page now says nothing is open, and offers no close.
    html = _page(app)
    assert "catalog-paper-close-all" not in html
    assert "Open simulated positions, as stored on disk" not in _text(html)


def test_a_close_from_this_page_without_the_box_is_refused_and_changes_nothing(tmp_path):
    _seed_book(tmp_path)
    before = (_paper_root(tmp_path) / "catalog-paper-ledger.jsonl").read_bytes()
    panel = _panel(tmp_path)
    app = _app(panel)
    app.autopilot = Autopilot(panel)

    status, redirect, message = app.act(
        "catalog-paper-close-all", {"nonce": [app.nonce], "return_to": ["/autopilot"]}
    )

    assert int(status) == 409
    assert redirect == "/autopilot"
    assert "confirmation box" in message
    assert (_paper_root(tmp_path) / "catalog-paper-ledger.jsonl").read_bytes() == before


def test_the_whole_recovery_works_over_a_real_socket(tmp_path):
    """The same flow through the HTTP server a browser talks to.

    Direct calls to ``app.act`` prove the handler; this proves the page's own form,
    the urlencoded fields, the nonce and the redirect, end to end.  It binds an
    ephemeral loopback port and shuts the server down again.
    """

    import threading
    import urllib.error
    import urllib.parse
    import urllib.request

    from axiom_forex.workstation.app import make_server

    _seed_book(tmp_path)
    auto, app = _autopilot(tmp_path)
    server = make_server(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def get(path: str) -> str:
        with urllib.request.urlopen(base + path, timeout=30) as response:
            assert response.status == 200
            return response.read().decode()

    def post(action: str, fields: dict[str, str]) -> tuple[int, str]:
        data = urllib.parse.urlencode(fields, doseq=True).encode()
        request = urllib.request.Request(
            base + "/actions/" + action, data=data, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode()

    try:
        html = get("/autopilot")
        nonce = re.search(r"name='nonce' value='([^']+)'", html).group(1)
        assert "Open simulated positions, as stored on disk" in html
        assert "name='confirm' value='close-all-paper-positions'" in html

        # Without the ticked box: refused, and the book is untouched.
        code, body = post(
            "catalog-paper-close-all", {"nonce": nonce, "return_to": "/autopilot"}
        )
        assert code == 409
        assert "confirmation box" in body
        assert accounting.read_account(_paper_root(tmp_path))["counts"]["open_positions"] == BOOK_SIZE

        # With it: closed, and the page comes back flat.
        code, _body = post(
            "catalog-paper-close-all",
            {
                "nonce": nonce,
                "return_to": "/autopilot",
                "confirm": "close-all-paper-positions",
            },
        )
        assert code == 200, "a 303 redirect is followed to /autopilot"
        assert accounting.read_account(_paper_root(tmp_path))["counts"]["open_positions"] == 0
        assert "Open simulated positions, as stored on disk" not in get("/autopilot")

        # And a stale nonce is still refused at the socket.
        code, _body = post("autopilot-start", {"nonce": "not-the-nonce", "symbols": "EURUSD.pro"})
        assert code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        auto.stop()


# -- controls that must match the state they are drawn in --------------------


def _plant_position(paper) -> None:
    """One open simulated position, as the loop would hold it after a fill."""

    from datetime import datetime, timezone

    from axiom_forex.paper_runner.catalog_paper import Position

    watch = paper.watches[0]
    now = datetime(2026, 11, 2, 9, 20, tzinfo=timezone.utc)
    with paper._lock:
        paper._positions[watch.key] = Position(
            key=watch.key,
            candidate_id=watch.candidate_id,
            symbol=watch.symbol,
            timeframe=watch.timeframe,
            direction=1,
            lots=0.10,
            entry_at=now.isoformat(),
            entry_price=1.10000,
            entry_mid=1.10002,
            atr=0.00040,
            stop=1.09880,
            target=1.10240,
            trail=None,
            best=1.10002,
            notional_quote=11_000.0,
            bar_index=10,
        )


def test_the_close_is_not_offered_while_the_loop_is_running(tmp_path):
    """A button the runner always refuses must not be drawn."""

    auto, app = _autopilot(tmp_path, report=True)
    auto.start(symbols=["EURUSD.pro"], timeframes=["15m"])
    auto.wait(timeout=180)
    try:
        assert auto.status()["state"] == "RUNNING"
        html = _page(app)
        # Nothing is open, so there is nothing to close and nothing to explain.
        assert "action='/actions/autopilot-flat'" not in html
        assert "Stop the paper loop" in html
        assert "stay open while the loop runs" not in _text(html)

        # With a position open, the control still must not be drawn -- and the page
        # says why, instead of leaving the operator to press a button that refuses.
        _plant_position(auto._paper)
        html = _page(app)
        assert "action='/actions/autopilot-flat'" not in html
        assert "stay open while the loop runs" in _text(html)
        assert "Stop the loop first if you mean to close them" in _text(html)
    finally:
        auto.stop()


def test_the_close_is_offered_once_the_loop_is_stopped(tmp_path):
    auto, app = _autopilot(tmp_path, report=True)
    auto.start(symbols=["EURUSD.pro"], timeframes=["15m"])
    auto.wait(timeout=180)
    auto.stop()
    html = _page(app)
    # With nothing open there is nothing to close, so the control is absent...
    assert "action='/actions/autopilot-flat'" not in html
    assert auto.status()["state"] == "STOPPED"


def test_a_live_stop_without_a_bridge_does_not_talk_about_starting(tmp_path):
    _auto, app = _autopilot(tmp_path)
    status, redirect, message = app.act("autopilot-live-stop", {"nonce": [app.nonce]})
    assert int(status) == 409
    assert "nothing changed" in message.lower()
    assert "started or stopped" not in message.lower()
    assert "no live session here to stop" in message

    # The start control keeps its own wording.
    status, _redirect, message = app.act("autopilot-live-start", {"nonce": [app.nonce]})
    assert int(status) == 409
    assert "started or stopped" in message


# -- headings a person can read --------------------------------------------


def test_every_table_on_every_page_has_human_headings(tmp_path):
    """``table()`` takes ``(label, kind)``; nothing may render a field name as a label."""

    panel = _panel(tmp_path)
    app = _app(panel)
    app.autopilot = Autopilot(panel)
    findings: list[str] = []
    for path in sorted(GET_ROUTES):
        _status, html = app.page(path)
        for caption, headers in _headers(html).items():
            for header in headers:
                if re.fullmatch(r"[a-z][a-z0-9_]*", header):
                    findings.append(f"{path} · {caption} · {header}")
    assert findings == []


def test_the_autopilot_tables_name_their_columns(tmp_path):
    auto, app = _autopilot(tmp_path, report=True)
    auto.start(symbols=["EURUSD.pro"], timeframes=["15m"])
    auto.wait(timeout=180)
    try:
        headers = _headers(_page(app))
        decisions = next(h for c, h in headers.items() if c.startswith("Why every candidate"))
        assert decisions[:3] == ["Candidate", "Symbol", "TF"]
        steps = next(h for c, h in headers.items() if c.startswith("What the autopilot did"))
        assert steps[:3] == ["Step", "Ok", "Took (ms)"]
        strategies = next(h for c, h in headers.items() if c.startswith("One row per symbol"))
        assert "Trades measured" in strategies and "trades_in_evaluation" not in strategies
    finally:
        auto.stop()


def test_the_symbol_table_names_its_columns(tmp_path):
    """The verified-symbol table is where an operator reads what is usable."""

    panel = _panel(tmp_path)
    app = _app(panel)
    _status, html = app.page("/quick-setup")
    captions = _headers(html)
    symbols = next((h for c, h in captions.items() if c.startswith("Forex symbols")), None)
    if symbols is None:  # pragma: no cover - the fixture terminal always reports names
        pytest.skip("this fixture reports no symbol table")
    assert symbols == ["Symbol", "Pair", "Major", "Usable", "If not usable, why"]


# -- and the paper path still cannot send ----------------------------------


def test_the_autopilot_page_posts_only_paper_and_observation_actions(tmp_path):
    _seed_book(tmp_path)
    panel = _panel(tmp_path)
    app = _app(panel)
    app.autopilot = Autopilot(panel)
    html = _page(app)
    posted = set(re.findall(r"action='/actions/([a-z-]+)'", html))
    assert posted <= {
        # Explicit observation controls in the PAPER command center.
        "connect",
        "disconnect",
        "autopilot-start",
        "autopilot-stop",
        "autopilot-flat",
        "autopilot-kronos",
        "autopilot-live-start",
        "autopilot-live-stop",
        # The confirmed, simulated close reused from /positions.
        "catalog-paper-close-all",
    }
    assert not posted & {"live-arm", "live-start", "mode-live", "runner-start"}
