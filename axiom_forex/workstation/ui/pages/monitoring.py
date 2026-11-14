"""The four monitoring pages: Health, Activity, Positions, Performance.

They share one shape — a page head that states the lane, a **facts ledger** for
the summary, **tables** for anything listed, and a provenance block — but each
answers a different question, and none of them borrows another's numbers:

* Health:   is the platform's own machinery passing, and what is not.
* Activity: what happened, in the order it happened, from the paper ledger.
* Positions: what is open, what was closed, and what cannot be marked.
* Performance: what the ledger says the paper test earned, and on what basis.

Every figure is read from the dictionary the accounting layer produced; none is
recomputed here, so the four pages cannot disagree about the same fill.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .. import format as fmt
from ..components import (
    button_link,
    field,
    cell,
    chip,
    chip_row,
    empty_state,
    facts,
    layout,
    notice,
    page_head,
    pager,
    section,
    stat,
    stats,
    table,
    timeline,
)
from axiom_forex.paper_runner.accounting import CURRENCY_CHOICES as CURRENCIES
from . import common
from . import runner as runner_page

BROKER_NOT_READ = "Not read, not used, and never mixed into a paper figure."

#: What each ledger event is called on the activity page.  A warning tone is an
#: event that skipped a decision or refused a resume; those are counted apart.
EVENT_LABELS: dict[str, tuple[str, str]] = {
    "simulated_fill": ("Entry fill", "paper"),
    "simulated_close": ("Exit fill", "paper"),
    "stale_quote": ("Stale quote skipped", "warn"),
    "market_closed": ("Market closed", "warn"),
    "signal": ("Signal", "muted"),
    "signal_skipped": ("Signal skipped", "warn"),
    "waiting_for_history": ("Waiting for history", "muted"),
    "position_restored": ("Position restored", "muted"),
    "paper_test_started": ("Session started", "ok"),
    "paper_test_stopped": ("Session stopped", "muted"),
    "paper_book_flattened": ("Paper book closed by the operator", "warn"),
    "state_migrated": ("State migrated", "muted"),
    "state_recovery_failed": ("Resume refused", "warn"),
}

PER_PAGE = 25
TRADE_PAGE = 25


def _event_label(kind: str) -> tuple[str, str]:
    return EVENT_LABELS.get(kind, (kind or "event", "muted"))


def _lane_chip(label: str) -> str:
    return chip(label, "paper", dot=True)


def _session_chip(session: Mapping[str, Any]) -> str:
    if session.get("running"):
        return chip("SESSION RUNNING", "paper", dot=True)
    return chip(f"SESSION {str(session.get('status') or 'NOT RUNNING').upper()}", "muted")


def _ledger_facts(ctx: Any, account: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Where the ledger numbers came from, worded once."""

    read_at = account.get("read_at")
    pairs: list[tuple[str, str]] = [
        ("Data source", f"{fmt.text(account.get('source') or 'durable paper ledger')}"),
        ("Paper platform", f"{common.data_chip(ctx.paper_label)} · source {fmt.text(ctx.paper.source or 'not reported')}"),
        ("Ledger file", f"<code>{fmt.text(account.get('ledger_path') or 'not reported')}</code>"),
        ("State file", f"<code>{fmt.text(account.get('state_path') or 'not reported')}</code>"),
        ("Read at", fmt.text(fmt.utc(read_at))),
        (
            "Latest ledger update",
            (
                f"{fmt.text(fmt.utc(account.get('latest_update')))} "
                f"<span class='small muted'>· {fmt.text(fmt.age(account.get('latest_update'), read_at))}</span>"
                if account.get("latest_update")
                else "No ledger update yet."
            ),
        ),
        ("Ledger updated", fmt.text(fmt.utc(account.get("latest_update"))) if account.get("latest_update") else "No ledger update yet."),
    ]
    return pairs


def _ledger_section(ctx: Any, account: Mapping[str, Any], *, id: str = "provenance") -> str:
    return common.provenance_section(
        _ledger_facts(ctx, account),
        title="Where these numbers come from",
        note="Every instant is UTC. An age is measured against the read below, not the "
        "clock on the screen. Nothing on this page is a broker balance.",
        id=id,
    )


def _paper_vs_live_notice(ctx: Any, *, extra: str = "") -> str:
    items = []
    if getattr(ctx.terminal, "simulated", False):
        broker_note = (
            "The offline simulator is running, so even the quotes are samples. Its balance "
            "and equity are never read into these figures."
        )
    elif getattr(ctx.terminal, "connected", False):
        broker_note = (
            "A real MetaTrader 5 account is connected for quotes only. Its balance and equity "
            "are never read into these figures."
        )
    else:
        broker_note = (
            "No terminal is connected, so no broker figure is read at all — and none of the "
            "figures here is a broker balance."
        )
    items.append(notice("REAL BROKER", broker_note, tone="muted"))
    items.append(
        notice(
            "PAPER",
            "Every order, fill, position and P&amp;L on this page is simulated on this "
            "machine. No order is sent and no broker capital is used."
            + (f" {extra}" if extra else ""),
            tone="info",
        )
    )
    return "".join(items)


# --------------------------------------------------------------------- health
_CHECK_LABELS = {
    "research_only_boundary": "Research-only boundary holds",
    "live_bridge_disabled": "Live bridge is off",
    "data_quality": "Market data passes the quality gate",
    "market_open": "The market session is open",
    "quote_freshness": "The last quote is fresh",
    "last_gate_decision": "The last pre-trade gate decision",
    "unresolved_orders": "No order is left unresolved",
    "persistence": "The database schema matches this build",
    "provider_broker_conformance": "The broker contract conforms",
    "provider_profile_onboarding": "Profile onboarding allows activation",
}


def _check_label(name: str) -> str:
    return _CHECK_LABELS.get(name, name.replace("_", " ").capitalize())


def render_health(ctx: Any) -> str:
    checks = tuple(ctx.paper.health_checks)
    failing = [(str(name), str(detail)) for name, ok, detail in checks if not ok]
    passing = len(checks) - len(failing)
    by_name = {str(name): (bool(ok), str(detail)) for name, ok, detail in checks}
    account = common.account_view(ctx)
    read_at = account.get("read_at") if account.get("available") else ""
    unresolved = by_name.get("unresolved_orders", (None, ""))[0]
    bridge = by_name.get("live_bridge_disabled", (None, ""))[0]
    healthy = str(ctx.paper.health_status or "UNKNOWN")

    rows = [
        [
            cell(
                f"{fmt.text(_check_label(str(name)))}"
                f"<br><span class='mono small muted'>{fmt.text(name)}</span>",
                label="Check",
            ),
            cell(chip("OK", "ok") if ok else chip("Attention", "warn"), label="Status"),
            cell(fmt.text(detail), label="Detail"),
        ]
        for name, ok, detail in checks
    ]

    head = page_head(
        "System health · local checks",
        "System health",
        "Read from the paper platform's own status. A check that cannot be read is "
        "reported as not reported, never as passing.",
        badges=chip(healthy, "ok" if healthy == "HEALTHY" else "warn", dot=True)
        + chip("PAPER · READ-ONLY", "paper", dot=True)
        + common.data_chip(ctx.paper_label),
        actions=button_link("/live", "Broker facts are on /live", small=True),
    )

    attention = ""
    if failing:
        items = "".join(
            f"<li><strong>{fmt.text(_check_label(name))}</strong> "
            f"<span class='mono small muted'>{fmt.text(name)}</span><br>"
            f"<span class='small muted'>{fmt.text(detail)}</span></li>"
            for name, detail in failing
        )
        attention = notice(
            f"{len(failing)} health check(s) need attention",
            "<ul class='stack--sm' style='margin:6px 0 0;padding-left:18px'>"
            f"{items}</ul><p class='small' style='margin:8px 0 0'>Nothing on this page "
            "changes a check or clears one: fixes are made where the check points, and "
            "this page reads the result again.</p>",
            tone="warn",
            role="alert",
        )

    checks_section = section(
        "Paper platform checks",
        table(
            (("Check", False), ("Status", False), ("Detail", False)),
            rows,
            caption="Check · status · the detail the platform reported for it.",
            empty=empty_state("Health unavailable", "No checks were reported."),
        ),
        note="Each row is one check the platform ran, in the order it reported them.",
        tools=chip(healthy, "ok" if healthy == "HEALTHY" else "warn", dot=True),
        id="checks",
        flush=True,
    )

    summary_pairs: list[tuple[str, str]] = [
        (
            "Overall status",
            f"{chip(healthy, 'ok' if healthy == 'HEALTHY' else 'warn', dot=True)} "
            + (
                f"<span class='small muted'>{passing} of {len(checks)} checks pass</span>"
                if checks
                else "<span class='small muted'>the platform reported no checks</span>"
            ),
        ),
        (
            "Checks needing attention",
            (
                f"{chip(f'{len(failing)} failing', 'warn', dot=True)} "
                f"<span class='small muted'>{fmt.text(', '.join(_check_label(n) for n, _ in failing[:6]))}</span>"
                if failing
                else chip("none", "ok", dot=True)
            ),
        ),
        (
            "Unresolved orders",
            (
                chip("none unresolved", "ok", dot=True)
                if unresolved is True
                else chip("attention", "warn", dot=True)
                if unresolved is False
                else chip("not reported", "muted")
            ),
        ),
        (
            "LIVE bridge",
            (
                chip("off", "ok", dot=True)
                if bridge is True
                else chip("check failing", "warn", dot=True)
                if bridge is False
                else chip("not reported", "muted")
            ),
        ),
        (
            "Paper platform data class",
            f"{common.data_chip(ctx.paper_label)} "
            f"<span class='small muted'>{fmt.text(ctx.paper.source or 'no source reported')}</span>",
        ),
        ("Platform read at", fmt.text(fmt.utc(ctx.paper.read_at))),
        (
            "Paper ledger read at",
            (
                fmt.text(fmt.utc(read_at))
                + (
                    f" <span class='small muted'>· updated {fmt.text(fmt.age(account.get('latest_update'), read_at))}</span>"
                    if account.get("latest_update")
                    else " <span class='small muted'>· no ledger update yet</span>"
                )
                if account.get("available")
                else " <span class='small muted'>· no ledger could be read</span>"
            ),
        ),
        (
            "Quotes",
            common_quote_chip(account) if account.get("available") else chip("no ledger read", "muted"),
        ),
        ("Broker account", BROKER_NOT_READ),
    ]

    aside = (
        section(
            "Summary",
            facts(summary_pairs),
            note="Read from the paper platform's own status.",
            id="summary",
            plain=True,
        )
        + common.boundaries_section(
            [
                ("Live trading", chip("disabled", "ok")),
                ("Broker mode", chip("local paper", "paper")),
                ("External broker", "not enabled from this UI"),
                ("Order entry", "not available"),
                ("Arming", "not available"),
                ("Market access token", "not used on the MT5 path"),
            ]
        )
        + common.provenance_section(
            [
                ("Paper platform", f"{common.data_chip(ctx.paper_label)} read at {fmt.text(fmt.utc(ctx.paper.read_at))}"),
                ("Paper platform data source", fmt.text(ctx.paper.source or "not reported")),
                ("Checks reported", fmt.text(f"{len(checks)} ({passing} OK, {len(failing)} attention)")),
            ],
            title="Where these checks and timestamps come from",
        )
    )

    body = head + layout(
        checks_section
        + attention
        + common.safety_section()
        + notice(
            "Broker answers are on /live",
            "Broker-reported positions, LIVE arming and the broker's own answers are on "
            "<a href='/live'>/live</a>. This page reads the paper platform and never "
            "connects to a broker.",
            tone="muted",
        ),
        aside,
    )
    return body


def common_quote_chip(account: Mapping[str, Any]) -> str:
    return common.quote_chip(account)


# ------------------------------------------------------------------- activity
def render_activity(ctx: Any) -> str:
    account = common.account_view(ctx)
    page = common.query_page(ctx)
    events = list(account.get("events") or ())
    shown = common.page_of(events, page, PER_PAGE)
    total = int(account.get("events_total") or 0)
    truncated = int(account.get("events_truncated") or 0)
    read_at = account.get("read_at")
    newest = str(account.get("latest_event_at") or "")
    attention = [event for event in shown if _event_label(str(event.get("kind") or ""))[1] == "warn"]
    audit = list(ctx.paper.activity)
    session = dict(account.get("session") or {})

    event_rows = [
        [
            cell(fmt.text(fmt.utc(event.get("at"))), label="When (UTC)", mono=True),
            cell(chip(*_event_label(str(event.get("kind") or ""))), label="Event"),
            cell(fmt.text(event.get("symbol") or "—"), label="Symbol"),
            cell(fmt.text(event.get("reason") or "—"), label="Reason"),
            cell(fmt.text(event.get("detail") or ""), label="Detail"),
        ]
        for event in reversed(shown)
    ]

    audit_rows = [
        [
            cell(fmt.text(str(event.get("recorded_at", ""))[:19]), label="When (UTC)", mono=True),
            cell(fmt.text(str(event.get("event_type", ""))), label="Event"),
            cell(fmt.text(str(event.get("mode", ""))), label="Mode"),
            cell(
                fmt.text(
                    "allowed"
                    if event.get("allowed")
                    else ("refused" if event.get("allowed") is False else "—")
                ),
                label="Outcome",
            ),
        ]
        for event in audit
    ]

    stats_html = stats(
        [
            stat("Events in the ledger", f"{total:,}"),
            stat("Events on this page", f"{len(shown):,}"),
            stat(
                "Older events not paged here",
                chip(f"{truncated:,} on disk", "warn", dot=True) if truncated else chip("0", "ok"),
            ),
            stat(
                "Attention events in view",
                f"{len(attention)} in view" if attention else chip("none in view", "ok"),
            ),
            stat("Platform audit events", f"{len(audit):,}"),
        ]
    )

    truncation = ""
    if truncated:
        truncation = notice(
            f"{truncated:,} older ledger event(s) are on disk but not paged here",
            f"This page keeps the most recent {total - truncated:,} of {total:,} events; "
            "nothing was deleted, and the older ones are still in the ledger file.",
            tone="muted",
        )
    attention_note = ""
    if attention:
        attention_note = notice(
            "Attention events in view",
            f"{len(attention)} attention event(s) in the events shown — a skipped decision, "
            "a stale quote, or a refused resume. They are counted separately from ordinary "
            "fills and signals.",
            tone="warn",
        )

    head = page_head(
        "Paper activity · local ledger",
        "Activity",
        "Newest first. Event kinds that skipped a decision or refused a resume are "
        "attention events and are counted separately; the timestamps are UTC.",
        badges=_lane_chip("PAPER · LOCAL") + common.data_chip(ctx.paper_label),
        actions=button_link("/live", "LIVE decisions are on /live", small=True),
    )

    summary = [
        ("Scope", f"{_lane_chip('PAPER')} <span class='small muted'>ledger events, local machine</span>"),
        ("Events in the ledger", f"{total:,}"),
        ("Events on this page", f"{len(shown):,}"),
        (
            "Older events not paged here",
            chip(f"{truncated:,} on disk", "warn", dot=True) if truncated else chip("0", "ok"),
        ),
        (
            "Newest event",
            (
                f"{fmt.text(fmt.utc(newest))} <span class='small muted'>· {fmt.text(fmt.age(newest, read_at))}</span>"
                if newest
                else "No event is recorded yet."
            ),
        ),
        (
            "Attention events in view",
            f"{len(attention)} in view" if attention else chip("none in view", "ok"),
        ),
        ("Platform audit events", f"{len(audit):,}"),
        ("Paper session", _session_chip(session)),
        (
            "Trailing stop",
            "armed per position from its entry bar; the levels are listed on /positions",
        ),
        ("Quotes", common_quote_chip(account)),
        ("Ledger read at", fmt.text(fmt.utc(read_at))),
        ("Platform read at", fmt.text(fmt.utc(ctx.paper.read_at))),
        ("Broker account", BROKER_NOT_READ),
    ]

    body = head + layout(
        common.paused_notice(account)
        + stats_html
        + truncation
        + attention_note
        + section(
            "Paper ledger timeline",
            table(
                (
                    ("When (UTC)", False),
                    ("Event", False),
                    ("Symbol", False),
                    ("Reason", False),
                    ("Detail", False),
                ),
                event_rows,
                caption="Read from the same ledger file the performance page uses.",
                empty=empty_state(
                    "The paper ledger is empty",
                    "Once the paper session runs, each cycle's decision, stale-quote skip, "
                    "simulated fill and close is written here.",
                ),
            )
            + pager(
                page,
                common.pages_for(events, PER_PAGE),
                lambda number: f"/activity?page={number}",
                noun=f"All {total:,} events shown newest first.",
            ),
            note="Read from the same ledger file the performance page uses.",
            id="timeline",
            flush=True,
            footer=f"Read from the paper platform at {fmt.text(fmt.utc(ctx.paper.read_at))}; "
            f"the ledger above was read at {fmt.text(fmt.utc(read_at))}. "
            f"Source: {fmt.text(ctx.paper.source or 'not reported')}.",
        )
        + section(
            "Platform audit trail",
            table(
                (("When (UTC)", False), ("Event", False), ("Mode", False), ("Outcome", False)),
                audit_rows,
                caption="Sanitized; credential-shaped fields are redacted before they reach this page.",
                dense=True,
                empty=empty_state("No audit events yet", "Run the offline paper workflow to create some."),
            ),
            note="The platform's own record, newest first. It is not a trading history.",
            tools=common.data_chip(_label_only("SAMPLE FIXTURE") if ctx.terminal.simulated else ctx.paper_label),
            id="audit",
            flush=True,
        )
        + runner_page.sections(ctx)
        + _paper_vs_live_notice(ctx, extra="Broker and LIVE decisions are on /live."),
        section("Summary", facts(summary), note="Activity at a glance, qualified in place.", id="summary", plain=True)
        + _ledger_section(ctx, account),
    )
    return body


def _label_only(label: str) -> dict[str, str]:
    return {"label": label, "tone": "fixture", "data_class": label}


# ------------------------------------------------------------------ positions
def _money(value: Any, currency: str = "") -> str:
    if value is None:
        return "—"
    return fmt.money(value, currency)


def render_positions(ctx: Any) -> str:
    account = common.account_view(ctx)
    positions = list(account.get("positions") or ())
    trades = list(account.get("trades") or ())
    counts = dict(account.get("counts") or {})
    session = dict(account.get("session") or {})
    read_at = account.get("read_at")
    integrity = dict(account.get("integrity") or {})
    extra = list(account.get("extra_positions") or ())
    open_count = int(counts.get("open_positions") or 0)
    closed_count = int(counts.get("closed_trades") or 0)
    priced = sum(1 for item in positions if item.get("unrealized_gross") is not None)
    unpriced = open_count - priced
    state_only = sum(1 for item in positions if str(item.get("cross_check")) == "state_only")

    position_rows = [
        [
            cell(fmt.text(item.get("symbol") or ""), label="Symbol"),
            cell(fmt.text(item.get("side") or ""), label="Side"),
            cell(fmt.text(fmt.quantity(item.get("lots"))), label="Lots", num=True),
            cell(fmt.text(fmt.utc(item.get("entry_at"))), label="Opened (UTC)", mono=True),
            cell(fmt.text(fmt.money(price_value(item.get("entry_price")), "")), label="Entry", num=True),
            cell(
                fmt.text(str(item.get("stop")) if item.get("stop") is not None else "—"),
                label="Stop",
                num=True,
            ),
            cell(
                fmt.text(str(item.get("target")) if item.get("target") is not None else "—"),
                label="Target",
                num=True,
            ),
            cell(
                fmt.text(fmt.money(price_value(item.get("trail")), ""))
                + (
                    f" <span class='small muted'>armed after "
                    f"{fmt.text(fmt.utc(item.get('entry_bar_at')))}</span>"
                    if item.get("trail") is not None and item.get("entry_bar_at")
                    else ""
                ),
                label="Trailing stop",
                num=True,
            ),
            cell(
                f"{fmt.text(str(item.get('mark_side') or ''))} "
                f"{fmt.text(fmt.money(price_value(item.get('mark')), ''))}"
                + (
                    ""
                    if item.get("mark_fresh")
                    else f" {chip(str(item.get('mark_age_text') or 'stale'), 'warn')}"
                ),
                label="Mark (closing side)",
                num=True,
            ),
            cell(_money(item.get("unrealized_gross"), str(item.get("currency") or "")), label="Unrealised", num=True),
            cell(fmt.text(str(item.get("unavailable_reason") or "") or "marked"), label="Basis"),
        ]
        for item in positions
    ]

    trade_rows = [
        [
            cell(fmt.text(trade.get("symbol") or ""), label="Symbol"),
            cell(fmt.text(trade.get("side") or ""), label="Side"),
            cell(fmt.text(fmt.quantity(trade.get("lots"))), label="Lots", num=True),
            cell(fmt.text(fmt.utc(trade.get("exit_at"))), label="Closed (UTC)", mono=True),
            cell(fmt.text(fmt.money(price_value(trade.get("entry_price")), "")), label="Entry", num=True),
            cell(fmt.text(fmt.money(price_value(trade.get("exit_price")), "")), label="Exit", num=True),
            cell(_money(trade.get("net"), str(trade.get("currency") or "")), label="Net", num=True),
            cell(fmt.text(trade.get("reason") or ""), label="Close reason"),
        ]
        for trade in sorted(trades, key=lambda item: str(item.get("exit_at") or ""), reverse=True)
    ]

    stale = [
        item
        for item in positions
        if item.get("unrealized_gross") is None and item.get("unavailable_reason")
    ]
    stale_notice = ""
    if stale:
        lines = "".join(
            f"<li><strong>{fmt.text(item.get('symbol') or '')} "
            f"({fmt.text(item.get('mark_age_text') or 'age unknown')})</strong> — "
            f"{fmt.text(item.get('unavailable_reason') or '')}</li>"
            for item in stale
        )
        stale_notice = notice(
            f"{len(stale)} open position(s) cannot be priced from a fresh quote",
            f"<ul class='stack--sm' style='margin:6px 0 0;padding-left:18px'>{lines}</ul>"
            "<p class='small' style='margin:8px 0 0'>Nothing is zeroed and no figure is "
            "invented for them: the unrealised total counts only the positions that could "
            "be marked.</p>",
            tone="warn",
        )

    stored_notice = ""
    if extra:
        lines = "".join(
            f"<li><strong>{fmt.text(item.get('symbol') or '')}</strong> — "
            f"{fmt.text(item.get('reason') or 'the position was not paired with a ledger entry fill')}</li>"
            for item in extra
        )
        stored_notice = notice(
            f"{len(extra)} stored position(s) are not paired with a ledger entry fill",
            "Positions restored from the state file without a ledger entry fill are shown "
            "here and are never counted as open paper positions, so no figure is invented "
            f"for them.<ul class='stack--sm' style='margin:6px 0 0;padding-left:18px'>{lines}</ul>",
            tone="warn",
        )

    runner = dict(getattr(ctx, "runner", {}) or {})

    head = page_head(
        "Paper positions · local book",
        "Paper positions",
        "Open positions are priced at the side they would close on. A position with no "
        "fresh quote keeps an unavailable figure and its reason; nothing is zeroed.",
        badges=_lane_chip("PAPER · LOCAL") + _session_chip(session) + common.data_chip(ctx.paper_label),
        actions=button_link("/autopilot", "Run this in Autopilot"),
    )

    symbol_count = len({str(item.get("symbol") or "") for item in positions if item.get("symbol")})
    open_line = (
        f"{open_count:,} open across {symbol_count:,} symbol(s)"
        if open_count
        else "Nothing open"
    )
    summary = [
        ("Scope", f"{_lane_chip('PAPER')} <span class='small muted'>simulated fills, local ledger</span>"),
        ("Open positions", f"{open_line} · {priced:,} priced"),
        ("Marked from a fresh quote", f"{priced:,} of {open_count:,}"),
        ("Unavailable marks", f"{unpriced:,}"),
        (
            "State-only positions",
            f"{state_only:,}"
            + (f" · {len(extra):,} unreconciled" if extra else " · 0 unreconciled"),
        ),
        ("Stored book", "no recovery report" if not integrity.get("recovery") else fmt.text(str(integrity.get("recovery")))),
        ("Closed trades", f"{closed_count:,}"),
        ("Unrealised P&L (price)", _metric_or_zero(account, "unrealized_gross", untouched=(not open_count and not closed_count), empty_note="nothing is open, so this is zero rather than a result")),
        ("Net P&L after costs", _metric_or_zero(account, "net_pnl", untouched=(not open_count and not closed_count))),
        ("Paper equity", _metric_or_zero(account, "equity", untouched=False)),
        (
            "Trailing stop",
            "armed per position from its entry bar; the levels are listed on /positions",
        ),
        ("Quotes", common_quote_chip(account)),
        ("Ledger updated", (
            f"{fmt.text(fmt.utc(account.get('latest_update')))} "
            f"<span class='small muted'>· {fmt.text(fmt.age(account.get('latest_update'), read_at))}</span>"
            if account.get("latest_update") else "No ledger update yet."
        )),
        ("Read at", fmt.text(fmt.utc(read_at))),
        ("Broker account", BROKER_NOT_READ),
    ]

    runner_section = runner_page.sections(ctx)

    body = head + layout(
        common.paused_notice(account)
        + stats(
            [
                stat("Open positions", f"{open_count:,} · {priced:,} priced"),
                stat("Marked from a fresh quote", f"{priced:,} of {open_count:,}"),
                stat(
                    "Unavailable marks",
                    f"{unpriced:,}"
                    + ("" if unpriced else " · nothing open" if not open_count else ""),
                    tone="warn" if unpriced else "",
                ),
                stat("State-only positions", f"{state_only:,}"),
                stat("Closed trades", f"{closed_count:,}"),
            ]
        )
        + stale_notice
        + stored_notice
        + section(
            "Open paper positions",
            table(
                (
                    ("Symbol", False),
                    ("Side", False),
                    ("Lots", True),
                    ("Opened (UTC)", False),
                    ("Entry", True),
                    ("Stop", True),
                    ("Target", True),
                    ("Trailing stop", True),
                    ("Mark (closing side)", True),
                    ("Unrealised", True),
                    ("Basis", False),
                ),
                position_rows,
                caption="Priced at the side each position would close on: longs at the bid, "
                "shorts at the ask.",
                empty=empty_state(
                    "No open paper positions",
                    "The paper ledger is flat. A position appears here only after a simulated "
                    "fill; this screen cannot open, change or close one.",
                ),
            ),
            note="Every row is a simulated fill from the local ledger.",
            tools=chip(open_line, "paper"),
            id="open",
            flush=True,
            footer=f"All {open_count:,} open positions shown.",
        )
        + section(
            "Closed paper trades",
            table(
                (
                    ("Symbol", False),
                    ("Side", False),
                    ("Lots", True),
                    ("Closed (UTC)", False),
                    ("Entry", True),
                    ("Exit", True),
                    ("Net", True),
                    ("Close reason", False),
                ),
                trade_rows,
                caption="Every close event in the ledger, newest first. Simulated fills only.",
                empty=empty_state(
                    "No closed paper trades yet",
                    "A trade appears here only after the paper test has opened and closed "
                    "one. Nothing is simulated for display.",
                ),
            ),
            note="Simulated fills only; the broker's own positions are on /live.",
            tools=chip(f"{closed_count:,} trade(s)", "paper"),
            id="closed",
            flush=True,
            footer=f"All {closed_count:,} closed trades shown.",
        )
        + runner_section
        + _recovery_section(ctx, account, open_count=open_count),
        section("Summary", facts(summary), note="Positions at a glance, qualified in place.", id="summary", plain=True)
        + _paper_vs_live_notice(ctx, extra="Broker-reported positions are on /live. Nothing on this page can open, change or close a broker position.")
        + _ledger_section(ctx, account),
    )
    return body


def price_value(value: Any) -> Any:
    if value is None or str(value).strip() == "":
        return None
    return value


def common_blockers(items: Sequence[Mapping[str, Any]]) -> str:
    if not items:
        return chip("none", "ok")
    return "<ul class='stack--sm' style='margin:0;padding-left:16px'>" + "".join(
        f"<li>{fmt.text(item.get('message') or item.get('reason') or '')}</li>"
        for item in items
    ) + "</ul>"


def _close_all_paper_form(ctx: Any) -> str:
    """The explicit, confirmed, simulated close of the whole paper book."""

    tick = (
        "<label class='field field--check' for='close-all-confirm'>"
        "<input type='checkbox' id='close-all-confirm' name='confirm' "
        "value='close-all-paper-positions' required>"
        "<span class='label'>I understand every open paper position is closed as a "
        "<strong>simulated</strong> fill at the current quote, that this cannot be undone, "
        "that no order is sent to a broker, and that no MetaTrader 5 position is touched."
        "</span></label>"
    )
    return (
        "<form method='post' action='/actions/catalog-paper-close-all' class='form'>"
        f"<input type='hidden' name='nonce' value='{fmt.attr(ctx.action_nonce)}'>"
        "<input type='hidden' name='return_to' value='/positions'>"
        f"{tick}"
        "<div class='actions'><button class='btn btn--danger' type='submit'>"
        "Simulate closing all paper positions</button></div></form>"
    )


def _recovery_section(ctx: Any, account: Mapping[str, Any], *, open_count: int) -> str:
    """What can be said, and what can be done, about a stored paper book."""

    if not open_count:
        return _close_all_section(ctx, open_count=open_count)
    integrity = dict(account.get("integrity") or {})
    session = dict(account.get("session") or {})
    recovery = dict(integrity.get("recovery") or session.get("recovery") or {})
    form = _close_all_paper_form(ctx)
    tail = (
        "<p class='small muted' style='margin:8px 0 0'>Simulated paper accounting only: no "
        "order is sent to a broker, no MetaTrader 5 position is touched, and the execution "
        "mode does not change. Once the book is flat you can retry starting the paper "
        "autopilot.</p>"
    )
    if recovery.get("ok") is True:
        return section(
            "Stored paper book",
            notice(
                "These positions can be resumed exactly",
                f"{open_count:,} stored position(s) match the rows the paper test would manage "
                "and carry every field its exit rules read, so there is nothing to close: start "
                "the paper autopilot to carry on. Nothing needs flattening.",
                tone="ok",
            ),
            note="A close is offered only when the paper test cannot prove it can resume the book.",
            id="recovery",
        )
    if not recovery:
        return section(
            "Stored paper book",
            notice(
                "This page cannot prove these positions can be resumed",
                f"{open_count:,} open position(s) are in the paper ledger, but the state file "
                "carries no recovery report, so there is nothing here that says the paper test "
                "could manage them again. Starting it is the honest next step: if it refuses, "
                "the reason is shown on this page. Closing them is the other way out, and it is "
                "all-or-nothing.",
                tone="warn",
            )
            + form
            + tail,
            note="Nothing is closed unless every position can be priced and closed in full.",
            id="recovery",
        )
    failed = [item for item in recovery.get("failed") or () if isinstance(item, Mapping)]
    names = ", ".join(
        f"{fmt.text(item.get('symbol') or item.get('key') or 'a position')} "
        f"({fmt.text(item.get('reason') or 'incomplete')})"
        for item in failed[:4]
    )
    if len(failed) > 4:
        names += f", and {len(failed) - 4} more"
    detail = "The paper test refused to resume this book"
    if recovery.get("detail"):
        detail += f": {fmt.text(str(recovery.get('detail')))}"
    if names:
        detail += f". Affected: {names}"
    detail += (
        ". It will not carry a position with a substituted value, and it will not attach a "
        "stored position to a different strategy row."
    )
    return section(
        "Stored paper book",
        notice(
            "This stored paper book has to be closed before the paper test can start",
            f"{detail}<p class='small' style='margin:6px 0'>Closing is the documented way out: "
            "each position is closed once, at its own closing side, from a fresh quote, and "
            "written to the same paper ledger as any other exit. If even one quote is stale or "
            "missing, or one position is missing a field a close needs, <strong>nothing is "
            "closed at all</strong> and the reason is shown here.</p>",
            tone="warn",
        )
        + form
        + tail,
        note="Nothing is closed unless every position can be priced and closed in full.",
        id="recovery",
    )


def _close_all_section(ctx: Any, *, open_count: int) -> str:
    """The one confirmed write these pages own: a simulated close of the book."""

    if not open_count:
        return section(
            "Simulated paper accounting only",
            empty_state(
                "Nothing to close",
                "No paper position is open. Closing the paper book writes simulated accounting "
                "events; no MetaTrader 5 position is touched and no order is sent.",
            ),
            note="Simulated paper accounting only — no MetaTrader 5 position is touched.",
            id="close-all",
        )
    tick = (
        "<label class='field field--check' for='close-all-confirm'>"
        "<input type='checkbox' id='close-all-confirm' name='confirm' "
        "value='close-all-paper-positions' required>"
        "<span class='label'>I understand every open paper position is closed as a "
        "<strong>simulated</strong> fill at the current quote, that this cannot be undone, "
        "that no broker order is sent, and that no MetaTrader 5 position is touched."
        "</span></label>"
    )
    form = (
        "<form method='post' action='/actions/catalog-paper-close-all' class='form'>"
        f"<input type='hidden' name='nonce' value='{fmt.attr(ctx.action_nonce)}'>"
        "<input type='hidden' name='return_to' value='/positions'>"
        f"{tick}"
        "<div class='actions'><button class='btn btn--danger' type='submit'>"
        "Simulate closing all paper positions</button></div></form>"
    )
    return section(
        "Simulated paper accounting only",
        form,
        note="This closes the local paper book at the current quote. It is accounting, not "
        "trading: no order is sent and no MetaTrader 5 position is touched.",
        tools=chip(f"{open_count:,} open", "warn"),
        id="close-all",
    )


def _metric_or_zero(
    account: Mapping[str, Any],
    key: str,
    *,
    untouched: bool,
    empty_note: str = "nothing has traded yet, so this is zero rather than a result",
) -> str:
    metric = dict((account.get("metric_by_key") or {}).get(key) or {})
    value = str(metric.get("value") or fmt.UNKNOWN)
    if not metric.get("available"):
        return (
            f"Unavailable <span class='small muted'>· "
            f"{fmt.text(str(metric.get('note') or ''))}</span>"
        )
    if untouched:
        return f"{fmt.text(value)} <span class='small muted'>· {empty_note}</span>"
    return fmt.text(value)


# ---------------------------------------------------------------- performance
def render_performance(ctx: Any) -> str:
    account = common.account_view(ctx)
    capital = dict(account.get("capital") or {})
    currency = str(account.get("currency") or "")
    session = dict(account.get("session") or {})
    counts = dict(account.get("counts") or {})
    metrics = list(account.get("metrics") or [])
    by_key = dict(account.get("metric_by_key") or {})
    positions = list(account.get("positions") or ())
    trades = list(account.get("trades") or ())
    read_at = account.get("read_at")
    trade_page = common.query_page(ctx, "trades")
    shown_trades = sorted(trades, key=lambda item: str(item.get("exit_at") or ""), reverse=True)
    shown_trades = common.page_of(shown_trades, trade_page, TRADE_PAGE)
    open_count = int(counts.get("open_positions") or 0)
    closed_count = int(counts.get("closed_trades") or 0)
    priced = sum(1 for item in positions if item.get("unrealized_gross") is not None)
    untouched = not open_count and not closed_count

    def value_of(key: str) -> tuple[str, bool, str]:
        metric = dict(by_key.get(key) or {})
        return (
            str(metric.get("value") or fmt.UNKNOWN),
            bool(metric.get("available")),
            str(metric.get("note") or ""),
        )

    def or_reason(key: str) -> str:
        value, available, note = value_of(key)
        if not available:
            return f"Unavailable <span class='small muted'>· {fmt.text(note)}</span>"
        if untouched:
            return (
                f"{fmt.text(value)} <span class='small muted'>· nothing has traded yet, so "
                "this is zero rather than a result</span>"
            )
        return fmt.text(value)

    money_keys = (
        "starting_capital",
        "cash",
        "open_value",
        "equity",
        "realized_gross",
        "realized_net",
        "unrealized_gross",
        "net_pnl",
        "return_pct",
    )
    money_metrics = [dict(by_key.get(key) or {}) for key in money_keys if key in by_key]
    count_metrics = [
        dict(by_key.get(key) or {})
        for key in ("orders", "fills", "open_positions", "closed_trades")
        if key in by_key
    ]

    curve = list(account.get("equity_curve") or ())
    scale = float(account.get("equity_curve_scale") or 0.0)
    curve_html = ""
    if curve:
        rows = "".join(
            "<li style='display:grid;grid-template-columns:110px minmax(0,1fr) 120px;"
            "gap:12px;align-items:center;padding:4px 0'>"
            f"<span class='mono small muted'>{fmt.text(point.get('date') or '')}</span>"
            f"<span class='meter{' meter--danger' if float(point.get('realized_gross') or 0) < 0 else ' meter--ok'}'>"
            f"<span style='width:{(abs(float(point.get('realized_gross') or 0.0)) / scale * 100) if scale else 0:.0f}%'></span></span>"
            f"<span class='mono small'>{fmt.text(fmt.money(round(float(point.get('realized_gross') or 0.0), 2), currency))}</span></li>"
            for point in curve
        )
        curve_html = (
            f"<ol class='events' style='list-style:none;margin:0;padding:0'>{rows}</ol>"
            f"<p class='small muted' style='margin:8px 0 0'>One point per UTC day that closed "
            f"a paper trade, drawn against the largest daily move ({fmt.text(fmt.money(round(scale, 2), currency))}).</p>"
        )
    else:
        curve_html = empty_state(
            "No dated performance yet",
            "The curve appears after the first trade closes. A flat line drawn before the "
            "first trade would be a made-up result.",
        )

    trade_rows = [
        [
            cell(fmt.text(trade.get("symbol") or ""), label="Symbol"),
            cell(fmt.text(trade.get("side") or ""), label="Side"),
            cell(fmt.text(fmt.quantity(trade.get("lots"))), label="Lots", num=True),
            cell(fmt.text(fmt.utc(trade.get("exit_at"))), label="Closed (UTC)", mono=True),
            cell(fmt.text(fmt.money(price_value(trade.get("entry_price")), "")), label="Entry", num=True),
            cell(fmt.text(fmt.money(price_value(trade.get("exit_price")), "")), label="Exit", num=True),
            cell(_money(trade.get("net"), str(trade.get("currency") or "")), label="Net", num=True),
            cell(fmt.text(trade.get("exit_basis") or ""), label="Exit basis"),
        ]
        for trade in shown_trades
    ]

    capital_form = ""
    if not capital.get("configured"):
        options = [(code, code) for code in CURRENCIES]
        capital_form = section(
            "Set the paper starting capital",
            "<p class='small'>This is the amount the local simulation starts with. It is not "
            "your broker balance, it is not read from your account, and it changes nothing "
            "about the terminal: no order is sent and no broker capital is used. Without it, "
            "cash, equity and return cannot be computed, and AXIOM will not invent one.</p>"
            "<form method='post' action='/actions/paper-capital' class='form' "
            "style='margin-top:12px'>"
            f"<input type='hidden' name='nonce' value='{fmt.attr(ctx.action_nonce)}'>"
            "<div class='form-row'>"
            + field(
                "amount",
                "Paper starting capital",
                hint="A positive number, no currency symbol.",
                placeholder="e.g. 10000",
                required=True,
            )
            + field("currency", "Currency", kind="select", options=options, hint="The capital's own currency.")
            + "</div>"
            "<div class='actions'><button class='btn btn--primary' type='submit'>"
            "Save paper capital</button>"
            "<span class='small muted'>One local, Git-ignored document. No credential, no "
            "account number, no order.</span></div></form>"
            + (
                notice("The stored paper capital could not be used", fmt.text(capital.get("reason") or ""), tone="warn")
                if capital.get("problem")
                else ""
            ),
            note="One local, Git-ignored document. No credential, no account number, no order.",
            tools=chip("NOT SET", "warn", dot=True),
            id="capital",
        )
    else:
        capital_form = section(
            "Paper starting capital",
            facts(
                [
                    ("Configured", chip("SET", "ok", dot=True)),
                    ("Amount", fmt.text(f"{capital.get('amount', '')} {capital.get('currency', '')}".strip())),
                    ("Set at", fmt.text(fmt.utc(capital.get("set_at")))),
                    ("Source", fmt.text(capital.get("source") or "operator (paper-capital.local.json)")),
                    ("Broker account", BROKER_NOT_READ),
                ]
            ),
            note="The local simulation's own basis. It is never read from the broker account.",
            id="capital",
        )

    head = page_head(
        "Paper performance · local ledger",
        "Paper performance",
        f"Source: {fmt.text(account.get('source') or 'durable paper ledger')} · "
        f"read {fmt.text(fmt.utc(read_at))} · last ledger update "
        f"{fmt.text(fmt.utc(account.get('latest_update')))}",
        badges=_lane_chip("PAPER · LOCAL")
        + _session_chip(session)
        + common.data_chip(ctx.paper_label),
        actions=button_link("/autopilot", "Open Autopilot"),
    )

    summary = [
        ("Scope", f"{_lane_chip('PAPER')} <span class='small muted'>local simulated ledger</span>"),
        (
            "Session",
            f"{chip('RUNNING', 'paper', dot=True)} <span class='small muted'>{fmt.text(str(session.get('status') or 'paper session'))}</span>"
            if session.get("running")
            else f"{chip('NOT RUNNING', 'muted')} <span class='small muted'>{fmt.text(str(session.get('reason') or session.get('status') or 'no paper session has run'))}</span>",
        ),
        ("Open positions", f"{open_count:,} <span class='small muted'>· {priced:,} priced</span>"),
        ("Closed trades", f"{closed_count:,}"),
        ("Paper equity", or_reason("equity")),
        ("Net P&L after costs", or_reason("net_pnl")),
        ("Realised P&L (price)", or_reason("realized_gross")),
        ("Unrealised P&L (price)", or_reason("unrealized_gross")),
        (
            "Marks",
            chip(f"{priced}/{open_count} priced", "ok", dot=True)
            if open_count and priced == open_count
            else chip(f"{open_count - priced} unpriced", "warn", dot=True)
            if open_count
            else chip("nothing open", "muted"),
        ),
        (
            "Trailing stop",
            "armed per position from its entry bar; the levels are listed on /positions",
        ),
        ("Quotes", common_quote_chip(account)),
        (
            "Ledger updated",
            f"{fmt.text(fmt.utc(account.get('latest_update')))} "
            f"<span class='small muted'>· {fmt.text(fmt.age(account.get('latest_update'), read_at))}</span>"
            if account.get("latest_update")
            else "No ledger update yet.",
        ),
        ("Read at", fmt.text(fmt.utc(read_at))),
        ("Broker account", BROKER_NOT_READ),
    ]

    integrity = dict(account.get("integrity") or {})
    integrity_html = ""
    if integrity and (integrity.get("ok") is False or integrity.get("warnings")):
        warnings = "".join(
            f"<li>{fmt.text(str(item))}</li>" for item in (integrity.get("warnings") or ())
        )
        integrity_html = notice(
            "Data integrity warning — figures that depend on this are unavailable",
            f"<ul class='stack--sm' style='margin:6px 0 0;padding-left:18px'>{warnings}</ul>"
            if warnings
            else "The ledger reported a problem; the figures it affects are shown as unavailable "
            "rather than guessed.",
            tone="warn",
        )

    quotes_html = section(
        "Quote freshness",
        facts(common.quote_facts(account)),
        note="Freshness is a verdict about now: the same ledger reads paused once its newest "
        "quote passes the allowance.",
        tools=common.quotes_chip(account),
        id="quotes",
        plain=True,
    )

    body = head + layout(
        common.paused_notice(account)
        + integrity_html
        + stats(
            [
                stat("Paper equity", or_reason("equity"), tone="paper", small=True),
                stat("Net P&L after costs", or_reason("net_pnl"), tone="paper", small=True),
                stat("Realised P&L (price)", or_reason("realized_gross"), small=True),
                stat("Unrealised P&L (price)", or_reason("unrealized_gross"), small=True),
            ]
        )
        + section(
            "Capital and P&L (paper only)",
            common.metrics_table(
                money_metrics,
                caption=f"Local simulated capital in {fmt.text(currency or 'an unset currency')}. "
                "Each basis column is the accounting layer's own note for that figure.",
            ),
            note="The connected broker account's balance and equity are never read into these "
            "figures: every number here is a local simulated figure, not a broker balance.",
            tools=chip("PAPER", "paper", dot=True),
            id="capital-metrics",
            flush=True,
        )
        + section(
            "Counts",
            common.metrics_table(count_metrics, caption="Orders, fills and book size."),
            note="A simulated order fills where it is priced; the fill count is the order count.",
            id="counts",
            flush=True,
        )
        + section(
            "Equity and P&L over time",
            curve_html,
            note="One point per UTC day that closed a paper trade.",
            id="curve",
        )
        + capital_form
        + section(
            "Trade history",
            table(
                (
                    ("Symbol", False),
                    ("Side", False),
                    ("Lots", True),
                    ("Closed (UTC)", False),
                    ("Entry", True),
                    ("Exit", True),
                    ("Net", True),
                    ("Exit basis", False),
                ),
                trade_rows,
                caption="Closed paper trades, newest first. Every figure is simulated.",
                empty=empty_state(
                    "No closed paper trades yet",
                    "A trade appears here only after the paper test has opened and closed one.",
                ),
            )
            + pager(
                trade_page,
                common.pages_for(trades, TRADE_PAGE),
                lambda number: f"/performance?trades={number}",
                noun=f"All {closed_count:,} closed trades.",
            ),
            note="Entry and exit are the ledger's own prices; net includes commission and swap where they are known.",
            id="trades",
            flush=True,
        )
        + section(
            "Open book",
            facts(
                [
                    ("Open positions", f"{open_count:,} open"),
                    ("Closed trades", f"{closed_count:,} trade(s)"),
                    ("Quotes", common.quotes_chip(account)),
                ]
            ),
            note="The same counts the positions page renders.",
            tools=chip(f"{open_count:,} open", "paper"),
            id="open-book",
            plain=True,
        )
        + quotes_html
        + section("Summary", facts(summary), note="Performance at a glance, qualified in place.", id="summary", plain=True)
        + _paper_vs_live_notice(
            ctx,
            extra="Broker-reported positions and P&amp;L are on /live; PAPER workflows and the paper book are on /autopilot.",
        )
        + _ledger_section(ctx, account),
    )
    return body
