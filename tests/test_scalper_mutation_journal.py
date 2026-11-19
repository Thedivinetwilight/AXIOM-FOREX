from pathlib import Path
import sqlite3

import pytest
from axiom_forex.scalper.mutation_journal import JournalError, MutationJournal

IDENTITY = dict(run_id="run", account_fingerprint="fp", account_server="broker",
                account_currency="USD", account_mode="DEMO", strategy_fingerprint="s",
                evidence_digest="e", config_digest="c", symbol="EURUSD")


def test_prepare_durable_and_duplicate_prevented_after_restart(tmp_path: Path):
    path = tmp_path / "journal.db"
    journal = MutationJournal(path, create=True)
    journal.prepare(request_id="request", action="entry", identity=IDENTITY,
                    intent={"request_id": "request", "lots": "0.01"})
    journal.close()
    reopened = MutationJournal(path)
    assert reopened.unresolved()[0]["intent"]["lots"] == "0.01"
    with pytest.raises(JournalError, match="duplicate"):
        reopened.prepare(request_id="request", action="entry", identity=IDENTITY,
                         intent={"request_id": "request"})
    reopened.record_result("request", result={"unknown": True}, readback=None)
    reopened.close()
    reopened = MutationJournal(path)
    assert reopened.unresolved()[0]["result"]["unknown"] is True
    reopened.resolve("request", {"reason": "exact terminal refusal"})
    assert reopened.unresolved() == []
    reopened.close()


@pytest.mark.parametrize("action", ["entry", "close", "protection"])
def test_requires_bound_identity_and_exact_id(tmp_path: Path, action):
    journal = MutationJournal(tmp_path / "journal.db", create=True)
    identity = {**IDENTITY, "account_mode": ""}
    with pytest.raises(JournalError, match="identity_incomplete"):
        journal.prepare(request_id="x", action=action, identity=identity,
                        intent={"request_id": "x", "position_id": "9"})
    with pytest.raises(JournalError, match="id_mismatch"):
        journal.prepare(request_id="x", action=action, identity=IDENTITY,
                        intent={"request_id": "y", "position_id": "9"})
    assert journal.list_all() == []
    journal.close()


def test_missing_or_corrupt_journal_fails_closed(tmp_path: Path):
    path = tmp_path / "journal.db"
    with pytest.raises(JournalError, match="missing"):
        MutationJournal(path)
    path.write_bytes(b"bad database")
    with pytest.raises(JournalError, match="unreadable"):
        MutationJournal(path)


def test_tampered_identity_is_not_accepted(tmp_path: Path):
    path = tmp_path / "journal.db"
    journal = MutationJournal(path, create=True)
    journal.prepare(request_id="x", action="entry", identity=IDENTITY, intent={"request_id": "x"})
    journal.close()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE mutations SET identity='{}'")
    with pytest.raises(JournalError, match="identity_incomplete"):
        MutationJournal(path)


@pytest.mark.parametrize("action", ["entry", "close", "protection"])
@pytest.mark.parametrize("response", ["confirmed", "refused", "unknown"])
def test_restart_preserves_each_response_without_automatically_resolving(tmp_path: Path, action, response):
    path = tmp_path / "journal.db"
    journal = MutationJournal(path, create=True)
    intent = {"request_id": "one", "position_id": "123", "prior": "old", "requested": "new"}
    journal.prepare(request_id="one", action=action, identity=IDENTITY, intent=intent)
    journal.record_result("one", result={"status": response}, readback={"ticket": "123"})
    journal.close()
    reopened = MutationJournal(path)
    row = reopened.unresolved()[0]
    assert row["action"] == action and row["intent"] == intent
    assert row["result"] == {"status": response} and row["readback"] == {"ticket": "123"}
    with pytest.raises(JournalError, match="unresolved_mutation_present"):
        reopened.prepare(request_id="two", action=action, identity=IDENTITY,
                         intent={"request_id": "two", "position_id": "123"})
    reopened.close()


def test_cross_instance_unresolved_latch_and_duplicate_identity(tmp_path: Path):
    path = tmp_path / "journal.db"
    first = MutationJournal(path, create=True)
    second = MutationJournal(path)
    first.prepare(request_id="one", action="entry", identity=IDENTITY, intent={"request_id": "one"})
    with pytest.raises(JournalError, match="unresolved_mutation_present"):
        second.prepare(request_id="two", action="entry", identity=IDENTITY, intent={"request_id": "two"})
    with pytest.raises(JournalError, match="duplicate"):
        second.prepare(request_id="one", action="entry", identity={**IDENTITY, "run_id": "other"},
                       intent={"request_id": "one"})
    first.resolve("one", {"reason": "exact terminal refusal"})
    second.prepare(request_id="two", action="entry", identity=IDENTITY, intent={"request_id": "two"})
    first.close()
    second.close()
