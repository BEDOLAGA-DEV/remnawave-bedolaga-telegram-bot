from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.cabinet.routes import admin_user_reminders as routes
from app.cabinet.schemas.user_reminders import AudienceRequest, ReminderPayload
from app.database.models import Base, User, UserReminder
from app.services.permission_service import PERMISSION_REGISTRY
from app.services.rbac_bootstrap_service import _PRESET_ROLES
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)
ADMIN = SimpleNamespace(id=99, telegram_id=555, language='ru')


def _payload(**kw) -> ReminderPayload:
    base = dict(
        name='Тест',
        channels='both',
        category='service',
        conditions={'auth': 'single_method'},
        repeat_every_days=7,
        max_sends=2,
        texts={'ru': {'title': 'T', 'body': 'B', 'button': 'Go'}},
        button_kind='cabinet',
        button_target='/profile/accounts',
    )
    base.update(kw)
    return ReminderPayload(**base)


def test_permissions_are_registered():
    assert PERMISSION_REGISTRY['user_reminders'] == ['read', 'create', 'edit', 'delete']


@pytest.mark.parametrize('role_name', ['Admin', 'Marketer'])
def test_preset_roles_grant_the_section(role_name):
    """Без этого записи есть только у Суперадмина — как у любого другого раздела
    из PERMISSION_REGISTRY (pinned_messages, wheel, landings…), которого нет в
    пресетах Admin/Marketer, тут быть не должно.
    """
    role = next(role for role in _PRESET_ROLES if role['name'] == role_name)
    assert 'user_reminders:*' in role['permissions']


@pytest.mark.parametrize(
    'kw',
    [
        {'texts': {'en': {'title': 'T', 'body': 'B'}}},
        {'button_kind': 'url', 'button_target': 'http://x.example'},
        {'button_kind': 'cabinet', 'button_target': '//evil'},
        {'repeat_every_days': 0},
        {'max_sends': 21},
        {'conditions': {'auth': 'nobody'}},
        {'channels': 'email'},
    ],
)
def test_payload_validation(kw):
    with pytest.raises(ValidationError):
        _payload(**kw)


@pytest.mark.asyncio
async def test_crud_flow_and_builtin_protection(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        created = await routes.create_reminder(_payload(), admin=ADMIN, db=db)
        assert created.is_active is False and created.is_builtin is False

        updated = await routes.update_reminder(created.id, _payload(name='Новое'), admin=ADMIN, db=db)
        assert updated.name == 'Новое'
        toggled = await routes.toggle_reminder(created.id, admin=ADMIN, db=db)
        assert toggled.is_active is True

        listed = await routes.list_reminders_route(admin=ADMIN, db=db)
        assert [r.id for r in listed] == [created.id]
        assert listed[0].stats.sent_total == 0

        db.add(
            UserReminder(
                name='b',
                builtin_key='k',
                channels='both',
                category='service',
                conditions={},
                repeat_every_days=7,
                max_sends=1,
                texts={'ru': {'title': 't', 'body': 'b'}},
                button_kind='none',
            )
        )
        await db.commit()
        builtin = next(r for r in await routes.list_reminders_route(admin=ADMIN, db=db) if r.is_builtin)
        with pytest.raises(HTTPException) as denied:
            await routes.delete_reminder(builtin.id, admin=ADMIN, db=db)
        assert denied.value.status_code == 409

        await routes.delete_reminder(created.id, admin=ADMIN, db=db)
        with pytest.raises(HTTPException) as missing:
            await routes.get_reminder_route(created.id, admin=ADMIN, db=db)
        assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_audience_counts_per_channel(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                User(id=1, telegram_id=10, first_name='A', language='ru', status='active', balance_kopeks=0),
                User(
                    id=2,
                    email='e@x',
                    password_hash='h',
                    first_name='B',
                    language='ru',
                    status='active',
                    balance_kopeks=0,
                ),
            ]
        )
        await db.commit()
        both = await routes.audience(
            AudienceRequest(conditions={'auth': 'single_method'}, channels='both'), admin=ADMIN, db=db
        )
        assert (both.bot, both.cabinet) == (1, 2)
        bot_only = await routes.audience(AudienceRequest(conditions={}, channels='bot'), admin=ADMIN, db=db)
        assert bot_only.cabinet is None


@pytest.mark.asyncio
async def test_audience_marketing_excludes_promo_opt_out(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                User(
                    id=1,
                    telegram_id=10,
                    first_name='A',
                    language='ru',
                    status='active',
                    balance_kopeks=0,
                    notification_settings={'promo_offers_enabled': False},
                ),
                User(id=2, telegram_id=20, first_name='B', language='ru', status='active', balance_kopeks=0),
            ]
        )
        await db.commit()

        marketing = await routes.audience(
            AudienceRequest(conditions={}, channels='bot', category='marketing'), admin=ADMIN, db=db
        )
        service = await routes.audience(
            AudienceRequest(conditions={}, channels='bot', category='service'), admin=ADMIN, db=db
        )

        assert marketing.bot == 1
        assert service.bot == 2


@pytest.mark.asyncio
async def test_response_audience_bot_uses_reminder_category(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                User(
                    id=1,
                    telegram_id=10,
                    first_name='A',
                    language='ru',
                    status='active',
                    balance_kopeks=0,
                    notification_settings={'promo_offers_enabled': False},
                ),
                User(id=2, telegram_id=20, first_name='B', language='ru', status='active', balance_kopeks=0),
            ]
        )
        await db.commit()

        created = await routes.create_reminder(_payload(channels='bot', category='marketing'), admin=ADMIN, db=db)
        assert created.stats.audience_bot == 1


@pytest.mark.asyncio
async def test_test_send_goes_to_the_admin(monkeypatch):
    bot = SimpleNamespace(send_message=AsyncMock(), session=SimpleNamespace(close=AsyncMock()))
    monkeypatch.setattr(routes, 'create_bot', lambda: bot)
    async with memory_session(monkeypatch, TABLES) as db:
        created = await routes.create_reminder(_payload(button_kind='none', button_target=None), admin=ADMIN, db=db)
        assert await routes.send_test(created.id, admin=ADMIN, db=db) == {'ok': True}
        kwargs = bot.send_message.await_args.kwargs
        assert kwargs['chat_id'] == 555 and kwargs['text'].startswith('<b>T</b>')

        with pytest.raises(HTTPException) as no_tg:
            await routes.send_test(created.id, admin=SimpleNamespace(id=1, telegram_id=None, language='ru'), db=db)
        assert no_tg.value.status_code == 400


@pytest.mark.asyncio
async def test_send_test_rejects_malformed_stored_texts(monkeypatch):
    """Битые тексты у уже сохранённого напоминания — 422, не 500, и бот не создаётся.

    ``render_bot_message`` ожидает ``texts['ru']`` (см. ``pick_text``) и падает
    ``KeyError`` без него — тест себе обязан ловить это раньше, валидацией.
    """
    create_bot_mock = AsyncMock()
    monkeypatch.setattr(routes, 'create_bot', create_bot_mock)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(
            UserReminder(
                name='broken',
                channels='both',
                category='service',
                conditions={},
                repeat_every_days=7,
                max_sends=1,
                texts={'en': {'title': 't', 'body': 'b'}},
                button_kind='none',
            )
        )
        await db.commit()
        broken = (await routes.list_reminders_route(admin=ADMIN, db=db))[0]

        with pytest.raises(HTTPException) as invalid:
            await routes.send_test(broken.id, admin=ADMIN, db=db)
        assert invalid.value.status_code == 422
        create_bot_mock.assert_not_called()


def test_uses_non_deprecated_422_constant():
    """status.HTTP_422_UNPROCESSABLE_ENTITY is deprecated in this Starlette version and
    emits a DeprecationWarning on every access — HTTP_422_UNPROCESSABLE_CONTENT does not.
    """
    import warnings

    from starlette import status as starlette_status

    assert routes.status.HTTP_422_UNPROCESSABLE_CONTENT == 422
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        assert starlette_status.HTTP_422_UNPROCESSABLE_CONTENT == 422


@pytest.mark.asyncio
async def test_malformed_stored_conditions_do_not_break_reads(monkeypatch):
    """Строка с некорректными conditions/текстами всё ещё отображается в списке и по id.

    Схема ответа (ReminderResponse) не должна прогонять валидаторы записи (Task 7
    controller ruling) — иначе битая строка в БД делает GET 500 и админ не может
    её исправить через тот же API.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(
            UserReminder(
                name='broken',
                channels='both',
                category='service',
                conditions={'auth': 'garbage'},
                repeat_every_days=7,
                max_sends=1,
                texts={'en': {'title': 't', 'body': 'b'}},
                button_kind='none',
            )
        )
        await db.commit()

        listed = await routes.list_reminders_route(admin=ADMIN, db=db)
        assert len(listed) == 1
        assert listed[0].name == 'broken'
        assert listed[0].stats.audience_bot is None
        assert listed[0].stats.audience_cabinet is None

        fetched = await routes.get_reminder_route(listed[0].id, admin=ADMIN, db=db)
        assert fetched.name == 'broken'
        assert fetched.stats.audience_bot is None
