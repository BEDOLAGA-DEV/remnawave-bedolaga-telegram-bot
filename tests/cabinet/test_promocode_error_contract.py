"""Regression tests for the cabinet promocode activation error contract.

Two Telegram-reported bugs traced to the cabinet promocode flow:

1. Every validation error that wasn't one of six hard-coded English phrases
   (e.g. ``active_discount_exists``, ``daily_limit``, ``no_subscription_for_days``)
   surfaced in the cabinet as the generic «Ошибка сервера», even though the
   backend logged nothing — they are ordinary 400s, not server faults. The
   frontend was reverse-engineering an error key from the prose ``detail``
   string and silently fell through to ``server_error`` for anything it didn't
   recognise.

The fix mirrors the canonical structured-error shape already used for
maintenance / blacklisted / channel-subscription guards: ``detail`` is now a
``{'code', 'message'}`` object so the frontend can map a stable machine code to
a localized message instead of substring-matching English text.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, status

from app.cabinet.routes import promocode as promocode_route
from app.database.models import User


def _patch_service(monkeypatch, result: dict) -> None:
    """Replace PromoCodeService with a stub returning ``result`` from activate."""
    fake_service = AsyncMock()
    fake_service.activate_promocode = AsyncMock(return_value=result)
    monkeypatch.setattr(promocode_route, 'PromoCodeService', lambda: fake_service)


@pytest.mark.parametrize(
    'error_code',
    [
        'active_discount_exists',
        'no_subscription_for_days',
        'subscription_not_found',
        'not_first_purchase',
        'daily_limit',
        'user_not_found',
        'expired',
        'not_found',
    ],
)
async def test_activate_returns_structured_error_code(monkeypatch, error_code):
    """Every error code must reach the client as ``detail.code`` verbatim."""
    _patch_service(monkeypatch, {'success': False, 'error': error_code})

    request = promocode_route.PromocodeActivateRequest(code='FUCKRKN20')
    user = User(id=1, telegram_id=123)

    with pytest.raises(HTTPException) as exc_info:
        await promocode_route.activate_promocode(request=request, user=user, db=AsyncMock())

    assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
    assert isinstance(exc_info.value.detail, dict)
    assert exc_info.value.detail['code'] == error_code
    assert exc_info.value.detail['message']  # human-readable fallback preserved


async def test_unknown_error_code_falls_back_to_server_error(monkeypatch):
    """An unmapped code degrades to a stable ``server_error`` code, not prose."""
    _patch_service(monkeypatch, {'success': False, 'error': 'something_new'})

    request = promocode_route.PromocodeActivateRequest(code='FUCKRKN20')
    user = User(id=1, telegram_id=123)

    with pytest.raises(HTTPException) as exc_info:
        await promocode_route.activate_promocode(request=request, user=user, db=AsyncMock())

    assert exc_info.value.detail['code'] == 'something_new'
