"""The four monitoring pages: an honest glance, then the exact detail.

Every test renders from a temporary ledger or a fixture platform.  Nothing here
connects to MetaTrader 5, arms LIVE, sends an order, closes a broker position or
writes into the operator's own paper folder.
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

import pytest

from axiom_forex.facade import build_fixture_platform
from axiom_forex.workstation import views
from axiom_forex.workstation.app import GET_ROUTES, POST_ACTIONS, WorkstationApp
from axiom_forex.workstation.session import locked_session
from axiom_forex.workstation.setup import SetupWizard
from tests.test_paper_accounting import (
    _account_app,
    _entry,
    _exit,
    _full_ledger,
    _mark,
    _write,
)
from tests.test_quick_setup_ui import offline_environment

MONITORING = ("/performance", "/positions", "/activity", "/health")
#: The only forms these four pages are allowed to carry: setting the local
#: simulated capital, and the explicit simulated close of the whole paper book.
ALLOWED_FORMS = {"paper-capital", "catalog-paper-close-all"}


def _plain(html: str) -> str:
    body = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", unescape(body)).strip()


def _forms(html: str) -> set[str]:
    return set(re.findall(r"action='/actions/([a-z0-9-]+)'", html))


class _Page(HTMLParser):
    """Structural reader: forms, controls, tables, landmarks, ids."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict] = []
        self._form: dict | None = None
        self.controls: list[dict] = []
        self.buttons: list[dict] = []
        self.tables: list[dict] = []
        self._table: dict | None = None
        self._labels: list[dict] = []
        self.label_fors: set[str] = set()
        self._button: dict | None = None
        self.landmarks: list[str] = []
        self.headings: list[str] = []
        self.ids: list[str] = []
        self.aria_current = 0
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {key: value or "" for key, value in attrs}
        if a.get("id"):
            self.ids.append(a["id"])
        if a.get("aria-current") == "page":
            self.aria_current += 1
        if tag in {"main", "nav", "header", "footer", "aside"}:
            self.landmarks.append(tag)
        if re.fullmatch(r"h[1-6]", tag):
            self.headings.append(tag)
        if tag == "a" and a.get("href"):
            self.hrefs.append(a["href"])
        if tag == "form":
            self._form = {"action": a.get("action", ""), "method": a.get("method", ""), "fields": []}
            self.forms.append(self._form)
        elif tag in {"input", "select", "textarea"}:
            item = {
                "tag": tag,
                "name": a.get("name", ""),
                "type": a.get("type", ""),
                "id": a.get("id", ""),
                "aria_label": a.get("aria-label", ""),
                "labelled": bool(self._labels),
                "disabled": "disabled" in a or a.get("aria-disabled") == "true",
            }
            self.controls.append(item)
            if self._form is not None:
                self._form["fields"].append(item)
        elif tag == "label":
            self._labels.append(a)
            if a.get("for"):
                self.label_fors.add(a["for"])
        elif tag == "button":
            self._button = {"aria_label": a.get("aria-label", ""), "text": ""}
            self.buttons.append(self._button)
        elif tag == "table":
            self._table = {"caption": False, "scoped_head": False}
            self.tables.append(self._table)
        elif tag == "caption" and self._table is not None:
            self._table["caption"] = True
        elif tag == "th" and self._table is not None and a.get("scope") == "col":
            self._table["scoped_head"] = True

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
        elif tag == "label" and self._labels:
            self._labels.pop()
        elif tag == "button":
            self._button = None
        elif tag == "table":
            self._table = None

    def handle_data(self, data):
        if self._button is not None:
            self._button["text"] += data


def _audit(html: str) -> _Page:
    page = _Page()
    page.feed(html)
    page.close()
    return page


class _Platform:
    """The fixture platform, with named checks forced to fail."""

    def __init__(self, failing: tuple[str, ...] = ()) -> None:
        self.base = build_fixture_platform()
        self.failing = failing

    def status(self) -> dict:
        payload = self.base.status()
        health = payload.setdefault("health", {})
        if self.failing:
            health["status"] = "ATTENTION"
        for check in health.get("checks") or ():
            if str(check.get("name")) in self.failing:
                check["ok"] = False
                check["detail"] = "the check refused: a position did not reach a final state"
        return payload

    def __getattr__(self, name):
        return getattr(self.base, name)


def _health_app(tmp_path: Path, *, failing: tuple[str, ...] = ()) -> WorkstationApp:
    """A workstation whose paper platform reports the fixture checks."""

    wizard = SetupWizard(
        environment=offline_environment(), session=locked_session("no_settings_document")
    )
    app = WorkstationApp(wizard=wizard, platform=_Platform(failing))
    # Never read the repository's own paper folder from a test.
    app.project_root = str(tmp_path)
    return app


def _no_platform_app(tmp_path: Path) -> WorkstationApp:
    wizard = SetupWizard(
        environment=offline_environment(), session=locked_session("no_settings_document")
    )
    app = WorkstationApp(wizard=wizard)
    app.project_root = str(tmp_path)
    return app


def _stale_app(tmp_path: Path):
    """A full ledger whose only open position has no fresh quote to mark with."""

    _full_ledger(tmp_path)
    return _account_app(tmp_path)


def _marked_app(tmp_path: Path):
    _full_ledger(tmp_path)
    return _account_app(tmp_path, marks={"GBPUSD": _mark("GBPUSD", 1.2680)})


def _state_only_app(tmp_path: Path):
    """One open paper position that exists only in the state file."""

    events = [
        _entry("k1", "EURUSD", "LONG", 1.08000, "2026-11-02T13:00:00+00:00"),
        _exit(
            "k1",
            "EURUSD",
            "LONG",
            1.08000,
            1.08500,
            "2026-11-02T14:00:00+00:00",
            entry_at="2026-11-02T13:00:00+00:00",
            gross=50.0,
            basis="LEVEL",
        ),
    ]
    _write(
        tmp_path,
        events,
        positions=[
            {
                "key": "orphan",
                "symbol": "GBPUSD",
                "direction": "SHORT",
                "lots": 0.1,
                "entry_price": 1.27,
                "stop": 1.272,
                "target": 1.265,
            }
        ],
    )
    return _account_app(tmp_path, marks={"GBPUSD": _mark("GBPUSD", 1.2680)})


# ---------------------------------------------------------------------------
# one glance, four pages, and the detail underneath it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "heading", "scope"),
    (
        ("/performance", "Paper performance", "PAPER · LOCAL"),
        ("/positions", "Paper positions", "PAPER · LOCAL"),
        ("/activity", "Activity", "PAPER · LOCAL"),
        ("/health", "System health", "PAPER · READ-ONLY"),
    ),
)
def test_each_monitoring_page_opens_with_a_labelled_summary(tmp_path, path, heading, scope):
    app = _health_app(tmp_path) if path == "/health" else _marked_app(tmp_path)
    html = app.page(path)[1]
    plain = _plain(html)

    # The redesign opens each page with its own h1 and a lane chip, then a
    # summary ledger in the aside whose pairs are label/value, not tiles.
    assert f"<h1>{heading}</h1>" in html, path
    assert scope in plain, path
    assert "id='summary'" in html, path
    assert "class='facts'" in html, path
    assert "<table" in html, path
    # It names the lane it belongs to and points at the other one instead of
    # borrowing its numbers.
    assert "Data labels:" in plain
    assert "href='/live'" in html
    if path != "/health":
        assert "href='/autopilot'" in html


def test_the_summary_carries_the_same_words_as_the_detail_below_it(tmp_path):
    app = _marked_app(tmp_path)
    account = app.paper_account()
    metrics = account["metric_by_key"]
    plain = _plain(app.page("/performance")[1])
    summary = plain[plain.find("Summary") :]

    # The summary quotes the accounting module rather than recomputing a figure.
    assert metrics["equity"]["value"] in summary
    assert metrics["unrealized_gross"]["value"] in summary
    assert metrics["realized_gross"]["value"] in summary
    # And the counts match the ledger the rest of the page reads.
    assert "Open positions 1 · 1 priced" in summary
    assert "Closed trades 1" in summary
    # The detail table repeats the same accounting values, with their basis.
    assert metrics["equity"]["value"] in plain
    assert "Basis" in plain


def test_the_glance_states_its_source_and_the_instant_it_was_read(tmp_path):
    app = _marked_app(tmp_path)
    account = app.paper_account()
    plain = _plain(app.page("/positions")[1])

    assert "Read at" in plain
    assert account["read_at"][:10] in plain
    assert "Ledger updated" in plain
    assert "Latest ledger update" in plain
    assert "Every instant is UTC" in plain
    assert "Data source" in plain
    assert "durable paper ledger" in plain.lower()
    # The ages are computed, not left as a bare timestamp.
    assert "5.0 min old" in plain


# ---------------------------------------------------------------------------
# honest numbers: unavailable, stale, and zero-because-nothing-happened
# ---------------------------------------------------------------------------


def test_an_unmarkable_position_is_unavailable_and_never_zero(tmp_path):
    html = _stale_app(tmp_path).page("/positions")[1]
    plain = _plain(html)

    assert "Unrealised P&L (price)" in plain
    unrealised = plain[plain.find("Unrealised P&L (price)") :][:260]
    assert "Unavailable" in unrealised
    assert "0.00" not in unrealised
    assert "not a price to mark with" in plain
    # The summary counts the marks it refused and says why.
    assert "Unavailable marks 1" in plain
    assert "cannot be priced from a fresh quote" in plain
    assert "GBPUSD (50.0 min old)" in plain


def test_a_fresh_mark_produces_a_figure_and_no_stale_warning(tmp_path):
    plain = _plain(_marked_app(tmp_path).page("/positions")[1])

    assert "Unavailable marks 0" in plain
    assert "cannot be priced from a fresh quote" not in plain
    assert "19.00 USD" in plain


def test_equity_that_cannot_be_computed_keeps_the_accountings_reason(tmp_path):
    account = _account_app(tmp_path)  # empty ledger, no capital document
    plain = _plain(account.page("/performance")[1])

    assert "Paper equity Unavailable" in plain
    assert "no paper starting capital is configured, and none is guessed" in plain
    # The zero that is a count is still shown as a count, and says what it is.
    assert "nothing has traded yet, so this is zero rather than a result" in plain


def test_nothing_open_is_zero_rather_than_a_result(tmp_path):
    plain = _plain(_account_app(tmp_path).page("/positions")[1])

    assert "nothing is open, so this is zero rather than a result" in plain
    assert "nothing open" in plain


def test_a_state_only_position_is_counted_named_and_not_priced(tmp_path):
    html = _state_only_app(tmp_path).page("/positions")[1]
    plain = _plain(html)

    assert "State-only positions" in plain
    assert "1 unreconciled" in plain
    assert "not paired with a ledger entry fill" in plain
    assert "Positions restored from the state file without a ledger entry fill" in plain
    assert "no figure is invented for them" in plain


def test_a_paper_book_with_no_recovery_report_is_not_called_resumable(tmp_path):
    plain = _plain(_stale_app(tmp_path).page("/positions")[1])

    # No recovery report in the fixture state file: the page refuses to claim one.
    assert "Stored book no recovery report" in plain


# ---------------------------------------------------------------------------
# activity: truncation, attention events and UTC
# ---------------------------------------------------------------------------


def test_activity_counts_its_window_and_names_truncation(tmp_path):
    events = [
        {"at": f"2026-11-02T10:{index // 60:02d}:{index % 60:02d}+00:00", "kind": "signal", "symbol": "EURUSD"}
        for index in range(2050)
    ]
    _write(tmp_path, events)
    app = _account_app(tmp_path)

    html = app.page("/activity")[1]
    plain = _plain(html)

    assert "Events in the ledger 2,050" in plain
    assert "Events on this page 25" in plain
    assert "Older events not paged here 50 on disk" in plain
    # The window, the truncation and the fact that nothing was deleted are all said.
    assert "50 older ledger event(s) are on disk but not paged here" in plain
    assert "This page keeps the most recent 2,000 of 2,050 events" in plain
    assert "nothing was deleted" in plain


def test_activity_names_the_events_that_stopped_a_decision(tmp_path):
    events = [
        _entry("k1", "EURUSD", "LONG", 1.08000, "2026-11-02T13:00:00+00:00"),
        {
            "at": "2026-11-02T14:30:00+00:00",
            "kind": "stale_quote",
            "symbol": "EURUSD",
            "reason": "QUOTE_STALE",
            "age_seconds": 900,
            "allowance_seconds": 120,
        },
        {
            "at": "2026-11-02T14:40:00+00:00",
            "kind": "state_recovery_failed",
            "symbol": "EURUSD",
            "reason": "resume refused",
        },
    ]
    _write(tmp_path, events)
    app = _account_app(tmp_path)
    html = app.page("/activity")[1]
    plain = _plain(html)

    assert "Attention events in view 2 in view" in plain
    assert "2 attention event(s) in the events shown" in plain
    assert "Stale quote skipped" in plain
    assert "Resume refused" in plain
    assert "2026-11-02 14:30:00 UTC" in plain
    # Every timestamp column says which clock it is on.
    assert "When (UTC)" in plain


def test_activity_shows_the_platform_audit_trail_and_when_it_was_read(tmp_path):
    app = _health_app(tmp_path)
    plain = _plain(app.page("/activity")[1])

    assert "Platform audit events 2" in plain
    assert "Read from the paper platform at" in plain
    assert "the ledger above was read at" in plain
    assert "SYNTHETIC_FIXTURE_BAR_SOURCE" in plain
    assert "When (UTC)" in plain


# ---------------------------------------------------------------------------
# health: failing checks, unresolved orders, and "not reported"
# ---------------------------------------------------------------------------


def test_health_glance_reports_a_healthy_platform_from_its_own_checks(tmp_path):
    plain = _plain(_health_app(tmp_path).page("/health")[1])

    assert "Overall status HEALTHY 10 of 10 checks pass" in plain
    assert "Checks needing attention none" in plain
    assert "Unresolved orders none unresolved" in plain
    assert "LIVE bridge off" in plain
    assert "SAMPLE FIXTURE" in plain


def test_health_lists_every_failing_check_with_its_detail(tmp_path):
    app = _health_app(tmp_path, failing=("unresolved_orders",))
    html = app.page("/health")[1]
    plain = _plain(html)

    assert "ATTENTION" in plain
    assert "1 failing" in plain
    assert "No order is left unresolved" in plain
    assert "a position did not reach a final state" in plain
    assert "Unresolved orders attention" in plain
    assert "1 health check(s) need attention" in plain
    assert "Nothing on this page changes a check or clears one" in plain
    # The failing check is still listed in the full table with its own code.
    assert "unresolved_orders" in plain


def test_health_never_reports_a_check_it_could_not_read_as_passing(tmp_path):
    plain = _plain(_no_platform_app(tmp_path).page("/health")[1])

    assert "the platform reported no checks" in plain
    assert "Unresolved orders not reported" in plain
    assert "LIVE bridge not reported" in plain
    assert "checks pass" not in plain


# ---------------------------------------------------------------------------
# PAPER and LIVE stay separate, and no control is dead
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", MONITORING)
def test_no_monitoring_page_mixes_a_broker_figure_into_a_paper_one(tmp_path, path):
    app = _health_app(tmp_path) if path == "/health" else _marked_app(tmp_path)
    html = app.page(path)[1]
    plain = _plain(html)

    # The lane chips are PAPER; the LIVE lane is a link, not a figure.
    assert "PAPER · " in plain
    assert "LIVE · REAL BROKER" not in plain
    assert "broker-reported open position profit" not in plain.lower()
    # Every page that can show a figure also states that the broker account is not read.
    assert "Broker account Not read, not used, and never mixed into a paper figure." in plain
    # And the pages say where the other lane lives.
    assert "/live" in plain


def _page_forms(html: str) -> set[str]:
    """Forms drawn by the page body, excluding the shared topbar's own controls."""

    _head, _marker, content = html.partition("<main")
    return _forms(content)


@pytest.mark.parametrize("path", MONITORING)
def test_the_monitoring_pages_carry_no_dead_controls(tmp_path, path):
    app = _health_app(tmp_path) if path == "/health" else _marked_app(tmp_path)
    html = app.page(path)[1]
    page = _audit(html)

    assert "<script" not in html.lower()
    assert _page_forms(html) <= ALLOWED_FORMS, _page_forms(html)
    for form in page.forms:
        assert form["method"].lower() == "post"
        assert form["action"].startswith("/actions/")
        assert form["action"].rsplit("/", 1)[-1] in POST_ACTIONS
        assert any(field["name"] == "nonce" for field in form["fields"])


def test_the_monitoring_pages_only_ever_show_paper_writes(tmp_path):
    """The one write on these four pages is the simulated close of the paper book."""

    app = _state_only_app(tmp_path)
    html = app.page("/positions")[1]
    assert _page_forms(html) == {"catalog-paper-close-all"}
    assert "Simulated paper accounting only" in _plain(html)
    assert "no MetaTrader 5 position is touched" in _plain(html)


@pytest.mark.parametrize("path", MONITORING)
def test_every_monitoring_page_is_one_accessible_frame(tmp_path, path):
    app = _health_app(tmp_path) if path == "/health" else _marked_app(tmp_path)
    html = app.page(path)[1]
    page = _audit(html)

    assert page.headings.count("h1") == 1
    assert "main" in page.landmarks and "nav" in page.landmarks
    assert page.aria_current == 1
    assert len(page.ids) == len(set(page.ids)), "duplicate ids"
    assert page.tables and all(table["caption"] and table["scoped_head"] for table in page.tables)
    for control in page.controls:
        if control["type"] == "hidden" or control["disabled"]:
            continue
        assert (
            control["aria_label"]
            or control["labelled"]
            or (control["id"] and control["id"] in page.label_fors)
        ), control
    for button in page.buttons:
        assert button["aria_label"] or button["text"].strip()
    for href in page.hrefs:
        if href.startswith("/") and not href.startswith("//"):
            assert href.split("#", 1)[0].split("?", 1)[0] in GET_ROUTES, href


def test_the_monitoring_pages_use_the_shared_narrow_layout():
    assert ".summary-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr))" in views.STYLES
    assert ".table-wrap{overflow-x:auto}" in views.STYLES
    narrow = views.STYLES[views.STYLES.find("@media (max-width:560px)") :][:900]
    for selector in ("summary-grid", "grid--two", "split-callout"):
        assert selector in narrow, selector
    assert "@media (max-width:860px)" in views.STYLES
    assert "@media (max-width:1120px)" in views.STYLES


def test_the_monitoring_routes_render_in_every_state(tmp_path):
    for name in ("wired", "stale", "empty", "state-only", "healthy", "failing", "none"):
        (tmp_path / name).mkdir(exist_ok=True)
    states = {
        "wired": _marked_app(tmp_path / "wired"),
        "stale": _stale_app(tmp_path / "stale"),
        "empty": _account_app(tmp_path / "empty"),
        "state-only": _state_only_app(tmp_path / "state-only"),
        "healthy-platform": _health_app(tmp_path / "healthy"),
        "failing-platform": _health_app(tmp_path / "failing", failing=("data_quality",)),
    }
    states["no-platform"] = _no_platform_app(tmp_path / "none")
    assert set(MONITORING) <= set(GET_ROUTES)

    for state, app in states.items():
        for path in MONITORING:
            status, html = app.page(path)
            assert int(status) == 200, (state, path, status)
            assert html.count("<style>") == 1, (state, path)
            assert "Traceback" not in html, (state, path)


def test_the_monitoring_pages_agree_with_the_autopilot_command_center(tmp_path):
    """One ledger, one set of numbers: the pages may not disagree with each other."""

    app = _marked_app(tmp_path)
    account = app.paper_account()
    net = account["metric_by_key"]["net_pnl"]["value"]
    equity = account["metric_by_key"]["equity"]["value"]

    pages = {path: _plain(app.page(path)[1]) for path in ("/performance", "/positions", "/autopilot")}
    for path, plain in pages.items():
        assert net in plain, (path, net)
    assert equity in pages["/performance"]
    # The same unavailable net figure is named, not replaced by a different number.
    assert pages["/performance"].count("Net P&L after costs") >= 1
    assert "Unavailable" in pages["/performance"]


def test_the_live_page_never_borrows_a_paper_total(tmp_path):
    """With both a paper ledger and a wired LIVE adapter, the lanes stay apart."""

    from tests.test_workstation_live_overview import _RichLive

    app = _marked_app(tmp_path)
    app.live = _RichLive()
    account = app.paper_account()
    equity = account["metric_by_key"]["equity"]["value"]
    realized = account["metric_by_key"]["realized_gross"]["value"]

    live = _plain(app.page("/live")[1])
    paper = _plain(app.page("/performance")[1])

    assert equity in paper and realized in paper
    assert equity not in live
    assert realized not in live
    # The LIVE page states the broker's own figure and says where the paper one lives.
    assert "12.50 USD" in live
    assert "PAPER positions and P&L are on /autopilot" in live


def test_the_performance_page_keeps_its_capital_form_and_no_other(tmp_path):
    html = _account_app(tmp_path).page("/performance")[1]
    assert _page_forms(html) == {"paper-capital"}
    assert "Set the paper starting capital" in _plain(html)
