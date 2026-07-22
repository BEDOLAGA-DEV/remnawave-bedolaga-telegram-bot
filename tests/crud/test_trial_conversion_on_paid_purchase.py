"""Регрессия (прод-репорт 2026-07, SALES_MODE=tariffs + MULTI_TARIFF_ENABLED):
первая платная покупка при ЖИВОМ триале должна КОНВЕРТИРОВАТЬ строку триала на
месте (тот же Remnawave-юзер и ссылка), а не вставлять новую подписку.

Старое поведение: ``create_paid_subscription`` вставлял новую строку → новый
панельный юзер (#created), а живой триал глушился в DISABLED (#disabled) и
продолжал висеть в кабинете рядом с купленным тарифом. Пользователь при этом
терял ссылку/устройства, настроенные за время триала.

Новое поведение зеркалит classic-режим (там триал всегда конвертировался в
той же записи): в мульти-тарифе живой (active/trial/limited) триал уходит в
``_convert_trial_subscription_to_paid`` → ``extend_subscription`` (смена тарифа,
перенос остатка по TRIAL_ADD_REMAINING_DAYS_TO_PAID, снятие is_trial), запись
сохраняет ``remnawave_uuid`` → вызывающие пути обновляют панельного юзера
вместо создания нового. Под локом кандидат перечитывается: конкурентная
покупка могла уже конвертировать его — тогда откат к обычной вставке, чтобы
вторая покупка не затёрла тариф первой.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from app.database.crud import subscription as sub_crud
from app.database.models import SubscriptionStatus


def _sub(**kw) -> MagicMock:
    s = MagicMock()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _db() -> AsyncMock:
    db = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.flush = AsyncMock()
    db.add = MagicMock()
    return db


# --- create_paid_subscription: ветка конверсии ---


async def test_create_paid_subscription_converts_alive_trial_of_other_tariff(monkeypatch):
    """Живой триал ДРУГОГО тарифа при платной покупке конвертируется на месте."""
    monkeypatch.setattr(type(sub_crud.settings), 'is_multi_tariff_enabled', lambda self: True)
    trial = _sub(id=11, user_id=7, tariff_id=6, is_trial=True, status=SubscriptionStatus.ACTIVE.value)
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_and_tariff', AsyncMock(return_value=None))
    monkeypatch.setattr(sub_crud, 'get_alive_trial_subscription', AsyncMock(return_value=trial))
    convert = AsyncMock(return_value=trial)
    monkeypatch.setattr(sub_crud, '_convert_trial_subscription_to_paid', convert)
    db = _db()

    result = await sub_crud.create_paid_subscription(db, user_id=7, duration_days=30, traffic_limit_gb=100, tariff_id=1)

    assert result is trial
    convert.assert_awaited_once()
    assert convert.await_args.kwargs.get('tariff_id') == 1
    db.add.assert_not_called()  # новую подписку (и нового панельного юзера) НЕ создаём


async def test_create_paid_subscription_prefers_same_tariff_alive_trial(monkeypatch):
    """Живой триал ТОГО ЖЕ тарифа берётся из lookup'а напрямую — без второго
    запроса, чтобы конверсия не столкнулась с uq_subscriptions_user_tariff_active."""
    monkeypatch.setattr(type(sub_crud.settings), 'is_multi_tariff_enabled', lambda self: True)
    trial = _sub(id=11, user_id=7, tariff_id=1, is_trial=True, status=SubscriptionStatus.TRIAL.value)
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_and_tariff', AsyncMock(return_value=trial))
    helper = AsyncMock(return_value=None)
    monkeypatch.setattr(sub_crud, 'get_alive_trial_subscription', helper)
    convert = AsyncMock(return_value=trial)
    monkeypatch.setattr(sub_crud, '_convert_trial_subscription_to_paid', convert)
    db = _db()

    result = await sub_crud.create_paid_subscription(db, user_id=7, duration_days=30, traffic_limit_gb=100, tariff_id=1)

    assert result is trial
    helper.assert_not_awaited()  # кандидат уже найден lookup'ом
    convert.assert_awaited_once()
    db.add.assert_not_called()


async def test_create_paid_subscription_uses_passed_conversion_trial(monkeypatch):
    """Кабинет передаёт пре-резолвленного кандидата — повторный lookup не нужен."""
    monkeypatch.setattr(type(sub_crud.settings), 'is_multi_tariff_enabled', lambda self: True)
    trial = _sub(id=11, user_id=7, tariff_id=6, is_trial=True, status=SubscriptionStatus.ACTIVE.value)
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_and_tariff', AsyncMock(return_value=None))
    helper = AsyncMock(return_value=None)
    monkeypatch.setattr(sub_crud, 'get_alive_trial_subscription', helper)
    convert = AsyncMock(return_value=trial)
    monkeypatch.setattr(sub_crud, '_convert_trial_subscription_to_paid', convert)
    db = _db()

    result = await sub_crud.create_paid_subscription(
        db, user_id=7, duration_days=30, traffic_limit_gb=100, tariff_id=1, conversion_trial=trial
    )

    assert result is trial
    helper.assert_not_awaited()
    convert.assert_awaited_once()


async def test_create_paid_subscription_falls_to_insert_when_conversion_raced(monkeypatch):
    """Конкурентная покупка успела конвертировать кандидата (конверсия вернула
    None) — падаем в обычную вставку, а не затираем чужой тариф."""
    monkeypatch.setattr(type(sub_crud.settings), 'is_multi_tariff_enabled', lambda self: True)
    trial = _sub(id=11, user_id=7, tariff_id=6, is_trial=True, status=SubscriptionStatus.ACTIVE.value)
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_and_tariff', AsyncMock(return_value=None))
    monkeypatch.setattr(sub_crud, 'get_alive_trial_subscription', AsyncMock(return_value=trial))
    monkeypatch.setattr(sub_crud, '_convert_trial_subscription_to_paid', AsyncMock(return_value=None))
    monkeypatch.setattr(sub_crud, 'generate_unique_short_id', AsyncMock(side_effect=RuntimeError('reached create')))
    db = _db()

    try:
        await sub_crud.create_paid_subscription(db, user_id=7, duration_days=30, traffic_limit_gb=100, tariff_id=1)
    except RuntimeError as e:
        assert str(e) == 'reached create'  # дошли до вставки


async def test_create_paid_subscription_without_trial_falls_to_insert(monkeypatch):
    """Нет живого триала — обычная вставка новой подписки, как раньше."""
    monkeypatch.setattr(type(sub_crud.settings), 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_and_tariff', AsyncMock(return_value=None))
    monkeypatch.setattr(sub_crud, 'get_alive_trial_subscription', AsyncMock(return_value=None))
    convert = AsyncMock()
    monkeypatch.setattr(sub_crud, '_convert_trial_subscription_to_paid', convert)
    # short-circuit тяжёлый путь создания сразу после guard'а
    monkeypatch.setattr(sub_crud, 'generate_unique_short_id', AsyncMock(side_effect=RuntimeError('reached create')))
    db = _db()

    try:
        await sub_crud.create_paid_subscription(db, user_id=7, duration_days=30, traffic_limit_gb=100, tariff_id=1)
    except RuntimeError as e:
        assert str(e) == 'reached create'

    convert.assert_not_awaited()


async def test_expired_same_tariff_revive_wins_over_conversion(monkeypatch):
    """Истёкшая запись ПОКУПАЕМОГО тарифа реанимируется (#3004) — конверсия
    триала не запускается: у той записи уже есть свой панельный юзер/ссылка."""
    monkeypatch.setattr(type(sub_crud.settings), 'is_multi_tariff_enabled', lambda self: True)
    expired = _sub(id=5, user_id=7, tariff_id=1, is_trial=False, status=SubscriptionStatus.EXPIRED.value)
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_and_tariff', AsyncMock(return_value=expired))
    revive = AsyncMock(return_value=expired)
    monkeypatch.setattr(sub_crud, '_revive_paid_subscription', revive)
    helper = AsyncMock()
    monkeypatch.setattr(sub_crud, 'get_alive_trial_subscription', helper)
    convert = AsyncMock()
    monkeypatch.setattr(sub_crud, '_convert_trial_subscription_to_paid', convert)
    db = _db()

    result = await sub_crud.create_paid_subscription(db, user_id=7, duration_days=30, traffic_limit_gb=100, tariff_id=1)

    assert result is expired
    revive.assert_awaited_once()
    helper.assert_not_awaited()
    convert.assert_not_awaited()


async def test_trial_creation_never_triggers_conversion(monkeypatch):
    """Создание САМОГО триала (is_trial=True) не трогает ветку конверсии."""
    monkeypatch.setattr(type(sub_crud.settings), 'is_multi_tariff_enabled', lambda self: True)
    lookup = AsyncMock()
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_and_tariff', lookup)
    helper = AsyncMock()
    monkeypatch.setattr(sub_crud, 'get_alive_trial_subscription', helper)
    monkeypatch.setattr(sub_crud, 'generate_unique_short_id', AsyncMock(side_effect=RuntimeError('reached create')))
    db = _db()

    try:
        await sub_crud.create_paid_subscription(
            db, user_id=7, duration_days=3, traffic_limit_gb=10, tariff_id=6, is_trial=True
        )
    except RuntimeError as e:
        assert str(e) == 'reached create'

    lookup.assert_not_awaited()
    helper.assert_not_awaited()


# --- _convert_trial_subscription_to_paid: делегирование и защита от гонки ---


async def test_convert_helper_delegates_to_extend(monkeypatch):
    """Обёртка конверсии ревалидирует кандидата под локом и делегирует
    extend_subscription (смена тарифа, перенос остатка, снятие is_trial,
    добивание других триалов — всё там). Счётчики серверов НЕ бампает:
    строка уже посчитана при создании триала."""
    trial = _sub(
        id=11,
        user_id=7,
        tariff_id=6,
        is_trial=True,
        status=SubscriptionStatus.ACTIVE.value,
        connected_squads=['paid-squad'],
        end_date=datetime.now(UTC) + timedelta(days=2),
    )
    monkeypatch.setattr(sub_crud, '_lock_subscription_row', AsyncMock())
    extend = AsyncMock(return_value=trial)
    monkeypatch.setattr(sub_crud, 'extend_subscription', extend)
    db = _db()

    result = await sub_crud._convert_trial_subscription_to_paid(
        db,
        trial,
        tariff_id=1,
        duration_days=30,
        traffic_limit_gb=100,
        device_limit=3,
        connected_squads=['paid-squad'],
        commit=True,
    )

    assert result is trial
    extend.assert_awaited_once()
    kwargs = extend.await_args.kwargs
    assert kwargs['days'] == 30
    assert kwargs['tariff_id'] == 1
    assert kwargs['traffic_limit_gb'] == 100
    assert kwargs['device_limit'] == 3
    assert kwargs['connected_squads'] == ['paid-squad']
    assert kwargs['commit'] is True
    db.add.assert_not_called()


async def test_convert_helper_bails_out_when_candidate_no_longer_trial(monkeypatch):
    """Гонка: под локом кандидат уже не живой триал (конкурентная покупка
    конвертировала его) — возвращаем None, extend НЕ вызывается, тариф
    первой покупки не затирается."""
    converted_by_other = _sub(
        id=11,
        user_id=7,
        tariff_id=2,  # уже чужой купленный тариф
        is_trial=False,
        status=SubscriptionStatus.ACTIVE.value,
        connected_squads=['sq'],
    )
    monkeypatch.setattr(sub_crud, '_lock_subscription_row', AsyncMock())
    extend = AsyncMock()
    monkeypatch.setattr(sub_crud, 'extend_subscription', extend)
    db = _db()

    result = await sub_crud._convert_trial_subscription_to_paid(
        db,
        converted_by_other,
        tariff_id=1,
        duration_days=30,
        traffic_limit_gb=100,
        device_limit=3,
        connected_squads=['sq'],
        commit=True,
    )

    assert result is None
    extend.assert_not_awaited()


# --- resolve_trial_conversion_candidate: единые приоритеты для кабинета ---


async def test_resolver_returns_none_when_revive_will_preempt(monkeypatch):
    """EXPIRED подписка покупаемого тарифа → create уйдёт в revive (#3004),
    кандидата нет: кабинет глушит триал по-старому (перенос остатка +
    отключение панельного юзера)."""
    expired = _sub(id=5, user_id=7, tariff_id=1, is_trial=False, status=SubscriptionStatus.EXPIRED.value)
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_and_tariff', AsyncMock(return_value=expired))
    helper = AsyncMock()
    monkeypatch.setattr(sub_crud, 'get_alive_trial_subscription', helper)

    result = await sub_crud.resolve_trial_conversion_candidate(_db(), 7, 1)

    assert result is None
    helper.assert_not_awaited()


async def test_resolver_prefers_same_tariff_alive_trial(monkeypatch):
    trial = _sub(id=11, user_id=7, tariff_id=1, is_trial=True, status=SubscriptionStatus.TRIAL.value)
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_and_tariff', AsyncMock(return_value=trial))
    helper = AsyncMock(return_value=None)
    monkeypatch.setattr(sub_crud, 'get_alive_trial_subscription', helper)

    result = await sub_crud.resolve_trial_conversion_candidate(_db(), 7, 1)

    assert result is trial
    helper.assert_not_awaited()


async def test_resolver_falls_back_to_freshest_alive_trial(monkeypatch):
    other_trial = _sub(id=12, user_id=7, tariff_id=6, is_trial=True, status=SubscriptionStatus.ACTIVE.value)
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_and_tariff', AsyncMock(return_value=None))
    monkeypatch.setattr(sub_crud, 'get_alive_trial_subscription', AsyncMock(return_value=other_trial))

    result = await sub_crud.resolve_trial_conversion_candidate(_db(), 7, 1)

    assert result is other_trial


# --- Source-pin кабинетного пути ---


def test_cabinet_purchase_excludes_conversion_candidate_from_trial_kill():
    """Source-pin (в духе test_purchase_tariff_expired_trial_reuse): кабинетный
    ``purchase_tariff`` обязан резолвить кандидата через
    resolve_trial_conversion_candidate, исключать его из раннего
    deactivate_user_trial_subscriptions и передавать в create_paid_subscription
    — иначе триал будет заглушен ДО create_paid_subscription и конверсия не
    увидит его (регресс к «новый панельный юзер + мёртвый триал в кабинете»)."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / 'app' / 'cabinet' / 'routes' / 'subscription_modules' / 'purchase.py'
    ).read_text(encoding='utf-8')

    assert 'resolve_trial_conversion_candidate' in source, (
        'cabinet purchase.py больше не резолвит кандидата конверсии триала — '
        'ранний deactivate_user_trial_subscriptions заглушит живой триал до '
        'create_paid_subscription, и конверсия на месте не сработает'
    )
    assert 'conversion_trial=_conversion_trial' in source, (
        'пре-резолвленный кандидат должен передаваться в create_paid_subscription, '
        'чтобы кабинет и CRUD гарантированно работали с ОДНОЙ и той же строкой триала'
    )
