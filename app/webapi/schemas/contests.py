from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from pydantic import BaseModel, Field

from app.localization.texts import get_texts


class ContestTemplateResponse(BaseModel):
    id: int
    name: str
    slug: str
    description: str | None = None
    prize_type: str
    prize_value: str
    max_winners: int
    attempts_per_user: int
    times_per_day: int
    schedule_times: str | None = None
    cooldown_hours: int
    payload: dict[str, Any] = Field(default_factory=dict)
    is_enabled: bool
    created_at: datetime
    updated_at: datetime


class ContestTemplateListResponse(BaseModel):
    items: list[ContestTemplateResponse]


class ContestTemplateUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    prize_type: str | None = None
    prize_value: str | None = None
    max_winners: int | None = Field(None, ge=1)
    attempts_per_user: int | None = Field(None, ge=1)
    times_per_day: int | None = Field(None, ge=1)
    schedule_times: str | None = None
    cooldown_hours: int | None = Field(None, ge=1)
    payload: dict[str, Any] | None = None
    is_enabled: bool | None = None


class StartRoundRequest(BaseModel):
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    cooldown_hours: int | None = Field(None, ge=1)
    payload: dict[str, Any] | None = None
    force: bool = False


class ContestRoundResponse(BaseModel):
    id: int
    template_id: int
    template_slug: str
    template_name: str | None = None
    starts_at: datetime
    ends_at: datetime
    status: str
    payload: dict[str, Any] = Field(default_factory=dict)
    winners_count: int
    max_winners: int
    attempts_per_user: int
    created_at: datetime
    updated_at: datetime


class ContestRoundListResponse(BaseModel):
    items: list[ContestRoundResponse]
    total: int
    limit: int
    offset: int


class ContestAttemptUser(BaseModel):
    id: int
    telegram_id: int | None = None
    username: str | None = None
    full_name: str | None = None


class ContestAttemptResponse(BaseModel):
    id: int
    round_id: int
    user: ContestAttemptUser
    answer: str | None = None
    is_winner: bool
    created_at: datetime


class ContestAttemptListResponse(BaseModel):
    items: list[ContestAttemptResponse]
    total: int
    limit: int
    offset: int


class ReferralContestResponse(BaseModel):
    id: int
    title: str
    description: str | None = None
    prize_text: str | None = None
    contest_type: str
    start_at: datetime
    end_at: datetime
    daily_summary_time: time
    daily_summary_times: str | None = None
    timezone: str
    is_active: bool
    last_daily_summary_date: date | None = None
    last_daily_summary_at: datetime | None = None
    final_summary_sent: bool
    created_by: int | None = None
    created_at: datetime
    updated_at: datetime


class ReferralContestListResponse(BaseModel):
    items: list[ReferralContestResponse]
    total: int
    limit: int
    offset: int


class ReferralContestCreateRequest(BaseModel):
    title: str
    description: str | None = None
    prize_text: str | None = None
    contest_type: str = Field('referral_paid', min_length=1)
    start_at: datetime
    end_at: datetime
    daily_summary_time: time = Field(default=time(hour=12))
    daily_summary_times: str | None = Field(
        default=None,
        description=get_texts().t(
            'REFERRAL_CONTEST_DAILY_SUMMARY_TIMES_EXAMPLE_DESCRIPTION',
            'Список времён ЧЧ:ММ через запятую (например, 12:00,18:00)',
        ),
    )
    timezone: str = Field(default='UTC')
    is_active: bool = True
    created_by: int | None = None


class ReferralContestUpdateRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    prize_text: str | None = None
    contest_type: str | None = Field(None, min_length=1)
    start_at: datetime | None = None
    end_at: datetime | None = None
    daily_summary_time: time | None = None
    daily_summary_times: str | None = Field(
        default=None,
        description=get_texts().t(
            'REFERRAL_CONTEST_DAILY_SUMMARY_TIMES_DESCRIPTION',
            'Список времён ЧЧ:ММ через запятую',
        ),
    )
    timezone: str | None = None
    is_active: bool | None = None
    final_summary_sent: bool | None = None
    created_by: int | None = None


class ReferralContestLeaderboardItem(BaseModel):
    user_id: int
    telegram_id: int | None = None
    username: str | None = None
    full_name: str | None = None
    referrals_count: int
    total_amount_kopeks: int
    total_amount_rubles: float


class ReferralContestDetailResponse(ReferralContestResponse):
    total_events: int | None = None
    leaderboard: list[ReferralContestLeaderboardItem] | None = None


class ReferralContestEventUser(BaseModel):
    id: int
    telegram_id: int | None = None
    username: str | None = None
    full_name: str | None = None


class ReferralContestEventResponse(BaseModel):
    id: int
    contest_id: int
    referrer: ReferralContestEventUser
    referral: ReferralContestEventUser
    event_type: str
    amount_kopeks: int
    amount_rubles: float
    occurred_at: datetime


class ReferralContestEventListResponse(BaseModel):
    items: list[ReferralContestEventResponse]
    total: int
    limit: int
    offset: int


class ReferralContestParticipant(BaseModel):
    referrer_id: int
    full_name: str
    total_referrals: int
    paid_referrals: int
    unpaid_referrals: int
    total_paid_amount: int


class ReferralContestDetailedStatsResponse(BaseModel):
    total_participants: int
    total_invited: int
    total_paid_amount: int
    total_unpaid: int
    participants: list[ReferralContestParticipant]
