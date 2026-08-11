from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class PromoGroupSummary(BaseModel):
    id: int
    name: str
    server_discount_percent: int
    traffic_discount_percent: int
    device_discount_percent: int
    apply_discounts_to_addons: bool = True


class SubscriptionSummary(BaseModel):
    id: int
    status: str
    actual_status: str
    is_trial: bool
    start_date: datetime
    end_date: datetime
    traffic_limit_gb: int
    traffic_used_gb: float
    device_limit: int
    autopay_enabled: bool
    autopay_days_before: int | None = None
    subscription_url: str | None = None
    subscription_crypto_link: str | None = None
    connected_squads: list[str] = Field(default_factory=list)
    tariff_id: int | None = None
    tariff_name: str | None = None


class UserResponse(BaseModel):
    id: int
    telegram_id: int | None = None
    email: str | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    status: str
    language: str
    balance_kopeks: int
    balance_rubles: float
    referral_code: str | None = None
    referred_by_id: int | None = None
    has_had_paid_subscription: bool
    has_made_first_topup: bool
    created_at: datetime
    updated_at: datetime
    last_activity: datetime | None = None
    promo_group: PromoGroupSummary | None = None
    subscription: SubscriptionSummary | None = None
    subscriptions: list[SubscriptionSummary] = Field(default_factory=list)


class UserListResponse(BaseModel):
    items: list[UserResponse]
    total: int
    limit: int
    offset: int


class UserCreateRequest(BaseModel):
    telegram_id: int | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    language: str = 'ru'
    referred_by_id: int | None = None
    promo_group_id: int | None = None


class UserUpdateRequest(BaseModel):
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    language: str | None = None
    status: str | None = None
    promo_group_id: int | None = None
    referral_code: str | None = None
    has_had_paid_subscription: bool | None = None
    has_made_first_topup: bool | None = None


class BalanceUpdateRequest(BaseModel):
    amount_kopeks: int = Field(..., ge=-100_000_000, le=100_000_000)
    description: str | None = Field(default='Корректировка через веб-API')
    create_transaction: bool = True


class BalanceDepositRequest(BaseModel):
    """Ручное пополнение баланса. Рассчитано на автоматизацию (агент поддержки)."""

    amount_kopeks: int = Field(
        ...,
        gt=0,
        le=100_000_000,
        description='Сумма пополнения в копейках. Только положительная: эндпоинт умеет лишь зачислять.',
    )
    idempotency_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description=(
            'Ключ идемпотентности (номер тикета, uuid попытки). Повторный запрос с тем же ключом '
            'не начислит деньги второй раз и вернёт исходный результат с duplicate=true. '
            'Обязателен по-хорошему для любой автоматической интеграции: без него сетевой таймаут '
            'и ретрай приведут к двойному начислению.'
        ),
    )
    description: str | None = Field(
        default=None,
        max_length=500,
        description='Описание транзакции. Видно пользователю в истории операций.',
    )
    notify_user: bool = Field(
        default=True,
        description='Отправить пользователю уведомление о пополнении (Telegram или email).',
    )
    apply_topup_bonuses: bool = Field(
        default=True,
        description=(
            'Запустить те же авто-действия, что и настоящий платёж: реферальная комиссия, '
            'отметка первого пополнения, возобновление приостановленной суточной подписки, '
            'автопокупка сохранённой корзины. Выключайте, только если нужно именно «просто деньги».'
        ),
    )


class BalanceDepositResponse(BaseModel):
    success: bool
    duplicate: bool = Field(
        ...,
        description='true — запрос с таким idempotency_key уже был обработан, баланс не менялся.',
    )
    user_id: int
    telegram_id: int | None = None
    transaction_id: int
    amount_kopeks: int = Field(..., description='Сумма проведённой транзакции.')
    old_balance_kopeks: int
    new_balance_kopeks: int
    new_balance_rubles: float


class UserSubscriptionCreateRequest(BaseModel):
    """Схема для создания подписки через users API (user_id берется из URL)"""

    is_trial: bool = False
    duration_days: int | None = None
    traffic_limit_gb: int | None = None
    device_limit: int | None = None
    squad_uuid: str | None = None
    connected_squads: list[str] | None = None
    replace_existing: bool = False
    subscription_id: int | None = Field(
        default=None,
        description='ID of existing subscription to replace (required in multi-tariff mode when replace_existing=true)',
    )
