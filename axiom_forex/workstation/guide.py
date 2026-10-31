"""The guided start page: five steps, one status summary, one next click.

A new operator should not have to know that "Account & symbols", "Data &
evaluation", "Quick setup" and the strategy library exist.  This module turns the
workstation's existing read models -- the terminal view, the setup wizard, the
catalog panel's evaluation and its live-market paper test, and the panel's own
history check -- into one ordered answer:

* which of the five steps is done, which needs attention, and why;
* what the single next action is, with the exact form it posts;
* one status summary: connection, paper session, symbols and strategy, market
  data and its freshness, the last signal/order/fill, open paper positions, and
  the one reason trading has not started.

It reads; it never acts.  Every action it names is an existing, allow-listed
workstation action, and every state it reports is taken from something already
measured -- a panel view, a stored record, or the terminal's own report.  Nothing
here can place an order, change the execution mode, or start a live session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

#: Step states.  ``next`` is the one the operator should do; ``blocked`` needs
#: something else first; ``running`` is in progress; ``unavailable`` means this
#: build has no such component wired.
DONE = "done"
NEXT = "next"
BLOCKED = "blocked"
RUNNING = "running"
UNAVAILABLE = "unavailable"

#: The timeframe the guided flow evaluates on.  The catalog's own default, so the
#: page can say one thing and the evaluator do exactly that.
GUIDE_TIMEFRAME = "1h"

#: How many screened rows the start page offers to paper trade at once.
MAX_OFFERED_CANDIDATES = 12


@dataclass(frozen=True)
class Step:
    """One step of the guided flow, in the operator's words."""

    key: str
    number: int
    title: str
    why: str
    state: str
    status: str
    detail: str = ""
    #: ``form`` posts an existing allow-listed action; ``link`` opens a page.
    action_kind: str = ""
    action_target: str = ""
    action_label: str = ""
    #: Hidden inputs for a form action, as ``(name, value)`` pairs.
    fields: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    #: Secondary links offered next to the primary action.
    links: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    #: Anything that stops this step, in plain language.
    blockers: tuple[str, ...] = field(default_factory=tuple)

    @property
    def complete(self) -> bool:
        return self.state in {DONE, RUNNING}

    @property
    def needs_attention(self) -> bool:
        return self.state in {NEXT, BLOCKED}

    def audit_view(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "number": self.number,
            "title": self.title,
            "state": self.state,
            "status": self.status,
            "detail": self.detail,
            "action_kind": self.action_kind,
            "action_target": self.action_target,
            "action_label": self.action_label,
            "fields": list(self.fields),
            "links": list(self.links),
            "blockers": list(self.blockers),
        }


#: What each step means, in one sentence, before any status is known.
STEP_WHY: dict[str, str] = {
    "connect": (
        "AXIOM reads the MetaTrader 5 terminal you already have open and signed in. "
        "It never logs in, and it never switches the account."
    ),
    "symbols": (
        "Only symbols the terminal reports both currency legs for can be used. "
        "Broker names are kept exactly as the broker writes them."
    ),
    "data": (
        "The evaluation reads the terminal's own historical bars and refuses a "
        "symbol whose history is too short instead of filling the gap."
    ),
    "evaluate": (
        "Every declared strategy is measured on that history. A candidate that "
        "passes the screen is a short-list entry, not a promise of profit."
    ),
    "paper": (
        "The paper session prices its entries and exits from live terminal quotes "
        "and fills them locally. No order is sent anywhere."
    ),
}


def _candidate_facts(candidate_id: str) -> dict[str, str]:
    """A screened row's plain-language name, when this build declares it.

    The record stores ``cat-<hash>`` ids, which are stable but meaningless to
    read.  The catalog itself knows the family label and the sentence describing
    the rule; when a build does not declare that id, the page shows the id rather
    than inventing a name for it.
    """

    if not candidate_id:
        return {}
    try:
        from axiom_forex.strategies.catalog.candidates import FAMILY_LABELS, by_id
    except Exception:  # pragma: no cover - the catalog ships with the repo
        return {}
    try:
        candidate = by_id(candidate_id)
    except Exception:
        return {}
    if candidate is None:
        return {}
    return {
        "family": str(FAMILY_LABELS.get(candidate.family, candidate.family)),
        "summary": str(candidate.summary),
        "structure": str(candidate.structure),
    }


def _short(text: str, limit: int = 140) -> str:
    """One readable line of a long rule description."""

    clean = " ".join(str(text or "").split())
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def _selected(wizard_view: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(item) for item in (wizard_view.get("symbols_selected") or ()) if str(item))


def _first(rows: Any) -> Mapping[str, Any]:
    items = [row for row in (rows or ()) if isinstance(row, Mapping)]
    return dict(items[-1]) if items else {}


def _age_words(seconds: Any) -> str:
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return ""
    if value < 90:
        return f"{int(value)}s ago"
    if value < 5400:
        return f"{int(value // 60)} min ago"
    if value < 172800:
        return f"{value / 3600:.1f} h ago"
    return f"{value / 86400:.1f} days ago"


def _paper_state(
    paper: Mapping[str, Any], runner: Mapping[str, Any]
) -> tuple[str, str, str, tuple[str, ...]]:
    """The paper session's state, as (state, status text, detail, blockers).

    The guided flow starts the catalog's live-market paper test.  A workstation
    launched with the older paper strategy runner reports that instead, so the
    summary never says "not trading" while a runner is cycling.
    """

    snapshot = dict(paper.get("snapshot") or {})
    status = str(snapshot.get("status") or "IDLE").upper()
    running = bool(paper.get("running") or snapshot.get("running"))
    blockers = tuple(
        str(item.get("detail") or item.get("reason") or "")
        for item in (paper.get("blockers") or ())
        if isinstance(item, Mapping)
    )
    if running:
        cycles = dict(snapshot.get("stats") or {})
        detail = (
            f"Following {len(snapshot.get('watches') or ())} screened row(s) · "
            f"{cycles.get('cycles', 0)} cycle(s), {cycles.get('signals', 0)} signal(s), "
            f"{cycles.get('fills', 0)} simulated fill(s)."
        )
        return RUNNING, "RUNNING", detail, ()
    if bool(runner.get("running")) and str(runner.get("state", "")).upper() in {"RUNNING"}:
        return (
            RUNNING,
            "RUNNING (PAPER RUNNER)",
            "The paper strategy runner is cycling on its own schedule. Nothing it "
            "produces is a broker fill.",
            (),
        )
    if status == "BLOCKED":
        return BLOCKED, "BLOCKED", (blockers[0] if blockers else "The paper session is blocked."), blockers
    if status == "STOPPED":
        return BLOCKED, "STOPPED", "The paper session was stopped. Start it again when you are ready.", blockers
    if not paper.get("built"):
        return BLOCKED, "NOT STARTED", "", blockers
    return BLOCKED, "NOT STARTED", "The paper session is built and waiting to be started.", blockers


def _market_data(
    paper: Mapping[str, Any], data_check: Mapping[str, Any]
) -> tuple[str, str, str]:
    """Latest market-data time and freshness, as (label, tone, detail)."""

    snapshot = dict(paper.get("snapshot") or {})
    quotes = [dict(item) for item in (snapshot.get("quotes") or ()) if isinstance(item, Mapping)]
    if quotes:
        fresh = [item for item in quotes if item.get("fresh")]
        newest = max(
            (item for item in quotes if item.get("checked_at")),
            key=lambda item: str(item.get("checked_at")),
            default={},
        )
        ages = [item.get("age_seconds") for item in quotes if item.get("age_seconds") is not None]
        age = min(ages) if ages else None
        tone = "ok" if fresh and len(fresh) == len(quotes) else "warn"
        return (
            "FRESH" if tone == "ok" else "STALE",
            tone,
            (
                f"{len(fresh)}/{len(quotes)} quote(s) inside the "
                f"{snapshot.get('max_quote_age_seconds', '')}s limit · newest "
                f"{newest.get('checked_at', '')}"
                + (f" ({_age_words(age)})" if age is not None else "")
            ).strip(),
        )
    rows = [dict(item) for item in (data_check.get("rows") or ()) if isinstance(item, Mapping)]
    usable = [row for row in rows if row.get("usable")]
    if usable:
        newest = max((str(row.get("last") or "") for row in usable), default="")
        ages = [row.get("age_seconds") for row in usable if row.get("age_seconds") is not None]
        age = min(ages) if ages else None
        closed_bar = age is not None and float(age) > 3600
        return (
            "HISTORY" if closed_bar else "RECENT",
            "ok",
            (
                f"Newest stored bar {newest}"
                + (f" ({_age_words(age)})" if age is not None else "")
                + " · prices start with the paper session"
            ),
        )
    if not data_check.get("available", True):
        return "NOT CHECKED", "muted", str(data_check.get("message") or "")
    return "NO HISTORY", "warn", str(data_check.get("message") or "No usable history yet.")


def build(
    *,
    terminal: Any,
    wizard_view: Mapping[str, Any],
    symbols: Mapping[str, Any],
    catalog: Mapping[str, Any],
    data_check: Mapping[str, Any],
    runner: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the guided start model from read-only views.

    ``symbols`` carries what the symbol pages already computed -- whether the
    terminal reported symbols at all, how many, and how many could be selected in
    one click -- so this page never re-walks the terminal on its own.
    """

    selected = _selected(wizard_view)
    runner_view = dict(runner or {})
    catalog = dict(catalog or {})
    data_check = dict(data_check or {})
    available = bool(catalog.get("available"))
    symbols_view = dict(catalog.get("symbols") or {})
    evaluation = dict(catalog.get("evaluation") or {})
    paper = dict(catalog.get("paper") or {})
    snapshot = dict(paper.get("snapshot") or {})
    paper_set = [dict(row) for row in (evaluation.get("paper_set") or ()) if isinstance(row, Mapping)]
    blockers = [dict(item) for item in (evaluation.get("blockers") or ()) if isinstance(item, Mapping)]

    connected = bool(getattr(terminal, "connected", False))
    simulated = bool(getattr(terminal, "simulated", False))
    connect_actions = (("connect",),)
    steps: list[Step] = []

    # 1 -- the terminal ---------------------------------------------------
    if connected:
        where = str(getattr(terminal, "server", "") or "a connected terminal")
        steps.append(
            Step(
                key="connect",
                number=1,
                title="Connect to MetaTrader 5",
                why=STEP_WHY["connect"],
                state=DONE,
                status="CONNECTED",
                detail=(
                    f"{where} · {getattr(terminal, 'mode', '') or 'mode unknown'}"
                    + (" · offline simulator, no terminal attached" if simulated else "")
                ),
            )
        )
    else:
        error = dict(getattr(terminal, "error", None) or {})
        reason = str(error.get("reason") or "")
        hint = str(error.get("hint") or error.get("detail") or "")
        if reason:
            # A refusal is part of the flow, not a hidden failure: the reason the
            # terminal was not adopted is the step's own text, with the fix.
            steps.append(
                Step(
                    key="connect",
                    number=1,
                    title="Connect to MetaTrader 5",
                    why=STEP_WHY["connect"],
                    state=BLOCKED,
                    status="CONNECTION REFUSED",
                    detail=f"{reason}{' — ' + hint if hint else ''}",
                    blockers=(f"{reason}",) if not hint else (),
                    action_kind="form",
                    action_target="connect",
                    action_label="Try again",
                    links=(("Setup steps", "/setup"),),
                )
            )
        else:
            steps.append(
                Step(
                    key="connect",
                    number=1,
                    title="Connect to MetaTrader 5",
                    why=STEP_WHY["connect"],
                    state=NEXT,
                    status="NOT CONNECTED",
                    detail=(
                        "Start the preview to see sample values, or launch on the Windows "
                        "machine that runs MetaTrader 5."
                        if simulated
                        else "Open MetaTrader 5, sign in, then connect here."
                    ),
                    action_kind="form",
                    action_target="connect",
                    action_label=(
                        "Start the simulator preview (not MT5)"
                        if simulated
                        else "Connect (read-only)"
                    ),
                    links=(("Setup steps", "/setup"),) if not simulated else (),
                )
            )

    # 2 -- the symbols ----------------------------------------------------
    select_all_count = int(symbols.get("select_all") or 0)
    reported = int(symbols.get("reported") or 0)
    if selected:
        steps.append(
            Step(
                key="symbols",
                number=2,
                title="Choose the forex symbols",
                why=STEP_WHY["symbols"],
                state=DONE,
                status=f"{len(selected)} SELECTED",
                detail=", ".join(selected),
                links=(("Change the selection", "/account"),),
            )
        )
    elif not connected:
        steps.append(
            Step(
                key="symbols",
                number=2,
                title="Choose the forex symbols",
                why=STEP_WHY["symbols"],
                state=BLOCKED,
                status="WAITING FOR THE TERMINAL",
                detail="The symbol list comes from the terminal, so connect first.",
                links=(("Open Account & symbols", "/account"),),
            )
        )
    else:
        detail = (
            f"{select_all_count} verified forex pair(s) of {reported} reported "
            "symbol(s) can be followed."
            if select_all_count
            else "No reported symbol could be verified as a forex pair. Open the "
            "symbol page to see each reason."
        )
        steps.append(
            Step(
                key="symbols",
                number=2,
                title="Choose the forex symbols",
                why=STEP_WHY["symbols"],
                state=NEXT if select_all_count else BLOCKED,
                status="NONE SELECTED",
                detail=detail,
                action_kind="form" if select_all_count else "",
                action_target="symbols" if select_all_count else "",
                action_label="Select all verified forex pairs",
                fields=(("bulk", "all"), ("return_to", "/")) if select_all_count else (),
                links=(("Choose individually", "/account"),),
            )
        )

    # 3 -- the history ----------------------------------------------------
    if not connected:
        steps.append(
            Step(
                key="data",
                number=3,
                title="Check the market data",
                why=STEP_WHY["data"],
                state=BLOCKED,
                status="WAITING FOR THE TERMINAL",
                detail="There is no other price source: the bars come from the terminal.",
            )
        )
    elif not selected:
        steps.append(
            Step(
                key="data",
                number=3,
                title="Check the market data",
                why=STEP_WHY["data"],
                state=BLOCKED,
                status="WAITING FOR A SYMBOL",
                detail="Choose at least one symbol; history is checked per symbol.",
            )
        )
    else:
        rows = [dict(item) for item in (data_check.get("rows") or ()) if isinstance(item, Mapping)]
        usable = [row for row in rows if row.get("usable")]
        unusable = [row for row in rows if not row.get("usable")]
        if not data_check.get("available", True):
            steps.append(
                Step(
                    key="data",
                    number=3,
                    title="Check the market data",
                    why=STEP_WHY["data"],
                    state=BLOCKED,
                    status="NOT AVAILABLE",
                    detail=str(data_check.get("message") or "This build has no bar reader."),
                )
            )
        elif usable:
            shortest = min(int(row.get("bars") or 0) for row in usable)
            steps.append(
                Step(
                    key="data",
                    number=3,
                    title="Check the market data",
                    why=STEP_WHY["data"],
                    state=DONE,
                    status=f"{len(usable)} SERIES READY",
                    detail=(
                        f"{data_check.get('timeframe', GUIDE_TIMEFRAME)} bars from the "
                        f"terminal · shortest {shortest} bar(s)"
                        + (
                            f" · {len(unusable)} symbol(s) skipped, listed below"
                            if unusable
                            else ""
                        )
                    ),
                    blockers=tuple(
                        f"{row.get('symbol')}: {row.get('reason')}" for row in unusable
                    ),
                    links=(("Re-check", "/?data=1"),),
                )
            )
        else:
            steps.append(
                Step(
                    key="data",
                    number=3,
                    title="Check the market data",
                    why=STEP_WHY["data"],
                    state=BLOCKED,
                    status="NO USABLE HISTORY",
                    detail=str(data_check.get("message") or "The terminal returned no bars."),
                    blockers=tuple(
                        f"{row.get('symbol')}: {row.get('reason')}" for row in rows
                    ),
                    links=(("Re-check", "/?data=1"),),
                )
            )

    # 4 -- the evaluation -------------------------------------------------
    if not available:
        steps.append(
            Step(
                key="evaluate",
                number=4,
                title="Evaluate the strategies",
                why=STEP_WHY["evaluate"],
                state=UNAVAILABLE,
                status="NOT WIRED IN THIS BUILD",
                detail="This process has no strategy catalog, so there is nothing to evaluate here.",
            )
        )
    elif evaluation.get("running"):
        steps.append(
            Step(
                key="evaluate",
                number=4,
                title="Evaluate the strategies",
                why=STEP_WHY["evaluate"],
                state=RUNNING,
                status="EVALUATING",
                detail=f"Started {evaluation.get('started_at', '')}. Reload this page for the result.",
            )
        )
    elif evaluation.get("on_record"):
        counts = dict(evaluation.get("counts") or {})
        screen = dict(counts.get("screen") or {})
        passed = int(screen.get("PASSED_SCREEN", 0) or 0)
        steps.append(
            Step(
                key="evaluate",
                number=4,
                title="Evaluate the strategies",
                why=STEP_WHY["evaluate"],
                state=DONE,
                status=f"{passed} PASSED THE SCREEN",
                detail=(
                    f"{counts.get('tested', 0)} row(s) tested, "
                    f"{counts.get('skipped', 0)} skipped, {counts.get('failed', 0)} failed · "
                    f"data origin {evaluation.get('data_origin', 'unknown')} · "
                    f"written {evaluation.get('written_at', '')}"
                ),
                blockers=tuple(
                    f"{item.get('reason', '')}: {item.get('detail', '')}".strip(": ")
                    for item in blockers
                ),
                links=(("See every row and reason", "/quick-setup"),),
            )
        )
    elif selected:
        steps.append(
            Step(
                key="evaluate",
                number=4,
                title="Evaluate the strategies",
                why=STEP_WHY["evaluate"],
                state=NEXT,
                status="NOT EVALUATED",
                detail=(
                    f"Runs the declared catalog on the terminal's own "
                    f"{GUIDE_TIMEFRAME} bars for up to 3 selected symbol(s)."
                ),
                action_kind="form",
                action_target="catalog-evaluate",
                action_label="Evaluate the strategies",
                fields=tuple(
                    [("symbols", name) for name in selected[:3]]
                    + [("timeframe", GUIDE_TIMEFRAME), ("return_to", "/")]
                ),
                links=(("Full catalog page", "/quick-setup"),),
            )
        )
    else:
        steps.append(
            Step(
                key="evaluate",
                number=4,
                title="Evaluate the strategies",
                why=STEP_WHY["evaluate"],
                state=BLOCKED,
                status="WAITING FOR A SYMBOL",
                detail="The evaluation needs at least one selected forex symbol.",
            )
        )

    # 5 -- the paper session ----------------------------------------------
    paper_state, paper_status, paper_detail, paper_blockers = _paper_state(paper, runner_view)
    if paper_set:
        # Nothing has been built yet on this page, so "build the test first" is not
        # a reason the operator is blocked: the button below builds and starts it.
        paper_blockers = tuple(
            item for item in paper_blockers if "Build the test" not in item
        )
    if not available:
        steps.append(
            Step(
                key="paper",
                number=5,
                title="Start paper trading",
                why=STEP_WHY["paper"],
                state=UNAVAILABLE,
                status="NOT WIRED IN THIS BUILD",
                detail="This process has no paper session to start.",
            )
        )
    elif paper_state == RUNNING:
        # The catalog's paper test is stopped from here; the older paper runner is
        # stopped on its own page, so this page links to it instead of posting an
        # action that would refuse.
        ours = bool(paper.get("running") or snapshot.get("running"))
        steps.append(
            Step(
                key="paper",
                number=5,
                title="Start paper trading",
                why=STEP_WHY["paper"],
                state=RUNNING,
                status="RUNNING · PAPER",
                detail=paper_detail,
                action_kind="form" if ours else "link",
                action_target="catalog-paper-stop" if ours else "/positions",
                action_label="Stop the paper session" if ours else "Open the paper runner",
                fields=(("return_to", "/"),) if ours else (),
                links=(("Open the paper ledger", "/positions"),),
            )
        )
    elif paper_set:
        offered = paper_set[:MAX_OFFERED_CANDIDATES]
        steps.append(
            Step(
                key="paper",
                number=5,
                title="Start paper trading",
                why=STEP_WHY["paper"],
                state=NEXT,
                status="READY TO START",
                detail=(
                    f"{len(paper_set)} screened candidate(s) on "
                    + ", ".join(sorted({str(row.get('symbol') or '') for row in paper_set}))
                    + ". Tick the ones to follow, then start."
                ),
                action_kind="form",
                action_target="catalog-paper-start",
                action_label="Start PAPER trading",
                fields=(("return_to", "/"),),
                links=(("What was evaluated", "/quick-setup"),),
                blockers=paper_blockers,
            )
        )
    else:
        reason = (
            paper_blockers[0]
            if paper_blockers
            else "No candidate passed the screen, so there is nothing to paper trade yet."
        )
        steps.append(
            Step(
                key="paper",
                number=5,
                title="Start paper trading",
                why=STEP_WHY["paper"],
                state=BLOCKED,
                status=paper_status,
                detail=reason,
                blockers=paper_blockers,
            )
        )

    next_step = next((step for step in steps if step.needs_attention), None)
    if paper_state == RUNNING:
        headline = "Paper trading is running."
        reason = ""
    else:
        headline = "Paper trading has not started yet."
        reason = (
            f"Step {next_step.number} — {next_step.title}: "
            f"{next_step.detail or STEP_WHY.get(next_step.key, '')}"
            if next_step is not None
            else "Every step is complete; press Start PAPER trading."
        )

    data_label, data_tone, data_detail = _market_data(paper, data_check)
    latest_signal = _first(snapshot.get("signals"))
    latest_order = _first(snapshot.get("orders"))
    latest_fill = _first(snapshot.get("fills"))
    positions = [dict(item) for item in (snapshot.get("positions") or ()) if isinstance(item, Mapping)]
    strategy_ids = sorted({str(row.get("candidate_id") or "") for row in paper_set} - {""})
    strategy_names = sorted(
        {
            _candidate_facts(item).get("family") or item[:12]
            for item in strategy_ids
        }
        - {""}
    )

    mode = str(getattr(terminal, "mode", "") or "")
    summary = {
        "headline": headline,
        "reason": reason,
        "ready": paper_state == RUNNING,
        "next_key": next_step.key if next_step is not None else "",
        "tiles": [
            {
                "key": "terminal",
                "label": "MetaTrader 5",
                "value": "Connected" if connected else "Not connected",
                "tone": "ok" if connected else "warn",
                "detail": (
                    f"{getattr(terminal, 'server', '') or 'terminal'} · {mode or 'mode unknown'}"
                    + (" · simulator" if simulated else "")
                    if connected
                    else "Nothing is read from a terminal yet."
                ),
            },
            {
                "key": "session",
                "label": "Paper session",
                "value": paper_status if available else "Not wired",
                "tone": (
                    "ok"
                    if paper_state == RUNNING
                    else ("warn" if available else "muted")
                ),
                "detail": paper_detail or "Simulated fills on live terminal quotes.",
            },
            {
                "key": "symbols",
                "label": "Symbols",
                "value": f"{len(selected)} selected" if selected else "None selected",
                "tone": "ok" if selected else "warn",
                "detail": ", ".join(selected) or "Choose the symbols to follow.",
            },
            {
                "key": "strategy",
                "label": "Strategy",
                "value": (
                    ", ".join(strategy_names[:3])
                    if strategy_names
                    else ("Screened set" if paper_state == RUNNING else "None yet")
                ),
                "tone": "ok" if strategy_names else "warn",
                "detail": (
                    f"{len(paper_set)} screened candidate row(s)"
                    if paper_set
                    else "Nothing has passed the screen yet."
                ),
            },
            {
                "key": "data",
                "label": "Market data",
                "value": data_label,
                "tone": data_tone,
                "detail": data_detail,
            },
            {
                "key": "signal",
                "label": "Latest signal",
                "value": (
                    f"{latest_signal.get('decision') or latest_signal.get('action') or 'seen'}"
                    if latest_signal
                    else "None yet"
                ),
                "tone": "paper" if latest_signal else "muted",
                "detail": (
                    f"{latest_signal.get('symbol', '')} {latest_signal.get('key', '')}".strip()
                    if latest_signal
                    else "The session records a decision for every cycle."
                ),
            },
            {
                "key": "order",
                "label": "Simulated orders and fills",
                "value": (
                    f"{len(snapshot.get('orders') or ())} order(s) · "
                    f"{len(snapshot.get('fills') or ())} filled"
                    if snapshot
                    else "None yet"
                ),
                "tone": "paper" if latest_order or latest_fill else "muted",
                "detail": (
                    "Locally simulated. No order was sent to a broker."
                    if latest_order or latest_fill
                    else "Paper fills appear here once a signal fires."
                ),
            },
            {
                "key": "positions",
                "label": "Open paper positions",
                "value": str(len(positions)),
                "tone": "paper" if positions else "muted",
                "detail": (
                    ", ".join(
                        f"{item.get('symbol', '')} {item.get('side', '')}".strip()
                        for item in positions[:4]
                    )
                    or "Nothing open."
                ),
            },
        ],
        "steps_done": sum(1 for step in steps if step.complete),
        "steps_total": len(steps),
        "mode": mode,
    }

    return {
        "steps": steps,
        "summary": summary,
        "next_step": next_step,
        "next": next_step.audit_view() if next_step is not None else {},
        "paper": paper,
        "evaluation": evaluation,
        "candidates": [
            {
                "key": "|".join(
                    str(row.get(field) or "")
                    for field in ("symbol", "timeframe", "candidate_id")
                ),
                "symbol": str(row.get("symbol") or ""),
                "timeframe": str(row.get("timeframe") or ""),
                "candidate": str(row.get("candidate_id") or ""),
                "family": (
                    _candidate_facts(str(row.get("candidate_id") or "")).get("family")
                    or str(row.get("family") or "")
                ),
                "trades": row.get("trades_in_evaluation", ""),
                "summary": _short(
                    _candidate_facts(str(row.get("candidate_id") or "")).get("summary")
                    or str(row.get("summary") or "")
                ),
            }
            for row in paper_set[:MAX_OFFERED_CANDIDATES]
        ],
        "advanced": (
            ("Account & symbols", "/account", "Pick symbols one by one, with reasons for each exclusion."),
            ("Broker terms (advanced)", "/provider-setup", "Only needed for LIVE execution; paper does not use it."),
            ("Stored history & research sweep", "/quick-setup", "A year of bars, costs, and the full sweep."),
            ("Data & evaluation", "/data", "Imported files, conversions, and dataset checks."),
            ("Strategy library", "/strategies", "Screen, compare, and select strategies by hand."),
            ("Trading mode", "/mode", "PAPER is the default. LIVE has its own safeguards."),
            ("Live execution", "/live", "Arming, limits, and the live plan. Nothing here starts it."),
            ("Setup steps", "/setup", "The classic step-by-step checklist."),
            ("Overview (classic)", "/overview", "The older dashboard: every figure in one place."),
            ("System health", "/health", "Local checks and the audit trail."),
            ("Kronos forecast", "/kronos", "Optional forecasting component. It cannot trade."),
        ),
    }


__all__ = [
    "BLOCKED",
    "DONE",
    "GUIDE_TIMEFRAME",
    "MAX_OFFERED_CANDIDATES",
    "NEXT",
    "RUNNING",
    "STEP_WHY",
    "Step",
    "UNAVAILABLE",
    "build",
]
