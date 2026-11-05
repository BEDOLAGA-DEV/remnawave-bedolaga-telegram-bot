from types import SimpleNamespace
from unittest.mock import Mock

import httpx
from fastapi import FastAPI

from app.config import settings
from app.external.pal24_client import Pal24APIError
from app.webserver import payments


async def test_callback_parse_error_does_not_expose_internal_details(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_pal24_configured', lambda self: True)
    provider = SimpleNamespace(
        is_configured=True,
        parse_callback=Mock(side_effect=Pal24APIError('private upstream response and internal path')),
    )
    monkeypatch.setattr(payments, 'Pal24Service', lambda: provider)
    app = FastAPI()
    app.include_router(payments.create_payment_router(SimpleNamespace(), SimpleNamespace()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post(settings.PAL24_WEBHOOK_PATH, json={'id': 'masked-1'})
    assert response.status_code == 400
    assert response.json() == {'status': 'error', 'reason': 'invalid_payload'}
