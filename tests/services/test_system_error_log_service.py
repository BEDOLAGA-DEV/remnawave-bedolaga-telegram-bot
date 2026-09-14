"""Журнал системных ошибок: редактирование секретов, учёт попыток, слив очереди.

Журнал пишет ошибку в БД до попытки доставки и отдаёт её через
``GET /admin/system-errors/{id}``. Это второй путь наружу для тех же данных,
которые на пути в Telegram проходят через ``_redact_telegram_secrets`` — значит,
фильтр обязан стоять и здесь, иначе токен бота ложится в таблицу открытым
текстом и уезжает в HTTP-ответ.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.system_error_log_service import (
    STATUS_FAILED,
    STATUS_SENT,
    STATUS_SKIPPED,
    STATUS_SUPPRESSED,
    SystemErrorLogService,
)


TOKEN = '8521897198:AAHnQ1exampleTOKENvalue_0123456789abcd'


def _event_dict_with_token() -> dict:
    try:
        raise RuntimeError(f'POST https://api.telegram.org/bot{TOKEN}/sendMessage failed')
    except RuntimeError:
        import sys

        return {
            'event': f'request to https://api.telegram.org/bot{TOKEN}/getMe failed',
            'level': 'error',
            'logger': 'app.x',
            'exc_info': sys.exc_info(),
            'url': f'https://api.telegram.org/bot{TOKEN}/sendMessage',
        }


# ============ редактирование секретов ============


def test_token_is_redacted_in_every_persisted_field():
    payload = SystemErrorLogService._build_payload(_event_dict_with_token(), 'uid1', None)

    assert TOKEN not in (payload['event'] or '')
    assert TOKEN not in (payload['traceback'] or '')
    assert TOKEN not in str(payload['context'])
    assert '[REDACTED]' in payload['event']


def test_redaction_keeps_the_rest_of_the_message():
    payload = SystemErrorLogService._build_payload(
        {'event': 'connection to panel refused', 'level': 'error', 'logger': 'app.y'}, 'uid2', None
    )
    assert payload['event'] == 'connection to panel refused'


# ============ учёт попыток доставки ============


@pytest.mark.parametrize(
    ('status', 'counted'),
    [
        (STATUS_SENT, True),
        (STATUS_FAILED, True),
        (STATUS_SUPPRESSED, False),
        (STATUS_SKIPPED, False),
    ],
)
async def test_only_real_delivery_attempts_are_counted(status, counted):
    """suppressed/skipped до Telegram не доходят — счётчик попыток они не двигают."""
    service = SystemErrorLogService()
    captured: list = []

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=lambda stmt: captured.append(stmt))
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    with patch('app.services.system_error_log_service.AsyncSessionLocal', return_value=session):
        await service._apply('status', {'event_uid': 'uid', 'status': status, 'error': None})

    values = captured[0].compile().params
    assert ('delivery_attempts' in str(captured[0])) is counted, (
        f'{status}: delivery_attempts {"должен" if counted else "не должен"} обновляться'
    )
    assert values['delivery_status'] == status


# ============ слив очереди при остановке ============


async def test_stop_flushes_what_is_already_queued():
    """При аварийном завершении в очереди лежат ровно те ошибки, что к нему привели."""
    service = SystemErrorLogService()
    applied: list = []

    async def fake_apply(op, payload):
        applied.append((op, payload.get('event_uid')))

    await service.start()
    try:
        with patch.object(service, '_apply', fake_apply):
            uid = service.record({'event': 'boom', 'level': 'error', 'logger': 'app.z'})
            assert uid is not None
            await service.stop()
    finally:
        await service.stop()

    assert applied == [('insert', uid)], 'событие из очереди должно быть записано до остановки'


async def test_stop_is_safe_without_start():
    await SystemErrorLogService().stop()
