"""Тесты POST /cabinet/branding/analytics/partner-click-id.

Calls the endpoint coroutine directly with mocked deps (no FastAPI TestClient)
so the test stays lightweight — same harness style as `tests/services/*`.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.cabinet.routes.branding import PartnerClickIdRequest, store_partner_click_id


@pytest.fixture
def fake_user() -> SimpleNamespace:
    return SimpleNamespace(id=42)


@pytest.fixture
def fake_db() -> AsyncMock:
    db = AsyncMock()
    return db


# ---------- Pydantic validation ----------


@pytest.mark.parametrize(
    'click_id,should_pass',
    [
        ('abc123', True),
        ('keitaro_id.with-allowed:chars', True),
        ('a' * 128, True),
        ('a' * 129, False),
        ('with space', False),
        ('semicolon;injection', False),
        ('', False),
    ],
)
def test_request_model_validation(click_id: str, should_pass: bool) -> None:
    if should_pass:
        body = PartnerClickIdRequest(click_id=click_id)
        assert body.click_id == click_id
    else:
        with pytest.raises(ValidationError):
            PartnerClickIdRequest(click_id=click_id)


# ---------- store_partner_click_id endpoint ----------


@pytest.mark.asyncio
async def test_endpoint_success_calls_upsert_subid_and_commits(monkeypatch, fake_user, fake_db) -> None:
    upsert = AsyncMock()
    monkeypatch.setattr(
        'app.database.crud.yandex_client_id.upsert_subid',
        upsert,
    )
    monkeypatch.setattr(
        'app.cabinet.routes.branding.RateLimitCache.is_rate_limited',
        AsyncMock(return_value=False),
    )

    body = PartnerClickIdRequest(click_id='abc123')
    result = await store_partner_click_id(body=body, user=fake_user, db=fake_db)

    assert result is None  # status_code=204
    upsert.assert_awaited_once_with(fake_db, 42, 'abc123', source='cabinet')
    fake_db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_endpoint_rate_limited_returns_429(monkeypatch, fake_user, fake_db) -> None:
    monkeypatch.setattr(
        'app.cabinet.routes.branding.RateLimitCache.is_rate_limited',
        AsyncMock(return_value=True),
    )
    upsert = AsyncMock()
    monkeypatch.setattr('app.database.crud.yandex_client_id.upsert_subid', upsert)

    body = PartnerClickIdRequest(click_id='abc123')
    with pytest.raises(HTTPException) as exc_info:
        await store_partner_click_id(body=body, user=fake_user, db=fake_db)

    assert exc_info.value.status_code == 429
    upsert.assert_not_called()
    fake_db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_endpoint_db_error_raises_500_and_rolls_back(monkeypatch, fake_user, fake_db) -> None:
    monkeypatch.setattr(
        'app.cabinet.routes.branding.RateLimitCache.is_rate_limited',
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        'app.database.crud.yandex_client_id.upsert_subid',
        AsyncMock(side_effect=RuntimeError('db down')),
    )

    body = PartnerClickIdRequest(click_id='abc123')
    with pytest.raises(HTTPException) as exc_info:
        await store_partner_click_id(body=body, user=fake_user, db=fake_db)

    assert exc_info.value.status_code == 500
    fake_db.rollback.assert_awaited()
