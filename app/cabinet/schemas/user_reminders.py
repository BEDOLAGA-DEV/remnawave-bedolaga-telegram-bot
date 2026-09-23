"""Схемы напоминаний: карточка пользователя и админ-API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.user_reminders.conditions import ReminderConditions
from app.services.user_reminders.texts import validate_button, validate_texts


class ReminderCardButton(BaseModel):
    kind: Literal['cabinet', 'url']
    target: str
    text: str


class ReminderCard(BaseModel):
    id: int
    title: str
    body: str
    button: ReminderCardButton | None = None


Channels = Literal['bot', 'cabinet', 'both']


class ReminderPayload(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str = Field(min_length=1, max_length=120)
    channels: Channels
    category: Literal['service', 'marketing'] = 'service'
    conditions: ReminderConditions = Field(default_factory=ReminderConditions)
    repeat_every_days: int = Field(7, ge=1, le=365)
    max_sends: int = Field(1, ge=1, le=20)
    texts: dict[str, dict]
    button_kind: Literal['none', 'cabinet', 'url'] = 'none'
    button_target: str | None = Field(None, max_length=500)

    @field_validator('texts')
    @classmethod
    def _texts(cls, value: dict) -> dict:
        return validate_texts(value)

    @model_validator(mode='after')
    def _button(self) -> ReminderPayload:
        validate_button(self.button_kind, self.button_target, self.texts)
        return self


class ReminderStats(BaseModel):
    sent_total: int = 0
    dismissed_total: int = 0
    audience_bot: int | None = None
    audience_cabinet: int | None = None


class ReminderResponse(BaseModel):
    """Форма для чтения (GET/list) — без валидаторов записи.

    Намеренно НЕ наследует ``ReminderPayload``: та прогоняет ``validate_texts``/
    ``validate_button``/``ReminderConditions`` при каждой сериализации, и битая
    строка, уже сохранённая в БД, роняла бы GET / list в 500 — админ не смог бы
    её даже увидеть, не то что исправить через тот же API.
    """

    model_config = ConfigDict(extra='ignore')

    id: int
    name: str
    channels: str
    category: str
    conditions: dict
    repeat_every_days: int
    max_sends: int
    texts: dict
    button_kind: str
    button_target: str | None = None
    is_active: bool
    is_builtin: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None
    stats: ReminderStats = Field(default_factory=ReminderStats)


class AudienceRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    conditions: ReminderConditions = Field(default_factory=ReminderConditions)
    channels: Channels
    category: Literal['service', 'marketing'] = 'service'


class AudienceResponse(BaseModel):
    bot: int | None = None
    cabinet: int | None = None
