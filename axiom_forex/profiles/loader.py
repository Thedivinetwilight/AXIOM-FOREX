"""Strict, dependency-free loading for versioned provider profile documents."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from axiom_forex.conformance.contracts import REQUIRED_PROVIDER_VALUES

from .contracts import (
    DEFAULT_SECTION_KEYS,
    PROFILE_FORMAT,
    ParseIssue,
    ProfileError,
    ProfileProvenance,
    ProfileTerm,
    ProviderProfile,
    REQUIRED_SECTIONS,
)


def read_profile_payload(source: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    """Return a deep-copied profile payload from a mapping or local JSON file."""
    if isinstance(source, Mapping):
        return deepcopy(dict(source))
    if isinstance(source, (str, Path)):
        path = Path(source)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise ProfileError(f"unreadable_profile:{type(exc).__name__}") from exc
        if not isinstance(payload, dict):
            raise ProfileError("profile_payload_must_be_object")
        return payload
    raise ProfileError("unsupported_profile_source")


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _bool(value: Any) -> bool:
    return value is True


def _parse_term(key: str, view: Any) -> ProfileTerm:
    data = view if isinstance(view, Mapping) else {}
    return ProfileTerm(
        key=key,
        value=deepcopy(data.get("value")),
        provenance=ProfileProvenance.from_mapping(data.get("provenance")),
    )


def load_provider_profile(
    source: Mapping[str, Any] | str | Path,
) -> ProviderProfile:
    """Load a profile without inventing values.

    Structural problems are captured as parse issues so validation can report
    them by field.  Only unreadable/non-object sources raise ``ProfileError``.
    """
    payload = read_profile_payload(source)
    issues: list[ParseIssue] = []

    format_value = _text(payload.get("format"))
    schema_version = _text(payload.get("schema_version"))
    if format_value != PROFILE_FORMAT:
        issues.append(
            ParseIssue(
                field="format",
                code="unsupported_profile_format",
                detail="format must match the supported provider-profile identifier",
            )
        )
    if not schema_version:
        issues.append(
            ParseIssue(
                field="schema_version",
                code="missing_schema_version",
                detail="schema_version is required",
            )
        )

    raw_profile = payload.get("profile")
    if not isinstance(raw_profile, Mapping):
        issues.append(
            ParseIssue(
                field="profile",
                code="missing_profile_identity",
                detail="profile identity object is required",
            )
        )
        raw_profile = {}

    def required_text(name: str) -> str:
        value = _text(raw_profile.get(name))
        if not value.strip():
            issues.append(
                ParseIssue(
                    field=f"profile.{name}",
                    code="missing_profile_field",
                    detail=f"profile.{name} is required",
                )
            )
        return value

    def required_bool(name: str) -> bool:
        value = raw_profile.get(name)
        if not isinstance(value, bool):
            issues.append(
                ParseIssue(
                    field=f"profile.{name}",
                    code="invalid_profile_boolean",
                    detail=f"profile.{name} must be an explicit boolean",
                )
            )
            return False
        return value

    profile_id = required_text("profile_id")
    profile_version = required_text("profile_version")
    environment = required_text("environment").upper()
    label = required_text("label")
    synthetic = required_bool("synthetic")
    external_execution_enabled = required_bool("external_execution_enabled")
    activation_mode = required_text("activation_mode").upper()
    evaluated_at = required_text("evaluated_at")
    effective_from = required_text("effective_from")
    expires_at = required_text("expires_at")
    review_by = required_text("review_by")

    raw_sections = payload.get("sections")
    if not isinstance(raw_sections, Mapping):
        issues.append(
            ParseIssue(
                field="sections",
                code="missing_sections",
                detail="sections object is required",
            )
        )
        raw_sections = {}
    sections: dict[str, tuple[str, ...]] = {}
    for section in REQUIRED_SECTIONS:
        value = raw_sections.get(section)
        if not isinstance(value, (list, tuple)) or not value:
            issues.append(
                ParseIssue(
                    field=f"sections.{section}",
                    code="missing_section_terms",
                    detail=f"sections.{section} must list term keys",
                )
            )
            sections[section] = DEFAULT_SECTION_KEYS[section]
            continue
        keys: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                issues.append(
                    ParseIssue(
                        field=f"sections.{section}",
                        code="invalid_section_term_key",
                        detail="section term keys must be non-empty strings",
                    )
                )
                continue
            keys.append(item)
        sections[section] = tuple(keys)
    for section, value in raw_sections.items():
        if section not in sections and isinstance(value, (list, tuple)):
            sections[str(section)] = tuple(
                str(item) for item in value if isinstance(item, str) and item.strip()
            )

    raw_terms = payload.get("terms")
    if not isinstance(raw_terms, Mapping):
        issues.append(
            ParseIssue(
                field="terms",
                code="missing_terms",
                detail="terms object is required",
            )
        )
        raw_terms = {}
    terms: dict[str, ProfileTerm] = {}
    for key, view in raw_terms.items():
        if not isinstance(key, str) or not key.strip():
            issues.append(
                ParseIssue(
                    field="terms",
                    code="invalid_term_key",
                    detail="term keys must be non-empty strings",
                )
            )
            continue
        if not isinstance(view, Mapping):
            issues.append(
                ParseIssue(
                    field=f"terms.{key}",
                    code="invalid_term_object",
                    detail="each term must be an object with value and provenance",
                )
            )
            terms[key] = _parse_term(key, {})
            continue
        if "value" not in view:
            issues.append(
                ParseIssue(
                    field=f"terms.{key}.value",
                    code="missing_term_value",
                    detail="term value must be present, even when null",
                )
            )
        if not isinstance(view.get("provenance"), Mapping):
            issues.append(
                ParseIssue(
                    field=f"terms.{key}.provenance",
                    code="missing_term_provenance",
                    detail="term provenance object is required",
                )
            )
        terms[key] = _parse_term(key, view)
    for required in REQUIRED_PROVIDER_VALUES:
        if required.key not in terms:
            issues.append(
                ParseIssue(
                    field=f"terms.{required.key}",
                    code="missing_required_term",
                    detail="required term is absent",
                )
            )
            terms[required.key] = ProfileTerm(
                key=required.key,
                value=None,
                provenance=ProfileProvenance(),
            )

    return ProviderProfile(
        format=format_value,
        schema_version=schema_version,
        profile_id=profile_id,
        profile_version=profile_version,
        environment=environment,
        label=label,
        synthetic=synthetic,
        external_execution_enabled=external_execution_enabled,
        activation_mode=activation_mode,
        evaluated_at=evaluated_at,
        effective_from=effective_from,
        expires_at=expires_at,
        review_by=review_by,
        sections=sections,
        terms=terms,
        parse_issues=tuple(issues),
    )
