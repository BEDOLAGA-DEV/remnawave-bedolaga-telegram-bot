"""Обрыв клиента на вебсокете не должен выглядеть аварией приложения.

Владельцу несколько раз в день приходил отчёт «⚠️ Ошибка во время работы» с
`ClientDisconnected: Cabinet WS: Failed to accept from`. Ничего не ломалось:
браузер закрывал вкладку (телефон уходил в сон, сеть моргала) между запросом
и нашим `accept()`. Uvicorn поднимает на этом `ClientDisconnected`, а код
писал его в `logger.error` — и конвейер отчётов честно нёс это владельцу,
топя в шуме настоящие ошибки.

Сторож требует: на обрыве клиента журнал молчит на уровне error, а на любой
другой поломке — по-прежнему говорит.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.utils.websocket_errors import is_client_gone


def _client_disconnected() -> BaseException:
    from uvicorn.protocols.utils import ClientDisconnected

    return ClientDisconnected()


class _Socket:
    """Сокет, который умирает на первом же обращении — как ушедший клиент."""

    def __init__(self, error: BaseException):
        self.error = error
        self.client = type('C', (), {'host': '1.2.3.4'})()
        self.query_params: dict[str, str] = {}

    async def accept(self, *args, **kwargs):
        raise self.error

    async def close(self, *args, **kwargs):
        raise self.error

    async def send_json(self, *args, **kwargs):
        raise self.error

    async def receive_text(self):
        raise self.error


def test_known_disconnects_are_recognised() -> None:
    from starlette.websockets import WebSocketDisconnect

    assert is_client_gone(_client_disconnected())
    assert is_client_gone(WebSocketDisconnect(code=1006))
    assert is_client_gone(ConnectionResetError())
    assert is_client_gone(BrokenPipeError())
    # Starlette отвечает обычным RuntimeError на запись в закрытый сокет.
    assert is_client_gone(RuntimeError('Cannot call "send" once a close message has been sent.'))


def test_real_failures_are_not_mistaken_for_a_disconnect() -> None:
    assert not is_client_gone(ValueError('bad payload'))
    assert not is_client_gone(RuntimeError('database is on fire'))
    assert not is_client_gone(KeyError('token'))


@pytest.mark.parametrize('error_factory', [_client_disconnected, ConnectionResetError])
async def test_cabinet_socket_stays_quiet_when_client_is_gone(error_factory) -> None:
    from app.cabinet.routes import websocket as ws_route

    socket = _Socket(error_factory())
    socket.query_params = {'token': 'good'}

    with (
        patch.object(ws_route, 'verify_cabinet_ws_token', AsyncMock(return_value=(1, False))),
        patch.object(ws_route.logger, 'error') as errored,
        patch.object(ws_route.logger, 'exception') as excepted,
    ):
        await ws_route.cabinet_websocket_endpoint(socket)

    assert not errored.called, 'обрыв клиента ушёл в отчёт владельцу как авария'
    assert not excepted.called


async def test_cabinet_socket_still_reports_a_real_failure() -> None:
    from app.cabinet.routes import websocket as ws_route

    socket = _Socket(RuntimeError('accept handler is broken'))
    socket.query_params = {'token': 'good'}

    with (
        patch.object(ws_route, 'verify_cabinet_ws_token', AsyncMock(return_value=(1, False))),
        patch.object(ws_route.logger, 'error') as errored,
    ):
        await ws_route.cabinet_websocket_endpoint(socket)

    assert errored.called, 'настоящая поломка обязана остаться видимой'


@pytest.mark.parametrize('token', [None, 'bad'])
async def test_cabinet_socket_survives_a_disconnect_while_refusing(token) -> None:
    """Отказ неавторизованному тоже пишет в сокет — и тоже может не застать клиента."""
    from app.cabinet.routes import websocket as ws_route

    socket = _Socket(_client_disconnected())
    if token:
        socket.query_params = {'token': token}

    with (
        patch.object(ws_route, 'verify_cabinet_ws_token', AsyncMock(return_value=(None, False))),
        patch.object(ws_route.logger, 'error') as errored,
        patch.object(ws_route.logger, 'exception') as excepted,
    ):
        await ws_route.cabinet_websocket_endpoint(socket)

    assert not errored.called
    assert not excepted.called


async def test_webapi_socket_stays_quiet_when_client_is_gone() -> None:
    from app.webapi.routes import websocket as ws_route

    socket = _Socket(_client_disconnected())
    socket.query_params = {'token': 'good'}

    with (
        patch.object(ws_route, 'verify_websocket_token', AsyncMock(return_value=True)),
        patch.object(ws_route.logger, 'error') as errored,
        patch.object(ws_route.logger, 'exception') as excepted,
    ):
        await ws_route.websocket_endpoint(socket)

    assert not errored.called
    assert not excepted.called
