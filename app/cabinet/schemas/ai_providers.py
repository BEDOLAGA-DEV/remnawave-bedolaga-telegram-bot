"""Cabinet AI Providers schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


# ─── Provider schemas ───

class CabinetAIProviderKeyResponse(BaseModel):
    """Ответ для ключа провайдера."""

    index: int
    masked: str  # sk-abcd...efgh
    is_active: bool

    class Config:
        from_attributes = True


class CabinetAIProviderResponse(BaseModel):
    """Ответ для объекта провайдера ИИ."""

    name: str
    enabled: bool
    priority: int
    keys_count: int
    keys: list[CabinetAIProviderKeyResponse] = Field(default_factory=list)
    active_key_index: int
    selected_model: str | None = None
    available_models: list[str] = Field(default_factory=list)
    base_url: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class CabinetAIProviderToggleRequest(BaseModel):
    """Запрос на включение/выключение провайдера."""

    enabled: bool


class CabinetAIProviderPriorityRequest(BaseModel):
    """Запрос на изменение приоритета провайдера."""

    priority: int = Field(ge=0, le=100)


class CabinetAIProviderAddKeyRequest(BaseModel):
    """Запрос на добавление ключа провайдера."""

    api_key: str = Field(min_length=1, max_length=1024)


class CabinetAIProviderRemoveKeyRequest(BaseModel):
    """Запрос на удаление ключа провайдера."""

    key_index: int = Field(ge=0)


class CabinetAIProviderSetModelRequest(BaseModel):
    """Запрос на выбор модели для провайдера."""

    model: str = Field(min_length=1, max_length=512)


class CabinetAIProviderTestResponse(BaseModel):
    """Ответ тестирования провайдера."""

    ok: bool
    models: list[str] = Field(default_factory=list)
    count: int = 0
    error: str | None = None


# ─── Prompt schemas ───

class CabinetAIPromptResponse(BaseModel):
    """Ответ системного промпта ИИ."""

    is_custom: bool
    prompt: str
    service_name: str

    class Config:
        from_attributes = True


class CabinetAIPromptUpdateRequest(BaseModel):
    """Запрос на обновление системного промпта."""

    prompt: str = Field(min_length=1, max_length=50000)
