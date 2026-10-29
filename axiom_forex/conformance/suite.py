"""Offline provider/broker conformance suite entry point."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .checks import run_conformance_checks
from .contracts import ConformanceReport
from .fixtures import ConformanceEnvironment, build_conformance_environment


@dataclass(frozen=True)
class ConformanceSuite:
    environment: ConformanceEnvironment

    @classmethod
    def for_platform(cls, platform: Any | None = None) -> "ConformanceSuite":
        """Build a suite from explicit platform fixtures or standalone fixtures."""
        return cls(build_conformance_environment(platform))

    def run(self) -> ConformanceReport:
        return run_conformance_checks(self.environment)

    def audit_view(self) -> dict[str, Any]:
        return self.run().audit_view()


def run_default_conformance() -> ConformanceReport:
    return ConformanceSuite.for_platform().run()
