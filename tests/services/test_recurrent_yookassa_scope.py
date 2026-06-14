"""Scoped YooKassa checks for recurrent balance topups."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
from app.database.models import SubscriptionStatus
from app.services import recurrent_payment_service


@pytest.mark.anyio('asyncio')
async def test_process_recurrent_payments_uses_bot_yookassa_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, 'YOOKASSA_ENABLED', False, raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_RECURRENT_ENABLED', False, raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_BOT_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_BOT_SHOP_ID', 'bot-shop', raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_BOT_SECRET_KEY', 'bot-secret', raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_BOT_RECURRENT_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'ENABLE_AUTOPAY', True, raising=False)

    async def fake_find_subscriptions(db: Any) -> list[Any]:
        return []

    monkeypatch.setattr(recurrent_payment_service, '_find_subscriptions_needing_topup', fake_find_subscriptions)

    result = await recurrent_payment_service.process_recurrent_payments(db=SimpleNamespace())

    assert result['checked'] == 0
    assert result.get('skipped') is None


@pytest.mark.anyio('asyncio')
async def test_recurrent_topup_uses_bot_scope_for_card_client_and_local_payment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(
        id=42,
        telegram_id=4242,
        balance_kopeks=1000,
        language='ru',
    )
    subscription = SimpleNamespace(
        id=77,
        user_id=user.id,
        user=user,
        tariff=SimpleNamespace(get_shortest_period=lambda: 30),
        end_date=datetime.now(UTC) + timedelta(hours=1),
        status=SubscriptionStatus.ACTIVE.value,
        autopay_days_before=3,
    )
    saved_method = SimpleNamespace(
        id=9,
        yookassa_payment_method_id='pm_bot_card',
        card_last4='4242',
    )
    active_method_scopes: list[str | None] = []
    created_payment_kwargs: dict[str, Any] = {}

    class FakeYooKassaService:
        configured = True

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def create_autopayment(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            return {
                'id': 'yk_bot_recurrent',
                'status': 'succeeded',
                'paid': True,
                'created_at': datetime.now(UTC).isoformat(),
                'test_mode': True,
            }

    fake_yookassa_service = FakeYooKassaService()

    class FakePaymentService:
        yookassa_service = fake_yookassa_service

        def __init__(self) -> None:
            self.requested_scopes: list[str | None] = []

        def _get_yookassa_service_for_scope(self, scope: str | None) -> FakeYooKassaService:
            self.requested_scopes.append(scope)
            return fake_yookassa_service

    class FakePricingEngine:
        async def calculate_renewal_price(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(final_total=5000)

    async def fake_lock_user_for_pricing(db: Any, user_id: int) -> Any:
        assert user_id == user.id
        return user

    async def fake_get_active_payment_methods_by_user(
        db: Any,
        user_id: int,
        yookassa_scope: str | None = None,
    ) -> list[Any]:
        assert user_id == user.id
        active_method_scopes.append(yookassa_scope)
        return [saved_method]

    async def fake_create_yookassa_payment(**kwargs: Any) -> SimpleNamespace:
        created_payment_kwargs.update(kwargs)
        return SimpleNamespace(id=123)

    monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', fake_lock_user_for_pricing)
    monkeypatch.setattr('app.services.pricing_engine.pricing_engine', FakePricingEngine())
    monkeypatch.setattr(
        'app.database.crud.saved_payment_method.get_active_payment_methods_by_user',
        fake_get_active_payment_methods_by_user,
    )
    monkeypatch.setattr('app.database.crud.yookassa.create_yookassa_payment', fake_create_yookassa_payment)

    payment_service = FakePaymentService()

    result = await recurrent_payment_service._process_single_subscription(
        db=SimpleNamespace(),
        subscription=subscription,
        user=user,
        bot=None,
        payment_service=payment_service,
        subscription_service=SimpleNamespace(),
    )

    assert result == 'created'
    assert payment_service.requested_scopes == ['bot']
    assert active_method_scopes == ['bot']
    assert fake_yookassa_service.calls[0]['metadata']['yookassa_scope'] == 'bot'
    assert created_payment_kwargs['yookassa_scope'] == 'bot'
