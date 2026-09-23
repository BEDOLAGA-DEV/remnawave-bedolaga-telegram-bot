"""Стартовое уведомление: сводка, по которой админ сразу видит состояние проекта.

Раньше «Триальных подписок» считало все триалы за всю историю (2 742 при 4 558
пользователях), сумма балансов шла как «24.0K RUB», а о том, что панель лежит или
включены техработы, сообщала только иконка в конце свёрнутой цитаты.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from unittest.mock import AsyncMock

import pytest

from app.database.models import Base, Subscription, SubscriptionStatus, Transaction, TransactionType, User
from app.services import startup_notification_service as startup
from app.services.startup_notification_service import (
    StartupNotificationService,
    _StartupStats,
    render_startup_message,
)
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)
# Классический parse_mode=HTML Telegram: других тегов он не примет.
TELEGRAM_TAGS = {'b', 'i', 'code', 'blockquote'}


def _stats(**changes) -> _StartupStats:
    base = {
        'version': '4.14.0',
        'users': 4558,
        'users_new': 37,
        'paid': 75,
        'trials': 312,
        'deposits_kopeks': 345_000,
        'balances_kopeks': 2_401_250,
        'open_tickets': 0,
        'panel_connected': True,
        'panel_status': 'на связи',
        'panel_latency_ms': 142,
        'maintenance': False,
        'sales_mode': 'multi_tariff',
    }
    return _StartupStats(**(base | changes))


class _TagChecker(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.unknown: set[str] = set()

    def handle_starttag(self, tag, attrs):
        if tag not in TELEGRAM_TAGS:
            self.unknown.add(tag)
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack[-1] == tag, f'незакрытый или лишний </{tag}>'
        self.stack.pop()


def _assert_valid_telegram_html(text: str) -> None:
    checker = _TagChecker()
    checker.feed(text)
    assert not checker.unknown, f'Telegram не знает тегов {checker.unknown}'
    assert not checker.stack, f'не закрыты {checker.stack}'


def test_healthy_start_shows_every_section_and_no_warnings():
    text = render_startup_message(_stats(), timestamp='22.09.2026 12:56:56')

    _assert_valid_telegram_html(text)
    assert '<code>v4.14.0</code>' in text
    assert '4 558' in text
    assert '+37' in text
    assert '3 450 ₽' in text
    assert '24 012 ₽' in text, 'баланс — рублями целиком, а не «24.0K RUB»'
    assert '142 мс' in text
    assert 'Режим продаж: мультитариф' in text
    assert 'Требует внимания' not in text


@pytest.mark.parametrize(('mode', 'label'), [('classic', 'классика'), ('tariffs', 'тарифы')])
def test_sales_mode_is_named(mode, label):
    assert f'Режим продаж: {label}' in render_startup_message(_stats(sales_mode=mode), timestamp='t')


def test_problems_are_called_out_explicitly():
    stats = _stats(
        panel_connected=False, panel_status='недоступна', panel_latency_ms=None, maintenance=True, open_tickets=3
    )

    text = render_startup_message(stats, timestamp='t')

    _assert_valid_telegram_html(text)
    assert '⚠️ <b>Требует внимания</b>' in text
    assert 'Панель Remnawave не отвечает' in text
    assert 'режим техработ' in text
    assert 'Открытых тикетов ждут ответа: 3' in text
    assert '🔴 Панель недоступна' in text


def test_metric_that_failed_is_a_dash_not_a_zero():
    text = render_startup_message(_stats(users=None, deposits_kopeks=None, users_new=None), timestamp='t')

    assert 'Всего: <b>—</b>' in text
    assert 'Пополнения за сутки: <b>—</b>' in text
    assert 'Новых за сутки: <b>—</b>' in text


def test_version_and_panel_status_are_escaped():
    text = render_startup_message(_stats(version='<x>', panel_status='a&b'), timestamp='<t>')

    _assert_valid_telegram_html(text)
    assert '&lt;x&gt;' in text


@pytest.mark.asyncio
async def test_counts_only_live_subscriptions_and_last_day_deposits(monkeypatch):
    now = datetime.now(UTC)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                User(id=1, telegram_id=1, first_name='A', language='ru', status='active', balance_kopeks=10_050),
                User(id=2, telegram_id=2, first_name='B', language='ru', status='active', balance_kopeks=0),
                User(id=3, telegram_id=3, first_name='C', language='ru', status='deleted', balance_kopeks=99_900),
            ]
        )
        await db.flush()
        for user in (1, 2):
            await db.execute(User.__table__.update().where(User.id == user).values(created_at=now - timedelta(days=5)))
        await db.execute(User.__table__.update().where(User.id == 2).values(created_at=now - timedelta(hours=2)))

        def sub(sid, *, trial, status, days):
            return Subscription(
                id=sid,
                user_id=1,
                remnawave_short_id=f's{sid}',
                is_trial=trial,
                status=status,
                start_date=now - timedelta(days=40),
                end_date=now + timedelta(days=days),
            )

        db.add_all(
            [
                sub(10, trial=False, status=SubscriptionStatus.ACTIVE.value, days=10),  # платная живая
                sub(11, trial=False, status=SubscriptionStatus.EXPIRED.value, days=-5),  # платная истёкшая
                sub(12, trial=True, status=SubscriptionStatus.TRIAL.value, days=2),  # триал живой
                sub(13, trial=True, status=SubscriptionStatus.EXPIRED.value, days=-30),  # триал из истории
                sub(14, trial=True, status=SubscriptionStatus.ACTIVE.value, days=-1),  # статус не погашен, срок вышел
            ]
        )

        def tx(tid, *, kind, amount, hours_ago, completed=True):
            return Transaction(
                id=tid,
                user_id=1,
                type=kind,
                amount_kopeks=amount,
                is_completed=completed,
                created_at=now - timedelta(hours=hours_ago),
            )

        db.add_all(
            [
                tx(1, kind=TransactionType.DEPOSIT.value, amount=30_000, hours_ago=3),
                tx(2, kind=TransactionType.DEPOSIT.value, amount=15_000, hours_ago=1),
                tx(3, kind=TransactionType.DEPOSIT.value, amount=70_000, hours_ago=30),  # позавчера
                tx(4, kind=TransactionType.DEPOSIT.value, amount=50_000, hours_ago=1, completed=False),  # не прошёл
                tx(5, kind=TransactionType.SUBSCRIPTION_PAYMENT.value, amount=-20_000, hours_ago=1),  # списание
            ]
        )
        await db.commit()

        lock = asyncio.Lock()
        monkeypatch.setattr(startup, 'AsyncSessionLocal', lambda: _SameSession(db, lock))
        service = StartupNotificationService(bot=AsyncMock())
        monkeypatch.setattr(service, '_check_remnawave_connection', AsyncMock(return_value=(True, 'на связи', 50)))

        stats = await service._collect_stats()

    assert stats.users == 2
    assert stats.users_new == 1
    assert stats.paid == 1
    assert stats.trials == 1, 'триалы из истории и с вышедшим сроком — не активные'
    assert stats.deposits_kopeks == 45_000
    assert stats.balances_kopeks == 10_050
    assert stats.open_tickets == 0


class _SameSession:
    """Одна in-memory база на все «сессии» сервиса. Сервис считает показатели
    параллельно, а AsyncSession параллельных запросов не допускает — по очереди."""

    def __init__(self, db, lock: asyncio.Lock) -> None:
        self._db = db
        self._lock = lock

    async def __aenter__(self):
        await self._lock.acquire()
        return self._db

    async def __aexit__(self, *exc) -> None:
        self._lock.release()


def test_rich_view_renders_the_same_sections(monkeypatch):
    """Сбой rich-рендера молча уводит в классический вид — ломку здесь никто бы не заметил."""
    monkeypatch.setattr('app.utils.rich_menu._resolve_rich_logo_url', lambda: None)

    rich = startup.render_startup_rich(_stats(maintenance=True))

    assert rich.count('<table') == 4
    assert 'v4.14.0' in rich
    assert 'Требует внимания' in rich
    assert '\n' not in rich, 'в rich-HTML перенос строки — только <br>'
