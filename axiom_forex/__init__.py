"""Isolated AXIOM-FOREX adaptation of AXIOM's full architecture.

This package is repository-local and research/paper oriented.  It does not
import, configure, stop, restart, or deploy the running live system.  Concrete
broker and data-provider integrations remain explicit injection points.
"""

__version__ = "0.2.0-forex-adaptation"
__all__ = [
    "ForexPlatform",
    "PaperWorkflowResult",
    "build_fixture_platform",
    "execute_paper_workflow",
]

from .facade import ForexPlatform, build_fixture_platform
from .workflow import PaperWorkflowResult, execute_paper_workflow
