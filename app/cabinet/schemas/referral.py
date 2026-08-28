"""Referral program schemas for cabinet."""

from datetime import datetime

from pydantic import BaseModel


class ReferralInfoResponse(BaseModel):
    """Referral program info for current user."""

    referral_code: str
    referral_link: str
    bot_referral_link: str = ''
    total_referrals: int
    active_referrals: int
    total_earnings_kopeks: int
    total_earnings_rubles: float
    # Награда днями имеет amount_kopeks == 0: без своего поля партнёр на
    # «дневной» программе видит нулевой доход при работающих начислениях.
    total_earnings_days: int = 0
    commission_percent: int
    available_balance_kopeks: int = 0
    available_balance_rubles: float = 0
    withdrawn_kopeks: int = 0


class ReferralItemResponse(BaseModel):
    """Single referral info."""

    id: int
    username: str | None = None
    first_name: str | None = None
    created_at: datetime
    has_subscription: bool
    has_paid: bool


class ReferralListResponse(BaseModel):
    """Paginated referral list."""

    items: list[ReferralItemResponse]
    total: int
    page: int
    per_page: int
    pages: int


class ReferralEarningResponse(BaseModel):
    """Referral earning history item.

    ``reward_type`` разделяет деньги и дни: строка с ``amount_kopeks == 0`` и
    ``days_granted > 0`` — это реальная награда, а не пустое начисление, и
    рисовать её как «0 ₽» неверно. ``level`` — единственное, что отличает
    в остальном одинаковые строки от разных звеньев цепочки.
    """

    id: int
    amount_kopeks: int
    amount_rubles: float
    reason: str
    reward_type: str = 'money'
    level: int = 1
    days_granted: int = 0
    tariff_id: int | None = None
    tariff_name: str | None = None
    referral_username: str | None = None
    referral_first_name: str | None = None
    campaign_name: str | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class ReferralEarningsListResponse(BaseModel):
    """Paginated referral earnings list."""

    items: list[ReferralEarningResponse]
    total: int
    total_amount_kopeks: int
    total_amount_rubles: float
    total_days_granted: int = 0
    page: int
    per_page: int
    pages: int


class ReferralDaysTargetOption(BaseModel):
    """Подписка, в которую можно направить дни награды."""

    id: int
    tariff_name: str | None = None
    # Срок нужен вместе с названием: подписок одного тарифа может быть несколько,
    # и по названию их не различить.
    end_date: str | None = None


class ReferralRewardChoiceRequest(BaseModel):
    """Правка предпочтений пользователя. Поля необязательны по отдельности.

    Экран правит их по одному, и присылать оба ради одного значило бы затирать
    выбор, сделанный из бота между двумя запросами.
    """

    reward_preference: str | None = None
    days_target_subscription_id: int | None = None
    # Явные признаки «поле прислано»: сам None — значимое значение («как
    # настроено правилом» и «подбирать автоматически»), и отличить его от
    # «не трогали» иначе нельзя.
    set_reward_preference: bool = False
    set_days_target: bool = False


class ReferralProgramLevel(BaseModel):
    """Одна ступень программы, разобранная на части.

    Кабинет раскладывает её по карточке, поэтому части приходят по отдельности, а
    не готовой строкой. Собирается тем же кодом, что и текст бота, — иначе экран
    и бот описывали бы одно правило двумя способами и разошлись бы.
    """

    level: int
    is_current: bool = False
    # Готовые куски награды пригласившему: «25% от суммы», «50 ₽», «7 дн. (Про)».
    rewards: list[str] = []
    # False — ступень пригласившему не платит; её показывают, только если она его.
    pays_referrer: bool = True
    trigger: str = ''
    trigger_label: str = ''
    required_referrals: int = 0
    required_referrals_active_only: bool = True
    # Что на этой ступени получает приглашённый. None — ничего.
    referee_reward: str | None = None


class ReferralTermsResponse(BaseModel):
    """Referral program terms."""

    is_enabled: bool
    commission_percent: int
    first_payment_commission_percent: int | None = None
    recurring_commission_tiers: str = ''
    minimum_topup_kopeks: int
    minimum_topup_rubles: float
    first_topup_bonus_kopeks: int
    first_topup_bonus_rubles: float
    inviter_bonus_kopeks: int
    inviter_bonus_rubles: float
    max_commission_payments: int = 0
    partner_section_visible: bool = True
    # Под многоуровневой схемой поля выше ничем не управляют: начисления идут по
    # таблице уровней. Публиковать их как «условия программы» значило бы обещать
    # пользователю то, чего бот не платит.
    scheme: str = 'legacy'
    level_descriptions: list[str] = []
    referee_bonus_description: str | None = None
    max_level_depth: int = 1
    # Что означает номер уровня: 'chain' — глубина цепочки, 'tiers' — ранг за
    # число рефералов. Клиент обязан различать: в рангах платят только прямому
    # пригласившему, и глубина цепочки там равна 1 независимо от настройки.
    levels_mode: str = 'chain'
    # Ранг самого пользователя. Заполняется только в режиме рангов — в цепочке
    # ранга не существует, и нули читались бы как «ранг 0».
    tier_current_level: int | None = None
    tier_next_level: int | None = None
    tier_next_remaining: int = 0
    tier_referrals_any: int = 0
    tier_referrals_active: int = 0
    # Ступени программы в том порядке, в котором их показывают: в цепочке по
    # номеру, в режиме за приглашённых — по возрастанию порога.
    levels: list[ReferralProgramLevel] = []
    # Личная ставка партнёра, если она перебивает процент ступени. None — нет.
    personal_percent: int | None = None
    # Что пользователю разрешено выбирать самому. Пока не разрешено, экран
    # настроек не показывается вовсе: выбор, ни на что не влияющий, обещает
    # влияние, которого нет.
    allow_reward_kind_choice: bool = False
    allow_days_target_choice: bool = False
    # Текущий выбор пользователя. 'money' | 'days' | None — «как настроено правилом».
    reward_preference: str | None = None
    days_target_subscription_id: int | None = None
    # Подписки, между которыми есть смысл выбирать. Пусто — выбирать не из чего.
    days_target_options: list[ReferralDaysTargetOption] = []
    # Что даёт каждая сторона выбора: «25% от суммы» и «7 дн. подписки (Про)».
    # Считается без учёта уже сделанного выбора — карточки показывают, что даёт
    # каждый вариант, а не только выбранный. None — этой стороны у правила нет.
    reward_choice_money: str | None = None
    reward_choice_days: str | None = None


class ReferralRewardLevelResponse(BaseModel):
    """Правило награды одного уровня цепочки."""

    level: int
    is_active: bool
    reward_mode: str
    trigger: str
    referrer_percent: int | None = None
    referrer_fixed_kopeks: int | None = None
    referrer_days: int = 0
    referrer_tariff_id: int | None = None
    referrer_tariff_name: str | None = None
    referee_fixed_kopeks: int | None = None
    referee_days: int = 0
    referee_tariff_id: int | None = None
    referee_tariff_name: str | None = None
    max_payments: int = 0
    # За сколько рефералов открывается уровень и кого считать.
    required_referrals: int = 0
    required_referrals_active_only: bool = True

    class Config:
        from_attributes = True


class ReferralRewardTariffOption(BaseModel):
    """Тариф, в который могут лечь дни награды."""

    id: int
    name: str


class ReferralRewardLevelsResponse(BaseModel):
    """Схема наград целиком: флаг режима, правила уровней и выбор тарифов.

    Список тарифов отдаётся здесь, а не берётся с ``/admin/tariffs``: тот
    эндпоинт требует права ``tariffs:read``, и админ с одним лишь
    ``partners:settings`` увидел бы экран без единого тарифа на выбор — то есть
    ровно ту конфигурацию, при которой дни теряются.
    """

    scheme: str
    scheme_locked_by_env: bool = False
    levels_mode: str = 'chain'
    levels_mode_locked_by_env: bool = False
    # Выключенный мультитариф означает, что у подписок нет тарифа и дни с
    # выбранным тарифом не начислятся вовсе. Бот об этом предупреждает на
    # карточке; кабинету нужен тот же признак, иначе выпадающий список тарифов
    # полон, а настройка молча ничего не даёт.
    multi_tariff_enabled: bool = True
    # Глубина, закреплённая в .env, не меняется из кабинета: PATCH вернёт 409.
    # Без флага поле оставалось активным, правка отбивалась, а несохранённое
    # значение продолжало висеть в форме — выглядело как принятое.
    max_level_depth_locked_by_env: bool = False
    max_level_depth: int
    max_supported_level: int
    levels: list[ReferralRewardLevelResponse]
    available_tariffs: list[ReferralRewardTariffOption] = []
    # Что перенос не смог выразить уровнем. Заполняется только ответом на импорт:
    # молча потерять ступени комиссии хуже, чем не перенести их с предупреждением.
    import_notes: list[str] = []


class ReferralRewardLevelUpdateRequest(BaseModel):
    """Правка уровня.

    Все поля необязательны: экран правит их по одному, и присылать весь объект
    ради одной галочки значило бы затирать чужую правку, сделанную из бота.
    """

    is_active: bool | None = None
    reward_mode: str | None = None
    trigger: str | None = None
    referrer_percent: int | None = None
    referrer_fixed_kopeks: int | None = None
    referrer_days: int | None = None
    referrer_tariff_id: int | None = None
    referee_fixed_kopeks: int | None = None
    referee_days: int | None = None
    referee_tariff_id: int | None = None
    max_payments: int | None = None
    required_referrals: int | None = None
    required_referrals_active_only: bool | None = None


class ReferralSchemeUpdateRequest(BaseModel):
    """Переключение схемы наград целиком."""

    scheme: str


class ReferralDepthUpdateRequest(BaseModel):
    """Глубина обхода цепочки: сколько звеньев вверх получают награду."""

    max_level_depth: int


class ReferralLevelsModeUpdateRequest(BaseModel):
    """Что означает номер уровня: 'chain' (глубина) или 'tiers' (ранг)."""

    levels_mode: str
