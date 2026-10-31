"""Durable history of forecasts, and what actually happened after them.

A forecast is only worth reading if it can be checked later.  Two rows matter:

``kronos_forecast``
    One row per forecast, written **before** it is shown to anybody, holding the
    complete settings fingerprint, the model reference and revision, the input
    data's quality report, and every forecast point.  A stored forecast can be
    reproduced from this row alone: nothing is looked up from today's settings.

``kronos_outcome``
    Added when the horizon has elapsed and bars exist for it.  It records the
    realised close, the error, whether the direction was right, whether the band
    covered the outcome, and the same numbers for two free baselines -- "no move"
    (last observed close) and "straight line" (the forecast's own drift applied
    mechanically).  It records **no** profit figure: costs depend on the strategy
    that would trade it, and that is not this module's business.

Reconciliation is explicit and forward-only: a row is scored once, and the score
is never recomputed to make a number look better.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .forecast import Forecast

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS kronos_forecast (
    forecast_id        TEXT PRIMARY KEY,
    created_at         TEXT NOT NULL,
    symbol             TEXT NOT NULL,
    timeframe          TEXT NOT NULL,
    horizon_end        TEXT NOT NULL,
    model_key          TEXT NOT NULL,
    model_revision     TEXT NOT NULL,
    settings_fingerprint TEXT NOT NULL,
    label              TEXT NOT NULL,
    payload            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS kronos_forecast_symbol
    ON kronos_forecast (symbol, timeframe, created_at);
CREATE TABLE IF NOT EXISTS kronos_outcome (
    forecast_id        TEXT PRIMARY KEY,
    scored_at          TEXT NOT NULL,
    realised_close     TEXT NOT NULL,
    realised_return    TEXT NOT NULL,
    forecast_return    TEXT NOT NULL,
    direction_correct  INTEGER NOT NULL,
    absolute_error     TEXT NOT NULL,
    band_covered       INTEGER NOT NULL,
    baseline_flat_error TEXT NOT NULL,
    baseline_drift_error TEXT NOT NULL,
    scored_against     TEXT NOT NULL,
    note               TEXT NOT NULL
);
"""

SCORING_NOTE = (
    "Scored against realised mid prices from the same feed. Direction, absolute "
    "error and band coverage only. Spread, commission, swap and slippage are NOT "
    "included, so this is not a profit figure and cannot be used as one."
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Outcome:
    forecast_id: str
    scored_at: str
    realised_close: Decimal
    realised_return: Decimal
    forecast_return: Decimal
    direction_correct: bool
    absolute_error: Decimal
    band_covered: bool
    baseline_flat_error: Decimal
    baseline_drift_error: Decimal
    scored_against: str
    note: str = SCORING_NOTE

    def audit_view(self) -> dict[str, Any]:
        return {
            "forecast_id": self.forecast_id,
            "scored_at": self.scored_at,
            "realised_close": str(self.realised_close),
            "realised_return": str(self.realised_return),
            "forecast_return": str(self.forecast_return),
            "direction_correct": self.direction_correct,
            "absolute_error": str(self.absolute_error),
            "band_covered": self.band_covered,
            "baseline_flat_error": str(self.baseline_flat_error),
            "baseline_drift_error": str(self.baseline_drift_error),
            "scored_against": self.scored_against,
            "note": self.note,
        }


class HistoryError(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class ForecastStore:
    """One SQLite file, one connection per operation.

    The scheduled loop writes from a worker thread and the page reads from the HTTP
    thread, so a connection is opened per call: no handle is ever shared across
    threads, which is the failure mode a long-lived handle would create.
    """

    def __init__(
        self, path: str | Path, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self.path = Path(path)
        self.clock = clock or _utc_now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(DDL)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10.0)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    # -- writes ---------------------------------------------------------

    def record(self, forecast: Forecast) -> None:
        horizon_end = forecast.points[-1].at if forecast.points else forecast.created_at
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO kronos_forecast (forecast_id, created_at, symbol,"
                " timeframe, horizon_end, model_key, model_revision,"
                " settings_fingerprint, label, payload) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    forecast.forecast_id,
                    forecast.created_at,
                    forecast.symbol,
                    forecast.timeframe,
                    horizon_end,
                    forecast.model_key,
                    forecast.model_revision,
                    forecast.settings_fingerprint,
                    forecast.label,
                    json.dumps(forecast.to_payload(), sort_keys=True),
                ),
            )
            connection.commit()

    def save_outcome(self, outcome: Outcome) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO kronos_outcome (forecast_id, scored_at,"
                " realised_close, realised_return, forecast_return, direction_correct,"
                " absolute_error, band_covered, baseline_flat_error,"
                " baseline_drift_error, scored_against, note)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    outcome.forecast_id,
                    outcome.scored_at,
                    str(outcome.realised_close),
                    str(outcome.realised_return),
                    str(outcome.forecast_return),
                    int(outcome.direction_correct),
                    str(outcome.absolute_error),
                    int(outcome.band_covered),
                    str(outcome.baseline_flat_error),
                    str(outcome.baseline_drift_error),
                    outcome.scored_against,
                    outcome.note,
                ),
            )
            connection.commit()

    # -- reads ----------------------------------------------------------

    def recent(
        self, *, symbol: str = "", limit: int = 25, unscored_only: bool = False
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT f.payload, o.realised_close, o.realised_return,"
            " o.forecast_return, o.direction_correct, o.absolute_error,"
            " o.band_covered, o.baseline_flat_error, o.baseline_drift_error,"
            " o.scored_at, o.scored_against"
            " FROM kronos_forecast f LEFT JOIN kronos_outcome o"
            " ON o.forecast_id = f.forecast_id"
        )
        clauses: list[str] = []
        parameters: list[Any] = []
        if symbol:
            clauses.append("f.symbol = ?")
            parameters.append(symbol)
        if unscored_only:
            clauses.append("o.forecast_id IS NULL")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY f.created_at DESC LIMIT ?"
        parameters.append(max(1, int(limit)))
        rows: list[dict[str, Any]] = []
        with closing(self._connect()) as connection:
            for row in connection.execute(query, parameters).fetchall():
                payload = json.loads(row[0])
                payload["outcome"] = (
                    {}
                    if row[1] is None
                    else {
                        "realised_close": row[1],
                        "realised_return": row[2],
                        "forecast_return": row[3],
                        "direction_correct": bool(row[4]),
                        "absolute_error": row[5],
                        "band_covered": bool(row[6]),
                        "baseline_flat_error": row[7],
                        "baseline_drift_error": row[8],
                        "scored_at": row[9],
                        "scored_against": row[10],
                    }
                )
                payload["scored"] = bool(payload["outcome"])
                rows.append(payload)
        return rows

    def pending(self, *, symbol: str = "", timeframe: str = "") -> list[dict[str, Any]]:
        """Forecasts whose horizon has passed but which have no outcome yet."""

        pending = [row for row in self.recent(symbol=symbol, limit=500, unscored_only=True)]
        if timeframe:
            pending = [row for row in pending if row.get("timeframe") == timeframe]
        return pending

    def summary(self, *, symbol: str = "", timeframe: str = "") -> dict[str, Any]:
        """Counts and error statistics.  Descriptive, with the sample size attached."""

        rows = self.recent(symbol=symbol, limit=1000)
        if timeframe:
            rows = [row for row in rows if row.get("timeframe") == timeframe]
        scored = [row for row in rows if row.get("scored")]
        if not scored:
            return {
                "scored": 0,
                "total": len(rows),
                "note": "No forecast has an outcome yet, so there is nothing to score.",
            }
        directions = sum(1 for row in scored if row["outcome"]["direction_correct"])
        covered = sum(1 for row in scored if row["outcome"]["band_covered"])
        error = _mean(row["outcome"]["absolute_error"] for row in scored)
        flat = _mean(row["outcome"]["baseline_flat_error"] for row in scored)
        drift = _mean(row["outcome"]["baseline_drift_error"] for row in scored)
        return {
            "scored": len(scored),
            "total": len(rows),
            "direction_correct": directions,
            "direction_rate": round(directions / len(scored), 4),
            "band_covered": covered,
            "band_coverage": round(covered / len(scored), 4),
            "mean_absolute_error": round(error, 6),
            "baseline_flat_mean_error": round(flat, 6),
            "baseline_drift_mean_error": round(drift, 6),
            "note": (
                f"{len(scored)} scored forecast(s). A sample this size supports no "
                "claim about future accuracy, and none of these numbers includes "
                "trading costs."
            ),
        }


def _mean(values: Iterable[Any]) -> float:
    items = [float(value) for value in values]
    return sum(items) / len(items) if items else 0.0


# ---------------------------------------------------------------------------
# scoring a forecast once its horizon has passed
# ---------------------------------------------------------------------------


def score_forecast(
    payload: Mapping[str, Any],
    series: Sequence[tuple[datetime, Decimal]],
    *,
    clock: Callable[[], datetime] | None = None,
) -> Outcome | None:
    """Score one stored forecast against realised closes.

    ``series`` is ``(timestamp, close)`` in ascending order, from the same feed the
    forecast was built on.  The realised price is the last close at or before the
    forecast's horizon timestamp; if the series has not reached it, ``None`` is
    returned and the row stays unscored -- guessing the outcome would be worse than
    waiting.
    """

    points = payload.get("points") or []
    if not points:
        return None
    horizon_text = str(points[-1].get("at", ""))
    try:
        horizon_end = datetime.fromisoformat(horizon_text)
    except ValueError:
        return None
    if horizon_end.tzinfo is None:
        horizon_end = horizon_end.replace(tzinfo=timezone.utc)
    last_close = Decimal(str(payload.get("last_close", "0")))
    if last_close <= 0:
        return None
    expected = Decimal(str(payload.get("band", {}).get("p50", [""])[-1] or "0"))
    if expected <= 0:
        return None
    low = Decimal(str(payload.get("band", {}).get("p10", [""])[-1] or "0"))
    high = Decimal(str(payload.get("band", {}).get("p90", [""])[-1] or "0"))

    realised_at: datetime | None = None
    realised: Decimal | None = None
    for timestamp, close in series:
        moment = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
        if moment <= horizon_end:
            realised_at, realised = moment, Decimal(str(close))
        else:
            break
    if realised is None or realised_at is None:
        return None
    if realised_at < horizon_end:
        # The series stops before the horizon: the horizon has not elapsed.
        return None

    forecast_return = (expected - last_close) / last_close
    realised_return = (realised - last_close) / last_close
    drift_close = last_close + (expected - last_close)
    now = (clock or _utc_now)().isoformat()
    return Outcome(
        forecast_id=str(payload.get("forecast_id", "")),
        scored_at=now,
        realised_close=realised,
        realised_return=realised_return,
        forecast_return=forecast_return,
        direction_correct=(forecast_return >= 0) == (realised_return >= 0),
        absolute_error=abs(expected - realised),
        band_covered=bool(low <= realised <= high),
        baseline_flat_error=abs(last_close - realised),
        baseline_drift_error=abs(drift_close - realised),
        scored_against=realised_at.isoformat(),
    )


def reconcile(
    store: ForecastStore,
    *,
    series_provider: Callable[[str, str], Sequence[tuple[datetime, Decimal]]],
    symbol: str = "",
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Score every pending forecast whose horizon has data available."""

    resolved = 0
    waiting = 0
    for payload in store.pending(symbol=symbol):
        try:
            series = series_provider(payload.get("symbol", ""), payload.get("timeframe", ""))
        except Exception:
            waiting += 1
            continue
        outcome = score_forecast(payload, series, clock=clock)
        if outcome is None:
            waiting += 1
            continue
        store.save_outcome(outcome)
        resolved += 1
    return {"resolved": resolved, "waiting": waiting}


__all__ = [
    "ForecastStore",
    "HistoryError",
    "Outcome",
    "SCHEMA_VERSION",
    "SCORING_NOTE",
    "reconcile",
    "score_forecast",
]
