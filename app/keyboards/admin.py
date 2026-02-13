from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.localization.texts import get_texts


def _t(texts, key: str) -> str:
    """Helper for localized button labels."""
    return texts.t(key)


def get_admin_main_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MAIN_USERS_SUBSCRIPTIONS'),
                    callback_data='admin_submenu_users',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MAIN_SERVERS'),
                    callback_data='admin_servers',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MAIN_TARIFFS'),
                    callback_data='admin_tariffs',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MAIN_PRICING'),
                    callback_data='admin_pricing',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MAIN_PROMO_STATS'),
                    callback_data='admin_submenu_promo',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MAIN_SUPPORT'),
                    callback_data='admin_submenu_support',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MAIN_MESSAGES'),
                    callback_data='admin_submenu_communications',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MAIN_SETTINGS'),
                    callback_data='admin_submenu_settings',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MAIN_SYSTEM'),
                    callback_data='admin_submenu_system',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MAIN_TRIALS'),
                    callback_data='admin_trials',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MAIN_PAYMENTS'),
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
                    text=_t(texts, 'ADMIN_CONTESTS'),
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
                    text=_t(texts, 'ADMIN_COMMUNICATIONS_POLLS'),
                    callback_data='admin_polls',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_COMMUNICATIONS_PROMO_OFFERS'),
                    callback_data='admin_promo_offers',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_COMMUNICATIONS_WELCOME_TEXT'),
                    callback_data='welcome_text_panel',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_COMMUNICATIONS_MENU_MESSAGES'),
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
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_SUPPORT_TICKETS'), callback_data='admin_tickets')],
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_SUPPORT_AUDIT'), callback_data='admin_support_audit')],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SUPPORT_SETTINGS'),
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
                    text=_t(texts, 'ADMIN_SETTINGS_BOT_CONFIG'),
                    callback_data='admin_bot_config',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_SETTINGS'),
                    callback_data='admin_mon_settings',
                )
            ],
            [
                InlineKeyboardButton(text=texts.ADMIN_RULES, callback_data='admin_rules'),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_SETTINGS_MAINTENANCE'), callback_data='maintenance_panel'),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SETTINGS_PRIVACY_POLICY'),
                    callback_data='admin_privacy_policy',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SETTINGS_PUBLIC_OFFER'),
                    callback_data='admin_public_offer',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SETTINGS_FAQ'),
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
                InlineKeyboardButton(text=_t(texts, 'ADMIN_SYSTEM_UPDATES'), callback_data='admin_updates'),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_SYSTEM_BACKUPS'), callback_data='backup_panel'),
            ],
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_SYSTEM_LOGS'), callback_data='admin_system_logs')],
            [InlineKeyboardButton(text=texts.t('ADMIN_REPORTS'), callback_data='admin_reports')],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')],
        ]
    )


def get_admin_trials_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_TRIALS_RESET_BUTTON'),
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
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_REPORTS_PREVIOUS_DAY'), callback_data='admin_reports_daily')],
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_REPORTS_LAST_WEEK'), callback_data='admin_reports_weekly')],
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_REPORTS_LAST_MONTH'), callback_data='admin_reports_monthly')],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')],
        ]
    )


def get_admin_report_result_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=texts.t('REPORT_CLOSE'), callback_data='admin_close_report')]]
    )


def get_admin_users_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_USERS_ALL'), callback_data='admin_users_list'),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_USERS_SEARCH'), callback_data='admin_users_search'),
            ],
            [
                InlineKeyboardButton(text=texts.ADMIN_STATISTICS, callback_data='admin_users_stats'),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_USERS_INACTIVE'), callback_data='admin_users_inactive'),
            ],
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_USERS_FILTERS'), callback_data='admin_users_filters')],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_USERS_BLACKLIST'),
                    callback_data='admin_blacklist_settings',
                )
            ],
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_USERS_BULK_BAN'), callback_data='admin_bulk_ban_start')],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_USERS_BLOCKED_CHECK'),
                    callback_data='admin_blocked_users',
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
                    text=_t(texts, 'ADMIN_USERS_FILTER_BALANCE'),
                    callback_data='admin_users_balance_filter',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_USERS_FILTER_RENEW_READY'),
                    callback_data='admin_users_ready_to_renew_filter',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_USERS_FILTER_POTENTIAL_CUSTOMERS'),
                    callback_data='admin_users_potential_customers_filter',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_USERS_FILTER_CAMPAIGN'),
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
                InlineKeyboardButton(text=_t(texts, 'ADMIN_SUBSCRIPTIONS_ALL'), callback_data='admin_subs_list'),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SUBSCRIPTIONS_EXPIRING'), callback_data='admin_subs_expiring'
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SUBSCRIPTIONS_COUNTRIES'),
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
                InlineKeyboardButton(text=_t(texts, 'ADMIN_PROMOCODES_ALL'), callback_data='admin_promo_list'),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_PROMOCODES_CREATE'), callback_data='admin_promo_create'),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_PROMOCODES_GENERAL_STATS'),
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
                InlineKeyboardButton(text=_t(texts, 'ADMIN_CAMPAIGNS_LIST'), callback_data='admin_campaigns_list'),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_CAMPAIGNS_CREATE'), callback_data='admin_campaigns_create'),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CAMPAIGNS_GENERAL_STATS'),
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
                    text=_t(texts, 'ADMIN_CONTESTS_REFERRAL'),
                    callback_data='admin_contests_referral',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CONTESTS_DAILY'),
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
                    text=_t(texts, 'ADMIN_CONTESTS_LIST'),
                    callback_data='admin_contests_list',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CONTESTS_CREATE'),
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
                    text=_t(texts, 'ADMIN_CONTEST_MODE_PAID'),
                    callback_data='admin_contest_mode_paid',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CONTEST_MODE_REGISTERED'),
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
    toggle_text = _t(texts, 'ADMIN_CONTEST_DISABLE') if is_enabled else _t(texts, 'ADMIN_CONTEST_ENABLE')
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=toggle_text, callback_data=f'admin_daily_toggle_{template_id}'),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CONTEST_START_NOW'),
                    callback_data=f'admin_daily_start_{template_id}',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CONTEST_START_MANUAL'),
                    callback_data=f'admin_daily_manual_{template_id}',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_EDIT_PRIZE_TYPE'),
                    callback_data=f'admin_daily_edit_{template_id}_prize_type',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_EDIT_PRIZE_VALUE'),
                    callback_data=f'admin_daily_edit_{template_id}_prize_value',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_EDIT_MAX_WINNERS'),
                    callback_data=f'admin_daily_edit_{template_id}_max_winners',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_EDIT_ATTEMPTS'),
                    callback_data=f'admin_daily_edit_{template_id}_attempts_per_user',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_EDIT_TIMES'),
                    callback_data=f'admin_daily_edit_{template_id}_times_per_day',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_EDIT_SCHEDULE'),
                    callback_data=f'admin_daily_edit_{template_id}_schedule_times',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_EDIT_COOLDOWN'),
                    callback_data=f'admin_daily_edit_{template_id}_cooldown_hours',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_EDIT_PAYLOAD'),
                    callback_data=f'admin_daily_payload_{template_id}',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_RESET_ATTEMPTS'),
                    callback_data=f'admin_daily_reset_attempts_{template_id}',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CLOSE_ROUND'),
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
    toggle_text = _t(texts, 'ADMIN_CONTEST_DISABLE') if is_active else _t(texts, 'ADMIN_CONTEST_ENABLE')

    rows = [
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_CONTEST_LEADERBOARD'),
                callback_data=f'admin_contest_leaderboard_{contest_id}',
            ),
            InlineKeyboardButton(
                text=toggle_text,
                callback_data=f'admin_contest_toggle_{contest_id}',
            ),
        ],
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_CONTEST_DETAILED_STATS'),
                callback_data=f'admin_contest_detailed_stats_{contest_id}',
            ),
        ],
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_CONTEST_EDIT_SUMMARY_TIMES'),
                callback_data=f'admin_contest_edit_times_{contest_id}',
            ),
        ],
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_CONTEST_VIRTUAL_USERS'),
                callback_data=f'admin_contest_vp_{contest_id}',
            ),
        ],
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_CONTEST_SYNC'),
                callback_data=f'admin_contest_sync_{contest_id}',
            ),
            InlineKeyboardButton(
                text=texts.t('ADMIN_CONTEST_DEBUG'),
                callback_data=f'admin_contest_debug_{contest_id}',
            ),
        ],
    ]

    if can_delete:
        rows.append(
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CONTEST_DELETE'),
                    callback_data=f'admin_contest_delete_{contest_id}',
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_BACK_TO_LIST'),
                callback_data='admin_contests_list',
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_campaign_management_keyboard(campaign_id: int, is_active: bool, language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)
    status_text = _t(texts, 'ADMIN_CAMPAIGN_DISABLE') if is_active else _t(texts, 'ADMIN_CAMPAIGN_ENABLE')

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CAMPAIGN_STATS'),
                    callback_data=f'admin_campaign_stats_{campaign_id}',
                ),
                InlineKeyboardButton(
                    text=status_text,
                    callback_data=f'admin_campaign_toggle_{campaign_id}',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CAMPAIGN_EDIT'),
                    callback_data=f'admin_campaign_edit_{campaign_id}',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CAMPAIGN_DELETE'),
                    callback_data=f'admin_campaign_delete_{campaign_id}',
                )
            ],
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_BACK_TO_LIST'), callback_data='admin_campaigns_list')],
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
                text=_t(texts, 'ADMIN_CAMPAIGN_EDIT_NAME'),
                callback_data=f'admin_campaign_edit_name_{campaign_id}',
            ),
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_CAMPAIGN_EDIT_START'),
                callback_data=f'admin_campaign_edit_start_{campaign_id}',
            ),
        ]
    ]

    if bonus_type == 'balance':
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CAMPAIGN_BONUS_BALANCE'),
                    callback_data=f'admin_campaign_edit_balance_{campaign_id}',
                )
            ]
        )
    elif bonus_type == 'subscription':
        keyboard.extend(
            [
                [
                    InlineKeyboardButton(
                        text=_t(texts, 'ADMIN_CAMPAIGN_DURATION'),
                        callback_data=f'admin_campaign_edit_sub_days_{campaign_id}',
                    ),
                    InlineKeyboardButton(
                        text=_t(texts, 'ADMIN_CAMPAIGN_TRAFFIC'),
                        callback_data=f'admin_campaign_edit_sub_traffic_{campaign_id}',
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=_t(texts, 'ADMIN_CAMPAIGN_DEVICES'),
                        callback_data=f'admin_campaign_edit_sub_devices_{campaign_id}',
                    ),
                    InlineKeyboardButton(
                        text=_t(texts, 'ADMIN_CAMPAIGN_SERVERS'),
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
                        text=_t(texts, 'ADMIN_CAMPAIGN_TARIFF'),
                        callback_data=f'admin_campaign_edit_tariff_{campaign_id}',
                    ),
                    InlineKeyboardButton(
                        text=_t(texts, 'ADMIN_CAMPAIGN_DURATION'),
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
                    text=_t(texts, 'ADMIN_CAMPAIGN_BONUS_BALANCE'),
                    callback_data='campaign_bonus_balance',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CAMPAIGN_BONUS_SUBSCRIPTION'),
                    callback_data='campaign_bonus_subscription',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CAMPAIGN_BONUS_TARIFF'), callback_data='campaign_bonus_tariff'
                ),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_CAMPAIGN_BONUS_NONE'), callback_data='campaign_bonus_none'),
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_campaigns')],
        ]
    )


def get_promocode_management_keyboard(promo_id: int, language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_PROMOCODE_EDIT'), callback_data=f'promo_edit_{promo_id}'),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_PROMOCODE_TOGGLE'), callback_data=f'promo_toggle_{promo_id}'
                ),
            ],
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_PROMOCODE_STATS'), callback_data=f'promo_stats_{promo_id}'),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_PROMOCODE_DELETE'), callback_data=f'promo_delete_{promo_id}'
                ),
            ],
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_BACK_TO_LIST'), callback_data='admin_promo_list')],
        ]
    )


def get_admin_messages_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_MESSAGES_ALL_USERS'), callback_data='admin_msg_all'),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MESSAGES_BY_SUBSCRIPTIONS'),
                    callback_data='admin_msg_by_sub',
                ),
            ],
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_MESSAGES_BY_CRITERIA'), callback_data='admin_msg_custom'),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_MESSAGES_HISTORY'), callback_data='admin_msg_history'),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_PINNED_MESSAGE'),
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
        _t(texts, 'ADMIN_PINNED_POSITION_BEFORE') if send_before_menu else _t(texts, 'ADMIN_PINNED_POSITION_AFTER')
    )
    toggle_callback = 'admin_pinned_message_position'

    start_mode_label = (
        _t(texts, 'ADMIN_PINNED_START_EVERY_TIME') if send_on_every_start else _t(texts, 'ADMIN_PINNED_START_ONCE')
    )
    start_mode_callback = 'admin_pinned_message_start_mode'

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_PINNED_MESSAGE_UPDATE'),
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
                    text=_t(texts, 'ADMIN_PINNED_MESSAGE_DELETE'),
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
                    text=_t(texts, 'ADMIN_PINNED_BROADCAST_NOW'),
                    callback_data=f'admin_pinned_broadcast_now:{pinned_message_id}',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_PINNED_BROADCAST_ON_START'),
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
                InlineKeyboardButton(text=_t(texts, 'ADMIN_MONITORING_START'), callback_data='admin_mon_start'),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_MONITORING_STOP'), callback_data='admin_mon_stop'),
            ],
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_MONITORING_STATUS'), callback_data='admin_mon_status'),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_MONITORING_LOGS'), callback_data='admin_mon_logs'),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_SETTINGS_BUTTON'),
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
                    text=_t(texts, 'ADMIN_REMNAWAVE_SYSTEM_STATS'),
                    callback_data='admin_rw_system',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_REMNAWAVE_MANAGE_NODES'),
                    callback_data='admin_rw_nodes',
                ),
            ],
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_REMNAWAVE_SYNC'), callback_data='admin_rw_sync'),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_REMNAWAVE_MANAGE_SQUADS'),
                    callback_data='admin_rw_squads',
                ),
            ],
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_REMNAWAVE_MIGRATION'), callback_data='admin_rw_migration')],
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_REMNAWAVE_TRAFFIC'), callback_data='admin_rw_traffic')],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_submenu_settings')],
        ]
    )


def get_admin_statistics_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_STATS_USERS'), callback_data='admin_stats_users'),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_STATS_SUBSCRIPTIONS'), callback_data='admin_stats_subs'),
            ],
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_STATS_REVENUE'), callback_data='admin_stats_revenue'),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_STATS_REFERRALS'), callback_data='admin_stats_referrals'),
            ],
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_STATS_SUMMARY'), callback_data='admin_stats_summary')],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_submenu_promo')],
        ]
    )


def get_user_management_keyboard(
    user_id: int, user_status: str, language: str = 'ru', back_callback: str = 'admin_users_list'
) -> InlineKeyboardMarkup:
    texts = get_texts(language)

    keyboard = [
        [
            InlineKeyboardButton(text=_t(texts, 'ADMIN_USER_BALANCE'), callback_data=f'admin_user_balance_{user_id}'),
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_USER_SUBSCRIPTION_SETTINGS'),
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
                text=_t(texts, 'ADMIN_USER_REFERRALS_BUTTON'),
                callback_data=f'admin_user_referrals_{user_id}',
            )
        ],
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_USER_STATISTICS'),
                callback_data=f'admin_user_statistics_{user_id}',
            )
        ],
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_USER_TRANSACTIONS'),
                callback_data=f'admin_user_transactions_{user_id}',
            )
        ],
    ]

    keyboard.append(
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_USER_SEND_MESSAGE'),
                callback_data=f'admin_user_send_message_{user_id}',
            )
        ]
    )

    # Кнопка управления ограничениями
    keyboard.append(
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_USER_RESTRICTIONS'),
                callback_data=f'admin_user_restrictions_{user_id}',
            )
        ]
    )

    if user_status == 'active':
        keyboard.append(
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_USER_BLOCK'), callback_data=f'admin_user_block_{user_id}'),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_USER_DELETE'), callback_data=f'admin_user_delete_{user_id}'),
            ]
        )
    elif user_status == 'blocked':
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_USER_UNBLOCK'),
                    callback_data=f'admin_user_unblock_{user_id}',
                ),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_USER_DELETE'), callback_data=f'admin_user_delete_{user_id}'),
            ]
        )
    elif user_status == 'deleted':
        keyboard.append([InlineKeyboardButton(text=_t(texts, 'ADMIN_USER_ALREADY_DELETED'), callback_data='noop')])

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
    topup_text = texts.t('ADMIN_USER_RESTRICTION_TOPUP_TOGGLE').format(emoji=topup_emoji)
    keyboard.append(
        [InlineKeyboardButton(text=topup_text, callback_data=f'admin_user_restriction_toggle_topup_{user_id}')]
    )

    # Toggle для ограничения подписки
    sub_emoji = '🚫' if restriction_subscription else '✅'
    sub_text = texts.t('ADMIN_USER_RESTRICTION_SUB_TOGGLE').format(emoji=sub_emoji)
    keyboard.append([InlineKeyboardButton(text=sub_text, callback_data=f'admin_user_restriction_toggle_sub_{user_id}')])

    # Кнопка изменения причины
    keyboard.append(
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_USER_RESTRICTION_REASON_BUTTON'),
                callback_data=f'admin_user_restriction_reason_{user_id}',
            )
        ]
    )

    # Кнопка снятия всех ограничений (если есть хотя бы одно)
    if restriction_topup or restriction_subscription:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_RESTRICTION_CLEAR_ALL_BUTTON'),
                    callback_data=f'admin_user_restriction_clear_{user_id}',
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
                    text=_t(texts, 'ADMIN_PROMOCODE_TYPE_BALANCE'), callback_data='promo_type_balance'
                ),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_PROMOCODE_TYPE_DAYS'), callback_data='promo_type_days'),
            ],
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_PROMOCODE_TYPE_TRIAL'), callback_data='promo_type_trial'),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_PROMOCODE_TYPE_PROMO_GROUP'),
                    callback_data='promo_type_group',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_PROMOCODE_TYPE_DISCOUNT'),
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
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_PROMOCODES_CREATE'), callback_data='admin_promo_create')],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_promocodes')],
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_broadcast_target_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_BROADCAST_TARGET_ALL'), callback_data='broadcast_all'),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_BROADCAST_TARGET_ACTIVE'), callback_data='broadcast_active'),
            ],
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_BROADCAST_TARGET_TRIAL'), callback_data='broadcast_trial'),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_BROADCAST_TARGET_NO_SUB'), callback_data='broadcast_no_sub'),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BROADCAST_TARGET_EXPIRING'),
                    callback_data='broadcast_expiring',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BROADCAST_TARGET_EXPIRED'), callback_data='broadcast_expired'
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BROADCAST_TARGET_ACTIVE_ZERO'),
                    callback_data='broadcast_active_zero',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BROADCAST_TARGET_TRIAL_ZERO'),
                    callback_data='broadcast_trial_zero',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BROADCAST_TARGET_BY_TARIFF'),
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
                InlineKeyboardButton(text=_t(texts, 'ADMIN_CRITERIA_TODAY'), callback_data='criteria_today'),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_CRITERIA_WEEK'), callback_data='criteria_week'),
            ],
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_CRITERIA_MONTH'), callback_data='criteria_month'),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CRITERIA_ACTIVE_TODAY'),
                    callback_data='criteria_active_today',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CRITERIA_INACTIVE_WEEK'),
                    callback_data='criteria_inactive_week',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CRITERIA_INACTIVE_MONTH'),
                    callback_data='criteria_inactive_month',
                ),
            ],
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_CRITERIA_REFERRALS'), callback_data='criteria_referrals'),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_CRITERIA_PROMOCODES'),
                    callback_data='criteria_promocodes',
                ),
            ],
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_CRITERIA_DIRECT'), callback_data='criteria_direct')],
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
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_HISTORY_REFRESH'), callback_data='admin_msg_history')],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_messages')],
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_sync_options_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = [
        [InlineKeyboardButton(text=_t(texts, 'ADMIN_SYNC_FULL'), callback_data='sync_all_users')],
        [InlineKeyboardButton(text=_t(texts, 'ADMIN_SYNC_TO_PANEL'), callback_data='sync_to_panel')],
        [InlineKeyboardButton(text=_t(texts, 'ADMIN_SYNC_ONLY_NEW'), callback_data='sync_new_users')],
        [InlineKeyboardButton(text=_t(texts, 'ADMIN_SYNC_UPDATE'), callback_data='sync_update_data')],
        [
            InlineKeyboardButton(text=_t(texts, 'ADMIN_SYNC_VALIDATE'), callback_data='sync_validate'),
            InlineKeyboardButton(text=_t(texts, 'ADMIN_SYNC_CLEANUP'), callback_data='sync_cleanup'),
        ],
        [InlineKeyboardButton(text=_t(texts, 'ADMIN_SYNC_RECOMMENDATIONS'), callback_data='sync_recommendations')],
        [InlineKeyboardButton(text=texts.BACK, callback_data='admin_remnawave')],
    ]

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_sync_confirmation_keyboard(sync_type: str, language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = [
        [InlineKeyboardButton(text=_t(texts, 'ADMIN_SYNC_CONFIRM'), callback_data=f'confirm_{sync_type}')],
        [InlineKeyboardButton(text=_t(texts, 'ADMIN_CANCEL'), callback_data='admin_rw_sync')],
    ]

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_sync_result_keyboard(sync_type: str, has_errors: bool = False, language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = []

    if has_errors:
        keyboard.append([InlineKeyboardButton(text=_t(texts, 'ADMIN_SYNC_RETRY'), callback_data=f'sync_{sync_type}')])

    if sync_type != 'all_users':
        keyboard.append([InlineKeyboardButton(text=_t(texts, 'ADMIN_SYNC_FULL'), callback_data='sync_all_users')])

    keyboard.extend(
        [
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_STATS_BUTTON'), callback_data='admin_rw_system'),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_SYNC_VALIDATE'), callback_data='sync_validate'),
            ],
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_SYNC_BACK'), callback_data='admin_rw_sync')],
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_BACK_TO_MAIN'), callback_data='admin_remnawave')],
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_period_selection_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_PERIOD_TODAY'), callback_data='period_today'),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_PERIOD_YESTERDAY'), callback_data='period_yesterday'),
            ],
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_PERIOD_WEEK'), callback_data='period_week'),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_PERIOD_MONTH'), callback_data='period_month'),
            ],
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_PERIOD_ALL'), callback_data='period_all')],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_statistics')],
        ]
    )


def get_node_management_keyboard(node_uuid: str, language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_NODE_ENABLE'), callback_data=f'node_enable_{node_uuid}'),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_NODE_DISABLE'), callback_data=f'node_disable_{node_uuid}'),
            ],
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_NODE_RESTART'), callback_data=f'node_restart_{node_uuid}'),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_NODE_STATS'), callback_data=f'node_stats_{node_uuid}'),
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
                    text=_t(texts, 'ADMIN_SQUAD_ADD_ALL'),
                    callback_data=f'squad_add_users_{squad_uuid}',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SQUAD_REMOVE_ALL'),
                    callback_data=f'squad_remove_users_{squad_uuid}',
                ),
            ],
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_SQUAD_EDIT'), callback_data=f'squad_edit_{squad_uuid}'),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_SQUAD_DELETE'), callback_data=f'squad_delete_{squad_uuid}'),
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
                    text=_t(texts, 'ADMIN_SQUAD_EDIT_INBOUNDS'),
                    callback_data=f'squad_edit_inbounds_{squad_uuid}',
                ),
            ],
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_SQUAD_RENAME'), callback_data=f'squad_rename_{squad_uuid}'),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BACK_TO_SQUADS'),
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
                InlineKeyboardButton(text=_t(texts, 'ADMIN_MONITORING_START'), callback_data='admin_mon_start'),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_MONITORING_STOP_HARD'), callback_data='admin_mon_stop'),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_FORCE_CHECK'),
                    callback_data='admin_mon_force_check',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_TRAFFIC_CHECK'),
                    callback_data='admin_mon_traffic_check',
                ),
            ],
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_MONITORING_LOGS'), callback_data='admin_mon_logs'),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_STATISTICS'), callback_data='admin_mon_statistics'
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_TEST_NOTIFICATIONS'),
                    callback_data='admin_mon_test_notifications',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_TRAFFIC_SETTINGS'),
                    callback_data='admin_mon_traffic_settings',
                ),
            ],
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_BACK_TO_ADMIN'), callback_data='admin_panel')],
        ]
    )


def get_monitoring_logs_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_HISTORY_REFRESH'), callback_data='admin_mon_logs'),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_CLEAR_OLD'),
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

    refresh_button = InlineKeyboardButton(text=_t(texts, 'ADMIN_HISTORY_REFRESH'), callback_data='admin_mon_logs')

    if has_logs:
        management_row.extend(
            [
                refresh_button,
                InlineKeyboardButton(text=_t(texts, 'ADMIN_MONITORING_CLEAR'), callback_data='admin_mon_clear_logs'),
            ]
        )
    else:
        management_row.append(refresh_button)

    keyboard.append(management_row)

    keyboard.append(
        [InlineKeyboardButton(text=_t(texts, 'ADMIN_BACK_TO_MONITORING'), callback_data='admin_monitoring')]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_log_detail_keyboard(log_id: int, current_page: int = 1, language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_DELETE_LOG'),
                    callback_data=f'admin_mon_delete_log_{log_id}',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_BACK_TO_LOGS'),
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
                    text=_t(texts, 'ADMIN_MONITORING_CONFIRM_CLEAR'),
                    callback_data='admin_mon_clear_logs_confirm',
                ),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_CANCEL'), callback_data='admin_mon_logs'),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_CLEAR_ALL'),
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
                InlineKeyboardButton(text=_t(texts, 'ADMIN_MONITORING_STOP_HARD'), callback_data='admin_mon_stop'),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_MONITORING_RESTART'), callback_data='admin_mon_restart'),
            ]
        )
    else:
        control_row.append(
            InlineKeyboardButton(text=_t(texts, 'ADMIN_MONITORING_START'), callback_data='admin_mon_start')
        )

    keyboard.append(control_row)

    monitoring_row = []

    if not is_running or last_check_ago_minutes > 10:
        monitoring_row.append(
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_MONITORING_FORCE_CHECK'),
                callback_data='admin_mon_force_check',
            )
        )
    else:
        monitoring_row.append(
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_MONITORING_CHECK_NOW'),
                callback_data='admin_mon_force_check',
            )
        )

    keyboard.append(monitoring_row)

    info_row = [
        InlineKeyboardButton(text=_t(texts, 'ADMIN_MONITORING_LOGS'), callback_data='admin_mon_logs'),
        InlineKeyboardButton(text=_t(texts, 'ADMIN_MONITORING_STATISTICS'), callback_data='admin_mon_statistics'),
    ]
    keyboard.append(info_row)

    test_row = [
        InlineKeyboardButton(
            text=_t(texts, 'ADMIN_MONITORING_TEST_NOTIFICATIONS'),
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
                    text=_t(texts, 'ADMIN_MONITORING_SET_INTERVAL'),
                    callback_data='admin_mon_set_interval',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_NOTIFICATIONS'),
                    callback_data='admin_mon_toggle_notifications',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_AUTOPAY_SETTINGS'),
                    callback_data='admin_mon_autopay_settings',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_AUTO_CLEANUP'),
                    callback_data='admin_mon_auto_cleanup',
                ),
            ],
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_BACK_TO_MONITORING'), callback_data='admin_monitoring')],
        ]
    )


def get_log_type_filter_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_FILTER_SUCCESS'),
                    callback_data='admin_mon_logs_filter_success',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_FILTER_ERRORS'),
                    callback_data='admin_mon_logs_filter_error',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_FILTER_CYCLES'),
                    callback_data='admin_mon_logs_filter_cycle',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MONITORING_FILTER_AUTOPAY'),
                    callback_data='admin_mon_logs_filter_autopay',
                ),
            ],
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_MONITORING_ALL_LOGS'), callback_data='admin_mon_logs'),
                InlineKeyboardButton(text=texts.BACK, callback_data='admin_monitoring'),
            ],
        ]
    )


def get_admin_servers_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_SERVERS_LIST'), callback_data='admin_servers_list'),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_SERVERS_SYNC'), callback_data='admin_servers_sync'),
            ],
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_SERVERS_ADD'), callback_data='admin_servers_add'),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_SERVERS_STATS'), callback_data='admin_servers_stats'),
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_subscriptions')],
        ]
    )


def get_server_edit_keyboard(server_id: int, is_available: bool, language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)

    toggle_text = _t(texts, 'ADMIN_SERVER_DISABLE') if is_available else _t(texts, 'ADMIN_SERVER_ENABLE')

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SERVER_EDIT_NAME'),
                    callback_data=f'admin_server_edit_name_{server_id}',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SERVER_EDIT_PRICE'),
                    callback_data=f'admin_server_edit_price_{server_id}',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SERVER_EDIT_COUNTRY'),
                    callback_data=f'admin_server_edit_country_{server_id}',
                ),
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SERVER_EDIT_LIMIT'),
                    callback_data=f'admin_server_edit_limit_{server_id}',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SERVER_EDIT_DESCRIPTION'),
                    callback_data=f'admin_server_edit_desc_{server_id}',
                )
            ],
            [InlineKeyboardButton(text=toggle_text, callback_data=f'admin_server_toggle_{server_id}')],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_SERVER_DELETE'), callback_data=f'admin_server_delete_{server_id}'
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
                    text=_t(texts, 'ADMIN_MAINTENANCE_DISABLE'),
                    callback_data='maintenance_toggle',
                )
            ]
        )
    else:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MAINTENANCE_ENABLE'),
                    callback_data='maintenance_toggle',
                )
            ]
        )

    if is_monitoring_active:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MAINTENANCE_STOP_MONITORING'),
                    callback_data='maintenance_monitoring',
                )
            ]
        )
    else:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_MAINTENANCE_START_MONITORING'),
                    callback_data='maintenance_monitoring',
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton(text=_t(texts, 'ADMIN_MAINTENANCE_CHECK_API'), callback_data='maintenance_check_api'),
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_MAINTENANCE_PANEL_STATUS') + ('⚠️' if panel_has_issues else ''),
                callback_data='maintenance_check_panel',
            ),
        ]
    )

    keyboard.append(
        [
            InlineKeyboardButton(
                text=_t(texts, 'ADMIN_MAINTENANCE_SEND_NOTIFICATION'),
                callback_data='maintenance_manual_notify',
            )
        ]
    )

    keyboard.append(
        [
            InlineKeyboardButton(text=_t(texts, 'ADMIN_REFRESH'), callback_data='maintenance_panel'),
            InlineKeyboardButton(text=texts.BACK, callback_data='admin_submenu_settings'),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_sync_simplified_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = [
        [InlineKeyboardButton(text=_t(texts, 'ADMIN_SYNC_FULL'), callback_data='sync_all_users')],
        [InlineKeyboardButton(text=texts.BACK, callback_data='admin_remnawave')],
    ]

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_welcome_text_keyboard(language: str = 'ru', is_enabled: bool = True) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    toggle_text = _t(texts, 'ADMIN_WELCOME_DISABLE') if is_enabled else _t(texts, 'ADMIN_WELCOME_ENABLE')
    toggle_callback = 'toggle_welcome_text'

    keyboard = [
        [InlineKeyboardButton(text=toggle_text, callback_data=toggle_callback)],
        [
            InlineKeyboardButton(text=_t(texts, 'ADMIN_WELCOME_EDIT'), callback_data='edit_welcome_text'),
            InlineKeyboardButton(text=_t(texts, 'ADMIN_WELCOME_SHOW'), callback_data='show_welcome_text'),
        ],
        [
            InlineKeyboardButton(text=_t(texts, 'ADMIN_WELCOME_PREVIEW'), callback_data='preview_welcome_text'),
            InlineKeyboardButton(text=_t(texts, 'ADMIN_WELCOME_RESET'), callback_data='reset_welcome_text'),
        ],
        [
            InlineKeyboardButton(text=_t(texts, 'ADMIN_WELCOME_HTML'), callback_data='show_formatting_help'),
            InlineKeyboardButton(text=_t(texts, 'ADMIN_WELCOME_PLACEHOLDERS'), callback_data='show_placeholders_help'),
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
                InlineKeyboardButton(text=_t(texts, 'ADMIN_BROADCAST_ADD_PHOTO'), callback_data='add_media_photo'),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_BROADCAST_ADD_VIDEO'), callback_data='add_media_video'),
            ],
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BROADCAST_ADD_DOCUMENT'),
                    callback_data='add_media_document',
                ),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_BROADCAST_SKIP_MEDIA'), callback_data='skip_media'),
            ],
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_CANCEL'), callback_data='admin_messages')],
        ]
    )


def get_media_confirm_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(texts, 'ADMIN_BROADCAST_USE_MEDIA'),
                    callback_data='confirm_media',
                ),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_BROADCAST_REPLACE_MEDIA'), callback_data='replace_media'),
            ],
            [
                InlineKeyboardButton(text=_t(texts, 'ADMIN_BROADCAST_NO_MEDIA'), callback_data='skip_media'),
                InlineKeyboardButton(text=_t(texts, 'ADMIN_CANCEL'), callback_data='admin_messages'),
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
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_BROADCAST_CHANGE_MEDIA'), callback_data='change_media')]
        )

    keyboard.extend(
        [
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_CONTINUE'), callback_data='buttons_confirm')],
            [InlineKeyboardButton(text=_t(texts, 'ADMIN_CANCEL'), callback_data='admin_messages')],
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)
