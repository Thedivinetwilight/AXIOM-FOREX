from __future__ import annotations

from typing import Any, Mapping

from app.audit.log import sanitize_audit_value

from .contract import contract_audit


def build_viewmodel(platform: Any) -> dict[str, Any]:
    status = sanitize_audit_value(platform.status())
    if not isinstance(status, dict):
        status = {}
    return {
        "app": "AXIOM-FOREX",
        "version": platform.version,
        "mode": "research_and_paper_only",
        "live_controls": [],
        "api_requests": contract_audit(),
        "status": status,
        "workflow": status.get("workflow", {}),
        "workflow_detail": platform.workflow_status(),
        "conformance": status.get("conformance", {}),
        "conformance_detail": sanitize_audit_value(platform.conformance_report()),
        # The market access setting: presence only, never the stored value.
        "market_access": status.get("market_access", {}),
        "profile": status.get("provider_profile", {}),
        "profile_detail": sanitize_audit_value(platform.provider_profile_report()),
        "dataset_provenance": status.get("data_provenance", {}),
        "dataset_provenance_detail": sanitize_audit_value(
            platform.dataset_provenance_report()
        ),
        "boundaries": [
            boundary.audit_view() for boundary in platform.boundaries
        ],
    }
