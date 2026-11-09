from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from axiom_forex.paper_runner.accounting import write_capital
from axiom_forex.scalper.live_config import LiveScalperConfigError, load_live_paper_config
from axiom_forex.scalper.live_host import ScalperPaperHost
from axiom_forex.scalper.scribe import POSITION_OPENED, STATE, Scribe, verify_jsonl

UTC = timezone.utc
NOW = datetime.now(UTC)
SYMBOL = "EURUSD.pro"

RISK = {
    "risk_fraction": "0.0025", "max_loss_per_trade_account": "25",
    "max_aggregate_risk_fraction": "0.01", "max_leverage": "5",
    "max_currency_exposure_fraction": "0.6", "max_correlated_positions": 2,
    "max_daily_loss_fraction": "0.02", "max_drawdown_fraction": "0.05",
    "max_loss_streak": 4, "max_spread_points_hard": "45",
    "max_slippage_points": "6", "max_fill_latency_ms": 750,
    "max_open_positions": 2, "max_quote_age_ms": 1500,
    "max_spread_points": "30", "max_hold_ms": 900000,
    "break_even_trigger_points": "8", "trail_after_points": "10",
    "trail_distance_points": "6",
}


def settings(symbol=SYMBOL):
    return {
        "schema": "axiom-scalper-paper-v1", "symbol": symbol, "margin_rate": "0.02",
        "costs": {"commission_per_lot": "7", "commission_state": "OBSERVED",
                  "slippage_points": "1", "slippage_state": "ESTIMATE",
                  "swap_state": "OBSERVED"},
        "risk": dict(RISK),
    }


class Terminal:
    def __init__(self):
        self.reads = 0
        self.mutations = []

    def symbol_info(self, symbol):
        assert symbol == SYMBOL
        return SimpleNamespace(name=symbol, tradable=True, point=Decimal("0.00001"),
            digits=5, trade_contract_size=Decimal("100000"), volume_min=Decimal("0.01"),
            volume_step=Decimal("0.01"), volume_max=Decimal("10"), stops_level=10,
            currency_base="EUR", currency_profit="USD")

    def tick(self, symbol):
        self.reads += 1
        return SimpleNamespace(symbol=symbol, bid=Decimal("1.10000"), ask=Decimal("1.10010"),
                               time=datetime.now(UTC), volume=0)

    def order_send(self, *_a, **_k): self.mutations.append("send"); raise AssertionError("paper sent")
    def order_check(self, *_a, **_k): self.mutations.append("check"); raise AssertionError("paper checked")
    def close_position(self, *_a, **_k): self.mutations.append("close"); raise AssertionError("paper closed")


class Session:
    connected = True
    def __init__(self): self.terminal = Terminal()
    def market_data_source(self): return self.terminal if self.connected else None
    def account(self): return {"currency": "USD", "mode": "DEMO"}
    def symbols(self):
        return ({"name": SYMBOL, "currency_base": "EUR", "currency_profit": "USD",
                 "currency_margin": "EUR", "digits": 5, "point": Decimal("0.00001"),
                 "trade_contract_size": Decimal("100000"), "volume_min": Decimal("0.01"),
                 "volume_step": Decimal("0.01"), "volume_max": Decimal("10"),
                 "trade_mode": 4, "stops_level": 10, "spread": 10,
                 "swap_long": Decimal("-1"), "swap_short": Decimal("-1"),
                 "swap_mode": 0, "swap_rollover3days": 3},)


def setup(tmp_path, session=None):
    session = session or Session()
    capital_root = tmp_path / "catalog-paper"
    write_capital(capital_root, "10000", "USD")
    config_path = tmp_path / "scalper_paper.local.json"
    config_path.write_text(json.dumps(settings()), encoding="utf-8")
    ledger_dir = tmp_path / "scalper_paper_ledgers"
    return session, config_path, capital_root, ledger_dir


def test_operator_symbol_selection_preserves_all_other_local_terms(tmp_path):
    session, config_path, capital_root, ledger_dir = setup(tmp_path)
    original = json.loads(config_path.read_text(encoding="utf-8"))
    host = ScalperPaperHost(session=session, config_path=config_path,
                            paper_root=capital_root, ledger_dir=ledger_dir)
    result = host.select_symbol(symbol=SYMBOL)
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert result["ok"] and saved["symbol"] == SYMBOL
    assert {key: value for key, value in saved.items() if key != "symbol"} == {
        key: value for key, value in original.items() if key != "symbol"
    }
    before = config_path.read_bytes()
    rejected = host.select_symbol(symbol="EURUSD")
    assert rejected["reason"] == "scalper_symbol_not_verified"
    assert config_path.read_bytes() == before


def test_host_strategy_review_fails_closed_until_independent_oos_evidence(tmp_path):
    session, config_path, capital_root, ledger_dir = setup(tmp_path)
    host = ScalperPaperHost(session=session, config_path=config_path,
                            paper_root=capital_root, ledger_dir=ledger_dir)
    review = host.status()["strategy_review"]
    assert review["eligible_count"] == 0
    assert "NO TRADE" in review["decision"]
    assert review["policy"]["min_trades"] == 200
    assert review["policy"]["min_out_of_sample_trades"] == 60
    assert all(not item["eligible"] for item in review["candidates"])


def test_live_config_uses_verified_broker_spec_and_local_paper_capital(tmp_path):
    session, config_path, capital_root, _ = setup(tmp_path)
    config, capital, currency = load_live_paper_config(session=session,
        settings_path=config_path, paper_root=capital_root)
    assert config.mode == "PAPER"
    assert config.instruments[0].source == "CONNECTED_MT5_SYMBOL_INFO"
    assert config.instruments[0].contract_size == Decimal("100000")
    assert capital == Decimal("10000.00") and currency == "USD"


@pytest.mark.parametrize(("mutate", "reason"), [
    (lambda x: x.pop("risk"), "scalper_risk_limits_missing"),
    (lambda x: x["costs"].pop("slippage_points"), "cost_term_missing"),
    (lambda x: x.update(symbol="EURUSD"), "scalper_symbol_not_verified"),
    (lambda x: x.update(password="never"), "unknown_scalper_setting"),
])
def test_invalid_or_unsafe_config_is_named_and_refused(tmp_path, mutate, reason):
    session, config_path, capital_root, _ = setup(tmp_path)
    payload = settings(); mutate(payload)
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(LiveScalperConfigError) as exc:
        load_live_paper_config(session=session, settings_path=config_path, paper_root=capital_root)
    assert exc.value.reason == reason


def test_missing_capital_costs_risk_and_disconnected_session_block(tmp_path):
    session, config_path, capital_root, _ = setup(tmp_path)
    (capital_root / "paper-capital.local.json").unlink()
    with pytest.raises(LiveScalperConfigError, match="paper_capital_missing"):
        load_live_paper_config(session=session, settings_path=config_path, paper_root=capital_root)
    session.connected = False
    with pytest.raises(LiveScalperConfigError, match="terminal_not_connected"):
        load_live_paper_config(session=session, settings_path=config_path, paper_root=capital_root)


def test_host_start_stop_persists_fills_as_separate_verified_paper_ledger(tmp_path):
    session, config_path, capital_root, ledger_dir = setup(tmp_path)
    host = ScalperPaperHost(session=session, config_path=config_path,
                            paper_root=capital_root, ledger_dir=ledger_dir)
    assert host.status()["account_type"] == "DEMO"
    started = host.start()
    assert started["ok"] and started["mode"] == "PAPER"
    assert "observation-only" in started["message"]
    assert host.desk.strategies == ()
    deadline = time.monotonic() + 2
    while session.terminal.reads == 0 and time.monotonic() < deadline:
        time.sleep(.005)
    assert session.terminal.reads > 0
    live_status = host.status()
    assert live_status["strategy_review"]["eligible_count"] == 0
    assert live_status["quote"]["accepted_ticks_by_symbol"][SYMBOL] > 0
    assert live_status["quote"]["realized_pnl"] == "0.00"
    assert live_status["quote"]["unrealized_pnl"] == "0"
    stopped = host.stop()
    assert host.desk.status()["stats"]["orders"] == 0
    assert stopped["ok"] and stopped["state"] == "STOPPED"
    ledger = Path(stopped["ledger"])
    assert verify_jsonl(ledger)["ok"]
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert any(row["kind"] == STATE and row["payload"].get("state") == "paper_stopped_positions_left_open" for row in rows)
    assert session.terminal.mutations == []


def test_restart_with_open_position_is_blocked_without_flatten_or_reassignment(tmp_path):
    session, config_path, capital_root, ledger_dir = setup(tmp_path)
    ledger_dir.mkdir(parents=True)
    scribe = Scribe(run_id="prior", mode="PAPER")
    scribe.record(POSITION_OPENED, at_ms=100, actor="test", position_id="paper-1",
                  symbol=SYMBOL, strategy="trend", fingerprint="abc", side="BUY",
                  lots="0.01", entry_price="1.1", stop_price="1.09")
    prior_path = ledger_dir / "prior.jsonl"
    scribe.to_jsonl(prior_path)
    ScalperPaperHost._write_checkpoint(prior_path, SimpleNamespace(scribe=scribe))
    host = ScalperPaperHost(session=session, config_path=config_path,
                            paper_root=capital_root, ledger_dir=ledger_dir)
    status = host.status()
    assert status["state"] == "BLOCKED" and status["reason"] == "recovery_required"
    blocker = status["recovery"][0]
    assert blocker["reason"] == "open_or_unresolved_paper_state_requires_exact_recovery"
    assert blocker["positions"][0]["position_id"] == "paper-1"
    assert any(item["reason"] == "position_state_incomplete_for_exact_recovery"
               for item in blocker["uncertain"])
    result = host.start()
    assert not result["ok"] and result["reason"] == "recovery_required"
    bad = host.acknowledge_recovery(confirmation="wrong")
    assert not bad["ok"] and bad["reason"] == "recovery_confirmation_mismatch"
    acknowledged = host.acknowledge_recovery(confirmation="KEEP_PAPER_BOOK_BLOCKED")
    assert acknowledged["ok"] and acknowledged["reason"] == "recovery_remains_blocked"
    assert host.start()["reason"] == "recovery_required"
    assert (ledger_dir / "recovery-decisions.jsonl").exists()
    assert session.terminal.reads == 0 and session.terminal.mutations == []


def test_truncated_checkpointed_ledger_blocks_start_without_repair(tmp_path):
    session, config_path, capital_root, ledger_dir = setup(tmp_path)
    host = ScalperPaperHost(session=session, config_path=config_path,
                            paper_root=capital_root, ledger_dir=ledger_dir)
    started = host.start()
    assert started["ok"]
    stopped = host.stop()
    ledger = Path(stopped["ledger"])
    assert ScalperPaperHost._checkpoint_path(ledger).exists()
    lines = ledger.read_text(encoding="utf-8").splitlines()
    assert len(lines) > 1
    ledger.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    truncated = ledger.read_bytes()
    restarted = ScalperPaperHost(session=session, config_path=config_path,
                                 paper_root=capital_root, ledger_dir=ledger_dir)
    assert restarted.start()["reason"] == "recovery_required"
    assert ledger.read_bytes() == truncated


def test_corrupt_ledger_blocks_start_and_is_not_repaired(tmp_path):
    session, config_path, capital_root, ledger_dir = setup(tmp_path)
    ledger_dir.mkdir(parents=True)
    scribe = Scribe(run_id="corrupt", mode="PAPER")
    scribe.record(STATE, at_ms=1, state="test")
    path = ledger_dir / "corrupt.jsonl"
    scribe.to_jsonl(path)
    path.write_text(path.read_text().replace('"state": "test"', '"state": "changed"'), encoding="utf-8")
    before = path.read_bytes()
    host = ScalperPaperHost(session=session, config_path=config_path,
                            paper_root=capital_root, ledger_dir=ledger_dir)
    assert host.start()["reason"] == "recovery_required"
    assert path.read_bytes() == before
