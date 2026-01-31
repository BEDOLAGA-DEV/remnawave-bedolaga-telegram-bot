from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.localization.texts import get_texts


def _t(texts, key: str, default: str) -> str:
    """Helper for localized button labels with fallbacks."""
    return texts.t(key, default)


def get_admin_main_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MAIN_USERS_SUBSCRIPTIONS', '👥 Юзеры/Подписки'),
                    callback_data='admin_submenu_users',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MAIN_SERVERS', '🌐 Серверы'),
                    callback_data='admin_servers',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MAIN_TARIFFS', '📦 Тарифы'),
                    callback_data='admin_tariffs',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MAIN_PRICING', '💰 Цены'),
                    callback_data='admin_pricing',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MAIN_PROMO_STATS', '💰 Промокоды/Статистика'),
                    callback_data='admin_submenu_promo',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MAIN_SUPPORT', '🛟 Поддержка'),
                    callback_data='admin_submenu_support',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MAIN_MESSAGES', '📨 Сообщения'),
                    callback_data='admin_submenu_communications',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MAIN_SETTINGS', '⚙️ Настройки'),
                    callback_data='admin_submenu_settings',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MAIN_SYSTEM', '🛠️ Система'),
                    callback_data='admin_submenu_system',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MAIN_TRIALS', '🧪 Триалы'),
                    callback_data='admin_trials',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MAIN_PAYMENTS', '💳 Пополнения'),
                    callback_data='admin_payments',
                ),
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')],
        ]
    )


def get_admin_users_submenu_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=texts.ADMIN_USERS, callback_data='admin_users'),
                InlineKeyboardButton(text=texts.ADMIN_REFERRALS, callback_data='admin_referrals'),
            ],
            [InlineKeyboardButton(text=texts.ADMIN_SUBSCRIPTIONS, callback_data='admin_subscriptions')],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')],
        ]
    )


def get_admin_promo_submenu_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=texts.ADMIN_PROMOCODES, callback_data='admin_promocodes'),
                InlineKeyboardButton(text=texts.ADMIN_STATISTICS, callback_data='admin_statistics'),
            ],
            [InlineKeyboardButton(text=texts.ADMIN_CAMPAIGNS, callback_data='admin_campaigns')],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CONTESTS', '🏆 Конкурсы'),
                    callback_data='admin_contests',
                )
            ],
            [InlineKeyboardButton(text=texts.ADMIN_PROMO_GROUPS, callback_data='admin_promo_groups')],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')],
        ]
    )


def get_admin_communications_submenu_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.ADMIN_MESSAGES, callback_data='admin_messages')],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_COMMUNICATIONS_POLLS', '🗳️ Опросы'),
                    callback_data='admin_polls',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_COMMUNICATIONS_PROMO_OFFERS', '🎯 Промо-предложения'),
                    callback_data='admin_promo_offers',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_COMMUNICATIONS_WELCOME_TEXT', '👋 Приветственный текст'),
                    callback_data='welcome_text_panel',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_COMMUNICATIONS_MENU_MESSAGES', '📢 Сообщения в меню'),
                    callback_data='user_messages_panel',
                ),
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')],
        ]
    )


def get_admin_support_submenu_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SUPPORT_TICKETS', '🎫 Тикеты поддержки'), callback_data='admin_tickets'
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SUPPORT_AUDIT', '🧾 Аудит модераторов'), callback_data='admin_support_audit'
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SUPPORT_SETTINGS', '🛟 Настройки поддержки'),
                    callback_data='admin_support_settings',
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')],
        ]
    )


def get_admin_settings_submenu_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=texts.ADMIN_REMNAWAVE, callback_data='admin_remnawave'),
                InlineKeyboardButton(text=texts.ADMIN_MONITORING, callback_data='admin_monitoring'),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SETTINGS_BOT_CONFIG', '🧩 Конфигурация бота'),
                    callback_data='admin_bot_config',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_SETTINGS', '⚙️ Настройки мониторинга'),
                    callback_data='admin_mon_settings',
                )
            ],
            [
                InlineKeyboardButton(text=texts.ADMIN_RULES, callback_data='admin_rules'),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SETTINGS_MAINTENANCE', '🔧 Техработы'), callback_data='maintenance_panel'
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SETTINGS_PRIVACY_POLICY', '🛡️ Политика конф.'),
                    callback_data='admin_privacy_policy',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SETTINGS_PUBLIC_OFFER', '📄 Публичная оферта'),
                    callback_data='admin_public_offer',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SETTINGS_FAQ', '❓ FAQ'),
                    callback_data='admin_faq',
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')],
        ]
    )


def get_admin_system_submenu_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SYSTEM_UPDATES', '📄 Обновления'), callback_data='admin_updates'
                ),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_SYSTEM_BACKUPS', '🗄️ Бекапы'), callback_data='backup_panel'),
            ],
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_SYSTEM_LOGS', '🧾 Логи'), callback_data='admin_system_logs')],
            [InlineKeyboardButton(text=texts.t('ADMIN_REPORTS', '📊 Отчеты'), callback_data='admin_reports')],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')],
        ]
    )


def get_admin_trials_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_TRIALS_RESET_BUTTON', '♻️ Сбросить все триалы'),
                    callback_data='admin_trials_reset',
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')],
        ]
    )


def get_admin_reports_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_REPORTS_PREVIOUS_DAY', '📆 За вчера'), callback_data='admin_reports_daily'
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_REPORTS_LAST_WEEK', '🗓️ За неделю'), callback_data='admin_reports_weekly'
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_REPORTS_LAST_MONTH', '📅 За месяц'), callback_data='admin_reports_monthly'
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')],
        ]
    )


def get_admin_report_result_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.t('REPORT_CLOSE', '❌ Закрыть'), callback_data='admin_close_report')]
        ]
    )


def get_admin_users_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_USERS_ALL', '👥 Все пользователи'), callback_data='admin_users_list'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_USERS_SEARCH', '🔍 Поиск'), callback_data='admin_users_search'
                ),
            ],
            [
                InlineKeyboardButton(text=texts.ADMIN_STATISTICS, callback_data='admin_users_stats'),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_USERS_INACTIVE', '🗑️ Неактивные'), callback_data='admin_users_inactive'
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_USERS_FILTERS', '⚙️ Фильтры'), callback_data='admin_users_filters'
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_USERS_BLACKLIST', '🔐 Черный список'),
                    callback_data='admin_blacklist_settings',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_USERS_BULK_BAN', '🛑 Массовый бан'), callback_data='admin_bulk_ban_start'
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_submenu_users')],
        ]
    )


def get_admin_users_filters_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_USERS_FILTER_BALANCE', '💰 По балансу'),
                    callback_data='admin_users_balance_filter',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_USERS_FILTER_TRAFFIC', '📶 По трафику'),
                    callback_data='admin_users_traffic_filter',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_USERS_FILTER_ACTIVITY', '🕒 По активности'),
                    callback_data='admin_users_activity_filter',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_USERS_FILTER_SPENDING', '💳 По сумме трат'),
                    callback_data='admin_users_spending_filter',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_USERS_FILTER_PURCHASES', '🛒 По количеству покупок'),
                    callback_data='admin_users_purchases_filter',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_USERS_FILTER_RENEW_READY', '♻️ Готовы к продлению'),
                    callback_data='admin_users_ready_to_renew_filter',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_USERS_FILTER_POTENTIAL_CUSTOMERS', '💰 Потенциальные клиенты'),
                    callback_data='admin_users_potential_customers_filter',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_USERS_FILTER_CAMPAIGN', '📢 По кампании'),
                    callback_data='admin_users_campaign_filter',
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_users')],
        ]
    )


def get_admin_subscriptions_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SUBSCRIPTIONS_ALL', '📱 Все подписки'), callback_data='admin_subs_list'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SUBSCRIPTIONS_EXPIRING', '⏰ Истекающие'), callback_data='admin_subs_expiring'
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SUBSCRIPTIONS_COUNTRIES', '🌍 Управление странами'),
                    callback_data='admin_subs_countries',
                )
            ],
            [InlineKeyboardButton(text=texts.ADMIN_STATISTICS, callback_data='admin_subs_stats')],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_submenu_users')],
        ]
    )


def get_admin_promocodes_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_PROMOCODES_ALL', '🎫 Все промокоды'), callback_data='admin_promo_list'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_PROMOCODES_CREATE', '➕ Создать'), callback_data='admin_promo_create'
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_PROMOCODES_GENERAL_STATS', '📊 Общая статистика'),
                    callback_data='admin_promo_general_stats',
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_submenu_promo')],
        ]
    )


def get_admin_campaigns_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CAMPAIGNS_LIST', '📋 Список кампаний'), callback_data='admin_campaigns_list'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CAMPAIGNS_CREATE', '➕ Создать'), callback_data='admin_campaigns_create'
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CAMPAIGNS_GENERAL_STATS', '📊 Общая статистика'),
                    callback_data='admin_campaigns_stats',
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_submenu_promo')],
        ]
    )


def get_admin_contests_root_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CONTESTS_REFERRAL', '🤝 Реферальные конкурсы'),
                    callback_data='admin_contests_referral',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CONTESTS_DAILY', '📆 Ежедневные конкурсы'),
                    callback_data='admin_contests_daily',
                )
            ],
            [
                InlineKeyboardButton(text=texts.BACK, callback_data='admin_submenu_promo'),
            ],
        ]
    )


def get_admin_contests_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CONTESTS_LIST', '📋 Текущие конкурсы'),
                    callback_data='admin_contests_list',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CONTESTS_CREATE', '➕ Новый конкурс'),
                    callback_data='admin_contests_create',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=texts.BACK,
                    callback_data='admin_contests',
                )
            ],
        ]
    )


def get_contest_mode_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CONTEST_MODE_PAID', '💳 Реферал с покупкой'),
                    callback_data='admin_contest_mode_paid',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CONTEST_MODE_REGISTERED', '🧑‍🤝‍🧑 Просто реферал'),
                    callback_data='admin_contest_mode_registered',
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_contests_referral')],
        ]
    )


def get_daily_contest_manage_keyboard(
    template_id: int,
    is_enabled: bool,
    language: str = 'ru',
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    toggle_text = (
        _t(texts, 'ADMIN_CONTEST_DISABLE', '⏸️ Остановить')
        if is_enabled
        else _t(texts, 'ADMIN_CONTEST_ENABLE', '▶️ Запустить')
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=toggle_text, callback_data=f'admin_daily_toggle_{template_id}'),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CONTEST_START_NOW', '🚀 Запустить раунд'),
                    callback_data=f'admin_daily_start_{template_id}',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CONTEST_START_MANUAL', '🧪 Ручной старт'),
                    callback_data=f'admin_daily_manual_{template_id}',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_EDIT_PRIZE_TYPE', '🏅 Тип приза'),
                    callback_data=f'admin_daily_edit_{template_id}_prize_type',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_EDIT_PRIZE_VALUE', '💰 Значение приза'),
                    callback_data=f'admin_daily_edit_{template_id}_prize_value',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_EDIT_MAX_WINNERS', '👥 Победителей'),
                    callback_data=f'admin_daily_edit_{template_id}_max_winners',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_EDIT_ATTEMPTS', '🔁 Попытки'),
                    callback_data=f'admin_daily_edit_{template_id}_attempts_per_user',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_EDIT_TIMES', '⏰ Раундов/день'),
                    callback_data=f'admin_daily_edit_{template_id}_times_per_day',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_EDIT_SCHEDULE', '🕒 Расписание'),
                    callback_data=f'admin_daily_edit_{template_id}_schedule_times',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_EDIT_COOLDOWN', '⌛ Длительность'),
                    callback_data=f'admin_daily_edit_{template_id}_cooldown_hours',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_EDIT_PAYLOAD', '🧩 Payload'),
                    callback_data=f'admin_daily_payload_{template_id}',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_RESET_ATTEMPTS', '🔄 Сбросить попытки'),
                    callback_data=f'admin_daily_reset_attempts_{template_id}',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CLOSE_ROUND', '❌ Закрыть раунд'),
                    callback_data=f'admin_daily_close_{template_id}',
                ),
            ],
            [
                InlineKeyboardButton(text=texts.BACK, callback_data='admin_contests_daily'),
            ],
        ]
    )


def get_referral_contest_manage_keyboard(
    contest_id: int,
    *,
    is_active: bool,
    can_delete: bool = False,
    language: str = 'ru',
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    toggle_text = (
        _t(texts, 'ADMIN_CONTEST_DISABLE', '⏸️ Остановить')
        if is_active
        else _t(texts, 'ADMIN_CONTEST_ENABLE', '▶️ Запустить')
    )

    rows = [
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_CONTEST_LEADERBOARD', '📊 Лидеры'),
                callback_data=f'admin_contest_leaderboard_{contest_id}',
            ),
            InlineKeyboardButton(
                text=toggle_text,
                callback_data=f'admin_contest_toggle_{contest_id}',
            ),
        ],
        [
            InlineKeyboardButton(
                text='📈 Детальная статистика',
                callback_data=f'admin_contest_detailed_stats_{contest_id}',
            ),
        ],
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_CONTEST_EDIT_SUMMARY_TIMES', '🕒 Итоги в день'),
                callback_data=f'admin_contest_edit_times_{contest_id}',
            ),
        ],
        [
            InlineKeyboardButton(
                text='👻 Виртуальные',
                callback_data=f'admin_contest_vp_{contest_id}',
            ),
        ],
        [
            InlineKeyboardButton(
                text='🔄 Синхронизация',
                callback_data=f'admin_contest_sync_{contest_id}',
            ),
            InlineKeyboardButton(
                text='🔍 Отладка',
                callback_data=f'admin_contest_debug_{contest_id}',
            ),
        ],
    ]

    if can_delete:
        rows.append(
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CONTEST_DELETE', '🗑 Удалить'),
                    callback_data=f'admin_contest_delete_{contest_id}',
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_BACK_TO_LIST', '⬅️ К списку'),
                callback_data='admin_contests_list',
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_campaign_management_keyboard(campaign_id: int, is_active: bool, language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)
    status_text = (
        _t(texts, 'ADMIN_CAMPAIGN_DISABLE', '🔴 Выключить')
        if is_active
        else _t(texts, 'ADMIN_CAMPAIGN_ENABLE', '🟢 Включить')
    )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CAMPAIGN_STATS', '📊 Статистика'),
                    callback_data=f'admin_campaign_stats_{campaign_id}',
                ),
                InlineKeyboardButton(
                    text=status_text,
                    callback_data=f'admin_campaign_toggle_{campaign_id}',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CAMPAIGN_EDIT', '✏️ Редактировать'),
                    callback_data=f'admin_campaign_edit_{campaign_id}',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CAMPAIGN_DELETE', '🗑️ Удалить'),
                    callback_data=f'admin_campaign_delete_{campaign_id}',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BACK_TO_LIST', '⬅️ К списку'), callback_data='admin_campaigns_list'
                )
            ],
        ]
    )


def get_campaign_edit_keyboard(
    campaign_id: int,
    *,
    bonus_type: str = None,
    is_balance_bonus: bool = None,  # deprecated, for backwards compatibility
    language: str = 'ru',
) -> InlineKeyboardMarkup:
    texts = get_texts(language)

    # Поддержка старого API
    if bonus_type is None and is_balance_bonus is not None:
        bonus_type = 'balance' if is_balance_bonus else 'subscription'

    keyboard: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_CAMPAIGN_EDIT_NAME', '✏️ Название'),
                callback_data=f'admin_campaign_edit_name_{campaign_id}',
            ),
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_CAMPAIGN_EDIT_START', '🔗 Параметр'),
                callback_data=f'admin_campaign_edit_start_{campaign_id}',
            ),
        ]
    ]

    if bonus_type == 'balance':
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CAMPAIGN_BONUS_BALANCE', '💰 Бонус на баланс'),
                    callback_data=f'admin_campaign_edit_balance_{campaign_id}',
                )
            ]
        )
    elif bonus_type == 'subscription':
        keyboard.extend(
            [
                [
                    InlineKeyboardButton(
                        text=_t(texts, 'ADMIN_CAMPAIGN_DURATION', '📅 Длительность'),
                        callback_data=f'admin_campaign_edit_sub_days_{campaign_id}',
                    ),
                    InlineKeyboardButton(
                        text=_t(texts, 'ADMIN_CAMPAIGN_TRAFFIC', '🌐 Трафик'),
                        callback_data=f'admin_campaign_edit_sub_traffic_{campaign_id}',
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=_t(texts, 'ADMIN_CAMPAIGN_DEVICES', '📱 Устройства'),
                        callback_data=f'admin_campaign_edit_sub_devices_{campaign_id}',
                    ),
                    InlineKeyboardButton(
                        text=_t(texts, 'ADMIN_CAMPAIGN_SERVERS', '🌍 Серверы'),
                        callback_data=f'admin_campaign_edit_sub_servers_{campaign_id}',
                    ),
                ],
            ]
        )
    elif bonus_type == 'tariff':
        keyboard.extend(
            [
                [
                    InlineKeyboardButton(
                        text=_t(texts, 'ADMIN_CAMPAIGN_TARIFF', '🎁 Тариф'),
                        callback_data=f'admin_campaign_edit_tariff_{campaign_id}',
                    ),
                    InlineKeyboardButton(
                        text=_t(texts, 'ADMIN_CAMPAIGN_DURATION', '📅 Длительность'),
                        callback_data=f'admin_campaign_edit_tariff_days_{campaign_id}',
                    ),
                ],
            ]
        )
    # bonus_type == "none" - только базовые кнопки (название и параметр)

    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_campaign_manage_{campaign_id}')])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_campaign_bonus_type_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CAMPAIGN_BONUS_BALANCE', '💰 Бонус на баланс'),
                    callback_data='campaign_bonus_balance',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CAMPAIGN_BONUS_SUBSCRIPTION', '📱 Пробная подписка'),
                    callback_data='campaign_bonus_subscription',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CAMPAIGN_BONUS_TARIFF', '🎁 Тариф'), callback_data='campaign_bonus_tariff'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CAMPAIGN_BONUS_NONE', '🔗 Только ссылка'), callback_data='campaign_bonus_none'
                ),
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_campaigns')],
        ]
    )


def get_promocode_management_keyboard(promo_id: int, language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_PROMOCODE_EDIT', '✏️ Редактировать'), callback_data=f'promo_edit_{promo_id}'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_PROMOCODE_TOGGLE', '🔄 Статус'), callback_data=f'promo_toggle_{promo_id}'
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_PROMOCODE_STATS', '📊 Статистика'), callback_data=f'promo_stats_{promo_id}'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_PROMOCODE_DELETE', '🗑️ Удалить'), callback_data=f'promo_delete_{promo_id}'
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BACK_TO_LIST', '⬅️ К списку'), callback_data='admin_promo_list'
                )
            ],
        ]
    )


def get_admin_messages_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MESSAGES_ALL_USERS', '📨 Всем пользователям'), callback_data='admin_msg_all'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MESSAGES_BY_SUBSCRIPTIONS', '🎯 По подпискам'),
                    callback_data='admin_msg_by_sub',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MESSAGES_BY_CRITERIA', '🔍 По критериям'), callback_data='admin_msg_custom'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MESSAGES_HISTORY', '📋 История'), callback_data='admin_msg_history'
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_PINNED_MESSAGE', '📌 Закрепленное сообщение'),
                    callback_data='admin_pinned_message',
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_submenu_communications')],
        ]
    )


def get_pinned_message_keyboard(
    language: str = 'ru',
    send_before_menu: bool = True,
    send_on_every_start: bool = True,
) -> InlineKeyboardMarkup:
    texts = get_texts(language)

    position_label = (
        _t(texts, 'ADMIN_PINNED_POSITION_BEFORE', '⬆️ Показать перед меню')
        if send_before_menu
        else _t(texts, 'ADMIN_PINNED_POSITION_AFTER', '⬇️ Показать после меню')
    )
    toggle_callback = 'admin_pinned_message_position'

    start_mode_label = (
        _t(texts, 'ADMIN_PINNED_START_EVERY_TIME', '🔁 Показать при каждом /start')
        if send_on_every_start
        else _t(texts, 'ADMIN_PINNED_START_ONCE', '🚫 Показывать только один раз')
    )
    start_mode_callback = 'admin_pinned_message_start_mode'

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_PINNED_MESSAGE_UPDATE', '✏️ Обновить'),
                    callback_data='admin_pinned_message_edit',
                )
            ],
            [
                InlineKeyboardButton(
                    text=position_label,
                    callback_data=toggle_callback,
                )
            ],
            [
                InlineKeyboardButton(
                    text=start_mode_label,
                    callback_data=start_mode_callback,
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_PINNED_MESSAGE_DELETE', '🗑️ Удалить и отключить'),
                    callback_data='admin_pinned_message_delete',
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_messages')],
        ]
    )


def get_pinned_broadcast_confirm_keyboard(
    language: str = 'ru',
    pinned_message_id: int = 0,
) -> InlineKeyboardMarkup:
    """Клавиатура для выбора: разослать сейчас или только при /start."""
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_PINNED_BROADCAST_NOW', '📨 Разослать сейчас всем'),
                    callback_data=f'admin_pinned_broadcast_now:{pinned_message_id}',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_PINNED_BROADCAST_ON_START', '⏳ Только при /start'),
                    callback_data=f'admin_pinned_broadcast_skip:{pinned_message_id}',
                )
            ],
        ]
    )


def get_admin_monitoring_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_START', '▶️ Запустить'), callback_data='admin_mon_start'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_STOP', '⏸️ Остановить'), callback_data='admin_mon_stop'
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_STATUS', '📊 Статус'), callback_data='admin_mon_status'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_LOGS', '📋 Логи'), callback_data='admin_mon_logs'
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_SETTINGS_BUTTON', '⚙️ Настройки'),
                    callback_data='admin_mon_settings',
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_submenu_settings')],
        ]
    )


def get_admin_remnawave_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_REMNAWAVE_SYSTEM_STATS', '📊 Системная статистика'),
                    callback_data='admin_rw_system',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_REMNAWAVE_MANAGE_NODES', '🖥️ Управление нодами'),
                    callback_data='admin_rw_nodes',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_REMNAWAVE_SYNC', '🔄 Синхронизация'), callback_data='admin_rw_sync'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_REMNAWAVE_MANAGE_SQUADS', '🌐 Управление сквадами'),
                    callback_data='admin_rw_squads',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_REMNAWAVE_MIGRATION', '🚚 Переезд'), callback_data='admin_rw_migration'
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_REMNAWAVE_TRAFFIC', '📈 Трафик'), callback_data='admin_rw_traffic'
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_submenu_settings')],
        ]
    )


def get_admin_statistics_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_STATS_USERS', '👥 Пользователи'), callback_data='admin_stats_users'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_STATS_SUBSCRIPTIONS', '📱 Подписки'), callback_data='admin_stats_subs'
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_STATS_REVENUE', '💰 Доходы'), callback_data='admin_stats_revenue'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_STATS_REFERRALS', '🤝 Партнерка'), callback_data='admin_stats_referrals'
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_STATS_SUMMARY', '📊 Общая сводка'), callback_data='admin_stats_summary'
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_submenu_promo')],
        ]
    )


def get_user_management_keyboard(
    user_id: int, user_status: str, language: str = 'ru', back_callback: str = 'admin_users_list'
) -> InlineKeyboardMarkup:
    texts = get_texts(language)

    keyboard = [
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_USER_BALANCE', '💰 Баланс'), callback_data=f'admin_user_balance_{user_id}'
            ),
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_USER_SUBSCRIPTION_SETTINGS', '📱 Подписка и настройки'),
                callback_data=f'admin_user_subscription_{user_id}',
            ),
        ],
        [
            InlineKeyboardButton(
                text=texts.ADMIN_USER_PROMO_GROUP_BUTTON, callback_data=f'admin_user_promo_group_{user_id}'
            )
        ],
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_USER_REFERRALS_BUTTON', '🤝 Рефералы'),
                callback_data=f'admin_user_referrals_{user_id}',
            )
        ],
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_USER_STATISTICS', '📊 Статистика'),
                callback_data=f'admin_user_statistics_{user_id}',
            )
        ],
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_USER_TRANSACTIONS', '📋 Транзакции'),
                callback_data=f'admin_user_transactions_{user_id}',
            )
        ],
    ]

    keyboard.append(
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_USER_SEND_MESSAGE', '✉️ Отправить сообщение'),
                callback_data=f'admin_user_send_message_{user_id}',
            )
        ]
    )

    # Кнопка управления ограничениями
    keyboard.append(
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_USER_RESTRICTIONS', '⚠️ Ограничить'),
                callback_data=f'admin_user_restrictions_{user_id}',
            )
        ]
    )

    if user_status == 'active':
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_USER_BLOCK', '🚫 Заблокировать'), callback_data=f'admin_user_block_{user_id}'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_USER_DELETE', '🗑️ Удалить'), callback_data=f'admin_user_delete_{user_id}'
                ),
            ]
        )
    elif user_status == 'blocked':
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_USER_UNBLOCK', '✅ Разблокировать'),
                    callback_data=f'admin_user_unblock_{user_id}',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_USER_DELETE', '🗑️ Удалить'), callback_data=f'admin_user_delete_{user_id}'
                ),
            ]
        )
    elif user_status == 'deleted':
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_USER_ALREADY_DELETED', '❌ Пользователь удален'), callback_data='noop'
                )
            ]
        )

    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data=back_callback)])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_user_restrictions_keyboard(
    user_id: int, restriction_topup: bool, restriction_subscription: bool, language: str = 'ru'
) -> InlineKeyboardMarkup:
    """Клавиатура управления ограничениями пользователя."""
    texts = get_texts(language)

    keyboard = []

    # Toggle для ограничения пополнения
    topup_emoji = '🚫' if restriction_topup else '✅'
    topup_text = f'{topup_emoji} Пополнение'
    keyboard.append(
        [InlineKeyboardButton(text=topup_text, callback_data=f'admin_user_restriction_toggle_topup_{user_id}')]
    )

    # Toggle для ограничения подписки
    sub_emoji = '🚫' if restriction_subscription else '✅'
    sub_text = f'{sub_emoji} Продление/покупка'
    keyboard.append([InlineKeyboardButton(text=sub_text, callback_data=f'admin_user_restriction_toggle_sub_{user_id}')])

    # Кнопка изменения причины
    keyboard.append(
        [InlineKeyboardButton(text='📝 Изменить причину', callback_data=f'admin_user_restriction_reason_{user_id}')]
    )

    # Кнопка снятия всех ограничений (если есть хотя бы одно)
    if restriction_topup or restriction_subscription:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text='🔓 Снять все ограничения', callback_data=f'admin_user_restriction_clear_{user_id}'
                )
            ]
        )

    # Кнопка назад
    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_user_manage_{user_id}')])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_user_promo_group_keyboard(
    promo_groups: list[tuple[Any, int]],
    user_id: int,
    current_group_ids,  # Can be Optional[int] or List[int]
    language: str = 'ru',
) -> InlineKeyboardMarkup:
    texts = get_texts(language)

    # Ensure current_group_ids is a list
    if current_group_ids is None:
        current_group_ids = []
    elif isinstance(current_group_ids, int):
        current_group_ids = [current_group_ids]

    keyboard: list[list[InlineKeyboardButton]] = []

    for group, members_count in promo_groups:
        # Check if user has this group
        has_group = group.id in current_group_ids
        prefix = '✅' if has_group else '👥'
        count_text = f' ({members_count})' if members_count else ''
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=f'{prefix} {group.name}{count_text}',
                    callback_data=f'admin_user_promo_group_toggle_{user_id}_{group.id}',
                )
            ]
        )

    keyboard.append(
        [InlineKeyboardButton(text=texts.ADMIN_USER_PROMO_GROUP_BACK, callback_data=f'admin_user_manage_{user_id}')]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_confirmation_keyboard(
    confirm_action: str, cancel_action: str = 'admin_panel', language: str = 'ru'
) -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=texts.YES, callback_data=confirm_action),
                InlineKeyboardButton(text=texts.NO, callback_data=cancel_action),
            ]
        ]
    )


def get_promocode_type_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_PROMOCODE_TYPE_BALANCE', '💰 Баланс'), callback_data='promo_type_balance'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_PROMOCODE_TYPE_DAYS', '📅 Дни подписки'), callback_data='promo_type_days'
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_PROMOCODE_TYPE_TRIAL', '🎁 Триал'), callback_data='promo_type_trial'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_PROMOCODE_TYPE_PROMO_GROUP', '🏷️ Промогруппа'),
                    callback_data='promo_type_group',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_PROMOCODE_TYPE_DISCOUNT', '💸 Одноразовая скидка'),
                    callback_data='promo_type_discount',
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_promocodes')],
        ]
    )


def get_promocode_list_keyboard(
    promocodes: list, page: int, total_pages: int, language: str = 'ru'
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = []

    for promo in promocodes:
        status_emoji = '✅' if promo.is_active else '❌'
        type_emoji = {'balance': '💰', 'subscription_days': '📅', 'trial_subscription': '🎁'}.get(promo.type, '🎫')

        keyboard.append(
            [
                InlineKeyboardButton(
                    text=f'{status_emoji} {type_emoji} {promo.code}', callback_data=f'promo_manage_{promo.id}'
                )
            ]
        )

    if total_pages > 1:
        pagination_row = []

        if page > 1:
            pagination_row.append(InlineKeyboardButton(text='⬅️', callback_data=f'admin_promo_list_page_{page - 1}'))

        pagination_row.append(InlineKeyboardButton(text=f'{page}/{total_pages}', callback_data='current_page'))

        if page < total_pages:
            pagination_row.append(InlineKeyboardButton(text='➡️', callback_data=f'admin_promo_list_page_{page + 1}'))

        keyboard.append(pagination_row)

    keyboard.extend(
        [
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_PROMOCODES_CREATE', '➕ Создать'), callback_data='admin_promo_create'
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_promocodes')],
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_broadcast_target_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BROADCAST_TARGET_ALL', '👥 Всем'), callback_data='broadcast_all'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BROADCAST_TARGET_ACTIVE', '📱 С подпиской'), callback_data='broadcast_active'
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BROADCAST_TARGET_TRIAL', '🎁 Триал'), callback_data='broadcast_trial'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BROADCAST_TARGET_NO_SUB', '❌ Без подписки'), callback_data='broadcast_no_sub'
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BROADCAST_TARGET_EXPIRING', '⏰ Истекающие'),
                    callback_data='broadcast_expiring',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BROADCAST_TARGET_EXPIRED', '🔚 Истекшие'), callback_data='broadcast_expired'
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BROADCAST_TARGET_ACTIVE_ZERO', '🧊 Активна 0 ГБ'),
                    callback_data='broadcast_active_zero',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BROADCAST_TARGET_TRIAL_ZERO', '🥶 Триал 0 ГБ'),
                    callback_data='broadcast_trial_zero',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BROADCAST_TARGET_BY_TARIFF', '📦 По тарифу'),
                    callback_data='broadcast_by_tariff',
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_messages')],
        ]
    )


def get_custom_criteria_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CRITERIA_TODAY', '📅 Сегодня'), callback_data='criteria_today'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CRITERIA_WEEK', '📅 За неделю'), callback_data='criteria_week'
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CRITERIA_MONTH', '📅 За месяц'), callback_data='criteria_month'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CRITERIA_ACTIVE_TODAY', '⚡ Активные сегодня'),
                    callback_data='criteria_active_today',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CRITERIA_INACTIVE_WEEK', '💤 Неактивные 7+ дней'),
                    callback_data='criteria_inactive_week',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CRITERIA_INACTIVE_MONTH', '💤 Неактивные 30+ дней'),
                    callback_data='criteria_inactive_month',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CRITERIA_REFERRALS', '🤝 Через рефералов'), callback_data='criteria_referrals'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CRITERIA_PROMOCODES', '🎫 Использовали промокоды'),
                    callback_data='criteria_promocodes',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CRITERIA_DIRECT', '🎯 Прямая регистрация'), callback_data='criteria_direct'
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_messages')],
        ]
    )


def get_broadcast_history_keyboard(page: int, total_pages: int, language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = []

    if total_pages > 1:
        pagination_row = []

        if page > 1:
            pagination_row.append(InlineKeyboardButton(text='⬅️', callback_data=f'admin_msg_history_page_{page - 1}'))

        pagination_row.append(InlineKeyboardButton(text=f'{page}/{total_pages}', callback_data='current_page'))

        if page < total_pages:
            pagination_row.append(InlineKeyboardButton(text='➡️', callback_data=f'admin_msg_history_page_{page + 1}'))

        keyboard.append(pagination_row)

    keyboard.extend(
        [
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_HISTORY_REFRESH', '🔄 Обновить'), callback_data='admin_msg_history'
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_messages')],
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_sync_options_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = [
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_SYNC_FULL', '🔄 Полная синхронизация'), callback_data='sync_all_users'
            )
        ],
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_SYNC_TO_PANEL', '⬆️ Синхронизация в панель'), callback_data='sync_to_panel'
            )
        ],
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_SYNC_ONLY_NEW', '🆕 Только новые'), callback_data='sync_new_users'
            )
        ],
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_SYNC_UPDATE', '📈 Обновить данные'), callback_data='sync_update_data'
            )
        ],
        [
            InlineKeyboardButton(text=_t(texts, 'ADMIN_SYNC_VALIDATE', '🔍 Валидация'), callback_data='sync_validate'),
            InlineKeyboardButton(text=_t(texts, 'ADMIN_SYNC_CLEANUP', '🧹 Очистка'), callback_data='sync_cleanup'),
        ],
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_SYNC_RECOMMENDATIONS', '💡 Рекомендации'), callback_data='sync_recommendations'
            )
        ],
        [InlineKeyboardButton(text=texts.BACK, callback_data='admin_remnawave')],
    ]

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_sync_confirmation_keyboard(sync_type: str, language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = [
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_SYNC_CONFIRM', '✅ Подтвердить'), callback_data=f'confirm_{sync_type}'
            )
        ],
        [InlineKeyboardButton(text=_t(texts, 'ADMIN_CANCEL', '❌ Отмена'), callback_data='admin_rw_sync')],
    ]

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_sync_result_keyboard(sync_type: str, has_errors: bool = False, language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = []

    if has_errors:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SYNC_RETRY', '🔄 Повторить'), callback_data=f'sync_{sync_type}'
                )
            ]
        )

    if sync_type != 'all_users':
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SYNC_FULL', '🔄 Полная синхронизация'), callback_data='sync_all_users'
                )
            ]
        )

    keyboard.extend(
        [
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_STATS_BUTTON', '📊 Статистика'), callback_data='admin_rw_system'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SYNC_VALIDATE', '🔍 Валидация'), callback_data='sync_validate'
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SYNC_BACK', '⬅️ К синхронизации'), callback_data='admin_rw_sync'
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BACK_TO_MAIN', '🏠 В главное меню'), callback_data='admin_remnawave'
                )
            ],
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_period_selection_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_PERIOD_TODAY', '📅 Сегодня'), callback_data='period_today'),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_PERIOD_YESTERDAY', '📅 Вчера'), callback_data='period_yesterday'
                ),
            ],
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_PERIOD_WEEK', '📅 Неделя'), callback_data='period_week'),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_PERIOD_MONTH', '📅 Месяц'), callback_data='period_month'),
            ],
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_PERIOD_ALL', '📅 Все время'), callback_data='period_all')],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_statistics')],
        ]
    )


def get_node_management_keyboard(node_uuid: str, language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_NODE_ENABLE', '▶️ Включить'), callback_data=f'node_enable_{node_uuid}'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_NODE_DISABLE', '⏸️ Отключить'), callback_data=f'node_disable_{node_uuid}'
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_NODE_RESTART', '🔄 Перезагрузить'), callback_data=f'node_restart_{node_uuid}'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_NODE_STATS', '📊 Статистика'), callback_data=f'node_stats_{node_uuid}'
                ),
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_rw_nodes')],
        ]
    )


def get_squad_management_keyboard(squad_uuid: str, language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SQUAD_ADD_ALL', '👥 Добавить всех пользователей'),
                    callback_data=f'squad_add_users_{squad_uuid}',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SQUAD_REMOVE_ALL', '❌ Удалить всех пользователей'),
                    callback_data=f'squad_remove_users_{squad_uuid}',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SQUAD_EDIT', '✏️ Редактировать'), callback_data=f'squad_edit_{squad_uuid}'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SQUAD_DELETE', '🗑️ Удалить сквад'), callback_data=f'squad_delete_{squad_uuid}'
                ),
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_rw_squads')],
        ]
    )


def get_squad_edit_keyboard(squad_uuid: str, language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SQUAD_EDIT_INBOUNDS', '🔧 Изменить инбаунды'),
                    callback_data=f'squad_edit_inbounds_{squad_uuid}',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SQUAD_RENAME', '✏️ Переименовать'), callback_data=f'squad_rename_{squad_uuid}'
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BACK_TO_SQUADS', '⬅️ Назад к сквадам'),
                    callback_data=f'admin_squad_manage_{squad_uuid}',
                )
            ],
        ]
    )


def get_monitoring_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_START', '▶️ Запустить'), callback_data='admin_mon_start'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_STOP_HARD', '⏹️ Остановить'), callback_data='admin_mon_stop'
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_FORCE_CHECK', '🔄 Проверка подписок'),
                    callback_data='admin_mon_force_check',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_TRAFFIC_CHECK', '📊 Проверка трафика'),
                    callback_data='admin_mon_traffic_check',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_LOGS', '📋 Логи'), callback_data='admin_mon_logs'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_STATISTICS', '📈 Статистика'), callback_data='admin_mon_statistics'
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_TEST_NOTIFICATIONS', '🧪 Тест уведомлений'),
                    callback_data='admin_mon_test_notifications',
                ),
                InlineKeyboardButton(text='⚙️ Настройки трафика', callback_data='admin_mon_traffic_settings'),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BACK_TO_ADMIN', '⬅️ Назад в админку'), callback_data='admin_panel'
                )
            ],
        ]
    )


def get_monitoring_logs_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_HISTORY_REFRESH', '🔄 Обновить'), callback_data='admin_mon_logs'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_CLEAR_OLD', '🗑️ Очистить старые'),
                    callback_data='admin_mon_clear_logs',
                ),
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_monitoring')],
        ]
    )


def get_monitoring_logs_navigation_keyboard(
    current_page: int, total_pages: int, has_logs: bool = True, language: str = 'ru'
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = []

    if total_pages > 1:
        nav_row = []

        if current_page > 1:
            nav_row.append(InlineKeyboardButton(text='⬅️', callback_data=f'admin_mon_logs_page_{current_page - 1}'))

        nav_row.append(InlineKeyboardButton(text=f'{current_page}/{total_pages}', callback_data='current_page_info'))

        if current_page < total_pages:
            nav_row.append(InlineKeyboardButton(text='➡️', callback_data=f'admin_mon_logs_page_{current_page + 1}'))

        keyboard.append(nav_row)

    management_row = []

    refresh_button = InlineKeyboardButton(
        text=_t(texts, 'ADMIN_HISTORY_REFRESH', '🔄 Обновить'), callback_data='admin_mon_logs'
    )

    if has_logs:
        management_row.extend(
            [
                refresh_button,
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_CLEAR', '🗑️ Очистить'), callback_data='admin_mon_clear_logs'
                ),
            ]
        )
    else:
        management_row.append(refresh_button)

    keyboard.append(management_row)

    keyboard.append(
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_BACK_TO_MONITORING', '⬅️ Назад к мониторингу'), callback_data='admin_monitoring'
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_log_detail_keyboard(log_id: int, current_page: int = 1, language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_DELETE_LOG', '🗑️ Удалить этот лог'),
                    callback_data=f'admin_mon_delete_log_{log_id}',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_BACK_TO_LOGS', '⬅️ К списку логов'),
                    callback_data=f'admin_mon_logs_page_{current_page}',
                )
            ],
        ]
    )


def get_monitoring_clear_confirm_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_CONFIRM_CLEAR', '✅ Да, очистить'),
                    callback_data='admin_mon_clear_logs_confirm',
                ),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_CANCEL', '❌ Отмена'), callback_data='admin_mon_logs'),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_CLEAR_ALL', '🗑️ Очистить ВСЕ логи'),
                    callback_data='admin_mon_clear_all_logs',
                )
            ],
        ]
    )


def get_monitoring_status_keyboard(
    is_running: bool, last_check_ago_minutes: int = 0, language: str = 'ru'
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = []

    control_row = []
    if is_running:
        control_row.extend(
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_STOP_HARD', '⏹️ Остановить'), callback_data='admin_mon_stop'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_RESTART', '🔄 Перезапустить'), callback_data='admin_mon_restart'
                ),
            ]
        )
    else:
        control_row.append(
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_MONITORING_START', '▶️ Запустить'), callback_data='admin_mon_start'
            )
        )

    keyboard.append(control_row)

    monitoring_row = []

    if not is_running or last_check_ago_minutes > 10:
        monitoring_row.append(
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_MONITORING_FORCE_CHECK', '⚡ Срочная проверка'),
                callback_data='admin_mon_force_check',
            )
        )
    else:
        monitoring_row.append(
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_MONITORING_CHECK_NOW', '🔄 Проверить сейчас'),
                callback_data='admin_mon_force_check',
            )
        )

    keyboard.append(monitoring_row)

    info_row = [
        InlineKeyboardButton(text=_t(texts, 'ADMIN_MONITORING_LOGS', '📋 Логи'), callback_data='admin_mon_logs'),
        InlineKeyboardButton(
            text=_t(texts, 'ADMIN_MONITORING_STATISTICS', '📊 Статистика'), callback_data='admin_mon_statistics'
        ),
    ]
    keyboard.append(info_row)

    test_row = [
        InlineKeyboardButton(
            text=_t(texts, 'ADMIN_MONITORING_TEST_NOTIFICATIONS', '🧪 Тест уведомлений'),
            callback_data='admin_mon_test_notifications',
        )
    ]
    keyboard.append(test_row)

    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='admin_submenu_settings')])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_monitoring_settings_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_SET_INTERVAL', '⏱️ Интервал проверки'),
                    callback_data='admin_mon_set_interval',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_NOTIFICATIONS', '🔔 Уведомления'),
                    callback_data='admin_mon_toggle_notifications',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_AUTOPAY_SETTINGS', '💳 Настройки автооплаты'),
                    callback_data='admin_mon_autopay_settings',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_AUTO_CLEANUP', '🧹 Автоочистка логов'),
                    callback_data='admin_mon_auto_cleanup',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BACK_TO_MONITORING', '⬅️ К мониторингу'), callback_data='admin_monitoring'
                )
            ],
        ]
    )


def get_log_type_filter_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_FILTER_SUCCESS', '✅ Успешные'),
                    callback_data='admin_mon_logs_filter_success',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_FILTER_ERRORS', '❌ Ошибки'),
                    callback_data='admin_mon_logs_filter_error',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_FILTER_CYCLES', '🔄 Циклы мониторинга'),
                    callback_data='admin_mon_logs_filter_cycle',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_FILTER_AUTOPAY', '💳 Автооплаты'),
                    callback_data='admin_mon_logs_filter_autopay',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_ALL_LOGS', '📋 Все логи'), callback_data='admin_mon_logs'
                ),
                InlineKeyboardButton(text=texts.BACK, callback_data='admin_monitoring'),
            ],
        ]
    )


def get_admin_servers_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SERVERS_LIST', '📋 Список серверов'), callback_data='admin_servers_list'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SERVERS_SYNC', '🔄 Синхронизация'), callback_data='admin_servers_sync'
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SERVERS_ADD', '➕ Добавить сервер'), callback_data='admin_servers_add'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SERVERS_STATS', '📊 Статистика'), callback_data='admin_servers_stats'
                ),
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_subscriptions')],
        ]
    )


def get_server_edit_keyboard(server_id: int, is_available: bool, language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    toggle_text = (
        _t(texts, 'ADMIN_SERVER_DISABLE', '❌ Отключить')
        if is_available
        else _t(texts, 'ADMIN_SERVER_ENABLE', '✅ Включить')
    )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SERVER_EDIT_NAME', '✏️ Название'),
                    callback_data=f'admin_server_edit_name_{server_id}',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SERVER_EDIT_PRICE', '💰 Цена'),
                    callback_data=f'admin_server_edit_price_{server_id}',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SERVER_EDIT_COUNTRY', '🌍 Страна'),
                    callback_data=f'admin_server_edit_country_{server_id}',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SERVER_EDIT_LIMIT', '👥 Лимит'),
                    callback_data=f'admin_server_edit_limit_{server_id}',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SERVER_EDIT_DESCRIPTION', '📝 Описание'),
                    callback_data=f'admin_server_edit_desc_{server_id}',
                )
            ],
            [InlineKeyboardButton(text=toggle_text, callback_data=f'admin_server_toggle_{server_id}')],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SERVER_DELETE', '🗑️ Удалить'), callback_data=f'admin_server_delete_{server_id}'
                ),
                InlineKeyboardButton(text=texts.BACK, callback_data='admin_servers_list'),
            ],
        ]
    )


def get_admin_pagination_keyboard(
    current_page: int, total_pages: int, callback_prefix: str, back_callback: str = 'admin_panel', language: str = 'ru'
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = []

    if total_pages > 1:
        row = []

        if current_page > 1:
            row.append(InlineKeyboardButton(text='⬅️', callback_data=f'{callback_prefix}_page_{current_page - 1}'))

        row.append(InlineKeyboardButton(text=f'{current_page}/{total_pages}', callback_data='current_page'))

        if current_page < total_pages:
            row.append(InlineKeyboardButton(text='➡️', callback_data=f'{callback_prefix}_page_{current_page + 1}'))

        keyboard.append(row)

    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data=back_callback)])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_maintenance_keyboard(
    language: str, is_maintenance_active: bool, is_monitoring_active: bool, panel_has_issues: bool = False
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = []

    if is_maintenance_active:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MAINTENANCE_DISABLE', '🟢 Выключить техработы'),
                    callback_data='maintenance_toggle',
                )
            ]
        )
    else:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MAINTENANCE_ENABLE', '🔧 Включить техработы'),
                    callback_data='maintenance_toggle',
                )
            ]
        )

    if is_monitoring_active:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MAINTENANCE_STOP_MONITORING', '⏹️ Остановить мониторинг'),
                    callback_data='maintenance_monitoring',
                )
            ]
        )
    else:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MAINTENANCE_START_MONITORING', '▶️ Запустить мониторинг'),
                    callback_data='maintenance_monitoring',
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_MAINTENANCE_CHECK_API', '🔍 Проверить API'), callback_data='maintenance_check_api'
            ),
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_MAINTENANCE_PANEL_STATUS', '🌐 Статус панели')
                + ('⚠️' if panel_has_issues else ''),
                callback_data='maintenance_check_panel',
            ),
        ]
    )

    keyboard.append(
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_MAINTENANCE_SEND_NOTIFICATION', '📢 Отправить уведомление'),
                callback_data='maintenance_manual_notify',
            )
        ]
    )

    keyboard.append(
        [
            InlineKeyboardButton(text=_t(texts, 'ADMIN_REFRESH', '🔄 Обновить'), callback_data='maintenance_panel'),
            InlineKeyboardButton(text=texts.BACK, callback_data='admin_submenu_settings'),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_sync_simplified_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = [
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_SYNC_FULL', '🔄 Полная синхронизация'), callback_data='sync_all_users'
            )
        ],
        [InlineKeyboardButton(text=texts.BACK, callback_data='admin_remnawave')],
    ]

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_welcome_text_keyboard(language: str = 'ru', is_enabled: bool = True) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    toggle_text = (
        _t(texts, 'ADMIN_WELCOME_DISABLE', '🔴 Отключить')
        if is_enabled
        else _t(texts, 'ADMIN_WELCOME_ENABLE', '🟢 Включить')
    )
    toggle_callback = 'toggle_welcome_text'

    keyboard = [
        [InlineKeyboardButton(text=toggle_text, callback_data=toggle_callback)],
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_WELCOME_EDIT', '📝 Изменить текст'), callback_data='edit_welcome_text'
            ),
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_WELCOME_SHOW', '👁️ Показать текущий'), callback_data='show_welcome_text'
            ),
        ],
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_WELCOME_PREVIEW', '👁️ Предпросмотр'), callback_data='preview_welcome_text'
            ),
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_WELCOME_RESET', '🔄 Сбросить'), callback_data='reset_welcome_text'
            ),
        ],
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_WELCOME_HTML', '🏷️ HTML форматирование'), callback_data='show_formatting_help'
            ),
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_WELCOME_PLACEHOLDERS', '💡 Плейсхолдеры'), callback_data='show_placeholders_help'
            ),
        ],
        [InlineKeyboardButton(text=texts.BACK, callback_data='admin_submenu_communications')],
    ]

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


DEFAULT_BROADCAST_BUTTONS = ('home',)

BROADCAST_BUTTONS = {
    'balance': {
        'default_text': '💰 Пополнить баланс',
        'text_key': 'ADMIN_BROADCAST_BUTTON_BALANCE',
        'callback': 'balance_topup',
    },
    'referrals': {
        'default_text': '🤝 Партнерка',
        'text_key': 'ADMIN_BROADCAST_BUTTON_REFERRALS',
        'callback': 'menu_referrals',
    },
    'promocode': {
        'default_text': '🎫 Промокод',
        'text_key': 'ADMIN_BROADCAST_BUTTON_PROMOCODE',
        'callback': 'menu_promocode',
    },
    'connect': {
        'default_text': '🔗 Подключиться',
        'text_key': 'ADMIN_BROADCAST_BUTTON_CONNECT',
        'callback': 'subscription_connect',
    },
    'subscription': {
        'default_text': '📱 Подписка',
        'text_key': 'ADMIN_BROADCAST_BUTTON_SUBSCRIPTION',
        'callback': 'menu_subscription',
    },
    'support': {
        'default_text': '🛠️ Техподдержка',
        'text_key': 'ADMIN_BROADCAST_BUTTON_SUPPORT',
        'callback': 'menu_support',
    },
    'home': {
        'default_text': '🏠 На главную',
        'text_key': 'ADMIN_BROADCAST_BUTTON_HOME',
        'callback': 'back_to_menu',
    },
}

BROADCAST_BUTTON_ROWS: tuple[tuple[str, ...], ...] = (
    ('balance', 'referrals'),
    ('promocode', 'connect'),
    ('subscription', 'support'),
    ('home',),
)


def get_broadcast_button_config(language: str) -> dict[str, dict[str, str]]:
    texts = get_texts(language)
    return {
        key: {
            'text': texts.t(config['text_key'], config['default_text']),
            'callback': config['callback'],
        }
        for key, config in BROADCAST_BUTTONS.items()
    }


def get_broadcast_button_labels(language: str) -> dict[str, str]:
    return {key: value['text'] for key, value in get_broadcast_button_config(language).items()}


def get_message_buttons_selector_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    return get_updated_message_buttons_selector_keyboard_with_media(list(DEFAULT_BROADCAST_BUTTONS), False, language)


def get_broadcast_media_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BROADCAST_ADD_PHOTO', '📷 Добавить фото'), callback_data='add_media_photo'
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BROADCAST_ADD_VIDEO', '🎥 Добавить видео'), callback_data='add_media_video'
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BROADCAST_ADD_DOCUMENT', '📄 Добавить документ'),
                    callback_data='add_media_document',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BROADCAST_SKIP_MEDIA', '⏭️ Пропустить медиа'), callback_data='skip_media'
                ),
            ],
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_CANCEL', '❌ Отмена'), callback_data='admin_messages')],
        ]
    )


def get_media_confirm_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BROADCAST_USE_MEDIA', '✅ Использовать это медиа'),
                    callback_data='confirm_media',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BROADCAST_REPLACE_MEDIA', '🔄 Заменить медиа'), callback_data='replace_media'
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BROADCAST_NO_MEDIA', '⏭️ Без медиа'), callback_data='skip_media'
                ),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_CANCEL', '❌ Отмена'), callback_data='admin_messages'),
            ],
        ]
    )


def get_updated_message_buttons_selector_keyboard_with_media(
    selected_buttons: list, has_media: bool = False, language: str = 'ru'
) -> InlineKeyboardMarkup:
    selected_buttons = selected_buttons or []

    texts = get_texts(language)
    button_config_map = get_broadcast_button_config(language)
    keyboard: list[list[InlineKeyboardButton]] = []

    for row in BROADCAST_BUTTON_ROWS:
        row_buttons: list[InlineKeyboardButton] = []
        for button_key in row:
            button_config = button_config_map[button_key]
            base_text = button_config['text']
            if button_key in selected_buttons:
                if ' ' in base_text:
                    toggle_text = f'✅ {base_text.split(" ", 1)[1]}'
                else:
                    toggle_text = f'✅ {base_text}'
            else:
                toggle_text = base_text
            row_buttons.append(InlineKeyboardButton(text=toggle_text, callback_data=f'btn_{button_key}'))
        if row_buttons:
            keyboard.append(row_buttons)

    if has_media:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BROADCAST_CHANGE_MEDIA', '🖼️ Изменить медиа'), callback_data='change_media'
                )
            ]
        )

    keyboard.extend(
        [
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_CONTINUE', '✅ Продолжить'), callback_data='buttons_confirm')],
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_CANCEL', '❌ Отмена'), callback_data='admin_messages')],
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)
