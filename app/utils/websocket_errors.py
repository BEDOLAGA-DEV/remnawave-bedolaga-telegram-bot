"""Обрыв соединения клиентом — конец разговора, а не авария приложения.

Браузер закрыл вкладку, телефон ушёл в сон, сеть моргнула в метро — сокет
умирает между нашим ``accept()`` и первым кадром. Uvicorn поднимает на этом
``ClientDisconnected``, Starlette — ``WebSocketDisconnect``, транспорт —
``ConnectionResetError``. Все они попадали в ``logger.error``, а оттуда
конвейер отчётов слал владельцу «⚠️ Ошибка во время работы» — несколько раз в
день на ровном месте, из-за чего настоящие ошибки в этом потоке терялись.

Здесь собран список «клиента больше нет». Ловить его руками в каждом
обработчике нельзя: ``ClientDisconnected`` живёт во внутренностях uvicorn и под
другим сервером (или другой версией) может не существовать — поэтому импорт
защищённый, а список строится один раз при загрузке.
"""

from __future__ import annotations

import contextlib


def _optional(module_name: str, attribute: str) -> type[BaseException] | None:
    """Класс исключения из необязательной зависимости; ``None`` — её здесь нет."""
    with contextlib.suppress(Exception):
        module = __import__(module_name, fromlist=[attribute])
        candidate = getattr(module, attribute, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            return candidate
    return None


def _collect() -> tuple[type[BaseException], ...]:
    found: list[type[BaseException]] = [ConnectionError]
    for module_name, attribute in (
        ('starlette.websockets', 'WebSocketDisconnect'),
        ('uvicorn.protocols.utils', 'ClientDisconnected'),
        ('anyio', 'ClosedResourceError'),
        ('anyio', 'BrokenResourceError'),
    ):
        found_class = _optional(module_name, attribute)
        if found_class is not None and found_class not in found:
            found.append(found_class)
    return tuple(found)


#: Исключения, означающие «клиента больше нет».
CLIENT_GONE_ERRORS: tuple[type[BaseException], ...] = _collect()

#: Starlette отвечает обычным RuntimeError на попытку писать в закрытый сокет.
#: Это то же самое «клиента больше нет», отличить можно только по тексту.
_CLOSED_SOCKET_MARKERS = (
    'once a close message has been sent',
    'websocket is not connected',
    'unexpected asgi message',
)


def is_client_gone(error: BaseException) -> bool:
    """Ушёл ли клиент — или это настоящая ошибка, которую надо показать."""
    if isinstance(error, CLIENT_GONE_ERRORS):
        return True
    if isinstance(error, RuntimeError):
        text = str(error).lower()
        return any(marker in text for marker in _CLOSED_SOCKET_MARKERS)
    return False
