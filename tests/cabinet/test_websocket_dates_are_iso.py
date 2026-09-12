"""Даты в WebSocket-событиях кабинета — ISO 8601 в UTC, а не строка для писем.

Репорт 2026-09-12 (бот 4.8.0 / кабинет 1.72.0): карточка «Подписка продлена!»
показывала «Действует до: Invalid Date». Бот клал в ``new_expires_at``
результат ``format_email_datetime`` («27.11.2030, 12:00»), а кабинет делал
``new Date(...)`` — такую строку браузер не разбирает. Контракт: бот шлёт
машинную дату, кабинет форматирует для человека сам.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes import websocket


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / 'app'

DATE_FIELDS = {'expires_at', 'new_expires_at'}
HUMAN_FORMATTERS = {'format_email_datetime', 'format_datetime', 'format_local_datetime', 'strftime'}


@pytest.fixture
def sent(monkeypatch) -> AsyncMock:
    send = AsyncMock()
    monkeypatch.setattr(websocket.cabinet_ws_manager, 'send_to_user', send)
    return send


def _payload(send: AsyncMock) -> dict:
    return send.await_args.args[1]


@pytest.mark.asyncio
async def test_renewed_event_carries_iso_utc_date(sent):
    await websocket.notify_user_subscription_renewed(
        1, subscription_id=7, new_expires_at=datetime(2030, 11, 27, 12, 0, tzinfo=UTC), amount_kopeks=39000
    )

    assert _payload(sent)['new_expires_at'] == '2030-11-27T12:00:00+00:00'


@pytest.mark.asyncio
async def test_activated_event_carries_iso_utc_date(sent):
    await websocket.notify_user_subscription_activated(
        1, subscription_id=7, expires_at=datetime(2030, 11, 27, 12, 0, tzinfo=UTC)
    )

    assert _payload(sent)['expires_at'] == '2030-11-27T12:00:00+00:00'


@pytest.mark.asyncio
async def test_naive_datetime_is_treated_as_utc(sent):
    await websocket.notify_user_subscription_renewed(1, new_expires_at=datetime(2030, 11, 27, 12, 0))

    assert _payload(sent)['new_expires_at'] == '2030-11-27T12:00:00+00:00'


@pytest.mark.asyncio
async def test_missing_date_is_an_empty_string(sent):
    await websocket.notify_user_subscription_renewed(1, new_expires_at=None)

    assert _payload(sent)['new_expires_at'] == ''


def _called_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return getattr(node.func, 'id', None)


def test_no_caller_sends_a_human_formatted_date():
    """Ни один вызов notify_user_* не подсовывает в поле даты отформатированную строку."""
    offenders = []
    for path in sorted(APP.rglob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not (_called_name(node) or '').startswith('notify_user_'):
                continue
            for keyword in node.keywords:
                value = keyword.value
                if (
                    keyword.arg in DATE_FIELDS
                    and isinstance(value, ast.Call)
                    and _called_name(value) in HUMAN_FORMATTERS
                ):
                    offenders.append(
                        f'{path.relative_to(ROOT)}:{node.lineno}: {keyword.arg}={_called_name(value)}(...)'
                    )
    assert not offenders, 'даты в WebSocket-событиях должны быть datetime, а не строкой для человека:\n' + '\n'.join(
        offenders
    )
