"""Управление устройствами до прогона бэкфила 3.0.0.

Миграция 0104 только ДОБАВЛЯЕТ `remnawave_id`; заполняет колонку одноразовый
`scripts/backfill_remnawave_ids.py`. В окне между ними — и навсегда для строк,
которые бэкфил честно оставил `unresolved`, — колонка пуста, и «Управление
устройствами» упиралось в алерт DEVICE_UUID_NOT_FOUND: панель не опрашивалась
вообще, хотя `shortUuid` апгрейд пережил и панель его по-прежнему знает
(`GET /api/users/by-short-uuid/{shortUuid}`).

Остальные денежные пути 4.0.0 этот подхват уже делают — сброс трафика
(`traffic.py`), `update_remnawave_user`, `create_remnawave_user`, grace-рантайм.
Хендлеры устройств ходят в панель сами, мимо `update_remnawave_user`, и потому
остались единственным путём без подхвата.

Тесты держат и обратную границу: подхват не должен превращаться в «взять хоть
какой-нибудь аккаунт». Без `shortUuid` и при занятом соседней подпиской id
операция обязана отказаться, а не молча показать чужие устройства.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.cabinet.routes.subscription_modules.devices as cabinet_devices
import app.handlers.subscription.devices as devices_mod
from app.config import Settings
from app.services.subscription_service import SubscriptionService


ADOPTED_PANEL_ID = 4242
USER_PANEL_ID = 101
SHORT_UUID = 'sh0rtUu1d'


def _set_multi(monkeypatch, value: bool) -> None:
    # Settings-методы патчатся на классе, поля — на инстансе (pydantic).
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: value)


def _make_callback():
    cb = MagicMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def _make_user(panel_id=None):
    return SimpleNamespace(id=1, language='ru', remnawave_id=panel_id, remnawave_uuid='legacy-user-uuid')


def _make_subscription(*, panel_id=None, short_uuid=SHORT_UUID):
    return SimpleNamespace(
        id=7,
        is_trial=False,
        status='active',
        remnawave_id=panel_id,
        remnawave_short_uuid=short_uuid,
        remnawave_uuid='legacy-sub-uuid',
    )


def _make_db(*, sibling_owner=None):
    """DB-дубль. `sibling_owner` — id подписки, уже держащей панельный аккаунт."""
    db = MagicMock()
    res = MagicMock()
    res.scalar_one_or_none = MagicMock(return_value=sibling_owner)
    db.execute = AsyncMock(return_value=res)
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    return db


def _devices_service(devices):
    """Дубль RemnaWaveService: та же форма ответа, что у реального API."""
    api = AsyncMock()
    api.get_user_devices = AsyncMock(return_value={'total': len(devices), 'devices': list(devices)})

    @asynccontextmanager
    async def _cm():
        yield api

    svc = MagicMock()
    # side_effect=_cm, а не lambda: каждый вызов должен отдавать СВЕЖИЙ CM.
    svc.get_api_client = MagicMock(side_effect=_cm)
    svc.api_double = api
    return svc


def _patch_adoption_api(monkeypatch, adopted):
    """Настоящая логика подхвата, ненастоящая панель: `adopted` — ответ по shortUuid."""
    api = AsyncMock()
    api.get_user_by_short_uuid = AsyncMock(return_value=adopted)

    @asynccontextmanager
    async def _cm(_self):
        yield api

    monkeypatch.setattr(SubscriptionService, 'get_api_client', _cm)
    return api


def _alerted_uuid_not_found(cb) -> bool:
    return any('UUID' in str(c.args[0]) for c in cb.answer.await_args_list if c.args)


async def _run_management(monkeypatch, *, subscription, user, db, adopted, devices=({'hwid': 'AA'},)):
    cb = _make_callback()
    adoption_api = _patch_adoption_api(monkeypatch, adopted)
    service = _devices_service(list(devices))

    with (
        patch.object(devices_mod, '_resolve_subscription', new=AsyncMock(return_value=(subscription, subscription.id))),
        patch.object(devices_mod, 'show_devices_page', new=AsyncMock()) as show,
        patch('app.services.remnawave_service.RemnaWaveService', return_value=service),
    ):
        await devices_mod.handle_device_management(cb, user, db, None)

    return SimpleNamespace(cb=cb, show=show, service=service, adoption_api=adoption_api)


@pytest.mark.anyio('asyncio')
async def test_adopts_panel_id_by_short_uuid_when_column_is_null(monkeypatch):
    """Репорт из Telegram: «UUID пользователя не найден» на доапгрейдной строке."""
    _set_multi(monkeypatch, True)
    sub, user, db = _make_subscription(), _make_user(), _make_db()

    run = await _run_management(
        monkeypatch, subscription=sub, user=user, db=db, adopted=SimpleNamespace(id=ADOPTED_PANEL_ID)
    )

    assert not _alerted_uuid_not_found(run.cb), 'подхват по shortUuid должен был спасти операцию'
    run.show.assert_awaited_once()
    # В панель ушёл именно опознанный числовой id.
    assert run.service.api_double.get_user_devices.await_args.args == (ADOPTED_PANEL_ID,)
    # Опознанный id закреплён за строкой, иначе подхват повторялся бы на каждый клик.
    # Коммитит сессию auth-мидлварь после хендлера, здесь достаточно flush.
    assert sub.remnawave_id == ADOPTED_PANEL_ID
    db.flush.assert_awaited()


@pytest.mark.anyio('asyncio')
async def test_refuses_when_there_is_no_short_uuid(monkeypatch):
    """Опознавать нечем — честный отказ, а не догадка."""
    _set_multi(monkeypatch, True)
    sub, user, db = _make_subscription(short_uuid=None), _make_user(), _make_db()

    run = await _run_management(monkeypatch, subscription=sub, user=user, db=db, adopted=None)

    assert _alerted_uuid_not_found(run.cb)
    run.show.assert_not_awaited()
    run.adoption_api.get_user_by_short_uuid.assert_not_awaited()
    assert sub.remnawave_id is None


@pytest.mark.anyio('asyncio')
async def test_refuses_when_panel_does_not_know_the_short_uuid(monkeypatch):
    """Панель ответила 404 — аккаунта нет, показывать нечего."""
    _set_multi(monkeypatch, True)
    sub, user, db = _make_subscription(), _make_user(), _make_db()

    run = await _run_management(monkeypatch, subscription=sub, user=user, db=db, adopted=None)

    assert _alerted_uuid_not_found(run.cb)
    run.show.assert_not_awaited()
    assert sub.remnawave_id is None


@pytest.mark.anyio('asyncio')
async def test_multi_tariff_does_not_borrow_a_sibling_subscriptions_account(monkeypatch):
    """Аккаунт держит соседний тариф — показать его устройства нельзя.

    Это тот же баг «общего лимита по наименьшему тарифу», от которого защищает
    `_get_panel_user_id`; подхват не имеет права его вернуть обходным путём.
    """
    _set_multi(monkeypatch, True)
    sub, user, db = _make_subscription(), _make_user(), _make_db(sibling_owner=99)

    run = await _run_management(
        monkeypatch, subscription=sub, user=user, db=db, adopted=SimpleNamespace(id=ADOPTED_PANEL_ID)
    )

    assert _alerted_uuid_not_found(run.cb)
    run.show.assert_not_awaited()
    run.service.api_double.get_user_devices.assert_not_awaited()
    assert sub.remnawave_id is None


@pytest.mark.anyio('asyncio')
async def test_multi_tariff_never_falls_back_to_the_user_level_id(monkeypatch):
    """Инвариант `_get_panel_user_id`: пустой id подписки не занимает user-level."""
    _set_multi(monkeypatch, True)
    sub, user, db = _make_subscription(short_uuid=None), _make_user(panel_id=USER_PANEL_ID), _make_db()

    run = await _run_management(monkeypatch, subscription=sub, user=user, db=db, adopted=None)

    assert _alerted_uuid_not_found(run.cb)
    run.service.api_double.get_user_devices.assert_not_awaited()


@pytest.mark.anyio('asyncio')
async def test_single_tariff_uses_user_level_id_without_adoption(monkeypatch):
    """Single-tariff со здоровой строкой: подхват не нужен и не должен срабатывать."""
    _set_multi(monkeypatch, False)
    sub, user, db = _make_subscription(), _make_user(panel_id=USER_PANEL_ID), _make_db()

    run = await _run_management(
        monkeypatch, subscription=sub, user=user, db=db, adopted=SimpleNamespace(id=ADOPTED_PANEL_ID)
    )

    run.show.assert_awaited_once()
    assert run.service.api_double.get_user_devices.await_args.args == (USER_PANEL_ID,)
    run.adoption_api.get_user_by_short_uuid.assert_not_awaited()


@pytest.mark.anyio('asyncio')
async def test_single_tariff_adoption_writes_the_id_onto_the_user(monkeypatch):
    """Single-tariff: все подписки смотрят в один аккаунт, id живёт на User.

    Писать его в `subscriptions.remnawave_id` нельзя — колонка частично уникальна,
    и вторая строка того же человека словила бы IntegrityError.
    """
    _set_multi(monkeypatch, False)
    sub, user, db = _make_subscription(), _make_user(), _make_db()

    run = await _run_management(
        monkeypatch, subscription=sub, user=user, db=db, adopted=SimpleNamespace(id=ADOPTED_PANEL_ID)
    )

    run.show.assert_awaited_once()
    assert run.service.api_double.get_user_devices.await_args.args == (ADOPTED_PANEL_ID,)
    assert user.remnawave_id == ADOPTED_PANEL_ID
    assert sub.remnawave_id is None


@pytest.mark.anyio('asyncio')
async def test_cabinet_persists_the_adopted_id(monkeypatch):
    """Кабинет обязан закоммитить подхват сам.

    `get_cabinet_db` сессию не коммитит, а `close()` откатит незакоммиченное:
    без явного commit опознанный id терялся бы и панель опрашивалась бы заново
    на каждый запрос устройств.
    """
    _set_multi(monkeypatch, True)
    sub, user, db = _make_subscription(), _make_user(), _make_db()
    _patch_adoption_api(monkeypatch, SimpleNamespace(id=ADOPTED_PANEL_ID))

    assert await cabinet_devices._resolve_panel_user_id_or_adopt(sub, user, db) == ADOPTED_PANEL_ID
    assert sub.remnawave_id == ADOPTED_PANEL_ID
    db.commit.assert_awaited()


@pytest.mark.anyio('asyncio')
async def test_cabinet_does_not_commit_when_nothing_was_adopted(monkeypatch):
    """Опознать не удалось — коммитить нечего и незачем."""
    _set_multi(monkeypatch, True)
    sub, user, db = _make_subscription(), _make_user(), _make_db()
    _patch_adoption_api(monkeypatch, None)

    assert await cabinet_devices._resolve_panel_user_id_or_adopt(sub, user, db) is None
    db.commit.assert_not_awaited()


@pytest.mark.anyio('asyncio')
async def test_cabinet_healthy_row_never_touches_the_panel(monkeypatch):
    """Заполненная колонка — читаем её и не ходим за идентичностью вообще."""
    _set_multi(monkeypatch, True)
    sub, user, db = _make_subscription(panel_id=ADOPTED_PANEL_ID), _make_user(), _make_db()
    api = _patch_adoption_api(monkeypatch, SimpleNamespace(id=777))

    assert await cabinet_devices._resolve_panel_user_id_or_adopt(sub, user, db) == ADOPTED_PANEL_ID
    api.get_user_by_short_uuid.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.anyio('asyncio')
async def test_panel_transient_error_does_not_look_like_a_missing_account(monkeypatch):
    """5xx/таймаут — «не знаем», а не «аккаунта нет»: строку не трогаем."""
    _set_multi(monkeypatch, True)
    sub, user, db = _make_subscription(), _make_user(), _make_db()

    cb = _make_callback()
    api = AsyncMock()
    api.get_user_by_short_uuid = AsyncMock(side_effect=RuntimeError('panel 503'))

    @asynccontextmanager
    async def _cm(_self):
        yield api

    monkeypatch.setattr(SubscriptionService, 'get_api_client', _cm)
    service = _devices_service([{'hwid': 'AA'}])

    with (
        patch.object(devices_mod, '_resolve_subscription', new=AsyncMock(return_value=(sub, sub.id))),
        patch.object(devices_mod, 'show_devices_page', new=AsyncMock()) as show,
        patch('app.services.remnawave_service.RemnaWaveService', return_value=service),
    ):
        await devices_mod.handle_device_management(cb, user, db, None)

    assert _alerted_uuid_not_found(cb)
    show.assert_not_awaited()
    assert sub.remnawave_id is None
