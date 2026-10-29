"""Broker boundaries: local paper by default, MetaTrader 5 when attached.

Importing this package does not import the optional ``MetaTrader5`` module,
connect to a terminal, or read a credential.
"""

from __future__ import annotations

from .external import (
    CONFORMANCE_REQUIREMENTS,
    ExternalBrokerBoundary,
    conformance_checklist,
    mt5_conformance_checklist,
)
from .interfaces import (
    BrokerAdapter,
    BrokerCapabilities,
    LocalPaperBroker,
    PaperOrderReceipt,
    TradingBlocked,
)
from .local_paper import make_local_paper_broker
from .mt5 import (
    MT5ExecutionMode,
    MT5PasswordStore,
    MT5Settings,
    MT5SettingsFileError,
    MT5SettingsSurface,
    MT5TradingAdapter,
    load_settings_file,
    make_mt5_adapter,
    make_password_store,
    mt5_settings_document,
)

__all__ = [
    "BrokerAdapter",
    "BrokerCapabilities",
    "CONFORMANCE_REQUIREMENTS",
    "ExternalBrokerBoundary",
    "LocalPaperBroker",
    "MT5ExecutionMode",
    "MT5PasswordStore",
    "MT5Settings",
    "MT5SettingsFileError",
    "MT5SettingsSurface",
    "MT5TradingAdapter",
    "PaperOrderReceipt",
    "TradingBlocked",
    "conformance_checklist",
    "load_settings_file",
    "make_local_paper_broker",
    "make_mt5_adapter",
    "make_password_store",
    "mt5_conformance_checklist",
    "mt5_settings_document",
]
