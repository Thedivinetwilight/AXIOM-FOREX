"""The frame every page is served inside: rail, status strip, banners, footer.

The old frame was a dark sidebar plus a top bar that repeated four coloured chips
on every route.  This one puts navigation in a horizontal command rail and moves
the facts that *change on their own* into one status strip, so the top of the page
answers "what am I looking at, and is anything live?" before any prose starts.

The three honesty rules the old shell enforced still hold, in the same words:

* a simulator is never described as connected, and its banner says what was not
  read and what it can never do;
* an action's outcome is a separate notice from the sample-data warning, so a
  refusal can never be swallowed by a mode;
* every page carries the data-label legend and the moment it was generated.
"""

from __future__ import annotations

from html import escape
from typing import Any, Mapping, Sequence

from . import format as fmt
from .components import chip, chip_row, notice, notice_stack, action_form
from .theme import STYLES

APP_NAME = "AXIOM-FOREX"
APP_SUBTITLE = "trading workstation"

#: Navigation groups, in reading order.  ``live`` sits next to ``autopilot`` on
#: purpose: they are the two ways a session can run, and the rail says which one
#: this page belongs to before its title does.
NAV_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Run", ("autopilot", "live")),
    ("Monitor", ("performance", "positions", "activity", "health")),
    ("Set up", ("start", "account", "data", "strategies")),
    ("Advanced", ("mode", "quick-setup", "provider-setup", "kronos", "setup", "overview")),
)

#: Flat ``(page, href, label)`` table, kept because other modules and tests read
#: it to prove every route is reachable from the navigation.
NAV: tuple[tuple[str, str, str], ...] = (
    ("autopilot", "/autopilot", "Autopilot"),
    ("live", "/live", "Live execution"),
    ("performance", "/performance", "Performance"),
    ("positions", "/positions", "Positions"),
    ("activity", "/activity", "Activity"),
    ("health", "/health", "Health"),
    ("start", "/", "Start paper trading"),
    ("account", "/account", "Account & symbols"),
    ("data", "/data", "Data & evaluation"),
    ("strategies", "/strategies", "Strategy library"),
    ("mode", "/mode", "Trading mode"),
    ("quick-setup", "/quick-setup", "Quick setup"),
    ("provider-setup", "/provider-setup", "Broker terms"),
    ("kronos", "/kronos", "Kronos forecast"),
    ("setup", "/setup", "Setup steps"),
    ("overview", "/overview", "Overview (classic)"),
)

PAGE_PATHS: dict[str, str] = {page: href for page, href, _label in NAV}

#: The lane a page belongs to.  One table, read by the whole UI, so a scope label
#: and the machine-readable ``data-scope`` can never drift apart.
PAGE_SCOPES: dict[str, tuple[str, str, str, str]] = {
    "autopilot": (
        "paper-command",
        "PAPER COMMAND CENTER",
        "paper",
        "Simulated fills on this machine; LIVE review and arming are on /live.",
    ),
    "live": (
        "live-execution",
        "LIVE · REAL BROKER",
        "danger",
        "Real MetaTrader 5 account. Nothing here is a simulated fill.",
    ),
    "mode": (
        "execution-mode",
        "EXECUTION MODE",
        "warn",
        "Choose PAPER or LIVE; choosing alone sends nothing.",
    ),
    "start": (
        "paper-guided",
        "PAPER GUIDED FLOW",
        "paper",
        "Five steps to a locally simulated paper session.",
    ),
    "performance": ("paper-detail", "PAPER PERFORMANCE", "paper", "Local ledger only."),
    "positions": ("paper-detail", "PAPER POSITIONS", "paper", "Locally simulated book."),
    "activity": ("paper-detail", "PAPER ACTIVITY", "paper", "Local paper audit events."),
    "overview": ("observation", "OBSERVATION", "muted", "Terminal and paper facts, kept separate."),
    "setup": ("observation", "OBSERVATION SETUP", "muted", "Read-only terminal connection."),
    "account": ("observation", "ACCOUNT & SYMBOLS", "muted", "Read-only terminal facts."),
    "data": ("research", "RESEARCH DATA", "fixture", "Imported or sample research data."),
    "quick-setup": ("research", "RESEARCH SETUP", "fixture", "Local evaluation data."),
    "provider-setup": ("research", "BROKER TERMS", "fixture", "Local document, validated before saving."),
    "strategies": ("research", "STRATEGY LIBRARY", "fixture", "Local evaluation evidence."),
    "kronos": ("research", "FORECAST RESEARCH", "fixture", "Optional forecast; never a trading authority."),
    "health": ("system", "SYSTEM HEALTH", "muted", "Local process and data checks."),
}

_DEFAULT_SCOPE = ("observation", "OBSERVATION", "muted", "Read-only view.")


def page_scope(ctx: Any) -> tuple[str, str, str, str]:
    return PAGE_SCOPES.get(str(getattr(ctx, "page", "")), _DEFAULT_SCOPE)


# --------------------------------------------------------------------- chips
def connection_chip(ctx: Any) -> str:
    """Connection state, worded once.  A simulator is never "connected"."""

    terminal = ctx.terminal
    if terminal.state == "CONNECTING":
        return chip("Connecting", "warn", dot=True)
    if terminal.state == "ERROR":
        return chip("Connection error", "danger", dot=True)
    if terminal.simulated:
        if terminal.connected:
            return chip("Simulator · not MT5", "simulated", dot=True)
        return chip("Simulator · not started", "simulated", dot=True)
    if not terminal.connected:
        return chip("Disconnected", "muted", dot=True)
    mode = terminal.mode if terminal.mode_confirmed else f"{terminal.mode}?"
    return chip(f"Connected · {mode}", "live", dot=True)


def execution_mode_chip(ctx: Any) -> str:
    mode = str((ctx.execution_mode or {}).get("mode", "PAPER")).upper()
    if mode == "LIVE":
        if ctx.live_effective:
            return chip("LIVE · MT5 execution", "danger", dot=True)
        return chip("LIVE · not confirmed", "warn", dot=True)
    return chip("PAPER · simulated fills", "paper", dot=True)


def account_mode_chip(ctx: Any) -> str:
    terminal = ctx.terminal
    if not terminal.connected:
        return chip("No account mode", "muted")
    if terminal.simulated:
        return chip(f"Sample mode: {terminal.mode}", "simulated")
    if not terminal.mode_confirmed:
        return chip(f"Account mode: {terminal.mode} (unconfirmed)", "warn")
    tone = "ok" if terminal.mode == "DEMO" else "warn"
    return chip(f"Account mode: {terminal.mode}", tone)


def paper_loop_chip(ctx: Any) -> str:
    """The paper loop's own state, so no page has to be revisited to learn it."""

    summary = dict((ctx.extra or {}).get("autopilot_state") or {})
    if not summary or summary.get("available") is False:
        return ""
    state = str(summary.get("state") or "IDLE")
    tone = {
        "RUNNING": "ok",
        "PREPARING": "warn",
        "BLOCKED": "danger",
        "STOPPED": "muted",
    }.get(state, "muted")
    label = f"Paper loop: {state}"
    open_positions = int(summary.get("open_positions") or 0)
    if open_positions:
        label += f" · {open_positions} open"
    inner = chip(label, tone, dot=state in {"RUNNING", "PREPARING"})
    if state in {"RUNNING", "PREPARING", "BLOCKED"}:
        return f"<a class='chip-link' href='/autopilot'>{inner}</a>"
    return inner


def quote_chip(account: Mapping[str, Any]) -> str:
    quotes = [dict(item) for item in (account.get("quotes") or ())]
    if not quotes:
        return chip("no quote yet", "muted")
    fresh = sum(1 for item in quotes if item.get("fresh"))
    if fresh == len(quotes):
        return chip(f"quotes fresh · {fresh} of {len(quotes)}", "ok", dot=True)
    return chip(f"stale quotes · {len(quotes) - fresh} of {len(quotes)}", "warn", dot=True)


# ----------------------------------------------------------------------- rail
def _rail(ctx: Any) -> str:
    by_page = {page: (href, label) for page, href, label in NAV}
    groups: list[str] = []
    for index, (name, pages) in enumerate(NAV_GROUPS):
        links = []
        for page in pages:
            if page not in by_page:
                continue
            href, label = by_page[page]
            current = " aria-current='page'" if str(getattr(ctx, "page", "")) == page else ""
            links.append(f"<a href='{escape(href)}'{current}>{escape(label)}</a>")
        if not links:
            continue
        if index:
            groups.append("<span class='rail-sep' aria-hidden='true'></span>")
        groups.append(f"<div class='rail-group'>{''.join(links)}</div>")

    ends = [paper_loop_chip(ctx), execution_mode_chip(ctx)]
    terminal = ctx.terminal
    if terminal.simulated and terminal.connected:
        ends.append(action_form("disconnect", ctx.action_nonce, "Stop simulator preview", tone="danger", small=True))
    elif terminal.connected:
        here = getattr(ctx, "path", "") or PAGE_PATHS.get(str(getattr(ctx, "page", "")), "/")
        ends.append(f"<a class='btn btn--sm' href='{escape(here)}'>Refresh</a>")
        ends.append(action_form("disconnect", ctx.action_nonce, "Disconnect", tone="danger", small=True))
    else:
        ends.append("<a class='btn btn--sm' href='/mode'>Trading mode</a>")
        ends.append("<a class='btn btn--sm' href='/setup'>Open setup</a>")

    return (
        "<header class='rail'>"
        "<div class='rail-in'>"
        f"<a class='brand' href='/autopilot'><span class='brand-mark'>AXIOM</span>"
        f"<span class='brand-name'>FOREX · workstation</span></a>"
        f"<nav class='rail-nav' aria-label='Workstation'>{''.join(groups)}</nav>"
        f"<div class='rail-end'>{''.join(ends)}</div>"
        "</div></header>"
    )


def _strip(ctx: Any) -> str:
    """The facts that change without a click, in one line, worded plainly."""

    facts: list[str] = []
    summary = dict((ctx.extra or {}).get("autopilot_state") or {})
    if summary and summary.get("available") is not False:
        facts.append(("Paper loop", escape(str(summary.get("state") or "IDLE"))))
    mode = str((ctx.execution_mode or {}).get("mode", "PAPER")).upper()
    mode_text = "LIVE" if mode == "LIVE" else "PAPER"
    if mode == "LIVE" and not ctx.live_effective:
        mode_text = "LIVE (not confirmed)"
    facts.append(("Execution", mode_text))
    terminal = ctx.terminal
    if terminal.simulated:
        terminal_text = "simulator preview" if terminal.connected else "simulator idle"
    elif terminal.connected:
        terminal_text = f"MT5 · {terminal.mode}"
    else:
        terminal_text = "not connected"
    facts.append(("Terminal", terminal_text))
    account = dict((ctx.extra or {}).get("account") or {})
    quotes = [dict(item) for item in (account.get("quotes") or ())]
    if quotes:
        fresh = sum(1 for item in quotes if item.get("fresh"))
        facts.append(("Quotes", f"{fresh} of {len(quotes)} fresh"))
    else:
        facts.append(("Quotes", "none yet"))
    capital = dict(account.get("capital") or {})
    if capital.get("configured"):
        facts.append(("Paper capital", escape(f"{capital.get('amount', '')} {capital.get('currency', '')}".strip())))
    else:
        facts.append(("Paper capital", "not set"))
    generated = getattr(ctx, "generated_at", None)
    if generated is not None:
        facts.append(("Read", escape(generated.strftime("%H:%M:%S UTC"))))
    items = "".join(
        f"<span class='strip-fact'><span class='k'>{escape(k)}</span>"
        f"<span class='v'>{v}</span></span>"
        for k, v in facts
    )
    return f"<div class='strip'><div class='strip-in'>{items}</div></div>"


# -------------------------------------------------------------------- banners
def banners(ctx: Any) -> str:
    """Sample-data warning, then a one-shot notice, then the action's outcome."""

    items: list[str] = []
    if ctx.terminal.simulated:
        if ctx.terminal.connected:
            items.append(
                notice(
                    "SIMULATED / SAMPLE DATA — no MetaTrader 5 terminal is attached",
                    "The offline simulator is running because the launcher was started with "
                    "--simulate. Nothing on this page was read from a broker or an account, and "
                    "this preview can never reach observation mode.",
                    tone="warn",
                )
            )
        else:
            items.append(
                notice(
                    "SIMULATED / SAMPLE DATA — offline simulator enabled",
                    "The launcher was started with --simulate. The preview is not started, no "
                    "terminal is attached, and nothing here was read from a broker or account.",
                    tone="warn",
                )
            )
    if getattr(ctx, "notice", ""):
        items.append(notice(ctx.notice, tone="ok"))
    feedback = dict(getattr(ctx, "feedback", {}) or {})
    if feedback:
        tone = str(feedback.get("tone") or "ok")
        mapped = {"error": "danger", "danger": "danger", "warn": "warn", "ok": "ok"}.get(tone, "info")
        items.append(
            notice(
                str(feedback.get("title") or "Action outcome"),
                escape(str(feedback.get("message") or "")),
                tone=mapped,
                role="alert" if mapped in {"warn", "danger"} else "status",
                attrs=f" data-action='{escape(str(feedback.get('action') or ''))}'",
            )
        )
    return notice_stack(items)


def legend() -> str:
    """The data-label key.  Words, not colours: the chips are a second carrier."""

    return (
        "<div class='labels' aria-label='Data legend'><span>Data labels:</span>"
        f"{chip('Real broker', 'live', dot=True)}"
        f"{chip('Paper', 'paper', dot=True)}"
        f"{chip('Sample fixture', 'fixture', dot=True)}"
        f"{chip('Simulated', 'simulated', dot=True)}"
        "</div>"
    )


# ------------------------------------------------------------------- document
def document(ctx: Any, body: str | Sequence[str]) -> str:
    if not isinstance(body, str):
        body = "".join(str(item) for item in body)
    scope_key, scope_label, scope_tone, scope_note = page_scope(ctx)
    generated = getattr(ctx, "generated_at", None)
    stamp = generated.strftime("%Y-%m-%d %H:%M:%S UTC") if generated is not None else ""
    title = escape(str(getattr(ctx, "title", "")))
    feedback = dict(getattr(ctx, "feedback", {}) or {})
    live_region = ""
    if feedback:
        live_region = (
            "<div class='sr' aria-live='polite' role='status'>"
            f"{escape(str(feedback.get('title') or 'Action outcome'))}: "
            f"{escape(str(feedback.get('message') or ''))}</div>"
        )
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{title} · {APP_NAME} {APP_SUBTITLE}</title>"
        "<meta name='color-scheme' content='light'>"
        "<meta name='description' content='Local AXIOM-FOREX trading workstation. "
        "Read-only observation, locally simulated paper fills, and a guarded live path.'>"
        f"<style>{STYLES}</style></head><body>"
        "<a class='skip' href='#main'>Skip to content</a>"
        f"{_rail(ctx)}{_strip(ctx)}"
        f"<main class='shell' id='main' tabindex='-1' data-scope='{escape(scope_key)}'>"
        f"<div class='sr page-scope page-scope--{escape(scope_tone)}' id='page-scope'>"
        f"{escape(scope_label)} — {escape(scope_note)}</div>"
        f"<div class='notice-stack' id='page-banners'>{banners(ctx)}</div>"
        f"{live_region}"
        f"{body}"
        f"<footer class='footer'><div>{legend()}</div>"
        f"<p>{APP_NAME} {APP_SUBTITLE} · scope "
        f"<span class='chip chip--{escape(scope_tone)}'>{escape(scope_label)}</span>"
        f" · generated {escape(stamp)}</p></footer>"
        "</main></body></html>"
    )


def scope_header(ctx: Any) -> str:
    """The scope line the page head shows above its title."""

    _key, label, _tone, note = page_scope(ctx)
    return f"{escape(label)} · {escape(note)}"
