"""Regression tests for paid guest-purchase fulfillment orchestration."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.database.models import GuestPurchaseStatus
from app.services.guest_purchase_service import GuestPurchaseError, fulfill_purchase
from app.services.registration_access_service import RegistrationAccessDecision, RegistrationAccessReason


@pytest.mark.asyncio
async def test_fulfill_purchase_passes_purchase_context_to_user_resolution():
    purchase = SimpleNamespace(
        id=17,
        token='a' * 64,
        status=GuestPurchaseStatus.PAID.value,
        is_gift=False,
        gift_recipient_type=None,
        gift_recipient_value=None,
        contact_type='email',
        contact_value='buyer@example.com',
        tariff_id=23,
    )
    result = MagicMock()
    result.scalars.return_value.first.return_value = purchase
    db = AsyncMock()
    db.execute.return_value = result
    user = SimpleNamespace(id=42, language='ru')

    with (
        patch(
            'app.services.guest_purchase_service._find_or_create_user',
            AsyncMock(return_value=(user, True)),
        ) as find_or_create_user,
        patch('app.services.guest_purchase_service.get_tariff_by_id', AsyncMock(return_value=None)),
        # Перед созданием юзера fulfillment перепроверяет invite-only политику. На
        # моке сессии чтение настройки падает и честно закрывается 503 — это своя
        # проверка (test_invite_only_landing), здесь она увела бы тест от предмета.
        patch(
            'app.services.guest_purchase_service.evaluate_guest_purchase_registration',
            AsyncMock(
                return_value=(None, RegistrationAccessDecision(True, RegistrationAccessReason.INVITE_ONLY_DISABLED))
            ),
        ),
    ):
        with pytest.raises(GuestPurchaseError, match='Tariff not found'):
            await fulfill_purchase(db, purchase.token)

    find_or_create_user.assert_awaited_once_with(
        db,
        'email',
        'buyer@example.com',
        purchase=purchase,
        pre_resolved_telegram_id=None,
        tariff_id=23,
    )
    db.rollback.assert_awaited_once()
