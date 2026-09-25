"""Abstractions for recurring/saved-card payment providers.

Each provider implements :class:`RecurringProvider` and registers itself in
``app/services/payment/recurring/__init__.py``. The rest of the codebase only
talks to the registry, so adding a new provider does not require touching the
monitoring service, CRUD layer or cabinet routes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class CardRegistration:
    """Saved-card data extracted from a provider's webhook payload."""

    provider_token: str
    method_type: str = 'bank_card'
    card_first6: str | None = None
    card_last4: str | None = None
    card_type: str | None = None
    card_expiry_month: str | None = None
    card_expiry_year: str | None = None
    title: str | None = None
    valid_thru: datetime | None = None


@dataclass
class ChargeResult:
    """Outcome of charging a saved card."""

    success: bool
    provider_payment_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class RecurringProvider(ABC):
    """Provider-side adapter for saved-card recurring charges."""

    #: Stable identifier persisted in ``saved_payment_methods.provider``.
    name: str = ''

    @abstractmethod
    def is_enabled(self) -> bool:
        """Return ``True`` if recurring charges via this provider are configured."""

    @abstractmethod
    async def charge(
        self,
        *,
        provider_token: str,
        amount_kopeks: int,
        description: str,
        metadata: dict[str, Any],
        idempotency_key: str,
        user_id: int | None = None,
    ) -> ChargeResult:
        """Initiate a saved-card charge.

        Args:
            provider_token: ``saved_payment_methods.provider_token`` — payment-method
                id (YooKassa), recurring_id (EtoPlatezhi), etc.
            amount_kopeks: charge amount in kopeks.
            description: human-readable payment description.
            metadata: provider metadata payload (passed through to the gateway).
            idempotency_key: idempotency key supplied by the orchestrator.
            user_id: internal user id (for logging only).
        """

    async def revoke(self, *, provider_token: str) -> bool:  # pragma: no cover - default impl
        """Best-effort revoke. Default is a no-op.

        Providers that support revoking saved-card data should override this.
        Returning ``True`` means "local state may now mark the card inactive".
        """
        return True
