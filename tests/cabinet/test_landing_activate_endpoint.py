"""POST /cabinet/landing/activate/{token} обязан доходить до сервиса.

Разделение подарочной активации (b42b75d5) сняло из landing.py импорт
``activate_purchase as activate_guest_purchase``, убрав одну из двух точек
вызова и оставив вторую. F821 в проекте отключён, модуль импортируется без
ошибки — эндпоинт падал NameError на каждом вызове, и так уехал в v4.2.0.

Проверка на определённость имени этого не ловит: имя может быть определено, а
вызов — не состояться. Здесь handler выполняется по-настоящему, с подменённым
сервисом, и тест краснеет ровно на том, что было сломано.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes import landing
from app.services.guest_purchase_service import GuestPurchaseError


@pytest.fixture
def open_gate(monkeypatch):
    """Пропускаем рейт-лимит и определение IP — проверяется не они."""
    monkeypatch.setattr(landing, 'get_client_ip', lambda _request: '203.0.113.7')
    monkeypatch.setattr(landing.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False))


def _request():
    return SimpleNamespace(headers={}, client=SimpleNamespace(host='203.0.113.7'))


@pytest.mark.asyncio
async def test_activation_reaches_the_service(monkeypatch, open_gate):
    purchase = SimpleNamespace(
        token='t' * 64,
        status='delivered',
        email='user@example.com',
        tariff_id=1,
        period_days=30,
        created_at=None,
        activated_at=None,
        amount_kopeks=0,
        is_gift=False,
    )
    called: list[str] = []

    async def fake_activate(_db, token, **_kwargs):
        called.append(token)
        return purchase

    monkeypatch.setattr(landing, 'activate_guest_purchase', fake_activate)
    monkeypatch.setattr(landing, '_build_purchase_status_response', lambda p: p)

    result = await landing.activate_purchase('t' * 64, raw_request=_request(), db=AsyncMock())

    assert called == ['t' * 64]
    assert result is purchase


@pytest.mark.asyncio
async def test_service_error_becomes_its_own_http_status(monkeypatch, open_gate):
    """Отказ сервиса должен доезжать до клиента своим кодом, а не пятисоткой."""
    from fastapi import HTTPException

    async def fake_activate(_db, _token, **_kwargs):
        raise GuestPurchaseError('Purchase not found', status_code=404)

    monkeypatch.setattr(landing, 'activate_guest_purchase', fake_activate)

    with pytest.raises(HTTPException) as excinfo:
        await landing.activate_purchase('t' * 64, raw_request=_request(), db=AsyncMock())

    assert excinfo.value.status_code == 404
