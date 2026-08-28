"""Гостевой платёж MulenPay должен нести контакт покупателя.

У гостя нет аккаунта, поэтому поле ``client`` — единственная зацепка для
поддержки провайдера. Контакт при этом уже известен и провалидирован: лендинг
кладёт его в ``GuestPurchase.contact_type``/``contact_value``. До правки
гостевой путь звал ``create_mulenpay_payment`` с ``user_id=None`` и не передавал
ничего — то есть поле пустовало ровно там, где нужнее всего.
"""

from types import SimpleNamespace
from typing import Any

import pytest

import app.services.payment_service as payment_service_module


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


class _Session:
    async def commit(self) -> None:
        return None

    async def refresh(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _service() -> payment_service_module.PaymentService:
    service = payment_service_module.PaymentService.__new__(payment_service_module.PaymentService)
    service.mulenpay_service = SimpleNamespace()
    return service


async def _run_guest_payment(
    monkeypatch: pytest.MonkeyPatch, purchase: Any, *, raises: Exception | None = None
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def fake_create_mulenpay_payment(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {'payment_url': 'https://mulenpay/pay', 'uuid': 'u-1', 'local_payment_id': 1}

    async def fake_get_purchase_by_token(_db: Any, _token: str) -> Any:
        if raises is not None:
            raise raises
        return purchase

    monkeypatch.setattr(
        payment_service_module.PaymentService,
        'create_mulenpay_payment',
        staticmethod(fake_create_mulenpay_payment),
        raising=False,
    )
    monkeypatch.setattr('app.database.crud.landing.get_purchase_by_token', fake_get_purchase_by_token, raising=False)

    monkeypatch.setattr(payment_service_module, '_GETTER_OVERRIDES', {}, raising=False)

    service = _service()
    result = await service.create_guest_payment(
        _Session(),
        amount_kopeks=25000,
        payment_method='mulenpay',
        description='Подписка',
        purchase_token='t' * 64,
        return_url='https://example.com/ok',
    )
    assert result is not None or captured
    return captured


@pytest.mark.anyio('asyncio')
async def test_guest_email_contact_is_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = await _run_guest_payment(
        monkeypatch, SimpleNamespace(contact_type='email', contact_value='guest@example.com')
    )

    assert captured['client'] == 'guest@example.com'


@pytest.mark.anyio('asyncio')
async def test_guest_telegram_contact_is_not_sent_as_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """@username — не тот контакт, который документирован примером с email."""
    captured = await _run_guest_payment(monkeypatch, SimpleNamespace(contact_type='telegram', contact_value='@someone'))

    assert captured['client'] is None


@pytest.mark.anyio('asyncio')
async def test_guest_contact_lookup_failure_does_not_block_payment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = await _run_guest_payment(monkeypatch, None, raises=RuntimeError('БД недоступна'))

    assert captured['client'] is None


@pytest.mark.anyio('asyncio')
async def test_guest_missing_purchase_yields_no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = await _run_guest_payment(monkeypatch, None)

    assert captured['client'] is None
