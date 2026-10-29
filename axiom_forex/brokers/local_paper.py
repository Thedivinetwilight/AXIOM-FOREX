from __future__ import annotations

from app.audit.log import AuditLog
from app.execution.broker import BrokerCapabilities, LocalPaperBroker
from app.risk.pretrade import PreTradeGate


def make_local_paper_broker(
    *,
    gate: PreTradeGate,
    capabilities: BrokerCapabilities,
    audit_log: AuditLog,
) -> LocalPaperBroker:
    return LocalPaperBroker(gate=gate, capabilities=capabilities, audit_log=audit_log)
