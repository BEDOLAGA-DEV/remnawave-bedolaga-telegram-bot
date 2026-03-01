"""Глобальные фикстуры и настройки окружения для тестов."""

import asyncio
import inspect
import os
import sys
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest


# Add project root to Python path for imports
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Подменяем параметры подключения к БД, чтобы SQLAlchemy не требовал aiosqlite.
os.environ.setdefault('DATABASE_MODE', 'postgresql')
os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://user:pass@localhost/test_db')
os.environ.setdefault('BOT_TOKEN', 'test-token')

# Создаём заглушки для драйверов, которых может не быть в окружении тестов.
sys.modules.setdefault('asyncpg', types.ModuleType('asyncpg'))
sys.modules.setdefault('aiosqlite', types.ModuleType('aiosqlite'))

# Эмуляция redis.asyncio, чтобы модуль кеша мог импортироваться.
if 'redis.asyncio' not in sys.modules:
    redis_module = types.ModuleType('redis')
    redis_async_module = types.ModuleType('redis.asyncio')

    class _FakeRedisClient:
        async def ping(self):
            """Имитируем успешный ответ ping."""
            return True

        async def close(self):
            """Закрытие соединения ничего не делает."""

        async def get(self, key):
            return None

        async def set(self, key, value, ex=None):
            return True

        async def delete(self, *keys):
            return 0

        async def keys(self, pattern='*'):
            return []

        async def exists(self, key):
            return False

        async def expire(self, key, seconds):
            return True

        async def incr(self, key):
            return 1

    def _from_url(url):
        return _FakeRedisClient()

    redis_async_module.from_url = _from_url
    redis_async_module.Redis = _FakeRedisClient
    sys.modules['redis'] = redis_module
    sys.modules['redis.asyncio'] = redis_async_module

# Минимальная реализация SDK YooKassa, чтобы импорт сервисов не падал.
if 'yookassa' not in sys.modules:
    fake_yookassa = types.ModuleType('yookassa')

    class _FakeConfiguration:
        @staticmethod
        def configure(*args, **kwargs):
            """Конфигурация заглушки ничего не делает."""

    class _FakePayment:
        @staticmethod
        def create(*args, **kwargs):
            """Возвращает объект с минимально необходимыми атрибутами."""

            class _Response:
                id = 'yk_fake'
                status = 'pending'
                paid = False
                refundable = False
                metadata = {}
                amount = types.SimpleNamespace(value='0.00', currency='RUB')
                confirmation = types.SimpleNamespace(confirmation_url='https://example.com')
                created_at = datetime.now(UTC)
                description = ''
                test = False

            return _Response()

    fake_yookassa.Configuration = _FakeConfiguration
    fake_yookassa.Payment = _FakePayment
    sys.modules['yookassa'] = fake_yookassa

    # Подготавливаем вложенные пакеты, используемые сервисом.
    domain_module = types.ModuleType('yookassa.domain')
    request_module = types.ModuleType('yookassa.domain.request')
    payment_builder_module = types.ModuleType('yookassa.domain.request.payment_request_builder')
    common_module = types.ModuleType('yookassa.domain.common')
    confirmation_module = types.ModuleType('yookassa.domain.common.confirmation_type')

    class _FakePaymentRequestBuilder:
        def __init__(self):
            self.data: dict = {}

        def set_amount(self, value):
            self.data['amount'] = value
            return self

        def set_capture(self, value):
            self.data['capture'] = value
            return self

        def set_confirmation(self, value):
            self.data['confirmation'] = value
            return self

        def set_description(self, value):
            self.data['description'] = value
            return self

        def set_metadata(self, value):
            self.data['metadata'] = value
            return self

        def set_receipt(self, value):
            self.data['receipt'] = value
            return self

        def set_payment_method_data(self, value):
            self.data['payment_method_data'] = value
            return self

        def build(self):
            return self.data

    class _FakeConfirmationType:
        REDIRECT = 'redirect'

    payment_builder_module.PaymentRequestBuilder = _FakePaymentRequestBuilder
    confirmation_module.ConfirmationType = _FakeConfirmationType

    exceptions_module = types.ModuleType('yookassa.domain.exceptions')
    not_found_module = types.ModuleType('yookassa.domain.exceptions.not_found_error')

    class _FakeNotFoundError(Exception):
        pass

    not_found_module.NotFoundError = _FakeNotFoundError
    exceptions_module.not_found_error = not_found_module

    sys.modules['yookassa.domain'] = domain_module
    sys.modules['yookassa.domain.request'] = request_module
    sys.modules['yookassa.domain.request.payment_request_builder'] = payment_builder_module
    sys.modules['yookassa.domain.common'] = common_module
    sys.modules['yookassa.domain.common.confirmation_type'] = confirmation_module
    sys.modules['yookassa.domain.exceptions'] = exceptions_module
    sys.modules['yookassa.domain.exceptions.not_found_error'] = not_found_module


@pytest.fixture
def fixed_datetime() -> datetime:
    """Возвращает фиксированную отметку времени для воспроизводимых проверок."""
    return datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)


def pytest_configure(config: pytest.Config) -> None:
    """Регистрируем маркеры для асинхронных тестов."""

    config.addinivalue_line(
        'markers',
        'asyncio: запуск асинхронного теста через встроенный цикл событий',
    )
    config.addinivalue_line(
        'markers',
        'anyio: запуск асинхронного теста через встроенный цикл событий',
    )


def _unwrap_test(obj):
    """Возвращает исходную функцию, снимая обёртки pytest и декораторов."""

    unwrapped = obj
    while hasattr(unwrapped, '__wrapped__'):
        unwrapped = unwrapped.__wrapped__
    return unwrapped


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool | None:
    """Позволяет запускать async def тесты без дополнительных плагинов."""

    # Пропускаем если pytest-asyncio уже обработал этот тест
    if hasattr(pyfuncitem, '_request') and hasattr(pyfuncitem._request, '_pyfuncitem'):
        markers = list(pyfuncitem.iter_markers())
        for marker in markers:
            if marker.name in ('asyncio', 'anyio'):
                # pytest-asyncio обработает этот тест
                return None

    test_func = _unwrap_test(pyfuncitem.obj)
    if not inspect.iscoroutinefunction(test_func):
        return None

    # Проверяем, не обработан ли уже тест плагином pytest-asyncio
    # Если pyfuncitem.obj не возвращает корутину - пропускаем
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        signature = inspect.signature(test_func)
        call_kwargs = {name: value for name, value in pyfuncitem.funcargs.items() if name in signature.parameters}
        coro = pyfuncitem.obj(**call_kwargs)
        if coro is None:
            # Уже обработано другим плагином
            return None
        loop.run_until_complete(coro)
    finally:
        asyncio.set_event_loop(None)
        loop.close()

    return True
