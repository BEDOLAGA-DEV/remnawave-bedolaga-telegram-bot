"""Копии тарифа: несколько живых подписок одного тарифа у одного человека.

Обычная покупка по-прежнему держит одну живую подписку на тариф (частичный
индекс плюс та же проверка в коде рядом с живой копией). Копия — явная покупка
со своим ``copy_key``: реанимирует истёкшую подписку тарифа или вставляет новую.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

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
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
    db.add = MagicMock()
    return db


@pytest.fixture
def multi_tariff(monkeypatch):
    monkeypatch.setattr(type(sub_crud.settings), 'is_multi_tariff_enabled', lambda self: True)


@pytest.fixture(autouse=True)
def _quiet_side_paths(monkeypatch):
    monkeypatch.setattr(sub_crud, 'deactivate_user_trial_subscriptions', AsyncMock(return_value=[]))
    monkeypatch.setattr(sub_crud, '_resolve_connected_squads', AsyncMock(return_value=['sq']))
    monkeypatch.setattr(sub_crud, 'generate_unique_short_id', AsyncMock(return_value='abc123'))


def test_copy_keys_are_unique_per_purchase():
    keys = {sub_crud.new_tariff_copy_key() for _ in range(100)}
    assert len(keys) == 100
    assert all(len(key) == 32 for key in keys)


def test_refusal_keeps_the_old_integrity_error_contract():
    """Вызывающие ловят IntegrityError по имени индекса — отказ в коде тот же класс."""
    error = sub_crud.TariffAlreadyActiveError(7, 3)
    assert isinstance(error, IntegrityError)
    assert sub_crud._is_trial_unique_violation(error)


async def test_ordinary_purchase_next_to_a_live_copy_is_refused(monkeypatch, multi_tariff):
    """Рядом с живой копией индекс обычную вставку не задел бы — отказ даёт код."""
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_and_tariff', AsyncMock(return_value=None))
    monkeypatch.setattr(sub_crud, '_trial_conversion_candidate', AsyncMock(return_value=None))
    monkeypatch.setattr(sub_crud, '_has_live_subscription_of_tariff', AsyncMock(return_value=True))
    db = _db()

    with pytest.raises(sub_crud.TariffAlreadyActiveError):
        await sub_crud.create_paid_subscription(db, user_id=7, duration_days=30, tariff_id=3)

    db.add.assert_not_called()


async def test_copy_purchase_inserts_a_new_row_with_its_key(monkeypatch, multi_tariff):
    monkeypatch.setattr(sub_crud, '_latest_expired_subscription_of_tariff', AsyncMock(return_value=None))
    monkeypatch.setattr(sub_crud, '_trial_conversion_candidate', AsyncMock(return_value=None))
    live_check = AsyncMock(return_value=True)
    monkeypatch.setattr(sub_crud, '_has_live_subscription_of_tariff', live_check)
    ordinary_lookup = AsyncMock()
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_and_tariff', ordinary_lookup)
    db = _db()

    subscription = await sub_crud.create_paid_subscription(
        db, user_id=7, duration_days=30, tariff_id=3, copy_key='k' * 32
    )

    db.add.assert_called_once()
    assert subscription.copy_key == 'k' * 32
    assert subscription.tariff_id == 3
    live_check.assert_not_awaited()  # у копии отказа «уже активен» нет
    ordinary_lookup.assert_not_awaited()  # живые подписки тарифа копия не продлевает


async def test_copy_purchase_revives_an_expired_copy_and_stamps_the_key(monkeypatch, multi_tariff):
    expired = _sub(id=9, user_id=7, tariff_id=3, is_trial=False, status=SubscriptionStatus.EXPIRED.value, copy_key=None)
    monkeypatch.setattr(sub_crud, '_latest_expired_subscription_of_tariff', AsyncMock(return_value=expired))
    revive = AsyncMock(return_value=expired)
    monkeypatch.setattr(sub_crud, '_revive_paid_subscription', revive)
    db = _db()

    result = await sub_crud.create_paid_subscription(db, user_id=7, duration_days=30, tariff_id=3, copy_key='new-key')

    assert result is expired
    assert expired.copy_key == 'new-key'
    revive.assert_awaited_once()
    db.add.assert_not_called()


async def test_copy_purchase_converts_a_trial_into_the_copy(monkeypatch, multi_tariff):
    trial = _sub(id=4, user_id=7, tariff_id=8, is_trial=True, status=SubscriptionStatus.TRIAL.value)
    monkeypatch.setattr(sub_crud, '_latest_expired_subscription_of_tariff', AsyncMock(return_value=None))
    monkeypatch.setattr(sub_crud, '_trial_conversion_candidate', AsyncMock(return_value=trial))
    convert = AsyncMock(return_value=trial)
    monkeypatch.setattr(sub_crud, '_convert_trial_subscription_to_paid', convert)
    db = _db()

    result = await sub_crud.create_paid_subscription(db, user_id=7, duration_days=30, tariff_id=3, copy_key='kk')

    assert result is trial
    assert convert.await_args.kwargs['copy_key'] == 'kk'


async def test_switch_into_an_owned_tariff_marks_the_row_as_a_copy(monkeypatch, multi_tariff):
    monkeypatch.setattr(sub_crud, '_has_live_subscription_of_tariff', AsyncMock(return_value=True))
    row = _sub(id=5, user_id=7, tariff_id=2, copy_key=None)

    assert await sub_crud.ensure_tariff_copy_key(_db(), row, 3) is True
    assert isinstance(row.copy_key, str) and len(row.copy_key) == 32


async def test_switch_into_a_free_tariff_stays_ordinary(monkeypatch, multi_tariff):
    monkeypatch.setattr(sub_crud, '_has_live_subscription_of_tariff', AsyncMock(return_value=False))
    row = _sub(id=5, user_id=7, tariff_id=2, copy_key=None)

    assert await sub_crud.ensure_tariff_copy_key(_db(), row, 3) is False
    assert row.copy_key is None


async def test_single_tariff_mode_never_makes_copies(monkeypatch):
    monkeypatch.setattr(type(sub_crud.settings), 'is_multi_tariff_enabled', lambda self: False)
    check = AsyncMock(return_value=True)
    monkeypatch.setattr(sub_crud, '_has_live_subscription_of_tariff', check)
    row = _sub(id=5, user_id=7, tariff_id=2, copy_key=None)

    assert await sub_crud.ensure_tariff_copy_key(_db(), row, 3) is False
    assert row.copy_key is None
    check.assert_not_awaited()


async def test_merge_lookup_uses_the_new_owner(monkeypatch, multi_tariff):
    """При переносе на другой аккаунт ищем соседей у нового владельца."""
    check = AsyncMock(return_value=True)
    monkeypatch.setattr(sub_crud, '_has_live_subscription_of_tariff', check)
    row = _sub(id=5, user_id=7, tariff_id=3, copy_key=None)

    assert await sub_crud.ensure_tariff_copy_key(_db(), row, 3, user_id=99) is True
    assert check.await_args.args[1] == 99
    assert check.await_args.kwargs['exclude_subscription_id'] == 5
    assert check.await_args.kwargs['ordinary_only'] is True


async def test_rewards_pick_the_copy_with_the_latest_end_date():
    """Награды без выбора — копия с самой поздней датой окончания (порядок запроса)."""
    db = _db()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

    await sub_crud.get_subscription_by_user_and_tariff(db, 7, 3)

    statement = str(db.execute.await_args.args[0])
    assert 'ORDER BY subscriptions.end_date DESC' in statement
