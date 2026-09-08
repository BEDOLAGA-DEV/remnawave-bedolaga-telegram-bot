"""Pydantic схемы для API конструктора меню."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.localization.texts import get_texts


class ButtonType(StrEnum):
    """Тип кнопки меню."""

    BUILTIN = 'builtin'  # Встроенная кнопка с callback_data
    URL = 'url'  # Внешняя ссылка
    MINI_APP = 'mini_app'  # Telegram Mini App
    CALLBACK = 'callback'  # Кастомная кнопка с любым callback_data


class ButtonVisibility(StrEnum):
    """Видимость кнопки."""

    ALL = 'all'  # Видна всем
    ADMINS = 'admins'  # Только админам
    MODERATORS = 'moderators'  # Только модераторам
    SUBSCRIBERS = 'subscribers'  # Только подписчикам


class ButtonOpenMode(StrEnum):
    """Режим открытия кнопки."""

    CALLBACK = 'callback'  # Отправляет callback_data боту (по умолчанию)
    DIRECT = 'direct'  # Сразу открывает Mini App через WebAppInfo


class ButtonConditions(BaseModel):
    """Условия показа кнопки."""

    # Существующие условия
    has_active_subscription: bool | None = Field(
        default=None,
        description=get_texts().t('MENU_CONDITION_HAS_ACTIVE_SUBSCRIPTION_DESCRIPTION', 'Требуется активная подписка'),
    )
    subscription_is_active: bool | None = Field(
        default=None,
        description=get_texts().t(
            'MENU_CONDITION_SUBSCRIPTION_IS_ACTIVE_DESCRIPTION',
            'Подписка должна быть активна (не приостановлена)',
        ),
    )
    has_traffic_limit: bool | None = Field(
        default=None,
        description=get_texts().t('MENU_CONDITION_HAS_TRAFFIC_LIMIT_DESCRIPTION', 'Подписка с лимитом трафика'),
    )
    is_admin: bool | None = Field(
        default=None, description=get_texts().t('MENU_CONDITION_IS_ADMIN_DESCRIPTION', 'Пользователь - админ')
    )
    is_moderator: bool | None = Field(
        default=None, description=get_texts().t('MENU_CONDITION_IS_MODERATOR_DESCRIPTION', 'Пользователь - модератор')
    )
    referral_enabled: bool | None = Field(
        default=None,
        description=get_texts().t('MENU_CONDITION_REFERRAL_ENABLED_DESCRIPTION', 'Реферальная программа включена'),
    )
    contests_visible: bool | None = Field(
        default=None, description=get_texts().t('MENU_CONDITION_CONTESTS_VISIBLE_DESCRIPTION', 'Конкурсы видимы')
    )
    support_enabled: bool | None = Field(
        default=None, description=get_texts().t('MENU_CONDITION_SUPPORT_ENABLED_DESCRIPTION', 'Поддержка включена')
    )
    language_selection_enabled: bool | None = Field(
        default=None,
        description=get_texts().t('MENU_CONDITION_LANGUAGE_SELECTION_ENABLED_DESCRIPTION', 'Выбор языка включен'),
    )
    happ_enabled: bool | None = Field(
        default=None, description=get_texts().t('MENU_CONDITION_HAPP_ENABLED_DESCRIPTION', 'Кнопка Happ включена')
    )
    simple_subscription_enabled: bool | None = Field(
        default=None,
        description=get_texts().t(
            'MENU_CONDITION_SIMPLE_SUBSCRIPTION_ENABLED_DESCRIPTION',
            'Простая подписка включена',
        ),
    )
    show_trial: bool | None = Field(
        default=None, description=get_texts().t('MENU_CONDITION_SHOW_TRIAL_DESCRIPTION', 'Показать пробный период')
    )
    show_buy: bool | None = Field(
        default=None, description=get_texts().t('MENU_CONDITION_SHOW_BUY_DESCRIPTION', 'Показать кнопку покупки')
    )
    has_saved_cart: bool | None = Field(
        default=None, description=get_texts().t('MENU_CONDITION_HAS_SAVED_CART_DESCRIPTION', 'Есть сохраненная корзина')
    )
    traffic_topup_enabled: bool | None = Field(
        default=None,
        description=get_texts().t('MENU_CONDITION_TRAFFIC_TOPUP_ENABLED_DESCRIPTION', 'Докупка трафика включена'),
    )

    # Расширенные условия
    min_balance_kopeks: int | None = Field(
        default=None,
        ge=0,
        description=get_texts().t('MENU_CONDITION_MIN_BALANCE_KOPEKS_DESCRIPTION', 'Минимальный баланс в копейках'),
    )
    max_balance_kopeks: int | None = Field(
        default=None,
        ge=0,
        description=get_texts().t('MENU_CONDITION_MAX_BALANCE_KOPEKS_DESCRIPTION', 'Максимальный баланс в копейках'),
    )
    min_registration_days: int | None = Field(
        default=None,
        ge=0,
        description=get_texts().t('MENU_CONDITION_MIN_REGISTRATION_DAYS_DESCRIPTION', 'Минимум дней с регистрации'),
    )
    max_registration_days: int | None = Field(
        default=None,
        ge=0,
        description=get_texts().t('MENU_CONDITION_MAX_REGISTRATION_DAYS_DESCRIPTION', 'Максимум дней с регистрации'),
    )
    min_referrals: int | None = Field(
        default=None,
        ge=0,
        description=get_texts().t('MENU_CONDITION_MIN_REFERRALS_DESCRIPTION', 'Минимальное количество рефералов'),
    )
    has_referrals: bool | None = Field(
        default=None, description=get_texts().t('MENU_CONDITION_HAS_REFERRALS_DESCRIPTION', 'Есть рефералы')
    )
    promo_group_ids: list[str] | None = Field(
        default=None,
        description=get_texts().t(
            'MENU_CONDITION_PROMO_GROUP_IDS_DESCRIPTION',
            'Список ID промо-групп (пользователь должен быть в одной из них)',
        ),
    )
    exclude_promo_group_ids: list[str] | None = Field(
        default=None,
        description=get_texts().t(
            'MENU_CONDITION_EXCLUDE_PROMO_GROUP_IDS_DESCRIPTION',
            'Исключить пользователей из этих промо-групп',
        ),
    )
    has_subscription_days_left: int | None = Field(
        default=None,
        ge=0,
        description=get_texts().t(
            'MENU_CONDITION_HAS_SUBSCRIPTION_DAYS_LEFT_DESCRIPTION',
            'Минимум дней до окончания подписки',
        ),
    )
    max_subscription_days_left: int | None = Field(
        default=None,
        ge=0,
        description=get_texts().t(
            'MENU_CONDITION_MAX_SUBSCRIPTION_DAYS_LEFT_DESCRIPTION',
            'Максимум дней до окончания подписки',
        ),
    )
    is_trial_user: bool | None = Field(
        default=None,
        description=get_texts().t('MENU_CONDITION_IS_TRIAL_USER_DESCRIPTION', 'Пользователь на пробном периоде'),
    )
    has_autopay: bool | None = Field(
        default=None, description=get_texts().t('MENU_CONDITION_HAS_AUTOPAY_DESCRIPTION', 'Автоплатёж включён')
    )

    model_config = ConfigDict(extra='ignore')


class MenuButtonConfig(BaseModel):
    """Конфигурация отдельной кнопки."""

    type: ButtonType = Field(..., description=get_texts().t('MENU_BUTTON_TYPE_DESCRIPTION', 'Тип кнопки'))
    builtin_id: str | None = Field(
        default=None,
        description=get_texts().t('MENU_BUTTON_BUILTIN_ID_DESCRIPTION', 'ID встроенной кнопки (для type=builtin)'),
    )
    text: dict[str, str] = Field(
        ...,
        description=get_texts().t('MENU_BUTTON_TEXT_DESCRIPTION', 'Локализованные тексты кнопки: {lang_code: text}'),
    )
    icon: str | None = Field(
        default=None,
        max_length=100,
        description=get_texts().t('MENU_BUTTON_ICON_DESCRIPTION', 'Эмодзи/иконка кнопки (отдельно от текста)'),
    )
    action: str = Field(
        ..., description=get_texts().t('MENU_BUTTON_ACTION_DESCRIPTION', 'callback_data или URL в зависимости от типа')
    )
    enabled: bool = Field(default=True, description=get_texts().t('MENU_BUTTON_ENABLED_DESCRIPTION', 'Кнопка активна'))
    visibility: ButtonVisibility = Field(
        default=ButtonVisibility.ALL,
        description=get_texts().t('MENU_BUTTON_VISIBILITY_DESCRIPTION', 'Видимость кнопки'),
    )
    conditions: ButtonConditions | None = Field(
        default=None, description=get_texts().t('MENU_BUTTON_CONDITIONS_DESCRIPTION', 'Дополнительные условия показа')
    )
    dynamic_text: bool = Field(
        default=False,
        description=get_texts().t(
            'MENU_BUTTON_DYNAMIC_TEXT_DESCRIPTION',
            'Текст содержит плейсхолдеры ({balance}, {username} и т.д.)',
        ),
    )
    open_mode: ButtonOpenMode = Field(
        default=ButtonOpenMode.CALLBACK,
        description=get_texts().t(
            'MENU_BUTTON_OPEN_MODE_DESCRIPTION',
            'Режим открытия: callback (через бота) или direct (сразу Mini App)',
        ),
    )
    webapp_url: str | None = Field(
        default=None,
        description=get_texts().t('MENU_BUTTON_WEBAPP_URL_DESCRIPTION', 'URL для Mini App при open_mode=direct'),
    )
    description: str | None = Field(
        default=None,
        max_length=200,
        description=get_texts().t('MENU_BUTTON_ADMIN_DESCRIPTION_DESCRIPTION', 'Описание кнопки для админ-панели'),
    )
    sort_order: int | None = Field(
        default=None,
        description=get_texts().t(
            'MENU_BUTTON_SORT_ORDER_DESCRIPTION',
            'Порядок сортировки (для отображения в админке)',
        ),
    )
    icon_custom_emoji_id: str | None = Field(
        default=None,
        max_length=100,
        description=get_texts().t(
            'MENU_BUTTON_CUSTOM_EMOJI_ID_DESCRIPTION',
            'ID кастомного Telegram emoji (Bot API 9.4+)',
        ),
    )

    model_config = ConfigDict(extra='ignore')


class MenuRowConfig(BaseModel):
    """Конфигурация строки меню."""

    id: str = Field(
        ..., min_length=1, max_length=50, description=get_texts().t('MENU_ROW_ID_DESCRIPTION', 'Уникальный ID строки')
    )
    buttons: list[str] = Field(
        ..., description=get_texts().t('MENU_ROW_BUTTONS_DESCRIPTION', 'Список ID кнопок в строке')
    )
    conditions: ButtonConditions | None = Field(
        default=None, description=get_texts().t('MENU_ROW_CONDITIONS_DESCRIPTION', 'Условия показа всей строки')
    )
    max_per_row: int = Field(
        default=2, ge=1, le=4, description=get_texts().t('MENU_ROW_MAX_PER_ROW_DESCRIPTION', 'Максимум кнопок в строке')
    )

    model_config = ConfigDict(extra='ignore')


class MenuLayoutConfig(BaseModel):
    """Полная конфигурация меню."""

    version: int = Field(
        default=1, description=get_texts().t('MENU_LAYOUT_VERSION_DESCRIPTION', 'Версия формата конфигурации')
    )
    rows: list[MenuRowConfig] = Field(
        default_factory=list, description=get_texts().t('MENU_LAYOUT_ROWS_DESCRIPTION', 'Строки меню')
    )
    buttons: dict[str, MenuButtonConfig] = Field(
        default_factory=dict, description=get_texts().t('MENU_LAYOUT_BUTTONS_DESCRIPTION', 'Конфигурации кнопок')
    )

    model_config = ConfigDict(extra='ignore')


# --- Response schemas ---


class MenuLayoutResponse(BaseModel):
    """Ответ с конфигурацией меню."""

    version: int
    rows: list[MenuRowConfig]
    buttons: dict[str, MenuButtonConfig]
    is_enabled: bool = Field(
        description=get_texts().t('MENU_LAYOUT_IS_ENABLED_DESCRIPTION', 'Включен ли конструктор меню')
    )
    updated_at: datetime | None = None


class BuiltinButtonInfo(BaseModel):
    """Информация о встроенной кнопке."""

    id: str = Field(description=get_texts().t('MENU_BUILTIN_BUTTON_ID_DESCRIPTION', 'Идентификатор кнопки'))
    default_text: dict[str, str] = Field(
        description=get_texts().t('MENU_BUTTON_DEFAULT_TEXT_DESCRIPTION', 'Текст по умолчанию')
    )
    callback_data: str = Field(
        description=get_texts().t('MENU_BUILTIN_BUTTON_CALLBACK_DATA_DESCRIPTION', 'callback_data кнопки')
    )
    default_conditions: ButtonConditions | None = Field(
        default=None,
        description=get_texts().t('MENU_BUILTIN_BUTTON_DEFAULT_CONDITIONS_DESCRIPTION', 'Условия показа по умолчанию'),
    )
    supports_dynamic_text: bool = Field(
        default=False,
        description=get_texts().t(
            'MENU_BUILTIN_BUTTON_SUPPORTS_DYNAMIC_TEXT_DESCRIPTION',
            'Поддерживает ли динамический текст',
        ),
    )
    supports_direct_open: bool = Field(
        default=False,
        description=get_texts().t(
            'MENU_BUILTIN_BUTTON_SUPPORTS_DIRECT_OPEN_DESCRIPTION',
            'Поддерживает ли прямое открытие Mini App',
        ),
    )


class BuiltinButtonsListResponse(BaseModel):
    """Список встроенных кнопок."""

    items: list[BuiltinButtonInfo]
    total: int


# --- Request schemas ---


class MenuLayoutUpdateRequest(BaseModel):
    """Запрос на обновление конфигурации меню."""

    rows: list[MenuRowConfig] | None = Field(
        default=None, description=get_texts().t('MENU_LAYOUT_UPDATE_ROWS_DESCRIPTION', 'Новая конфигурация строк')
    )
    buttons: dict[str, MenuButtonConfig] | None = Field(
        default=None, description=get_texts().t('MENU_LAYOUT_UPDATE_BUTTONS_DESCRIPTION', 'Новая конфигурация кнопок')
    )

    model_config = ConfigDict(extra='forbid')


class ButtonUpdateRequest(BaseModel):
    """Запрос на обновление отдельной кнопки."""

    text: dict[str, str] | None = Field(
        default=None, description=get_texts().t('MENU_BUTTON_UPDATE_TEXT_DESCRIPTION', 'Новые локализованные тексты')
    )
    icon: str | None = Field(
        default=None,
        max_length=100,
        description=get_texts().t('MENU_BUTTON_ICON_SHORT_DESCRIPTION', 'Эмодзи/иконка кнопки'),
    )
    enabled: bool | None = Field(
        default=None, description=get_texts().t('MENU_BUTTON_UPDATE_ENABLED_DESCRIPTION', 'Включить/выключить')
    )
    visibility: ButtonVisibility | None = Field(
        default=None, description=get_texts().t('MENU_BUTTON_UPDATE_VISIBILITY_DESCRIPTION', 'Новая видимость')
    )
    conditions: ButtonConditions | None = Field(
        default=None, description=get_texts().t('MENU_BUTTON_UPDATE_CONDITIONS_DESCRIPTION', 'Новые условия показа')
    )
    action: str | None = Field(
        default=None,
        description=get_texts().t('MENU_BUTTON_UPDATE_ACTION_DESCRIPTION', 'Новый action (callback_data или URL)'),
    )
    dynamic_text: bool | None = Field(
        default=None,
        description=get_texts().t('MENU_BUTTON_DYNAMIC_TEXT_SHORT_DESCRIPTION', 'Текст содержит плейсхолдеры'),
    )
    open_mode: ButtonOpenMode | None = Field(
        default=None,
        description=get_texts().t('MENU_BUTTON_UPDATE_OPEN_MODE_DESCRIPTION', 'Режим открытия: callback или direct'),
    )
    webapp_url: str | None = Field(
        default=None,
        description=get_texts().t('MENU_BUTTON_WEBAPP_URL_DESCRIPTION', 'URL для Mini App при open_mode=direct'),
    )
    description: str | None = Field(
        default=None,
        max_length=200,
        description=get_texts().t('MENU_BUTTON_UPDATE_DESCRIPTION_DESCRIPTION', 'Описание кнопки'),
    )
    sort_order: int | None = Field(
        default=None, description=get_texts().t('MENU_BUTTON_UPDATE_SORT_ORDER_DESCRIPTION', 'Порядок сортировки')
    )
    icon_custom_emoji_id: str | None = Field(
        default=None,
        max_length=100,
        description=get_texts().t(
            'MENU_BUTTON_CUSTOM_EMOJI_ID_DESCRIPTION',
            'ID кастомного Telegram emoji (Bot API 9.4+)',
        ),
    )

    model_config = ConfigDict(extra='forbid')


class RowsReorderRequest(BaseModel):
    """Запрос на изменение порядка строк."""

    ordered_ids: list[str] = Field(
        ...,
        min_length=1,
        description=get_texts().t('MENU_ROWS_REORDER_ORDERED_IDS_DESCRIPTION', 'Список ID строк в новом порядке'),
    )

    model_config = ConfigDict(extra='forbid')


class AddRowRequest(BaseModel):
    """Запрос на добавление новой строки."""

    id: str = Field(
        ..., min_length=1, max_length=50, description=get_texts().t('MENU_ADD_ROW_ID_DESCRIPTION', 'ID новой строки')
    )
    buttons: list[str] = Field(..., description=get_texts().t('MENU_ADD_ROW_BUTTONS_DESCRIPTION', 'Список ID кнопок'))
    conditions: ButtonConditions | None = Field(
        default=None, description=get_texts().t('MENU_BUTTON_CONDITIONS_SHORT_DESCRIPTION', 'Условия показа')
    )
    max_per_row: int = Field(
        default=2,
        ge=1,
        le=4,
        description=get_texts().t('MENU_ADD_ROW_MAX_PER_ROW_DESCRIPTION', 'Макс. кнопок в строке'),
    )
    position: int | None = Field(
        default=None,
        ge=0,
        description=get_texts().t('MENU_ADD_ROW_POSITION_DESCRIPTION', 'Позиция вставки (по умолчанию - в конец)'),
    )

    model_config = ConfigDict(extra='forbid')


class AddCustomButtonRequest(BaseModel):
    """Запрос на добавление кастомной кнопки."""

    id: str = Field(
        ...,
        min_length=1,
        max_length=50,
        description=get_texts().t('MENU_ADD_BUTTON_ID_DESCRIPTION', 'ID кнопки (уникальный)'),
    )
    type: ButtonType = Field(
        ..., description=get_texts().t('MENU_ADD_BUTTON_TYPE_DESCRIPTION', 'Тип кнопки (url, mini_app или callback)')
    )
    text: dict[str, str] = Field(
        ..., description=get_texts().t('MENU_ADD_BUTTON_TEXT_DESCRIPTION', 'Локализованные тексты')
    )
    icon: str | None = Field(
        default=None,
        max_length=10,
        description=get_texts().t('MENU_BUTTON_ICON_SHORT_DESCRIPTION', 'Эмодзи/иконка кнопки'),
    )
    action: str = Field(
        ..., min_length=1, description=get_texts().t('MENU_ADD_BUTTON_ACTION_DESCRIPTION', 'URL или callback_data')
    )
    visibility: ButtonVisibility = Field(
        default=ButtonVisibility.ALL, description=get_texts().t('MENU_ADD_BUTTON_VISIBILITY_DESCRIPTION', 'Видимость')
    )
    conditions: ButtonConditions | None = Field(
        default=None, description=get_texts().t('MENU_BUTTON_CONDITIONS_SHORT_DESCRIPTION', 'Условия показа')
    )
    dynamic_text: bool = Field(
        default=False,
        description=get_texts().t('MENU_BUTTON_DYNAMIC_TEXT_SHORT_DESCRIPTION', 'Текст содержит плейсхолдеры'),
    )
    row_id: str | None = Field(
        default=None, description=get_texts().t('MENU_ADD_BUTTON_ROW_ID_DESCRIPTION', 'ID строки для добавления кнопки')
    )
    description: str | None = Field(
        default=None,
        max_length=200,
        description=get_texts().t('MENU_BUTTON_ADMIN_DESCRIPTION_DESCRIPTION', 'Описание кнопки для админ-панели'),
    )
    icon_custom_emoji_id: str | None = Field(
        default=None,
        max_length=100,
        description=get_texts().t(
            'MENU_BUTTON_CUSTOM_EMOJI_ID_DESCRIPTION',
            'ID кастомного Telegram emoji (Bot API 9.4+)',
        ),
    )

    model_config = ConfigDict(extra='forbid')


class MenuPreviewRequest(BaseModel):
    """Запрос на предпросмотр меню."""

    language: str = Field(
        default='ru', description=get_texts().t('MENU_PREVIEW_LANGUAGE_DESCRIPTION', 'Язык для предпросмотра')
    )
    is_admin: bool = Field(
        default=False, description=get_texts().t('MENU_PREVIEW_IS_ADMIN_DESCRIPTION', 'Режим админа')
    )
    is_moderator: bool = Field(
        default=False, description=get_texts().t('MENU_PREVIEW_IS_MODERATOR_DESCRIPTION', 'Режим модератора')
    )
    has_active_subscription: bool = Field(
        default=False,
        description=get_texts().t('MENU_PREVIEW_HAS_ACTIVE_SUBSCRIPTION_DESCRIPTION', 'Есть активная подписка'),
    )
    subscription_is_active: bool = Field(
        default=False, description=get_texts().t('MENU_PREVIEW_SUBSCRIPTION_IS_ACTIVE_DESCRIPTION', 'Подписка активна')
    )
    balance_kopeks: int = Field(
        default=0, ge=0, description=get_texts().t('MENU_PREVIEW_BALANCE_KOPEKS_DESCRIPTION', 'Баланс в копейках')
    )

    model_config = ConfigDict(extra='forbid')


class MenuPreviewButton(BaseModel):
    """Кнопка в предпросмотре."""

    text: str
    action: str
    type: ButtonType


class MenuPreviewRow(BaseModel):
    """Строка в предпросмотре."""

    buttons: list[MenuPreviewButton]


class MenuPreviewResponse(BaseModel):
    """Ответ с предпросмотром меню."""

    rows: list[MenuPreviewRow]
    total_buttons: int


# --- Схемы для перемещения кнопок ---


class MoveButtonToRowRequest(BaseModel):
    """Запрос на перемещение кнопки в другую строку."""

    target_row_id: str = Field(
        ..., description=get_texts().t('MENU_MOVE_BUTTON_TARGET_ROW_ID_DESCRIPTION', 'ID целевой строки')
    )
    position: int | None = Field(
        default=None,
        ge=0,
        description=get_texts().t('MENU_MOVE_BUTTON_POSITION_DESCRIPTION', 'Позиция в строке (по умолчанию - в конец)'),
    )

    model_config = ConfigDict(extra='forbid')


class ReorderButtonsInRowRequest(BaseModel):
    """Запрос на изменение порядка кнопок в строке."""

    ordered_button_ids: list[str] = Field(
        ...,
        min_length=1,
        description=get_texts().t(
            'MENU_REORDER_BUTTONS_ORDERED_BUTTON_IDS_DESCRIPTION',
            'Список ID кнопок в новом порядке',
        ),
    )

    model_config = ConfigDict(extra='forbid')


class SwapButtonsRequest(BaseModel):
    """Запрос на обмен местами двух кнопок."""

    button_id_1: str = Field(
        ..., description=get_texts().t('MENU_SWAP_BUTTONS_BUTTON_ID_1_DESCRIPTION', 'ID первой кнопки')
    )
    button_id_2: str = Field(
        ..., description=get_texts().t('MENU_SWAP_BUTTONS_BUTTON_ID_2_DESCRIPTION', 'ID второй кнопки')
    )

    model_config = ConfigDict(extra='forbid')


class MoveButtonResponse(BaseModel):
    """Ответ на перемещение кнопки."""

    button_id: str
    new_row_index: int | None = None
    target_row_id: str | None = None
    position: int | None = None


class SwapButtonsResponse(BaseModel):
    """Ответ на обмен кнопок."""

    button_1: dict[str, Any]
    button_2: dict[str, Any]


class ReorderButtonsResponse(BaseModel):
    """Ответ на изменение порядка кнопок."""

    row_id: str
    buttons: list[str]


# --- Схемы для доступных callback_data ---


class AvailableCallback(BaseModel):
    """Информация о доступном callback_data."""

    callback_data: str = Field(
        description=get_texts().t('MENU_CALLBACK_CALLBACK_DATA_DESCRIPTION', 'callback_data для кнопки')
    )
    name: str = Field(description=get_texts().t('MENU_CALLBACK_NAME_DESCRIPTION', 'Человекочитаемое название'))
    description: str | None = Field(
        default=None, description=get_texts().t('MENU_CALLBACK_DESCRIPTION_DESCRIPTION', 'Описание действия')
    )
    category: str = Field(
        description=get_texts().t(
            'MENU_CALLBACK_CATEGORY_DESCRIPTION',
            'Категория: menu, subscription, balance, referral, support, etc.',
        ),
    )
    default_text: dict[str, str] | None = Field(
        default=None, description=get_texts().t('MENU_BUTTON_DEFAULT_TEXT_DESCRIPTION', 'Текст по умолчанию')
    )
    default_icon: str | None = Field(
        default=None, description=get_texts().t('MENU_CALLBACK_DEFAULT_ICON_DESCRIPTION', 'Иконка по умолчанию')
    )
    requires_subscription: bool = Field(
        default=False,
        description=get_texts().t('MENU_CALLBACK_REQUIRES_SUBSCRIPTION_DESCRIPTION', 'Требует активную подписку'),
    )
    is_in_menu: bool = Field(
        default=False, description=get_texts().t('MENU_CALLBACK_IS_IN_MENU_DESCRIPTION', 'Уже добавлена в меню')
    )


class AvailableCallbacksResponse(BaseModel):
    """Список всех доступных callback_data."""

    items: list[AvailableCallback]
    total: int
    categories: list[str] = Field(
        description=get_texts().t('MENU_CALLBACKS_CATEGORIES_DESCRIPTION', 'Список всех категорий')
    )


# --- Схемы для импорта/экспорта ---


class MenuLayoutExportResponse(BaseModel):
    """Экспорт конфигурации меню."""

    version: int
    rows: list[MenuRowConfig]
    buttons: dict[str, MenuButtonConfig]
    exported_at: datetime
    bot_version: str | None = None


class MenuLayoutImportRequest(BaseModel):
    """Импорт конфигурации меню."""

    version: int
    rows: list[MenuRowConfig]
    buttons: dict[str, MenuButtonConfig]
    merge_mode: str = Field(
        default='replace',
        description=get_texts().t(
            'MENU_LAYOUT_IMPORT_MERGE_MODE_DESCRIPTION',
            'Режим импорта: replace (заменить всё), merge (объединить)',
        ),
    )

    model_config = ConfigDict(extra='forbid')


class MenuLayoutImportResponse(BaseModel):
    """Результат импорта."""

    success: bool
    imported_rows: int
    imported_buttons: int
    warnings: list[str] = Field(default_factory=list)


# --- Схемы для истории изменений ---


class MenuLayoutHistoryEntry(BaseModel):
    """Запись в истории изменений."""

    id: int
    created_at: datetime
    action: str = Field(
        description=get_texts().t('MENU_LAYOUT_HISTORY_ACTION_DESCRIPTION', 'Тип действия: update, reset, import')
    )
    changes_summary: str = Field(
        description=get_texts().t('MENU_LAYOUT_HISTORY_CHANGES_SUMMARY_DESCRIPTION', 'Краткое описание изменений')
    )
    user_info: str | None = Field(
        default=None,
        description=get_texts().t('MENU_LAYOUT_HISTORY_USER_INFO_DESCRIPTION', 'Информация о пользователе'),
    )


class MenuLayoutHistoryResponse(BaseModel):
    """История изменений."""

    items: list[MenuLayoutHistoryEntry]
    total: int


class MenuLayoutRollbackRequest(BaseModel):
    """Запрос на откат к предыдущей версии."""

    history_id: int = Field(
        description=get_texts().t('MENU_LAYOUT_ROLLBACK_HISTORY_ID_DESCRIPTION', 'ID записи в истории для отката')
    )

    model_config = ConfigDict(extra='forbid')


# --- Схемы для валидации ---


class ValidationError(BaseModel):
    """Ошибка валидации."""

    field: str
    message: str
    severity: str = Field(description=get_texts().t('MENU_VALIDATION_SEVERITY_DESCRIPTION', 'error или warning'))


class MenuLayoutValidateRequest(BaseModel):
    """Запрос на валидацию конфигурации."""

    rows: list[MenuRowConfig] | None = None
    buttons: dict[str, MenuButtonConfig] | None = None

    model_config = ConfigDict(extra='forbid')


class MenuLayoutValidateResponse(BaseModel):
    """Результат валидации."""

    is_valid: bool
    errors: list[ValidationError] = Field(default_factory=list)
    warnings: list[ValidationError] = Field(default_factory=list)


# --- Схемы для статистики кликов ---


class ButtonClickStats(BaseModel):
    """Статистика кликов по кнопке."""

    button_id: str
    clicks_total: int = Field(default=0)
    clicks_today: int = Field(default=0)
    clicks_week: int = Field(default=0)
    clicks_month: int = Field(default=0)
    last_click_at: datetime | None = None
    unique_users: int = Field(
        default=0, description=get_texts().t('MENU_BUTTON_STATS_UNIQUE_USERS_DESCRIPTION', 'Уникальные пользователи')
    )


class ButtonClickStatsResponse(BaseModel):
    """Статистика кликов для одной кнопки."""

    button_id: str
    stats: ButtonClickStats
    clicks_by_day: list[dict[str, Any]] = Field(
        default_factory=list,
        description=get_texts().t('MENU_BUTTON_STATS_CLICKS_BY_DAY_DESCRIPTION', 'Клики по дням [{date, count}]'),
    )


class MenuClickStatsResponse(BaseModel):
    """Общая статистика кликов по всем кнопкам."""

    items: list[ButtonClickStats]
    total_clicks: int
    period_start: datetime
    period_end: datetime


class ButtonTypeStats(BaseModel):
    """Статистика по типу кнопки."""

    button_type: str
    clicks_total: int
    unique_users: int


class ButtonTypeStatsResponse(BaseModel):
    """Статистика кликов по типам кнопок."""

    items: list[ButtonTypeStats]
    total_clicks: int


class HourlyStats(BaseModel):
    """Статистика по часам."""

    hour: int
    count: int


class HourlyStatsResponse(BaseModel):
    """Статистика кликов по часам дня."""

    items: list[HourlyStats]
    button_id: str | None = None


class WeekdayStats(BaseModel):
    """Статистика по дням недели."""

    weekday: int
    weekday_name: str
    count: int


class WeekdayStatsResponse(BaseModel):
    """Статистика кликов по дням недели."""

    items: list[WeekdayStats]
    button_id: str | None = None


class TopUserStats(BaseModel):
    """Статистика пользователя."""

    user_id: int
    clicks_count: int
    last_click_at: datetime | None = None


class TopUsersResponse(BaseModel):
    """Топ пользователей по кликам."""

    items: list[TopUserStats]
    button_id: str | None = None
    limit: int


class PeriodComparisonResponse(BaseModel):
    """Сравнение периодов."""

    current_period: dict[str, Any]
    previous_period: dict[str, Any]
    change: dict[str, Any]
    button_id: str | None = None


class UserClickSequence(BaseModel):
    """Последовательность кликов пользователя."""

    button_id: str
    button_text: str | None = None
    clicked_at: datetime


class UserClickSequencesResponse(BaseModel):
    """Последовательности кликов пользователя."""

    user_id: int
    items: list[UserClickSequence]
    total: int


# --- Схемы для плейсхолдеров ---


class DynamicPlaceholder(BaseModel):
    """Информация о динамическом плейсхолдере."""

    placeholder: str = Field(
        description=get_texts().t('MENU_PLACEHOLDER_PLACEHOLDER_DESCRIPTION', 'Плейсхолдер, например {balance}')
    )
    description: str = Field(description=get_texts().t('MENU_PLACEHOLDER_DESCRIPTION_DESCRIPTION', 'Описание'))
    example: str = Field(description=get_texts().t('MENU_PLACEHOLDER_EXAMPLE_DESCRIPTION', 'Пример значения'))
    category: str = Field(
        description=get_texts().t(
            'MENU_PLACEHOLDER_CATEGORY_DESCRIPTION',
            'Категория: user, subscription, referral, etc.',
        ),
    )


class DynamicPlaceholdersResponse(BaseModel):
    """Список доступных плейсхолдеров."""

    items: list[DynamicPlaceholder]
    total: int
