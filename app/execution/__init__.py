"""Local paper-only execution boundary. No remote order routing exists."""

from .broker import (
    BrokerAdapter,
    BrokerCapabilities,
    LocalPaperBroker,
    PaperOrderReceipt,
    TradingBlocked,
)

__all__ = [
    "BrokerAdapter",
    "BrokerCapabilities",
    "LocalPaperBroker",
    "PaperOrderReceipt",
    "TradingBlocked",
]
