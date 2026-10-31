"""Every variant that was ever tried, and every choice the operator made.

Two tables in one local SQLite file:

``strategy_attempt``
    One row per (variant, symbol, timeframe, dataset).  Written whether the
    attempt passed, failed, or could not be run.  A variant that was tried and
    rejected stays in the record forever: deleting a failed attempt is exactly how
    a search turns into a story about one lucky combination.

``strategy_selection``
    The operator's explicit choice.  Only an attempt whose screen says
    ``ELIGIBLE`` may be selected, and selecting records *the attempt* (which
    dataset, which symbol, which timeframe) -- not "this strategy is good".

Nothing here can trade, arm, size, or change a mode.  A selection is a label on a
row; using it still requires the live plan, its review, the confirmation phrase,
the risk gate, and a broker-side stop.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

SCHEMA_VERSION = 1

#: Attempt outcomes.  ``ELIGIBLE`` is a statement about the screen, not a licence
#: to trade; ``SELECTED`` only exists for an operator choice.
ATTEMPTED = "ATTEMPTED"
ELIGIBLE = "ELIGIBLE"
REJECTED = "REJECTED"
INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
UNAVAILABLE = "UNAVAILABLE"
SELECTED = "SELECTED_BY_OPERATOR"

DDL = """
CREATE TABLE IF NOT EXISTS strategy_attempt (
    attempt_id      TEXT PRIMARY KEY,
    card_id         TEXT NOT NULL,
    variant_id      TEXT NOT NULL,
    grid_version    TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    dataset_id      TEXT NOT NULL,
    dataset_fingerprint TEXT NOT NULL,
    recorded_at     TEXT NOT NULL,
    status          TEXT NOT NULL,
    parameters      TEXT NOT NULL,
    metrics         TEXT NOT NULL,
    reason          TEXT NOT NULL,
    checks          TEXT NOT NULL,
    p_value         TEXT NOT NULL,
    q_value         TEXT NOT NULL,
    reality_check   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS strategy_attempt_variant
    ON strategy_attempt (variant_id, dataset_fingerprint);
CREATE TABLE IF NOT EXISTS strategy_selection (
    variant_id      TEXT PRIMARY KEY,
    attempt_id      TEXT NOT NULL,
    selected_at     TEXT NOT NULL,
    card_id         TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    dataset_id      TEXT NOT NULL,
    note            TEXT NOT NULL
);
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SelectionRefused(Exception):
    """The operator asked to select something the evidence does not support."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail

    def audit_view(self) -> dict[str, str]:
        return {"reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    card_id: str
    variant_id: str
    grid_version: str
    symbol: str
    timeframe: str
    dataset_id: str
    dataset_fingerprint: str
    recorded_at: str
    status: str
    parameters: Mapping[str, str]
    metrics: Mapping[str, Any]
    reason: str
    checks: Sequence[Mapping[str, Any]]
    p_value: str = ""
    q_value: str = ""
    reality_check: str = ""

    def audit_view(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "card_id": self.card_id,
            "variant_id": self.variant_id,
            "grid_version": self.grid_version,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "dataset_id": self.dataset_id,
            "dataset_fingerprint": self.dataset_fingerprint,
            "recorded_at": self.recorded_at,
            "status": self.status,
            "parameters": dict(self.parameters),
            "metrics": dict(self.metrics),
            "reason": self.reason,
            "checks": [dict(item) for item in self.checks],
            "p_value": self.p_value,
            "q_value": self.q_value,
            "reality_check": self.reality_check,
        }


class StrategyLedger:
    """A durable, honest record of what was tried and what was chosen."""

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
        return connection

    # -- writes ---------------------------------------------------------

    @staticmethod
    def attempt_id(variant_id: str, symbol: str, timeframe: str, fingerprint: str) -> str:
        return f"{variant_id}|{symbol}|{timeframe}|{fingerprint}"

    def record(
        self,
        *,
        variant_id: str,
        card_id: str,
        grid_version: str,
        symbol: str,
        timeframe: str,
        dataset_id: str,
        dataset_fingerprint: str,
        status: str,
        parameters: Mapping[str, Any],
        metrics: Mapping[str, Any],
        reason: str = "",
        checks: Iterable[Mapping[str, Any]] = (),
        p_value: str = "",
        q_value: str = "",
        reality_check: str = "",
    ) -> Attempt:
        attempt_id = self.attempt_id(variant_id, symbol, timeframe, dataset_fingerprint)
        recorded_at = self.clock().isoformat()
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO strategy_attempt (attempt_id, card_id,"
                " variant_id, grid_version, symbol, timeframe, dataset_id,"
                " dataset_fingerprint, recorded_at, status, parameters, metrics,"
                " reason, checks, p_value, q_value, reality_check)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt_id,
                    card_id,
                    variant_id,
                    grid_version,
                    symbol,
                    timeframe,
                    dataset_id,
                    dataset_fingerprint,
                    recorded_at,
                    status,
                    json.dumps({name: str(value) for name, value in parameters.items()}, sort_keys=True),
                    json.dumps(dict(metrics), sort_keys=True, default=str),
                    reason,
                    json.dumps([dict(item) for item in checks], sort_keys=True),
                    str(p_value),
                    str(q_value),
                    str(reality_check),
                ),
            )
            connection.commit()
        return self.get(attempt_id)  # type: ignore[return-value]

    def clear_for_dataset(self, dataset_fingerprint: str) -> int:
        """Forget attempts on one dataset -- used when its fingerprint changes."""

        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "DELETE FROM strategy_attempt WHERE dataset_fingerprint = ?",
                (dataset_fingerprint,),
            )
            connection.commit()
            return int(cursor.rowcount or 0)

    def select(self, attempt_id: str, *, note: str = "") -> dict[str, Any]:
        """Record the operator's choice.  Only an eligible attempt may be selected.

        There is no override.  "Let me select it anyway" is how an unvalidated
        strategy reaches an account, so the refusal is absolute rather than a flag.
        """

        attempt = self.get(attempt_id)
        if attempt is None:
            raise SelectionRefused("attempt_not_found", str(attempt_id))
        if attempt.status == REJECTED:
            raise SelectionRefused(
                "attempt_rejected",
                attempt.reason or "the screen rejected this variant on this dataset",
            )
        if attempt.status != ELIGIBLE:
            raise SelectionRefused(
                "attempt_not_eligible",
                attempt.reason or f"screen status is {attempt.status}",
            )
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO strategy_selection (variant_id, attempt_id,"
                " selected_at, card_id, symbol, timeframe, dataset_id, note)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    attempt.variant_id,
                    attempt.attempt_id,
                    self.clock().isoformat(),
                    attempt.card_id,
                    attempt.symbol,
                    attempt.timeframe,
                    attempt.dataset_id,
                    note,
                ),
            )
            connection.commit()
        return self.selection(attempt.variant_id) or {}

    def deselect(self, variant_id: str) -> bool:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "DELETE FROM strategy_selection WHERE variant_id = ?", (str(variant_id),)
            )
            connection.commit()
            return bool(cursor.rowcount)

    # -- reads ----------------------------------------------------------

    def _row(self, row: Sequence[Any]) -> Attempt:
        return Attempt(
            attempt_id=row[0],
            card_id=row[1],
            variant_id=row[2],
            grid_version=row[3],
            symbol=row[4],
            timeframe=row[5],
            dataset_id=row[6],
            dataset_fingerprint=row[7],
            recorded_at=row[8],
            status=row[9],
            parameters=json.loads(row[10]),
            metrics=json.loads(row[11]),
            reason=row[12],
            checks=json.loads(row[13]),
            p_value=row[14],
            q_value=row[15],
            reality_check=row[16],
        )

    def get(self, attempt_id: str) -> Attempt | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT attempt_id, card_id, variant_id, grid_version, symbol,"
                " timeframe, dataset_id, dataset_fingerprint, recorded_at, status,"
                " parameters, metrics, reason, checks, p_value, q_value,"
                " reality_check FROM strategy_attempt WHERE attempt_id = ?",
                (str(attempt_id),),
            ).fetchone()
        return self._row(row) if row else None

    def attempts(
        self, *, card_id: str = "", variant_id: str = "", status: str = "", limit: int = 200
    ) -> list[Attempt]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if card_id:
            clauses.append("card_id = ?")
            parameters.append(card_id)
        if variant_id:
            clauses.append("variant_id = ?")
            parameters.append(variant_id)
        if status:
            clauses.append("status = ?")
            parameters.append(status)
        query = (
            "SELECT attempt_id, card_id, variant_id, grid_version, symbol, timeframe,"
            " dataset_id, dataset_fingerprint, recorded_at, status, parameters,"
            " metrics, reason, checks, p_value, q_value, reality_check"
            " FROM strategy_attempt"
        )
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY recorded_at DESC LIMIT ?"
        parameters.append(max(1, int(limit)))
        with closing(self._connect()) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._row(row) for row in rows]

    def best_attempt_per_variant(self) -> dict[str, Attempt]:
        best: dict[str, Attempt] = {}
        for attempt in self.attempts(limit=2000):
            current = best.get(attempt.variant_id)
            if current is None or _rank(attempt) > _rank(current):
                best[attempt.variant_id] = attempt
        return best

    def selection(self, variant_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT variant_id, attempt_id, selected_at, card_id, symbol,"
                " timeframe, dataset_id, note FROM strategy_selection"
                " WHERE variant_id = ?",
                (str(variant_id),),
            ).fetchone()
        if row is None:
            return None
        return {
            "variant_id": row[0],
            "attempt_id": row[1],
            "selected_at": row[2],
            "card_id": row[3],
            "symbol": row[4],
            "timeframe": row[5],
            "dataset_id": row[6],
            "note": row[7],
        }

    def selections(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT variant_id, attempt_id, selected_at, card_id, symbol,"
                " timeframe, dataset_id, note FROM strategy_selection"
                " ORDER BY selected_at DESC"
            ).fetchall()
        return [
            {
                "variant_id": row[0],
                "attempt_id": row[1],
                "selected_at": row[2],
                "card_id": row[3],
                "symbol": row[4],
                "timeframe": row[5],
                "dataset_id": row[6],
                "note": row[7],
            }
            for row in rows
        ]

    def counts(self) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            total = connection.execute("SELECT COUNT(*) FROM strategy_attempt").fetchone()[0]
            rows = connection.execute(
                "SELECT card_id, status, COUNT(*) FROM strategy_attempt GROUP BY card_id, status"
            ).fetchall()
            selected = connection.execute("SELECT COUNT(*) FROM strategy_selection").fetchone()[0]
        per_card: dict[str, dict[str, int]] = {}
        for card_id, status, count in rows:
            per_card.setdefault(card_id, {})[status] = int(count)
        return {
            "attempts": int(total),
            "selected": int(selected),
            "per_card": per_card,
            "note": (
                "Every attempted variant is counted, including the ones that failed. "
                "A rejected attempt is never deleted."
            ),
        }


_RANK = {ELIGIBLE: 4, ATTEMPTED: 2, INSUFFICIENT: 1, REJECTED: 0, UNAVAILABLE: 0}


def _rank(attempt: Attempt) -> int:
    return _RANK.get(attempt.status, 0)


__all__ = [
    "ATTEMPTED",
    "Attempt",
    "ELIGIBLE",
    "INSUFFICIENT",
    "REJECTED",
    "SCHEMA_VERSION",
    "SELECTED",
    "SelectionRefused",
    "StrategyLedger",
    "UNAVAILABLE",
]
