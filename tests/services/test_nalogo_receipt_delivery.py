from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.nalogo_receipt_delivery import deliver_nalogo_receipt


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


class DummyBot:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_message(self, **kwargs) -> None:
        self.messages.append(kwargs)


@pytest.mark.anyio('asyncio')
async def test_deliver_nalogo_receipt_sends_email_and_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    email_calls: list[dict] = []

    def fake_send_email(*, to_email: str, subject: str, body_html: str) -> bool:
        email_calls.append(
            {
                'to_email': to_email,
                'subject': subject,
                'body_html': body_html,
            }
        )
        return True

    import app.cabinet.services.email_service as email_service_module

    monkeypatch.setattr(
        email_service_module,
        'email_service',
        SimpleNamespace(is_configured=lambda: True, send_email=fake_send_email),
    )
    monkeypatch.setattr('app.services.nalogo_receipt_delivery._build_receipt_qr', lambda _url: None)

    bot = DummyBot()
    result = await deliver_nalogo_receipt(
        receipt_url='https://example.com/receipt',
        receipt_uuid='abc123',
        telegram_id=42,
        email='user@example.com',
        language='ru',
        bot=bot,
    )

    assert result == {'email': True, 'telegram': True}
    assert email_calls[0]['to_email'] == 'user@example.com'
    assert 'abc123' in email_calls[0]['body_html']
    assert bot.messages[0]['chat_id'] == 42
    assert 'abc123' in bot.messages[0]['text']


@pytest.mark.anyio('asyncio')
async def test_deliver_nalogo_receipt_without_contacts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('app.services.nalogo_receipt_delivery._build_receipt_qr', lambda _url: None)

    result = await deliver_nalogo_receipt(
        receipt_url='https://example.com/receipt',
        receipt_uuid='abc123',
    )

    assert result == {'email': False, 'telegram': False}


@pytest.mark.anyio('asyncio')
async def test_deliver_nalogo_receipt_is_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    email_calls: list[dict] = []
    cache_state: dict[str, bool] = {}

    def fake_send_email(*, to_email: str, subject: str, body_html: str) -> bool:
        email_calls.append(
            {
                'to_email': to_email,
                'subject': subject,
                'body_html': body_html,
            }
        )
        return True

    async def fake_cache_get(key: str):
        return cache_state.get(key)

    async def fake_cache_set(key: str, value, expire=None):
        cache_state[key] = value
        return True

    import app.cabinet.services.email_service as email_service_module

    monkeypatch.setattr(
        email_service_module,
        'email_service',
        SimpleNamespace(is_configured=lambda: True, send_email=fake_send_email),
    )
    monkeypatch.setattr('app.services.nalogo_receipt_delivery._build_receipt_qr', lambda _url: None)
    monkeypatch.setattr('app.services.nalogo_receipt_delivery.cache.get', fake_cache_get)
    monkeypatch.setattr('app.services.nalogo_receipt_delivery.cache.set', fake_cache_set)

    bot = DummyBot()

    first_result = await deliver_nalogo_receipt(
        receipt_url='https://example.com/receipt',
        receipt_uuid='abc123',
        telegram_id=42,
        email='user@example.com',
        language='ru',
        bot=bot,
    )
    second_result = await deliver_nalogo_receipt(
        receipt_url='https://example.com/receipt',
        receipt_uuid='abc123',
        telegram_id=42,
        email='user@example.com',
        language='ru',
        bot=bot,
    )

    assert first_result == {'email': True, 'telegram': True}
    assert second_result == {'email': True, 'telegram': True}
    assert len(email_calls) == 1
    assert len(bot.messages) == 1
