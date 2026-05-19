"""Registry of recurring payment providers.

Import providers here so they self-register via ``register_provider``. Other
modules should use :func:`get_provider` / :func:`enabled_providers` instead of
importing concrete classes.
"""

from __future__ import annotations

from app.services.payment.recurring.base import (
    CardRegistration,
    ChargeResult,
    RecurringProvider,
)
from app.services.payment.recurring.yookassa_provider import YooKassaRecurringProvider


_PROVIDERS: dict[str, RecurringProvider] = {}


def register_provider(provider: RecurringProvider) -> None:
    """Register a provider in the global registry. Last writer wins."""
    if not provider.name:
        raise ValueError('RecurringProvider.name must be a non-empty string')
    _PROVIDERS[provider.name] = provider


def get_provider(name: str) -> RecurringProvider | None:
    """Return the registered provider or ``None`` when unknown."""
    return _PROVIDERS.get(name)


def enabled_providers() -> list[RecurringProvider]:
    """Return all providers whose configuration is currently complete."""
    return [provider for provider in _PROVIDERS.values() if provider.is_enabled()]


def is_any_recurring_enabled() -> bool:
    """True if at least one recurring provider can charge saved cards."""
    return any(provider.is_enabled() for provider in _PROVIDERS.values())


# Built-in providers
register_provider(YooKassaRecurringProvider())


__all__ = [
    'CardRegistration',
    'ChargeResult',
    'RecurringProvider',
    'enabled_providers',
    'get_provider',
    'is_any_recurring_enabled',
    'register_provider',
]
