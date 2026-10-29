"""Strict account-currency conversion helper; no identity substitution for missing rates."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.accounting.fx import ConversionRateBook
from app.config.settings import AccountProfile


class MissingConversionRate(Exception):
    pass


def account_rate(
    *,
    conversion_rates: ConversionRateBook,
    account: AccountProfile,
    currency: str,
    at: datetime,
) -> Decimal:
    if conversion_rates.source != account.conversion_source:
        raise MissingConversionRate("conversion_source_mismatch")
    result = conversion_rates.get(currency, account.account_currency_code, at)
    if not result.ok or result.rate is None:
        raise MissingConversionRate(result.reason.value)
    return result.rate
