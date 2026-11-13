"""The paper strategy runner, drawn the same way everywhere it appears.

The runner keeps its own book and its own counters.  This module is the one place
they are described, so the positions page, the overview and the autopilot page
cannot call the same engine running, stopped, or blocked in three different ways.

Nothing here starts, stops or sizes anything on its own: every control posts the
action the application already handles, with the process nonce, and the text
beside it says what that action will and will not do.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .. import format as fmt
from ..components import (
    action_form,
    blockers,
    cell,
    chip,
    empty_state,
    facts,
    section,
    table,
)

#: How a blocker code is said when its producer supplied no sentence.
BLOCKER_LABELS: dict[str, str] = {
    "paper_test_not_built": "No paper test is built yet",
    "no_evaluation_on_record": "No evaluation is on record",
    "no_record": "No evaluation is on record",
    "record_version_mismatch": "The stored evaluation is from an older version",
    "partial_record": "The stored evaluation did not finish",
    "no_paper_set": "No candidate passed the screen",
    "stale_record": "The stored evaluation is too old to start from",
    "terminal_not_connected": "No terminal session is open",
    "symbols_not_selected": "No symbols have been chosen",
    "strategy_not_evaluated": "No strategy has been evaluated",
    "no_strategy_selected": "No strategy has been selected",
    "not_connected": "No terminal session is open",
    "paper_start_refused": "The paper test did not start",
    "recovery_blocked": "The stored paper book could not be resumed exactly",
    "runner_status_unreadable": "The runner's setup state could not be read",
}


def state_chip(runner: Mapping[str, Any] | None) -> str:
    """The runner's own word for its state; a stopped runner is never green."""

    runner = dict(runner or {})
    if not runner or not runner.get("available", True):
        return chip("RUNNER UNAVAILABLE", "muted")
    state = str(runner.get("state", "")).upper()
    if state == "RUNNING":
        return chip("RUNNING (PAPER)", "paper", dot=True)
    if state == "BLOCKED":
        return chip("BLOCKED", "danger", dot=True)
    if state == "STOPPED":
        return chip("STOPPED", "muted")
    return chip(state or "UNKNOWN", "muted")


def blocker_items(items: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Normalise the two blocker shapes in use into the component's fields."""

    out: list[dict[str, Any]] = []
    for item in items or ():
        if not isinstance(item, Mapping):
            continue
        code = str(item.get("reason") or "").strip()
        title = str(item.get("title") or "").strip() or BLOCKER_LABELS.get(
            code, code.replace("_", " ").capitalize() or "Blocked"
        )
        detail = ""
        for key in ("message", "detail"):
            text = str(item.get(key) or "").strip()
            if text:
                detail = text
                break
        hint = str(item.get("hint") or "").strip()
        out.append(
            {
                "title": title,
                "detail": f"{detail}{' — ' + hint if hint else ''}".strip(),
                "code": code,
                "tone": "danger" if str(item.get("tone") or "") == "danger" else "",
            }
        )
    return out


def _actions(ctx: Any, runner: Mapping[str, Any], live_ready: bool, live_reasons: Sequence[str]) -> str:
    """Exactly one primary control, and it says which lane it belongs to."""

    if ctx.live_effective and not runner.get("live_configured", False):
        return chip(
            "Live mode is active but no live path is configured: the runner refuses to start",
            "danger",
        )
    if ctx.live_effective and not live_ready:
        return chip(
            "Live mode is active: starting is refused until the live checks pass ("
            + fmt.text(", ".join(live_reasons[:3]) or "not ready")
            + ")",
            "danger",
        )
    if ctx.live_effective and runner.get("running"):
        return action_form("runner-stop", ctx.action_nonce, "Stop live runner", tone="danger")
    if ctx.live_effective:
        return action_form("runner-start", ctx.action_nonce, "Start LIVE runner", tone="danger")
    if runner.get("running"):
        return action_form("runner-stop", ctx.action_nonce, "Stop runner", tone="warn")
    return action_form("runner-start", ctx.action_nonce, "Start runner", tone="primary")


def sections(ctx: Any) -> str:
    """The runner's state and its own book, as two sections or one honest gap."""

    runner = dict(getattr(ctx, "runner", {}) or {})
    if not runner:
        return section(
            "Paper strategy runner",
            empty_state(
                "No runner configured",
                "This workstation was started without a paper strategy runner. Nothing is "
                "trading and nothing can be started from here.",
            ),
            note="A runner needs imported history, a passed evaluation, and paper mode.",
            tools=chip("NOT CONFIGURED", "muted"),
            id="runner",
        )
    if not runner.get("available", True):
        first = next(iter(runner.get("blockers") or ()), None)
        message = (
            str(first.get("message"))
            if isinstance(first, Mapping) and first.get("message")
            else "The runner could not be reached. Nothing is trading."
        )
        return section(
            "Paper strategy runner",
            empty_state("Runner unavailable", fmt.text(message)),
            note="Nothing is trading, and no control here can start it.",
            tools=state_chip(runner),
            id="runner",
        )

    if runner.get("pending"):
        return section(
            "Paper strategy runner",
            empty_state(
                "Waiting for the terminal",
                fmt.text(
                    str(runner.get("reason_detail") or "Connect in the browser, then press Start.")
                ),
            )
            + blockers(blocker_items(runner.get("setup_blockers") or []))
            + "<div class='actions' style='margin-top:12px'>"
            + action_form("runner-start", ctx.action_nonce, "Start runner", tone="primary")
            + "</div>",
            note="The symbol, the price grid, and the contract terms come from your "
            "connected terminal. Nothing is substituted while it is absent.",
            tools=state_chip(runner),
            id="runner",
        )

    strategy = dict(runner.get("strategy") or {})
    feed = dict(runner.get("data") or {})
    policy = dict(feed.get("policy") or {})
    counters = dict(runner.get("counters") or {})
    live = dict(runner.get("live") or {})
    live_ready = bool(live.get("ready"))
    live_reasons = [str(item.get("reason", "")) for item in (live.get("blockers") or ())]
    status = str(strategy.get("status", ""))
    reason = str(runner.get("reason", ""))
    detail = str(runner.get("reason_detail", ""))

    pending = blocker_items(runner.get("blockers") or ())
    if not runner.get("running"):
        seen = {item["code"] for item in pending}
        for item in blocker_items(runner.get("setup_blockers") or ()):
            if item["code"] in seen:
                continue
            seen.add(item["code"])
            pending.append(item)

    live_fact = (
        f"{chip('ready' if live_ready else 'blocked', 'ok' if live_ready else 'danger', dot=True)} "
        f"<span class='small muted'>{fmt.text(', '.join(live_reasons[:4]) or 'every live check passes')}"
        f" · orders sent {fmt.text(str(runner.get('live_orders', 0)))}</span>"
        if runner.get("live_configured")
        else "<span class='chip chip--warn'>not configured</span> "
        "<span class='small muted'>this process has no MT5 trading adapter; live execution "
        "runs from the operator's own <code>axiom live-run</code> session, never from the "
        "browser</span>"
    )

    rows = [
        ("State", f"{state_chip(runner)} <span class='small muted'>{fmt.text(reason)}</span>"),
        ("Last tick", fmt.text(str(runner.get("last_tick_at", "") or "—"))),
        (
            "Last signal",
            f"{fmt.text(str(runner.get('last_signal', '') or '—'))} "
            f"<span class='small muted'>{fmt.text(str(runner.get('last_signal_at', '') or ''))}</span>",
        ),
        ("Last fill", fmt.text(str(runner.get("last_fill_at", "") or "—"))),
        (
            "Strategy",
            f"{fmt.text(str(strategy.get('candidate_id', '')))} "
            f"<span class='small muted'>{fmt.text(str(strategy.get('parameters', '')))}</span>",
        ),
        (
            "Candidate status",
            chip("—" if not status else status, "ok" if status == "SELECTED_FOR_PAPER" else "warn"),
        ),
        (
            "Symbol",
            f"{fmt.text(str(runner.get('symbol', '')))} "
            f"<span class='small muted'>qty {fmt.text(str(runner.get('quantity', '')))}</span>",
        ),
        (
            "Feed",
            f"{fmt.text(str(feed.get('kind', '')))} "
            f"<span class='small muted'>{fmt.text(str(feed.get('data_class', '')))} · every "
            f"{fmt.text(str(policy.get('interval_seconds', '—')))}s · max quote age "
            f"{fmt.text(str(policy.get('max_quote_age_seconds', '—')))}s — "
            f"{fmt.text(str(feed.get('label', '')))}</span>",
        ),
        (
            "Mode",
            f"{chip(str(runner.get('execution_mode', 'PAPER')), 'danger' if str(runner.get('execution_mode', '')).upper() == 'LIVE' else 'paper')} "
            f"<span class='small muted'>order sending: "
            f"{'yes' if runner.get('order_sending') else 'no'} · "
            f"{fmt.text(str(runner.get('trading_capability', '')))}</span>",
        ),
        ("Live path", live_fact),
        ("Cycles / bars", f"{fmt.text(str(counters.get('cycles', 0)))} / {fmt.text(str(counters.get('bars', 0)))}"),
        (
            "Signals / orders (this runner)",
            f"{fmt.text(str(counters.get('signals', 0)))} / {fmt.text(str(counters.get('orders', 0)))}",
        ),
        (
            "Simulated fills (this runner)",
            f"{fmt.text(str(counters.get('fills', 0)))} "
            f"<span class='small muted'>of {fmt.text(str(counters.get('orders', 0)))} order(s) "
            "it recorded itself</span>",
        ),
        ("Refusals", fmt.text(str(counters.get("refusals", 0)))),
        ("Why", f"{fmt.text(reason)} <span class='small muted'>{fmt.text(detail[:120])}</span>"),
        ("Restored", "yes" if runner.get("restored") else "no (fresh state)"),
    ]

    recent = list(runner.get("recent") or ())[:5]
    recent_rows = [
        [
            cell(fmt.text(str(item.get("at", ""))[:19]), label="When", mono=True),
            cell(fmt.text(str(item.get("reason", ""))), label="Reason"),
            cell(fmt.text(str(item.get("code", "") or "—")), label="Code"),
            cell(fmt.text(str(item.get("detail", ""))[:80]), label="Detail"),
        ]
        for item in recent
    ]

    footer_text = (
        "Live mode: a signal that passes the same pre-trade gate is sent to MT5 and the "
        "broker's own fill is booked back into the ledger. The runner never arms the "
        "terminal, never retries an order, and stops on an unknown outcome."
        if runner.get("live_configured")
        else "Paper only: signals are simulated against the local ledger. The runner cannot "
        "send an order, arm live execution, or change the execution mode; without a live "
        "path it refuses to run in LIVE mode."
    )

    main = section(
        "Paper strategy runner",
        facts(rows)
        + blockers(pending)
        + "<div class='actions' style='margin-top:12px'>"
        + _actions(ctx, runner, live_ready, live_reasons)
        + "</div>"
        + section(
            "Last decisions",
            table(
                (("When", False), ("Reason", False), ("Code", False), ("Detail", False)),
                recent_rows,
                dense=True,
                empty=empty_state(
                    "No decisions yet",
                    "Press Start; the first cycle records its reason here.",
                ),
            ),
            plain=True,
            flush=True,
        ),
        note="Simulated fills only, kept in this runner's own book. The paper test's ledger "
        "figures are on Paper performance; the two are separate engines and their counters "
        "are labelled accordingly.",
        tools=state_chip(runner),
        id="runner",
        footer=footer_text,
    )

    positions = list(runner.get("positions") or ())
    fills = list(runner.get("fills") or ())
    position_rows = [
        [
            cell(fmt.text(str(item.get("pair", ""))), label="Pair"),
            cell(fmt.text(str(item.get("side", ""))), label="Side"),
            cell(
                fmt.text(str(item.get("remaining_quantity", item.get("quantity", "")))),
                label="Qty",
                num=True,
            ),
            cell(fmt.text(str(item.get("entry_price", ""))), label="Entry", num=True),
            cell(fmt.text(str(item.get("entry_timestamp", ""))[:19]), label="Opened", mono=True),
        ]
        for item in positions
    ]
    fill_rows = [
        [
            cell(fmt.text(str(item.get("filled_at", ""))[:19]), label="Filled", mono=True),
            cell(fmt.text(str(item.get("action", ""))), label="Action"),
            cell(fmt.text(str(item.get("side", ""))), label="Side"),
            cell(fmt.text(str(item.get("quantity", ""))), label="Qty", num=True),
            cell(fmt.text(str(item.get("price", ""))), label="Price", num=True),
        ]
        for item in reversed(fills[-8:])
    ]

    book = section(
        "Runner positions and fills",
        table(
            (("Pair", False), ("Side", False), ("Qty", True), ("Entry", True), ("Opened", False)),
            position_rows,
            caption="Positions opened by the runner (simulated ledger)",
            empty=empty_state(
                "No open runner positions", "Flat until the strategy signals an entry."
            ),
        )
        + table(
            (("Filled", False), ("Action", False), ("Side", False), ("Qty", True), ("Price", True)),
            fill_rows,
            caption="Recent simulated fills",
            empty=empty_state(
                "No fills yet", "A fill appears only after a signal passes every gate."
            ),
        ),
        note="This runner's own book. The paper test's ledger is a different engine.",
        id="runner-book",
        flush=True,
    )
    return main + book
