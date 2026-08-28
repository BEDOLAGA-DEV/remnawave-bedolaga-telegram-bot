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

from app.database.models import PromoCodeType, SubscriptionStatus
from app.services.promocode_service import PromoCodeService


def _combo_promocode(**overrides) -> SimpleNamespace:
    base = dict(
        id=42,
        code='COMBO50',
        type=PromoCodeType.BALANCE_AND_DAYS.value,
        balance_bonus_kopeks=50000,  # 500 ₽
        subscription_days=7,
        traffic_gb=0,  # третий бонус набора; по умолчанию выключен
        tariff_id=None,
        promo_group_id=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _service(monkeypatch, panel=None) -> PromoCodeService:
    monkeypatch.setattr('app.services.promocode_service.RemnaWaveService', MagicMock())
    stub = panel or SimpleNamespace(update_remnawave_user=AsyncMock(), enable_remnawave_user=AsyncMock())
    monkeypatch.setattr('app.services.promocode_service.SubscriptionService', lambda: stub)
    return PromoCodeService()


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=1, telegram_id=100, email=None, balance_kopeks=0, language='ru')


def _subscription(**overrides) -> SimpleNamespace:
    base = dict(
        id=5,
        days_left=3,
        tariff=None,
        is_trial=False,
        # трафик читается начислением: 0 означает безлимит (Subscription.add_traffic)
        traffic_limit_gb=100,
        status=SubscriptionStatus.ACTIVE.value,
        remnawave_id=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


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


def test_cabinet_bonus_set_requires_at_least_one_component():
    """Кабинетная валидация: в наборе должна быть хотя бы одна составляющая."""
    from app.cabinet.routes.admin_promocodes import _validate_create_payload

    def payload(**kw):
        base = dict(
            code='COMBO',
            type=PromoCodeType.BALANCE_AND_DAYS,
            balance_bonus_kopeks=10000,
            subscription_days=7,
            traffic_gb=0,
            valid_from=None,
            valid_until=None,
        )
        base.update(kw)
        return SimpleNamespace(**base)

    _validate_create_payload(payload())  # весь набор — ок

    # Набор собирается галочками: одной включённой составляющей достаточно,
    # иначе «дни + трафик» без баланса нельзя было бы завести.
    _validate_create_payload(payload(balance_bonus_kopeks=0))
    _validate_create_payload(payload(subscription_days=0))
    _validate_create_payload(payload(balance_bonus_kopeks=0, subscription_days=0, traffic_gb=50))

    # А вот пустой набор бессмысленен — ничего не начисляется.
    with pytest.raises(HTTPException):
        _validate_create_payload(payload(balance_bonus_kopeks=0, subscription_days=0, traffic_gb=0))

    with pytest.raises(HTTPException):
        _validate_create_payload(payload(traffic_gb=-5))


def test_webapi_bonus_set_requires_at_least_one_component():
    from app.webapi.routes.promocodes import _validate_create_payload

    def payload(**kw):
        base = dict(
            code='COMBO',
            type=PromoCodeType.BALANCE_AND_DAYS,
            balance_bonus_kopeks=10000,
            subscription_days=7,
            traffic_gb=0,
            valid_from=None,
            valid_until=None,
        )
        base.update(kw)
        return SimpleNamespace(**base)

    _validate_create_payload(payload())

    _validate_create_payload(payload(balance_bonus_kopeks=0))
    _validate_create_payload(payload(subscription_days=0))

    with pytest.raises(HTTPException):
        _validate_create_payload(payload(balance_bonus_kopeks=0, subscription_days=0))


async def test_combo_grants_traffic(monkeypatch):
    """Трафик из набора начисляется той же подписке, что и дни."""
    monkeypatch.setattr(
        type(__import__('app.config', fromlist=['settings']).settings),
        'is_multi_tariff_enabled',
        lambda self: False,
        raising=False,
    )
    service = _service(monkeypatch)

    sub = _subscription()
    monkeypatch.setattr('app.services.promocode_service.get_subscription_by_user_id', AsyncMock(return_value=sub))
    monkeypatch.setattr('app.services.promocode_service.extend_subscription', AsyncMock(return_value=sub))
    monkeypatch.setattr('app.services.promocode_service.add_user_balance', AsyncMock(return_value=True))
    add_traffic = AsyncMock(return_value=sub)
    monkeypatch.setattr('app.database.crud.subscription.add_subscription_traffic', add_traffic)

    description = await service._apply_promocode_effects(AsyncMock(), _user(), _combo_promocode(traffic_gb=50))

    add_traffic.assert_awaited_once()
    assert add_traffic.await_args.args[1] is sub  # та же подписка, что у дней
    assert add_traffic.await_args.args[2] == 50
    assert '50 ГБ' in description


async def test_combo_without_traffic_does_not_touch_it(monkeypatch):
    """traffic_gb=0 — начисления нет, старые коды ведут себя как прежде."""
    monkeypatch.setattr(
        type(__import__('app.config', fromlist=['settings']).settings),
        'is_multi_tariff_enabled',
        lambda self: False,
        raising=False,
    )
    service = _service(monkeypatch)

    sub = _subscription()
    monkeypatch.setattr('app.services.promocode_service.get_subscription_by_user_id', AsyncMock(return_value=sub))
    monkeypatch.setattr('app.services.promocode_service.extend_subscription', AsyncMock(return_value=sub))
    monkeypatch.setattr('app.services.promocode_service.add_user_balance', AsyncMock(return_value=True))
    add_traffic = AsyncMock()
    monkeypatch.setattr('app.database.crud.subscription.add_subscription_traffic', add_traffic)

    await service._apply_promocode_effects(AsyncMock(), _user(), _combo_promocode())

    add_traffic.assert_not_awaited()


async def test_traffic_reactivates_limited_subscription(monkeypatch):
    """Трафик чаще всего дарят тому, у кого он кончился, — подписка в LIMITED.

    Без реактивации гигабайты лягут в базу, а в панель уедет тот же LIMITED:
    человек получит бонус и останется без доступа.
    """
    monkeypatch.setattr(
        type(__import__('app.config', fromlist=['settings']).settings),
        'is_multi_tariff_enabled',
        lambda self: False,
        raising=False,
    )
    panel = SimpleNamespace(update_remnawave_user=AsyncMock(), enable_remnawave_user=AsyncMock())
    service = _service(monkeypatch, panel)

    sub = _subscription(status=SubscriptionStatus.LIMITED.value)

    async def fake_reactivate(db, subscription):
        subscription.status = SubscriptionStatus.ACTIVE.value
        return subscription

    reactivate = AsyncMock(side_effect=fake_reactivate)
    monkeypatch.setattr('app.services.promocode_service.get_subscription_by_user_id', AsyncMock(return_value=sub))
    monkeypatch.setattr('app.services.promocode_service.add_user_balance', AsyncMock(return_value=True))
    monkeypatch.setattr('app.database.crud.subscription.add_subscription_traffic', AsyncMock(return_value=sub))
    monkeypatch.setattr('app.database.crud.subscription.reactivate_subscription', reactivate)

    user = _user()
    user.remnawave_id = 4242
    await service._apply_promocode_effects(AsyncMock(), user, _combo_promocode(traffic_gb=50, subscription_days=0))

    reactivate.assert_awaited_once()
    assert sub.status == SubscriptionStatus.ACTIVE.value
    # PATCH не всегда снимает LIMITED — включаем явно
    panel.enable_remnawave_user.assert_awaited_once_with(4242)


async def test_traffic_not_granted_on_unlimited_subscription(monkeypatch):
    """Безлимит: Subscription.add_traffic ничего не делает — и обещать нечего.

    Иначе код сгорает, а пользователю рапортуют о гигабайтах, которых он
    не получил.
    """
    monkeypatch.setattr(
        type(__import__('app.config', fromlist=['settings']).settings),
        'is_multi_tariff_enabled',
        lambda self: False,
        raising=False,
    )
    service = _service(monkeypatch)

    sub = _subscription(traffic_limit_gb=0)
    add_traffic = AsyncMock()
    monkeypatch.setattr('app.services.promocode_service.get_subscription_by_user_id', AsyncMock(return_value=sub))
    monkeypatch.setattr('app.services.promocode_service.extend_subscription', AsyncMock(return_value=sub))
    monkeypatch.setattr('app.services.promocode_service.add_user_balance', AsyncMock(return_value=True))
    monkeypatch.setattr('app.database.crud.subscription.add_subscription_traffic', add_traffic)

    description = await service._apply_promocode_effects(AsyncMock(), _user(), _combo_promocode(traffic_gb=50))

    add_traffic.assert_not_awaited()
    assert 'ГБ' not in description


async def test_target_subscription_picked_once_per_activation(monkeypatch):
    """Дни и трафик обязаны попасть в ОДНУ подписку — выбор делается один раз.

    Второй независимый выбор в мультитарифе может вернуть другую строку, и
    один код разложится по разным подпискам.
    """
    monkeypatch.setattr(
        type(__import__('app.config', fromlist=['settings']).settings),
        'is_multi_tariff_enabled',
        lambda self: False,
        raising=False,
    )
    service = _service(monkeypatch)

    sub = _subscription()
    monkeypatch.setattr('app.services.promocode_service.get_subscription_by_user_id', AsyncMock(return_value=sub))
    monkeypatch.setattr('app.services.promocode_service.extend_subscription', AsyncMock(return_value=sub))
    monkeypatch.setattr('app.services.promocode_service.add_user_balance', AsyncMock(return_value=True))
    monkeypatch.setattr('app.database.crud.subscription.add_subscription_traffic', AsyncMock(return_value=sub))
    monkeypatch.setattr('app.database.crud.subscription.reactivate_subscription', AsyncMock(return_value=sub))

    picks = []
    original = service._pick_target_subscription

    async def counting_pick(db, user, promocode, subscription_id):
        picks.append(subscription_id)
        return await original(db, user, promocode, subscription_id)

    monkeypatch.setattr(service, '_pick_target_subscription', counting_pick)

    await service._apply_promocode_effects(AsyncMock(), _user(), _combo_promocode(traffic_gb=50))

    assert len(picks) == 1


async def test_traffic_only_applies_to_the_bonus_set_type(monkeypatch):
    """Трафик — составляющая набора. Код другого типа его не раздаёт.

    Поле есть у всех строк промокодов, поэтому без проверки типа сюда попал бы
    и код «только дни», у которого трафик выставили по ошибке.
    """
    monkeypatch.setattr(
        type(__import__('app.config', fromlist=['settings']).settings),
        'is_multi_tariff_enabled',
        lambda self: False,
        raising=False,
    )
    service = _service(monkeypatch)

    sub = _subscription()
    add_traffic = AsyncMock()
    monkeypatch.setattr('app.services.promocode_service.get_subscription_by_user_id', AsyncMock(return_value=sub))
    monkeypatch.setattr('app.services.promocode_service.extend_subscription', AsyncMock(return_value=sub))
    monkeypatch.setattr('app.database.crud.subscription.add_subscription_traffic', add_traffic)

    days_only = _combo_promocode(type=PromoCodeType.SUBSCRIPTION_DAYS.value, traffic_gb=50, balance_bonus_kopeks=0)
    await service._apply_promocode_effects(AsyncMock(), _user(), days_only)

    add_traffic.assert_not_awaited()


async def test_traffic_only_on_unlimited_does_not_burn_the_code(monkeypatch):
    """Трафик — единственная составляющая, а подписка безлимитная: попытка не сгорает.

    Запись использования и инкремент счётчика делаются ДО эффектов и
    откатываются только через исключение. Вернуть общий успех значит забрать у
    человека единственную попытку и не дать ничего.
    """
    monkeypatch.setattr(
        type(__import__('app.config', fromlist=['settings']).settings),
        'is_multi_tariff_enabled',
        lambda self: False,
        raising=False,
    )
    service = _service(monkeypatch)

    sub = _subscription(traffic_limit_gb=0)
    monkeypatch.setattr('app.services.promocode_service.get_subscription_by_user_id', AsyncMock(return_value=sub))
    monkeypatch.setattr('app.database.crud.subscription.add_subscription_traffic', AsyncMock())

    traffic_only = _combo_promocode(traffic_gb=50, subscription_days=0, balance_bonus_kopeks=0)

    with pytest.raises(ValueError, match='traffic_not_applicable'):
        await service._apply_promocode_effects(AsyncMock(), _user(), traffic_only)


async def test_unlimited_subscription_keeps_other_bonuses(monkeypatch):
    """Если в наборе есть что-то ещё — оно начисляется, а код не откатывается."""
    monkeypatch.setattr(
        type(__import__('app.config', fromlist=['settings']).settings),
        'is_multi_tariff_enabled',
        lambda self: False,
        raising=False,
    )
    service = _service(monkeypatch)

    sub = _subscription(traffic_limit_gb=0)
    monkeypatch.setattr('app.services.promocode_service.get_subscription_by_user_id', AsyncMock(return_value=sub))
    monkeypatch.setattr('app.services.promocode_service.extend_subscription', AsyncMock(return_value=sub))
    monkeypatch.setattr('app.services.promocode_service.add_user_balance', AsyncMock(return_value=True))
    monkeypatch.setattr('app.database.crud.subscription.add_subscription_traffic', AsyncMock())

    description = await service._apply_promocode_effects(AsyncMock(), _user(), _combo_promocode(traffic_gb=50))

    assert 'ГБ' not in description
    assert description.strip()
