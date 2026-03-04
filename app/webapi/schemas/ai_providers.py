from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


# ─── Provider schemas ───

class AIProviderKeyResponse(BaseModel):
    index: int
    masked: str  # sk-abcd...efgh
    is_active: bool


class AIProviderResponse(BaseModel):
    name: str
    enabled: bool
    priority: int
    keys_count: int
    keys: list[AIProviderKeyResponse] = Field(default_factory=list)
    active_key_index: int
    selected_model: str | None = None
    available_models: list[str] = Field(default_factory=list)
    base_url: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AIProviderToggleRequest(BaseModel):
    enabled: bool


class AIProviderPriorityRequest(BaseModel):
    priority: int = Field(ge=0, le=100)


class AIProviderAddKeyRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=1024)


class AIProviderRemoveKeyRequest(BaseModel):
    key_index: int = Field(ge=0)


class AIProviderSetModelRequest(BaseModel):
    model: str = Field(min_length=1, max_length=512)


class AIProviderTestResponse(BaseModel):
    ok: bool
    models: list[str] = Field(default_factory=list)
    count: int = 0
    error: str | None = None


# ─── Prompt schemas ───

class AIPromptResponse(BaseModel):
    is_custom: bool
    prompt: str
    service_name: str


class AIPromptUpdateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=50000)
