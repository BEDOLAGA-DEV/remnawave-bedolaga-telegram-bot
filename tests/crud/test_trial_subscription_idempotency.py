"""
Регрессия: create_trial_subscription должна быть идемпотентной.

Проблема: при двойном клике или гонке параллельных запросов оба потока проходили
проверку is_trial_already_used() и оба доходили до INSERT. Второй INSERT нарушал
частичный уникальный индекс ``uq_subscriptions_user_tariff_active`` (user_id,
tariff_id WHERE status IN ('active','trial','limited')) и падал с IntegrityError.

Фикс — два слоя защиты в create_trial_subscription:

1. Идемпотентность: если при проверке existing живая подписка уже найдена
   (active/trial/limited, не PENDING), возвращаем её без INSERT.
2. Защита от гонки (TOCTOU): если между проверкой и commit конкурентный запрос
   успел вставить запись — перехватываем IntegrityError, делаем rollback и
   возвращаем подписку, созданную параллельным потоком.

GitHub issue: https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/issues/2995
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.database.crud import subscription as sub_crud
from app.database.models import SubscriptionStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _integrity_error() -> IntegrityError:
    """IntegrityError с orig.constraint_name — имитирует asyncpg UniqueViolationError.

    Использует sub_crud.UQ_TRIAL_CONSTRAINT как единственный источник истины,
    чтобы не дублировать строку и не расходиться при переименовании.
    """
    orig = MagicMock()
    orig.constraint_name = sub_crud.UQ_TRIAL_CONSTRAINT
    return IntegrityError('duplicate key', {}, orig)


def _sub(**kw) -> MagicMock:
    """Создаёт мок-подписку с заданными атрибутами."""
    s = MagicMock()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _db() -> AsyncMock:
    """Мок AsyncSession с нужными методами."""
    db = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    db.flush = AsyncMock()
    db.add = MagicMock()
    db.expunge = MagicMock()  # синхронный метод SQLAlchemy
    return db


# ---------------------------------------------------------------------------
# 1. Идемпотентность: возврат активной подписки без повторного INSERT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_existing_active_subscription_without_insert(monkeypatch):
    """Если у пользователя уже есть живая (active) подписка на тариф — возвращаем
    её, db.add и db.commit не вызываем."""
    monkeypatch.setattr(type(sub_crud.settings), 'is_multi_tariff_enabled', lambda self: True)

    existing = _sub(
        id=10,
        user_id=177,
        tariff_id=1,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=True,
    )
    monkeypatch.setattr(
        sub_crud,
        'get_active_subscriptions_by_user_id',
        AsyncMock(return_value=[existing]),
    )
    monkeypatch.setattr(sub_crud, 'generate_unique_short_id', AsyncMock(return_value='abc123'))

    db = _db()
    result = await sub_crud.create_trial_subscription(
        db,
        user_id=177,
        connected_squads=['squad-1'],
        tariff_id=1,
    )

    assert result is existing
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_subscription_does_not_block_new_trial_single_tariff(monkeypatch):
    """В single-tariff режиме EXPIRED-подписка не блокирует создание нового триала
    (регрессия: if existing: без проверки статуса возвращал устаревшую запись)."""
    monkeypatch.setattr(type(sub_crud.settings), 'is_multi_tariff_enabled', lambda self: False)

    expired = _sub(
        id=99,
        user_id=5,
        tariff_id=None,
        status=SubscriptionStatus.EXPIRED.value,
        is_trial=True,
    )
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_id', AsyncMock(return_value=expired))
    monkeypatch.setattr(sub_crud, 'generate_unique_short_id', AsyncMock(return_value='exp-new'))

    db = _db()
    result = await sub_crud.create_trial_subscription(
        db,
        user_id=5,
        connected_squads=['squad-x'],
    )

    # Новая подписка создана, EXPIRED не вернулась
    db.add.assert_called_once()
    db.commit.assert_awaited_once()
    added_obj = db.add.call_args[0][0]
    assert result is added_obj


@pytest.mark.asyncio
async def test_returns_existing_trial_subscription_without_insert(monkeypatch):
    """Статус trial тоже считается живым — возвращаем без INSERT."""
    monkeypatch.setattr(type(sub_crud.settings), 'is_multi_tariff_enabled', lambda self: True)

    existing = _sub(
        id=11,
        user_id=177,
        tariff_id=1,
        status=SubscriptionStatus.TRIAL.value,
        is_trial=True,
    )
    monkeypatch.setattr(
        sub_crud,
        'get_active_subscriptions_by_user_id',
        AsyncMock(return_value=[existing]),
    )
    monkeypatch.setattr(sub_crud, 'generate_unique_short_id', AsyncMock(return_value='xyz789'))

    db = _db()
    result = await sub_crud.create_trial_subscription(
        db,
        user_id=177,
        connected_squads=['squad-1'],
        tariff_id=1,
    )

    assert result is existing
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_returns_existing_limited_subscription_without_insert(monkeypatch):
    """Статус limited тоже живой (трафик кончился, время ещё есть) — возвращаем без INSERT."""
    monkeypatch.setattr(type(sub_crud.settings), 'is_multi_tariff_enabled', lambda self: True)

    existing = _sub(
        id=12,
        user_id=177,
        tariff_id=1,
        status=SubscriptionStatus.LIMITED.value,
        is_trial=True,
    )
    monkeypatch.setattr(
        sub_crud,
        'get_active_subscriptions_by_user_id',
        AsyncMock(return_value=[existing]),
    )
    monkeypatch.setattr(sub_crud, 'generate_unique_short_id', AsyncMock(return_value='lim001'))

    db = _db()
    result = await sub_crud.create_trial_subscription(
        db,
        user_id=177,
        connected_squads=['squad-1'],
        tariff_id=1,
    )

    assert result is existing
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# 2. PENDING-триал: переиспользуется (старый путь не сломан)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_trial_is_activated_not_duplicated(monkeypatch):
    """Существующая PENDING-триальная подписка должна быть переведена в active,
    а не создана дублем — старый путь должен работать."""
    monkeypatch.setattr(type(sub_crud.settings), 'is_multi_tariff_enabled', lambda self: True)

    pending = _sub(
        id=20,
        user_id=177,
        tariff_id=1,
        status=SubscriptionStatus.PENDING.value,
        is_trial=True,
        remnawave_short_id='old-short-id',
    )
    monkeypatch.setattr(
        sub_crud,
        'get_active_subscriptions_by_user_id',
        AsyncMock(return_value=[pending]),
    )

    db = _db()
    result = await sub_crud.create_trial_subscription(
        db,
        user_id=177,
        connected_squads=['squad-1'],
        tariff_id=1,
    )

    assert result is pending
    assert pending.status == SubscriptionStatus.ACTIVE.value
    db.commit.assert_awaited_once()
    db.add.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Защита от гонки: перехват IntegrityError в multi-tariff-режиме
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integrity_error_on_commit_returns_concurrent_subscription_multitariff(monkeypatch):
    """Если commit падает с IntegrityError (гонка), делаем rollback, expunge объекта
    из сессии и возвращаем подписку, созданную конкурентным запросом (multi-tariff)."""
    monkeypatch.setattr(type(sub_crud.settings), 'is_multi_tariff_enabled', lambda self: True)

    # При первичной проверке подписки нет — оба потока прошли проверку
    monkeypatch.setattr(
        sub_crud,
        'get_active_subscriptions_by_user_id',
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(sub_crud, 'generate_unique_short_id', AsyncMock(return_value='race-id'))

    concurrent = _sub(id=30, user_id=177, tariff_id=1, status=SubscriptionStatus.ACTIVE.value, is_trial=True)
    monkeypatch.setattr(
        sub_crud,
        'get_subscription_by_user_and_tariff',
        AsyncMock(return_value=concurrent),
    )

    db = _db()
    # commit бросает IntegrityError при дублирующей вставке
    db.commit.side_effect = _integrity_error()

    result = await sub_crud.create_trial_subscription(
        db,
        user_id=177,
        connected_squads=['squad-1'],
        tariff_id=1,
    )

    assert result is concurrent
    db.rollback.assert_awaited_once()
    # Объект удалён из сессии, чтобы не спровоцировать повторный flush
    db.expunge.assert_called_once()
    # После rollback ищем подписку конкурентного запроса именно по user+tariff
    sub_crud.get_subscription_by_user_and_tariff.assert_awaited_once_with(db, 177, 1)


@pytest.mark.asyncio
async def test_integrity_error_on_commit_reraises_if_no_concurrent_sub(monkeypatch):
    """Если commit падает с IntegrityError, но найти конкурентную подписку не удалось
    (нештатная ситуация) — IntegrityError пробрасывается дальше."""
    monkeypatch.setattr(type(sub_crud.settings), 'is_multi_tariff_enabled', lambda self: True)

    monkeypatch.setattr(
        sub_crud,
        'get_active_subscriptions_by_user_id',
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(sub_crud, 'generate_unique_short_id', AsyncMock(return_value='err-id'))
    monkeypatch.setattr(
        sub_crud,
        'get_subscription_by_user_and_tariff',
        AsyncMock(return_value=None),
    )

    db = _db()
    db.commit.side_effect = _integrity_error()

    with pytest.raises(IntegrityError):
        await sub_crud.create_trial_subscription(
            db,
            user_id=177,
            connected_squads=['squad-1'],
            tariff_id=1,
        )

    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_integrity_error_from_unrelated_constraint_is_reraised_immediately(monkeypatch):
    """IntegrityError по постороннему constraint (не uq_subscriptions_user_tariff_active)
    должна пробрасываться немедленно — rollback выполняется, но поиск concurrent
    не запускается и подписка не возвращается."""
    monkeypatch.setattr(type(sub_crud.settings), 'is_multi_tariff_enabled', lambda self: True)

    monkeypatch.setattr(
        sub_crud,
        'get_active_subscriptions_by_user_id',
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(sub_crud, 'generate_unique_short_id', AsyncMock(return_value='other-err'))

    lookup = AsyncMock()
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_and_tariff', lookup)

    # orig.constraint_name содержит ДРУГОЕ имя — не наш constraint
    orig = MagicMock()
    orig.constraint_name = 'some_other_constraint'
    unrelated_error = IntegrityError('other violation', {}, orig)

    db = _db()
    db.commit.side_effect = unrelated_error

    with pytest.raises(IntegrityError):
        await sub_crud.create_trial_subscription(
            db,
            user_id=177,
            connected_squads=['squad-1'],
            tariff_id=1,
        )

    # Чужая IntegrityError пробрасывается, rollback выполняется (сессия очищается),
    # но поиск конкурентной подписки не запускался
    db.rollback.assert_awaited_once()
    lookup.assert_not_awaited()


# ---------------------------------------------------------------------------
# 4. Защита от гонки: non-multi-tariff-режим
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integrity_error_on_commit_returns_concurrent_subscription_single_tariff(monkeypatch):
    """Та же защита от гонки в режиме без multi-tariff: используем
    get_subscription_by_user_id вместо get_subscription_by_user_and_tariff."""
    monkeypatch.setattr(type(sub_crud.settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(sub_crud, 'generate_unique_short_id', AsyncMock(return_value='single-id'))

    concurrent = _sub(id=40, user_id=5, tariff_id=None, status=SubscriptionStatus.ACTIVE.value, is_trial=True)

    # Первый вызов (при проверке existing) → None, второй (после rollback) → concurrent
    get_sub_mock = AsyncMock(side_effect=[None, concurrent])
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_id', get_sub_mock)

    db = _db()
    db.commit.side_effect = _integrity_error()

    result = await sub_crud.create_trial_subscription(
        db,
        user_id=5,
        connected_squads=['squad-x'],
        tariff_id=None,
    )

    assert result is concurrent
    db.rollback.assert_awaited_once()


# ---------------------------------------------------------------------------
# 5. Счастливый путь: создание новой подписки (нет existing, нет гонки)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_creates_new_subscription_when_no_existing(monkeypatch):
    """Когда у пользователя нет подписки — создаётся новая, db.add и db.commit вызываются."""
    monkeypatch.setattr(type(sub_crud.settings), 'is_multi_tariff_enabled', lambda self: True)

    monkeypatch.setattr(
        sub_crud,
        'get_active_subscriptions_by_user_id',
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(sub_crud, 'generate_unique_short_id', AsyncMock(return_value='new-short'))

    db = _db()
    result = await sub_crud.create_trial_subscription(
        db,
        user_id=99,
        connected_squads=['squad-new'],
        tariff_id=2,
    )

    db.add.assert_called_once()
    db.commit.assert_awaited_once()
    db.refresh.assert_awaited_once()
    # Возвращается объект, который передали в db.add
    added_obj = db.add.call_args[0][0]
    assert result is added_obj


# ---------------------------------------------------------------------------
# 6. Подписка другого тарифа не считается конфликтом
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_different_tariff_subscription_does_not_block_trial_creation(monkeypatch):
    """Живая подписка на тариф 2 не мешает создать триал на тариф 1."""
    monkeypatch.setattr(type(sub_crud.settings), 'is_multi_tariff_enabled', lambda self: True)

    other_tariff_sub = _sub(
        id=50,
        user_id=177,
        tariff_id=2,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
    )
    monkeypatch.setattr(
        sub_crud,
        'get_active_subscriptions_by_user_id',
        AsyncMock(return_value=[other_tariff_sub]),
    )
    monkeypatch.setattr(sub_crud, 'generate_unique_short_id', AsyncMock(return_value='diff-tariff'))

    db = _db()
    result = await sub_crud.create_trial_subscription(
        db,
        user_id=177,
        connected_squads=['squad-1'],
        tariff_id=1,  # другой тариф
    )

    # Создалась новая подписка, а не вернулась existing
    db.add.assert_called_once()
    db.commit.assert_awaited_once()
    added_obj = db.add.call_args[0][0]
    assert result is added_obj
