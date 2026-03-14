from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MiniAppBranding(BaseModel):
    service_name: dict[str, str | None] = Field(default_factory=dict)
    service_description: dict[str, str | None] = Field(default_factory=dict)


class MiniAppSubscriptionRequest(BaseModel):
    init_data: str = Field(..., alias='initData')


class MiniAppMaintenanceStatusResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    is_active: bool = Field(..., alias='isActive')
    message: str | None = None
    reason: str | None = None


class MiniAppSubscriptionUser(BaseModel):
    telegram_id: int | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    display_name: str
    language: str | None = None
    status: str
    subscription_status: str
    subscription_actual_status: str
    status_label: str
    expires_at: datetime | None = None
    device_limit: int | None = None
    traffic_used_gb: float = 0.0
    traffic_used_label: str
    traffic_limit_gb: int | None = None
    traffic_limit_label: str
    lifetime_used_traffic_gb: float = 0.0
    has_active_subscription: bool = False
    promo_offer_discount_percent: int = 0
    promo_offer_discount_expires_at: datetime | None = None
    promo_offer_discount_source: str | None = None
    # Суточные тарифы
    is_daily_tariff: bool = False
    is_daily_paused: bool = False
    daily_tariff_name: str | None = None
    daily_price_kopeks: int | None = None
    daily_price_label: str | None = None
    daily_next_charge_at: datetime | None = None  # Время следующего списания


class MiniAppPromoGroup(BaseModel):
    id: int
    name: str
    server_discount_percent: int = 0
    traffic_discount_percent: int = 0
    device_discount_percent: int = 0
    period_discounts: dict[str, int] = Field(default_factory=dict)
    apply_discounts_to_addons: bool = True


class MiniAppAutoPromoGroupLevel(BaseModel):
    id: int
    name: str
    threshold_kopeks: int
    threshold_rubles: float
    threshold_label: str
    is_reached: bool = False
    is_current: bool = False
    server_discount_percent: int = 0
    traffic_discount_percent: int = 0
    device_discount_percent: int = 0
    period_discounts: dict[str, int] = Field(default_factory=dict)
    apply_discounts_to_addons: bool = True


class MiniAppConnectedServer(BaseModel):
    uuid: str
    name: str


class MiniAppDevice(BaseModel):
    hwid: str | None = None
    platform: str | None = None
    device_model: str | None = None
    app_version: str | None = None
    last_seen: str | None = None
    last_ip: str | None = None


class MiniAppDeviceRemovalRequest(BaseModel):
    init_data: str = Field(..., alias='initData')
    hwid: str


class MiniAppDeviceRemovalResponse(BaseModel):
    success: bool = True
    message: str | None = None


class MiniAppTransaction(BaseModel):
    id: int
    type: str
    amount_kopeks: int
    amount_rubles: float
    description: str | None = None
    payment_method: str | None = None
    external_id: str | None = None
    is_completed: bool
    created_at: datetime
    completed_at: datetime | None = None


class MiniAppPromoOffer(BaseModel):
    id: int
    status: str
    notification_type: str | None = None
    offer_type: str | None = None
    effect_type: str | None = None
    discount_percent: int = 0
    bonus_amount_kopeks: int = 0
    bonus_amount_label: str | None = None
    expires_at: datetime | None = None
    claimed_at: datetime | None = None
    is_active: bool = False
    template_id: int | None = None
    template_name: str | None = None
    button_text: str | None = None
    title: str | None = None
    message_text: str | None = None
    icon: str | None = None
    test_squads: list[MiniAppConnectedServer] = Field(default_factory=list)
    active_discount_expires_at: datetime | None = None
    active_discount_started_at: datetime | None = None
    active_discount_duration_seconds: int | None = None


class MiniAppPromoOfferClaimRequest(BaseModel):
    init_data: str = Field(..., alias='initData')


class MiniAppPromoOfferClaimResponse(BaseModel):
    success: bool = True
    code: str | None = None


class MiniAppSubscriptionAutopay(BaseModel):
    enabled: bool = False
    autopay_enabled: bool | None = None
    autopay_enabled_at: datetime | None = None
    days_before: int | None = None
    autopay_days_before: int | None = None
    default_days_before: int | None = None
    autopay_days_options: list[int] = Field(default_factory=list)
    days_options: list[int] = Field(default_factory=list)
    options: list[int] = Field(default_factory=list)

    model_config = ConfigDict(extra='allow')


class MiniAppSubscriptionRenewalPeriod(BaseModel):
    id: str
    days: int | None = None
    months: int | None = None
    price_kopeks: int | None = Field(default=None, alias='priceKopeks')
    price_label: str | None = Field(default=None, alias='priceLabel')
    original_price_kopeks: int | None = Field(default=None, alias='originalPriceKopeks')
    original_price_label: str | None = Field(default=None, alias='originalPriceLabel')
    discount_percent: int = Field(default=0, alias='discountPercent')
    price_per_month_kopeks: int | None = Field(default=None, alias='pricePerMonthKopeks')
    price_per_month_label: str | None = Field(default=None, alias='pricePerMonthLabel')
    is_recommended: bool = Field(default=False, alias='isRecommended')
    description: str | None = None
    badge: str | None = None
    title: str | None = None

    model_config = ConfigDict(populate_by_name=True)


class MiniAppSubscriptionRenewalOptionsRequest(BaseModel):
    init_data: str = Field(..., alias='initData')
    subscription_id: int | None = Field(default=None, alias='subscriptionId')

    model_config = ConfigDict(populate_by_name=True)


class MiniAppSubscriptionRenewalOptionsResponse(BaseModel):
    success: bool = True
    subscription_id: int | None = Field(default=None, alias='subscriptionId')
    currency: str
    balance_kopeks: int | None = Field(default=None, alias='balanceKopeks')
    balance_label: str | None = Field(default=None, alias='balanceLabel')
    promo_group: MiniAppPromoGroup | None = Field(default=None, alias='promoGroup')
    promo_offer: dict[str, Any] | None = Field(default=None, alias='promoOffer')
    periods: list[MiniAppSubscriptionRenewalPeriod] = Field(default_factory=list)
    default_period_id: str | None = Field(default=None, alias='defaultPeriodId')
    missing_amount_kopeks: int | None = Field(default=None, alias='missingAmountKopeks')
    status_message: str | None = Field(default=None, alias='statusMessage')
    autopay_enabled: bool = False
    autopay_days_before: int | None = None
    autopay_days_options: list[int] = Field(default_factory=list)
    autopay: MiniAppSubscriptionAutopay | None = None
    autopay_settings: MiniAppSubscriptionAutopay | None = None
    # Флаги для определения типа действия (покупка vs продление)
    is_trial: bool = Field(default=False, alias='isTrial')
    sales_mode: str = Field(default='classic', alias='salesMode')

    model_config = ConfigDict(populate_by_name=True, extra='allow')


class MiniAppSubscriptionRenewalRequest(BaseModel):
    init_data: str = Field(..., alias='initData')
    subscription_id: int | None = Field(default=None, alias='subscriptionId')
    period_id: str | None = Field(default=None, alias='periodId')
    period_days: int | None = Field(default=None, alias='periodDays')
    method: str | None = None

    model_config = ConfigDict(populate_by_name=True)


class MiniAppSubscriptionRenewalResponse(BaseModel):
    success: bool = True
    message: str | None = None
    balance_kopeks: int | None = Field(default=None, alias='balanceKopeks')
    balance_label: str | None = Field(default=None, alias='balanceLabel')
    subscription_id: int | None = Field(default=None, alias='subscriptionId')
    renewed_until: datetime | None = Field(default=None, alias='renewedUntil')
    requires_payment: bool = Field(default=False, alias='requiresPayment')
    payment_method: str | None = Field(default=None, alias='paymentMethod')
    payment_url: str | None = Field(default=None, alias='paymentUrl')
    payment_amount_kopeks: int | None = Field(default=None, alias='paymentAmountKopeks')
    payment_id: int | None = Field(default=None, alias='paymentId')
    invoice_id: str | None = Field(default=None, alias='invoiceId')
    payment_payload: str | None = Field(default=None, alias='paymentPayload')
    payment_extra: dict[str, Any] | None = Field(default=None, alias='paymentExtra')

    model_config = ConfigDict(populate_by_name=True)


class MiniAppSubscriptionAutopayRequest(BaseModel):
    init_data: str = Field(..., alias='initData')
    subscription_id: int | None = Field(default=None, alias='subscriptionId')
    enabled: bool | None = None
    days_before: int | None = Field(default=None, alias='daysBefore')

    model_config = ConfigDict(populate_by_name=True)


class MiniAppSubscriptionAutopayResponse(BaseModel):
    success: bool = True
    subscription_id: int | None = Field(default=None, alias='subscriptionId')
    autopay_enabled: bool = False
    autopay_days_before: int | None = None
    autopay_days_options: list[int] = Field(default_factory=list)
    autopay: MiniAppSubscriptionAutopay | None = None
    autopay_settings: MiniAppSubscriptionAutopay | None = None

    model_config = ConfigDict(populate_by_name=True, extra='allow')


class MiniAppPromoCode(BaseModel):
    code: str
    type: str | None = None
    balance_bonus_kopeks: int = 0
    subscription_days: int = 0
    max_uses: int | None = None
    current_uses: int | None = None
    valid_until: datetime | None = None


class MiniAppPromoCodeActivationRequest(BaseModel):
    init_data: str = Field(..., alias='initData')
    code: str


class MiniAppPromoCodeActivationResponse(BaseModel):
    success: bool = True
    description: str | None = None
    promocode: MiniAppPromoCode | None = None


class MiniAppFaqItem(BaseModel):
    id: int
    title: str | None = None
    content: str | None = None
    display_order: int | None = None


class MiniAppFaq(BaseModel):
    requested_language: str
    language: str
    is_enabled: bool = True
    total: int = 0
    items: list[MiniAppFaqItem] = Field(default_factory=list)


class MiniAppRichTextDocument(BaseModel):
    requested_language: str
    language: str
    title: str | None = None
    is_enabled: bool = True
    content: str = ''
    created_at: datetime | None = None
    updated_at: datetime | None = None


class MiniAppLegalDocuments(BaseModel):
    public_offer: MiniAppRichTextDocument | None = None
    service_rules: MiniAppRichTextDocument | None = None
    privacy_policy: MiniAppRichTextDocument | None = None


class MiniAppReferralTerms(BaseModel):
    minimum_topup_kopeks: int = 0
    minimum_topup_label: str | None = None
    first_topup_bonus_kopeks: int = 0
    first_topup_bonus_label: str | None = None
    inviter_bonus_kopeks: int = 0
    inviter_bonus_label: str | None = None
    commission_percent: float = 0.0


class MiniAppReferralStats(BaseModel):
    invited_count: int = 0
    paid_referrals_count: int = 0
    active_referrals_count: int = 0
    total_earned_kopeks: int = 0
    total_earned_label: str | None = None
    month_earned_kopeks: int = 0
    month_earned_label: str | None = None
    conversion_rate: float = 0.0


class MiniAppReferralRecentEarning(BaseModel):
    amount_kopeks: int = 0
    amount_label: str | None = None
    reason: str | None = None
    referral_name: str | None = None
    created_at: datetime | None = None


class MiniAppReferralItem(BaseModel):
    id: int
    telegram_id: int | None = None
    full_name: str | None = None
    username: str | None = None
    created_at: datetime | None = None
    last_activity: datetime | None = None
    has_made_first_topup: bool = False
    balance_kopeks: int = 0
    balance_label: str | None = None
    total_earned_kopeks: int = 0
    total_earned_label: str | None = None
    topups_count: int = 0
    days_since_registration: int | None = None
    days_since_activity: int | None = None
    status: str | None = None


class MiniAppReferralList(BaseModel):
    total_count: int = 0
    has_next: bool = False
    has_prev: bool = False
    current_page: int = 1
    total_pages: int = 1
    items: list[MiniAppReferralItem] = Field(default_factory=list)


class MiniAppReferralInfo(BaseModel):
    referral_code: str | None = None
    referral_link: str | None = None
    terms: MiniAppReferralTerms | None = None
    stats: MiniAppReferralStats | None = None
    recent_earnings: list[MiniAppReferralRecentEarning] = Field(default_factory=list)
    referrals: MiniAppReferralList | None = None


class MiniAppPaymentMethodsRequest(BaseModel):
    init_data: str = Field(..., alias='initData')


class MiniAppPaymentIntegrationType(StrEnum):
    IFRAME = 'iframe'
    REDIRECT = 'redirect'


class MiniAppPaymentOption(BaseModel):
    id: str
    icon: str | None = None
    title: str | None = None
    description: str | None = None
    title_key: str | None = Field(default=None, alias='titleKey')
    description_key: str | None = Field(default=None, alias='descriptionKey')

    model_config = ConfigDict(populate_by_name=True)


class MiniAppPaymentIframeConfig(BaseModel):
    expected_origin: str

    @model_validator(mode='after')
    def _normalize_expected_origin(cls, values: MiniAppPaymentIframeConfig) -> MiniAppPaymentIframeConfig:
        origin = (values.expected_origin or '').strip()
        if not origin:
            raise ValueError('expected_origin must not be empty')

        parsed = urlparse(origin)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError('expected_origin must include scheme and host')

        values.expected_origin = f'{parsed.scheme}://{parsed.netloc}'
        return values


class MiniAppPaymentMethod(BaseModel):
    id: str
    name: str | None = None
    icon: str | None = None
    requires_amount: bool = False
    currency: str = 'RUB'
    min_amount_kopeks: int | None = None
    max_amount_kopeks: int | None = None
    amount_step_kopeks: int | None = None
    integration_type: MiniAppPaymentIntegrationType
    options: list[MiniAppPaymentOption] = Field(default_factory=list)
    iframe_config: MiniAppPaymentIframeConfig | None = None

    @model_validator(mode='after')
    def _ensure_iframe_config(cls, values: MiniAppPaymentMethod) -> MiniAppPaymentMethod:
        if values.integration_type == MiniAppPaymentIntegrationType.IFRAME and values.iframe_config is None:
            raise ValueError("iframe_config is required when integration_type is 'iframe'")
        return values


class MiniAppPaymentMethodsResponse(BaseModel):
    methods: list[MiniAppPaymentMethod] = Field(default_factory=list)


class MiniAppPaymentCreateRequest(BaseModel):
    init_data: str = Field(..., alias='initData')
    method: str
    amount_rubles: float | None = Field(default=None, alias='amountRubles')
    amount_kopeks: int | None = Field(default=None, alias='amountKopeks')
    payment_option: str | None = Field(default=None, alias='option')


class MiniAppPaymentCreateResponse(BaseModel):
    success: bool = True
    method: str
    payment_url: str | None = None
    amount_kopeks: int | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class MiniAppPaymentStatusQuery(BaseModel):
    method: str
    local_payment_id: int | None = Field(default=None, alias='localPaymentId')
    payment_link_id: str | None = Field(default=None, alias='paymentLinkId')
    invoice_id: str | None = Field(default=None, alias='invoiceId')
    payment_id: str | None = Field(default=None, alias='paymentId')
    payload: str | None = None
    amount_kopeks: int | None = Field(default=None, alias='amountKopeks')
    started_at: str | None = Field(default=None, alias='startedAt')


class MiniAppPaymentStatusRequest(BaseModel):
    init_data: str = Field(..., alias='initData')
    payments: list[MiniAppPaymentStatusQuery] = Field(default_factory=list)


class MiniAppPaymentStatusResult(BaseModel):
    method: str
    status: str
    is_paid: bool = False
    amount_kopeks: int | None = None
    currency: str | None = None
    completed_at: datetime | None = None
    transaction_id: int | None = None
    external_id: str | None = None
    message: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class MiniAppPaymentStatusResponse(BaseModel):
    results: list[MiniAppPaymentStatusResult] = Field(default_factory=list)


# =============================================================================
# Тарифы для режима продаж "Тарифы"
# =============================================================================


class MiniAppTariffPeriod(BaseModel):
    """Период тарифа с ценой."""

    days: int
    months: int | None = None
    label: str
    price_kopeks: int
    price_label: str
    price_per_month_kopeks: int | None = None
    price_per_month_label: str | None = None
    # Скидка промогруппы
    original_price_kopeks: int | None = None  # Цена без скидки
    original_price_label: str | None = None
    discount_percent: int = 0  # Процент скидки


class MiniAppTariff(BaseModel):
    """Тариф для отображения в miniapp."""

    id: int
    name: str
    description: str | None = None
    tier_level: int = 1
    traffic_limit_gb: int
    traffic_limit_label: str
    is_unlimited_traffic: bool = False
    device_limit: int
    servers_count: int
    servers: list[MiniAppConnectedServer] = Field(default_factory=list)
    periods: list[MiniAppTariffPeriod] = Field(default_factory=list)
    is_current: bool = False
    is_available: bool = True
    # Для режима мгновенного переключения тарифа
    switch_cost_kopeks: int | None = None  # Стоимость переключения (None если не в режиме switch)
    switch_cost_label: str | None = None  # Форматированная стоимость
    is_upgrade: bool | None = None  # True = повышение, False = понижение
    is_switch_free: bool | None = None  # True = бесплатное переключение
    # Суточные тарифы
    is_daily: bool = False
    daily_price_kopeks: int = 0
    daily_price_label: str | None = None


class MiniAppTrafficTopupPackage(BaseModel):
    """Пакет докупки трафика."""

    gb: int
    price_kopeks: int
    price_label: str
    # Скидка промогруппы на трафик
    original_price_kopeks: int | None = None
    original_price_label: str | None = None
    discount_percent: int = 0


class MiniAppCurrentTariff(BaseModel):
    """Текущий тариф пользователя."""

    id: int
    name: str
    description: str | None = None
    tier_level: int = 1
    traffic_limit_gb: int
    traffic_limit_label: str
    is_unlimited_traffic: bool = False
    device_limit: int
    servers_count: int
    # Месячная цена для расчёта стоимости переключения тарифа
    monthly_price_kopeks: int = 0
    # Докупка трафика
    traffic_topup_enabled: bool = False
    traffic_topup_packages: list[MiniAppTrafficTopupPackage] = Field(default_factory=list)
    # Лимит докупки трафика (0 = без лимита)
    max_topup_traffic_gb: int = 0
    available_topup_gb: int | None = None  # Сколько еще можно докупить (None = без лимита)
    # Суточные тарифы
    is_daily: bool = False
    daily_price_kopeks: int = 0
    daily_price_label: str | None = None


class MiniAppTrafficTopupRequest(BaseModel):
    """Запрос на докупку трафика."""

    init_data: str = Field(..., alias='initData')
    subscription_id: int | None = Field(None, alias='subscriptionId')
    gb: int


class MiniAppTrafficTopupResponse(BaseModel):
    """Ответ на докупку трафика."""

    success: bool = True
    message: str = ''
    new_traffic_limit_gb: int = 0
    new_balance_kopeks: int = 0
    charged_kopeks: int = 0


class MiniAppTariffsRequest(BaseModel):
    """Запрос списка тарифов."""

    init_data: str = Field(..., alias='initData')


class MiniAppTariffsResponse(BaseModel):
    """Ответ со списком тарифов."""

    success: bool = True
    sales_mode: str = 'tariffs'
    tariffs: list[MiniAppTariff] = Field(default_factory=list)
    current_tariff: MiniAppCurrentTariff | None = None
    balance_kopeks: int = 0
    balance_label: str | None = None
    promo_group: MiniAppPromoGroup | None = None  # Промогруппа пользователя для отображения скидок


class MiniAppTariffPurchaseRequest(BaseModel):
    """Запрос на покупку/смену тарифа."""

    init_data: str = Field(..., alias='initData')
    tariff_id: int = Field(..., alias='tariffId')
    period_days: int = Field(..., alias='periodDays')


class MiniAppTariffPurchaseResponse(BaseModel):
    """Ответ на покупку тарифа."""

    success: bool = True
    message: str | None = None
    subscription_id: int | None = None
    tariff_id: int | None = None
    tariff_name: str | None = None
    new_end_date: datetime | None = None
    balance_kopeks: int | None = None
    balance_label: str | None = None


class MiniAppTariffSwitchRequest(BaseModel):
    """Запрос на переключение тарифа (без выбора периода)."""

    init_data: str = Field(...)
    tariff_id: int = Field(...)


class MiniAppTariffSwitchPreviewResponse(BaseModel):
    """Предпросмотр переключения тарифа."""

    can_switch: bool = True
    current_tariff_id: int | None = None
    current_tariff_name: str | None = None
    new_tariff_id: int
    new_tariff_name: str
    remaining_days: int = 0
    upgrade_cost_kopeks: int = 0  # 0 если даунгрейд или равная цена
    upgrade_cost_label: str = ''
    balance_kopeks: int = 0
    balance_label: str = ''
    has_enough_balance: bool = True
    missing_amount_kopeks: int = 0
    missing_amount_label: str = ''
    is_upgrade: bool = False  # True если новый тариф дороже
    message: str | None = None


class MiniAppTariffSwitchResponse(BaseModel):
    """Ответ на переключение тарифа."""

    success: bool = True
    message: str | None = None
    tariff_id: int
    tariff_name: str
    charged_kopeks: int = 0
    balance_kopeks: int = 0
    balance_label: str = ''


class MiniAppDailySubscriptionToggleRequest(BaseModel):
    """Запрос на паузу/возобновление суточной подписки."""

    init_data: str = Field(...)


class MiniAppDailySubscriptionToggleResponse(BaseModel):
    """Ответ на паузу/возобновление суточной подписки."""

    success: bool = True
    message: str | None = None
    is_paused: bool = False
    balance_kopeks: int = 0
    balance_label: str = ''


class MiniAppTrafficPurchase(BaseModel):
    """Докупка трафика с индивидуальной датой истечения."""

    id: int
    traffic_gb: int
    expires_at: datetime
    created_at: datetime
    days_remaining: int
    progress_percent: float


class MiniAppSubscriptionResponse(BaseModel):
    success: bool = True
    subscription_id: int | None = None
    remnawave_short_uuid: str | None = None
    user: MiniAppSubscriptionUser
    traffic_purchases: list[MiniAppTrafficPurchase] = Field(default_factory=list)
    subscription_url: str | None = None
    hide_subscription_link: bool = False  # Скрывать ли отображение ссылки (но кнопки работают)
    subscription_crypto_link: str | None = None
    subscription_purchase_url: str | None = None
    links: list[str] = Field(default_factory=list)
    ss_conf_links: dict[str, str] = Field(default_factory=dict)
    connected_squads: list[str] = Field(default_factory=list)
    connected_servers: list[MiniAppConnectedServer] = Field(default_factory=list)
    connected_devices_count: int = 0
    connected_devices: list[MiniAppDevice] = Field(default_factory=list)
    happ: dict[str, Any] | None = None
    happ_link: str | None = None
    happ_crypto_link: str | None = None
    happ_cryptolink_redirect_link: str | None = None
    happ_cryptolink_redirect_template: str | None = None
    balance_kopeks: int = 0
    balance_rubles: float = 0.0
    balance_currency: str | None = None
    transactions: list[MiniAppTransaction] = Field(default_factory=list)
    promo_offers: list[MiniAppPromoOffer] = Field(default_factory=list)
    promo_group: MiniAppPromoGroup | None = None
    auto_assign_promo_groups: list[MiniAppAutoPromoGroupLevel] = Field(default_factory=list)
    total_spent_kopeks: int = 0
    total_spent_rubles: float = 0.0
    total_spent_label: str | None = None
    subscription_type: str
    autopay_enabled: bool = False
    autopay_days_before: int | None = None
    autopay_days_options: list[int] = Field(default_factory=list)
    autopay: MiniAppSubscriptionAutopay | None = None
    autopay_settings: MiniAppSubscriptionAutopay | None = None
    branding: MiniAppBranding | None = None
    faq: MiniAppFaq | None = None
    legal_documents: MiniAppLegalDocuments | None = None
    referral: MiniAppReferralInfo | None = None
    subscription_missing: bool = False
    subscription_missing_reason: str | None = None
    trial_available: bool = False
    trial_duration_days: int | None = None
    trial_status: str | None = None
    trial_payment_required: bool = Field(default=False, alias='trialPaymentRequired')
    trial_price_kopeks: int | None = Field(default=None, alias='trialPriceKopeks')
    trial_price_label: str | None = Field(default=None, alias='trialPriceLabel')

    # Режим продаж и тариф
    sales_mode: str = Field(default='classic', alias='salesMode')
    current_tariff: MiniAppCurrentTariff | None = Field(default=None, alias='currentTariff')

    model_config = ConfigDict(extra='allow', populate_by_name=True)


class MiniAppSubscriptionServerOption(BaseModel):
    uuid: str
    name: str | None = None
    price_kopeks: int | None = None
    price_label: str | None = None
    discount_percent: int | None = None
    is_connected: bool = False
    is_available: bool = True
    disabled_reason: str | None = None


class MiniAppSubscriptionTrafficOption(BaseModel):
    value: int | None = None
    label: str | None = None
    price_kopeks: int | None = None
    price_label: str | None = None
    is_current: bool = False
    is_available: bool = True
    description: str | None = None


class MiniAppSubscriptionDeviceOption(BaseModel):
    value: int
    label: str | None = None
    price_kopeks: int | None = None
    price_label: str | None = None


class MiniAppSubscriptionCurrentSettings(BaseModel):
    servers: list[MiniAppConnectedServer] = Field(default_factory=list)
    traffic_limit_gb: int | None = None
    traffic_limit_label: str | None = None
    device_limit: int = 0


class MiniAppSubscriptionServersSettings(BaseModel):
    available: list[MiniAppSubscriptionServerOption] = Field(default_factory=list)
    min: int = 0
    max: int = 0
    can_update: bool = True
    hint: str | None = None


class MiniAppSubscriptionTrafficSettings(BaseModel):
    options: list[MiniAppSubscriptionTrafficOption] = Field(default_factory=list)
    can_update: bool = True
    current_value: int | None = None


class MiniAppSubscriptionDevicesSettings(BaseModel):
    options: list[MiniAppSubscriptionDeviceOption] = Field(default_factory=list)
    can_update: bool = True
    min: int = 0
    max: int = 0
    step: int = 1
    current: int = 0
    price_kopeks: int | None = None
    price_label: str | None = None


class MiniAppSubscriptionBillingContext(BaseModel):
    months_remaining: int = 1
    period_hint_days: int | None = None
    renews_at: datetime | None = None


class MiniAppSubscriptionSettings(BaseModel):
    subscription_id: int
    currency: str = 'RUB'
    current: MiniAppSubscriptionCurrentSettings
    servers: MiniAppSubscriptionServersSettings
    traffic: MiniAppSubscriptionTrafficSettings
    devices: MiniAppSubscriptionDevicesSettings
    billing: MiniAppSubscriptionBillingContext | None = None


class MiniAppSubscriptionSettingsResponse(BaseModel):
    success: bool = True
    settings: MiniAppSubscriptionSettings


class MiniAppSubscriptionSettingsRequest(BaseModel):
    init_data: str = Field(..., alias='initData')
    subscription_id: int | None = None

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode='before')
    @classmethod
    def _populate_aliases(cls, values: Any) -> Any:
        if isinstance(values, dict):
            if 'subscriptionId' in values and 'subscription_id' not in values:
                values['subscription_id'] = values['subscriptionId']
        return values


class MiniAppSubscriptionServersUpdateRequest(BaseModel):
    init_data: str = Field(..., alias='initData')
    subscription_id: int | None = None
    servers: list[str] | None = None
    squads: list[str] | None = None
    server_uuids: list[str] | None = None
    squad_uuids: list[str] | None = None

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode='before')
    @classmethod
    def _populate_aliases(cls, values: Any) -> Any:
        if isinstance(values, dict):
            alias_map = {
                'subscriptionId': 'subscription_id',
                'serverUuids': 'server_uuids',
                'squadUuids': 'squad_uuids',
            }
            for alias, target in alias_map.items():
                if alias in values and target not in values:
                    values[target] = values[alias]
        return values


class MiniAppSubscriptionTrafficUpdateRequest(BaseModel):
    init_data: str = Field(..., alias='initData')
    subscription_id: int | None = None
    traffic: int | None = None
    traffic_gb: int | None = None

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode='before')
    @classmethod
    def _populate_aliases(cls, values: Any) -> Any:
        if isinstance(values, dict):
            alias_map = {
                'subscriptionId': 'subscription_id',
                'trafficGb': 'traffic_gb',
            }
            for alias, target in alias_map.items():
                if alias in values and target not in values:
                    values[target] = values[alias]
        return values


class MiniAppSubscriptionDevicesUpdateRequest(BaseModel):
    init_data: str = Field(..., alias='initData')
    subscription_id: int | None = None
    devices: int | None = None
    device_limit: int | None = None

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode='before')
    @classmethod
    def _populate_aliases(cls, values: Any) -> Any:
        if isinstance(values, dict):
            alias_map = {
                'subscriptionId': 'subscription_id',
                'deviceLimit': 'device_limit',
            }
            for alias, target in alias_map.items():
                if alias in values and target not in values:
                    values[target] = values[alias]
        return values


class MiniAppSubscriptionUpdateResponse(BaseModel):
    success: bool = True
    message: str | None = None


class MiniAppSubscriptionPurchaseOptionsRequest(BaseModel):
    init_data: str = Field(..., alias='initData')

    model_config = ConfigDict(populate_by_name=True)


class MiniAppSubscriptionPurchaseOptionsResponse(BaseModel):
    success: bool = True
    currency: str
    balance_kopeks: int | None = Field(default=None, alias='balanceKopeks')
    balance_label: str | None = Field(default=None, alias='balanceLabel')
    subscription_id: int | None = Field(default=None, alias='subscriptionId')
    data: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(populate_by_name=True)


class MiniAppSubscriptionPurchasePreviewRequest(BaseModel):
    init_data: str = Field(..., alias='initData')
    subscription_id: int | None = Field(default=None, alias='subscriptionId')
    selection: dict[str, Any] | None = None
    period_id: str | None = Field(default=None, alias='periodId')
    period_days: int | None = Field(default=None, alias='periodDays')
    period: str | None = None
    traffic_value: int | None = Field(default=None, alias='trafficValue')
    traffic: int | None = None
    traffic_gb: int | None = Field(default=None, alias='trafficGb')
    servers: list[str] | None = None
    countries: list[str] | None = None
    server_uuids: list[str] | None = Field(default=None, alias='serverUuids')
    devices: int | None = None
    device_limit: int | None = Field(default=None, alias='deviceLimit')

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode='before')
    @classmethod
    def _merge_selection(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        selection = values.get('selection')
        if isinstance(selection, dict):
            merged = {**selection, **values}
        else:
            merged = dict(values)
        aliases = {
            'period_id': ('periodId', 'period', 'code'),
            'period_days': ('periodDays',),
            'traffic_value': ('trafficValue', 'traffic', 'trafficGb'),
            'servers': ('countries', 'server_uuids', 'serverUuids'),
            'devices': ('deviceLimit',),
        }
        for target, sources in aliases.items():
            if merged.get(target) is not None:
                continue
            for source in sources:
                if source in merged and merged[source] is not None:
                    merged[target] = merged[source]
                    break
        return merged


class MiniAppSubscriptionPurchasePreviewResponse(BaseModel):
    success: bool = True
    preview: dict[str, Any] = Field(default_factory=dict)
    balance_kopeks: int | None = Field(default=None, alias='balanceKopeks')
    balance_label: str | None = Field(default=None, alias='balanceLabel')

    model_config = ConfigDict(populate_by_name=True)


class MiniAppSubscriptionPurchaseRequest(MiniAppSubscriptionPurchasePreviewRequest):
    pass


class MiniAppSubscriptionPurchaseResponse(BaseModel):
    success: bool = True
    message: str | None = None
    balance_kopeks: int | None = Field(default=None, alias='balanceKopeks')
    balance_label: str | None = Field(default=None, alias='balanceLabel')
    subscription_id: int | None = Field(default=None, alias='subscriptionId')

    model_config = ConfigDict(populate_by_name=True)


class MiniAppSubscriptionTrialRequest(BaseModel):
    init_data: str = Field(..., alias='initData')

    model_config = ConfigDict(populate_by_name=True)


class MiniAppSubscriptionTrialResponse(BaseModel):
    success: bool = True
    message: str | None = None
    subscription_id: int | None = Field(default=None, alias='subscriptionId')
    trial_status: str | None = Field(default=None, alias='trialStatus')
    trial_duration_days: int | None = Field(default=None, alias='trialDurationDays')
    charged_amount_kopeks: int | None = Field(default=None, alias='chargedAmountKopeks')
    charged_amount_label: str | None = Field(default=None, alias='chargedAmountLabel')
    balance_kopeks: int | None = Field(default=None, alias='balanceKopeks')
    balance_label: str | None = Field(default=None, alias='balanceLabel')

    model_config = ConfigDict(populate_by_name=True)
