from __future__ import annotations

from typing import Any, Mapping

from app.audit.log import sanitize_audit_value


def _lines(status: Mapping[str, Any]) -> list[str]:
    health = status.get("health", {})
    settings = status.get("settings", {})
    dataset = status.get("dataset", {})
    calendar = status.get("calendar", {})
    portfolio = status.get("portfolio", {})
    decisions = status.get("open_decisions", [])
    backtests = status.get("backtests", [])
    workflow = status.get("workflow", {})
    workflow_persistence = workflow.get("persistence", {})
    conformance = status.get("conformance", {})
    profile_report = status.get("provider_profile", {})
    profile_identity = profile_report.get("profile", {})
    profile_validation = profile_report.get("validation", {})
    profile_activation = profile_report.get("activation", {})
    provenance = status.get("data_provenance", {})
    market_access = status.get("market_access", {})
    market_storage = market_access.get("secure_storage", {})
    provenance_manifest = provenance.get("dataset_manifest", {})
    import_manifest = provenance.get("import_manifest") or {}
    latest_import = provenance.get("latest_import") or {}
    required_values = conformance.get("required_values", [])
    supplied_values = sum(
        1 for item in required_values if item.get("supplied")
    )
    lines = [
        "# AXIOM-FOREX verification report",
        "",
        "This is a research/paper-only adaptation. It is not a claim of profitability or live-trading readiness.",
        "",
        "## Safety boundary",
        "",
        f"- Research only: `{settings.get('research_only')}`",
        f"- Live bridge enabled: `{settings.get('live_bridge_enabled')}`",
        f"- Health status: `{health.get('status')}`",
        "",
        "## Market access setting",
        "",
        "- Purpose: future market-data provider (read-only). Not a broker trading credential.",
        f"- Configured: `{bool(market_access.get('configured'))}`",
        f"- Stored in: `{market_access.get('storage', 'os_credential_vault')}`",
        f"- Secure storage available: `{market_storage.get('available')}`",
        f"- Plaintext fallback: `{market_access.get('plaintext_fallback')}`",
        f"- Provider selected: `{bool(market_access.get('provider_selected'))}`",
        f"- Used for data-provider requests: `{bool(market_access.get('used_for_requests'))}`",
        "",
        "The token value is never written to this report, the database, the audit trail, or any API response; only its presence is shown.",
        "",
        "## Provider/broker conformance",
        "",
        f"- Provider/broker conformance: `{'PASS' if conformance.get('passed') else 'FAIL'}`",
        f"- Suite: `{conformance.get('suite_id', 'not-run')}`",
        f"- Checks: `{conformance.get('check_count', 0)}`",
        f"- Errors: `{conformance.get('error_count', 0)}`",
        f"- Required provider-specific values supplied: `{supplied_values}/{len(required_values)}`",
        "",
        "This conformance result validates offline contracts and deterministic fixtures only. It does not enable an external provider, broker, account, or live order path.",
        "",
        "## Provider profile onboarding",
        "",
        f"- Provider profile onboarding: `{'PASS' if profile_report.get('passed') else 'FAIL'}`",
        f"- Profile: `{profile_identity.get('profile_id', 'not-loaded')}`",
        f"- Profile version: `{profile_identity.get('profile_version', 'unknown')}`",
        f"- Environment: `{profile_identity.get('environment', 'UNKNOWN')}`",
        f"- Validation status: `{profile_validation.get('status', 'UNKNOWN')}`",
        f"- Activation: `{profile_activation.get('status', 'BLOCKED')}`",
        f"- Missing fields: `{len(profile_report.get('missing_fields', []))}`",
        f"- Invalid fields: `{len(profile_report.get('invalid_fields', []))}`",
        f"- Stale fields: `{len(profile_report.get('stale_fields', []))}`",
        f"- Contradictory fields: `{len(profile_report.get('contradictory_fields', []))}`",
        f"- Compatibility failures: `{len(profile_report.get('compatibility_failures', []))}`",
        "",
        "The profile view contains no term values, credentials, or sensitive account values. Only an explicitly synthetic local-paper fixture profile can be activated.",
        "",
        "## Data provenance",
        "",
        f"- Data class: `{provenance.get('data_class', 'UNKNOWN')}`",
        f"- Imported (user-supplied): `{provenance.get('imported', False)}`",
        f"- Execution eligible: `{provenance.get('execution_eligible', False)}`",
        f"- Source label: `{provenance.get('source', 'unknown')}`",
        f"- Source version: `{provenance.get('source_version', 'unknown')}`",
        f"- Timezone basis: `{provenance.get('timezone_basis', 'unknown')}`",
        f"- Bars: `{provenance_manifest.get('bar_count', 0)}`",
        f"- Coverage: `{provenance_manifest.get('first_timestamp', 'n/a')}` to `{provenance_manifest.get('last_timestamp', 'n/a')}`",
        f"- Validation: `{provenance_manifest.get('validation_classification', 'n/a')}`",
        f"- Import manifest valid: `{provenance.get('import_manifest_valid', False)}`",
        f"- Import source hash: `{str(import_manifest.get('source_sha256', 'n/a'))[:16]}`",
        f"- Latest imported dataset: `{latest_import.get('import_id', 'none')}`",
        f"- Latest imported data class: `{latest_import.get('data_class', 'none')}`",
        f"- Latest import manifest valid: `{latest_import.get('manifest_valid', False)}`",
        "",
        "Synthetic fixtures are generated locally. Imported data is user-supplied research material: it is never relabeled as a fixture or production feed, is never used for paper fills, and its reports contain no local file paths, credentials, or account values.",
        "",
        "## Offline fixture paper workflow",
        "",
        f"- Workflow: `{workflow.get('workflow_id', 'not-run')}`",
        f"- Executed: `{workflow.get('executed', False)}`",
        f"- Blocked: `{workflow.get('blocked', False)}`",
        f"- Reason: `{workflow.get('reason', 'none')}`",
        f"- Persisted orders: `{workflow_persistence.get('orders', 0)}`",
        f"- Persisted fills: `{workflow_persistence.get('fills', 0)}`",
        f"- Dataset: `{workflow.get('dataset', {}).get('dataset_id', dataset.get('dataset_id'))}`",
        "",
        "This workflow is a deterministic local simulation, not a profitability or live-readiness claim.",
        "",
        "## Data and calendar",
        "",
        f"- Dataset: `{dataset.get('dataset_id')}`",
        f"- Calendar signature: `{calendar.get('signature')}`",
        f"- Calendar source: `{calendar.get('source')}`",
        "",
        "## Portfolio snapshot",
        "",
        f"- Equity ({portfolio.get('account_currency')}): `{portfolio.get('equity_account')}`",
        f"- Gross exposure: `{portfolio.get('gross_exposure_account')}`",
        f"- Margin: `{portfolio.get('margin_account')}`",
        f"- Cost total: `{portfolio.get('cost_total_account')}`",
        f"- Rollover total: `{portfolio.get('rollover_total_account')}`",
        "",
        "## Backtests",
        "",
        f"- Recorded runs: `{len(backtests)}`",
        "",
        "## Decisions still requiring broker/provider conformance",
        "",
    ]
    for decision in decisions:
        lines.append(
            f"- `{decision.get('key')}`: {decision.get('provider_neutral_stance')}"
        )
    lines.extend(
        [
            "",
            "## Verification",
            "",
            "- Run the focused pytest suite.",
            "- Confirm no external broker or data-provider adapter is enabled.",
            "- Confirm repository-only changes have not been deployed to the running live system.",
            "",
        ]
    )
    return lines


def render_markdown_report(status: Mapping[str, Any]) -> str:
    clean = sanitize_audit_value(dict(status))
    if not isinstance(clean, dict):
        clean = {}
    return "\n".join(_lines(clean)) + "\n"
