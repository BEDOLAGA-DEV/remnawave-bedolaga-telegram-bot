from unittest.mock import AsyncMock, MagicMock

import pytest

import app.database.crud.partner_promo as crud


def test_is_safe_url_accepts_https():
    assert crud._is_safe_url('https://partner.example.com/path') is True


@pytest.mark.parametrize('bad', [
    'http://insecure.example.com',
    'javascript:alert(1)',
    'data:text/html,x',
    'ftp://x',
    '',
    'partner.example.com',
])
def test_is_safe_url_rejects(bad):
    assert crud._is_safe_url(bad) is False


@pytest.mark.asyncio
async def test_create_rejects_non_https():
    db = MagicMock(); db.add = MagicMock(); db.commit = AsyncMock()
    with pytest.raises(ValueError):
        await crud.create(db, title={'ru': 'X'}, url='http://x.com')


@pytest.mark.asyncio
async def test_create_rejects_bad_image_url():
    db = MagicMock(); db.add = MagicMock(); db.commit = AsyncMock()
    with pytest.raises(ValueError):
        await crud.create(db, title={'ru': 'X'}, url='https://ok.com', image_url='javascript:x')


@pytest.mark.asyncio
async def test_increment_click_uses_atomic_update():
    db = MagicMock(); db.execute = AsyncMock(); db.commit = AsyncMock()
    await crud.increment_click(db, 7)
    assert db.execute.await_count == 1
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_go_redirects_and_counts(monkeypatch):
    import app.webserver.partner_promo as pp
    from types import SimpleNamespace
    from contextlib import asynccontextmanager

    monkeypatch.setattr(pp.settings, 'PARTNER_SHOWCASE_ENABLED', True, raising=False)
    promo = SimpleNamespace(id=1, url='https://partner.example.com', is_active=True)
    monkeypatch.setattr(pp.crud, 'get', AsyncMock(return_value=promo))
    inc = AsyncMock()
    monkeypatch.setattr(pp.crud, 'increment_click', inc)

    @asynccontextmanager
    async def _fake_session():
        yield MagicMock()
    monkeypatch.setattr(pp, 'AsyncSessionLocal', _fake_session)

    resp = await pp.partner_promo_go(1)
    assert resp.status_code == 302
    assert resp.headers['location'] == 'https://partner.example.com'
    inc.assert_awaited_once()


@pytest.mark.asyncio
async def test_go_404_when_disabled(monkeypatch):
    import app.webserver.partner_promo as pp
    from fastapi import HTTPException
    monkeypatch.setattr(pp.settings, 'PARTNER_SHOWCASE_ENABLED', False, raising=False)
    with pytest.raises(HTTPException) as exc:
        await pp.partner_promo_go(1)
    assert exc.value.status_code == 404
