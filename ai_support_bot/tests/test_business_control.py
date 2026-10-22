from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ai_support_bot.app.db import database
from ai_support_bot.app.db.models import Base, BusinessChatControl
from ai_support_bot.app.services import business_control as control


@pytest_asyncio.fixture
async def store(monkeypatch, tmp_path):
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "control.db"}')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(database, 'AsyncSessionLocal', async_sessionmaker(engine, expire_on_commit=False))
    yield
    await engine.dispose()


@pytest.mark.asyncio
async def test_controls_persist_and_are_scoped_per_connection_and_chat(store):
    first = await control.get_chat('one', 555, 900)
    same = await control.get_chat('one', 555, 900)
    other_chat = await control.get_chat('one', 556, 900)
    other_account = await control.get_chat('two', 555, 901)
    assert same.id == first.id
    await control.set_manual(first.id, True)
    assert control.paused(await control.get_by_id(first.id))
    assert not control.paused(await control.get_by_id(other_chat.id))
    assert not control.paused(await control.get_by_id(other_account.id))
    assert (await control.find_owned_chat(900, 555)).id == first.id
    assert await control.find_owned_chat(902, 555) is None


@pytest.mark.asyncio
async def test_operator_messages_extend_pause_but_stale_messages_do_not_shorten_it(store):
    chat = await control.get_chat('one', 555, 900)
    now = datetime.now(UTC)
    await control.operator_message(chat.id, now - timedelta(minutes=10))
    first = await control.get_by_id(chat.id)
    assert control.paused(first)
    assert first.pause_until.replace(tzinfo=UTC) == now + timedelta(minutes=20)
    await control.operator_message(chat.id, now)
    renewed = await control.get_by_id(chat.id)
    assert renewed.pause_until.replace(tzinfo=UTC) == now + timedelta(minutes=30)
    await control.operator_message(chat.id, now - timedelta(minutes=20))
    await control.operator_message(chat.id, now - timedelta(hours=1))
    assert (await control.get_by_id(chat.id)).pause_until == renewed.pause_until
    assert not await control.active(chat.id, first.revision)


@pytest.mark.asyncio
async def test_manual_pause_survives_auto_expiry_and_resume_invalidates_old_generation(store):
    chat = await control.get_chat('one', 555, 900)
    assert await control.active(chat.id, chat.revision)
    await control.set_manual(chat.id, True)
    await control.operator_message(chat.id, datetime.now(UTC))
    paused = await control.get_by_id(chat.id)
    assert paused.manual_pause
    assert not await control.active(chat.id, paused.revision)
    await control.set_manual(chat.id, False)
    resumed = await control.get_by_id(chat.id)
    assert resumed.pause_until is None
    assert await control.active(chat.id, resumed.revision)
    assert not await control.active(chat.id, chat.revision)


@pytest.mark.asyncio
async def test_expired_auto_pause_does_not_block_new_messages(store):
    chat = await control.get_chat('one', 555, 900)

    async with database.AsyncSessionLocal() as db:
        await db.execute(
            update(BusinessChatControl)
            .where(BusinessChatControl.id == chat.id)
            .values(
                pause_until=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        await db.commit()
    assert await control.active(chat.id, chat.revision)
