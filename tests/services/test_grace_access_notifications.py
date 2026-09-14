"""Уведомления о grace-доступе: админам в чат и человеку в бота.

Владелец (2026-09-14): «нужна уведа в боте для админов, что чел получил
грейс, закончился грейс — чтобы понимал админ, ибо выдача втухлую — это тупо».
До этого grace выдавался и закрывался молча: ни админ, ни человек не знали,
что доступ временный и только к Telegram.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database.models import Base, GraceAccessSessionModel, Subscription, Tariff, User
from app.services import grace_access_notifications as notify
from app.services.admin_notification_service import AdminNotificationService, NotificationCategory
from app.services.notification_delivery_service import notification_delivery_service
from app.services.notification_types import NotificationType
from tests.fixtures.sqlite_memory import ensure_real_aiosqlite


GIB = 1024**3
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


async def _seed(maker, *, state='active', reason='expired', completion_reason=None, last_error=None) -> None:
    async with maker() as db:
        db.add(
            User(
                id=1,
                telegram_id=1001,
                first_name='Иван',
                username='ivan',
                language='ru',
                status='active',
                balance_kopeks=0,
            )
        )
        db.add(
            Tariff(
                id=1,
                name='Стартовый',
                description='',
                is_active=True,
                traffic_limit_gb=50,
                device_limit=3,
                allowed_squads=['sq'],
                period_prices={'30': 10_000},
                display_order=1,
            )
        )
        await db.flush()
        db.add(
            Subscription(
                id=10,
                remnawave_short_id='s10',
                user_id=1,
                status='expired',
                is_trial=False,
                start_date=NOW - timedelta(days=31),
                end_date=NOW - timedelta(days=1),
                traffic_limit_gb=50,
                traffic_used_gb=5.0,
                device_limit=3,
                tariff_id=1,
                connected_squads=['sq'],
            )
        )
        await db.flush()
        db.add(
            GraceAccessSessionModel(
                id='11111111-2222-3333-4444-555555555555',
                subscription_id=10,
                remnawave_id=77,
                reason=reason,
                incident_key='expired:2026-09-13',
                state=state,
                snapshot_version=3,
                billing_before={},
                panel_before={'used_traffic_bytes': 5 * GIB},
                overlay={'traffic_limit_bytes': 6 * GIB, 'expire_at': (NOW + timedelta(hours=72)).isoformat()},
                started_at=NOW,
                grace_until=NOW + timedelta(hours=72),
                updated_at=NOW,
                completion_reason=completion_reason,
                completed_at=NOW if completion_reason else None,
                last_error=last_error,
                version=1,
            )
        )
        await db.commit()


@pytest_asyncio.fixture
async def lab(monkeypatch):
    ensure_real_aiosqlite(monkeypatch)
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=list(Base.metadata.sorted_tables)))
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(notify, 'AsyncSessionLocal', maker)
    admin_send = AsyncMock(return_value=True)
    user_send = AsyncMock(return_value=True)
    monkeypatch.setattr(AdminNotificationService, '_send_message', admin_send)
    monkeypatch.setattr(notification_delivery_service, 'send_notification', user_send)
    monkeypatch.setattr(settings, 'GRACE_ACCESS_NOTIFY_ADMINS', True)
    monkeypatch.setattr(settings, 'GRACE_ACCESS_NOTIFY_USER', True)
    monkeypatch.setattr(settings, 'GRACE_ACCESS_DURATION_HOURS', 72)
    monkeypatch.setattr(settings, 'GRACE_ACCESS_ALLOWED_SERVICES', 'Telegram и личный кабинет')
    # Подпись тарифа в тексте — правило мультитарифа, как у вебхуков.
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    try:
        yield SimpleNamespace(maker=maker, admin=admin_send, user=user_send, bot=SimpleNamespace())
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_granted_tells_admins_who_why_and_until_when(lab):
    await _seed(lab.maker)

    await notify.announce_grace_event(lab.bot, 10, 'granted')

    assert lab.admin.await_count == 1
    text = lab.admin.await_args.args[0]
    assert lab.admin.await_args.kwargs['category'] is NotificationCategory.RENEWALS
    assert 'GRACE-ДОСТУП ВЫДАН' in text
    assert 'Иван' in text and '1001' in text and 'ivan' in text
    assert 'Стартовый' in text
    assert 'срок подписки истёк' in text
    assert '1 ГБ' in text, 'квота = лимит оверлея минус уже израсходованное'
    assert '72 ч' in text


@pytest.mark.asyncio
async def test_granted_tells_the_user_in_their_language_with_a_renew_button(lab):
    await _seed(lab.maker)

    await notify.announce_grace_event(lab.bot, 10, 'granted')

    assert lab.user.await_count == 1
    kwargs = lab.user.await_args.kwargs
    assert kwargs['notification_type'] is NotificationType.GRACE_ACCESS_GRANTED
    assert kwargs['bot'] is lab.bot
    message = kwargs['telegram_message']
    assert 'Telegram и личный кабинет' in message, 'что доступно — фраза оператора, не прибитый Telegram'
    assert '72' in message and '1 ГБ' in message and '«Стартовый»' in message
    assert '{' not in message, 'все подстановки заполнены'
    email = kwargs['context']
    assert email['allowed'] == 'Telegram и личный кабинет' and email['reason'] == 'expired'
    assert email['tariff_name'] == 'Стартовый' and email['hours'] == 72
    buttons = [button.text for row in kwargs['telegram_markup'].inline_keyboard for button in row]
    assert any('родл' in label.lower() for label in buttons), buttons


@pytest.mark.asyncio
async def test_limited_reason_is_named_as_traffic(lab):
    await _seed(lab.maker, reason='limited')

    await notify.announce_grace_event(lab.bot, 10, 'granted')

    assert 'исчерпан трафик' in lab.admin.await_args.args[0]
    assert 'Трафик' in lab.user.await_args.kwargs['telegram_message']


@pytest.mark.asyncio
async def test_timeout_tells_admins_and_the_user_that_access_is_closed(lab):
    await _seed(lab.maker, state='completed', completion_reason='timeout')

    await notify.announce_grace_event(lab.bot, 10, 'ended')

    text = lab.admin.await_args.args[0]
    assert 'GRACE-ДОСТУП ЗАВЕРШЁН' in text
    assert 'не продлили' in text
    assert lab.user.await_args.kwargs['notification_type'] is NotificationType.GRACE_ACCESS_ENDED
    assert 'закончился' in lab.user.await_args.kwargs['telegram_message']


@pytest.mark.asyncio
async def test_paid_ending_is_reported_to_admins_only(lab):
    """О продлении человек уже получил своё уведомление — второе было бы шумом."""
    await _seed(lab.maker, state='completed', completion_reason='paid')

    await notify.announce_grace_event(lab.bot, 10, 'ended')

    assert 'продлил' in lab.admin.await_args.args[0]
    assert lab.user.await_count == 0


@pytest.mark.asyncio
async def test_conflict_ending_carries_the_error_text(lab):
    await _seed(
        lab.maker, state='completed', completion_reason='conflict', last_error='Remnawave changed outside grace'
    )

    await notify.announce_grace_event(lab.bot, 10, 'ended')

    text = lab.admin.await_args.args[0]
    assert 'конфликт' in text.lower() and 'Remnawave changed outside grace' in text
    assert lab.user.await_count == 0


@pytest.mark.asyncio
async def test_switches_silence_each_audience_separately(lab, monkeypatch):
    await _seed(lab.maker)

    monkeypatch.setattr(settings, 'GRACE_ACCESS_NOTIFY_ADMINS', False)
    await notify.announce_grace_event(lab.bot, 10, 'granted')
    assert lab.admin.await_count == 0 and lab.user.await_count == 1

    monkeypatch.setattr(settings, 'GRACE_ACCESS_NOTIFY_ADMINS', True)
    monkeypatch.setattr(settings, 'GRACE_ACCESS_NOTIFY_USER', False)
    await notify.announce_grace_event(lab.bot, 10, 'granted')
    assert lab.admin.await_count == 1 and lab.user.await_count == 1


@pytest.mark.asyncio
async def test_without_a_bot_or_a_session_nothing_is_sent_and_nothing_raises(lab):
    await notify.announce_grace_event(None, 10, 'granted')
    await notify.announce_grace_event(lab.bot, 10, 'granted')  # ни подписки, ни сессии в базе

    assert lab.admin.await_count == 0 and lab.user.await_count == 0


@pytest.mark.asyncio
async def test_delivery_failure_never_reaches_the_caller(lab):
    await _seed(lab.maker)
    lab.admin.side_effect = RuntimeError('telegram down')

    await notify.announce_grace_event(lab.bot, 10, 'granted')

    assert lab.user.await_count == 1, 'сбой одного канала не глушит другой'


@pytest.mark.asyncio
async def test_operator_phrase_and_tariff_are_escaped_for_telegram_markup(lab, monkeypatch):
    """Сообщение — HTML Telegram: угловые скобки из настройки ломали бы разметку."""
    await _seed(lab.maker)
    monkeypatch.setattr(settings, 'GRACE_ACCESS_ALLOWED_SERVICES', 'Telegram <b>и кабинет</b>')

    await notify.announce_grace_event(lab.bot, 10, 'granted')

    message = lab.user.await_args.kwargs['telegram_message']
    assert '&lt;b&gt;и кабинет&lt;/b&gt;' in message
    assert message.count('<b>') == 1, 'жирным остаётся только наш заголовок'
    assert lab.user.await_args.kwargs['context']['allowed'] == 'Telegram <b>и кабинет</b>', (
        'письму — сырое, оно экранирует само'
    )


@pytest.mark.asyncio
async def test_empty_phrase_falls_back_to_telegram(lab, monkeypatch):
    await _seed(lab.maker)
    monkeypatch.setattr(settings, 'GRACE_ACCESS_ALLOWED_SERVICES', '   ')

    await notify.announce_grace_event(lab.bot, 10, 'granted')

    assert 'только: Telegram.' in lab.user.await_args.kwargs['telegram_message']


@pytest.mark.asyncio
async def test_admins_see_the_operator_phrase_not_a_hardcoded_telegram(lab):
    """Стенд 2026-09-14: человеку писали «Telegram и личный кабинет», админу — «только Telegram»."""
    await _seed(lab.maker)

    await notify.announce_grace_event(lab.bot, 10, 'granted')

    text = lab.admin.await_args.args[0]
    assert 'Telegram и личный кабинет' in text
    assert 'только Telegram,' not in text


@pytest.mark.parametrize('language', ['ru', 'en', 'zh', 'ua', 'fa'])
@pytest.mark.parametrize('key', ['GRACE_ACCESS_GRANTED_EXPIRED', 'GRACE_ACCESS_GRANTED_LIMITED', 'GRACE_ACCESS_ENDED'])
def test_operator_phrase_stands_in_a_case_neutral_slot(language, key):
    """Фразу «что доступно» пишет оператор в именительном падеже, склонять её нельзя.

    Стенд 2026-09-14: «доступ только к Telegram и личный кабинет». Слот после
    двоеточия подходит любой фразе на любом языке.
    """
    import json
    import re
    from pathlib import Path

    template = json.loads((Path('app/localization/locales') / f'{language}.json').read_text(encoding='utf-8'))[key]

    assert re.search(r'[:：]\s*\{allowed\}', template), f'{language}.{key}: {{allowed}} должен стоять после двоеточия'
