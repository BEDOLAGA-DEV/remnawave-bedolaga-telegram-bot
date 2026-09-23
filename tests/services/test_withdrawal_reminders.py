"""Напоминания о заявках на вывод, оставшихся без решения.

У тикетов поддержки есть SLA-напоминалка («⏰ Ожидание ответа на тикет
превышено»), у заявок на вывод реферального баланса до этого было только разовое
уведомление при создании: заявка, которую никто не открыл, терялась в потоке.
Механика повторяет тикеты — заявка в статусе pending старше лимита получает
напоминание, повтор не раньше кулдауна, любое решение по заявке (статус не
pending) напоминания прекращает.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import (
    MonitoringLog,
    User,
    UserStatus,
    WithdrawalRequest,
    WithdrawalRequestStatus,
)
from app.services import admin_notification_service as ans, monitoring_service as ms
from tests.fixtures.sqlite_memory import memory_session


TABLES = (User.__table__, WithdrawalRequest.__table__, MonitoringLog.__table__)


@pytest.fixture(autouse=True)
def reminder_settings(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_WITHDRAWAL_REMINDER_ENABLED', True)
    monkeypatch.setattr(settings, 'REFERRAL_WITHDRAWAL_REMINDER_MINUTES', 60)
    monkeypatch.setattr(settings, 'REFERRAL_WITHDRAWAL_REMINDER_COOLDOWN_MINUTES', 30)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', True)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', -100123)


@pytest.fixture
def sent(monkeypatch) -> AsyncMock:
    mock = AsyncMock(return_value=True)
    monkeypatch.setattr(ans.AdminNotificationService, 'send_withdrawal_pending_reminder', mock)
    return mock


def _user(telegram_id: int = 1001) -> User:
    return User(
        telegram_id=telegram_id,
        username='partner',
        first_name='Partner',
        status=UserStatus.ACTIVE.value,
        language='ru',
        balance_kopeks=0,
    )


async def _seed(db, *, age_minutes: int, status: str = WithdrawalRequestStatus.PENDING.value, **extra):
    user = _user()
    db.add(user)
    await db.flush()
    request = WithdrawalRequest(
        user_id=user.id,
        amount_kopeks=150000,
        status=status,
        payment_details='card 1234',
        created_at=datetime.now(UTC) - timedelta(minutes=age_minutes),
        **extra,
    )
    db.add(request)
    await db.commit()
    return request


async def _run(db) -> int:
    svc = ms.MonitoringService(bot=object())
    return await svc._check_withdrawal_reminders(db)


@pytest.mark.asyncio
async def test_stale_pending_request_gets_reminder(monkeypatch, sent):
    async with memory_session(monkeypatch, TABLES) as db:
        request = await _seed(db, age_minutes=90)

        assert await _run(db) == 1

        sent.assert_awaited_once()
        reminded, waited_minutes = sent.await_args.args
        assert reminded.id == request.id
        assert 89 <= waited_minutes <= 91, 'в напоминании — реальное время ожидания'
        await db.refresh(request)
        assert request.last_reminder_at is not None, 'отметка нужна для кулдауна'
        events = (await db.execute(select(MonitoringLog))).scalars().all()
        assert [e.event_type for e in events] == ['withdrawal_reminders_sent']


@pytest.mark.asyncio
async def test_fresh_request_waits_out_the_limit(monkeypatch, sent):
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, age_minutes=10)
        assert await _run(db) == 0
        sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_decided_request_is_silent(monkeypatch, sent):
    """Любой статус, кроме pending, — решение принято, напоминать не о чем."""
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, age_minutes=90, status=WithdrawalRequestStatus.APPROVED.value)
        assert await _run(db) == 0
        sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_cooldown_gates_repeats(monkeypatch, sent):
    now = datetime.now(UTC)
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, age_minutes=90, last_reminder_at=now - timedelta(minutes=10))
        assert await _run(db) == 0, 'кулдаун 30 мин ещё не вышел'
        sent.assert_not_awaited()

    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, age_minutes=90, last_reminder_at=now - timedelta(minutes=40))
        assert await _run(db) == 1, 'кулдаун вышел — напоминаем снова'


@pytest.mark.asyncio
async def test_disabled_flag_silences_everything(monkeypatch, sent):
    monkeypatch.setattr(settings, 'REFERRAL_WITHDRAWAL_REMINDER_ENABLED', False)
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, age_minutes=90)
        assert await _run(db) == 0
        sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_send_keeps_request_for_next_round(monkeypatch, sent):
    """Не ушло — отметку не ставим, следующий круг попробует снова."""
    sent.return_value = False
    async with memory_session(monkeypatch, TABLES) as db:
        request = await _seed(db, age_minutes=90)
        assert await _run(db) == 0
        await db.refresh(request)
        assert request.last_reminder_at is None


# --- Маршрут и кнопки самого напоминания -------------------------------------


def _request(telegram_id: int | None = 777) -> SimpleNamespace:
    return SimpleNamespace(
        id=5,
        user_id=42,
        amount_kopeks=150000,
        user=SimpleNamespace(id=42, first_name='Ann', username='ann', telegram_id=telegram_id, email=None),
    )


@pytest.fixture
def plain_sender(monkeypatch):
    """Rich-рендер выключен: проверяем классический send_message с thread_id."""
    monkeypatch.setattr(ans, 'try_send_rich_admin_message', AsyncMock(return_value=False))
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', True)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', -100123)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_TOPIC_ID', None)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_PARTNERS_TOPIC_ID', 11)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_PARTNERS_ENABLED', True)


@pytest.mark.asyncio
async def test_reminder_goes_to_withdrawal_topic_when_configured(monkeypatch, plain_sender):
    monkeypatch.setattr(settings, 'REFERRAL_WITHDRAWAL_NOTIFICATIONS_TOPIC_ID', 22)
    bot = SimpleNamespace(send_message=AsyncMock())

    assert await ans.AdminNotificationService(bot).send_withdrawal_pending_reminder(_request(), 125) is True

    kwargs = bot.send_message.await_args.kwargs
    assert kwargs['message_thread_id'] == 22, 'топик заявок на вывод важнее топика категории'
    assert 'Заявка на вывод ждёт решения' in kwargs['text']
    assert '2 ч 5 мин' in kwargs['text']
    # Групповой админ-чат: только действия — профиль там не открыть.
    callbacks = [b.callback_data for row in kwargs['reply_markup'].inline_keyboard for b in row]
    assert callbacks == ['admin_withdrawal_approve_5', 'admin_withdrawal_reject_5']


@pytest.mark.asyncio
async def test_reminder_falls_back_to_partners_topic(monkeypatch, plain_sender):
    monkeypatch.setattr(settings, 'REFERRAL_WITHDRAWAL_NOTIFICATIONS_TOPIC_ID', None)
    bot = SimpleNamespace(send_message=AsyncMock())

    assert await ans.AdminNotificationService(bot).send_withdrawal_pending_reminder(_request(None), 5) is True

    kwargs = bot.send_message.await_args.kwargs
    assert kwargs['message_thread_id'] == 11
    callbacks = [b.callback_data for row in kwargs['reply_markup'].inline_keyboard for b in row]
    assert callbacks == ['admin_withdrawal_approve_5', 'admin_withdrawal_reject_5'], 'без telegram_id нет профиля'


@pytest.mark.parametrize(
    ('role', 'expected'),
    [
        # Профиль — по id из базы: у admin_user_<telegram_id> обработчика нет.
        ('admin', ['admin_withdrawal_approve_5', 'admin_withdrawal_reject_5', 'admin_user_manage_42']),
        ('group', ['admin_withdrawal_approve_5', 'admin_withdrawal_reject_5']),
        # Одобрение только для админа: модератору кнопки ответили бы «нет доступа».
        ('moderator', []),
    ],
)
@pytest.mark.asyncio
async def test_reminder_buttons_match_the_original_notification(monkeypatch, plain_sender, role, expected):
    monkeypatch.setattr(settings, 'REFERRAL_WITHDRAWAL_NOTIFICATIONS_TOPIC_ID', None)
    monkeypatch.setattr(ans.AdminNotificationService, 'resolve_recipient_role', lambda self: role)
    bot = SimpleNamespace(send_message=AsyncMock())

    assert await ans.AdminNotificationService(bot).send_withdrawal_pending_reminder(_request(), 30) is True

    markup = bot.send_message.await_args.kwargs.get('reply_markup')
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row] if markup else []
    assert callbacks == expected


@pytest.mark.parametrize(
    ('minutes', 'expected'),
    [(15, '15 мин'), (60, '1 ч'), (125, '2 ч 5 мин'), (1440, '1 д'), (2940, '2 д 1 ч')],
)
def test_waiting_time_reads_naturally(minutes, expected):
    """Двое суток ожидания — «2 д 1 ч», а не «49 ч 0 мин»."""
    assert ans._format_waiting(minutes) == expected
