"""«Активность» — полный след пользователя: каждый экран и каждое действие.

Повторная жалоба владельца: у новых людей в «Активности» одни события подписки.
Живой замер на проде (30 последних пользователей): действия в кабинете есть у
двоих, клики в боте у двоих, Mini App — ноль. Главное меню бота = кабинет, а
кабинет писал ТОЛЬКО изменения (покупка, триал, пополнение): открыл подписку,
скопировал ключ, прошёлся по экранам — ни одной записи. Решение владельца:
«активность создана, чтобы видеть каждый чих — всё, что делает юзер и в боте,
и в кабинете».

Что держат тесты ниже:

* кабинет сообщает об открытии каждого экрана — запись ``SCREEN <путь>``;
  секретные сегменты (токены купонов, подарков, слияния) в журнал не попадают;
* Mini App пишет и просмотры экранов, а не только действия; повтор того же
  экрана в пределах минуты — одна запись (опрос статуса платежа — не сессия);
* кабинет и Mini App двигают ``User.last_activity`` — по ней карточка
  показывает «последнюю активность», а сторож удаляет «неактивных»;
* клик в боте ищет пользователя по Telegram ID, запись из веба — по внутреннему
  id; раньше одно число искали в обеих колонках без порядка;
* оплата Stars в боте (сообщение successful_payment) — тоже действие;
* фоновые записи держатся сильными ссылками (цикл событий вправе потерять
  задачу без них).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.config import settings
from app.services import user_action_log_service as log_module
from app.services.user_action_log_service import (
    ACTIVITY_TOUCH_INTERVAL,
    CABINET_BUTTON_TYPE,
    MINIAPP_BUTTON_TYPE,
    SCREEN_PREFIX,
    mark_user_seen,
    normalize_screen_path,
    remember_task,
    schedule_screen_view_log,
)


NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _fresh_dedup(monkeypatch):
    """Окно дедупликации — процессное состояние; тесты не должны видеть друг друга."""
    monkeypatch.setattr(settings, 'USER_ACTION_LOG_ENABLED', True, raising=False)
    log_module._recent_screens.clear()


@pytest.fixture
def spawned(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr(log_module, '_spawn', lambda **kwargs: calls.append(kwargs))
    return calls


# ---------------------------------------------------------------------------
# Пути экранов: числа и секреты маскируются, публичные слаги остаются
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('/subscription', '/subscription'),
        ('/subscriptions/42/renew', '/subscriptions/{id}/renew'),
        ('/info/faq', '/info/faq'),
        ('/news/how-to-connect', '/news/how-to-connect'),
        ('/coupon/AbCdEfGh12345678', '/coupon/{token}'),
        ('/buy/gift/9f1c2b3a4d5e6f7a8b9c', '/buy/gift/{token}'),
        ('/buy/success/tok_0123456789abcdef', '/buy/success/{token}'),
        ('/merge/anything-here', '/merge/{token}'),
        ('/auto-login/abc', '/auto-login/{token}'),
        ('/verify-email/x', '/verify-email/{token}'),
        ('/reset-password/x', '/reset-password/{token}'),
        ('/balance/top-up/result/yookassa', '/balance/top-up/result/yookassa'),
        ('/profile/', '/profile'),
    ],
)
def test_normalize_screen_path(raw, expected):
    assert normalize_screen_path(raw) == expected


# ---------------------------------------------------------------------------
# Экран кабинета: одна запись на открытие, повтор в окне — не пишется
# ---------------------------------------------------------------------------


def test_screen_view_is_logged_with_prefix(spawned):
    schedule_screen_view_log(7, '/subscriptions/42')

    assert spawned == [
        {
            'user_id': 7,
            'button_id': f'{SCREEN_PREFIX}/subscriptions/{{id}}',
            'callback_data': '/subscriptions/{id}',
            'button_type': CABINET_BUTTON_TYPE,
        }
    ]


def test_screen_view_secret_never_reaches_the_log(spawned):
    schedule_screen_view_log(7, '/coupon/SECRET-TOKEN-1234567')

    assert 'SECRET' not in str(spawned)
    assert spawned[0]['button_id'] == f'{SCREEN_PREFIX}/coupon/{{token}}'


def test_same_screen_within_window_is_one_record(spawned, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(log_module, '_monotonic', lambda: clock[0])

    schedule_screen_view_log(7, '/subscription')
    clock[0] += 5
    schedule_screen_view_log(7, '/subscription')
    clock[0] += log_module.SCREEN_DEDUP_SECONDS + 1
    schedule_screen_view_log(7, '/subscription')

    assert len(spawned) == 2


def test_dedup_is_per_user_and_per_screen(spawned):
    schedule_screen_view_log(7, '/subscription')
    schedule_screen_view_log(7, '/balance')
    schedule_screen_view_log(8, '/subscription')

    assert len(spawned) == 3


def test_screen_view_is_gated_by_flag(spawned, monkeypatch):
    monkeypatch.setattr(settings, 'USER_ACTION_LOG_ENABLED', False, raising=False)

    schedule_screen_view_log(7, '/subscription')

    assert spawned == []


def test_dedup_memory_is_bounded(monkeypatch, spawned):
    monkeypatch.setattr(log_module, 'SCREEN_DEDUP_MAX_ENTRIES', 50)
    for user_id in range(120):
        schedule_screen_view_log(user_id, '/subscription')

    assert len(log_module._recent_screens) <= 50
    assert len(spawned) == 120


def test_activity_route_itself_is_not_logged_as_an_action():
    """Иначе каждый экран давал бы две записи: экран и «POST /cabinet/activity/screen»."""
    assert log_module.should_log_cabinet_action('POST', '/cabinet/activity/screen') is False


# ---------------------------------------------------------------------------
# Mini App: просмотры пишутся как экраны, действия — как действия
# ---------------------------------------------------------------------------


def test_miniapp_read_is_logged_as_screen(spawned):
    log_module.schedule_miniapp_action_log(7, '/miniapp/subscription')

    assert spawned == [
        {
            'user_id': 7,
            'button_id': f'{SCREEN_PREFIX}/miniapp/subscription',
            'callback_data': '/miniapp/subscription',
            'button_type': MINIAPP_BUTTON_TYPE,
        }
    ]


def test_miniapp_action_is_still_an_action(spawned):
    log_module.schedule_miniapp_action_log(7, '/miniapp/subscription/purchase')

    assert spawned[0]['button_id'] == 'POST /miniapp/subscription/purchase'


def test_miniapp_status_polling_is_one_screen(spawned):
    for _ in range(5):
        log_module.schedule_miniapp_action_log(7, '/miniapp/payments/status')

    assert len(spawned) == 1


def test_miniapp_unknown_path_is_ignored(spawned):
    log_module.schedule_miniapp_action_log(7, '/miniapp/whatever')

    assert spawned == []


# ---------------------------------------------------------------------------
# last_activity: кабинет и Mini App тоже «активность»
# ---------------------------------------------------------------------------


def _user(last_activity: datetime | None) -> SimpleNamespace:
    return SimpleNamespace(id=7, last_activity=last_activity)


def test_stale_user_is_bumped():
    user = _user(NOW - timedelta(days=3))

    assert mark_user_seen(user, now=NOW) is True
    assert user.last_activity == NOW


def test_fresh_user_is_left_alone():
    seen = NOW - ACTIVITY_TOUCH_INTERVAL + timedelta(seconds=1)
    user = _user(seen)

    assert mark_user_seen(user, now=NOW) is False
    assert user.last_activity == seen


def test_never_seen_user_is_bumped():
    assert mark_user_seen(_user(None), now=NOW) is True


def test_naive_timestamp_is_treated_as_utc():
    """Старые строки без таймзоны не должны ронять авторизацию сравнением naive/aware."""
    user = _user((NOW - timedelta(days=1)).replace(tzinfo=None))

    assert mark_user_seen(user, now=NOW) is True


@pytest.mark.asyncio
async def test_cabinet_dependency_touches_last_activity():
    from app.cabinet.dependencies import get_current_cabinet_user

    user = SimpleNamespace(
        id=100,
        telegram_id=555,
        username='u',
        email=None,
        email_verified=False,
        status='active',
        balance_kopeks=0,
        last_activity=NOW - timedelta(days=40),
        updated_at=NOW - timedelta(days=40),
        cabinet_last_login=None,
        referral_code='abc',
        referred_by_id=None,
        remnawave_uuid=None,
    )
    db = AsyncMock()
    request = MagicMock()
    request.headers.get = MagicMock(return_value=None)
    request.method = 'GET'
    request.url.path = '/cabinet/subscription'

    with (
        patch('app.cabinet.dependencies.get_token_payload', return_value={'sub': '100', 'type': 'access'}),
        patch('app.cabinet.dependencies.get_user_by_id', AsyncMock(return_value=user)),
        patch('app.cabinet.dependencies.blacklist_service.is_user_blacklisted', AsyncMock(return_value=(False, None))),
        patch('app.cabinet.dependencies.maintenance_service.is_maintenance_active', return_value=False),
        patch('app.cabinet.dependencies.settings.CHANNEL_IS_REQUIRED_SUB', False, create=True),
    ):
        await get_current_cabinet_user(request=request, credentials=MagicMock(credentials='t'), db=db)

    assert datetime.now(UTC) - user.last_activity < timedelta(minutes=1), (
        'просмотр подписки в кабинете — тоже активность'
    )
    assert user.cabinet_last_login is not None
    db.commit.assert_awaited()


class _SessionProxy:
    """`async with AsyncSessionLocal() as db` поверх уже открытой тестовой сессии."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_miniapp_auth_touches_last_activity_and_logs_the_screen(monkeypatch):
    from app.database.models import Base, ButtonClickLog, User
    from app.webapi.routes import miniapp
    from tests.fixtures.sqlite_memory import memory_session

    monkeypatch.setattr(miniapp, 'parse_webapp_init_data', lambda init_data, token: {'user': {'id': 555000111}})

    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        stale = datetime.now(UTC) - timedelta(days=3)
        db.add(
            User(
                id=7,
                telegram_id=555000111,
                first_name='U',
                status='active',
                language='ru',
                balance_kopeks=0,
                created_at=stale,
                last_activity=stale,
            )
        )
        await db.commit()
        # Фоновая запись открывает СВОЮ сессию к боевой базе — подменяем на эту.
        monkeypatch.setattr(log_module, 'AsyncSessionLocal', lambda: _SessionProxy(db))

        token = log_module.bind_request_path('/miniapp/subscription')
        try:
            user = await miniapp._authorize_miniapp_user('stub', db)
        finally:
            log_module.reset_request_path(token)
        await log_module.drain_pending_actions()

        db.expire_all()
        stored = await db.get(User, 7)
        rows = (await db.execute(select(ButtonClickLog))).scalars().all()

    assert user.id == 7
    assert stored.last_activity > stale + timedelta(days=2)
    assert [(row.button_id, row.button_type, row.user_id) for row in rows] == [
        (f'{SCREEN_PREFIX}/miniapp/subscription', MINIAPP_BUTTON_TYPE, 7)
    ]


# ---------------------------------------------------------------------------
# Роут кабинета: экран принимается, мусор — нет
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_screen_route_accepts_path_and_rejects_garbage(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.cabinet.dependencies import get_current_cabinet_user
    from app.cabinet.routes import activity as route

    seen: list[tuple[int, str]] = []
    monkeypatch.setattr(route, 'schedule_screen_view_log', lambda user_id, path: seen.append((user_id, path)))

    app = FastAPI()
    app.include_router(route.router, prefix='/cabinet')
    app.dependency_overrides[get_current_cabinet_user] = lambda: SimpleNamespace(id=7)

    with TestClient(app) as http:
        ok = http.post('/cabinet/activity/screen', json={'path': '/subscriptions/42'})
        with_query = http.post('/cabinet/activity/screen', json={'path': '/subscription?token=x'})
        relative = http.post('/cabinet/activity/screen', json={'path': 'subscription'})
        too_long = http.post('/cabinet/activity/screen', json={'path': '/' + 'a' * 300})

    assert ok.status_code == 204, ok.text
    assert with_query.status_code == 422
    assert relative.status_code == 422
    assert too_long.status_code == 422
    assert seen == [(7, '/subscriptions/42')]


# ---------------------------------------------------------------------------
# Таймлайн: экран показывается как экран, а не как «POST …»
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeline_marks_screens_and_payments(monkeypatch):
    from app.cabinet.routes.admin_users import get_user_activity
    from app.database.models import Base, ButtonClickLog, User
    from tests.fixtures.sqlite_memory import memory_session

    admin = SimpleNamespace(id=1, telegram_id=1)
    now = datetime.now(UTC)

    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        db.add(
            User(id=7, telegram_id=1, first_name='U', status='active', language='ru', balance_kopeks=0, created_at=now)
        )
        db.add_all(
            [
                ButtonClickLog(
                    button_id=f'{SCREEN_PREFIX}/subscription',
                    user_id=7,
                    button_type=CABINET_BUTTON_TYPE,
                    clicked_at=now,
                ),
                ButtonClickLog(
                    button_id=f'{SCREEN_PREFIX}/miniapp/subscription',
                    user_id=7,
                    button_type=MINIAPP_BUTTON_TYPE,
                    clicked_at=now,
                ),
                ButtonClickLog(
                    button_id='POST /cabinet/subscription/trial',
                    user_id=7,
                    callback_data='/cabinet/subscription/trial',
                    button_type=CABINET_BUTTON_TYPE,
                    clicked_at=now,
                ),
                ButtonClickLog(button_id='successful_payment', user_id=7, button_type='payment', clicked_at=now),
            ]
        )
        await db.commit()

        response = await get_user_activity(user_id=7, offset=0, limit=50, types=None, admin=admin, db=db)

    shaped = {(item.type, item.subtype, item.title) for item in response.items}
    assert shaped == {
        ('button_click', 'payment', 'successful_payment'),
        ('cabinet_action', None, 'POST /cabinet/subscription/trial'),
        ('cabinet_action', 'screen', '/subscription'),
        ('miniapp_action', 'screen', '/miniapp/subscription'),
    }


# ---------------------------------------------------------------------------
# Клик в боте: Telegram ID не путается с внутренним id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bot_click_is_attributed_by_telegram_id(monkeypatch):
    from app.database.models import Base, ButtonClickLog, User
    from app.services.menu_layout.stats_service import MenuLayoutStatsService
    from tests.fixtures.sqlite_memory import memory_session

    now = datetime.now(UTC)
    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        # Коллизия чисел: внутренний id одного равен Telegram ID другого.
        db.add_all(
            [
                User(
                    id=555,
                    telegram_id=111,
                    first_name='A',
                    status='active',
                    language='ru',
                    balance_kopeks=0,
                    created_at=now,
                ),
                User(
                    id=1,
                    telegram_id=555,
                    first_name='B',
                    status='active',
                    language='ru',
                    balance_kopeks=0,
                    created_at=now,
                ),
            ]
        )
        await db.commit()

        by_telegram = await MenuLayoutStatsService.log_button_click(db, 'menu_subscription', telegram_id=555)
        by_internal = await MenuLayoutStatsService.log_button_click(db, 'POST /cabinet/x', user_id=555)
        unknown = await MenuLayoutStatsService.log_button_click(db, 'menu_subscription', telegram_id=999)

        rows = (await db.execute(select(ButtonClickLog))).scalars().all()

    assert by_telegram.user_id == 1, 'клик из бота — по Telegram ID'
    assert by_internal.user_id == 555, 'запись из кабинета — по внутреннему id'
    assert unknown.user_id is None
    assert len(rows) == 3


def test_middleware_passes_telegram_id_and_keeps_tasks():
    """Пин: middleware зовёт запись с telegram_id и держит фоновую задачу сильной ссылкой."""
    source = (Path(__file__).resolve().parents[2] / 'app' / 'middlewares' / 'button_stats.py').read_text(
        encoding='utf-8'
    )
    assert 'telegram_id=user_id' in source
    assert source.count('remember_task(') >= 2


def test_stars_payment_message_is_logged(monkeypatch):
    from app.middlewares.button_stats import ButtonStatsMiddleware

    middleware = ButtonStatsMiddleware()
    calls: list[dict] = []

    def fake_log(**kwargs):
        calls.append(kwargs)

        async def _noop():
            return None

        return _noop()

    monkeypatch.setattr(middleware, '_log_button_click_async', fake_log)
    message = SimpleNamespace(text=None, successful_payment=object(), from_user=SimpleNamespace(id=922920255))

    with patch('app.middlewares.button_stats.asyncio.create_task', MagicMock(side_effect=lambda coro: coro.close())):
        middleware._log_command(message)

    assert calls == [
        {
            'button_id': 'successful_payment',
            'user_id': 922920255,
            'callback_data': None,
            'button_type': 'payment',
            'button_text': None,
        }
    ]


# ---------------------------------------------------------------------------
# Сильные ссылки на фоновые задачи
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remember_task_holds_until_done():
    started = asyncio.Event()

    async def _work():
        await started.wait()

    task = asyncio.create_task(_work())
    remember_task(task)
    assert task in log_module._pending_actions

    started.set()
    await log_module.drain_pending_actions()
    assert task not in log_module._pending_actions


def test_remember_task_ignores_non_tasks():
    """Тесты middleware подменяют create_task заглушкой, возвращающей None."""
    remember_task(None)
