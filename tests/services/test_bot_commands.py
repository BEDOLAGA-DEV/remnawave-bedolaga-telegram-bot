from unittest.mock import AsyncMock

import pytest

from app.services.bot_commands import CommandMenuSync, build_menu, desired_menus


def test_private_scopes_and_admin_permissions():
    menus = desired_menus(['ru', 'ua'], 'ru', [42])
    assert all(key[0] in ('private', '42') for key in menus)
    assert ('private', 'uk') in menus
    assert ('private', 'ua') not in menus
    assert 'admin_help' not in [c.command for c in menus['private', 'ru']]
    assert 'admin_help' in [c.command for c in menus['42', 'ru']]
    assert 'start' in [c.command for c in menus['42', 'ru']]


def test_descriptions_and_no_unimplemented_commands():
    for lang in ('ru', 'en', 'ua', 'zh', 'fa', 'unknown'):
        commands = build_menu(lang, admin=True)
        assert all(1 <= len(c.description) <= 256 for c in commands)
        assert len({c.command for c in commands}) == len(commands)
        assert 'paysupport' in [c.command for c in commands]


@pytest.mark.asyncio
async def test_restart_revocation_and_deleted_locale():
    bot = AsyncMock()
    store = AsyncMock()
    store.load.return_value = {('42', 'ru'), ('42', ''), ('private', 'uk')}
    sync = CommandMenuSync(bot, store)
    desired = desired_menus(['ru'], 'ru', [])
    await sync.reconcile(desired)
    assert bot.delete_my_commands.await_count == 3
    assert bot.set_my_commands.await_count == 2
    await sync.reconcile(desired)
    assert bot.set_my_commands.await_count == 2
    assert bot.delete_my_commands.await_count == 3


@pytest.mark.asyncio
async def test_intent_persisted_before_api_and_failure_retried():
    bot = AsyncMock()
    store = AsyncMock()
    store.load.return_value = set()
    sync = CommandMenuSync(bot, store)

    async def fail(**kwargs):
        assert store.save.await_count == 1
        raise RuntimeError('network unavailable')

    bot.set_my_commands.side_effect = fail
    desired = {('private', ''): build_menu('en')}
    with pytest.raises(RuntimeError):
        await sync.reconcile(desired)
    bot.set_my_commands.side_effect = None
    await sync.reconcile(desired)
    assert bot.set_my_commands.await_count == 2


@pytest.mark.asyncio
async def test_failed_delete_remains_tracked():
    bot = AsyncMock()
    store = AsyncMock()
    store.load.return_value = {('42', '')}
    sync = CommandMenuSync(bot, store)
    bot.delete_my_commands.side_effect = RuntimeError('offline')
    with pytest.raises(RuntimeError):
        await sync.reconcile({})
    store.save.assert_not_awaited()
    bot.delete_my_commands.side_effect = None
    await sync.reconcile({})
    store.save.assert_awaited_once_with(set())


@pytest.mark.asyncio
async def test_one_unreachable_admin_does_not_block_other_menus():
    bot = AsyncMock()
    store = AsyncMock()
    store.load.return_value = set()

    async def set_commands(**kwargs):
        if getattr(kwargs['scope'], 'chat_id', None) == 42:
            raise RuntimeError('chat not found')

    bot.set_my_commands.side_effect = set_commands
    sync = CommandMenuSync(bot, store)
    with pytest.raises(RuntimeError):
        await sync.reconcile(desired_menus(['ru'], 'ru', [42, 43]))
    assert ('43', 'ru') in sync.applied


@pytest.mark.asyncio
async def test_retry_after_stops_batch_immediately():
    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.methods import SetMyCommands

    bot = AsyncMock()
    store = AsyncMock()
    store.load.return_value = set()
    bot.set_my_commands.side_effect = TelegramRetryAfter(
        method=SetMyCommands(commands=[]), message='rate limit', retry_after=120
    )
    sync = CommandMenuSync(bot, store)
    with pytest.raises(TelegramRetryAfter):
        await sync.reconcile(desired_menus(['ru'], 'ru', [42, 43]))
    assert bot.set_my_commands.await_count == 1


@pytest.mark.asyncio
async def test_store_survives_restart_and_separates_bots(monkeypatch):
    from contextlib import asynccontextmanager

    from app.database.models import SystemSetting
    from app.services import bot_commands
    from tests.fixtures.sqlite_memory import memory_session

    async with memory_session(monkeypatch, [SystemSetting.__table__]) as db:

        @asynccontextmanager
        async def sessions():
            yield db

        monkeypatch.setattr(bot_commands, 'AsyncSessionLocal', sessions)
        store = bot_commands.CommandMenuStore(100)
        await store.save({('42', ''), ('private', 'uk')})
        assert await bot_commands.CommandMenuStore(100).load() == {('42', ''), ('private', 'uk')}
        assert await bot_commands.CommandMenuStore(101).load() == set()
        restarted = CommandMenuSync(AsyncMock(), bot_commands.CommandMenuStore(100))
        await restarted.reconcile(desired_menus(['en'], 'en', []))
        assert await store.load() == {('private', ''), ('private', 'en')}


@pytest.mark.asyncio
async def test_worker_reloads_runtime_settings_and_cancels(monkeypatch):
    import asyncio

    from app.services import bot_commands

    monkeypatch.setattr(bot_commands.settings, 'ADMIN_IDS', '42')
    monkeypatch.setattr(bot_commands.settings, 'AVAILABLE_LANGUAGES', 'ru')
    sync = CommandMenuSync(AsyncMock(), AsyncMock())
    sync.reconcile = AsyncMock()
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)
        if len(sleeps) == 1:
            monkeypatch.setattr(bot_commands.settings, 'ADMIN_IDS', '43')
            monkeypatch.setattr(bot_commands.settings, 'AVAILABLE_LANGUAGES', 'en')
        else:
            raise asyncio.CancelledError

    monkeypatch.setattr(bot_commands.asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        await sync.run()
    first, second = [call.args[0] for call in sync.reconcile.await_args_list]
    assert ('42', 'ru') in first
    assert ('42', 'ru') not in second
    assert ('43', 'en') in second
    assert sleeps == [60, 60]


@pytest.mark.asyncio
async def test_readding_admin_after_delete_journal_failure_republishes():
    bot = AsyncMock()
    store = AsyncMock()
    store.load.return_value = set()
    sync = CommandMenuSync(bot, store)
    desired = {('42', ''): build_menu('en', admin=True)}
    await sync.reconcile(desired)
    store.save.side_effect = RuntimeError('database unavailable')
    with pytest.raises(RuntimeError):
        await sync.reconcile({})
    store.save.side_effect = None
    await sync.reconcile(desired)
    assert bot.set_my_commands.await_count == 2


@pytest.mark.parametrize('extension', ['json', 'yaml'])
@pytest.mark.parametrize('language', ['de', 'es', 'pt-br', 'pt_BR'])
def test_command_descriptions_use_operator_locales(tmp_path, monkeypatch, extension, language):
    import json

    from app.localization.loader import clear_locale_cache
    from app.services import bot_commands

    translations = {
        f'BOT_COMMAND_{name.upper()}': f'Custom {name}'
        for name in ('start', 'paysupport', 'contests', 'admin_help', 'rules_stats', 'clear_rules', 'monitoring')
    }
    # JSON is also valid YAML; both loader paths must work.
    (tmp_path / f'{language.lower()}.{extension}').write_text(json.dumps(translations))
    monkeypatch.setattr(bot_commands.settings, 'LOCALES_PATH', str(tmp_path))
    clear_locale_cache()
    try:
        menu = build_menu(language, admin=True)
        assert [command.description for command in menu] == list(translations.values())
        menus = desired_menus([language], language, [])
        telegram_code = language[:2].lower()
        assert menus['private', telegram_code][0].description == 'Custom start'
    finally:
        clear_locale_cache()


def test_builtin_override_is_not_ignored(tmp_path, monkeypatch):
    from app.localization.loader import clear_locale_cache
    from app.services import bot_commands

    (tmp_path / 'ru.json').write_text('{"BOT_COMMAND_START": "Открыть кабинет"}')
    monkeypatch.setattr(bot_commands.settings, 'LOCALES_PATH', str(tmp_path))
    clear_locale_cache()
    try:
        assert build_menu('ru')[0].description == 'Открыть кабинет'
    finally:
        clear_locale_cache()


@pytest.mark.parametrize('invalid', ['', '   ', None, 42, 'x' * 257])
def test_invalid_custom_description_does_not_break_publication(tmp_path, monkeypatch, invalid):
    import json

    from app.localization.loader import clear_locale_cache
    from app.services import bot_commands

    (tmp_path / 'de.json').write_text(json.dumps({'BOT_COMMAND_START': invalid}))
    monkeypatch.setattr(bot_commands.settings, 'LOCALES_PATH', str(tmp_path))
    clear_locale_cache()
    try:
        assert build_menu('de')[0].description == 'Main menu'
    finally:
        clear_locale_cache()


@pytest.mark.asyncio
async def test_stars_toggle_republishes_menus_without_restart(monkeypatch):
    from app.config import settings

    bot = AsyncMock()
    store = AsyncMock()
    store.load.return_value = set()
    sync = CommandMenuSync(bot, store)
    for enabled in (True, False, True):
        monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', enabled)
        await sync.reconcile(desired_menus(['ru'], 'ru', [42]))
        commands = bot.set_my_commands.call_args.kwargs['commands']
        assert ('paysupport' in [command.command for command in commands]) is enabled
    assert bot.set_my_commands.await_count == 12
