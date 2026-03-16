# База по структуре проекта

Этот документ сгенерирован для быстрой навигации по репозиторию. В нём перечислены основные директории, модули, классы и функции.

## Общая структура корня
- `.dockerignore` — файл
- `.env` — файл
- `.env.example` — файл
- `.gitignore` — файл
- `CONTRIBUTING.md` — файл
- `Dockerfile` — файл
- `LICENSE` — файл
- `README.md` — файл
- `SECURITY.md` — файл
- `__pycache__/` — директория
- `alembic.ini` — файл
- `app/` — директория
- `app-config.json` — файл
- `assets/` — директория
- `data/` — директория
- `docker-compose.local.yml` — файл
- `docker-compose.yml` — файл
- `docs/` — директория
- `install_bot.sh` — файл
- `locales/` — директория
- `logs/` — директория
- `main.py` — файл
- `migrations/` — директория
- `miniapp/` — директория
- `requirements.txt` — файл
- `tests/` — директория
- `venv/` — директория
- `vpn_logo.png` — файл

## app

- `app/bot.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/config.py` — Python-модуль
  Классы: `Settings` (103 методов)
  Функции: `refresh_period_prices` — Rebuild cached period price mapping using the latest settings., `get_traffic_prices`, `refresh_traffic_prices`
- `app/database/`
- `app/external/`
- `app/handlers/`
- `app/keyboards/`
- `app/localization/`
- `app/middlewares/`
- `app/services/`
- `app/states.py` — Python-модуль
  Классы: `RegistrationStates`, `SubscriptionStates`, `BalanceStates`, `PromoCodeStates`, `AdminStates`, `SupportStates`, `TicketStates`, `AdminTicketStates`, `SupportSettingsStates`, `BotConfigStates`, `PricingStates`, `AutoPayStates`, `SquadCreateStates`, `SquadRenameStates`, `SquadMigrationStates`, `AdminSubmenuStates`
  Функции: нет
- `app/utils/`
- `app/webapi/`

### app/database

- `app/database/crud/`
- `app/database/database.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/database/models.py` — Python-модуль
  Классы: `UserStatus`, `SubscriptionStatus`, `TransactionType`, `PromoCodeType`, `PaymentMethod`, `MainMenuButtonActionType`, `MainMenuButtonVisibility`, `YooKassaPayment` (6 методов), `CryptoBotPayment` (5 методов), `MulenPayPayment` (2 методов), `Pal24Payment` (3 методов), `PromoGroup` (3 методов), `User` (5 методов), `Subscription` (11 методов), `Transaction` (1 методов), `SubscriptionConversion` (2 методов), `PromoCode` (2 методов), `PromoCodeUse`, `ReferralEarning` (1 методов), `Squad` (1 методов), `ServiceRule`, `PrivacyPolicy`, `PublicOffer`, `FaqSetting`, `FaqPage`, `SystemSetting`, `MonitoringLog`, `SentNotification`, `DiscountOffer`, `PromoOfferTemplate`, `SubscriptionTemporaryAccess`, `PromoOfferLog`, `BroadcastHistory`, `ServerSquad` (3 методов), `SubscriptionServer`, `SupportAuditLog`, `UserMessage` (1 методов), `WelcomeText`, `AdvertisingCampaign` (2 методов), `AdvertisingCampaignRegistration` (1 методов), `TicketStatus`, `Ticket` (8 методов), `TicketMessage` (3 методов), `WebApiToken` (1 методов), `MainMenuButton` (3 методов)
  Функции: нет
- `app/database/migrations.py` — Programmatic Alembic migration runner
  Классы: нет
  Функции: `run_alembic_upgrade`, `stamp_alembic_head`

#### app/database/crud

- `app/database/crud/campaign.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/database/crud/cryptobot.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/database/crud/heleket.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/database/crud/discount_offer.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/database/crud/faq.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/database/crud/main_menu_button.py` — Python-модуль
  Классы: нет
  Функции: `_enum_value`
- `app/database/crud/mulenpay.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/database/crud/notification.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/database/crud/pal24.py` — CRUD helpers for PayPalych (Pal24) payments.
  Классы: нет
  Функции: нет
- `app/database/crud/privacy_policy.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/database/crud/promo_group.py` — Python-модуль
  Классы: нет
  Функции: `_normalize_period_discounts`
- `app/database/crud/promo_offer_log.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/database/crud/promo_offer_template.py` — Python-модуль
  Классы: нет
  Функции: `_format_template_fields`
- `app/database/crud/promocode.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/database/crud/public_offer.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/database/crud/referral.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/database/crud/rules.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/database/crud/server_squad.py` — Python-модуль
  Классы: нет
  Функции: `_generate_display_name`, `_extract_country_code`
- `app/database/crud/squad.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/database/crud/subscription.py` — Python-модуль
  Классы: нет
  Функции: нет (ранее `_get_discount_percent` — удалена при консолидации в PricingEngine; см. `PricingEngine.resolve_promo_group()` и `PromoGroup.get_discount_percent()`)
- `app/database/crud/subscription_conversion.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/database/crud/system_setting.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/database/crud/ticket.py` — Python-модуль
  Классы: `TicketCRUD` — CRUD операции для работы с тикетами, `TicketMessageCRUD` — CRUD операции для работы с сообщениями тикетов
  Функции: нет
- `app/database/crud/transaction.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/database/crud/user.py` — Python-модуль
  Классы: нет
  Функции: `generate_referral_code`
- `app/database/crud/user_message.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/database/crud/web_api_token.py` — CRUD операции для токенов административного веб-API.
  Классы: нет
  Функции: нет
- `app/database/crud/welcome_text.py` — Python-модуль
  Классы: нет
  Функции: `replace_placeholders`, `get_available_placeholders`
- `app/database/crud/yookassa.py` — Python-модуль
  Классы: нет
  Функции: нет

### app/external

- `app/external/cryptobot.py` — Python-модуль
  Классы: `CryptoBotService` (2 методов)
  Функции: нет
- `app/external/heleket.py` — Python-модуль
  Классы: `HeleketService` (3 методов)
  Функции: нет
- `app/external/heleket_webhook.py` — Python-модуль
  Классы: `HeleketWebhookHandler` (3 методов)
  Функции: `create_heleket_app`, `start_heleket_webhook_server`
- `app/external/pal24_client.py` — Async client for PayPalych (Pal24) API.
  Классы: `Pal24APIError` — Base error for Pal24 API operations., `Pal24Response` (2 методов) — Wrapper for Pal24 API responses., `Pal24Client` (5 методов) — Async client implementing PayPalych API methods.
  Функции: нет
- `app/external/remnawave_api.py` — Python-модуль
  Классы: `UserStatus`, `TrafficLimitStrategy`, `RemnaWaveUser`, `RemnaWaveInternalSquad`, `RemnaWaveNode`, `SubscriptionInfo`, `RemnaWaveAPIError` (1 методов), `RemnaWaveAPI` (8 методов)
  Функции: `format_bytes`, `parse_bytes`
- `app/external/telegram_stars.py` — Python-модуль
  Классы: `TelegramStarsService` (3 методов)
  Функции: нет
- `app/external/tribute.py` — Python-модуль
  Классы: `TributeService` (2 методов)
  Функции: нет
- `app/external/webhook_server.py` — Python-модуль
  Классы: `WebhookServer` (3 методов)
  Функции: нет
- `app/external/yookassa_webhook.py` — Python-модуль
  Классы: `YooKassaWebhookHandler` (3 методов)
  Функции: `create_yookassa_webhook_app`

### app/handlers

- `app/handlers/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/handlers/admin/`
- `app/handlers/balance.py` — Python-модуль
  Классы: нет
  Функции: `get_quick_amount_buttons`, `register_handlers`
- `app/handlers/common.py` — Python-модуль
  Классы: нет
  Функции: `register_handlers`
- `app/handlers/menu.py` — Python-модуль
  Классы: нет
  Функции: `_format_rubles`, `_collect_period_discounts`, `_build_group_discount_lines`, `_get_subscription_status`, `_insert_random_message`, `register_handlers`
- `app/handlers/promocode.py` — Python-модуль
  Классы: нет
  Функции: `register_handlers`
- `app/handlers/referral.py` — Python-модуль
  Классы: нет
  Функции: `register_handlers`
- `app/handlers/server_status.py` — Python-модуль
  Классы: нет
  Функции: `_build_status_message`, `_split_into_pages`, `_format_server_lines`, `register_handlers`
- `app/handlers/stars_payments.py` — Python-модуль
  Классы: нет
  Функции: `register_stars_handlers`
- `app/handlers/start.py` — Python-модуль
  Классы: нет
  Функции: `_get_language_prompt_text`, `_get_subscription_status`, `_get_subscription_status_simple`, `_insert_random_message`, `get_referral_code_keyboard`, `register_handlers`
- `app/handlers/subscription/` — пакет обработчиков подписки
  Ключевые модули:
    - `common.py` — вспомогательные функции форматирования, расчётов и построения клавиатур.
    - `purchase.py` — пользовательские сценарии, регистрация обработчиков (`register_handlers`).
    - `countries.py`, `devices.py`, `traffic.py`, `autopay.py`, `promo.py`, `happ.py`, `links.py`, `notifications.py`, `pricing.py` — тематические обработчики и сервисные утилиты.
  Публичные функции доступны через `app.handlers.subscription` (например, `create_deep_link`, `get_servers_display_names`, `register_handlers`).
- `app/handlers/support.py` — Python-модуль
  Классы: нет
  Функции: `register_handlers`
- `app/handlers/tickets.py` — Python-модуль
  Классы: `TicketStates`
  Функции: `_split_text_into_pages`, `register_handlers` — Регистрация обработчиков тикетов
- `app/handlers/webhooks.py` — Python-модуль
  Классы: нет
  Функции: нет

#### app/handlers/admin

- `app/handlers/admin/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/handlers/admin/backup.py` — Python-модуль
  Классы: `BackupStates`
  Функции: `get_backup_main_keyboard`, `get_backup_list_keyboard`, `get_backup_manage_keyboard`, `get_backup_settings_keyboard`, `register_handlers`
- `app/handlers/admin/bot_configuration.py` — Python-модуль
  Классы: `BotConfigInputFilter` (1 методов)
  Функции: `_get_group_meta`, `_get_group_description`, `_get_group_icon`, `_get_group_status`, `_get_setting_icon`, `_render_dashboard_overview`, `_build_group_category_index`, `_perform_settings_search`, `_build_search_results_keyboard`, `_parse_env_content`, `_format_preset_preview`, `_chunk`, `_parse_category_payload`, `_parse_group_payload`, `_get_grouped_categories`, `_build_groups_keyboard`, `_build_categories_keyboard`, `_build_settings_keyboard`, `_build_setting_keyboard`, `_render_setting_text`, `register_handlers`
- `app/handlers/admin/campaigns.py` — Python-модуль
  Классы: нет
  Функции: `_format_campaign_summary`, `_build_campaign_servers_keyboard`, `register_handlers`
- `app/handlers/admin/faq.py` — Python-модуль
  Классы: нет
  Функции: `_format_timestamp`, `register_handlers`
- `app/handlers/admin/main.py` — Python-модуль
  Классы: нет
  Функции: `register_handlers`
- `app/handlers/admin/maintenance.py` — Python-модуль
  Классы: `MaintenanceStates`
  Функции: `register_handlers`
- `app/handlers/admin/messages.py` — Python-модуль
  Классы: нет
  Функции: `get_message_buttons_selector_keyboard`, `get_updated_message_buttons_selector_keyboard`, `create_broadcast_keyboard`, `get_target_name`, `get_target_display_name`, `register_handlers`
- `app/handlers/admin/monitoring.py` — Python-модуль
  Классы: нет
  Функции: `_format_toggle`, `_build_notification_settings_view`, `_build_notification_preview_message`, `get_monitoring_logs_keyboard`, `get_monitoring_logs_back_keyboard`, `register_handlers`
- `app/handlers/admin/pricing.py` — Python-модуль
  Классы: `ChoiceOption` (1 методов), `SettingEntry` (2 методов)
  Функции: `_traffic_package_sort_key`, `_collect_traffic_packages`, `_serialize_traffic_packages`, `_language_code`, `_format_period_label`, `_format_traffic_label`, `_format_trial_summary`, `_format_core_summary`, `_get_period_items`, `_get_traffic_items`, `_get_extra_items`, `_build_period_summary`, `_build_traffic_summary`, `_build_period_options_summary`, `_build_extra_summary`, `_build_settings_section`, `_build_traffic_options_section`, `_build_period_options_section`, `_build_overview`, `_build_section`, `_build_price_prompt`, `_parse_price_input`, `_resolve_label`, `register_handlers`
- `app/handlers/admin/privacy_policy.py` — Python-модуль
  Классы: нет
  Функции: `_format_timestamp`, `register_handlers`
- `app/handlers/admin/promo_groups.py` — Python-модуль
  Классы: нет
  Функции: `_format_discount_lines`, `_format_addon_discounts_line`, `_get_addon_discounts_button_text`, `_normalize_periods_dict`, `_collect_period_discounts`, `_format_period_discounts_lines`, `_format_period_discounts_value`, `_parse_period_discounts_input`, `_format_rubles`, `_format_auto_assign_line`, `_format_auto_assign_value`, `_parse_auto_assign_threshold_input`, `_build_edit_menu_content`, `_get_edit_prompt_keyboard`, `_validate_percent`, `register_handlers`
- `app/handlers/admin/promo_offers.py` — Python-модуль
  Классы: нет
  Функции: `_render_template_text`, `_build_templates_keyboard`, `_build_offer_detail_keyboard`, `_format_offer_remaining`, `_extract_offer_active_hours`, `_extract_template_id_from_notification`, `_format_promo_offer_log_entry`, `_build_logs_keyboard`, `_build_send_keyboard`, `_build_user_button_label`, `_describe_offer`, `_build_connect_button_rows`, `register_handlers`
- `app/handlers/admin/promocodes.py` — Python-модуль
  Классы: нет
  Функции: `register_handlers`
- `app/handlers/admin/public_offer.py` — Python-модуль
  Классы: нет
  Функции: `_format_timestamp`, `register_handlers`
- `app/handlers/admin/referrals.py` — Python-модуль
  Классы: нет
  Функции: `register_handlers`
- `app/handlers/admin/remnawave.py` — Python-модуль
  Классы: нет
  Функции: `_format_migration_server_label`, `_build_migration_keyboard`, `register_handlers`
- `app/handlers/admin/reports.py` — Python-модуль
  Классы: нет
  Функции: `register_handlers`
- `app/handlers/admin/rules.py` — Python-модуль
  Классы: нет
  Функции: `register_handlers`
- `app/handlers/admin/servers.py` — Python-модуль
  Классы: нет
  Функции: `_build_server_edit_view`, `_build_server_promo_groups_keyboard`, `register_handlers`
- `app/handlers/admin/statistics.py` — Python-модуль
  Классы: нет
  Функции: `register_handlers`
- `app/handlers/admin/subscriptions.py` — Python-модуль
  Классы: нет
  Функции: `get_country_flag`, `register_handlers`
- `app/handlers/admin/support_settings.py` — Python-модуль
  Классы: `SupportAdvancedStates`
  Функции: `_get_support_settings_keyboard`, `register_handlers`
- `app/handlers/admin/system_logs.py` — Python-модуль
  Классы: нет
  Функции: `_resolve_log_path`, `_format_preview_block`, `_build_logs_message`, `_get_logs_keyboard`, `register_handlers`
- `app/handlers/admin/tickets.py` — Python-модуль
  Классы: нет
  Функции: `register_handlers` — Регистрация админских обработчиков тикетов
- `app/handlers/admin/updates.py` — Python-модуль
  Классы: нет
  Функции: `get_updates_keyboard`, `get_version_info_keyboard`, `register_handlers`
- `app/handlers/admin/user_messages.py` — Python-модуль
  Классы: `UserMessageStates`
  Функции: `get_user_messages_keyboard`, `get_message_actions_keyboard`, `register_handlers`
- `app/handlers/admin/users.py` — Python-модуль
  Классы: нет
  Функции: `register_handlers`
- `app/handlers/admin/welcome_text.py` — Python-модуль
  Классы: нет
  Функции: `get_telegram_formatting_info`, `register_welcome_text_handlers`

### app/keyboards

- `app/keyboards/admin.py` — Python-модуль
  Классы: нет
  Функции: `_t` — Helper for localized button labels with fallbacks., `get_admin_main_keyboard`, `get_admin_users_submenu_keyboard`, `get_admin_promo_submenu_keyboard`, `get_admin_communications_submenu_keyboard`, `get_admin_support_submenu_keyboard`, `get_admin_settings_submenu_keyboard`, `get_admin_system_submenu_keyboard`, `get_admin_reports_keyboard`, `get_admin_report_result_keyboard`, `get_admin_users_keyboard`, `get_admin_users_filters_keyboard`, `get_admin_subscriptions_keyboard`, `get_admin_promocodes_keyboard`, `get_admin_campaigns_keyboard`, `get_campaign_management_keyboard`, `get_campaign_edit_keyboard`, `get_campaign_bonus_type_keyboard`, `get_promocode_management_keyboard`, `get_admin_messages_keyboard`, `get_admin_monitoring_keyboard`, `get_admin_remnawave_keyboard`, `get_admin_statistics_keyboard`, `get_user_management_keyboard`, `get_user_promo_group_keyboard`, `get_confirmation_keyboard`, `get_promocode_type_keyboard`, `get_promocode_list_keyboard`, `get_broadcast_target_keyboard`, `get_custom_criteria_keyboard`, `get_broadcast_history_keyboard`, `get_sync_options_keyboard`, `get_sync_confirmation_keyboard`, `get_sync_result_keyboard`, `get_period_selection_keyboard`, `get_node_management_keyboard`, `get_squad_management_keyboard`, `get_squad_edit_keyboard`, `get_monitoring_keyboard`, `get_monitoring_logs_keyboard`, `get_monitoring_logs_navigation_keyboard`, `get_log_detail_keyboard`, `get_monitoring_clear_confirm_keyboard`, `get_monitoring_status_keyboard`, `get_monitoring_settings_keyboard`, `get_log_type_filter_keyboard`, `get_admin_servers_keyboard`, `get_server_edit_keyboard`, `get_admin_pagination_keyboard`, `get_maintenance_keyboard`, `get_sync_simplified_keyboard`, `get_welcome_text_keyboard`, `get_broadcast_button_config`, `get_broadcast_button_labels`, `get_message_buttons_selector_keyboard`, `get_broadcast_media_keyboard`, `get_media_confirm_keyboard`, `get_updated_message_buttons_selector_keyboard_with_media`
- `app/keyboards/inline.py` — Python-модуль
  Классы: нет
  Функции: `_get_localized_value`, `_build_additional_buttons`, `get_rules_keyboard`, `get_channel_sub_keyboard`, `get_post_registration_keyboard`, `get_language_selection_keyboard`, `_build_cabinet_main_menu_keyboard`, `get_main_menu_keyboard`, `get_info_menu_keyboard`, `get_happ_download_button_row`, `get_happ_cryptolink_keyboard`, `get_happ_download_platform_keyboard`, `get_happ_download_link_keyboard`, `get_back_keyboard`, `get_server_status_keyboard`, `get_insufficient_balance_keyboard`, `get_subscription_keyboard`, `get_payment_methods_keyboard_with_cart`, `get_subscription_confirm_keyboard_with_cart`, `get_insufficient_balance_keyboard_with_cart`, `get_trial_keyboard`, `get_subscription_period_keyboard`, `get_traffic_packages_keyboard`, `get_countries_keyboard`, `get_devices_keyboard`, `_get_device_declension`, `get_subscription_confirm_keyboard`, `get_balance_keyboard`, `get_payment_methods_keyboard`, `get_yookassa_payment_keyboard`, `get_autopay_notification_keyboard`, `get_subscription_expiring_keyboard`, `get_referral_keyboard`, `get_support_keyboard`, `get_pagination_keyboard`, `get_confirmation_keyboard`, `get_autopay_keyboard`, `get_autopay_days_keyboard`, `_get_days_word`, `get_extend_subscription_keyboard`, `get_add_traffic_keyboard`, `get_change_devices_keyboard`, `get_confirm_change_devices_keyboard`, `get_reset_traffic_confirm_keyboard`, `get_manage_countries_keyboard`, `get_device_selection_keyboard`, `get_connection_guide_keyboard`, `get_app_selection_keyboard`, `get_specific_app_keyboard`, `get_extend_subscription_keyboard_with_prices`, `get_cryptobot_payment_keyboard`, `get_devices_management_keyboard`, `get_updated_subscription_settings_keyboard`, `get_device_reset_confirm_keyboard`, `get_device_management_help_keyboard`, `get_ticket_cancel_keyboard`, `get_my_tickets_keyboard`, `get_ticket_view_keyboard`, `get_ticket_reply_cancel_keyboard`, `get_admin_tickets_keyboard`, `get_admin_ticket_view_keyboard`, `get_admin_ticket_reply_cancel_keyboard`
- `app/keyboards/reply.py` — Python-модуль
  Классы: нет
  Функции: `get_main_reply_keyboard`, `get_admin_reply_keyboard`, `get_cancel_keyboard`, `get_confirmation_reply_keyboard`, `get_skip_keyboard`, `remove_keyboard`, `get_contact_keyboard`, `get_location_keyboard`

### app/localization

- `app/localization/default_locales/`
- `app/localization/loader.py` — Python-модуль
  Классы: нет
  Функции: `_normalize_language_code`, `_resolve_user_locales_dir`, `_locale_file_exists`, `_select_fallback_language`, `_determine_default_language`, `_normalize_key`, `_flatten_locale_dict`, `_normalize_locale_dict`, `ensure_locale_templates`, `_load_default_locale`, `_load_user_locale`, `_load_locale_file`, `_merge_dicts`, `load_locale`, `clear_locale_cache`
- `app/localization/locales/`
- `app/localization/texts.py` — Python-модуль
  Классы: `Texts` (8 методов)
  Функции: `_get_cached_rules_value`, `_build_dynamic_values`, `get_texts`, `_get_default_rules`, `get_rules_sync`, `clear_rules_cache`, `reload_locales`

#### app/localization/default_locales

- `app/localization/default_locales/en.yml` — файл (.yml)
- `app/localization/default_locales/ru.yml` — файл (.yml)

#### app/localization/locales

- `app/localization/locales/en.json` — файл (.json)
- `app/localization/locales/ru.json` — файл (.json)

### app/middlewares

- `app/middlewares/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/middlewares/auth.py` — Python-модуль
  Классы: `AuthMiddleware`
  Функции: нет
- `app/middlewares/channel_checker.py` — Python-модуль
  Классы: `ChannelCheckerMiddleware` (1 методов)
  Функции: нет
- `app/middlewares/display_name_restriction.py` — Python-модуль
  Классы: `DisplayNameRestrictionMiddleware` (4 методов) — Blocks users whose display name imitates links or official accounts.
  Функции: нет
- `app/middlewares/global_error.py` — Python-модуль
  Классы: `GlobalErrorMiddleware` (4 методов), `ErrorStatisticsMiddleware` (4 методов)
  Функции: нет
- `app/middlewares/logging.py` — Python-модуль
  Классы: `LoggingMiddleware`
  Функции: нет
- `app/middlewares/maintenance.py` — Python-модуль
  Классы: `MaintenanceMiddleware`
  Функции: нет
- `app/middlewares/subscription_checker.py` — Python-модуль
  Классы: `SubscriptionStatusMiddleware`
  Функции: нет
- `app/middlewares/throttling.py` — Python-модуль
  Классы: `ThrottlingMiddleware` (1 методов)
  Функции: нет

### app/services

- `app/services/__init__.py` — Сервисы бизнес-логики
  Классы: нет
  Функции: нет
- `app/services/admin_notification_service.py` — Python-модуль
  Классы: `AdminNotificationService` (11 методов)
  Функции: нет
- `app/services/backup_service.py` — Python-модуль
  Классы: `BackupMetadata`, `BackupSettings`, `BackupService` (7 методов)
  Функции: нет
- `app/services/broadcast_service.py` — Python-модуль
  Классы: `BroadcastMediaConfig`, `BroadcastConfig`, `_BroadcastTask`, `BroadcastService` (4 методов) — Handles broadcast execution triggered from the admin web API.
  Функции: нет
- `app/services/campaign_service.py` — Python-модуль
  Классы: `CampaignBonusResult`, `AdvertisingCampaignService` (1 методов)
  Функции: нет
- `app/services/external_admin_service.py` — Утилиты для синхронизации токена внешней админки.
  Классы: нет
  Функции: нет
- `app/services/faq_service.py` — Python-модуль
  Классы: `FaqService` (3 методов)
  Функции: нет
- `app/services/main_menu_button_service.py` — Python-модуль
  Классы: `_MainMenuButtonData`, `MainMenuButtonService` (2 методов)
  Функции: нет
- `app/services/maintenance_service.py` — Python-модуль
  Классы: `MaintenanceStatus`, `MaintenanceService` (6 методов)
  Функции: нет
- `app/services/monitoring_service.py` — Python-модуль
  Классы: `MonitoringService` (5 методов)
  Функции: нет
- `app/services/mulenpay_service.py` — Python-модуль
  Классы: `MulenPayService` (4 методов) — Интеграция с Mulen Pay API.
  Функции: нет
- `app/services/notification_settings_service.py` — Python-модуль
  Классы: `NotificationSettingsService` (32 методов) — Runtime-editable notification settings stored on disk.
  Функции: нет
- `app/services/pal24_service.py` — High level integration with PayPalych API.
  Классы: `Pal24Service` (5 методов) — Wrapper around :class:`Pal24Client` providing domain helpers.
  Функции: нет
- `app/services/payment_service.py` — Python-модуль
  Классы: `PaymentService` (3 методов)
  Функции: нет
- `app/services/privacy_policy_service.py` — Python-модуль
  Классы: `PrivacyPolicyService` (3 методов) — Utility helpers around privacy policy storage and presentation.
  Функции: нет
- `app/services/promo_group_assignment.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/services/promo_offer_service.py` — Python-модуль
  Классы: `PromoOfferService` (1 методов)
  Функции: нет
- `app/services/promocode_service.py` — Python-модуль
  Классы: `PromoCodeService` (1 методов)
  Функции: нет
- `app/services/public_offer_service.py` — Python-модуль
  Классы: `PublicOfferService` (4 методов) — Helpers for managing the public offer text and visibility.
  Функции: нет
- `app/services/referral_service.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/services/remnawave_service.py` — Python-модуль
  Классы: `RemnaWaveConfigurationError` — Raised when RemnaWave API configuration is missing., `RemnaWaveService` (7 методов)
  Функции: нет
- `app/services/reporting_service.py` — Python-модуль
  Классы: `ReportingServiceError` — Base error for the reporting service., `ReportPeriod`, `ReportPeriodRange`, `ReportingService` (7 методов) — Generates admin summary reports and can schedule daily delivery.
  Функции: нет
- `app/services/server_status_service.py` — Python-модуль
  Классы: `ServerStatusEntry`, `ServerStatusError` — Raised when server status information cannot be fetched or parsed., `ServerStatusService` (6 методов)
  Функции: нет
- `app/services/subscription_checkout_service.py` — Python-модуль
  Классы: нет
  Функции: `should_offer_checkout_resume` — Determine whether checkout resume button should be available for the user.
- `app/services/subscription_purchase_service.py` — Python-модуль
  Классы: `PurchaseTrafficOption` (1 методов), `PurchaseTrafficConfig` (1 методов), `PurchaseServerOption` (1 методов), `PurchaseServersConfig` (1 методов), `PurchaseDevicesConfig` (1 методов), `PurchasePeriodConfig` (1 методов), `PurchaseSelection`, `PurchasePricingResult`, `PurchaseOptionsContext`, `PurchaseValidationError` (1 методов), `PurchaseBalanceError` (1 методов), `MiniAppSubscriptionPurchaseService` (5 методов) — Builds configuration and pricing for subscription purchases in the mini app.
  Функции: `_apply_percentage_discount`, `_apply_discount_to_monthly_component`, `_get_promo_offer_discount_percent`, `_apply_promo_offer_discount`, `_build_server_option`
- `app/services/subscription_service.py` — Python-модуль
  Классы: `SubscriptionService` (7 методов)
  Функции: `_resolve_discount_percent`, `_resolve_addon_discount_percent`, `get_traffic_reset_strategy`
- `app/services/support_settings_service.py` — Python-модуль
  Классы: `SupportSettingsService` (23 методов) — Runtime editable support settings with JSON persistence.
  Функции: нет
- `app/services/system_settings_service.py` — Python-модуль
  Классы: `SettingDefinition` (1 методов), `ChoiceOption`, `ReadOnlySettingError` — Исключение, выбрасываемое при попытке изменить настройку только для чтения., `BotConfigurationService` (33 методов)
  Функции: `_title_from_key`, `_truncate`
- `app/services/tribute_service.py` — Python-модуль
  Классы: `TributeService` (1 методов)
  Функции: нет
- `app/services/user_service.py` — Python-модуль
  Классы: `UserService`
  Функции: нет
- `app/services/version_service.py` — Python-модуль
  Классы: `VersionInfo` (5 методов), `VersionService` (5 методов)
  Функции: нет
- `app/services/web_api_token_service.py` — Python-модуль
  Классы: `WebApiTokenService` (2 методов) — Сервис для управления токенами административного веб-API.
  Функции: нет
- `app/services/yookassa_service.py` — Python-модуль
  Классы: `YooKassaService` (1 методов)
  Функции: нет

### app/utils

- `app/utils/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/utils/cache.py` — Python-модуль
  Классы: `CacheService` (1 методов), `UserCache`, `SystemCache`, `RateLimitCache`
  Функции: `cache_key`
- `app/utils/check_reg_process.py` — Python-модуль
  Классы: нет
  Функции: `is_registration_process`
- `app/utils/currency_converter.py` — Python-модуль
  Классы: `CurrencyConverter` (1 методов)
  Функции: нет
- `app/utils/decorators.py` — Python-модуль
  Классы: нет
  Функции: `admin_required`, `error_handler`, `_extract_event`, `state_cleanup`, `typing_action`, `rate_limit`
- `app/utils/formatters.py` — Python-модуль
  Классы: нет
  Функции: `format_datetime`, `format_date`, `format_time_ago`, `format_days_declension`, `format_duration`, `format_bytes`, `format_percentage`, `format_number`, `format_price_range`, `truncate_text`, `format_username`, `format_subscription_status`, `format_traffic_usage`, `format_boolean`
- `app/utils/message_patch.py` — Python-модуль
  Классы: нет
  Функции: `is_qr_message`, `_get_language`, `_default_privacy_hint`, `append_privacy_hint`, `prepare_privacy_safe_kwargs`, `is_privacy_restricted_error`, `patch_message_methods`
- `app/utils/miniapp_buttons.py` — Python-модуль
  Классы: нет
  Функции: `build_cabinet_url`, `build_miniapp_or_callback_button` — Create a button that opens the cabinet miniapp section or falls back to a callback.
- `app/utils/pagination.py` — Python-модуль
  Классы: `PaginationResult` (1 методов)
  Функции: `paginate_list`, `get_pagination_info`, `get_page_numbers`
- `app/utils/payment_utils.py` — Python-модуль
  Классы: нет
  Функции: `get_available_payment_methods` — Возвращает список доступных способов оплаты с их настройками, `get_payment_methods_text` — Генерирует текст с описанием доступных способов оплаты, `is_payment_method_available` — Проверяет, доступен ли конкретный способ оплаты, `get_payment_method_status` — Возвращает статус всех способов оплаты, `get_enabled_payment_methods_count` — Возвращает количество включенных способов оплаты (не считая поддержку)
- `app/utils/photo_message.py` — Python-модуль
  Классы: нет
  Функции: `_resolve_media`, `_get_language`, `_build_base_kwargs`
- `app/utils/pricing_utils.py` — Python-модуль
  Классы: нет
  Функции: `calculate_months_from_days`, `get_remaining_months`, `calculate_period_multiplier`, `calculate_prorated_price`, `apply_percentage_discount`, `format_period_description`, `validate_pricing_calculation`, `get_period_info`
- `app/utils/promo_offer.py` — Python-модуль
  Классы: нет
  Функции: `_escape_format_braces` — Escape braces so str.format treats them as literals., `get_user_active_promo_discount_percent`, `_format_time_left`, `_build_progress_bar`
- `app/utils/security.py` — Утилиты безопасности и генерации ключей.
  Классы: нет
  Функции: `hash_api_token` — Возвращает хеш токена в формате hex., `generate_api_token` — Генерирует криптографически стойкий токен.
- `app/utils/startup_timeline.py` — Python-модуль
  Классы: `StepRecord`, `StageHandle` (6 методов), `StartupTimeline` (6 методов)
  Функции: нет
- `app/utils/subscription_utils.py` — Python-модуль
  Классы: нет
  Функции: `get_display_subscription_link`, `get_happ_cryptolink_redirect_link`, `convert_subscription_link_to_happ_scheme`
- `app/utils/telegram_webapp.py` — Utilities for validating Telegram WebApp initialization data.
  Классы: `TelegramWebAppAuthError` — Raised when Telegram WebApp init data fails validation.
  Функции: `parse_webapp_init_data` — Validate and parse Telegram WebApp init data.
- `app/utils/user_utils.py` — Python-модуль
  Классы: нет
  Функции: `format_referrer_info` — Return formatted referrer info for admin notifications.
- `app/utils/validators.py` — Python-модуль
  Классы: нет
  Функции: `validate_email`, `validate_phone`, `validate_telegram_username`, `validate_promocode`, `validate_amount`, `validate_positive_integer`, `validate_date_string`, `validate_url`, `validate_uuid`, `validate_traffic_amount`, `validate_subscription_period`, `sanitize_html`, `sanitize_telegram_name` — Санитизация Telegram-имени для безопасной вставки в HTML и хранения., `validate_device_count`, `validate_referral_code`, `validate_html_tags`, `validate_html_structure`, `fix_html_tags`, `get_html_help_text`, `validate_rules_content`

### app/webapi

- `app/webapi/__init__.py` — Пакет административного веб-API.
  Классы: нет
  Функции: нет
- `app/webapi/app.py` — Python-модуль
  Классы: нет
  Функции: `create_web_api_app`
- `app/webapi/background/`
- `app/webapi/dependencies.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/webapi/middleware.py` — Python-модуль
  Классы: `RequestLoggingMiddleware` — Логирование входящих запросов в административный API.
  Функции: нет
- `app/webapi/routes/`
- `app/webapi/schemas/`
- `app/webapi/server.py` — Python-модуль
  Классы: `WebAPIServer` (1 методов) — Асинхронный uvicorn-сервер для административного API.
  Функции: нет

#### app/webapi/background

- `app/webapi/background/__init__.py` — Background utilities for Web API.
  Классы: нет
  Функции: нет
- `app/webapi/background/backup_tasks.py` — Python-модуль
  Классы: `BackupTaskState`, `BackupTaskManager` (1 методов)
  Функции: нет

#### app/webapi/routes

- `app/webapi/routes/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/webapi/routes/backups.py` — Python-модуль
  Классы: нет
  Функции: `_parse_datetime`, `_to_int`, `_serialize_backup`
- `app/webapi/routes/broadcasts.py` — Python-модуль
  Классы: нет
  Функции: `_serialize_broadcast`
- `app/webapi/routes/campaigns.py` — Python-модуль
  Классы: нет
  Функции: `_serialize_campaign`
- `app/webapi/routes/config.py` — Python-модуль
  Классы: нет
  Функции: `_coerce_value`, `_serialize_definition`
- `app/webapi/routes/health.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/webapi/routes/main_menu_buttons.py` — Python-модуль
  Классы: нет
  Функции: `_serialize`
- `app/webapi/routes/miniapp.py` — Python-модуль
  Классы: нет
  Функции: `_normalize_autopay_days`, `_get_autopay_day_options`, `_build_autopay_payload`, `_autopay_response_extras`, `_compute_cryptobot_limits`, `_current_request_timestamp`, `_compute_stars_min_amount`, `_normalize_stars_amount`, `_build_balance_invoice_payload`, `_merge_purchase_selection_from_request`, `_parse_client_timestamp`, `_classify_status`, `_format_gb`, `_format_gb_label`, `_format_limit_label`, `_normalize_amount_kopeks`, `_extract_template_id`, `_extract_offer_extra`, `_extract_offer_type`, `_normalize_effect_type`, `_determine_offer_icon`, `_extract_offer_test_squad_uuids`, `_format_offer_message`, `_extract_offer_duration_hours`, `_format_bonus_label`, `_bytes_to_gb`, `_status_label`, `_parse_datetime_string`, `_resolve_display_name`, `_is_remnawave_configured`, `_serialize_transaction`, `_is_trial_available_for_user`, `_safe_int`, `_normalize_period_discounts`, `_extract_promo_discounts`, `_normalize_language_code`, `_build_renewal_status_message`, `_build_promo_offer_payload`, `_build_renewal_period_id`, `_parse_period_identifier`, `_get_addon_discount_percent_for_user`, `_get_period_hint_from_subscription`, `_validate_subscription_id`, `_ensure_paid_subscription`
- `app/webapi/routes/pages.py` — Python-модуль
  Классы: нет
  Функции: `_serialize_rich_page`, `_serialize_faq_page`, `_serialize_rules`
- `app/webapi/routes/promo_groups.py` — Python-модуль
  Классы: нет
  Функции: `_normalize_period_discounts`, `_serialize`
- `app/webapi/routes/promo_offers.py` — Python-модуль
  Классы: нет
  Функции: `_serialize_user`, `_serialize_subscription`, `_serialize_offer`, `_serialize_template`, `_build_log_response`
- `app/webapi/routes/promocodes.py` — Python-модуль
  Классы: нет
  Функции: `_normalize_datetime`, `_serialize_promocode`, `_serialize_recent_use`, `_validate_create_payload`, `_validate_update_payload`
- `app/webapi/routes/remnawave.py` — Python-модуль
  Классы: нет
  Функции: `_get_service`, `_ensure_service_configured`, `_serialize_node`, `_parse_last_updated`
- `app/webapi/routes/stats.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/webapi/routes/subscriptions.py` — Python-модуль
  Классы: нет
  Функции: `_serialize_subscription`
- `app/webapi/routes/tickets.py` — Python-модуль
  Классы: нет
  Функции: `_serialize_message`, `_serialize_ticket`
- `app/webapi/routes/tokens.py` — Python-модуль
  Классы: нет
  Функции: `_serialize`
- `app/webapi/routes/transactions.py` — Python-модуль
  Классы: нет
  Функции: `_serialize`
- `app/webapi/routes/users.py` — Python-модуль
  Классы: нет
  Функции: `_serialize_promo_group`, `_serialize_subscription`, `_serialize_user`, `_apply_search_filter`

#### app/webapi/schemas

- `app/webapi/schemas/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/webapi/schemas/backups.py` — Python-модуль
  Классы: `BackupCreateResponse`, `BackupInfo`, `BackupListResponse`, `BackupStatusResponse`, `BackupTaskInfo`, `BackupTaskListResponse`
  Функции: нет
- `app/webapi/schemas/broadcasts.py` — Python-модуль
  Классы: `BroadcastMedia`, `BroadcastCreateRequest` (2 методов), `BroadcastResponse`, `BroadcastListResponse`
  Функции: нет
- `app/webapi/schemas/campaigns.py` — Python-модуль
  Классы: `CampaignBase` (1 методов), `CampaignCreateRequest` (2 методов), `CampaignResponse`, `CampaignListResponse`, `CampaignUpdateRequest` (3 методов)
  Функции: нет
- `app/webapi/schemas/config.py` — Python-модуль
  Классы: `SettingCategorySummary` — Краткое описание категории настройки., `SettingCategoryRef` — Ссылка на категорию, к которой относится настройка., `SettingChoice` — Вариант значения для настройки с выбором., `SettingDefinition` — Полное описание настройки и её текущего состояния., `SettingUpdateRequest` — Запрос на обновление значения настройки.
  Функции: нет
- `app/webapi/schemas/health.py` — Python-модуль
  Классы: `HealthFeatureFlags` — Флаги доступности функций административного API., `HealthCheckResponse` — Ответ на health-check административного API.
  Функции: нет
- `app/webapi/schemas/main_menu_buttons.py` — Python-модуль
  Классы: `MainMenuButtonResponse`, `MainMenuButtonCreateRequest`, `MainMenuButtonUpdateRequest` (2 методов), `MainMenuButtonListResponse`
  Функции: `_clean_text`, `_validate_action_value`
- `app/webapi/schemas/miniapp.py` — Python-модуль
  Классы: `MiniAppBranding`, `MiniAppSubscriptionRequest`, `MiniAppSubscriptionUser`, `MiniAppPromoGroup`, `MiniAppAutoPromoGroupLevel`, `MiniAppConnectedServer`, `MiniAppDevice`, `MiniAppDeviceRemovalRequest`, `MiniAppDeviceRemovalResponse`, `MiniAppTransaction`, `MiniAppPromoOffer`, `MiniAppPromoOfferClaimRequest`, `MiniAppPromoOfferClaimResponse`, `MiniAppSubscriptionAutopay`, `MiniAppSubscriptionRenewalPeriod`, `MiniAppSubscriptionRenewalOptionsRequest`, `MiniAppSubscriptionRenewalOptionsResponse`, `MiniAppSubscriptionRenewalRequest`, `MiniAppSubscriptionRenewalResponse`, `MiniAppSubscriptionAutopayRequest`, `MiniAppSubscriptionAutopayResponse`, `MiniAppPromoCode`, `MiniAppPromoCodeActivationRequest`, `MiniAppPromoCodeActivationResponse`, `MiniAppFaqItem`, `MiniAppFaq`, `MiniAppRichTextDocument`, `MiniAppLegalDocuments`, `MiniAppReferralTerms`, `MiniAppReferralStats`, `MiniAppReferralRecentEarning`, `MiniAppReferralItem`, `MiniAppReferralList`, `MiniAppReferralInfo`, `MiniAppPaymentMethodsRequest`, `MiniAppPaymentMethod`, `MiniAppPaymentMethodsResponse`, `MiniAppPaymentCreateRequest`, `MiniAppPaymentCreateResponse`, `MiniAppPaymentStatusQuery`, `MiniAppPaymentStatusRequest`, `MiniAppPaymentStatusResult`, `MiniAppPaymentStatusResponse`, `MiniAppSubscriptionResponse`, `MiniAppSubscriptionServerOption`, `MiniAppSubscriptionTrafficOption`, `MiniAppSubscriptionDeviceOption`, `MiniAppSubscriptionCurrentSettings`, `MiniAppSubscriptionServersSettings`, `MiniAppSubscriptionTrafficSettings`, `MiniAppSubscriptionDevicesSettings`, `MiniAppSubscriptionBillingContext`, `MiniAppSubscriptionSettings`, `MiniAppSubscriptionSettingsResponse`, `MiniAppSubscriptionSettingsRequest` (1 методов), `MiniAppSubscriptionServersUpdateRequest` (1 методов), `MiniAppSubscriptionTrafficUpdateRequest` (1 методов), `MiniAppSubscriptionDevicesUpdateRequest` (1 методов), `MiniAppSubscriptionUpdateResponse`, `MiniAppSubscriptionPurchaseOptionsRequest`, `MiniAppSubscriptionPurchaseOptionsResponse`, `MiniAppSubscriptionPurchasePreviewRequest` (1 методов), `MiniAppSubscriptionPurchasePreviewResponse`, `MiniAppSubscriptionPurchaseRequest`, `MiniAppSubscriptionPurchaseResponse`, `MiniAppSubscriptionTrialRequest`, `MiniAppSubscriptionTrialResponse`
  Функции: нет
- `app/webapi/schemas/pages.py` — Python-модуль
  Классы: `RichTextPageResponse` — Generic representation for rich text informational pages., `RichTextPageUpdateRequest`, `FaqPageResponse`, `FaqPageListResponse`, `FaqPageCreateRequest`, `FaqPageUpdateRequest`, `FaqReorderItem`, `FaqReorderRequest`, `FaqStatusResponse`, `FaqStatusUpdateRequest`, `ServiceRulesResponse`, `ServiceRulesUpdateRequest`, `ServiceRulesHistoryResponse`
  Функции: нет
- `app/webapi/schemas/promo_groups.py` — Python-модуль
  Классы: `PromoGroupResponse`, `_PromoGroupBase` (1 методов), `PromoGroupCreateRequest`, `PromoGroupUpdateRequest`, `PromoGroupListResponse`
  Функции: `_normalize_period_discounts`
- `app/webapi/schemas/promo_offers.py` — Python-модуль
  Классы: `PromoOfferUserInfo`, `PromoOfferSubscriptionInfo`, `PromoOfferResponse`, `PromoOfferListResponse`, `PromoOfferCreateRequest`, `PromoOfferTemplateResponse`, `PromoOfferTemplateListResponse`, `PromoOfferTemplateUpdateRequest`, `PromoOfferLogOfferInfo`, `PromoOfferLogResponse`, `PromoOfferLogListResponse`
  Функции: нет
- `app/webapi/schemas/promocodes.py` — Python-модуль
  Классы: `PromoCodeResponse`, `PromoCodeListResponse`, `PromoCodeCreateRequest`, `PromoCodeUpdateRequest`, `PromoCodeRecentUse`, `PromoCodeDetailResponse`
  Функции: нет
- `app/webapi/schemas/remnawave.py` — Python-модуль
  Классы: `RemnaWaveConnectionStatus`, `RemnaWaveStatusResponse`, `RemnaWaveNode`, `RemnaWaveNodeListResponse`, `RemnaWaveNodeActionRequest`, `RemnaWaveNodeActionResponse`, `RemnaWaveNodeStatisticsResponse`, `RemnaWaveNodeUsageResponse`, `RemnaWaveBandwidth`, `RemnaWaveTrafficPeriod`, `RemnaWaveTrafficPeriods`, `RemnaWaveSystemSummary`, `RemnaWaveServerInfo`, `RemnaWaveSystemStatsResponse`, `RemnaWaveSquad`, `RemnaWaveSquadListResponse`, `RemnaWaveSquadCreateRequest`, `RemnaWaveSquadUpdateRequest`, `RemnaWaveSquadActionRequest`, `RemnaWaveOperationResponse`, `RemnaWaveInboundsResponse`, `RemnaWaveUserTrafficResponse`, `RemnaWaveSyncFromPanelRequest`, `RemnaWaveGenericSyncResponse`, `RemnaWaveSquadMigrationPreviewResponse`, `RemnaWaveSquadMigrationRequest`, `RemnaWaveSquadMigrationStats`, `RemnaWaveSquadMigrationResponse`
  Функции: нет
- `app/webapi/schemas/subscriptions.py` — Python-модуль
  Классы: `SubscriptionResponse`, `SubscriptionCreateRequest`, `SubscriptionExtendRequest`, `SubscriptionTrafficRequest`, `SubscriptionDevicesRequest`, `SubscriptionSquadRequest`
  Функции: нет
- `app/webapi/schemas/tickets.py` — Python-модуль
  Классы: `TicketMessageResponse`, `TicketResponse`, `TicketStatusUpdateRequest`, `TicketPriorityUpdateRequest`, `TicketReplyBlockRequest`
  Функции: нет
- `app/webapi/schemas/tokens.py` — Python-модуль
  Классы: `TokenResponse`, `TokenCreateRequest`, `TokenCreateResponse`
  Функции: нет
- `app/webapi/schemas/transactions.py` — Python-модуль
  Классы: `TransactionResponse`, `TransactionListResponse`
  Функции: нет
- `app/webapi/schemas/users.py` — Python-модуль
  Классы: `PromoGroupSummary`, `SubscriptionSummary`, `UserResponse`, `UserListResponse`, `UserCreateRequest`, `UserUpdateRequest`, `BalanceUpdateRequest`
  Функции: нет

## tests

- `tests/conftest.py` — Глобальные фикстуры и настройки окружения для тестов.
  Классы: нет
  Функции: `fixed_datetime` — Возвращает фиксированную отметку времени для воспроизводимых проверок.
- `tests/external/`
- `tests/services/`
- `tests/test_miniapp_payments.py` — Python-модуль
  Классы: нет
  Функции: `anyio_backend`, `test_compute_cryptobot_limits_scale_with_rate`
- `tests/utils/`

### tests/external

- `tests/external/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `tests/external/test_cryptobot_service.py` — Тесты для внешнего клиента CryptoBotService.
  Классы: нет
  Функции: `anyio_backend`, `_enable_token`, `test_verify_webhook_signature`, `test_verify_webhook_signature_without_secret`
- `tests/external/test_webhook_server.py` — Тестирование хендлеров WebhookServer без запуска реального сервера.
  Классы: `DummyBot`
  Функции: `anyio_backend`, `webhook_server`, `_mock_request`

### tests/services

- `tests/services/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `tests/services/test_mulenpay_service_adapter.py` — Юнит-тесты MulenPayService.
  Классы: нет
  Функции: `anyio_backend`, `_enable_service`, `test_is_configured`, `test_format_and_signature`
- `tests/services/test_pal24_service_adapter.py` — Тесты Pal24Service и вспомогательных функций.
  Классы: `StubPal24Client` (1 методов)
  Функции: `_enable_pal24`, `anyio_backend`, `test_parse_callback_success`, `test_parse_callback_missing_fields`, `test_convert_to_kopeks_and_expiration`
- `tests/services/test_payment_service_cryptobot.py` — Тесты сценариев CryptoBot в PaymentService.
  Классы: `DummySession` (2 методов), `DummyLocalPayment` (1 методов), `StubCryptoBotService` (1 методов)
  Функции: `anyio_backend`, `_make_service`
- `tests/services/test_payment_service_heleket.py` — Тесты сценариев Heleket в PaymentService.
  Классы: `DummySession` (2 методов), `DummyLocalPayment` (1 методов), `StubHeleketService` (1 методов)
  Функции: `anyio_backend`, `_make_service`
- `tests/services/test_payment_service_mulenpay.py` — Тесты для сценариев MulenPay в PaymentService.
  Классы: `DummySession`, `DummyLocalPayment` (1 методов), `StubMulenPayService` (1 методов)
  Функции: `anyio_backend`, `_make_service`
- `tests/services/test_payment_service_pal24.py` — Тесты Pal24 сценариев PaymentService.
  Классы: `DummySession`, `DummyLocalPayment` (1 методов), `StubPal24Service` (1 методов)
  Функции: `anyio_backend`, `_make_service`
- `tests/services/test_payment_service_stars.py` — Тесты для Telegram Stars-сценариев внутри PaymentService.
  Классы: `DummyBot` (1 методов) — Минимальная заглушка aiogram.Bot для тестов.
  Функции: `anyio_backend` — Ограничиваем anyio тесты только бэкендом asyncio., `_make_service` — Создаёт экземпляр PaymentService без выполнения полного конструктора.
- `tests/services/test_payment_service_tribute.py` — Тесты Tribute-платежей PaymentService.
  Классы: нет
  Функции: `anyio_backend`, `_make_service`, `test_verify_tribute_webhook_signature`, `test_verify_tribute_webhook_returns_false_without_key`
- `tests/services/test_payment_service_webhooks.py` — Интеграционные проверки обработки вебхуков PaymentService.
  Классы: `DummyBot` (1 методов), `FakeSession` (2 методов)
  Функции: `_make_service`, `anyio_backend`
- `tests/services/test_payment_service_yookassa.py` — Тесты для YooKassa-сценариев PaymentService.
  Классы: `DummySession` (1 методов) — Простейшая заглушка AsyncSession., `DummyLocalPayment` (1 методов) — Объект, имитирующий локальную запись платежа., `StubYooKassaService` (1 методов) — Заглушка для SDK, сохраняющая вызовы.
  Функции: `anyio_backend` — Запускаем async-тесты на asyncio, чтобы избежать зависимостей trio., `_make_service`
- `tests/services/test_yookassa_service_adapter.py` — Тесты низкоуровневого сервиса YooKassaService.
  Классы: `DummyLoop`
  Функции: `anyio_backend`, `_prepare_config`, `test_init_without_credentials`

### tests/utils

- `tests/utils/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `tests/utils/test_formatters_basic.py` — Тесты для базовых форматтеров из app.utils.formatters.
  Классы: нет
  Функции: `test_format_datetime_handles_iso_strings` — ISO-строка должна корректно преобразовываться в отформатированный текст., `test_format_date_uses_custom_format` — Можно задавать собственный шаблон вывода., `test_format_time_ago_returns_human_readable_text` — Разница во времени должна переводиться в человеко-понятную строку., `test_format_days_declension_handles_russian_rules` — Склонение дней в русском языке зависит от числа., `test_format_duration_switches_units` — В зависимости от длины интервала выбирается подходящая единица измерения., `test_format_bytes_scales_value` — Размер должен выражаться в наиболее подходящей единице., `test_format_percentage_respects_precision` — Проценты форматируются с нужным количеством знаков., `test_format_number_inserts_separators` — Разделители тысяч должны расставляться корректно как для int, так и для float., `test_truncate_text_appends_suffix` — Строки, превышающие лимит, должны обрезаться и дополняться суффиксом., `test_format_username_prefers_full_name` — Полное имя имеет приоритет, затем username, затем ID., `test_format_subscription_status_handles_active_and_expired` — Статус подписки различается для активных/просроченных случаев., `test_format_traffic_usage_supports_unlimited` — При безлимитном тарифе в строке должна появляться бесконечность., `test_format_boolean_localises_output` — Булевые значения отображаются локализованными словами.
- `tests/utils/test_security.py` — Тесты для функций безопасности из app.utils.security.
  Классы: нет
  Функции: `test_hash_api_token_default_algorithm_matches_hashlib` — Проверяем, что алгоритм по умолчанию совпадает с hashlib.sha256., `test_hash_api_token_accepts_supported_algorithms` — Каждый поддерживаемый алгоритм должен выдавать корректный результат., `test_hash_api_token_rejects_unknown_algorithm` — Некорректное имя алгоритма должно приводить к ValueError., `test_generate_api_token_respects_length_bounds` — Функция должна ограничивать длину токена безопасным диапазоном., `test_generate_api_token_produces_random_values` — Два последовательных вызова должны выдавать разные токены.
- `tests/utils/test_validators_basic.py` — Базовые тесты для валидаторов из app.utils.validators.
  Классы: нет
  Функции: `test_validate_email_handles_expected_patterns` — Проверяем типичные корректные и некорректные адреса., `test_validate_phone_strips_formatting_and_checks_pattern` — Телефон должен соответствовать стандарту E.164 после очистки., `test_validate_telegram_username_enforces_length` — Telegram-логин должен быть 5-32 символов и содержать допустимые символы., `test_validate_amount_returns_float_within_bounds` — Числа должны конвертироваться с уважением к диапазону., `test_validate_positive_integer_enforces_upper_bound` — Положительное целое число выходит за пределы — возвращаем None., `test_validate_traffic_amount_supports_units` — Валидатор трафика распознаёт разные единицы измерения и особые значения., `test_validate_subscription_period_accepts_reasonable_range` — Диапазон допустимой длительности от 1 до 3650 дней., `test_validate_uuid_detects_standard_format` — UUID должен соответствовать HEX шаблону версии 4/5., `test_validate_url_recognises_https_links` — Валидатор URL допускает http/https ссылки и отклоняет произвольные строки., `test_validate_html_tags_rejects_unknown_tags` — Неизвестные HTML теги должны приводить к отказу., `test_validate_html_structure_detects_wrong_nesting` — Неправильная вложенность тегов должна сообщаться пользователю., `test_fix_html_tags_repairs_missing_quotes` — Автоисправление должно добавлять кавычки у ссылок., `test_validate_rules_content_detects_structure_error` — При нарушении структуры должны вернуться сообщение и отсутствие подсказки., `test_validate_rules_content_accepts_supported_markup` — Корректный HTML должен проходить проверку без сообщений.

## migrations

- `migrations/alembic/`

### migrations/alembic

- `migrations/alembic/alembic.ini` — файл (.ini)
- `migrations/alembic/env.py` — Python-модуль
  Классы: нет
  Функции: `run_migrations_offline`, `do_run_migrations`, `run_migrations_online`
- `migrations/alembic/versions/`

#### migrations/alembic/versions

- `migrations/alembic/versions/1f5f3a3f5a4d_add_promo_groups_and_user_fk.py` — add promo groups table and link users
  Классы: нет
  Функции: `_table_exists`, `_column_exists`, `_index_exists`, `_foreign_key_exists`, `upgrade`, `downgrade`
- `migrations/alembic/versions/4b6b0f58c8f9_add_period_discounts_to_promo_groups.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/5d1f1f8b2e9a_add_advertising_campaigns.py` — add advertising campaigns tables
  Классы: нет
  Функции: `_table_exists`, `_index_exists`, `upgrade`, `downgrade`
- `migrations/alembic/versions/8fd1e338eb45_add_sent_notifications_table.py` — add sent notifications table
  Классы: нет
  Функции: `_table_exists`, `_unique_constraint_exists`, `upgrade`, `downgrade`

## docs

- `docs/miniapp-setup.md` — файл (.md)
- `docs/web-admin-integration.md` — файл (.md)

## miniapp

- `miniapp/app-config.json` — файл (.json)
- `miniapp/index.html` — файл (.html)
- `miniapp/redirect/`

### miniapp/redirect

- `miniapp/redirect/index.html` — файл (.html)

## assets

- `assets/bedolaga_app3.svg` — файл (.svg)
- `assets/logo2.svg` — файл (.svg)

## locales

- `locales/en.json` — файл (.json)
- `locales/ru.json` — файл (.json)

## data

- `data/backups/`
- `data/bot.db` — файл (.db)

### data/backups


## logs

- `logs/bot.log` — файл (.log)
