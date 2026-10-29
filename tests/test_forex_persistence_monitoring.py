import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from axiom_forex.config.runtime import ForexRuntimeSettings
from axiom_forex.database.repository import ForexRepository
from axiom_forex.database.schema import SCHEMA_VERSION, apply_schema, schema_version
from axiom_forex.monitoring.anomalies import detect_anomalies
from axiom_forex.monitoring.health import build_health


def _repository_health():
    return {
        "schema_version": SCHEMA_VERSION,
        "expected_schema_version": SCHEMA_VERSION,
    }


def test_repository_applies_idempotent_additive_migrations(tmp_path):
    path = tmp_path / "forex.db"
    repository = ForexRepository(str(path))
    try:
        assert repository.schema_version == SCHEMA_VERSION
        assert repository.health()["expected_schema_version"] == SCHEMA_VERSION
    finally:
        repository.close()
    reopened = ForexRepository(str(path))
    try:
        assert reopened.schema_version == SCHEMA_VERSION
        assert reopened.list_backtests() == []
    finally:
        reopened.close()


def test_existing_legacy_table_survives_migration(tmp_path):
    path = tmp_path / "migration.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE legacy_table (value TEXT NOT NULL)")
        connection.execute("INSERT INTO legacy_table(value) VALUES ('keep-me')")
        connection.execute("CREATE TABLE schema_meta (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_meta(version) VALUES (0)")
        connection.commit()
        assert schema_version(connection) == 0
        assert apply_schema(connection) == SCHEMA_VERSION
        assert schema_version(connection) == SCHEMA_VERSION
        assert connection.execute("SELECT value FROM legacy_table").fetchone()[0] == "keep-me"
    finally:
        connection.close()


def test_repository_sanitizes_secrets_and_persists_architecture_records(tmp_path):
    repository = ForexRepository(str(tmp_path / "records.db"))
    try:
        repository.record_audit(
            {
                "event_type": "TEST_AUDIT",
                "api_token": "must-not-appear",
                "url": "https://example.invalid/path",
                "decimal": Decimal("1.2300"),
                "when": datetime(2026, 3, 10, tzinfo=timezone.utc),
            }
        )
        audit = repository.recent_audit()[0]
        assert audit["api_token"] == "[REDACTED]"
        assert audit["url"] == "[URL_REDACTED]"
        assert "must-not-appear" not in str(audit)

        repository.save_specs(
            version="catalog-v1",
            specs=[{"pair": "AAA/BBB", "signature": "abc", "spec_version": "spec-v1"}],
        )
        repository.save_dataset(
            "dataset-v1", "digest-v1", {"dataset_id": "dataset-v1", "source": "fixture"}
        )
        repository.save_backtest(
            {
                "run_id": "run-v1",
                "config": {"dataset_id": "dataset-v1", "strategy_id": "strategy-v1"},
                "net_pnl_account": Decimal("1.25"),
            }
        )
        repository.save_orders(
            [
                {
                    "request_id": "request-v1",
                    "simulation_id": "simulation-v1",
                    "state": "FILLED",
                    "updated_at": "2026-03-10T00:00:00+00:00",
                }
            ]
        )
        repository.save_fills(
            [
                {
                    "simulation_id": "simulation-v1",
                    "request_id": "request-v1",
                    "filled_at": "2026-03-10T00:00:00+00:00",
                }
            ]
        )
        repository.save_positions([{"position_id": "position-v1"}], state="OPEN")
        repository.save_snapshot("snapshot-v1", "equity", {"value": Decimal("10000")})
        repository.save_lifecycle("strategy-v1", {"stage": "RESEARCH"})
        repository.record_monitoring(
            "monitor-v1", "TEST", "WARNING", {"detail": "fixture"}
        )

        assert repository.get_dataset("dataset-v1")["sha256"] == "digest-v1"
        assert repository.list_specs(version="catalog-v1")[0]["pair"] == "AAA/BBB"
        assert repository.list_backtests()[0]["run_id"] == "run-v1"
        assert repository.list_orders()[0]["state"] == "FILLED"
        assert repository.list_snapshots()[0]["value"] == "10000"
        assert repository.list_lifecycle()[0]["stage"] == "RESEARCH"
        assert repository.list_monitoring()[0]["detail"] == "fixture"
        assert repository.health()["counts"]["backtests"] == 1
    finally:
        repository.close()


def test_detect_anomalies_flags_safety_conditions():
    events = [
        {"event_type": "PRE_TRADE_DECISION", "allowed": False},
        {"event_type": "PRE_TRADE_DECISION", "allowed": False},
        {"event_type": "PRE_TRADE_DECISION", "allowed": False},
        {"event_type": "LOCAL_PAPER_LIVE_REQUEST_BLOCKED"},
    ]
    orders = [{"request_id": "q1", "is_unresolved": True}]
    anomalies = detect_anomalies(
        audit_events=events,
        order_records=orders,
        calendar_open=False,
        quality_usable=False,
        quote_fresh=False,
        rejection_threshold=3,
    )
    codes = {anomaly.code for anomaly in anomalies}
    assert {
        "MARKET_CLOSED",
        "DATA_QUALITY_UNUSABLE",
        "QUOTE_NOT_FRESH",
        "UNRESOLVED_ORDER",
        "REPEATED_GATE_REJECTIONS",
        "LIVE_REQUEST_BLOCKED",
    } <= codes
    assert any(anomaly.severity == "ERROR" for anomaly in anomalies)


def test_build_health_fails_closed_when_live_bridge_enabled():
    settings = replace(ForexRuntimeSettings(), live_bridge_enabled=True)
    health = build_health(
        settings=settings,
        quality_usable=True,
        calendar_open=True,
        quote_fresh=True,
        gate_allowed=True,
        unresolved_orders=0,
        anomalies=(),
        repository_health=_repository_health(),
    )
    assert health.status == "UNSAFE"
    assert health.live_bridge_enabled is True
    assert not health.healthy


def test_build_health_is_degraded_when_market_closed():
    health = build_health(
        settings=ForexRuntimeSettings(),
        quality_usable=True,
        calendar_open=False,
        quote_fresh=True,
        gate_allowed=True,
        unresolved_orders=0,
        anomalies=(),
        repository_health=_repository_health(),
    )
    assert health.status == "DEGRADED"
    assert any(check["name"] == "market_open" for check in health.checks)
