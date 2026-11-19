"""Read-only exact MT5 evidence for one durable scalper mutation.

No correlation by comment, timing or a coincidental position is accepted. The
broker order ticket must already be present in the locally persisted response.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable, Mapping

from ..brokers.mt5.reconcile import _time_window, history_deals, reconcile_record


def inspect_exact_history(*, adapter: Any, order: Mapping[str, Any],
                          journal: Mapping[str, Any],
                          account_matches: Callable[[], bool]) -> dict[str, Any]:
    """Read a uniquely linked receipt and current position; never submit or book."""
    def unknown(reason: str) -> dict[str, Any]:
        return {"resolved": False, "reason": reason}

    try:
        intent, identity = journal["intent"], journal["identity"]
        request_id, action = str(journal["request_id"]), str(journal["action"])
        if action not in {"entry", "close"} or request_id != str(order.get("request_id") or ""):
            return unknown("request_identity_mismatch")
        if (str(intent.get("request_id") or "") != request_id
                or str(order.get("provider_symbol") or "") != str(identity.get("symbol") or "")
                or str(order.get("account_fingerprint") or "") != str(identity.get("account_fingerprint") or "")
                or str(order.get("account_server") or "") != str(identity.get("account_server") or "")
                or str(order.get("account_currency") or "") != str(identity.get("account_currency") or "")
                or str(order.get("account_mode") or "") != str(identity.get("account_mode") or "")
                or not str(order.get("strategy_id") or "").startswith("ax-scalper-live:")
                or int(order.get("magic") or 0) <= 0
                or int(order.get("order_ticket") or 0) <= 0):
            return unknown("broker_order_identity_incomplete")
        prior = intent.get("prior_position") or {}
        requested = Decimal(str(intent.get("lots") if action == "entry" else intent.get("requested_lots")))
        if not requested.is_finite() or requested <= 0 or Decimal(str(order.get("volume"))) != requested:
            return unknown("broker_order_volume_mismatch")
        expected_side = str(intent.get("side") or "").upper() if action == "entry" else (
            "SELL" if prior.get("side") == "BUY" else "BUY" if prior.get("side") == "SELL" else "")
        if str(order.get("side") or "").upper() != expected_side or expected_side not in {"BUY", "SELL"}:
            return unknown("broker_order_side_mismatch")
        if action == "entry" and order.get("close_position_id"):
            return unknown("broker_order_action_mismatch")
        if action == "close":
            ticket = str(intent.get("position_id") or "")
            if (not ticket or str(order.get("close_position_ticket") or "") != ticket
                    or str(order.get("close_position_id") or "") != "mt5:" + ticket
                    or int(order.get("close_position_identifier") or 0) <= 0):
                return unknown("broker_close_identity_mismatch")
        if not account_matches():
            return unknown("account_identity_mismatch")
        result = reconcile_record(adapter.terminal, order)
        if not result.resolved or result.state not in {"FILLED", "REJECTED"}:
            return unknown("broker_history_" + result.evidence)
        previous_state = str(order.get("state") or "").upper()
        previous_class = str(order.get("classification") or "").upper()
        previous_volume = Decimal(str(order.get("volume_filled") or 0))
        if (not previous_volume.is_finite() or previous_volume < 0
                or order.get("booked") is True
                or (result.state == "REJECTED" and (previous_volume > 0
                    or previous_state in {"FILLED", "PARTIAL"} or previous_class == "OK"))
                or (result.state == "FILLED" and (previous_state in {"REJECTED", "CANCELED"}
                    or previous_class in {"REJECTED", "RETRYABLE"}))):
            return unknown("broker_history_contradicts_durable_response")
        if result.state == "REJECTED":
            if not account_matches():
                return unknown("account_changed_during_history")
            return {"resolved": True, "state": "REJECTED", "request_id": request_id,
                    "recovery": result.audit_view()}
        start, end = _time_window(order)
        deals = [deal for deal in history_deals(adapter.terminal, start=start, end=end)
                 if deal.order == result.order_ticket]
        if (len(deals) != 1 or tuple(d.ticket for d in deals) != result.deal_tickets
                or result.volume_matched != requested):
            return unknown("multi_or_incomplete_deal_history")
        deal = deals[0]
        if deal.time is None or deal.volume != requested:
            return unknown("deal_time_or_volume_missing")
        broker = list(adapter.positions())
        matching = [position for position in broker if position.identifier == deal.position_id]
        if action == "entry":
            if (len(matching) != 1 or matching[0].identifier <= 0
                    or matching[0].symbol != identity["symbol"] or matching[0].side != expected_side
                    or matching[0].magic != int(order["magic"]) or matching[0].volume != requested
                    or matching[0].price_open != deal.price):
                return unknown("entry_position_not_exact")
            position = matching[0]
            stop = Decimal(str(intent.get("stop_price")))
            target = None if intent.get("target_price") is None else Decimal(str(intent.get("target_price")))
            actual_target = position.take_profit if position.take_profit and position.take_profit > 0 else None
            if (not stop.is_finite() or stop <= 0
                    or (expected_side == "BUY" and stop >= deal.price)
                    or (expected_side == "SELL" and stop <= deal.price)
                    or (target is not None and (not target.is_finite() or target <= 0
                        or expected_side == "BUY" and target <= deal.price
                        or expected_side == "SELL" and target >= deal.price))
                    or position.stop_loss != stop or actual_target != target):
                return unknown("entry_protection_not_exact")
            ticket = str(position.ticket)
        else:
            ticket = str(intent["position_id"])
            if (deal.position_id != int(order["close_position_identifier"])
                    or str(prior.get("position_id") or "") != ticket):
                return unknown("close_position_identifier_mismatch")
            remaining = Decimal(str(prior.get("lots"))) - requested
            if remaining < 0:
                return unknown("close_volume_exceeds_prior")
            if remaining == 0:
                if matching or any(str(position.ticket) == ticket for position in broker):
                    return unknown("closed_position_still_present")
            else:
                if len(matching) != 1:
                    return unknown("partial_close_position_unavailable")
                position = matching[0]
                actual_target = position.take_profit if position.take_profit and position.take_profit > 0 else None
                target = None if prior.get("target_price") is None else Decimal(str(prior["target_price"]))
                if (str(position.ticket) != ticket or position.symbol != identity["symbol"]
                        or position.side != str(prior.get("side") or "")
                        or position.volume != remaining
                        or position.price_open != Decimal(str(prior.get("entry_price")))
                        or position.stop_loss != Decimal(str(prior.get("stop_price")))
                        or actual_target != target):
                    return unknown("partial_close_position_mismatch")
        if not account_matches():
            return unknown("account_changed_during_history")
        return {"resolved": True, "state": "FILLED", "request_id": request_id,
                "ticket": ticket, "price": str(deal.price), "lots": str(deal.volume),
                "at": deal.time.isoformat(), "deal_ticket": deal.ticket,
                "position_identifier": deal.position_id, "recovery": result.audit_view()}
    except Exception as exc:
        return unknown("history_unavailable:" + type(exc).__name__)
