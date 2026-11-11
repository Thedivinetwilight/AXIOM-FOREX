"""Independent signed-evidence raw-tick replay (artifact format v2).

The replay uses AX-LAB's real strategy, TickCore, PaperVenue, cost accounting and
position management in an isolated in-memory run. It never opens a workstation
ledger or accepts the active host's outcomes as evidence.
"""
from __future__ import annotations

import csv
import hashlib
from dataclasses import asdict
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

REPLAY_VERSION = "axiom_scalper_raw_replay_v1"
RAW_COLUMNS = {"timestamp", "symbol", "broker_symbol", "bid", "ask", "split"}


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False, default=str).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def source_digest() -> str:
    from . import config, desk, execution, lab, positions, regime, signals, ticks
    h = hashlib.sha256()
    for path in (Path(signals.__file__), Path(lab.__file__), Path(desk.__file__),
                 Path(execution.__file__), Path(positions.__file__), Path(regime.__file__),
                 Path(ticks.__file__), Path(config.__file__), Path(__file__)):
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def _load_rows(path: Path, *, data: Mapping[str, Any], symbol: str,
               start: datetime, end: datetime) -> list[dict[str, Any]]:
    if path.suffix.lower() != ".csv":
        raise ValueError("raw_format_unsupported: expected UTF-8 CSV v1")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not RAW_COLUMNS.issubset(set(reader.fieldnames)):
            raise ValueError("raw_tick_fields_missing")
        rows = []
        previous = None
        for number, row in enumerate(reader, 2):
            try:
                stamp = datetime.fromisoformat(row["timestamp"])
                bid, ask = Decimal(row["bid"]), Decimal(row["ask"])
            except (ValueError, TypeError, ArithmeticError):
                raise ValueError(f"raw_tick_field_invalid:line={number}") from None
            if stamp.tzinfo is None:
                raise ValueError(f"raw_tick_timestamp_timezone_missing:line={number}")
            if previous is not None and stamp <= previous:
                raise ValueError(f"raw_tick_chronology_invalid:line={number}")
            if stamp < start or stamp > end:
                raise ValueError(f"raw_tick_outside_declared_range:line={number}")
            if row["symbol"] != symbol or row["broker_symbol"] != str(data.get("broker_symbol") or ""):
                raise ValueError(f"raw_tick_symbol_identity_mismatch:line={number}")
            if not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask <= bid:
                raise ValueError(f"raw_tick_quote_invalid:line={number}")
            if row["split"] not in {"train", "validation", "test"}:
                raise ValueError(f"raw_tick_split_invalid:line={number}")
            previous = stamp
            rows.append({"timestamp": stamp, "symbol": symbol, "bid": bid, "ask": ask,
                         "split": row["split"]})
    if not rows:
        raise ValueError("raw_tick_dataset_empty")
    order = {"train": 0, "validation": 1, "test": 2}
    if [order[r["split"]] for r in rows] != sorted(order[r["split"]] for r in rows):
        raise ValueError("raw_tick_split_chronology_invalid")
    for split in ("train", "validation", "test"):
        if not any(row["split"] == split for row in rows):
            raise ValueError(f"raw_tick_partition_missing:{split}")
    return rows


def verify_raw_artifact(artifact_path: Path, document: Mapping[str, Any], *,
                        config: Any, strategy: Any, instrument: Mapping[str, Any],
                        expected_costs: Mapping[str, Any]) -> Mapping[str, Any]:
    """Run the actual deterministic replay and compare signed input/output claims."""
    from .lab import Scenario, collect_outcomes, run_scenario
    from .ticks import ReplayTick

    try:
        replay = document.get("replay")
        data = document.get("data")
        if not isinstance(replay, dict) or replay.get("replay_version") != REPLAY_VERSION:
            raise ValueError("replay_version_unsupported")
        if not isinstance(data, dict):
            raise ValueError("evidence_data_provenance_incomplete")
        root = artifact_path.resolve(strict=True).parent
        raw_target = root / str(data.get("file") or "")
        if raw_target.is_symlink():
            raise ValueError("evidence_data_path_unsafe")
        raw_path = raw_target.resolve(strict=True)
        if not raw_path.is_relative_to(root):
            raise ValueError("evidence_data_path_unsafe")
        raw_sha = file_digest(raw_path)
        if raw_sha != str(data.get("sha256") or "").lower():
            raise ValueError("evidence_data_digest_mismatch")
        if data.get("origin") not in {"MT5_BROKER_HISTORICAL_EXPORT", "MT5_BROKER_TICK_EXPORT"}:
            raise ValueError("evidence_data_origin_not_independent")
        if not data.get("source") or not data.get("broker_symbol"):
            raise ValueError("evidence_data_provenance_incomplete")
        expected_symbol = str(instrument.get("symbol") or "")
        if data.get("symbol") != expected_symbol:
            raise ValueError("evidence_instrument_identity_mismatch")
        start, end = datetime.fromisoformat(str(data.get("start"))), datetime.fromisoformat(str(data.get("end")))
        rows = _load_rows(raw_path, data=data, symbol=expected_symbol, start=start, end=end)
        strategy_spec = strategy.spec()
        if document.get("strategy") != {"strategy_id": strategy.name,
                                        "rule_version": strategy.rule_version,
                                        "fingerprint": strategy.fingerprint}:
            raise ValueError("evidence_strategy_identity_mismatch")
        replay_cfg = document.get("replay_config")
        full_config = asdict(config)
        if not isinstance(replay_cfg, dict) or replay_cfg.get("configuration_digest") != digest(full_config):
            raise ValueError("evidence_configuration_digest_mismatch")
        if replay_cfg.get("strategy_source_digest") != source_digest():
            raise ValueError("evidence_strategy_source_digest_mismatch")
        if document.get("instrument") != dict(instrument):
            raise ValueError("evidence_instrument_identity_mismatch")
        costs = document.get("costs")
        if costs != {**dict(expected_costs), "verified": True}:
            raise ValueError("evidence_configured_cost_mismatch")
        raw_view = [{"timestamp": r["timestamp"].isoformat(), "symbol": r["symbol"],
                     "bid": str(r["bid"]), "ask": str(r["ask"]), "split": r["split"]} for r in rows]
        input_digest = digest({"raw_sha256": raw_sha, "rows": raw_view,
                               "strategy": strategy_spec, "config": full_config,
                               "instrument": instrument, "costs": costs,
                               "walk_forward": document.get("walk_forward")})
        split_scenarios = {}
        for split in ("train", "validation", "test"):
            selected = [r for r in rows if r["split"] == split]
            ticks = tuple(ReplayTick(symbol=r["symbol"], bid=r["bid"], ask=r["ask"],
                                     at=r["timestamp"]) for r in selected)
            split_scenarios[split] = Scenario(name=f"evidence-{split}", description="signed raw broker ticks",
                                               ticks=ticks, data_class="BROKER_RAW_EVIDENCE")
        trained = collect_outcomes([split_scenarios["train"]], config=config, strategies=[strategy])["book"]
        # Both holdouts are driven through AX-LAB's actual ScalperDesk + PaperVenue.
        validation = run_scenario(split_scenarios["validation"], config=config,
                                  strategies=[strategy], trained_book=trained,
                                  run_id="independent-evidence-validation")
        test = run_scenario(split_scenarios["test"], config=config,
                            strategies=[strategy], trained_book=trained,
                            run_id="independent-evidence-test")
        def stable_result(result: Any) -> dict[str, Any]:
            # The Scribe chain embeds process/run-specific IDs. Replay claims bind
            # reproducible outcomes instead: every closed trade plus the full
            # metrics, funnel and protection/execution audit, never host ledger data.
            audit = result.audit_view()
            audit.pop("ledger", None)
            audit["trades"] = [trade.audit_view() for trade in result.trades]
            return audit
        results = {"validation": stable_result(validation), "test": stable_result(test)}
        windows = document.get("walk_forward")
        if not isinstance(windows, list) or not windows:
            raise ValueError("evidence_walk_forward_windows_missing")
        walk_results = []
        last_test_end = None
        for index, window in enumerate(windows):
            if not isinstance(window, dict) or not all(window.get(k) for k in
                    ("train_start", "train_end", "test_start", "test_end")):
                raise ValueError(f"evidence_walk_forward_window_incomplete:{index}")
            a, b, c, d = (datetime.fromisoformat(str(window[k])) for k in
                          ("train_start", "train_end", "test_start", "test_end"))
            if any(x.tzinfo is None for x in (a, b, c, d)) or not (start <= a <= b < c <= d <= end):
                raise ValueError(f"evidence_walk_forward_chronology_invalid:{index}")
            if last_test_end is not None and c <= last_test_end:
                raise ValueError(f"evidence_walk_forward_windows_overlap:{index}")
            last_test_end = d
            train_rows = [r for r in rows if a <= r["timestamp"] <= b]
            test_rows = [r for r in rows if c <= r["timestamp"] <= d]
            if not train_rows or not test_rows:
                raise ValueError(f"evidence_walk_forward_window_empty:{index}")
            def scenario_for(name: str, selected: list[dict[str, Any]]) -> Any:
                return Scenario(name=name, description="signed raw broker ticks",
                    ticks=tuple(ReplayTick(symbol=r["symbol"], bid=r["bid"], ask=r["ask"],
                                           at=r["timestamp"]) for r in selected),
                    data_class="BROKER_RAW_EVIDENCE")
            train_scenario = scenario_for(f"walk-forward-{index}-train", train_rows)
            test_scenario = scenario_for(f"walk-forward-{index}-test", test_rows)
            fold_book = collect_outcomes([train_scenario], config=config, strategies=[strategy])["book"]
            fold_report = run_scenario(test_scenario, config=config, strategies=[strategy],
                trained_book=fold_book, run_id=f"independent-evidence-wf-{index}")
            walk_results.append({"window": window, "result": stable_result(fold_report)})
        results["walk_forward"] = walk_results
        output_digest = digest(results)
        claims = replay.get("claims")
        if not isinstance(claims, dict):
            raise ValueError("evidence_replay_claims_missing")
        if replay.get("engine_digest") != source_digest():
            raise ValueError("evidence_replay_engine_digest_mismatch")
        if replay.get("input_digest") != input_digest:
            raise ValueError("evidence_replay_input_digest_mismatch")
        if claims != results:
            def first_diff(left: Any, right: Any, path: str = "claims") -> str:
                if type(left) is not type(right): return f"{path}:type"
                if isinstance(left, dict):
                    if set(left) != set(right): return f"{path}:keys"
                    for key in left:
                        found = first_diff(left[key], right[key], f"{path}.{key}")
                        if found: return found
                elif isinstance(left, list):
                    if len(left) != len(right): return f"{path}:length"
                    for i, (a, b) in enumerate(zip(left, right)):
                        found = first_diff(a, b, f"{path}[{i}]")
                        if found: return found
                elif left != right: return path
                return ""
            raise ValueError(f"evidence_replay_claims_mismatch:{first_diff(claims, results)}")
        if replay.get("output_digest") != output_digest:
            raise ValueError("evidence_replay_output_digest_mismatch")
        return {"verified": True, "replay_version": REPLAY_VERSION,
                "engine_digest": source_digest(), "input_digest": input_digest,
                "output_digest": output_digest, "results": results}
    except Exception as exc:
        return {"verified": False, "reason": str(exc) or type(exc).__name__}
