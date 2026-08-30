"""Токен NaloGO переживает и перезапуск бота, и отказ входа по паролю.

Каждый чек логинился заново по ИНН и паролю, а сохранённый токен не читался
никогда. Когда ФНС перестала принимать пару ИНН+пароль, чеки встали, хотя на
руках был живой токен и бессрочный refresh к нему.
"""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import app.services.nalogo_service as nalogo_module
from app.services.nalogo_queue_service import NalogoQueueService
from app.services.nalogo_service import (
    NALOGO_MANUAL_VERIFICATION_MARKER,
    NALOGO_PENDING_VERIFICATION_KEY,
    NaloGoService,
)


def _token(expires_in: timedelta = timedelta(hours=1), **overrides) -> dict:
    base = {
        'token': 'access-token',
        'refreshToken': 'refresh-token',
        'tokenExpireIn': (datetime.now(UTC) + expires_in).isoformat().replace('+00:00', 'Z'),
        'profile': {'inn': '123456789012'},
    }
    return base | overrides


def _service(stored_token: dict | None, refreshed: dict | None = None) -> NaloGoService:
    """Сервис с подменённым клиентом: сеть не трогаем, только ветвление."""
    auth_provider = SimpleNamespace(
        get_token=AsyncMock(return_value=stored_token),
        reload_token_from_storage=MagicMock(),
        refresh=AsyncMock(return_value=refreshed),
    )
    client = SimpleNamespace(
        auth_provider=auth_provider,
        authenticate=AsyncMock(),
        create_new_access_token=AsyncMock(return_value=json.dumps(_token())),
        user=MagicMock(return_value=SimpleNamespace(get=AsyncMock(return_value={'inn': '123456789012'}))),
    )

    service = NaloGoService.__new__(NaloGoService)
    service.configured = True
    service.client = client
    service.inn = '123456789012'
    service.password = 'secret'
    return service


async def test_saved_token_is_used_instead_of_password_login():
    """Живой токен из хранилища избавляет от логина по паролю."""
    service = _service(stored_token=_token())

    assert await service.authenticate() is True
    service.client.create_new_access_token.assert_not_awaited()
    service.client.authenticate.assert_awaited_once()


async def test_expired_token_is_refreshed():
    """Протухший токен продлевается refresh-токеном, а не паролем."""
    refreshed = _token(token='fresh-token')
    service = _service(stored_token=_token(expires_in=timedelta(minutes=-1)), refreshed=refreshed)

    assert await service.authenticate() is True
    service.client.auth_provider.refresh.assert_awaited_once_with('refresh-token')
    service.client.create_new_access_token.assert_not_awaited()
    assert json.loads(service.client.authenticate.await_args.args[0])['token'] == 'fresh-token'


async def test_token_rejected_by_fns_falls_back_to_password():
    """Отозванный раньше срока токен не должен запирать выпуск чеков."""
    service = _service(stored_token=_token())
    service.client.user.return_value.get.side_effect = RuntimeError('authentication.failed')

    assert await service.authenticate() is True
    service.client.create_new_access_token.assert_awaited_once_with('123456789012', 'secret')


async def test_dead_refresh_token_falls_back_to_password():
    """Refresh не сработал — идём логиниться, а не сдаёмся."""
    service = _service(stored_token=_token(expires_in=timedelta(minutes=-1)), refreshed=None)

    assert await service.authenticate() is True
    service.client.create_new_access_token.assert_awaited_once()


async def test_storage_is_reread_on_every_attempt():
    """Долгоживущий сервис обязан увидеть токен, появившийся после его создания."""
    service = _service(stored_token=_token())

    await service.authenticate()

    service.client.auth_provider.reload_token_from_storage.assert_called_once()


async def test_usable_token_skips_authentication():
    """Пока токен на руках живой, повторная авторизация не нужна."""
    service = _service(stored_token=_token())
    assert await service._has_usable_token() is True

    stale = _service(stored_token=_token(expires_in=timedelta(minutes=1)))
    assert await stale._has_usable_token() is False


class _FakeCache:
    """Минимальный in-memory дублёр app.utils.cache."""

    def __init__(self, lists=None):
        self.lists = {k: list(v) for k, v in (lists or {}).items()}
        self.values = {}

    async def lrange(self, key):
        return list(self.lists.get(key, []))

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)
        return True

    async def delete(self, key):
        self.lists.pop(key, None)
        self.values.pop(key, None)
        return True

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, expire=None):
        self.values[key] = value
        return True


async def test_manual_verification_blocks_a_second_receipt(monkeypatch):
    """«Чек создан» из админки должно защищать от повторного выпуска.

    UUID при ручной проверке неизвестен, но отметка о созданном чеке обязана
    появиться — иначе тот же платёж пройдёт мимо защиты от дублей.
    """
    pending = {'payment_id': 'pay-1', 'amount': 506.0}
    cache = _FakeCache({NALOGO_PENDING_VERIFICATION_KEY: [pending]})
    monkeypatch.setattr(nalogo_module, 'cache', cache)
    service = _service(stored_token=_token())

    await service.mark_pending_as_verified('pay-1', receipt_uuid=None, was_created=True)

    assert cache.values['nalogo:created:pay-1'] == NALOGO_MANUAL_VERIFICATION_MARKER
    # повторная попытка выписать чек по тому же платежу до API не доходит
    assert await service.create_receipt(name='Оплата', amount=506.0, payment_id='pay-1') is None


async def test_receipt_awaiting_manual_check_is_not_requeued():
    """Таймаут после авторизации: чек мог быть создан, повтор выпишет дубль."""
    receipt = {'payment_id': 'pay-1', 'amount': 506.0, 'attempts': 0, 'created_at': None}
    nalogo_service = SimpleNamespace(
        configured=True,
        get_queue_length=AsyncMock(return_value=0),
        pop_receipt_from_queue=AsyncMock(side_effect=[receipt, None]),
        create_receipt=AsyncMock(return_value=None),
        is_pending_verification=AsyncMock(return_value=True),
        requeue_receipt=AsyncMock(),
        get_queued_receipts=AsyncMock(return_value=[]),
    )
    queue = NalogoQueueService(nalogo_service)
    nalogo_service.get_queue_length = AsyncMock(side_effect=[1, 0])

    await queue._process_pending_receipts()

    nalogo_service.requeue_receipt.assert_not_awaited()
