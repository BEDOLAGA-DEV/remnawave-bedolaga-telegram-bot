"""`wipe_trial_subscriptions`: удаление панельного аккаунта при сбросе триала.

Самая разрушительная операция во всей миграции — она вызывает `delete_user`
в панели и потом сносит строку в БД. После 0104 у каждой доапгрейдной строки
`remnawave_id IS NULL`, поэтому ветка «нет id» перестала однозначно означать
«в панели удалять нечего»: у строки может быть живой аккаунт, найденный по
`remnawave_short_uuid`. Ошибка здесь либо оставляет ACTIVE-аккаунт-сироту
(занимает лицензию, связи с ботом нет), либо удаляет ЧУЖОЙ аккаунт.

Мутационная проверка показала, что раньше все эти сценарии проходили при
зелёном прогоне, поэтому они пиннятся отдельно.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.services.grace_access_runtime as grace_runtime_mod
import app.services.subscription_service as subscription_service_mod
from app.config import Settings, settings
from app.database.crud.subscription import wipe_trial_subscriptions


@pytest.fixture
def api():
    client = AsyncMock()
    client.delete_user.return_value = True
    return client


@pytest.fixture
def patched_service(monkeypatch, api):
    """Подменяем SubscriptionService целиком: нужен только его API-клиент."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_client():
        yield api

    class _Service:
        is_configured = True
        configuration_error = None

        def get_api_client(self):
            return fake_client()

    # `wipe_trial_subscriptions` импортирует зависимости внутри функции, поэтому
    # подменять их надо в модулях-источниках.
    monkeypatch.setattr(subscription_service_mod, 'SubscriptionService', _Service)
    # Grace-guard ходит в реальный bind — здесь проверяется не он.
    monkeypatch.setattr(grace_runtime_mod, 'ensure_no_open_grace_for_subscriptions', AsyncMock(return_value=None))
    return api


def _sub(sub_id=42, *, panel_id=None, short_uuid=None, user_panel_id=None):
    return SimpleNamespace(
        id=sub_id,
        remnawave_id=panel_id,
        remnawave_short_uuid=short_uuid,
        remnawave_uuid=None,
        user=SimpleNamespace(id=1, remnawave_id=user_panel_id),
        user_id=1,
    )


@pytest.fixture
def db():
    session = AsyncMock()
    session.execute.return_value = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_adopts_by_short_uuid_and_deletes_the_right_account(monkeypatch, patched_service, db):
    """Ключевой сценарий: id ещё не пробэкфилен, но панель знает shortUuid.

    Мутация «panel_user_id = subscription.id» проходила зелёный прогон и
    удаляла НЕ ТОТ аккаунт, поэтому проверяем именно переданный id.
    """
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    patched_service.get_user_by_short_uuid.return_value = SimpleNamespace(id=9001)

    await wipe_trial_subscriptions(db, [_sub(42, short_uuid='abc')])

    patched_service.get_user_by_short_uuid.assert_awaited_once_with('abc')
    patched_service.delete_user.assert_awaited_once_with(9001)


@pytest.mark.asyncio
async def test_does_not_orphan_a_live_panel_account(monkeypatch, patched_service, db):
    """Пропустить панель и удалить строку — значит оставить ACTIVE-сироту."""
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    patched_service.get_user_by_short_uuid.return_value = SimpleNamespace(id=9001)

    await wipe_trial_subscriptions(db, [_sub(42, short_uuid='abc')])

    assert patched_service.delete_user.await_count == 1, 'панельный аккаунт обязан быть удалён'


@pytest.mark.asyncio
async def test_panel_error_during_lookup_does_not_wipe_the_row(monkeypatch, patched_service, db):
    """Таймаут — не доказательство. Строку оставляем следующему запуску."""
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    patched_service.get_user_by_short_uuid.side_effect = RuntimeError('panel down')

    wiped = await wipe_trial_subscriptions(db, [_sub(42, short_uuid='abc')])

    assert wiped == 0
    patched_service.delete_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_short_uuid_does_not_block_the_reset_forever(monkeypatch, patched_service, db):
    """Панель этот shortUuid забыла — удалять нечего, но и застревать нельзя.

    Прежний guard возвращал False без запроса к панели, и такая строка не
    сбрасывалась НИКОГДА: бэкфил её тоже не разрешает по построению.
    """
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    patched_service.get_user_by_short_uuid.return_value = None

    wiped = await wipe_trial_subscriptions(db, [_sub(42, short_uuid='gone')])

    assert wiped == 1
    patched_service.delete_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_row_that_never_had_a_panel_user_needs_no_lookup(monkeypatch, patched_service, db):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)

    wiped = await wipe_trial_subscriptions(db, [_sub(42)])

    assert wiped == 1
    patched_service.get_user_by_short_uuid.assert_not_awaited()
    patched_service.delete_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_numeric_id_is_used_directly(monkeypatch, patched_service, db):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)

    await wipe_trial_subscriptions(db, [_sub(42, panel_id=8812, short_uuid='abc')])

    patched_service.get_user_by_short_uuid.assert_not_awaited()
    patched_service.delete_user.assert_awaited_once_with(8812)


# ---------------------------------------------------------------------------
# REMNAWAVE_USER_DELETE_MODE. Отчёт из «Багов»: при сбросе триала аккаунт
# удалялся из панели, хотя в .env стоял disable. Режим читался только при
# полном удалении пользователя; сброс триала всегда звал delete_user.
# ---------------------------------------------------------------------------


def _executed_updates(db) -> list:
    from sqlalchemy.sql.dml import Update

    return [call.args[0] for call in db.execute.await_args_list if isinstance(call.args[0], Update)]


@pytest.mark.asyncio
async def test_disable_mode_deactivates_instead_of_deleting(monkeypatch, patched_service, db):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(settings, 'REMNAWAVE_USER_DELETE_MODE', 'disable')

    wiped = await wipe_trial_subscriptions(db, [_sub(42, panel_id=9001)])

    assert wiped == 1
    patched_service.disable_user.assert_awaited_once_with(9001)
    patched_service.delete_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_disable_mode_keeps_single_tariff_user_identity(monkeypatch, patched_service, db):
    """Аккаунт остаётся (отключённым) — users.remnawave_id обязан остаться с ним:
    следующий триал или покупка включат тот же аккаунт заново."""
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(settings, 'REMNAWAVE_USER_DELETE_MODE', 'disable')

    wiped = await wipe_trial_subscriptions(db, [_sub(42, user_panel_id=555)])

    assert wiped == 1
    patched_service.disable_user.assert_awaited_once_with(555)
    patched_service.delete_user.assert_not_awaited()
    assert _executed_updates(db) == []


@pytest.mark.asyncio
async def test_delete_mode_still_clears_single_tariff_user_identity(monkeypatch, patched_service, db):
    """Регресс-стража: в режиме delete аккаунта больше нет — ссылку на него стираем."""
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(settings, 'REMNAWAVE_USER_DELETE_MODE', 'delete')

    wiped = await wipe_trial_subscriptions(db, [_sub(42, user_panel_id=555)])

    assert wiped == 1
    patched_service.delete_user.assert_awaited_once_with(555)
    assert len(_executed_updates(db)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('panel_message', ['User not found', 'User already disabled'])
async def test_disable_mode_treats_gone_or_already_disabled_as_success(monkeypatch, patched_service, db, panel_message):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(settings, 'REMNAWAVE_USER_DELETE_MODE', 'disable')
    patched_service.disable_user.side_effect = RuntimeError(panel_message)

    wiped = await wipe_trial_subscriptions(db, [_sub(42, panel_id=9001)])

    assert wiped == 1


# ---------------------------------------------------------------------------
# Мультитариф: первый аккаунт записан и человеку (users.remnawave_id). Сброс
# триала удалял аккаунт в панели, но ссылку у человека стирал только в
# однотарифном режиме — следующая покупка наследовала мёртвый id
# (should_create_panel_account) и сыпала «User not found». Тот же класс, что
# «удалил подписку → купил заново» (8e37e780), только через сброс триала.
# ---------------------------------------------------------------------------


async def _seed_multi(db, *, user_panel_id: int, subs: list[tuple[int, bool]]):
    """subs: (remnawave_id, is_trial). Возвращает (user, [subscriptions])."""
    from datetime import UTC, datetime, timedelta

    from app.database.models import Subscription, SubscriptionStatus, User

    user = User(telegram_id=700, username='multi', language='ru', remnawave_id=user_panel_id)
    db.add(user)
    await db.flush()
    rows = []
    for index, (panel_id, is_trial) in enumerate(subs):
        row = Subscription(
            user_id=user.id,
            status=SubscriptionStatus.EXPIRED.value if is_trial else SubscriptionStatus.ACTIVE.value,
            is_trial=is_trial,
            end_date=datetime.now(UTC) + (timedelta(days=-1) if is_trial else timedelta(days=30)),
            remnawave_id=panel_id,
            remnawave_short_id=f'short{index}',
        )
        db.add(row)
        rows.append(row)
    await db.commit()
    return user, rows


async def _user_panel_id(db, user_id: int):
    from sqlalchemy import select

    from app.database.models import User

    return (await db.execute(select(User.remnawave_id).where(User.id == user_id))).scalar_one()


@pytest.mark.asyncio
async def test_multi_tariff_trial_reset_clears_dead_account_on_user(monkeypatch, patched_service):
    from app.database.models import Base
    from tests.fixtures.sqlite_memory import memory_session

    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(settings, 'REMNAWAVE_USER_DELETE_MODE', 'delete')
    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        user, (trial,) = await _seed_multi(db, user_panel_id=8812, subs=[(8812, True)])

        assert await wipe_trial_subscriptions(db, [trial]) == 1
        await db.commit()

        patched_service.delete_user.assert_awaited_once_with(8812)
        assert await _user_panel_id(db, user.id) is None


@pytest.mark.asyncio
async def test_multi_tariff_trial_reset_keeps_live_account_of_other_subscription(monkeypatch, patched_service):
    """У человека записан аккаунт живой платной подписки — сброс триала его не трогает."""
    from app.database.models import Base
    from tests.fixtures.sqlite_memory import memory_session

    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(settings, 'REMNAWAVE_USER_DELETE_MODE', 'delete')
    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        user, (paid, trial) = await _seed_multi(db, user_panel_id=5000, subs=[(5000, False), (8812, True)])

        assert await wipe_trial_subscriptions(db, [trial]) == 1
        await db.commit()

        patched_service.delete_user.assert_awaited_once_with(8812)
        assert await _user_panel_id(db, user.id) == 5000


@pytest.mark.asyncio
async def test_multi_tariff_trial_reset_in_disable_mode_keeps_link(monkeypatch, patched_service):
    """disable: аккаунт жив (отключён) — связь с ним стирать нельзя."""
    from app.database.models import Base
    from tests.fixtures.sqlite_memory import memory_session

    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(settings, 'REMNAWAVE_USER_DELETE_MODE', 'disable')
    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        user, (trial,) = await _seed_multi(db, user_panel_id=8812, subs=[(8812, True)])

        assert await wipe_trial_subscriptions(db, [trial]) == 1
        await db.commit()

        patched_service.disable_user.assert_awaited_once_with(8812)
        assert await _user_panel_id(db, user.id) == 8812
