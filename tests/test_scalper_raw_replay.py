import csv
import hashlib
from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal

from axiom_forex.scalper.config import fixture_config
from axiom_forex.scalper.lab import Scenario, collect_outcomes, run_scenario, trending
from axiom_forex.scalper.raw_replay import REPLAY_VERSION, canonical, digest, source_digest, verify_raw_artifact
from axiom_forex.scalper.signals import default_strategies
from axiom_forex.scalper.ticks import ReplayTick

UTC = timezone.utc


def _make(tmp_path):
    config = fixture_config()
    strategy = default_strategies()[0]
    base = trending(symbol=config.instruments[0].symbol, count=1800).ticks
    groups = (base[:1200], base[1200:1500], base[1500:])
    rows = []
    for split, group in zip(("train", "validation", "test"), groups):
        for tick in group:
            rows.append((tick.at, tick.symbol, tick.bid, tick.ask, split))
    raw = tmp_path / "ticks.csv"
    with raw.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(("timestamp", "symbol", "broker_symbol", "bid", "ask", "split"))
        for at, symbol, bid, ask, split in rows:
            writer.writerow((at.isoformat(), symbol, symbol, str(bid), str(ask), split))
    raw_sha = hashlib.sha256(raw.read_bytes()).hexdigest()
    data = {"origin": "MT5_BROKER_HISTORICAL_EXPORT", "source": "fixture only",
            "symbol": config.instruments[0].symbol, "broker_symbol": config.instruments[0].symbol,
            "start": rows[0][0].isoformat(), "end": rows[-1][0].isoformat(),
            "file": raw.name, "sha256": raw_sha}
    scenarios = {}
    for split, group in zip(("train", "validation", "test"), groups):
        scenarios[split] = Scenario(name=f"evidence-{split}", description="signed raw broker ticks",
            ticks=tuple(ReplayTick(symbol=t.symbol, bid=t.bid, ask=t.ask, at=t.at) for t in group),
            data_class="BROKER_RAW_EVIDENCE")
    trained = collect_outcomes([scenarios["train"]], config=config, strategies=[strategy])["book"]
    results = {}
    for split in ("validation", "test"):
        report = run_scenario(scenarios[split], config=config, strategies=[strategy],
                              trained_book=trained, run_id=f"independent-evidence-{split}")
        audit = report.audit_view()
        audit.pop("ledger", None)
        audit["trades"] = [trade.audit_view() for trade in report.trades]
        results[split] = audit
    window = {"train_start": rows[0][0].isoformat(), "train_end": groups[0][-1].at.isoformat(),
              "test_start": groups[2][0].at.isoformat(), "test_end": groups[2][-1].at.isoformat()}
    wf_train = Scenario(name="walk-forward-0-train", description="signed raw broker ticks",
        ticks=tuple(ReplayTick(symbol=t.symbol, bid=t.bid, ask=t.ask, at=t.at) for t in groups[0]),
        data_class="BROKER_RAW_EVIDENCE")
    wf_test = Scenario(name="walk-forward-0-test", description="signed raw broker ticks",
        ticks=tuple(ReplayTick(symbol=t.symbol, bid=t.bid, ask=t.ask, at=t.at) for t in groups[2]),
        data_class="BROKER_RAW_EVIDENCE")
    wf_book = collect_outcomes([wf_train], config=config, strategies=[strategy])["book"]
    wf_report = run_scenario(wf_test, config=config, strategies=[strategy], trained_book=wf_book,
                             run_id="independent-evidence-wf-0")
    wf_audit = wf_report.audit_view()
    wf_audit.pop("ledger", None)
    wf_audit["trades"] = [trade.audit_view() for trade in wf_report.trades]
    results["walk_forward"] = [{"window": window, "result": wf_audit}]
    instrument = config.instruments[0].audit_view()
    costs = {"currency": "USD", **config.cost.audit_view(), "verified": True}
    from axiom_forex.scalper.raw_replay import source_digest
    document = {
        "data": data,
        "strategy": {"strategy_id": strategy.name, "rule_version": strategy.rule_version,
                     "fingerprint": strategy.fingerprint},
        "instrument": instrument, "costs": costs, "walk_forward": [window],
        "replay_config": {"configuration_digest": digest(asdict(config)),
                          "strategy_source_digest": source_digest()},
        "replay": {"replay_version": REPLAY_VERSION, "engine_digest": source_digest(),
                   "input_digest": "", "output_digest": digest(results), "claims": results},
    }
    raw_view = [{"timestamp": row[0].isoformat(), "symbol": row[1], "bid": str(row[2]),
                 "ask": str(row[3]), "split": row[4]} for row in rows]
    document["replay"]["input_digest"] = digest({"raw_sha256": raw_sha, "rows": raw_view,
        "strategy": strategy.spec(), "config": asdict(config), "instrument": instrument,
        "costs": costs, "walk_forward": [window]})
    artifact = tmp_path / "candidate.json"
    artifact.write_text("{}", encoding="utf-8")
    return artifact, document, config, strategy, instrument, costs, raw


def test_real_strategy_and_paper_fill_replay_reproduces_claims_without_ledger(tmp_path):
    artifact, doc, config, strategy, instrument, costs, raw = _make(tmp_path)
    ledger = tmp_path / "active-user-ledger.jsonl"
    ledger.write_bytes(b"preserve these active ledger bytes\n")
    before = ledger.read_bytes()
    a = verify_raw_artifact(artifact, doc, config=config, strategy=strategy,
                            instrument=instrument, expected_costs={k: v for k, v in costs.items() if k != "verified"})
    b = verify_raw_artifact(artifact, doc, config=config, strategy=strategy,
                            instrument=instrument, expected_costs={k: v for k, v in costs.items() if k != "verified"})
    assert a["verified"] and b["verified"], (a, b)
    assert a["output_digest"] == b["output_digest"]
    assert ledger.read_bytes() == before
    assert a["results"]["test"]["limitations"]
    assert a["results"]["test"]["funnel"]["signals"] > 0
    assert "trades" in a["results"]["test"]
    assert "walk_forward" in a["results"]


def test_raw_tamper_and_claim_changes_fail(tmp_path):
    artifact, doc, config, strategy, instrument, costs, raw = _make(tmp_path)
    raw.write_text(raw.read_text() + "\n", encoding="utf-8")
    result = verify_raw_artifact(artifact, doc, config=config, strategy=strategy,
        instrument=instrument, expected_costs={k: v for k, v in costs.items() if k != "verified"})
    assert not result["verified"] and "digest_mismatch" in result["reason"]
    artifact, doc, config, strategy, instrument, costs, raw = _make(tmp_path)
    doc["replay"]["claims"]["test"]["metrics"]["n"] += 1
    result = verify_raw_artifact(artifact, doc, config=config, strategy=strategy,
        instrument=instrument, expected_costs={k: v for k, v in costs.items() if k != "verified"})
    assert not result["verified"] and "replay_claims_mismatch" in result["reason"]


def test_actual_replay_refuses_strategy_config_symbol_and_cost_mismatch(tmp_path):
    artifact, doc, config, strategy, instrument, costs, raw = _make(tmp_path)
    mismatch = replace(config, max_quote_age_ms=config.max_quote_age_ms + 1)
    result = verify_raw_artifact(artifact, doc, config=mismatch, strategy=strategy,
        instrument=instrument, expected_costs={k: v for k, v in costs.items() if k != "verified"})
    assert not result["verified"] and "configuration_digest_mismatch" in result["reason"]
    other_strategy = default_strategies()[1]
    result = verify_raw_artifact(artifact, doc, config=config, strategy=other_strategy,
        instrument=instrument, expected_costs={k: v for k, v in costs.items() if k != "verified"})
    assert not result["verified"] and "strategy_identity_mismatch" in result["reason"]
    wrong = {**instrument, "symbol": "OTHER"}
    result = verify_raw_artifact(artifact, doc, config=config, strategy=strategy,
        instrument=wrong, expected_costs={k: v for k, v in costs.items() if k != "verified"})
    assert not result["verified"]
    wrong_costs = {k: v for k, v in costs.items() if k != "verified"}
    wrong_costs["slippage_points"] = "999"
    result = verify_raw_artifact(artifact, doc, config=config, strategy=strategy,
        instrument=instrument, expected_costs=wrong_costs)
    assert not result["verified"] and "configured_cost_mismatch" in result["reason"]


def test_raw_chronology_and_required_fields_are_validated(tmp_path):
    artifact, doc, config, strategy, instrument, costs, raw = _make(tmp_path)
    lines = raw.read_text().splitlines()
    lines[3], lines[4] = lines[4], lines[3]
    raw.write_text("\n".join(lines) + "\n")
    doc["data"]["sha256"] = hashlib.sha256(raw.read_bytes()).hexdigest()
    result = verify_raw_artifact(artifact, doc, config=config, strategy=strategy,
        instrument=instrument, expected_costs={k: v for k, v in costs.items() if k != "verified"})
    assert not result["verified"] and "chronology_invalid" in result["reason"]


def test_raw_required_quote_field_missing_is_named(tmp_path):
    artifact, doc, config, strategy, instrument, costs, raw = _make(tmp_path)
    lines = raw.read_text().splitlines()
    fields = lines[0].split(",")
    fields.remove("ask")
    values = lines[1].split(",")
    values.pop(4)
    raw.write_text(",".join(fields) + "\n" + ",".join(values) + "\n" + "\n".join(lines[2:]) + "\n")
    doc["data"]["sha256"] = hashlib.sha256(raw.read_bytes()).hexdigest()
    result = verify_raw_artifact(artifact, doc, config=config, strategy=strategy,
        instrument=instrument, expected_costs={k: v for k, v in costs.items() if k != "verified"})
    assert not result["verified"] and "raw_tick_fields_missing" in result["reason"]
