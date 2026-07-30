"""Cabinet-эндпоинты автопродления Lava.

Покрывают ``app/cabinet/routes/subscription_modules/lava_recurrent.py``:

* ``POST /subscription/lava-recurrent/enable``
* ``GET  /subscription/lava-recurrent``
* ``POST /subscription/lava-recurrent/cancel``

Enable/get обязаны гейтиться ``settings.is_lava_recurrent_enabled()`` ДО любых
обращений к БД; cancel — намеренно НЕ гейтится (операция безопасности: при
выключенной фиче живые привязки продолжают списывать).

Enable обязан грузить тариф явно через ``get_tariff_by_id``: ленивое обращение
к ``subscription.tariff`` вне живого запроса падает MissingGreenlet — поэтому
подписка-заглушка бросает при доступе к ``.tariff``.

Роут-функции вызываются напрямую (без HTTP-клиента), как в
``test_platega_recurrent_routes.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, status

from app.cabinet.routes.subscription_modules import lava_recurrent as route
from app.config import settings
from app.database.models import User


@pytest.fixture
def user() -> User:
    return User(id=1, telegram_id=123456)


class _Subscription:
    def __init__(self, *, id: int = 10, tariff_id: int | None = 5, is_trial: bool = False) -> None:
        self.id = id
        self.tariff_id = tariff_id
        self.is_trial = is_trial

    @property
    def tariff(self):
        raise AssertionError('subscription.tariff must not be lazily accessed — use get_tariff_by_id')


def _gate(monkeypatch, enabled: bool) -> None:
    monkeypatch.setattr(type(settings), 'is_lava_recurrent_enabled', lambda self: enabled)


async def test_enable_gated_before_touching_db(monkeypatch, user):
    _gate(monkeypatch, False)
    resolve = AsyncMock()
    monkeypatch.setattr(route, 'resolve_subscription', resolve)

    with pytest.raises(HTTPException) as exc:
        await route.enable_lava_recurrent(user=user, db=object(), subscription_id=None)

    assert exc.value.status_code == status.HTTP_403_FORBIDDEN
    resolve.assert_not_awaited()


async def test_get_gated_before_touching_db(monkeypatch, user):
    _gate(monkeypatch, False)
    resolve = AsyncMock()
    monkeypatch.setattr(route, 'resolve_subscription', resolve)

    with pytest.raises(HTTPException) as exc:
        await route.get_lava_recurrent(user=user, db=object(), subscription_id=None)

    assert exc.value.status_code == status.HTTP_403_FORBIDDEN
    resolve.assert_not_awaited()


async def test_cancel_works_even_when_gate_off(monkeypatch, user):
    """Отмена — операция безопасности, флагом не гейтится."""
    _gate(monkeypatch, False)
    subscription = _Subscription()
    monkeypatch.setattr(route, 'resolve_subscription', AsyncMock(return_value=subscription))

    cancelled: list[int] = []

    async def fake_cancel(db, subscription_id):
        cancelled.append(subscription_id)

    import app.services.payment.lava as lava_module

    monkeypatch.setattr(lava_module, 'cancel_lava_recurring_for_subscription_safe', fake_cancel)

    result = await route.cancel_lava_recurrent(user=user, db=object(), subscription_id=None)

    assert result == {'status': 'cancelled'}
    assert cancelled == [subscription.id]


async def test_enable_rejects_trial_subscription(monkeypatch, user):
    _gate(monkeypatch, True)
    monkeypatch.setattr(route, 'resolve_subscription', AsyncMock(return_value=_Subscription(is_trial=True)))

    with pytest.raises(HTTPException) as exc:
        await route.enable_lava_recurrent(user=user, db=object(), subscription_id=None)

    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST


async def test_enable_surfaces_missing_product_reason(monkeypatch, user):
    """У тарифа не задан продукт Lava — причина доходит до пользователя."""
    _gate(monkeypatch, True)
    monkeypatch.setattr(route, 'resolve_subscription', AsyncMock(return_value=_Subscription()))

    import app.database.crud.tariff as tariff_crud
    import app.services.payment.lava as lava_module

    monkeypatch.setattr(tariff_crud, 'get_tariff_by_id', AsyncMock(return_value=SimpleNamespace(id=5)))
    monkeypatch.setattr(
        lava_module,
        'enable_lava_recurring',
        AsyncMock(side_effect=ValueError('Для тарифа не задан продукт Lava — автопродление недоступно')),
    )

    with pytest.raises(HTTPException) as exc:
        await route.enable_lava_recurrent(user=user, db=object(), subscription_id=None)

    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST
    assert 'продукт Lava' in exc.value.detail


async def test_enable_returns_payment_url(monkeypatch, user):
    _gate(monkeypatch, True)
    monkeypatch.setattr(route, 'resolve_subscription', AsyncMock(return_value=_Subscription()))

    import app.database.crud.tariff as tariff_crud
    import app.services.payment.lava as lava_module

    monkeypatch.setattr(tariff_crud, 'get_tariff_by_id', AsyncMock(return_value=SimpleNamespace(id=5)))
    monkeypatch.setattr(
        lava_module,
        'enable_lava_recurring',
        AsyncMock(return_value={'status': 'PENDING', 'redirect_url': 'https://pay.lava/x', 'local_id': 3}),
    )

    result = await route.enable_lava_recurrent(user=user, db=object(), subscription_id=None)

    assert result == {'status': 'PENDING', 'redirect_url': 'https://pay.lava/x'}


async def test_get_returns_none_status_without_binding(monkeypatch, user):
    _gate(monkeypatch, True)
    monkeypatch.setattr(route, 'resolve_subscription', AsyncMock(return_value=_Subscription()))

    import app.services.payment.lava as lava_module

    monkeypatch.setattr(lava_module, 'get_lava_recurring_status', AsyncMock(return_value=None))

    assert await route.get_lava_recurrent(user=user, db=object(), subscription_id=None) == {'status': 'none'}


async def test_get_returns_binding_state(monkeypatch, user):
    from datetime import UTC, datetime

    _gate(monkeypatch, True)
    monkeypatch.setattr(route, 'resolve_subscription', AsyncMock(return_value=_Subscription()))

    next_charge = datetime(2026, 8, 1, tzinfo=UTC)
    import app.services.payment.lava as lava_module

    monkeypatch.setattr(
        lava_module,
        'get_lava_recurring_status',
        AsyncMock(
            return_value={
                'status': 'ACTIVE',
                'charge_days': 30,
                'amount_kopeks': 10000,
                'next_charge_at': next_charge,
                'redirect_url': None,
            }
        ),
    )

    result = await route.get_lava_recurrent(user=user, db=object(), subscription_id=None)

    assert result['status'] == 'ACTIVE'
    assert result['charge_days'] == 30
    assert result['amount_kopeks'] == 10000
    assert result['next_charge_at'] == next_charge.isoformat()


async def test_purchase_gated_and_maps_errors(monkeypatch, user):
    """Покупка привязкой: гейт фичи, отказы доносятся как 400."""
    _gate(monkeypatch, False)
    with pytest.raises(HTTPException) as exc:
        await route.purchase_with_lava_recurrent(tariff_id=5, user=user, db=object())
    assert exc.value.status_code == status.HTTP_403_FORBIDDEN

    _gate(monkeypatch, True)
    import app.database.crud.tariff as tariff_crud
    import app.services.payment.lava as lava_module

    monkeypatch.setattr(tariff_crud, 'get_tariff_by_id', AsyncMock(return_value=SimpleNamespace(id=5, is_active=True)))
    monkeypatch.setattr(
        lava_module,
        'purchase_tariff_with_lava_recurring',
        AsyncMock(side_effect=ValueError('Оформление через Lava недоступно для триальной подписки')),
    )
    with pytest.raises(HTTPException) as exc:
        await route.purchase_with_lava_recurrent(tariff_id=5, user=user, db=object())
    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST
    assert 'триальной' in exc.value.detail


async def test_purchase_returns_payment_url_and_subscription(monkeypatch, user):
    _gate(monkeypatch, True)
    import app.database.crud.tariff as tariff_crud
    import app.services.payment.lava as lava_module

    monkeypatch.setattr(tariff_crud, 'get_tariff_by_id', AsyncMock(return_value=SimpleNamespace(id=5, is_active=True)))
    monkeypatch.setattr(
        lava_module,
        'purchase_tariff_with_lava_recurring',
        AsyncMock(return_value={'status': 'PENDING', 'redirect_url': 'https://pay.lava/y', 'subscription_id': 77}),
    )

    result = await route.purchase_with_lava_recurrent(tariff_id=5, user=user, db=object())

    assert result == {'status': 'PENDING', 'redirect_url': 'https://pay.lava/y', 'subscription_id': 77}
