from __future__ import annotations

from html import escape
from typing import Any, Mapping

from app.audit.log import sanitize_audit_value


def health_rows(status: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    health = status.get("health", {})
    rows = [("Overall", str(health.get("status", "UNKNOWN")), "")]
    for check in health.get("checks", []):
        rows.append(
            (
                str(check.get("name", "")),
                "OK" if check.get("ok") else "ATTENTION",
                str(check.get("detail", "")),
            )
        )
    return rows


def risk_rows(status: Mapping[str, Any]) -> list[tuple[str, str]]:
    portfolio = status.get("portfolio", {})
    keys = (
        "equity_account",
        "gross_exposure_account",
        "margin_account",
        "effective_leverage",
        "open_positions",
        "trades_today",
        "cost_total_account",
        "rollover_total_account",
    )
    return [(key, str(portfolio.get(key, "n/a"))) for key in keys]


def calendar_rows(status: Mapping[str, Any]) -> list[tuple[str, str]]:
    calendar = status.get("calendar", {})
    return [(str(key), str(value)) for key, value in calendar.items()]


def workflow_rows(status: Mapping[str, Any]) -> list[tuple[str, str]]:
    workflow = status.get("workflow", {})
    health = status.get("health", {})
    persistence = workflow.get("persistence", {})
    return [
        ("Workflow", str(workflow.get("workflow_id", "not-run"))),
        ("Executed", str(workflow.get("executed", False))),
        ("Blocked", str(workflow.get("blocked", False))),
        ("Reason", str(workflow.get("reason", "none"))),
        ("Health", str(health.get("status", "UNKNOWN"))),
        ("Orders", str(persistence.get("orders", 0))),
        ("Fills", str(persistence.get("fills", 0))),
    ]


def market_access_rows(status: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Presence and storage health only -- never the stored value."""

    setting = status.get("market_access", {})
    storage = setting.get("secure_storage", {})
    return [
        ("Purpose", str(setting.get("purpose", "MARKET_DATA_READ_ONLY"))),
        ("Broker trading use", "no" if not setting.get("broker_trading_use") else "yes"),
        (
            "Configured",
            "Configured" if setting.get("configured") else "Not configured",
        ),
        ("Stored in", str(setting.get("storage", "os_credential_vault"))),
        ("Secure storage available", str(storage.get("available", False))),
        ("Plaintext fallback", str(setting.get("plaintext_fallback", False))),
        ("Provider selected", str(setting.get("provider_selected", False))),
        ("Used for requests", str(setting.get("used_for_requests", False))),
    ]


def conformance_rows(status: Mapping[str, Any]) -> list[tuple[str, str]]:
    report = status.get("conformance", {})
    required_values = report.get("required_values", [])
    supplied = sum(1 for item in required_values if item.get("supplied"))
    return [
        ("Suite", str(report.get("suite_id", "not-run"))),
        ("Provider/broker conformance", "PASS" if report.get("passed") else "FAIL"),
        ("Status", str(report.get("status", "UNKNOWN"))),
        ("Checks", str(report.get("check_count", 0))),
        ("Errors", str(report.get("error_count", 0))),
        ("Required values supplied", f"{supplied}/{len(required_values)}"),
    ]


def profile_rows(status: Mapping[str, Any]) -> list[tuple[str, str]]:
    report = status.get("provider_profile", {})
    identity = report.get("profile", {})
    validation = report.get("validation", {})
    activation = report.get("activation", {})
    return [
        ("Profile", str(identity.get("profile_id", "not-loaded"))),
        ("Version", str(identity.get("profile_version", "unknown"))),
        ("Environment", str(identity.get("environment", "UNKNOWN"))),
        ("Provider profile onboarding", "PASS" if report.get("passed") else "FAIL"),
        ("Validation status", str(validation.get("status", "UNKNOWN"))),
        ("Activation", str(activation.get("status", "BLOCKED"))),
        ("Missing fields", str(len(report.get("missing_fields", [])))),
        ("Invalid fields", str(len(report.get("invalid_fields", [])))),
        ("Stale fields", str(len(report.get("stale_fields", [])))),
        ("Compatibility failures", str(len(report.get("compatibility_failures", [])))),
        ("External execution", str(identity.get("external_execution_enabled", False))),
    ]


def data_provenance_rows(status: Mapping[str, Any]) -> list[tuple[str, str]]:
    report = status.get("data_provenance", {})
    manifest = report.get("dataset_manifest", {})
    import_manifest = report.get("import_manifest") or {}
    latest = report.get("latest_import") or {}
    return [
        ("Data class", str(report.get("data_class", "UNKNOWN"))),
        ("Imported data", str(report.get("imported", False))),
        ("Execution eligible", str(report.get("execution_eligible", False))),
        ("Source", str(report.get("source", "unknown"))),
        ("Source version", str(report.get("source_version", "unknown"))),
        ("Timezone basis", str(report.get("timezone_basis", "unknown"))),
        ("Dataset", str(report.get("dataset_id", "unknown"))),
        ("Bars", str(manifest.get("bar_count", 0))),
        ("Coverage from", str(manifest.get("first_timestamp", "n/a"))),
        ("Coverage to", str(manifest.get("last_timestamp", "n/a"))),
        ("Validation", str(manifest.get("validation_classification", "n/a"))),
        ("Import manifest valid", str(report.get("import_manifest_valid", False))),
        ("Import source hash", str(import_manifest.get("source_sha256", "n/a"))[:16]),
        ("Latest import", str(latest.get("import_id", "none"))),
        ("Latest import class", str(latest.get("data_class", "none"))),
        ("Latest import valid", str(latest.get("manifest_valid", False))),
        ("Latest import adopted", str(latest.get("adopted_for_research", False))),
    ]


def _table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    head = "".join(f"<th>{escape(header)}</th>" for header in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{escape(str(cell))}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_dashboard(status: Mapping[str, Any]) -> str:
    clean = sanitize_audit_value(dict(status))
    if not isinstance(clean, dict):
        clean = {}
    title = escape("AXIOM-FOREX research dashboard")
    health = _table(("Check", "Status", "Detail"), health_rows(clean))
    risk = _table(("Metric", "Value"), risk_rows(clean))
    calendar = _table(("Calendar", "Value"), calendar_rows(clean))
    workflow = _table(("Workflow", "Value"), workflow_rows(clean))
    conformance = _table(("Conformance", "Value"), conformance_rows(clean))
    profile = _table(("Profile onboarding", "Value"), profile_rows(clean))
    provenance = _table(("Data provenance", "Value"), data_provenance_rows(clean))
    market_access = _table(("Market access setting", "Value"), market_access_rows(clean))
    safety = escape(
        "Research/paper only. Live bridge disabled. No order controls are exposed."
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title>"
        "<style>body{font-family:system-ui;margin:2rem;color:#17202a}"
        "table{border-collapse:collapse;margin-bottom:1.5rem;width:100%}"
        "th,td{border:1px solid #ccd1d1;padding:.45rem;text-align:left}"
        "th{background:#eef2f3}h1,h2{color:#1b4f72}.safety{background:#fef9e7;padding:1rem}"
        "</style></head><body>"
        f"<h1>{title}</h1><p class='safety'>{safety}</p>"
        f"<h2>Health</h2>{health}<h2>Offline paper workflow</h2>{workflow}"
        f"<h2>Provider/broker conformance</h2>{conformance}"
        f"<h2>Provider profile onboarding</h2>{profile}"
        f"<h2>Data provenance</h2>{provenance}"
        f"<h2>Market access setting</h2>{market_access}"
        "<p>The market access token is held only by the operating-system "
        "credential vault. This page shows whether one is configured, never its "
        "value, and no data-provider request uses it in this build.</p>"
        f"<h2>Risk and portfolio</h2>{risk}"
        f"<h2>Calendar</h2>{calendar}</body></html>"
    )
