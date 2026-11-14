"""First-run setup: the six plain steps between a cold machine and a session.

Every step is a fact list with the one control that moves it, and the count in
the header is the count that is rendered -- the page used to print "Seven plain
steps" above a list of six.
"""

from __future__ import annotations

from html import escape
from typing import Any, Mapping

from .. import format as fmt
from ..components import (
    action_form,
    button_link,
    chip,
    facts,
    layout,
    meter,
    notice,
    page_head,
    section,
    table,
)

STEP_TONES = {
    "complete": ("Complete", "ok"),
    "action_required": ("Action needed", "warn"),
    "blocked": ("Blocked", "danger"),
    "pending": ("Waiting", "muted"),
}


def _step_actions(step: Mapping[str, Any], ctx: Any) -> str:
    action = str(step.get("action") or "")
    label = str(step.get("action_label") or "Continue")
    nonce = ctx.action_nonce
    if not action:
        return ""
    if action == "recheck":
        return button_link("/setup", "Check again", small=True)
    if action == "acknowledge-sign-in":
        return action_form("acknowledge-sign-in", nonce, label)
    if action == "connect":
        simulated = bool(getattr(getattr(ctx, "terminal", None), "simulated", False))
        return action_form("connect", nonce, label, tone="" if simulated else "primary")
    if action == "disconnect":
        return action_form("disconnect", nonce, label, tone="danger")
    if action == "confirm":
        return button_link("/account", "Review &amp; confirm", tone="primary", small=True)
    return ""


def _step(step: Mapping[str, Any], ctx: Any, index: int) -> str:
    status = str(step.get("status") or "pending")
    label, tone = STEP_TONES.get(status, (status, "muted"))
    items = tuple(step.get("items") or ())
    item_table = (
        table(
            (("Field", False), ("Value", False)),
            [
                [
                    f"<td data-label='Field'>{escape(str(item.get('label') or ''))}</td>",
                    f"<td data-label='Value'>{escape(str(item.get('value') or ''))}</td>",
                ]
                for item in items
            ],
            caption="Facts for this setup step",
        )
        if items
        else ""
    )
    detail = (
        f"<p class='small muted'>{escape(str(step.get('detail') or ''))}</p>"
        if step.get("detail")
        else ""
    )
    actions = _step_actions(step, ctx)
    return (
        f"<li class='step step--{escape(status)}'>"
        f"<span class='step-num' aria-hidden='true'>{int(step.get('number') or index + 1)}</span>"
        "<div class='step-body'>"
        f"<div class='step-title'><h3>{escape(str(step.get('title') or ''))}</h3>"
        f"{chip(label, tone)}</div>"
        f"<p>{escape(str(step.get('summary') or ''))}</p>{detail}{item_table}"
        + (f"<div class='actions'>{actions}</div>" if actions else "")
        + "</div></li>"
    )


def _prerequisites(view: Mapping[str, Any]) -> str:
    items = tuple(view.get("prerequisites") or ())
    if not items:
        return notice(
            "All prerequisites look good",
            "Windows, Python, the MetaTrader5 package and the terminal were all found.",
            tone="ok",
        )
    return "".join(
        notice(
            str(item.get("title") or "A prerequisite is missing"),
            escape(str(item.get("detail") or "")),
            tone="warn",
        )
        for item in items
    )


def _template(view: Mapping[str, Any]) -> str:
    return (
        "<p class='small muted' style='margin:0 0 8px'>Save this as "
        "<span class='mono'>mt5_settings.local.json</span> next to the launcher. It holds no "
        "password: AXIOM uses the terminal's own signed-in session.</p>"
        f"<pre class='template'>{escape(str(view.get('settings_template') or ''))}</pre>"
    )


def _safety(view: Mapping[str, Any]) -> str:
    safety = dict(view.get("safety") or {})
    lines = tuple(safety.get("lines") or ())
    off = sorted(
        key
        for key, value in safety.items()
        if key != "lines" and value is False
    )
    return facts(
        [
            ("Trading controls", fmt.text(", ".join(safety.get("trading_controls") or ()) or "none")),
            ("Execution modes", fmt.text(", ".join(safety.get("execution_modes") or ()) or "none")),
            ("Credential storage", chip("no", "ok") if safety.get("stores_credentials") is False else chip("check", "warn")),
            ("Asks for the MT5 password", chip("never", "ok") if safety.get("asks_for_mt5_password") is False else chip("check", "warn")),
            (
                "Refuses unless set",
                fmt.text(", ".join(key.replace("_", " ") for key in off) or "nothing"),
            ),
        ]
    ) + "<ul class='check-list'>" + "".join(f"<li>{escape(str(line))}</li>" for line in lines) + "</ul>"


def render_setup(ctx: Any) -> str:
    view = dict(ctx.wizard.view())
    progress = dict(view.get("progress") or {})
    steps = tuple(view.get("steps") or ())
    total = int(progress.get("total") or len(steps))
    completed = int(progress.get("completed") or 0)
    percent = int(progress.get("percent") or 0)

    head = page_head(
        "First run",
        "Set this machine up",
        f"{total} plain steps from a cold machine to a session. Each step names the one control "
        "that moves it, and nothing here connects, arms or sends an order on its own.",
        badges=chip(f"{completed} of {total} done", "ok" if completed == total else "warn", dot=True)
        + (chip("SIMULATED / SAMPLE DATA", "fixture") if view.get("simulated") else "")
        + (chip("requires Windows", "warn") if not view.get("live_terminal") else chip("terminal found", "ok")),
        actions=button_link("/account", "Account & symbols", small=True)
        + button_link("/", "Start PAPER trading", tone="primary", small=True),
    )

    body = section(
        "Progress",
        f"<p class='small muted' style='margin:0 0 6px'>{completed} of {total} steps complete "
        f"· {percent}%</p>"
        + meter(percent / 100.0, tone="ok" if percent >= 100 else "")
        + f"<p class='small muted' style='margin:6px 0 0'>{total} plain steps. Nothing is assumed "
        "about your broker, account or terminal path.</p>",
        id="progress",
    )

    steps_html = (
        "<ol class='guide-steps'>"
        + "".join(_step(step, ctx, index) for index, step in enumerate(steps))
        + "</ol>"
    )
    main = body + section(
        "The steps",
        steps_html,
        note="The highlighted control is the one that moves the step you are on.",
        id="steps",
    )

    aside = (
        section("Prerequisites", _prerequisites(view), id="prerequisites")
        + section("Local settings template", _template(view), id="template")
        + section("Guarantees", _safety(view), id="guarantees")
    )
    return head + layout(main, aside)
