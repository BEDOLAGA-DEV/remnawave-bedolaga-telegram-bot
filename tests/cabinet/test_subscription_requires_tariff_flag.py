"""Ответ кабинета говорит, что подписке нужен тариф.

Старая подписка (платная, без тарифа при включённых тарифах) не продлевается:
кабинету нужен явный признак ``requires_tariff_selection``, чтобы вместо
«Продлить» вести на выбор тарифа и не показывать тумблер автоплатежа.
Признак считает бот — кабинет не должен угадывать по режиму продаж.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.cabinet.routes.subscription_modules.helpers import _subscription_to_response
from app.config import Settings
from app.database.models import Subscription, SubscriptionStatus


def _subscription(*, tariff_id: int | None, is_trial: bool = False) -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        id=1,
        user_id=1,
        status=SubscriptionStatus.TRIAL.value if is_trial else SubscriptionStatus.ACTIVE.value,
        is_trial=is_trial,
        tariff_id=tariff_id,
        start_date=now - timedelta(days=5),
        end_date=now + timedelta(days=10),
        traffic_limit_gb=0,
        traffic_used_gb=0.0,
        device_limit=3,
        connected_squads=[],
        autopay_enabled=False,
        autopay_days_before=3,
    )


@pytest.fixture
def tariffs_mode(monkeypatch):
    monkeypatch.setattr(Settings, 'is_tariffs_mode', lambda self: True)


@pytest.fixture
def classic_mode(monkeypatch):
    monkeypatch.setattr(Settings, 'is_tariffs_mode', lambda self: False)


def test_paid_subscription_without_tariff_requires_tariff(tariffs_mode):
    response = _subscription_to_response(_subscription(tariff_id=None))

    assert response.requires_tariff_selection is True


def test_subscription_with_tariff_does_not_require_tariff(tariffs_mode):
    response = _subscription_to_response(_subscription(tariff_id=7))

    assert response.requires_tariff_selection is False


def test_trial_without_tariff_does_not_require_tariff(tariffs_mode):
    """Пробная подписка идёт своим путём (покупка тарифа), признак — только для платных."""
    response = _subscription_to_response(_subscription(tariff_id=None, is_trial=True))

    assert response.requires_tariff_selection is False


def test_classic_mode_never_requires_tariff(classic_mode):
    response = _subscription_to_response(_subscription(tariff_id=None))

    assert response.requires_tariff_selection is False


def test_list_item_carries_the_flag_for_paid_subscription_without_tariff(tariffs_mode):
    """Список подписок (мультитариф) тоже говорит кабинету, что подписке нужен тариф."""
    from app.cabinet.routes.subscription_modules.multi_tariff import _subscription_to_list_item

    assert _subscription_to_list_item(_subscription(tariff_id=None)).requires_tariff_selection is True
    assert _subscription_to_list_item(_subscription(tariff_id=7)).requires_tariff_selection is False
    assert _subscription_to_list_item(_subscription(tariff_id=None, is_trial=True)).requires_tariff_selection is False
