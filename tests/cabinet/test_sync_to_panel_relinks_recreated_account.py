"""«Из бота в панель» после удаления учётки в панели (issue #3277).

Кнопка корректно обнаруживает, что записанный панельный id мёртв, и заводит
новую учётку — но связь оставалась старой, и следующее нажатие заводило ещё
одну. Так копились дубли панельных аккаунтов.

Опыт ставится на настоящей сессии БД: тесты writer'а этот сценарий покрывают
и проходят, значит расхождение появляется именно на пути админки.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.models import Base, Subscription, SubscriptionStatus, User
from app.external.remnawave_api import RemnaWaveAPIError
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.tables.values())

DEAD_PANEL_ID = 55
NEW_PANEL_ID = 60
OLD_SHORT_UUID = 'mMurPPpCdX1zrNpu'
NEW_SHORT_UUID = 'f-1DVgt_8SvdQb17'


def _panel_user(user_id: int, short_uuid: str):
    return SimpleNamespace(
        id=user_id,
        short_uuid=short_uuid,
        subscription_url=f'https://panel/{short_uuid}',
        happ_crypto_link='crypto',
        expire_at=datetime.now(UTC) + timedelta(days=30),
        username=f'user_{user_id}',
    )


def _api() -> AsyncMock:
    """Панель, в которой записанной учётки уже нет."""
    api = AsyncMock()
    not_found = RemnaWaveAPIError('User not found', 404, {'errorCode': 'A063'})
    # Клиент на 404 возвращает None, а не бросает — так и записано в журнале issue.
    api.get_user_by_id.return_value = None
    api.get_user_by_short_uuid.return_value = None
    api.get_user_by_telegram_id.return_value = None
    api.get_user_by_username.return_value = None
    api.get_user_by_email.return_value = None
    api.update_user.side_effect = not_found
    api.reset_user_devices.return_value = True
    api.create_user.return_value = _panel_user(NEW_PANEL_ID, NEW_SHORT_UUID)
    return api


async def _seed(db) -> tuple[User, Subscription]:
    user = User(
        id=53,
        telegram_id=777,
        first_name='Пользователь',
        language='ru',
        status='active',
        balance_kopeks=0,
        remnawave_id=DEAD_PANEL_ID,
    )
    subscription = Subscription(
        id=44,
        user_id=53,
        status=SubscriptionStatus.ACTIVE.value,
        start_date=datetime.now(UTC) - timedelta(days=1),
        end_date=datetime.now(UTC) + timedelta(days=30),
        traffic_limit_gb=100,
        device_limit=3,
        connected_squads=['squad'],
        remnawave_id=DEAD_PANEL_ID,
        remnawave_short_uuid=OLD_SHORT_UUID,
    )
    db.add_all([user, subscription])
    await db.commit()
    return user, subscription


@contextlib.asynccontextmanager
async def _client(api):
    yield api


async def _call_sync(monkeypatch, db, api):
    """Дёрнуть ручку так же, как это делает кабинет."""
    from app.cabinet.routes import admin_users as route
    from app.cabinet.schemas.users import SyncToPanelRequest
    from app.services import remnawave_service as service_module

    # Роут импортирует сервис внутри функции, поэтому подменяем его по месту
    # объявления, а не в модуле роута.
    monkeypatch.setattr(
        service_module,
        'RemnaWaveService',
        lambda *a, **kw: SimpleNamespace(
            is_configured=True,
            configuration_error=None,
            get_api_client=lambda: _client(api),
        ),
    )

    return await route.sync_user_to_panel(
        user_id=53,
        subscription_id=None,
        request=SyncToPanelRequest(),
        admin=SimpleNamespace(id=1, telegram_id=1),
        db=db,
    )


@pytest.mark.asyncio
async def test_recreated_account_is_written_to_both_tables(monkeypatch) -> None:
    api = _api()

    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        response = await _call_sync(monkeypatch, db, api)

        assert response.action == 'created'
        assert response.panel_user_id == NEW_PANEL_ID

        subscription = await db.get(Subscription, 44)
        user = await db.get(User, 53)

        assert subscription.remnawave_short_uuid == NEW_SHORT_UUID
        assert subscription.remnawave_id == NEW_PANEL_ID, (
            'в подписке остался id удалённой учётки — следующее нажатие заведёт ещё одну'
        )
        assert user.remnawave_id == NEW_PANEL_ID, 'у человека остался id удалённой учётки'


@pytest.mark.asyncio
async def test_account_found_by_short_uuid_also_replaces_the_dead_link(monkeypatch) -> None:
    """Тот же перекос возникал и без создания новой учётки.

    Записанный id мёртв, но аккаунт находится по shortUuid: бот пишет в него и
    рапортует успех, а в колонке остаётся мёртвый номер. Следующий проход снова
    начнёт со старого id.
    """
    api = _api()
    alive = _panel_user(NEW_PANEL_ID, OLD_SHORT_UUID)
    api.get_user_by_short_uuid.return_value = alive
    api.update_user.side_effect = None
    api.update_user.return_value = alive

    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        response = await _call_sync(monkeypatch, db, api)

        assert response.action == 'updated'
        api.create_user.assert_not_awaited()

        subscription = await db.get(Subscription, 44)
        user = await db.get(User, 53)

        assert subscription.remnawave_id == NEW_PANEL_ID
        assert user.remnawave_id == NEW_PANEL_ID


@pytest.mark.asyncio
async def test_live_recorded_id_is_left_alone(monkeypatch) -> None:
    """Живую связь не трогаем: затирать её было бы хуже исходной болезни."""
    api = _api()
    alive = _panel_user(DEAD_PANEL_ID, OLD_SHORT_UUID)
    api.get_user_by_id.return_value = alive
    api.update_user.side_effect = None
    api.update_user.return_value = alive

    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        response = await _call_sync(monkeypatch, db, api)

        assert response.action == 'updated'
        api.create_user.assert_not_awaited()

        subscription = await db.get(Subscription, 44)
        user = await db.get(User, 53)

        assert subscription.remnawave_id == DEAD_PANEL_ID
        assert user.remnawave_id == DEAD_PANEL_ID


async def _call_status(monkeypatch, db, api):
    from app.cabinet.routes import admin_users as route
    from app.services import remnawave_service as service_module

    monkeypatch.setattr(
        service_module,
        'RemnaWaveService',
        lambda *a, **kw: SimpleNamespace(
            is_configured=True,
            configuration_error=None,
            get_api_client=lambda: _client(api),
        ),
    )
    return await route.get_user_sync_status(
        user_id=53,
        subscription_id=None,
        admin=SimpleNamespace(id=1, telegram_id=1),
        db=db,
    )


@pytest.mark.asyncio
async def test_missing_panel_account_is_not_reported_as_synced(monkeypatch) -> None:
    """Пустая панельная сторона — это расхождение, а не «синхронизировано».

    Пока связь протухшая, все запросы к панели отвечают 404, колонка «Панель»
    состоит из прочерков — а заголовок карточки был зелёным. Выглядела здоровой
    карточка человека, у которого доступа нет вовсе.
    """
    api = _api()
    api.find_users_by_telegram_id.return_value = []
    api.find_users_by_email.return_value = []

    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        status = await _call_status(monkeypatch, db, api)

    assert status.panel_found is False
    assert status.has_differences is True, 'карточка без панельной стороны показывалась синхронизированной'
    assert any('панел' in line.lower() for line in status.differences)


@pytest.mark.asyncio
async def test_unreadable_panel_is_not_reported_as_missing(monkeypatch) -> None:
    """Панель не ответила — это «не прочитали», а не «аккаунта нет».

    Разница важна: «нет аккаунта» зовёт оператора пересоздавать учётку, а при
    обрыве связи пересоздавать нечего и незачем.
    """
    api = _api()
    api.get_user_by_id.side_effect = RuntimeError('panel is down')

    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        status = await _call_status(monkeypatch, db, api)

    assert status.has_differences is True
    assert not any('не найден' in line.lower() for line in status.differences)


@pytest.mark.asyncio
async def test_sync_stamp_is_not_set_when_the_link_did_not_move(monkeypatch) -> None:
    """Отметка «синхронизировано» не ставится поверх необновлённой связи.

    Записать id мешает соседняя подписка: колонка частично уникальна, и адрес
    уже держит она. Случай редкий, но отметка времени тогда врёт — карточка
    выглядит свежесинхронизированной, а связь осталась прежней.
    """
    api = _api()

    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        # Соседняя подписка того же человека уже держит новый панельный id.
        db.add(
            Subscription(
                id=45,
                user_id=53,
                status=SubscriptionStatus.ACTIVE.value,
                start_date=datetime.now(UTC) - timedelta(days=1),
                end_date=datetime.now(UTC) + timedelta(days=30),
                traffic_limit_gb=10,
                device_limit=1,
                connected_squads=[],
                remnawave_id=NEW_PANEL_ID,
                remnawave_short_uuid='neighbour',
                # Колонка уникальна, и дефолт у обеих строк совпал бы.
                remnawave_short_id='nb0001',
            )
        )
        await db.commit()

        response = await _call_sync(monkeypatch, db, api)

        subscription = await db.get(Subscription, 44)
        user = await db.get(User, 53)

        assert subscription.remnawave_id != NEW_PANEL_ID
        assert user.last_remnawave_sync is None, 'отметка времени поставлена поверх необновлённой связи'
        assert response.errors, 'оператору не сказали, что связь не обновилась'
