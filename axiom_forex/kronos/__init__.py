"""Kronos as an optional, replaceable forecasting component -- never an oracle.

The official project (``shiyu-coder/Kronos``, MIT) is a foundation model that
predicts the next few K-lines.  It is used here for exactly that, and for nothing
else:

* it **forecasts**; it does not decide, size, arm, or send anything;
* it runs only if an operator installs its runtime, points at the official code
  and pinned weights, and presses Load -- nothing here downloads, installs, or
  starts by itself;
* every forecast is stored with its model, revision, settings fingerprint, and
  input quality, and is scored later against realised bars, with the baselines'
  numbers beside it;
* it is ``FORECAST_ONLY_NOT_A_SIGNAL`` until an operator explicitly configures a
  separate strategy to read it, and even then the existing risk gate, the
  protective-stop requirement, and the PAPER/LIVE distinction still apply.

Modules, in the order they are used:

``registry``   the official facts: licence, commit, variants, context lengths,
               the predictor's own arguments, and what the model does *not*
               support.
``settings``   every inference setting, its model limit, validation, and named
               presets.
``inputs``     the explicit bid/ask -> mid OHLCV mapping, and the data-quality
               report that travels with every forecast.
``runtime``    can it run here, roughly what it will cost, and the
               NOT_LOADED/READY/FAILED/STOPPED state machine.
``forecast``   rollouts -> median and bands, plus the recorded forecast object.
``history``    the SQLite forecast history and its outcome reconciliation.
``benchmark``  per pair and timeframe, against hold/drift/momentum baselines, on
               the chronological out-of-sample window.

Nothing in this package can place an order, change an execution mode, or edit a
risk setting: those objects are not imported here at all.
"""

from __future__ import annotations

__all__: list[str] = []
