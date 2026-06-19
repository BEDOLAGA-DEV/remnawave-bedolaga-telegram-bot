import pytest
from fastapi import HTTPException

from app.config import settings


DISPLAY_KEYS = (
    'PRIVACY_POLICY_DISPLAY_MODE',
    'PUBLIC_OFFER_DISPLAY_MODE',
    'SERVICE_RULES_DISPLAY_MODE',
    'FAQ_DISPLAY_MODE',
)


@pytest.mark.asyncio
async def test_visibility_defaults_all_true(monkeypatch):
    from app.cabinet.routes.info import get_info_visibility

    for key in DISPLAY_KEYS:
        monkeypatch.setattr(settings, key, 'both', raising=False)
    response = await get_info_visibility()
    assert response.faq is True
    assert response.rules is True
    assert response.privacy is True
    assert response.offer is True


@pytest.mark.asyncio
async def test_visibility_hides_bot_only_sections(monkeypatch):
    from app.cabinet.routes.info import get_info_visibility

    monkeypatch.setattr(settings, 'FAQ_DISPLAY_MODE', 'bot', raising=False)
    monkeypatch.setattr(settings, 'SERVICE_RULES_DISPLAY_MODE', 'web', raising=False)
    monkeypatch.setattr(settings, 'PRIVACY_POLICY_DISPLAY_MODE', 'both', raising=False)
    monkeypatch.setattr(settings, 'PUBLIC_OFFER_DISPLAY_MODE', 'bot', raising=False)
    response = await get_info_visibility()
    assert response.faq is False
    assert response.rules is True
    assert response.privacy is True
    assert response.offer is False


@pytest.mark.asyncio
async def test_rules_endpoint_404_when_bot_only(monkeypatch):
    from app.cabinet.routes.info import get_rules

    monkeypatch.setattr(settings, 'SERVICE_RULES_DISPLAY_MODE', 'bot', raising=False)
    with pytest.raises(HTTPException) as exc_info:
        await get_rules(language='ru', db=None)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_privacy_endpoint_404_when_bot_only(monkeypatch):
    from app.cabinet.routes.info import get_privacy_policy

    monkeypatch.setattr(settings, 'PRIVACY_POLICY_DISPLAY_MODE', 'bot', raising=False)
    with pytest.raises(HTTPException) as exc_info:
        await get_privacy_policy(language='ru', db=None)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_offer_endpoint_404_when_bot_only(monkeypatch):
    from app.cabinet.routes.info import get_public_offer

    monkeypatch.setattr(settings, 'PUBLIC_OFFER_DISPLAY_MODE', 'bot', raising=False)
    with pytest.raises(HTTPException) as exc_info:
        await get_public_offer(language='ru', db=None)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_faq_list_empty_when_bot_only(monkeypatch):
    from app.cabinet.routes.info import get_faq_pages

    monkeypatch.setattr(settings, 'FAQ_DISPLAY_MODE', 'bot', raising=False)
    assert await get_faq_pages(language='ru', db=None) == []
