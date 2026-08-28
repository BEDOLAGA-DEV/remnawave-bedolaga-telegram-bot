from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class PartnerReferrerItem(BaseModel):
    id: int
    telegram_id: int | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    referral_code: str | None = None
    referral_commission_percent: int | None = None
    effective_referral_commission_percent: int
    invited_count: int
    active_referrals: int
    total_earned_kopeks: int
    total_earned_rubles: float
    month_earned_kopeks: int
    month_earned_rubles: float
    created_at: datetime
    last_activity: datetime | None = None


class PartnerReferrerListResponse(BaseModel):
    items: list[PartnerReferrerItem] = Field(default_factory=list)
    total: int
    limit: int
    offset: int


class PartnerReferralItem(BaseModel):
    id: int
    telegram_id: int | None = None
    full_name: str
    username: str | None = None
    created_at: datetime
    last_activity: datetime | None = None
    has_made_first_topup: bool
    balance_kopeks: int
    balance_rubles: float
    total_earned_kopeks: int
    total_earned_rubles: float
    topups_count: int
    days_since_registration: int
    days_since_activity: int | None = None
    status: str


class PartnerReferralList(BaseModel):
    items: list[PartnerReferralItem] = Field(default_factory=list)
    total: int
    limit: int
    offset: int
    has_next: bool
    has_prev: bool
    current_page: int
    total_pages: int


class PartnerReferrerDetail(BaseModel):
    referrer: PartnerReferrerItem
    referrals: PartnerReferralList


class PartnerReferralCommissionUpdate(BaseModel):
    referral_commission_percent: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description='Индивидуальный процент реферальной комиссии для пользователя',
    )


# ============================================================================
# РАСШИРЕННАЯ СТАТИСТИКА ПАРТНЁРОВ
# ============================================================================


class EarningsByPeriod(BaseModel):
    """Заработки по периодам.

    Дни — вторая валюта программы: награда днями имеет ``amount_kopeks == 0``, и
    без своих полей внешний API отдаёт нули на установке, где начисления идут
    днями подписки.
    """

    all_time_kopeks: int
    year_kopeks: int
    month_kopeks: int
    week_kopeks: int
    today_kopeks: int
    all_time_days: int = 0
    month_days: int = 0


class ReferralsCountByPeriod(BaseModel):
    """Количество рефералов по периодам."""

    all_time: int
    year: int
    month: int
    week: int
    today: int


class ReferrerSummary(BaseModel):
    """Сводка по рефереру."""

    total_referrals: int
    paid_referrals: int
    active_referrals: int
    conversion_to_paid_percent: float
    conversion_to_active_percent: float
    avg_earnings_per_referral_kopeks: float


class ReferrerDetailedStats(BaseModel):
    """Детальная статистика реферера."""

    user_id: int
    summary: ReferrerSummary
    earnings: EarningsByPeriod
    referrals_count: ReferralsCountByPeriod
    # Какую часть дохода даёт глубина сети — без этого «сколько заработал
    # партнёр» не отвечает на вопрос, окупается ли цепочка.
    earnings_by_level: list[RewardsByLevel] = []


class DailyStats(BaseModel):
    """Статистика за день."""

    date: str
    referrals_count: int
    earnings_kopeks: int


class DailyStatsResponse(BaseModel):
    """Ответ со статистикой по дням."""

    items: list[DailyStats]
    days: int
    user_id: int | None = None


class TopReferralItem(BaseModel):
    """Топ реферал."""

    id: int
    telegram_id: int | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    full_name: str
    created_at: datetime
    has_made_first_topup: bool
    is_active: bool
    total_earnings_kopeks: int
    total_earnings_days: int = 0


class TopReferralsResponse(BaseModel):
    """Топ рефералов реферера."""

    items: list[TopReferralItem]
    user_id: int


class PeriodData(BaseModel):
    """Данные за период."""

    days: int
    start: str
    end: str
    referrals_count: int
    earnings_kopeks: int


class ChangeData(BaseModel):
    """Данные об изменении."""

    absolute: int
    percent: float
    trend: str  # up, down, stable


class PeriodChange(BaseModel):
    """Изменения между периодами."""

    referrals: ChangeData
    earnings: ChangeData


class PeriodComparisonResponse(BaseModel):
    """Сравнение периодов."""

    current_period: PeriodData
    previous_period: PeriodData
    change: PeriodChange
    user_id: int | None = None


class GlobalPartnerSummary(BaseModel):
    """Глобальная сводка партнёрской программы."""

    total_referrers: int
    total_referrals: int
    paid_referrals: int
    conversion_rate_percent: float
    avg_earnings_per_referral_kopeks: float


class PayoutsByPeriod(BaseModel):
    """Выплаты по периодам."""

    all_time_kopeks: int
    year_kopeks: int
    month_kopeks: int
    week_kopeks: int
    today_kopeks: int
    all_time_days: int = 0
    month_days: int = 0


class RewardsByLevel(BaseModel):
    """Разбивка по уровням цепочки: главная величина многоуровневой схемы."""

    level: int
    money_kopeks: int
    days: int


class NewReferralsByPeriod(BaseModel):
    """Новые рефералы по периодам."""

    today: int
    week: int
    month: int


class GlobalPartnerStats(BaseModel):
    """Глобальная статистика партнёрской программы."""

    summary: GlobalPartnerSummary
    payouts: PayoutsByPeriod
    new_referrals: NewReferralsByPeriod
    payouts_by_level: list[RewardsByLevel] = []


class TopReferrerItem(BaseModel):
    """Топ реферер."""

    id: int
    telegram_id: int | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    full_name: str
    referral_code: str | None = None
    referrals_count: int
    total_earnings_kopeks: int
    total_earnings_days: int = 0


class TopReferrersResponse(BaseModel):
    """Топ рефереров."""

    items: list[TopReferrerItem]
    days: int | None = None
