"""Живое меню: фон правит то же сообщение, только когда видимое изменилось, и уступает нагрузке."""

import asyncio
import inspect
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import ClientDecodeError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import EditMessageText
from aiogram.types import InlineKeyboardMarkup

import app.handlers.menu as menu_mod
from app.config import Settings, settings
from app.external.remnawave_api import RemnaWaveAPI
from app.localization.texts import Texts, get_texts
from app.services import live_menu_service as live
from app.services.monitoring_service import MonitoringService
from app.utils import rich_menu
from app.utils.cache import CacheService
from app.webserver.telegram import TelegramWebhookProcessor
from tests.utils.test_rich_menu import DummyTexts, _make_callback, _make_keyboard, _make_subscription, _make_user


KEY = 'live_menu:100'
GIB = 1024**3
_BOT_PATH = Path(__file__).resolve().parents[2] / 'app' / 'bot.py'


class FakeRedis:
    """Redis на dict: байты, как у настоящего клиента; eval — только CAS живого меню."""

    def __init__(self):
        self.data: dict[str, bytes] = {}
        self.ex: dict[str, int | None] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key], self.ex[key] = value.encode(), ex
        return True

    async def delete(self, key):
        return int(self.data.pop(key, None) is not None)

    # KEYS нет намеренно: он блокирует Redis на обход всех ключей бота, живое меню ходит SCAN'ом.
    async def scan_iter(self, match, count=None):
        for key in list(self.data):
            if fnmatch(key, match):
                yield key.encode()

    async def eval(self, script, numkeys, key, expected, new):
        assert script is live._CAS, 'фейк эмулирует только CAS живого меню'
        assert "'KEEPTTL'" in script, 'фоновая правка не должна снимать TTL ключа'
        if self.data.get(key) == expected:
            self.data[key] = new.encode()


@pytest.fixture
def env(monkeypatch):
    rich_menu._reset_rich_menu_availability()
    for name, value in (
        ('MAIN_MENU_RICH_ENABLED', True),
        ('MAIN_MENU_LIVE_ENABLED', True),
        ('MAIN_MENU_RICH_LOGO_URL', ''),
        ('WEBHOOK_URL', None),
    ):
        monkeypatch.setattr(settings, name, value, raising=False)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)

    cache = CacheService()
    cache.redis_client = FakeRedis()
    cache._connected = True
    db = AsyncMock()
    subscription = SimpleNamespace(
        id=7,
        actual_status='active',
        end_date=datetime.now(UTC) + timedelta(days=12),
        updated_at=datetime.now(UTC) - timedelta(hours=1),
        tariff_id=None,
        is_trial=False,
        traffic_used_gb=31.2,
        traffic_limit_gb=500,
        device_limit=3,
    )
    user = SimpleNamespace(
        language='ru', balance_kopeks=0, remnawave_id=501, subscription=subscription, subscriptions=[subscription]
    )

    @asynccontextmanager
    async def session():
        yield db

    async def no_sleep(delay):
        return None

    monkeypatch.setattr(live, 'cache', cache)
    monkeypatch.setattr(rich_menu, 'cache', cache)
    monkeypatch.setattr(live, 'AsyncSessionLocal', session)
    monkeypatch.setattr(live, 'get_user_by_telegram_id', AsyncMock(return_value=user))
    monkeypatch.setattr(live, 'build_main_menu_rich_html', AsyncMock(return_value='<p>menu</p>'))
    monkeypatch.setattr(live, 'set_committed_value', setattr)  # SimpleNamespace — не ORM-объект
    monkeypatch.setattr(live, '_pool_counters', lambda pool: None)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    monkeypatch.setattr(menu_mod, 'build_main_menu_keyboard', AsyncMock(return_value=keyboard))
    monkeypatch.setattr(RemnaWaveAPI, '_throttled_until', 0.0)
    monkeypatch.setattr(asyncio, 'sleep', no_sleep)
    yield SimpleNamespace(cache=cache, db=db, user=user, bot=AsyncMock())
    rich_menu._reset_rich_menu_availability()


async def _track(env, key=KEY):
    """Меню показано до изменений: снимок с текущими данными."""
    fp = rich_menu.live_menu_fingerprint(env.user, get_texts('ru'))
    await env.cache.set(key, {'m': 42, 'fp': fp}, expire=60)


def _panel(used_bytes):
    api = SimpleNamespace(
        get_all_users_stream=AsyncMock(return_value=[SimpleNamespace(id=501, used_traffic_bytes=used_bytes)])
    )

    @asynccontextmanager
    async def client():
        yield api

    return SimpleNamespace(get_api_client=MagicMock(side_effect=client))


def test_live_menu_is_wired():
    """Пины: при переносе на новый upstream эти строки теряются молча, а остальные тесты зелёные."""
    middlewares = re.findall(r'dp\.callback_query\.middleware\((\w+)', _BOT_PATH.read_text(encoding='utf-8'))
    # Сброс первым: middleware дальше по цепочке могут править сообщение или ответить сами.
    assert middlewares[:2] == ['ContextVarsMiddleware', 'forget_live_menu_on_callback']
    assert 'live_menu_loop(self)' in inspect.getsource(MonitoringService.start_monitoring)


async def test_press_on_live_menu_forgets_it_and_passes_through(env, monkeypatch):
    """Нажатие на живом меню — дальше подменю: фон это сообщение больше не трогает."""
    await env.cache.set(KEY, {'m': 42, 'fp': 'x'})
    # И при выключенной настройке: иначе «выкл → подменю → вкл» — фон перетрёт подменю.
    monkeypatch.setattr(settings, 'MAIN_MENU_LIVE_ENABLED', False, raising=False)
    handler = AsyncMock(return_value='handled')

    def press(message_id):
        return SimpleNamespace(message=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=message_id))

    assert await rich_menu.forget_live_menu_on_callback(handler, press(43), {}) == 'handled'
    await rich_menu.forget_live_menu_on_callback(handler, SimpleNamespace(message=None), {})
    assert await env.cache.get(KEY) == {'m': 42, 'fp': 'x'}, 'кнопка на другом сообщении'

    await rich_menu.forget_live_menu_on_callback(handler, press(42), {})
    assert await env.cache.get(KEY) is None
    assert handler.await_count == 3


@pytest.mark.parametrize(
    ('photo', 'edit_error', 'expected_message_id'),
    [
        (None, None, 42),
        ([MagicMock()], None, 77),  # фото пересоздаётся — живым становится новое сообщение
        (None, TelegramBadRequest(method=None, message='message is not modified'), 42),
        (None, [TelegramBadRequest(method=None, message='Bad Request: RICH_MESSAGE_DATE_INVALID'), None], 42),
        (None, TelegramForbiddenError(method=None, message='bot was blocked by the user'), None),
    ],
)
async def test_try_edit_remembers_live_menu(monkeypatch, env, photo, edit_error, expected_message_id):
    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', AsyncMock(return_value='<p>menu</p>'))
    callback = _make_callback(text=None if photo else 'menu', photo=photo)
    callback.bot.side_effect = edit_error
    callback.bot.send_rich_message.return_value = SimpleNamespace(message_id=77)

    edited = await rich_menu.try_edit_rich_main_menu(
        callback, _make_user(None), DummyTexts(), AsyncMock(), _make_keyboard()
    )

    assert edited is True
    state = await env.cache.get(KEY)
    assert (state and state['m']) == expected_message_id


async def test_try_send_remembers_live_menu_only_when_enabled(monkeypatch, env):
    monkeypatch.setattr(rich_menu, 'build_main_menu_rich_html', AsyncMock(return_value='<p>menu</p>'))
    bot = AsyncMock()
    bot.send_rich_message.return_value = SimpleNamespace(message_id=55)

    async def send():
        return await rich_menu.try_send_rich_main_menu(bot, 100, _make_user(None), DummyTexts(), None, MagicMock())

    monkeypatch.setattr(settings, 'MAIN_MENU_LIVE_ENABLED', False, raising=False)
    assert await send() is True
    assert env.cache.redis_client.data == {}

    monkeypatch.setattr(settings, 'MAIN_MENU_LIVE_ENABLED', True, raising=False)
    assert await send() is True
    assert (await env.cache.get(KEY))['m'] == 55
    assert env.cache.redis_client.ex[KEY] == rich_menu.LIVE_MENU_TTL
    assert Settings.model_fields['MAIN_MENU_LIVE_ENABLED'].default is False

    # Живость не повод уводить уже показанное меню в классику.
    monkeypatch.setattr(rich_menu, 'live_menu_fingerprint', MagicMock(side_effect=RuntimeError('fp')))
    assert await send() is True


def test_live_menu_fingerprint_tracks_only_visible_changes():
    """Меню показывает целые ГБ: доли гигабайта не повод править сообщение."""
    texts = SimpleNamespace(format_traffic=Texts.format_traffic)
    now = datetime.now(UTC)

    def fingerprint(used_gb=31.2, balance_kopeks=125_000, extra_days=0, **changes):
        subscription = _make_subscription(now, days_left=12 + extra_days)
        subscription.traffic_used_gb = used_gb
        vars(subscription).update(changes)
        user = _make_user(subscription)
        user.balance_kopeks = balance_kopeks
        return rich_menu.live_menu_fingerprint(user, texts)

    assert fingerprint(used_gb=31.4) == fingerprint()
    assert fingerprint(used_gb=31) != fingerprint(used_gb=32)
    assert fingerprint(balance_kopeks=125_100) != fingerprint()
    assert fingerprint(extra_days=1) != fingerprint()
    assert fingerprint(actual_status='expired') != fingerprint()
    assert fingerprint(device_limit=5) != fingerprint()
    assert fingerprint(tariff_id=2) != fingerprint()
    assert fingerprint(is_trial=True) != fingerprint()


async def test_sub_gigabyte_change_goes_to_db_without_edit(env):
    await _track(env)

    assert await live.refresh_live_menus(env.bot, _panel(int(31.4 * GIB))) is False

    statement = env.db.execute.await_args.args[0]
    # Полная синхронизация по updated_at решает, чьи поля новее, — фон его не двигает.
    assert 'updated_at=subscriptions.updated_at' in str(statement.compile())
    env.db.commit.assert_awaited_once()
    assert env.user.subscription.traffic_used_gb == pytest.approx(31.4)
    env.bot.assert_not_awaited()


async def test_subscription_changed_after_snapshot_keeps_db_value(env):
    """Сброс трафика коммитит БД до вызова панели: снимок, взятый чуть позже, ещё со старым расходом."""
    await _track(env)
    fetched_at = env.user.subscription.updated_at + timedelta(seconds=1)

    assert await live._refresh_one(env.bot, KEY, {501: 480 * GIB}, fetched_at) is None
    env.db.execute.assert_not_awaited()
    assert env.user.subscription.traffic_used_gb == 31.2
    env.bot.assert_not_awaited()


async def test_visible_change_edits_the_same_message(env):
    await _track(env)

    assert await live._refresh_one(env.bot, KEY, {501: 32 * GIB}, datetime.now(UTC)) == 'edited'

    env.bot.assert_awaited_once()
    request = env.bot.await_args.args[0]
    assert isinstance(request, EditMessageText)
    assert (request.chat_id, request.message_id, request.parse_mode) == (100, 42, None)
    fp = rich_menu.live_menu_fingerprint(env.user, get_texts('ru'))
    assert await env.cache.get(KEY) == {'m': 42, 'fp': fp}


async def test_multi_tariff_takes_traffic_by_subscription_panel_id(env, monkeypatch):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    env.user.subscription.remnawave_id = 777
    await _track(env)

    assert await live._refresh_one(env.bot, KEY, {501: 0, 777: 32 * GIB}, datetime.now(UTC)) == 'edited'
    assert env.user.subscription.traffic_used_gb == 32


async def test_new_menu_during_build_is_not_overwritten(env, monkeypatch):
    await _track(env)

    async def build_while_user_opens_menu(user, texts, db):
        await env.cache.set(KEY, {'m': 99, 'fp': 'new'})
        return '<p>menu</p>'

    monkeypatch.setattr(live, 'build_main_menu_rich_html', build_while_user_opens_menu)

    assert await live._refresh_one(env.bot, KEY, {501: 32 * GIB}, datetime.now(UTC)) is None
    env.bot.assert_not_awaited()
    assert await env.cache.get(KEY) == {'m': 99, 'fp': 'new'}


async def test_press_during_edit_is_not_undone(env):
    """Нажали на меню, пока фон ждал ответа Telegram: запись снимка не возвращает ключ."""
    await _track(env)

    async def edit_while_user_presses(request):
        env.cache.redis_client.data.pop(KEY)

    env.bot.side_effect = edit_while_user_presses

    assert await live._refresh_one(env.bot, KEY, {501: 32 * GIB}, datetime.now(UTC)) == 'edited'
    assert await env.cache.get(KEY) is None


@pytest.mark.parametrize(
    ('error', 'expected_result', 'kept'),
    [
        (TelegramBadRequest(method=None, message='Bad Request: message to edit not found'), 'dropped', False),
        (TelegramForbiddenError(method=None, message='Forbidden: bot was blocked by the user'), 'dropped', False),
        (TelegramBadRequest(method=None, message='Bad Request: message is not modified'), 'edited', True),
        ([TelegramBadRequest(method=None, message='Bad Request: RICH_MESSAGE_DATE_INVALID'), None], 'edited', True),
        (ClientDecodeError('bad rich block', ValueError('x'), {}), 'edited', True),  # правка дошла
    ],
)
async def test_telegram_errors(env, error, expected_result, kept):
    await _track(env)
    env.bot.side_effect = error

    assert await live._refresh_one(env.bot, KEY, {501: 32 * GIB}, datetime.now(UTC)) == expected_result

    fp = rich_menu.live_menu_fingerprint(env.user, get_texts('ru'))
    assert await env.cache.get(KEY) == ({'m': 42, 'fp': fp} if kept else None)
    # Удалённое сообщение — не повод выключать rich или логотип всему боту.
    assert rich_menu.is_rich_menu_enabled() is True


async def test_flood_limit_aborts_the_pass(env):
    await _track(env)
    await _track(env, key='live_menu:200')
    env.bot.side_effect = TelegramRetryAfter(method=SimpleNamespace(), message='flood', retry_after=3)

    assert await live.refresh_live_menus(env.bot, _panel(32 * GIB)) is True
    env.bot.assert_awaited_once()


_LOADS = {
    'panel_429': (RemnaWaveAPI, '_throttled_until', float('inf')),
    'db_pool': (live, '_pool_counters', lambda pool: {'checked_out': settings.DATABASE_POOL_SIZE}),
    'broadcast': (live.broadcast_service, '_tasks', {1: object()}),
    'webhook_queue': (
        TelegramWebhookProcessor,
        'active',
        SimpleNamespace(is_running=True, _queue=SimpleNamespace(qsize=lambda: 3)),
    ),
    'loop_lag': (live, '_loop_lag', AsyncMock(return_value=live.LAG_BUSY * 2)),
}


@pytest.mark.parametrize('load', list(_LOADS))
async def test_busy_bot_skips_the_pass(env, monkeypatch, load):
    await _track(env)
    monkeypatch.setattr(*_LOADS[load])
    panel = _panel(32 * GIB)

    assert await live.refresh_live_menus(env.bot, panel) is True
    panel.get_api_client.assert_not_called()
    env.bot.assert_not_awaited()


@pytest.mark.parametrize(('load', 'throttled'), [('loop_lag', True), ('disabled', False)])
async def test_load_or_switch_off_during_pass_stops_it(env, monkeypatch, load, throttled):
    """Нагрузка или выключение в кабинете посреди прохода: остальные меню не трогаем."""
    await _track(env)
    await _track(env, key='live_menu:200')
    lag_between_users = live.LAG_ABORT * 2 if load == 'loop_lag' else 0.0
    monkeypatch.setattr(live, '_loop_lag', AsyncMock(side_effect=[0.0] * 5 + [lag_between_users]))
    if load == 'disabled':
        env.bot.side_effect = lambda request: monkeypatch.setattr(settings, 'MAIN_MENU_LIVE_ENABLED', False)

    assert await live.refresh_live_menus(env.bot, _panel(32 * GIB)) is throttled
    env.bot.assert_awaited_once()


async def test_panel_down_still_redraws_from_db_but_backs_off(env):
    await _track(env)
    env.user.balance_kopeks = 100
    panel = SimpleNamespace(get_api_client=MagicMock(side_effect=RuntimeError('panel down')))

    assert await live.refresh_live_menus(env.bot, panel) is True
    env.bot.assert_awaited_once()


async def test_interval_backs_off_under_load_and_resets_when_idle(env, monkeypatch):
    monitoring = SimpleNamespace(is_running=True, bot=object(), subscription_service=object())
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)
        if len(sleeps) == 11:
            monitoring.is_running = False

    refresh = AsyncMock(side_effect=[True, RuntimeError('boom'), False] + [True] * 8)  # ошибка — как нагрузка
    monkeypatch.setattr(asyncio, 'sleep', fake_sleep)
    monkeypatch.setattr(live, 'refresh_live_menus', refresh)

    await live.live_menu_loop(monitoring)
    assert sleeps == [900, 1800, 3600, 900, 1800, 3600, 7200, 14400, 21600, 21600, 21600]
    # Не общий клиент мониторинга: его RemnaWaveAPI в async with подменяет сессию.
    assert refresh.await_args.args[1] is not monitoring.subscription_service

    monkeypatch.setattr(settings, 'MAIN_MENU_LIVE_ENABLED', False, raising=False)
    sleeps.clear()
    refresh.reset_mock()
    monitoring.is_running = True

    await live.live_menu_loop(monitoring)
    assert sleeps == [900] * 11
    refresh.assert_not_awaited()
