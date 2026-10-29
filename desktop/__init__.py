"""Pure desktop contract and view models; no backend process or live control."""

from .contract import DESKTOP_REQUESTS, DesktopRequest, contract_audit
from .market_access import (
    CREDENTIAL_PURPOSE,
    MASKED_DISPLAY,
    MARKET_ACCESS_TOKEN_ENTRY,
    CredentialVault,
    KeyringCredentialVault,
    MarketAccessSettings,
    MarketAccessTokenError,
    MarketAccessTokenStore,
    SecureStorageUnavailable,
    SettingsAccessDenied,
    default_vault,
)
from .viewmodels import build_viewmodel

__all__ = [
    "DESKTOP_REQUESTS",
    "CREDENTIAL_PURPOSE",
    "MASKED_DISPLAY",
    "MARKET_ACCESS_TOKEN_ENTRY",
    "CredentialVault",
    "DesktopRequest",
    "KeyringCredentialVault",
    "MarketAccessSettings",
    "MarketAccessTokenError",
    "MarketAccessTokenStore",
    "SecureStorageUnavailable",
    "SettingsAccessDenied",
    "build_viewmodel",
    "contract_audit",
    "default_vault",
]
