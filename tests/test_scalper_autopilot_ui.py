from pathlib import Path
from types import SimpleNamespace

from axiom_forex.workstation.app import POST_ACTIONS, WorkstationApp
from axiom_forex.workstation.views import _render_scalper_paper_card


class FakeScalper:
    def __init__(self):
        self.calls = []
    def status(self):
        return {"available": True, "state": "STOPPED", "mode": "PAPER",
                "source": "MT5_LIVE_PAPER", "connected": False,
                "account_type": "UNKNOWN", "config": {"reason": "terminal_not_connected",
                "remediation": "Press Connect on Setup."}, "symbols": [],
                "symbol_exclusions": [], "recovery": [], "quote": {}, "paper_only": True,
                "strategy_review": {"policy": {"version": "skeptic-v1", "min_trades": 200,
                "min_out_of_sample_trades": 60, "min_walk_forward_folds": 3,
                "cost_multiple_survival": 1.5, "max_pbo": 0.25,
                "min_deflated_sharpe": 0.95, "min_profit_factor": 1.10},
                "decision": "NO TRADE", "eligible_count": 0,
                "candidates": [{"name": "Momentum", "family": "momentum",
                "rule_version": "v1", "fingerprint": "abc", "eligible": False,
                "reason": "no independent OOS evidence"}],
                "live_route": "No candidate is eligible."}}
    def start(self):
        self.calls.append("start")
        return {"ok": False, "reason": "terminal_not_connected",
                "message": "Press Connect on Setup."}
    def stop(self):
        self.calls.append("stop")
        return {"ok": True, "message": "Stopped; paper positions left open."}
    def select_symbol(self, *, symbol):
        self.calls.append(("select_symbol", symbol))
        return {"ok": True, "message": "Selected."}
    def acknowledge_recovery(self, *, confirmation):
        self.calls.append(("ack", confirmation))
        return {"ok": confirmation == "KEEP_PAPER_BOOK_BLOCKED",
                "reason": "recovery_remains_blocked", "message": "Still blocked."}


def test_start_stop_handlers_require_nonce_and_return_visible_result():
    host = FakeScalper()
    app = WorkstationApp(wizard=SimpleNamespace(session=SimpleNamespace(source=None)), scalper_paper=host)
    assert {"scalper-paper-start", "scalper-paper-stop", "scalper-paper-select-symbol"} <= POST_ACTIONS
    status, _, message = app.act("scalper-paper-start", {"nonce": ["stale"]})
    assert status == 403 and "stale" in message and host.calls == []
    status, _, message = app.act("scalper-paper-start", {})
    assert status == 403 and host.calls == []
    status, path, message = app.act("scalper-paper-start", {"nonce": [app.nonce]})
    assert status == 409 and path == "/autopilot" and "Connect" in message
    status, path, _ = app.act("scalper-paper-stop", {"nonce": [app.nonce]})
    assert status == 303 and path == "/autopilot" and host.calls == ["start", "stop"]
    status, path, _ = app.act("scalper-paper-select-symbol",
                              {"nonce": [app.nonce], "symbol": ["EURUSD.pro"]})
    assert status == 303 and path == "/autopilot" and host.calls[-1] == ("select_symbol", "EURUSD.pro")
    status, _, _ = app.act("scalper-paper-acknowledge-recovery",
                           {"nonce": [app.nonce], "confirmation": ["wrong"]})
    assert status == 409 and host.calls[-1] == ("ack", "wrong")


def test_autopilot_card_has_explicit_paper_label_and_handled_actions():
    html = _render_scalper_paper_card(FakeScalper().status(), "nonce-value")
    assert "AX-SCALPER PAPER" in html
    assert "MT5 live quote" in html
    assert "Start AX-SCALPER PAPER" in html
    assert "action='/actions/scalper-paper-start'" in html
    assert "name='nonce' value='nonce-value'" in html
    assert "cannot send or close broker orders" in html


def test_strategy_review_is_visible_and_symbol_picker_offers_exact_verified_names():
    view = FakeScalper().status()
    view["connected"] = True
    view["symbols"] = [{"broker_symbol": "EURUSD.pro", "pair": "EUR/USD", "tradable": True}]
    view["config"]["symbol"] = "EURUSD.pro"
    html = _render_scalper_paper_card(view, "n")
    assert "Strategy review" in html and "NO TRADE" in html
    assert "Minimum total / OOS trades" in html
    assert "scalper-paper-select-symbol" in html and "EURUSD.pro" in html
    assert "LIVE remains a separate workflow" in html


def test_autopilot_route_renders_scalper_card_even_without_catalog_runner():
    from tests.test_workstation_ui import simulated_wizard
    app = WorkstationApp(wizard=simulated_wizard(), scalper_paper=FakeScalper())
    status, html = app.page("/autopilot")
    assert status == 200
    assert "AX-SCALPER PAPER" in html and "/actions/scalper-paper-start" in html


def test_scalper_paper_source_has_no_broker_mutation_dependencies():
    root = Path(__file__).resolve().parents[1] / "axiom_forex/scalper"
    for filename in ("live_quotes.py", "live_host.py"):
        source = (root / filename).read_text(encoding="utf-8")
        for forbidden in ("order_send(", "order_check(", "close_position(",
                          "MT5TradingAdapter", "MT5OrderAdapter"):
            assert forbidden not in source
