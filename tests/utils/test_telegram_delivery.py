"""Один классификатор ожидаемых отказов Telegram при доставке сообщения пользователю.

Раньше channel_checker, maintenance и глобальный обработчик держали свои списки
маркеров, а места без списка (реферальные уведомления) слали админам полный
traceback на каждого заблокировавшего бота (лог владельца 2026-09-10).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from app.utils.telegram_delivery import describe_unreachable, is_user_unreachable


def _forbidden(message: str) -> TelegramForbiddenError:
    return TelegramForbiddenError(method=MagicMock(), message=message)


def _bad_request(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=MagicMock(), message=message)


@pytest.mark.parametrize(
    'error',
    [
        _forbidden('Forbidden: bot was blocked by the user'),
        _forbidden("Forbidden: bot can't initiate conversation with a user"),
        _forbidden('Forbidden: user is deactivated'),
        _bad_request('Bad Request: chat not found'),
        _bad_request('Bad Request: user is deactivated'),
    ],
)
def test_expected_delivery_refusals_are_unreachable(error):
    assert is_user_unreachable(error)


@pytest.mark.parametrize(
    'error',
    [_bad_request('Bad Request: message is not modified'), ValueError('boom'), RuntimeError('chat not found')],
)
def test_other_errors_are_not_unreachable(error):
    assert not is_user_unreachable(error)


@pytest.mark.parametrize(
    ('error', 'fragment'),
    [
        (_forbidden('Forbidden: bot was blocked by the user'), 'заблокировал бота'),
        (_forbidden("Forbidden: bot can't initiate conversation with a user"), 'не начинал диалог'),
        (_forbidden('Forbidden: user is deactivated'), 'удалён'),
        (_bad_request('Bad Request: chat not found'), 'не начинал диалог'),
        (_forbidden("Forbidden: bots can't send messages to bots"), 'заблокировал бота или диалога'),
    ],
)
def test_reason_is_plain_russian(error, fragment):
    assert fragment in describe_unreachable(error)
