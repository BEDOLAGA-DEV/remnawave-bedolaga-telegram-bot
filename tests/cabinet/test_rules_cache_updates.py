"""Rules saved through the cabinet must reach an already running Telegram bot."""

from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.cabinet.routes import admin_legal_pages, info
from app.config import settings
from app.database import database
from app.database.crud.rules import create_or_update_rules, restore_rules_version
from app.database.models import ServiceRule
from app.localization import texts
from tests.fixtures.sqlite_memory import memory_session


@pytest.fixture(autouse=True)
def isolated_rules_cache(monkeypatch):
    monkeypatch.setattr(texts, '_cached_rules', {})
    monkeypatch.setattr(settings, 'AVAILABLE_LANGUAGES', 'ru,en')
    monkeypatch.setattr(settings, 'SERVICE_RULES_DISPLAY_MODE', 'both')


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en'])
@pytest.mark.parametrize('new_content', ['<b>New rules</b>', ''])
async def test_cabinet_save_updates_warm_bot_rules(monkeypatch, language, new_content):
    async with memory_session(monkeypatch, [ServiceRule.__table__]) as db:
        monkeypatch.setattr(database, 'AsyncSessionLocal', async_sessionmaker(db.bind, expire_on_commit=False))
        other_language = 'en' if language == 'ru' else 'ru'
        await create_or_update_rules(db, '<b>Old rules</b>', language)
        await create_or_update_rules(db, '<b>Other language</b>', other_language)
        assert await texts.get_rules(language) == '<b>Old rules</b>'
        assert await texts.get_rules(other_language) == '<b>Other language</b>'

        await admin_legal_pages.update_rules_admin(
            request=admin_legal_pages.RulesUpdateRequest(items=[{'language': language, 'content': new_content}]),
            admin=SimpleNamespace(id=1),
            db=db,
        )
        await db.rollback()
        # Public cabinet reads directly from DB; the bot must observe the same update.
        public_rules = await info.get_rules(language=language, db=db)
        if new_content:
            assert public_rules.content == '<b>New rules</b>'
            assert await texts.get_rules(language) == '<b>New rules</b>'
            assert texts.get_rules_sync(language) == '<b>New rules</b>'
        else:
            assert '<b>Old rules</b>' not in public_rules.content
            assert '<b>Old rules</b>' not in await texts.get_rules(language)
            assert '<b>Old rules</b>' not in texts.get_rules_sync(language)
        assert await texts.get_rules(other_language) == '<b>Other language</b>'


@pytest.mark.asyncio
async def test_restore_rules_updates_warm_cache(monkeypatch):
    async with memory_session(monkeypatch, [ServiceRule.__table__]) as db:
        monkeypatch.setattr(database, 'AsyncSessionLocal', async_sessionmaker(db.bind, expire_on_commit=False))
        original = await create_or_update_rules(db, 'Original rules', 'ru')
        await create_or_update_rules(db, 'Replacement rules', 'ru')
        await texts.refresh_rules_cache('ru')
        assert await texts.get_rules('ru') == 'Replacement rules'

        await restore_rules_version(db, original.id, 'ru')

        assert await texts.get_rules('ru') == 'Original rules'
        assert texts.get_rules_sync('ru') == 'Original rules'


@pytest.mark.asyncio
async def test_failed_rules_write_keeps_committed_cache(monkeypatch):
    from unittest.mock import AsyncMock

    async with memory_session(monkeypatch, [ServiceRule.__table__]) as db:
        monkeypatch.setattr(database, 'AsyncSessionLocal', async_sessionmaker(db.bind, expire_on_commit=False))
        await create_or_update_rules(db, 'Committed rules', 'ru')
        assert await texts.get_rules('ru') == 'Committed rules'
        monkeypatch.setattr(db, 'commit', AsyncMock(side_effect=RuntimeError('write failed')))

        with pytest.raises(RuntimeError, match='write failed'):
            await create_or_update_rules(db, 'Uncommitted rules', 'ru')
        await db.rollback()

        assert await texts.get_rules('ru') == 'Committed rules'
        assert (await info.get_rules(language='ru', db=db)).content == 'Committed rules'
