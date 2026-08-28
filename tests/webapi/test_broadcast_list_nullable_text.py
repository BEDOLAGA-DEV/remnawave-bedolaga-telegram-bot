"""Список рассылок не должен падать из-за строки без текста.

`broadcast_history.message_text` объявлен nullable намеренно — у email-рассылки
текста телеграм-сообщения нет. Схема ответа требовала строку, поэтому одна
такая запись роняла pydantic-валидацией ВЕСЬ `GET /broadcasts`, а не только
свою строку: страница рассылок в админке переставала открываться целиком.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.database.models import BroadcastHistory
from app.webapi.routes.broadcasts import _serialize_broadcast, list_broadcasts


def _row(broadcast_id: int, message_text: str | None) -> BroadcastHistory:
    return BroadcastHistory(
        id=broadcast_id,
        target_type='all_email' if message_text is None else 'all',
        message_text=message_text,
        has_media=False,
        total_count=1,
        sent_count=1,
        failed_count=0,
        blocked_count=0,
        status='completed',
        admin_id=None,
        admin_name=None,
        created_at=datetime.now(UTC),
        completed_at=None,
    )


def test_row_without_text_serializes():
    """Email-рассылка без текста отдаётся как есть, а не ломает сериализацию."""
    response = _serialize_broadcast(_row(1, None))

    assert response.message_text is None
    assert response.id == 1


def test_one_empty_row_does_not_break_the_whole_list():
    """Соседние рассылки обязаны доехать до ответа вместе с пустой."""
    rows = [_row(1, 'обычная рассылка'), _row(2, None), _row(3, 'ещё одна')]

    items = [_serialize_broadcast(row) for row in rows]

    assert [item.id for item in items] == [1, 2, 3]
    assert [item.message_text for item in items] == ['обычная рассылка', None, 'ещё одна']


@pytest.mark.asyncio
async def test_list_endpoint_returns_rows_with_null_text(monkeypatch):
    """Сам маршрут отвечает 200, а не 500, когда в выборку попала пустая строка."""

    class _Scalars:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return _Scalars(self._rows)

    class _Db:
        async def scalar(self, _query):
            return 2

        async def execute(self, _query):
            return _Result([_row(1, None), _row(2, 'текст')])

    response = await list_broadcasts(db=_Db(), limit=50, offset=0)

    assert response.total == 2
    assert [item.message_text for item in response.items] == [None, 'текст']
