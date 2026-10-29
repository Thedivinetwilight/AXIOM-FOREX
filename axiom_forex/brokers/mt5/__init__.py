"""Official MetaTrader 5 integration: terminal, data, and live execution.

Importing this package never imports the ``MetaTrader5`` package, never connects
to a terminal, and never reads a credential.  The module is injected (or lazily
imported) only when an operator explicitly connects.
"""

from __future__ import annotations

from ...config.mt5 import (
    MASKED_DISPLAY,
    MT5_CREDENTIAL_PURPOSE,
    MT5_VAULT_SERVICE,
    MT5ExecutionMode,
    MT5PasswordStore,
    MT5Settings,
    MT5SettingsError,
    MT5SettingsFileError,
    MT5SettingsSurface,
    MT5TradingNotAuthorised,
    load_settings_file,
    make_password_store,
    mt5_settings_document,
)
from .account import (
    AccountTerms,
    MT5AccountMismatch,
    account_terms,
    margin_for_order,
    position_accounting,
    require_declared_currency,
    require_declared_leverage,
    require_margin_mode,
)
from .adapter import (
    MT5BrokerCapabilities,
    MT5OrderReceipt,
    MT5SubmitRefused,
    MT5TradingAdapter,
    make_mt5_adapter,
)
from .conformance import (
    BROKER_DEPENDENT_REQUIREMENTS,
    MT5_CONFORMANCE_REQUIREMENTS,
    broker_dependent_requirements,
    conformance_requirements,
)
from .identity import account_fingerprint
from .orders import (
    MT5OrderCheck,
    MT5OrderOutcome,
    MT5OrderRefused,
    MT5OrderOutcomeUnknown,
    MT5Position,
    build_market_request,
    cancel_pending_order,
    check_order,
    close_volume_check,
    filling_for,
    netting_conflict,
    open_positions,
    send_order,
)
from .reconcile import (
    MT5_BLOCKING_STATES,
    MT5_FINAL_STATES,
    MT5Deal,
    MT5HistoryOrder,
    PositionReconciliation,
    RecoveryResult,
    reconcile_positions,
    reconcile_record,
)
from .retcodes import RetcodeClass, classify, label, summary as retcode_summary
from .session import (
    ANY_ACCOUNT,
    DEMO_ONLY,
    SELECTED_ACCOUNT,
    MAX_ORDERS_PER_CYCLE,
    MT5SessionCycle,
    MT5SessionRefused,
    SessionCheck,
    SessionPolicy,
    SessionReport,
    run_session,
)
from .symbols import (
    SymbolCrossCheck,
    broker_terms_candidate,
    cross_check,
    require_data,
    require_trading,
    to_broker_volume,
)
from .terminal import (
    TIMEFRAMES,
    MT5DataRefused,
    MT5Error,
    MT5ModuleUnavailable,
    MT5NotConnected,
    MT5SymbolInfo,
    MT5Terminal,
    MT5TerminalStatus,
    MT5TerminalUnavailable,
    MT5Tick,
    aggregate_ticks_to_bars,
    import_mt5,
)

__all__ = [name for name in globals() if not name.startswith("_")]
