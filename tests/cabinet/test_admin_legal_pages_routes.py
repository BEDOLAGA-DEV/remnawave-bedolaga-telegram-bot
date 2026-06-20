from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.config import settings


def test_admin_legal_pages_routes_registered():
    from app.cabinet.routes import router

    paths = {route.path for route in router.routes}
    assert '/cabinet/admin/legal-pages/privacy-policy' in paths
    assert '/cabinet/admin/legal-pages/public-offer' in paths
    assert '/cabinet/admin/legal-pages/rules' in paths
    assert '/cabinet/admin/legal-pages/faq' in paths
    assert '/cabinet/admin/legal-pages/faq/pages' in paths
    assert '/cabinet/admin/legal-pages/faq/pages/{page_id}' in paths


def test_legal_responses_expose_env_lock_flag():
    from app.cabinet.routes.admin_legal_pages import FaqResponse, LegalDocumentResponse, RulesResponse

    for model in (LegalDocumentResponse, RulesResponse, FaqResponse):
        assert 'display_mode_env_locked' in model.model_fields


@pytest.mark.asyncio
async def test_set_display_mode_commits(monkeypatch):
    from app.cabinet.routes import admin_legal_pages

    monkeypatch.setattr(settings, 'PRIVACY_POLICY_DISPLAY_MODE', 'both', raising=False)
    service = SimpleNamespace(set_value=AsyncMock(), is_env_overridden=lambda key: False)
    monkeypatch.setattr(admin_legal_pages, 'bot_configuration_service', service)
    db = AsyncMock()

    await admin_legal_pages._set_display_mode(db, 'privacy-policy', 'web')

    service.set_value.assert_awaited_once_with(db, 'PRIVACY_POLICY_DISPLAY_MODE', 'web')
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_display_mode_env_locked_conflict(monkeypatch):
    from app.cabinet.routes import admin_legal_pages

    monkeypatch.setattr(settings, 'PRIVACY_POLICY_DISPLAY_MODE', 'both', raising=False)
    service = SimpleNamespace(set_value=AsyncMock(), is_env_overridden=lambda key: True)
    monkeypatch.setattr(admin_legal_pages, 'bot_configuration_service', service)
    db = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await admin_legal_pages._set_display_mode(db, 'privacy-policy', 'web')

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == 'display_mode is locked by environment variable'
    service.set_value.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_display_mode_env_locked_same_value_allowed(monkeypatch):
    from app.cabinet.routes import admin_legal_pages

    monkeypatch.setattr(settings, 'PRIVACY_POLICY_DISPLAY_MODE', 'web', raising=False)
    service = SimpleNamespace(set_value=AsyncMock(), is_env_overridden=lambda key: True)
    monkeypatch.setattr(admin_legal_pages, 'bot_configuration_service', service)
    db = AsyncMock()

    await admin_legal_pages._set_display_mode(db, 'privacy-policy', 'web')

    service.set_value.assert_awaited_once_with(db, 'PRIVACY_POLICY_DISPLAY_MODE', 'web')
    db.commit.assert_awaited_once()


def test_require_language_rejects_unknown(monkeypatch):
    from app.cabinet.routes import admin_legal_pages

    monkeypatch.setattr(settings, 'AVAILABLE_LANGUAGES', 'ru,en', raising=False)

    assert admin_legal_pages._require_language('RU-ru') == 'ru'
    with pytest.raises(HTTPException) as exc_info:
        admin_legal_pages._require_language('xx')
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == 'Unsupported language: xx'
