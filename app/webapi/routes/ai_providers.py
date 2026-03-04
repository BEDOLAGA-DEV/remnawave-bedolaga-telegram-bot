"""
Web API routes for AI Provider management.

Tag: ai-providers
Prefix: /ai-providers

Follows the same patterns as existing routes (tickets, config, etc.):
- Security(require_api_token) for auth
- Depends(get_db_session) for DB
- Pydantic response models
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Security, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.ai_ticket.services import ai_manager
from app.modules.ai_ticket.services import prompt_service

from ..dependencies import get_db_session, require_api_token
from ..schemas.ai_providers import (
    AIPromptResponse,
    AIPromptUpdateRequest,
    AIProviderAddKeyRequest,
    AIProviderKeyResponse,
    AIProviderPriorityRequest,
    AIProviderRemoveKeyRequest,
    AIProviderResponse,
    AIProviderSetModelRequest,
    AIProviderTestResponse,
    AIProviderToggleRequest,
)


router = APIRouter()
logger = structlog.get_logger(__name__)


def _mask_key(key: str) -> str:
    if len(key) <= 12:
        return '***'
    return key[:8] + '…' + key[-4:]


def _serialize_provider(p) -> AIProviderResponse:
    keys = p.api_keys or []
    active_idx = p.active_key_index or 0
    return AIProviderResponse(
        name=p.name,
        enabled=p.enabled,
        priority=p.priority,
        keys_count=len(keys),
        keys=[
            AIProviderKeyResponse(
                index=i,
                masked=_mask_key(k),
                is_active=(i == active_idx),
            )
            for i, k in enumerate(keys)
        ],
        active_key_index=active_idx,
        selected_model=p.selected_model,
        available_models=p.available_models or [],
        base_url=p.base_url,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


# ───────────────── System Prompt (before parameterized routes) ─────────────────

@router.get('/prompt/current', response_model=AIPromptResponse)
async def get_prompt(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> AIPromptResponse:
    """Get the current system prompt (stock or custom)."""
    current = await prompt_service.get_system_prompt(db)
    stock = prompt_service.get_stock_prompt()
    return AIPromptResponse(
        is_custom=(current != stock),
        prompt=current,
        service_name=prompt_service.get_service_name(),
    )


@router.put('/prompt/current', response_model=AIPromptResponse)
async def update_prompt(
    payload: AIPromptUpdateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> AIPromptResponse:
    """Set a custom system prompt override."""
    await prompt_service.set_custom_prompt(db, payload.prompt)
    current = await prompt_service.get_system_prompt(db)
    stock = prompt_service.get_stock_prompt()
    return AIPromptResponse(
        is_custom=(current != stock),
        prompt=current,
        service_name=prompt_service.get_service_name(),
    )


@router.delete('/prompt/current', response_model=AIPromptResponse)
async def reset_prompt(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> AIPromptResponse:
    """Reset to stock prompt."""
    await prompt_service.reset_to_stock(db)
    stock = prompt_service.get_stock_prompt()
    return AIPromptResponse(
        is_custom=False,
        prompt=stock,
        service_name=prompt_service.get_service_name(),
    )


# ───────────────── Providers CRUD ─────────────────

@router.get('', response_model=list[AIProviderResponse])
async def list_providers(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> list[AIProviderResponse]:
    """Get all AI providers sorted by priority."""
    await ai_manager.ensure_providers_exist(db)
    providers = await ai_manager.get_providers(db)
    return [_serialize_provider(p) for p in providers]


@router.get('/{provider_name}', response_model=AIProviderResponse)
async def get_provider(
    provider_name: str,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> AIProviderResponse:
    """Get a single provider by name."""
    await ai_manager.ensure_providers_exist(db)
    provider = await ai_manager.get_provider(db, provider_name)
    if not provider:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Provider not found')
    return _serialize_provider(provider)


@router.post('/{provider_name}/toggle', response_model=AIProviderResponse)
async def toggle_provider(
    provider_name: str,
    payload: AIProviderToggleRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> AIProviderResponse:
    """Enable or disable a provider."""
    success = await ai_manager.set_enabled(db, provider_name, payload.enabled)
    if not success:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Provider not found')
    provider = await ai_manager.get_provider(db, provider_name)
    return _serialize_provider(provider)


@router.post('/{provider_name}/priority', response_model=AIProviderResponse)
async def set_priority(
    provider_name: str,
    payload: AIProviderPriorityRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> AIProviderResponse:
    """Set provider priority (lower = tried first)."""
    success = await ai_manager.set_priority(db, provider_name, payload.priority)
    if not success:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Provider not found')
    provider = await ai_manager.get_provider(db, provider_name)
    return _serialize_provider(provider)


# ───────────────── API Keys ─────────────────

@router.post('/{provider_name}/keys', response_model=AIProviderResponse, status_code=status.HTTP_201_CREATED)
async def add_key(
    provider_name: str,
    payload: AIProviderAddKeyRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> AIProviderResponse:
    """Add an API key to a provider."""
    success = await ai_manager.add_key(db, provider_name, payload.api_key)
    if not success:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Provider not found')
    provider = await ai_manager.get_provider(db, provider_name)
    return _serialize_provider(provider)


@router.delete('/{provider_name}/keys', response_model=AIProviderResponse)
async def remove_key(
    provider_name: str,
    payload: AIProviderRemoveKeyRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> AIProviderResponse:
    """Remove an API key by index."""
    success = await ai_manager.remove_key(db, provider_name, payload.key_index)
    if not success:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Invalid key index or provider not found')
    provider = await ai_manager.get_provider(db, provider_name)
    return _serialize_provider(provider)


# ───────────────── Test Connection & Models ─────────────────

@router.post('/{provider_name}/test', response_model=AIProviderTestResponse)
async def test_connection(
    provider_name: str,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> AIProviderTestResponse:
    """Test provider connection and fetch available models."""
    result = await ai_manager.test_connection(db, provider_name)
    return AIProviderTestResponse(
        ok=result.get('ok', False),
        models=result.get('models', []),
        count=len(result.get('models', [])),
        error=result.get('error'),
    )


@router.post('/{provider_name}/model', response_model=AIProviderResponse)
async def set_model(
    provider_name: str,
    payload: AIProviderSetModelRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> AIProviderResponse:
    """Set the selected model for a provider."""
    success = await ai_manager.set_model(db, provider_name, payload.model)
    if not success:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Provider not found')
    provider = await ai_manager.get_provider(db, provider_name)
    return _serialize_provider(provider)
