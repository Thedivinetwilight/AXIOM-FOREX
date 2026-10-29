"""Offline provider profile onboarding entry point."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .loader import load_provider_profile
from .paths import DEFAULT_FIXTURE_PROFILE_PATH
from .validation import validate_provider_profile
from .contracts import ProviderProfileReport


@dataclass(frozen=True)
class ProviderProfileOnboarding:
    source: Mapping[str, Any] | str | Path
    evaluated_at: datetime | None = None

    def run(self) -> ProviderProfileReport:
        profile = load_provider_profile(self.source)
        return validate_provider_profile(profile, evaluated_at=self.evaluated_at)

    def audit_view(self) -> dict[str, Any]:
        return self.run().audit_view()


def default_provider_profile_onboarding() -> ProviderProfileOnboarding:
    return ProviderProfileOnboarding(DEFAULT_FIXTURE_PROFILE_PATH)


def run_default_provider_profile_onboarding() -> ProviderProfileReport:
    return default_provider_profile_onboarding().run()
