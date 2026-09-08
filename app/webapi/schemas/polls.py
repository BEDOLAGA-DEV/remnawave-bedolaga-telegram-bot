from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from app.localization.texts import get_texts


class PollOptionCreate(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)

    @field_validator('text')
    @classmethod
    def strip_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError(get_texts().t('POLL_OPTION_TEXT_EMPTY', 'Option text cannot be empty'))
        return text


class PollQuestionCreate(BaseModel):
    text: str = Field(..., min_length=1, max_length=1000)
    options: list[PollOptionCreate] = Field(..., min_length=2)

    @field_validator('text')
    @classmethod
    def strip_question_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError(get_texts().t('POLL_QUESTION_TEXT_EMPTY', 'Question text cannot be empty'))
        return text

    @field_validator('options')
    @classmethod
    def validate_options(cls, value: list[PollOptionCreate]) -> list[PollOptionCreate]:
        seen: set[str] = set()
        for option in value:
            normalized = option.text.lower()
            if normalized in seen:
                raise ValueError(
                    get_texts().t('POLL_OPTION_TEXTS_NOT_UNIQUE', 'Option texts must be unique within a question')
                )
            seen.add(normalized)
        return value


class PollCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4000)
    reward_enabled: bool = False
    reward_amount_kopeks: int = Field(default=0, ge=0, le=1_000_000_000)
    questions: list[PollQuestionCreate] = Field(..., min_length=1)

    @field_validator('title')
    @classmethod
    def strip_title(cls, value: str) -> str:
        title = value.strip()
        if not title:
            raise ValueError(get_texts().t('POLL_TITLE_EMPTY', 'Title cannot be empty'))
        return title

    @field_validator('description')
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        description = value.strip()
        return description or None

    @model_validator(mode='after')
    def validate_reward(self) -> PollCreateRequest:
        if self.reward_enabled and self.reward_amount_kopeks <= 0:
            raise ValueError(
                get_texts().t(
                    'POLL_REWARD_AMOUNT_MUST_BE_POSITIVE', 'Reward amount must be positive when rewards are enabled'
                )
            )
        if not self.reward_enabled:
            self.reward_amount_kopeks = 0
        return self


class PollQuestionOptionResponse(BaseModel):
    id: int
    text: str
    order: int


class PollQuestionResponse(BaseModel):
    id: int
    text: str
    order: int
    options: list[PollQuestionOptionResponse]


class PollSummaryResponse(BaseModel):
    id: int
    title: str
    description: str | None
    reward_enabled: bool
    reward_amount_kopeks: int
    reward_amount_rubles: float
    questions_count: int
    responses_count: int
    created_at: datetime
    updated_at: datetime


class PollDetailResponse(BaseModel):
    id: int
    title: str
    description: str | None
    reward_enabled: bool
    reward_amount_kopeks: int
    reward_amount_rubles: float
    questions: list[PollQuestionResponse]
    created_at: datetime
    updated_at: datetime


class PollListResponse(BaseModel):
    items: list[PollSummaryResponse]
    total: int
    limit: int
    offset: int


class PollOptionStats(BaseModel):
    id: int
    text: str
    count: int


class PollQuestionStats(BaseModel):
    id: int
    text: str
    order: int
    options: list[PollOptionStats]


class PollStatisticsResponse(BaseModel):
    poll_id: int
    poll_title: str
    total_responses: int
    completed_responses: int
    reward_sum_kopeks: int
    reward_sum_rubles: float
    questions: list[PollQuestionStats]


class PollAnswerResponse(BaseModel):
    question_id: int | None
    question_text: str | None
    option_id: int | None
    option_text: str | None
    created_at: datetime


class PollUserResponse(BaseModel):
    id: int
    user_id: int | None
    user_telegram_id: int | None
    user_username: str | None
    sent_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    reward_given: bool
    reward_amount_kopeks: int
    reward_amount_rubles: float
    answers: list[PollAnswerResponse]


class PollResponsesListResponse(BaseModel):
    items: list[PollUserResponse]
    total: int
    limit: int
    offset: int


class PollSendRequest(BaseModel):
    target: str = Field(
        ...,
        description=get_texts().t(
            'POLL_SEND_TARGET_DESCRIPTION',
            'Аудитория для отправки опроса (например: all, active, trial, custom_today и т.д.)',
        ),
        max_length=100,
    )


class PollSendResponse(BaseModel):
    poll_id: int
    target: str
    sent: int
    failed: int
    skipped: int
    total: int
