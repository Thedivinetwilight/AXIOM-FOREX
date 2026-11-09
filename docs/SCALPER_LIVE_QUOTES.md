# AX-SCALPER live-quote PAPER handoff

`axiom_forex.scalper.live_quotes.MT5LiveQuoteSource` is a read-only bridge for
code that already owns AXIOM's `ObservationSession`. Construct it only after the
operator presses **Connect** in the workstation. The source checks the session's
connected state on every read and obtains the current read-only terminal with
`market_data_source()`; it never calls `connect()`, initializes MetaTrader 5, or
reconnects. A disconnection during a read rejects the quote.

`LivePaperPump` accepts an already-constructed PAPER `ScalperDesk`, its exact
configured symbol, and the source. `start()` refuses before connection. The pump
labels every quote `MT5_LIVE_PAPER`, passes broker time and local UTC receipt time
through the desk's normal TickCore, and pauses when the session disconnects. The
TickCore handles duplicate, out-of-order, stale, and invalid prices. The pump has
no broker execution object; its only execution boundary is the desk's configured
paper venue.

## Integration status and blockers

This is an explicit Python handoff API, **not yet a wired Autopilot UI action or a
Windows/MT5-tested live quote runner**. The workstation does not yet construct an
AX-SCALPER desk from its verified symbol facts, operator-declared costs, paper
capital, and enforceable risk policy, nor does it persist/recover the AX-SCALPER
position book across a process restart. Therefore the existing workstation does
not expose a Start button for this pump, and the pump must not be used to open
positions in a production session until those integrations and recovery checks
are added. No fixture configuration should be substituted for live operator
terms. Existing bar-based Autopilot and its PAPER/LIVE paths are unchanged.

The source and pump can be exercised with fakes, which proves that this bridge
itself only reads quotes. This is not evidence of profitability or readiness for
live trading. PAPER simulated fills are not broker orders or broker positions.
