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
