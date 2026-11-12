"""The /live REAL-money workflow: exact plan, gates, broker facts and decisions.

These tests use an in-memory stand-in for the live adapter and the typed-intent
bridge. They render only; no MetaTrader 5 connection, LIVE arming, broker order,
approval, admission, local user data or PAPER-to-LIVE promotion is involved.
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

from axiom_forex.paper_runner.live_workflow import ARM_CONFIRMATION_PHRASE
from axiom_forex.scalper.live_approval import APPROVAL_PHRASE
from axiom_forex.scalper.live_bridge import REVIEW_PHRASE
from axiom_forex.workstation import views
from axiom_forex.workstation.app import GET_ROUTES, POST_ACTIONS
from tests.test_workstation_ui import app_for, simulated_wizard


def _plain(html: str) -> str:
    body = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", unescape(body)).strip()


def _forms(html: str) -> set[str]:
    return set(re.findall(r"action='/actions/([a-z0-9-]+)'", html))


_FULL_SELECTED = {
    "intent_id": "intent-1",
    "status": "RECONCILED",
    "plan_fingerprint": "fingerprint-1",
    "reviewed": True,
    "approved": True,
    "armed": True,
    "broker": {
        "state": "FILLED",
        "request_id": "intent-1",
        "retcode_name": "TRADE_RETCODE_DONE",
        "volume_filled": "0.10",
    },
    "reconciliation": {
        "request_id": "intent-1",
        "result": {"reconciled": True},
        "order_record": {"state": "FILLED"},
    },
    "last_event": {"event_type": "AX_SCALPER_LIVE_RECONCILED"},
}

_FULL_PNL = {
    "available": True,
    "currency": "USD",
    "total": "12.50",
    "basis": "broker-reported open position profit only",
    "reason": "",
}

_POSITIONS = [{
    "symbol": "EURUSD.pro",
    "side": "BUY",
    "volume": "0.10",
    "price_open": "1.1000",
    "profit": "12.50",
}]


class _RichLive:
    """A wired /live view with exact, broker-reported facts. Sends nothing."""

    def __init__(
        self,
        *,
        selected: dict | None = None,
        refusal: dict | None = None,
        pnl: dict | None = None,
        positions: list[dict] | None = None,
        unresolved: list[str] | None = None,
        last_outcome: dict | None = None,
    ) -> None:
        self.selected = dict(selected or _FULL_SELECTED)
        self.refusal = dict(refusal or {})
        self.pnl = dict(pnl or _FULL_PNL)
        self.positions = list(positions if positions is not None else _POSITIONS)
        self.unresolved = list(unresolved if unresolved is not None else ["request-ambiguous"])
        self.last_outcome = dict(last_outcome or {})

    def view(self) -> dict:
        return {
            "configured": True,
            "armed": False,
            "connected": True,
            "live_effective": False,
            "mode": "LIVE",
            "account_mode": "DEMO",
            "session_running": False,
            "next_signal_sends_real_order": False,
            "plan": {"present": False},
            "review": {},
            "blockers": [],
            "candidates": [{
                "candidate_id": "candidate-fixed-1",
                "evidence_state": "MEASURED",
                "evidence_note": "Independent replay signed by a reviewer.",
                "rationale": "Exact typed plan, not a PAPER result.",
            }],
            "symbols": ["EURUSD.pro"],
            "timeframes": ["M1"],
            "live_timeframe": "M1",
            "account": {
                "login_masked": "••1234",
                "server": "Fake-Demo",
                "fingerprint": "account-1",
                "currency": "USD",
                "leverage": "100",
            },
            "account_fingerprint": "account-1",
            "positions": self.positions,
            "unresolved": self.unresolved,
            "orders_sent": 0,
            "confirmation_phrase": ARM_CONFIRMATION_PHRASE,
            "session_state": "STOPPED",
            "stopped_reason": "",
            "warnings": [],
            "broker_stop": {"available": False, "detail": "no broker position has been read"},
            "stop_setting": {},
            "runner": {},
            "feed": {},
            "protective_stop": {},
            "last_outcome": self.last_outcome,
            "last_refusal": self.refusal,
            "scalper_live": {
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
                "selected": self.selected,
                "proposals": [],
                "positions": self.positions,
                "unresolved_orders": self.unresolved,
                "pnl": self.pnl,
                "blockers": [{
                    "reason": "proposal_missing",
                    "message": "No exact proposal.",
                    "hint": "",
                }],
                "note": "PAPER results cannot enter this bridge.",
            },
        }

    def summary(self) -> dict:
        return {
            "present": True,
            "armed": False,
            "next_signal_sends_real_order": False,
            "sentence": "No real order can be sent right now.",
        }

    def confirmed_account_fingerprint(self) -> str:
        return "account-1"


def _wired_app(**kwargs):
    app = app_for(simulated_wizard())
    app.live = _RichLive(**kwargs)
    return app


class _LiveAudit(HTMLParser):
    """One rendered page's forms, tables, controls and landmarks."""

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
        self.aria_current: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {key: value or "" for key, value in attrs}
        if a.get("id"):
            self.ids.append(a["id"])
        if a.get("aria-current") == "page":
            self.aria_current.append(tag)
        if tag in {"main", "nav", "header", "footer", "aside"}:
            self.landmarks.append(tag)
        if re.fullmatch(r"h[1-6]", tag):
            self.headings.append(tag)
        if tag == "form":
            self._form = {"action": a.get("action", ""), "fields": []}
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


def test_the_live_page_is_a_separate_workflow_with_an_honest_overview():
    html = _wired_app().page("/live")[1]
    plain = _plain(html)
    assert "LIVE workflow at a glance" in plain
    assert "LIVE · REAL BROKER" in plain
    assert "data-scope='live-execution'" in html
    assert "SIMULATED / SAMPLE DATA" in plain
    # The warning names the PAPER lane and explicitly refuses promotion.
    assert "The PAPER command center is on /autopilot" in plain
    assert "this page never promotes its results" in plain
    assert "href='/autopilot'" in html
    # Exact plan, evidence, lifecycle, broker response and P&L are all on /live.
    for label in (
        "Exact plan, candidate and evidence",
        "Strategy candidates and their evidence",
        "One-shot intent lifecycle",
        "Broker response and unresolved requests",
        "Broker-reported P&L and positions",
        "LIVE and PAPER stay separate",
    ):
        assert label in plain, label
    assert "aria-label='One-shot LIVE intent lifecycle'" in html
    assert "candidate-fixed-1" in plain
    assert "MEASURED" in plain
    assert "Independent replay signed by a reviewer" in plain
    assert "PAPER command center cannot arm, submit, or close a broker order" in plain
    # The six guarded one-shot controls stay on /live; PAPER controls do not.
    for action in (
        "scalper-live-register", "scalper-live-review", "scalper-live-approve",
        "scalper-live-arm", "scalper-live-submit", "scalper-live-reconcile",
    ):
        assert action in _forms(html), action
    for forbidden in (
        "autopilot-start", "autopilot-stop", "runner-start", "mode-live",
        "scalper-paper-start", "scalper-paper-stop", "catalog-paper-close-all",
    ):
        assert forbidden not in _forms(html), forbidden


def test_the_last_arm_decision_is_rendered_once_even_without_an_adapter():
    app = app_for(simulated_wizard())
    app.live_result = {
        "armed": False,
        "reason": "review_required",
        "message": "Nothing was armed: review the exact plan first.",
        "blockers": [{
            "reason": "review_required",
            "message": "Review the exact plan first.",
            "hint": "Use Final review on /live.",
        }],
    }
    html = app.page("/live")[1]
    plain = _plain(html)
    assert "Last arming decision" in plain
    assert "Not armed" in plain
    assert "Nothing was armed: review the exact plan first." in plain
    assert "review_required" in plain
    # Even the not-configured early return shows the consumed one-shot state.
    assert "No MetaTrader 5 trading adapter is wired in" in plain
    assert "LIVE workflow at a glance" in plain
    refreshed = app.page("/live")[1]
    assert "Last arming decision" not in _plain(refreshed)


def test_the_last_arm_decision_is_rendered_on_a_wired_live_page():
    app = _wired_app()
    app.live_result = {
        "armed": True,
        "reason": "armed",
        "message": "Armed exact plan.",
        "blockers": [],
    }
    html = app.page("/live")[1]
    plain = _plain(html)
    assert "Last arming decision" in plain
    assert "Armed" in plain
    assert "Armed exact plan." in plain
    assert "A record of the arming click only" in plain
    assert "LIVE workflow at a glance" in plain
    assert "Last arming decision" not in _plain(app.page("/live")[1])


def test_a_recorded_live_refusal_and_its_blockers_are_no_longer_dropped():
    app = _wired_app(refusal={
        "at": "2026-11-11T10:00:00+00:00",
        "blockers": [{
            "reason": "account_confirmation_mismatch",
            "message": "Account fingerprint did not match.",
            "hint": "Retype the fingerprint shown on /live.",
        }],
    })
    html = app.page("/live")[1]
    plain = _plain(html)
    assert "Last LIVE refusal" in plain
    assert "Arming or an order was refused" in plain
    assert "2026-11-11T10:00:00+00:00" in plain
    assert "account_confirmation_mismatch" in plain
    assert "Account fingerprint did not match." in plain
    assert "No order was sent; the refusal above is the last recorded outcome." in plain
    assert "No order attempt yet" in plain


def test_the_live_lifecycle_names_every_stage_and_the_current_step():
    partial = {
        "intent_id": "intent-2",
        "status": "APPROVED",
        "plan_fingerprint": "fingerprint-2",
        "reviewed": True,
        "approved": True,
        "armed": False,
        "broker": {},
        "reconciliation": {},
        "last_event": {"event_type": "AX_SCALPER_LIVE_APPROVED"},
    }
    html = _wired_app(selected=partial).page("/live")[1]
    plain = _plain(html)
    for label in ("Recorded", "Reviewed", "Approved", "Armed", "Submitted", "Reconciled"):
        assert label in plain, label
    assert "<li aria-current='step'>" in html
    assert "current step" in plain
    assert "not reached" in plain
    assert "Current status: APPROVED" in plain
    assert "An ambiguous request is reconciled by the same durable id" in plain
    complete = _wired_app().page("/live")[1]
    assert "<li aria-current='step'>" not in complete
    assert "Current status: RECONCILED" in _plain(complete)


def test_broker_response_and_unresolved_requests_are_shown():
    html = _wired_app().page("/live")[1]
    plain = _plain(html)
    for fact in (
        "Broker state", "FILLED", "Broker request id", "intent-1",
        "Broker retcode", "TRADE_RETCODE_DONE", "Filled volume", "0.10",
        "Reconciliation", "reconciled", "AX_SCALPER_LIVE_RECONCILED",
        "Unresolved broker requests", "request-ambiguous",
    ):
        assert fact in plain, fact


def test_an_unreported_fill_is_named_and_a_reported_zero_is_shown_as_zero():
    unreported = dict(_FULL_SELECTED, broker=dict(_FULL_SELECTED["broker"], volume_filled=""))
    plain = _plain(_wired_app(selected=unreported).page("/live")[1])
    assert "Filled volume not reported" in plain
    assert "Filled volume 0.10" not in plain
    zero = dict(_FULL_SELECTED, broker=dict(_FULL_SELECTED["broker"], volume_filled="0"))
    plain = _plain(_wired_app(selected=zero).page("/live")[1])
    assert "Filled volume 0 " in plain or "Filled volume 0" in plain
    assert "Filled volume not reported" not in plain


def test_a_broker_answer_without_reconciliation_is_not_called_reconciled():
    bare = {
        "intent_id": "intent-3",
        "status": "UNRESOLVED",
        "broker": {"state": "UNRESOLVED", "request_id": "req-3"},
    }
    html = _wired_app(selected=bare, unresolved=["req-3"]).page("/live")[1]
    plain = _plain(html)
    assert "Reconciliation not recorded" in plain
    assert "reconciled · broker order state" not in plain
    assert "Last durable event not recorded" in plain
    assert "Broker retcode not reported" in plain


def test_broker_pnl_and_positions_are_broker_reported_and_never_paper():
    html = _wired_app().page("/live")[1]
    plain = _plain(html)
    assert "Broker-reported P&L availability" in plain
    assert "12.50 USD" in plain
    assert "broker-reported open position profit only" in plain
    assert "Broker-reported open positions (not PAPER positions)" in plain
    assert "EURUSD.pro" in plain
    assert "Jump to the broker position table" in plain
    assert "id='broker-positions'" in html
    assert "href='#broker-positions'" in html
    assert "PAPER positions and P&L are on /autopilot" in plain


def test_unavailable_broker_pnl_is_named_not_zeroed():
    pnl = {
        "available": False,
        "currency": "USD",
        "total": "",
        "basis": "broker-reported open position profit only",
        "reason": "No complete broker-reported profit is available; balance, equity and closed P&L are not read.",
    }
    html = _wired_app(pnl=pnl, positions=[], unresolved=[]).page("/live")[1]
    plain = _plain(html)
    assert "unavailable — No complete broker-reported profit is available" in plain
    assert "No broker position is open" in plain
    assert "No unresolved broker requests" in plain
    assert "12.50 USD" not in plain
    assert "0.00 USD" not in plain


def test_the_live_overview_is_accessible_and_has_a_narrow_layout():
    html = _wired_app().page("/live")[1]
    audit = _LiveAudit()
    audit.feed(html)
    audit.close()
    assert audit.headings.count("h1") == 1
    assert "main" in audit.landmarks and "nav" in audit.landmarks
    assert len(audit.aria_current) == 1
    assert len(audit.ids) == len(set(audit.ids)), "duplicate ids"
    assert audit.forms
    for form in audit.forms:
        assert form["action"].startswith("/actions/")
        assert form["action"].rsplit("/", 1)[-1] in POST_ACTIONS
        assert any(field["name"] == "nonce" for field in form["fields"])
    for control in audit.controls:
        if control["type"] == "hidden" or control["disabled"]:
            continue
        assert (
            control["aria_label"] or control["labelled"]
            or (control["id"] and control["id"] in audit.label_fors)
        ), control
    for button in audit.buttons:
        assert button["aria_label"] or button["text"].strip()
    for table in audit.tables:
        assert table["caption"] and table["scoped_head"], table
    for href in {
        item.split("#", 1)[0].split("?", 1)[0]
        for item in re.findall(r"href='(/[^']*)'", html)
    }:
        assert href in GET_ROUTES, href
    assert ".lifecycle{" in views.STYLES
    assert ".command-grid,.lifecycle,.tile-grid" in views.STYLES
    assert "@media (max-width:560px)" in views.STYLES
