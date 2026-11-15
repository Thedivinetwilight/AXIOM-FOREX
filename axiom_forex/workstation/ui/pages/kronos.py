"""Kronos: an optional forecast, and everything that is true about it.

The page has three jobs and keeps them apart.  It can install the model into a
folder AXIOM manages, it can run one forecast or a schedule of them and record
what happened, and it can benchmark the forecast against simple baselines on the
operator's own bars.  None of those can produce an order: a forecast is an
estimate of the next bars, and the only route from here to a trade is through a
separately specified strategy, the LIVE page's review, and the risk gate -- none
of which this page can reach.

Every state on the page is the runtime's own word.  A missing dependency, a
missing bar source, an uninstalled model and a failed load are four different
sentences, and none of them is a zero or a dash.
"""

from __future__ import annotations

from typing import Any, Mapping

from .. import format as fmt
from ..components import (
    action_form,
    button_link,
    cell,
    chip,
    empty_state,
    facts,
    field,
    jump_nav,
    jump_nav_bar,
    layout,
    meter,
    notice,
    page_head,
    section,
    table,
)
from . import common

#: How the runtime's state is said.  Never a colour without the word.
_STATE_CHIPS: dict[str, tuple[str, str, bool]] = {
    "READY": ("model loaded · forecast only", "ok", True),
    "LOADING": ("loading", "warn", True),
    "STOPPED": ("stopped", "muted", True),
}

#: Blocks of settings, in the order the operator meets them.
_GROUP_ORDER = ("Basic", "Advanced")


def status_chip(view: Mapping[str, Any]) -> str:
    state = dict(view.get("state") or {})
    name = str(state.get("state", "NOT_LOADED"))
    if name in _STATE_CHIPS:
        label, tone, dot = _STATE_CHIPS[name]
        return chip(label, tone, dot=dot)
    if name == "FAILED":
        return chip(f"failed · {state.get('reason', 'unknown')}", "danger", dot=True)
    return chip("not loaded", "muted", dot=True)


def _bytes_human(count: Any) -> str:
    try:
        value = float(count)
    except (TypeError, ValueError):
        return "unknown"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


# ---------------------------------------------------------------------------
# the chart: recent mid closes and the forecast band, inline and script-free
# ---------------------------------------------------------------------------


def chart_svg(chart: Mapping[str, Any]) -> str:
    """An inline SVG: the recent closes, then the forecast with its band.

    No script, no library, no external asset.  Floats are used only to place
    points; every price is printed as text elsewhere on the page.
    """

    history = [dict(item) for item in (chart.get("history") or ())]
    forecast = dict(chart.get("forecast") or {})
    if not history:
        return ""
    closes = []
    for item in history:
        try:
            closes.append(float(item.get("close")))
        except (TypeError, ValueError):
            continue
    if not closes:
        return ""
    p10 = [float(value) for value in (forecast.get("p10") or ())]
    p50 = [float(value) for value in (forecast.get("p50") or ())]
    p90 = [float(value) for value in (forecast.get("p90") or ())]
    width, height, pad = 760, 220, 34
    span = max(1, len(closes) + len(p50) - 1)
    values = list(closes) + p10 + p50 + p90
    low, high = min(values), max(values)
    if high <= low:
        high = low + 1e-9
    step_x = (width - pad * 2) / max(1, span)
    scale_y = (height - pad * 2) / (high - low)

    def x(index: int) -> float:
        return pad + index * step_x

    def y(value: float) -> float:
        return height - pad - (value - low) * scale_y

    path = " ".join(
        f"{'M' if index == 0 else 'L'}{x(index):.1f},{y(value):.1f}"
        for index, value in enumerate(closes)
    )
    parts = [
        f"<svg viewBox='0 0 {width} {height}' width='100%' height='{height}' role='img' "
        "aria-label='Recent mid closes and the last forecast with its uncertainty band'>",
        f"<line x1='{pad}' y1='{height - pad}' x2='{width - pad}' y2='{height - pad}' "
        "stroke='#8e9cb2' stroke-width='1'/>",
    ]
    if p10 and p90 and p50:
        upper = " ".join(
            f"{x(len(closes) + index):.1f},{y(value):.1f}" for index, value in enumerate(p90)
        )
        lower = " ".join(
            f"{x(len(closes) + index):.1f},{y(value):.1f}"
            for index, value in reversed(list(enumerate(p10)))
        )
        parts.append(
            f"<polygon points='{upper} {lower}' fill='rgba(27,77,216,0.14)' "
            "stroke='rgba(27,77,216,0.45)' stroke-width='1'/>"
        )
        median = " ".join(
            f"{'M' if index == 0 else 'L'}{x(len(closes) + index):.1f},{y(value):.1f}"
            for index, value in enumerate(p50)
        )
        parts.append(
            f"<path d='{median}' fill='none' stroke='#1b4dd8' stroke-width='2' "
            "stroke-dasharray='5 4'/>"
        )
        parts.append(
            f"<line x1='{x(len(closes) - 1):.1f}' y1='{pad}' x2='{x(len(closes) - 1):.1f}' "
            f"y2='{height - pad}' stroke='#8e9cb2' stroke-width='1' stroke-dasharray='3 4'/>"
        )
    parts.append(f"<path d='{path}' fill='none' stroke='#3d4b63' stroke-width='2'/>")
    parts.append(
        f"<text x='{pad}' y='{height - 10}' fill='#5d6b83' font-size='11'>history</text>"
        + (
            f"<text x='{width - pad - 52}' y='{height - 10}' fill='#12379c' "
            "font-size='11'>forecast</text>"
            if p50
            else ""
        )
    )
    parts.append(
        f"<text x='{pad}' y='{pad - 12}' fill='#5d6b83' font-size='11'>{high:.5f}</text>"
        f"<text x='{pad}' y='{height - pad + 16}' fill='#5d6b83' font-size='11'>{low:.5f}</text>"
    )
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# install: one explicit action, and a state machine that says what it is doing
# ---------------------------------------------------------------------------


def _progress(job: Mapping[str, Any]) -> str:
    percent = job.get("percent")
    try:
        width = max(0.0, min(100.0, float(percent)))
    except (TypeError, ValueError):
        width = 0.0
    label = (
        f"{_bytes_human(job.get('bytes_done'))} of {_bytes_human(job.get('bytes_total'))}"
        if job.get("bytes_total")
        else f"{_bytes_human(job.get('bytes_done'))} downloaded"
    )
    step = (
        f" · step {fmt.text(str(job.get('step', '')))} of {fmt.text(str(job.get('steps', '')))}"
        f" · {fmt.text(str(job.get('asset_label', '')))}"
        if job.get("steps")
        else ""
    )
    return (
        f"<div class='progress' role='progressbar' aria-valuemin='0' aria-valuemax='100' "
        f"aria-valuenow='{width:.0f}' aria-label='Download progress'>{meter(width / 100)}</div>"
        f"<p class='small muted' style='margin:6px 0 0'>{label}{step}</p>"
    )


def _install_section(view: Mapping[str, Any], nonce: str) -> str:
    install = dict(view.get("install") or {})
    plan = dict(install.get("plan") or {})
    job = dict(install.get("job") or {})
    manifest = dict(install.get("manifest") or {})
    state = str(install.get("state") or "NOT_INSTALLED")
    hardware = dict(plan.get("hardware") or {})
    licence = dict(plan.get("licence") or {})
    installed = bool(install.get("installed"))
    running = bool(job.get("running")) or state == "DOWNLOADING"
    download = (
        plan.get("download_human")
        if plan.get("exact")
        else f"about {plan.get('estimated_human', 'unknown')} (estimated; check for the exact size)"
    )

    body = facts(
        [
            (
                "Recommended",
                fmt.text(str(plan.get("variant_name", "Kronos-mini")))
                + " · the smallest variant with official public weights",
            ),
            ("Download", fmt.text(str(download or "unknown"))),
            ("Free on this machine", fmt.text(str(plan.get("free_human", "unknown")))),
            (
                "Installs into",
                f"<code>{fmt.text(str(install.get('destination', '')))}</code>",
            ),
            (
                "Hardware",
                fmt.text(str(hardware.get("device_hint", "")))
                + " "
                + (
                    chip("torch present", "ok")
                    if hardware.get("torch_present")
                    else chip("torch missing", "danger")
                ),
            ),
            (
                "Revisions",
                "code pinned to "
                f"<code>{fmt.text(str(plan.get('code_commit', ''))[:12])}</code>; model and "
                "tokenizer pinned to the commit the check resolves and recorded in the install "
                "manifest, so a later load never follows a moving branch",
            ),
            (
                "Licence",
                f"code {fmt.text(str(licence.get('code', 'MIT')))}; weights "
                f"{fmt.text(str(licence.get('weights_state', 'UNVERIFIED_BY_AXIOM')))}",
            ),
        ]
    )
    body += (
        "<ul class='check-list' style='margin-top:12px'>"
        "<li><strong>Storage.</strong> Everything is written into the AXIOM-managed folder "
        f"<code>{fmt.text(str(install.get('managed_root', '')))}</code>, and nowhere else. "
        "<em>Clean up</em> removes it again.</li>"
        "<li><strong>Internet.</strong> Only when you press Install, AXIOM contacts the two "
        "official Hugging Face repositories and the pinned GitHub source tree, for the files "
        "listed here. Nothing is fetched at startup or while a page is being read.</li>"
        "<li><strong>Licence.</strong> The code is MIT (Copyright (c) 2025 ShiYu). The weights "
        "are published under <code>NeoQuasar/*</code>, and their licence is not something AXIOM "
        "can vouch for: read the model card before you rely on them.</li>"
        "<li><strong>Forecast only.</strong> Installing downloads files. It cannot create a "
        "signal, switch PAPER/LIVE, arm execution, or place an order.</li>"
        "</ul>"
    )

    blockers_list = [str(item) for item in (plan.get("blockers") or ())]
    if "not_enough_disk_space" in blockers_list:
        body += notice(
            "Not enough free space for this download",
            f"It needs {fmt.text(str(download))} and "
            f"{fmt.text(str(plan.get('free_human', 'unknown')))} is free. Free some space, then "
            "press Check again. Nothing was downloaded.",
            tone="warn",
        )
    if "runtime_packages_missing" in blockers_list:
        missing = ", ".join(str(item) for item in (hardware.get("missing_packages") or ()))
        body += notice(
            "The Kronos runtime is not installed on this machine",
            f"Missing: {fmt.text(missing)}. AXIOM does not install software; install the "
            "official requirements first (they are listed further down this page), then press "
            "Check.",
            tone="warn",
        )

    if running:
        body += "<div style='margin-top:12px'>" + _progress(job) + "</div>"
        body += (
            "<div class='actions' style='margin-top:12px'>"
            + action_form("kronos-install-cancel", nonce, "Cancel download", tone="danger")
            + "</div><p class='small muted' style='margin:6px 0 0'>Cancelling stops between "
            "files; whatever arrived stays in the managed folder so you can retry, or clean "
            "it up.</p>"
        )
    elif installed:
        paths = dict(install.get("paths") or {})
        revisions = dict(manifest.get("revisions") or {})
        body += notice(
            "The recommended Kronos setup is installed",
            f"Installed {fmt.text(str(manifest.get('installed_at', '')))} · "
            f"{_bytes_human(manifest.get('bytes'))}. Paths are recorded, so nothing has to be "
            "typed. Press Load model to use it.",
            tone="ok",
        )
        body += facts(
            [
                ("Code", f"<code>{fmt.text(str(paths.get('code_path', '')))}</code>"),
                ("Model", f"<code>{fmt.text(str(paths.get('model_path', '')))}</code>"),
                (
                    "Tokenizer",
                    f"<code>{fmt.text(str(paths.get('tokenizer_path', '')))}</code>",
                ),
                (
                    "Pinned model revision",
                    fmt.text(str(revisions.get("model", ""))) or "—",
                ),
                (
                    "Pinned code commit",
                    f"<code>{fmt.text(str(plan.get('code_commit', ''))[:12])}</code>",
                ),
            ]
        )
        body += (
            "<div class='actions' style='margin-top:12px'>"
            + action_form("kronos-load", nonce, "Load model", tone="primary")
            + action_form("kronos-install-check", nonce, "Check the sources again")
            + action_form("kronos-install-cleanup", nonce, "Clean up the install", tone="danger")
            + "</div>"
        )
    else:
        if state in {"FAILED", "CANCELLED"}:
            detail = f"{job.get('detail', '') or job.get('reason', '')} {job.get('hint', '')}".strip()
            body += notice(
                "The install did not finish" if state == "FAILED" else "The install was cancelled",
                fmt.text(detail or "The installer reported no detail."),
                tone="warn",
            )
        body += (
            "<div class='actions' style='margin-top:12px'>"
            + action_form("kronos-install-check", nonce, "Check this machine and the download")
            + action_form(
                "kronos-install",
                nonce,
                "Retry install" if state in {"FAILED", "CANCELLED"} else "Install recommended setup",
                tone="primary",
            )
            + (
                action_form("kronos-install-cleanup", nonce, "Clean up partial download")
                if state in {"FAILED", "CANCELLED"}
                else ""
            )
            + "</div>"
        )
        body += (
            "<p class='small muted' style='margin:8px 0 0'>"
            + (
                f"Checked at {fmt.text(str(plan.get('checked_at', '')))}. "
                if install.get("checked")
                else "Install checks this machine and the sources first: it shows the exact "
                "size, then downloads when you press Install again. "
            )
            + "No download starts without that press.</p>"
        )

    return section(
        "Install the recommended setup",
        body,
        note="One action, official sources only, into a folder AXIOM manages: no paths to "
        "type, nothing downloaded until you press Install, and a Clean up button when you "
        "want it gone.",
        tools=(
            chip("installed", "ok", dot=True)
            if installed
            else chip("downloading", "warn", dot=True)
            if running
            else chip("not installed", "muted", dot=True)
        ),
        id="install",
    )


# ---------------------------------------------------------------------------
# the forecast: controls, state, chart, last result
# ---------------------------------------------------------------------------


def _forecast_section(view: Mapping[str, Any], nonce: str) -> str:
    settings = dict(view.get("settings") or {})
    choices = [str(item) for item in (view.get("symbol_choices") or ())]
    current = str(settings.get("symbol", ""))
    timeframes = [str(item) for item in (view.get("timeframes") or ())]
    if choices:
        symbol_field = field(
            "symbol",
            "Symbol",
            kind="select",
            value=current,
            options=[(item, item) for item in choices],
            hint="The verified symbols you selected on Account & symbols. The first one is the "
            "default.",
            field_id="kronos-forecast-symbol",
        )
    else:
        symbol_field = field(
            "symbol",
            "Symbol",
            value=current,
            hint="No verified symbol is selected yet. Select one on Account & symbols, then it "
            "appears here as a choice.",
            field_id="kronos-forecast-symbol",
        )
    body = (
        "<form method='post' action='/actions/kronos-forecast' class='form'>"
        f"<input type='hidden' name='nonce' value='{fmt.attr(nonce)}'>"
        "<div class='form-row'>"
        + symbol_field
        + field(
            "timeframe",
            "Timeframe",
            kind="select",
            value=str(settings.get("timeframe", "")),
            options=[(item, item) for item in timeframes],
            hint="Built from the terminal's bid/ask bars.",
            field_id="kronos-forecast-timeframe",
        )
        + field(
            "pred_len",
            "Horizon (bars)",
            kind="number",
            value=str(settings.get("pred_len", "")),
            hint="How many bars ahead to forecast.",
            field_id="kronos-forecast-horizon",
        )
        + "</div><div class='actions' style='margin-top:12px'>"
        "<button class='btn btn--primary' type='submit'>Run one forecast</button></div></form>"
    )
    state = str(dict(view.get("state") or {}).get("state", "NOT_LOADED"))
    if state != "READY":
        body += (
            "<p class='small muted' style='margin:10px 0 0'>The model is not loaded yet. Install "
            "and load it above; the symbol and timeframe here are remembered either way, and "
            "installing never waits for a symbol.</p>"
        )
    return section(
        "Forecast controls",
        body,
        note="Symbol, timeframe and horizon are part of the forecast, not of the load: you can "
        "change them and run again without reloading the model.",
        tools=chip(
            "READY" if state == "READY" else fmt.text(state.replace("_", " ").lower()),
            "ok" if state == "READY" else "muted",
        ),
        id="forecast",
    )


def _status_section(view: Mapping[str, Any]) -> str:
    state = dict(view.get("state") or {})
    settings = dict(view.get("settings") or {})
    rows: list[tuple[str, str]] = [
        ("Component", chip("optional · replaceable", "muted")),
        ("Model state", status_chip(view)),
        ("Variant", fmt.text(str(settings.get("model_key", "")))),
        ("Device", fmt.text(str(state.get("device", ""))) or "—"),
        ("Settings fingerprint", f"<code>{fmt.text(str(view.get('fingerprint', '')))}</code>"),
        (
            "Scheduled forecasting",
            chip("running", "ok", dot=True) if view.get("schedule_running") else chip("stopped", "muted"),
        ),
        ("Forecasts this session", fmt.text(str(state.get("forecasts", 0)))),
        ("Failures", fmt.text(str(state.get("failures", 0)))),
        ("Last forecast", fmt.text(str(state.get("last_forecast_at", ""))) or "—"),
        ("Last error", fmt.text(str(state.get("last_error", ""))) or "—"),
    ]
    if state.get("reason"):
        rows.append(
            ("Failure reason", fmt.text(str(state.get("detail", "")) or str(state.get("reason", ""))))
        )
    return section(
        "Status",
        facts(rows),
        note="A state is only ever what the runtime reported. Nothing retries by itself.",
        tools=status_chip(view),
        id="status",
    )


def _chart_section(view: Mapping[str, Any]) -> str:
    chart = dict(view.get("chart") or {})
    svg = chart_svg(chart) if chart else ""
    if not svg:
        return ""
    caption = (
        f"{fmt.text(str(chart.get('symbol', '')))} · {fmt.text(str(chart.get('timeframe', '')))} "
        f"· mid price (not tradable)"
    )
    band_note = (
        "The shaded band is the 10th–90th percentile of the sampled paths. A band that excludes "
        "the outcome is a failure of the model's uncertainty, and it is recorded as one."
        if chart.get("forecast")
        else "History only: no forecast has been run on this page yet."
    )
    return section(
        "Input and forecast",
        f"<div class='chart'>{svg}</div>",
        note=f"{caption}. {band_note}",
        tools=chip("mid price · not tradable", "muted"),
        id="chart",
    )


def _last_section(view: Mapping[str, Any]) -> str:
    last = dict(view.get("last") or {})
    if not last:
        return ""
    quality = dict(last.get("quality") or {})
    band = dict(last.get("band_at_horizon") or {})
    return section(
        "The last forecast",
        facts(
            [
                ("Recorded", fmt.text(str(last.get("created_at", "")))),
                (
                    "Symbol / timeframe",
                    f"{fmt.text(str(last.get('symbol', '')))} "
                    f"{fmt.text(str(last.get('timeframe', '')))}",
                ),
                (
                    "Model",
                    f"{fmt.text(str(last.get('model_key', '')))} · "
                    f"{fmt.text(str(last.get('model_revision', '')))}",
                ),
                ("Horizon", f"{fmt.text(str(last.get('horizon', '')))} bar(s)"),
                ("Last observed close", fmt.text(str(last.get("last_close", "")))),
                ("Median at horizon", fmt.text(str(band.get("p50", ""))) or "—"),
                (
                    "Band at horizon",
                    f"{fmt.text(str(band.get('p10', '')))} – {fmt.text(str(band.get('p90', '')))}",
                ),
                (
                    "Expected move",
                    f"{fmt.text(str(last.get('expected_move', '')))} "
                    f"({fmt.text(str(last.get('expected_move_basis_points', '')))} bp)",
                ),
                (
                    "Data freshness",
                    f"{fmt.text(str(quality.get('last_at', '')))} · "
                    f"{chip(str(quality.get('volume_mode', '')), 'muted')}",
                ),
                (
                    "Input quality",
                    f"{fmt.text(str(quality.get('bars', '')))} bars · "
                    f"{fmt.text(str(quality.get('gaps', 0)))} gap(s) · "
                    f"{fmt.text(str(quality.get('crossed', 0)))} crossed",
                ),
                (
                    "Inference time",
                    f"{fmt.text(str(last.get('runtime_seconds', '')))} s for "
                    f"{fmt.text(str(last.get('rollouts', '')))} rollout(s)",
                ),
                ("Label", chip(str(last.get("label", "")), "paper")),
            ]
        ),
        note="Stored before it was shown, with the settings fingerprint and input quality it "
        "was built from.",
        tools=chip("forecast only", "paper"),
        id="last",
    )


def _run_section(view: Mapping[str, Any], nonce: str) -> str:
    controls = [
        action_form("kronos-load", nonce, "Load model", tone="primary"),
        action_form("kronos-forecast", nonce, "Run one forecast"),
        (
            action_form("kronos-schedule-stop", nonce, "Stop scheduled forecasting", tone="danger")
            if view.get("schedule_running")
            else action_form("kronos-schedule-start", nonce, "Start scheduled forecasting")
        ),
        action_form("kronos-stop", nonce, "Stop and unload", tone="danger"),
    ]
    return section(
        "Run",
        f"<div class='actions'>{''.join(controls)}</div>",
        note="Load reads the saved settings and the model files you placed on this machine. It "
        "never downloads, installs, or starts anything at boot.",
        id="run",
    )


# ---------------------------------------------------------------------------
# settings, presets and what they cost
# ---------------------------------------------------------------------------


def _control(control: Mapping[str, Any], prefix: str = "kronos") -> str:
    name = str(control.get("name", ""))
    ident = f"{prefix}-{name.replace('_', '-')}"
    hint = str(control.get("help", ""))
    limits = []
    if control.get("min"):
        limits.append(f"min {control.get('min')}")
    if control.get("max"):
        limits.append(f"max {control.get('max')}")
    if limits:
        hint = f"{hint} ({', '.join(limits)})." if hint else f"({', '.join(limits)})."
    if str(control.get("kind")) == "select":
        return field(
            name,
            str(control.get("label", "")),
            kind="select",
            value=str(control.get("value", "")),
            options=[(str(option), str(option)) for option in (control.get("options") or ())],
            hint=hint,
            field_id=ident,
        )
    kind = "number" if str(control.get("kind")) == "number" else "text"
    return field(
        name,
        str(control.get("label", "")),
        kind=kind,
        value=str(control.get("value", "")),
        hint=hint,
        field_id=ident,
    )


def _settings_section(view: Mapping[str, Any], nonce: str) -> str:
    controls = [dict(item) for item in (view.get("controls") or ())]
    problems = [dict(item) for item in (view.get("problems") or ())]
    body = ""
    if problems:
        body += notice(
            "These settings cannot be loaded yet",
            "<ul class='check-list'>"
            + "".join(
                f"<li><code>{fmt.text(str(item.get('path', '')))}</code> "
                f"{fmt.text(str(item.get('message', '')))}</li>"
                for item in problems
            )
            + "</ul>",
            tone="warn",
            role="status",
        )
    groups = []
    for group in _GROUP_ORDER:
        items = [item for item in controls if str(item.get("group")) == group]
        if not items:
            continue
        groups.append(
            f"<h3 style='margin-top:16px'>{fmt.text(group)}</h3>"
            "<div class='form-row' style='margin-top:8px'>"
            + "".join(_control(item) for item in items)
            + "</div>"
        )
    if not groups:
        body += empty_state(
            "No settings exposed", "This build exposes no Kronos setting."
        )
    else:
        body += (
            "<form method='post' action='/actions/kronos-save' class='form'>"
            f"<input type='hidden' name='nonce' value='{fmt.attr(nonce)}'>"
            + "".join(groups)
            + "<div class='actions' style='margin-top:16px'>"
            "<button class='btn btn--primary' type='submit'>Save these settings</button>"
            "</div></form>"
        )
    presets = [str(item) for item in (view.get("presets") or ())]
    body += (
        "<h3 style='margin-top:20px'>Named presets</h3>"
        "<form method='post' action='/actions/kronos-preset-save' class='form' "
        "style='margin-top:8px'>"
        f"<input type='hidden' name='nonce' value='{fmt.attr(nonce)}'>"
        + field(
            "preset_name",
            "Name",
            placeholder="e.g. eurusd 15m quiet",
            hint="Saved beside the launcher as kronos_presets.local.json, which Git ignores.",
            field_id="kronos-preset-name",
        )
        + "<div class='actions'><button class='btn' type='submit'>Save current settings"
        "</button></div></form>"
    )
    if presets:
        body += (
            "<form method='post' action='/actions/kronos-preset-load' class='form' "
            "style='margin-top:12px'>"
            f"<input type='hidden' name='nonce' value='{fmt.attr(nonce)}'>"
            + field(
                "preset_name",
                "Saved presets",
                kind="select",
                value=presets[0],
                options=[(item, item) for item in presets],
                field_id="kronos-preset-load",
            )
            + "<div class='actions'><button class='btn' type='submit'>Load into the form"
            "</button></div></form>"
        )
    else:
        body += "<p class='small muted' style='margin-top:12px'>No presets saved yet.</p>"
    return section(
        "Inference settings",
        body,
        note="Every control maps to a real Kronos argument or to an AXIOM-side scheduling "
        "choice. Limits come from the selected variant's published context length. AXIOM-managed "
        "install paths are filled in; a path you type here overrides them. Loading a preset fills "
        "the form; it does not load the model.",
        tools=chip("advanced", "muted"),
        id="settings",
    )


def _estimate_section(view: Mapping[str, Any]) -> str:
    estimate = dict(view.get("estimate") or {})
    availability = dict(view.get("availability") or {})
    packages = [dict(item) for item in (availability.get("packages") or ())]
    cuda = availability.get("cuda_available")
    rows: list[tuple[str, str]] = [
        ("Parameters", fmt.text(str(estimate.get("parameters", "")))),
        (
            "Weights (estimated)",
            f"{fmt.text(str(estimate.get('weights_megabytes', '')))} MB at fp32",
        ),
        (
            "Context / horizon",
            f"{fmt.text(str(estimate.get('context_bars', '')))} bars → "
            f"{fmt.text(str(estimate.get('horizon_bars', '')))} bars",
        ),
        (
            "Rollouts",
            f"{fmt.text(str(estimate.get('rollouts', '')))} sequential predict() calls",
        ),
        ("Python", fmt.text(str(availability.get("python", "")))),
        (
            "torch",
            chip("present", "ok") if availability.get("torch_present") else chip("missing", "danger"),
        ),
        (
            "CUDA",
            "—"
            if cuda is None
            else chip("available", "ok")
            if cuda
            else chip("not available", "warn"),
        ),
    ]
    body = facts(rows)
    if packages:
        body += table(
            [
                ("Requirement", "text"),
                ("Import name", "text"),
                ("State", "text"),
            ],
            [
                [
                    cell(fmt.text(str(item.get("requirement", ""))), label="Requirement"),
                    cell(f"<code>{fmt.text(str(item.get('import_name', '')))}</code>", label="Import name"),
                    cell(
                        chip("present", "ok")
                        if item.get("present")
                        else chip(
                            "missing" if item.get("required") else "missing (optional)",
                            "danger" if item.get("required") else "warn",
                        ),
                        label="State",
                    ),
                ]
                for item in packages
            ],
            caption="Dependencies from the official requirements.txt",
        )
    notes = [str(item) for item in (estimate.get("notes") or ())]
    if notes:
        body += (
            "<h3 style='margin-top:16px'>What that estimate does not include</h3>"
            "<ul class='check-list'>"
            + "".join(f"<li>{fmt.text(note)}</li>" for note in notes)
            + "</ul>"
        )
    return section(
        "Before you load: what it costs and what is installed",
        body,
        note="AXIOM never installs software. Missing packages are reported with the exact "
        "requirement so you can install them yourself.",
        tools=chip("estimate, not a measurement", "muted"),
        id="estimate",
    )


def _weights_section(view: Mapping[str, Any], nonce: str) -> str:
    source = dict(view.get("source") or {})
    weights = dict(view.get("weights") or {})
    settings = dict(view.get("settings") or {})
    body = facts(
        [
            ("Repository", fmt.text(str(source.get("repo", "")))),
            ("Commit read for these facts", fmt.text(str(source.get("commit_url", "")))),
            (
                "Code licence",
                f"{fmt.text(str(source.get('code_licence', '')))} · "
                f"{fmt.text(str(source.get('code_copyright', '')))}",
            ),
            ("Paper", fmt.text(str(source.get("paper_url", "")))),
            ("Weights host", fmt.text(str(source.get("weights_host", "")))),
            ("Weights licence", chip(str(source.get("weights_licence", "")), "warn")),
            (
                "Model file location",
                fmt.text(str(settings.get("model_path", ""))) or "official Hub id",
            ),
            ("Pinned revision", fmt.text(str(settings.get("revision", ""))) or "—"),
            ("Local digests", fmt.text(str(weights.get("state", "not inspected")))),
        ]
    )
    body += (
        "<p class='small muted' style='margin:10px 0 0'>AXIOM downloads only the three official "
        "sources listed above, only when you press Install, and only into its own managed folder. "
        "The inspection below hashes whatever is on disk -- including an install you already have "
        "-- so the files can be compared with the publisher's own values. AXIOM does not install "
        "software packages.</p>"
    )
    files = [dict(item) for item in (weights.get("files") or ())]
    if files:
        body += table(
            [
                ("File", "text"),
                ("Bytes", "num"),
                ("SHA-256", "text"),
            ],
            [
                [
                    cell(fmt.text(str(item.get("path", ""))), label="File"),
                    cell(fmt.text(str(item.get("bytes", ""))), label="Bytes", num=True),
                    cell(
                        f"<code>{fmt.text(str(item.get('sha256', ''))[:24])}…</code>",
                        label="SHA-256",
                    ),
                ]
                for item in files
            ],
            caption="Files hashed locally",
        )
    body += (
        "<form method='post' action='/actions/kronos-inspect-weights' class='form' "
        "style='margin-top:12px'>"
        f"<input type='hidden' name='nonce' value='{fmt.attr(nonce)}'>"
        f"<input type='hidden' name='model_path' "
        f"value='{fmt.attr(str(settings.get('model_path', '')))}'>"
        "<div class='actions'><button class='btn' type='submit'>Inspect and hash the model files"
        "</button></div></form>"
    )
    return section(
        "Model, licence, and where the files come from",
        body,
        note="Licence and version are shown before anything is used; the weights' licence is "
        "not something AXIOM can vouch for.",
        tools=chip("official sources only", "warn"),
        id="weights",
    )


# ---------------------------------------------------------------------------
# history, benchmark, mapping and what a forecast may never do
# ---------------------------------------------------------------------------


def _history_section(view: Mapping[str, Any], nonce: str) -> str:
    history = dict(view.get("history") or {})
    recent = [dict(item) for item in (history.get("recent") or ())]
    summary = dict(history.get("summary") or {})
    rows = []
    for item in recent:
        outcome = dict(item.get("outcome") or {})
        if outcome:
            verdict = (
                chip("scored", "ok")
                + " · "
                + ("right" if outcome.get("direction_correct") else "wrong")
                + f" · error {fmt.text(str(outcome.get('absolute_error', '')))}"
            )
        else:
            verdict = chip("waiting for its horizon", "muted")
        rows.append(
            [
                cell(fmt.text(str(item.get("created_at", ""))), label="Recorded"),
                cell(
                    f"{fmt.text(str(item.get('symbol', '')))} "
                    f"{fmt.text(str(item.get('timeframe', '')))}",
                    label="Series",
                ),
                cell(f"<code>{fmt.text(str(item.get('model_key', '')))}</code>", label="Model"),
                cell(
                    fmt.text(str((item.get("band_at_horizon") or {}).get("p50", ""))),
                    label="Median",
                    num=True,
                ),
                cell(verdict, label="Outcome"),
            ]
        )
    body = table(
        [
            ("Recorded", "text"),
            ("Series", "text"),
            ("Model", "text"),
            ("Median", "num"),
            ("Outcome", "text"),
        ],
        rows,
        caption="Most recent forecasts from kronos_forecasts.sqlite3",
        empty=empty_state(
            "No forecast recorded yet",
            "Run one forecast, or start scheduled forecasting. Each result is stored with its "
            "settings fingerprint before it is shown.",
        ),
    )
    if recent:
        body += (
            "<div class='actions' style='margin-top:12px'>"
            + action_form("kronos-reconcile", nonce, "Score forecasts whose horizon has passed")
            + "</div>"
        )
    if summary.get("note"):
        body += f"<p class='small muted' style='margin-top:10px'>{fmt.text(str(summary['note']))}</p>"
    return section(
        "Recorded forecasts and what happened",
        body,
        note="Direction, absolute error and band coverage only. No trading costs are included, "
        "so these are not profit figures.",
        tools=chip(f"{len(recent)} RECORD(S)", "muted"),
        id="history",
    )


def _benchmark_section(view: Mapping[str, Any], nonce: str) -> str:
    settings = dict(view.get("settings") or {})
    baselines = [dict(item) for item in (view.get("baselines") or ())]
    benchmark = dict(view.get("benchmark") or {})
    timeframes = [str(item) for item in (view.get("timeframes") or ())]
    body = table(
        [
            ("Baseline", "text"),
            ("What it does", "text"),
        ],
        [
            [
                cell(f"<code>{fmt.text(str(item.get('key', '')))}</code>", label="Baseline"),
                cell(fmt.text(str(item.get("note", ""))), label="What it does"),
            ]
            for item in baselines
        ],
        caption="The configured comparison set",
    )
    body += (
        "<form method='post' action='/actions/kronos-benchmark' class='form' "
        "style='margin-top:12px'>"
        f"<input type='hidden' name='nonce' value='{fmt.attr(nonce)}'>"
        "<div class='form-row'>"
        + field(
            "symbol",
            "Symbol",
            value=str(settings.get("symbol", "")),
            field_id="kronos-benchmark-symbol",
        )
        + field(
            "timeframe",
            "Timeframe",
            kind="select",
            value=str(settings.get("timeframe", "")),
            options=[(item, item) for item in timeframes],
            field_id="kronos-benchmark-timeframe",
        )
        + field(
            "include_kronos",
            "Include Kronos",
            kind="select",
            value="yes",
            options=[("yes", "yes, if it is loaded"), ("no", "no, baselines only")],
            field_id="kronos-benchmark-kronos",
        )
        + "</div><div class='actions' style='margin-top:12px'>"
        "<button class='btn btn--primary' type='submit'>Run the benchmark</button>"
        "</div></form>"
    )
    scores = [dict(item) for item in (benchmark.get("scores") or ())]
    if scores:
        body += (
            "<h3 style='margin-top:18px'>The last run</h3>"
            + facts(
                [
                    ("Ran at", fmt.text(str(benchmark.get("ran_at", "")))),
                    (
                        "Series",
                        f"{fmt.text(str(benchmark.get('symbol', '')))} "
                        f"{fmt.text(str(benchmark.get('timeframe', '')))}",
                    ),
                    ("Steps scored", fmt.text(str(benchmark.get("steps", "")))),
                ]
            )
            + table(
                [
                    ("Model", "text"),
                    ("Observations", "num"),
                    ("Direction", "num"),
                    ("Mean abs error", "num"),
                    ("Band coverage", "num"),
                    ("Spread-adjusted move", "num"),
                ],
                [
                    [
                        cell(f"<code>{fmt.text(str(row.get('key', '')))}</code>", label="Model"),
                        cell(
                            fmt.text(str(row.get("observations", ""))),
                            label="Observations",
                            num=True,
                        ),
                        cell(
                            fmt.text(str(row.get("direction_rate", "—"))),
                            label="Direction",
                            num=True,
                        ),
                        cell(
                            fmt.text(str(row.get("mean_absolute_error", "—"))),
                            label="Mean abs error",
                            num=True,
                        ),
                        cell(
                            fmt.text(str(row.get("band_coverage", "—"))),
                            label="Band coverage",
                            num=True,
                        ),
                        cell(
                            fmt.text(str(row.get("mean_spread_adjusted_move", "—"))),
                            label="Spread-adjusted move",
                            num=True,
                        ),
                    ]
                    for row in sorted(scores, key=lambda item: str(item.get("key", "")))
                ],
                caption="Forecast error and spread-adjusted direction. Not profit.",
            )
        )
        notes = [str(item) for item in (benchmark.get("notes") or ())]
        if notes:
            body += (
                "<ul class='check-list' style='margin-top:12px'>"
                + "".join(f"<li>{fmt.text(note)}</li>" for note in notes)
                + "</ul>"
            )
    return section(
        "Compare against simple baselines",
        body,
        note="Chronological out-of-sample window only, nothing fitted on it. A benchmark score is "
        "never evidence of profit.",
        id="benchmark",
    )


def _mapping_section(view: Mapping[str, Any]) -> str:
    mapping = [dict(item) for item in (view.get("mapping") or ())]
    return section(
        "What is sent to the model",
        table(
            [
                ("Kronos column", "text"),
                ("AXIOM source", "text"),
                ("Basis", "text"),
            ],
            [
                [
                    cell(f"<code>{fmt.text(str(item.get('column', '')))}</code>", label="Kronos column"),
                    cell(fmt.text(str(item.get("source", ""))), label="AXIOM source"),
                    cell(chip(str(item.get("label", "")), "muted"), label="Basis"),
                ]
                for item in mapping
            ],
            caption="The input mapping, field by field",
        ),
        note="Mid prices are not tradable, and MT5 tick volume is a count of price updates, not "
        "exchange volume. Nothing absent is invented.",
        id="mapping",
    )


def _unsupported_section(view: Mapping[str, Any]) -> str:
    unsupported = [dict(item) for item in (view.get("unsupported") or ())]
    return section(
        "Settings Kronos does not support",
        "<ul class='check-list'>"
        + "".join(
            f"<li><code>{fmt.text(str(item.get('setting', '')))}</code> "
            f"{fmt.text(str(item.get('why', '')))}</li>"
            for item in unsupported
        )
        + "</ul>",
        note="Documented rather than invented. If you need one of these, it is not a setting to "
        "add here -- it is a different model.",
        id="unsupported",
    )


def _boundaries_section(view: Mapping[str, Any]) -> str:
    notes = [str(item) for item in (view.get("notes") or ())]
    return section(
        "How a forecast could ever reach an order",
        "<p class='small muted' style='margin:0 0 10px'>A forecast is not a signal, and this page "
        "cannot configure one. Using it in trading requires a separately specified strategy, an "
        "explicit operator selection of that strategy, the existing risk gate, a broker-side "
        "protective stop for every position, and a PAPER/LIVE mode the operator chose. Until "
        "results exist on your broker's data, any strategy built on it is labelled UNVALIDATED."
        "</p><ul class='check-list'>"
        + "".join(f"<li>{fmt.text(note)}</li>" for note in notes)
        + "</ul>",
        note="Nothing on this page can create an order, size a position or change a mode.",
        tools=chip("forecast only", "paper"),
        id="boundaries",
    )


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------


def render_kronos(ctx: Any) -> str:
    view = dict((ctx.extra or {}).get("kronos") or {})
    nonce = str(getattr(ctx, "action_nonce", ""))

    head = page_head(
        "Kronos forecast",
        "An optional forecast of the next bars",
        "Install it, run one forecast or a schedule, and read what the model got wrong. "
        "Nothing here creates a signal, sizes a position, arms execution or places an order.",
        badges=common.scope_badges(ctx)
        + (status_chip(view) if view.get("available") else chip("not wired", "muted"))
        + chip("forecast only", "paper"),
        actions=button_link("/data", "Data &amp; evaluation", small=True)
        + button_link("/autopilot", "PAPER command center", small=True),
    )

    if not view.get("available"):
        return head + layout(
            notice(
                "The Kronos panel is not wired into this workstation",
                "This process has no forecasting component, so there is nothing to configure "
                "here. It is optional and never required for trading.",
                tone="warn",
            )
            + common.safety_section(id="safety"),
            wide=True,
        )
    if view.get("error"):
        return head + layout(
            notice(
                "The forecast panel could not be read",
                f"It reported <code>{fmt.text(str(view.get('error')))}</code>. Nothing was "
                "loaded, forecast or written, and the rest of the workstation is unaffected.",
                tone="danger",
                role="alert",
            )
            + common.safety_section(id="safety"),
            wide=True,
        )

    state = dict(view.get("state") or {})
    body = notice(
        "Forecast only — this page cannot place an order",
        "Kronos estimates the next bars. Nothing here sizes a position, sets a stop, changes an "
        "execution mode, or creates an order. Using a forecast in a strategy is a separate, "
        "explicit configuration through the existing risk gate.",
        tone="info",
    )
    if not view.get("has_bar_source"):
        body += notice(
            "No bar source is wired in",
            "Forecasting needs broker bars. Start this workstation against your MetaTrader 5 "
            "terminal with a connected session, or import bars on the Data page. AXIOM will not "
            "invent prices to fill the gap, so until then this page can validate settings but "
            "cannot produce a forecast.",
            tone="warn",
        )

    result = dict(view.get("result") or {})
    if result.get("message"):
        body += notice(
            "The last Kronos action",
            fmt.text(str(result.get("message"))),
            tone="ok" if result.get("ok") else "warn",
            role="status" if result.get("ok") else "alert",
        )

    jumps = [
        ("install", "Install"),
        ("forecast", "Forecast"),
        ("status", "Status"),
        ("chart", "Chart"),
        ("run", "Run"),
        ("estimate", "Before you load"),
        ("history", "History"),
        ("benchmark", "Benchmark"),
    ]
    main = (
        body
        + jump_nav_bar(jumps)
        + jump_nav(jumps)
        + _install_section(view, nonce)
        + _forecast_section(view, nonce)
        + _status_section(view)
        + _chart_section(view)
        + _last_section(view)
        + _run_section(view, nonce)
        + "<details class='disclosure'><summary>Advanced settings — model variants, custom "
        "paths, revisions, and hardware overrides</summary>"
        + _settings_section(view, nonce)
        + "<p class='small muted' style='margin:8px 0 0'>The recommended install needs none of "
        "this. Open it only to use a different variant, to point at files you placed yourself, "
        "to pin a different revision, or to force a device.</p></details>"
        + _estimate_section(view)
        + _weights_section(view, nonce)
        + _history_section(view, nonce)
        + _benchmark_section(view, nonce)
        + _mapping_section(view)
        + _unsupported_section(view)
        + _boundaries_section(view)
    )
    del state
    return head + layout(main, wide=True)
