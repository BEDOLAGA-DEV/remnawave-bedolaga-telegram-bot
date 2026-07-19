"""Комбинированный промокод BALANCE_AND_DAYS: и бонус на баланс, и дни подписки
одним кодом (фича-реквест из кабинета: раньше можно было выбрать только одно).

Ключевой инвариант — ПОРЯДОК эффектов: дни применяются ПЕРЕД балансом. Блок
дней может прерваться исключением (нет подписки, выбор подписки в
мульти-тарифе) с rollback'ом всей активации, а add_user_balance коммитит
внутри себя — обратный порядок дарил бы баланс при откате записи
использования, и повторная активация задваивала бы бонус.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.database.models import PromoCodeType
from app.services.promocode_service import PromoCodeService


def _combo_promocode(**overrides) -> SimpleNamespace:
    base = dict(
        id=42,
        code='COMBO50',
        type=PromoCodeType.BALANCE_AND_DAYS.value,
        balance_bonus_kopeks=50000,  # 500 ₽
        subscription_days=7,
        tariff_id=None,
        promo_group_id=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _service(monkeypatch) -> PromoCodeService:
    monkeypatch.setattr('app.services.promocode_service.RemnaWaveService', MagicMock())
    monkeypatch.setattr(
        'app.services.promocode_service.SubscriptionService',
        lambda: SimpleNamespace(update_remnawave_user=AsyncMock()),
    )
    return PromoCodeService()


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=1, telegram_id=100, email=None, balance_kopeks=0, language='ru')


def _subscription() -> SimpleNamespace:
    return SimpleNamespace(id=5, days_left=3, tariff=None, is_trial=False)


async def test_combo_applies_both_days_and_balance(monkeypatch):
    """Оба эффекта применяются: подписка продлена И баланс пополнен."""
    monkeypatch.setattr(
        type(__import__('app.config', fromlist=['settings']).settings),
        'is_multi_tariff_enabled',
        lambda self: False,
        raising=False,
    )
    service = _service(monkeypatch)

    sub = _subscription()
    monkeypatch.setattr('app.services.promocode_service.get_subscription_by_user_id', AsyncMock(return_value=sub))
    extend = AsyncMock(return_value=sub)
    monkeypatch.setattr('app.services.promocode_service.extend_subscription', extend)
    add_balance = AsyncMock(return_value=True)
    monkeypatch.setattr('app.services.promocode_service.add_user_balance', add_balance)

    description = await service._apply_promocode_effects(AsyncMock(), _user(), _combo_promocode())

    extend.assert_awaited_once()
    assert extend.await_args.args[2] == 7  # дни из промокода
    add_balance.assert_awaited_once()
    assert add_balance.await_args.args[2] == 50000  # копейки из промокода
    assert 'продлена на 7' in description
    assert '500' in description  # 500₽ в тексте эффекта


async def test_combo_days_failure_prevents_balance_credit(monkeypatch):
    """Нет подписки → блок дней падает ДО начисления баланса: add_user_balance
    не вызывается, activate откатит активацию целиком — без подарённых денег."""
    monkeypatch.setattr(
        type(__import__('app.config', fromlist=['settings']).settings),
        'is_multi_tariff_enabled',
        lambda self: False,
        raising=False,
    )
    service = _service(monkeypatch)

    monkeypatch.setattr('app.services.promocode_service.get_subscription_by_user_id', AsyncMock(return_value=None))
    add_balance = AsyncMock(return_value=True)
    monkeypatch.setattr('app.services.promocode_service.add_user_balance', add_balance)

    with pytest.raises(ValueError, match='no_subscription_for_days'):
        await service._apply_promocode_effects(AsyncMock(), _user(), _combo_promocode())

    add_balance.assert_not_awaited()  # порядок: дни раньше баланса


async def test_single_balance_type_untouched(monkeypatch):
    """Одиночный BALANCE-код по-прежнему только пополняет баланс."""
    service = _service(monkeypatch)

    add_balance = AsyncMock(return_value=True)
    monkeypatch.setattr('app.services.promocode_service.add_user_balance', add_balance)
    extend = AsyncMock()
    monkeypatch.setattr('app.services.promocode_service.extend_subscription', extend)

    promo = _combo_promocode(type=PromoCodeType.BALANCE.value, subscription_days=0)
    await service._apply_promocode_effects(AsyncMock(), _user(), promo)

    add_balance.assert_awaited_once()
    extend.assert_not_awaited()


def test_cabinet_create_validation_requires_both_fields():
    """Кабинетная валидация: комбинированному коду нужны И сумма, И дни."""
    from app.cabinet.routes.admin_promocodes import _validate_create_payload

    def payload(**kw):
        base = dict(
            code='COMBO',
            type=PromoCodeType.BALANCE_AND_DAYS,
            balance_bonus_kopeks=10000,
            subscription_days=7,
            valid_from=None,
            valid_until=None,
        )
        base.update(kw)
        return SimpleNamespace(**base)

    _validate_create_payload(payload())  # оба поля > 0 — ок

    with pytest.raises(HTTPException):
        _validate_create_payload(payload(balance_bonus_kopeks=0))

    with pytest.raises(HTTPException):
        _validate_create_payload(payload(subscription_days=0))


def test_webapi_create_validation_requires_both_fields():
    from app.webapi.routes.promocodes import _validate_create_payload

    def payload(**kw):
        base = dict(
            code='COMBO',
            type=PromoCodeType.BALANCE_AND_DAYS,
            balance_bonus_kopeks=10000,
            subscription_days=7,
            valid_from=None,
            valid_until=None,
        )
        base.update(kw)
        return SimpleNamespace(**base)

    _validate_create_payload(payload())

    with pytest.raises(HTTPException):
        _validate_create_payload(payload(balance_bonus_kopeks=0))

    with pytest.raises(HTTPException):
        _validate_create_payload(payload(subscription_days=0))
