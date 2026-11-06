from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.notification_delivery_service import NotificationDeliveryService


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'language, days_word', [('ru', 'дн.'), ('en', 'days'), ('ua', 'дн.'), ('zh', '天'), ('fa', 'روز')]
)
async def test_referral_context_localizes_description_and_preserves_amounts(language, days_word):
    service = NotificationDeliveryService()
    service.send_notification = AsyncMock(return_value=True)
    user = SimpleNamespace(language=language)
    await service.notify_referral_bonus(
        user, 12500, 'Custom person', bonus_days=7, tariff_name='Custom <plan>', level=2
    )
    data = service.send_notification.await_args.kwargs['context']
    assert data['bonus_kopeks'] == 12500
    assert data['bonus_rubles'] == 125
    assert data['bonus_days'] == 7
    assert data['level'] == 2
    assert days_word in data['formatted_reward']
    assert 'Custom <plan>' in data['formatted_reward']
    assert data['referral_name'] == 'Custom person'


@pytest.mark.asyncio
async def test_custom_ban_reason_is_data():
    service = NotificationDeliveryService()
    service.send_notification = AsyncMock(return_value=True)
    await service.notify_ban(SimpleNamespace(language='fa'), reason='Operator <reason>')
    assert service.send_notification.await_args.kwargs['context']['reason'] == 'Operator <reason>'
