"""Repository-local paths for provider profile documents."""

from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PROFILE_DIRECTORY = REPOSITORY_ROOT / "profiles"
DEFAULT_FIXTURE_PROFILE_PATH = PROFILE_DIRECTORY / "offline_fixture_profile.json"
DEFAULT_TEMPLATE_PROFILE_PATH = PROFILE_DIRECTORY / "provider_onboarding_template.json"
