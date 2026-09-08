"""Очередь писем в админке: посмотреть и очистить.

Отчёт из «Багов»: у владельца нет SMTP, письма копились в очереди и умирали с
ошибкой в админ-чат, а увидеть очередь или очистить её можно было только
запросом в базу. Раздел писем теперь показывает состояние очереди и даёт
кнопку «очистить».
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.cabinet.routes import admin_email_queue as queue_routes
from app.config import settings
from app.database.models import EmailQueueItem
from tests.fixtures.sqlite_memory import memory_session


TABLES = (EmailQueueItem.__table__,)
ADMIN = SimpleNamespace(id=1, username='admin')


def _item(**kw) -> EmailQueueItem:
    now = datetime.now(tz=UTC)
    return EmailQueueItem(
        to_email=kw.get('to_email', 'user@example.com'),
        subject=kw.get('subject', 'Код подтверждения'),
        body_html=kw.get('body_html', '<p>123456</p>'),
        status=kw.get('status', 'pending'),
        attempts=kw.get('attempts', 0),
        next_attempt_at=kw.get('next_attempt_at', now + timedelta(minutes=5)),
        last_error=kw.get('last_error'),
        created_at=kw.get('created_at', now),
    )


@pytest.mark.asyncio
async def test_summary_counts_each_status(monkeypatch):
    monkeypatch.setattr(settings, 'SMTP_HOST', None)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _item(),
                _item(to_email='b@example.com'),
                _item(status='dead', last_error='SMTP не настроен — отправлять письмо некому'),
                _item(status='sent'),
            ]
        )
        await db.commit()

        result = await queue_routes.get_email_queue(admin=ADMIN, db=db)

    assert result['pending'] == 2
    assert result['dead'] == 1
    assert result['sent'] == 1
    assert result['smtp_configured'] is False
    assert len(result['items']) == 4


@pytest.mark.asyncio
async def test_items_are_newest_first_and_carry_no_letter_body(monkeypatch):
    """Тело письма — это код или ссылка входа: наружу его не отдаём."""
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_item(to_email='old@example.com', created_at=datetime.now(tz=UTC) - timedelta(hours=2)))
        db.add(_item(to_email='new@example.com'))
        await db.commit()

        result = await queue_routes.get_email_queue(admin=ADMIN, db=db)

    assert [i['to_email'] for i in result['items']] == ['new@example.com', 'old@example.com']
    assert all('body_html' not in i and 'body_text' not in i for i in result['items'])


@pytest.mark.asyncio
async def test_clear_removes_the_queue_and_reports_the_count(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_item(), _item(status='dead'), _item(status='sent')])
        await db.commit()

        # Прямой вызов корутины не проходит через FastAPI: дефолт Query(False) —
        # объект и он истинный, поэтому флаг задаём явно (см. тест дефолта ниже).
        result = await queue_routes.clear_email_queue(pending_only=False, admin=ADMIN, db=db)
        left = (await db.execute(select(EmailQueueItem.status))).scalars().all()

    assert result['removed'] == 3
    assert left == []


@pytest.mark.asyncio
async def test_clear_pending_only_leaves_history(monkeypatch):
    """«Отменить ожидающие» не должно стирать историю доставленных и потерянных."""
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_item(), _item(status='dead'), _item(status='sent')])
        await db.commit()

        result = await queue_routes.clear_email_queue(pending_only=True, admin=ADMIN, db=db)
        left = sorted((await db.execute(select(EmailQueueItem.status))).scalars().all())

    assert result['removed'] == 1
    assert left == ['dead', 'sent']


def test_clear_defaults_to_wiping_everything():
    """Запрос без параметров чистит очередь целиком — дефолт проверяем по сигнатуре.

    Тесты зовут корутину напрямую и передают флаг сами, поэтому дефолт
    query-параметра ими не покрывается.
    """
    import inspect

    default = inspect.signature(queue_routes.clear_email_queue).parameters['pending_only'].default
    assert getattr(default, 'default', default) is False


@pytest.mark.asyncio
async def test_empty_queue_is_not_an_error(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        summary = await queue_routes.get_email_queue(admin=ADMIN, db=db)
        cleared = await queue_routes.clear_email_queue(pending_only=False, admin=ADMIN, db=db)

    assert summary['pending'] == 0
    assert summary['items'] == []
    assert cleared['removed'] == 0


@pytest.mark.asyncio
async def test_routes_are_registered(registered_paths):
    assert 'GET' in registered_paths.get('/cabinet/admin/email-queue', set())
    assert 'DELETE' in registered_paths.get('/cabinet/admin/email-queue', set())
