"""POST /users/{id}/notify: письма только на подтверждённый адрес.

Остальные уведомления бота уходят на почту лишь после подтверждения — адрес
мог быть введён с опечаткой или чужой. Предупреждение о нарушении тем более
не должно попадать в чужой ящик.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.webapi.routes import users as users_routes
from app.webapi.schemas.users import UserNotifyRequest


def _user(**overrides):
    base = dict(id=5, telegram_id=None, email='client@example.com', email_verified=True)
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def email_service(monkeypatch):
    module = importlib.import_module('app.cabinet.services.email_service')

    service = MagicMock()
    service.is_configured.return_value = True
    service.send_email.return_value = True
    monkeypatch.setattr(module, 'email_service', service)
    return service


async def _notify(monkeypatch, user, **payload):
    monkeypatch.setattr(users_routes, 'get_user_by_id', AsyncMock(return_value=user))
    request = UserNotifyRequest(channels=['email'], **{'text': 'предупреждение', **payload})
    return await users_routes.notify_user(user_id=user.id, payload=request, _=None, db=None)


@pytest.mark.asyncio
async def test_email_goes_to_verified_address(monkeypatch, email_service):
    response = await _notify(monkeypatch, _user())

    assert response.email.sent is True
    email_service.send_email.assert_called_once()


@pytest.mark.asyncio
async def test_unverified_email_is_skipped(monkeypatch, email_service):
    response = await _notify(monkeypatch, _user(email_verified=False))

    assert response.email.sent is False
    assert response.email.reason == 'email_not_verified'
    email_service.send_email.assert_not_called()


@pytest.mark.asyncio
async def test_plain_text_is_escaped_in_email(monkeypatch, email_service):
    await _notify(monkeypatch, _user(), text='a < b & c', parse_mode=None)

    assert email_service.send_email.call_args.kwargs['body_html'] == '<p>a &lt; b &amp; c</p>'


@pytest.mark.asyncio
async def test_blank_text_is_rejected(monkeypatch, email_service):
    with pytest.raises(HTTPException) as error:
        await _notify(monkeypatch, _user(), text='   ')

    assert error.value.status_code == 422
    email_service.send_email.assert_not_called()
