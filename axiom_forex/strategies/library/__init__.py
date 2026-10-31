"""A curated, extensible library of documented FX strategy templates.

The library is deliberately small at the top and wide at the bottom: a handful of
templates, each traceable to a published description, plus the parameter variants
of those templates that AXIOM is willing to *attempt*.  "Large" here is measured in
recorded attempts, not in claims.

Modules:

``cards``         the templates: rules, parameters, supported timeframes, horizon,
                  assumptions, limitations, source and licence -- and which ones
                  this deployment cannot supply data for.
``rules``         the executable rule for each evaluable template.
``variants``      the predeclared parameter grid, the attempt ceiling, and the
                  adapter that plugs a variant into the engine.
``multiplicity``  block-bootstrap p-values, Benjamini--Hochberg, and White's
                  Reality Check.
``ledger``        the durable record of every attempt and of the operator's own
                  selection.
``monitor``       the predeclared screen, the search-level report, and which
                  symbols/timeframes can be monitored at all.
``panel``         the state behind the ``/strategies`` page.

Nothing in this package places an order, sizes a position, arms live execution, or
changes an execution mode.  A selection is a label on a ledger row; acting on it is
the live path's job, with all of its own gates.
"""

from __future__ import annotations

__all__: list[str] = []
