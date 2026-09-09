"""Настоящий пакет ``redis`` в тесте вместо заглушки из conftest.

conftest подставляет заглушку ``redis.asyncio``, чтобы модули импортировались в
окружении без пакета. Заглушка принимает ЛЮБЫЕ аргументы, поэтому проверить на
ней совместимость наших аргументов с библиотекой невозможно — а именно там и
жил баг: асинхронное соединение не принимало ``maint_notifications_config``, и
Redis у бота не работал вовсе.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType


def ensure_real_redis(monkeypatch) -> ModuleType:
    """Снять заглушку и вернуть перезагруженный ``app.utils.redis_client``.

    Настоящий пакет установлен; заглушка отличается отсутствием ``__file__``.
    """
    stub = sys.modules.get('redis')
    if stub is not None and getattr(stub, '__file__', None) is None:
        for name in list(sys.modules):
            if name == 'redis' or name.startswith('redis.'):
                monkeypatch.delitem(sys.modules, name, raising=False)

    monkeypatch.delitem(sys.modules, 'app.utils.redis_client', raising=False)
    return importlib.import_module('app.utils.redis_client')
