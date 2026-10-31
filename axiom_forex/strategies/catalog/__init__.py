"""A large, reproducible FX strategy catalog, declared before anything is measured.

The package is split so that "what exists" can be audited without running it:

``indicators``  causal array indicators, memoised per series.
``primitives``  the declared entry, exit and filter blocks with their fixed grids.
``candidates``  the catalog itself: stable ids, plain-language logic, families,
                supported timeframes, and the three separate counts (candidates,
                distinct signatures, distinct rule structures).
``simulate``    the causal per-candidate simulation: fills at the next bar's open
                on the side the trade pays, ATR brackets and trailing stops, costs,
                and no peeking.
``evaluate``    the chronological screening run: train / validation / test, walk
                forward, a wall-clock budget, and a report that lists what was
                measured, skipped or failed and why.
``store``       the durable record of one evaluation, so a paper test can only
                start from a set that was actually evaluated.

Nothing in this package can place an order, size a position, arm live execution,
or change an execution mode.
"""

from __future__ import annotations

__all__: list[str] = []
