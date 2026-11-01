"""The broker-side stop, as the Live page reports it.

These are the surfaces an operator actually reads, so they are asserted in the
words they print:

* the broker's own answer about the stop on every open position, never inferred;
* an unprotected position is an urgent warning with its tickets, not a row in a
  table, and it says no new entry will be sent;
* a stop distance that is not configured says exactly that, instead of implying
  the account is protected;
* the plan's watched limit and the broker-side stop are shown as two different
  things, in two different rows.

Read-only: no adapter, terminal, or order is involved.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from axiom_forex.paper_runner.live_workflow import LiveTrading
from axiom_forex.workstation.views import (
    _broker_stop_cell,
    _live_blockers,
    _live_session_card,
)

MAGIC = 26081001


def position(ticket: int, *, stop_loss=None, magic: int = MAGIC):
    return SimpleNamespace(
        ticket=ticket,
        magic=magic,
        stop_loss=stop_loss,
        symbol="EURUSD.pro",
        side="BUY",
        volume="0.10",
    )


def requirement(points: int = 200, declared: str = "") -> dict:
    """What ``MT5TradingAdapter.stop_requirement`` reports for one symbol."""

    text = declared or (f"{points} points" if points else "")
    return {
        "pair": "EUR/USD",
        "broker_symbol": "EURUSD.pro",
        "declared": text,
        "unit": "points" if text else "",
        "distance": str(points) if points else "",
        "distance_points": points or None,
        "distance_price": f"{points * 0.00001:.5f}" if points else None,
        "point": "0.00001",
        "pip_size": "0.0001",
        "broker_min_points": 0,
        "broker_min_price": "0",
        "valid": bool(points),
        "reason": "" if points else "mt5_protective_stop_not_configured_for_symbol",
        "detail": "" if points else "no distance is declared for EUR/USD",
    }


def adapter(*, points: int = 200, positions=(), raises: bool = False):
    class _Adapter:
        settings = SimpleNamespace(protective_stops={}, magic=MAGIC)

        def stop_requirement(self, spec, *, text=None, symbol=None, info=None):
            return requirement(points)

        def positions(self, *, symbol=None):
            if raises:
                raise RuntimeError("terminal gone")
            return list(positions)

    return _Adapter()


def facts(points: int = 200, **kwargs):
    controller = LiveTrading(adapter=adapter(points=points, **kwargs))
    return controller._broker_stop_facts(requirement(points))


def warnings(points: int = 200, **kwargs):
    controller = LiveTrading(adapter=adapter(points=points, **kwargs))
    return controller._stop_warnings(controller._broker_stop_facts(requirement(points)))


# -- what the adapter-side read reports ------------------------------------


def test_no_adapter_means_nothing_has_been_checked():
    result = LiveTrading()._broker_stop_facts()
    assert result["available"] is False
    assert result["detail"] == "no MT5 adapter is attached"
    assert result["unprotected"] == []


def test_unreadable_positions_are_not_a_pass():
    result = facts(raises=True)
    assert result["available"] is False
    assert result["detail"] == "positions unreadable: RuntimeError"
    # The configured distance is still reported; the positions are what is unknown.
    assert result["configured_points"] == 200


def test_a_position_without_a_stop_is_listed_with_its_ticket():
    result = facts(
        positions=[
            position(7001, stop_loss=0.0),
            position(7002, stop_loss=1.0982),
            position(7003),
        ]
    )
    assert result["available"] is True
    assert [row["ticket"] for row in result["unprotected"]] == [7001, 7003]
    assert float(result["unprotected"][0]["stop_loss"]) == 0.0
    # "None" is what the terminal reports for a position with no stop at all.
    assert result["unprotected"][1]["stop_loss"] == ""
    assert result["confirmed"] is False


def test_positions_of_another_expert_advisor_are_not_counted():
    result = facts(positions=[position(7003, magic=42)])
    assert result["unprotected"] == []


def test_a_zero_stop_is_unprotected_but_a_positive_one_is_not():
    result = facts(positions=[position(7004, stop_loss=0.0), position(7005)])
    assert [row["ticket"] for row in result["unprotected"]] == [7004, 7005]
    protected = facts(positions=[position(7006, stop_loss=1.0982)])
    assert protected["unprotected"] == []
    assert protected["confirmed"] is True


# -- warnings ---------------------------------------------------------------


def test_an_unprotected_position_is_an_urgent_warning_naming_the_ticket():
    rows = warnings(positions=[position(7007)])
    urgent = [row for row in rows if row["reason"] == "live_protective_stop_missing"]
    assert urgent and urgent[0]["urgent"] is True
    assert urgent[0]["tickets"] == [7007]
    assert "no broker-side stop loss" in urgent[0]["message"]
    assert "No new entry will be sent" in urgent[0]["hint"]


def test_an_unconfigured_distance_warns_without_being_urgent():
    rows = warnings(points=0)
    assert [row["reason"] for row in rows] == ["broker_stop_not_usable"]
    assert rows[0]["urgent"] is False
    assert "will be refused" in rows[0]["message"]


def test_a_configured_and_protected_book_warns_about_nothing():
    assert warnings(positions=[position(7008, stop_loss=1.0982)]) == []


# -- the page ---------------------------------------------------------------


def card(view):
    return _live_session_card(
        {
            "protective_stop": {"pips": "20", "note": "a limit AXIOM watches"},
            **view,
        }
    )


def test_the_page_separates_the_plan_limit_from_the_broker_stop():
    html = card({"broker_stop": facts(positions=[position(7009, stop_loss=1.0982)])})
    assert "Plan protective stop" in html
    assert "Broker-side stop" in html
    assert "verified by broker" in html


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"points": 0}, "not configured"),
        ({"positions": [position(7010)]}, "unprotected"),
        ({"positions": []}, "configured"),
    ],
)
def test_the_stop_cell_says_what_is_actually_known(kwargs, expected):
    html = _broker_stop_cell({"broker_stop": facts(**kwargs)})
    assert expected in html


def test_the_stop_cell_never_claims_a_check_that_did_not_happen():
    html = _broker_stop_cell({"broker_stop": LiveTrading()._broker_stop_facts()})
    assert "not checked" in html
    assert "verified" not in html


def test_an_unprotected_position_reaches_the_page_as_a_danger_notice():
    page = _live_blockers(
        {
            "blockers": [],
            "warnings": warnings(positions=[position(7011)]),
        }
    )
    assert "notice--danger" in page
    assert "Unprotected exposure" in page
    assert "7011" in page
    # An urgent warning must never sit above "nothing stands in the way".
    assert "Every arming precondition holds" not in page
    assert "No arming precondition is missing" in page
    assert "An entry is still refused while the warning above stands" in page


def test_only_an_unconfigured_distance_is_a_softer_notice():
    page = _live_blockers({"blockers": [], "warnings": warnings(points=0)})
    assert "Unprotected exposure" not in page
    # Nothing is unprotected, so the ordinary "you may arm" sentence is still true.
    assert "Every arming precondition holds" in page
