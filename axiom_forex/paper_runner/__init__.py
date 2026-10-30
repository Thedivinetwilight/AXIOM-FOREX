"""Paper-only strategy runner: live prices in, simulated fills out, nothing sent.

The runner reads completed bars from a feed (terminal ticks, or a labelled
offline replay), asks one documented candidate strategy for a signal, and passes
any signal through the *existing* planner, pre-trade gate, paper engine,
reservations, settlement and ledger.  It cannot call ``order_send``, cannot arm a
live session, and cannot change the execution mode: it refuses to run at all
unless the mode is ``PAPER``.

Modules
-------
``feed``
    Terminal tick aggregation and offline replay, with explicit stale/gap events.
``strategy``
    The candidate (``TrendPullbackV1``) and its baselines, plus candidate status.
``journal``
    The durable plan *before* an order, and the run state after each cycle.
``runner``
    The loop, its guards, its restart recovery, and its read model.
``evaluation``
    Chronological out-of-sample and walk-forward evaluation with predeclared
    robustness criteria and a one-use locked test window.
``data_audit``
    What historical data exists, where it came from, and what it is missing.
"""

from __future__ import annotations

__all__ = ["data_audit", "evaluation", "feed", "journal", "runner", "strategy"]
