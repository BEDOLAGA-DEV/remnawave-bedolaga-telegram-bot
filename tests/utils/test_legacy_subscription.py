"""Одно правило «старой подписки» для бота и кабинета.

Старая подписка — платная, без тарифа, а оператор уже на тарифах (куплена в
классике, потом включили тарифы). Продлить её нельзя, автоплатёж не работает,
единственный путь — выбрать тариф, он надевается на неё же. Правило одно на
меню бота, список тарифов, ответы кабинета и список подписок.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.database.models import Subscription, SubscriptionStatus
from app.utils.legacy_subscription import is_legacy_subscription


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
    )


@pytest.fixture
def tariffs_mode(monkeypatch):
    monkeypatch.setattr(Settings, 'is_tariffs_mode', lambda self: True)


def test_paid_without_tariff_in_tariffs_mode_is_legacy(tariffs_mode):
    assert is_legacy_subscription(_subscription(tariff_id=None)) is True


def test_subscription_with_tariff_is_not_legacy(tariffs_mode):
    assert is_legacy_subscription(_subscription(tariff_id=7)) is False


def test_trial_without_tariff_is_not_legacy(tariffs_mode):
    """Пробная идёт своим путём (покупка тарифа), это не старая подписка."""
    assert is_legacy_subscription(_subscription(tariff_id=None, is_trial=True)) is False


def test_classic_mode_has_no_legacy_subscriptions(monkeypatch):
    monkeypatch.setattr(Settings, 'is_tariffs_mode', lambda self: False)
    assert is_legacy_subscription(_subscription(tariff_id=None)) is False


def test_missing_subscription_is_not_legacy(tariffs_mode):
    assert is_legacy_subscription(None) is False


def test_classic_mode_addon_guard_does_not_fire(monkeypatch):
    """В классическом режиме сторож докупок молчит: подписка без тарифа там — обычная."""
    from app.cabinet.routes.subscription_modules.helpers import ensure_subscription_has_tariff

    monkeypatch.setattr(Settings, 'is_tariffs_mode', lambda self: False)

    ensure_subscription_has_tariff(_subscription(tariff_id=None))  # не бросает
