# База по структуре проекта

> Документ собирается автоматически: `make docs-structure`.
> Правки руками затрутся — меняйте генератор `scripts/generate_structure_reference.py`.

Перечислены файлы под контролем версий. Для Python-модулей указаны классы
(с числом методов) и функции верхнего уровня; имена, начинающиеся с
подчёркивания, опущены как внутренние.

## Общая структура корня

- `.dockerignore` — файл
- `.env.example` — файл
- `.github/`
- `.gitignore` — файл
- `.python-version` — файл
- `.release-please-manifest.json` — файл
- `CHANGELOG.md` — файл
- `CONTRIBUTING.md` — файл
- `Dockerfile` — файл
- `LICENSE` — файл
- `Makefile` — файл
- `README.md` — файл
- `SECURITY.md` — файл
- `alembic.ini` — файл
- `app/`
- `assets/`
- `docker-compose.local.yml` — файл
- `docker-compose.yml` — файл
- `docs/`
- `main.py` — Python-модуль
  Классы: `GracefulExit` (2 методов)
  Функции: `main`
- `migrations/`
- `pyproject.toml` — файл
- `release-please-config.json` — файл
- `scripts/`
- `tests/`
- `uv.lock` — файл
- `vpn_logo.png` — файл

## .github

- `.github/ISSUE_TEMPLATE/`
- `.github/assets/`
- `.github/codeql/`
- `.github/dependabot.yml` — файл
- `.github/workflows/`

### .github/ISSUE_TEMPLATE

- `.github/ISSUE_TEMPLATE/bug-bedolage.md` — файл
- `.github/ISSUE_TEMPLATE/feat-bedolage.md` — файл

### .github/assets

- `.github/assets/bot-preview.png` — файл
- `.github/assets/cabinet-admin.png` — файл
- `.github/assets/cabinet-preview.png` — файл
- `.github/assets/logo.png` — файл
- `.github/assets/platega-logo.jpg` — файл
- `.github/assets/wata-logo.jpg` — файл

### .github/codeql

- `.github/codeql/codeql-config.yml` — файл

### .github/workflows

- `.github/workflows/codeql.yml` — файл
- `.github/workflows/docker-hub.yml` — файл
- `.github/workflows/docker-registry.yml` — файл
- `.github/workflows/lint.yml` — файл
- `.github/workflows/release-please.yml` — файл
- `.github/workflows/release.yml` — файл
- `.github/workflows/security-audit.yml` — файл
- `.github/workflows/tests.yml` — файл

## app

- `app/bot.py` — Python-модуль
  Классы: нет
  Функции: `debug_callback_handler`, `setup_bot`, `shutdown_bot`
- `app/bot_factory.py` — Python-модуль
  Классы: нет
  Функции: `create_bot` — Create a Bot instance with SOCKS5 proxy and/or custom Telegram API server.
- `app/cabinet/`
- `app/config.py` — Python-модуль
  Классы: `Settings` (397 методов)
  Функции: `transliterate_cyrillic` — Заменяет кириллические буквы латинскими, сохраняя регистр («Шмель» → «Shmel»)., `set_period_prices_from_db` — Устанавливает периоды/цены из БД., `get_db_period_prices` — Возвращает периоды/цены из БД если они загружены., `clear_db_period_prices` — Очищает кеш цен из тарифов (при переключении в classic mode)., `refresh_period_prices` — Rebuild cached period price mapping., `refresh_classic_period_prices` — Rebuild CLASSIC_PERIOD_PRICES from current settings., `get_traffic_prices`, `refresh_traffic_prices`
- `app/database/`
- `app/external/`
- `app/handlers/`
- `app/keyboards/`
- `app/lib/`
- `app/localization/`
- `app/logging_config.py` — Python-модуль
  Классы: нет
  Функции: `setup_logging` — Configure structlog and return formatters + notifier.
- `app/logging_handler.py` — Python-модуль
  Классы: `TelegramNotifierProcessor` (8 методов)
  Функции: нет
- `app/middlewares/`
- `app/services/`
- `app/states.py` — Python-модуль
  Классы: `RegistrationStates`, `SubscriptionStates`, `GiftPurchaseStates`, `GiftActivationStates`, `BalanceStates`, `PromoCodeStates`, `AdminStates`, `SupportStates`, `TicketStates`, `AdminTicketStates`, `SupportSettingsStates`, `BotConfigStates`, `PricingStates`, `AutoPayStates`, `SquadCreateStates`, `SquadRenameStates`, `SquadMigrationStates`, `RemnaWaveSyncStates`, `ContestStates`, `AdminSubmenuStates`, `BlacklistStates`, `ReferralWithdrawalStates`
  Функции: нет
- `app/tools/`
- `app/utils/`
- `app/webapi/`
- `app/webserver/`

### app/cabinet

- `app/cabinet/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/cabinet/apple_iap.py` — Python-модуль
  Классы: нет
  Функции: `apple_iap_lifespan`, `get_apple_iap_fulfillment_service`, `get_apple_iap_redis_client`, `apple_iap_account_token` — Return the stable StoreKit appAccountToken UUID for the authenticated user., `apple_purchase` — Verify an Apple consumable transaction and credit the user's internal balance., `search_apple_iap_transactions` — Support lookup for Apple IAP ledger entries., `reconcile_apple_iap_transactions` — Reconcile recent Apple IAP transactions against Apple's API.
- `app/cabinet/auth/`
- `app/cabinet/dependencies.py` — Python-модуль
  Классы: нет
  Функции: `get_cabinet_db` — Get database session for cabinet operations., `get_current_cabinet_user` — Get current authenticated cabinet user from JWT token., `get_optional_cabinet_user` — Optionally get current authenticated cabinet user., `get_current_admin_user` — Get current authenticated admin user., `require_permission` — FastAPI dependency factory for RBAC permission checks.
- `app/cabinet/ip_utils.py` — Python-модуль
  Классы: нет
  Функции: `get_client_ip` — Extract real client IP, trusting proxy headers only from known proxies.
- `app/cabinet/routes/`
- `app/cabinet/schemas/`
- `app/cabinet/services/`
- `app/cabinet/utils/`

#### app/cabinet/auth

- `app/cabinet/auth/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/cabinet/auth/email_verification.py` — Python-модуль
  Классы: нет
  Функции: `generate_email_change_code` — Generate a 6-digit verification code for email change., `get_email_change_expires_at` — Get the expiration datetime for an email change code., `generate_verification_token` — Generate a secure random verification token., `generate_password_reset_token` — Generate a secure random password reset token., `get_verification_expires_at` — Get the expiration datetime for a verification token., `get_password_reset_expires_at` — Get the expiration datetime for a password reset token., `is_token_expired` — Check if a token has expired.
- `app/cabinet/auth/jwt_handler.py` — Python-модуль
  Классы: нет
  Функции: `create_access_token` — Create a short-lived access token., `create_refresh_token` — Create a long-lived refresh token., `decode_token` — Decode and validate a JWT token., `get_token_payload` — Decode token and verify its type., `create_auto_login_token` — Short-lived JWT for auto-login from guest purchase success page., `get_refresh_token_expires_at` — Get the expiration datetime for a new refresh token.
- `app/cabinet/auth/merge_service.py` — Python-модуль
  Классы: нет
  Функции: `store_email_merge_otp` — Store a pending email-merge code for the initiator (overwrites any prior)., `get_email_merge_otp` — Read the pending email-merge code without consuming it., `clear_email_merge_otp` — Drop the pending email-merge code., `create_merge_token` — Generate a merge token and store its payload in Redis., `get_merge_token_data` — Read merge token payload *without* consuming it., `consume_merge_token` — Atomically read and delete a merge token (GETDEL)., `restore_merge_token` — Re-store a consumed merge token so the user can retry after a DB failure.
- `app/cabinet/auth/oauth_providers.py` — Python-модуль
  Классы: `OAuthProviderConfig`, `OAuthTokenResponse`, `GoogleUserInfoResponse`, `YandexUserInfoResponse`, `DiscordUserInfoResponse`, `VKIDUserData`, `VKIDUserInfoResponse`, `OAuthUserInfo`, `OAuthProvider` (5 методов), `GoogleProvider` (3 методов), `YandexProvider` (3 методов), `DiscordProvider` (3 методов), `VKProvider` (5 методов)
  Функции: `generate_oauth_state` — Generate a CSRF state token for OAuth flow., `validate_oauth_state` — Validate and consume a CSRF state token from Redis., `resolve_oauth_redirect_uri` — Pick the OAuth redirect_uri for the request's origin., `get_provider` — Get an OAuth provider instance if enabled.
- `app/cabinet/auth/password_utils.py` — Python-модуль
  Классы: нет
  Функции: `hash_password` — Hash a password using bcrypt., `verify_password` — Verify a password against its hash.
- `app/cabinet/auth/registration_access.py` — Python-модуль
  Классы: нет
  Функции: `evaluate_public_registration`, `raise_for_registration_decision`, `is_env_admin_recovery` — True when a non-ACTIVE account must be restored because env names it as admin.
- `app/cabinet/auth/telegram_auth.py` — Python-модуль
  Классы: нет
  Функции: `validate_telegram_login_widget` — Validate Telegram Login Widget data., `validate_telegram_init_data` — Validate Telegram WebApp initData., `extract_telegram_user_from_init_data` — Extract and validate user info from Telegram WebApp initData., `validate_telegram_oidc_token` — Validate a Telegram OIDC id_token using JWKS.

#### app/cabinet/routes

- `app/cabinet/routes/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/cabinet/routes/account_linking.py` — Python-модуль
  Классы: `OAuthStateData`, `LinkedProvider`, `LinkedProvidersResponse`, `LinkInitResponse`, `LinkCallbackRequest`, `LinkCallbackResponse`, `UnlinkResponse`, `LinkTelegramRequest` (1 методов), `MergePreviewSubscription`, `MergePreviewUser`, `MergePreviewResponse`, `MergeRequest`, `MergeResponse`, `ServerCompleteRequest`, `ServerCompleteResponse`
  Функции: `get_linked_providers` — Return all auth methods with their link status for the current user., `link_provider_init` — Start OAuth flow for linking a new provider to the current account., `link_provider_callback` — Handle OAuth callback for linking a provider to the current account., `unlink_provider` — Unlink an OAuth provider from the current account., `link_telegram` — Link Telegram account via WebApp initData, OIDC id_token, or Login Widget., `link_server_complete` — Complete OAuth account linking without JWT., `get_merge_preview_endpoint` — Preview the result of merging two accounts before confirming., `execute_merge_endpoint` — Execute account merge. Consumes the merge token (one-time use).
- `app/cabinet/routes/admin_apps.py` — Python-модуль
  Классы: `RemnaWaveConfigStatus`, `UpdateRemnaWaveUuidRequest`
  Функции: `get_remnawave_config_status` — Get RemnaWave config integration status., `set_remnawave_config_uuid` — Set RemnaWave subscription config UUID., `get_remnawave_subscription_config` — Fetch subscription page config from RemnaWave panel., `list_remnawave_subscription_configs` — List available subscription page configs from RemnaWave panel.
- `app/cabinet/routes/admin_audit_log.py` — Python-модуль
  Классы: `AuditLogEntry`, `AuditLogListResponse`
  Функции: `list_audit_logs` — List audit log entries with optional filters and pagination., `export_audit_logs` — Export audit logs as CSV file.
- `app/cabinet/routes/admin_ban_system.py` — Python-модуль
  Классы: нет
  Функции: `get_ban_system_status` — Get Ban System integration status., `get_stats_raw` — Get raw stats from Ban System API for debugging., `get_stats` — Get overall Ban System statistics., `get_users` — Get list of users from Ban System., `get_users_over_limit` — Get users who exceeded their device limit., `search_users` — Search for users., `get_user_detail` — Get detailed user information., `get_punishments` — Get list of active punishments (bans)., `unban_user` — Unban (enable) a user., `ban_user` — Manually ban a user., `get_punishment_history` — Get punishment history for a user., `get_nodes` — Get list of connected nodes., `get_agents` — Get list of monitoring agents., `get_agents_summary` — Get agents summary statistics., `get_traffic_violations` — Get list of traffic limit violations., `get_traffic` — Get full traffic statistics including top users., `get_traffic_top` — Get top users by traffic., `get_settings` — Get all Ban System settings., `get_setting` — Get a specific setting., `set_setting` — Set a setting value., `toggle_setting` — Toggle a boolean setting., `whitelist_add` — Add user to whitelist., `whitelist_remove` — Remove user from whitelist., `get_report` — Get period report., `get_health` — Get Ban System health status., `get_health_detailed` — Get detailed health information., `get_agent_history` — Get agent statistics history., `get_user_punishment_history` — Get punishment history for a specific user.
- `app/cabinet/routes/admin_broadcasts.py` — Python-модуль
  Классы: нет
  Функции: `get_filters` — Get all available filters with user counts., `get_tariffs` — Get tariffs for broadcast filtering., `get_buttons` — Get available buttons for broadcasts., `preview_broadcast` — Preview broadcast recipients count., `create_broadcast` — Create and start a broadcast., `list_broadcasts` — Get list of broadcasts with pagination., `get_email_filters` — Get all available email filters with user counts., `preview_email_broadcast` — Preview email broadcast recipients count., `render_email_broadcast` — Письмо рассылки так, как его получит адресат., `create_combined_broadcast` — Create and start a combined broadcast (telegram/email/both)., `get_broadcast` — Get broadcast details., `stop_broadcast` — Stop a running broadcast (telegram or email).
- `app/cabinet/routes/admin_bulk_actions.py` — Python-модуль
  Классы: нет
  Функции: `bulk_execute` — Execute a bulk action on multiple users or subscriptions.
- `app/cabinet/routes/admin_button_styles.py` — Python-модуль
  Классы: `ButtonSectionConfig`, `ButtonStylesResponse`, `ButtonSectionUpdate`, `ButtonStylesUpdate`
  Функции: `get_button_styles` — Return current per-section button styles. Admin only., `update_button_styles` — Partially update per-section button styles. Admin only., `reset_button_styles` — Reset all button styles to defaults. Admin only.
- `app/cabinet/routes/admin_campaigns.py` — Python-модуль
  Классы: нет
  Функции: `get_overview` — Get campaigns overview statistics., `get_available_servers` — Get list of available server squads for campaign subscription bonus., `get_available_tariffs` — Get list of available tariffs for campaign tariff bonus., `get_available_partners` — Get list of approved partners for campaign partner selector., `list_campaigns` — Get list of all campaigns., `get_campaign` — Get detailed campaign info., `get_campaign_chart_data` — Get chart data for admin campaign analytics., `get_campaign_stats` — Get detailed campaign statistics., `get_campaign_registrations` — Get list of users registered through campaign., `create_new_campaign` — Create a new advertising campaign., `update_existing_campaign` — Update an existing campaign., `delete_existing_campaign` — Delete a campaign., `toggle_campaign` — Toggle campaign active status.
- `app/cabinet/routes/admin_channels.py` — Python-модуль
  Классы: нет
  Функции: `list_channels`, `create_channel`, `update_channel_endpoint`, `toggle_channel_endpoint`, `delete_channel_endpoint`
- `app/cabinet/routes/admin_coupons.py` — Python-модуль
  Классы: нет
  Функции: `list_coupon_batches` — List coupon batches with redemption stats., `create_coupon_batch_endpoint` — Create a batch of one-time coupons and return the generated links., `get_coupon_batch` — Batch card with redemption stats., `export_coupon_batch_links` — Still-active coupon links of the batch (for handing to the partner)., `revoke_coupon_batch` — Revoke all still-active coupons of the batch (e.g. the partner did not pay)., `delete_batch` — Полностью удаляет партию вместе с её купонами.
- `app/cabinet/routes/admin_email_templates.py` — Python-модуль
  Классы: `EmailTemplateUpdate`, `EmailTemplateEnabledRequest`, `EmailTemplatePreviewRequest`, `EmailTemplateSendTestRequest`
  Функции: `list_template_types` — List all available email template types with override status., `get_templates_for_type` — Get all language templates for a specific notification type., `update_template` — Save a custom email template override., `set_template_enabled` — Выключатель писем по типу: отключённое письмо не отправляется никому., `reset_template` — Delete custom template override, reverting to default., `preview_template` — Preview a rendered email template with sample data., `send_test_email` — Send a test email to the admin's email address.
- `app/cabinet/routes/admin_grace_access.py` — Python-модуль
  Классы: `GraceAccessConfig`, `GraceAccessRuntimeState`, `GraceAccessStats`, `GraceAccessIssue`, `GraceSessionError`, `GraceAccessOverview`, `GraceAccessUpdate`, `GraceSessionUser`, `GraceSessionItem`, `GraceSessionsPage`, `GraceSquadOption`, `GraceSquadsResponse`
  Функции: `get_grace_access_overview` — Configuration, running state and session health in one payload., `list_grace_squads` — Squads offered by the panel, for picking the grace squads by name., `list_grace_sessions` — Grace sessions, newest first., `update_grace_access` — Apply a partial configuration change, validated as a whole.
- `app/cabinet/routes/admin_info_pages.py` — Python-модуль
  Классы: нет
  Функции: `list_all_info_pages` — Get all info pages (admin view, includes inactive)., `get_info_page_detail` — Get a single info page by ID (admin view)., `create_page` — Create a new info page., `update_page` — Update an existing info page., `remove_page` — Delete an info page., `reorder_pages` — Bulk update sort_order for info pages., `toggle_active` — Toggle the active status of an info page.
- `app/cabinet/routes/admin_landings.py` — Python-модуль
  Классы: `LandingFeatureInput` (3 методов), `LandingPaymentMethodInput` (6 методов), `LandingCreateRequest` (12 методов), `LandingUpdateRequest` (11 методов), `PurchaseStats`, `LandingListItem` (1 методов), `LandingDetailResponse` (1 методов), `OrderRequest`, `LandingDailyStat`, `LandingTariffStat`, `LandingPaymentMethodStat`, `LandingSourceStat`, `LandingStatsResponse`, `LandingPurchaseItem`, `LandingPurchaseListResponse`
  Функции: `list_landings` — List all landing pages with purchase stats., `create_landing_page` — Create a new landing page., `update_landings_order` — Batch update display order for landing pages., `get_landing_detail` — Get a single landing page with full details., `update_landing_page` — Update a landing page., `delete_landing_page` — Delete a landing page., `toggle_landing_active` — Toggle active/inactive state of a landing page., `get_landing_stats` — Get daily statistics and tariff breakdown for a landing page., `get_landing_purchases` — Get paginated list of purchases for a landing page.
- `app/cabinet/routes/admin_legal_pages.py` — Python-модуль
  Классы: `LegalDocumentItem`, `LegalDocumentResponse`, `LegalDocumentItemUpdate`, `LegalDocumentUpdateRequest`, `RulesItem`, `RulesResponse`, `RulesItemUpdate`, `RulesUpdateRequest`, `FaqSettingItem`, `FaqPageItem`, `FaqResponse`, `FaqUpdateRequest`, `FaqPageCreateRequest`, `FaqPageUpdateRequest`
  Функции: `get_privacy_policy_admin`, `update_privacy_policy_admin`, `get_public_offer_admin`, `update_public_offer_admin`, `get_recurrent_payments_admin`, `update_recurrent_payments_admin`, `get_rules_admin`, `update_rules_admin`, `get_faq_admin`, `update_faq_admin`, `create_faq_page_admin`, `update_faq_page_admin`, `delete_faq_page_admin`
- `app/cabinet/routes/admin_menu_layout.py` — Python-модуль
  Классы: `ButtonConfig`, `RowConfig`, `MenuConfigResponse`, `MenuConfigUpdateRequest`
  Функции: `get_menu_layout` — Return merged menu layout config (rows + button styles). Admin only., `update_menu_layout` — Save full menu layout config. Splits into layout + button styles. Admin only., `reset_menu_layout` — Reset menu layout AND button styles to defaults. Admin only.
- `app/cabinet/routes/admin_news.py` — Python-модуль
  Классы: нет
  Функции: `list_all_news` — Get all news articles (admin view, includes unpublished)., `get_article_detail` — Get a single news article by ID (admin view)., `create_article` — Create a new news article., `update_article` — Update an existing news article., `remove_article` — Delete a news article., `toggle_publish` — Toggle the published status of a news article., `toggle_featured` — Toggle the featured status of a news article.
- `app/cabinet/routes/admin_news_categories.py` — Python-модуль
  Классы: нет
  Функции: `list_categories` — Get all news categories., `create_new_category` — Create a new news category., `update_existing_category` — Update an existing news category., `remove_category` — Delete a news category. Articles using it will have category_id set to NULL.
- `app/cabinet/routes/admin_news_media.py` — Python-модуль
  Классы: нет
  Функции: `upload_media` — Upload an image or video for a news article., `delete_media` — Delete a previously uploaded media file.
- `app/cabinet/routes/admin_news_tags.py` — Python-модуль
  Классы: нет
  Функции: `list_tags` — Get all news tags., `create_new_tag` — Create a new news tag., `update_existing_tag` — Update an existing news tag., `remove_tag` — Delete a news tag. Articles using it will have tag_id set to NULL.
- `app/cabinet/routes/admin_overpay_certificate.py` — Python-модуль
  Классы: `OverpayCertificateStatusResponse`, `OverpayCertificateUploadResponse`
  Функции: `get_certificate_status`, `upload_certificate`, `delete_certificate`
- `app/cabinet/routes/admin_partners.py` — Python-модуль
  Классы: `PartnerSettingsResponse`, `PartnerSettingsUpdateRequest`
  Функции: `get_partner_settings` — Get partner system settings., `update_partner_settings` — Update partner system settings., `list_applications` — List partner applications., `approve_application` — Approve a partner application., `reject_application` — Reject a partner application., `get_partner_stats` — Get overall partner statistics., `list_partners` — List approved partners., `list_referral_levels` — Список уровней реферальных наград и текущая схема., `upsert_referral_level` — Создать или обновить правило уровня., `remove_referral_level` — Удалить правило уровня., `update_referral_depth` — Сколько звеньев цепочки получают награду., `import_legacy_referral_settings` — Перенести действующие настройки ``REFERRAL_*`` в уровень 1., `update_referral_levels_mode` — Что означает номер уровня: глубина цепочки или ранг партнёра., `update_referral_scheme` — Переключить схему наград., `get_partner_detail` — Get detailed partner info., `update_commission` — Update partner commission percent., `revoke_partner` — Revoke partner status., `assign_campaign` — Assign a campaign to a partner., `unassign_campaign` — Unassign a campaign from a partner.
- `app/cabinet/routes/admin_payment_methods.py` — Python-модуль
  Классы: `SubOptionInfo`, `PaymentMethodConfigResponse`, `PaymentMethodConfigUpdateRequest` (2 методов), `SortOrderRequest`, `PromoGroupSimple`
  Функции: `list_payment_methods` — List all payment method configurations., `list_promo_groups` — List all promo groups for filter selector., `get_payment_method` — Get a single payment method configuration., `update_payment_methods_order` — Batch update sort order for payment methods., `update_payment_method` — Update a payment method configuration.
- `app/cabinet/routes/admin_payments.py` — Python-модуль
  Классы: `PendingPaymentResponse`, `PendingPaymentListResponse`, `ManualCheckResponse`, `PaymentsStatsResponse`, `SearchStatsResponse`
  Функции: `get_all_pending_payments` — Get all pending payments for admin verification., `get_payments_stats` — Get statistics about pending payments., `search_payments_endpoint` — Search payments across all providers with filters., `search_payments_stats_endpoint` — Get aggregated statistics for payment search results., `get_pending_payment_details` — Get details of a specific pending payment., `check_payment_status` — Manually check and update payment status.
- `app/cabinet/routes/admin_pinned_messages.py` — Python-модуль
  Классы: нет
  Функции: `list_pinned_messages` — Get list of pinned messages with pagination., `get_active_message` — Get current active pinned message., `get_pinned_message` — Get pinned message by ID., `create_pinned_message` — Create a new pinned message., `update_pinned_message` — Update a pinned message content, media, or settings., `update_pinned_message_settings` — Update only pinned message display settings., `deactivate_active_message` — Deactivate the current active pinned message without unpinning from users., `unpin_active_message` — Unpin messages from all users and deactivate the active pinned message., `activate_pinned_message` — Activate a pinned message., `broadcast_message` — Broadcast a pinned message to all active users., `delete_pinned_message` — Delete a pinned message. Active messages must be deactivated first.
- `app/cabinet/routes/admin_policies.py` — Python-модуль
  Классы: `PolicyResponse`, `PolicyCreateRequest`, `PolicyUpdateRequest`
  Функции: `list_policies` — List all access policies. Optionally filter by role_id., `create_policy` — Create a new access policy (ABAC rule)., `update_policy` — Update an existing access policy., `delete_policy` — Delete an access policy.
- `app/cabinet/routes/admin_promo_offers.py` — Python-модуль
  Классы: `PromoOfferUserInfo`, `PromoOfferResponse`, `PromoOfferListResponse`, `PromoOfferTemplateResponse`, `PromoOfferTemplateListResponse`, `PromoOfferTemplateUpdateRequest`, `PromoOfferBroadcastRequest` (1 методов), `PromoOfferBroadcastResponse`, `PromoOfferSegment`, `PromoOfferSegmentListResponse`, `PromoOfferLogOfferInfo`, `PromoOfferLogResponse`, `PromoOfferLogListResponse`
  Функции: `list_segments` — Число пользователей в каждом сегменте — чтобы админ видел охват до отправки., `list_templates` — Get list of promo offer templates., `get_template` — Get a promo offer template., `update_template` — Update a promo offer template., `list_offers` — Get list of promo offers., `broadcast_offer` — Broadcast promo offer to users with optional Telegram notification., `get_logs` — Get promo offer logs.
- `app/cabinet/routes/admin_promocodes.py` — Python-модуль
  Классы: `PromoCodeResponse`, `PromoCodeListResponse`, `PromoCodeRecentUse`, `PromoCodeDetailResponse`, `PromoCodeCreateRequest`, `PromoCodeUpdateRequest`, `PromoGroupResponse`, `PromoGroupListResponse`, `PromoGroupCreateRequest`, `PromoGroupUpdateRequest`, `DeactivateDiscountResponse`
  Функции: `list_promocodes` — Get list of all promocodes., `get_promocode` — Get promocode details with usage statistics., `create_promocode_endpoint` — Create a new promocode., `update_promocode_endpoint` — Update an existing promocode., `delete_promocode_endpoint` — Delete a promocode., `admin_deactivate_discount_promocode` — Admin: deactivate a user's active discount (promo code or promo offer)., `list_promo_groups` — Get list of all promo groups., `get_promo_group` — Get promo group details., `create_promo_group_endpoint` — Create a new promo group., `update_promo_group_endpoint` — Update a promo group., `delete_promo_group_endpoint` — Delete a promo group.
- `app/cabinet/routes/admin_reachability.py` — Python-модуль
  Классы: нет
  Функции: `get_status`, `get_units`, `get_hosts`, `get_nodes`, `get_subscription_configs`, `parse_input` — Поле «Конфиг или подписка»: ссылки, URL подписок, base64 → конфиги с готовыми целями., `update_pref`, `preview_job`, `create_job`, `list_jobs`, `get_job`, `cancel_job`, `preview_batch`, `create_batch`, `list_batches`, `get_batch`, `cancel_batch`, `get_summary`
- `app/cabinet/routes/admin_referral_network.py` — Python-модуль
  Классы: `NetworkUserNode`, `TopReferrer`, `NetworkCampaignNode`, `NetworkEdge`, `NetworkGraphResponse`, `NetworkUserDetail`, `NetworkCampaignDetail`, `NetworkSearchResult`, `CampaignOption`, `PartnerOption`, `ScopeOptionsResponse`
  Функции: `get_referral_network` — Return full referral network graph data for visualization., `get_scope_options` — Return lightweight lists of campaigns and partners for the scope selector., `get_scoped_referral_network` — Return scoped referral network graph for selected campaigns, partners, and/or users., `get_network_user_detail` — Return detailed info about a specific user in the referral network., `get_network_campaign_detail` — Return detailed info about a specific advertising campaign., `search_referral_network` — Search users and campaigns in the referral network by telegram_id, username, email, or campaign name.
- `app/cabinet/routes/admin_remnawave.py` — Python-модуль
  Классы: `RestartAllNodesPayload`
  Функции: `get_remnawave_status` — Get RemnaWave configuration and connection status., `get_system_statistics` — Get full system statistics from RemnaWave., `get_recap` — Panel recap: lifetime/this-month traffic, version, uptime, distinct countries., `get_devices_stats` — HWID device statistics: breakdown by platform and app + totals., `get_top_consumers_route` — Top traffic-consuming users aggregated across nodes for the last N days., `get_health_route` — Panel process runtime health: RAM, event-loop p99 lag, uptime., `get_subscription_requests_route` — Subscription-link request stats by client app., `list_nodes` — Get list of all nodes., `get_nodes_overview` — Get nodes overview with statistics., `get_nodes_realtime` — Get realtime node usage data., `get_node_details` — Get detailed information about a specific node., `get_node_statistics` — Get node statistics with usage history., `get_node_usage` — Get node usage history for a date range., `perform_node_action` — Perform an action on a node (enable/disable/restart)., `restart_all_nodes` — Restart all nodes., `start_node_geocheck` — Queue a GeoCheck on the node and return its job id., `get_node_geocheck` — Poll a GeoCheck job: the node may take up to a minute to answer., `list_squads` — Get list of all squads with local database info., `get_squad_details` — Get detailed information about a squad., `create_squad` — Create a new squad in RemnaWave., `update_squad` — Update a squad in RemnaWave., `perform_squad_action` — Perform an action on a squad., `delete_squad` — Delete a squad., `preview_migration` — Get migration preview for a squad., `migrate_squad_users` — Migrate users from one squad to another., `list_inbounds` — Get list of all available inbounds., `get_auto_sync_status` — Get auto sync status., `toggle_auto_sync` — Toggle auto sync on/off., `run_auto_sync_now` — Run auto sync immediately., `sync_from_panel` — Sync users from RemnaWave panel to bot., `sync_to_panel` — Sync users from bot to RemnaWave panel., `sync_servers` — Sync servers/squads from RemnaWave., `validate_subscriptions` — Validate and fix subscriptions., `cleanup_subscriptions` — Cleanup orphaned subscriptions., `sync_subscription_statuses` — Sync subscription statuses., `get_sync_recommendations` — Get sync recommendations.
- `app/cabinet/routes/admin_roles.py` — Python-модуль
  Классы: `RoleResponse`, `RoleCreateRequest`, `RoleUpdateRequest`, `RoleAssignRequest`, `PermissionSection`, `UserRoleResponse`, `AdminWithRolesResponse`
  Функции: `get_permission_registry` — Get all available permissions grouped by section., `list_rbac_users` — List all users that have at least one active RBAC role., `list_role_users` — List user-role assignments for a specific role., `list_roles` — List all admin roles with user counts., `create_role` — Create a new custom admin role., `update_role` — Update an existing admin role., `delete_role` — Delete a custom admin role. System roles cannot be deleted., `assign_role` — Assign a role to a user. Hierarchy enforcement applies., `revoke_role` — Revoke a role assignment. Superadmin roles are managed via env config.
- `app/cabinet/routes/admin_sales_stats.py` — Python-модуль
  Классы: `SalesSummary`, `ProviderBreakdownItem`, `DailyTrialItem`, `TrialsStatsResponse`, `SalesByTariffItem`, `SalesByPeriodItem`, `DailySalesItem`, `DailyTariffSalesItem`, `SalesStatsResponse`, `DailyRenewalItem`, `RenewalPeriodStats`, `RenewalChange`, `RenewalsStatsResponse`, `AddonByPackageItem`, `DailyAddonItem`, `DailyDeviceItem`, `AddonsStatsResponse`, `DepositByMethodItem`, `DailyDepositItem`, `DailyDepositByMethodItem`, `DepositsStatsResponse`, `GatewaySuccessItem`, `PaymentHealthResponse`
  Функции: `get_sales_summary` — Get summary statistics for sales dashboard cards., `get_trials_stats` — Get trial registration statistics with provider breakdown., `get_sales_stats` — Get subscription sales statistics., `get_renewals_stats` — Get renewal statistics with period comparison., `get_addons_stats` — Get add-on purchase statistics., `get_deposits_stats` — Get deposit statistics with payment method breakdown., `get_payment_health` — Payment reliability: per-gateway success-rate + failed-purchase rollbacks.
- `app/cabinet/routes/admin_servers.py` — Python-модуль
  Классы: нет
  Функции: `list_servers` — Get list of all servers., `get_server` — Get detailed server info., `update_existing_server` — Update an existing server., `toggle_server` — Toggle server availability., `toggle_server_trial` — Toggle server trial eligibility., `get_server_stats` — Get server statistics., `sync_servers` — Sync servers with RemnaWave.
- `app/cabinet/routes/admin_settings.py` — Python-модуль
  Классы: `SettingCategoryRef`, `SettingCategorySummary`, `SettingChoice`, `SettingHint`, `SettingDefinition`, `SettingUpdateRequest`
  Функции: `list_categories` — Get list of setting categories., `list_settings` — Get list of all settings or settings for a specific category., `get_setting` — Get a specific setting by key., `update_setting` — Update a setting value., `reset_setting` — Reset a setting to its default value.
- `app/cabinet/routes/admin_stats.py` — Python-модуль
  Классы: `NodeStatus`, `NodesOverview`, `RevenueData`, `SubscriptionStats`, `FinancialStats`, `ServerStats`, `TariffStatItem`, `TariffStats`, `DashboardStats`, `SystemInfoResponse`, `TopReferrerItem`, `TopReferrersResponse`, `TopCampaignItem`, `TopCampaignsResponse`, `RecentPaymentItem`, `RecentPaymentsResponse`
  Функции: `get_dashboard_stats` — Get complete dashboard statistics for admin panel., `get_system_info` — Get system information for admin dashboard., `get_nodes_status` — Get status of all nodes., `restart_node` — Restart a node., `toggle_node` — Enable or disable a node., `get_top_referrers` — Get top referrers with earnings breakdown by period., `get_top_campaigns` — Get top advertising campaigns with statistics., `get_recent_payments` — Get recent payments with user info.
- `app/cabinet/routes/admin_system_errors.py` — Python-модуль
  Классы: `SystemErrorListItem`, `SystemErrorDetail`, `SystemErrorListResponse`, `SystemErrorSummary`
  Функции: `system_errors_summary` — Сводка для бейджа в шапке и верхнего блока страницы., `list_system_errors` — Список ошибок с фильтрами по уровню, статусу доставки и периоду., `get_system_error` — Полная запись с трейсбеком и контекстом., `retry_system_error_delivery` — Повторно отправить сохранённую ошибку в админ-чат.
- `app/cabinet/routes/admin_tariffs.py` — Python-модуль
  Классы: нет
  Функции: `list_tariffs` — Get list of all tariffs., `get_available_servers` — Get list of all servers for tariff selection., `get_available_external_squads` — Fetch external squads from RemnaWave panel., `update_tariff_order` — Update the display order of tariffs., `get_tariff` — Get detailed tariff info., `create_new_tariff` — Create a new tariff., `update_existing_tariff` — Update an existing tariff., `delete_existing_tariff` — Delete a tariff., `toggle_tariff` — Toggle tariff active status., `toggle_trial_tariff` — Toggle tariff trial availability., `get_tariff_stats` — Get tariff statistics., `sync_tariff_squads` — Sync squads from tariff to all active/trial subscriptions in Remnawave panel.
- `app/cabinet/routes/admin_tickets.py` — Python-модуль
  Классы: `AdminTicketUserInfo`, `AdminTicketResponse`, `AdminTicketDetailResponse`, `AdminTicketListResponse`, `AdminReplyRequest` (1 методов), `AdminStatusUpdateRequest`, `AdminPriorityUpdateRequest`, `AdminStatsResponse`, `TicketSettingsResponse`, `TicketSettingsUpdateRequest`
  Функции: `get_ticket_stats` — Get ticket statistics., `get_ticket_settings` — Get ticket system settings., `update_ticket_settings` — Update ticket system settings., `get_all_tickets` — Get all tickets for admin., `get_ticket_detail` — Get ticket with all messages for admin., `reply_to_ticket` — Reply to a ticket as admin., `update_ticket_status` — Update ticket status., `update_ticket_priority` — Update ticket priority.
- `app/cabinet/routes/admin_traffic.py` — Python-модуль
  Классы: нет
  Функции: `get_traffic_usage` — Get paginated per-user traffic usage by node., `get_traffic_enrichment` — Return enrichment data: device counts, spending, dates, last node., `export_traffic_csv` — Generate CSV with traffic usage and send to admin's Telegram DM.
- `app/cabinet/routes/admin_updates.py` — Python-модуль
  Классы: `ReleaseItem`, `ProjectReleasesInfo`, `ReleasesResponse`
  Функции: `get_releases` — Get release information for bot and cabinet.
- `app/cabinet/routes/admin_users.py` — Python-модуль
  Классы: нет
  Функции: `list_users` — Get paginated list of users with filtering and sorting., `get_users_stats` — Get overall users statistics., `get_user_by_remnawave_identifier` — Resolve an exact Remnawave identifier through its owning subscription only., `get_user_detail` — Get detailed user information by ID., `get_user_by_telegram` — Get user by Telegram ID., `get_user_panel_info` — Get user panel info from Remnawave (config links, traffic, connection data)., `get_subscription_request_history` — Get subscription request history from RemnaWave panel., `get_user_node_usage` — Get user per-node traffic usage (always 30 days with daily breakdown)., `update_user_balance` — Update user balance., `update_user_subscription` — Update user subscription., `cancel_user_sbp_recurring` — Admin best-effort cancel of a user's active Platega SBP auto-renewal., `delete_user_subscription` — Удалить конкретную подписку пользователя из его карточки., `get_user_available_tariffs` — Get list of tariffs available for a specific user., `update_user_status` — Update user status (active, blocked, deleted)., `block_user` — Block a user — sets DB status AND disables panel user in RemnaWave., `unblock_user` — Unblock a user — sets DB status AND re-enables panel user in RemnaWave., `send_user_message` — Send a direct Telegram message to the user via the bot., `update_user_restrictions` — Update user restrictions (topup, subscription)., `update_user_promo_group` — Update user promo group., `update_user_referral_commission` — Update user's individual referral commission percentage., `assign_user_referrer` — Manually assign a referrer to a user (e.g. cabinet-registered users without telegram_id)., `remove_user_referrer` — Remove who referred this user (set referred_by_id to None)., `remove_user_referral` — Remove a specific referral from a user (unbind referral_user from this referrer)., `get_user_devices` — Get user devices from Remnawave panel., `delete_user_device` — Delete a single device for user., `rename_user_device` — Set/clear a local alias for a user's HWID device (admin override)., `reset_user_devices` — Reset all devices for user., `delete_user` — Delete a user., `full_delete_user` — Full user deletion - removes from bot database AND Remnawave panel., `reset_user_trial` — Reset user trial - allows user to activate trial again., `reset_user_subscription` — Reset user subscription - removes/deactivates subscription., `disable_user` — Disable user - deactivates subscription and blocks access., `get_user_referrals` — Get list of users referred by this user., `get_user_transactions` — Get user transactions., `get_user_activity` — Таймлайн активности пользователя в боте и кабинете., `get_user_sync_status` — Get sync status between bot and panel for a user., `sync_user_from_panel` — Sync user data FROM panel TO bot., `sync_user_to_panel` — Sync user data FROM bot TO panel., `get_user_gifts` — Get all gift subscriptions sent and received by user.
- `app/cabinet/routes/admin_wheel.py` — Python-модуль
  Классы: нет
  Функции: `get_admin_wheel_config` — Получить полную конфигурацию колеса., `update_admin_wheel_config` — Обновить конфигурацию колеса., `get_prizes` — Получить список призов., `create_prize` — Создать новый приз., `update_prize` — Обновить приз., `delete_prize_endpoint` — Удалить приз., `reorder_prizes` — Переупорядочить призы., `get_statistics` — Получить статистику колеса., `get_all_spins_endpoint` — Получить все спины с фильтрами.
- `app/cabinet/routes/admin_withdrawals.py` — Python-модуль
  Классы: нет
  Функции: `list_withdrawals` — List all withdrawal requests., `get_withdrawal_detail` — Get detailed withdrawal request with risk analysis., `approve_withdrawal` — Approve a withdrawal request., `reject_withdrawal` — Reject a withdrawal request., `complete_withdrawal` — Mark a withdrawal as completed (money transferred).
- `app/cabinet/routes/auth.py` — Python-модуль
  Классы: нет
  Функции: `auth_telegram` — Authenticate using Telegram WebApp initData., `auth_telegram_widget` — Authenticate using Telegram Login Widget data., `auth_telegram_oidc` — Authenticate using Telegram OIDC id_token (popup flow)., `register_email` — Register/link email to existing Telegram account., `verify_email_merge` — Confirm an email account merge with the code mailed to the existing account., `register_email_standalone` — Register new account with email and password., `verify_email` — Verify email with token and return auth tokens., `resend_verification` — Resend verification email., `login_email` — Login with email and password., `refresh_token` — Refresh access token using refresh token., `logout` — Logout and revoke refresh token., `auto_login` — Auto-login using a short-lived JWT from guest purchase success page., `forgot_password` — Request password reset., `reset_password` — Reset password with token., `get_current_user` — Get current authenticated user info., `get_my_avatar` — Фото профиля Telegram для шапки кабинета., `get_my_permissions` — Get current user's RBAC permissions, roles, and level., `check_is_admin` — Check if current user is an admin (legacy config or RBAC)., `request_email_change` — Request email change., `verify_email_change` — Verify email change with code., `cancel_email_change` — Cancel pending email change., `get_email_change_status` — Get pending email change status., `request_deep_link_token` — Generate a one-time deep link auth token., `poll_deep_link_token` — Poll for deep link auth completion.
- `app/cabinet/routes/balance.py` — Python-модуль
  Классы: нет
  Функции: `get_balance` — Get current user's balance., `get_transactions` — Get transaction history., `get_payment_methods` — Get available payment methods for the current user., `create_stars_invoice` — Создать Telegram Stars invoice для пополнения баланса., `create_topup` — Create payment for balance top-up., `get_pending_payments` — Get user's pending payments for manual verification., `get_latest_payment_by_method` — Get user's most recent payment for a given method (any status, not just pending)., `get_pending_payment_details` — Get details of a specific pending payment., `check_payment_status` — Manually check and update payment status., `get_saved_cards` — Get user's saved payment methods (cards) for recurrent payments., `delete_saved_card` — Unlink (deactivate) a saved payment method.
- `app/cabinet/routes/branding.py` — Python-модуль
  Классы: `BrandingResponse`, `BrandingNameUpdate`, `ThemeColorsResponse`, `ThemeColorsUpdate`, `EnabledThemesResponse`, `EnabledThemesUpdate`, `AnimationEnabledResponse`, `AnimationEnabledUpdate`, `AnimationConfigResponse`, `AnimationConfigUpdate` (1 методов), `FullscreenEnabledResponse`, `FullscreenEnabledUpdate`, `EmailAuthEnabledResponse`, `EmailAuthEnabledUpdate`, `TelegramWidgetConfigResponse`, `LiteModeEnabledResponse`, `LiteModeEnabledUpdate`, `GiftEnabledResponse`, `GiftEnabledUpdate`, `OfflineConvGoal`, `AnalyticsCountersResponse`, `AnalyticsCountersUpdate`, `BotStartVideoResponse`, `YandexCidRequest`, `FooterEnabledResponse`, `FooterEnabledUpdate`
  Функции: `ensure_branding_dir` — Ensure branding directory exists., `set_setting_value` — Set a setting value in database., `get_logo_path` — Get the path to the custom logo file (any supported format)., `has_custom_logo` — Check if a custom logo exists., `get_branding` — Get current branding settings., `get_logo` — Get the custom logo image., `get_favicon` — Иконка для <link rel="icon"> кабинета: логотип из админки или монограмма., `get_bot_logo` — Get the BOT's menu logo (settings.LOGO_FILE, e.g. vpn_logo.png)., `get_bot_start_video` — Текущее видео стартового меню бота (Telegram file_id)., `upload_bot_start_video` — Загружает видео, которое бот прикрепляет к стартовому меню., `delete_bot_start_video` — Убирает видео — меню возвращается к фото-логотипу/тексту., `update_branding_name` — Update the project name. Admin only. Empty name allowed (logo only mode)., `upload_logo` — Upload a custom logo. Admin only., `delete_logo` — Delete custom logo and revert to letter. Admin only., `validate_hex_color` — Validate hex color format., `get_theme_colors` — Get current theme colors., `update_theme_colors` — Update theme colors. Admin only. Partial update supported., `reset_theme_colors` — Reset theme colors to defaults. Admin only., `get_enabled_themes` — Get which themes are enabled., `update_enabled_themes` — Update which themes are enabled. Admin only. At least one theme must be enabled., `get_animation_enabled` — Get animation enabled setting., `update_animation_enabled` — Update animation enabled setting. Admin only., `get_animation_config` — Get full animation config. Public endpoint., `update_animation_config` — Update animation config (partial update). Admin only., `get_fullscreen_enabled` — Get fullscreen enabled setting., `update_fullscreen_enabled` — Update fullscreen enabled setting. Admin only., `get_email_auth_enabled` — Get email auth enabled setting., `update_email_auth_enabled` — Update email auth enabled setting. Admin only., `get_telegram_widget_config` — Get Telegram Login Widget configuration., `get_analytics_counters` — Get analytics counter settings., `update_analytics_counters` — Update analytics counter settings. Admin only. Partial update supported., `store_yandex_cid` — Store Yandex Metrika ClientID for the authenticated cabinet user., `get_lite_mode_enabled` — Get lite mode enabled setting., `update_lite_mode_enabled` — Update lite mode enabled setting. Admin only., `get_gift_enabled` — Get gift feature enabled setting. Public endpoint., `update_gift_enabled` — Update gift feature enabled setting. Admin only., `get_footer_enabled` — Get legal footer enabled setting. Public endpoint - no authentication required., `update_footer_enabled` — Update legal footer enabled setting. Admin only.
- `app/cabinet/routes/contests.py` — Python-модуль
  Классы: `ContestInfo`, `ContestGameData`, `ContestAnswerRequest`, `ContestResult`, `ContestsCountResponse`
  Функции: `get_contests_count` — Get count of contests available for the user., `get_contests` — Get list of available contests/games., `get_contest_game` — Get game data for a specific contest round., `submit_contest_answer` — Submit answer for a contest round.
- `app/cabinet/routes/coupon.py` — Python-модуль
  Классы: нет
  Функции: `redeem_coupon_endpoint` — Redeem a one-time coupon for the current cabinet user., `coupon_status` — Public info about a still-redeemable coupon (no authentication).
- `app/cabinet/routes/gift.py` — Python-модуль
  Классы: нет
  Функции: `get_gift_config` — Get gift subscription configuration: tariffs, payment methods, balance., `create_gift_purchase` — Create a gift subscription purchase from the cabinet., `get_pending_gifts` — Get pending gift purchases that the current user can activate., `get_gift_purchase_status` — Get the status of a cabinet gift purchase., `get_sent_gifts` — Get all gifts the current user has sent., `get_received_gifts` — Get all gifts the current user has received., `activate_gift_by_code` — Activate a gift subscription by its code (token).
- `app/cabinet/routes/info.py` — Python-модуль
  Классы: `FaqPageResponse`, `RulesResponse`, `PrivacyPolicyResponse`, `PublicOfferResponse`, `RecurrentPaymentsResponse`, `ServiceInfoResponse`, `SupportConfigResponse`, `InfoVisibilityResponse`, `LegalConsentConfigResponse`
  Функции: `get_faq_pages` — Get list of FAQ pages., `get_faq_page` — Get a specific FAQ page by ID., `get_rules` — Get service rules - uses same function as bot., `get_privacy_policy` — Get privacy policy., `get_public_offer` — Get public offer., `get_recurrent_payments` — Get recurring-payments terms document., `get_service_info` — Get general service information., `get_available_languages` — Get list of available languages., `get_user_language` — Get current user's language., `update_user_language` — Update user's language preference., `get_support_config` — Get support/tickets configuration for cabinet., `get_legal_consent_config` — Нужны ли новому пользователю галочки «ознакомлен» и с чем именно., `get_info_visibility`
- `app/cabinet/routes/info_pages.py` — Python-модуль
  Классы: нет
  Функции: `list_active_info_pages` — Get all active info pages (public, no auth required)., `get_info_page_tab_replacements` — Get tab replacement mapping (public, no auth required)., `get_info_page_by_slug_public` — Get a single info page by slug (public, no auth required).
- `app/cabinet/routes/landing.py` — Python-модуль
  Классы: `LandingFeature`, `LandingTariffPeriod`, `LandingTariff`, `LandingPaymentMethodSubOption`, `LandingPaymentMethod`, `LandingDiscountInfo`, `LandingConfigResponse`, `PurchaseRequest` (1 методов), `PurchaseResponse`, `PurchaseStatusResponse`, `GiftClaimRequest` (1 методов), `GiftClaimResponse`
  Функции: `get_purchase_status` — Get the status of a guest purchase by token., `activate_purchase` — Activate a pending guest purchase, replacing the user's current subscription., `get_gift_claim` — Public gift claim page data (tariff, period, message, claim links)., `claim_gift` — Web (email) arm of the channel-agnostic gift claim., `get_landing_config` — Get public landing page configuration with tariffs and payment methods., `create_landing_purchase` — Create a guest purchase on a landing page.
- `app/cabinet/routes/media.py` — Python-модуль
  Классы: `MediaUploadResponse`
  Функции: `make_media_token` — Signed, expiring token authorizing download of `file_id`., `upload_media` — Upload media file for use in ticket messages., `download_media` — Download media file by file_id.
- `app/cabinet/routes/news.py` — Python-модуль
  Классы: нет
  Функции: `list_categories` — Get list of distinct news categories., `list_published_news` — Get paginated list of published news articles., `get_article_by_slug` — Get a single published news article by slug. Increments view count.
- `app/cabinet/routes/notifications.py` — Python-модуль
  Классы: `NotificationSettingsResponse`, `NotificationSettingsUpdate`
  Функции: `get_notification_settings` — Get user's notification settings., `update_notification_settings` — Update user's notification settings., `send_test_notification` — Send a test notification to the user., `get_notification_history` — Get user's notification history.
- `app/cabinet/routes/oauth.py` — Python-модуль
  Классы: `OAuthProviderInfo`, `OAuthProvidersResponse`, `OAuthAuthorizeResponse`, `OAuthCallbackRequest`
  Функции: `get_oauth_providers` — Get list of enabled OAuth providers., `get_oauth_authorize_url` — Get authorization URL for an OAuth provider., `oauth_callback` — Handle OAuth callback: exchange code, find/create user, return JWT.
- `app/cabinet/routes/partner_application.py` — Python-модуль
  Классы: нет
  Функции: `get_partner_status` — Get partner status and latest application for current user., `get_campaign_stats` — Get detailed stats for a single campaign belonging to the current partner., `apply_for_partner` — Submit partner application.
- `app/cabinet/routes/polls.py` — Python-модуль
  Классы: `PollOptionResponse`, `PollQuestionResponse`, `PollInfo`, `PollStartResponse`, `AnswerRequest`, `AnswerResponse`, `PollsCountResponse`
  Функции: `get_polls_count` — Get count of polls available for the user., `get_available_polls` — Get list of polls available for the user., `get_poll_details` — Get details of a specific poll response., `start_poll` — Start or continue a poll., `answer_question` — Submit answer for a poll question.
- `app/cabinet/routes/promo.py` — Python-модуль
  Классы: `PromoOfferInfo`, `ActiveDiscountInfo`, `ClaimOfferRequest`, `ClaimOfferResponse`, `PromoGroupDiscounts`, `LoyaltyTierInfo`, `LoyaltyTiersResponse`
  Функции: `get_promo_offers` — Get list of available promo offers for the user., `get_active_discount` — Get user's currently active discount., `get_promo_group_discounts` — Get user's promo group discounts., `get_loyalty_tiers` — Get all loyalty tiers (promo groups with auto-assign thresholds) and user's progress., `claim_promo_offer` — Claim a promo offer., `clear_active_discount` — Clear user's active discount.
- `app/cabinet/routes/promocode.py` — Python-модуль
  Классы: `PromocodeActivateRequest`, `PromocodeActivateResponse`, `PromocodeDeactivateResponse`
  Функции: `activate_promocode` — Activate a promo code for the current user., `deactivate_discount_promocode` — Deactivate the currently active discount promo code for the current user.
- `app/cabinet/routes/referral.py` — Python-модуль
  Классы: нет
  Функции: `get_referral_info` — Get referral program info for current user., `get_referral_list` — Get list of invited users., `get_referral_earnings` — Get referral earnings history., `update_reward_choice` — Сохранить, что получать и куда класть дни., `get_referral_terms` — Get referral program terms.
- `app/cabinet/routes/site_verification.py` — Python-модуль
  Классы: нет
  Функции: `get_site_verification` — Return all configured site-verification tokens.
- `app/cabinet/routes/subscription.py` — Python-модуль
  Классы: нет
  Функции: `get_subscription`
- `app/cabinet/routes/subscription_modules/`
- `app/cabinet/routes/support_ws.py` — Python-модуль
  Классы: `WsUserContext` (2 методов), `UploadTransfer` (1 методов), `DownloadTransfer` (1 методов), `SupportWsSession` (2 методов), `SupportWsManager` (4 методов)
  Функции: `support_mobile_websocket_endpoint` — Mobile-only support ticket command WebSocket., `register_support_ticket_event_bridge` — Attach the support-socket bridge to the global event emitter (idempotent).
- `app/cabinet/routes/ticket_notifications.py` — Python-модуль
  Классы: `TicketNotificationResponse`, `TicketNotificationListResponse`, `UnreadCountResponse`
  Функции: `get_user_notifications` — Get ticket notifications for current user., `get_user_unread_count` — Get unread notifications count for current user., `mark_notification_as_read` — Mark a notification as read., `mark_all_notifications_as_read` — Mark all notifications as read for current user., `mark_ticket_notifications_as_read` — Mark all notifications for a specific ticket as read., `get_admin_notifications` — Get ticket notifications for admins., `get_admin_unread_count` — Get unread notifications count for admins., `mark_admin_notification_as_read` — Mark an admin notification as read., `mark_all_admin_notifications_as_read` — Mark all admin notifications as read., `mark_admin_ticket_notifications_as_read` — Mark all admin notifications for a specific ticket as read.
- `app/cabinet/routes/tickets.py` — Python-модуль
  Классы: нет
  Функции: `get_tickets` — Get user's support tickets., `create_ticket` — Create a new support ticket., `get_ticket` — Get ticket with all messages., `add_ticket_message` — Add message to existing ticket.
- `app/cabinet/routes/unsubscribe.py` — Python-модуль
  Классы: нет
  Функции: `unsubscribe_page` — Ничего не меняет — только отдаёт самоотправляющуюся форму., `unsubscribe_one_click` — Единственное место, где отписка применяется.
- `app/cabinet/routes/websocket.py` — Python-модуль
  Классы: `CabinetConnectionManager` (5 методов)
  Функции: `verify_cabinet_ws_token` — Проверить JWT токен для WebSocket., `cabinet_websocket_endpoint` — WebSocket endpoint для real-time уведомлений кабинета., `notify_user_ticket_reply` — Уведомить пользователя об ответе в тикете., `notify_admins_new_ticket` — Уведомить админов о новом тикете., `notify_admins_ticket_reply` — Уведомить админов об ответе пользователя., `notify_user_balance_topup` — Уведомить пользователя о пополнении баланса., `notify_user_balance_change` — Уведомить пользователя об изменении баланса., `notify_user_subscription_activated` — Уведомить пользователя об активации подписки., `notify_user_subscription_expiring` — Уведомить пользователя о скором истечении подписки., `notify_user_subscription_expired` — Уведомить пользователя об истечении подписки., `notify_user_subscription_renewed` — Уведомить пользователя о продлении подписки., `notify_user_devices_purchased` — Уведомить пользователя о покупке устройств., `notify_user_traffic_purchased` — Уведомить пользователя о покупке трафика., `notify_user_autopay_success` — Уведомить пользователя об успешном автопродлении., `notify_user_autopay_failed` — Уведомить пользователя о неудачном автопродлении., `notify_user_autopay_insufficient_funds` — Уведомить о недостатке средств для автопродления., `notify_user_ban` — Уведомить пользователя о блокировке., `notify_user_unban` — Уведомить пользователя о разблокировке., `notify_user_warning` — Уведомить пользователя о предупреждении., `notify_user_referral_bonus` — Уведомить пользователя о реферальном бонусе., `notify_user_referral_registered` — Уведомить пользователя о регистрации нового реферала., `notify_user_daily_debit` — Уведомить о ежедневном списании., `notify_user_traffic_reset` — Уведомить о сбросе трафика., `notify_user_payment_received` — Уведомить о полученном платеже.
- `app/cabinet/routes/wheel.py` — Python-модуль
  Классы: `StarsInvoiceResponse`
  Функции: `get_wheel_config` — Получить конфигурацию колеса удачи., `check_spin_availability` — Проверить доступность спина., `spin_wheel` — Крутить колесо удачи., `get_spin_history` — Получить историю спинов пользователя., `create_stars_invoice` — Создать Telegram Stars invoice для оплаты спина колеса.
- `app/cabinet/routes/withdrawal.py` — Python-модуль
  Классы: нет
  Функции: `get_withdrawal_balance` — Get withdrawal balance stats for current user., `create_withdrawal` — Create a withdrawal request., `get_withdrawal_history` — Get user's withdrawal request history., `cancel_withdrawal` — Cancel a pending withdrawal request.

##### app/cabinet/routes/subscription_modules

- `app/cabinet/routes/subscription_modules/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/cabinet/routes/subscription_modules/autopay.py` — Python-модуль
  Классы: нет
  Функции: `update_autopay` — Update autopay settings.
- `app/cabinet/routes/subscription_modules/daily.py` — Python-модуль
  Классы: нет
  Функции: `toggle_subscription_pause` — Toggle pause/resume for daily subscription.
- `app/cabinet/routes/subscription_modules/devices.py` — Python-модуль
  Классы: `DeviceRenameRequest`
  Функции: `purchase_devices_legacy` — Purchase additional device slots (legacy endpoint)., `purchase_devices` — Purchase additional device slots for subscription., `save_devices_cart` — Save cart for device purchase (for insufficient balance flow)., `get_device_price` — Get price for additional devices., `get_devices` — Get list of connected devices., `rename_device` — Set/clear a local alias for the user's HWID device., `delete_device` — Delete a specific device by HWID., `delete_all_devices` — Delete all connected devices., `get_device_reduction_info` — Get info about device limit reduction availability., `reduce_devices` — Reduce device limit (no refund).
- `app/cabinet/routes/subscription_modules/helpers.py` — Python-модуль
  Классы: нет
  Функции: `resolve_subscription` — Resolve target subscription: by ID in multi-tariff mode, or legacy fallback.
- `app/cabinet/routes/subscription_modules/lava_recurrent.py` — Python-модуль
  Классы: нет
  Функции: `enable_lava_recurrent` — Включает автопродление Lava для выбранной подписки., `purchase_with_lava_recurrent` — Оформление подписки на тариф оплатой привязкой Lava., `get_lava_recurrent` — Текущее состояние автопродления Lava для подписки., `cancel_lava_recurrent` — Отменяет автопродление Lava (best-effort).
- `app/cabinet/routes/subscription_modules/multi_tariff.py` — Python-модуль
  Классы: `SubscriptionListItem`, `SubscriptionsListResponse`
  Функции: `list_subscriptions` — List all user subscriptions. Returns all subscriptions regardless of multi-tariff mode., `get_subscription_detail` — Get specific subscription details with ownership check., `delete_subscription` — Delete an expired/disabled subscription. Active subscriptions cannot be deleted.
- `app/cabinet/routes/subscription_modules/platega_recurrent.py` — Python-модуль
  Классы: нет
  Функции: `enable_platega_recurrent` — Enable Platega SBP auto-renewal for the resolved subscription., `purchase_with_platega_recurrent` — Оформление подписки на тариф через СБП-автопродление (оплата привязкой)., `get_platega_recurrent` — Return the current Platega SBP auto-renewal state for the subscription., `cancel_platega_recurrent` — Cancel Platega SBP auto-renewal for the resolved subscription (best-effort).
- `app/cabinet/routes/subscription_modules/purchase.py` — Python-модуль
  Классы: нет
  Функции: `get_purchase_options` — Get all subscription purchase options (periods, servers, traffic, devices)., `preview_purchase` — Calculate and preview the total price for selected options (classic mode only)., `submit_purchase` — Submit subscription purchase (deduct from balance, classic mode only)., `purchase_tariff` — Purchase a tariff (for tariffs mode)., `get_trial_info` — Get trial subscription info and availability., `activate_trial` — Activate trial subscription.
- `app/cabinet/routes/subscription_modules/renewal.py` — Python-модуль
  Классы: нет
  Функции: `get_renewal_options` — Get available subscription renewal options with prices., `renew_subscription` — Renew subscription (pay from balance).
- `app/cabinet/routes/subscription_modules/revoke.py` — Python-модуль
  Классы: нет
  Функции: `revoke_subscription` — Revoke and reissue subscription (generate new connection link).
- `app/cabinet/routes/subscription_modules/servers.py` — Python-модуль
  Классы: нет
  Функции: `get_available_countries` — Get available countries/servers for the user., `update_countries` — Update subscription countries/servers.
- `app/cabinet/routes/subscription_modules/status.py` — Python-модуль
  Классы: нет
  Функции: `get_subscription` — Get current user's subscription details., `get_connection_link` — Get subscription connection link and instructions., `get_happ_downloads` — Get hApp download links for different platforms., `get_app_config` — Get app configuration for connection with deep links.
- `app/cabinet/routes/subscription_modules/tariff_switch.py` — Python-модуль
  Классы: нет
  Функции: `preview_tariff_switch` — Preview tariff switch - shows cost calculation., `switch_tariff` — Switch to a different tariff without changing end date.
- `app/cabinet/routes/subscription_modules/traffic.py` — Python-модуль
  Классы: нет
  Функции: `get_traffic_packages` — Get available traffic packages., `purchase_traffic` — Purchase additional traffic., `save_traffic_cart` — Save cart for traffic purchase (for insufficient balance flow)., `switch_traffic_package` — Switch to a different traffic package (change limit)., `refresh_traffic` — Refresh traffic usage from RemnaWave panel.

#### app/cabinet/schemas

- `app/cabinet/schemas/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/cabinet/schemas/apple_iap.py` — Python-модуль
  Классы: `ApplePurchaseRequest` (1 методов), `ApplePurchaseResponse`, `AppleAccountTokenResponse`
  Функции: нет
- `app/cabinet/schemas/auth.py` — Python-модуль
  Классы: `TelegramAuthRequest`, `TelegramWidgetAuthRequest`, `TelegramOIDCAuthRequest`, `EmailRegisterRequest`, `EmailVerifyRequest`, `EmailLoginRequest`, `RefreshTokenRequest`, `PasswordForgotRequest`, `PasswordResetRequest`, `AutoLoginRequest`, `TokenResponse`, `UserResponse`, `UserAvatarResponse`, `EmailRegisterStandaloneRequest`, `CampaignBonusInfo`, `AuthResponse`, `RegisterResponse`, `EmailChangeRequest`, `EmailChangeVerifyRequest`, `EmailMergeVerifyRequest`, `EmailChangeResponse`, `DeepLinkTokenResponse`, `DeepLinkPollRequest`
  Функции: нет
- `app/cabinet/schemas/balance.py` — Python-модуль
  Классы: `BalanceResponse`, `TransactionResponse`, `TransactionListResponse`, `PaymentOptionResponse`, `PaymentMethodResponse`, `TopUpRequest`, `TopUpResponse`, `StarsInvoiceRequest`, `StarsInvoiceResponse`, `PendingPaymentResponse`, `PendingPaymentListResponse`, `ManualCheckResponse`, `SavedCardResponse`, `SavedCardsListResponse`
  Функции: нет
- `app/cabinet/schemas/ban_system.py` — Python-модуль
  Классы: `BanSystemStatusResponse`, `BanSystemStatsResponse`, `BanUserIPInfo`, `BanUserRequestLog`, `BanUserListItem`, `BanUsersListResponse`, `BanUserDetailResponse`, `BanPunishmentItem`, `BanPunishmentsListResponse`, `BanHistoryResponse`, `BanUserRequest`, `UnbanResponse`, `BanNodeItem`, `BanNodesListResponse`, `BanAgentItem`, `BanAgentsSummary`, `BanAgentsListResponse`, `BanTrafficStats`, `BanTrafficUserItem`, `BanTrafficViolationItem`, `BanTrafficViolationsResponse`, `BanTrafficTopItem`, `BanTrafficResponse`, `BanSettingDefinition`, `BanSettingsResponse`, `BanSettingUpdateRequest`, `BanWhitelistRequest`, `BanReportTopViolator`, `BanReportResponse`, `BanHealthComponent`, `BanHealthResponse`, `BanHealthDetailedResponse`, `BanAgentHistoryItem`, `BanAgentHistoryResponse`
  Функции: нет
- `app/cabinet/schemas/broadcasts.py` — Python-модуль
  Классы: `BroadcastFilter`, `TariffFilter`, `BroadcastFiltersResponse`, `TariffForBroadcast`, `BroadcastTariffsResponse`, `BroadcastButton`, `BroadcastButtonsResponse`, `CustomBroadcastButton` (1 методов), `BroadcastMediaRequest`, `BroadcastCreateRequest`, `BroadcastResponse`, `BroadcastListResponse`, `BroadcastPreviewRequest`, `BroadcastPreviewResponse`, `EmailFilterItem`, `EmailFiltersResponse`, `CombinedBroadcastCreateRequest`, `EmailPreviewRequest`, `EmailPreviewResponse`, `EmailRenderRequest`, `EmailRenderResponse`
  Функции: нет
- `app/cabinet/schemas/bulk_actions.py` — Python-модуль
  Классы: `BulkActionType`, `BulkActionParams`, `BulkSubscriptionInfo`, `BulkExecuteRequest` (1 методов), `BulkUserResult`, `BulkExecuteResponse`
  Функции: нет
- `app/cabinet/schemas/campaigns.py` — Python-модуль
  Классы: `TariffInfo`, `CampaignListItem`, `CampaignListResponse`, `CampaignDetailResponse`, `CampaignCreateRequest`, `CampaignUpdateRequest`, `CampaignToggleResponse`, `CampaignStatisticsResponse`, `CampaignRegistrationItem`, `CampaignRegistrationsResponse`, `CampaignsOverviewResponse`, `AvailablePartnerItem`, `ServerSquadInfo`, `AdminDailyStatItem`, `AdminPeriodStats`, `AdminPeriodChange`, `AdminPeriodComparison`, `AdminTopRegistrationItem`, `AdminCampaignChartDataResponse`
  Функции: нет
- `app/cabinet/schemas/channel.py` — Python-модуль
  Классы: `ChannelResponse`, `ChannelListResponse`, `ChannelCreateRequest` (2 методов), `ChannelUpdateRequest` (2 методов), `ChannelSubscriptionStatus`
  Функции: нет
- `app/cabinet/schemas/coupons.py` — Python-модуль
  Классы: `CouponBatchResponse`, `CouponBatchListResponse`, `CouponBatchCreateRequest`, `CouponBatchCreatedResponse`, `CouponBatchLinksResponse`, `CouponBatchRevokeResponse`, `CouponRedeemRequest`, `CouponRedeemResponse`, `CouponStatusResponse`, `CouponBatchDeleteResponse`
  Функции: нет
- `app/cabinet/schemas/gift.py` — Python-модуль
  Классы: `GiftConfigSubOption`, `GiftConfigTariffPeriod`, `GiftConfigTariff`, `GiftConfigPaymentMethod`, `GiftConfigResponse`, `GiftPurchaseRequest` (1 методов), `GiftPurchaseResponse`, `GiftPurchaseStatusResponse`, `PendingGiftResponse`, `SentGiftResponse`, `ReceivedGiftResponse`, `ActivateGiftRequest`, `ActivateGiftResponse`
  Функции: нет
- `app/cabinet/schemas/info_pages.py` — Python-модуль
  Классы: `InfoPageResponse`, `InfoPageListItem`, `InfoPageCreateRequest`, `InfoPageUpdateRequest`, `ReorderItem`, `ReorderRequest`
  Функции: нет
- `app/cabinet/schemas/media.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/cabinet/schemas/news.py` — Python-модуль
  Классы: `NewsArticleResponse`, `NewsArticleListItem`, `NewsListResponse`, `NewsCreateRequest` (5 методов), `NewsUpdateRequest` (4 методов), `NewsToggleResponse`
  Функции: нет
- `app/cabinet/schemas/news_categories.py` — Python-модуль
  Классы: `NewsCategoryCreate` (1 методов), `NewsCategoryUpdate` (1 методов), `NewsCategoryResponse`
  Функции: нет
- `app/cabinet/schemas/news_media.py` — Python-модуль
  Классы: `NewsMediaUploadResponse`
  Функции: нет
- `app/cabinet/schemas/news_tags.py` — Python-модуль
  Классы: `NewsTagCreate` (1 методов), `NewsTagUpdate` (1 методов), `NewsTagResponse`
  Функции: нет
- `app/cabinet/schemas/partners.py` — Python-модуль
  Классы: `PartnerApplicationRequest`, `PartnerApplicationInfo`, `PartnerCampaignInfo`, `PartnerStatusResponse`, `DailyStatItem`, `PeriodStats`, `PeriodChange`, `PeriodComparison`, `CampaignReferralItem`, `PartnerCampaignDetailedStats`, `AdminPartnerApplicationItem`, `AdminPartnerApplicationsResponse`, `AdminApproveRequest`, `AdminRejectRequest`, `AdminPartnerItem`, `AdminPartnerListResponse`, `CampaignSummary`, `AdminPartnerDetailResponse`, `AdminUpdateCommissionRequest`
  Функции: нет
- `app/cabinet/schemas/pinned_messages.py` — Python-модуль
  Классы: `PinnedMessageMedia`, `PinnedMessageCreateRequest`, `PinnedMessageUpdateRequest`, `PinnedMessageSettingsRequest`, `PinnedMessageResponse`, `PinnedMessageBroadcastResponse`, `PinnedMessageUnpinResponse`, `PinnedMessageListResponse`
  Функции: нет
- `app/cabinet/schemas/reachability.py` — Python-модуль
  Классы: `TargetIn` (1 методов), `ProbesIn`, `JobCreateRequest` (1 методов), `BatchCreateRequest` (1 методов), `ParseInputRequest`, `PrefUpdateRequest`, `UnitOut`, `UnitsResponse`, `ActiveJobOut`, `ReferenceOut`, `ActiveBatchOut`, `StatusResponse`, `HostTargetOut`, `HostsResponse`, `NodeTargetOut`, `NodesResponse`, `ConfigOut`, `RejectedOut`, `SubscriptionConfigsResponse`, `SourceOut`, `ParsedConfigOut`, `ParsedInputResponse`, `PrefOut`, `SkippedOut`, `TargetOut`, `PreviewResponse`, `LegOut`, `JobOut`, `JobListResponse`, `BatchPreviewResponse`, `BatchJobOut`, `BatchOut`, `BatchListResponse`, `CellOut`, `SummaryRow`, `SummaryResponse`
  Функции: нет
- `app/cabinet/schemas/referral.py` — Python-модуль
  Классы: `ReferralInfoResponse`, `ReferralItemResponse`, `ReferralListResponse`, `ReferralEarningResponse`, `ReferralEarningsListResponse`, `ReferralDaysTargetOption`, `ReferralRewardChoiceRequest`, `ReferralProgramLevel`, `ReferralTermsResponse`, `ReferralRewardLevelResponse`, `ReferralRewardTariffOption`, `ReferralRewardLevelsResponse`, `ReferralRewardLevelUpdateRequest`, `ReferralSchemeUpdateRequest`, `ReferralDepthUpdateRequest`, `ReferralLevelsModeUpdateRequest`
  Функции: нет
- `app/cabinet/schemas/remnawave.py` — Python-модуль
  Классы: `ConnectionStatus`, `RemnaWaveStatusResponse`, `SystemSummary`, `ServerInfo`, `Bandwidth`, `TrafficPeriod`, `TrafficPeriods`, `SystemStatsResponse`, `NodeInfo`, `NodesListResponse`, `NodesOverview`, `NodeStatisticsResponse`, `NodeUsageResponse`, `NodeActionRequest`, `NodeActionResponse`, `GeocheckRequest` (4 методов), `GeocheckStartResponse`, `GeocheckImage`, `GeocheckResult`, `GeocheckJobResponse`, `SquadInfo`, `SquadWithLocalInfo`, `SquadsListResponse`, `SquadDetailResponse`, `SquadCreateRequest`, `SquadUpdateRequest`, `SquadActionRequest`, `SquadOperationResponse`, `MigrationPreviewResponse`, `MigrationRequest`, `MigrationStats`, `MigrationResponse`, `InboundInfo`, `InboundsListResponse`, `AutoSyncTime`, `AutoSyncStatus`, `AutoSyncToggleRequest`, `AutoSyncRunResponse`, `SyncMode`, `SyncResponse`, `SyncRecommendations`, `RecapTotal`, `RecapThisMonth`, `RecapResponse`, `PlatformCount`, `AppCount`, `DeviceTopUser`, `DevicesStatsResponse`, `TopConsumer`, `TopConsumersResponse`, `HealthResponse`, `SubscriptionRequestStatsResponse`
  Функции: нет
- `app/cabinet/schemas/servers.py` — Python-модуль
  Классы: `PromoGroupInfo`, `ServerListItem`, `ServerListResponse`, `ServerDetailResponse`, `ServerUpdateRequest`, `ServerToggleResponse`, `ServerTrialToggleResponse`, `ServerStatsResponse`, `ServerSyncResponse`, `ServerSyncRequest`
  Функции: нет
- `app/cabinet/schemas/subscription.py` — Python-модуль
  Классы: `ServerInfo`, `TrafficPurchaseInfo`, `SubscriptionData`, `SubscriptionStatusResponse`, `RenewalOptionResponse`, `RenewalRequest`, `TrafficPackageResponse`, `TrafficPurchaseRequest`, `DevicePurchaseRequest`, `AutopayUpdateRequest`, `TrialActivateRequest`, `TrialInfoResponse`, `PurchaseSelectionRequest`, `PurchasePreviewRequest`, `TariffPurchaseRequest`
  Функции: нет
- `app/cabinet/schemas/tariffs.py` — Python-модуль
  Классы: `PeriodPrice` (1 методов), `ServerTrafficLimit`, `ServerInfo`, `PromoGroupInfo`, `TariffListItem`, `TariffListResponse`, `TariffDetailResponse`, `ExternalSquadInfoResponse`, `TariffCreateRequest`, `TariffUpdateRequest`, `TariffSortOrderRequest`, `TariffToggleResponse`, `TariffTrialResponse`, `TariffStatsResponse`, `SyncSquadsResponse`
  Функции: нет
- `app/cabinet/schemas/tickets.py` — Python-модуль
  Классы: `TicketMediaItem` (1 методов), `TicketMessageResponse`, `TicketResponse`, `TicketDetailResponse`, `TicketListResponse`, `TicketCreateRequest` (1 методов), `TicketMessageCreateRequest` (1 методов)
  Функции: нет
- `app/cabinet/schemas/traffic.py` — Python-модуль
  Классы: `TrafficNodeInfo`, `SubscriptionTrafficInfo`, `UserTrafficItem`, `TrafficUsageResponse`, `SubscriptionEnrichmentInfo`, `UserTrafficEnrichment`, `TrafficEnrichmentResponse`, `ExportCsvRequest`, `ExportCsvResponse`
  Функции: нет
- `app/cabinet/schemas/users.py` — Python-модуль
  Классы: `UserStatusEnum`, `SubscriptionStatusEnum`, `SortByEnum`, `TrafficPurchaseItem`, `UserSubscriptionInfo`, `UserPromoGroupInfo`, `SubscriptionListItem`, `UserListItem`, `UsersListResponse`, `UserByRemnawaveResponse`, `UserTransactionItem`, `UserActivityItem`, `UserActivityResponse`, `UserReferralInfo`, `UserDetailResponse`, `UserPanelInfoResponse`, `UserNodeUsageItem`, `UserNodeUsageResponse`, `UpdateBalanceRequest`, `UpdateBalanceResponse`, `UpdateSubscriptionRequest`, `UpdateSubscriptionResponse`, `UpdateUserStatusRequest`, `UpdateUserStatusResponse`, `SendUserMessageRequest`, `SendUserMessageResponse`, `UpdateRestrictionsRequest`, `UpdateRestrictionsResponse`, `UpdatePromoGroupRequest`, `UpdatePromoGroupResponse`, `UpdateReferralCommissionRequest`, `UpdateReferralCommissionResponse`, `AssignReferrerRequest`, `AssignReferrerResponse`, `RemoveReferrerResponse`, `RemoveReferralResponse`, `DeviceInfo`, `UserDevicesResponse`, `DeleteDeviceResponse`, `RenameDeviceRequest`, `RenameDeviceResponse`, `ResetDevicesResponse`, `DeleteUserRequest`, `DeleteUserResponse`, `UsersStatsResponse`, `UserSearchRequest`, `PeriodPriceInfo`, `UserAvailableTariffItem`, `UserAvailableTariffsResponse`, `PanelUserInfo`, `SyncFromPanelRequest`, `SyncFromPanelResponse`, `SyncToPanelRequest`, `SyncToPanelResponse`, `PanelSyncStatusResponse`, `FullDeleteUserRequest`, `FullDeleteUserResponse`, `ResetTrialRequest`, `ResetTrialResponse`, `ResetSubscriptionRequest`, `ResetSubscriptionResponse`, `DisableUserRequest`, `DisableUserResponse`, `AdminUserGiftItem`, `AdminUserGiftsResponse`
  Функции: нет
- `app/cabinet/schemas/wheel.py` — Python-модуль
  Классы: `WheelPaymentType`, `WheelPrizeType`, `WheelPrizeDisplay`, `WheelConfigResponse`, `SpinAvailabilityResponse`, `SpinRequest`, `SpinResultResponse`, `SpinHistoryItem`, `SpinHistoryResponse`, `WheelPrizeAdminResponse`, `AdminWheelConfigResponse`, `UpdateWheelConfigRequest`, `CreatePrizeRequest`, `UpdatePrizeRequest`, `ReorderPrizesRequest`, `AdminSpinItem`, `AdminSpinsResponse`, `WheelStatisticsResponse`
  Функции: нет
- `app/cabinet/schemas/withdrawals.py` — Python-модуль
  Классы: `WithdrawalBalanceResponse`, `WithdrawalCreateRequest`, `WithdrawalItemResponse`, `WithdrawalListResponse`, `WithdrawalCreateResponse`, `AdminWithdrawalItem`, `AdminWithdrawalListResponse`, `AdminWithdrawalDetailResponse`, `AdminApproveWithdrawalRequest`, `AdminRejectWithdrawalRequest`
  Функции: нет

#### app/cabinet/services

- `app/cabinet/services/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/cabinet/services/email_layout.py` — Python-модуль
  Классы: нет
  Функции: `layout_is_valid` — Обёртка без {content} проглотила бы тело каждого письма., `unsubscribe_block` — Ссылка отписки — только у маркетинговых писем, у остальных пусто., `build_layout_context` — Реальные значения плейсхолдеров обёртки для языка., `render_email_layout` — Подставляет плейсхолдеры обёртки. Недостающие берутся из build_layout_context., `extract_content_fragment` — Тело письма из готового HTML по маркерам, None если маркеров нет., `reset_email_layout_cache`, `set_cached_email_layouts` — Заменяет кэш; обёртки без {content} отбрасываются с предупреждением., `get_cached_email_layout` — Обёртка для языка; нет своей — русская; нет и её — None (встроенная)., `resolve_email_layout`, `is_layout_cache_stale`, `refresh_email_layout_cache` — Подтягивает сохранённые обёртки, если кэш устарел (или force после сохранения).
- `app/cabinet/services/email_service.py` — Python-модуль
  Классы: `EmailService` (24 методов)
  Функции: нет
- `app/cabinet/services/email_template_overrides.py` — Python-модуль
  Классы: нет
  Функции: `apply_legacy_aliases` — Возвращает копию контекста со старыми именами плейсхолдеров, если есть новые., `build_common_context` — Values for the type-independent placeholders., `substitute_context_vars` — Replace {var} placeholders in template text with context values., `get_template_override` — Get custom email template from the database., `get_all_overrides` — Get all custom template overrides from the database., `get_overrides_for_type` — Get all language overrides for a specific notification type., `save_template_override` — Save or update a custom email template in the database., `get_rendered_override` — Get a custom template override rendered with the base email template., `delete_template_override` — Delete a custom template override (revert to default).
- `app/cabinet/services/email_templates.py` — Python-модуль
  Классы: `EmailNotificationTemplates` (46 методов)
  Функции: нет
- `app/cabinet/services/email_type_switch.py` — Python-модуль
  Классы: нет
  Функции: `can_disable_email_type`, `is_email_type_enabled` — False — письмо этого типа отключено админом; отправитель молча пропускает его., `set_email_type_enabled` — Включает/выключает тип письма; возвращает новый набор отключённых.
- `app/cabinet/services/email_unsubscribe.py` — Python-модуль
  Классы: нет
  Функции: `build_token` — Собирает токен отписки для конкретного адреса., `parse_token` — Достаёт (user_id, category) БЕЗ проверки подписи., `verify_token` — Проверяет подпись токена против текущего адреса пользователя., `build_unsubscribe_url` — Публичная ссылка отписки. Пустая строка = отписки в этом письме не будет., `build_unsubscribe_mailto` — mailto-вариант для клиентов без HTTP one-click. Пусто, если не настроен., `apply_unsubscribe` — Выключает маркетинговые рассылки по токену.

#### app/cabinet/utils

- `app/cabinet/utils/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/cabinet/utils/brand_monogram.py` — Python-модуль
  Классы: нет
  Функции: `monogram_letter` — Первая буква строки заглавной; при пустой строке — ``fallback``., `monogram_svg`, `monogram_png` — PNG-монограмма в геометрии SVG. Рисуется один раз на букву.
- `app/cabinet/utils/device_ownership.py` — Python-модуль
  Классы: нет
  Функции: `verify_hwid_belongs_to_user` — Best-effort check that `hwid` is on one of the user's RemnaWave panels.
- `app/cabinet/utils/favicon_tile.py` — Python-модуль
  Классы: нет
  Функции: `is_raster_logo`, `rounded_logo_tile` — PNG ``size``×``size``: логотип вписан по принципу object-fit: cover, углы прозрачные., `cached_rounded_logo_tile` — Плитка по файлу логотипа; перерисовывается только когда файл заменили.
- `app/cabinet/utils/fonts/`
- `app/cabinet/utils/links.py` — Python-модуль
  Классы: нет
  Функции: `get_campaign_deep_link` — Generate a Telegram deep link for a campaign., `get_campaign_web_link` — Generate a web app link for a campaign.
- `app/cabinet/utils/locale.py` — Python-модуль
  Классы: нет
  Функции: `resolve_locale_text` — Resolve a localized text dict to a single string for the given language., `ensure_locale_dict` — Coerce a value to a locale dict. Plain strings become ``{'ru': value}``., `validate_locale_dict` — Validate that all keys are supported locales and values respect length limits.

##### app/cabinet/utils/fonts

- `app/cabinet/utils/fonts/OFL.txt` — файл
- `app/cabinet/utils/fonts/manrope-variable.ttf` — файл

### app/database

- `app/database/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/database/constants.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/database/crud/`
- `app/database/database.py` — Python-модуль
  Классы: `DatabaseManager` (4 методов), `BatchOperations` (2 методов)
  Функции: `with_db_retry` — Декоратор для автоматического retry при сбоях подключения к БД., `execute_with_retry` — Выполнение SQL с retry логикой., `get_db` — Стандартная dependency для FastAPI, `get_db_read_only` — Read-only dependency для тяжелых SELECT запросов, `close_db` — Корректное закрытие всех соединений, `sync_postgres_sequences` — Ensure PostgreSQL sequences match the current max values after restores., `get_pool_metrics` — Детальные метрики пула для Prometheus/Grafana
- `app/database/migrations.py` — Python-модуль
  Классы: нет
  Функции: `run_alembic_upgrade` — Run ``alembic upgrade head``, handling fresh and legacy databases., `stamp_alembic_head` — Stamp the DB as being at head without running migrations (for existing DBs).
- `app/database/models.py` — Python-модуль
  Классы: `AwareDateTime` (2 методов), `UserStatus`, `SubscriptionStatus`, `TransactionType`, `PromoCodeType`, `PaymentMethod`, `MainMenuButtonActionType`, `MainMenuButtonVisibility`, `WheelPrizeType`, `WheelSpinPaymentType`, `YooKassaPayment` (6 методов), `SavedPaymentMethod` (1 методов), `CryptoBotPayment` (5 методов), `AppleTransaction` (2 методов), `AppleIAPAccount` (1 методов), `AppleNotification` (1 методов), `AppleIAPAbuseEvent` (1 методов), `HeleketPayment` (5 методов), `MulenPayPayment` (2 методов), `Pal24Payment` (3 методов), `WataPayment` (2 методов), `PlategaPayment` (2 методов), `PlategaSubscription` (1 методов), `LavaSubscription` (1 методов), `CloudPaymentsPayment` (5 методов), `FreekassaPayment` (5 методов), `KassaAiPayment` (5 методов), `RioPayPayment` (5 методов), `SeverPayPayment` (5 методов), `PayPearPayment` (5 методов), `RollyPayPayment` (5 методов), `OverpayPayment` (5 методов), `AuraPayPayment` (5 методов), `EtoplatezhiPayment` (5 методов), `AntilopayPayment` (5 методов), `JupiterPayment` (5 методов), `DonutPayment` (5 методов), `LavaPayment` (5 методов), `CisPayPayment` (5 методов), `TabPayPayment` (5 методов), `ParityPayPayment` (5 методов), `PromoGroup` (3 методов), `UserPromoGroup` (1 методов), `Tariff` (20 методов), `PartnerStatus`, `User` (12 методов), `Subscription` (15 методов), `GraceAccessSessionModel`, `TrafficPurchase` (1 методов), `Transaction` (1 методов), `SubscriptionConversion` (2 методов), `PromoCode` (2 методов), `PromoCodeUse`, `CouponStatus`, `CouponBatch` (2 методов), `Coupon` (1 методов), `ReferralRewardType`, `ReferralRewardTrigger`, `ReferralRewardMode`, `ReferralRewardLevel` (1 методов), `ReferralEarning` (1 методов), `WithdrawalRequestStatus`, `WithdrawalRequest` (1 методов), `PartnerApplication`, `ReferralContest` (1 методов), `ReferralContestEvent` (1 методов), `ReferralContestVirtualParticipant` (1 методов), `ContestTemplate`, `ContestRound`, `ContestAttempt`, `Squad` (1 методов), `ServiceRule`, `PrivacyPolicy`, `PublicOffer`, `LegalConsent`, `RecurrentPayments`, `FaqSetting`, `FaqPage`, `SystemSetting`, `EmailTemplate`, `MonitoringLog`, `SentNotification`, `SubscriptionEvent`, `DiscountOffer`, `PromoOfferTemplate`, `SubscriptionTemporaryAccess`, `PromoOfferLog`, `BroadcastHistory`, `Poll`, `PollQuestion`, `PollOption`, `PollResponse`, `PollAnswer`, `ServerSquad` (3 методов), `SubscriptionServer`, `SupportAuditLog`, `UserMessage` (1 методов), `WelcomeText`, `PinnedMessage`, `AdvertisingCampaign` (4 методов), `AdvertisingCampaignRegistration` (1 методов), `TicketStatus`, `Ticket` (8 методов), `TicketMessage` (3 методов), `WebApiToken` (1 методов), `MainMenuButton` (3 методов), `MenuLayoutHistory` (1 методов), `ButtonClickLog` (1 методов), `Webhook` (1 методов), `WebhookDelivery` (1 методов), `CabinetRefreshToken` (4 методов), `WheelConfig` (1 методов), `WheelPrize` (1 методов), `WheelSpin` (3 методов), `TicketNotification` (1 методов), `PaymentMethodConfig` (1 методов), `RequiredChannel` (1 методов), `UserChannelSubscription` (1 методов), `AdminRole` (1 методов), `UserRole` (1 методов), `AccessPolicy` (1 методов), `AdminAuditLog` (1 методов), `LandingPage` (1 методов), `GuestPurchaseStatus`, `GuestPurchase` (1 методов), `NewsArticle` (1 методов), `NewsCategory` (1 методов), `NewsTag` (1 методов), `YandexClientIdMap`, `InfoPage`, `UserDeviceAlias`, `SystemErrorEvent`, `EmailQueueItem`, `ReachabilityBatch`, `ReachabilityJob`, `ReachabilityLeg`, `ReachabilityTargetPref`
  Функции: нет

#### app/database/crud

- `app/database/crud/antilopay.py` — Python-модуль
  Классы: нет
  Функции: `create_antilopay_payment` — Создает запись о платеже Antilopay., `get_antilopay_payment_by_order_id` — Получает платеж по order_id (internal)., `get_antilopay_payment_by_invoice_id` — Получает платеж по ID от Antilopay., `get_antilopay_payment_by_id` — Получает платеж по ID., `get_antilopay_payment_by_id_for_update` — Получает платеж по ID с блокировкой FOR UPDATE., `update_antilopay_payment_status` — Обновляет статус платежа., `get_pending_antilopay_payments` — Получает незавершенные платежи пользователя., `get_expired_pending_antilopay_payments` — Получает просроченные платежи в статусе pending., `link_antilopay_payment_to_transaction` — Связывает платеж с транзакцией.
- `app/database/crud/apple_iap.py` — Python-модуль
  Классы: нет
  Функции: `get_or_create_apple_iap_account` — Return a stable StoreKit appAccountToken UUID for a user., `get_apple_iap_account_by_token`, `create_apple_transaction`, `get_apple_transaction_by_transaction_id`, `get_apple_transaction_by_web_order_line_item_id`, `find_apple_transactions_for_support`, `get_recent_apple_transactions`, `get_unprocessed_apple_notifications`, `get_apple_transaction_by_transaction_id_for_update` — Get apple transaction with FOR UPDATE lock for safe concurrent access., `mark_apple_transaction_refunded` — Mark an Apple transaction as refunded. Returns the transaction or None if not found., `create_apple_notification`, `get_apple_notification_by_uuid`, `get_apple_notification_by_payload_hash`, `mark_apple_notification_processed`, `create_apple_abuse_event`
- `app/database/crud/aurapay.py` — Python-модуль
  Классы: нет
  Функции: `create_aurapay_payment` — Создает запись о платеже AuraPay., `get_aurapay_payment_by_order_id` — Получает платеж по order_id (internal)., `get_aurapay_payment_by_invoice_id` — Получает платеж по UUID от AuraPay., `get_aurapay_payment_by_id` — Получает платеж по ID., `get_aurapay_payment_by_id_for_update` — Получает платеж по ID с блокировкой FOR UPDATE., `update_aurapay_payment_status` — Обновляет статус платежа., `get_pending_aurapay_payments` — Получает незавершенные платежи пользователя., `get_expired_pending_aurapay_payments` — Получает просроченные платежи в статусе pending., `link_aurapay_payment_to_transaction` — Связывает платеж с транзакцией.
- `app/database/crud/campaign.py` — Python-модуль
  Классы: нет
  Функции: `create_campaign`, `get_campaign_by_id`, `get_campaign_by_start_parameter`, `get_campaigns_list`, `get_campaigns_count`, `update_campaign`, `delete_campaign`, `get_campaign_registration_by_user`, `record_campaign_registration` — Создаёт или возвращает запись регистрации в рекламной кампании., `get_campaign_statistics`, `get_campaigns_overview`
- `app/database/crud/cispay.py` — Python-модуль
  Классы: нет
  Функции: `create_cispay_payment` — Создаёт запись о платеже cisPay., `get_cispay_payment_by_order_id` — Получает платеж по order_id (internal)., `get_cispay_payment_by_invoice_id` — Получает платёж по id транзакции, выданному cisPay., `get_cispay_payment_by_id` — Получает платеж по локальному ID., `get_cispay_payment_by_id_for_update` — Получает платёж с блокировкой FOR UPDATE., `update_cispay_payment_status` — Обновляет статус платежа., `get_pending_cispay_payments` — Возвращает незавершённые платежи пользователя., `get_expired_pending_cispay_payments` — Возвращает просроченные платежи в статусе pending., `link_cispay_payment_to_transaction` — Связывает платёж с транзакцией.
- `app/database/crud/cloudpayments.py` — Python-модуль
  Классы: нет
  Функции: `create_cloudpayments_payment` — Create a new CloudPayments payment record., `get_cloudpayments_payment_by_invoice_id` — Get CloudPayments payment by invoice ID., `get_cloudpayments_payment_by_id` — Get CloudPayments payment by internal ID., `get_cloudpayments_payment_by_id_for_update`, `get_cloudpayments_payment_by_transaction_id` — Get CloudPayments payment by CloudPayments transaction ID., `update_cloudpayments_payment` — Update CloudPayments payment record., `mark_cloudpayments_payment_as_paid` — Mark CloudPayments payment as paid., `link_cloudpayments_payment_to_transaction` — Link CloudPayments payment to internal transaction., `get_user_cloudpayments_payments` — Get CloudPayments payments for a user.
- `app/database/crud/contest.py` — Python-модуль
  Классы: нет
  Функции: `get_template_by_id`, `get_template_by_slug`, `list_templates`, `upsert_template`, `update_template_fields`, `create_round`, `get_active_rounds`, `get_active_round_by_template`, `finish_round`, `increment_winner_count`, `get_attempt`, `create_attempt`, `update_attempt` — Update existing attempt with answer and winner status., `clear_attempts`, `list_winners`
- `app/database/crud/coupon.py` — Python-модуль
  Классы: нет
  Функции: `generate_coupon_token` — 128-bit hex token: ``coupon_`` + 32 chars fits Telegram's 64-char start param., `create_coupon_batch`, `get_coupon_batch_by_id`, `get_coupon_batches`, `get_coupon_batches_count`, `get_coupon_by_token`, `get_batch_coupon_tokens` — Plain token strings (no entity hydration) — enough for the links export., `get_batch_status_counts`, `get_status_counts_for_batches` — Status counts for many batches in one query (list views)., `revoke_batch_coupons` — Flip all still-active coupons of the batch to REVOKED. Returns how many were revoked., `count_batch_redemptions_by_user` — Сколько купонов партии уже активировал пользователь., `delete_coupon_batch` — Полностью удаляет партию вместе с купонами. Возвращает число купонов., `delete_coupon` — Удаляет один купон партии (например, лишний непогашенный)., `get_coupon_by_id`
- `app/database/crud/cryptobot.py` — Python-модуль
  Классы: нет
  Функции: `create_cryptobot_payment`, `get_cryptobot_payment_by_invoice_id`, `get_cryptobot_payment_by_id`, `get_cryptobot_payment_by_invoice_id_for_update`, `get_cryptobot_payment_by_id_for_update`, `update_cryptobot_payment_status`, `link_cryptobot_payment_to_transaction`, `get_user_cryptobot_payments`, `get_pending_cryptobot_payments`
- `app/database/crud/discount_offer.py` — Python-модуль
  Классы: нет
  Функции: `upsert_discount_offer` — Create or refresh a discount offer for a user., `get_offer_by_id`, `list_discount_offers`, `list_active_discount_offers_for_user` — Return active (not yet claimed) offers for a user., `count_discount_offers`, `mark_offer_claimed`, `deactivate_expired_offers`, `get_latest_claimed_offer_for_user`
- `app/database/crud/donut.py` — Python-модуль
  Классы: нет
  Функции: `create_donut_payment` — Создаёт запись о платеже Donut., `get_donut_payment_by_order_id` — Получает платеж по order_id (internal)., `get_donut_payment_by_invoice_id` — Получает платёж по transaction_id, выданному Donut., `get_donut_payment_by_id` — Получает платеж по локальному ID., `get_donut_payment_by_id_for_update` — Получает платёж с блокировкой FOR UPDATE., `update_donut_payment_status` — Обновляет статус платежа., `get_pending_donut_payments` — Возвращает незавершённые платежи пользователя., `get_expired_pending_donut_payments` — Возвращает просроченные платежи в статусе pending., `link_donut_payment_to_transaction` — Связывает платёж с транзакцией.
- `app/database/crud/etoplatezhi.py` — Python-модуль
  Классы: нет
  Функции: `create_etoplatezhi_payment` — Создает запись о платеже Etoplatezhi., `get_etoplatezhi_payment_by_order_id` — Получает платеж по order_id (internal)., `get_etoplatezhi_payment_by_invoice_id` — Получает платеж по ID от Etoplatezhi., `get_etoplatezhi_payment_by_id` — Получает платеж по ID., `get_etoplatezhi_payment_by_id_for_update` — Получает платеж по ID с блокировкой FOR UPDATE., `update_etoplatezhi_payment_status` — Обновляет статус платежа., `get_pending_etoplatezhi_payments` — Получает незавершенные платежи пользователя., `get_expired_pending_etoplatezhi_payments` — Получает просроченные платежи в статусе pending., `link_etoplatezhi_payment_to_transaction` — Связывает платеж с транзакцией.
- `app/database/crud/faq.py` — Python-модуль
  Классы: нет
  Функции: `get_faq_setting`, `set_faq_enabled`, `upsert_faq_setting`, `get_faq_pages`, `get_faq_page_by_id`, `create_faq_page`, `update_faq_page`, `delete_faq_page`, `bulk_update_order`
- `app/database/crud/freekassa.py` — Python-модуль
  Классы: нет
  Функции: `create_freekassa_payment` — Создает запись о платеже Freekassa., `get_freekassa_payment_by_order_id` — Получает платеж по order_id., `get_freekassa_payment_by_fk_order_id` — Получает платеж по ID от Freekassa (intid)., `get_freekassa_payment_by_id` — Получает платеж по ID., `get_freekassa_payment_by_id_for_update`, `update_freekassa_payment_status` — Обновляет статус платежа., `get_pending_freekassa_payments` — Получает незавершенные платежи пользователя., `get_user_freekassa_payments` — Получает платежи пользователя с пагинацией., `get_expired_pending_payments` — Получает просроченные платежи в статусе pending.
- `app/database/crud/heleket.py` — Python-модуль
  Классы: нет
  Функции: `create_heleket_payment`, `get_heleket_payment_by_uuid`, `get_heleket_payment_by_order_id`, `get_heleket_payment_by_id`, `get_heleket_payment_by_id_for_update`, `update_heleket_payment`, `link_heleket_payment_to_transaction`
- `app/database/crud/info_pages.py` — Python-модуль
  Классы: нет
  Функции: `create_info_page` — Create a new info page., `get_info_page_by_id` — Get an info page by ID., `get_info_page_by_slug` — Get an info page by slug., `get_all_info_pages` — Get all info pages, ordered by sort_order ascending., `update_info_page` — Update an info page. Only whitelisted fields are applied., `delete_info_page` — Delete an info page., `get_tab_replacements` — Return a mapping of tab name to info page slug for active pages with replaces_tab set., `clear_replaces_tab` — Clear replaces_tab for all pages that currently replace the given tab., `reorder_info_pages` — Bulk update sort_order for info pages.
- `app/database/crud/jupiter.py` — Python-модуль
  Классы: нет
  Функции: `create_jupiter_payment` — Создаёт запись о платеже Jupiter., `get_jupiter_payment_by_order_id` — Получает платеж по order_id (internal)., `get_jupiter_payment_by_invoice_id` — Получает платёж по transaction_id, выданному Jupiter., `get_jupiter_payment_by_id` — Получает платеж по локальному ID., `get_jupiter_payment_by_id_for_update` — Получает платёж с блокировкой FOR UPDATE., `update_jupiter_payment_status` — Обновляет статус платежа., `get_pending_jupiter_payments` — Возвращает незавершённые платежи пользователя., `get_expired_pending_jupiter_payments` — Возвращает просроченные платежи в статусе pending., `link_jupiter_payment_to_transaction` — Связывает платёж с транзакцией.
- `app/database/crud/kassa_ai.py` — Python-модуль
  Классы: нет
  Функции: `create_kassa_ai_payment` — Создает запись о платеже KassaAI., `get_kassa_ai_payment_by_order_id` — Получает платеж по order_id., `get_kassa_ai_payment_by_external_order_id` — Получает платеж по ID от KassaAI (orderId)., `get_kassa_ai_payment_by_id` — Получает платеж по ID., `get_kassa_ai_payment_by_id_for_update`, `update_kassa_ai_payment_status` — Обновляет статус платежа., `get_pending_kassa_ai_payments` — Получает незавершенные платежи пользователя., `get_user_kassa_ai_payments` — Получает платежи пользователя с пагинацией., `get_expired_pending_kassa_ai_payments` — Получает просроченные платежи в статусе pending.
- `app/database/crud/landing.py` — Python-модуль
  Классы: нет
  Функции: `get_landing_by_slug` — Get a landing page by its slug., `get_landing_by_id` — Get a landing page by its ID., `get_active_landing_by_slug` — Get an active landing page by its slug., `get_all_landings` — Get all landing pages ordered by display_order., `create_landing` — Create a new landing page., `update_landing` — Update a landing page by ID. Returns None if not found., `delete_landing` — Delete a landing page by ID. Returns True if deleted., `update_landing_order` — Set display_order for landing pages based on position in list., `generate_purchase_token` — Generate a cryptographically secure purchase token., `create_guest_purchase` — Create a new guest purchase with an auto-generated token., `get_purchase_by_token` — Get a guest purchase by its token., `update_purchase_status` — Update the status of a guest purchase and optional extra fields., `get_landing_purchase_stats` — Get purchase counts grouped by status for a landing page., `get_all_landing_purchase_stats` — Get purchase counts grouped by landing_id and status in a single query.
- `app/database/crud/lava.py` — Python-модуль
  Классы: нет
  Функции: `create_lava_payment` — Создаёт запись о платеже Lava., `get_lava_payment_by_order_id` — Получает платёж по нашему orderId., `get_lava_payment_by_invoice_id` — Получает платёж по invoice_id, выданному Lava., `get_lava_payment_by_id` — Получает платёж по локальному ID., `get_lava_payment_by_id_for_update` — Получает платёж с FOR UPDATE-блокировкой., `update_lava_payment_status` — Обновляет статус платежа., `get_pending_lava_payments` — Возвращает незавершённые платежи пользователя., `get_expired_pending_lava_payments` — Возвращает просроченные платежи в статусе pending., `link_lava_payment_to_transaction` — Связывает платёж с транзакцией.
- `app/database/crud/lava_subscription.py` — Python-модуль
  Классы: нет
  Функции: `create_lava_subscription`, `get_lava_subscription_by_id`, `get_lava_subscription_by_id_for_update`, `get_lava_subscription_by_lava_id`, `get_lava_subscription_by_order_id` — Поиск по orderId — основной путь вебхука: списание приходит инвойсом., `get_active_lava_subscription_by_subscription`, `update_lava_subscription`, `list_lava_subscriptions_by_statuses`, `list_recently_cancelled_lava_subscriptions` — Недавно отменённые локально записи с remote-идентификатором.
- `app/database/crud/main_menu_button.py` — Python-модуль
  Классы: нет
  Функции: `count_main_menu_buttons`, `get_main_menu_buttons`, `get_main_menu_button_by_id`, `get_next_display_order`, `create_main_menu_button`, `update_main_menu_button`, `delete_main_menu_button`, `reorder_main_menu_buttons`
- `app/database/crud/mulenpay.py` — Python-модуль
  Классы: нет
  Функции: `create_mulenpay_payment`, `get_mulenpay_payment_by_local_id`, `get_mulenpay_payment_by_id_for_update`, `get_mulenpay_payment_by_uuid`, `get_mulenpay_payment_by_mulen_id`, `update_mulenpay_payment_status`, `update_mulenpay_payment_metadata`, `link_mulenpay_payment_to_transaction`
- `app/database/crud/news.py` — Python-модуль
  Классы: нет
  Функции: `create_news_article` — Create a new news article., `get_news_article_by_id` — Get a news article by ID with author, category, and tag relationships., `get_news_article_by_slug` — Get a news article by slug with author, category, and tag relationships., `get_published_news` — Get published news articles, ordered by published_at descending., `get_published_news_count` — Get count of published news articles, optionally filtered by category., `get_all_news` — Get all news articles (admin), ordered by created_at descending., `get_all_news_count` — Get total count of all news articles., `get_news_categories` — Get distinct categories from published articles., `unfeature_all_news` — Remove featured flag from all articles (so only one can be featured)., `update_news_article` — Update a news article. Only whitelisted fields are applied., `delete_news_article` — Delete a news article., `increment_views` — Atomically increment the views counter and return the new count.
- `app/database/crud/news_categories.py` — Python-модуль
  Классы: нет
  Функции: `get_all_categories` — Get all news categories ordered by name., `get_category_by_id` — Get a single news category by primary key., `create_category` — Create a new news category., `update_category` — Update an existing news category., `delete_category` — Delete a news category and clear category fields from all linked articles.
- `app/database/crud/news_tags.py` — Python-модуль
  Классы: нет
  Функции: `get_all_tags` — Get all news tags ordered by name., `get_tag_by_id` — Get a single news tag by primary key., `create_tag` — Create a new news tag., `update_tag` — Update an existing news tag., `delete_tag` — Delete a news tag and clear tag fields from all linked articles.
- `app/database/crud/notification.py` — Python-модуль
  Классы: нет
  Функции: `notification_sent`, `record_notification`, `clear_notifications`, `clear_notification_by_type`
- `app/database/crud/overpay.py` — Python-модуль
  Классы: нет
  Функции: `create_overpay_payment` — Создает запись о платеже Overpay., `get_overpay_payment_by_order_id` — Получает платеж по order_id (internal)., `get_overpay_payment_by_overpay_id` — Получает платеж по ID от Overpay., `get_overpay_payment_by_id` — Получает платеж по ID., `get_overpay_payment_by_id_for_update` — Получает платеж по ID с блокировкой FOR UPDATE., `update_overpay_payment_status` — Обновляет статус платежа., `get_pending_overpay_payments` — Получает незавершенные платежи пользователя., `get_expired_pending_overpay_payments` — Получает просроченные платежи в статусе pending., `link_overpay_payment_to_transaction` — Связывает платеж с транзакцией.
- `app/database/crud/pal24.py` — Python-модуль
  Классы: нет
  Функции: `create_pal24_payment`, `get_pal24_payment_by_id`, `get_pal24_payment_by_id_for_update`, `get_pal24_payment_by_bill_id`, `get_pal24_payment_by_order_id`, `update_pal24_payment_status`, `link_pal24_payment_to_transaction`
- `app/database/crud/paritypay.py` — Python-модуль
  Классы: нет
  Функции: `create_paritypay_payment` — Создаёт запись о платеже ParityPay., `get_paritypay_payment_by_order_id` — Получает платеж по order_id (наш)., `get_paritypay_payment_by_invoice_id` — Получает платёж по идентификатору, выданному ParityPay., `get_paritypay_payment_by_id` — Получает платеж по локальному ID., `get_paritypay_payment_by_id_for_update` — Получает платёж с блокировкой FOR UPDATE., `update_paritypay_payment_status` — Обновляет статус платежа., `is_paritypay_event_processed` — Обрабатывалась ли уже пара (id, status) из вебхука., `remember_paritypay_event` — Помечает пару (id, status) обработанной., `get_pending_paritypay_payments` — Возвращает незавершённые платежи пользователя., `link_paritypay_payment_to_transaction` — Связывает платёж с транзакцией.
- `app/database/crud/payment_gateway_stats.py` — Python-модуль
  Классы: нет
  Функции: `get_gateway_success_rates` — Per-gateway {method, total, paid, success_rate} for gateways with activity.
- `app/database/crud/paypear.py` — Python-модуль
  Классы: нет
  Функции: `create_paypear_payment` — Создает запись о платеже PayPear., `get_paypear_payment_by_order_id` — Получает платеж по order_id (internal)., `get_paypear_payment_by_paypear_id` — Получает платеж по ID от PayPear., `get_paypear_payment_by_id` — Получает платеж по ID., `get_paypear_payment_by_id_for_update` — Получает платеж по ID с блокировкой FOR UPDATE., `update_paypear_payment_status` — Обновляет статус платежа., `get_pending_paypear_payments` — Получает незавершенные платежи пользователя., `get_expired_pending_paypear_payments` — Получает просроченные платежи в статусе pending., `link_paypear_payment_to_transaction` — Связывает платеж с транзакцией.
- `app/database/crud/platega.py` — Python-модуль
  Классы: нет
  Функции: `create_platega_payment`, `get_platega_payment_by_id`, `get_platega_payment_by_id_for_update`, `get_platega_payment_by_transaction_id`, `get_platega_payment_by_correlation_id`, `update_platega_payment`, `link_platega_payment_to_transaction`
- `app/database/crud/platega_subscription.py` — Python-модуль
  Классы: нет
  Функции: `create_platega_subscription`, `get_platega_subscription_by_id`, `get_platega_subscription_by_id_for_update`, `get_platega_subscription_by_platega_id`, `get_active_platega_subscription_by_subscription`, `update_platega_subscription`, `list_platega_subscriptions_by_statuses`, `list_recently_cancelled_platega_subscriptions` — Недавно отменённые локально записи с remote-идентификатором.
- `app/database/crud/poll.py` — Python-модуль
  Классы: нет
  Функции: `create_poll`, `list_polls`, `get_poll_by_id`, `delete_poll`, `create_poll_response`, `get_poll_response_by_id`, `record_poll_answer`, `reset_poll_answers`, `get_poll_statistics`, `get_poll_responses_with_answers`
- `app/database/crud/privacy_policy.py` — Python-модуль
  Классы: нет
  Функции: `get_privacy_policy`, `upsert_privacy_policy`, `set_privacy_policy_enabled`
- `app/database/crud/promo_group.py` — Python-модуль
  Классы: нет
  Функции: `get_promo_groups_with_counts`, `get_auto_assign_promo_groups`, `has_auto_assign_promo_groups`, `get_promo_group_by_id`, `count_promo_groups`, `get_default_promo_group`, `create_promo_group`, `update_promo_group`, `delete_promo_group`, `get_promo_group_members`, `count_promo_group_members`
- `app/database/crud/promo_offer_log.py` — Python-модуль
  Классы: нет
  Функции: `log_promo_offer_action` — Persist a promo offer log entry., `list_promo_offer_logs`
- `app/database/crud/promo_offer_template.py` — Python-модуль
  Классы: нет
  Функции: `ensure_default_templates`, `list_promo_offer_templates`, `get_promo_offer_template_by_id`, `get_promo_offer_template_by_type`, `update_promo_offer_template`
- `app/database/crud/promocode.py` — Python-модуль
  Классы: нет
  Функции: `get_promocode_by_code`, `get_promocode_by_id` — Получает промокод по ID с eager loading всех связанных данных., `check_promocode_validity` — Проверяет существование и валидность промокода без активации., `create_promocode`, `check_user_promocode_usage`, `create_promocode_use`, `get_promocode_use_by_user_and_code`, `count_user_recent_activations` — Подсчитывает количество активаций промокодов пользователем за последние N часов., `get_user_promocodes`, `get_promocodes_list`, `get_promocodes_count`, `update_promocode`, `delete_promocode`, `get_active_discount_promocode_for_user` — Находит активный промокод на скидку, который сейчас действует у пользователя., `get_promocode_statistics`
- `app/database/crud/public_offer.py` — Python-модуль
  Классы: нет
  Функции: `get_public_offer`, `upsert_public_offer`, `set_public_offer_enabled`
- `app/database/crud/rbac.py` — Python-модуль
  Классы: `AdminRoleCRUD` (7 методов), `UserRoleCRUD` (5 методов), `AccessPolicyCRUD` (6 методов), `AuditLogCRUD` (2 методов)
  Функции: нет
- `app/database/crud/reachability.py` — Python-модуль
  Классы: нет
  Функции: `create_job`, `get_job`, `update_job`, `list_jobs`, `get_active_job`, `list_unfinished_jobs`, `replace_legs`, `latest_legs` — Последний лег на каждую пару (target_key, op_key)., `get_pref`, `upsert_pref`, `list_prefs`, `last_vless_leg_price_kopeks` — Цена одного лега VLESS по последней завершённой задаче (cost / (серверы × симки))., `create_batch`, `get_batch`, `update_batch`, `list_batches`, `list_unfinished_batches`, `get_active_batch`, `jobs_for_batch`
- `app/database/crud/recurrent_payments.py` — Python-модуль
  Классы: нет
  Функции: `get_recurrent_payments`, `upsert_recurrent_payments`, `set_recurrent_payments_enabled`
- `app/database/crud/referral.py` — Python-модуль
  Классы: нет
  Функции: `not_referee_directed` — Предикат «строка описывает МОЕГО реферала»., `get_user_campaign_id` — Получить campaign_id первой регистрации пользователя., `create_referral_earning` — Строка реферального ledger'а., `get_commission_payment_count` — Подсчитать количество комиссионных начислений реферера за платежи конкретного реферала., `get_referral_earnings_by_user`, `get_referral_earnings_by_referral`, `get_referral_earnings_sum` — Денежный заработок пригласившего за период., `get_referral_earnings_totals` — Заработок пригласившего: (копейки, дни)., `get_referral_statistics`, `get_top_referrers_by_period` — Получает топ рефереров за период., `get_user_referral_stats`
- `app/database/crud/referral_contest.py` — Python-модуль
  Классы: нет
  Функции: `create_referral_contest`, `list_referral_contests`, `get_referral_contests_count`, `get_referral_contest`, `update_referral_contest`, `toggle_referral_contest`, `get_contests_for_events`, `get_contests_for_summaries`, `add_contest_event`, `get_contest_leaderboard` — Получить лидерборд конкурса., `get_contest_participants` — Получить участников конкурса., `get_referrer_score`, `get_contest_events_count`, `get_contest_events`, `mark_daily_summary_sent`, `mark_final_summary_sent`, `delete_referral_contest`, `get_contest_payment_stats` — Получить статистику оплат по конкурсу., `get_contest_transaction_breakdown` — Получить разбивку транзакций по типам для конкурса., `upsert_contest_event` — Создать или обновить событие конкурса., `debug_contest_transactions` — Показать транзакции которые учитываются в конкурсе для отладки., `sync_contest_events` — Синхронизировать события конкурса с реальными данными., `cleanup_invalid_contest_events` — Удалить события конкурса для рефералов, зарегистрированных ВНЕ периода конкурса., `add_virtual_participant`, `list_virtual_participants`, `delete_virtual_participant`, `update_virtual_participant_count`, `get_contest_leaderboard_with_virtual` — Лидерборд с виртуальными участниками.
- `app/database/crud/referral_reward_level.py` — Python-модуль
  Классы: нет
  Функции: `normalize_reward_preference` — Предпочтение награды или ``None`` — «как настроено правилом»., `normalize_mode` — Режим наград с приведением к известному значению., `normalize_trigger`, `get_all_reward_levels`, `get_reward_level`, `get_reward_level_by_id`, `upsert_reward_level` — Создать или обновить правило уровня., `delete_reward_level`
- `app/database/crud/required_channel.py` — Python-модуль
  Классы: нет
  Функции: `validate_channel_id` — Validate and normalize channel_id. Auto-prefixes -100 for bare digits., `get_active_channels` — Get all active required channels (sorted by sort_order)., `get_all_channels` — Get all required channels (including inactive)., `get_channel_by_id`, `get_channel_by_channel_id`, `add_channel`, `update_channel` — Update channel fields. Only fields in _UPDATABLE_FIELDS are accepted., `delete_channel`, `toggle_channel`, `upsert_user_channel_sub` — Upsert user subscription status (PostgreSQL ON CONFLICT)., `get_user_channel_subs` — Get all channel subscriptions for a user., `get_user_channel_sub`, `bulk_upsert_user_subs` — Batch upsert user subscriptions with single multi-row INSERT.
- `app/database/crud/riopay.py` — Python-модуль
  Классы: нет
  Функции: `create_riopay_payment` — Создает запись о платеже RioPay., `get_riopay_payment_by_order_id` — Получает платеж по order_id (internal)., `get_riopay_payment_by_riopay_order_id` — Получает платеж по ID от RioPay (UUID)., `get_riopay_payment_by_id` — Получает платеж по ID., `get_riopay_payment_by_id_for_update` — Получает платеж по ID с блокировкой FOR UPDATE (для защиты от TOCTOU race)., `update_riopay_payment_status` — Обновляет статус платежа., `get_pending_riopay_payments` — Получает незавершенные платежи пользователя., `get_expired_pending_riopay_payments` — Получает просроченные платежи в статусе pending.
- `app/database/crud/rollypay.py` — Python-модуль
  Классы: нет
  Функции: `create_rollypay_payment` — Создает запись о платеже RollyPay., `get_rollypay_payment_by_order_id` — Получает платеж по order_id (internal)., `get_rollypay_payment_by_rollypay_id` — Получает платеж по ID от RollyPay., `get_rollypay_payment_by_id` — Получает платеж по ID., `get_rollypay_payment_by_id_for_update` — Получает платеж по ID с блокировкой FOR UPDATE., `update_rollypay_payment_status` — Обновляет статус платежа., `get_pending_rollypay_payments` — Получает незавершенные платежи пользователя., `get_expired_pending_rollypay_payments` — Получает просроченные платежи в статусе pending., `link_rollypay_payment_to_transaction` — Связывает платеж с транзакцией.
- `app/database/crud/rules.py` — Python-модуль
  Классы: нет
  Функции: `get_rules_by_language`, `create_or_update_rules`, `clear_all_rules`, `get_current_rules_content`, `get_all_rules_versions`, `restore_rules_version`, `get_rules_statistics`
- `app/database/crud/saved_payment_method.py` — Python-модуль
  Классы: нет
  Функции: `create_saved_payment_method` — Создаёт или реактивирует сохранённый метод оплаты., `get_active_payment_methods_by_user` — Получить все активные сохранённые методы оплаты пользователя., `get_user_ids_with_active_payment_methods` — Вернуть подмножество user_ids, у которых есть хотя бы один активный метод оплаты., `get_payment_method_by_yookassa_id` — Найти сохранённый метод по YooKassa payment_method.id., `deactivate_payment_method` — Деактивировать (soft-delete) сохранённый метод оплаты., `deactivate_all_user_payment_methods` — Деактивировать все методы оплаты пользователя. Возвращает количество деактивированных.
- `app/database/crud/server_squad.py` — Python-модуль
  Классы: нет
  Функции: `create_server_squad`, `get_server_squad_by_uuid`, `get_server_squad_by_id`, `get_all_server_squads`, `get_available_server_squads`, `get_effective_tariff_squad_uuids` — Resolve tariff squads, treating an empty list as "all available squads"., `get_active_server_squads` — Возвращает список активных серверов, доступных для подключения., `choose_random_active_server_squad` — Возвращает случайный активный сервер., `get_random_active_squad_uuid` — Возвращает UUID случайного активного сервера или запасной UUID., `update_server_squad_promo_groups`, `update_server_squad`, `delete_server_squad`, `sync_with_remnawave`, `get_server_connected_users`, `get_trial_eligible_server_squads`, `choose_random_trial_server_squad`, `get_random_trial_squad_uuid`, `get_server_statistics`, `count_active_users_for_squad` — Возвращает количество активных подписок, подключенных к указанному скваду., `add_user_to_servers`, `remove_user_from_servers`, `update_server_user_counts` — Increment and decrement server user counters in a single sorted pass., `get_server_ids_by_uuids`, `get_server_squads_by_uuids` — Получает список ServerSquad объектов по их UUID с загрузкой allowed_promo_groups., `ensure_servers_synced` — Проверяет и синхронизирует серверы при запуске., `sync_server_user_counts`
- `app/database/crud/severpay.py` — Python-модуль
  Классы: нет
  Функции: `create_severpay_payment` — Создает запись о платеже SeverPay., `get_severpay_payment_by_order_id` — Получает платеж по order_id (internal)., `get_severpay_payment_by_severpay_id` — Получает платеж по ID от SeverPay., `get_severpay_payment_by_id` — Получает платеж по ID., `get_severpay_payment_by_id_for_update` — Получает платеж по ID с блокировкой FOR UPDATE., `update_severpay_payment_status` — Обновляет статус платежа., `get_pending_severpay_payments` — Получает незавершенные платежи пользователя., `get_expired_pending_severpay_payments` — Получает просроченные платежи в статусе pending., `link_severpay_payment_to_transaction` — Связывает платеж с транзакцией.
- `app/database/crud/squad.py` — Python-модуль
  Классы: нет
  Функции: `get_squad_by_uuid`, `get_available_squads`, `create_squad`, `update_squad`
- `app/database/crud/subscription.py` — Python-модуль
  Классы: нет
  Функции: `generate_unique_short_id` — Generate a unique remnawave_short_id (6 hex chars) with collision check., `is_recently_updated_by_webhook` — Return True if subscription was updated by webhook within guard window., `calc_device_limit_on_tariff_switch` — Calculate device_limit when switching tariffs., `is_active_paid_subscription` — Return True if subscription is active, paid (non-trial), and not expired., `get_subscription_by_user_id` — Get primary subscription for user., `apply_trial_conversion_defaults` — Настройки, которые подписка получает, перестав быть триалом., `create_trial_subscription` — Создает триальную подписку., `resolve_trial_conversion_candidate` — Кандидат конверсии триала, каким его увидит ``create_paid_subscription``., `create_paid_subscription`, `replace_subscription` — Перезаписывает параметры существующей подписки пользователя., `should_carry_trial_remaining_days` — Переносить ли остаток триальных дней на платную подписку при переходе., `extend_subscription` — Продлевает подписку на указанное количество дней., `add_subscription_traffic`, `add_subscription_devices`, `add_subscription_squad`, `remove_subscription_squad`, `decrement_subscription_server_counts` — Decrease server counters linked to the provided subscription., `update_subscription_autopay`, `deactivate_subscription`, `reset_subscription` — Полностью обнулить подписку «как будто пользователь её не оформлял», НЕ удаляя, `reactivate_subscription` — Реактивация подписки (например, после повторной подписки на канал или докупки трафика)., `get_expiring_subscriptions`, `get_expired_subscriptions`, `get_subscriptions_for_autopay`, `get_subscriptions_statistics`, `get_trial_statistics`, `wipe_trial_subscriptions` — Снимает доступ и удаляет переданные триал-подписки — единый код для ботовой, `reset_trials_for_users_without_paid_subscription` — Bulk-сброс истёкших триалов у неплативших (кнопка «Сбросить триалы» в боте)., `update_subscription_usage`, `get_all_subscriptions`, `get_subscriptions_batch` — Получает подписки пачками для синхронизации. Загружает связанных пользователей и тарифы., `add_subscription_servers`, `get_subscription_server_ids`, `remove_subscription_servers`, `expire_subscription`, `check_and_update_subscription_status`, `create_subscription_no_commit` — Создает подписку без немедленного коммита для пакетной обработки, `create_subscription`, `create_pending_subscription` — Creates a pending subscription that will be activated after payment., `create_sbp_pending_subscription` — Заготовка подписки под СБП-оформление (покупка через Platega-рекуррент)., `create_pending_trial_subscription` — Creates a pending trial subscription. Wrapper for create_pending_subscription with is_trial=True., `activate_pending_subscription` — Активирует pending подписку пользователя, меняя её статус на ACTIVE., `activate_pending_trial_subscription` — Активирует pending триальную подписку по её ID после оплаты., `get_daily_subscriptions_for_charge` — Получает все суточные подписки, которые нужно обработать для списания., `get_disabled_daily_subscriptions_for_resume` — Получает список DISABLED суточных подписок, которые можно возобновить., `get_expired_daily_subscriptions_for_recovery` — Получает EXPIRED суточные подписки, которые были ошибочно экспайрены, `pause_daily_subscription` — Приостанавливает суточную подписку (списание не будет происходить)., `resume_daily_subscription` — Возобновляет суточную подписку (списание продолжится)., `update_daily_charge_time` — Обновляет время последнего суточного списания и продлевает подписку на 1 день., `suspend_daily_subscription_insufficient_balance` — Приостанавливает подписку из-за недостатка баланса., `get_subscription_with_tariff` — Получает подписку пользователя с загруженным тарифом., `toggle_daily_subscription_pause` — Переключает состояние паузы суточной подписки., `get_active_subscriptions_by_user_id` — Get all active/trial/limited subscriptions for a user., `get_subscription_by_id_for_user` — Get subscription by ID with ownership check (IDOR protection)., `get_subscription_by_id` — Get subscription by ID (admin use only, no ownership check)., `get_subscription_by_user_and_tariff` — Get a subscription for a specific user+tariff combination., `get_alive_trial_subscription` — Alive (active/trial/limited) trial subscription of the user, if any., `deactivate_user_trial_subscriptions` — Deactivate all trial subscriptions for a user., `get_all_subscriptions_by_user_id` — Get all subscriptions for a user (any status).
- `app/database/crud/subscription_conversion.py` — Python-модуль
  Классы: нет
  Функции: `create_subscription_conversion`, `get_conversion_by_user_id`, `get_conversion_statistics`, `get_users_had_trial_count`
- `app/database/crud/subscription_event.py` — Python-модуль
  Классы: нет
  Функции: `create_subscription_event`, `list_subscription_events`
- `app/database/crud/system_errors.py` — Python-модуль
  Классы: нет
  Функции: `list_error_events` — Постранично отдать события с фильтрами. Возвращает (записи, всего)., `get_error_event`, `get_error_summary` — Сводка для бейджа и шапки страницы., `mark_delivery_result` — Записать исход ручной повторной доставки.
- `app/database/crud/system_setting.py` — Python-модуль
  Классы: нет
  Функции: `upsert_system_setting`, `get_setting_value` — Get a setting value from database., `delete_system_setting`
- `app/database/crud/tabpay.py` — Python-модуль
  Классы: нет
  Функции: `create_tabpay_payment` — Создаёт запись о платеже TabPay., `get_tabpay_payment_by_order_id` — Получает платеж по order_id (наш)., `get_tabpay_payment_by_invoice_id` — Получает платёж по идентификатору, выданному TabPay., `get_tabpay_payment_by_id` — Получает платеж по локальному ID., `get_tabpay_payment_by_id_for_update` — Получает платёж с блокировкой FOR UPDATE., `update_tabpay_payment_status` — Обновляет статус платежа., `is_tabpay_event_processed` — Обрабатывалась ли уже пара (id, status) из вебхука., `remember_tabpay_event` — Помечает пару (id, status) обработанной., `get_pending_tabpay_payments` — Возвращает незавершённые платежи пользователя., `link_tabpay_payment_to_transaction` — Связывает платёж с транзакцией.
- `app/database/crud/tariff.py` — Python-модуль
  Классы: нет
  Функции: `get_all_tariffs` — Получает все тарифы с опциональной фильтрацией по активности., `get_tariff_by_id` — Получает тариф по ID., `count_tariffs` — Подсчитывает количество тарифов., `get_trial_tariff` — Получает тариф, доступный для триала (is_trial_available=True)., `set_trial_tariff` — Устанавливает тариф как триальный (снимает флаг с других тарифов)., `clear_trial_tariff` — Снимает флаг триала со всех тарифов., `get_all_active_tariffs` — Get all active tariffs., `get_tariffs_for_user` — Получает тарифы, доступные для пользователя с учетом его промогруппы., `create_tariff` — Создает новый тариф., `update_tariff` — Обновляет существующий тариф., `delete_tariff` — Удаляет тариф., `get_tariff_subscriptions_count` — Подсчитывает количество подписок на тарифе., `get_active_subscriptions_count_by_tariff_id` — Подсчитывает количество активных (active/trial) подписок на тарифе., `set_tariff_promo_groups` — Устанавливает промогруппы для тарифа., `add_promo_group_to_tariff` — Добавляет промогруппу к тарифу., `remove_promo_group_from_tariff` — Удаляет промогруппу из тарифа., `get_tariffs_with_subscriptions_count` — Получает тарифы с количеством подписок., `reorder_tariffs` — Изменяет порядок отображения тарифов., `sync_default_tariff_from_config` — Синхронизирует дефолтный тариф из конфига (.env) в БД., `load_period_prices_from_db` — Загружает периоды/цены из тарифа в PERIOD_PRICES., `ensure_tariffs_synced` — Проверяет и синхронизирует тарифы при запуске.
- `app/database/crud/ticket.py` — Python-модуль
  Классы: `TicketCRUD` (20 методов), `TicketMessageCRUD` (4 методов)
  Функции: нет
- `app/database/crud/ticket_notification.py` — Python-модуль
  Классы: `TicketNotificationCRUD` (13 методов)
  Функции: нет
- `app/database/crud/transaction.py` — Python-модуль
  Классы: нет
  Функции: `traffic_addon_clause` — SQL-условие: описание транзакции похоже на докупку трафика., `device_addon_clause` — SQL-условие: описание транзакции похоже на покупку доп. устройств., `addon_description_clause` — SQL-условие: транзакция — любой доп (трафик или устройства), не продажа/продление., `create_transaction`, `emit_transaction_side_effects` — Fire side-effects that were deferred when create_transaction(commit=False) was used., `get_transaction_by_id`, `get_transaction_by_external_id`, `get_user_transactions`, `get_user_transactions_count`, `get_user_total_spent_kopeks` — Sum of personal spending for promo group auto-assignment., `complete_transaction`, `get_pending_transactions`, `get_transactions_statistics`, `get_revenue_by_period` — Доход по дням — реальные платежи + прямые покупки подписок (лендинги)., `find_tribute_transactions_by_payment_id`, `check_tribute_payment_duplicate`, `create_unique_tribute_transaction` — Create a Tribute deposit transaction idempotently.
- `app/database/crud/user.py` — Python-модуль
  Классы: нет
  Функции: `generate_referral_code`, `get_user_by_id`, `get_user_by_telegram_id`, `find_phantom_user_by_username` — Find a phantom user created by guest purchase (no telegram_id, auth_type=telegram)., `get_user_by_username`, `get_user_by_referral_code`, `get_user_by_remnawave_id` — Найти бот-пользователя по числовому id пользователя панели., `create_unique_referral_code`, `create_user_no_commit` — Создает пользователя без немедленного коммита для пакетной обработки, `emit_user_created_event` — Emit the best-effort post-commit user.created event for a persisted user., `create_user`, `update_user`, `lock_user_for_update` — Lock user row with SELECT FOR UPDATE to prevent concurrent balance modifications., `add_user_balance`, `add_user_balance_by_id`, `lock_user_for_pricing` — Lock user row with FOR UPDATE and return refreshed instance., `subtract_user_balance`, `cleanup_expired_promo_offer_discounts`, `get_users_list`, `get_users_count`, `get_users_spending_stats` — Получает статистику трат для списка пользователей., `get_referrals`, `get_users_for_promo_segment`, `get_inactive_users`, `delete_user`, `get_users_statistics`, `get_users_with_active_subscriptions` — Получает список пользователей с активными подписками., `create_user_by_email` — Создать пользователя через email регистрацию (без Telegram)., `get_user_by_email` — Get user by email address (case-insensitive)., `get_user_by_email_alias` — Найти пользователя, чей адрес ведёт в тот же ящик, что и ``email``., `is_email_taken` — Check if email is already taken by another user., `set_email_change_pending` — Set pending email change for user., `verify_and_apply_email_change` — Verify email change code and apply the change., `clear_email_change_pending` — Clear pending email change data., `get_user_by_oauth_provider` — Find a user by OAuth provider ID., `set_user_oauth_provider_id` — Link an OAuth provider ID to an existing user., `clear_user_oauth_provider_id` — Unlink an OAuth provider from an existing user (set column to None)., `create_user_by_oauth` — Create a new user via OAuth provider., `lock_user_subscriptions_for_update` — Lock all subscriptions for a user using SELECT FOR UPDATE.
- `app/database/crud/user_device_alias.py` — Python-модуль
  Классы: нет
  Функции: `normalize_alias` — Strip + collapse whitespace + cap length. Returns '' for empty/None input., `get_aliases_for_user` — Return all device aliases for a user as a {hwid: alias} dict., `get_alias` — Return a single alias or None when not set., `set_alias` — Insert or update an alias., `upsert_alias` — Deprecated convenience wrapper: empty `alias` deletes the row., `delete_alias` — Remove the alias for a (user, hwid) pair., `attach_aliases_to_devices` — Mutate-and-return: enrich each device dict with a `local_name` field.
- `app/database/crud/user_message.py` — Python-модуль
  Классы: нет
  Функции: `create_user_message`, `get_user_message_by_id`, `get_active_user_messages`, `get_random_active_message`, `get_all_user_messages`, `get_user_messages_count`, `update_user_message`, `toggle_user_message_status`, `delete_user_message`, `get_user_messages_stats`
- `app/database/crud/user_promo_group.py` — Python-модуль
  Классы: нет
  Функции: `sync_user_primary_promo_group` — Публичная обертка для синхронизации primary промогруппы пользователя., `add_user_to_promo_group` — Добавляет пользователю промогруппу., `remove_user_from_promo_group` — Удаляет промогруппу у пользователя., `get_user_promo_groups` — Получает все промогруппы пользователя, отсортированные по приоритету., `get_primary_user_promo_group` — Получает промогруппу пользователя с максимальным приоритетом., `has_user_promo_group` — Проверяет наличие промогруппы у пользователя., `count_user_promo_groups` — Подсчитывает количество промогрупп у пользователя., `replace_user_promo_groups` — Заменяет все промогруппы пользователя на новый список.
- `app/database/crud/wata.py` — Python-модуль
  Классы: нет
  Функции: `create_wata_payment`, `get_wata_payment_by_id`, `get_wata_payment_by_id_for_update`, `get_wata_payment_by_link_id`, `get_wata_payment_by_order_id`, `update_wata_payment_status`, `link_wata_payment_to_transaction`
- `app/database/crud/web_api_token.py` — Python-модуль
  Классы: нет
  Функции: `list_tokens`, `get_token_by_id`, `get_token_by_hash`, `create_token`, `update_token`, `set_tokens_active_status`, `delete_token`
- `app/database/crud/webhook.py` — Python-модуль
  Классы: нет
  Функции: `create_webhook` — Создать новый webhook., `get_webhook_by_id` — Получить webhook по ID., `list_webhooks` — Список webhooks с фильтрами., `get_active_webhooks_for_event` — Получить все активные webhooks для конкретного события., `update_webhook` — Обновить webhook., `delete_webhook` — Удалить webhook., `record_webhook_delivery` — Записать попытку доставки webhook., `update_webhook_stats` — Обновить статистику webhook.
- `app/database/crud/welcome_text.py` — Python-модуль
  Классы: нет
  Функции: `get_active_welcome_text`, `get_current_welcome_text_settings`, `get_welcome_text_by_id`, `list_welcome_texts`, `count_welcome_texts`, `toggle_welcome_text_status`, `set_welcome_text`, `create_welcome_text`, `update_welcome_text`, `delete_welcome_text`, `get_current_welcome_text_or_default`, `replace_placeholders`, `get_welcome_text_for_user`, `get_available_placeholders`
- `app/database/crud/wheel.py` — Python-модуль
  Классы: нет
  Функции: `get_wheel_config` — Получить текущую конфигурацию колеса (всегда id=1)., `get_or_create_wheel_config` — Получить или создать конфигурацию колеса., `update_wheel_config` — Обновить конфигурацию колеса., `get_wheel_prizes` — Получить список призов колеса., `get_wheel_prize_by_id` — Получить приз по ID., `create_wheel_prize` — Создать новый приз на колесе., `update_wheel_prize` — Обновить приз колеса., `delete_wheel_prize` — Удалить приз колеса., `reorder_wheel_prizes` — Переупорядочить призы колеса., `create_wheel_spin` — Создать запись о спине колеса., `get_wheel_spin_by_charge_id` — Найти спин по Telegram charge id (идемпотентность Stars-платежа)., `mark_spin_applied` — Отметить спин как примененный., `get_user_spins_today` — Получить количество спинов пользователя за сегодня., `get_user_spin_history` — Получить историю спинов пользователя., `get_all_spins` — Получить все спины с фильтрами (для админки)., `get_wheel_statistics` — Получить статистику колеса удачи.
- `app/database/crud/yandex_client_id.py` — Python-модуль
  Классы: нет
  Функции: `upsert_cid` — Insert or update Yandex ClientID for a user (race-safe via ON CONFLICT)., `get_cid` — Get Yandex ClientID mapping for a user., `mark_registration_sent` — Mark registration event as sent for a user., `mark_trial_sent` — Mark trial event as sent for a user., `upsert_subid` — Save subid for a user. Updates existing record or creates with placeholder CID., `get_subid` — Get subid for a user.
- `app/database/crud/yookassa.py` — Python-модуль
  Классы: нет
  Функции: `create_yookassa_payment`, `get_yookassa_payment_by_id`, `get_yookassa_payment_by_local_id`, `update_yookassa_payment_status`, `link_yookassa_payment_to_transaction`, `get_user_yookassa_payments`, `get_pending_yookassa_payments`, `get_succeeded_yookassa_payments_without_transaction`, `delete_yookassa_payment`, `get_yookassa_payments_stats`

### app/external

- `app/external/apple_iap.py` — Python-модуль
  Классы: `AppleIAPConfigurationError`, `AppleIAPService` (10 методов)
  Функции: `parse_apple_timestamp` — Convert Apple millisecond timestamps or ISO strings to aware UTC datetimes.
- `app/external/ban_system_api.py` — Python-модуль
  Классы: `BanSystemAPIError` (1 методов), `BanSystemAPI` (34 методов)
  Функции: нет
- `app/external/bschek_api.py` — Python-модуль
  Классы: `BschekAPIError` (1 методов), `BschekGatewayError`, `BschekAPI` (19 методов)
  Функции: `build_operators_params` — Query для GET /operators. aiohttp сам percent-encode'ит кириллицу.
- `app/external/cryptobot.py` — Python-модуль
  Классы: `CryptoBotService` (9 методов)
  Функции: нет
- `app/external/heleket.py` — Python-модуль
  Классы: `HeleketService` (9 методов)
  Функции: нет
- `app/external/heleket_webhook.py` — Python-модуль
  Классы: `HeleketWebhookHandler` (4 методов)
  Функции: `create_heleket_app`, `start_heleket_webhook_server`
- `app/external/pal24_client.py` — Python-модуль
  Классы: `Pal24APIError`, `Pal24Response` (2 методов), `Pal24Client` (14 методов)
  Функции: нет
- `app/external/remnawave_api.py` — Python-модуль
  Классы: `UserStatus`, `TrafficLimitStrategy`, `UserTraffic`, `RemnaWaveUser` (4 методов), `RemnaWaveInbound`, `RemnaWaveInternalSquad`, `RemnaWaveAccessibleNode`, `RemnaWaveHost`, `RemnaWaveNode` (2 методов), `SubscriptionInfo`, `SubscriptionPageConfig`, `RemnaWaveExternalSquad`, `RemnaWaveAPIError` (1 методов), `RemnaWaveTransientError`, `RemnaWaveInvalidUserIdError`, `RemnaWaveAPI` (110 методов)
  Функции: `coerce_panel_user_id` — Привести локально хранимый идентификатор к числовому id панели., `is_user_not_found_error` — Панель не нашла пользователя (удалён/протух идентификатор)., `format_bytes`, `parse_bytes`, `test_api_connection`
- `app/external/telegram_stars.py` — Python-модуль
  Классы: `TelegramStarsService` (6 методов)
  Функции: нет
- `app/external/tribute.py` — Python-модуль
  Классы: `TributeService` (6 методов)
  Функции: нет
- `app/external/wata_webhook.py` — Python-модуль
  Классы: `WataPublicKeyProvider` (3 методов), `WataWebhookHandler` (5 методов)
  Функции: `create_wata_webhook_app`, `start_wata_webhook_server`
- `app/external/webhook_server.py` — Python-модуль
  Классы: `WebhookServer` (9 методов)
  Функции: нет
- `app/external/yookassa_webhook.py` — Python-модуль
  Классы: `YooKassaWebhookHandler` (5 методов)
  Функции: `collect_yookassa_ip_candidates`, `resolve_yookassa_ip`, `is_yookassa_ip_allowed`, `create_yookassa_webhook_app`, `start_yookassa_webhook_server`

### app/handlers

- `app/handlers/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/handlers/admin/`
- `app/handlers/balance/`
- `app/handlers/channel_member.py` — Python-модуль
  Классы: нет
  Функции: `on_user_joined_channel` — User subscribed to a channel -- update cache and reactivate VPN if applicable., `on_user_left_channel` — User unsubscribed from a channel -- update cache and deactivate VPN if applicable., `register_handlers` — Register channel member event handlers on the dispatcher/router.
- `app/handlers/common.py` — Python-модуль
  Классы: нет
  Функции: `handle_delete_ban_notification` — Удаляет уведомление о бане при нажатии на кнопку, `handle_webhook_notification_close` — Удаляет webhook-уведомление при нажатии кнопки Закрыть., `handle_unknown_callback`, `handle_noop`, `handle_current_page`, `handle_cancel`, `handle_unknown_message`, `show_rules`, `register_handlers`
- `app/handlers/contests.py` — Python-модуль
  Классы: нет
  Функции: `open_contests_menu_message` — Send the contests menu as a fresh message — entry point for the, `cmd_contests` — `/contests` command: open the contests menu., `show_contests_menu` — Show menu with available contest games., `play_contest` — Start playing a specific contest., `handle_pick` — Handle button pick in contest games., `handle_text_answer` — Handle text answer in contest games., `register_handlers` — Register contest handlers.
- `app/handlers/gift_activation.py` — Python-модуль
  Классы: нет
  Функции: `handle_gift_activate` — Handle gift_activate:{purchase_id} callback from Telegram notification., `register_handlers`
- `app/handlers/menu.py` — Python-модуль
  Классы: нет
  Функции: `show_main_menu`, `handle_profile_unavailable`, `show_service_rules`, `show_info_menu`, `show_promo_groups_info`, `show_faq_pages`, `show_faq_page`, `show_privacy_policy`, `show_public_offer`, `show_info_page`, `show_language_menu`, `process_language_change`, `handle_back_to_menu`, `get_main_menu_text`, `handle_activate_button` — Умная кнопка активации — система сама решает что делать:, `register_handlers`
- `app/handlers/polls.py` — Python-модуль
  Классы: нет
  Функции: `handle_poll_start`, `handle_poll_answer`, `register_handlers`
- `app/handlers/promocode.py` — Python-модуль
  Классы: нет
  Функции: `show_promocode_menu`, `activate_promocode_for_registration` — Активирует промокод для пользователя., `process_promocode`, `handle_promo_subscription_select` — Handle subscription selection for promocode with days in multi-tariff., `register_handlers`
- `app/handlers/referral.py` — Python-модуль
  Классы: нет
  Функции: `show_referral_info`, `show_referral_qr`, `show_detailed_referral_list`, `show_referral_analytics`, `create_invite_message`, `show_withdrawal_info` — Показывает информацию о выводе реферального баланса., `start_withdrawal_request` — Начинает процесс оформления заявки на вывод., `process_withdrawal_amount` — Обрабатывает ввод суммы для вывода., `process_withdrawal_amount_callback` — Обрабатывает выбор суммы для вывода через кнопку., `process_payment_details` — Обрабатывает ввод реквизитов и показывает подтверждение., `confirm_withdrawal_request` — Подтверждает и создаёт заявку на вывод., `cancel_withdrawal_request` — Отменяет процесс создания заявки на вывод., `register_handlers`
- `app/handlers/referral_settings.py` — Python-модуль
  Классы: нет
  Функции: `show_reward_settings`, `set_reward_preference` — Сохранить, что получать. Неразрешённая настройка не сохраняется вовсе., `set_days_target` — Сохранить подписку для дней., `register_handlers`
- `app/handlers/server_status.py` — Python-модуль
  Классы: нет
  Функции: `show_server_status`, `change_server_status_page`, `register_handlers`
- `app/handlers/simple_subscription.py` — Python-модуль
  Классы: нет
  Функции: `start_simple_subscription_purchase` — Начинает процесс простой покупки подписки., `handle_simple_subscription_pay_with_balance` — Обрабатывает оплату простой подписки с баланса., `handle_simple_subscription_pay_with_balance_disabled` — Показывает уведомление, если баланса недостаточно для прямой оплаты., `handle_simple_subscription_other_payment_methods` — Обрабатывает выбор других способов оплаты., `handle_simple_subscription_payment_method` — Обрабатывает выбор метода оплаты для простой подписки., `check_simple_pal24_payment_status`, `check_simple_mulenpay_payment_status`, `check_simple_cryptobot_payment_status`, `check_simple_heleket_payment_status`, `check_simple_wata_payment_status`, `confirm_simple_subscription_purchase` — Обрабатывает подтверждение простой покупки подписки при наличии активной платной подписки., `register_simple_subscription_handlers` — Регистрирует обработчики простой покупки подписки.
- `app/handlers/stars_payments.py` — Python-модуль
  Классы: нет
  Функции: `handle_pre_checkout_query`, `handle_successful_payment`, `register_stars_handlers`
- `app/handlers/start.py` — Python-модуль
  Классы: нет
  Функции: `answer_menu_with_media` — Отвечает меню с медиа-шапкой на входящее сообщение (например, /start)., `send_menu_with_media` — Отправляет меню с медиа-шапкой: видео → фото-логотип → обычный текст., `handle_potential_referral_code`, `cmd_start`, `process_language_selection`, `process_rules_accept` — Обрабатывает принятие или отклонение правил пользователем., `process_privacy_policy_accept`, `process_referral_code_input`, `process_referral_code_skip`, `complete_registration_from_callback`, `complete_registration`, `get_referral_code_keyboard`, `get_main_menu_text`, `get_main_menu_text_simple`, `required_sub_channel_check`, `process_webauth_confirm` — Handle web auth confirmation or denial., `register_handlers`
- `app/handlers/subscription/`
- `app/handlers/support.py` — Python-модуль
  Классы: нет
  Функции: `show_support_info`, `register_handlers`
- `app/handlers/tickets.py` — Python-модуль
  Классы: `TicketStates`
  Функции: `show_ticket_priority_selection` — Начать создание тикета без выбора приоритета: сразу просим заголовок, `handle_ticket_title_input`, `handle_ticket_message_input`, `show_my_tickets`, `show_my_tickets_closed`, `view_ticket` — Показать детали тикета с пагинацией, `send_ticket_attachments`, `user_delete_message`, `reply_to_ticket` — Начать ответ на тикет, `handle_ticket_reply`, `close_ticket` — Закрыть тикет, `cancel_ticket_creation` — Отменить создание тикета, `cancel_ticket_reply` — Отменить ответ на тикет, `close_ticket_notification` — Закрыть уведомление о тикете, `notify_admins_about_new_ticket` — Уведомить админов о новом тикете, `notify_admins_about_ticket_reply` — Уведомить админов об ответе пользователя на тикет, `register_handlers` — Регистрация обработчиков тикетов
- `app/handlers/webhooks.py` — Python-модуль
  Классы: нет
  Функции: `set_webhook_bot` — Устанавливает экземпляр бота для отправки уведомлений об ошибках в webhook., `tribute_webhook`, `handle_successful_payment`, `handle_pre_checkout_query`

#### app/handlers/admin

- `app/handlers/admin/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/handlers/admin/backup.py` — Python-модуль
  Классы: `BackupStates`
  Функции: `get_backup_main_keyboard`, `get_backup_list_keyboard`, `get_backup_manage_keyboard`, `get_backup_settings_keyboard`, `show_backup_panel`, `create_backup_handler`, `show_backup_list`, `manage_backup_file`, `delete_backup_confirm`, `delete_backup_execute`, `restore_backup_start`, `restore_backup_execute`, `handle_backup_file_upload`, `show_backup_settings`, `toggle_backup_setting`, `register_handlers`
- `app/handlers/admin/blacklist.py` — Python-модуль
  Классы: нет
  Функции: `show_blacklist_settings` — Показывает настройки черного списка, `toggle_blacklist` — Переключает статус проверки черного списка, `update_blacklist` — Обновляет черный список из GitHub, `show_blacklist_users` — Показывает список пользователей в черном списке, `start_set_blacklist_url` — Начинает процесс установки URL к черному списку, `process_blacklist_url` — Обрабатывает введенный URL к черному списку, `register_blacklist_handlers` — Регистрация обработчиков черного списка
- `app/handlers/admin/blocked_users.py` — Python-модуль
  Классы: `BlockedUsersText`, `BlockedUsersCallback`, `BlockedUsersStates`
  Функции: `get_blocked_users_menu_keyboard` — Клавиатура главного меню модуля., `get_blocked_list_keyboard` — Клавиатура списка заблокированных пользователей., `get_confirm_keyboard` — Клавиатура подтверждения действия., `show_blocked_users_menu` — Показывает главное меню модуля заблокированных пользователей., `start_scan` — Запускает сканирование пользователей., `show_blocked_list` — Показывает список заблокированных пользователей., `handle_blocked_list_pagination` — Обрабатывает пагинацию списка заблокированных., `show_action_confirm` — Показывает подтверждение действия., `handle_action_delete_db` — Обрабатывает выбор удаления из БД., `handle_action_delete_remnawave` — Обрабатывает выбор удаления из Remnawave., `handle_action_delete_both` — Обрабатывает выбор полного удаления., `handle_action_mark` — Обрабатывает выбор пометки как заблокированных., `handle_confirm_action` — Выполняет подтвержденное действие., `handle_cancel` — Отменяет текущее действие и возвращает в меню., `register_handlers` — Регистрирует хендлеры модуля заблокированных пользователей.
- `app/handlers/admin/bot_configuration.py` — Python-модуль
  Классы: `BotConfigInputFilter` (2 методов)
  Функции: `start_settings_search`, `handle_search_query`, `show_presets`, `preview_preset`, `apply_preset`, `export_settings`, `start_import_settings`, `handle_import_message`, `show_settings_history`, `show_help`, `show_bot_config_menu`, `show_bot_config_group`, `show_bot_config_category`, `show_simple_subscription_squad_selector`, `select_simple_subscription_squad`, `test_remnawave_connection`, `test_payment_provider`, `show_bot_config_setting`, `start_edit_setting`, `handle_edit_setting`, `handle_direct_setting_input`, `reset_setting`, `toggle_setting`, `apply_setting_choice`, `show_remna_config_menu` — Show available Remnawave subscription page configs for selection., `select_remna_config` — Select a Remnawave subscription page config., `clear_remna_config` — Clear the Remnawave config, disabling guide mode until new config is selected., `register_handlers`
- `app/handlers/admin/bulk_ban.py` — Python-модуль
  Классы: нет
  Функции: `start_bulk_ban_process` — Начало процесса массовой блокировки пользователей, `process_bulk_ban_list` — Обработка списка Telegram ID и выполнение массовой блокировки, `register_bulk_ban_handlers` — Регистрация обработчиков команд для массовой блокировки
- `app/handlers/admin/campaigns.py` — Python-модуль
  Классы: нет
  Функции: `show_campaigns_menu`, `show_campaigns_overall_stats`, `show_campaigns_list`, `show_campaign_detail`, `show_campaign_edit_menu`, `start_edit_campaign_name`, `process_edit_campaign_name`, `start_edit_campaign_start_parameter`, `process_edit_campaign_start_parameter`, `start_edit_campaign_balance_bonus`, `process_edit_campaign_balance_bonus`, `start_edit_campaign_subscription_days`, `process_edit_campaign_subscription_days`, `start_edit_campaign_subscription_traffic`, `process_edit_campaign_subscription_traffic`, `start_edit_campaign_subscription_devices`, `process_edit_campaign_subscription_devices`, `start_edit_campaign_subscription_servers`, `toggle_edit_campaign_server`, `save_edit_campaign_subscription_servers`, `toggle_campaign_status`, `show_campaign_stats`, `confirm_delete_campaign`, `delete_campaign_confirmed`, `start_campaign_creation`, `process_campaign_name`, `process_campaign_start_parameter`, `select_campaign_bonus_type`, `process_campaign_balance_value`, `process_campaign_subscription_days`, `process_campaign_subscription_traffic`, `process_campaign_subscription_devices`, `toggle_campaign_server`, `finalize_campaign_subscription`, `select_campaign_tariff` — Обработка выбора тарифа для кампании., `process_campaign_tariff_days` — Обработка ввода длительности тарифа для кампании., `start_edit_campaign_tariff` — Начало редактирования тарифа кампании., `set_campaign_tariff` — Установка тарифа для кампании., `start_edit_campaign_tariff_days` — Начало редактирования длительности тарифа., `process_edit_campaign_tariff_days` — Обработка ввода новой длительности тарифа., `register_handlers`
- `app/handlers/admin/contests.py` — Python-модуль
  Классы: нет
  Функции: `show_contests_menu`, `show_referral_contests_menu`, `list_contests`, `show_contest_details`, `toggle_contest`, `prompt_edit_summary_times`, `process_edit_summary_times`, `delete_contest`, `show_leaderboard`, `start_contest_creation`, `select_contest_mode`, `process_title`, `process_description`, `process_prize`, `process_start_date`, `process_end_date`, `finalize_contest_creation`, `show_detailed_stats`, `show_detailed_stats_page`, `sync_contest` — Синхронизировать события конкурса с реальными платежами., `debug_contest_transactions` — Показать транзакции рефералов конкурса для отладки., `show_virtual_participants`, `start_add_virtual_participant`, `process_virtual_participant_name`, `process_virtual_participant_count`, `delete_virtual_participant_handler`, `start_mass_virtual_participants` — Начинает массовое создание виртуальных участников (массовка)., `process_mass_virtual_count` — Обрабатывает количество призраков для массового создания., `process_mass_virtual_referrals` — Создаёт массовку призраков с рандомными именами., `start_edit_virtual_participant`, `process_edit_virtual_participant_count`, `register_handlers`
- `app/handlers/admin/coupons.py` — Python-модуль
  Классы: нет
  Функции: `show_coupons_menu`, `handle_coupon_list_page`, `start_coupon_batch_creation`, `select_coupon_batch_tariff`, `process_coupon_batch_days`, `process_coupon_batch_count`, `process_coupon_batch_name`, `process_coupon_batch_price`, `process_coupon_batch_expiry`, `process_coupon_batch_per_user`, `confirm_coupon_batch_creation`, `show_coupon_batch`, `export_coupon_batch`, `ask_revoke_coupon_batch`, `confirm_revoke_coupon_batch`, `ask_delete_coupon_batch`, `confirm_delete_coupon_batch`, `register_handlers`
- `app/handlers/admin/daily_contests.py` — Python-модуль
  Классы: нет
  Функции: `show_daily_contests`, `show_daily_contest`, `toggle_daily_contest`, `start_round_now`, `manual_start_round`, `prompt_edit_field`, `process_edit_field`, `edit_payload`, `process_payload`, `start_all_contests`, `close_all_rounds`, `reset_all_attempts`, `reset_attempts`, `close_round`, `register_handlers`
- `app/handlers/admin/display_mode_button.py` — Python-модуль
  Классы: нет
  Функции: `cycle_display_mode_setting`
- `app/handlers/admin/faq.py` — Python-модуль
  Классы: нет
  Функции: `show_faq_management`, `toggle_faq`, `cycle_faq_display_mode`, `start_create_faq_page`, `cancel_faq_creation`, `process_new_faq_title`, `process_new_faq_content`, `show_faq_page_details`, `start_edit_faq_title`, `process_edit_faq_title`, `start_edit_faq_content`, `process_edit_faq_content`, `toggle_faq_page`, `delete_faq_page`, `move_faq_page`, `show_faq_html_help`, `register_handlers`
- `app/handlers/admin/main.py` — Python-модуль
  Классы: нет
  Функции: `show_admin_panel`, `show_users_submenu`, `show_promo_submenu`, `show_communications_submenu`, `show_support_submenu`, `show_moderator_panel`, `show_support_audit`, `show_settings_submenu`, `show_system_submenu`, `clear_rules_command`, `rules_stats_command`, `admin_commands_help`, `register_handlers`
- `app/handlers/admin/maintenance.py` — Python-модуль
  Классы: `MaintenanceStates`
  Функции: `show_maintenance_panel`, `toggle_maintenance_mode`, `process_maintenance_reason`, `toggle_monitoring`, `force_api_check`, `check_panel_status`, `send_manual_notification`, `handle_manual_notification`, `process_notification_message`, `back_to_admin_panel`, `register_handlers`
- `app/handlers/admin/messages.py` — Python-модуль
  Классы: нет
  Функции: `safe_edit_or_send_text` — Безопасно редактирует сообщение или удаляет и отправляет новое., `get_message_buttons_selector_keyboard`, `get_updated_message_buttons_selector_keyboard`, `create_broadcast_keyboard`, `show_messages_menu`, `show_pinned_message_menu`, `prompt_pinned_message_update`, `toggle_pinned_message_position`, `toggle_pinned_message_start_mode`, `delete_pinned_message`, `process_pinned_message_update`, `handle_pinned_broadcast_now` — Разослать закреплённое сообщение сейчас всем пользователям., `handle_pinned_broadcast_skip` — Пропустить рассылку — пользователи увидят при /start., `show_broadcast_targets`, `show_tariff_filter` — Показывает список тарифов для фильтрации рассылки., `show_messages_history`, `show_custom_broadcast`, `select_custom_criteria`, `select_broadcast_target`, `process_broadcast_message`, `handle_media_selection`, `process_broadcast_media`, `show_media_preview`, `handle_media_confirmation`, `handle_change_media`, `show_button_selector_callback`, `show_button_selector`, `toggle_button_selection`, `confirm_button_selection`, `confirm_broadcast`, `get_target_users_count` — Быстрый подсчёт пользователей через SQL COUNT вместо загрузки всех в память., `get_target_users`, `get_custom_users_count`, `get_custom_users`, `get_users_statistics`, `get_target_name`, `get_target_display_name`, `register_handlers`
- `app/handlers/admin/monitoring.py` — Python-модуль
  Классы: нет
  Функции: `admin_monitoring_menu`, `admin_monitoring_settings`, `admin_notify_settings`, `toggle_trial_channel_notification`, `preview_trial_channel_notification`, `toggle_expired_1d_notification`, `preview_expired_1d_notification`, `toggle_second_wave_notification`, `preview_second_wave_notification`, `toggle_third_wave_notification`, `preview_third_wave_notification`, `preview_all_notifications`, `edit_second_wave_percent`, `edit_second_wave_hours`, `edit_third_wave_percent`, `edit_third_wave_hours`, `edit_third_wave_threshold`, `start_monitoring_callback`, `stop_monitoring_callback`, `force_check_callback`, `traffic_check_callback` — Ручная проверка трафика — использует snapshot и дельту., `monitoring_logs_callback`, `clear_logs_callback`, `test_notifications_callback`, `monitoring_statistics_callback`, `nalogo_force_process_callback` — Принудительная отправка чеков из очереди., `nalogo_pending_callback` — Просмотр чеков ожидающих ручной проверки., `nalogo_mark_verified_callback` — Пометить чек как созданный в налоговой., `nalogo_retry_callback` — Повторно отправить чек в налоговую., `nalogo_clear_pending_callback` — Очистить всю очередь проверки., `receipts_missing_callback` — Сверка чеков по логам., `receipts_link_old_callback` — Привязать старые чеки из NaloGO к транзакциям по сумме и дате., `receipts_reconcile_menu_callback` — Меню выбора периода сверки., `receipts_reconcile_logs_refresh_callback` — Обновить сверку по логам., `receipts_reconcile_logs_details_callback` — Детальный список платежей без чеков., `get_monitoring_logs_keyboard`, `get_monitoring_logs_back_keyboard`, `monitoring_command`, `process_notification_value_input`, `admin_traffic_settings` — Показывает настройки мониторинга трафика., `toggle_fast_check` — Переключает быструю проверку трафика., `toggle_daily_check` — Переключает суточную проверку трафика., `edit_fast_interval` — Начинает редактирование интервала быстрой проверки., `edit_fast_threshold` — Начинает редактирование порога быстрой проверки., `edit_daily_time` — Начинает редактирование времени суточной проверки., `edit_daily_threshold` — Начинает редактирование суточного порога., `edit_cooldown` — Начинает редактирование кулдауна уведомлений., `process_traffic_setting_input` — Обрабатывает ввод настройки мониторинга трафика., `register_handlers`
- `app/handlers/admin/overpay_certificate.py` — Python-модуль
  Классы: `OverpayCertStates`
  Функции: `show_certificate_status`, `start_certificate_upload`, `confirm_certificate_delete`, `delete_certificate`, `process_certificate_file`, `process_certificate_passphrase`, `register_handlers`
- `app/handlers/admin/payments.py` — Python-модуль
  Классы: нет
  Функции: `show_payments_overview`, `show_payment_details`, `manual_check_payment`, `check_all_payments` — Массовая проверка всех ожидающих платежей., `export_payments` — Экспорт данных платежей в JSON файл., `register_handlers`
- `app/handlers/admin/polls.py` — Python-модуль
  Классы: `PollCreationStates`
  Функции: `show_polls_panel`, `start_poll_creation`, `process_poll_title`, `process_poll_description`, `process_poll_reward`, `process_poll_question`, `show_poll_details`, `start_poll_send`, `show_custom_target_menu`, `select_poll_target`, `select_custom_poll_target`, `confirm_poll_send`, `show_poll_stats`, `confirm_poll_delete`, `delete_poll_handler`, `register_handlers`
- `app/handlers/admin/pricing.py` — Python-модуль
  Классы: `ChoiceOption` (1 методов), `SettingEntry` (2 методов)
  Функции: `show_pricing_menu`, `show_pricing_section`, `start_price_edit`, `start_setting_edit`, `process_pricing_input`, `toggle_setting`, `select_setting_choice`, `toggle_traffic_package`, `toggle_period_option`, `register_handlers`
- `app/handlers/admin/privacy_policy.py` — Python-модуль
  Классы: нет
  Функции: `show_privacy_policy_management`, `toggle_privacy_policy`, `cycle_privacy_policy_display_mode`, `start_edit_privacy_policy`, `cancel_edit_privacy_policy`, `process_privacy_policy_edit`, `view_privacy_policy`, `show_privacy_policy_html_help`, `register_handlers`
- `app/handlers/admin/promo_groups.py` — Python-модуль
  Классы: нет
  Функции: `show_promo_groups_menu`, `show_promo_group_details`, `start_create_promo_group`, `process_create_group_name`, `process_create_group_priority`, `process_create_group_traffic`, `process_create_group_servers`, `process_create_group_devices`, `process_create_group_period_discounts`, `process_create_group_auto_assign`, `start_edit_promo_group`, `prompt_edit_promo_group_field`, `process_edit_group_name`, `process_edit_group_priority`, `process_edit_group_traffic`, `process_edit_group_servers`, `process_edit_group_devices`, `process_edit_group_period_discounts`, `process_edit_group_auto_assign`, `show_promo_group_members`, `request_delete_promo_group`, `delete_promo_group_confirmed`, `toggle_promo_group_addon_discounts`, `register_handlers`
- `app/handlers/admin/promo_offers.py` — Python-модуль
  Классы: нет
  Функции: `show_promo_offers_menu`, `show_promo_offer_details`, `show_promo_offer_logs`, `prompt_edit_message`, `prompt_edit_button`, `prompt_edit_valid`, `prompt_edit_discount`, `prompt_edit_active_duration`, `prompt_edit_duration`, `prompt_edit_squads`, `show_send_segments`, `show_send_user_list`, `prompt_send_user_search`, `reset_send_user_search`, `back_to_user_list`, `process_send_user_search`, `show_selected_user_details`, `send_offer_to_segment`, `send_offer_to_user`, `process_edit_message_text`, `process_edit_button_text`, `process_edit_valid_hours`, `process_edit_active_duration_hours`, `process_edit_discount_percent`, `process_edit_test_duration`, `paginate_squad_selection`, `select_squad_for_template`, `clear_squad_for_template`, `back_to_offer_from_squads`, `register_handlers`
- `app/handlers/admin/promocodes.py` — Python-модуль
  Классы: нет
  Функции: `show_promocodes_menu`, `show_promocodes_list`, `show_promocodes_list_page` — Обработчик пагинации списка промокодов., `show_promocode_management`, `show_promocode_edit_menu`, `start_edit_promocode_date`, `start_edit_promocode_amount`, `start_edit_promocode_days`, `start_edit_promocode_uses`, `start_promocode_creation`, `select_promocode_type`, `process_promocode_code`, `process_promo_group_selection` — Handle promo group selection for promocode, `process_promocode_value`, `process_promocode_combo_days` — Шаг 2 комбинированного промокода (BALANCE_AND_DAYS): ввод дней подписки., `handle_edit_value`, `process_promocode_uses`, `handle_edit_uses`, `process_promocode_expiry`, `process_discount_hours` — Обработчик ввода срока действия скидки в часах для DISCOUNT промокода., `handle_edit_expiry`, `toggle_promocode_status`, `toggle_promocode_first_purchase` — Переключает режим 'только для первой покупки'., `confirm_delete_promocode`, `delete_promocode_confirmed`, `show_promocode_stats`, `show_general_promocode_stats`, `register_handlers`
- `app/handlers/admin/public_offer.py` — Python-модуль
  Классы: нет
  Функции: `show_public_offer_management`, `toggle_public_offer`, `cycle_public_offer_display_mode`, `start_edit_public_offer`, `cancel_edit_public_offer`, `process_public_offer_edit`, `view_public_offer`, `show_public_offer_html_help`, `register_handlers`
- `app/handlers/admin/quick_amounts.py` — Python-модуль
  Классы: `QuickAmountsStates`
  Функции: `show_quick_amounts_list`, `view_quick_amounts`, `disable_quick_amounts` — Полностью убирает кнопки быстрых сумм: пользователь вводит сумму вручную., `start_edit_quick_amounts`, `reset_quick_amounts`, `process_quick_amounts`, `register_handlers`
- `app/handlers/admin/referral_levels.py` — Python-модуль
  Классы: нет
  Функции: `show_reward_levels`, `toggle_reward_scheme` — Переключить схему наград., `add_reward_level`, `import_legacy_settings` — Перенести действующие настройки ``REFERRAL_*`` в уровень 1., `show_reward_level`, `toggle_level_active`, `cycle_level_mode` — Перебрать активные бонусы уровня: деньги → дни → оба., `cycle_level_trigger`, `confirm_delete_level` — Спросить перед удалением., `toggle_threshold_population` — Кого считать при проверке порога: всех приглашённых или только с пополнением., `delete_level`, `choose_level_tariff`, `set_level_tariff`, `start_level_value_edit`, `process_level_value`, `toggle_levels_mode` — Переключить, что означает номер уровня: глубину цепочки или ранг партнёра., `toggle_user_choice` — Разрешить или запретить пользователю выбирать вид награды и подписку для дней., `start_depth_edit` — Правка глубины обхода цепочки., `process_depth_value`, `register_handlers`
- `app/handlers/admin/referrals.py` — Python-модуль
  Классы: нет
  Функции: `show_referral_statistics`, `show_top_referrers` — Показывает топ рефереров (по умолчанию: неделя, по заработку)., `show_top_referrers_filtered` — Обрабатывает выбор периода и сортировки., `show_referral_settings`, `show_pending_withdrawal_requests` — Показывает список ожидающих заявок на вывод., `view_withdrawal_request` — Показывает детали заявки на вывод., `approve_withdrawal_request` — Одобряет заявку на вывод., `reject_withdrawal_request` — Отклоняет заявку на вывод., `complete_withdrawal_request` — Отмечает заявку как выполненную (деньги переведены)., `start_test_referral_earning` — Начинает процесс тестового начисления реферального дохода., `process_test_referral_earning` — Обрабатывает ввод тестового начисления., `show_referral_diagnostics` — Показывает диагностику реферальной системы по логам., `preview_referral_fixes` — Показывает предпросмотр исправлений потерянных рефералов., `apply_referral_fixes` — Применяет исправления потерянных рефералов., `check_missing_bonuses` — Проверяет по БД — всем ли рефералам начислены бонусы., `apply_missing_bonuses` — Применяет начисление пропущенных бонусов., `sync_referrals_with_contest` — Синхронизирует всех рефералов с активными конкурсами., `request_log_file_upload` — Запрашивает загрузку лог-файла для анализа., `receive_log_file` — Получает и анализирует загруженный лог-файл., `register_handlers`
- `app/handlers/admin/remnawave.py` — Python-модуль
  Классы: нет
  Функции: `show_squad_migration_menu`, `paginate_migration_source`, `handle_migration_source_selection`, `paginate_migration_target`, `handle_migration_target_selection`, `change_migration_target`, `confirm_squad_migration`, `cancel_squad_migration`, `handle_migration_page_info`, `show_remnawave_menu`, `show_system_stats`, `show_traffic_stats`, `show_nodes_management`, `show_node_details`, `manage_node`, `show_node_statistics`, `show_squad_details`, `manage_squad_action`, `show_squad_edit_menu`, `show_squad_inbounds_selection`, `show_squad_rename_form`, `cancel_squad_rename`, `process_squad_new_name`, `toggle_squad_inbound`, `save_squad_inbounds`, `show_squad_edit_menu_short`, `start_squad_creation`, `process_squad_name`, `toggle_create_inbound`, `finish_squad_creation`, `cancel_squad_creation`, `restart_all_nodes`, `show_sync_options`, `show_auto_sync_settings`, `toggle_auto_sync_setting`, `prompt_auto_sync_schedule`, `cancel_auto_sync_schedule`, `run_auto_sync_now`, `save_auto_sync_schedule`, `sync_all_users` — Выполняет полную синхронизацию всех пользователей, `sync_users_to_panel`, `show_sync_recommendations`, `validate_subscriptions`, `cleanup_subscriptions`, `force_cleanup_all_orphaned_users`, `confirm_force_cleanup`, `sync_users`, `show_squads_management`, `register_handlers`
- `app/handlers/admin/reports.py` — Python-модуль
  Классы: нет
  Функции: `show_reports_menu`, `send_daily_report`, `send_weekly_report`, `send_monthly_report`, `close_report_message`, `register_handlers`
- `app/handlers/admin/required_channels.py` — Python-модуль
  Классы: `AddChannelStates`
  Функции: `show_channels_list`, `view_channel`, `toggle_channel_handler`, `delete_channel_handler`, `start_add_channel`, `process_channel_id`, `process_channel_link`, `process_channel_title`, `register_handlers`
- `app/handlers/admin/rules.py` — Python-модуль
  Классы: нет
  Функции: `show_rules_management`, `cycle_rules_display_mode`, `view_current_rules`, `start_edit_rules`, `process_rules_edit`, `save_rules`, `clear_rules_confirmation`, `confirm_clear_rules`, `show_html_help`, `register_handlers`
- `app/handlers/admin/servers.py` — Python-модуль
  Классы: нет
  Функции: `show_servers_menu`, `show_servers_list`, `sync_servers_with_remnawave`, `show_server_edit_menu`, `show_server_users`, `toggle_server_availability`, `toggle_server_trial_assignment`, `start_server_edit_price`, `process_server_price_edit`, `start_server_edit_name`, `process_server_name_edit`, `delete_server_confirm`, `delete_server_execute`, `show_server_detailed_stats`, `start_server_edit_country`, `process_server_country_edit`, `start_server_edit_limit`, `process_server_limit_edit`, `start_server_edit_description`, `process_server_description_edit`, `start_server_edit_promo_groups`, `toggle_server_promo_group`, `save_server_promo_groups`, `sync_server_user_counts_handler`, `handle_servers_pagination`, `register_handlers`
- `app/handlers/admin/statistics.py` — Python-модуль
  Классы: нет
  Функции: `show_statistics_menu`, `show_users_statistics`, `show_subscriptions_statistics`, `show_revenue_statistics`, `show_referral_statistics`, `show_summary_statistics`, `show_revenue_by_period`, `register_handlers`
- `app/handlers/admin/subscriptions.py` — Python-модуль
  Классы: нет
  Функции: `get_country_flag`, `get_users_by_countries` — Распределение активных пользователей по странам подключённых серверов., `show_subscriptions_menu`, `show_subscriptions_list`, `show_expiring_subscriptions`, `show_subscriptions_stats`, `show_countries_management`, `send_expiry_reminders`, `handle_subscriptions_pagination`, `register_handlers`
- `app/handlers/admin/support_settings.py` — Python-модуль
  Классы: `SupportAdvancedStates`
  Функции: `show_support_settings`, `toggle_support_menu`, `toggle_admin_notifications`, `toggle_user_notifications`, `toggle_sla`, `start_set_sla_minutes`, `handle_sla_minutes`, `start_add_moderator`, `start_remove_moderator`, `handle_moderator_id`, `list_moderators`, `set_mode_tickets`, `set_mode_contact`, `set_mode_both`, `start_edit_desc`, `handle_new_desc`, `send_desc_copy`, `delete_sent_message`, `register_handlers`
- `app/handlers/admin/system_logs.py` — Python-модуль
  Классы: нет
  Функции: `show_system_logs`, `refresh_system_logs`, `download_system_logs`, `register_handlers`
- `app/handlers/admin/tariff_custom_traffic.py` — Python-модуль
  Классы: нет
  Функции: `format_custom_traffic_settings` — Format custom-traffic state for the main tariff card., `render_custom_traffic_settings` — Render the dedicated custom-traffic settings screen., `get_custom_traffic_keyboard` — Build the dedicated custom-traffic settings keyboard., `show_custom_traffic_settings` — Show custom-traffic settings for a tariff and leave any field-edit state., `toggle_custom_traffic` — Enable or disable custom traffic after validating stored settings., `start_edit_custom_traffic_price` — Start editing the custom-traffic price per gigabyte., `start_edit_custom_traffic_min` — Start editing the minimum selectable traffic amount., `start_edit_custom_traffic_max` — Start editing the maximum selectable traffic amount., `process_custom_traffic_price_input` — Persist a validated custom-traffic price per gigabyte., `process_custom_traffic_min_input` — Persist a validated minimum selectable traffic amount., `process_custom_traffic_max_input` — Persist a validated maximum selectable traffic amount., `register_custom_traffic_handlers` — Register callbacks and FSM handlers for custom-traffic administration.
- `app/handlers/admin/tariffs.py` — Python-модуль
  Классы: нет
  Функции: `get_tariffs_list_keyboard` — Создает клавиатуру списка тарифов., `get_tariff_view_keyboard` — Создает клавиатуру просмотра тарифа., `format_tariff_info` — Форматирует информацию о тарифе., `show_tariffs_list` — Показывает список тарифов., `show_tariffs_page` — Показывает страницу списка тарифов., `view_tariff` — Просмотр тарифа., `toggle_tariff` — Переключает активность тарифа., `toggle_trial_tariff` — Переключает тариф как триальный., `toggle_daily_tariff` — Переключает суточный режим тарифа., `start_edit_daily_price` — Начинает редактирование суточной цены., `process_daily_price_input` — Обрабатывает ввод суточной цены (создание и редактирование)., `start_create_tariff` — Начинает создание тарифа., `process_tariff_name` — Обрабатывает название тарифа., `process_tariff_traffic` — Обрабатывает лимит трафика., `process_tariff_devices` — Обрабатывает лимит устройств., `process_tariff_tier` — Обрабатывает уровень тарифа., `select_tariff_type_periodic` — Выбирает периодный тип тарифа., `select_tariff_type_daily` — Выбирает суточный тип тарифа., `process_tariff_prices` — Обрабатывает цены тарифа., `start_edit_tariff_name` — Начинает редактирование названия тарифа., `process_edit_tariff_name` — Обрабатывает новое название тарифа., `start_edit_tariff_description` — Начинает редактирование описания тарифа., `process_edit_tariff_description` — Обрабатывает новое описание тарифа., `start_edit_tariff_traffic` — Начинает редактирование трафика тарифа., `process_edit_tariff_traffic` — Обрабатывает новый лимит трафика., `start_edit_tariff_devices` — Начинает редактирование лимита устройств., `process_edit_tariff_devices` — Обрабатывает новый лимит устройств., `start_edit_tariff_tier` — Начинает редактирование уровня тарифа., `process_edit_tariff_tier` — Обрабатывает новый уровень тарифа., `start_edit_tariff_prices` — Начинает редактирование цен тарифа., `process_edit_tariff_prices` — Обрабатывает новые цены тарифа., `start_edit_tariff_device_price` — Начинает редактирование цены за устройство., `process_edit_tariff_device_price` — Обрабатывает новую цену за устройство., `start_edit_tariff_max_devices` — Начинает редактирование макс. устройств., `process_edit_tariff_max_devices` — Обрабатывает новое макс. кол-во устройств., `start_edit_tariff_trial_days` — Начинает редактирование дней триала., `process_edit_tariff_trial_days` — Обрабатывает новое количество дней триала., `start_edit_tariff_traffic_topup` — Показывает меню настройки докупки трафика., `toggle_tariff_traffic_topup` — Переключает включение/выключение докупки трафика., `start_edit_traffic_topup_packages` — Начинает редактирование пакетов докупки трафика., `process_edit_traffic_topup_packages` — Обрабатывает новые пакеты докупки трафика., `start_edit_max_topup_traffic` — Начинает редактирование максимального лимита докупки трафика., `process_edit_max_topup_traffic` — Обрабатывает новое значение максимального лимита докупки трафика., `confirm_delete_tariff` — Запрашивает подтверждение удаления тарифа., `delete_tariff_confirmed` — Удаляет тариф после подтверждения., `start_edit_tariff_squads` — Показывает меню выбора серверов для тарифа., `toggle_tariff_squad` — Переключает выбор сервера для тарифа., `clear_tariff_squads` — Очищает список серверов тарифа., `select_all_tariff_squads` — Выбирает все серверы для тарифа., `start_edit_tariff_promo_groups` — Показывает меню выбора промогрупп для тарифа., `toggle_tariff_promo_group` — Переключает выбор промогруппы для тарифа., `clear_tariff_promo_groups` — Очищает список промогрупп тарифа., `get_traffic_reset_mode_keyboard` — Создает клавиатуру для выбора режима сброса трафика., `start_edit_traffic_reset_mode` — Начинает редактирование режима сброса трафика., `set_traffic_reset_mode` — Устанавливает режим сброса трафика для тарифа., `register_handlers` — Регистрирует обработчики для управления тарифами.
- `app/handlers/admin/tickets.py` — Python-модуль
  Классы: нет
  Функции: `show_admin_tickets` — Показать все тикеты для админов, `view_admin_ticket` — Показать детали тикета для админа с пагинацией, `reply_to_admin_ticket` — Начать ответ на тикет от админа, `handle_admin_ticket_reply`, `mark_ticket_as_answered` — Отметить тикет как отвеченный, `close_all_open_admin_tickets` — Закрыть все открытые тикеты., `close_admin_ticket` — Закрыть тикет админом, `cancel_admin_ticket_reply` — Отменить ответ админа на тикет, `block_user_in_ticket`, `handle_admin_block_duration_input`, `unblock_user_in_ticket`, `block_user_permanently`, `notify_user_about_ticket_reply` — Уведомить пользователя о новом ответе в тикете, `register_handlers` — Регистрация админских обработчиков тикетов
- `app/handlers/admin/trials.py` — Python-модуль
  Классы: нет
  Функции: `show_trials_panel`, `reset_trials`, `register_handlers`
- `app/handlers/admin/updates.py` — Python-модуль
  Классы: нет
  Функции: `get_updates_keyboard`, `get_version_info_keyboard`, `show_updates_menu`, `check_updates`, `show_version_info`, `register_handlers`
- `app/handlers/admin/user_messages.py` — Python-модуль
  Классы: `UserMessageStates`
  Функции: `get_user_messages_keyboard`, `get_message_actions_keyboard`, `show_user_messages_panel`, `add_user_message_start`, `process_new_message_text`, `list_user_messages`, `view_user_message`, `toggle_message_status`, `delete_message_confirm` — Подтвердить удаление сообщения, `show_messages_stats`, `edit_user_message_start`, `process_edit_message_text`, `register_handlers`
- `app/handlers/admin/users.py` — Python-модуль
  Классы: `UserFilterType`, `UserFilterConfig`
  Функции: `show_users_menu`, `show_users_filters`, `show_users_list`, `show_users_list_by_balance` — Список пользователей, отсортированный по балансу (убывание)., `show_users_ready_to_renew` — Показывает пользователей с истекшей подпиской и балансом >= порога., `show_potential_customers` — Показывает пользователей без активной подписки с балансом >= месячной цены., `show_users_list_by_campaign` — Список пользователей по кампании регистрации., `handle_users_list_pagination_fixed`, `handle_users_balance_list_pagination`, `handle_users_ready_to_renew_pagination`, `handle_potential_customers_pagination`, `handle_users_campaign_list_pagination`, `start_user_search`, `show_users_statistics`, `show_user_subscription`, `admin_select_user_subscription` — Handle subscription picker selection in multi-tariff mode., `show_user_transactions`, `confirm_user_delete`, `delete_user_account`, `process_user_search`, `show_user_management`, `show_user_referrals`, `start_edit_referral_percent`, `set_referral_percent_button`, `process_referral_percent_input`, `start_edit_user_referrals`, `process_edit_user_referrals`, `show_user_promo_group`, `set_user_promo_group`, `start_balance_edit`, `start_send_user_message`, `process_send_user_message`, `process_balance_edit`, `confirm_user_block`, `block_user`, `show_user_restrictions` — Показать меню управления ограничениями пользователя., `toggle_user_restriction_topup` — Переключить ограничение на пополнение баланса., `toggle_user_restriction_subscription` — Переключить ограничение на продление/покупку подписки., `ask_restriction_reason` — Запросить ввод причины ограничения., `save_restriction_reason` — Сохранить причину ограничения., `clear_user_restrictions` — Снять все ограничения с пользователя., `show_inactive_users`, `confirm_user_unblock`, `unblock_user`, `show_user_statistics`, `get_detailed_referral_stats`, `extend_user_subscription`, `process_subscription_extension_days`, `process_subscription_extension_text`, `add_subscription_traffic`, `process_traffic_addition_button`, `process_traffic_addition_text`, `deactivate_user_subscription`, `confirm_subscription_deactivation`, `reset_user_subscription` — Подтверждение полного обнуления подписки (с сохранением пользователя и тикетов)., `confirm_subscription_reset`, `delete_user_subscription` — Show confirmation for deleting a subscription (multi-tariff only)., `confirm_subscription_deletion` — Delete a subscription permanently (multi-tariff only)., `activate_user_subscription`, `grant_trial_subscription`, `grant_paid_subscription`, `process_subscription_grant_days`, `process_subscription_grant_text`, `show_user_servers_management`, `show_server_selection`, `toggle_user_server`, `refresh_server_selection_screen`, `start_devices_edit`, `set_user_devices_button`, `process_devices_edit_text`, `start_traffic_edit`, `set_user_traffic_button`, `process_traffic_edit_text`, `confirm_reset_devices`, `reset_user_devices`, `cleanup_inactive_users`, `change_subscription_type`, `admin_buy_subscription`, `admin_buy_subscription_confirm`, `admin_buy_subscription_execute`, `admin_buy_tariff` — Показывает список тарифов для покупки админом., `admin_buy_tariff_period` — Показывает выбор периода для тарифа., `admin_buy_tariff_confirm` — Подтверждение покупки тарифа., `admin_buy_tariff_execute` — Выполняет покупку тарифа для пользователя., `change_subscription_type_confirm`, `show_admin_tariff_change` — Показывает список доступных тарифов для смены., `select_admin_tariff_change` — Подтверждение выбора тарифа., `confirm_admin_tariff_change` — Применяет смену тарифа., `show_admin_user_autopay` — Admin view of a subscription's autopay settings., `toggle_admin_user_autopay` — Admin: flip autopay_enabled on a subscription., `show_admin_user_autopay_days` — Admin: show days-before picker., `set_admin_user_autopay_days` — Admin: persist days-before choice., `show_admin_user_autopay_period` — Admin: show period picker., `set_admin_user_autopay_period` — Admin: persist period choice. Value `0` means clear (use default)., `register_handlers`
- `app/handlers/admin/welcome_text.py` — Python-модуль
  Классы: нет
  Функции: `validate_html_tags` — Проверяет HTML-теги в тексте на соответствие требованиям Telegram API., `get_telegram_formatting_info`, `show_welcome_text_panel`, `toggle_welcome_text`, `show_current_welcome_text`, `show_placeholders_help`, `show_formatting_help`, `start_edit_welcome_text`, `process_welcome_text_edit`, `reset_welcome_text`, `show_preview_welcome_text`, `register_welcome_text_handlers`

#### app/handlers/balance

- `app/handlers/balance/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/handlers/balance/antilopay.py` — Python-модуль
  Классы: нет
  Функции: `process_antilopay_payment_amount` — Process payment amount directly., `start_antilopay_topup`, `start_antilopay_sbp_topup`, `start_antilopay_card_topup`, `start_antilopay_sberpay_topup`
- `app/handlers/balance/aurapay.py` — Python-модуль
  Классы: нет
  Функции: `process_aurapay_payment_amount` — Process payment amount directly., `start_aurapay_topup`, `start_aurapay_sbp_topup`, `start_aurapay_card_topup`
- `app/handlers/balance/cispay.py` — Python-модуль
  Классы: нет
  Функции: `process_cispay_payment_amount` — Обрабатывает сумму, введённую пользователем для cisPay., `start_cispay_topup`, `start_cispay_card_topup`, `start_cispay_sbp_topup`
- `app/handlers/balance/cloudpayments.py` — Python-модуль
  Классы: нет
  Функции: `process_cloudpayments_payment_amount` — Process payment amount directly., `start_cloudpayments_payment` — Start CloudPayments payment flow., `process_cloudpayments_amount` — Process entered amount and create CloudPayments payment.
- `app/handlers/balance/cryptobot.py` — Python-модуль
  Классы: нет
  Функции: `start_cryptobot_payment`, `process_cryptobot_payment_amount`, `check_cryptobot_payment_status`
- `app/handlers/balance/donut.py` — Python-модуль
  Классы: нет
  Функции: `process_donut_payment_amount` — Обрабатывает сумму, введённую пользователем для Donut., `start_donut_topup`, `start_donut_card_topup`, `start_donut_sbp_topup`, `start_donut_sbp_qr_topup`
- `app/handlers/balance/etoplatezhi.py` — Python-модуль
  Классы: нет
  Функции: `process_etoplatezhi_payment_amount` — Process payment amount directly., `start_etoplatezhi_topup`, `start_etoplatezhi_sbp_topup`, `start_etoplatezhi_card_topup`
- `app/handlers/balance/freekassa.py` — Python-модуль
  Классы: нет
  Функции: `process_freekassa_payment_amount` — Process payment amount directly., `start_freekassa_topup`, `start_freekassa_sbp_topup`, `start_freekassa_card_topup`, `process_freekassa_custom_amount` — Process custom amount input for Freekassa payment.
- `app/handlers/balance/heleket.py` — Python-модуль
  Классы: нет
  Функции: `start_heleket_payment`, `process_heleket_payment_amount`, `check_heleket_payment_status`
- `app/handlers/balance/jupiter.py` — Python-модуль
  Классы: нет
  Функции: `process_jupiter_payment_amount` — Обрабатывает сумму, введённую пользователем для Jupiter., `start_jupiter_topup`, `start_jupiter_sbp_topup`
- `app/handlers/balance/kassa_ai.py` — Python-модуль
  Классы: нет
  Функции: `process_kassa_ai_payment_amount` — Process payment amount directly (called from custom_amount handlers)., `start_kassa_ai_topup` — Start KassaAI top-up process - ask for amount., `process_kassa_ai_custom_amount` — Process custom amount input for KassaAI payment., `start_kassa_ai_sbp_topup` — Start KassaAI SBP top-up process., `start_kassa_ai_card_topup` — Start KassaAI Card top-up process., `start_kassa_ai_sberpay_topup` — Start KassaAI SberPay top-up process.
- `app/handlers/balance/lava.py` — Python-модуль
  Классы: нет
  Функции: `process_lava_payment_amount` — Обрабатывает сумму для Lava., `start_lava_topup`, `start_lava_card_topup`, `start_lava_sbp_topup`
- `app/handlers/balance/main.py` — Python-модуль
  Классы: нет
  Функции: `route_payment_by_method` — Роутер платежей по методу оплаты., `show_balance_menu`, `show_balance_history`, `handle_balance_history_pagination`, `show_payment_methods`, `handle_payment_methods_unavailable`, `handle_successful_topup_with_cart`, `request_support_topup`, `process_topup_amount`, `handle_sbp_payment`, `handle_topup_amount_callback`, `register_balance_handlers`
- `app/handlers/balance/mulenpay.py` — Python-модуль
  Классы: нет
  Функции: `start_mulenpay_payment`, `process_mulenpay_payment_amount`, `check_mulenpay_payment_status`
- `app/handlers/balance/overpay.py` — Python-модуль
  Классы: нет
  Функции: `process_overpay_payment_amount`, `start_overpay_topup`, `start_overpay_fps_topup`, `start_overpay_card_topup`, `start_overpay_int_topup`
- `app/handlers/balance/pal24.py` — Python-модуль
  Классы: нет
  Функции: `start_pal24_payment`, `process_pal24_payment_amount`, `handle_pal24_method_selection`, `check_pal24_payment_status`
- `app/handlers/balance/paritypay.py` — Python-модуль
  Классы: нет
  Функции: `process_paritypay_payment_amount` — Обрабатывает сумму, введённую пользователем для ParityPay., `start_paritypay_topup`, `start_paritypay_card_topup`, `start_paritypay_sbp_topup`
- `app/handlers/balance/paypear.py` — Python-модуль
  Классы: нет
  Функции: `process_paypear_payment_amount` — Process payment amount directly., `start_paypear_topup` — Start PayPear top-up process - ask for amount.
- `app/handlers/balance/platega.py` — Python-модуль
  Классы: нет
  Функции: `start_platega_payment`, `handle_platega_method_selection`, `start_platega_direct_method` — Handle direct Platega method selection from the main payment screen (inline mode)., `process_platega_payment_amount`, `check_platega_payment_status`
- `app/handlers/balance/riopay.py` — Python-модуль
  Классы: нет
  Функции: `process_riopay_payment_amount` — Process payment amount directly., `start_riopay_topup` — Start RioPay top-up process - ask for amount., `process_riopay_custom_amount` — Process custom amount input for RioPay payment.
- `app/handlers/balance/rollypay.py` — Python-модуль
  Классы: нет
  Функции: `process_rollypay_payment_amount` — Process payment amount directly., `start_rollypay_topup` — Start RollyPay top-up process - ask for amount.
- `app/handlers/balance/severpay.py` — Python-модуль
  Классы: нет
  Функции: `process_severpay_payment_amount` — Process payment amount directly., `start_severpay_topup` — Start SeverPay top-up process - ask for amount.
- `app/handlers/balance/stars.py` — Python-модуль
  Классы: нет
  Функции: `start_stars_payment`, `process_stars_payment_amount`
- `app/handlers/balance/tabpay.py` — Python-модуль
  Классы: нет
  Функции: `process_tabpay_payment_amount` — Обрабатывает сумму, введённую пользователем для TabPay., `start_tabpay_topup`, `start_tabpay_card_topup`, `start_tabpay_sbp_topup`
- `app/handlers/balance/tribute.py` — Python-модуль
  Классы: нет
  Функции: `start_tribute_payment`
- `app/handlers/balance/wata.py` — Python-модуль
  Классы: нет
  Функции: `start_wata_payment`, `process_wata_payment_amount`, `check_wata_payment_status`
- `app/handlers/balance/yookassa.py` — Python-модуль
  Классы: нет
  Функции: `start_yookassa_payment`, `start_yookassa_sbp_payment`, `process_yookassa_payment_amount`, `process_yookassa_sbp_payment_amount`, `check_yookassa_payment_status`

#### app/handlers/subscription

- `app/handlers/subscription/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/handlers/subscription/autopay.py` — Python-модуль
  Классы: нет
  Функции: `handle_autopay_menu`, `toggle_autopay`, `show_autopay_days`, `set_autopay_days`, `show_autopay_period` — Period picker UI for autopay., `set_autopay_period` — Handle period selection (autopay_period_<N> or autopay_period_default)., `handle_sbp_recurring_menu` — СБП-автопродление Platega: статус текущей подписки + Enable/Cancel., `handle_sbp_recurring_enable` — Подключить СБП-автопродление: создать рекуррентную Platega-подписку и, `handle_sbp_recurring_cancel` — Отменить активное СБП-автопродление и обновить статус-вью., `handle_saved_cards_list`, `handle_unlink_card`, `handle_confirm_unlink`, `handle_subscription_config_back`, `handle_subscription_cancel`
- `app/handlers/subscription/common.py` — Python-модуль
  Классы: нет
  Функции: `resolve_subscription_from_context` — Resolve subscription for multi-tariff bot handlers., `update_traffic_prices`, `format_traffic_display`, `validate_traffic_price`, `get_localized_value`, `render_guide_blocks` — Render block-format guide steps to HTML text., `build_redirect_link`, `get_device_name`, `load_app_config_async` — Load app config from Remnawave API (if configured), with TTL cache., `invalidate_app_config_cache` — Clear the cached app config so next call re-fetches from Remnawave., `get_apps_for_platform_async` — Get apps for a device type from Remnawave config., `normalize_app` — Normalize Remnawave app dict to a unified format with blocks., `get_platforms_list` — Extract available platforms from config for keyboard generation., `resolve_button_url` — Resolve template variables in button URLs (port of cabinet's _resolve_button_url)., `create_deep_link`, `get_reset_devices_confirm_keyboard`, `get_traffic_switch_keyboard`, `get_confirm_switch_traffic_keyboard`
- `app/handlers/subscription/countries.py` — Python-модуль
  Классы: нет
  Функции: `handle_add_countries`, `get_countries_price_by_uuids_fallback`, `handle_manage_country`, `apply_countries_changes`, `select_country`, `countries_continue`, `handle_add_country_to_subscription`, `confirm_add_countries_to_subscription`
- `app/handlers/subscription/devices.py` — Python-модуль
  Классы: нет
  Функции: `get_current_devices_detailed`, `get_servers_display_names`, `get_current_devices_count`, `handle_change_devices`, `confirm_change_devices`, `execute_change_devices`, `handle_device_management`, `show_devices_page`, `handle_devices_page`, `start_device_rename` — Callback `device_rename_<idx>_<page>` — prompts user for the new alias., `process_device_rename` — Text input from the user with the new alias (or `-`/`/clear` to delete)., `cancel_device_rename` — Callback `device_rename_cancel` — «Отмена» в промпте переименования., `handle_single_device_reset`, `handle_all_devices_reset_from_management`, `confirm_add_devices`, `handle_reset_devices`, `confirm_reset_devices`, `handle_device_guide`, `handle_app_selection`, `handle_specific_app_guide`, `show_device_connection_help`
- `app/handlers/subscription/gift.py` — Python-модуль
  Классы: нет
  Функции: `handle_gift_catalog` — Entry point for native gift catalog and history hub., `handle_gift_tariff_select` — Handle tariff selection in gift flow and render periods., `handle_gift_period_select` — Handle period selection in gift flow and render confirmation summary., `handle_gift_back_tariffs` — Navigate back to tariff catalog., `handle_gift_back_periods` — Navigate back to period selection for current tariff., `handle_gift_cancel` — Cancel gift checkout, clean up saved gift cart, and return to origin subscription view., `handle_gift_confirm` — Confirmation handler: validates selection, preflights channels, purchases from balance, and renders result., `handle_return_to_gift_cart` — Resume gift cart after balance top-up (Task 6)., `handle_gift_enter_code` — Prompt user to manually enter gift code or link., `handle_gift_activation_cancel` — Cancel manual code entry and return to gift catalog view., `handle_gift_code_input` — Handle manual gift code or link input in GiftActivationStates.waiting_for_code., `handle_gift_my` — Entry handler for 'My gifts' history list (Page 1)., `handle_gift_my_page` — Handle pagination page change in gift history., `handle_gift_my_open` — Open detail card for a specific gift owned by the sender (IDOR protected)., `handle_gift_my_qr` — Показать QR со ссылкой на активацию подарка., `handle_gift_my_text` — Готовое сообщение получателю, скопировать одним нажатием., `handle_gift_my_back` — Return from gift detail card to history list., `register_gift_handlers` — Register all gift purchase, navigation, and code activation handlers.
- `app/handlers/subscription/happ.py` — Python-модуль
  Классы: нет
  Функции: `handle_happ_download_request`, `handle_happ_download_platform_choice`, `handle_happ_download_close`, `handle_happ_download_back`
- `app/handlers/subscription/links.py` — Python-модуль
  Классы: нет
  Функции: `handle_connect_subscription`, `handle_open_subscription_link`
- `app/handlers/subscription/my_subscriptions.py` — Python-модуль
  Классы: нет
  Функции: `show_my_subscriptions` — Show list of all user subscriptions., `show_subscription_detail` — Show detail view for a single subscription (IDOR protected)., `handle_subscription_link` — Delegation: sl:{sub_id} → connect subscription link handler., `handle_subscription_extend` — Delegation: se:{sub_id} → extend/renew subscription handler., `handle_subscription_traffic` — Delegation: st:{sub_id} → traffic management handler., `handle_subscription_devices` — Delegation: sd:{sub_id} → devices menu with buy + manage options., `handle_change_devices_menu` — Delegation: change_devices_menu:{sub_id} → buy/change device limit., `handle_device_management_menu` — Delegation: device_management:{sub_id} → manage connected devices., `handle_subscription_delete_confirm` — Show delete confirmation for an expired/disabled subscription., `handle_subscription_delete_execute` — Actually delete an expired/disabled subscription.
- `app/handlers/subscription/notifications.py` — Python-модуль
  Классы: нет
  Функции: `send_trial_notification`, `send_purchase_notification`, `send_extension_notification`
- `app/handlers/subscription/pricing.py` — Python-модуль
  Классы: нет
  Функции: `get_subscription_cost`, `get_subscription_info_text`
- `app/handlers/subscription/promo.py` — Python-модуль
  Классы: нет
  Функции: `claim_discount_offer`, `handle_promo_offer_close`
- `app/handlers/subscription/purchase.py` — Python-модуль
  Классы: нет
  Функции: `show_subscription_info`, `show_trial_offer`, `activate_trial`, `start_subscription_purchase`, `save_cart_and_redirect_to_topup`, `return_to_saved_cart`, `handle_extend_subscription`, `confirm_extend_subscription`, `select_period`, `select_devices`, `devices_continue`, `confirm_purchase`, `resume_subscription_checkout`, `create_paid_subscription_with_traffic_mode`, `handle_subscription_settings`, `clear_saved_cart`, `handle_toggle_daily_subscription_pause` — Переключает паузу суточной подписки., `handle_trial_pay_with_balance` — Обрабатывает оплату триала с баланса., `handle_trial_payment_method` — Обрабатывает выбор метода оплаты для платного триала., `register_handlers`, `handle_simple_subscription_purchase` — Обрабатывает простую покупку подписки.
- `app/handlers/subscription/revoke.py` — Python-модуль
  Классы: нет
  Функции: `start_subscription_revoke` — Show revoke confirmation for classic single-subscription mode., `confirm_subscription_revoke` — Execute revoke for classic or multi-tariff mode (uses FSM state for multi)., `start_multi_revoke` — Show revoke confirmation for multi-tariff mode (callback_data = 'sr:{sub_id}').
- `app/handlers/subscription/summary.py` — Python-модуль
  Классы: нет
  Функции: `present_subscription_summary` — Render the subscription purchase summary and switch to the confirmation state.
- `app/handlers/subscription/tariff_purchase.py` — Python-модуль
  Классы: нет
  Функции: `format_tariffs_list_text` — Форматирует текст со списком тарифов для отображения., `get_tariffs_keyboard` — Создает компактную клавиатуру выбора тарифов (только названия)., `get_tariff_periods_keyboard` — Создает клавиатуру выбора периода для тарифа с учетом скидок по периодам., `get_tariff_periods_keyboard_with_traffic` — Клавиатура выбора периода для тарифа с кастомным трафиком (переход к настройке трафика)., `get_tariff_confirm_keyboard` — Создает клавиатуру подтверждения покупки тарифа., `get_tariff_insufficient_balance_keyboard` — Создает клавиатуру при недостаточном балансе., `get_tariff_extend_insufficient_balance_keyboard` — Клавиатура «Недостаточно средств» при продлении тарифа., `format_tariff_info_for_user` — Форматирует информацию о тарифе для пользователя., `get_daily_tariff_confirm_keyboard` — Создает клавиатуру подтверждения покупки суточного тарифа., `get_daily_tariff_insufficient_balance_keyboard` — Создает клавиатуру при недостаточном балансе для суточного тарифа., `get_custom_tariff_keyboard` — Создает клавиатуру для настройки кастомных дней и трафика., `format_custom_tariff_preview` — Форматирует предпросмотр покупки с кастомными параметрами., `show_tariffs_list` — Показывает список тарифов для покупки., `select_tariff` — Обрабатывает выбор тарифа., `handle_custom_days_change` — Обрабатывает изменение количества дней., `handle_custom_traffic_change` — Обрабатывает изменение количества трафика., `handle_custom_confirm` — Подтверждает покупку тарифа с кастомными параметрами., `select_tariff_period_with_traffic` — Обрабатывает выбор периода для тарифа с кастомным трафиком - показывает экран настройки трафика., `select_tariff_period` — Обрабатывает выбор периода для тарифа., `confirm_tariff_purchase` — Подтверждает покупку тарифа и создает подписку., `confirm_daily_tariff_purchase` — Подтверждает покупку суточного тарифа., `get_tariff_extend_keyboard` — Создает клавиатуру выбора периода для продления по тарифу с учетом скидок по периодам., `get_tariff_extend_confirm_keyboard` — Создает клавиатуру подтверждения продления по тарифу., `show_tariff_extend` — Показывает экран продления по текущему тарифу., `select_tariff_extend_period` — Обрабатывает выбор периода для продления., `confirm_tariff_extend` — Подтверждает продление по тарифу., `format_tariff_switch_list_text` — Форматирует текст со списком тарифов для переключения., `get_tariff_switch_keyboard` — Создает компактную клавиатуру выбора тарифа для переключения., `get_tariff_switch_periods_keyboard` — Создает клавиатуру выбора периода для переключения тарифа с учетом скидок по периодам., `get_tariff_switch_confirm_keyboard` — Создает клавиатуру подтверждения переключения тарифа., `get_tariff_switch_insufficient_balance_keyboard` — Создает клавиатуру при недостаточном балансе для переключения., `show_tariff_switch_list` — Показывает список тарифов для переключения., `select_tariff_switch` — Обрабатывает выбор тарифа для переключения., `select_tariff_switch_period` — Обрабатывает выбор периода для переключения тарифа., `confirm_tariff_switch` — Подтверждает переключение тарифа., `confirm_daily_tariff_switch` — Подтверждает смену на суточный тариф., `format_instant_switch_list_text` — Форматирует текст со списком тарифов для мгновенного переключения., `get_instant_switch_keyboard` — Создает клавиатуру для мгновенного переключения тарифа., `get_instant_switch_confirm_keyboard` — Создает клавиатуру подтверждения мгновенного переключения., `get_instant_switch_insufficient_balance_keyboard` — Создает клавиатуру при недостаточном балансе для мгновенного переключения., `show_instant_switch_list` — Показывает список тарифов для мгновенного переключения., `preview_instant_switch` — Показывает превью мгновенного переключения тарифа., `purchase_tariff_with_lava` — Оформление подписки на тариф через автопродление Lava., `confirm_instant_switch` — Подтверждает мгновенное переключение тарифа., `return_to_saved_tariff_cart` — Восстанавливает сохраненную корзину тарифа после пополнения баланса., `purchase_tariff_with_sbp` — Оформление подписки на тариф через СБП-автопродление Platega., `register_tariff_purchase_handlers` — Регистрирует обработчики покупки по тарифам.
- `app/handlers/subscription/traffic.py` — Python-модуль
  Классы: нет
  Функции: `handle_add_traffic`, `handle_reset_traffic`, `confirm_reset_traffic`, `refresh_traffic_config`, `get_traffic_packages_info`, `select_traffic`, `add_traffic`, `handle_no_traffic_packages`, `handle_switch_traffic`, `confirm_switch_traffic`, `execute_switch_traffic`

### app/keyboards

- `app/keyboards/admin.py` — Python-модуль
  Классы: нет
  Функции: `get_admin_main_keyboard`, `get_admin_users_submenu_keyboard`, `get_admin_promo_submenu_keyboard`, `get_admin_communications_submenu_keyboard`, `get_admin_support_submenu_keyboard`, `get_admin_settings_submenu_keyboard`, `get_admin_system_submenu_keyboard`, `get_admin_trials_keyboard`, `get_admin_reports_keyboard`, `get_admin_report_result_keyboard`, `get_admin_users_keyboard`, `get_admin_users_filters_keyboard`, `get_admin_subscriptions_keyboard`, `get_admin_promocodes_keyboard`, `get_admin_campaigns_keyboard`, `get_admin_contests_root_keyboard`, `get_admin_contests_keyboard`, `get_contest_mode_keyboard`, `get_daily_contest_manage_keyboard`, `get_referral_contest_manage_keyboard`, `get_campaign_management_keyboard`, `get_campaign_edit_keyboard`, `get_campaign_bonus_type_keyboard`, `get_promocode_management_keyboard`, `get_admin_messages_keyboard`, `get_pinned_message_keyboard`, `get_pinned_broadcast_confirm_keyboard` — Клавиатура для выбора: разослать сейчас или только при /start., `get_admin_monitoring_keyboard`, `get_admin_remnawave_keyboard`, `get_admin_statistics_keyboard`, `get_user_management_keyboard`, `get_user_restrictions_keyboard` — Клавиатура управления ограничениями пользователя., `get_user_promo_group_keyboard`, `get_confirmation_keyboard`, `get_promocode_type_keyboard`, `get_promocode_list_keyboard`, `get_broadcast_target_keyboard`, `get_custom_criteria_keyboard`, `get_broadcast_history_keyboard`, `get_sync_options_keyboard`, `get_sync_confirmation_keyboard`, `get_sync_result_keyboard`, `get_period_selection_keyboard`, `get_node_management_keyboard`, `get_squad_management_keyboard`, `get_squad_edit_keyboard`, `get_monitoring_keyboard`, `get_monitoring_logs_keyboard`, `get_monitoring_logs_navigation_keyboard`, `get_log_detail_keyboard`, `get_monitoring_clear_confirm_keyboard`, `get_monitoring_status_keyboard`, `get_monitoring_settings_keyboard`, `get_log_type_filter_keyboard`, `get_admin_servers_keyboard`, `get_server_edit_keyboard`, `get_admin_pagination_keyboard`, `get_maintenance_keyboard`, `get_sync_simplified_keyboard`, `get_welcome_text_keyboard`, `get_broadcast_button_config`, `get_broadcast_button_labels`, `get_message_buttons_selector_keyboard`, `get_broadcast_media_keyboard`, `get_media_confirm_keyboard`, `get_updated_message_buttons_selector_keyboard_with_media`
- `app/keyboards/inline.py` — Python-модуль
  Классы: нет
  Функции: `get_main_menu_keyboard_async` — Асинхронная версия get_main_menu_keyboard с поддержкой конструктора меню., `get_rules_keyboard`, `get_privacy_policy_keyboard`, `get_channel_sub_keyboard` — Subscription keyboard for required channels., `get_post_registration_keyboard`, `get_language_selection_keyboard`, `get_main_menu_keyboard`, `get_info_menu_keyboard`, `get_happ_download_button_row`, `get_happ_cryptolink_keyboard`, `get_happ_download_platform_keyboard`, `get_happ_download_link_keyboard`, `get_back_keyboard`, `get_server_status_keyboard`, `get_insufficient_balance_keyboard`, `get_subscription_keyboard`, `get_payment_methods_keyboard_with_cart`, `get_subscription_confirm_keyboard_with_cart`, `get_insufficient_balance_keyboard_with_cart`, `get_trial_keyboard`, `get_subscription_period_keyboard` — Generate subscription period selection keyboard with personalized pricing., `get_traffic_packages_keyboard`, `get_countries_keyboard`, `get_devices_keyboard`, `get_subscription_confirm_keyboard`, `get_balance_keyboard`, `get_payment_methods_keyboard`, `get_yookassa_payment_keyboard`, `get_autopay_notification_keyboard`, `get_referral_keyboard`, `get_support_keyboard`, `get_pagination_keyboard`, `get_confirmation_keyboard`, `get_autopay_keyboard`, `get_saved_cards_keyboard`, `get_confirm_unlink_keyboard`, `get_autopay_days_keyboard`, `get_autopay_period_keyboard` — Period picker for autopay. `current_period=None` means "use default"., `get_add_traffic_keyboard`, `get_add_traffic_keyboard_from_tariff` — Клавиатура для докупки трафика из настроек тарифа., `get_change_devices_keyboard`, `get_confirm_change_devices_keyboard`, `get_reset_traffic_confirm_keyboard`, `get_manage_countries_keyboard`, `get_device_selection_keyboard`, `get_connection_guide_keyboard`, `get_app_selection_keyboard`, `get_specific_app_keyboard`, `get_extend_subscription_keyboard_with_prices`, `get_cryptobot_payment_keyboard`, `get_devices_management_keyboard`, `get_updated_subscription_settings_keyboard`, `get_device_reset_confirm_keyboard`, `get_device_management_help_keyboard`, `get_ticket_cancel_keyboard`, `get_my_tickets_keyboard`, `get_ticket_view_keyboard`, `get_ticket_reply_cancel_keyboard`, `get_admin_tickets_keyboard`, `get_admin_ticket_view_keyboard`, `get_ticket_notification_keyboard` — Клавиатура для уведомления о тикете (личный или групповой админ-чат)., `get_admin_ticket_reply_cancel_keyboard`
- `app/keyboards/reply.py` — Python-модуль
  Классы: нет
  Функции: `get_main_reply_keyboard`, `get_admin_reply_keyboard`, `get_cancel_keyboard`, `get_confirmation_reply_keyboard`, `get_skip_keyboard`, `remove_keyboard`, `get_contact_keyboard`, `get_location_keyboard`
- `app/keyboards/topup_amounts.py` — Python-модуль
  Классы: нет
  Функции: `resolve_config_method_id`, `format_quick_amount`, `get_topup_amount_keyboard`

### app/lib

- `app/lib/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/lib/nalogo/`

#### app/lib/nalogo

- `app/lib/nalogo/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/lib/nalogo/_http.py` — Python-модуль
  Классы: `AuthProvider` (2 методов), `AsyncHTTPClient` (8 методов)
  Функции: нет
- `app/lib/nalogo/auth.py` — Python-модуль
  Классы: `AuthProviderImpl` (9 методов)
  Функции: `generate_device_id` — Generate device ID similar to PHP's DeviceIdGenerator.
- `app/lib/nalogo/client.py` — Python-модуль
  Классы: `Client` (11 методов)
  Функции: нет
- `app/lib/nalogo/dto/`
- `app/lib/nalogo/exceptions.py` — Python-модуль
  Классы: `DomainException` (5 методов), `ValidationException`, `UnauthorizedException`, `ForbiddenException`, `NotFoundException`, `ClientException`, `PhoneException`, `ServerException`, `UnknownErrorException`
  Функции: `raise_for_status` — Raise appropriate domain exception based on HTTP status code.
- `app/lib/nalogo/income.py` — Python-модуль
  Классы: `IncomeAPI` (5 методов)
  Функции: нет
- `app/lib/nalogo/payment_type.py` — Python-модуль
  Классы: `PaymentTypeAPI` (3 методов)
  Функции: нет
- `app/lib/nalogo/receipt.py` — Python-модуль
  Классы: `ReceiptAPI` (3 методов)
  Функции: нет
- `app/lib/nalogo/tax.py` — Python-модуль
  Классы: `TaxAPI` (4 методов)
  Функции: нет
- `app/lib/nalogo/user.py` — Python-модуль
  Классы: `UserAPI` (2 методов)
  Функции: нет

##### app/lib/nalogo/dto

- `app/lib/nalogo/dto/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/lib/nalogo/dto/device.py` — Python-модуль
  Классы: `DeviceInfo` (1 методов)
  Функции: нет
- `app/lib/nalogo/dto/income.py` — Python-модуль
  Классы: `IncomeType`, `PaymentType`, `CancelCommentType`, `AtomDateTime` (3 методов), `IncomeServiceItem` (5 методов), `IncomeClient` (3 методов), `IncomeRequest` (2 методов), `CancelRequest` (2 методов)
  Функции: нет
- `app/lib/nalogo/dto/invoice.py` — Python-модуль
  Классы: `InvoiceServiceItem` (3 методов), `InvoiceClient` (1 методов)
  Функции: нет
- `app/lib/nalogo/dto/payment_type.py` — Python-модуль
  Классы: `PaymentType` (2 методов), `PaymentTypeCollection` (3 методов)
  Функции: нет
- `app/lib/nalogo/dto/tax.py` — Python-модуль
  Классы: `Tax` (1 методов), `History` (1 методов), `HistoryRecords` (3 методов), `Payment` (1 методов), `PaymentRecords` (3 методов)
  Функции: нет
- `app/lib/nalogo/dto/user.py` — Python-модуль
  Классы: `UserType` (5 методов)
  Функции: нет

### app/localization

- `app/localization/default_locales/`
- `app/localization/loader.py` — Python-модуль
  Классы: нет
  Функции: `ensure_locale_templates`, `load_locale`, `clear_locale_cache`
- `app/localization/locales/`
- `app/localization/texts.py` — Python-модуль
  Классы: `Texts` (9 методов)
  Функции: `get_texts`, `get_rules_from_db`, `get_privacy_policy`, `get_rules_sync`, `get_rules`, `refresh_rules_cache`, `clear_rules_cache`, `reload_locales`

#### app/localization/default_locales

- `app/localization/default_locales/en.yml` — файл
- `app/localization/default_locales/ru.yml` — файл

#### app/localization/locales

- `app/localization/locales/en.json` — файл
- `app/localization/locales/fa.json` — файл
- `app/localization/locales/ru.json` — файл
- `app/localization/locales/ua.json` — файл
- `app/localization/locales/zh.json` — файл

### app/middlewares

- `app/middlewares/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/middlewares/auth.py` — Python-модуль
  Классы: `AuthMiddleware` (1 методов)
  Функции: нет
- `app/middlewares/blacklist.py` — Python-модуль
  Классы: `BlacklistMiddleware` (1 методов)
  Функции: нет
- `app/middlewares/button_stats.py` — Python-модуль
  Классы: `ButtonStatsMiddleware` (6 методов)
  Функции: нет
- `app/middlewares/channel_checker.py` — Python-модуль
  Классы: `ChannelCheckerMiddleware` (8 методов)
  Функции: `save_pending_payload_to_redis` — Save pending_start_payload to Redis via the shared cache singleton., `get_pending_payload_from_redis` — Get pending_start_payload from Redis via the shared cache singleton., `delete_pending_payload_from_redis` — Delete pending_start_payload from Redis via the shared cache singleton.
- `app/middlewares/chat_type_filter.py` — Python-модуль
  Классы: `ChatTypeFilterMiddleware` (1 методов)
  Функции: нет
- `app/middlewares/context_binding.py` — Python-модуль
  Классы: `ContextVarsMiddleware` (1 методов)
  Функции: нет
- `app/middlewares/display_name_restriction.py` — Python-модуль
  Классы: `DisplayNameRestrictionMiddleware` (5 методов)
  Функции: нет
- `app/middlewares/global_error.py` — Python-модуль
  Классы: `GlobalErrorMiddleware` (10 методов), `ErrorStatisticsMiddleware` (5 методов)
  Функции: `send_error_to_admin_chat` — Отправляет уведомление об ошибке в админский чат с троттлингом.
- `app/middlewares/logging.py` — Python-модуль
  Классы: `LoggingMiddleware` (1 методов)
  Функции: нет
- `app/middlewares/maintenance.py` — Python-модуль
  Классы: `MaintenanceMiddleware` (1 методов)
  Функции: нет
- `app/middlewares/subscription_checker.py` — Python-модуль
  Классы: `SubscriptionStatusMiddleware` (1 методов)
  Функции: нет
- `app/middlewares/throttling.py` — Python-модуль
  Классы: `ThrottlingMiddleware` (3 методов)
  Функции: нет

### app/services

- `app/services/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/services/account_merge_service.py` — Python-модуль
  Классы: нет
  Функции: `compute_auth_methods` — Вычисляет список методов авторизации пользователя., `get_merge_preview` — Возвращает превью данных обоих аккаунтов для подтверждения мержа., `flush_remnawave_deletions` — Удаляет (или деактивирует как fallback) пользователей RemnaWave., `execute_merge` — Выполняет атомарный мерж двух аккаунтов. Caller отвечает за commit/rollback.
- `app/services/admin_notification_service.py` — Python-модуль
  Классы: `NotificationCategory`, `AdminNotificationService` (48 методов)
  Функции: нет
- `app/services/antilopay_service.py` — Python-модуль
  Классы: `AntilopayAPIError` (1 методов), `AntilopayService` (12 методов)
  Функции: нет
- `app/services/apple_iap.py` — Python-модуль
  Классы: `AppleFulfillmentResult`, `AppleIAPFulfillmentService` (6 методов), `AppleIAPNotificationService` (9 методов)
  Функции: нет
- `app/services/apple_iap_reconciliation_service.py` — Python-модуль
  Классы: `AppleReconciliationResult`, `AppleIAPReconciliationService` (3 методов)
  Функции: нет
- `app/services/aurapay_service.py` — Python-модуль
  Классы: `AuraPayAPIError` (1 методов), `AuraPayService` (10 методов)
  Функции: нет
- `app/services/backup_service.py` — Python-модуль
  Классы: `BackupMetadata`, `BackupSettings`, `BackupService` (54 методов)
  Функции: нет
- `app/services/ban_notification_service.py` — Python-модуль
  Классы: `BanNotificationService` (9 методов)
  Функции: `get_delete_keyboard` — Клавиатура с кнопкой удаления уведомления
- `app/services/blacklist_service.py` — Python-модуль
  Классы: `BlacklistService` (12 методов)
  Функции: нет
- `app/services/blocked_users_service.py` — Python-модуль
  Классы: `BlockCheckStatus`, `BlockedUserAction`, `BlockCheckResult`, `BlockedUsersScanResult` (1 методов), `CleanupResult`, `BlockedUsersService` (8 методов)
  Функции: нет
- `app/services/broadcast_service.py` — Python-модуль
  Классы: `BroadcastMediaConfig`, `BroadcastConfig`, `EmailBroadcastConfig`, `BroadcastService` (15 методов), `EmailBroadcastService` (15 методов)
  Функции: `cleanup_blocked_broadcast_users` — Фоновая очистка пользователей, заблокировавших бота (обнаруженных при рассылке).
- `app/services/bulk_ban_service.py` — Python-модуль
  Классы: `BulkBanService` (3 методов)
  Функции: нет
- `app/services/campaign_service.py` — Python-модуль
  Классы: `CampaignBonusResult`, `AdvertisingCampaignService` (8 методов)
  Функции: нет
- `app/services/channel_subscription_service.py` — Python-модуль
  Классы: `ChannelSubscriptionService` (16 методов)
  Функции: нет
- `app/services/cispay_service.py` — Python-модуль
  Классы: `CisPayAPIError` (1 методов), `CisPayService` (13 методов)
  Функции: нет
- `app/services/cloudpayments_service.py` — Python-модуль
  Классы: `CloudPaymentsAPIError` (1 методов), `CloudPaymentsService` (16 методов)
  Функции: нет
- `app/services/contest_rotation_service.py` — Python-модуль
  Классы: `ContestRotationService` (15 методов)
  Функции: нет
- `app/services/contests/`
- `app/services/coupon_service.py` — Python-модуль
  Классы: `CouponRedemptionError` (1 методов), `CouponRedemptionResult`
  Функции: `is_coupon_token` — True if ``value`` has the exact coupon-token format (no DB hit)., `build_coupon_deeplink` — The single source of truth for the coupon activation deep link., `redeem_coupon` — Atomically redeem a one-time coupon for ``user``.
- `app/services/daily_subscription_service.py` — Python-модуль
  Классы: `DailySubscriptionService` (19 методов)
  Функции: нет
- `app/services/disposable_email_service.py` — Python-модуль
  Классы: `DisposableEmailService` (7 методов)
  Функции: нет
- `app/services/donut_service.py` — Python-модуль
  Классы: `DonutAPIError` (1 методов), `DonutService` (18 методов)
  Функции: нет
- `app/services/email_retry_service.py` — Python-модуль
  Классы: `EmailRetryService` (12 методов)
  Функции: нет
- `app/services/etoplatezhi_service.py` — Python-модуль
  Классы: `EtoplatezhiService` (7 методов)
  Функции: нет
- `app/services/event_emitter.py` — Python-модуль
  Классы: `EventEmitter` (7 методов)
  Функции: нет
- `app/services/faq_service.py` — Python-модуль
  Классы: `FaqService` (13 методов)
  Функции: нет
- `app/services/freekassa_service.py` — Python-модуль
  Классы: `FreekassaService` (16 методов)
  Функции: `get_public_ip` — Получает публичный IP сервера.
- `app/services/gift_claim_service.py` — Python-модуль
  Классы: `GiftClaimError`, `GiftClaimNotFoundError`, `GiftClaimSelfActivationError`, `GiftClaimAlreadyOwnedError`, `GiftClaimNotActivatableError`
  Функции: `claim_gift_for_user` — Claim and activate a gift subscription for an authenticated user., `claim_bound_gift_for_user` — Activate an already bound gift subscription by purchase ID (directed callback).
- `app/services/gift_history_service.py` — Python-модуль
  Классы: `GiftHistoryItem` (3 методов)
  Функции: `format_safe_recipient` — Format a privacy-safe recipient representation for display., `list_sender_gifts` — Query paginated gift history purchased by the given buyer., `get_sender_gift` — Retrieve a single gift purchase owned by the buyer by its ID., `has_sender_gifts` — Check whether the buyer has any eligible gift history (lightweight check).
- `app/services/gift_notification_service.py` — Python-модуль
  Классы: нет
  Функции: `resolve_gift_claim_channel` — Resolve and validate available claim channels (bot username and/or cabinet URL)., `build_gift_result_presentation` — Build localized text and inline keyboard for gift purchase result., `build_gift_history_detail_presentation` — Build localized HTML text and inline keyboard for sender gift detail view., `send_gift_result_message` — Send localized gift result presentation directly to user's Telegram chat.
- `app/services/gift_purchase_service.py` — Python-модуль
  Классы: `GiftQuote` (2 методов), `GiftTariffOffer`, `GiftRecipient`, `GiftPurchaseResult`, `GiftError`, `GiftFeatureDisabledError`, `GiftTariffUnavailableError`, `GiftPeriodUnavailableError`, `GiftPurchaseRestrictedError`, `GiftInsufficientBalanceError` (1 методов), `GiftPriceChangedError` (1 методов), `GiftIdempotencyConflictError`
  Функции: `is_gift_enabled` — Check if the gift feature is enabled via system settings., `list_gift_offers` — List eligible tariffs and their personalized quotes for gift purchase., `quote_gift_purchase` — Calculate personalized quote for a specific tariff and period., `purchase_gift_from_balance` — Atomically purchase a gift subscription from user balance with database idempotency.
- `app/services/grace_access_runtime.py` — Python-модуль
  Классы: `GraceSnapshotError`, `GracePanelError`, `GraceAccessDeletionBlocked` (1 методов), `GracePanelUpdateLease` (1 методов), `SQLAlchemyGraceSessionStore` (6 методов), `SQLAlchemyGraceBillingGateway` (2 методов), `RemnawaveGracePanelGateway` (4 методов), `GraceAccessRuntime` (16 методов)
  Функции: `get_open_grace_subscription_ids` — One-query guard shared by both directions of full synchronization., `lock_grace_sensitive_panel_updates` — Serialize an outbound panel PATCH with grace creation/reconciliation., `apply_recovered_grace_update_locked` — Apply one canonical panel PATCH and finish a recovered grace session., `grace_sensitive_panel_update` — Hold a grace lock and expose billing state read only after lock acquisition., `update_panel_user_grace_safe` — Apply a normal panel update without overwriting an open grace overlay., `create_panel_user_grace_safe` — Create a panel user only while the subscription cannot have an overlay., `grace_sensitive_global_panel_update` — Block all grace creation while one all-users panel mutation runs., `set_panel_user_enabled_state_grace_safe` — Serialize an intentional enable/disable and its grace suppression marker., `ensure_no_open_grace_for_subscriptions` — Fail before an irreversible panel/DB delete can orphan an overlay., `ensure_no_open_grace_for_user` — User-level version of the pre-delete guard., `ensure_no_open_grace_for_users` — Acquire every affected subscription lock in deterministic order., `collect_grace_status` — Session counters and the newest failures, as one read-only snapshot.
- `app/services/grace_access_service.py` — Python-модуль
  Классы: `GraceReason`, `GraceSubscriptionKind`, `GraceAccessMode` (1 методов), `GraceSessionState`, `GraceCompletionReason`, `GraceRestoreOutcome`, `GracePanelTransitionPending`, `GracePanelTransitionConflict`, `GraceStartDecision`, `GraceAccessPolicy` (2 методов), `GraceBillingState`, `GracePanelSnapshot`, `GracePanelOverlay`, `GraceAccessSession`, `GraceStartResult`, `GraceReconcileResult`, `GraceSessionStore` (5 методов), `GracePanelGateway` (4 методов), `GraceBillingGateway` (1 методов), `GraceAccessService` (14 методов)
  Функции: `build_incident_key` — Build a stable identifier so one incident receives grace only once., `billing_still_matches_session` — Compare canonical fields that identify the incident without panel metadata., `classify_subscription_kind` — Classify once, in priority order, so overlapping flags are unambiguous., `policy_allows_subscription`, `billing_incident_is_eligible` — Check current incident safety without applying new-issuance feature flags., `billing_is_eligible` — Apply incident safety and subscription-kind flags to a new grant., `billing_is_revoked` — Return whether grace must be removed immediately for safety., `panel_status_matches_reason`, `build_panel_overlay` — Calculate temporary panel values without resetting consumed traffic., `billing_has_recovered` — Detect a real renewal or traffic purchase in the canonical billing state., `panel_matches_overlay` — Match only fields controlled by grace; used traffic is intentionally ignored., `panel_is_safe_pending_source` — Recognize only states that this PENDING activation could have produced., `webhook_matches_overlay_event` — Require strong overlay markers before hiding a status webhook., `webhook_matches_overlay` — Strictly match a user.modified echo without hiding unrelated updates.
- `app/services/guest_purchase_service.py` — Python-модуль
  Классы: `GuestPurchaseError` (1 методов)
  Функции: `get_claimable_gift` — Resolve one still-activatable gift by full token or deep-link prefix., `validate_and_calculate` — Validate tariff/period against landing config and return (tariff, price_kopeks)., `create_purchase` — Create a guest purchase record., `fulfill_purchase` — Fulfill a paid guest purchase by creating/extending the user account and subscription., `find_guest_purchase_user` — Find a guest-purchase user without mutating the account or session state., `evaluate_guest_purchase_registration` — Evaluate invite-only policy before a landing flow may mutate ``User``., `send_guest_notification` — Send notification for guest purchase delivery or activation requirement., `notify_gift_claim_available` — Best-effort: tell people a paid gift is waiting, with the CLAIM link., `activate_purchase` — Activate a PENDING_ACTIVATION purchase by replacing or creating a subscription., `retry_stuck_paid_purchases` — Retry fulfillment for purchases stuck in PAID status., `retry_stuck_pending_activation` — Retry activation for purchases stuck in PENDING_ACTIVATION status., `recover_stuck_pending_purchases` — Recover purchases stuck in PENDING by checking provider payment status.
- `app/services/jupiter_service.py` — Python-модуль
  Классы: `JupiterAPIError` (1 методов), `JupiterService` (19 методов)
  Функции: нет
- `app/services/kassa_ai_service.py` — Python-модуль
  Классы: `KassaAiService` (10 методов)
  Функции: `get_public_ip` — Получает публичный IP сервера.
- `app/services/lava_recurrent.py` — Python-модуль
  Классы: нет
  Функции: `build_recurrent_order_id` — orderId подписки: префикс + id локальной подписки + случайный хвост., `is_recurrent_order_id`, `resolve_product_charge_days` — Шаг продления по данным продукта Lava., `normalize_remote_status` — Приводит статус подписки из ``subscription/status`` к нормализованному виду., `lava_reconcile_decision` — Новый локальный статус по данным Lava, либо None — не трогать.
- `app/services/lava_service.py` — Python-модуль
  Классы: `LavaAPIError` (1 методов), `LavaService` (20 методов)
  Функции: нет
- `app/services/legal_consent_service.py` — Python-модуль
  Классы: `LegalConsentRequirement`
  Функции: `get_requirement` — Требование согласия для НОВОГО пользователя кабинета., `missing_documents` — Какие из обязательных документов пользователь не отметил., `record_consent` — Записать факт согласия. Сбой записи не должен ронять регистрацию.
- `app/services/log_rotation_service.py` — Python-модуль
  Классы: `LogRotationStatus`, `LogRotationService` (16 методов)
  Функции: нет
- `app/services/main_menu_button_service.py` — Python-модуль
  Классы: `MainMenuButtonService` (4 методов)
  Функции: нет
- `app/services/maintenance_service.py` — Python-модуль
  Классы: `MaintenanceStatus`, `MaintenanceService` (19 методов)
  Функции: нет
- `app/services/manual_topup_service.py` — Python-модуль
  Классы: `ManualTopupKeyConflict` (1 методов), `ManualTopupResult`
  Функции: `build_manual_topup_external_id`, `credit_manual_topup` — Зачислить деньги на баланс так же, как это делает платёжный шлюз.
- `app/services/menu_layout/`
- `app/services/menu_layout_service.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/services/monitoring_service.py` — Python-модуль
  Классы: `AutopayFailState` (2 методов), `MonitoringService` (45 методов)
  Функции: `resolve_autopay_period_candidate` — Return ``candidate`` only if it is a valid renewal period for ``tariff``., `decide_autopay_fail_notification` — Decide whether/what to send on a failed-autopay tick., `apply_autopay_fail_notification` — Mutate state to record that a notification with `reason` was just sent.
- `app/services/mulenpay_service.py` — Python-модуль
  Классы: `MulenPayService` (10 методов)
  Функции: нет
- `app/services/nalogo_queue_service.py` — Python-модуль
  Классы: `NalogoQueueService` (15 методов)
  Функции: нет
- `app/services/nalogo_service.py` — Python-модуль
  Классы: `NaloGoService` (18 методов)
  Функции: `send_nalogo_receipt_notifications` — Отправляет ссылку на созданный чек NaloGO пользователю и дублирует её в
- `app/services/news_media_service.py` — Python-модуль
  Классы: `SavedMedia`
  Функции: `ensure_upload_dirs` — Create images/, videos/, thumbnails/ subdirectories under upload_path., `detect_file_type` — Detect media type and extension from magic bytes., `save_image` — Validate, resize, and save an image file. Runs PIL operations in a thread., `save_video` — Validate and save a video file. Runs I/O in a thread., `delete_media_file` — Delete a media file by filename with path traversal protection.
- `app/services/notification_delivery_service.py` — Python-модуль
  Классы: `NotificationType`, `NotificationDeliveryService` (23 методов)
  Функции: нет
- `app/services/notification_settings_service.py` — Python-модуль
  Классы: `NotificationSettingsService` (28 методов)
  Функции: нет
- `app/services/overpay_certificate_service.py` — Python-модуль
  Классы: нет
  Функции: `get_canonical_path`, `validate_p12`, `store_certificate`, `delete_certificate`, `get_status`
- `app/services/overpay_service.py` — Python-модуль
  Классы: `OverpayAPIError` (1 методов), `OverpayService` (13 методов)
  Функции: нет
- `app/services/pal24_service.py` — Python-модуль
  Классы: `Pal24Service` (9 методов)
  Функции: нет
- `app/services/paritypay_service.py` — Python-модуль
  Классы: `ParityPayAPIError` (1 методов), `ParityPayNetworkError`, `ParityPayService` (18 методов)
  Функции: `amount_to_kopeks` — Сумма провайдера (рубли) -> целые копейки., `kopeks_to_amount` — Копейки -> рубли для тела запроса.
- `app/services/partner_application_service.py` — Python-модуль
  Классы: `PartnerApplicationService` (7 методов)
  Функции: нет
- `app/services/partner_stats_service.py` — Python-модуль
  Классы: `PartnerStatsService` (12 методов)
  Функции: нет
- `app/services/payment/`
- `app/services/payment_method_config_service.py` — Python-модуль
  Классы: нет
  Функции: `refresh_display_name_overrides` — Reload the method_id -> display_name override cache from the DB., `get_display_name_override` — Sync read of a cabinet-set display name for a method, or None if not set., `normalize_quick_amounts`, `get_effective_quick_amounts`, `ensure_payment_method_configs` — Initialize payment method configs if they don't exist yet., `get_all_configs` — Get all payment method configs ordered by sort_order., `get_config_by_method_id` — Get a single config by method_id., `update_config` — Update a payment method config., `update_sort_order` — Batch update sort order for all methods., `get_all_promo_groups` — Get all promo groups for the filter selector., `get_enabled_methods_for_user` — Get payment methods available for a specific user.
- `app/services/payment_search_service.py` — Python-модуль
  Классы: `StatusFilter`, `PeriodPreset`, `SearchParams` (2 методов), `SearchStats`
  Функции: `search_payments` — Search payments across all (or filtered) providers., `search_payments_stats` — Compute aggregated statistics for the given search filters.
- `app/services/payment_service.py` — Python-модуль
  Классы: `PaymentService` (2 методов)
  Функции: `create_yookassa_payment`, `update_yookassa_payment_status`, `link_yookassa_payment_to_transaction`, `get_yookassa_payment_by_id`, `get_yookassa_payment_by_local_id`, `create_transaction`, `get_transaction_by_external_id`, `add_user_balance`, `get_user_by_id`, `get_user_by_telegram_id`, `create_mulenpay_payment`, `get_mulenpay_payment_by_uuid`, `get_mulenpay_payment_by_mulen_id`, `get_mulenpay_payment_by_local_id`, `update_mulenpay_payment_status`, `update_mulenpay_payment_metadata`, `link_mulenpay_payment_to_transaction`, `create_pal24_payment`, `get_pal24_payment_by_bill_id`, `get_pal24_payment_by_order_id`, `get_pal24_payment_by_id`, `update_pal24_payment_status`, `link_pal24_payment_to_transaction`, `create_wata_payment`, `get_wata_payment_by_link_id`, `get_wata_payment_by_id`, `get_wata_payment_by_local_id`, `get_wata_payment_by_order_id`, `update_wata_payment_status`, `link_wata_payment_to_transaction`, `create_platega_payment`, `get_platega_payment_by_id`, `get_platega_payment_by_id_for_update`, `get_platega_payment_by_transaction_id`, `get_platega_payment_by_correlation_id`, `update_platega_payment`, `link_platega_payment_to_transaction`, `create_cryptobot_payment`, `get_cryptobot_payment_by_invoice_id`, `update_cryptobot_payment_status`, `link_cryptobot_payment_to_transaction`, `create_heleket_payment`, `get_heleket_payment_by_uuid`, `get_heleket_payment_by_id`, `update_heleket_payment`, `link_heleket_payment_to_transaction`, `create_cloudpayments_payment`, `get_cloudpayments_payment_by_invoice_id`, `get_cloudpayments_payment_by_id`, `update_cloudpayments_payment`, `create_severpay_payment`, `get_severpay_payment_by_order_id`, `get_severpay_payment_by_severpay_id`, `get_severpay_payment_by_id`, `get_severpay_payment_by_id_for_update`, `update_severpay_payment_status`, `link_severpay_payment_to_transaction`, `create_paypear_payment`, `get_paypear_payment_by_order_id`, `get_paypear_payment_by_paypear_id`, `get_paypear_payment_by_id`, `get_paypear_payment_by_id_for_update`, `update_paypear_payment_status`, `link_paypear_payment_to_transaction`, `create_rollypay_payment`, `get_rollypay_payment_by_order_id`, `get_rollypay_payment_by_rollypay_id`, `get_rollypay_payment_by_id`, `get_rollypay_payment_by_id_for_update`, `update_rollypay_payment_status`, `link_rollypay_payment_to_transaction`, `create_overpay_payment`, `get_overpay_payment_by_order_id`, `get_overpay_payment_by_overpay_id`, `get_overpay_payment_by_id`, `get_overpay_payment_by_id_for_update`, `update_overpay_payment_status`, `link_overpay_payment_to_transaction`, `create_aurapay_payment`, `get_aurapay_payment_by_order_id`, `get_aurapay_payment_by_invoice_id`, `get_aurapay_payment_by_id`, `get_aurapay_payment_by_id_for_update`, `update_aurapay_payment_status`, `link_aurapay_payment_to_transaction`, `create_etoplatezhi_payment`, `get_etoplatezhi_payment_by_order_id`, `get_etoplatezhi_payment_by_invoice_id`, `get_etoplatezhi_payment_by_id`, `get_etoplatezhi_payment_by_id_for_update`, `update_etoplatezhi_payment_status`, `link_etoplatezhi_payment_to_transaction`, `create_antilopay_payment`, `get_antilopay_payment_by_order_id`, `get_antilopay_payment_by_invoice_id`, `get_antilopay_payment_by_id`, `get_antilopay_payment_by_id_for_update`, `update_antilopay_payment_status`, `link_antilopay_payment_to_transaction`, `create_jupiter_payment`, `get_jupiter_payment_by_order_id`, `get_jupiter_payment_by_invoice_id`, `get_jupiter_payment_by_id`, `get_jupiter_payment_by_id_for_update`, `update_jupiter_payment_status`, `link_jupiter_payment_to_transaction`, `create_donut_payment`, `get_donut_payment_by_order_id`, `get_donut_payment_by_invoice_id`, `get_donut_payment_by_id`, `get_donut_payment_by_id_for_update`, `update_donut_payment_status`, `link_donut_payment_to_transaction`, `create_lava_payment`, `get_lava_payment_by_order_id`, `get_lava_payment_by_invoice_id`, `get_lava_payment_by_id`, `get_lava_payment_by_id_for_update`, `update_lava_payment_status`, `link_lava_payment_to_transaction`, `create_cispay_payment`, `get_cispay_payment_by_order_id`, `get_cispay_payment_by_invoice_id`, `get_cispay_payment_by_id`, `get_cispay_payment_by_id_for_update`, `update_cispay_payment_status`, `link_cispay_payment_to_transaction`, `create_tabpay_payment`, `get_tabpay_payment_by_order_id`, `get_tabpay_payment_by_invoice_id`, `get_tabpay_payment_by_id`, `get_tabpay_payment_by_id_for_update`, `update_tabpay_payment_status`, `link_tabpay_payment_to_transaction`, `create_paritypay_payment`, `get_paritypay_payment_by_order_id`, `get_paritypay_payment_by_invoice_id`, `get_paritypay_payment_by_id`, `get_paritypay_payment_by_id_for_update`, `update_paritypay_payment_status`, `link_paritypay_payment_to_transaction`
- `app/services/payment_verification_service.py` — Python-модуль
  Классы: `PendingPayment` (1 методов), `AutoPaymentVerificationService` (7 методов)
  Функции: `method_display_name`, `get_enabled_auto_methods`, `list_recent_pending_payments` — Return pending payments (top-ups) from supported providers within the age window., `get_payment_record` — Load single payment record and normalize it to :class:`PendingPayment`., `run_manual_check` — Trigger provider specific status refresh and return the updated record.
- `app/services/paypear_service.py` — Python-модуль
  Классы: `PayPearAPIError` (1 методов), `PayPearService` (10 методов)
  Функции: нет
- `app/services/permission_service.py` — Python-модуль
  Классы: `PermissionService` (3 методов)
  Функции: `get_all_permissions` — Return flat list of all permissions: ``['users:read', 'users:edit', ...]``., `permission_matches` — Check if *user_perm* grants access for *required_perm*.
- `app/services/phantom_service.py` — Python-модуль
  Классы: нет
  Функции: `claim_phantom` — Claim a phantom user by backfilling Telegram profile data., `merge_phantom_into_user` — Merge phantom user into active user using the full account merge service., `sync_remnawave_after_phantom_merge` — Sync Remnawave panel after a phantom merge that transferred a subscription.
- `app/services/pinned_message_service.py` — Python-модуль
  Классы: нет
  Функции: `get_active_pinned_message`, `set_active_pinned_message`, `deactivate_active_pinned_message`, `deliver_pinned_message_to_user`, `broadcast_pinned_message` — Рассылает закреплённое сообщение всем активным пользователям., `unpin_active_pinned_message` — Открепляет активное сообщение у всех пользователей.
- `app/services/platega_recurrent.py` — Python-модуль
  Классы: `CallbackFields`
  Функции: `resolve_platega_interval` — Возвращает (interval, charge_days) для подписки Platega., `platega_reconcile_decision` — New local status given the Platega-reported status, or None for no change., `is_subscription_callback` — Относится ли коллбек к рекуррентной СБП-подписке., `read_callback_fields` — Поля подписочного коллбека, независимо от регистра ключей.
- `app/services/platega_service.py` — Python-модуль
  Классы: `PlategaApiError` (1 методов), `PlategaService` (17 методов)
  Функции: нет
- `app/services/poll_service.py` — Python-модуль
  Классы: нет
  Функции: `build_start_keyboard`, `send_poll_to_users`, `reward_user_for_poll`, `get_next_question`, `get_question_option`
- `app/services/pricing_engine.py` — Python-модуль
  Классы: `TariffBreakdown`, `ClassicBreakdown`, `RenewalPricing` (1 методов), `TariffSwitchResult` (2 методов), `PricingEngine` (19 методов)
  Функции: нет
- `app/services/privacy_policy_service.py` — Python-модуль
  Классы: `PrivacyPolicyService` (9 методов)
  Функции: нет
- `app/services/promo_group_assignment.py` — Python-модуль
  Классы: нет
  Функции: `maybe_assign_promo_group_by_total_spent`
- `app/services/promo_offer_email.py` — Python-модуль
  Классы: нет
  Функции: `send_promo_offer_email` — Шлёт одно промопредложение на почту. True — письмо реально отправлено.
- `app/services/promo_offer_service.py` — Python-модуль
  Классы: `PromoOfferService` (3 методов)
  Функции: нет
- `app/services/promocode_service.py` — Python-модуль
  Классы: `PromoCodeService` (7 методов)
  Функции: нет
- `app/services/public_offer_service.py` — Python-модуль
  Классы: `PublicOfferService` (10 методов)
  Функции: нет
- `app/services/rbac_bootstrap_service.py` — Python-модуль
  Классы: `AdminEnvCheck` (2 методов)
  Функции: `normalize_admin_email` — Канонизация email для сравнения с ADMIN_EMAILS: NFKC + lower + strip., `is_user_admin_by_env` — Проверяет, является ли юзер админом по ENV-конфигу (ADMIN_IDS/ADMIN_EMAILS)., `is_protected_from_blocking` — An account named in ADMIN_IDS/ADMIN_EMAILS must never end up BLOCKED., `bootstrap_superadmins` — Ensure every user from ADMIN_IDS / ADMIN_EMAILS has the Superadmin role., `ensure_superadmin_role_on_login` — Idempotent Superadmin assign for ADMIN_IDS / ADMIN_EMAILS users at login time.
- `app/services/reachability/`
- `app/services/recurrent_amount.py` — Python-модуль
  Классы: нет
  Функции: `resolve_true_renewal_amount` — Цена продления подписки за ``charge_days`` для сверки с суммой привязки., `sync_recurrent_bindings_after_price_change` — Гасит привязки, чья сумма больше не соответствует цене продления.
- `app/services/recurrent_payment_service.py` — Python-модуль
  Классы: нет
  Функции: `process_recurrent_payments` — Основная функция: находит подписки, которым скоро нужно продление,
- `app/services/recurrent_payments_service.py` — Python-модуль
  Классы: `RecurrentPaymentsService` (8 методов)
  Функции: нет
- `app/services/referral_contest_service.py` — Python-модуль
  Классы: `ReferralContestService` (23 методов)
  Функции: нет
- `app/services/referral_diagnostics_service.py` — Python-модуль
  Классы: `ReferralClick`, `LostReferral` (2 методов), `DiagnosticReport` (2 методов), `FixDetail`, `FixReport`, `MissingBonus` (2 методов), `MissingBonusReport` (2 методов), `ReferralDiagnosticsService` (12 методов)
  Функции: нет
- `app/services/referral_reward_service.py` — Python-модуль
  Классы: `RewardEvent`, `LevelConfig` (3 методов), `RewardComponent` (1 методов), `ReferralRewardLevelService` (5 методов), `DaysGrant`, `GrantOutcome` (1 методов), `LevelView`, `TierProgress`
  Функции: `resolve_referrer_chain` — Цепочка пригласивших снизу вверх: [(1, прямой), (2, его пригласивший), ...]., `count_level_payments` — Сколько раз этот уровень уже платил за эту пару., `count_referrals` — Сколько рефералов у пользователя., `is_level_unlocked` — Открыт ли уровень для этого партнёра., `select_tier_config` — Ранг партнёра: единственный уровень, который к нему применяется., `resolve_reward_preference` — Что получатель предпочитает, когда правило платит и деньгами, и днями., `build_reward_components` — Что и кому причитается. Ничего не начисляет., `grant_reward_days` — Выдать дни подписки. Возвращает исход, а не бросает., `is_referee_directed` — Строка описывает награду приглашённому: ``referral_id`` в ней — пригласивший., `award_referral_rewards` — Посчитать и выдать награды всей цепочке. Возвращает фактически выданное., `build_level_views` — Ступени программы, уровень смотрящего и его личная ставка., `describe_reward_choice_sides` — Что человек получит, выбрав деньги, и что — выбрав дни., `describe_active_levels` — Человекочитаемое описание активных ступеней., `describe_referee_bonus` — Что получит сам приглашённый. ``None`` — ничего не настроено., `resolve_tier_progress` — Ранг партнёра и ближайшая недостигнутая ступень. ``None`` вне режима рангов., `format_tier_progress` — Прогресс по рангам двумя строками: где сейчас и сколько до следующего., `format_reward_total` — Выплаченное одной строкой: деньги, дни или и то и другое., `legacy_percent_for_import` — Процент для переносимого уровня и то, о чём нужно предупредить.
- `app/services/referral_service.py` — Python-модуль
  Классы: нет
  Функции: `save_pending_referral` — Save pending referral to Redis for a not-yet-registered user., `get_pending_referral` — Get pending referral from Redis., `clear_pending_referral` — Clear pending referral after successful registration., `get_paid_referrals_count`, `get_referral_reward_payment_count`, `calculate_referral_commission_percent`, `attach_referrer_if_missing` — Eagerly attach a referrer to ``user`` when one isn't already set., `save_pending_campaign` — Сохранить атрибуцию кампании в Redis для ещё не зарегистрированного пользователя., `get_pending_campaign` — Get pending campaign from Redis., `clear_pending_campaign` — Clear pending campaign after successful application., `send_referral_notification` — Отправляет реферальное уведомление в Telegram или по email., `process_referral_registration`, `process_referral_topup`, `process_referral_purchase` — Process referral commission for balance-based subscription purchases.
- `app/services/referral_withdrawal_service.py` — Python-модуль
  Классы: `ReferralWithdrawalService` (20 методов)
  Функции: нет
- `app/services/registration_access_service.py` — Python-модуль
  Классы: `RegistrationChannel`, `RegistrationAccessReason`, `RegistrationInviteKind`, `VerifiedRegistrationIdentity`, `RegistrationInviteEvidence`, `RegistrationInviteValidator` (1 методов), `RegistrationAccessContext`, `RegistrationAccessDecision`, `RegistrationAccessService` (3 методов)
  Функции: нет
- `app/services/registration_invite_service.py` — Python-модуль
  Классы: `RegistrationInviteConflict`, `RegistrationInviteService` (2 методов)
  Функции: нет
- `app/services/remnawave_identity_backfill.py` — Python-модуль
  Классы: `UnresolvedRow`, `AppliedRow`, `BackfillReport` (3 методов)
  Функции: `backfill_remnawave_ids` — Populate ``remnawave_id`` on subscriptions, users and grace sessions.
- `app/services/remnawave_resync_service.py` — Python-модуль
  Классы: нет
  Функции: `resync_user_subscriptions_with_panel` — Resync all active subscriptions for a user with the RemnaWave panel.
- `app/services/remnawave_retry_queue.py` — Python-модуль
  Классы: `RetryItem`, `RemnaWaveRetryQueue` (8 методов)
  Функции: нет
- `app/services/remnawave_service.py` — Python-модуль
  Классы: `RemnaWaveConfigurationError`, `RemnaWaveService` (63 методов)
  Функции: нет
- `app/services/remnawave_sync_service.py` — Python-модуль
  Классы: `RemnaWaveAutoSyncStatus`, `RemnaWaveAutoSyncService` (13 методов)
  Функции: нет
- `app/services/remnawave_webhook_service.py` — Python-модуль
  Классы: `RemnaWaveWebhookService` (48 методов)
  Функции: нет
- `app/services/reporting_service.py` — Python-модуль
  Классы: `ReportingServiceError`, `ReportPeriod`, `ReportPeriodRange`, `ReportingService` (22 методов)
  Функции: нет
- `app/services/riopay_service.py` — Python-модуль
  Классы: `RioPayAPIError` (1 методов), `RioPayService` (9 методов)
  Функции: нет
- `app/services/rollypay_service.py` — Python-модуль
  Классы: `RollyPayAPIError` (1 методов), `RollyPayService` (9 методов)
  Функции: нет
- `app/services/s2s_postback_service.py` — Python-модуль
  Классы: нет
  Функции: `send_postback` — Send S2S postback for an event.
- `app/services/server_status_service.py` — Python-модуль
  Классы: `ServerStatusEntry`, `ServerStatusError`, `ServerStatusService` (7 методов)
  Функции: нет
- `app/services/severpay_service.py` — Python-модуль
  Классы: `SeverPayAPIError` (1 методов), `SeverPayService` (9 методов)
  Функции: нет
- `app/services/start_media_service.py` — Python-модуль
  Классы: нет
  Функции: `get_start_video_file_id` — file_id видео для стартового меню либо None., `set_start_video_file_id` — Сохраняет (или очищает) file_id видео стартового меню., `reset_start_video_cache` — Сбрасывает кеш (для тестов и ручной инвалидации).
- `app/services/startup_notification_service.py` — Python-модуль
  Классы: `StartupNotificationService` (11 методов)
  Функции: `send_bot_startup_notification` — Удобная функция для отправки стартового уведомления., `send_crash_notification` — Отправляет уведомление о падении бота с лог-файлом.
- `app/services/subscription_auto_purchase_service.py` — Python-модуль
  Классы: `AutoPurchaseContext`, `AutoExtendContext`
  Функции: `try_auto_extend_expired_after_topup` — Try to auto-extend an expired subscription after balance top-up., `try_resume_disabled_daily_after_topup` — Resume a DISABLED daily subscription immediately after balance top-up., `auto_purchase_saved_cart_after_topup` — Attempts to automatically purchase subscriptions from saved carts.
- `app/services/subscription_checkout_service.py` — Python-модуль
  Классы: нет
  Функции: `save_subscription_checkout_draft` — Persist subscription checkout draft data in cache., `get_subscription_checkout_draft` — Retrieve subscription checkout draft from cache., `clear_subscription_checkout_draft` — Remove stored subscription checkout draft for the user., `has_subscription_checkout_draft`, `should_offer_checkout_resume` — Determine whether checkout resume button should be available for the user.
- `app/services/subscription_dedup_service.py` — Python-модуль
  Классы: нет
  Функции: `dedupe_expired_tariff_subscriptions` — Background-safe entrypoint: never raises, returns the count removed.
- `app/services/subscription_deletion_service.py` — Python-модуль
  Классы: нет
  Функции: `delete_subscription_record` — Удалить подписку целиком. Грейс-гард пробрасывается наружу.
- `app/services/subscription_purchase_service.py` — Python-модуль
  Классы: `PurchaseTrafficOption` (1 методов), `PurchaseTrafficConfig` (1 методов), `PurchaseServerOption` (1 методов), `PurchaseServersConfig` (1 методов), `PurchaseDevicesConfig` (1 методов), `PurchasePeriodConfig` (1 методов), `PurchaseSelection`, `PurchasePricingResult`, `PurchaseOptionsContext`, `PurchaseValidationError` (1 методов), `PurchaseBalanceError` (1 методов), `MiniAppSubscriptionPurchaseService` (8 методов), `SubscriptionPurchaseService` (1 методов)
  Функции: нет
- `app/services/subscription_renewal_service.py` — Python-модуль
  Классы: `SubscriptionRenewalError`, `SubscriptionRenewalChargeError`, `SubscriptionRenewalPricing` (2 методов), `SubscriptionRenewalResult`, `RenewalPaymentDescriptor` (1 методов), `SubscriptionRenewalService` (1 методов)
  Функции: `build_renewal_period_id`, `build_payment_descriptor`, `encode_payment_payload`, `decode_payment_payload`, `build_payment_metadata`, `parse_payment_metadata`, `with_admin_notification_service`, `calculate_missing_amount`
- `app/services/subscription_service.py` — Python-модуль
  Классы: `PropagateSquadsResult`, `SubscriptionService` (30 методов)
  Функции: `get_traffic_reset_strategy` — Получает стратегию сброса трафика., `reset_subscription_with_panel` — Обнулить подписку «как будто не оформляли» и снять доступ в панели RemnaWave,
- `app/services/support_settings_service.py` — Python-модуль
  Классы: `SupportSettingsService` (28 методов)
  Функции: нет
- `app/services/system_error_log_service.py` — Python-модуль
  Классы: `SystemErrorLogService` (13 методов)
  Функции: нет
- `app/services/system_settings_service.py` — Python-модуль
  Классы: `SettingDefinition` (1 методов), `ChoiceOption`, `ReadOnlySettingError`, `BotConfigurationService` (53 методов)
  Функции: нет
- `app/services/tabpay_service.py` — Python-модуль
  Классы: `TabPayAPIError` (1 методов), `TabPayNetworkError`, `TabPayService` (15 методов)
  Функции: нет
- `app/services/tariff_custom_traffic.py` — Python-модуль
  Классы: нет
  Функции: `parse_positive_rubles_to_kopeks` — Parse a positive ruble amount without floating-point rounding., `parse_positive_gb` — Parse a positive whole-number traffic amount in gigabytes., `validate_custom_traffic_configuration` — Return user-facing validation errors for enabling custom traffic.
- `app/services/traffic_monitoring_service.py` — Python-модуль
  Классы: `TrafficViolation`, `TrafficMonitoringServiceV2` (35 методов), `TrafficMonitoringSchedulerV2` (8 методов), `TrafficMonitoringService` (6 методов), `TrafficMonitoringScheduler` (9 методов)
  Функции: нет
- `app/services/trial_activation_service.py` — Python-модуль
  Классы: `TrialPaymentError`, `TrialPaymentInsufficientFunds` (1 методов), `TrialPaymentChargeFailed`, `TrialActivationReversionResult`
  Функции: `get_trial_activation_charge_amount` — Returns the configured activation charge in kopeks if payment is enabled., `preview_trial_activation_charge` — Validates that the user can afford the trial activation charge., `charge_trial_activation_if_required` — Charges the user's balance if paid trial activation is enabled., `refund_trial_activation_charge` — Refunds a previously charged trial activation amount back to the user., `rollback_trial_subscription_activation` — Attempts to undo a previously created trial subscription., `revert_trial_activation` — Rolls back a trial subscription and refunds any charged amount.
- `app/services/tribute_service.py` — Python-модуль
  Классы: `TributeService` (14 методов)
  Функции: нет
- `app/services/user_action_log_service.py` — Python-модуль
  Классы: нет
  Функции: `normalize_cabinet_path` — Сворачивает числовые сегменты пути в {id} для группировки однотипных действий., `should_log_cabinet_action`, `schedule_cabinet_action_log` — Fire-and-forget запись действия юзера в кабинете — не задерживает запрос.
- `app/services/user_avatar_service.py` — Python-модуль
  Классы: нет
  Функции: `pick_avatar_file_id` — Самый маленький размер, который ещё не мылится в шапке; иначе самый крупный., `get_avatar_file_id` — file_id текущего фото профиля или None. Никогда не бросает: аватар — не повод ронять кабинет.
- `app/services/user_cart_service.py` — Python-модуль
  Классы: `UserCartService` (15 методов)
  Функции: нет
- `app/services/user_revival_service.py` — Python-модуль
  Классы: `NotDeletedError`
  Функции: `revive_deleted_user` — Flip ``user.status`` from DELETED back to ACTIVE.
- `app/services/user_service.py` — Python-модуль
  Классы: `DeleteUserResult`, `UserService` (19 методов)
  Функции: нет
- `app/services/version_service.py` — Python-модуль
  Классы: `VersionInfo` (6 методов), `VersionService` (11 методов)
  Функции: нет
- `app/services/wata_service.py` — Python-модуль
  Классы: `WataAPIError`, `WataService` (13 методов)
  Функции: нет
- `app/services/web_api_token_service.py` — Python-модуль
  Классы: `WebApiTokenService` (8 методов)
  Функции: `ensure_default_web_api_token` — Ensure the bootstrap web API token from config exists in the DB.
- `app/services/web_auth_service.py` — Python-модуль
  Классы: нет
  Функции: `create_web_auth_token` — Generate a web auth token and store it in Redis (pending state)., `link_web_auth_token` — Link a web auth token to a Telegram user (called by bot on /start)., `poll_web_auth_token` — Poll for web auth token status (non-destructive)., `consume_web_auth_token` — Atomically get and delete a web auth token.
- `app/services/webhook_service.py` — Python-модуль
  Классы: `DeliveryResult`, `WebhookService` (7 методов)
  Функции: нет
- `app/services/wheel_service.py` — Python-модуль
  Классы: `SpinResult`, `EligibleSubscription`, `SpinAvailability`, `FortuneWheelService` (14 методов)
  Функции: нет
- `app/services/yandex_offline_conv_service.py` — Python-модуль
  Классы: нет
  Функции: `spawn_bg` — Spawn a background Yandex conversion task with proper reference tracking., `fire_registration_bg` — Fire registration event in background with its own DB session., `fire_trial_bg` — Fire trial event in background with its own DB session., `fire_purchase_bg` — Fire purchase event in background with its own DB session., `store_cid` — Store Yandex ClientID (and optional yclid) for a user. Returns True if stored., `store_cid_and_fire_registration` — Store Yandex CID and fire registration conversion in background (best-effort)., `store_cid_and_fire_purchase` — Persist a freshly-provided CID, then fire a purchase event in background., `store_cid_only` — Persist a freshly-provided CID WITHOUT firing a purchase event., `store_cid_and_fire_trial` — Same as `store_cid_and_fire_purchase` but for trial activation (#558449)., `on_registration` — Fire registration event (once per user)., `on_trial` — Fire trial-add event (once per user)., `on_purchase` — Fire ecommerce purchase event (every payment)., `parse_cid_from_start_param` — Extract Yandex CID from bot start parameter.
- `app/services/yookassa_service.py` — Python-модуль
  Классы: `YooKassaService` (5 методов)
  Функции: нет

#### app/services/contests

- `app/services/contests/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/services/contests/attempt_service.py` — Python-модуль
  Классы: `AttemptResult`, `ContestAttemptService` (5 методов)
  Функции: нет
- `app/services/contests/enums.py` — Python-модуль
  Классы: `GameType` (2 методов), `RoundStatus`, `PrizeType`
  Функции: нет
- `app/services/contests/games.py` — Python-модуль
  Классы: `GameRenderResult`, `AnswerCheckResult`, `BaseGameStrategy` (5 методов), `QuestButtonsStrategy` (3 методов), `LockHackStrategy` (3 методов), `ServerLotteryStrategy` (3 методов), `BlitzReactionStrategy` (3 методов), `LetterCipherStrategy` (3 методов), `EmojiGuessStrategy` (3 методов), `AnagramStrategy` (3 методов)
  Функции: `get_game_strategy` — Get game strategy by type., `get_all_game_types` — Get list of all supported game types.

#### app/services/menu_layout

- `app/services/menu_layout/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/services/menu_layout/constants.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/services/menu_layout/context.py` — Python-модуль
  Классы: `MenuContext`
  Функции: нет
- `app/services/menu_layout/history_service.py` — Python-модуль
  Классы: `MenuLayoutHistoryService` (5 методов)
  Функции: нет
- `app/services/menu_layout/service.py` — Python-модуль
  Классы: `MenuLayoutService` (48 методов)
  Функции: нет
- `app/services/menu_layout/stats_service.py` — Python-модуль
  Классы: `MenuLayoutStatsService` (14 методов)
  Функции: нет

#### app/services/payment

- `app/services/payment/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/services/payment/antilopay.py` — Python-модуль
  Классы: `AntilopayPaymentMixin` (4 методов)
  Функции: нет
- `app/services/payment/aurapay.py` — Python-модуль
  Классы: `AuraPayPaymentMixin` (4 методов)
  Функции: нет
- `app/services/payment/cispay.py` — Python-модуль
  Классы: `CisPayPaymentMixin` (4 методов)
  Функции: `resolve_cispay_method` — Определяет payment_method для API cisPay.
- `app/services/payment/cloudpayments.py` — Python-модуль
  Классы: `CloudPaymentsPaymentMixin` (6 методов)
  Функции: нет
- `app/services/payment/common.py` — Python-модуль
  Классы: `PaymentCommonMixin` (4 методов)
  Функции: `notify_email_user_topup` — «Пополнение успешно» для юзеров без Telegram (#2952)., `send_cart_notification_after_topup` — Run post-topup side-effects: resume daily / auto-purchase saved cart / auto-extend., `try_fulfill_guest_purchase` — Attempt to fulfill a guest purchase detected in payment metadata.
- `app/services/payment/cryptobot.py` — Python-модуль
  Классы: `CryptoBotPaymentMixin` (6 методов)
  Функции: нет
- `app/services/payment/donut.py` — Python-модуль
  Классы: `DonutPaymentMixin` (5 методов)
  Функции: нет
- `app/services/payment/etoplatezhi.py` — Python-модуль
  Классы: `EtoplatezhiPaymentMixin` (3 методов)
  Функции: нет
- `app/services/payment/freekassa.py` — Python-модуль
  Классы: `FreekassaPaymentMixin` (5 методов)
  Функции: нет
- `app/services/payment/heleket.py` — Python-модуль
  Классы: `HeleketPaymentMixin` (5 методов)
  Функции: нет
- `app/services/payment/jupiter.py` — Python-модуль
  Классы: `JupiterPaymentMixin` (5 методов)
  Функции: нет
- `app/services/payment/kassa_ai.py` — Python-модуль
  Классы: `KassaAiPaymentMixin` (5 методов)
  Функции: нет
- `app/services/payment/lava.py` — Python-модуль
  Классы: `LavaPaymentMixin` (14 методов)
  Функции: `enable_lava_recurring` — Включить автопродление Lava. Возвращает {local_id, lava_subscription_id, redirect_url, status}., `purchase_tariff_with_lava_recurring` — Оформление подписки на тариф оплатой через автопродление Lava., `cancel_lava_recurring_for_subscription_safe` — Точка входа для путей удаления/отзыва подписки: отменяет активное, `get_lava_recurring_status` — Состояние активной привязки Lava для UI (бот/кабинет) либо None., `cancel_lava_recurring_by_local_id` — Отмена привязки по локальному id (кабинет/бот). Идемпотентна., `shift_lava_next_charge_after_manual_extension` — Сдвигает дату следующего списания Lava после РУЧНОГО продления подписки.
- `app/services/payment/mulenpay.py` — Python-модуль
  Классы: `MulenPayPaymentMixin` (6 методов)
  Функции: нет
- `app/services/payment/overpay.py` — Python-модуль
  Классы: `OverpayPaymentMixin` (4 методов)
  Функции: нет
- `app/services/payment/pal24.py` — Python-модуль
  Классы: `Pal24PaymentMixin` (11 методов)
  Функции: нет
- `app/services/payment/paritypay.py` — Python-модуль
  Классы: `ParityPayPaymentMixin` (5 методов)
  Функции: `resolve_paritypay_method` — Определяет поле ``service`` для API ParityPay.
- `app/services/payment/paypear.py` — Python-модуль
  Классы: `PayPearPaymentMixin` (4 методов)
  Функции: нет
- `app/services/payment/platega.py` — Python-модуль
  Классы: `PlategaPaymentMixin` (9 методов)
  Функции: `enable_platega_sbp_recurring` — Создать СБП-автопродление. Возвращает {local_id, platega_subscription_id, redirect_url, status}., `purchase_tariff_with_sbp_recurring` — Оформление подписки на тариф с оплатой через СБП-автопродление Platega., `replay_missed_platega_charges` — Доначисляет списания, чьи CONFIRMED-коллбеки потерялись., `cancel_platega_recurring_for_subscription_safe` — Точка входа для путей удаления/отзыва подписки: отменяет активную
- `app/services/payment/riopay.py` — Python-модуль
  Классы: `RioPayPaymentMixin` (4 методов)
  Функции: нет
- `app/services/payment/rollypay.py` — Python-модуль
  Классы: `RollyPayPaymentMixin` (4 методов)
  Функции: нет
- `app/services/payment/severpay.py` — Python-модуль
  Классы: `SeverPayPaymentMixin` (4 методов)
  Функции: нет
- `app/services/payment/stars.py` — Python-модуль
  Классы: `TelegramStarsMixin` (7 методов)
  Функции: нет
- `app/services/payment/tabpay.py` — Python-модуль
  Классы: `TabPayPaymentMixin` (5 методов)
  Функции: `resolve_tabpay_method` — Определяет поле ``method`` для API TabPay.
- `app/services/payment/tribute.py` — Python-модуль
  Классы: `TributePaymentMixin` (2 методов)
  Функции: нет
- `app/services/payment/wata.py` — Python-модуль
  Классы: `WataPaymentMixin` (4 методов)
  Функции: нет
- `app/services/payment/yookassa.py` — Python-модуль
  Классы: `YooKassaPaymentMixin` (15 методов)
  Функции: нет

#### app/services/reachability

- `app/services/reachability/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/services/reachability/batches.py` — Python-модуль
  Классы: `BatchService` (3 методов), `BatchPreview`
  Функции: `chunk_targets`, `estimate_batch_minutes` — Примерное время всей пачки: раунды по ``parallel`` чашек, раунд длится по числу симок., `batch_status_from_jobs` — None — пачка ещё идёт; иначе итог: отменена, не удалась целиком или завершена., `batch_cost_kopeks`, `batch_done_targets`, `preview_batch` — Цена и время всей пачки: превью каждой чашки (бесплатно, без троттла) и сумма., `create_batch` — Одна пачка и задача на каждую чашку; деньги проверяются до записи, драйвер стартует после коммита.
- `app/services/reachability/cores.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/services/reachability/gate.py` — Python-модуль
  Классы: `PaidCallGate` (3 методов)
  Функции: нет
- `app/services/reachability/jobs.py` — Python-модуль
  Классы: `RunnerConfig`, `JobNotCancellable`, `JobRunner` (41 методов)
  Функции: нет
- `app/services/reachability/kinds.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/services/reachability/legs.py` — Python-модуль
  Классы: нет
  Функции: `build_probe_legs` — Леги probe: ``by_target[цель].by_operator[op_key]`` → по одному легу на пару., `vless_op_key` — У лега VLESS нет op_key — собираем ``оператор|округ|on/off`` из его полей., `build_vless_legs` — Леги VLESS: сервер ищется по ``server_addr`` (= target_key), запасной путь — по имени., `merge_skipped` — Наши пропуски (расчёт по каталогу) + пропуски из ответа API. Всегда новый словарь., `partial_probe_progress` — Срез частичного результата пробы из 409 request_in_progress — для «проверяем…» в кабинете.
- `app/services/reachability/links.py` — Python-модуль
  Классы: `ParsedLink`, `RejectedLink`
  Функции: `parse_links`, `expand_raw_input` — Строки поля «Конфиг или подписка»: ссылки и URL как есть, base64-блоб — в ссылки.
- `app/services/reachability/panel_links.py` — Python-модуль
  Классы: нет
  Функции: `decode_subscription_body` — Тело публичной подписки: ссылки построчно, base64 от них или xray-json; страница — пусто., `hwid_required`, `fetch_panel_links` — Первый непустой список ссылок из трёх источников; ошибки источника — в лог, не наружу.
- `app/services/reachability/preview.py` — Python-модуль
  Классы: `PreviewResult`
  Функции: нет
- `app/services/reachability/pricing.py` — Python-модуль
  Классы: `CostLimitExceeded` (1 методов)
  Функции: `format_rubles`, `credits_to_kopeks`, `estimate_vless_kopeks`, `enforce_cost_limit`
- `app/services/reachability/requests.py` — Python-модуль
  Классы: `RequestBuildError`
  Функции: `normalize_probes`, `sni_name_for` — Имя для TLS-SNI: SNI цели, а без него — её адрес, если это домен. У голого IP имени нет., `sni_hosts_for` — Имена для SNI-пробы по всем целям: уникальные, по алфавиту, без IP-адресов., `normalize_sni_hosts` — Свои имена для TLS-SNI: домены в нижнем регистре, без повторов, не больше пяти., `resolve_sni_hosts` — Имена для SNI-пробы: свои → из целей → белый домен по умолчанию., `build_probe_request`, `build_vless_request`, `build_scan_request`
- `app/services/reachability/resolver.py` — Python-модуль
  Классы: `TargetResolutionError`, `HostView`, `NodeView`, `SubscriptionConfigs`, `TargetResolver` (15 методов)
  Функции: `target_from_host`, `target_from_node`, `target_from_link`, `target_from_cidr`
- `app/services/reachability/service.py` — Python-модуль
  Классы: `ReachabilityDisabled` (1 методов), `ReachabilityUnhealthy` (1 методов), `ReachabilityBusy` (1 методов), `PanelUnavailable` (1 методов), `JobNotFound`, `Health` (1 методов), `Quote`, `ParsedConfig`, `ParsedInput`, `ReachabilityService` (50 методов)
  Функции: нет
- `app/services/reachability/status.py` — Python-модуль
  Классы: `StatusSource` (8 методов), `AccountCache` (3 методов)
  Функции: `account_summary` — Поля аккаунта для фронта; webhook_secret клиент уже отбросил., `reference_status`, `collect_status`
- `app/services/reachability/subscriptions.py` — Python-модуль
  Классы: `SubscriptionFetchError`, `PublicOnlyResolver` (3 методов)
  Функции: `is_subscription_url`, `validate_public_url`, `fetch_subscription_links` — Ссылки конфигов по публичному URL подписки; страница или заглушка вместо них — ошибка.
- `app/services/reachability/summary.py` — Python-модуль
  Классы: нет
  Функции: `build_summary_rows` — Всегда новые словари: строки и ячейки не разделяются между вызовами.
- `app/services/reachability/targets.py` — Python-модуль
  Классы: `TargetValidationError`, `Target` (2 методов)
  Функции: `is_hostname` — Синтаксически корректное доменное имя (строчные буквы, без localhost)., `target_key`, `probe_api_target` — Строка цели для API: IP/домен с портом либо без., `normalize_custom_target` — IP, домен, адрес:порт или URL → цель. Схема отбрасывается (HTTP-проба у API с http:// не работает)., `is_reality_like` — SNI чужого домена — признак Reality с dest на «белом» сайте., `guess_purpose`, `validate_cidr24`, `cidr24_for_ip`, `hosts_for_node` — Хосты ноды: по инбаунду, а без него — по совпадению адреса с адресом/IP ноды.
- `app/services/reachability/units.py` — Python-модуль
  Классы: `SelectorError`, `Unit` (1 методов), `Selector`, `Expansion`, `UnitsCatalog` (3 методов), `UnitsCache` (2 методов)
  Функции: `parse_selector`
- `app/services/reachability/verdict.py` — Python-модуль
  Классы: нет
  Функции: `probe_leg_verdict`, `compact_probe_verdict` — Вердикт по компактной ячейке частичного результата (409): SNI → TCP → ICMP, как у полной формы., `vless_leg_verdict`, `matches_expectation` — True/False, когда ожидание есть; None — справочная строка без ожидания.
- `app/services/reachability/xray_json.py` — Python-модуль
  Классы: нет
  Функции: `links_from_xray_json` — Ссылки из JSON-подписки; не JSON или без прокси-outbound — пусто.

### app/tools

- `app/tools/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/tools/grace_access.py` — Python-модуль
  Классы: нет
  Функции: `main`

### app/utils

- `app/utils/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/utils/bot_identity.py` — Python-модуль
  Классы: нет
  Функции: `sync_bot_username` — Sync settings.BOT_USERNAME from the live bot (token-authoritative).
- `app/utils/button_styles_cache.py` — Python-модуль
  Классы: нет
  Функции: `get_cached_button_styles` — Return the current merged config (DB overrides + defaults)., `load_button_styles_cache` — Load button styles from DB and refresh the module cache.
- `app/utils/cache.py` — Python-модуль
  Классы: `CacheService` (20 методов), `UserCache` (6 методов), `SystemCache` (6 методов), `RateLimitCache` (4 методов), `TokenReplayCache` (1 методов), `ChannelSubCache` (8 методов)
  Функции: `cache_key`, `cached_function`
- `app/utils/chat_menu_button.py` — Python-модуль
  Классы: нет
  Функции: `configure_chat_menu_button` — Выставляет кнопку меню на открытие веб-кабинета. Возвращает True, если выставлена.
- `app/utils/check_reg_process.py` — Python-модуль
  Классы: нет
  Функции: `is_registration_process`
- `app/utils/currency_converter.py` — Python-модуль
  Классы: `CurrencyConverter` (8 методов)
  Функции: нет
- `app/utils/decorators.py` — Python-модуль
  Классы: нет
  Функции: `admin_required`, `auth_required` — Простая проверка на наличие пользователя в апдейте. Middleware уже подтягивает db_user,, `error_handler`, `state_cleanup`, `typing_action`, `rate_limit`
- `app/utils/display_mode.py` — Python-модуль
  Классы: нет
  Функции: `normalize_display_mode`, `is_visible_in_bot`, `is_visible_in_web`, `next_display_mode`, `display_mode_label`
- `app/utils/email_alias.py` — Python-модуль
  Классы: нет
  Функции: `canonical_email` — Вид адреса для сравнения: тот же ящик — та же строка., `is_email_alias_of` — Оба адреса ведут в один ящик, но записаны по-разному., `email_domain` — Домен адреса с учётом слияния доменов-близнецов (ya.ru → yandex.ru)., `has_alias_forms` — У этого адреса вообще бывают алиасы — есть ли смысл искать двойников., `sibling_domains` — Домены, письма с которых попадают в тот же ящик, включая сам домен., `alias_match_clause` — SQLAlchemy-условие: колонка хранит адрес того же ящика, что и ``email``.
- `app/utils/formatters.py` — Python-модуль
  Классы: нет
  Функции: `format_datetime`, `format_date`, `format_time_ago`, `format_days_declension`, `format_duration`, `format_bytes`, `format_percentage`, `format_number`, `format_price_range`, `truncate_text`, `format_username`, `format_username_link` — Telegram-логин явной ссылкой — для rich-сообщений., `format_subscription_status`, `format_traffic_usage`, `format_boolean`
- `app/utils/formatting.py` — Python-модуль
  Классы: нет
  Функции: `safe_html_name` — HTML-escape a display name for Telegram HTML messages., `user_html_link` — Build an HTML-safe clickable user link for Telegram messages., `format_traffic` — Форматирует трафик., `format_price_kopeks` — Форматирует цену из копеек в рубли., `format_period` — Форматирует период.
- `app/utils/gift_links.py` — Python-модуль
  Классы: `GiftLinkError`, `InvalidGiftTokenError`, `InvalidBotUsernameError`, `MissingBotUsernameError`, `InvalidCabinetUrlError`, `MissingCabinetUrlError`, `InvalidClaimLinkError`, `InvalidShareTextError`, `GiftClaimArtifacts`
  Функции: `build_gift_public_code` — Build the canonical public gift code (``GIFT_<59_chars>``)., `parse_gift_claim_input` — Parse and normalize gift claim credentials from various user inputs., `build_gift_claim_artifacts` — Build immutable gift claim artifacts with public code and available channel URLs., `build_bot_gift_claim_link` — Build a canonical Telegram deep-link for claiming a gift subscription., `build_cabinet_gift_claim_link` — Build a canonical web cabinet claim link containing the full bearer token., `build_telegram_gift_share_url` — Build a native Telegram share URL (``https://t.me/share/url``) with prefilled text.
- `app/utils/incy_crypt1.py` — Python-модуль
  Классы: нет
  Функции: `encrypt_incy_link` — Шифрует ссылку подписки в ``incy://crypt1/...``., `wrap_incy_deep_link` — Подменяет ``incy://import|add/<url>`` на ``incy://crypt1/<зашифрованное>``.
- `app/utils/log_handlers.py` — Python-модуль
  Классы: `LevelFilterHandler` (5 методов), `PaymentLogFilter` (1 методов), `ExcludePaymentFilter` (1 методов)
  Функции: нет
- `app/utils/long_messages.py` — Python-модуль
  Классы: нет
  Функции: `answer_long_text` — message.answer с разбивкой; клавиатура — на последнем куске., `edit_long_text` — message.edit_text с разбивкой., `send_long_text` — bot.send_message с разбивкой; клавиатура — на последнем куске.
- `app/utils/markdown_to_telegram.py` — Python-модуль
  Классы: нет
  Функции: `github_markdown_to_telegram_html` — Convert GitHub-flavored Markdown to Telegram HTML., `truncate_for_blockquote` — Truncate description HTML to fit within Telegram message limit inside a blockquote.
- `app/utils/menu_layout_cache.py` — Python-модуль
  Классы: нет
  Функции: `get_cached_menu_layout` — Return the current layout config (DB overrides + defaults)., `load_menu_layout_cache` — Load menu layout from DB and refresh the module cache.
- `app/utils/message_patch.py` — Python-модуль
  Классы: нет
  Функции: `caption_exceeds_telegram_limit` — Check if text exceeds Telegram's caption limit (1024 parsed chars)., `get_logo_media` — Возвращает кешированный file_id или FSInputFile для логотипа., `is_qr_message`, `append_privacy_hint`, `prepare_privacy_safe_kwargs`, `is_privacy_restricted_error`, `is_topic_required_error` — Проверяет, является ли ошибка связанной с топиками/форумами., `patch_message_methods`
- `app/utils/miniapp_buttons.py` — Python-модуль
  Классы: нет
  Функции: `strip_leading_emoji` — Удалить ведущий юникод-emoji + следующий пробел. Безопасно для текста без emoji., `build_main_menu_button` — Always-callback button for "Main Menu" / "Главное меню" navigation., `build_cabinet_url` — Join ``MINIAPP_CUSTOM_URL`` with an optional *path* segment., `build_miniapp_or_callback_button` — Create a button that opens the cabinet miniapp or falls back to a callback., `build_subscription_extend_button` — Кнопка «Продлить подписку» для уведомлений — единая точка на весь бот., `build_miniapp_startapp_url` — Собрать t.me Mini App deep link, открывающий кабинет в ЛЮБОМ типе чата., `build_admin_ticket_cabinet_button` — Кнопка «открыть тикет в админ-кабинете» для уведомления о тикете.
- `app/utils/notification_prefs.py` — Python-модуль
  Классы: нет
  Функции: `get_user_notification_pref` — Get a single notification preference for user., `is_subscription_expiry_enabled` — Check if subscription expiry notifications are enabled for user., `get_subscription_expiry_days` — Get the number of days before expiry to notify., `is_traffic_warning_enabled` — Check if traffic warning notifications are enabled for user., `get_traffic_warning_percent` — Get the traffic usage percentage threshold for warning., `is_balance_low_enabled` — Check if low balance notifications are enabled for user., `get_balance_low_threshold` — Get the low balance threshold in kopeks., `is_news_enabled` — Check if news notifications are enabled for user., `is_promo_offers_enabled` — Check if promo offer notifications are enabled for user., `filter_users_by_broadcast_category` — Отсеивает отписавшихся от рассылки этой категории.
- `app/utils/pagination.py` — Python-модуль
  Классы: `PaginationResult` (1 методов)
  Функции: `paginate_list`, `get_pagination_info`, `get_page_numbers`
- `app/utils/panel_node_usage.py` — Python-модуль
  Классы: нет
  Функции: `coerce_bytes`, `normalize_node_usage` — Привести элементы потребления к форме `{user_id, username, node_uuid, total_bytes}`.
- `app/utils/payment_logger.py` — Python-модуль
  Классы: нет
  Функции: `configure_payment_logger` — Configure the payment logger with the given handler., `get_payment_logger` — Return the payment logger instance.
- `app/utils/payment_utils.py` — Python-модуль
  Классы: нет
  Функции: `verify_payment_amount` — Check that the received amount matches the expected amount within tolerance., `get_available_payment_methods` — Возвращает список доступных способов оплаты с их настройками, `get_payment_methods_text` — Генерирует текст с описанием доступных способов оплаты, `is_payment_method_available` — Проверяет, доступен ли конкретный способ оплаты, `get_payment_method_status` — Возвращает статус всех способов оплаты, `get_enabled_payment_methods_count` — Возвращает количество включенных способов оплаты (не считая поддержку)
- `app/utils/photo_message.py` — Python-модуль
  Классы: нет
  Функции: `safe_edit_or_resend` — Безопасно отредактировать текст сообщения или отправить новое при ошибке., `edit_or_answer_photo`
- `app/utils/price_display.py` — Python-модуль
  Классы: `PriceInfo` (2 методов)
  Функции: `calculate_user_price` — Calculate final price for a user with all applicable discounts., `format_price_button` — Format a price button text with unified discount display., `format_price_text` — Format a price for message text (not button) with unified discount display.
- `app/utils/pricing_utils.py` — Python-модуль
  Классы: нет
  Функции: `calculate_months_from_days`, `calculate_price_per_month` — Месячная ставка для периода длиной period_days дней (месяц = 30 дней)., `calculate_prorated_price` — Calculate prorated price based on remaining days., `apply_percentage_discount` — Apply percentage discount using PricingEngine's floor division., `resolve_discount_percent` — Определяет размер скидки для указанной категории., `compute_simple_subscription_price` — Вычисляет стоимость простой подписки с учетом всех доплат и скидок., `format_period_description`, `validate_pricing_calculation`
- `app/utils/promo_offer.py` — Python-модуль
  Классы: нет
  Функции: `get_user_active_promo_discount_percent`, `consume_user_promo_offer` — Consume the user's one-shot promo-offer discount (zeroes out the fields)., `build_promo_offer_timer_line`, `build_promo_offer_hint`, `build_test_access_hint`
- `app/utils/promo_rate_limiter.py` — Python-модуль
  Классы: `PromoRateLimiter` (8 методов)
  Функции: `validate_promo_format` — Проверяет формат промокода: 3-50 символов, только буквы/цифры/дефис/подчёркивание.
- `app/utils/proxy.py` — Python-модуль
  Классы: нет
  Функции: `mask_proxy_url` — Mask credentials in a proxy URL for safe logging., `sanitize_proxy_error` — Strip proxy credentials from exception messages.
- `app/utils/redis_client.py` — Python-модуль
  Классы: нет
  Функции: `create_redis` — Клиент с пулом соединений к ``url`` (по умолчанию ``settings.REDIS_URL``).
- `app/utils/rich_admin.py` — Python-модуль
  Классы: нет
  Функции: `is_rich_admin_enabled`, `rich_footer_now` — Футер с меткой и временем: tg-time рендерится в таймзоне админа., `rich_kv_table` — Таблица «показатель → значение» (bordered/striped). Значения — сырой HTML., `rich_traceback_details` — Сворачиваемый traceback: <details> + <pre><code class="language-python">., `classic_admin_html_to_rich` — Конвертирует классическое HTML-уведомление в rich-разметку., `try_send_rich_admin_message` — Отправляет rich-сообщение в админ-чат. False — слать классический вариант.
- `app/utils/rich_buttons.py` — Python-модуль
  Классы: нет
  Функции: `render_keyboard_as_rich_html` — Клавиатура целиком в виде рядов ``<tg-button-row>``.
- `app/utils/rich_menu.py` — Python-модуль
  Классы: нет
  Функции: `is_rich_menu_enabled`, `build_main_menu_rich_html` — Собирает rich-HTML главного меню (контент, без клавиатуры)., `try_send_rich_main_menu` — Отправляет главное меню rich-сообщением. False — показать классическое меню., `try_answer_rich_main_menu` — Rich-аналог message.answer(menu_text) для /start и завершения регистрации., `try_edit_rich_main_menu` — Rich-аналог edit_or_answer_photo для callback-навигации. False — рисовать классику.
- `app/utils/rich_notify.py` — Python-модуль
  Классы: нет
  Функции: `build_notification_rich_html` — Текст уведомления → rich-разметка в стиле главного меню., `try_send_rich_notification` — Шлёт уведомление rich-сообщением. ``False`` — отправить классическое.
- `app/utils/security.py` — Python-модуль
  Классы: нет
  Функции: `hash_api_token` — Возвращает хеш токена в формате hex., `generate_api_token` — Генерирует криптографически стойкий токен.
- `app/utils/startup_timeline.py` — Python-модуль
  Классы: `StepRecord`, `StageHandle` (6 методов), `StartupTimeline` (7 методов)
  Функции: нет
- `app/utils/subscription_utils.py` — Python-модуль
  Классы: нет
  Функции: `cleanup_duplicate_subscriptions`, `get_display_subscription_link`, `get_happ_cryptolink_redirect_link`, `convert_subscription_link_to_happ_scheme`, `device_limit_needs_heal` — Return True if a stored ``device_limit`` is structurally invalid., `coerce_panel_device_limit` — Normalize ``hwidDeviceLimit`` from a RemnaWave panel response., `resolve_min_device_limit` — Нижняя граница, до которой пользователь может уменьшить лимит устройств., `resolve_hwid_device_limit` — Return a device limit value for RemnaWave payloads when selection is enabled., `resolve_hwid_device_limit_for_payload` — Return the device limit that should be sent to RemnaWave APIs., `resolve_simple_subscription_device_limit` — Return the effective device limit for simple subscription flows.
- `app/utils/telegram_html.py` — Python-модуль
  Классы: нет
  Функции: `html_to_telegram`, `info_page_faq_to_telegram`, `stored_html_to_telegram_pages` — Сохранённый HTML страницы → куски, которые Telegram согласится разобрать., `trim_broken_markup` — Обрезает незакрытый тег или неполную HTML-сущность в конце куска., `split_telegram_text`
- `app/utils/telegram_webapp.py` — Python-модуль
  Классы: `TelegramWebAppAuthError`
  Функции: `parse_webapp_init_data` — Validate and parse Telegram WebApp init data.
- `app/utils/text_search.py` — Python-модуль
  Классы: нет
  Функции: `case_variants` — Регистровые варианты термина, по которым имеет смысл искать., `contains_patterns` — LIKE-шаблоны «содержит term» для всех регистровых вариантов., `contains_conditions` — ILIKE-условия «колонка содержит term» — по каждой колонке и каждому варианту., `contains_clause` — Готовое OR-условие «хотя бы одна колонка содержит term».
- `app/utils/ticket_text.py` — Python-модуль
  Классы: нет
  Функции: `preview_text` — Короткое превью сообщения для уведомлений (полный текст — в карточке тикета)., `split_long_block` — Режет блок по границам строк/слов, не разрывая HTML-теги и сущности., `build_ticket_pages` — Собирает страницы «шапка + сообщения», не теряя ни одного символа.
- `app/utils/timezone.py` — Python-модуль
  Классы: нет
  Функции: `get_local_timezone` — Return the configured local timezone., `panel_datetime_to_utc` — Normalize a RemnaWave panel datetime to aware UTC., `to_local_datetime` — Convert a datetime value to the configured local timezone., `format_local_datetime` — Format a datetime value in the configured local timezone., `format_email_datetime` — Format a datetime for email-template substitution.
- `app/utils/user_utils.py` — Python-модуль
  Классы: нет
  Функции: `format_referrer_info` — Return formatted referrer info for admin notifications., `generate_unique_referral_code`, `get_effective_referral_commission_percent` — Возвращает индивидуальный процент комиссии пользователя или дефолтное значение., `mark_user_as_had_paid_subscription`, `get_user_referral_summary`, `get_detailed_referral_list`, `get_referral_analytics`
- `app/utils/validators.py` — Python-модуль
  Классы: нет
  Функции: `validate_email`, `validate_phone`, `validate_telegram_username`, `validate_promocode`, `validate_amount`, `validate_positive_integer`, `validate_date_string`, `validate_url`, `validate_uuid`, `validate_traffic_amount`, `validate_subscription_period`, `sanitize_html` — Безопасно санитизирует HTML-текст, заменяя HTML-сущности на соответствующие теги,, `sanitize_telegram_name` — Санитизация Telegram-имени для безопасной вставки в HTML и хранения., `validate_device_count`, `validate_referral_code`, `validate_html_tags`, `validate_html_structure`, `fix_html_tags`, `get_html_help_text`, `validate_rules_content`

### app/webapi

- `app/webapi/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/webapi/app.py` — Python-модуль
  Классы: нет
  Функции: `create_web_api_app`
- `app/webapi/background/`
- `app/webapi/dependencies.py` — Python-модуль
  Классы: нет
  Функции: `get_db_session`, `require_api_token`
- `app/webapi/docs.py` — Python-модуль
  Классы: нет
  Функции: `add_redoc_endpoint` — Attach a ReDoc endpoint if docs are enabled.
- `app/webapi/middleware.py` — Python-модуль
  Классы: `RequestLoggingMiddleware` (1 методов)
  Функции: нет
- `app/webapi/routes/`
- `app/webapi/schemas/`
- `app/webapi/server.py` — Python-модуль
  Классы: `WebAPIServer` (3 методов)
  Функции: нет

#### app/webapi/background

- `app/webapi/background/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/webapi/background/backup_tasks.py` — Python-модуль
  Классы: `BackupTaskState`, `BackupTaskManager` (5 методов)
  Функции: нет

#### app/webapi/routes

- `app/webapi/routes/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/webapi/routes/_subscription_state.py` — Python-модуль
  Классы: нет
  Функции: `snapshot_subscription_state`, `restore_subscription_state`
- `app/webapi/routes/backups.py` — Python-модуль
  Классы: нет
  Функции: `create_backup_endpoint`, `list_backups`, `get_backup_status`, `list_backup_tasks`, `download_backup`, `restore_backup`, `upload_and_restore_backup`, `delete_backup`
- `app/webapi/routes/ban_notifications.py` — Python-модуль
  Классы: нет
  Функции: `send_ban_notification` — Отправить уведомление пользователю от ban системы
- `app/webapi/routes/broadcasts.py` — Python-модуль
  Классы: нет
  Функции: `create_broadcast`, `list_broadcasts`, `stop_broadcast`
- `app/webapi/routes/campaigns.py` — Python-модуль
  Классы: нет
  Функции: `create_campaign_endpoint`, `list_campaigns`, `delete_campaign_endpoint`, `update_campaign_endpoint`
- `app/webapi/routes/config.py` — Python-модуль
  Классы: нет
  Функции: `list_categories`, `list_settings`, `get_setting`, `update_setting`, `reset_setting`
- `app/webapi/routes/contests.py` — Python-модуль
  Классы: нет
  Функции: `list_daily_templates`, `get_daily_template`, `update_daily_template`, `start_round_now`, `list_rounds`, `get_round`, `finish_round_now`, `list_attempts`, `list_referral`, `create_referral`, `get_referral`, `update_referral`, `toggle_referral`, `delete_referral`, `list_referral_events`, `get_referral_detailed_stats`
- `app/webapi/routes/health.py` — Python-модуль
  Классы: нет
  Функции: `health_check`, `database_health` — Детальная информация о состоянии базы данных., `pool_metrics` — Метрики пула подключений к базе данных.
- `app/webapi/routes/logs.py` — Python-модуль
  Классы: нет
  Функции: `get_system_log_preview` — Получить предпросмотр системного лог-файла бота., `download_system_log` — Скачать полный лог-файл бота., `get_system_log_full` — Получить полный системный лог-файл бота., `list_monitoring_logs` — Получить список логов мониторинга с пагинацией., `list_monitoring_event_types` — Получить список доступных типов событий мониторинга., `list_support_audit_logs` — Получить список аудита действий модераторов поддержки., `list_support_audit_actions` — Получить список действий, доступных в аудите поддержки.
- `app/webapi/routes/main_menu_buttons.py` — Python-модуль
  Классы: нет
  Функции: `list_main_menu_buttons`, `create_main_menu_button_endpoint`, `update_main_menu_button_endpoint`, `delete_main_menu_button_endpoint`
- `app/webapi/routes/media.py` — Python-модуль
  Классы: нет
  Функции: `upload_media`, `download_media`
- `app/webapi/routes/menu_layout.py` — Python-модуль
  Классы: нет
  Функции: `get_menu_layout` — Получить текущую конфигурацию меню., `update_menu_layout` — Обновить конфигурацию меню полностью., `reset_menu_layout` — Сбросить конфигурацию к дефолтной., `list_builtin_buttons` — Получить список встроенных кнопок., `update_button` — Обновить конфигурацию отдельной кнопки., `reorder_rows` — Изменить порядок строк., `add_row` — Добавить новую строку., `delete_row` — Удалить строку., `add_custom_button` — Добавить кастомную кнопку (URL, MiniApp или callback)., `delete_custom_button` — Удалить кастомную кнопку., `preview_menu` — Предпросмотр меню для указанного контекста пользователя., `move_button_up` — Переместить кнопку вверх (в предыдущую строку или на позицию выше в текущей строке)., `move_button_down` — Переместить кнопку вниз (в следующую строку или на позицию ниже в текущей строке)., `move_button_to_row` — Переместить кнопку в указанную строку., `reorder_buttons_in_row` — Изменить порядок кнопок в строке., `swap_buttons` — Обменять местами две кнопки (даже из разных строк)., `list_available_callbacks` — Получить список всех доступных callback_data для создания кнопок., `list_dynamic_placeholders` — Получить список доступных динамических плейсхолдеров для текста кнопок., `export_menu_layout` — Экспортировать конфигурацию меню., `import_menu_layout` — Импортировать конфигурацию меню., `validate_menu_layout` — Валидировать конфигурацию меню без сохранения., `get_menu_layout_history` — Получить историю изменений меню., `get_history_entry` — Получить конкретную запись истории с полной конфигурацией., `rollback_to_history` — Откатить конфигурацию к записи из истории., `get_menu_click_stats` — Получить общую статистику кликов по всем кнопкам., `get_button_click_stats` — Получить статистику кликов по конкретной кнопке., `log_button_click` — Записать клик по кнопке (для внешней интеграции)., `get_stats_by_button_type` — Получить статистику кликов по типам кнопок (builtin, callback, url, mini_app)., `get_clicks_by_hour` — Получить статистику кликов по часам дня (0-23)., `get_clicks_by_weekday` — Получить статистику кликов по дням недели., `get_top_users` — Получить топ пользователей по количеству кликов., `get_period_comparison` — Сравнить статистику текущего и предыдущего периода., `get_user_click_sequences` — Получить последовательности кликов пользователя.
- `app/webapi/routes/miniapp.py` — Python-модуль
  Классы: нет
  Функции: `get_maintenance_status`, `get_payment_methods`, `create_payment_link`, `get_payment_statuses`, `get_subscription_details`, `update_subscription_autopay_endpoint`, `activate_subscription_trial_endpoint`, `activate_promo_code`, `claim_promo_offer`, `remove_connected_device`, `get_subscription_renewal_options_endpoint`, `submit_subscription_renewal_endpoint`, `get_subscription_purchase_options_endpoint`, `subscription_purchase_preview_endpoint`, `subscription_purchase_endpoint`, `get_subscription_settings_endpoint`, `update_subscription_servers_endpoint`, `update_subscription_traffic_endpoint`, `update_subscription_devices_endpoint`, `get_tariffs_endpoint` — Возвращает список доступных тарифов для пользователя., `purchase_tariff_endpoint` — Покупка или смена тарифа., `preview_tariff_switch_endpoint` — Предпросмотр переключения тарифа - показывает стоимость., `switch_tariff_endpoint` — Переключение тарифа без изменения даты окончания., `purchase_traffic_topup_endpoint` — Докупка трафика для подписки., `toggle_daily_subscription_pause_endpoint` — Переключает паузу/активацию суточной подписки.
- `app/webapi/routes/pages.py` — Python-модуль
  Классы: нет
  Функции: `get_public_offer`, `update_public_offer`, `get_privacy_policy`, `update_privacy_policy`, `list_faq_pages`, `get_faq_status`, `update_faq_status`, `create_faq_page`, `get_faq_page`, `update_faq_page`, `delete_faq_page`, `reorder_faq_pages`, `get_service_rules`, `update_service_rules`, `clear_service_rules`, `get_service_rules_history`, `restore_service_rules_version`
- `app/webapi/routes/partners.py` — Python-модуль
  Классы: нет
  Функции: `list_referrers`, `get_referrer_detail`, `update_referrer_commission`, `get_global_partner_stats` — Глобальная статистика партнёрской программы., `get_global_daily_stats` — Глобальная статистика по дням., `get_top_referrers` — Топ рефереров по заработку., `get_referrer_detailed_stats` — Детальная статистика реферера., `get_referrer_daily_stats` — Статистика реферера по дням., `get_referrer_top_referrals` — Топ рефералов реферера по принесённому доходу., `get_referrer_period_comparison` — Сравнение периодов для реферера.
- `app/webapi/routes/pinned_messages.py` — Python-модуль
  Классы: нет
  Функции: `list_pinned_messages` — Получить список всех закреплённых сообщений., `get_active_message` — Получить текущее активное закреплённое сообщение., `get_pinned_message` — Получить закреплённое сообщение по ID., `create_pinned_message` — Создать новое закреплённое сообщение., `update_pinned_message` — Обновить закреплённое сообщение., `update_pinned_message_settings` — Обновить только настройки закреплённого сообщения., `activate_pinned_message` — Активировать закреплённое сообщение., `broadcast_message` — Разослать закреплённое сообщение всем активным пользователям., `deactivate_active_message` — Деактивировать текущее активное закреплённое сообщение., `unpin_active_message` — Открепить сообщение у всех пользователей и деактивировать., `delete_pinned_message` — Удалить закреплённое сообщение.
- `app/webapi/routes/polls.py` — Python-модуль
  Классы: нет
  Функции: `list_polls`, `get_poll`, `create_poll_endpoint`, `delete_poll`, `get_poll_stats`, `get_poll_responses`, `send_poll`
- `app/webapi/routes/promo_groups.py` — Python-модуль
  Классы: нет
  Функции: `list_promo_groups`, `get_promo_group`, `create_promo_group_endpoint`, `update_promo_group_endpoint`, `delete_promo_group_endpoint`
- `app/webapi/routes/promo_offers.py` — Python-модуль
  Классы: нет
  Функции: `list_promo_offers`, `create_promo_offer`, `broadcast_promo_offers`, `get_promo_offer_logs`, `list_promo_offer_templates_endpoint`, `get_promo_offer_template_endpoint`, `update_promo_offer_template_endpoint`, `get_promo_offer_endpoint`
- `app/webapi/routes/promocodes.py` — Python-модуль
  Классы: нет
  Функции: `list_promocodes`, `get_promocode`, `create_promocode_endpoint`, `update_promocode_endpoint`, `delete_promocode_endpoint`
- `app/webapi/routes/remnawave.py` — Python-модуль
  Классы: нет
  Функции: `get_remnawave_status`, `get_system_statistics`, `list_nodes`, `get_nodes_realtime_usage`, `get_node_details`, `get_node_statistics`, `get_node_usage_range`, `manage_node`, `restart_all_nodes`, `list_squads`, `get_squad_details`, `create_squad`, `update_squad`, `squad_actions`, `list_inbounds`, `get_user_traffic`, `preview_squad_migration`, `sync_from_panel`, `sync_to_panel`, `validate_and_fix_subscriptions`, `cleanup_orphaned_subscriptions`, `sync_subscription_statuses`, `get_sync_recommendations`, `migrate_squad`
- `app/webapi/routes/servers.py` — Python-модуль
  Классы: нет
  Функции: `list_servers`, `get_servers_statistics`, `create_server_endpoint`, `get_server_endpoint`, `update_server_endpoint`, `delete_server_endpoint`, `get_server_connected_users_endpoint`, `sync_servers_with_remnawave`, `sync_server_counts`
- `app/webapi/routes/settings.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/webapi/routes/stats.py` — Python-модуль
  Классы: нет
  Функции: `stats_overview`, `stats_full`
- `app/webapi/routes/subscription_events.py` — Python-модуль
  Классы: нет
  Функции: `receive_subscription_event`, `list_subscription_event_logs`
- `app/webapi/routes/subscriptions.py` — Python-модуль
  Классы: нет
  Функции: `list_subscriptions`, `get_subscription`, `create_subscription`, `extend_subscription_endpoint`, `add_subscription_traffic_endpoint`, `add_subscription_devices_endpoint`, `add_subscription_squad_endpoint`, `remove_subscription_squad_endpoint`, `delete_subscription` — Деактивировать подписку.
- `app/webapi/routes/tickets.py` — Python-модуль
  Классы: нет
  Функции: `list_tickets`, `get_ticket`, `update_ticket_status`, `update_ticket_priority`, `update_reply_block`, `clear_reply_block`, `reply_to_ticket`, `get_ticket_message_media`
- `app/webapi/routes/tokens.py` — Python-модуль
  Классы: нет
  Функции: `get_tokens`, `create_token`, `revoke_token`, `activate_token`, `delete_token_endpoint`
- `app/webapi/routes/transactions.py` — Python-модуль
  Классы: нет
  Функции: `list_transactions`
- `app/webapi/routes/user_messages.py` — Python-модуль
  Классы: нет
  Функции: `list_user_messages`, `create_user_message_endpoint`, `update_user_message_endpoint`, `toggle_user_message_endpoint`, `delete_user_message_endpoint`
- `app/webapi/routes/users.py` — Python-модуль
  Классы: нет
  Функции: `list_users`, `get_user`, `get_user_by_telegram_id_endpoint` — Get user by Telegram ID, `create_user_endpoint`, `update_user_endpoint`, `update_balance`, `deposit_balance` — Ручное пополнение баланса — как настоящий платёж, но инициированное поддержкой., `create_user_subscription` — Создать или заменить подписку для пользователя., `patch_user_subscription`, `delete_user_subscription` — Деактивировать подписку пользователя.
- `app/webapi/routes/webhooks.py` — Python-модуль
  Классы: нет
  Функции: `list_webhooks_endpoint` — Список webhooks., `get_webhook_stats` — Статистика по webhooks., `get_webhook` — Получить webhook по ID., `create_webhook_endpoint` — Создать новый webhook., `update_webhook_endpoint` — Обновить webhook., `delete_webhook_endpoint` — Удалить webhook., `list_webhook_deliveries` — Список доставок webhook.
- `app/webapi/routes/websocket.py` — Python-модуль
  Классы: нет
  Функции: `verify_websocket_token` — Проверить токен для WebSocket подключения., `websocket_endpoint` — WebSocket endpoint для real-time обновлений.
- `app/webapi/routes/welcome_texts.py` — Python-модуль
  Классы: нет
  Функции: `list_welcome_texts_endpoint`, `create_welcome_text_endpoint`, `get_welcome_text_endpoint`, `update_welcome_text_endpoint`, `delete_welcome_text_endpoint`

#### app/webapi/schemas

- `app/webapi/schemas/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/webapi/schemas/backups.py` — Python-модуль
  Классы: `BackupCreateResponse`, `BackupInfo`, `BackupListResponse`, `BackupStatusResponse`, `BackupTaskInfo`, `BackupTaskListResponse`, `BackupRestoreRequest`, `BackupRestoreResponse`, `BackupDeleteResponse`
  Функции: нет
- `app/webapi/schemas/ban_notifications.py` — Python-модуль
  Классы: `BanNotificationRequest`, `BanNotificationResponse`
  Функции: нет
- `app/webapi/schemas/broadcasts.py` — Python-модуль
  Классы: `BroadcastMedia`, `BroadcastCreateRequest` (2 методов), `BroadcastResponse`, `BroadcastListResponse`
  Функции: нет
- `app/webapi/schemas/campaigns.py` — Python-модуль
  Классы: `CampaignBase` (1 методов), `CampaignCreateRequest` (4 методов), `CampaignResponse`, `CampaignListResponse`, `CampaignUpdateRequest` (5 методов)
  Функции: нет
- `app/webapi/schemas/config.py` — Python-модуль
  Классы: `SettingCategorySummary`, `SettingCategoryRef`, `SettingChoice`, `SettingDefinition`, `SettingUpdateRequest`
  Функции: нет
- `app/webapi/schemas/contests.py` — Python-модуль
  Классы: `ContestTemplateResponse`, `ContestTemplateListResponse`, `ContestTemplateUpdateRequest`, `StartRoundRequest`, `ContestRoundResponse`, `ContestRoundListResponse`, `ContestAttemptUser`, `ContestAttemptResponse`, `ContestAttemptListResponse`, `ReferralContestResponse`, `ReferralContestListResponse`, `ReferralContestCreateRequest`, `ReferralContestUpdateRequest`, `ReferralContestLeaderboardItem`, `ReferralContestDetailResponse`, `ReferralContestEventUser`, `ReferralContestEventResponse`, `ReferralContestEventListResponse`, `ReferralContestParticipant`, `ReferralContestDetailedStatsResponse`
  Функции: нет
- `app/webapi/schemas/health.py` — Python-модуль
  Классы: `HealthFeatureFlags`, `HealthCheckResponse`
  Функции: нет
- `app/webapi/schemas/logs.py` — Python-модуль
  Классы: `MonitoringLogEntry`, `MonitoringLogListResponse`, `MonitoringLogTypeListResponse`, `SupportAuditLogEntry`, `SupportAuditLogListResponse`, `SupportAuditActionsResponse`, `SystemLogPreviewResponse`, `SystemLogFullResponse`
  Функции: нет
- `app/webapi/schemas/main_menu_buttons.py` — Python-модуль
  Классы: `MainMenuButtonResponse`, `MainMenuButtonCreateRequest`, `MainMenuButtonUpdateRequest` (2 методов), `MainMenuButtonListResponse`
  Функции: нет
- `app/webapi/schemas/media.py` — Python-модуль
  Классы: `MediaUploadResponse`
  Функции: нет
- `app/webapi/schemas/menu_layout.py` — Python-модуль
  Классы: `ButtonType`, `ButtonVisibility`, `ButtonOpenMode`, `ButtonConditions`, `MenuButtonConfig`, `MenuRowConfig`, `MenuLayoutConfig`, `MenuLayoutResponse`, `BuiltinButtonInfo`, `BuiltinButtonsListResponse`, `MenuLayoutUpdateRequest`, `ButtonUpdateRequest`, `RowsReorderRequest`, `AddRowRequest`, `AddCustomButtonRequest`, `MenuPreviewRequest`, `MenuPreviewButton`, `MenuPreviewRow`, `MenuPreviewResponse`, `MoveButtonToRowRequest`, `ReorderButtonsInRowRequest`, `SwapButtonsRequest`, `MoveButtonResponse`, `SwapButtonsResponse`, `ReorderButtonsResponse`, `AvailableCallback`, `AvailableCallbacksResponse`, `MenuLayoutExportResponse`, `MenuLayoutImportRequest`, `MenuLayoutImportResponse`, `MenuLayoutHistoryEntry`, `MenuLayoutHistoryResponse`, `MenuLayoutRollbackRequest`, `ValidationError`, `MenuLayoutValidateRequest`, `MenuLayoutValidateResponse`, `ButtonClickStats`, `ButtonClickStatsResponse`, `MenuClickStatsResponse`, `ButtonTypeStats`, `ButtonTypeStatsResponse`, `HourlyStats`, `HourlyStatsResponse`, `WeekdayStats`, `WeekdayStatsResponse`, `TopUserStats`, `TopUsersResponse`, `PeriodComparisonResponse`, `UserClickSequence`, `UserClickSequencesResponse`, `DynamicPlaceholder`, `DynamicPlaceholdersResponse`
  Функции: нет
- `app/webapi/schemas/miniapp.py` — Python-модуль
  Классы: `MiniAppBranding`, `MiniAppSubscriptionRequest`, `MiniAppMaintenanceStatusResponse`, `MiniAppSubscriptionUser`, `MiniAppPromoGroup`, `MiniAppAutoPromoGroupLevel`, `MiniAppConnectedServer`, `MiniAppDevice`, `MiniAppDeviceRemovalRequest`, `MiniAppDeviceRemovalResponse`, `MiniAppTransaction`, `MiniAppPromoOffer`, `MiniAppPromoOfferClaimRequest`, `MiniAppPromoOfferClaimResponse`, `MiniAppSubscriptionAutopay`, `MiniAppSubscriptionRenewalPeriod`, `MiniAppSubscriptionRenewalOptionsRequest`, `MiniAppSubscriptionRenewalOptionsResponse`, `MiniAppSubscriptionRenewalRequest`, `MiniAppSubscriptionRenewalResponse`, `MiniAppSubscriptionAutopayRequest`, `MiniAppSubscriptionAutopayResponse`, `MiniAppPromoCode`, `MiniAppPromoCodeActivationRequest`, `MiniAppEligibleSubscription`, `MiniAppPromoCodeActivationResponse`, `MiniAppFaqItem`, `MiniAppFaq`, `MiniAppRichTextDocument`, `MiniAppLegalDocuments`, `MiniAppReferralTerms`, `MiniAppReferralStats`, `MiniAppReferralRecentEarning`, `MiniAppReferralItem`, `MiniAppReferralList`, `MiniAppReferralInfo`, `MiniAppPaymentMethodsRequest`, `MiniAppPaymentIntegrationType`, `MiniAppPaymentOption`, `MiniAppPaymentIframeConfig` (1 методов), `MiniAppPaymentMethod` (1 методов), `MiniAppPaymentMethodsResponse`, `MiniAppPaymentCreateRequest`, `MiniAppPaymentCreateResponse`, `MiniAppPaymentStatusQuery`, `MiniAppPaymentStatusRequest`, `MiniAppPaymentStatusResult`, `MiniAppPaymentStatusResponse`, `MiniAppTariffPeriod`, `MiniAppTariff`, `MiniAppTrafficTopupPackage`, `MiniAppCurrentTariff`, `MiniAppTrafficTopupRequest`, `MiniAppTrafficTopupResponse`, `MiniAppTariffsRequest`, `MiniAppTariffsResponse`, `MiniAppTariffPurchaseRequest`, `MiniAppTariffPurchaseResponse`, `MiniAppTariffSwitchRequest`, `MiniAppTariffSwitchPreviewResponse`, `MiniAppTariffSwitchResponse`, `MiniAppDailySubscriptionToggleRequest`, `MiniAppDailySubscriptionToggleResponse`, `MiniAppTrafficPurchase`, `MiniAppSubscriptionResponse`, `MiniAppSubscriptionServerOption`, `MiniAppSubscriptionTrafficOption`, `MiniAppSubscriptionDeviceOption`, `MiniAppSubscriptionCurrentSettings`, `MiniAppSubscriptionServersSettings`, `MiniAppSubscriptionTrafficSettings`, `MiniAppSubscriptionDevicesSettings`, `MiniAppSubscriptionBillingContext`, `MiniAppSubscriptionSettings`, `MiniAppSubscriptionSettingsResponse`, `MiniAppSubscriptionSettingsRequest` (1 методов), `MiniAppSubscriptionServersUpdateRequest` (1 методов), `MiniAppSubscriptionTrafficUpdateRequest` (1 методов), `MiniAppSubscriptionDevicesUpdateRequest` (1 методов), `MiniAppSubscriptionUpdateResponse`, `MiniAppSubscriptionPurchaseOptionsRequest`, `MiniAppSubscriptionPurchaseOptionsResponse`, `MiniAppSubscriptionPurchasePreviewRequest` (1 методов), `MiniAppSubscriptionPurchasePreviewResponse`, `MiniAppSubscriptionPurchaseRequest`, `MiniAppSubscriptionPurchaseResponse`, `MiniAppSubscriptionTrialRequest`, `MiniAppSubscriptionTrialResponse`
  Функции: нет
- `app/webapi/schemas/pages.py` — Python-модуль
  Классы: `RichTextPageResponse`, `RichTextPageUpdateRequest`, `FaqPageResponse`, `FaqPageListResponse`, `FaqPageCreateRequest`, `FaqPageUpdateRequest`, `FaqReorderItem`, `FaqReorderRequest`, `FaqStatusResponse`, `FaqStatusUpdateRequest`, `ServiceRulesResponse`, `ServiceRulesUpdateRequest`, `ServiceRulesHistoryResponse`
  Функции: нет
- `app/webapi/schemas/partners.py` — Python-модуль
  Классы: `PartnerReferrerItem`, `PartnerReferrerListResponse`, `PartnerReferralItem`, `PartnerReferralList`, `PartnerReferrerDetail`, `PartnerReferralCommissionUpdate`, `EarningsByPeriod`, `ReferralsCountByPeriod`, `ReferrerSummary`, `ReferrerDetailedStats`, `DailyStats`, `DailyStatsResponse`, `TopReferralItem`, `TopReferralsResponse`, `PeriodData`, `ChangeData`, `PeriodChange`, `PeriodComparisonResponse`, `GlobalPartnerSummary`, `PayoutsByPeriod`, `RewardsByLevel`, `NewReferralsByPeriod`, `GlobalPartnerStats`, `TopReferrerItem`, `TopReferrersResponse`
  Функции: нет
- `app/webapi/schemas/pinned_messages.py` — Python-модуль
  Классы: `PinnedMessageMedia`, `PinnedMessageBase`, `PinnedMessageCreateRequest`, `PinnedMessageUpdateRequest`, `PinnedMessageSettingsRequest`, `PinnedMessageResponse`, `PinnedMessageBroadcastResponse`, `PinnedMessageUnpinResponse`, `PinnedMessageListResponse`
  Функции: нет
- `app/webapi/schemas/polls.py` — Python-модуль
  Классы: `PollOptionCreate` (1 методов), `PollQuestionCreate` (2 методов), `PollCreateRequest` (3 методов), `PollQuestionOptionResponse`, `PollQuestionResponse`, `PollSummaryResponse`, `PollDetailResponse`, `PollListResponse`, `PollOptionStats`, `PollQuestionStats`, `PollStatisticsResponse`, `PollAnswerResponse`, `PollUserResponse`, `PollResponsesListResponse`, `PollSendRequest`, `PollSendResponse`
  Функции: нет
- `app/webapi/schemas/promo_groups.py` — Python-модуль
  Классы: `PromoGroupResponse`, `PromoGroupCreateRequest`, `PromoGroupUpdateRequest`, `PromoGroupListResponse`
  Функции: нет
- `app/webapi/schemas/promo_offers.py` — Python-модуль
  Классы: `PromoOfferUserInfo`, `PromoOfferSubscriptionInfo`, `PromoOfferResponse`, `PromoOfferListResponse`, `PromoOfferCreateRequest`, `PromoOfferBroadcastRequest` (1 методов), `PromoOfferBroadcastResponse`, `PromoOfferTemplateResponse`, `PromoOfferTemplateListResponse`, `PromoOfferTemplateUpdateRequest`, `PromoOfferLogOfferInfo`, `PromoOfferLogResponse`, `PromoOfferLogListResponse`
  Функции: нет
- `app/webapi/schemas/promocodes.py` — Python-модуль
  Классы: `PromoCodeResponse`, `PromoCodeListResponse`, `PromoCodeCreateRequest`, `PromoCodeUpdateRequest`, `PromoCodeRecentUse`, `PromoCodeDetailResponse`
  Функции: нет
- `app/webapi/schemas/remnawave.py` — Python-модуль
  Классы: `RemnaWaveConnectionStatus`, `RemnaWaveStatusResponse`, `RemnaWaveNode`, `RemnaWaveNodeListResponse`, `RemnaWaveNodeActionRequest`, `RemnaWaveNodeActionResponse`, `RemnaWaveNodeUsageItem`, `RemnaWaveNodeStatisticsResponse`, `RemnaWaveNodeUsageResponse`, `RemnaWaveBandwidth`, `RemnaWaveTrafficPeriod`, `RemnaWaveTrafficPeriods`, `RemnaWaveSystemSummary`, `RemnaWaveServerInfo`, `RemnaWaveSystemStatsResponse`, `RemnaWaveSquad`, `RemnaWaveSquadListResponse`, `RemnaWaveSquadCreateRequest`, `RemnaWaveSquadUpdateRequest`, `RemnaWaveSquadActionRequest`, `RemnaWaveOperationResponse`, `RemnaWaveInboundsResponse`, `RemnaWaveUserTrafficResponse`, `RemnaWaveSyncFromPanelRequest`, `RemnaWaveGenericSyncResponse`, `RemnaWaveSquadMigrationPreviewResponse`, `RemnaWaveSquadMigrationRequest`, `RemnaWaveSquadMigrationStats`, `RemnaWaveSquadMigrationResponse`
  Функции: нет
- `app/webapi/schemas/servers.py` — Python-модуль
  Классы: `ServerResponse`, `ServerListResponse`, `ServerCreateRequest`, `ServerUpdateRequest`, `ServerSyncResponse`, `ServerStatisticsResponse`, `ServerCountsSyncResponse`, `ServerConnectedUser`, `ServerConnectedUsersResponse`, `ServerDeleteResponse`
  Функции: нет
- `app/webapi/schemas/subscription_events.py` — Python-модуль
  Классы: `SubscriptionEventCreate` (1 методов), `SubscriptionEventResponse`, `SubscriptionEventListResponse`
  Функции: нет
- `app/webapi/schemas/subscriptions.py` — Python-модуль
  Классы: `SubscriptionResponse`, `SubscriptionCreateRequest`, `SubscriptionExtendRequest`, `SubscriptionTrafficRequest`, `SubscriptionDevicesRequest`, `SubscriptionSquadRequest`
  Функции: нет
- `app/webapi/schemas/tickets.py` — Python-модуль
  Классы: `TicketMediaItemResponse`, `TicketMessageResponse`, `TicketResponse`, `TicketStatusUpdateRequest`, `TicketPriorityUpdateRequest`, `TicketReplyBlockRequest`, `TicketReplyRequest`, `TicketReplyResponse`, `TicketMediaResponse`
  Функции: нет
- `app/webapi/schemas/tokens.py` — Python-модуль
  Классы: `TokenResponse`, `TokenCreateRequest`, `TokenCreateResponse`
  Функции: нет
- `app/webapi/schemas/transactions.py` — Python-модуль
  Классы: `TransactionResponse`, `TransactionListResponse`
  Функции: нет
- `app/webapi/schemas/user_messages.py` — Python-модуль
  Классы: `UserMessageResponse`, `UserMessageCreateRequest`, `UserMessageUpdateRequest` (1 методов), `UserMessageListResponse`
  Функции: нет
- `app/webapi/schemas/users.py` — Python-модуль
  Классы: `PromoGroupSummary`, `SubscriptionSummary`, `UserResponse`, `UserListResponse`, `UserCreateRequest`, `UserUpdateRequest`, `BalanceUpdateRequest`, `BalanceDepositRequest`, `BalanceDepositResponse`, `UserSubscriptionCreateRequest`
  Функции: нет
- `app/webapi/schemas/webhooks.py` — Python-модуль
  Классы: `WebhookCreateRequest`, `WebhookUpdateRequest`, `WebhookResponse`, `WebhookListResponse`, `WebhookDeliveryResponse`, `WebhookDeliveryListResponse`, `WebhookStatsResponse`
  Функции: нет
- `app/webapi/schemas/welcome_texts.py` — Python-модуль
  Классы: `WelcomeTextResponse`, `WelcomeTextCreateRequest`, `WelcomeTextUpdateRequest` (1 методов), `WelcomeTextListResponse`
  Функции: нет

### app/webserver

- `app/webserver/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `app/webserver/apple_iap.py` — Python-модуль
  Классы: нет
  Функции: `create_apple_iap_router`
- `app/webserver/payments.py` — Python-модуль
  Классы: нет
  Функции: `drain_webhook_bg_tasks` — Дождаться фоновых обработчиков вебхуков перед остановкой процесса., `create_payment_router` — Роутер вебхуков платёжных провайдеров.
- `app/webserver/remnawave_webhook.py` — Python-модуль
  Классы: нет
  Функции: `create_remnawave_webhook_router` — Build the FastAPI router for RemnaWave webhooks.
- `app/webserver/telegram.py` — Python-модуль
  Классы: `TelegramWebhookProcessorError`, `TelegramWebhookProcessorNotRunningError`, `TelegramWebhookOverloadedError`, `TelegramWebhookProcessor` (7 методов)
  Функции: `create_telegram_router`
- `app/webserver/unified_app.py` — Python-модуль
  Классы: нет
  Функции: `create_unified_app`

## assets

- `assets/bedolaga_app3.svg` — файл
- `assets/logo2.svg` — файл

## docs

- `docs/apple-iap-consumable-topups.md` — файл
- `docs/apple-iap-ios-requirements.md` — файл
- `docs/contests-api.md` — файл
- `docs/grace-access.md` — файл
- `docs/handoffs/`
- `docs/menu_stats_api_usage.md` — файл
- `docs/miniapp-setup.md` — файл
- `docs/mobile-support-websocket-v1.md` — файл
- `docs/persistent_cart_system.md` — файл
- `docs/project_structure_reference.md` — файл
- `docs/referral_program_setting.md` — файл
- `docs/web-admin-integration-guide.md` — файл
- `docs/web-admin-integration.md` — файл
- `docs/websocket-and-webhooks.md` — файл

### docs/handoffs

- `docs/handoffs/handoff-2026-08-31-1659.md` — файл
- `docs/handoffs/handoff-2026-08-31-1944.md` — файл

## migrations

- `migrations/alembic/`

### migrations/alembic

- `migrations/alembic/env.py` — Python-модуль
  Классы: нет
  Функции: `run_migrations_offline`, `do_run_migrations`, `run_async_migrations`, `run_migrations_online`
- `migrations/alembic/script.py.mako` — файл
- `migrations/alembic/versions/`

#### migrations/alembic/versions

- `migrations/alembic/versions/0001_initial_schema.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0002_add_campaign_id_to_referral_earnings.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0003_add_partner_system.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0004_add_email_templates.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0005_repair_missing_columns.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0006_add_broadcast_email_columns.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0007_fix_naive_timestamps.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0008_add_required_channels.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0009_add_channel_id_index.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0010_add_channel_leave_settings.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0011_add_rbac_tables.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0012_add_missing_subscription_columns.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0013_add_desired_commission_percent.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0014_fix_referral_transaction_types.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0015_add_promocode_uses_unique_constraint.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0016_add_ondelete_cascade_to_user_fks.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0017_add_unique_constraint_transaction_external_id.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0018_add_landing_pages_and_guest_purchases.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0019_yookassa_payment_user_id_nullable.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0020_add_guest_purchases_indexes.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0021_landing_localized_texts.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0022_payment_tables_user_id_nullable.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0023_users_promo_group_id_nullable.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0024_guest_purchase_cabinet_password.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0025_guest_purchase_auto_login_token.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0026_tariff_external_squad_uuid.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0027_landing_discount.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0028_landing_discount_constraints.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0029_guest_purchases_composite_stats_index.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0030_add_background_config_to_landing_pages.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0031_drop_legacy_prize_days_column.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0032_guest_purchase_source_and_buyer.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0033_guest_purchase_gift_index.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0034_guest_purchase_recipient_warning.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0035_guest_purchase_token_pattern_ops_index.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0036_add_riopay_payments.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0037_add_saved_payment_methods.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0038_add_show_in_gift_to_tariffs.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0039_riopay_user_id_nullable.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0040_add_severpay_payments.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0041_add_performance_indexes.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0042_add_retry_count_and_payment_recovery_indexes.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0043_add_rbac_and_email_indexes.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0044_fix_null_payment_method_manual_topups.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0045_add_receipt_uuid_to_guest_purchases.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0046_add_news_articles.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0047_add_cascade_to_subscription_servers.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0048_add_lower_username_index.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0049_add_news_categories_and_tags.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0050_multi_subscription_foundation.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0051_add_subscription_remnawave_short_id.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0052_add_promocode_tariff_id.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0053_include_limited_in_unique_active_index.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0054_add_broadcast_category.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0055_add_pending_campaign_slug.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0056_create_cabinet_refresh_tokens.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0057_alter_notification_settings_to_jsonb.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0058_create_paypear_payments.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0059_create_rollypay_payments.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0060_create_aurapay_payments.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0061_add_ticket_media_items.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0062_add_landing_analytics_and_sticky.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0063_add_yandex_client_id_map.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0064_create_overpay_payments.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0065_create_info_pages.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0066_add_page_type_to_info_pages.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0067_add_replaces_tab_to_info_pages.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0068_add_apple_transactions.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0069_create_etoplatezhi_payments.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0070_create_antilopay_payments.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0071_add_last_revoke_at_to_subscriptions.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0072_create_jupiter_payments.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0073_create_donut_payments.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0074_create_lava_payments.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0075_rebuild_apple_iap_ledgers.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0076_unique_apple_web_order_line_item_id.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0077_ensure_uq_campaign_user.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0078_add_revocation_source_to_user_roles.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0079_add_email_verification_source.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0080_add_traffic_purchases_sub_expires_index.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0081_drop_redundant_traffic_purchases_subscription_id_index.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0082_add_open_url_direct_to_payment_method_config.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0083_add_user_device_aliases.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0084_drop_stale_stars_rate_default.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0085_referral_pending_unique.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0086_users_referred_by_paid_index.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0087_add_autopay_period_days_to_subscriptions.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0088_dedupe_tariff_subscriptions.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0089_wheel_spins_telegram_charge_id.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0090_add_quick_amounts_to_payment_method_configs.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0091_add_info_pages_display_mode.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0092_backfill_vk_yandex_email_verified.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0093_yclid.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0094_payment_method_description.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0095_add_coupons.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0096_add_recurrent_payments.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0097_add_grace_access.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0098_create_cispay_payments.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0099_add_platega_subscriptions.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0100_platega_sub_unique_alive.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0101_add_lava_subscriptions.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0102_coupon_max_per_user.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0103_add_legal_consents.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0104_remnawave_numeric_id.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0105_promocode_traffic_gb.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0106_guest_purchase_campaign.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0107_guest_purchase_idempotency.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0108_referral_reward_levels.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0109_referral_level_thresholds.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0110_referral_user_reward_choice.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0111_create_system_error_events.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0112_create_email_queue.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0113_create_tabpay_payments.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0114_create_paritypay_payments.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0115_create_reachability_tables.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`
- `migrations/alembic/versions/0116_reachability_batches.py` — Python-модуль
  Классы: нет
  Функции: `upgrade`, `downgrade`

## scripts

- `scripts/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `scripts/backfill_remnawave_ids.py` — Python-модуль
  Классы: нет
  Функции: `main`
- `scripts/generate_structure_reference.py` — Python-модуль
  Классы: нет
  Функции: `tracked_paths` — Файлы проекта: отслеживаемые плюс новые, которые git не игнорирует., `describe_module` — Строки «Классы:» и «Функции:» для модуля., `render_entries`, `render`, `build`, `main`

## tests

- `tests/_manual_grace_core_runner.py` — Python-модуль
  Классы: нет
  Функции: нет
- `tests/cabinet/`
- `tests/ci/`
- `tests/conftest.py` — Python-модуль
  Классы: нет
  Функции: `fixed_datetime` — Возвращает фиксированную отметку времени для воспроизводимых проверок., `registered_paths` — Карта `путь -> {HTTP-методы}` кабинетного роутера, как их реально, `pytest_pyfunc_call` — Позволяет запускать async def тесты без дополнительных плагинов.
- `tests/contracts/`
- `tests/crud/`
- `tests/database/`
- `tests/external/`
- `tests/fixtures/`
- `tests/handlers/`
- `tests/integration/`
- `tests/live/`
- `tests/middlewares/`
- `tests/services/`
- `tests/test_config_languages.py` — Python-модуль
  Классы: нет
  Функции: `test_available_languages_default_contains_fa`, `test_available_languages_normalizes_and_deduplicates`
- `tests/test_coupon_deeplink_pins.py` — Python-модуль
  Классы: нет
  Функции: `test_cmd_start_parses_coupon_deep_link`, `test_coupon_redeem_called_at_every_gift_activation_site`, `test_coupon_branch_only_swallows_existing_coupons` — `start_parameter = None` must sit INSIDE the is_coupon_token() guard AND, `test_redeem_flips_status_before_remnawave_sync`, `test_redeem_claims_under_row_lock_and_rechecks`, `test_redeem_aborts_when_remnawave_sync_returns_none`, `test_revoke_confirm_registered_before_revoke_prefix`, `test_create_confirm_is_guarded_against_stale_buttons_and_double_taps`, `test_admin_states_for_coupon_creation_exist`
- `tests/test_declared_dependencies.py` — Python-модуль
  Классы: нет
  Функции: `test_every_imported_package_is_declared` — Прямой импорт — прямая зависимость., `test_dependency_names_are_unique` — Один пакет не должен быть объявлен дважды с разными ограничениями., `test_previously_transitive_imports_stay_declared` — Именно эти пять держались на транзитивности — закрепляем результат.
- `tests/test_device_limit_display.py` — Python-модуль
  Классы: нет
  Функции: `test_absent_limit_renders_as_infinity`, `test_real_limit_renders_as_number`, `test_traffic_and_device_limits_agree_on_unlimited` — Трафик уже показывал безлимит бесконечностью — устройства не должны отставать., `test_zero_used_devices_is_not_confused_with_zero_limit` — Форматтер — только для ЛИМИТА: счётчик использованных нулём и остаётся.
- `tests/test_device_limit_resolution.py` — Python-модуль
  Классы: `DummySubscription` (1 методов), `StubSettings` (4 методов)
  Функции: `test_resolve_hwid_device_limit_disabled_mode`, `test_resolve_hwid_device_limit_enabled_mode`, `test_resolve_hwid_device_limit_enabled_ignores_non_positive`, `test_resolve_hwid_device_limit_for_payload_returns_subscription_limit`, `test_resolve_hwid_device_limit_for_payload_ignores_non_positive`, `test_resolve_hwid_device_limit_for_payload_prefers_forced_limit`, `test_resolve_hwid_device_limit_for_payload_handles_zero`, `test_resolve_simple_subscription_device_limit`, `test_coerce_panel_device_limit_preserves_zero_and_rejects_invalid`, `test_coerce_panel_device_limit_honors_default`, `test_device_limit_needs_heal_preserves_zero`
- `tests/test_expired_sub_tariff_button.py` — Python-модуль
  Классы: нет
  Функции: `test_expired_sub_offers_buy_not_switch`, `test_disabled_sub_offers_buy_not_switch`, `test_active_sub_keeps_change_tariff`, `test_limited_sub_keeps_change_tariff` — 'limited' = traffic exhausted but time remaining (end_date>now) — switch still valid.
- `tests/test_free_tariff_instant_switch.py` — Python-модуль
  Классы: нет
  Функции: `test_keyboard_free_tariff_routes_to_period_switch`, `test_keyboard_free_tariff_keeps_instant_when_reset_disabled` — TARIFF_SWITCH_RESET_FREE_DAYS=false — админ явно разрешил перенос бесплатных, `test_keyboard_paid_tariff_keeps_instant_switch`, `test_instant_list_redirects_free_source`, `test_instant_preview_redirects_free_source` — Устаревшая кнопка instant_sw_preview в старом сообщении не должна, `test_instant_confirm_refuses_free_source_and_never_charges` — Ядро фикса: подтверждение instant-switch с бесплатного тарифа не должно, `test_instant_list_keeps_prorated_flow_for_paid_source` — Платный источник: prorated instant-switch работает как раньше.
- `tests/test_grace_env_example_stays_commented.py` — Python-модуль
  Классы: нет
  Функции: `test_no_grace_key_is_active_in_the_example`, `test_commented_values_match_the_code_defaults`
- `tests/test_lazy_package_exports.py` — Python-модуль
  Классы: нет
  Функции: `test_package_declares_exports`, `test_every_exported_name_resolves`, `test_unknown_name_still_raises` — __getattr__ не должен выдавать что попало вместо AttributeError.
- `tests/test_locale_integrity.py` — Python-модуль
  Классы: нет
  Функции: `locales`, `test_all_locales_have_identical_keys`, `test_placeholders_consistent_across_locales` — Every {placeholder} must be identical across languages — the code calls, `test_t_calls_without_default_exist_in_ru` — texts.t('KEY') with NO fallback raises KeyError if the key is absent from ru., `test_t_calls_with_static_default_exist_in_ru` — texts.t('KEY', 'статический дефолт') с ключом вне ru.json отдаёт русский, `test_invite_only_keys_exist_in_every_locale`
- `tests/test_logo_path_validation.py` — Python-модуль
  Классы: нет
  Функции: `test_valid_file_passes_validation`, `test_directory_fails_validation` — The exact failure mode from #586617 — bind-mount created a dir., `test_missing_path_fails_validation` — File doesn't exist at all — sending would raise FileNotFoundError., `test_validator_logs_actionable_message_for_directory` — The warning must mention the path so the operator can fix it without
- `tests/test_logo_resize.py` — Python-модуль
  Классы: нет
  Функции: `test_small_logo_used_as_is`, `test_oversized_logo_is_resized_under_cap` — A 2000×2000 PNG (~well over _LOGO_MAX_DIMENSION) gets resized., `test_oversized_non_square_logo_keeps_aspect` — 1980×1267 (the literal `vpn_logo.png` shipped in the repo) keeps ratio., `test_cached_resized_copy_is_reused` — Subsequent calls on the same source must hit the cached resized file., `test_missing_pil_falls_back_to_original` — If Pillow chokes on the file for any reason we return the source path —, `test_size_thresholds_are_sane` — Guard against accidental edits that would render the resize a noop., `test_get_logo_media_uses_resized_copy` — End-to-end: get_logo_media() returns FSInputFile pointing at the resized copy.
- `tests/test_menu_subscription_status.py` — Python-модуль
  Классы: `DummyTexts` (1 методов)
  Функции: `test_get_subscription_status_marks_trial_as_trial`
- `tests/test_miniapp_payments.py` — Python-модуль
  Классы: нет
  Функции: `anyio_backend`, `test_compute_cryptobot_limits_scale_with_rate`, `test_encode_decode_renewal_payload_preserves_snapshot`, `test_submit_subscription_renewal_uses_balance_when_sufficient`, `test_submit_subscription_renewal_returns_cryptobot_invoice`, `test_submit_subscription_renewal_rounds_up_cryptobot_amount`, `test_cryptobot_renewal_uses_pricing_snapshot`, `test_cryptobot_renewal_accepts_changed_pricing_without_snapshot`, `test_cryptobot_webhook_uses_inline_payload_when_db_missing`, `test_create_payment_link_pal24_uses_selected_option`, `test_create_payment_link_wata_returns_payload`, `test_resolve_yookassa_status_includes_identifiers`, `test_resolve_payment_status_supports_yookassa_sbp`, `test_resolve_pal24_status_includes_identifiers`, `test_resolve_wata_payment_status_success`, `test_resolve_wata_payment_status_uses_payment_link_lookup`, `test_create_payment_link_stars_normalizes_amount`, `test_get_payment_methods_exposes_stars_min_amount`, `test_get_payment_methods_includes_wata`, `test_get_payment_methods_marks_mulenpay_iframe`, `test_find_recent_deposit_ignores_transactions_before_attempt`, `test_find_recent_deposit_accepts_recent_transactions`
- `tests/test_no_undefined_names.py` — Python-модуль
  Классы: нет
  Функции: `test_no_new_undefined_names`, `test_baseline_does_not_rot` — Исправленное имя обязано выпадать из базы, иначе она копит ложь.
- `tests/test_pricing_engine.py` — Python-модуль
  Классы: `TestApplyDiscount` (6 методов), `TestStackedDiscounts` (5 методов), `TestPeriodDaysValidation` (3 методов), `TestCalculateServersPrice` (8 методов), `TestCalculateTrafficPrice` (5 методов), `TestCalculateRenewalPriceTariffMode` (7 методов), `TestCalculateRenewalPriceClassicMode` (10 методов), `TestServerPromoGroupFiltering` (2 методов), `TestFromPayloadRoundTrip` (1 методов), `TestFromPayloadLegacyRoundTrip` (1 методов), `TestOriginalPriceIdentity` (3 методов)
  Функции: `test_renewal_pricing_is_frozen`
- `tests/test_promo_group_base_discounts.py` — Python-модуль
  Классы: нет
  Функции: `base_discount_settings`, `test_base_promo_discount_applies_to_all_categories`, `test_specific_category_discount_overrides_base`
- `tests/test_readme_payment_providers.py` — Python-модуль
  Классы: нет
  Функции: `test_every_gateway_is_listed_in_readme`, `test_table_has_no_rows_for_unknown_providers` — Каждая строка таблицы указывает на существующий шлюз., `test_claimed_provider_count_matches_reality` — Число провайдеров в тексте не должно отставать от кода.
- `tests/test_rich_menu_pins.py` — Python-модуль
  Классы: нет
  Функции: `test_show_main_menu_tries_rich_before_classic`, `test_back_to_menu_tries_rich_before_classic`, `test_start_menu_sites_guarded_by_rich_helpers`, `test_single_subscription_block_reuses_menu_status_builder`, `test_trial_deeplink_wired_in_start` — Диплинк /start trial: ветка сташит pending_trial, drain — рядом с купонным
- `tests/test_sla_defaults_match_env_example.py` — Python-модуль
  Классы: нет
  Функции: `test_code_default_matches_env_example`, `test_sla_is_off_by_default` — Явно: без .env напоминания молчат.
- `tests/test_start_menu_text_consistency.py` — Python-модуль
  Классы: нет
  Функции: `test_start_main_menu_text_delegates_to_menu_builder`, `test_start_no_longer_has_duplicate_status_formatter` — The duplicate formatter that caused the /start-vs-menu divergence is gone.
- `tests/test_structure_reference_is_current.py` — Python-модуль
  Классы: нет
  Функции: `test_document_matches_the_code`, `test_only_tracked_files_are_listed` — Документ не должен зависеть от мусора в рабочей копии., `test_generator_is_deterministic` — Два запуска подряд дают один и тот же текст., `test_payment_mixins_are_documented` — Ровно та дыра, из-за которой всё это затевалось.
- `tests/test_subscription_cart_integration.py` — Python-модуль
  Классы: нет
  Функции: `mock_callback_query`, `mock_user`, `mock_db`, `mock_state`, `test_save_cart_and_redirect_to_topup` — Тест сохранения корзины и перенаправления к пополнению, `test_return_to_saved_cart_success` — Тест возврата к сохраненной корзине с достаточным балансом, `test_return_to_saved_cart_skips_edit_when_message_matches`, `test_return_to_saved_cart_normalizes_devices_when_disabled`, `test_return_to_saved_cart_insufficient_funds` — Тест возврата к сохраненной корзине с недостаточным балансом, `test_clear_saved_cart` — Тест очистки сохраненной корзины, `test_handle_subscription_cancel_clears_saved_cart` — Отмена покупки должна очищать сохраненную корзину, `test_handle_subscription_cancel_clears_only_current_subscription_cart` — Отмена покупки в мультитарифном сценарии чистит только корзину текущей подписки
- `tests/test_support_contact_telegram.py` — Python-модуль
  Классы: нет
  Функции: `test_telegram_contacts_detected`, `test_external_contacts_not_telegram`, `test_empty_contact_is_not_telegram`
- `tests/test_sync_bot_username.py` — Python-модуль
  Классы: нет
  Функции: `test_sync_overrides_stale_username`, `test_sync_keeps_config_on_get_me_failure`, `test_sync_noop_when_already_correct`
- `tests/test_tariff_insufficient_balance_keyboard.py` — Python-модуль
  Классы: нет
  Функции: `test_classic_topup_when_autopurchase_disabled`, `test_inlines_prefilled_payment_when_autopurchase_enabled`, `test_classic_topup_when_missing_zero`, `test_sbp_purchase_offered_on_insufficient_balance` — СБП-оформлению баланс не нужен — кнопка обязана быть и на экране нехватки средств., `test_no_sbp_purchase_when_recurrent_disabled`, `test_daily_insufficient_balance_offers_sbp_purchase`, `test_falls_back_to_topup_without_direct_payment_methods`, `test_extend_inlines_prefilled_payment_when_autopurchase_enabled`, `test_extend_classic_topup_when_autopurchase_disabled`, `test_extend_prefilled_amount_is_missing_not_full_price` — Частичная нехватка: доплата = missing (260), не цена (630) и не баланс (370)., `test_extend_back_points_at_the_subscription_in_multi_tariff` — «Назад» обязан адресовать конкретную подписку, иначе он никуда не ведёт., `test_extend_back_stays_generic_without_multi_tariff`, `test_extend_falls_back_to_topup_without_direct_payment_methods` — Фильтр строк оплаты — единственное, что делает зеркалирование безопасным.
- `tests/test_ticket_notification_keyboard.py` — Python-модуль
  Классы: нет
  Функции: `test_admin_gets_full_set_including_user_manage`, `test_moderator_omits_user_manage_but_keeps_actions`, `test_user_manage_uses_db_id_not_telegram_id`, `test_user_manage_hidden_when_no_db_id_even_for_admin`, `test_url_buttons_present_for_username_and_telegram_id`, `test_username_with_at_prefix_is_stripped`, `test_no_username_hides_dm_keeps_profile`, `test_email_user_without_telegram_id_has_no_url_buttons_but_keeps_callbacks`, `test_non_numeric_telegram_id_dropped_from_profile_url`, `test_blocked_user_shows_unblock_not_block_controls`, `test_closed_ticket_hides_reply_and_close`, `test_notification_keyboard_never_has_back_button`, `test_group_keyboard_omits_fsm_buttons_keeps_reliable`, `test_group_keyboard_omits_user_manage`, `test_group_keyboard_blocked_shows_unblock`, `test_group_keyboard_closed_ticket_leaves_only_block_forever`, `service_factory` — Build an AdminNotificationService with patched permission helpers., `test_role_admin`, `test_role_moderator`, `test_role_admin_takes_precedence_over_moderator`, `test_role_outsider_private_chat_is_none`, `test_role_group_or_channel_is_group`, `test_role_zero_chat_id_is_none`, `test_role_none_chat_id_is_none`, `test_role_string_username_chat_is_none`, `test_role_numeric_string_chat_id_is_resolved`, `cabinet_settings` — Configure settings for the cabinet deep-link button., `test_cabinet_button_none_when_not_cabinet_mode`, `test_cabinet_button_private_is_webapp_to_admin_ticket_path`, `test_cabinet_button_private_none_without_custom_url`, `test_cabinet_button_group_is_startapp_deeplink`, `test_cabinet_button_group_none_without_short_name`, `test_cabinet_button_group_none_without_bot_username`, `test_keyboard_places_cabinet_button_on_top`
- `tests/test_topup_amounts_keyboard.py` — Python-модуль
  Классы: нет
  Функции: `test_resolve_maps_callback_methods_to_config_ids`, `test_resolve_keeps_direct_config_ids`, `test_resolve_maps_overpay_variants_to_overpay`, `test_format_quick_amount`, `test_keyboard_builds_amount_buttons_within_limits`, `test_keyboard_chunks_amounts_two_per_row`, `test_keyboard_min_amount_override_raises_lower_bound`, `test_keyboard_falls_back_to_back_only_on_db_error`
- `tests/test_trial_activation_paid.py` — Python-модуль
  Классы: нет
  Функции: `trial_callback_query`, `trial_user`, `trial_db`, `test_activate_trial_paid_shows_payment_screen_with_trial_price`, `test_activate_free_trial_insufficient_funds_redirects_to_topup`
- `tests/test_trial_disabled_menu_gating.py` — Python-модуль
  Классы: нет
  Функции: `test_keyboard_hides_trial_when_duration_zero`, `test_keyboard_hides_trial_when_disabled_for_all`, `test_menu_layout_hides_trial_when_disabled`, `test_show_trial_offer_blocks_when_duration_zero`, `test_activate_trial_blocks_when_duration_zero`
- `tests/test_user_cart_service.py` — Python-модуль
  Классы: `MockRedis` (5 методов)
  Функции: `mock_redis`, `user_cart_service`, `test_save_user_cart` — Тест сохранения корзины пользователя, `test_get_user_cart` — Тест получения корзины пользователя, `test_get_user_cart_not_found` — Тест получения несуществующей корзины пользователя, `test_delete_user_cart` — Тест удаления корзины пользователя, `test_delete_user_cart_not_found` — Тест удаления несуществующей корзины пользователя, `test_has_user_cart` — Тест проверки наличия корзины пользователя, `test_has_user_cart_not_found` — Тест проверки отсутствия корзины пользователя, `test_save_cart_with_return_to_cart_sets_intent` — return_to_cart=True ставит метку намерения пополнить ради корзины., `test_save_cart_without_return_to_cart_no_intent` — Обычное сохранение корзины (без return_to_cart) метку НЕ ставит., `test_has_topup_intent_is_non_destructive` — Проверка наличия метки не гасит её — частичное пополнение может до-сработать., `test_clear_topup_intent` — clear_topup_intent гасит метку (вызывается после успешной авто-покупки)., `test_delete_user_cart_clears_intent` — Очистка корзины снимает и метку намерения, чтобы она не «висела»., `test_has_topup_intent_false_when_redis_down` — Redis недоступен → намерение считается отсутствующим (не списываем молча).
- `tests/test_wheel_fixes.py` — Python-модуль
  Классы: нет
  Функции: `test_spin_rechecks_daily_limit_under_lock` — Even if check_availability passed, spin() must re-count under the lock and, `test_spin_under_limit_proceeds_to_payment` — Sanity: when the re-check is below the limit, spin() proceeds to payment., `test_stars_wheel_spin_idempotent_on_redelivery` — A successful_payment redelivered with the same charge id must NOT grant a
- `tests/utils/`
- `tests/webapi/`
- `tests/webserver/`

### tests/cabinet

- `tests/cabinet/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `tests/cabinet/test_admin_delete_user_subscription.py` — Python-модуль
  Классы: нет
  Функции: `test_route_registered` — Метод и путь закреплены: иначе маршрут можно переименовать с зелёным CI., `test_force_defaults_to_off` — Без явного force активную платную подписку снести нельзя., `test_deletes_expired_trial` — Базовый случай из отчёта: отработавший триал убирается из карточки., `test_foreign_subscription_not_found` — Подписка чужого пользователя не удаляется по одному лишь sub_id., `test_active_paid_needs_force` — Оплаченный активный доступ не сносится одним промахом., `test_open_grace_blocks_deletion` — Пока открыт временный доступ, подписку из-под него не вырывают.
- `tests/cabinet/test_admin_grace_access.py` — Python-модуль
  Классы: `TestEnabling` (6 методов), `TestRejectedInput` (3 методов), `TestPartialUpdate` (3 методов), `TestEnvLock` (3 методов), `TestOverview` (9 методов), `TestSquadPicker` (4 методов)
  Функции: `test_routes_registered`, `test_each_url_reaches_its_own_handler`, `test_sessions_endpoint_also_requires_users_read` — Список отдаёт чужие telegram_id, @логины и имена., `test_configuration_endpoints_stay_on_settings_permissions`, `config` — Живые настройки grace с валидной конфигурацией; правки не утекают в другие тесты., `saved` — Перехват записи настроек: значение сразу видно и в ``settings``, как в проде., `empty_db`, `status_snapshot` — Счётчики сессий подменяются: раздел читает их из общего сборщика.
- `tests/cabinet/test_admin_grace_access_http.py` — Python-модуль
  Классы: нет
  Функции: `test_overview_serializes`, `test_sessions_serialize_dates_and_owner`, `test_put_writes_and_returns_fresh_overview`, `test_put_refuses_enabling_without_a_squad`, `test_out_of_range_value_is_a_validation_error` — 422 отдаёт detail списком — фронт обязан уметь его прочитать., `test_explicit_null_is_refused` — Пустое значение уезжало в system_settings как NULL и переживало перезапуск., `test_unknown_mode_is_refused`
- `tests/cabinet/test_admin_grace_access_sessions.py` — Python-модуль
  Классы: `TestStatusSnapshot` (4 методов), `TestSessionsList` (4 методов)
  Функции: нет
- `tests/cabinet/test_admin_legal_pages_routes.py` — Python-модуль
  Классы: нет
  Функции: `test_admin_legal_pages_routes_registered`, `test_legal_responses_expose_env_lock_flag`, `test_set_display_mode_commits`, `test_set_display_mode_env_locked_conflict`, `test_set_display_mode_env_locked_same_value_allowed`, `test_require_language_rejects_unknown`
- `tests/cabinet/test_admin_overpay_certificate_routes.py` — Python-модуль
  Классы: нет
  Функции: `p12_bytes`, `stubbed_service`, `test_admin_overpay_certificate_routes_registered`, `test_upload_certificate_commits`, `test_upload_certificate_env_locked_warning`, `test_upload_certificate_invalid_returns_422`, `test_upload_certificate_oversize_returns_413`, `test_delete_certificate_commits`
- `tests/cabinet/test_admin_reachability.py` — Python-модуль
  Классы: нет
  Функции: `service`, `test_routes_are_registered`, `test_routes_require_expected_permission`, `test_target_in_validation`, `test_status_maps_service_dict`, `test_preview_errors_are_translated`, `test_busy_is_409_with_job_reference`, `test_cancel_not_cancellable_is_409_and_not_found_is_404`, `test_create_job_logs_audit_and_hides_raw_links`, `test_cancel_logs_audit`, `test_list_jobs_passes_filters_and_paginates`, `test_units_splits_csv_filters`, `test_subscription_configs_hide_credentials`, `test_preview_response_omits_request_body`, `test_update_pref_calls_service_with_admin`, `test_summary_maps_rows_and_units`, `test_status_exposes_default_sni`, `test_job_request_accepts_up_to_five_sni_hosts_and_normalizes_them`, `test_target_in_accepts_subscription_config_by_url`, `test_parse_input_route_maps_configs_and_hides_raw_links`, `test_job_out_exposes_probes_and_sni_hosts_from_request`
- `tests/cabinet/test_admin_reachability_batches.py` — Python-модуль
  Классы: нет
  Функции: `service`, `test_batch_routes_registered`, `test_batch_routes_require_expected_permission`, `test_batch_request_validation`, `test_preview_batch_returns_totals`, `test_create_batch_returns_jobs_with_partial_and_audits`, `test_get_batch_counts_done_targets`, `test_list_batches_paginates`, `test_cancel_and_missing_batch_map_domain_errors`, `test_job_out_carries_batch_id`
- `tests/cabinet/test_admin_referral_levels_routes.py` — Python-модуль
  Классы: `TestValidation` (3 методов), `TestPartialUpdate` (2 методов), `TestSchemeSwitch` (3 методов), `TestDeletion` (1 методов), `TestTermsEndpointUnderLevels` (3 методов), `TestLegacyImportEndpoint` (2 методов), `TestDepthEndpoint` (3 методов), `TestLevelsModeSetting` (3 методов), `TestLevelsModeEndpoint` (5 методов), `TestLevelsPayloadReportsTheMode` (2 методов), `TestBotAndCabinetAgree` (3 методов)
  Функции: `test_referral_level_routes_registered`, `test_each_url_reaches_its_own_handler` — Наличия пути в списке МАЛО — важно, какой обработчик его получит., `wired`
- `tests/cabinet/test_admin_remnawave_geocheck.py` — Python-модуль
  Классы: нет
  Функции: `patched_service` — Подменяет фабрику сервиса в роутере на заранее собранный дубль., `test_geocheck_routes_are_registered`, `test_geocheck_routes_require_expected_permission`, `test_geocheck_request_rejects_ip_and_interface_together`, `test_geocheck_request_rejects_malformed_ip`, `test_geocheck_request_accepts_ipv4_and_ipv6`, `test_geocheck_request_rejects_malformed_interface`, `test_geocheck_request_accepts_interface_names`, `test_geocheck_request_blank_strings_mean_default_route`, `test_start_geocheck_returns_job_id`, `test_start_geocheck_forwards_selected_route`, `test_start_geocheck_on_old_panel_names_required_version` — 404 = у панели нет такого эндпоинта; админ должен узнать почему, а не «ошибка»., `test_start_geocheck_propagates_panel_error_message`, `test_get_geocheck_maps_running_job`, `test_get_geocheck_maps_completed_result_to_snake_case`, `test_get_geocheck_maps_failed_job`, `test_get_geocheck_unknown_job_is_404`
- `tests/cabinet/test_admin_send_user_message.py` — Python-модуль
  Классы: нет
  Функции: `test_send_message_success`, `test_send_message_email_only_user_rejected` — Email-only юзер → 400 с кодом no_telegram_id, бот не создаётся., `test_send_message_forbidden_maps_to_400_and_closes_session` — Юзер заблокировал бота → 400 с кодом forbidden, сессия бота закрыта., `test_send_message_user_not_found`, `test_send_message_permission_registered` — users:send_message должен существовать в реестре RBAC — иначе
- `tests/cabinet/test_admin_traffic_period_days.py` — Python-модуль
  Классы: нет
  Функции: `test_dates_are_read_only_where_they_are_assigned` — Чтение start_dt/end_dt не должно жить вне ветки, которая их задаёт., `test_period_days_is_set_in_every_branch` — Обе ветки разбора дат обязаны задать period_days.
- `tests/cabinet/test_admin_user_activity.py` — Python-модуль
  Классы: нет
  Функции: `test_activity_route_registered`, `test_activity_sources_shape`, `test_activity_dedup_filters_in_sql` — Транзакции, покрытые событиями/начислениями, исключаются на уровне SQL;, `test_activity_unknown_user_404`, `test_activity_unknown_type_400`, `test_activity_merges_and_sorts_desc` — Записи из разных источников сливаются и сортируются по времени убыванию., `test_activity_types_filter_limits_sources`, `test_button_click_sources_split_by_type` — Клики бота и действия кабинета — раздельные источники одной таблицы.
- `tests/cabinet/test_admin_user_detail_subscription_ownership.py` — Python-модуль
  Классы: нет
  Функции: `owned_subscription`, `foreign_subscription`, `ownership_boundary` — Make the authoritative lookup return only the subscription owned by OWNER_ID., `panel_service`, `test_authoritative_subscription_lookup_constrains_id_and_user_id` — Would fail if a route reverted to an id-only subscription lookup., `test_subscription_reads_and_device_delete_accept_owned_subscription` — Every BP-S route uses the selected owned subscription in either mode., `test_subscription_reads_and_device_delete_reject_foreign_and_absent_without_panel_access` — No rejected BP-S request may construct or call the panel client in either mode., `test_panel_info_validates_supplied_subscription_before_unconfigured_service_access` — Would fail if panel-info checks service configuration before ownership., `test_panel_info_does_not_fall_back_to_user_panel_id_for_selected_unlinked_subscription` — Would fail if a selected null-link subscription leaked legacy panel information., `test_subscription_actions_accept_an_owned_subscription` — An ownership guard must not reject the requested user's own subscription., `test_selected_actions_pin_sync_to_the_selected_identity_when_legacy_mode_is_enabled` — A supplied subscription id must opt out of all single-tariff fallbacks., `test_selected_sync_uses_only_selected_panel_id_when_legacy_mode_is_enabled` — The helper itself must not substitute the user's legacy panel user id., `test_selected_sync_with_no_panel_link_does_not_substitute_legacy_identity` — A null selected link must cause no panel access, even in legacy mode., `test_selected_devices_return_selected_subscription_device_limit`, `test_selected_reset_returns_unsuccessful_when_panel_deactivation_fails`, `test_selected_reset_without_link_does_not_substitute_legacy_identity`, `test_selected_reset_cancels_both_recurring_bindings` — Живая привязка автопродления воскресила бы только что сброшенную подписку., `test_subscription_actions_reject_foreign_or_absent_ids_before_mutation` — Would fail if action selection trusted an eagerly loaded subscription list.
- `tests/cabinet/test_admin_user_remnawave_resolver.py` — Python-модуль
  Классы: нет
  Функции: `test_resolver_route_is_registered_before_user_id_route`, `test_resolver_requires_users_read_permission`, `test_resolver_returns_the_exact_matching_subscription` — Would fail if the resolver returned a user-level or primary subscription ID., `test_resolver_accepts_a_short_uuid_for_subscriptions_without_a_panel_id` — Would fail if the resolver only understood numeric panel ids., `test_resolver_rejects_unusable_identifiers_without_any_lookup` — Would fail if garbage input were guessed from user data or hit the database., `test_resolver_rejects_an_identifier_present_only_on_the_legacy_user_field` — Would fail if the route reused legacy user-level resolution., `test_resolver_treats_a_physically_absent_deleted_subscription_as_not_found` — Would fail if absent/deleted records were accidentally resolved., `test_resolver_rejects_duplicate_subscription_mappings_as_a_conflict` — Would fail if corrupted mappings silently selected one subscription.
- `tests/cabinet/test_autopay_cancels_sbp.py` — Python-модуль
  Классы: нет
  Функции: `test_enable_autopay_cancels_active_sbp_recurring`, `test_disable_autopay_does_not_cancel_sbp` — Disabling balance-autopay must NOT touch SBP — only the enable path, `test_enable_autopay_rejected_for_trial_does_not_cancel_sbp` — A rejected enable (trial subscription -> 400) must not fire the
- `tests/cabinet/test_branding_favicon.py` — Python-модуль
  Классы: нет
  Функции: `test_without_logo_returns_png_monogram_of_the_first_letter`, `test_empty_name_falls_back_to_v`, `test_unset_name_uses_build_default`, `test_render_failure_falls_back_to_svg`, `test_monogram_png_is_a_square_raster_with_the_letter_drawn`, `test_corner_ratio_stays_below_the_safari_plate_threshold`, `test_with_logo_serves_a_rounded_tile_with_short_cache`, `test_rounded_tile_is_cached_until_the_logo_file_changes`, `test_svg_logo_is_served_as_is`, `test_unreadable_logo_falls_back_to_the_raw_file`, `test_logo_endpoint_keeps_its_hour_cache`, `test_monogram_escapes_and_uppercases`
- `tests/cabinet/test_broadcast_media_validation.py` — Python-модуль
  Классы: нет
  Функции: `test_send_rejects_media_caption_over_1024`, `test_send_checks_the_caption_that_is_actually_sent` — Если у медиа своя подпись, ограничение относится к ней, а не к message_text., `test_media_file_id_must_look_like_a_telegram_file_id`
- `tests/cabinet/test_bulk_actions_admin_identity.py` — Python-модуль
  Классы: нет
  Функции: `test_subscription_batch_survives_rollback_inside_loop` — Откат на первой подписке не должен уносить весь ответ в 500., `test_user_batch_survives_rollback_inside_loop` — То же для режима по пользователям: id админа читается на каждой итерации., `test_streamed_batch_survives_rollback_inside_loop` — SSE-поток не должен обрываться на финальном логе после отката., `test_delete_user_permission_check_runs_before_snapshot` — Снимок id не должен обгонять проверку прав: отказ обязан остаться отказом., `test_stream_helpers_take_plain_admin_id` — Генераторы принимают int, а не ORM-объект: протухать в них нечему.
- `tests/cabinet/test_bulk_change_tariff_preserves_period.py` — Python-модуль
  Классы: нет
  Функции: `db`, `test_change_tariff_preserves_remaining_period` — A 5-days-left subscription keeps its 5 days — tariff swap must not refill to 30., `test_change_tariff_does_not_extend_almost_expired_sub` — An almost-expired sub stays almost-expired after a tariff change., `test_change_tariff_keeps_trial_a_trial` — Bug #629889: changing a TRIAL's tariff must NOT convert it to paid.
- `tests/cabinet/test_bulk_delete_subscription_lazy_user.py` — Python-модуль
  Классы: нет
  Функции: `test_known_subscriptions_falls_back_to_target` — Коллекция недоступна → берём целевую подписку, а не падаем., `test_known_subscriptions_uses_loaded_collection` — Коллекция загружена → отдаём её целиком, запасная не нужна., `test_known_subscriptions_keeps_loaded_empty_list_empty` — Загруженный пустой список — это «подписок нет», а не пробел в данных., `test_active_paid_skip_reports_target_without_collection` — Ветка «активная платная» тоже читает подписки — и тоже не должна падать., `test_execute_for_user_survives_unloaded_collection` — Досборка подписок в _execute_for_user не должна ронять действие., `test_delete_subscription_survives_unloaded_collection` — Удаление истёкшего триала доходит до конца, а не падает на подписках.
- `tests/cabinet/test_coupon_routes.py` — Python-модуль
  Классы: нет
  Функции: `test_coupon_routes_registered`, `test_coupons_permissions_registered`, `test_create_batch_returns_links_and_tokens`, `test_create_batch_rejects_inactive_tariff`, `test_create_batch_rejects_blank_name`, `test_get_batch_404_when_missing`, `test_links_export_counts_active_only`, `test_revoke_returns_count_and_updated_card`, `test_redeem_success_for_telegram_user_sends_no_email`, `test_redeem_notifies_email_only_user`, `test_redeem_maps_service_errors_to_structured_contract`, `test_public_status_returns_offer_for_active_coupon`, `test_public_status_is_uniform_404_for_consumed_coupon`, `test_public_status_rate_limited`, `test_public_status_rejects_malformed_token_without_db_hit`
- `tests/cabinet/test_deleted_user_revival.py` — Python-модуль
  Классы: нет
  Функции: `db`, `test_dependencies_auto_revives_deleted_user_with_valid_init_data` — REGRESSION: signed initData proving same telegram_id → revive in place., `test_dependencies_rejects_deleted_user_without_init_data` — Without a fresh signature, return structured 403 — never auto-revive., `test_dependencies_rejects_deleted_user_with_mismatched_init_data` — initData proving DIFFERENT telegram_id → cross-account 401, NOT revival., `test_dependencies_blocks_revival_for_blacklisted_deleted_user` — A DELETED + blacklisted row must NOT be revived. Banned stays banned., `test_dependencies_blacklist_runs_before_status_check_for_no_init_data` — REGRESSION: blacklisted+DELETED without initData must still return, `test_dependencies_preserves_blocked_status_with_generic_message` — Status=BLOCKED is an admin action, not inactivity — generic 403., `test_dependencies_active_user_still_passes_through` — Negative-control: ACTIVE user is unaffected by all the new branches., `test_dependencies_auto_revive_persists_via_db_commit` — Pin the caller-owns-commit contract at the dependency boundary., `test_dependencies_rejects_deleted_user_with_invalid_init_data` — initData header present but signature INVALID → falls back to no-proof path., `test_dependencies_deleted_email_only_user_without_telegram_id` — Email-only DELETED user (telegram_id=None) → 403 account_deleted, no AttributeError.
- `tests/cabinet/test_device_addon_prorate.py` — Python-модуль
  Классы: нет
  Функции: `test_device_addon_prorates_to_full_remaining_period` — Device add-on scales with the actual remaining days — no one-month cap., `test_no_upper_cap_long_subscription_costs_more_than_one_month` — A year-long subscription must charge ~12× the monthly rate, not a flat month., `test_short_remainder_is_prorated_down` — 5 days left → pay for ~5 days, not a full month., `test_one_ruble_floor_for_paid_devices` — Tiny prorated amounts floor to 1₽ (100 kopeks)., `test_free_devices_cost_nothing` — Zero chargeable monthly price → free, no floor applied., `test_multi_device_scales_linearly` — N devices = N × per-device prorated price., `test_bot_and_cabinet_formulas_agree` — The bot (calculate_prorated_price) and the cabinet inline math must match.
- `tests/cabinet/test_device_ownership.py` — Python-модуль
  Классы: нет
  Функции: `test_collect_panel_user_ids_deduplicates_and_preserves_order` — user.remnawave_id first, then unique subscription ids in declared order., `test_collect_panel_user_ids_handles_classic_mode_user_only` — Classic mode: only user.remnawave_id, no subscriptions array., `test_collect_panel_user_ids_handles_multi_tariff_no_top_id` — Multi-tariff: top-level user.remnawave_id often None, sub ids only., `test_collect_panel_user_ids_returns_empty_when_no_panel_attached`, `test_collect_panel_user_ids_ignores_legacy_uuid_column` — Только remnawave_id: юзер с одними легаси-UUID панели не привязан., `test_verify_finds_hwid_on_first_panel`, `test_verify_finds_hwid_on_non_primary_subscription_panel` — REGRESSION: multi-tariff user with device on sub-B's panel user must pass., `test_verify_returns_false_when_hwid_on_no_panel`, `test_verify_short_circuits_after_first_hit` — We stop iterating panels as soon as we find the device — fewer remote calls., `test_verify_degrades_open_on_remnawave_failure` — Degrade-open contract: panel unreachable → True so renames don't break., `test_verify_does_not_degrade_open_on_unusable_panel_id` — Битая ссылка в НАШЕЙ БД — не сбой панели: такой id пропускается, проверка закрыта., `test_verify_skips_unusable_id_but_still_checks_remaining_panels` — Один непригодный id не должен обрывать обход остальных панелей юзера., `test_verify_returns_false_when_user_has_no_panel_id` — No panel id on user or any subscription → False (nothing to validate against).
- `tests/cabinet/test_email_change_otp_security.py` — Python-модуль
  Классы: нет
  Функции: `test_verify_blocked_and_code_not_checked_when_ip_rate_limited`, `test_verify_per_account_cap_burns_pending_change`, `test_request_change_rejects_unowned_admin_email`, `test_verify_and_apply_rejects_wrong_code_and_applies_correct`
- `tests/cabinet/test_email_from_header.py` — Python-модуль
  Классы: нет
  Функции: `test_non_ascii_from_name_keeps_single_valid_address`, `test_ascii_from_name_unaffected`
- `tests/cabinet/test_email_layout.py` — Python-модуль
  Классы: нет
  Функции: `test_default_layout_is_the_previous_wrapper` — Без сохранённой обёртки письма выглядят ровно как раньше., `test_default_layout_contains_every_placeholder_the_editor_offers`, `test_custom_layout_applies_to_default_templates`, `test_custom_layout_falls_back_to_ru_for_other_languages`, `test_custom_layout_applies_to_editor_fragments_but_not_full_documents`, `test_layout_without_content_slot_is_ignored` — Обёртка без {content} проглотила бы тело каждого письма — её не берём., `test_render_keeps_content_raw_and_escapes_text_values`, `test_layout_can_use_bare_unsubscribe_url_for_its_own_link_text` — Свой текст отписки вместо готового блока: {unsubscribe_url} доступен в обёртке., `test_layout_sees_recipient_vars_from_default_and_override_paths` — {username}/{email} доступны в обёртке — рендер письма отдаёт ей получателя., `test_override_fragment_keeps_unsubscribe_link` — Маркетинговый override третьего уровня раньше терял ссылку отписки в подвале., `test_cache_is_stale_until_loaded_and_fresh_after`, `test_rendered_override_refreshes_layout_cache_first` — Каждая отправка проходит через get_rendered_override — там и подтягивается обёртка., `test_refresh_survives_database_errors` — Недоступная база — это «обёртки нет», а не потерянное письмо., `test_editor_lists_layout_first_with_placeholders_intact`, `test_layout_preview_puts_sample_letter_inside_custom_layout`, `test_saving_layout_without_content_slot_is_rejected`, `test_saving_layout_refreshes_cache_and_uses_fixed_subject`
- `tests/cabinet/test_email_login_deleted_oracle.py` — Python-модуль
  Классы: нет
  Функции: `test_email_login_does_not_return_account_deleted_code` — REGRESSION: /email/login must not expose `code: account_deleted`., `test_email_login_returns_generic_401_for_deleted_users` — The DELETED branch in /email/login must raise 401 with 'Invalid email or password'., `test_email_login_status_check_runs_before_email_verification_gate` — REGRESSION: status check must come before the email-verification gate.
- `tests/cabinet/test_email_merge_otp_flow.py` — Python-модуль
  Классы: нет
  Функции: `test_email_conflict_sends_code_not_token` — Knowing the victim's email mails a code to THEM — no merge token is issued., `test_verify_wrong_code_rejected`, `test_verify_correct_code_issues_token`, `test_execute_rejects_non_initiator` — A leaked token can't be executed by anyone but the authenticated initiator.
- `tests/cabinet/test_email_plaintext_conversion.py` — Python-модуль
  Классы: нет
  Функции: `test_style_block_content_is_stripped`, `test_script_block_content_is_stripped`, `test_style_block_stripped_case_insensitive_and_multiline`, `test_entities_unescaped_amp_last`, `test_blank_line_runs_are_collapsed`, `test_real_default_template_produces_clean_plain_text` — Регрессия на живом шаблоне: дефолтное письмо верификации собирается на, `test_unclosed_style_block_does_not_leak_css` — Битый шаблон (частый случай для кастомных писем из админки): открытый, `test_send_email_plain_part_is_clean` — End-to-end: send_email должен положить в text/plain часть очищенный текст,
- `tests/cabinet/test_email_rendering_integrity.py` — Python-модуль
  Классы: нет
  Функции: `test_default_template_renders_clean_for_every_language`, `captured_send` — Перехватывает send_email и притворяется, что SMTP настроен., `test_verification_email_uses_unified_template`, `test_password_reset_email_uses_unified_template`, `test_email_change_code_uses_unified_template`, `test_custom_override_bypasses_default_rendering`, `test_wrap_full_document_is_not_double_wrapped`, `test_wrap_fragment_gets_base_template_once`, `test_wrap_styled_fragment_gets_minimal_wrapper`, `test_editor_default_roundtrips_through_override_render` — Сохранение дефолта из редактора как override не ломает письмо.
- `tests/cabinet/test_email_reply_to.py` — Python-модуль
  Классы: нет
  Функции: `smtp_ready`, `test_reply_to_is_set_when_configured` — Настроенный адрес попадает в Reply-To, From остаётся прежним., `test_no_reply_to_header_by_default` — Пустая настройка — поведение как раньше, лишнего заголовка нет., `test_broken_reply_to_is_dropped` — Мусор из .env не должен ни ломать письмо, ни дописывать чужой заголовок., `test_reply_to_is_trimmed` — Пробелы вокруг адреса обязаны срезаться до сборки заголовка.
- `tests/cabinet/test_email_send_failures.py` — Python-модуль
  Классы: `TestUnreachableServer` (4 методов), `TestServerRefusal` (3 методов), `TestUnexpectedErrors` (1 методов)
  Функции: `service`
- `tests/cabinet/test_email_template_editor.py` — Python-модуль
  Классы: нет
  Функции: `test_editor_exposes_every_email_template_type` — Жалоба из «Багов»: письма уходят, а поменять их шаблон нельзя., `test_every_template_type_has_sample_context_for_all_vars`, `test_default_template_renders_for_editor` — Каждый тип из списка редактора должен иметь рабочий дефолтный шаблон., `test_editor_payload_keeps_placeholders_not_sample_values` — Редактор получает {placeholder}-токены, а не подставленные примеры., `test_verification_template_placeholder_survives_roundtrip` — Сценарий бага: сохранить дефолтный шаблон как override и отправить письмо., `test_substitute_escapes_html_in_body`, `test_substitute_subject_strips_newlines_without_escaping`, `test_substitute_none_value_becomes_empty`, `test_override_without_required_var_falls_back_to_default` — Сломанный override (без {verification_url}) отбрасывается → дефолт., `test_override_with_required_var_is_used`, `test_required_var_with_empty_value_does_not_reject_override` — Пустое значение переменной не должно отбрасывать override., `test_preview_substitutes_sample_values_into_custom_body`, `test_preview_substitutes_common_vars_into_custom_body` — Общие переменные ({cabinet_url}, {service_name}) работают в любом шаблоне., `test_override_render_injects_common_vars` — Боевой рендер override подставляет общие переменные без участия вызывающего кода., `test_common_vars_exposed_to_editor` — Редактор получает список общих переменных для всех типов., `test_recipient_common_vars_never_leak_as_literals` — Даже если отправитель не передал username/email — литерал {username} не уходит в письмо., `test_preview_default_template_uses_sample_values`, `test_saving_template_without_required_placeholder_is_rejected_with_clear_error` — Раньше такой override молча подменялся стандартным письмом при отправке —, `test_saving_template_with_required_placeholder_passes`, `test_legacy_placeholder_names_resolve_in_sample_context` — 7 марта редактор переименовал переменные; шаблоны на старых именах должны жить., `test_override_with_legacy_placeholder_renders_value` — Шаблон пользователя с {new_end_date} — в письме дата, а не литерал., `test_saving_template_with_unknown_placeholder_is_rejected`, `test_saving_template_with_legacy_placeholder_is_allowed`
- `tests/cabinet/test_email_type_switch.py` — Python-модуль
  Классы: нет
  Функции: `test_disabled_setting_switches_types_off`, `test_always_on_types_ignore_the_setting`, `test_set_enabled_writes_setting_through_configuration_service`, `test_endpoint_refuses_to_disable_always_on_types`, `test_promo_email_is_skipped_when_disabled`, `test_receipt_email_is_skipped_when_disabled`, `test_switch_module_is_free_of_side_effects_on_import`, `test_delivery_service_skips_disabled_type` — Основной путь уведомлений: отключённый тип не уходит, включённый — уходит.
- `tests/cabinet/test_email_unsubscribe.py` — Python-модуль
  Классы: нет
  Функции: `smtp_ready` — Делает email_service «настроенным» и подменяет SMTP-соединение., `test_token_roundtrip` — Свежий токен опознаётся: отдаёт user_id и категорию., `test_token_email_is_case_insensitive` — Регистр адреса не должен ломать ссылку из письма., `test_token_rejects_tampering` — Подменённый user_id не проходит проверку подписи., `test_token_dies_with_old_email` — Смена адреса обесценивает старые ссылки — токен привязан к email., `test_parse_token_survives_garbage` — Мусор в query-параметре не должен ронять публичный эндпоинт., `test_build_url_empty_when_disabled` — Выключенная отписка не должна протаскивать битую ссылку в письмо., `test_build_url_uses_cabinet_url_by_default` — Без явного EMAIL_UNSUBSCRIBE_BASE_URL берём публичный путь кабинета., `test_send_email_adds_one_click_headers` — List-Unsubscribe + One-Click — то, из чего Gmail рисует свою кнопку., `test_transactional_email_has_no_unsubscribe_headers` — Письмо со сбросом пароля не должно предлагать отписку., `test_unsubscribe_url_is_header_injection_safe` — Перенос строки в URL не должен дописать чужой заголовок., `test_base_template_renders_footer_link` — Ссылка в футере — для клиентов, которые не рисуют кнопку из заголовка., `test_base_template_without_url_has_no_dangling_footer` — У транзакционных писем футер остаётся прежним., `test_common_context_exposes_unsubscribe_placeholder` — {unsubscribe_url} обязан резолвиться, иначе он утечёт в письмо литералом., `test_email_broadcast_filters_by_category` — Тумблеры кабинета обязаны резать email-рассылку так же, как Telegram., `test_get_does_not_unsubscribe` — GET по ссылке не должен ничего менять., `test_post_applies_and_answers_html_to_browser` — POST из формы: отписка применяется, браузеру отдаём страницу результата., `test_post_answers_bare_200_to_mail_client` — One-click от Gmail: пустой 200, без HTML., `test_parse_token_rejects_ids_outside_int4` — Публичный эндпоинт не должен падать на подобранном id., `test_mailto_with_crlf_is_dropped` — mailto уходит в тот же заголовок — перенос строки дописал бы свой., `test_marketing_email_blocked_after_unsubscribe` — Отписавшийся не должен получать промо и winback — ради этого всё и делалось., `test_marketing_email_carries_unsubscribe_url` — У маркетингового письма ссылка отписки обязана быть., `test_transactional_email_has_no_unsubscribe` — Транзакционному письму ссылка отписки не положена., `test_email_broadcast_path_applies_category_filter` — Проверяем не хелпер, а то, что путь рассылки его ЗОВЁТ., `test_signature_is_compared_in_constant_time` — Сравнение подписи — только hmac.compare_digest.
- `tests/cabinet/test_gift_routes.py` — Python-модуль
  Классы: нет
  Функции: `bypass_rate_limit` — Disable rate limiting for route tests., `test_branding_gift_enabled_routes` — get_gift_enabled and update_gift_enabled in branding read/write the shared setting., `test_gift_config_when_disabled` — When gift feature is disabled, /gift/config returns is_enabled=False and user balance., `test_gift_config_filters_tariffs_and_orders` — Only active tariffs with show_in_gift=True are returned, ordered by display_order then id., `test_gift_config_personalized_quote_fields` — Personalized discounts (promo group & active promo offer) populate quote fields., `test_purchase_gift_balance_success` — Balance checkout creates a paid GuestPurchase with cabinet idempotency and debits balance., `test_purchase_gift_balance_directed_and_notification` — Directed gift persists recipient details and invokes claim notification., `test_purchase_gift_balance_insufficient_balance` — When balance is insufficient, raises 400 Insufficient balance., `test_purchase_gift_restricted_user` — Restricted buyer receives 403 Forbidden., `test_purchase_gift_disabled_feature` — When gift feature is disabled, purchase raises 400 Gift feature is not enabled., `test_purchase_gift_tariff_not_found_or_inactive` — Inactive or non-gift tariffs raise 404 Tariff not found or inactive., `test_purchase_gift_invalid_period` — Requesting unconfigured period raises 400 Price is not configured for this period., `test_purchase_gift_self_gift_prevention` — Self-gifting by username or email raises 400 Cannot gift to yourself., `test_purchase_gift_gateway_success` — Gateway mode creates a payment via PaymentService and does not debit user balance., `test_purchase_gift_gateway_provider_error` — When payment provider returns None, raises 502 Bad Gateway., `test_purchase_gift_gateway_invalid_response` — When payment provider returns no payment_url, raises 502 Bad Gateway., `test_purchase_gift_telegram_unresolvable_warning` — When a recipient telegram username is not in DB and unresolvable via Bot API, warning is returned., `test_purchase_gift_gateway_consumes_one_time_promo_offer` — P1 Gateway Discount: personal one-time promo discount is applied and consumed on successful payment creation., `test_purchase_gift_gateway_provider_error_rolls_back_promo_offer` — P1 Gateway Discount: provider failure or exception cleanly rolls back promo offer consumption., `test_purchase_gift_gateway_internal_adapter_commit_is_deferred_until_url_validation` — An adapter commit must not persist promo consumption before the route validates its result., `test_purchase_gift_gateway_concurrent_requests_apply_promo_at_most_once` — P1 Gateway Concurrency: when 2 gateway requests run with 1 personal discount,, `test_purchase_gift_balance_returns_canonical_fields` — Balance gift purchase returns additive canonical gift_code, bot_claim_url, cabinet_claim_url., `test_purchase_gift_gateway_pending_has_no_claim_fields` — Gateway gift purchase returns null claim fields while in pending state., `test_get_gift_purchase_status_pending_has_no_claim_fields` — Pending purchase status returns is_claimable=False and no claim credentials., `test_get_gift_purchase_status_paid_code_only` — Paid code-only gift status returns canonical code and links with legacy 12-char token., `test_get_gift_purchase_status_directed_gift` — Directed gift status populates recipient value and claim artifacts for the buyer., `test_get_gift_purchase_status_delivered_has_no_claim_actions` — Delivered gift retains metadata but exposes no reusable claim actions/links., `test_get_gift_purchase_status_uniform_404_for_non_buyer` — Querying another buyer's purchase token raises 404 to avoid token existence oracle., `test_get_gift_purchase_status_absent_bot_or_cabinet_config` — When bot username or cabinet URL is not configured, canonical code is still returned and missing URLs are None., `test_get_sent_gifts_contract_and_channel_parity` — get_sent_gifts returns canonical claim fields for claimable gifts and omits them for delivered gifts., `test_get_sent_gifts_includes_bot_origin_gifts` — Gifts purchased via Telegram bot appear in cabinet /gift/sent with canonical claim artifacts., `test_landing_purchase_status_canonical_fields_parity` — _build_purchase_status_response in landing routes includes additive canonical fields., `test_historical_gifts_derive_canonical_codes_without_migration` — Historical gift rows in DB seamlessly derive canonical GIFT_<59> public codes without migration., `test_activate_gift_backward_compatibility_short_codes_and_canonical` — Cabinet /gift/activate endpoint accepts 8-char, 12-char, GIFT- prefix, canonical GIFT_, and full URLs., `test_get_gift_purchase_status_empty_token_prefix_returns_404` — Passing empty or prefix-only token like 'GIFT_' returns 404 even if user owns purchases.
- `tests/cabinet/test_happ_crypto_button_urls.py` — Python-модуль
  Классы: `TestCabinetResolveButtonUrl` (4 методов), `TestBotResolveButtonUrl` (2 методов), `TestCreateDeepLink` (4 методов), `TestBotCreateDeepLink` (2 методов)
  Функции: нет
- `tests/cabinet/test_incy_crypt_button_urls.py` — Python-модуль
  Классы: `TestCabinetButtons` (3 методов), `TestBotButtons` (3 методов)
  Функции: `decrypted_url`, `incy_enabled`
- `tests/cabinet/test_info_display_mode_gating.py` — Python-модуль
  Классы: нет
  Функции: `test_visibility_defaults_all_true`, `test_visibility_hides_bot_only_sections`, `test_rules_endpoint_404_when_bot_only`, `test_privacy_endpoint_404_when_bot_only`, `test_offer_endpoint_404_when_bot_only`, `test_recurrent_endpoint_404_when_bot_only`, `test_faq_list_empty_when_bot_only`
- `tests/cabinet/test_info_service.py` — Python-модуль
  Классы: нет
  Функции: `branded`, `test_contact_settings_exist` — Поля должны быть в модели, иначе .env их не задаст, а ручка снова врёт., `test_name_and_description_come_from_branding` — Источник тот же, что у мини-аппа — сервис не называется в двух местах по-разному., `test_unknown_and_dirty_language_codes_resolve` — Локали без брендинга берут дефолт, а хвост региона/пробелы не мешают., `test_contacts_are_returned`, `test_blank_contacts_are_null_not_empty_string` — Пустая переменная в .env — это «контакта нет», как и до правки., `test_name_is_never_the_old_hardcoded_stub` — Даже с пустым брендингом имя берётся из фолбэка брендинга, а не из ручки.
- `tests/cabinet/test_invite_only_auth.py` — Python-модуль
  Классы: нет
  Функции: `test_http_mapping_distinguishes_policy_and_infrastructure_errors`, `test_public_adapter_short_circuits_when_invite_only_is_disabled`, `test_cabinet_gate_forwards_verified_admin_identity`, `test_every_signed_telegram_arm_revives_its_own_deleted_account` — initData, widget and OIDC all prove the same identity, so all three must revive., `test_non_deleted_inactive_account_without_admin_proof_is_refused`
- `tests/cabinet/test_invite_only_landing.py` — Python-модуль
  Классы: нет
  Функции: `test_find_guest_purchase_user_is_non_mutating_for_existing_email_user`, `test_landing_access_uses_existing_user_and_requested_channel`, `test_web_gift_claim_denies_missing_user_before_account_mutation`, `test_bot_gift_claim_link_uses_safe_prefix_threshold`, `test_paid_fulfillment_rechecks_access_before_find_or_create`, `test_paid_gift_fulfillment_never_creates_recipient_user`
- `tests/cabinet/test_invite_only_oauth.py` — Python-модуль
  Классы: нет
  Функции: `test_current_oauth_provider_must_be_trusted_for_admin_email_recovery`
- `tests/cabinet/test_landing_activate_endpoint.py` — Python-модуль
  Классы: нет
  Функции: `open_gate` — Пропускаем рейт-лимит и определение IP — проверяется не они., `test_activation_reaches_the_service`, `test_service_error_becomes_its_own_http_status` — Отказ сервиса должен доезжать до клиента своим кодом, а не пятисоткой.
- `tests/cabinet/test_landing_purchase_campaign.py` — Python-модуль
  Классы: нет
  Функции: `test_body_slug_wins_over_cookie`, `test_cookie_is_used_when_body_has_no_slug`, `test_returns_none_without_any_source`, `test_invalid_cookie_is_ignored`, `test_invalid_body_slug_does_not_fall_back_to_cookie` — Мусор в теле — ошибка вызывающей стороны, а не повод молча подставить, `test_blank_body_slug_still_lets_the_cookie_work` — Пустое поле — «не прислали», а не «прислали мусор»., `test_trailing_newline_in_body_is_rejected` — re-шный «$» пропускает \n в конце, pydantic-паттерн кабинета — нет.
- `tests/cabinet/test_lava_recurrent_routes.py` — Python-модуль
  Классы: нет
  Функции: `user`, `test_enable_gated_before_touching_db`, `test_get_gated_before_touching_db`, `test_cancel_works_even_when_gate_off` — Отмена — операция безопасности, флагом не гейтится., `test_enable_rejects_trial_subscription`, `test_enable_surfaces_missing_product_reason` — У тарифа не задан продукт Lava — причина доходит до пользователя., `test_enable_returns_payment_url`, `test_get_returns_none_status_without_binding`, `test_get_returns_binding_state`, `test_purchase_gated_and_maps_errors` — Покупка привязкой: гейт фичи, отказы доносятся как 400., `test_purchase_returns_payment_url_and_subscription`
- `tests/cabinet/test_media_token_security.py` — Python-модуль
  Классы: нет
  Функции: `test_token_roundtrip`, `test_token_is_bound_to_file_id`, `test_token_rejects_tampered_and_garbage`, `test_token_rejects_expired`, `test_download_rejects_missing_token`
- `tests/cabinet/test_media_xss_hardening.py` — Python-модуль
  Классы: нет
  Функции: `test_raster_images_served_inline_with_their_type`, `test_non_raster_forced_to_download_as_octet_stream`, `test_html_is_never_text_html`, `test_svg_is_never_image_svg_xml`, `test_hardening_headers_always_present`, `test_filename_sanitized_against_header_injection`, `test_empty_filename_falls_back`, `test_blocked_upload_lists_cover_active_content`
- `tests/cabinet/test_my_avatar.py` — Python-модуль
  Классы: нет
  Функции: `test_picks_the_smallest_size_that_is_still_sharp`, `test_falls_back_to_the_largest_when_all_are_small`, `test_no_photos_means_no_avatar`, `test_file_id_is_cached_including_the_absence_of_a_photo`, `test_telegram_error_does_not_break_the_cabinet`, `test_route_returns_signed_media_url`, `test_route_without_telegram_account_returns_nothing`
- `tests/cabinet/test_oauth_email_merge_revival.py` — Python-модуль
  Классы: нет
  Функции: `db`, `test_email_merge_revives_deleted_user_when_both_verified` — REGRESSION: with BOTH IdP and local row email_verified, a DELETED row gets revived., `test_email_merge_blocks_409_when_local_email_unverified` — SECURITY: local row with email_verified=False must NOT be merged., `test_email_merge_active_user_links_without_revive` — An ACTIVE local user found by email gets the provider linked, NOT revived.
- `tests/cabinet/test_oauth_link_email_backfill.py` — Python-модуль
  Классы: нет
  Функции: `test_backfills_verified_email_when_user_has_none`, `test_does_not_overwrite_existing_email`, `test_skips_backfill_when_email_owned_by_another_account_but_still_links`, `test_does_not_backfill_unverified_provider_email`
- `tests/cabinet/test_oauth_redirect_uri_per_origin.py` — Python-модуль
  Классы: нет
  Функции: `origins`, `test_allowed_mirror_returns_to_itself` — Разрешённое зеркало завершает OAuth на своём же домене., `test_trailing_slash_matches_on_both_sides` — Слэш в конце — у заголовка или в настройке — не должен ломать совпадение., `test_unknown_origin_falls_back_to_canonical` — Чужой Origin не должен получать authorization code., `test_lookalike_origin_is_not_accepted` — Совпадение точное: домен-двойник с суффиксом не проходит., `test_missing_origin_falls_back_to_canonical` — Запрос без Origin — прежнее поведение канонического домена., `test_canonical_origin_allowed_even_without_the_list` — Свой домен работает, даже если список разрешённых пуст., `test_wildcard_in_the_list_does_not_open_everything` — CABINET_ALLOWED_ORIGINS='*' не должен пускать произвольный домен., `test_authorize_stores_redirect_uri_in_state_and_keeps_it_out_of_the_url` — Выбранный адрес возврата уезжает в state, но не в ссылку авторизации., `test_authorize_from_unknown_origin_stores_canonical` — С чужого домена в state попадает канонический адрес, а не присланный., `test_linking_init_uses_the_request_origin` — Привязка провайдера с зеркала тоже возвращается на зеркало., `test_linking_exchange_reuses_the_redirect_uri_from_state` — Обмен кода при привязке идёт с тем же адресом, что и на init.
- `tests/cabinet/test_oauth_relink_forbidden.py` — Python-модуль
  Классы: нет
  Функции: `test_relink_to_another_account_is_refused_not_merged` — Provider identity already on account #2 -> 409, no link, no merge token., `test_relinking_over_occupied_slot_is_refused_not_overwritten` — User already has a *different* Google linked -> 409, old one preserved., `test_same_identity_is_idempotent_no_op` — Re-linking the identity already on this account is a harmless no-op.
- `tests/cabinet/test_oauth_revival_security.py` — Python-модуль
  Классы: нет
  Функции: `test_email_merge_requires_local_user_email_verified` — Source-level guard: the email-merge branch checks user.email_verified., `test_revived_log_field_uses_pre_revival_snapshot` — `revived=<bool>` in the logger.info call must come from a snapshot, `test_revive_called_without_commit_kwarg` — Architect's call: revive_deleted_user no longer accepts `commit=`., `test_revive_service_does_not_commit` — Hard pin: revive_deleted_user implementation does not commit.
- `tests/cabinet/test_platega_recurrent_admin.py` — Python-модуль
  Классы: нет
  Функции: `test_async_builder_populates_sbp_status_when_gate_on`, `test_async_builder_leaves_sbp_status_none_without_active_record` — Gate on, but no active Platega subscription for this subscription_id., `test_async_builder_skips_query_when_gate_off`, `test_sync_builder_never_sets_sbp_fields` — The sync builder has no DB access and must leave both fields at their, `test_route_registered`, `test_cancel_sbp_recurring_owned_subscription_cancels_and_awaits_helper`, `test_cancel_sbp_recurring_wrong_owner_404_and_helper_not_called`, `test_cancel_sbp_recurring_missing_subscription_404` — Same 404 path for a subscription_id that doesn't exist at all.
- `tests/cabinet/test_platega_recurrent_routes.py` — Python-модуль
  Классы: нет
  Функции: `user`, `test_enable_403_when_gate_disabled`, `test_get_403_when_gate_disabled`, `test_cancel_works_even_when_gate_disabled` — Отмена НЕ гейтится: выключение фичи не должно бросать юзеров с живыми, `test_enable_404_when_no_subscription`, `test_enable_400_for_trial_subscription` — Триал — пробник: подключать к нему рекуррентное списание нельзя, `test_enable_400_when_subscription_has_no_tariff`, `test_enable_400_when_tariff_not_found`, `test_enable_happy_path_returns_status_and_redirect` — Gate on, tariff loaded explicitly, helper succeeds -> {status, redirect_url}., `test_enable_value_error_maps_to_400` — No price for the resolved charge period -> 400, not a 500., `test_enable_runtime_error_maps_to_409` — Platega API didn't return a transactionId -> 409, not a 500., `test_get_404_when_no_subscription`, `test_get_returns_none_status_without_active_record`, `test_get_returns_full_shape_for_active_record`, `test_get_next_charge_at_none_serializes_to_none` — A PENDING record has no next_charge_at yet (no callback received)., `test_cancel_404_when_no_subscription`, `test_cancel_returns_cancelled_and_awaits_safe_helper`, `test_purchase_403_when_gate_disabled`, `test_purchase_400_when_tariff_not_found`, `test_purchase_happy_path_returns_redirect_and_subscription_id`, `test_purchase_value_error_maps_to_400` — Отказ сервиса (триал/чужой тариф/disabled) -> 400 с текстом причины.
- `tests/cabinet/test_promo_offer_broadcast_notify.py` — Python-модуль
  Классы: нет
  Функции: `test_delivery_runs_off_plain_ids` — В сервис рассылок уходят голые telegram_id, без ORM-объектов сессии запроса., `test_nothing_queued_without_telegram_recipients` — Некому слать в Telegram — запись рассылки не заводится., `test_promo_preferences_filter_telegram_and_email_notifications`
- `tests/cabinet/test_promo_offer_broadcast_progress.py` — Python-модуль
  Классы: нет
  Функции: `test_broadcast_creates_tracked_delivery` — Отправка сегменту заводит запись рассылки с числом получателей и возвращает её id., `test_broadcast_without_notification_skips_delivery_record` — Без send_notification офферы создаются, но рассылку заводить не за чем., `test_segments_endpoint_returns_counts_for_every_segment` — Кабинет получает охват по каждому сегменту, а не только по выбранному.
- `tests/cabinet/test_promo_offer_email_notify.py` — Python-модуль
  Классы: нет
  Функции: `test_send_promo_offer_email_skips_when_smtp_not_configured`, `test_send_promo_offer_email_renders_template_and_sends`, `test_send_promo_offer_email_prefers_db_override`, `test_promo_offer_email_template_registered` — Дефолтный шаблон promo_offer должен резолвиться из EmailNotificationTemplates., `test_email_fanout_counts_sent_and_failed` — Фан-аут работает на скалярных таргетах (email, language, username) и, `test_email_fanout_empty_targets_noop`
- `tests/cabinet/test_promocode_error_contract.py` — Python-модуль
  Классы: нет
  Функции: `test_activate_returns_structured_error_code` — Every error code must reach the client as ``detail.code`` verbatim., `test_unknown_error_code_falls_back_to_server_error` — An unmapped code degrades to a stable ``server_error`` code, not prose.
- `tests/cabinet/test_promocode_traffic_roundtrip.py` — Python-модуль
  Классы: нет
  Функции: `test_traffic_survives_create` — Указанный при создании трафик попадает в строку и в ответ., `test_traffic_is_updatable` — Правка меняет трафик, а не отвечает 200 со старым значением.
- `tests/cabinet/test_purchase_tariff_expired_trial_reuse.py` — Python-модуль
  Классы: нет
  Функции: `test_purchase_tariff_tariff_lookup_includes_inactive` — REGRESSION: the ``get_subscription_by_user_and_tariff`` fallback CALL
- `tests/cabinet/test_purchase_tariff_refund_on_failure.py` — Python-модуль
  Классы: нет
  Функции: `test_persistence_wrapped_in_refund_guard` — REGRESSION: both persistence branches (extend + create) must sit inside, `test_refund_helper_uses_fresh_user_and_refund_transaction` — REGRESSION: ``_refund_charge`` must re-fetch the user via, `test_refund_helper_records_failed_refund_when_credit_fails` — REGRESSION: ``add_user_balance`` swallows its own errors and returns False, `test_extend_subscription_post_commit_cleanup_is_best_effort` — REGRESSION: ``extend_subscription`` commits the extension, then runs, `test_charge_precedes_guard_and_delivery_steps_stay_outside` — REGRESSION: the guard must start AFTER the committed charge (so it covers, `test_trial_conversion_stays_enabled_in_extend_branch` — REGRESSION: the ``extend_subscription`` call must NOT pass
- `tests/cabinet/test_referral_reward_choice.py` — Python-модуль
  Классы: `TestPermissionIsEnforcedServerSide` (2 методов), `TestOwnership` (2 методов), `TestNullIsAValue` (4 методов)
  Функции: `allowed`, `stub_terms` — Ответ эндпоинта собирается тем же get_referral_terms — здесь он не предмет., `options`
- `tests/cabinet/test_remnawave_import_fallback.py` — Python-модуль
  Классы: нет
  Функции: `test_fallback_is_usable_in_except_clause`
- `tests/cabinet/test_remnawave_sync_timeout.py` — Python-модуль
  Классы: нет
  Функции: `test_sync_timeout_constant_is_sane`, `test_inline_sync_is_time_bounded`, `test_timeout_defers_to_fallback_and_returns_promptly`
- `tests/cabinet/test_role_grant_subset.py` — Python-модуль
  Классы: нет
  Функции: `test_permission_covered_wildcards`, `test_cannot_grant_permissions_not_held`, `test_superadmin_exempt_and_does_not_query`
- `tests/cabinet/test_route_shadowing.py` — Python-модуль
  Классы: нет
  Функции: `cabinet_routes`, `test_no_literal_route_is_shadowed`, `test_guard_detects_shadowing` — Сама проверка обязана быть чувствительной, иначе она молча зелёная.
- `tests/cabinet/test_settings_choice_types.py` — Python-модуль
  Классы: `TestChoiceKeyNormalisation` (2 методов)
  Функции: `test_every_listed_option_is_accepted` — Вариант, показанный админу, обязан сохраняться., `test_boolean_setting_accepts_both_shapes` — Кабинет шлёт настоящий bool, бот — строку. Принимать надо обе формы., `test_string_choices_still_reject_unknown_values` — Контроль: смягчение сравнения не должно открыть дорогу чему угодно., `test_setting_without_choices_is_not_restricted` — Ограничение задаётся списком, а не самим фактом проверки.
- `tests/cabinet/test_support_config_external_url.py` — Python-модуль
  Классы: нет
  Функции: `test_contact_mode_with_telegram_username`, `test_contact_mode_with_external_url`, `test_both_mode_exposes_external_url`, `test_both_mode_with_telegram_username`, `test_tickets_mode_ignores_contact`, `test_empty_contact_yields_no_url`
- `tests/cabinet/test_support_ws_contract.py` — Python-модуль
  Классы: нет
  Функции: `test_shared_error_contract_uses_integer_or_null_retry_after`, `test_support_ws_rejects_query_token_auth`, `test_support_ws_rejects_missing_subprotocol`, `test_support_ws_accepts_bearer_and_echoes_supported_subprotocol`, `test_ticket_snapshot_keeps_assignment_fields_explicitly_nullable`, `test_owner_ticket_reply_respects_reply_block`, `test_owner_ws_reply_rejected_for_globally_blocked_user` — REGRESSION: бан в поддержке обходился через веб-сокет., `test_owner_ws_reply_rejected_when_tickets_disabled` — REGRESSION: при SUPPORT_SYSTEM_MODE=contact сокет продолжал принимать ответы., `test_owner_ws_reply_notifies_legacy_admin_websocket`, `test_support_ws_reply_notifies_legacy_user_websocket`, `test_privileged_ws_reply_writes_permission_audit_without_sensitive_payload`, `test_privileged_ws_status_update_writes_permission_audit`, `test_upload_lifecycle_makes_media_attachable_only_after_finish`, `test_upload_finish_rejects_checksum_mismatch`, `test_upload_begin_requires_ticket_id` — Uploads must be anchored to a visible ticket; a ticketless begin is rejected, `test_upload_begin_enforces_active_transfer_cap` — A single session cannot pin unbounded memory by opening endless transfers;, `test_ticket_create_declared_out_of_scope_in_ready_event`, `test_ws_reply_idempotent_retry_replays_without_duplicate` — An identical retry under the same idempotencyKey must replay the cached, `test_ws_owner_reply_sets_open_status_and_resets_sla` — A user (owner) reply must move the ticket to 'open' and clear the SLA, `test_pick_message_prefers_id_then_last`, `test_bridge_message_added_broadcasts_contract_message_created`, `test_bridge_message_added_noops_when_ticket_missing`, `test_bridge_ticket_created_broadcasts_opening_message`, `test_bridge_status_changed_broadcasts_status_updated`, `test_register_support_ticket_event_bridge_is_idempotent`, `test_bridge_only_emits_whitelisted_event_names`, `test_bridge_consumes_cabinet_emit_payload`, `test_bridge_listener_is_nonblocking_and_isolates_failures`, `test_schedule_bridge_coerces_non_dict_payload_to_empty`
- `tests/cabinet/test_sync_from_panel_relink.py` — Python-модуль
  Классы: нет
  Функции: `test_relinks_id_wiped_sub_and_restores_status_and_squads`, `test_relinks_by_email_when_the_account_has_no_telegram_id`, `test_does_not_steal_panel_user_already_linked_to_sibling`, `test_ambiguous_orphans_refuse_to_relink`
- `tests/cabinet/test_system_errors_permissions.py` — Python-модуль
  Классы: нет
  Функции: `test_section_is_registered`, `test_permission_is_grantable`, `test_wildcard_from_bootstrap_survives_a_role_save` — Bootstrap раздаёт ``system_errors:*`` — редактор ролей обязан его принять., `test_every_permission_required_by_cabinet_routes_is_registered` — Ратчет: право, проверяемое роутом, но не заведённое в реестре, не выдать никому., `test_bootstrap_roles_only_grant_registered_permissions` — То же с другой стороны: роль из bootstrap должна проходить валидацию редактора.
- `tests/cabinet/test_telegram_auth_jwks.py` — Python-модуль
  Классы: нет
  Функции: `test_build_public_keys_handles_mixed_jwks` — Real Telegram JWKS (RSA + EC + OKP + EC-secp256k1) must not raise., `test_build_public_keys_loads_ec_and_okp_keys` — EC P-256 + Ed25519 + EC secp256k1 keys should all parse with pyjwt 2.11+., `test_build_public_keys_skips_unsupported_kty` — Unknown kty (e.g. future-Telegram-quantum key) is silently skipped, not crashes., `test_build_public_keys_skips_jwk_without_kid` — JWK без kid не может быть selected'ом по header'у токена — пропускаем., `test_build_public_keys_returns_tuple_compatible_with_pyjwt_decode` — Возврат должен быть (public_key, alg) — иначе validate_telegram_oidc_token упадёт., `test_build_public_keys_defaults_alg_when_jwk_omits_it` — JWK без поля `alg` → берём _KTY_DEFAULT_ALG[kty]; alg всё равно не пустой.
- `tests/cabinet/test_telegram_consent_keeps_credential.py` — Python-модуль
  Классы: нет
  Функции: `test_widget_428_leaves_the_payload_unconsumed`, `test_widget_retry_with_consent_consumes_payload_once_and_creates_account`, `test_widget_existing_user_is_still_one_time`, `test_oidc_428_leaves_the_token_unconsumed`, `test_oidc_retry_with_consent_consumes_token_once_and_creates_account`, `test_oidc_existing_user_is_still_one_time`
- `tests/cabinet/test_telegram_widget_replay.py` — Python-модуль
  Классы: нет
  Функции: `test_widget_login_is_one_time_and_24h`, `test_widget_link_is_one_time_and_24h`
- `tests/cabinet/test_ticket_admin_author.py` — Python-модуль
  Классы: нет
  Функции: `test_cabinet_admin_reply_stores_admin_id` — REGRESSION: reply_to_ticket в admin_tickets.py должен писать admin.id,, `test_support_ws_reply_stores_actor_id_for_admin` — REGRESSION: _handle_ticket_reply в support_ws.py должен писать, `test_bot_admin_reply_still_stores_admin_id` — Пин существующей (корректной) семантики бот-пути: add_message получает
- `tests/cabinet/test_ticket_cabinet_guards.py` — Python-модуль
  Классы: нет
  Функции: `user`, `test_create_ticket_rejects_blocked_user` — REGRESSION: заблокированный в поддержке пользователь не должен создавать, `test_add_message_rejects_blocked_user` — REGRESSION: дозапись в тикет тоже должна упираться в глобальную блокировку., `test_expired_block_does_not_reject` — Истёкшая блокировка не считается: CRUD возвращает None, guard молчит., `test_create_ticket_rejects_second_open_ticket` — REGRESSION: второй незакрытый тикет через кабинет создать нельзя., `test_add_message_not_limited_by_open_ticket_check` — Лимит открытых тикетов относится только к созданию: отвечать в свой, `test_get_ticket_respects_disabled_mode` — REGRESSION: при SUPPORT_SYSTEM_MODE=contact чтение тикета должно давать, `test_add_message_respects_disabled_mode` — REGRESSION: при выключенных тикетах дозапись в существующий тикет, `test_create_ticket_respects_disabled_mode` — Пин существующего поведения create_ticket., `test_get_tickets_respects_disabled_mode` — Пин существующего поведения списка тикетов.
- `tests/cabinet/test_traffic_packages_discount.py` — Python-модуль
  Классы: нет
  Функции: `classic_mode` — Drive ``get_traffic_packages`` down the classic (non-tariff) branch., `test_traffic_packages_expose_promo_group_discount` — A 20% traffic promo-group discount surfaces on every package., `test_traffic_packages_no_discount_when_group_has_none` — No promo discount → no discount fields, raw price unchanged., `test_traffic_packages_respect_apply_discounts_to_addons_flag` — When the promo group opts out of addon discounts, traffic stays full price., `test_traffic_packages_apply_discount_in_tariff_mode` — Tariff-mode packages go through the same discount path as classic mode., `test_traffic_packages_floor_displayed_price_at_one_ruble` — An extreme discount never displays below 1₽ — matching POST's max(100,...) floor., `test_traffic_packages_default_group_uses_prorated_period_hint` — Default group period-based traffic discount uses the same ceil(remaining days)
- `tests/cabinet/test_webhook_email_templates.py` — Python-модуль
  Классы: нет
  Функции: `test_every_webhook_type_has_email_template_in_every_language` — Новый WEBHOOK_* тип без email-шаблона — регресс к «почта молча пропущена»., `test_webhook_email_language_fallback_to_ru`, `test_webhook_email_localized_subjects_differ_from_ru` — zh/ua — не заглушки: тема отличается от русской., `test_device_name_substitution_and_placeholder_hygiene`, `test_device_name_is_html_escaped`, `test_winback_types_have_email_template_in_every_language`, `test_winback_discount_renders_percent_everywhere`, `test_winback_expired_1d_escapes_end_date`

### tests/ci

- `tests/ci/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `tests/ci/test_workflow_versions.py` — Python-модуль
  Классы: нет
  Функции: `test_every_action_is_pinned_to_a_single_version` — Одно действие — одна версия во всём репозитории., `test_all_actions_are_pinned` — Плавающих ссылок вроде @main или @master быть не должно., `test_python_version_is_the_same_everywhere` — Тесты, линтер и аудит обязаны идти на одной версии Python., `test_python_version_matches_pyproject` — CI не должен проверять код на версии, которую проект не поддерживает., `test_postgres_image_matches_production_compose` — Тестовая база должна быть той же версии, что и боевая.

### tests/contracts

- `tests/contracts/test_public_registration_gate.py` — Python-модуль
  Классы: нет
  Функции: `test_every_public_user_mutation_is_gated_or_narrowly_trusted`, `test_legacy_guest_find_or_create_wrapper_cannot_reappear_in_public_routes`, `test_registration_twins_bind_the_locked_gift_symmetrically`, `test_no_admission_branch_binds_the_locked_gift_twice`, `test_registration_twins_never_bind_the_gift_unguarded` — A raw bind_locked_gift in a twin would surface a lost race as a 500, not a denial.

### tests/crud

- `tests/crud/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `tests/crud/test_autopay_default_on_trial_conversion.py` — Python-модуль
  Классы: нет
  Функции: `test_helper_applies_the_env_default`, `test_helper_respects_a_disabled_default`, `test_trial_conversion_enables_autopay_by_default`, `test_free_relabel_without_conversion_leaves_autopay_alone` — ``convert_trial=False`` — бесплатная смена тарифа, триал остаётся триалом., `test_renewal_of_a_paid_subscription_does_not_touch_autopay` — Пользователь выключил автоплатёж на платной подписке — продление не включает его обратно., `test_classic_purchase_paths_apply_the_default_on_conversion` — В classic-режиме конверсию делает не CRUD, а обработчики — они тоже обязаны применить дефолт.
- `tests/crud/test_create_user_race.py` — Python-модуль
  Классы: `TestViolatedConstraint` (5 методов), `TestCreateUserHappyPath` (1 методов), `TestCreateUserRaceCondition` (4 методов), `TestCreateUserSequenceDesync` (2 методов), `TestCreateUserUnknownIntegrityError` (1 методов)
  Функции: нет
- `tests/crud/test_email_alias_lookup.py` — Python-модуль
  Классы: нет
  Функции: `test_alias_of_an_existing_mailbox_is_found`, `test_different_mailboxes_are_not_matched`, `test_like_wildcards_in_the_local_part_are_escaped` — «_» — обычный символ в адресе, но джокер в LIKE., `test_own_alias_is_not_taken_even_next_to_a_stranger` — LIMIT 1 без исключения себя мог вернуть своего же юзера и скрыть чужого., `test_degenerate_and_unknown_addresses_do_not_query`
- `tests/crud/test_free_tariff_day_reset.py` — Python-модуль
  Классы: нет
  Функции: `test_is_free_paid_periodic`, `test_is_free_zero_periodic`, `test_is_free_mixed_is_not_free`, `test_is_free_empty_prices_not_free`, `test_is_free_daily_zero`, `test_is_free_daily_paid`, `test_paid_sub_carries_days`, `test_free_source_does_not_carry`, `test_trial_does_not_carry_by_default`, `test_trial_carries_only_when_add_on_and_reset_off`, `test_reset_free_days_overrides_trial_add` — Ядро фикса: TARIFF_SWITCH_RESET_FREE_DAYS=true перебивает, `test_carry_trial_helper_matrix`, `test_is_free_source_tariff_true`, `test_is_free_source_tariff_false_for_paid`, `test_is_free_source_tariff_handles_missing`, `test_is_free_source_tariff_safe_on_error` — Любая ошибка lookup → False (переносим дни как раньше, смена не падает).
- `tests/crud/test_payment_crud_roundtrip.py` — Python-модуль
  Классы: нет
  Функции: `test_create_round_trip`, `test_lookup_by_order_id_and_invoice_id`, `test_order_id_is_unique_in_database` — Два платежа с одним order_id разошлись бы по одному уведомлению., `test_processed_events_survive_commit` — Главная проверка: список обработанных событий обязан долетать до БД., `test_second_event_appends_in_database` — Возврат после оплаты — второе событие, первое не должно пропасть., `test_update_status_persists`, `test_update_status_keeps_is_paid_when_not_given` — is_paid=None означает «не трогать»: возврат не должен обнулять факт оплаты., `test_link_to_transaction_persists`, `test_pending_list_excludes_paid`, `test_for_update_lock_is_not_verifiable_on_sqlite` — SQLite молча игнорирует FOR UPDATE — блокировка тут НЕ проверяется., `test_expires_at_round_trip` — Дата истечения должна возвращаться с часовым поясом, а не наивной.
- `tests/crud/test_payment_event_idempotency.py` — Python-модуль
  Классы: `FakePayment` (1 методов)
  Функции: `test_unknown_event_is_not_processed`, `test_remembered_event_is_recognised`, `test_same_id_other_status_is_separate_event` — Возврат по оплаченному счёту — другое событие того же платежа., `test_remember_is_idempotent`, `test_remember_rebuilds_list_instead_of_mutating` — SQLAlchemy замечает изменение JSON-колонки только по присваиванию., `test_remember_handles_none_column` — У записей, созданных до появления колонки, там NULL., `test_both_providers_expose_the_same_helpers` — Расхождение API между провайдерами ломает миксин на ровном месте.
- `tests/crud/test_promocode_crud.py` — Python-модуль
  Классы: нет
  Функции: `test_create_promocode_with_promo_group_id` — Test creating a promocode with promo_group_id, `test_create_promocode_without_promo_group_id` — Test creating a promocode without promo_group_id (other types), `test_get_promocode_by_code_loads_promo_group` — Test that get_promocode_by_code loads promo_group relationship, `test_get_promocodes_list_loads_promo_groups` — Test that get_promocodes_list loads promo_group relationships
- `tests/crud/test_real_payment_methods.py` — Python-модуль
  Классы: нет
  Функции: `test_includes_previously_missing_gateways` — Jupiter / Donut / Lava — те самые забытые шлюзы — теперь учитываются., `test_excludes_only_non_gateway_methods` — MANUAL (отдельная строка) и BALANCE (двойной счёт) — единственные исключения., `test_covers_every_gateway_in_enum` — Любой реальный шлюз из enum обязан быть в списке (защита от будущих пропусков).
- `tests/crud/test_referral_earnings_filter.py` — Python-модуль
  Классы: `TestLevelPaymentCap` (4 методов), `TestLevelUnlockThreshold` (4 методов)
  Функции: `test_referee_directed_days_are_excluded` — Строка награды приглашённому не должна попадать в заработок владельца user_id., `test_predicate_keeps_every_referrer_reason` — Предикат обязан отбрасывать ТОЛЬКО награды приглашённому., `test_distinct_referral_id_does_not_count_own_inviter` — «Сколько у меня рефералов» через DISTINCT referral_id.
- `tests/crud/test_subscription_revive.py` — Python-модуль
  Классы: нет
  Функции: `test_revive_expired_starts_fresh_period` — Истёкшую реанимируем: статус active, период с «сейчас», трафик обнулён., `test_revive_alive_extends_from_end_date` — Ещё живую продлеваем от её end_date, накопленный трафик не сбрасываем., `test_create_paid_subscription_revives_existing_in_multitariff` — Мульти-тариф + есть ИСТЁКШАЯ запись тарифа → revive, без вставки дубля., `test_create_paid_subscription_revives_expired_trial` — #3004 (централизовано): ИСТЁКШИЙ ТРИАЛ того же тарифа при платной покупке, `test_revive_expired_trial_converts_to_paid` — Реанимация истёкшего ТРИАЛА: снимаем триальный флаг, обнуляем трафик,, `test_create_paid_subscription_does_not_revive_active` — Активную (не истёкшую) НЕ реанимируем — падаем в обычное создание/IntegrityError., `test_create_paid_subscription_skips_revive_without_tariff` — Классический режим (tariff_id=None) — lookup тарифа не дёргаем, создаём как раньше.
- `tests/crud/test_trial_conversion_on_paid_purchase.py` — Python-модуль
  Классы: нет
  Функции: `test_create_paid_subscription_converts_alive_trial_of_other_tariff` — Живой триал ДРУГОГО тарифа при платной покупке конвертируется на месте., `test_create_paid_subscription_prefers_same_tariff_alive_trial` — Живой триал ТОГО ЖЕ тарифа берётся из lookup'а напрямую — без второго, `test_create_paid_subscription_uses_passed_conversion_trial` — Кабинет передаёт пре-резолвленного кандидата — повторный lookup не нужен., `test_create_paid_subscription_falls_to_insert_when_conversion_raced` — Конкурентная покупка успела конвертировать кандидата (конверсия вернула, `test_create_paid_subscription_without_trial_falls_to_insert` — Нет живого триала — обычная вставка новой подписки, как раньше., `test_expired_same_tariff_revive_wins_over_conversion` — Истёкшая запись ПОКУПАЕМОГО тарифа реанимируется (#3004) — конверсия, `test_trial_creation_never_triggers_conversion` — Создание САМОГО триала (is_trial=True) не трогает ветку конверсии., `test_convert_helper_delegates_to_extend` — Обёртка конверсии ревалидирует кандидата под локом и делегирует, `test_convert_helper_bails_out_when_candidate_no_longer_trial` — Гонка: под локом кандидат уже не живой триал (конкурентная покупка, `test_resolver_returns_none_when_revive_will_preempt` — EXPIRED подписка покупаемого тарифа → create уйдёт в revive (#3004),, `test_resolver_prefers_same_tariff_alive_trial`, `test_resolver_falls_back_to_freshest_alive_trial`, `test_cabinet_purchase_excludes_conversion_candidate_from_trial_kill` — Source-pin (в духе test_purchase_tariff_expired_trial_reuse): кабинетный
- `tests/crud/test_trial_subscription_idempotency.py` — Python-модуль
  Классы: нет
  Функции: `test_returns_existing_active_subscription_without_insert` — Если у пользователя уже есть живая (active) подписка на тариф — возвращаем, `test_expired_subscription_does_not_block_new_trial_single_tariff` — В single-tariff режиме EXPIRED-подписка не блокирует создание нового триала, `test_returns_existing_trial_subscription_without_insert` — Статус trial тоже считается живым — возвращаем без INSERT., `test_returns_existing_limited_subscription_without_insert` — Статус limited тоже живой (трафик кончился, время ещё есть) — возвращаем без INSERT., `test_pending_trial_is_activated_not_duplicated` — Существующая PENDING-триальная подписка должна быть переведена в active,, `test_integrity_error_on_commit_returns_concurrent_subscription_multitariff` — Если commit падает с IntegrityError (гонка), делаем rollback, expunge объекта, `test_integrity_error_on_commit_reraises_if_no_concurrent_sub` — Если commit падает с IntegrityError, но найти конкурентную подписку не удалось, `test_integrity_error_from_unrelated_constraint_is_reraised_immediately` — IntegrityError по постороннему constraint (не uq_subscriptions_user_tariff_active), `test_integrity_error_on_commit_returns_concurrent_subscription_single_tariff` — Та же защита от гонки в режиме без multi-tariff: используем, `test_creates_new_subscription_when_no_existing` — Когда у пользователя нет подписки — создаётся новая, db.add и db.commit вызываются., `test_different_tariff_subscription_does_not_block_trial_creation` — Живая подписка на тариф 2 не мешает создать триал на тариф 1.
- `tests/crud/test_user_search_conditions.py` — Python-модуль
  Классы: нет
  Функции: `test_in_range_number_matches_telegram_id`, `test_bigint_max_boundary_still_matches_telegram_id`, `test_number_over_bigint_max_falls_back_to_text_only`, `test_very_long_number_falls_back_to_text_only`, `test_text_search_never_touches_telegram_id`
- `tests/crud/test_users_list_subscription_end_sort.py` — Python-модуль
  Классы: нет
  Функции: `test_order_by_subscription_end_soonest_first_then_no_sub`, `test_active_daily_subscriptions_do_not_hog_the_top` — Суточные тарифы обязаны быть исключены — иначе сортировка бесполезна., `test_sort_follows_the_subscription_status_filter` — Связка «покажи истёкших + отсортируй по дате» обязана работать.
- `tests/crud/test_yookassa_idempotent.py` — Python-модуль
  Классы: нет
  Функции: `test_returns_existing_without_insert_on_duplicate` — Запись с таким payment_id уже есть → вернуть её, НЕ вставлять повторно., `test_inserts_when_new` — Записи нет → создаём, коммитим, возвращаем новый платёж., `test_idempotent_on_insert_race` — Пре-чек пуст, но коммит упал по дубликату (гонка) → вернуть запись, `test_real_integrity_error_returns_none_and_logs` — Не дубликат (например, настоящая FK по user_id) → None и корректный ERROR.

### tests/database

- `tests/database/crud/`
- `tests/database/test_central_purchase_hook.py` — Python-модуль
  Классы: нет
  Функции: `yandex_spy` — Patch the Yandex service hooks plus the other lazy side-effects., `test_completed_subscription_payment_fires_once` — Completed SUBSCRIPTION_PAYMENT → fire_purchase_bg(user_id, abs(amount)) once., `test_deposit_does_not_fire` — DEPOSIT is a balance top-up, not a purchase → no purchase event., `test_gift_payment_does_not_fire` — GIFT_PAYMENT is not a self-purchase → no purchase event., `test_refund_does_not_fire` — REFUND must never count as a purchase conversion., `test_not_completed_subscription_payment_does_not_fire_inline` — A pending (is_completed=False) SUBSCRIPTION_PAYMENT must not fire inline., `test_commit_false_does_not_fire_inline` — commit=False defers all side-effects → nothing fires from create_transaction., `test_negative_stored_amount_fires_positive_abs` — SUBSCRIPTION_PAYMENT is stored as a negative debit; the conversion event, `test_deferred_subscription_payment_fires_once` — emit_transaction_side_effects on a completed SUBSCRIPTION_PAYMENT → fires once., `test_deferred_deposit_does_not_fire` — Deferred DEPOSIT side-effects must not fire a purchase event., `test_deferred_not_completed_does_not_fire` — Deferred SUBSCRIPTION_PAYMENT that isn't completed must not fire., `test_deferred_negative_amount_fires_positive_abs` — Deferred path must also pass the positive abs() amount., `test_single_transaction_does_not_double_fire` — One purchase = one fire. The inline (commit=True) path and the deferred
- `tests/database/test_guest_purchase_gift_idempotency.py` — Python-модуль
  Классы: нет
  Функции: `test_guest_purchase_model_has_idempotency_key_column` — GuestPurchase model must have idempotency_key column and ux_guest_purchases_idempotency_key index., `test_multiple_null_idempotency_keys_are_allowed` — Multiple legacy guest purchases with NULL idempotency_key must be allowed., `test_duplicate_non_null_idempotency_key_is_rejected` — Duplicate non-null idempotency_key must trigger uniqueness violation., `test_migration_0107_upgrade_downgrade_upgrade_lifecycle` — Verify revision 0107 upgrade, downgrade, and upgrade on a SQLite database with legacy null rows.
- `tests/database/test_info_page_display_mode.py` — Python-модуль
  Классы: нет
  Функции: `test_model_has_display_mode_column_with_both_default`, `test_crud_update_whitelist_includes_display_mode`, `test_create_request_accepts_valid_display_mode`, `test_create_request_defaults_to_both`, `test_update_request_rejects_invalid_display_mode`, `test_response_schemas_expose_display_mode`
- `tests/database/test_migration_chain.py` — Python-модуль
  Классы: нет
  Функции: `test_single_head`, `test_revision_ids_are_unique`, `test_every_revision_reaches_base` — Разрыв в down_revision оставил бы часть миграций неприменёнными.
- `tests/database/test_paritypay_payments_schema_parity.py` — Python-модуль
  Классы: нет
  Функции: `both`, `test_columns_match`, `test_indexes_match`, `test_column_types_match` — Integer вместо Boolean в рукописном DDL иначе не заметить., `test_order_id_is_unique` — Уникальность orderId не даёт двум записям претендовать на один вебхук., `test_downgrade_removes_the_table` — Откат обязан снимать таблицу, иначе повторный upgrade упрётся в неё.
- `tests/database/test_payment_callback_concurrency_postgres.py` — Python-модуль
  Классы: `Gateway` (5 методов)
  Функции: `test_two_simultaneous_deliveries_credit_balance_once` — Провайдер доставил один вебхук дважды одновременно — баланс вырос один раз., `test_repeated_delivery_after_success_changes_nothing` — Обычный повтор доставки — не гонка, а частый случай: провайдер шлёт до семи раз., `test_late_payment_after_expiry_is_credited_once` — Поздняя оплата: сначала EXPIRED, следом настоящая оплата., `test_wrong_amount_never_credits_balance` — Сумма из уведомления не сошлась со счётом — баланс не трогаем., `test_webhook_and_api_reconciliation_do_not_double_credit` — Вебхук и фоновая сверка по API столкнулись на одном платеже.
- `tests/database/test_payment_locking_postgres.py` — Python-модуль
  Классы: нет
  Функции: `test_for_update_blocks_second_session` — Вторая сессия обязана ЖДАТЬ строку, занятую первой., `test_plain_read_does_not_block` — Контроль к предыдущему тесту: ожидание вызвано именно блокировкой., `test_waiter_sees_committed_changes_after_lock_release` — Дождавшись блокировки, вторая сессия читает уже НОВОЕ значение., `test_duplicate_delivery_credits_exactly_once` — Два одновременных вебхука об одном событии — ровно одно зачисление., `test_duplicate_order_id_loses_race_on_unique_index` — Уникальность order_id обеспечивает БД, а не проверка перед вставкой., `test_order_id_longer_than_column_is_rejected` — VARCHAR(64) на order_id — не декорация: обрезка в клиенте обязательна., `test_payment_without_user_is_rejected` — Внешний ключ на users PostgreSQL проверяет, SQLite — нет., `test_timestamps_keep_the_same_instant_across_timezones` — TIMESTAMPTZ хранит момент времени, а не текст с числами., `test_json_columns_survive_round_trip` — JSON-колонки возвращаются ровно тем же деревом значений.
- `tests/database/test_pool_config.py` — Python-модуль
  Классы: нет
  Функции: `test_sqlite_uses_nullpool_without_kwargs` — Для SQLite пул не применяется — kwargs пустые (NullPool без пулинга)., `test_postgres_pool_kwargs_read_from_settings` — Настраиваемые знобы берутся из settings, безопасные дефолты — фиксированы., `test_pool_defaults_preserve_legacy_values` — Дефолты совпадают с прежними захардкоженными значениями (без регрессии)., `test_pool_size_clamped_to_at_least_one` — pool_size=0 у QueuePool означает «без лимита» — клампим к >= 1., `test_max_overflow_clamped_to_nonnegative`, `test_pool_timeout_clamped_to_at_least_one`, `test_invalid_values_fall_back_to_defaults` — Мусор в env не должен ронять старт — откатываемся к дефолтам., `test_custom_env_values_are_applied` — Числа из env (как строки) корректно парсятся., `test_live_engine_is_wired_with_pool_kwargs` — Боевой engine реально получает kwargs из хелпера — это и есть фикс #3000., `test_custom_pool_values_reach_a_real_engine` — End-to-end: настроенные значения долетают через create_async_engine в пул.
- `tests/database/test_postgres_fixture_guard.py` — Python-модуль
  Классы: нет
  Функции: `test_missing_url_skips_by_default` — Окружение без PostgreSQL не должно ронять прогон., `test_missing_url_fails_when_postgres_is_required` — С поднятым флагом отсутствие базы — падение, а не пропуск., `test_requirement_flag_accepts_usual_spellings`, `test_requirement_flag_ignores_everything_else`, `test_blank_url_counts_as_absent` — Пустая переменная — это отсутствие базы, а не адрес из пробелов., `test_ci_workflow_runs_postgres_tests_for_real` — CI обязан поднимать базу и требовать, чтобы тесты на ней прошли.
- `tests/database/test_reachability_batches_schema_parity.py` — Python-модуль
  Классы: нет
  Функции: `both`, `test_columns_match`, `test_indexes_match`, `test_column_types_match`, `test_jobs_reference_batches`, `test_downgrade_removes_batches`
- `tests/database/test_reachability_crud.py` — Python-модуль
  Классы: нет
  Функции: `test_create_get_and_update_job`, `test_idempotency_key_is_unique`, `test_active_job_is_found_per_kind_only_while_running`, `test_list_jobs_filters_and_counts`, `test_latest_legs_returns_newest_per_target_and_unit`, `test_replace_legs_drops_previous_legs_of_the_job`, `test_prefs_upsert_and_list`, `test_last_vless_leg_price_uses_latest_done_vless_job`
- `tests/database/test_referral_levels_schema_parity.py` — Python-модуль
  Классы: нет
  Функции: `both`, `test_reward_levels_columns_match`, `test_reward_levels_indexes_match` — Именно это и расходилось: create_all делает ix_..._id, миграция — не делала., `test_new_earning_columns_match`, `test_no_duplicate_tariff_foreign_key` — Миграция не должна вешать второй FK поверх созданного по модели., `test_column_shapes_match`, `test_threshold_columns_are_not_nullable` — Порог и флаг подсчёта читаются напрямую в расчёт награды., `test_downgrade_removes_everything_it_added` — Откат обязан возвращать базу к исходному виду., `test_upgrade_is_idempotent` — Повторный прогон на уже обновлённой базе не должен падать.
- `tests/database/test_tabpay_payments_schema_parity.py` — Python-модуль
  Классы: нет
  Функции: `both`, `test_columns_match`, `test_indexes_match`, `test_column_types_match` — Integer вместо Boolean в рукописном DDL иначе не заметить., `test_order_id_is_unique` — Уникальность orderId не даёт двум записям претендовать на один вебхук., `test_downgrade_removes_the_table` — Откат обязан снимать таблицу, иначе повторный upgrade упрётся в неё.
- `tests/database/test_user_balance_lock_postgres.py` — Python-модуль
  Классы: нет
  Функции: `test_user_lock_blocks_second_session` — Пока одно зачисление держит строку пользователя, второе ждёт., `test_concurrent_topups_do_not_lose_money` — Два одновременных зачисления складываются, а не затирают друг друга., `test_lock_returns_fresh_values_not_the_cached_object` — Блокировка обязана отдавать значения из БД, а не из кеша сессии.

#### tests/database/crud

- `tests/database/crud/test_subscription.py` — Python-модуль
  Классы: нет
  Функции: `test_create_trial_subscription_uses_all_available_squads_by_default`, `test_extend_subscription_convert_trial_false_keeps_trial` — Bug #629889 guardrail: extend_subscription(tariff_id=..., convert_trial=False), `test_extend_subscription_default_converts_trial_on_purchase` — Default convert_trial=True (a real tariff purchase) still clears is_trial., `test_reset_trials_deletes_panel_first_and_skips_panel_failures` — #630055-trial: панель удаляется ПЕРВОЙ; если удалить в панели не удалось —, `test_reset_trials_keeps_row_when_panel_id_is_unusable` — Непригодный локальный идентификатор (RemnaWaveInvalidUserIdError) — это битая, `test_reset_trials_panel_not_configured_db_only` — Панель не настроена → orphan'ить нечего, чистим только БД, без вызовов панели., `test_is_trial_already_used_gate` — Единый гейт триала (раньше дублировался в 4 местах purchase.py)., `test_subscription_property_ignores_pending_trial_draft` — Незавершённый платный триал не должен подставляться как основная подписка.
- `tests/database/crud/test_wipe_trial_panel_lookup.py` — Python-модуль
  Классы: нет
  Функции: `api`, `patched_service` — Подменяем SubscriptionService целиком: нужен только его API-клиент., `db`, `test_adopts_by_short_uuid_and_deletes_the_right_account` — Ключевой сценарий: id ещё не пробэкфилен, но панель знает shortUuid., `test_does_not_orphan_a_live_panel_account` — Пропустить панель и удалить строку — значит оставить ACTIVE-сироту., `test_panel_error_during_lookup_does_not_wipe_the_row` — Таймаут — не доказательство. Строку оставляем следующему запуску., `test_unknown_short_uuid_does_not_block_the_reset_forever` — Панель этот shortUuid забыла — удалять нечего, но и застревать нельзя., `test_row_that_never_had_a_panel_user_needs_no_lookup`, `test_existing_numeric_id_is_used_directly`

### tests/external

- `tests/external/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `tests/external/test_apple_iap.py` — Python-модуль
  Классы: `TestAppleDependency` (1 методов), `TestSettings` (8 методов), `TestTransactionValidation` (3 методов), `TestAdapter` (2 методов), `TestSchemas` (4 методов), `TestTimestampParsing` (2 методов), `TestCabinetAppleIAPRoutes` (8 методов), `TestFulfillmentService` (6 методов), `TestAdapterFallback` (1 методов), `TestNotificationService` (7 методов), `TestAppleIAPRouting` (3 методов)
  Функции: `anyio_backend`
- `tests/external/test_bschek_api.py` — Python-модуль
  Классы: нет
  Функции: `test_error_envelope_is_mapped`, `test_no_dpi_on_carries_skipped_units_in_details`, `test_rate_limited_exposes_retry_after`, `test_validation_422_keeps_fields`, `test_cloudflare_524_without_body_is_gateway_error`, `test_html_502_is_gateway_error`, `test_success_body_is_returned_as_is`, `test_no_dpi_on_race_with_200_is_not_an_error`, `test_every_recorded_error_fixture_parses_to_a_code` — Сторож: новый записанный ответ с конвертом ошибки обязан разбираться., `test_operators_params_join_lists_and_keep_cyrillic`, `test_get_openapi_reads_spec_from_api_root`, `test_account_hides_webhook_secret`, `test_methods_hit_expected_paths`, `test_api_key_never_appears_in_repr`
- `tests/external/test_cryptobot_service.py` — Python-модуль
  Классы: нет
  Функции: `anyio_backend`, `test_create_invoice_uses_make_request`, `test_make_request_returns_none_without_token`, `test_verify_webhook_signature`, `test_verify_webhook_signature_without_token`
- `tests/external/test_remnawave_3_0_0.py` — Python-модуль
  Классы: нет
  Функции: `test_coerce_panel_user_id_accepts_ints_and_digit_strings`, `test_coerce_panel_user_id_rejects_everything_else` — Мусорный идентификатор обязан падать на границе клиента, а не уходить в панель:, `test_invalid_user_id_error_is_a_remnawave_api_error` — Вызывающий код ловит RemnaWaveAPIError — новый тип не должен пролетать мимо., `test_is_user_not_found_error_recognises_only_real_absence`, `test_is_user_not_found_error_never_true_for_invalid_local_id` — Битая ссылка в БД бота — это баг данных, а не «в панели нет пользователя»., `test_parsed_user_has_numeric_id_and_no_uuid_field` — 3.0.0 удалил ``uuid`` из UsersSchema — датакласс не должен его воскрешать., `test_get_user_by_id_uses_numeric_path`, `test_get_user_by_id_rejects_uuid_before_any_request`, `test_update_user_body_is_keyed_on_id_not_uuid` — UpdateUserCommand.RequestBodySchema в 3.0.0 не имеет поля ``uuid``: zod срежет, `test_update_user_coerces_digit_string_id_to_number`, `test_update_user_rejects_uuid_before_any_request`, `test_user_actions_are_addressed_by_numeric_id`, `test_resolve_user_sends_exactly_one_identifier`, `test_resolve_user_rejects_zero_or_multiple_identifiers` — Панель требует ровно одно поле — отсекаем локально, не тратя запрос на 400., `test_resolve_user_returns_none_when_panel_has_no_such_user`, `test_resolve_user_propagates_non_not_found_errors` — 400 — это отказ панели обработать запрос, а не «пользователя нет»:, `test_resolve_user_returns_none_on_empty_response_envelope`, `test_extend_user_expiration_sends_days_body`, `test_extend_user_expiration_requires_at_least_one_day` — days < 1 панель отвергнет валидацией — запрос не отправляем вовсе., `test_extend_user_expiration_rejects_uuid_before_any_request`, `test_remove_device_body_uses_numeric_user_id_not_user_uuid` — 2.8.0 переименовал ``userUuid`` -> ``userId`` в HWID-командах, 3.0.0 сделал его, `test_remove_device_coerces_digit_string_user_id`, `test_remove_device_with_uuid_fails_without_touching_panel` — Протухший uuid в БД бота: сообщаем о неудаче, но не шлём заведомо битый запрос., `test_reset_user_devices_uses_single_delete_all_call` — Раньше это был цикл из N удалений с эвристикой «успех, если упало меньше, `test_reset_user_devices_with_uuid_fails_without_touching_panel`, `test_get_user_devices_is_addressed_by_numeric_id`, `test_delete_user_returns_true_on_empty_body` — 3.0.0: DELETE отвечает 204 (синхронно) либо 202 (в очередь) — тела нет,, `test_empty_body_actions_return_true_without_reading_response` — Bulk-операции сквадов, удаление сквада и рестарт ноды выполняются в фоне:, `test_add_many_users_sends_numeric_ids`, `test_add_many_users_rejects_invalid_ids_before_request`, `test_bulk_squad_actions_skip_request_for_empty_id_list`, `test_restart_node_sends_force_restart_body_default_false`, `test_restart_node_forwards_force_restart_true`, `test_restart_all_nodes_sends_force_restart_body`, `test_users_page_stream_omits_cursor_on_first_page`, `test_users_page_stream_passes_cursor_when_given`, `test_users_page_stream_keeps_cursor_as_string` — Запрос коерсит курсор в число (z.coerce.number), а ответ отдаёт его строкой —, `test_users_page_stream_clamps_size_to_panel_contract` — Контракт панели (zod): size строго 1..1000, иначе 400 «Validation failed», `test_users_stream_follows_cursor_until_exhausted`, `test_users_stream_stops_when_next_cursor_is_null_even_if_has_more_true` — Defensive: a null nextCursor terminates the scan regardless of hasMore., `test_find_users_by_telegram_id_filters_in_query_string` — ``GET /api/users/by-telegram-id/{id}`` удалён — поиск живёт в query-фильтре, `test_find_users_by_email_filters_in_query_string` — ``GET /api/users/by-email/{email}`` удалён — тот же query-фильтр стрима., `test_find_users_passes_all_supported_filters`, `test_find_users_sends_no_filters_when_none_given`, `test_find_users_follows_cursor_and_honours_max_results`, `test_find_users_stops_early_once_max_results_reached`, `test_happ_encrypt_404_disables_panel_endpoint_and_falls_back` — 2.8.0 removed POST /api/system/tools/happ/encrypt → 404 must disable further, `test_happ_encrypt_non_404_error_keeps_endpoint_enabled` — A transient 5xx must NOT permanently disable happ-encrypt (only a 404 = removed)., `test_happ_api_fallback_caches_by_subscription_url` — The client is recreated per request — the crypt5 cache must live on the class, `test_happ_api_fallback_cooldown_after_failure` — A Happ API outage must not stall hot paths — one failure pauses further calls., `test_happ_api_fallback_rejects_unexpected_payload_per_url` — A non-happ:// body is a per-URL problem: never cached as a link, never retried,, `test_happ_api_fallback_4xx_does_not_poison_global_cooldown` — A 4xx rejection of one URL must not disable the fallback for everyone., `test_happ_api_fallback_429_arms_cooldown_not_per_url_ban` — 429 is service throttling: pause globally, but the URL must stay retryable., `test_enrich_uses_external_fallback_only_in_cryptolink_mode` — enrich runs on every get_user_by_*: subscription URLs must not go to the, `test_happ_api_fallback_disabled_by_setting` — HAPP_CRYPTOLINK_API_FALLBACK_ENABLED=false must skip the external service., `test_happ_local_encryption_roundtrip` — Локальное шифрование должно давать happ://crypt4/<base64>, расшифровываемый, `test_happ_local_encryption_real_key_single_rsa4096_block` — Со вшитым ключом Happ v4 (RSA-4096) шифртекст — один блок в 512 байт,, `test_happ_local_encryption_rejects_oversized_payload` — PKCS#1 v1.5 вмещает size_in_bytes()-11: слишком длинная ссылка -> None,, `test_happ_local_encryption_disabled_by_setting` — HAPP_CRYPTOLINK_LOCAL_ENCRYPTION_ENABLED=false должен пропустить локальный, `test_happ_local_encryption_stable_for_same_url` — Паддинг PKCS#1 v1.5 случайный, поэтому без кэша каждый вызов давал бы новую, `test_enrich_uses_local_encryption_without_network` — С локальным шифрованием enrich заполняет crypt-ссылку в любом режиме бота,, `test_delete_all_devices_reports_failure_when_devices_remain` — Панель может ответить 200, оставив устройства — это не успех., `test_delete_all_devices_reports_success_when_panel_is_empty`
- `tests/external/test_remnawave_geocheck.py` — Python-модуль
  Классы: нет
  Функции: `test_request_geocheck_posts_to_connections_endpoint`, `test_request_geocheck_default_mode_sends_empty_json_body` — requestBody у команды required: «по умолчанию» — это ``{}``, а не отсутствие тела., `test_request_geocheck_sends_ip_only`, `test_request_geocheck_sends_interface_only`, `test_request_geocheck_rejects_ip_and_interface_together` — Панель выбирает один источник маршрута; отправлять оба — молча неоднозначно., `test_request_geocheck_ignores_blank_values`, `test_request_geocheck_raises_when_panel_returns_no_job_id`, `test_get_geocheck_result_uses_job_id_path`, `test_get_geocheck_result_returns_completed_payload`, `test_parse_node_exposes_ips`, `test_parse_node_ips_defaults_to_empty_list`
- `tests/external/test_remnawave_hosts.py` — Python-модуль
  Классы: нет
  Функции: `test_parse_host_maps_panel_fields`, `test_parse_host_tolerates_missing_optional_fields`, `test_get_all_hosts_calls_hosts_endpoint`, `test_parse_node_exposes_active_inbound_uuids_for_host_linking`, `test_parse_node_without_config_profile_has_no_inbounds`
- `tests/external/test_remnawave_remove_device.py` — Python-модуль
  Классы: нет
  Функции: `test_remove_device_posts_numeric_user_id_in_body` — Тело запроса — {'userId': int, 'hwid': str}; никакого userUuid., `test_remove_device_coerces_digit_string_id_to_int` — БД отдаёт BigInteger, но JSON/FSM могут донести строку — коерсим до запроса., `test_remove_device_rejects_uuid_id_without_hitting_the_panel` — Протухший UUID вместо id — наша битая ссылка, а не запрос к панели., `test_success_when_target_hwid_absent_from_remaining_list`, `test_failure_when_panel_acks_but_hwid_still_present`, `test_404_is_treated_as_success`, `test_other_api_error_is_failure`, `test_transient_exception_is_failure`, `test_bare_ack_without_device_list_is_success` — Panels that reply with just an ack (no devices echo) keep the old behaviour., `test_empty_response_is_success`, `test_reset_user_devices_is_a_single_delete_all_call`, `test_reset_user_devices_coerces_digit_string_id_to_int`, `test_reset_user_devices_rejects_uuid_id_without_hitting_the_panel`, `test_reset_user_devices_404_is_success` — Пользователя/устройств уже нет — цель достигнута., `test_reset_user_devices_failure_is_reported`
- `tests/external/test_tribute_webhook_donation_id.py` — Python-модуль
  Классы: нет
  Функции: `test_distinct_donations_via_same_link_get_distinct_payment_ids` — Два разных доната через одну ссылку не должны схлопываться в один платёж., `test_replayed_webhook_keeps_identical_payment_id` — Повторная доставка того же события обязана дать тот же ключ (дедуп реплеев)., `test_same_second_donations_from_different_users_stay_distinct` — Одновременные донаты разных юзеров не должны делить ключ идемпотентности.
- `tests/external/test_users_stream_size_clamp.py` — Python-модуль
  Классы: нет
  Функции: `test_stream_size_clamped_to_panel_contract`
- `tests/external/test_wata_webhook.py` — Python-модуль
  Классы: `DummyPaymentService` (1 методов), `StubPublicKeyProvider` (2 методов)
  Функции: `anyio_backend`, `test_verify_signature_success`, `test_verify_signature_fails_with_invalid_signature`, `test_verify_signature_fails_without_public_key`
- `tests/external/test_yookassa_webhook.py` — Python-модуль
  Классы: `DummyDB` (1 методов)
  Функции: `configure_settings`, `test_resolve_yookassa_ip_trust_rules`, `test_resolve_yookassa_ip_prefers_last_forwarded_candidate`, `test_resolve_yookassa_ip_accepts_allowed_last_forwarded_candidate`, `test_resolve_yookassa_ip_skips_trusted_proxy_hops`, `test_resolve_yookassa_ip_trusted_public_proxy`, `test_resolve_yookassa_ip_returns_none_when_no_candidates`, `test_handle_webhook_success`, `test_handle_webhook_trusts_cf_connecting_ip`, `test_handle_webhook_with_optional_signature`, `test_handle_webhook_accepts_canceled_event`, `test_handle_webhook_rejects_non_yookassa_ip_by_default`, `test_handle_webhook_skip_ip_check_bypasses_ip_gate`

### tests/fixtures

- `tests/fixtures/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `tests/fixtures/bschek/`
- `tests/fixtures/bschek_fixtures.py` — Python-модуль
  Классы: нет
  Функции: `load_bschek_fixture` — Возвращает фикстуру целиком: status, headers, request, idempotency_key, body., `iter_bschek_fixtures`
- `tests/fixtures/postgres_db.py` — Python-модуль
  Классы: нет
  Функции: `postgres_dsn` — URL тестовой базы из окружения или ``None``., `postgres_is_required` — Требует ли окружение, чтобы тесты на PostgreSQL действительно шли., `require_postgres_dsn` — URL живого PostgreSQL, иначе пропуск теста (или падение, если требуется)., `real_asyncpg` — Снимает заглушку ``sys.modules['asyncpg']``, поставленную conftest., `postgres_database` — URL тестовой базы, в которой уже создана полная схема проекта., `truncate_tables` — Очищает переданные таблицы вместе со счётчиками идентификаторов., `postgres_engine` — Движок к тестовой базе; переданные таблицы очищаются до и после теста., `postgres_session` — Одна сессия к тестовой базе (зеркало ``memory_session``, но на PostgreSQL)., `postgres_sessions` — Несколько независимых сессий, каждая на своём соединении., `lock_waiter_appeared` — Дождалась ли база сессии, стоящей в очереди за блокировкой., `wait_for_lock_waiter` — То же, но отсутствие соперника — сразу падение теста.
- `tests/fixtures/promocode_fixtures.py` — Python-модуль
  Классы: нет
  Функции: `sample_promo_group` — Sample PromoGroup object for testing, `sample_user` — Sample User object for testing, `sample_promocode_balance` — Balance type promocode, `sample_promocode_subscription` — Subscription days type promocode, `sample_promocode_promo_group` — Promo group type promocode, `sample_promocode_invalid` — Invalid/expired promocode, `mock_db_session` — Mock AsyncSession, `mock_has_user_promo_group` — Mock has_user_promo_group function, `mock_add_user_to_promo_group` — Mock add_user_to_promo_group function, `mock_get_promo_group_by_id` — Mock get_promo_group_by_id function, `mock_get_user_by_id` — Mock get_user_by_id function, `mock_get_promocode_by_code` — Mock get_promocode_by_code function, `mock_check_user_promocode_usage` — Mock check_user_promocode_usage function, `mock_create_promocode_use` — Mock create_promocode_use function, `mock_remnawave_service` — Mock RemnaWaveService, `mock_subscription_service` — Mock SubscriptionService, `make_promocode_valid` — Helper to make promocode appear valid (is_valid property)
- `tests/fixtures/sqlite_memory.py` — Python-модуль
  Классы: нет
  Функции: `ensure_real_aiosqlite` — Снять заглушку sys.modules['aiosqlite'] из conftest перед созданием engine., `memory_session` — Сессия к :memory: БД, где созданы только переданные таблицы.

#### tests/fixtures/bschek

- `tests/fixtures/bschek/README.md` — файл
- `tests/fixtures/bschek/account.json` — файл
- `tests/fixtures/bschek/auth_bad.json` — файл
- `tests/fixtures/bschek/auth_none.json` — файл
- `tests/fixtures/bschek/conc_a.json` — файл
- `tests/fixtures/bschek/method_405.json` — файл
- `tests/fixtures/bschek/op_bad.json` — файл
- `tests/fixtures/bschek/op_dpi_on.json` — файл
- `tests/fixtures/bschek/op_mts_beeline.json` — файл
- `tests/fixtures/bschek/op_probeable.json` — файл
- `tests/fixtures/bschek/op_region.json` — файл
- `tests/fixtures/bschek/op_region_enc.json` — файл
- `tests/fixtures/bschek/op_region_lat.json` — файл
- `tests/fixtures/bschek/op_unknown.json` — файл
- `tests/fixtures/bschek/operators.json` — файл
- `tests/fixtures/bschek/p1_probe.json` — файл
- `tests/fixtures/bschek/p1_replay.json` — файл
- `tests/fixtures/bschek/p1_reused.json` — файл
- `tests/fixtures/bschek/p2_full.json` — файл
- `tests/fixtures/bschek/p2_replay.json` — файл
- `tests/fixtures/bschek/p3_bare_mts.json` — файл
- `tests/fixtures/bschek/p4_bare_mts_any.json` — файл
- `tests/fixtures/bschek/pF_fleet.json` — файл
- `tests/fixtures/bschek/pF_replay_0.json` — файл
- `tests/fixtures/bschek/pF_replay_late.json` — файл
- `tests/fixtures/bschek/pF_same_key_while_running.json` — файл
- `tests/fixtures/bschek/p_blocked.json` — файл
- `tests/fixtures/bschek/p_dpi_off.json` — файл
- `tests/fixtures/bschek/p_empty_ops.json` — файл
- `tests/fixtures/bschek/p_legacy_alias.json` — файл
- `tests/fixtures/bschek/p_noidem.json` — файл
- `tests/fixtures/bschek/pv_11_targets.json` — файл
- `tests/fixtures/bschek/pv_all_any.json` — файл
- `tests/fixtures/bschek/pv_all_default.json` — файл
- `tests/fixtures/bschek/pv_all_stars.json` — файл
- `tests/fixtures/bschek/pv_bad_key.json` — файл
- `tests/fixtures/bschek/pv_bare_mts.json` — файл
- `tests/fixtures/bschek/pv_bare_mts_any.json` — файл
- `tests/fixtures/bschek/pv_cfo_stub.json` — файл
- `tests/fixtures/bschek/pv_conflict.json` — файл
- `tests/fixtures/bschek/pv_dup_targets.json` — файл
- `tests/fixtures/bschek/pv_empty_ops.json` — файл
- `tests/fixtures/bschek/pv_garbage.json` — файл
- `tests/fixtures/bschek/pv_ipport_target.json` — файл
- `tests/fixtures/bschek/pv_lat_region.json` — файл
- `tests/fixtures/bschek/pv_mixed.json` — файл
- `tests/fixtures/bschek/pv_mts_cfo_off.json` — файл
- `tests/fixtures/bschek/pv_no_probes.json` — файл
- `tests/fixtures/bschek/pv_no_target.json` — файл
- `tests/fixtures/bschek/pv_old_format.json` — файл
- `tests/fixtures/bschek/pv_sni.json` — файл
- `tests/fixtures/bschek/pv_sni_hosts_only.json` — файл
- `tests/fixtures/bschek/pv_sni_no_hosts.json` — файл
- `tests/fixtures/bschek/pv_star_cfo_any.json` — файл
- `tests/fixtures/bschek/pv_star_cfo_on.json` — файл
- `tests/fixtures/bschek/pv_two_targets.json` — файл
- `tests/fixtures/bschek/pv_unknown_op.json` — файл
- `tests/fixtures/bschek/pv_url_target.json` — файл
- `tests/fixtures/bschek/rl2_a.json` — файл
- `tests/fixtures/bschek/rl2_b.json` — файл
- `tests/fixtures/bschek/s1_poll_00.json` — файл
- `tests/fixtures/bschek/s1_poll_01.json` — файл
- `tests/fixtures/bschek/s1_poll_03.json` — файл
- `tests/fixtures/bschek/s1_second.json` — файл
- `tests/fixtures/bschek/s1_submit.json` — файл
- `tests/fixtures/bschek/sB_after_0.json` — файл
- `tests/fixtures/bschek/sB_cancel.json` — файл
- `tests/fixtures/bschek/sB_cancel_again.json` — файл
- `tests/fixtures/bschek/sB_submit.json` — файл
- `tests/fixtures/bschek/sC_poll_05.json` — файл
- `tests/fixtures/bschek/sC_replay_done.json` — файл
- `tests/fixtures/bschek/sC_replay_running.json` — файл
- `tests/fixtures/bschek/sC_submit.json` — файл
- `tests/fixtures/bschek/sD_poll_37.json` — файл
- `tests/fixtures/bschek/sD_submit.json` — файл
- `tests/fixtures/bschek/s_cancel_done.json` — файл
- `tests/fixtures/bschek/s_notfound.json` — файл
- `tests/fixtures/bschek/sv_25.json` — файл
- `tests/fixtures/bschek/sv_all_any.json` — файл
- `tests/fixtures/bschek/sv_cfo_any_sni.json` — файл
- `tests/fixtures/bschek/sv_cfo_on.json` — файл
- `tests/fixtures/bschek/sv_not24.json` — файл
- `tests/fixtures/bschek/sv_one_unit.json` — файл
- `tests/fixtures/bschek/sv_webhook.json` — файл
- `tests/fixtures/bschek/v1_poll_00.json` — файл
- `tests/fixtures/bschek/v1_poll_01.json` — файл
- `tests/fixtures/bschek/v1_poll_12.json` — файл
- `tests/fixtures/bschek/v1_second.json` — файл
- `tests/fixtures/bschek/v1_submit.json` — файл
- `tests/fixtures/bschek/v2_cancel.json` — файл
- `tests/fixtures/bschek/v2_cancel_again.json` — файл
- `tests/fixtures/bschek/v2_replay.json` — файл
- `tests/fixtures/bschek/v2_status.json` — файл
- `tests/fixtures/bschek/v2_submit.json` — файл
- `tests/fixtures/bschek/vA_poll_02.json` — файл
- `tests/fixtures/bschek/vA_submit.json` — файл
- `tests/fixtures/bschek/vB_poll_34.json` — файл
- `tests/fixtures/bschek/vB_submit.json` — файл
- `tests/fixtures/bschek/vC_after_cancel.json` — файл
- `tests/fixtures/bschek/vC_cancel.json` — файл
- `tests/fixtures/bschek/vC_same_key_now.json` — файл
- `tests/fixtures/bschek/vC_submit.json` — файл
- `tests/fixtures/bschek/vD_poll_19.json` — файл
- `tests/fixtures/bschek/vD_submit.json` — файл
- `tests/fixtures/bschek/vE_poll_01.json` — файл
- `tests/fixtures/bschek/vE_submit.json` — файл
- `tests/fixtures/bschek/v_cancel_done.json` — файл
- `tests/fixtures/bschek/v_noconfigs.json` — файл
- `tests/fixtures/bschek/v_notfound.json` — файл
- `tests/fixtures/bschek/v_suburl.json` — файл
- `tests/fixtures/bschek/v_too_large.json` — файл
- `tests/fixtures/bschek/v_too_many.json` — файл

### tests/handlers

- `tests/handlers/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `tests/handlers/test_admin_referral_levels.py` — Python-модуль
  Классы: `TestSingleAnswerPerCallback` (4 методов), `TestActiveBonusSelection` (2 методов), `TestNewLevelSafety` (3 методов), `TestValueInput` (8 методов), `TestTariffSelection` (2 методов), `TestCallbackRouting` (2 методов), `TestPendingInputIsCancelled` (4 методов), `TestDeletedLevelDoesNotResurrectActive` (1 методов), `TestEditorTraps` (7 методов), `TestChainDepthEditing` (4 методов), `TestLevelUnlockThresholdEditing` (4 методов), `TestLevelsModeToggle` (8 методов), `TestCallbackAnswerLength` (3 методов), `TestRegistrationPercentTrap` (3 методов), `TestNonFiniteInput` (2 методов), `TestThresholdWarningPrecision` (2 методов), `TestDepthInputIsCancelledToo` (3 методов)
  Функции: `wired` — Подменяет CRUD уровней и тарифов, собирая записи.
- `tests/handlers/test_admin_tariff_custom_traffic.py` — Python-модуль
  Классы: нет
  Функции: `test_tariff_card_renders_custom_traffic_status_and_navigation`, `test_custom_traffic_screen_uses_neutral_value_for_unset_price`, `test_enable_persists_only_enabled_flag_for_valid_settings`, `test_enable_rejects_invalid_settings_without_write`, `test_disable_preserves_price_and_bounds`, `test_price_input_converts_rubles_exactly_and_updates_only_price`, `test_invalid_price_keeps_fsm_active_and_does_not_write`, `test_minimum_above_current_maximum_is_rejected`, `test_maximum_below_current_minimum_is_rejected`, `test_missing_tariff_is_handled_without_update`, `test_custom_traffic_screen_renders_all_unset_values_and_back_navigation`, `test_existing_topup_control_remains_independent`, `test_show_settings_clears_field_edit_state`, `test_minimum_success_updates_only_minimum`, `test_maximum_success_updates_only_maximum`
- `tests/handlers/test_admin_tariff_custom_traffic_contract.py` — Python-модуль
  Классы: нет
  Функции: `test_tariff_card_exposes_custom_traffic_entry`, `test_tariff_summary_includes_custom_traffic_block`, `test_custom_traffic_module_is_registered_from_tariff_router`, `test_custom_traffic_screen_and_handlers_are_registered`, `test_generic_tariff_toggle_excludes_custom_traffic_callback`, `test_custom_traffic_fsm_states_exist`, `test_disable_path_updates_only_enabled_flag`, `test_field_handlers_use_existing_crud_boundary`
- `tests/handlers/test_broadcast_custom_buttons.py` — Python-модуль
  Классы: нет
  Функции: `test_keyboard_passes_icon_custom_emoji_id`, `test_schema_roundtrips_icon_custom_emoji_id`, `test_schema_defaults_to_none_and_rejects_garbage`
- `tests/handlers/test_device_rename_cancel.py` — Python-модуль
  Классы: нет
  Функции: `test_cancel_button_reopens_device_list`, `test_typed_cancel_reopens_device_list`, `test_valid_name_saves_and_reopens`, `test_empty_after_normalize_keeps_state_for_retry`
- `tests/handlers/test_gift_deeplink_activation.py` — Python-модуль
  Классы: `TestGiftDeeplinkActivation` (7 методов), `TestGiftSubscriptionActivationMultiTariff` (3 методов), `TestGiftSubscriptionActivationSingleTariff` (3 методов), `TestGiftProvisioningInvariants` (2 методов)
  Функции: нет
- `tests/handlers/test_gift_share_screens.py` — Python-модуль
  Классы: `TestAccessControl` (4 методов), `TestQrScreen` (3 методов), `TestCopyTextScreen` (3 методов), `TestGiftCardOwnership` (2 методов)
  Функции: `wired`, `test_both_screens_are_registered` — Кнопка без обработчика молча ничего не делает., `test_card_offers_both_buttons`
- `tests/handlers/test_info_menu_keyboard.py` — Python-модуль
  Классы: нет
  Функции: `test_rules_button_shown_by_default`, `test_rules_button_hidden_when_disabled`, `test_custom_page_buttons_added`, `test_no_custom_buttons_without_pages`
- `tests/handlers/test_no_undefined_texts_in_handlers.py` — Python-модуль
  Классы: нет
  Функции: `test_handlers_never_use_undefined_texts`
- `tests/handlers/test_overpay_options.py` — Python-модуль
  Классы: нет
  Функции: `test_option_map_covers_all_payment_methods`, `test_available_options_without_int`, `test_available_options_with_int`, `test_int_disabled_mid_flow_rejects_and_clears_state`
- `tests/handlers/test_page_html_is_telegram_safe.py` — Python-модуль
  Классы: нет
  Функции: `visible` — Все инфо-разделы показываются в боте., `test_faq_page_drops_unsupported_markup`, `test_privacy_policy_drops_unsupported_markup`, `test_public_offer_drops_unsupported_markup`, `test_service_rules_drop_unsupported_markup`, `test_allowed_formatting_survives` — Преобразование не должно съедать разметку, ради которой её и писали., `test_long_page_is_split_without_breaking_a_tag` — Нарезка идёт по преобразованному тексту, иначе тег рвётся посередине., `test_admin_privacy_preview_drops_unsupported_markup` — Экран «Текущий текст политики» показывает то же, что увидит пользователь., `test_admin_offer_preview_drops_unsupported_markup`
- `tests/handlers/test_platega_sbp_status_text.py` — Python-модуль
  Классы: нет
  Функции: `test_none_record_means_not_connected`, `test_pending_status`, `test_active_status_with_next_charge_at`, `test_active_status_without_next_charge_at_shows_placeholder` — ACTIVE достижим и без next_charge_at — например, сразу после коллбека, `test_past_due_status`, `test_cancelled_status`, `test_failed_status`, `test_unknown_status_falls_back_to_raw_value` — Защитная ветка: неизвестный статус не должен молча теряться (как и в
- `tests/handlers/test_promo_segment_counts.py` — Python-модуль
  Классы: нет
  Функции: `test_promo_segment_counts_match_recipient_lists` — Для каждого промо-сегмента SQL-счётчик равен длине списка получателей., `test_promo_segment_counts_are_not_all_zero` — Явные ожидания — паритет сам по себе прошёл бы и на двух одинаково пустых ветках., `test_expired_segment_edge_cases` — Каждый расходившийся случай — в отдельной БД, чтобы ошибки не сокращались., `test_unknown_segment_counts_zero` — Неизвестный ключ не роняет запрос и не показывает мусорный охват.
- `tests/handlers/test_referral_invite.py` — Python-модуль
  Классы: нет
  Функции: `test_create_invite_message_wraps_links_in_code`
- `tests/handlers/test_referral_settings_screen.py` — Python-модуль
  Классы: `TestVisibility` (3 методов), `TestCurrentStateIsVisible` (3 методов), `TestSaving` (7 методов), `TestDaysTargetFollowsTheKindChoice` (2 методов), `TestAmountsAreShown` (2 методов)
  Функции: `allowed`, `sides` — Суммы сторон считает движок по настоящей БД — здесь она не предмет., `subs`, `test_every_handler_is_registered` — Обработчик без регистрации — кнопка, которая молча ничего не делает.
- `tests/handlers/test_resolve_subscription_footgun_guard.py` — Python-модуль
  Классы: нет
  Функции: `test_underscore_trailing_number_is_NOT_used_as_sub_id`, `test_colon_trailing_real_sub_id_is_used`
- `tests/handlers/test_sbp_recurring_handlers.py` — Python-модуль
  Классы: нет
  Функции: `test_autopay_menu_daily_tariff_still_shows_sbp_entry` — Daily-tariff subscriptions can't use balance-autopay, but Platega's SBP, `test_autopay_menu_daily_tariff_gate_off_hides_sbp_entry` — Counterpart of the reachability test: with the Platega recurrent gate, `test_menu_gate_off_shows_alert_and_does_not_render`, `test_menu_no_active_record_shows_enable_button`, `test_menu_active_record_shows_cancel_button`, `test_menu_no_subscription_shows_alert`, `test_enable_gate_off_shows_alert`, `test_enable_trial_subscription_blocked_before_helper` — Trial subscriptions must not be able to authorize a real recurring bank, `test_enable_without_tariff_shows_alert_and_skips_helper` — No tariff on the subscription -> must short-circuit BEFORE calling the, `test_enable_value_error_shows_friendly_alert`, `test_enable_runtime_error_shows_friendly_alert`, `test_enable_success_shows_redirect_url_button`, `test_enable_idempotent_return_without_redirect_shows_status` — Idempotent return (already-active record) may carry no redirect_url —, `test_cancel_works_even_when_gate_off` — Отмена НЕ гейтится (паритет с кабинетным cancel): выключение фичи при, `test_cancel_gate_on_calls_helper_and_refreshes_menu`, `test_toggle_autopay_enable_cancels_active_sbp_recurring`, `test_toggle_autopay_disable_does_not_touch_sbp` — Disabling balance-autopay must NOT cancel SBP — only the enable path, `test_toggle_autopay_enable_blocked_before_cancel_for_trial` — A trial subscription is rejected before update_subscription_autopay is, `test_tariff_confirm_keyboard_shows_sbp_button_when_gate_on`, `test_tariff_confirm_keyboard_hides_sbp_button_when_gate_off`, `test_daily_tariff_confirm_keyboard_gates_sbp_button`
- `tests/handlers/test_setting_save_confirmation.py` — Python-модуль
  Классы: нет
  Функции: `test_env_pinned_setting_warns_instead_of_ok`, `test_regular_setting_reports_ok`
- `tests/handlers/test_show_tariffs_list_single_skip.py` — Python-модуль
  Классы: нет
  Функции: `test_show_tariffs_list_single_tariff_skips_list_and_proceeds` — Один тариф из get_tariffs_for_user → не рисуем список, сразу _proceed с skip_selection., `test_show_tariffs_list_multiple_tariffs_shows_list` — Два и больше тарифов → список как раньше, без авто-перехода., `test_select_tariff_wrapper_parses_id_and_delegates` — select_tariff остаётся тонкой обёрткой: парсит id и зовёт _proceed без skip_selection., `test_proceed_skip_selection_uses_back_to_menu` — Схлопнутый выбор: клавиатура периодов с back_callback=back_to_menu., `test_proceed_normal_selection_uses_menu_buy_back` — Обычный выбор из списка: клавиатура периодов с back_callback=menu_buy., `test_back_target_survives_a_redraw_of_the_custom_screen` — «Назад» не должен деградировать при перерисовке конфигуратора., `test_single_owned_tariff_shows_the_list_instead_of_a_dead_button` — Если покупать нечего, пропуск превращает «Купить» в мёртвую кнопку.
- `tests/handlers/test_start_invite_only.py` — Python-модуль
  Классы: нет
  Функции: `test_registration_invite_payload_preserves_original_start_parameter`, `test_telegram_access_evaluation_forwards_identity_and_lock`, `test_create_user_with_registration_invite_commits_gift_atomically`, `test_invite_denial_includes_support_button_when_contact_is_configured`, `test_invite_denial_renders_without_button_when_contact_is_empty`, `test_pending_gift_drain_delegates_to_the_shared_claim_service` — Активация из FSM идёт через общий claim-сервис, а не через собственный запрос., `test_already_claimed_gift_is_reported_instead_of_ignored` — Ссылка, которую уже забрал другой человек, обязана отвечать, а не молчать.
- `tests/handlers/test_start_menu_media.py` — Python-модуль
  Классы: нет
  Функции: `test_video_takes_precedence_over_logo`, `test_without_video_falls_back_to_logo`, `test_removed_video_returns_to_logo` — Удаление видео в кабинете сразу возвращает прежнее поведение., `test_long_caption_goes_to_plain_text` — Подпись длиннее лимита Telegram нельзя приложить ни к видео, ни к фото., `test_video_send_failure_still_delivers_menu` — Битый file_id не должен оставлять пользователя без меню., `test_video_used_even_when_logo_mode_disabled` — Видео — самостоятельная настройка, не зависит от ENABLE_LOGO_MODE., `test_answer_path_sends_video` — /start отвечает через message.answer — видео обязано работать и там., `test_answer_path_without_video_delegates_unchanged` — Без видео поведение обязано остаться ровно прежним (патченный answer)., `test_answer_path_falls_back_when_video_broken`, `test_answer_path_long_caption_delegates_to_text`
- `tests/handlers/test_start_subid.py` — Python-модуль
  Классы: `TestSplitStartParamSubid` (9 методов)
  Функции: нет
- `tests/handlers/test_start_subid_drain.py` — Python-модуль
  Классы: нет
  Функции: `test_drain_calls_upsert_subid_with_pending_value` — Happy path: FSM has pending_subid → drain calls upsert_subid with, `test_drain_is_noop_when_no_pending_subid` — No pending_subid in FSM → drain returns without calling upsert. This is the, `test_drain_is_noop_when_pending_subid_is_empty_string` — Empty-string subid (defensive — parser rejects this, but a future bug or, `test_drain_swallows_upsert_failure` — When upsert_subid raises (DB transient, FK violation), the drain must catch, `test_drain_is_noop_when_get_data_returns_none` — state.get_data() returning None (aiogram quirk on fresh state) → drain treats, `test_drain_function_imported_from_start_module` — The drain function must remain importable as a public(ish) symbol on
- `tests/handlers/test_subscription_detail_keyboard.py` — Python-модуль
  Классы: нет
  Функции: `test_autopay_button_present_for_active_subscription`, `test_autopay_button_uses_legacy_callback_without_sub_id` — The button intentionally uses the existing `subscription_autopay` exact-match, `test_autopay_button_hidden_on_expired_subscription`, `test_autopay_button_hidden_on_disabled_subscription`, `test_autopay_button_present_when_status_unknown` — When sub=None, the keyboard treats the subscription as active (is_inactive=False)., `test_show_subscription_detail_writes_active_subscription_id_to_fsm` — The whole multi-tariff autopay fix hinges on this side effect: opening a, `test_show_subscription_detail_does_not_write_fsm_on_idor_miss` — When the subscription doesn't belong to the requesting user (IDOR check returns
- `tests/handlers/test_subscription_gift_button_fallbacks.py` — Python-модуль
  Классы: `TestGiftButtonFallbacks` (7 методов)
  Функции: `mock_user`, `simulate_missing_gift_keys` — Simulate missing GIFT_* keys in locale files while preserving other keys.
- `tests/handlers/test_subscription_gift_catalog.py` — Python-модуль
  Классы: `TestSubscriptionGiftEntryVisibility` (10 методов), `TestSubscriptionGiftCatalogFlow` (12 методов), `TestGiftHandlerRegistrationAndCollision` (1 методов)
  Функции: `mock_db_user`, `mock_db`, `mock_callback`, `memory_state`
- `tests/handlers/test_subscription_gift_code_activation.py` — Python-модуль
  Классы: `TestGiftCodeActivationEntryAndIsolation` (7 методов), `TestGiftCodeInputAndResultMapping` (10 методов)
  Функции: нет
- `tests/handlers/test_subscription_gift_history.py` — Python-модуль
  Классы: `TestGiftMenuVisibilityAndEmptyHistory` (6 методов), `TestGiftPaginationAndOwnership` (7 методов), `TestGiftRecoveryDetail` (1 методов), `TestSourceNeutralPresentation` (2 методов)
  Функции: `mock_db_user`, `mock_db`, `mock_callback`, `memory_state`
- `tests/handlers/test_subscription_gift_purchase.py` — Python-модуль
  Классы: `TestGiftBalanceConfirmation` (7 методов), `TestGiftReplayAndPresentation` (9 методов)
  Функции: `mock_db_user`, `mock_db`, `mock_bot`, `mock_callback`, `memory_state`, `sample_quote`, `sample_purchase_result`
- `tests/handlers/test_tariff_extend_subscription_id.py` — Python-модуль
  Классы: нет
  Функции: `test_confirm_keyboard_puts_subscription_id_first_period_last`, `test_extend_keyboard_embeds_subscription_id_before_tariff_and_period`
- `tests/handlers/test_tariff_preview_price_matches_engine.py` — Python-модуль
  Классы: нет
  Функции: `test_preview_uses_engine_price_with_extra_devices`
- `tests/handlers/test_tariff_switch_subscription_resolution.py` — Python-модуль
  Классы: нет
  Функции: `test_switch_resolver_prefers_fsm_over_callback_trailing`, `test_switch_resolver_falls_back_to_single_active_when_no_fsm`, `test_switch_resolver_asks_to_choose_when_ambiguous`
- `tests/handlers/test_ticket_message_not_truncated.py` — Python-модуль
  Классы: нет
  Функции: `test_handlers_do_not_slice_user_text`, `test_handlers_enforce_shared_length_limit`
- `tests/handlers/test_ticket_notification_escaping.py` — Python-модуль
  Классы: нет
  Функции: `test_reply_preview_is_escaped`, `test_escaping_does_not_split_entities_on_the_cut` — Обрезка идёт до экранирования, поэтому `&quot;` не разрывается., `test_plain_reply_is_unchanged`
- `tests/handlers/test_ticket_view_opens_last_page.py` — Python-модуль
  Классы: нет
  Функции: `test_opens_on_the_last_page_when_no_page_requested`, `test_explicit_page_from_pagination_button_wins`
- `tests/handlers/test_user_messages_list_refresh.py` — Python-модуль
  Классы: нет
  Функции: `test_render_list_edits_message_and_never_answers`, `test_delete_confirm_renders_via_helper_and_answers_once`

### tests/integration

- `tests/integration/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `tests/integration/test_cross_channel_gift_lifecycle.py` — Python-модуль
  Классы: нет
  Функции: `test_lifecycle_bot_purchase_to_bot_activation` — 1. Bot purchase -> Bot activation:, `test_lifecycle_bot_purchase_to_cabinet_activation` — 2. Bot purchase -> Cabinet activation:, `test_lifecycle_cabinet_balance_purchase_to_bot_activation` — 3. Cabinet balance purchase -> Bot activation:, `test_lifecycle_cabinet_gateway_purchase_after_webhook_to_bot_activation` — 4. Cabinet gateway purchase after paid webhook -> Bot activation:, `test_lifecycle_cabinet_purchase_to_cabinet_activation` — 5. Cabinet purchase -> Cabinet activation:, `test_recovery_of_all_purchase_origins_in_bot_my_gifts` — 6. Recovery of each successful source in bot "My gifts":, `test_backward_compat_historical_full_token_derives_canonical_representation` — Historical full-token gifts receive canonical representation without database migration., `test_backward_compat_legacy_short_codes_in_cabinet_and_strict_in_bot` — Legacy short codes (8-char, 12-char, GIFT-<12>) succeed in cabinet but are rejected in bot., `test_backward_compat_directed_gift_callbacks_and_landing_public_email` — Directed gift callbacks (claim_bound_gift_for_user) and public landing email gifts work seamlessly.
- `tests/integration/test_promocode_promo_group_flow.py` — Python-модуль
  Классы: нет
  Функции: `test_promo_group_promocode_full_workflow` — Integration test: Full workflow of promo group promocode, `test_duplicate_promo_group_assignment_edge_case` — Edge case: User already has promo group from previous promocode, `test_missing_promo_group_graceful_failure` — Edge case: Promocode references deleted/non-existent promo group

### tests/live

- `tests/live/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `tests/live/test_bschek_live.py` — Python-модуль
  Классы: нет
  Функции: `api_key`, `test_operators_shape_and_catalog_parsing`, `test_openapi_core_versions_match_constant` — Версии ядер Xray живут только в описании параметра ``core`` OpenAPI — сверяем константу с ним., `test_account_shape_without_secret`, `test_probe_preview_breakdown`, `test_probe_preview_sni_needs_both_fields`, `test_validation_codes_still_the_same`, `test_scan_preview_rejects_non_24`, `test_scan_preview_shape`, `test_bad_key_is_unauthenticated`

### tests/middlewares

- `tests/middlewares/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `tests/middlewares/test_button_stats_commands.py` — Python-модуль
  Классы: нет
  Функции: `test_start_command_logged`, `test_command_payload_not_stored` — Payload диплинков (webauth_/GIFT_/coupon_ токены) не должен попадать в лог., `test_command_with_bot_mention_normalized`, `test_plain_text_not_logged` — Обычные сообщения (промокоды, переписка с поддержкой) не логируются., `test_middleware_registered_for_messages` — Пин: middleware подключён и к message-апдейтам (иначе команды не видны).
- `tests/middlewares/test_channel_checker_blocked_user.py` — Python-модуль
  Классы: нет
  Функции: `fake_logger`, `test_unreachable_user_does_not_reach_admins`, `test_real_send_failure_still_reported`, `test_prompt_is_delivered_when_user_is_reachable`, `test_blocked_user_pressing_stale_button_is_swallowed` — 403 на edit_text не должен долетать до GlobalErrorMiddleware., `test_unexpected_edit_failure_still_propagates`, `test_deactivation_notice_to_blocked_user_is_quiet` — Отписавшийся от канала и заблокировавший бота давал сразу два отчёта админам., `test_real_notification_failure_still_reported`
- `tests/middlewares/test_channel_checker_payload.py` — Python-модуль
  Классы: `TestPayloadFunctions` (7 методов)
  Функции: нет
- `tests/middlewares/test_invite_only_admin_recovery.py` — Python-модуль
  Классы: нет
  Функции: `test_blocked_env_admin_still_reaches_the_bot` — BLOCKED is set automatically when a user mutes the bot — it must not lock the owner out., `test_refresh_remnawave_description_uses_numeric_panel_id`
- `tests/middlewares/test_maintenance_expected_errors.py` — Python-модуль
  Классы: нет
  Функции: `test_stale_callback_logged_quietly`, `test_blocked_bot_logged_quietly`, `test_unexpected_bad_request_stays_error`
- `tests/middlewares/test_rich_error_report.py` — Python-модуль
  Классы: нет
  Функции: `test_rich_error_report_structure`, `test_rich_error_report_none_when_oversized`, `test_send_error_uses_rich_and_clears_buffer`, `test_send_error_falls_back_to_document_when_rich_unavailable`

### tests/services

- `tests/services/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `tests/services/reachability/`
- `tests/services/test_account_merge_service.py` — Python-модуль
  Классы: `TestComputeAuthMethods` (6 методов), `TestBuildSubscriptionPreview` (3 методов), `TestBuildUserPreview` (2 методов), `TestGetMergePreview` (4 методов), `TestExecuteMergeValidation` (6 методов), `TestExecuteMergeOAuthTransfer` (2 методов), `TestExecuteMergeTelegramTransfer` (2 методов), `TestExecuteMergeEmailTransfer` (2 методов), `TestExecuteMergeBalance` (3 методов), `TestExecuteMergePartnerStatus` (4 методов), `TestExecuteMergeReferralCommission` (2 методов), `TestExecuteMergeSecondaryDeleted` (3 методов), `TestExecuteMergeSubscription` (7 методов), `TestExecuteMergeSubscriptionMultiTariff` (2 методов), `TestExecuteMergeBulkUpdates` (1 методов), `TestExecuteMergeSelfReferralPrevention` (6 методов)
  Функции: нет
- `tests/services/test_admin_notification_hardening.py` — Python-модуль
  Классы: нет
  Функции: `test_redact_telegram_secrets`, `test_redact_telegram_secrets_handles_multiple_tokens`, `admin_service`, `test_send_message_retries_on_flood_control` — RetryAfter on attempt 1, success on attempt 2 → exactly one sleep, returns True., `test_send_message_respects_global_admin_switch`, `test_send_message_gives_up_after_three_flood_errors` — Three consecutive RetryAfter → two sleeps, third attempt returns False without sleeping., `test_send_message_caps_retry_after_at_30s` — retry_after=120 from Telegram must be clamped to 30s to avoid blocking the flush task., `webhook_service`, `test_node_event_coalescing_keeps_one_flush_task` — 7 concurrent enqueues land in one buffer with one scheduled flush task., `test_node_event_buffer_overflow_counts_dropped_events` — Past BUFFER_MAX, events are dropped but counted in overflow., `test_coalesced_summary_truncates_and_reports_overflow` — 50 unique nodes + 7 overflow → 40 lines + 'truncated' line + 'отброшено' line., `test_coalesced_summary_single_event_omits_count_suffix` — One event → header without '× N' suffix., `test_coalesced_summary_dedupes_by_name_and_address` — Same (name, address) repeated 5 times → 1 line, header shows × 5 total., `test_enqueue_tracks_flush_task_in_pending_set` — Active flush task is held in the strong-ref set; auto-removed after completion., `test_stop_drains_buffered_events` — Pending events in the coalesce window get flushed on stop()., `test_stop_is_idempotent_when_buffer_empty` — Calling stop() with no pending events sends nothing and doesn't crash., `test_enqueue_blocked_after_stop` — After stop() the service refuses new enqueues — no orphaned flush tasks., `test_send_message_logs_clamped_retry_after` — retry_after=120 → log includes both clamped value (30) and original (120)., `test_telegram_notifier_processor_redacts_token_in_traceback` — If a future aiogram leaks the bot token in exc str / traceback, it gets redacted.
- `tests/services/test_admin_notification_promo_group.py` — Python-модуль
  Классы: нет
  Функции: `test_promo_group_resolved_after_refresh_dropped_the_relationship` — Точное воспроизведение прода: refresh сбросил связь, уведомление не должно упасть., `test_promo_group_returned_when_already_loaded`, `test_user_without_promo_group_returns_none`, `test_loaded_relationship_never_triggers_io_for_unloaded` — Хелпер обязан отдавать None, а не лезть в базу., `test_loaded_relationship_falls_back_for_non_orm_objects` — Тестовые фейки не инспектируются SQLAlchemy — для них работает обычный getattr.
- `tests/services/test_admin_notification_username_links.py` — Python-модуль
  Классы: нет
  Функции: `test_referrer_info_links_the_referrer_username` — Реферер в уведомлении о пополнении — та же ссылка, что и у плательщика., `test_referrer_info_keeps_non_telegram_login_as_text` — OAuth-логин из кабинета не является Telegram-логином — ссылки быть не должно., `test_referrer_info_without_username_falls_back_to_id` — Без логина остаётся прежний вид — идентификатор, без пустой ссылки., `test_new_ticket_notification_links_username` — Текст уведомления о новом тикете содержит ссылку, а не голый @логин., `test_new_ticket_notification_without_username_has_no_stray_at` — Без логина строка остаётся текстом-заглушкой, без осиротевшей собаки.
- `tests/services/test_apple_iap_reconciliation_service.py` — Python-модуль
  Классы: нет
  Функции: `anyio_backend`, `test_lookup_delegates_to_support_query`, `test_reconcile_recent_transactions_flags_drift_and_counts_backlog`
- `tests/services/test_apple_iap_service.py` — Python-модуль
  Классы: нет
  Функции: `anyio_backend`, `test_fulfill_verified_transaction_happy_path_credits_balance_after_user_lock`, `test_fulfill_verified_transaction_insert_race_returns_existing_without_double_credit`, `test_one_time_charge_dispatch_fulfills_account_owner`, `test_refund_success_debits_balance_and_marks_transaction_refunded`, `test_consumption_request_requires_recorded_user_consent`, `test_notification_payload_hash_insert_race_is_treated_as_replay`
- `tests/services/test_attach_referrer_if_missing.py` — Python-модуль
  Классы: нет
  Функции: `db`, `test_no_op_when_user_already_has_referrer`, `test_no_op_when_no_pending_and_no_code`, `test_attaches_referrer_from_explicit_code`, `test_attaches_referrer_from_redis_pending_when_no_code` — REGRESSION: this is the exact race the user reported., `test_explicit_code_takes_precedence_over_redis` — Explicit URL/state-provided code wins over a stale Redis entry., `test_rejects_self_referral_by_id`, `test_rejects_self_referral_by_telegram_id` — Different DB user IDs but same Telegram account → still self-referral., `test_rejects_self_referral_by_email`, `test_commit_failure_rolls_back_and_returns_none` — If the DB commit fails, the helper rolls back and reports None., `test_registration_event_failure_still_keeps_attachment` — If process_referral_registration raises, the referrer attachment survives., `test_user_without_telegram_id_skips_redis_fallback` — Email-only user (no telegram_id) must not query Redis., `test_invalid_pending_referrer_id_type_is_handled` — Malformed Redis payload (referrer_id is a string that can't int()), `test_process_referral_registration_skips_duplicate_pending_row` — REGRESSION: a second call for the same (referrer, referral) must NOT, `test_process_referral_registration_inserts_first_pending_row` — Negative-control: when no existing pending row, INSERT proceeds normally., `test_helper_lazy_creates_bot_when_caller_omits_it` — Cabinet endpoints don't have a bot in scope; the helper must, `test_cabinet_retroactive_calls_pass_none_for_referral_code` — Source-level pin: the three retroactive attach call sites in, `test_concurrent_attach_uses_conditional_update_not_unconditional_write` — REGRESSION: the helper must use ``UPDATE ... WHERE referred_by_id IS NULL``, `test_concurrent_attach_loser_does_not_fire_event` — When ``rowcount == 0`` (another session already attached), the, `test_helper_uses_caller_supplied_bot_when_provided` — When the bot caller already has a bot (start.py passes message.bot),
- `tests/services/test_auto_extend_description_plural.py` — Python-модуль
  Классы: нет
  Функции: `test_format_days_declension_ru` — The Russian declension matters at the user-visible boundary —, `test_autopay_description_uses_correct_plural_for_one_day` — End-to-end shape: the autopay path renders, `test_autopay_description_uses_correct_plural_for_few_days`, `test_autopay_description_uses_correct_plural_for_many_days`
- `tests/services/test_auto_extend_inactive_tariff.py` — Python-модуль
  Классы: нет
  Функции: `test_try_auto_extend_skips_when_target_tariff_is_inactive` — Operator deactivated the tariff → user must NOT be billed even though, `test_try_auto_extend_skips_inactive_tariff_in_multi_tariff_mode` — Same guard for the multi-tariff selection branch., `test_prepare_auto_extend_context_skips_inactive_target_tariff` — Cart-driven autopay path — the inactive-target guard belongs there too.
- `tests/services/test_auto_extend_trial_traffic_limit.py` — Python-модуль
  Классы: нет
  Функции: `test_trial_conversion_adopts_unlimited_tariff_limit` — The reported case: unlimited (0) paid tariff must win over the trial's 10 GB., `test_trial_conversion_falls_back_to_subscription_tariff_id` — When the cart has no tariff_id, resolve via the subscription's tariff_id., `test_explicit_cart_traffic_is_preserved` — An explicit (custom) cart traffic value must NOT be overwritten by the tariff., `test_non_trial_renewal_is_untouched` — Ordinary (non-trial) renewal: no tariff lookup, return the cart value as-is.
- `tests/services/test_autopay_fail_notifications.py` — Python-модуль
  Классы: нет
  Функции: `test_config_defaults_present`, `test_state_dict_roundtrip`, `test_state_from_none_is_empty`, `test_first_failure_outside_final_window_returns_first`, `test_silent_between_first_and_final_when_no_repeat`, `test_final_reminder_inside_window`, `test_max_cap_blocks_after_two`, `test_post_expiry_blocked_when_cap_reached`, `test_max_zero_disables_all`, `test_late_first_failure_inside_window_sends_final_only`, `test_repeat_interval_sends_after_elapsed`, `test_repeat_interval_not_yet_elapsed_stays_silent`, `test_full_cycle_default_yields_exactly_two_then_silence` — Core guarantee: across ticks from window-open through post-expiry, default config, `test_fresh_cycle_allows_notifications_again` — A renewal advances end_date → caller loads a FRESH state for the new cycle_token., `test_load_save_state_in_memory_roundtrip` — With Redis returning nothing, the in-memory fallback must persist state across, `test_cleanup_evicts_old_cycles` — In-memory state for cycles whose end_date is >72h in the past must be evicted., `test_maybe_notify_sends_first_then_silent` — _maybe_notify_autopay_failure sends on first failure, records state, and stays, `test_maybe_notify_final_when_inside_window` — When the first failure lands inside the final window, the wrapper sends a final, `test_final_not_starved_by_repeats_when_cap_reached` — max=2 + repeats=6h: first + repeat exhaust the cap, but the final still fires, `test_final_fires_even_if_interval_steps_past_window` — If the monitoring tick jumps over the final window (4h → expired), the still-unsent, `test_max_one_still_delivers_final` — Even with max=1 the final is guaranteed (cap bounds repeats, not the final)., `test_load_reads_redis_on_inmemory_miss` — Cross-restart durability: on an in-memory miss, _load must consult Redis and, `test_save_persists_with_ttl_floor` — _save writes key/value/TTL to Redis; the TTL is floored at 60s so a near-zero, `test_email_only_path_uses_cause_specific_reason` — Non-Telegram (email) users: the reason text reflects the failure cause —, `test_email_only_final_reminder_reason` — Email users get the distinct final-reminder wording when the cycle reaches the window.
- `tests/services/test_autopay_period.py` — Python-модуль
  Классы: нет
  Функции: `test_resolve_autopay_period_candidate_with_tariff`, `test_resolve_autopay_period_candidate_falls_back_to_global_when_tariff_has_no_periods` — When the tariff has no priced periods, validation falls back to the global allowlist, `test_resolve_autopay_period_candidate_falls_back_to_global_when_no_tariff` — Classic-mode (no tariff) subscriptions still need bounded periods — the global, `test_resolve_autopay_period_candidate_rejects_when_both_allowlists_empty` — Fail-closed: with no allowlist available anywhere, ANY candidate is rejected and the, `test_resolve_autopay_period_candidate_swallows_broken_tariff` — A tariff whose ``get_available_periods`` raises (corrupted period_prices, ORM lazy-load, `test_update_subscription_autopay_sentinel_does_not_touch_period_when_omitted` — Legacy callers (autopay.py:154, autopay.py:188, miniapp.py:3733) invoke with positional, `test_update_subscription_autopay_explicit_none_clears_period` — When the user clicks "По умолчанию" in the period picker, the handler passes, `test_update_subscription_autopay_explicit_int_sets_period`, `test_update_subscription_autopay_enable_cancels_sbp_recurring` — Взаимоисключение движков продления ЦЕНТРАЛИЗОВАНО в CRUD: включение, `test_update_subscription_autopay_disable_does_not_touch_sbp` — Выключение balance-autopay НЕ должно трогать СБП-автопродление —, `test_autopay_period_unset_sentinel_is_module_private` — The sentinel must stay a private object — exporting it would tempt callers to, `test_set_autopay_period_default_suffix_clears_override` — Suffix `default` → clear the per-subscription override (passes period_days=None)., `test_set_autopay_period_valid_int_writes_period` — Suffix matching a valid tariff period → write it to the subscription., `test_set_autopay_period_invalid_int_alerts_without_writing` — Suffix matching an integer NOT in the tariff allowlist → alert the user and do NOT
- `tests/services/test_backup_covers_all_payments.py` — Python-модуль
  Классы: нет
  Функции: `test_every_payment_table_is_exported`, `test_export_list_has_no_phantom_payment_tables` — Обратная сторона: модель в списке, а таблицы уже нет., `test_payment_table_is_cleared_on_restore` — Восстановление «с заменой» должно чистить и таблицы платежей.
- `tests/services/test_backup_schedule_skip.py` — Python-модуль
  Классы: нет
  Функции: `test_skips_all_missed_hourly_slots_in_one_step`, `test_skips_missed_15min_slots`, `test_future_next_run_advances_by_one_interval`
- `tests/services/test_backup_scheduler.py` — Python-модуль
  Классы: нет
  Функции: `service`, `test_backup_time_interpreted_in_configured_timezone`, `test_backup_time_rolls_to_next_day_by_local_clock`, `test_utc_timezone_keeps_legacy_behavior`, `test_naive_reference_is_treated_as_utc` — Naive reference не должен интерпретироваться в системной зоне хоста —, `test_format_local_renders_next_run_in_configured_timezone` — Лог-время запуска выводится в settings.TIMEZONE (с меткой зоны), чтобы, `test_invalid_timezone_falls_back_to_utc`, `test_concurrent_starts_leave_exactly_one_loop` — РЕГРЕССИЯ #3030: 6 конкурентных start_auto_backup (холодный старт) не, `test_stop_without_running_task_is_noop`
- `tests/services/test_backup_service_list.py` — Python-модуль
  Классы: нет
  Функции: `backup_dir` — Isolated backup directory under pytest's tmp_path., `service` — A minimally-mocked BackupService bound to a temp backup dir., `test_get_backup_list_skips_empty_tar_gz` — Empty .tar.gz must not raise — it's marked corrupted and listing continues., `test_get_backup_list_recovers_other_files_when_one_is_empty` — One bad file doesn't poison the rest of the listing., `test_get_backup_list_handles_truncated_gzip` — Truncated/corrupted gzip → caught as known-corruption, not as bare Exception., `test_get_backup_list_handles_garbage_json` — Non-archive JSON backup with garbage content → corrupted entry, not crash., `test_get_backup_list_corrupted_entries_do_not_log_as_error` — Known corruption logs as warning, not error — TelegramNotifierProcessor
- `tests/services/test_backup_terminate_backends.py` — Python-модуль
  Классы: нет
  Функции: `test_terminates_other_sessions_and_counts`, `test_best_effort_on_privilege_error`
- `tests/services/test_ban_notification_global_switch.py` — Python-модуль
  Классы: нет
  Функции: `service`, `test_typed_ban_is_silent_when_notifications_are_off`, `test_typed_ban_is_delivered_when_notifications_are_on` — Обратная сторона: рубильник не должен глушить включённые уведомления., `test_switch_is_checked_before_touching_the_database` — Выход обязан быть до поиска пользователя, как у соседних методов., `test_every_send_method_respects_the_switch` — Ни один способ уведомить пользователя не должен обходить рубильник.
- `tests/services/test_broadcast_bad_request_logging.py` — Python-модуль
  Классы: нет
  Функции: `test_bad_request_is_logged_once_per_cause_with_telegram_text`, `test_each_distinct_cause_gets_its_own_error_entry`, `test_blocked_users_are_still_counted_quietly` — «chat not found» — это не сбой рассылки, а ушедший пользователь: без error.
- `tests/services/test_broadcast_email_render.py` — Python-модуль
  Классы: нет
  Функции: `test_fragment_gets_branded_layout_with_recipient_and_unsubscribe`, `test_custom_layout_applies_to_broadcasts_too`, `test_full_document_is_sent_as_is_with_unsubscribe_placeholder`, `test_system_broadcast_has_no_unsubscribe_link`, `test_email_render_endpoint_matches_delivery`
- `tests/services/test_broadcast_per_recipient_keyboard.py` — Python-модуль
  Классы: нет
  Функции: `test_keyboard_factory_builds_personal_keyboard_per_recipient` — Каждому получателю уходит своя клавиатура, а не одна общая., `test_blocked_recipients_counted_separately` — Заблокировавшие бота идут в blocked, а не в общую кучу ошибок., `test_without_factory_shared_keyboard_is_used` — Обычные рассылки не затронуты: без фабрики уходит общая клавиатура.
- `tests/services/test_campaign_attribution.py` — Python-модуль
  Классы: нет
  Функции: `test_returns_none_for_empty_slug`, `test_returns_none_when_campaign_not_found`, `test_partner_cannot_be_attributed_to_own_campaign` — Иначе партнёр накрутит себе регистрацию по собственной ссылке., `test_partner_own_campaign_stops_before_any_write` — Отказ обязан случиться ДО привязки реферала, а не только в бонусе., `test_existing_registration_blocks_second_bonus`, `test_successful_attribution_applies_bonus`, `test_unsuccessful_bonus_returns_none`, `test_partner_is_attached_as_referrer` — Кампания партнёра должна проставить его реферером — иначе он не, `test_link_partner_referral_writes_the_referrer` — Сам факт вызова ничего не гарантирует — проверяем результат., `test_existing_referrer_is_not_overwritten_by_campaign_partner` — Кто привёл первым, тот и получает комиссию — перебивать нельзя., `test_errors_are_swallowed_and_rolled_back` — Привязка кампании — побочный эффект: она не имеет права уронить
- `tests/services/test_campaign_bonus_expired_user.py` — Python-модуль
  Классы: нет
  Функции: `test_apply_campaign_bonus_refreshes_user_before_attribute_access` — Пользователь перечитывается асинхронно на входе — до любых sync-чтений, `test_apply_campaign_bonus_survives_refresh_failure` — Сбой refresh (например, PendingRollbackError) не роняет начисление —
- `tests/services/test_channel_check_uncertain.py` — Python-модуль
  Классы: нет
  Функции: `test_member_check_returns_true_when_user_is_member`, `test_member_check_returns_false_on_confirmed_user_not_found` — A BadRequest with 'user not found' is a confirmed non-membership —, `test_member_check_returns_none_on_network_error` — Transient network error must NOT be treated as 'not a member' —, `test_member_check_returns_none_on_bot_removed_from_channel` — Bot's own access loss is the operator's problem, not the user's —, `test_member_check_returns_none_on_unknown_bad_request` — Unrecognised BadRequest message — treat as uncertain rather than, `test_member_check_returns_none_on_double_rate_limit_failure` — Telegram is rate-limiting us hard — the user is not at fault., `test_member_check_returns_none_on_generic_exception` — Any unexpected error keeps the user's access., `test_check_user_subscriptions_preserves_last_known_on_uncertain` — Integration: when API check is uncertain (None), the public result
- `tests/services/test_combo_promocode.py` — Python-модуль
  Классы: нет
  Функции: `test_combo_applies_both_days_and_balance` — Оба эффекта применяются: подписка продлена И баланс пополнен., `test_combo_days_failure_prevents_balance_credit` — Нет подписки → блок дней падает ДО начисления баланса: add_user_balance, `test_single_balance_type_untouched` — Одиночный BALANCE-код по-прежнему только пополняет баланс., `test_cabinet_bonus_set_requires_at_least_one_component` — Кабинетная валидация: в наборе должна быть хотя бы одна составляющая., `test_webapi_bonus_set_requires_at_least_one_component`, `test_combo_grants_traffic` — Трафик из набора начисляется той же подписке, что и дни., `test_combo_without_traffic_does_not_touch_it` — traffic_gb=0 — начисления нет, старые коды ведут себя как прежде., `test_traffic_reactivates_limited_subscription` — Трафик чаще всего дарят тому, у кого он кончился, — подписка в LIMITED., `test_traffic_not_granted_on_unlimited_subscription` — Безлимит: Subscription.add_traffic ничего не делает — и обещать нечего., `test_target_subscription_picked_once_per_activation` — Дни и трафик обязаны попасть в ОДНУ подписку — выбор делается один раз., `test_traffic_only_applies_to_the_bonus_set_type` — Трафик — составляющая набора. Код другого типа его не раздаёт., `test_traffic_only_on_unlimited_does_not_burn_the_code` — Трафик — единственная составляющая, а подписка безлимитная: попытка не сгорает., `test_unlimited_subscription_keeps_other_bonuses` — Если в наборе есть что-то ещё — оно начисляется, а код не откатывается.
- `tests/services/test_coupon_per_user_limit.py` — Python-модуль
  Классы: нет
  Функции: `test_batch_stores_per_user_limit`, `test_zero_limit_keeps_previous_unlimited_behaviour` — 0 — прежнее поведение: сколько угодно купонов партии одному человеку., `test_limit_blocks_second_activation`, `test_limit_is_per_user_not_global` — Лимит одного пользователя не мешает другим забрать свои купоны., `test_limit_counts_only_this_batch` — Активации в другой партии не расходуют лимит текущей., `test_revoked_coupons_do_not_consume_limit` — Считаем только реально погашенные — отозванные пользователю не достались., `test_delete_batch_removes_batch_and_coupons`
- `tests/services/test_coupon_service.py` — Python-модуль
  Классы: нет
  Функции: `test_generated_token_matches_format_and_fits_start_param`, `test_generated_tokens_are_unique`, `test_is_coupon_token_rejects_wrong_shapes`, `test_invalid_format_never_touches_db`, `test_unknown_token_is_invalid`, `test_token_is_normalized_before_lookup`, `test_rejection_never_takes_the_row_lock` — Failed links must not hold FOR UPDATE for the rest of the /start handler., `test_redeemed_by_same_user_is_distinguishable`, `test_redeemed_by_other_user_is_uniform_invalid`, `test_revoked_coupon_is_uniform_invalid`, `test_expired_batch_raises_expired`, `test_missing_tariff_is_internal_error`, `test_concurrent_claim_lost_after_lock_is_rejected` — The locked re-read must re-check the status — a concurrent redemption may win., `test_success_claims_under_lock_and_flips_before_remnawave_sync`, `test_failed_remnawave_sync_aborts_redemption` — create_remnawave_user swallows API errors and returns None WITHOUT, `test_grant_failure_rolls_back_and_raises_internal`, `test_grant_extends_active_subscription`, `test_grant_replaces_expired_subscription`, `test_grant_creates_subscription_when_none_exists`, `test_grant_multi_tariff_looks_up_by_tariff`
- `tests/services/test_daily_reload_subscription.py` — Python-модуль
  Классы: нет
  Функции: `test_reload_daily_subscription_refetches_and_returns_loaded_object`, `test_reload_query_eager_loads_user_and_tariff` — The re-fetch must carry selectinload options for user AND tariff.
- `tests/services/test_daily_subscription_traffic_reset.py` — Python-модуль
  Классы: нет
  Функции: `test_defers_drop_when_panel_resets_and_user_over_limit` — MONTH + used 130 > нового лимита 100 → понижение ОТКЛАДЫВАЕТСЯ до сброса панели., `test_applies_drop_cleanly_when_user_under_limit` — MONTH + used 40 <= нового лимита 100 → лимит понижается, сброса used нет., `test_no_reset_tariff_resets_used_when_over_limit` — NO_RESET (панель сама не сбрасывает) + used 130 > 100 → лимит вниз + сброс used (clamp)., `test_forces_drop_after_grace_even_if_panel_resets` — MONTH, но докупка просрочена >40д → не ждём вечно: понижаем + добиваем used., `test_traffic_reset_only_loop_runs_processor` — #630055: с ВЫКЛЮЧЕННЫМИ суточными тарифами джоба сброса докупок всё равно
- `tests/services/test_email_retry_service.py` — Python-модуль
  Классы: нет
  Функции: `test_backoff_outlives_short_lived_codes` — Фиксируем сам факт расхождения: без срока бэкофф шлёт письма после смерти кода., `test_expired_item_is_killed_without_sending`, `test_live_item_is_still_sent`, `test_item_without_expiry_is_unrestricted`, `test_auth_emails_declare_a_deadline` — Три письма с секретом внутри обязаны передавать срок годности в очередь., `test_body_is_purged_after_successful_delivery`, `test_body_is_purged_when_attempts_run_out`, `test_body_survives_between_attempts` — Пока попытки не исчерпаны, тело нужно — иначе повторять будет нечего., `test_stop_is_safe_without_start`
- `tests/services/test_email_topup_notifications.py` — Python-модуль
  Классы: нет
  Функции: `sent` — Перехватывает send_notification роутера., `test_topup_notification_sent_for_email_user`, `test_topup_notification_skipped_for_telegram_user`, `test_topup_notification_skipped_without_email`, `test_topup_notification_swallows_router_errors`, `test_topup_hook_notifies_before_auto_purchase` — Порядок каналов: письмо о пополнении уходит ДО автопокупки из корзины,, `test_auto_purchase_notification_activated`, `test_auto_purchase_notification_renewed`, `test_auto_purchase_notification_skipped_for_telegram_user`, `test_auto_purchase_notification_swallows_errors`, `test_real_topup_template_renders_with_helper_context`, `test_real_activated_template_renders_with_helper_context`, `test_real_renewed_template_renders_with_helper_context`, `test_trial_conversion_labeled_activated_not_renewed` — Пин: сайты автопокупки тарифа/суточного/extend не должны помечать
- `tests/services/test_env_admin_block_protection.py` — Python-модуль
  Классы: нет
  Функции: `env_admin`, `test_predicate_covers_admin_ids_and_admin_emails`, `test_admin_panel_ban_refuses_env_admin`, `test_cabinet_disable_user_refuses_env_admin`, `test_broadcast_auto_block_skips_env_admin` — A muted bot reports the owner as "blocked" — that must not flip their status., `test_blocked_users_scan_skips_env_admin`, `test_cabinet_restores_stale_blocked_admin_with_invite_only_off` — The gate only emits VERIFIED_ADMIN while invite-only is on — recovery must not depend on it., `test_cabinet_still_refuses_a_blocked_ordinary_user`, `test_middleware_heals_the_stale_blocked_flag_on_an_env_admin` — Letting the admin through is not enough — the flag also suppresses reactivation.
- `tests/services/test_expiring_notification_days_placeholder.py` — Python-модуль
  Классы: нет
  Функции: `test_expiring_paid_supports_days_placeholder`
- `tests/services/test_format_email_datetime.py` — Python-модуль
  Классы: нет
  Функции: `test_default_format_is_locale_independent` — The fallback shape must be ``DD.MM.YYYY, HH:MM`` — no month, `test_explicit_fmt_arg_overrides_settings` — Caller-provided ``fmt`` wins over the global setting — lets, `test_settings_override_takes_effect_without_restart` — An admin who updates ``EMAIL_DATE_FORMAT`` via system_settings, `test_empty_or_invalid_setting_falls_back_to_default` — Operator misconfiguration (empty / non-string) must not crash, `test_localizes_to_configured_timezone` — UTC datetime gets shifted to ``settings.TIMEZONE`` before formatting., `test_naive_datetime_is_treated_as_utc` — Some legacy paths still pass naive datetimes. Treat them as, `test_iso_string_is_parsed_and_reformatted` — Legacy callers that pre-isoformat'd their datetime get parsed, `test_unparseable_string_passes_through` — If the caller pre-formatted the string in some custom shape we, `test_empty_input_returns_placeholder` — ``None`` / empty must not render the literal Python repr or, `test_custom_placeholder_respected`, `test_non_datetime_non_string_input_returns_placeholder` — Garbage input (int, list, etc.) → placeholder. Defensive., `test_no_microseconds_in_output` — REGRESSION (2026-05-18): user saw, `test_no_offset_in_output` — Non-UTC TZ values also must not leak the offset into the email., `test_notification_delivery_service_uses_format_email_datetime` — ``notify_subscription_expiring`` and ``notify_autopay_success``, `test_auto_purchase_service_uses_format_email_datetime` — All ``expires_at`` / ``new_expires_at`` kwarg call sites in, `test_helper_signature_is_stable` — The helper is called from 9+ production sites. Lock its
- `tests/services/test_gift_claim_notify.py` — Python-модуль
  Классы: нет
  Функции: `test_email_recipient_and_buyer_both_get_claim_link`, `test_telegram_recipient_is_not_auto_dmed_but_buyer_still_gets_link`, `test_send_failure_never_raises`, `test_non_gift_purchase_is_a_noop`, `test_email_recipient_gets_admin_override_when_saved` — Жалоба из «Багов»: шаблон письма нельзя было поменять со стандартного., `test_override_lookup_uses_the_gift_template_type_and_claim_context` — Override ищется под тем же типом, что и дефолт, и с тем же контекстом (ссылка на claim)., `test_buyer_backstop_uses_its_own_template_and_admin_override` — Письмо покупателю со ссылкой раньше было зашито в код на двух языках —, `test_disabled_types_are_not_sent` — Выключатель писем: отключённый тип пропускается, остальные уходят.
- `tests/services/test_gift_claim_service.py` — Python-модуль
  Классы: нет
  Функции: `test_claim_gift_bot_origin_canonical_code` — Claiming a bot-origin gift via canonical GIFT_<59> code binds and delivers subscription., `test_claim_gift_cabinet_origin_telegram_deeplink` — Claiming a cabinet/landing origin gift via Telegram deep-link URL binds and delivers., `test_claim_gift_web_url_and_full_token_inputs` — Claiming via cabinet claim URL and raw 64-char token works identically., `test_legacy_short_code_support_flag` — Legacy short codes succeed when allow_legacy_short=True and fail with GiftClaimNotFoundError when False., `test_self_claim_rejected` — Buyer attempting to claim their own gift raises GiftClaimSelfActivationError and does not mutate purchase., `test_claim_already_owned_by_another_user_rejected` — Attempting to claim a gift bound or delivered to another user raises GiftClaimAlreadyOwnedError., `test_idempotent_repeated_claim_by_same_user` — Repeated claim by the SAME user returns DELIVERED purchase without reactivating or extending twice., `test_unactivatable_status_rejected` — Gifts in FAILED, PENDING, or other unactivatable statuses raise GiftClaimNotActivatableError., `test_malformed_or_nonexistent_input_raises_not_found` — Malformed strings, bad schemes, empty strings, and non-existent tokens raise GiftClaimNotFoundError., `test_claim_bound_gift_for_user_directed_callback` — claim_bound_gift_for_user by purchase_id activates directed gifts for the bound claimant., `test_activation_failure_rolls_back_cleanly` — When underlying activate_purchase fails, exception propagates and transaction rolls back.
- `tests/services/test_gift_history_service.py` — Python-модуль
  Классы: нет
  Функции: `test_list_sender_gifts_source_neutral_and_status_filtering` — History returns bot-, cabinet-, and landing-origin gifts for the buyer,, `test_list_sender_gifts_stable_ordering_with_equal_timestamps` — Ordering must be strictly `created_at DESC, id DESC` for deterministic pagination., `test_list_sender_gifts_pagination_and_boundary_clamping` — Pagination metadata must be accurate and limit/offset parameters clamped., `test_list_sender_gifts_returns_more_than_legacy_fifty_item_cap` — The cabinet's 100-item request must not be silently truncated to the old 50-item cap., `test_list_sender_gifts_graceful_on_missing_deleted_tariff` — If a tariff was deleted (ON DELETE SET NULL), the history item must load gracefully., `test_safe_recipient_display_formatting` — Safe recipient display formats username or masked email without leaking private data (names, IDs)., `test_get_sender_gift_exact_owner_and_status_checks` — get_sender_gift returns the item only if buyer_id matches and status is eligible., `test_has_sender_gifts_lightweight_existence_query` — has_sender_gifts returns boolean indicating if buyer owns any eligible gifts., `test_gift_history_item_immutability_and_artifacts` — GiftHistoryItem is a frozen immutable dataclass that builds canonical claim artifacts., `test_gift_history_service_is_purely_read_only` — History queries must never modify database state or create transactions.
- `tests/services/test_gift_payment_log_safety.py` — Python-модуль
  Классы: нет
  Функции: `test_gift_payment_paths_do_not_log_purchase_token_fragments`
- `tests/services/test_gift_purchase_service.py` — Python-модуль
  Классы: нет
  Функции: `test_is_gift_enabled_reflects_cabinet_gift_setting` — Missing or non-true CABINET_GIFT_ENABLED disables the service., `test_list_gift_offers_returns_empty_when_feature_disabled` — When CABINET_GIFT_ENABLED is missing or false, catalog is empty., `test_list_gift_offers_filters_and_orders_tariffs` — Catalog only contains active, show_in_gift tariffs with prices, ordered by display_order then id., `test_list_gift_offers_applies_sender_discounts_and_clamps` — Sender promo-group and promo-offer discounts are applied in quotes and clamped to >= 1 kopek., `test_quote_gift_purchase_success_and_errors` — quote_gift_purchase returns typed GiftQuote on valid selection and raises typed errors on invalid ones., `test_purchase_gift_from_balance_success_bot_mode` — Successful balance purchase: exact debit, GIFT_PAYMENT transaction, promo offer consumed, paid purchase created., `test_purchase_gift_from_balance_cabinet_recipient` — Cabinet mode with GiftRecipient persists recipient fields and custom transaction description., `test_purchase_gift_insufficient_balance_rolls_back` — Insufficient balance raises typed error and leaves DB untouched., `test_purchase_gift_restricted_user` — User with restriction_subscription cannot buy gifts., `test_purchase_gift_stale_expected_price_rejected` — If expected price does not match fresh price calculation, fail without debit., `test_idempotent_replay_and_conflict` — Repeating same idempotency key returns original result; changing input raises conflict., `test_replay_never_attaches_another_gifts_transaction` — Повтор обязан вернуть списание ИМЕННО этого подарка либо ничего.
- `tests/services/test_gift_token_prefix_threshold.py` — Python-модуль
  Классы: нет
  Функции: `test_generated_token_is_64_chars`, `test_threshold_accepts_every_legitimate_truncation`, `test_threshold_rejects_the_old_short_floor`, `test_canonical_bot_gift_claim_link_meets_threshold` — Canonical build_bot_gift_claim_link produces links accepted by the threshold., `test_legacy_12_char_slice_is_rejected_by_threshold` — Legacy 12-char slice is strictly rejected by the 48-char security threshold.
- `tests/services/test_gift_topup_flow.py` — Python-модуль
  Классы: `MockRedis` (6 методов), `TestGiftInsufficientBalanceAndCart` (6 методов), `TestGiftTopupSuccessKeyboardAndResume` (8 методов), `TestGiftAutoPurchaseAndIsolation` (7 методов), `TestGiftAutoPurchaseDeliveryAndPriceChangeSafeguards` (6 методов)
  Функции: `mock_redis`, `test_cart_service`, `mock_db_user`, `mock_db`, `mock_bot`, `mock_callback`, `memory_state`, `sample_quote`, `sample_purchase_result`
- `tests/services/test_grace_access_runtime.py` — Python-модуль
  Классы: `FakeRemnawaveApi` (3 методов)
  Функции: `make_panel_user`, `make_overlay`, `make_limited_billing`, `make_limited_snapshot`, `make_v2_session_row` — Строка, записанная до апгрейда панели: snapshot_version=2 и uuid в JSON., `install_fake_api`, `assert_no_derived_status_writes`, `test_panel_target_serializer_removes_derived_statuses`, `test_apply_limited_billing_restores_canonical_fields_without_writing_limited`, `test_apply_limited_billing_keeps_grace_routing_until_panel_derives_limited`, `test_restore_limited_snapshot_recognizes_safe_active_intermediate`, `test_restore_does_not_overwrite_manual_or_unrelated_panel_state`, `test_apply_limited_billing_does_not_overwrite_manual_or_unrelated_panel_state`, `test_apply_limited_billing_updates_device_limit_even_when_other_fields_already_match`, `test_apply_non_derived_billing_status_remains_one_phase`, `test_apply_overlay_detaches_external_squad_first_and_addresses_the_numeric_id`, `test_read_snapshot_returns_the_numeric_panel_identity`, `test_read_snapshot_rejects_a_legacy_uuid_instead_of_reporting_no_panel_user`, `test_v2_snapshot_row_stays_readable_after_the_identity_backfill`, `test_v2_row_without_a_backfilled_id_fails_loudly_instead_of_closing_silently`, `test_unsupported_snapshot_version_is_rejected_instead_of_guessed`, `test_saving_a_v2_row_upgrades_it_to_v3_without_erasing_the_historical_uuid`, `test_adopt_or_create_patches_the_adopted_panel_user` — Подхватить аккаунт мало — вызывающий просил привести панель к состоянию., `test_adopt_or_create_creates_when_panel_denies_the_short_uuid`, `test_adopt_or_create_propagates_a_non_404_panel_error` — Проглотить 5xx и создать нового — это и есть дубль рядом с живым аккаунтом., `test_adopt_does_not_wipe_squads_when_the_local_list_is_empty` — Пустой список сквадов НЕ должен уходить в PATCH., `test_adopt_forwards_a_non_empty_squad_list` — Обратная сторона: реальный список обязан доехать., `test_open_session_without_panel_id_is_repaired_from_the_subscription` — Сессия с пустым `remnawave_id` должна чиниться, а не жить вечно., `test_get_open_no_longer_explodes_on_a_session_without_panel_id` — `get_open` вызывают продление и разбор платежа — он не должен падать.
- `tests/services/test_grace_access_service.py` — Python-модуль
  Классы: `MutableClock` (3 методов), `MemoryGraceStore` (7 методов), `FakePanelGateway` (5 методов), `FakeBillingGateway` (2 методов)
  Функции: `test_runtime_mode_values_are_explicit_and_fail_closed`, `make_billing`, `make_snapshot`, `make_policy`, `make_service`, `test_subscription_kind_priority_and_feature_flags`, `test_limited_incident_key_tracks_end_limit_and_reset_timestamp`, `test_expired_grace_changes_only_panel_overlay`, `test_limited_grace_adds_bytes_above_usage_without_resetting_usage`, `test_same_incident_is_not_granted_twice`, `test_pending_session_retries_same_overlay_after_temporary_error`, `test_pending_retry_accepts_only_known_external_squad_detach_intermediate`, `test_pending_retry_never_reenables_an_unexpected_manual_panel_state`, `test_timeout_restores_original_panel_values_once`, `test_limited_snapshot_restore_stays_restoring_while_panel_derives_status`, `test_payment_wins_over_grace_snapshot`, `test_confirmed_panel_sync_can_finish_payment_without_duplicate_panel_update`, `test_canonical_squad_change_ends_grace_and_applies_fresh_billing`, `test_panel_identity_change_restores_the_old_user_instead_of_pushing_billing_onto_it`, `test_limited_canonical_change_waits_without_error_then_completes`, `test_limited_transition_conflict_completes_without_retry_error`, `test_webhook_suppression_matches_only_grace_echo`, `test_unlimited_panel_limit_becomes_exact_grace_quota_above_usage`, `test_expired_and_exhausted_subscription_receives_temporary_bytes`, `test_drain_never_activates_a_pending_session`, `test_normal_drain_keeps_active_session_until_its_deadline`, `test_blocked_user_is_revoked_immediately`, `test_limited_grace_fails_closed_when_panel_omits_usage`, `test_expired_grace_fails_closed_when_panel_omits_usage`, `test_disabling_kind_flag_does_not_interrupt_an_open_session`, `test_limited_grace_can_repeat_after_a_new_traffic_period`, `test_external_squad_is_detached_only_in_overlay_and_kept_in_snapshot`, `test_manual_panel_change_is_terminal_conflict_and_never_reapplied`, `test_unexpected_active_panel_state_fails_closed_to_billing`, `test_restore_conflict_is_terminal_instead_of_blocking_drain_forever`, `test_intentional_admin_expiry_is_suppressed_for_current_incident`, `test_grace_external_squad_policy_options`
- `tests/services/test_grace_access_sqlite_safety.py` — Python-модуль
  Классы: нет
  Функции: `test_sqlite_delete_guard_preserves_open_snapshot_and_cascades_completed_history`, `test_sqlite_predelete_noop_write_blocks_a_concurrent_pending_insert`, `test_sqlite_user_lock_blocks_a_new_subscription_during_full_delete`, `test_sqlite_delete_guard_also_blocks_user_cascade`
- `tests/services/test_guest_notification_switch.py` — Python-модуль
  Классы: нет
  Функции: `test_disabled_main_email_does_not_silence_credentials`, `test_enabled_main_email_sends_both`
- `tests/services/test_guest_purchase_campaign.py` — Python-модуль
  Классы: нет
  Функции: `test_campaign_is_attributed_after_delivery`, `test_purchase_without_slug_is_skipped`, `test_gift_is_not_attributed_to_recipient` — Гифт активирует получатель, а по рекламе приходил покупатель — записать, `test_attribution_failure_does_not_propagate` — Подписка уже доставлена и оплачена — упасть здесь значит уронить
- `tests/services/test_guest_purchase_daily_tariff.py` — Python-модуль
  Классы: нет
  Функции: `test_daily_tariff_one_day_is_priced_from_daily_price` — A 1-day purchase of a daily tariff must cost ``daily_price_kopeks``,, `test_daily_tariff_rejects_non_daily_period` — A daily tariff only sells a 1-day period; asking for 30 days must fail
- `tests/services/test_guest_purchase_fulfillment.py` — Python-модуль
  Классы: нет
  Функции: `test_fulfill_purchase_passes_purchase_context_to_user_resolution`
- `tests/services/test_guest_purchase_receipt_contact.py` — Python-модуль
  Классы: нет
  Функции: `test_regular_purchase_uses_the_fulfilment_user` — Обычная покупка: покупатель и получатель — одно лицо., `test_gift_receipt_goes_to_the_linked_buyer` — Подарок из кабинета: buyer_user_id проставлен — чек уходит дарителю., `test_guest_gift_falls_back_to_the_purchase_contact` — Подарок с лендинга: аккаунта дарителя нет, но его почта есть на покупке., `test_guest_gift_with_telegram_contact_has_no_usable_channel` — contact_value для telegram — это username, отправить по нему чек нельзя., `test_gift_never_leaks_the_recipient_contacts` — Инвариант: контакты одаряемого не попадают в чек ни в одной ветке.
- `tests/services/test_guest_purchase_referral_code.py` — Python-модуль
  Классы: нет
  Функции: `test_new_email_user_is_created_with_referral_code` — A landing-page email purchase must persist `referral_code` on the new, `test_new_telegram_user_is_created_with_referral_code` — Same guarantee for the telegram-username guest-purchase branch., `test_existing_email_user_without_referral_code_is_backfilled` — Legacy users created before the fix (with referral_code=NULL) must be, `test_existing_email_user_with_referral_code_is_not_overwritten` — Idempotency: if the user already has a referral_code, do not regenerate
- `tests/services/test_invite_only_settings.py` — Python-модуль
  Классы: нет
  Функции: `test_invite_only_defaults_are_backward_compatible`, `test_invite_only_settings_are_exposed_in_registration_access_category`
- `tests/services/test_kassa_ai_notifications.py` — Python-модуль
  Классы: нет
  Функции: `test_telegram_id_saved_before_commit` — Тест: проверяем что telegram_id сохраняется в локальную переменную ДО commit., `test_send_message_called_with_correct_params` — Тест: проверяем что bot.send_message вызывается с правильными параметрами., `test_no_send_when_no_telegram_id` — Тест: уведомление НЕ отправляется если нет telegram_id.
- `tests/services/test_lava_recurrent.py` — Python-модуль
  Классы: нет
  Функции: `test_order_id_roundtrip_marks_recurrent_charges` — Вебхук отличает списание по подписке от инвойса пополнения по префиксу., `test_resolve_product_charge_days`, `test_normalize_remote_status`, `test_reconcile_transport_failure_does_not_bury_pending` — Транспортный сбой (remote_missing=False) откладывает решение., `test_reconcile_status_rules`, `test_enable_creates_binding_and_disables_balance_autopay` — Рекуррент провайдера и balance-autopay взаимоисключающи., `test_enable_is_idempotent_and_restores_mutual_exclusion`, `test_enable_without_product_id_is_rejected` — У тарифа нет продукта Lava — понятная ошибка, а не молчаливый успех., `test_enable_rejects_zero_price_product` — Продукт без цены дал бы пустые регулярные «списания»., `test_cancel_marks_local_even_when_provider_fails` — Недоступность Lava не должна блокировать отмену навсегда., `test_charge_extends_subscription_without_touching_balance`, `test_repeated_charge_callback_does_not_extend_twice` — Ретрай вебхука Lava (до 5 раз) не должен продлевать дважды., `test_late_redelivery_of_older_charge_is_ignored` — last_charge_external_id хранит только последний id — сверяемся с транзакциями., `test_success_without_invoice_id_does_not_extend` — Без invoice_id идемпотентность не работает — продлевать нельзя., `test_charge_on_locally_cancelled_record_extends_but_keeps_cancelled` — Деньги взяты — продлеваем честно, но отмену пользователя не стираем., `test_failed_charge_moves_record_to_past_due`, `test_callback_for_unknown_order_is_reported`, `test_enabling_lava_cancels_live_platega_binding` — Два push-провайдера на одной подписке списывали бы дважды за цикл., `test_manual_extension_shifts_next_charge` — Ручное продление при живой привязке двигает автосписание Lava., `test_shift_next_charge_swallows_provider_errors` — Продление уже закоммичено — сбой Lava не должен всплывать., `test_zero_price_product_rejected_before_remote_subscribe` — Вся валидация — ДО subscribe: иначе остаётся живая привязка без локальной, `test_unavailable_product_list_does_not_guess_cadence` — Догадка «30 дней» навсегда исказила бы каденс годового продукта., `test_subscribe_without_subscription_id_is_cancelled_and_raises` — Без subscriptionId отключаются обе страховки — отменяем по orderId., `test_failed_charge_does_not_resurrect_cancelled_binding` — Протухший счёт по отменённой привязке не должен стирать отмену., `test_successful_charge_sets_next_charge_at` — Без этого колонка и строка «следующее списание» в кабинете мертвы., `test_charge_for_deleted_subscription_stops_further_charges` — Продлевать нечего и ретраев не будет — единственное полезное действие:, `test_real_lava_status_values_are_normalized` — Фактические значения Lava — activated/deactivated (примеры из спеки)., `test_purchase_rejects_trial_and_foreign_tariff` — Привязкой нельзя конвертировать триал и оплачивать чужой тариф.
- `tests/services/test_lava_service.py` — Python-модуль
  Классы: нет
  Функции: `service` — LavaService with deterministic credentials., `test_outgoing_signature_is_in_header_not_body` — REGRESSION: signature must be in `Signature` HTTP header, NOT body field., `test_outgoing_signature_is_hmac_of_raw_body_bytes` — Signature header value = HMAC-SHA256(raw_body_bytes, LAVA_SECRET_KEY) hex., `test_outgoing_body_uses_payload_key_order_not_sorted` — We must NOT sort keys outgoing — sorted body + HMAC of raw would not match., `test_http_error_raises_lava_api_error` — 4xx/5xx must surface as LavaAPIError with status and message., `test_webhook_verify_accepts_raw_body_hmac` — Modern shops sign raw body — verify must accept., `test_webhook_verify_accepts_canonical_json_hmac` — Legacy PHP-SDK shops sign canonical (sorted-keys) JSON — verify must still accept., `test_webhook_verify_rejects_unknown_signature`, `test_webhook_verify_rejects_empty_signature`, `test_webhook_verify_rejects_missing_webhook_secret` — No webhook secret configured → fail closed, not open., `test_webhook_verify_handles_garbage_body` — Non-JSON body falls through to raw-only path, then mismatch → False (no crash)., `test_strip_url_query_removes_query_and_fragment` — Lava Business rejects success/fail URLs with a query string (HTTP 422 'ошибочный
- `tests/services/test_legal_consent.py` — Python-модуль
  Классы: нет
  Функции: `test_both_documents_required_by_default`, `test_setting_disables_the_gate`, `test_prechecked_flag_is_reported`, `test_document_hidden_from_web_is_not_required` — Документ только для бота нельзя прочитать в кабинете — галочки по нему нет., `test_empty_document_is_not_required`, `test_no_documents_at_all_disables_the_gate` — Иначе установка без юр. документов заблокировала бы регистрацию всем., `test_broken_document_read_does_not_block_login`, `test_missing_documents_reports_unchecked_boxes`, `test_record_consent_writes_a_row_per_document`, `test_record_consent_with_no_documents_is_a_noop`, `test_gate_rejects_missing_consent`, `test_gate_passes_with_full_consent`, `test_gate_is_transparent_when_disabled` — Выключенная настройка не должна ломать регистрацию без чекбоксов.
- `tests/services/test_log_level_resolver.py` — Python-модуль
  Классы: нет
  Функции: `test_resolves_canonical_uppercase_names`, `test_resolves_lowercase_names` — REGRESSION: ``LOG_LEVEL=warning`` from .env must NOT return, `test_resolves_mixed_case_and_whitespace` — Whitespace and mixed-case variants normalize to the canonical level., `test_lowercase_does_not_return_the_logger_function` — The exact failure mode: the ``logging`` module has BOTH, `test_unknown_or_empty_falls_back_to_default`, `test_non_string_input_falls_back_to_default` — The resolver accepts only str input. Anything else → default., `test_default_argument_is_respected` — Custom default values flow through the fallback paths., `test_resolver_output_is_acceptable_to_structlog` — REGRESSION smoke: ``make_filtering_bound_logger`` must accept
- `tests/services/test_main_menu_button_cabinet_mode.py` — Python-модуль
  Классы: нет
  Функции: `test_back_to_menu_is_not_in_cabinet_path_mapping` — REGRESSION: ``back_to_menu`` must NOT be a key in, `test_back_to_menu_is_not_in_cabinet_style_mapping` — Dead config caught: if ``back_to_menu`` were styled per-section, `test_build_miniapp_or_callback_button_falls_through_for_back_to_menu` — Belt-and-suspenders: even in cabinet mode, calling, `test_build_main_menu_button_returns_callback_button` — The dedicated helper always returns a callback button. No mode, `test_build_main_menu_button_body_does_not_reference_cabinet_mode` — Pin the design contract: ``build_main_menu_button`` ignores, `test_topup_success_keyboard_main_menu_button_is_callback` — Source-level pin: ``app/services/payment/common.py`` must use, `test_no_other_callsite_wraps_back_to_menu_in_miniapp_helper` — AST-based scan: no callsite anywhere in ``app/`` may invoke, `test_home_button_key_is_not_in_broadcast_cabinet_path_mapping` — Structural pin (per architect-review top priority): the foot-gun, `test_home_button_key_is_not_in_cabinet_miniapp_button_keys` — Belt-and-suspenders set-membership pin., `test_topup_success_keyboard_renders_callback_main_menu_button_in_cabinet_mode` — REGRESSION (behavioural): ``build_topup_success_keyboard``,
- `tests/services/test_maintenance_restart.py` — Python-модуль
  Классы: `TestCacheDoesNotResurrectManualMode` (3 методов), `TestEnvPinning` (2 методов), `TestPanelWarnsWhenEnvLocked` (2 методов), `TestReferralKeysAreEditable` (2 методов)
  Функции: `service`
- `tests/services/test_manual_topup_service.py` — Python-модуль
  Классы: нет
  Функции: `test_credits_balance_and_records_transaction`, `test_repeat_with_same_key_does_not_credit_twice` — Ретрай агента после таймаута не должен стать вторым начислением., `test_same_key_with_other_amount_is_a_conflict`, `test_same_key_for_another_user_is_a_conflict` — Ключи глобальны: переиспользованный номер тикета не должен «зачислить» чужому., `test_different_keys_credit_independently`, `test_without_key_every_call_credits` — Без ключа защиты нет — это осознанный режим, а не забытая проверка., `test_key_is_namespaced_and_does_not_clash_with_plain_manual_rows` — Обычные ручные корректировки пишут external_id=NULL и не мешают ключам., `test_rejects_non_positive_amount`, `test_notification_failure_does_not_fail_the_deposit` — Упавший Telegram не должен ни откатывать деньги, ни отдавать вызывающему ошибку., `test_bonuses_disabled_skips_post_topup_pipeline`, `test_notify_user_gate_is_respected`, `test_email_notification_not_duplicated_by_cart_helper` — Письмо шлёт _notify_user; общий пост-топап хелпер не должен слать второе., `test_external_id_fits_the_column` — Ключ из схемы (<=200) плюс префикс обязан влезать в external_id String(255)., `test_deposit_route_is_registered` — Роут должен реально висеть на users-роутере (он включается с префиксом /users)., `test_deposit_route_rejects_amount_above_limit`, `test_deposit_route_conflicts_on_key_reuse_with_other_amount` — Тот же ключ с другой суммой — ошибка вызывающего, а не тихий «дубликат».
- `tests/services/test_menu_layout_service.py` — Python-модуль
  Классы: нет
  Функции: `test_build_button_connect_direct_mode_with_url` — Тест: кнопка connect с open_mode=direct и валидным URL должна создавать WebAppInfo., `test_build_button_connect_direct_mode_with_subscription_url` — Тест: кнопка connect с open_mode=direct должна получать URL из подписки., `test_build_button_connect_callback_mode` — Тест: кнопка connect с open_mode=callback должна создавать callback кнопку., `test_build_button_connect_direct_mode_fallback_to_callback` — Тест: кнопка connect с open_mode=direct без URL должна fallback на callback.
- `tests/services/test_monitoring_notification_switches.py` — Python-модуль
  Классы: нет
  Функции: `test_global_switch_stops_monitoring_notification_queries`, `test_expiration_state_updates_even_when_notifications_are_disabled`
- `tests/services/test_monitoring_send_timeout.py` — Python-модуль
  Классы: нет
  Функции: `test_text_send_times_out_and_skips`, `test_photo_send_times_out_and_skips`
- `tests/services/test_mulenpay_guest_client.py` — Python-модуль
  Классы: нет
  Функции: `anyio_backend`, `test_guest_email_contact_is_forwarded`, `test_guest_telegram_contact_is_not_sent_as_client` — @username — не тот контакт, который документирован примером с email., `test_guest_contact_lookup_failure_does_not_block_payment`, `test_guest_missing_purchase_yields_no_client`
- `tests/services/test_mulenpay_service_adapter.py` — Python-модуль
  Классы: нет
  Функции: `anyio_backend`, `test_is_configured`, `test_format_and_signature`, `test_create_payment_success`, `test_create_payment_includes_client`, `test_create_payment_omits_empty_client` — Пустое значение не должно уходить в payload как пустая строка., `test_create_payment_truncates_client`, `test_create_payment_failure`, `test_get_payment`, `test_request_success`, `test_request_retries_on_server_error`, `test_request_returns_none_after_timeouts`, `test_request_reraises_cancelled`
- `tests/services/test_multi_tariff_device_uuid.py` — Python-модуль
  Классы: нет
  Функции: `test_bot_multi_tariff_uses_subscription_panel_id`, `test_bot_multi_tariff_null_sub_does_not_fall_back_to_user` — The bleed: a null sub id must NOT borrow the user's (another tariff's) panel user., `test_bot_single_tariff_falls_back_to_user`, `test_bot_no_subscription_uses_user_panel_id`, `test_bot_never_returns_legacy_uuid` — Пустой remnawave_id не должен подмениться легаси-uuid'ом ни в одном режиме., `test_cabinet_multi_tariff_uses_subscription_panel_id`, `test_cabinet_multi_tariff_null_sub_no_user_fallback`, `test_cabinet_single_tariff_uses_user_panel_id`, `test_cabinet_no_subscription_uses_user_panel_id`, `test_cabinet_never_returns_legacy_uuid`, `test_both_resolvers_agree_and_return_numeric_ids` — В multi-tariff кабинет и бот обязаны резолвить одинаково и отдавать именно число., `test_deterministic_suffix_distinct_per_subscription_when_short_id_empty` — Two tariffs of one user with empty/legacy short_id must still build DISTINCT
- `tests/services/test_nalogo_pending_verification.py` — Python-модуль
  Классы: нет
  Функции: `test_pending_verification_entry_keeps_user_email` — Почта покупателя обязана лечь в очередь — иначе при пересылке её нет., `test_retry_delivers_receipt_to_the_buyer` — Чек, созданный ручной пересылкой, доставляется покупателю., `test_retry_recovers_amount_from_legacy_entry` — Старые записи очереди без amount_kopeks — сумма берётся из amount., `test_retry_keeps_receipt_when_delivery_fails` — Доставка упала — чек в ФНС уже создан, из очереди он всё равно уходит., `test_retry_without_bot_still_creates_receipt` — Без bot чек всё равно создаётся — старое поведение не ломаем.
- `tests/services/test_nalogo_receipt_notifications.py` — Python-модуль
  Классы: нет
  Функции: `test_sends_to_user_and_duplicates_to_admin_topic`, `test_no_telegram_id_admin_only_with_guest_mark`, `test_user_send_failure_does_not_block_admin_duplicate` — Юзер заблокировал бота — админ-топик всё равно получает чек., `test_no_print_url_sends_nothing`, `test_no_admin_chat_user_only`, `test_get_receipt_print_url_builds_v1_link` — Ссылка обязана содержать /v1 — библиотечный print_url() строит без него, `test_receipt_delivered_as_photo_when_download_succeeds` — lknpd недоступен клиентам за VPN — при успешном серверном скачивании чек, `test_receipt_delivered_as_document_for_pdf`, `test_download_failure_falls_back_to_link` — Сбой скачивания (сеть/503 ФНС) не ломает доставку — уходит текст со ссылкой., `test_telegram_rejects_file_falls_back_to_link` — Telegram отверг сам файл — чек обязан дойти, поэтому уходит ссылкой., `test_blocked_user_is_not_retried_as_message` — Юзер заблокировал бота — это не проблема файла, повторять текстом бессмысленно., `test_download_rejects_html_error_page` — ФНС отдаёт HTML-заглушку с кодом 200 — её нельзя слать как «чек»., `test_download_accepts_image_and_pdf`, `test_download_reads_full_body_not_just_first_network_chunk` — Тело длиннее одной сетевой порции обязано склеиться целиком., `test_download_rejects_oversized_receipt` — Предохранитель от вычитывания мусора в память., `test_email_only_user_gets_receipt_by_email` — У покупателя нет Telegram (кабинет/лендинг) — чек уходит на почту файлом., `test_blocked_bot_falls_back_to_email_from_db` — Юзер заблокировал бота — почту берём из БД, чек уходит письмом., `test_telegram_rejected_file_falls_back_to_email_with_attachment` — Telegram отверг файл — ссылка в чате не считается доставкой чека., `test_delivered_to_telegram_skips_email` — Чек дошёл в Telegram — письмо не дублируем., `test_email_not_sent_when_smtp_unconfigured` — SMTP не настроен — не падаем, чек остаётся хотя бы в админ-топике., `test_routine_delivery_failures_are_warnings_not_errors` — «Чат не найден» и флуд-контроль — штатные исходы рассылки, не сбои кода., `test_receipt_email_is_branded_and_uses_admin_override` — Чек на почту раньше был зашит в код (по-русски, без обёртки) — теперь это, `test_receipt_filename_comes_from_setting` — Имя файла чека — настройка NALOGO_RECEIPT_FILENAME; битый шаблон не ломает отправку.
- `tests/services/test_new_gateways_finalize.py` — Python-модуль
  Классы: `FakeSession` (4 методов), `FakeUser` (2 методов), `FakePayment` (1 методов), `FakeTransaction` (1 методов)
  Функции: `anyio_backend`, `wired` — Подменяет всё окружение зачисления и отдаёт наблюдаемые вызовы., `test_credits_exact_amount_and_creates_transaction`, `test_does_not_credit_twice_when_transaction_linked` — Платёж уже связан с транзакцией — выходим до всякого зачисления., `test_existing_transaction_with_credited_marker_does_not_double_credit` — Транзакция уже есть и баланс уже начислен — второй раз не начисляем., `test_repeated_finalize_credits_only_once` — Два прохода подряд (вебхук + сверка по API) дают одно зачисление., `test_guest_purchase_short_circuits_without_crediting_balance` — Покупка с лендинга выдаёт товар, а не пополняет баланс., `test_missing_user_refuses_to_credit`, `test_referral_failure_does_not_block_crediting` — Реферальная логика вторична: её падение не должно отменять пополнение., `test_tabpay_sandbox_payment_never_credits` — У TabPay есть песочница: такой платёж не должен доходить до зачисления.
- `tests/services/test_new_gateways_registration.py` — Python-модуль
  Классы: нет
  Функции: `anyio_backend`, `test_hidden_when_disabled`, `test_generic_button_when_no_sub_methods` — Саб-методы не настроены — показываем одну кнопку, способ выберет плательщик., `test_sub_methods_replace_generic_button` — Включены карта и СБП — общей кнопки быть не должно, иначе их три., `test_availability_predicates`, `test_present_in_registries`, `test_sub_options_declared_for_cabinet`, `test_guest_method_split` — Кабинет кодирует выбор суффиксом — база и опция должны разделяться., `test_guest_payment_routes_to_gateway`, `test_guest_payment_refused_when_gateway_disabled`, `test_pending_predicate`, `test_enabled_in_verification_when_configured`
- `tests/services/test_notification_delivery_preferences.py` — Python-модуль
  Классы: нет
  Функции: `test_global_switch_blocks_regular_notifications`, `test_transactional_messages_bypass_global_switch`, `test_per_user_preferences_block_matching_notifications`, `test_referral_switch_is_enforced_by_unified_delivery`, `test_blocked_user_can_receive_the_ban_notification`, `test_blocked_user_does_not_receive_unrelated_notifications`, `test_promo_opt_out_suppresses_the_message_but_keeps_the_discount` — Отказ от промо-уведомлений глушит СООБЩЕНИЕ, но не отбирает скидку.
- `tests/services/test_overpay_certificate_service.py` — Python-модуль
  Классы: нет
  Функции: `cert_and_key`, `test_validate_p12_with_password`, `test_validate_p12_without_password`, `test_validate_p12_wrong_passphrase`, `test_validate_p12_garbage_bytes`, `test_validate_p12_oversize`, `stubbed_env`, `test_store_certificate`, `test_store_certificate_is_readable_by_owner_only` — p12 содержит приватный ключ — на диске он должен быть доступен только владельцу., `test_store_certificate_env_locked_warning`, `test_store_certificate_invalid_writes_nothing`, `test_delete_certificate`, `test_get_status`
- `tests/services/test_overpay_service.py` — Python-модуль
  Классы: `FakeResponse` (2 методов), `FakeClient` (3 методов), `InvalidJsonResponse` (1 методов)
  Функции: `service`, `test_create_payment_uses_explicit_project_id`, `test_create_payment_defaults_to_settings_project_id`, `test_create_payment_s2s_payload_contract`, `test_create_payment_s2s_omits_client_without_email`, `test_create_payment_s2s_raises_on_error`, `test_wait_for_redirect_link_polls_until_link`, `test_wait_for_redirect_link_survives_invalid_json`, `test_wait_for_redirect_link_stops_on_declined`, `test_wait_for_redirect_link_gives_up_after_attempts`
- `tests/services/test_overpay_settings.py` — Python-модуль
  Классы: нет
  Функции: `test_new_settings_exist_with_safe_defaults`, `test_new_keys_resolve_to_overpay_category`, `test_terminal_id_falls_back_to_project_id`, `test_int_enabled_requires_flag_and_rate`, `test_sbp_direct_qr_requires_server_ip`
- `tests/services/test_pal24_service_adapter.py` — Python-модуль
  Классы: `StubPal24Client` (5 методов)
  Функции: `anyio_backend`, `test_create_bill_success`, `test_create_bill_requires_configuration`, `test_get_bill_payments`, `test_parse_callback_success`, `test_parse_callback_missing_fields`, `test_convert_to_kopeks_and_expiration`
- `tests/services/test_paritypay_client.py` — Python-модуль
  Классы: `RecordingService` (2 методов)
  Функции: `anyio_backend`, `test_create_invoice_sends_rubles_not_kopeks` — 125000 копеек обязаны уйти как 1250.0 — иначе счёт будет на 125 000 ₽., `test_create_invoice_request_shape`, `test_create_invoice_never_sends_subscription_block` — Подписки не оформляем: блок subscription не должен появляться никогда., `test_create_invoice_omits_empty_optionals`, `test_create_invoice_rejects_response_without_link`, `test_create_invoice_rejects_response_without_id`, `test_get_invoice_by_id_and_by_order_id`, `test_get_invoice_prefers_id_over_order_id` — Спека: передаётся ОДИН из параметров, не оба., `test_get_invoice_without_identifiers_raises`, `test_headers_carry_shop_and_secret_key`, `test_base_url_strips_slash_and_falls_back`, `test_error_message_uses_error_field` — Формат ошибки провайдера — объект {"error": "текст"}., `test_request_404_allowed_returns_none`, `test_request_422_raises_business_error`, `test_request_400_raises`, `test_connection_error_and_timeout_become_network_error`
- `tests/services/test_paritypay_signature.py` — Python-модуль
  Классы: нет
  Функции: `test_signature_payload_sorts_keys_and_joins_values`, `test_valid_signature_accepted`, `test_tampered_amount_breaks_signature`, `test_wrong_key_and_empty_signature_rejected`, `test_blank_secret_fails_closed`, `test_number_text_from_json_is_preserved` — Числа не должны переформатироваться: иначе подпись отправителя не сойдётся., `test_parse_rejects_non_object_and_broken_json`, `test_amount_to_kopeks`, `test_kopeks_to_amount`, `test_amount_roundtrip_has_no_float_drift` — Через float 0.1+0.2 ломается; здесь Decimal и обратный путь обязан сходиться.
- `tests/services/test_payment_common.py` — Python-модуль
  Классы: нет
  Функции: `anyio_backend`, `test_send_payment_success_notification_recovers_missing_greenlet`
- `tests/services/test_payment_method_config_overpay.py` — Python-модуль
  Классы: нет
  Функции: `test_sub_options_without_int`, `test_sub_options_with_int`, `test_method_defaults_use_dynamic_sub_options`
- `tests/services/test_payment_provider_configured.py` — Python-модуль
  Классы: нет
  Функции: `test_every_listed_provider_has_both_predicates` — Список ниже — контракт с create_payment_router, а не украшение., `test_enabled_is_flag_and_configured`, `test_enabled_is_flag_when_credentials_are_present` — С заполненными кредами включение решает только флаг., `test_missing_credential_disables_the_provider` — Убрали любую креду — провайдер не настроен и не включён., `test_tribute_has_a_configured_predicate` — У Tribute нет is_*_enabled, но маршруту нужен тот же признак.
- `tests/services/test_payment_service_cispay.py` — Python-модуль
  Классы: `DummySession` (3 методов), `DummyLocalPayment` (1 методов), `FakeCisPayPayment` (1 методов), `StubCisPayService` (2 методов)
  Функции: `anyio_backend`, `test_create_cispay_payment_success`, `test_create_cispay_payment_sbp_sub_method`, `test_create_cispay_payment_respects_amount_limits`, `test_create_cispay_payment_disabled`, `test_process_cispay_callback_paid_finalizes`, `test_process_cispay_callback_amount_mismatch`, `test_process_cispay_callback_already_paid_is_idempotent`, `test_process_cispay_callback_sticky_terminal_status` — Провайдер не может «починить» отклонённый платёж повторным вебхуком., `test_process_cispay_callback_missing_amount_does_not_credit` — PAID без amount: зачислять нечего сверять — платёж остаётся pending под ретрай., `test_process_cispay_callback_unparseable_amount_is_mismatch`, `test_process_cispay_callback_missing_fields`, `test_process_cispay_callback_non_paid_status_updates`, `test_generic_method_falls_back_to_sbp_when_card_disabled` — SBP-only магазин: генерик-метод обязан слать SBP, иначе cisPay отклонит платёж., `test_is_cispay_enabled_rejects_blank_credentials` — Пустая строка ключа не должна включать шлюз — иначе HMAC вебхука подделывается., `test_verify_webhook_signature_blank_key_fails_closed`, `test_verify_webhook_signature_valid`, `test_verify_webhook_signature_invalid`, `test_verify_webhook_signature_tampered_body`
- `tests/services/test_payment_service_cryptobot.py` — Python-модуль
  Классы: `DummySession` (4 методов), `DummyLocalPayment` (1 методов), `StubCryptoBotService` (2 методов)
  Функции: `anyio_backend`, `test_create_cryptobot_payment_success`, `test_create_cryptobot_payment_returns_none_when_service_missing`, `test_create_cryptobot_payment_handles_empty_response`
- `tests/services/test_payment_service_heleket.py` — Python-модуль
  Классы: `DummySession` (4 методов), `DummyLocalPayment` (1 методов), `StubHeleketService` (4 методов)
  Функции: `anyio_backend`, `test_create_heleket_payment_success`, `test_create_heleket_payment_returns_none_without_service`, `test_create_heleket_payment_handles_empty_response`, `test_sync_heleket_payment_status_success`, `test_sync_heleket_payment_status_without_response`, `test_sync_heleket_payment_status_history_fallback`
- `tests/services/test_payment_service_modularity.py` — Python-модуль
  Классы: нет
  Функции: `test_payment_service_mro_contains_all_mixins` — Убеждаемся, что сервис действительно включает все mixin-классы., `test_payment_service_exposes_provider_methods` — Каждый mixin обязан добавить публичный метод в PaymentService.
- `tests/services/test_payment_service_mulenpay.py` — Python-модуль
  Классы: `DummySession` (5 методов), `DummyLocalPayment` (1 методов), `StubMulenPayService` (2 методов)
  Функции: `anyio_backend`, `test_create_mulenpay_payment_success`, `test_build_mulenpay_client_sends_only_verified_email`, `test_build_mulenpay_client_truncates_overlong_value`, `test_create_mulenpay_payment_skips_lookup_for_guest` — Гостевой платёж не ходит в БД за пользователем, которого нет., `test_create_mulenpay_payment_survives_contact_lookup_failure` — Контакт — необязательное поле и не имеет права сорвать оплату., `test_create_mulenpay_payment_handles_missing_user`, `test_explicit_client_wins_over_lookup` — Гостевой поток передаёт контакт покупателя явно — лукап при этом не нужен., `test_create_mulenpay_payment_respects_amount_limits`, `test_create_mulenpay_payment_returns_none_without_service`, `test_process_mulenpay_callback_avoids_duplicate_transactions`
- `tests/services/test_payment_service_overpay.py` — Python-модуль
  Классы: `DummySession` (3 методов), `DummyUser` (1 методов), `DummyLocalPayment` (1 методов), `StubOverpayService` (4 методов)
  Функции: `overpay_settings`, `test_card_option_routes_to_card_terminal`, `test_int_option_converts_to_eur`, `test_int_option_below_min_eur_rejected`, `test_int_option_rejected_when_disabled`, `test_fps_direct_qr_uses_s2s_flow`, `test_fps_direct_qr_without_link_returns_none`, `test_fps_direct_qr_s2s_error_falls_back_to_form`, `test_fps_without_direct_qr_uses_form`, `test_legacy_call_without_option_keeps_old_behavior`, `test_guest_payment_forwards_int_option`, `test_status_map_extended`
- `tests/services/test_payment_service_pal24.py` — Python-модуль
  Классы: `DummySession` (1 методов), `DummyLocalPayment` (1 методов), `StubPal24Service` (5 методов)
  Функции: `anyio_backend`, `test_create_pal24_payment_success`, `test_create_pal24_payment_default_method`, `test_create_pal24_payment_limits_and_configuration`, `test_create_pal24_payment_handles_api_errors`, `test_get_pal24_payment_status_updates_from_remote`
- `tests/services/test_payment_service_paritypay.py` — Python-модуль
  Классы: `DummySession` (3 методов), `DummyLocalPayment` (1 методов), `FakeParityPayPayment` (1 методов), `StubParityPayService` (2 методов), `RecordingClient` (3 методов), `StubInvoiceApi` (2 методов)
  Функции: `anyio_backend`, `test_create_sends_rubles_not_kopeks` — API принимает рубли: 125000 копеек должны уйти как 1250.0, а не 125000., `test_create_with_sbp_sub_method`, `test_generic_method_pins_the_only_enabled_option`, `test_create_respects_limits_and_disabled`, `test_network_failure_reuses_existing_invoice`, `test_network_failure_recreates_when_nothing_found`, `test_duplicate_order_id_422_returns_existing` — «Order id is not unique» — счёт уже есть, создавать нечего., `test_other_422_is_not_retried` — Недопустимый service — повторами не лечится., `test_validation_400_is_not_retried`, `test_callback_paid_credits_and_stores_credited` — Сумма приходит СТРОКОЙ в рублях — сверка обязана сойтись с копейками., `test_callback_amount_mismatch_does_not_credit`, `test_callback_unparseable_amount_leaves_pending`, `test_callback_idempotent_by_id_and_status`, `test_late_payment_after_expired_is_credited` — QR СБП оплатили после истечения: PAID поверх EXPIRED обязан зачислиться., `test_callback_error_status_recorded`, `test_refund_after_paid_is_recorded`, `test_unknown_order_is_acknowledged`, `test_subscription_notification_is_acknowledged` — Без 200 провайдер повторит доставку пять раз — подтверждаем и логируем., `test_subscription_charge_invoice_is_acknowledged` — Списание по подписке: order_id вида "{sub_id}_2" нашему счёту не принадлежит., `test_callback_missing_fields`, `test_is_paritypay_enabled_requires_all_three_credentials`, `test_api_check_credits_paid_invoice` — Уведомление потерялось — сверка обязана закрыть оплаченный счёт., `test_api_check_falls_back_to_order_id` — Если id процессинга не сохранился, ищем по своему order_id., `test_api_check_amount_mismatch_blocks_credit`, `test_api_check_unparseable_amount_blocks_credit`, `test_api_check_syncs_non_paid_status`, `test_api_check_handles_missing_invoice` — 404 у провайдера — счёта нет, зачислять нечего., `test_api_check_survives_api_error` — Провайдер недоступен — возвращаем текущее состояние, а не падаем., `test_api_check_skips_already_paid_and_final` — К провайдеру не ходим: платёж уже закрыт., `test_api_check_unknown_order_returns_none`
- `tests/services/test_payment_service_platega.py` — Python-модуль
  Классы: `DummySession` (2 методов), `DummyLocalPayment` (1 методов), `StubPlategaService` (3 методов)
  Функции: `anyio_backend`, `test_create_platega_payment_success`, `test_create_platega_payment_respects_limits_and_configuration`, `test_create_platega_payment_handles_service_errors`, `test_get_platega_active_methods_parses_and_filters`, `test_get_platega_active_methods_returns_default`, `test_platega_method_display_helpers`
- `tests/services/test_payment_service_stars.py` — Python-модуль
  Классы: `DummyBot` (3 методов), `DummySession` (4 методов), `DummySubscription` (1 методов), `DummyUser` (1 методов), `DummyTransaction` (1 методов), `DummySubscriptionService` (2 методов)
  Функции: `anyio_backend` — Ограничиваем anyio тесты только бэкендом asyncio., `test_create_stars_invoice_calculates_stars` — Количество звёзд должно рассчитываться по курсу с округлением вниз и нижним порогом 1., `test_create_stars_invoice_enforces_minimum_star` — При слишком маленькой сумме минимум должен составлять 1 звезду., `test_create_stars_invoice_uses_explicit_stars` — Если передано значение stars_amount, функция должна использовать его напрямую., `test_create_stars_invoice_rejects_invalid_rate` — Отрицательный или нулевой курс должен приводить к исключению., `test_create_stars_invoice_requires_bot` — Без экземпляра бота и stars_service функция должна отказывать., `test_process_stars_payment_simple_subscription_success` — Оплата простой подписки через Stars активирует pending подписку и уведомляет пользователя.
- `tests/services/test_payment_service_tabpay.py` — Python-модуль
  Классы: `DummySession` (3 методов), `DummyLocalPayment` (1 методов), `FakeTabPayPayment` (1 методов), `StubTabPayService` (2 методов), `RecordingTabPayService` (3 методов)
  Функции: `anyio_backend`, `test_create_tabpay_payment_success`, `test_create_tabpay_payment_sbp_sub_method`, `test_generic_method_pins_the_only_enabled_option` — SBP-only магазин: генерик-метод обязан слать SBP, иначе TabPay ответит 409., `test_create_tabpay_payment_respects_amount_limits`, `test_create_tabpay_payment_disabled`, `test_network_failure_reuses_existing_payment` — Ответ потерян, но платёж создался: берём его payUrl, а не плодим второй., `test_network_failure_recreates_when_nothing_found` — Платежа нет — только тогда создаём заново, с тем же orderId., `test_duplicate_order_id_returns_existing_payment` — 409 на дубль orderId — платёж уже есть, повторять создание нечем., `test_validation_error_is_not_retried` — 400 — наша ошибка в запросе: ни сверки, ни повтора., `test_server_error_reconciles_before_recreating` — 5xx — исход неизвестен, действуем как при сетевом сбое., `test_callback_success_finalizes`, `test_callback_is_idempotent_by_id_and_status` — Повторная доставка того же события не зачисляет баланс второй раз., `test_late_payment_after_expired_is_credited` — QR СБП оплатили после таймаута: EXPIRED -> SUCCESS обязан зачислиться., `test_callback_amount_mismatch_does_not_credit`, `test_callback_missing_amount_does_not_credit` — SUCCESS без amountKopecks: сверять нечего — оставляем платёж под ретрай., `test_callback_test_button_webhook_touches_nothing` — Кнопка «Отправить тестовый вебхук»: id вида test-..., orderId не наш., `test_callback_non_final_status_updates_record`, `test_callback_refund_after_success_is_recorded`, `test_callback_ignores_events_after_final_status` — Из CANCELED/REFUNDED переходов нет — платёж не «чинится» новым вебхуком., `test_callback_unknown_status_is_acknowledged`, `test_callback_missing_fields`, `test_callback_unknown_order_is_acknowledged` — Чужой orderId повторами не появится — подтверждаем доставку., `test_verify_webhook_signature_valid`, `test_verify_webhook_signature_rejects_stale_timestamp` — Переигрывание перехваченного вебхука отсекается окном свежести., `test_verify_webhook_signature_timestamp_is_part_of_payload` — Подпись считается от «метка.тело», а не от одного тела., `test_verify_webhook_signature_rejects_tampered_body`, `test_verify_webhook_signature_rejects_wrong_key_and_empty`, `test_verify_webhook_signature_blank_secret_fails_closed` — Пустой секрет — подпись подделал бы кто угодно, поэтому отказ., `test_is_tabpay_enabled_requires_both_secrets`, `test_api_check_does_not_credit_test_payment` — Магазин-песочница: деньги не двигались, зачислять нечего., `test_finalize_refuses_test_payment` — Единая точка зачисления обязана сама отсекать тестовые платежи., `test_callback_records_sandbox_payment_without_crediting` — Платёж песочницы — наш, его исход фиксируем, но баланс не трогаем., `test_callback_treats_stringy_test_flag_as_test` — Нестрогое значение флага толкуем в сторону «не зачислять»., `test_create_marks_test_only_on_explicit_yes` — Боевой платёж не должен помечаться тестовым из-за нестрогого isTest., `test_create_marks_test_on_explicit_yes`, `test_webhook_credits_unless_test_is_explicit_yes` — Боевой вебхук обязан зачислять: нераспознанный флаг не повод съесть оплату., `test_api_check_credits_production_payment` — Сверка по API тоже обязана зачислять боевой платёж., `test_finalize_credits_production_payment` — Страж в точке зачисления не должен срабатывать на боевом платеже.
- `tests/services/test_payment_service_tribute.py` — Python-модуль
  Классы: нет
  Функции: `anyio_backend`, `test_create_tribute_payment_requires_enabled`, `test_create_tribute_payment_success`, `test_verify_tribute_webhook_signature`, `test_verify_tribute_webhook_returns_false_without_key`
- `tests/services/test_payment_service_wata.py` — Python-модуль
  Классы: `DummySession` (3 методов), `DummyLocalPayment` (1 методов), `StubWataService` (2 методов), `DummyWataPayment` (1 методов)
  Функции: `anyio_backend`, `test_wata_service_format_datetime_accepts_utc`, `test_wata_service_parse_datetime_returns_naive_utc`, `test_create_wata_payment_success`, `test_create_wata_payment_respects_amount_limits`, `test_create_wata_payment_returns_none_without_service`, `test_process_wata_webhook_updates_status`, `test_process_wata_webhook_finalizes_paid`, `test_process_wata_webhook_returns_false_when_payment_missing`
- `tests/services/test_payment_service_webhooks.py` — Python-модуль
  Классы: `DummyBot` (2 методов), `FakeScalarResult` (6 методов), `FakeResult` (9 методов), `FakeSession` (8 методов)
  Функции: `anyio_backend`, `test_process_mulenpay_callback_success`, `test_process_cryptobot_webhook_success`, `test_process_heleket_webhook_success`, `test_process_yookassa_webhook_success`, `test_process_yookassa_webhook_uses_remote_status`, `test_process_yookassa_webhook_handles_cancellation`, `test_process_yookassa_webhook_restores_missing_payment`, `test_process_yookassa_webhook_missing_metadata`, `test_process_yookassa_webhook_missing_id`, `test_process_yookassa_webhook_skip_ip_rejects_unconfirmed` — Fail-closed: с YOOKASSA_SKIP_IP_CHECK и без подтверждения от API YooKassa, `test_process_yookassa_webhook_skip_ip_rejects_on_api_timeout` — Fail-closed при таймауте API в skip-режиме: нет подтверждения — нет начисления., `test_process_yookassa_webhook_skip_ip_credits_when_confirmed` — В skip-режиме подтверждённый API платёж начисляется как обычно;, `test_process_yookassa_webhook_default_mode_failopen_preserved` — Регрессия: при выключенном флаге (дефолт) отсутствие подтверждения от API, `test_process_pal24_callback_success`, `test_get_pal24_payment_status_auto_finalize`, `test_process_pal24_callback_payment_not_found`
- `tests/services/test_payment_service_yookassa.py` — Python-модуль
  Классы: `DummySession` (3 методов), `DummyLocalPayment` (1 методов), `StubYooKassaService` (3 методов)
  Функции: `anyio_backend` — Запускаем async-тесты на asyncio, чтобы избежать зависимостей trio., `test_create_yookassa_payment_success` — Успешное создание платежа формирует корректные метаданные и локальную запись., `test_create_yookassa_payment_returns_none_when_service_missing` — Если сервис не настроен, метод должен вернуть None., `test_create_yookassa_payment_handles_error_response` — Ответ с ключом error должен приводить к None без записи в БД., `test_create_yookassa_sbp_payment_success` — Проверяем SBP-сценарий, включая передачу confirmation_token., `test_create_yookassa_sbp_payment_returns_none_on_error` — Ошибочный ответ СБП не должен создавать запись.
- `tests/services/test_platega_callback_casing.py` — Python-модуль
  Классы: `TestIsSubscriptionCallback` (6 методов), `TestReadCallbackFields` (6 методов)
  Функции: нет
- `tests/services/test_platega_recurrent_cancel_hooks.py` — Python-модуль
  Классы: нет
  Функции: `test_cancel_by_subscription_calls_platega_and_marks_cancelled` — cancel_platega_recurring_for_subscription: находит активную запись по, `test_cancel_best_effort_swallows_platega_error` — Ошибка Platega API при отмене (сеть/5xx) не должна пробрасываться, `test_cancel_pending_without_platega_id_skips_api` — PENDING-запись, которая не успела получить platega_subscription_id, `test_cancel_safe_works_even_when_gate_off` — Гейт выключен (``PLATEGA_RECURRENT_ENABLED=False``) — отмена ВСЁ РАВНО, `test_cancel_safe_commit_false_defers_to_caller_transaction` — ``commit=False`` (мерж аккаунтов): локальный CANCELLED уходит flush'ем, `test_cancel_safe_gate_on_cancels_active_record` — Гейт включён + есть активная запись + ``PlategaService.cancel_subscription``, `test_cancel_safe_never_raises_on_platega_error` — Даже если ``PlategaService.cancel_subscription`` бросает исключение, `test_enable_sbp_recurring_raises_when_gate_off` — enable_platega_sbp_recurring: гейт выключен -> RuntimeError сразу, до, `test_enable_sbp_recurring_gate_on_returns_redirect_url` — Гейт включён + ``PlategaService.create_subscription`` застаблен ->, `test_cancel_safe_wiring_proof_multi_tariff_delete_subscription` — Доказательство подключения (Task 11) на самом маленьком вызываемом шве:, `test_cancel_safe_wiring_proof_my_subscriptions_delete_execute` — Same wiring proof as the multi_tariff test above, for the second, `test_cancel_safe_wiring_proof_admin_bulk_delete_subscription` — Same wiring proof, for the third reachable delete path that was, `test_delete_user_account_cancels_platega_between_grace_checks` — UserService.delete_user_account (app/services/user_service.py) already, `test_delete_user_from_db_cancels_platega_for_each_subscription` — blocked_users_service.py::delete_user_from_db (blocked-user cleanup)
- `tests/services/test_platega_recurrent_logic.py` — Python-модуль
  Классы: нет
  Функции: `test_resolve_platega_interval`, `test_is_daily_wins_over_period_days`, `test_platega_reconcile_decision`, `test_reconcile_outage_does_not_bury_stuck_pending` — Транспортный сбой (remote_missing=False) — зависший PENDING не хороним:
- `tests/services/test_platega_service.py` — Python-модуль
  Классы: нет
  Функции: `test_sanitize_description_limits_utf8_bytes`, `test_sanitize_description_returns_clean_value`, `test_create_payment_defaults_to_v1_endpoint`, `test_create_payment_uses_v2_endpoint_when_configured`, `test_base_url_version_suffix_forces_version_and_is_stripped` — Обход из #2934 (PLATEGA_BASE_URL=…/v2) не должен собирать /v2/v2/… и не, `test_get_transaction_stays_unversioned`, `test_unknown_api_version_falls_back_to_v1`, `test_parse_redirect_url_accepts_v1_field`, `test_parse_redirect_url_accepts_v2_field`, `test_parse_redirect_url_missing_or_empty`, `test_base_url_version_suffix_is_case_insensitive` — A manually appended suffix may be uppercase ('/V2'); it must still be stripped, `test_v2_url_field_reaches_returned_redirect_url` — END-TO-END regression for #2934: a v2 create response carrying the link in
- `tests/services/test_platega_subscription_callbacks.py` — Python-модуль
  Классы: нет
  Функции: `test_create_sbp_subscription_persists_and_disables_autopay` — create_platega_sbp_subscription: сохраняет запись, выключает autopay_enabled,, `test_create_sbp_subscription_is_idempotent_on_repeat_call` — Повторный вызов (двойной тап / ретрай клиента) не должен плодить вторую, `test_confirmed_charge_extends_subscription_and_is_idempotent` — CONFIRMED: продлевает Subscription.end_date на charge_days, пишет аудитную, `test_confirmed_charge_syncs_remnawave_panel_after_extension` — CONFIRMED: после коммита продления должен best-effort синкнуться в панель, `test_confirmed_charge_panel_sync_failure_is_best_effort` — Сбой синка панели (Remnawave недоступна) — best-effort: не должен, `test_confirmed_charge_with_empty_id_does_not_extend` — CONFIRMED без Id (или с пустым Id) — недоверенный коллбек: без id, `test_confirmed_charge_missing_subscription_does_not_report_false_success` — CONFIRMED, но привязанная Subscription отсутствует (гонка/рассинхрон,, `test_canceled_charge_marks_past_due_and_counts_failure`, `test_subscription_past_due_status_transition`, `test_subscription_cancelled_status_transition`, `test_subscription_failed_status_transition`, `test_subscription_activated_status_transition`, `test_confirmed_charge_emits_ws_event_to_cabinet` — CONFIRMED-коллбек должен отправить WS-событие sbp_recurring.confirmed, `test_ws_emission_failure_does_not_break_callback` — Сбой отправки WS-события (соединения нет / менеджер упал) — best-effort,, `test_ws_emission_fires_without_bot_attribute` — Эмиссия WS-события должна происходить ДО early-return по, `test_callback_noops_on_missing_or_unknown_subscription_id` — Отсутствующий/неизвестный SubscriptionId и нераспознанный статус — не, `test_confirmed_charge_on_cancelled_record_extends_but_stays_cancelled` — Списание по локально ОТМЕНЁННОЙ записи (удалённая отмена не прошла):, `test_confirmed_late_redelivery_of_older_charge_is_skipped` — last_charge_external_id хранит только ПОСЛЕДНИЙ charge Id: поздний, `test_failed_and_expired_charge_statuses_mark_past_due` — Словарь провального списания не ограничен CANCELED: разовые платежи, `test_create_sbp_subscription_rejects_zero_price` — Нулевая цена отклоняется наравне с отсутствующей: подписка Platega на, `test_create_sbp_short_circuit_reenforces_autopay_off` — Идемпотентный повтор create при живой записи: если между вызовами юзер, `test_sbp_purchase_creates_expired_stub_for_new_tariff` — Нет подписки этого тарифа → создаётся EXPIRED-заготовка (без доступа),, `test_sbp_purchase_binds_to_existing_expired_subscription` — Есть истёкшая подписка тарифа → привязка на неё, заготовка не создаётся., `test_sbp_purchase_refuses_trial_disabled_and_foreign_tariff` — Отказы: триал (конверсию делает только balance-покупка), disabled/pending, `test_sbp_purchase_gate_off_raises`, `test_concurrent_enable_race_returns_winner_and_cancels_orphan` — Гонка конкурентного enable: оба прошли идемпотентную проверку, второй, `test_replay_missed_charges_extends_and_advances_counters` — Remote chargesSuccess > локального = потерянные коллбеки: подписка, `test_replay_waits_out_fresh_charge_window` — lastChargeAt свежее 2 часов — настоящий коллбек ещё может доехать, `test_replay_noop_without_metrics_or_deficit`, `test_camel_case_charge_extends_subscription` — Списание в camelCase (реальная форма Platega) продлевает подписку., `test_camel_case_charge_replay_is_idempotent` — Ретрай доставки того же списания не продлевает подписку дважды., `test_lowercase_confirmed_status_extends_subscription` — Регистр статуса тоже не должен решать судьбу продления.
- `tests/services/test_platega_subscription_crud.py` — Python-модуль
  Классы: нет
  Функции: `test_model_table_and_columns`, `test_create_and_fetch_round_trip` — create → get_by_platega_id / get_active_by_subscription → update → CANCELLED больше не активна.
- `tests/services/test_platega_subscription_service.py` — Python-модуль
  Классы: нет
  Функции: `test_create_subscription_posts_method_6`, `test_create_subscription_uses_v2_endpoint_when_configured`, `test_create_subscription_omits_description_when_not_provided`, `test_create_subscription_truncates_long_cyrillic_description`, `test_get_subscription_is_unversioned`, `test_list_subscriptions_builds_query_params`, `test_list_subscriptions_omits_none_params`, `test_cancel_subscription_posts_cancel`, `test_format_amount_integer_and_decimal`, `test_recurrent_gate`, `test_reconcile_unconfigured_platega_is_noop` — Неконфигурированный Platega (нет мерчанта/секрета) — no-op до БД., `test_reconcile_cancelled_sweep_runs_with_recurrent_flag_off` — Cancelled-свип (ретрай недошедших отмен) обязан работать и при, `test_reconcile_marks_stuck_pending_as_failed` — Safety net: a PENDING record that never got a platega_subscription_id back, `test_reconcile_recancels_remotely_active_cancelled_record` — Контрольный свип отменённых: локальный CANCELLED, но remote-статус, `test_reconcile_skips_cancelled_record_confirmed_remotely` — CANCELLED-запись, у которой remote-статус тоже cancelled, — свип не, `test_create_subscription_raises_actionable_error_on_val0001` — VAL_0001 с key=paymentMethod (формат запроса совпадает с доками) =, `test_create_subscription_transport_failure_returns_none` — Транспортный сбой (status=None) — прежний контракт: None, без исключения.
- `tests/services/test_promocode_rollback_keeps_user_usable.py` — Python-модуль
  Классы: нет
  Функции: `test_failed_trial_activation_keeps_user_attributes_loaded`
- `tests/services/test_promocode_service.py` — Python-модуль
  Классы: нет
  Функции: `test_activate_promo_group_promocode_success` — Test successful activation of PROMO_GROUP type promocode, `test_activate_promo_group_user_already_has_group` — Test activation when user already has the promo group, `test_activate_promo_group_group_not_found` — Test activation when promo group doesn't exist (deleted/invalid), `test_activate_promo_group_assignment_error` — Test activation when promo group assignment fails, `test_activate_promo_group_assigned_by_value` — Test that assigned_by parameter is correctly set to 'promocode', `test_activate_promo_group_description_includes_group_name` — Test that result description includes promo group name, `test_promocode_data_includes_promo_group_id` — Test that returned promocode data includes promo_group_id, `test_activate_trial_promocode_uses_all_available_squads_when_tariff_has_no_restrictions`, `test_subscription_days_promo_keeps_trial_a_trial` — Bug #629889 (class): a days-promocode on a TRIAL must NOT flip is_trial., `test_subscription_days_promo_revives_expired_sub_in_multi_tariff` — A days-promo must revive an EXPIRED subscription in multi-tariff mode too., `test_activation_aborts_when_usage_slot_cannot_be_claimed` — F18/F17: the atomic conditional increment is the authoritative gate., `test_trial_promo_refunds_instead_of_fake_success_when_subscription_exists` — F15: a trial promo that can't create/extend must raise (refund), not fake success.
- `tests/services/test_purchase_register_handlers_lint.py` — Python-модуль
  Классы: нет
  Функции: `test_register_handlers_does_not_shadow_module_imports` — No name imported at module-level may also be bound inside register_handlers., `test_subscription_states_is_module_level_only` — Explicit narrow guard for the exact 2026-05-16 incident.
- `tests/services/test_quick_amounts_disable.py` — Python-модуль
  Классы: нет
  Функции: `test_normalize_keeps_explicit_empty_list_as_disabled`, `test_normalize_none_means_defaults`, `test_normalize_dedup_and_sort_unchanged`, `test_effective_empty_list_disables_buttons`, `test_effective_none_falls_back_to_defaults`, `test_effective_custom_list_still_filtered_by_bounds`
- `tests/services/test_quick_amounts_service.py` — Python-модуль
  Классы: нет
  Функции: `test_normalize_none_returns_none`, `test_normalize_sorts_and_dedupes`, `test_normalize_empty_list_means_disabled`, `test_normalize_rejects_non_list`, `test_normalize_rejects_non_int_items`, `test_normalize_rejects_non_positive_items`, `test_normalize_rejects_more_than_ten_items`, `test_normalize_caps_after_dedupe`, `test_effective_returns_defaults_when_not_configured`, `test_effective_filters_by_min_max`, `test_effective_returns_empty_when_all_filtered_out`
- `tests/services/test_reachability_registries.py` — Python-модуль
  Классы: нет
  Функции: `test_permission_section_registered`, `test_permission_is_grantable`, `test_wildcard_from_bootstrap_survives_a_role_save`, `test_admin_preset_gets_wildcard`, `test_settings_defaults`, `test_settings_land_in_bschek_category`, `test_category_has_title_and_description`, `test_api_key_is_masked_and_numbers_are_not`, `test_env_example_block_is_commented_out` — Раскомментированный BSCHEK_* в .env затеняет значение, заданное из кабинета.
- `tests/services/test_recreate_deleted_panel_user.py` — Python-модуль
  Классы: нет
  Функции: `test_not_found_by_status_404`, `test_not_found_by_error_code_without_404`, `test_other_errors_are_not_not_found`, `test_plain_400_is_not_not_found` — 3.0.0: user-маршруты параметризованы ``z.coerce.number().positive()``, поэтому, `test_invalid_user_id_error_is_never_not_found` — ``RemnaWaveInvalidUserIdError`` — баг в данных бота (протухший UUID/None в, `test_coerce_panel_user_id_rejects_non_numeric_identifiers` — Мусорный идентификатор отсекается на границе клиента и не доходит до сети., `test_recreate_active_delegates_to_create_flow`, `test_recreate_trial_is_also_alive`, `test_recreate_skips_expired_status` — Истёкшую подписку не пересоздаём: админ удалил панель-юзера намеренно., `test_recreate_skips_active_status_with_past_end_date` — Статус ACTIVE, но end_date уже прошёл (scheduled job ещё не отработал) — не пересоздаём., `test_update_addresses_panel_by_numeric_id` — 3.0.0: гейт «обновлять или создавать» и сам PATCH идут по числовому, `test_update_skips_panel_when_no_panel_id` — Пустой ``remnawave_id`` — обновлять нечего: запроса в панель быть не должно, `test_update_recreates_deleted_panel_user`, `test_update_does_not_recreate_on_other_api_errors`, `test_update_does_not_recreate_on_invalid_panel_user_id` — Битый локальный идентификатор (клиент отсёк его до сети) — НЕ повод считать,, `test_update_does_not_recreate_on_plain_400` — Голая 400 VALIDATION от панели (3.0.0 отвечает так на непригодный id) тоже, `test_update_recreates_on_a063_without_404` — A063 без статуса 404 — тоже маркер «панель-юзера нет»: пересоздаём., `test_update_does_not_recreate_on_a039_fk_violation` — A039 — FK violation на externalSquadUuid (панель жива, стейловый сквад),, `test_update_does_not_recreate_for_expired_subscription` — Пуш DISABLED в удалённого панель-юзера: молча пропускаем, панель не засоряем., `test_multi_tariff_gate_updates_existing_panel_user_instead_of_creating_a_duplicate` — Multi-tariff: у подписки есть числовой ``remnawave_id`` → PATCH этого юзера., `test_multi_tariff_gate_ignores_the_legacy_uuid_and_creates_when_no_numeric_id` — Обратное направление: сама по себе легаси-колонка НЕ является привязкой., `test_multi_tariff_gate_does_not_create_on_invalid_local_panel_id` — Битый локальный id (клиент отбил его до сети) обязан пробросить исключение., `test_single_tariff_gate_updates_by_user_panel_id_without_searching` — Single-tariff: привязка живёт в ``users.remnawave_id``., `test_single_tariff_gate_falls_back_to_stream_search_before_creating` — Привязки нет → сперва ищем существующего юзера в панели, только потом создаём., `test_legacy_panel_uuid_column_is_read_only_by_the_two_modules_that_own_it`, `test_monitoring_update_happy_path_reaches_panel` — Регрессия: self._gb_to_bytes не существовал у MonitoringService — метод падал, `test_monitoring_update_recreates_deleted_panel_user`, `test_monitoring_update_does_not_recreate_on_other_api_errors`, `test_monitoring_update_does_not_recreate_on_invalid_panel_user_id` — Тот же контракт в рутинном синке: битый локальный id — не «юзера нет»., `test_monitoring_update_does_not_recreate_on_plain_400`, `test_monitoring_update_recreates_on_a018_without_404` — A018 (без статуса 404) — маркер удалённого панель-юзера: пересоздаём., `test_multi_tariff_adopts_panel_user_by_short_uuid_instead_of_duplicating` — Гейт создания читает только remnawave_id; до бэкфила он пуст у всех строк., `test_multi_tariff_still_creates_when_panel_does_not_know_the_short_uuid` — Обратная сторона: подхват не должен блокировать законное создание., `test_transient_panel_failure_never_creates_a_duplicate` — Таймаут панели — это не «юзера нет»., `test_validation_adopts_panel_id_and_keeps_the_recovery_key`, `test_validation_cleans_only_when_the_panel_denies_the_short_uuid`, `test_validation_never_cleans_on_a_panel_error` — Таймаут — не доказательство отсутствия аккаунта., `test_adoption_does_not_swallow_a_non_404_panel_error` — Только 404 доказывает, что аккаунта нет., `test_update_adopts_panel_id_by_short_uuid_instead_of_giving_up` — Между 0104 и бэкфилом `remnawave_id` пуст у всех доапгрейдных строк., `test_update_still_gives_up_when_the_panel_does_not_know_the_short_uuid`, `test_update_gives_up_when_the_panel_is_unreachable` — Транзиент — не повод выдумать идентичность и записать её на строку., `test_single_mode_prefers_exact_keys_over_ambiguous_telegram_search` — Точный ключ обязан побеждать неоднозначный поиск по telegramId., `test_single_mode_uses_the_subscription_own_panel_id` — `subscriptions.remnawave_id` — тоже точный адрес, и в single-tariff его не спрашивали., `test_create_does_not_break_on_the_partial_unique_index` — Соседняя подписка уже держит этот панельный аккаунт — запись обязана уступить., `test_stale_panel_id_still_gets_rescued_by_short_uuid` — Протухший числовой id не должен отменять спасение по shortUuid., `test_short_uuid_adoption_respects_the_partial_unique_index` — Привязка по shortUuid обязана уступить, если аккаунт держит соседняя строка., `test_degraded_short_uuid_endpoint_does_not_abort_a_resolvable_sync` — Деградация точного ключа не должна ронять то, что решается другим ключом., `test_degraded_short_uuid_endpoint_still_refuses_to_create_a_duplicate` — Но если больше НИЧЕМ не опознали — создавать нельзя, надо честно упасть., `test_foreign_account_is_cleaned_not_re_anchored` — Несовпадение владельца обязано вести в очистку, а не в перепривязку., `test_short_uuid_rescue_refuses_a_foreign_account` — И сама перепривязка обязана проверять владельца., `test_ownership_mismatch_is_not_rescued_by_a_telegram_less_account` — Несовпадение владельца нельзя «спасать» аккаунтом без telegramId., `test_adoption_refuses_when_a_sibling_owns_the_account` — Занятый аккаунт — повод отменить операцию, а не только пропустить запись.
- `tests/services/test_recurrent_amount_sync.py` — Python-модуль
  Классы: нет
  Функции: `test_true_amount_includes_extra_devices` — Цена продления учитывает доп. устройства — это и есть «правильная» сумма., `test_matching_amount_keeps_bindings` — Совпадающая сумма — привязки не трогаем., `test_stale_bindings_of_both_providers_are_cancelled` — Подписка подорожала (докуплены устройства) — обе привязки гасятся., `test_sync_never_raises_on_missing_subscription` — Best-effort: докупка уже оплачена и не должна падать из-за рекуррента., `test_device_purchase_triggers_sync` — Докупка устройств вызывает согласование сумм из CRUD.
- `tests/services/test_referral_days_target_choice.py` — Python-модуль
  Классы: нет
  Функции: `choice_on`, `test_chosen_subscription_wins_over_the_automatic_pick` — Автоподбор взял бы подписку с самым поздним сроком — выбор важнее., `test_without_the_setting_the_choice_is_ignored` — Выключенная настройка возвращает прежний автоподбор — целиком., `test_a_foreign_subscription_is_never_used` — Ссылка живёт в строке пользователя и переживает что угодно., `test_a_stale_choice_falls_back_instead_of_refusing` — Удалённая подписка не должна отменять награду., `test_the_rule_tariff_still_wins_over_the_choice` — Тариф в правиле — указание админа, куда дни обязаны лечь., `test_days_actually_land_in_the_chosen_subscription` — Сквозная проверка: не только выбор цели, но и само продление.
- `tests/services/test_referral_diagnostics.py` — Python-модуль
  Классы: нет
  Функции: `temp_log_file` — Создаёт временный лог-файл для тестов., `sample_log_content` — Пример содержимого лог-файла с реферальными событиями., `test_parse_logs_basic` — Тест базового парсинга логов., `test_analyze_period_with_issues` — Тест анализа с проблемными случаями., `test_empty_log_file` — Тест работы с пустым лог-файлом., `test_nonexistent_log_file` — Тест работы с несуществующим лог-файлом., `test_analyze_today` — Тест метода analyze_today.
- `tests/services/test_referral_grant_days.py` — Python-модуль
  Классы: `TestDaysLandWhereConfigured` (3 методов), `TestMissingSubscription` (3 методов), `TestTrialIsNeverConverted` (2 методов), `TestPanelSyncFailure` (1 методов), `TestSubscriptionOnAnotherTariff` (3 методов), `TestStatesWhereDaysMustNotLand` (4 методов), `TestManySubscriptionsInMultiTariff` (5 методов), `TestClassicMode` (3 методов)
  Функции: `no_panel_sync` — Remnawave в тестах не поднимается — синхронизация подменяется.
- `tests/services/test_referral_ledger_orientation.py` — Python-модуль
  Классы: нет
  Функции: `test_referral_id_grouping_excludes_referee_rows`, `test_ratchet_actually_sees_the_guarded_sites` — Сам храповик должен что-то находить, иначе он молча деградирует в no-op.
- `tests/services/test_referral_levels_safety.py` — Python-модуль
  Классы: `TestCacheInvalidation` (2 методов), `TestCacheReload` (1 методов), `TestDiagnosticsGuard` (3 методов), `TestBackupCoverage` (1 методов), `TestMergeChainRepair` (3 методов), `TestAsyncSessionHazards` (2 методов), `TestCacheGeneration` (1 методов), `TestFirstTopupClaim` (1 методов)
  Функции: нет
- `tests/services/test_referral_reward_levels.py` — Python-модуль
  Классы: `TestChainWalk` (4 методов), `TestSchemeGate` (1 методов), `TestMoneyPerLevel` (6 методов), `TestTriggers` (2 методов), `TestActiveBonusSelection` (3 методов), `TestRefereeSide` (3 методов), `TestGranting` (9 методов), `TestNullPercentIsZero` (2 методов), `TestLevelNotifications` (6 методов), `TestRewardFormatting` (1 методов), `TestProgramDescription` (4 методов), `TestInvitePromise` (2 методов), `TestDepthHonesty` (2 методов), `TestUngrantablePromises` (4 методов), `TestRefereeRowsStayOutOfReferrerTotals` (1 методов), `TestGeneratedTextIsLocalized` (5 методов), `TestLegacyImportPercent` (4 методов), `TestDepthOnThePayoutPath` (2 методов), `TestThresholdGatesThePayout` (4 методов)
  Функции: `chain` — Цепочка 4 → 3 → 2 → 1 и схема 'levels'., `granting` — Обвязка выдачи: начисления и записи ledger'а собираются в списки.
- `tests/services/test_referral_rewards_baseline.py` — Python-модуль
  Классы: `TestTopupBranches` (5 методов), `TestCommissionPercent` (3 методов), `TestNoMultiLevelYet` (1 методов)
  Функции: `wired` — Общая обвязка: реферал → реферер, без похода в БД и Telegram.
- `tests/services/test_referral_service.py` — Python-модуль
  Классы: нет
  Функции: `test_referral_notification_respects_notification_switches`, `test_referral_notification_enabled_sends_telegram_message`, `test_disabled_referral_notifications_skip_email_delivery`, `test_commission_accrues_before_minimum_first_topup`, `test_first_topup_inviter_gets_fixed_plus_commission` — Inviter bonus should be fixed bonus + commission, not max(fixed, commission)., `test_first_payment_commission_percent_overrides_flat_percent`, `test_recurring_commission_percent_uses_paid_referrals_tier`, `test_second_small_topup_uses_recurring_tier_not_first_payment_percent`, `test_parse_recurring_commission_tiers_handles_edge_cases`, `test_calculate_recurring_commission_tier_boundary`
- `tests/services/test_referral_tier_mode.py` — Python-модуль
  Классы: `TestDefaultIsChain` (3 методов), `TestSelection` (5 методов), `TestPayout` (10 методов), `TestReferee` (2 методов), `TestLadderText` (3 методов), `TestProgress` (5 методов), `TestTextMatchesPayout` (9 методов), `TestChainModeIsUntouchedByTheNewArguments` (4 методов), `TestProgressFormatting` (4 методов), `TestModeNormalisation` (2 методов), `TestChainRefereePromise` (3 методов), `TestRestoreInvalidatesLevelCache` (2 методов), `TestImpossiblePercentIsNotPromised` (3 методов)
  Функции: `tiers` — Режим рангов, цепочка 4 → 3 → 2 → 1 и управляемое число рефералов.
- `tests/services/test_referral_tier_mode_db.py` — Python-модуль
  Классы: нет
  Функции: `tier_mode`, `test_threshold_counts_only_direct_referrals` — Порог отвечает на «насколько вырос сам партнёр», а не «сколько под ним всего»., `test_active_only_counts_those_who_topped_up`, `test_tier_is_chosen_from_real_rows` — Сквозной путь: строки БД → выбор ранга → начисленная сумма., `test_inactive_referrals_do_not_open_a_tier` — Порог по «с пополнением» не берётся накруткой пустых аккаунтов., `test_payment_cap_reads_this_tier_only` — Строки ДРУГИХ уровней не должны исчерпывать лимит ранга., `test_payment_cap_does_stop_this_tier` — Контроль к предыдущему: свои строки лимит исчерпывают., `test_progress_reports_real_counts`, `test_chain_mode_still_pays_the_whole_chain` — Контроль: режим по умолчанию не изменился ни на строку., `test_level_repair_would_flatten_a_rank` — Показывает УЩЕРБ, от которого защищает гейт ниже., `test_merge_gates_level_repair_on_chain_mode` — Сторож на вызов: пересчёт уровней запускается только в режиме цепочки., `test_average_income_counts_every_rank` — «Средний доход с реферала» брал срез level==1 — верный только для цепочки., `test_overview_average_counts_every_rank_too` — Вторая точка того же среза по level==1 — в сводке по всем партнёрам., `test_cap_applies_to_an_earning_made_in_the_same_second` — Лимит обязан считать начисление, сделанное сразу после создания правила., `test_margin_does_not_swallow_older_history` — Контроль: запас на границе не должен втягивать историю прежних схем.
- `tests/services/test_referral_tier_wiring.py` — Python-модуль
  Классы: `TestCabinetTerms` (4 методов), `TestBotInviteScreen` (1 методов)
  Функции: `spies` — Подменяет описания на шпионов, записывающих переданные аргументы.
- `tests/services/test_referral_user_choice.py` — Python-модуль
  Классы: `TestDefaultsAreOff` (4 методов), `TestRewardKindChoice` (5 методов), `TestLadderMatchesTheChoice` (3 методов)
  Функции: `wired`
- `tests/services/test_registration_access_service.py` — Python-модуль
  Классы: `FakeValidator` (2 методов)
  Функции: `reader`, `context`, `test_access_matrix`, `test_non_telegram_channel_cannot_create_or_revive_when_enabled`, `test_web_gift_claim_is_admitted_by_the_gift_token_it_carries` — The 64-char token the web claim requires is the same bearer invite the deep link wraps., `test_web_gift_claim_without_resolvable_gift_stays_denied`, `test_web_gift_claim_respects_disabled_gift_invites`, `test_active_user_does_not_touch_settings_or_validator`, `test_validator_error_is_fail_closed_for_new_user`, `test_gift_flag_is_forwarded_to_validator`, `test_invite_validator_protocol_stub_is_not_executable`
- `tests/services/test_registration_invite_service.py` — Python-модуль
  Классы: `ScalarResult` (2 методов), `GiftScalars` (2 методов), `GiftResult` (2 методов), `FakeDB` (3 методов)
  Функции: `test_inactive_referrer_is_not_an_invitation`, `test_active_referrer_grants_invitation`, `test_active_campaign_grants_invitation`, `test_gift_is_not_accepted_when_gift_invites_are_disabled`, `test_claimable_gift_is_returned_and_bound_without_commit`, `test_self_gift_does_not_grant_invitation`, `test_early_gift_validation_does_not_lock_row`
- `tests/services/test_remnawave_devices_stats.py` — Python-модуль
  Классы: нет
  Функции: `test_devices_statistics_aggregates_nested_byapp_2_8_0`, `test_devices_statistics_prefers_top_level_byapp_2_7_x`, `test_devices_statistics_explicit_none_byapp_aggregates_nested` — byApp explicitly None (not just absent) must still trigger nested aggregation., `test_devices_statistics_platform_without_byapp_is_skipped_not_fatal` — A platform lacking byApp (or with a malformed non-list byApp) is skipped; siblings still aggregate., `test_devices_statistics_none_counts_coerced_to_zero` — Present-but-null counts must not leak None into the response (defensive)., `test_devices_statistics_empty_byplatform_yields_empty_byapp`
- `tests/services/test_remnawave_expiration_webhook.py` — Python-модуль
  Классы: нет
  Функции: `test_new_and_old_events_both_registered`, `test_canonical_hours_map_to_legacy_messages`, `test_reads_receiver_meta_key_not_raw_meta` — Regression: the handler reads data['_meta'] (receiver contract). A payload, `test_non_canonical_negative_picks_nearest_before_message`, `test_non_canonical_positive_uses_expired_message`, `test_missing_or_invalid_meta_sends_nothing`, `test_no_subscription_sends_nothing`, `test_webhook_expiry_respects_user_days_threshold`, `test_new_2_8_0_api_token_admin_events_registered` — 2.8.0 added service.api_token_created/deleted — surfaced as admin notifications., `test_user_modified_syncs_used_traffic_from_nested_user_traffic` — usedTrafficBytes lives nested in userTraffic (ExtendedUsersSchema); the, `test_user_modified_used_traffic_falls_back_to_flat_key` — Old panels send a flat usedTrafficBytes — keep the fallback working.
- `tests/services/test_remnawave_identity_backfill.py` — Python-модуль
  Классы: нет
  Функции: `panel_user`, `subscription`, `bot_user`, `test_short_uuid_is_the_exact_key`, `test_stored_short_uuid_unknown_to_panel_does_not_fall_back` — The panel user was deleted., `test_unique_telegram_id_resolves_when_no_short_uuid`, `test_multi_tariff_ambiguity_is_refused_without_a_discriminator`, `test_multi_tariff_short_id_suffix_disambiguates`, `test_email_only_user_resolves_by_email`, `test_duplicate_email_is_refused`, `test_no_surviving_identifier_is_reported_not_guessed`, `test_already_claimed_panel_user_is_not_reused_via_username` — Two subscriptions must never converge on one panel account., `test_index_tolerates_missing_fields` — Panel rows legitimately have null telegramId/email; indexing must not crash., `test_exact_only_defers_rows_without_a_short_uuid` — Pass 1 must not consume weak matches., `test_exact_only_still_reports_a_dead_short_uuid` — A stored shortUuid the panel does not know is a verdict, not a deferral., `test_exact_match_wins_over_a_lower_numbered_weak_match` — The regression this was written for., `test_rerun_does_not_reassign_an_already_persisted_panel_id` — `claimed` must be primed from rows a previous run already wrote., `test_dry_run_writes_nothing`, `test_conflict_between_different_users_rolls_back` — Один панельный аккаунт, на который претендуют РАЗНЫЕ пользователи., `test_sibling_rows_of_one_user_are_not_a_conflict` — Штатное состояние single-tariff, а не ошибка., `test_multi_tariff_never_fills_the_user_level_column` — В multi-tariff `users.remnawave_id` обязан остаться пустым., `test_rerun_does_not_invent_a_conflict_for_a_sibling` — Повторный прогон — штатный сценарий, а не исключение., `test_live_subscription_wins_the_panel_id_not_the_expired_one` — id достаётся живой строке, а не самой старой., `test_multi_tariff_shared_account_is_skipped_locally_not_aborted` — Общий аккаунт в multi-tariff ненормален, но не должен ронять весь прогон., `test_id_moves_to_the_live_row_when_only_the_dead_one_kept_the_short_uuid` — Самая частая форма в single-tariff, и сортировкой её не решить., `test_between_two_live_rows_the_later_end_date_wins` — Пинит саму сортировку, а не перенос id., `test_transfer_does_not_leave_the_target_marked_unresolved` — Отчёт — единственная поверхность принятия решения для оператора., `test_grace_session_on_the_donor_row_still_gets_an_id` — Перенос id не должен обесточивать grace-сессию строки-донора., `test_multi_tariff_sibling_session_resolves_by_its_own_uuid` — Мультитариф: сессия на «сиблинге» тоже обязана получить идентичность., `test_session_of_a_deleted_panel_account_is_not_given_the_live_one` — Сессия удалённого аккаунта не должна получить живой аккаунт владельца., `test_single_tariff_session_resolves_by_the_user_uuid_it_actually_stores` — Реальная форма однотарифных данных: в сессии лежит uuid ПОЛЬЗОВАТЕЛЯ., `test_session_resolves_when_its_subscription_was_linked_by_an_earlier_run` — Карта uuid обязана праймиться из уже записанных строк., `test_panel_id_already_owned_by_another_user_is_reported_not_assigned` — `users.remnawave_id` уникальна глобально — второй претендент идёт в отчёт., `test_blocked_strongest_evidence_does_not_fall_through_to_a_weaker_one` — Заблокированная подписочная улика НЕ должна спускаться к телеграм-ветке., `test_account_claimed_by_another_users_subscription_is_not_given_away` — Аккаунт, разобранный подпиской ДРУГОГО пользователя, нельзя отдать по telegram_id., `test_one_uuid_pointing_at_two_accounts_is_dropped_not_coin_flipped` — Противоречивый uuid нельзя разрешать «как повезёт»., `test_uuid_clash_discovered_during_matching_is_also_dropped` — Коллизия, всплывшая в `assign`, а не при прайминге, — тот же случай., `test_user_takes_the_exact_id_from_a_previously_linked_subscription` — Точный id соседней строки бьёт догадку по telegram_id., `test_user_with_two_different_panel_accounts_is_reported_not_guessed` — Две подписки на РАЗНЫЕ аккаунты — угадывать нечего, нужна строка в отчёте., `test_a_dead_persisted_id_does_not_beat_a_live_telegram_match` — Сохранённый id, которого в панели уже нет, не должен ничего решать., `test_every_write_is_recorded_in_the_audit_trail` — Прогон обязан оставлять построчный след того, что записал., `test_audit_filename_reflects_the_kind_of_run_not_the_commit_flag` — Холостой прогон — это `dryrun`, а не `conflicts`.
- `tests/services/test_remnawave_service_sync.py` — Python-модуль
  Классы: нет
  Функции: `test_deduplicate_prefers_latest_expire_date`, `test_deduplicate_prefers_active_status_on_same_expire`, `test_deduplicate_ignores_records_without_expire_date`, `test_get_or_create_user_handles_unique_violation`, `test_get_or_create_user_creates_new`
- `tests/services/test_remnawave_username.py` — Python-модуль
  Классы: нет
  Функции: `test_format_remnawave_username_within_max_without_suffix` — Default behaviour stays bounded by REMNAWAVE_USERNAME_MAX_LENGTH., `test_format_remnawave_username_reserves_room_for_caller_suffix` — reserve_suffix_chars=N → base fits in MAX-N so caller can append safely., `test_format_remnawave_username_email_user_default_template` — Email-only user with the bundled default template still fits., `test_format_remnawave_username_does_not_go_below_min_with_huge_reserve` — If caller asks for more reserve than the cap allows, base falls back to MIN., `test_format_remnawave_username_repro_38_char_bug` — Exact production payload from log.rw/ARVm79dH must come out ≤ 36 chars., `test_build_subscription_username_production_repro` — Production repro through the high-level helper used by all 3 callers., `test_build_subscription_username_empty_suffix_is_legacy_format` — suffix='' → equivalent to plain format_remnawave_username (single-tariff path)., `test_build_subscription_username_handles_pathological_long_suffix` — Suffix longer than MAX_LENGTH: helper must still produce a string ≤ MAX_LENGTH., `test_skeleton_detector_falls_back_when_username_template_renders_constant` — Template `user_{username}` for email-only user (no TG username) renders to, `test_skeleton_detector_falls_back_when_template_has_no_variables` — A template with no variables (admin misconfig) is itself degenerate —, `test_skeleton_detector_does_not_trigger_for_telegram_users` — TG users with a real @username are NOT degenerate — the template renders, `test_skeleton_detector_uses_user_id_when_template_references_it` — Template that references {user_id} (which always has a value) must NOT, `test_cyrillic_full_name_is_transliterated` — Issue #1659: кириллица в {full_name} должна транслитерироваться, а не выпадать., `test_ascii_full_name_stays_unchanged` — Латинские имена не должны меняться транслитерацией., `test_transliterate_cyrillic_preserves_case_and_non_cyrillic`, `test_transliterated_long_cyrillic_name_respects_max_length` — Транслитерация удлиняет строку (щ → shch) — итог всё равно должен влезать в лимит.
- `tests/services/test_reset_subscription.py` — Python-модуль
  Классы: нет
  Функции: `test_reset_subscription_zeroes_fields`, `test_reset_with_panel_disables_subscription_panel_id`, `test_reset_with_panel_multitariff_no_sub_panel_id_skips_panel` — Multi-tariff + no per-sub panel id → must NOT fall back to user.remnawave_id, `test_reset_with_panel_singletariff_falls_back_to_user_panel_id`, `test_reset_with_panel_no_panel_id_skips_panel`, `test_reset_with_panel_survives_panel_error` — A panel disable failure must not block the bot-side reset (best effort)., `test_user_modified_does_not_resurrect_disabled_end_date` — A user.modified webhook carrying a stale FUTURE expireAt must NOT restore the, `test_user_modified_still_syncs_end_date_for_active` — Regression guard: ACTIVE subs still get end_date synced from the panel., `test_user_level_reset_deletes_all_three_current_subscriptions` — The user reset remains one user-scoped operation, not a selected-sub reset., `test_user_level_reset_panel_failure_preserves_all_subscription_retry_identities`
- `tests/services/test_settings_categories.py` — Python-модуль
  Классы: нет
  Функции: `test_setting_lands_in_expected_category`, `test_no_setting_falls_into_a_single_verb_category` — Категория из одного глагола — признак забытой привязки в CATEGORY_KEY_OVERRIDES., `test_every_setting_is_exposed_unless_explicitly_excluded` — Ни одна настройка не должна пропасть из админки молча.
- `tests/services/test_settings_excluded_keys.py` — Python-модуль
  Классы: нет
  Функции: `test_identity_and_auth_secrets_are_excluded`, `test_excluded_keys_have_no_editable_definition`
- `tests/services/test_settings_numeric_units.py` — Python-модуль
  Классы: нет
  Функции: `test_devices_selection_disabled_amount_is_not_a_price`, `test_devices_selection_disabled_amount_zero_stays_plain`, `test_money_keys_keep_currency_formatting`
- `tests/services/test_settings_secret_masking.py` — Python-модуль
  Классы: нет
  Функции: `test_is_secret_key_matches_secret_names`, `test_is_secret_key_ignores_plain_names`, `test_string_secret_is_masked`, `test_unset_secret_is_not_masked`, `test_numeric_settings_with_secretish_names_are_not_masked`, `test_no_nonstring_definition_value_is_ever_masked` — Sweep every real setting definition: a masked value must always be a string.
- `tests/services/test_stars_payload_amount.py` — Python-модуль
  Классы: нет
  Функции: `test_parser_extracts_amount_kopeks_from_known_payload_shapes`, `test_parser_returns_none_for_unrecognised_shapes`, `test_plausibility_accepts_lossless_round_trip` — At rate=1.0 with integer rubles, payload == reconstructed exactly., `test_plausibility_accepts_sub_ruble_drift` — 50.50 ₽ requested → 50 ⭐ × 1.0 = 50.00 ₽ reconstructed → 50 kopeks drift, well within tolerance., `test_plausibility_accepts_20pct_drift` — A 20% rate change between invoice creation and payment must NOT trip the guard., `test_plausibility_rejects_inflated_payload` — A payload claiming 10× the reconstructed amount is pathological — fall back to stars×rate., `test_plausibility_rejects_zero_or_negative`, `test_plausibility_uses_minimum_100_kopek_floor_for_tiny_amounts` — For tiny amounts (e.g. 50 kopeks reconstructed), 20% would be 10 — too tight., `test_negative_control_old_rate_was_lossy` — Regression cover: the pre-fix flow under rate=1.3 lost 0.50 ₽ on a 150 ₽ top-up., `test_negative_control_at_new_rate_is_lossless_for_integer_rubles` — At rate=1.0 with integer rubles, payload and reconstructed agree exactly.
- `tests/services/test_startup_logo_prewarm.py` — Python-модуль
  Классы: нет
  Функции: `test_prewarm_caches_file_id_and_deletes_message`, `test_prewarm_skips_when_already_cached`, `test_prewarm_no_target_chat_skips`, `test_prewarm_is_best_effort_on_timeout`
- `tests/services/test_structlog_reserved_kwargs.py` — Python-модуль
  Классы: нет
  Функции: `test_no_reserved_kwargs_in_log_calls`, `test_guard_detects_a_planted_call` — Сторож обязан быть чувствительным, иначе он молча зелёный., `test_reserved_kwarg_really_raises` — Не теория: такой вызов действительно падает на настоящем structlog., `test_guard_sees_every_logger_shape` — Сторож обязан узнавать логгер во всех формах, которые встречаются в коде., `test_guard_does_not_fire_on_unrelated_code` — И не должен срабатывать на том, что логгером не является.
- `tests/services/test_subscription_auto_purchase_service.py` — Python-модуль
  Классы: `DummyTexts` (2 методов)
  Функции: `test_auto_purchase_saved_cart_after_topup_success`, `test_auto_purchase_saved_cart_after_topup_extension`, `test_race_guard_fresh_updated_at_without_subscription_payment_allows_purchase` — Свежий updated_at без SUBSCRIPTION_PAYMENT (только deposit) не блокирует автопокупку., `test_race_guard_fresh_updated_at_with_subscription_payment_skips_purchase` — Свежий updated_at + свежий SUBSCRIPTION_PAYMENT → пропуск автопокупки (защита от двойного списания)., `test_auto_purchase_trial_preserved_on_insufficient_balance` — Тест: триал сохраняется, если не хватает денег для автопокупки, `test_auto_purchase_trial_converted_after_successful_extension` — Тест: триал конвертируется в платную подписку ТОЛЬКО после успешного продления, `test_auto_purchase_trial_preserved_on_extension_failure` — Тест: триал НЕ конвертируется и вызывается rollback при ошибке в extend_subscription, `test_auto_purchase_trial_remaining_days_transferred` — Тест: остаток триала переносится на платную подписку при TRIAL_ADD_REMAINING_DAYS_TO_PAID=True, `test_auto_purchase_skipped_without_topup_intent` — Без свежего намерения корзина НЕ покупается, даже если она сохранена и
- `tests/services/test_subscription_dedup_service.py` — Python-модуль
  Классы: нет
  Функции: `test_collapses_report_scenario`, `test_never_removes_alive_even_if_outranked_by_date`, `test_disabled_duplicate_is_removed_active_survives`, `test_single_rows_untouched`
- `tests/services/test_subscription_deletion_panel_target.py` — Python-модуль
  Классы: нет
  Функции: `test_multi_tariff_takes_the_subscriptions_own_account` — Мультитариф: у подписки свой аккаунт, его и удаляем., `test_single_tariff_falls_back_to_the_user_account` — Однотарифный: колонка подписки пуста, но аккаунт есть — и его надо снять., `test_single_tariff_spares_account_of_a_live_sibling` — Однотарифный: живая соседка сидит на том же аккаунте — не трогаем его., `test_single_tariff_ignores_dead_sibling` — Мёртвая соседка ничего не держит — аккаунт всё равно отключаем., `test_single_tariff_uses_subscription_id_when_user_has_none` — Историческая строка: id остался только на подписке — работаем по нему., `test_single_tariff_deletion_disables_shared_account` — Однотарифный, соседок нет: доступ снят, аккаунт остался живым для новой покупки., `test_single_tariff_deletion_leaves_live_sibling_alone` — Уборка отработавшей строки не должна трогать аккаунт живой соседки., `test_multi_tariff_deletion_deletes_own_account` — Мультитариф: аккаунт подписки удаляется и помечается намеренным., `test_open_grace_aborts_before_anything_irreversible` — Грейс-гард обязан пробрасываться наружу, а не глохнуть внутри сервиса., `test_step_order_is_pinned` — Порядок шагов удаления — не косметика, каждый стоит там не случайно.
- `tests/services/test_subscription_extend_cabinet_mode.py` — Python-модуль
  Классы: нет
  Функции: `cabinet_mode` — Полностью настроенный cabinet-режим: и режим меню, и URL кабинета., `test_multi_tariff_cabinet_button_opens_that_subscription_renewal` — РЕГРЕССИЯ: раньше здесь была callback-кнопка, уводившая в бота., `test_single_tariff_cabinet_button_opens_cabinet` — Одиночный режим работал и раньше — поведение не должно поменяться., `test_multi_tariff_without_id_falls_back_to_subscription_list` — Без id конкретной подписки вести некуда — открываем список подписок., `test_bot_mode_keeps_callback_button` — Вне cabinet-режима кнопка обязана остаться обычным callback'ом., `test_cabinet_mode_without_url_falls_back_to_callback` — Cabinet-режим без ``MINIAPP_CUSTOM_URL`` не должен ломать кнопку., `test_dynamic_callback_keeps_subscription_section_styling` — Стиль берётся по секции ``subscription``, а не теряется из-за ``se:{id}``., `test_dynamic_callback_is_not_added_to_static_mapping` — ``se:{id}`` динамический — в статическом маппинге ему места нет., `test_call_sites_do_not_build_extend_callback_by_hand` — РЕГРЕССИЯ: каждый ручной ``f'se:{...}'`` — это ещё одна кнопка в бота., `test_expired_notification_keyboard_opens_cabinet` — Сквозная проверка на том самом уведомлении из отчёта пользователя.
- `tests/services/test_support_settings_sync.py` — Python-модуль
  Классы: нет
  Функции: `support_storage` — Изолированное JSON-хранилище + сброс кеша класса на каждый тест., `test_load_syncs_system_mode_into_settings` — REGRESSION: persisted-режим должен доезжать до settings при загрузке —, `test_mode_survives_restart_for_cabinet` — REGRESSION (сквозной сценарий): админ выключил тикеты в боте, бот, `test_load_syncs_menu_enabled_into_settings` — REGRESSION: у menu_enabled была ровно та же проблема., `test_set_support_menu_enabled_syncs_settings` — Сеттер меню тоже обязан обновлять settings (раньше не обновлял вовсе)., `test_absent_json_keeps_env_value` — Без сохранённого значения settings остаётся как задан в .env., `test_invalid_persisted_mode_does_not_clobber_settings` — Мусор в JSON не должен затирать settings невалидным режимом., `test_corrupt_json_does_not_clobber_settings` — Битый JSON: _load глотает ошибку, settings остаётся из .env.
- `tests/services/test_sync_users_to_panel_adoption.py` — Python-модуль
  Классы: нет
  Функции: `harness` — Один батч из одной подписки, gracce-lease разрешён, клиент — мок., `test_adopts_existing_panel_user_instead_of_creating_a_duplicate`, `test_creates_when_the_panel_does_not_know_the_short_uuid`, `test_single_tariff_writes_identity_onto_the_user` — Мутация «поменять ветки местами» схлопывала все подписки юзера на один id., `test_update_branch_does_not_wipe_squads_when_the_local_list_is_empty` — Сиблинг того же дефекта: ветка обновления по УЖЕ известному id., `test_update_branch_forwards_a_non_empty_squad_list`
- `tests/services/test_system_error_log_service.py` — Python-модуль
  Классы: нет
  Функции: `test_token_is_redacted_in_every_persisted_field`, `test_redaction_keeps_the_rest_of_the_message`, `test_only_real_delivery_attempts_are_counted` — suppressed/skipped до Telegram не доходят — счётчик попыток они не двигают., `test_stop_flushes_what_is_already_queued` — При аварийном завершении в очереди лежат ровно те ошибки, что к нему привели., `test_stop_is_safe_without_start`
- `tests/services/test_system_settings_env_priority.py` — Python-модуль
  Классы: нет
  Функции: `test_env_override_prevents_set_value`, `test_env_override_prevents_reset_value`, `test_initialize_skips_db_value_for_env_override`, `test_set_value_applies_without_env_override`
- `tests/services/test_tabpay_client.py` — Python-модуль
  Классы: `RecordingService` (2 методов)
  Функции: `anyio_backend`, `test_create_payment_request_shape`, `test_create_payment_omits_empty_optionals` — Необязательные поля не должны уходить как null — спека их просто не ждёт., `test_create_payment_truncates_to_api_limits` — orderId 1-64 символа, description до 255 — обрезаем на своей стороне., `test_create_payment_rejects_response_without_pay_url` — Без payUrl платёж бесполезен: покупателя некуда вести., `test_create_payment_rejects_response_without_id`, `test_get_payment_by_id_and_by_order_id`, `test_headers_carry_api_key`, `test_base_url_falls_back_and_strips_slash`, `test_error_message_formats` — При ошибках валидации message — массив со всеми проблемами сразу., `test_request_returns_none_on_404_when_allowed`, `test_request_raises_on_404_when_not_allowed`, `test_request_raises_api_error_with_message`, `test_connection_error_becomes_network_error` — Исход неизвестен — вызывающий обязан отличать это от отказа API., `test_timeout_becomes_network_error`
- `tests/services/test_tariff_custom_traffic.py` — Python-модуль
  Классы: нет
  Функции: `test_parse_positive_rubles_to_kopeks`, `test_parse_positive_rubles_to_kopeks_rejects_invalid_values`, `test_parse_positive_gb`, `test_parse_positive_gb_rejects_invalid_values`, `test_valid_custom_traffic_configuration_has_no_errors`, `test_invalid_custom_traffic_configuration_reports_specific_error`, `test_invalid_configuration_reports_all_independent_missing_fields`
- `tests/services/test_tariff_purchase_subscription_pinning.py` — Python-модуль
  Классы: нет
  Функции: `test_select_tariff_period_resolves_and_pins_target_subscription_id` — REGRESSION: ``select_tariff_period`` must resolve the existing, `test_confirm_tariff_purchase_reads_target_subscription_id_from_fsm` — REGRESSION: ``confirm_tariff_purchase`` must read, `test_confirm_tariff_purchase_guards_against_tariff_divergence` — If the FSM-pinned subscription's tariff_id no longer matches, `test_confirm_tariff_purchase_does_not_use_only_tariff_lookup` — Pre-fix shape: confirm_tariff_purchase ran a single
- `tests/services/test_telegram_stars_rate.py` — Python-модуль
  Классы: нет
  Функции: `test_default_stars_rate_is_one_ruble_per_star` — REGRESSION: default rate must stay at 1.0 ₽/⭐., `test_integer_ruble_amounts_round_trip_losslessly` — REGRESSION: at rate=1.0, integer ruble top-ups credit back exactly., `test_rubles_to_stars_rejects_invalid_rate` — Defensive check: zero/negative rate must raise rather than divide-by-zero., `test_rubles_to_stars_clamps_to_minimum_one_star` — Even at rate=1.0, a 0 ₽ request must return ≥1 ⭐ (Telegram requires positive amount)., `test_rate_change_is_propagated_through_telegram_stars_service` — `TelegramStarsService.calculate_*` helpers must defer to settings — no hardcoded copies.
- `tests/services/test_ticket_reply_email.py` — Python-модуль
  Классы: нет
  Функции: `sent` — Перехватывает send_notification роутера., `last_message` — Подменяет чтение последнего сообщения тикета (проверка на фото)., `test_email_user_gets_ticket_reply_email`, `test_photo_reply_marked_in_context`, `test_long_reply_is_previewed`, `test_telegram_user_does_not_get_email` — Юзеру с Telegram ответ уже ушёл в бот — письмо было бы дублем., `test_disabled_toggle_blocks_email`, `test_global_notifications_switch_does_not_mute_support_replies` — ENABLE_NOTIFICATIONS не должен глушить ответ поддержки только email-юзеру., `test_user_without_verified_email_is_skipped`, `test_delivery_failure_does_not_raise`, `test_template_renders_for_supported_languages`, `test_template_escapes_html_in_preview` — Ответ поддержки вида «откройте <config>» не должен ломать вёрстку письма., `test_template_mentions_photo_when_reply_has_one`
- `tests/services/test_traffic_daily_sum.py` — Python-модуль
  Классы: нет
  Функции: `test_sums_per_node_totals`, `test_the_old_top_level_total_is_not_used` — Регрессия, ради которой всё и переписывалось., `test_falls_back_to_daily_breakdown_when_a_node_has_no_total`, `test_accepts_a_flat_list_of_series`, `test_tolerates_garbage_without_raising`, `test_daily_window_is_one_complete_previous_day` — Окно обязано быть ОДНИМИ полными сутками, и не текущими.
- `tests/services/test_traffic_monitoring_redis.py` — Python-модуль
  Классы: нет
  Функции: `service` — Создаёт экземпляр сервиса для тестов., `mock_cache` — Мок для cache сервиса., `sample_snapshot` — Пример snapshot данных: ключ — числовой id панельного юзера (Remnawave 3.0.0)., `stored_snapshot` — Тот же snapshot в том виде, в котором лежит в Redis: JSON не умеет числовые ключи., `test_redis_keys_are_versioned_for_panel_id_identity` — Префикс v3 — версия идентичности панельного юзера. До Remnawave 3.0.0 те же, `test_save_snapshot_to_redis_success` — Тест успешного сохранения snapshot в Redis., `test_save_snapshot_to_redis_failure` — Тест неудачного сохранения snapshot в Redis., `test_save_snapshot_to_redis_exception` — Тест обработки исключения при сохранении., `test_load_snapshot_from_redis_success` — Тест успешной загрузки snapshot из Redis: строковые ключи возвращаются к числовым id., `test_load_snapshot_from_redis_empty` — Тест загрузки когда snapshot отсутствует., `test_load_snapshot_from_redis_invalid_data` — Тест загрузки невалидных данных., `test_load_snapshot_from_redis_skips_non_numeric_keys` — Непригодный ключ (например, протухший UUID) пропускается поштучно, а не роняет, `test_load_snapshot_from_redis_exception` — Тест обработки исключения при загрузке., `test_get_snapshot_time_from_redis_success` — Тест получения времени snapshot., `test_get_snapshot_time_from_redis_empty` — Тест когда время отсутствует., `test_has_snapshot_redis_exists` — Тест has_snapshot когда snapshot есть в Redis., `test_has_snapshot_memory_fallback` — Тест has_snapshot с fallback на память., `test_has_snapshot_none` — Тест has_snapshot когда snapshot нет нигде., `test_get_snapshot_age_minutes_from_redis` — Тест возраста snapshot из Redis., `test_get_snapshot_age_minutes_memory_fallback` — Тест возраста snapshot из памяти., `test_get_snapshot_age_minutes_no_snapshot` — Тест возраста когда snapshot нет., `test_save_snapshot_redis_success` — Тест сохранения snapshot в Redis успешно., `test_save_snapshot_fallback_to_memory` — Тест fallback на память когда Redis недоступен., `test_get_current_snapshot_from_redis` — Тест получения snapshot из Redis., `test_get_current_snapshot_fallback_to_memory` — Тест fallback на память., `test_save_notification_to_redis` — Тест сохранения времени уведомления. Кулдаун заключён на числовой id панели., `test_get_notification_time_from_redis` — Тест получения времени уведомления., `test_should_send_notification_no_previous` — Тест should_send_notification когда уведомлений не было., `test_should_send_notification_cooldown_active` — Тест should_send_notification когда кулдаун активен., `test_should_send_notification_cooldown_expired` — Тест should_send_notification когда кулдаун истёк., `test_should_send_notification_memory_fallback_keyed_by_panel_id` — Fallback на память тоже заключён на числовой id — ключ должен совпасть., `test_record_notification_redis` — Тест record_notification сохраняет в Redis., `test_record_notification_fallback_to_memory` — Тест record_notification с fallback на память., `test_create_initial_snapshot_uses_existing_redis` — Тест что create_initial_snapshot использует существующий snapshot из Redis., `test_create_initial_snapshot_creates_new` — Тест создания нового snapshot когда в Redis пусто., `test_create_initial_snapshot_skips_user_without_panel_id` — Юзер без числового id непригоден как ключ snapshot — пропускаем, а не падаем., `test_cleanup_notification_cache_removes_old` — Тест очистки старых записей из памяти.
- `tests/services/test_traffic_monitoring_status_filter.py` — Python-модуль
  Классы: нет
  Функции: `service`, `test_disabled_and_expired_are_filtered_out` — DISABLED/EXPIRED отсекаются, ACTIVE/LIMITED остаются., `test_all_active_pass_through` — Когда все активны — ничего не теряется., `test_all_inactive_returns_empty` — Сплошь DISABLED/EXPIRED → пустой список (никого не проверяем)., `test_filter_applies_across_paginated_batches` — Фильтр работает на каждом батче; пагинация — по сырому размеру страницы.
- `tests/services/test_user_action_log_service.py` — Python-модуль
  Классы: нет
  Функции: `test_defaults`, `test_normalize_cabinet_path`, `test_should_log_gates`, `test_schedule_skips_when_gated`, `test_write_uses_stats_service`
- `tests/services/test_user_created_event.py` — Python-модуль
  Классы: нет
  Функции: `test_emit_user_created_event_uses_persisted_user`
- `tests/services/test_user_device_alias.py` — Python-модуль
  Классы: нет
  Функции: `test_normalize_alias_basic`, `test_normalize_alias_caps_at_max_length`, `test_normalize_alias_preserves_unicode`, `test_attach_aliases_to_devices_sets_local_name_when_match`, `test_attach_aliases_to_devices_handles_empty_aliases`, `test_attach_aliases_to_devices_handles_missing_hwid` — Device without hwid key — alias merge must not crash, just yield None., `test_attach_aliases_to_devices_is_in_place_mutation` — The helper mutates each dict for cheap downstream rendering., `test_attach_aliases_empty_alias_string_falls_back_to_none` — `''` in the alias dict is treated as 'not set' so renderers fall back., `test_set_alias_rejects_empty_input` — set_alias is the explicit setter — empty input must raise, not silently delete., `test_set_alias_executes_on_conflict_update_touching_updated_at` — The compiled statement must update both `alias` AND `updated_at`., `test_set_alias_with_commit_false_does_not_commit` — commit=False defers commit to caller (cabinet route session middleware)., `test_set_alias_with_commit_true_does_commit` — commit=True (default) commits — used by bot FSM handler that has no session middleware., `test_upsert_alias_with_empty_input_calls_delete` — Legacy upsert wrapper: empty/whitespace input → delete_alias path.
- `tests/services/test_user_revival_service.py` — Python-модуль
  Классы: нет
  Функции: `db`, `test_revive_flips_status_to_active`, `test_revive_preserves_balance_and_referral_state` — The cabinet revival path is NOT a wipe — value-bearing fields stay., `test_revive_never_commits_caller_owns_transaction` — Architect's call: revive_deleted_user must NEVER commit., `test_revive_raises_when_already_active` — Misuse-guard: revive must NEVER silently no-op on ACTIVE rows., `test_revive_raises_on_blocked_user` — A BLOCKED admin-action row is a separate domain — revival is wrong here.
- `tests/services/test_webhook_sibling_expiry.py` — Python-модуль
  Классы: нет
  Функции: `test_pre_multitariff_sibling_with_future_end_date_not_expired`, `test_sibling_alive_in_panel_via_user_panel_id_fallback_not_expired`, `test_sibling_not_expired_on_transient_api_error`, `test_sibling_not_expired_on_panel_validation_400` — 400 VALIDATION — не «пользователя нет»., `test_sibling_not_expired_when_local_panel_id_is_unusable` — Битый локальный идентификатор — баг данных бота, а не «юзера нет»., `test_sibling_genuinely_gone_is_still_expired` — Don't break legitimate expiry: panel says gone (None == 404) + past end_date -> expire., `test_intentional_panel_deletion_suppresses_sibling_sweep`, `test_single_tariff_sibling_with_its_own_live_account_is_not_expired` — Однотарифный режим не покрывался ни одним тестом., `test_single_tariff_sibling_without_any_live_account_is_expired` — А если и его собственный ключ панель не знает — истекаем, это и есть смысл цикла.
- `tests/services/test_yandex_purchase_hook.py` — Python-модуль
  Классы: нет
  Функции: `test_passes_cid_through_to_store_and_fires_purchase` — Frontend cached CID → backend stores it, then fires purchase event., `test_no_cid_still_fires_purchase_event` — If the separate /yandex-cid POST already completed, frontend may pass, `test_disabled_feature_skips_everything` — When offline conversions are off, neither store nor fire should run., `test_store_failure_does_not_block_purchase_event` — Even if persisting the CID throws, the purchase event must still fire —
- `tests/services/test_yookassa_service_adapter.py` — Python-модуль
  Классы: `DummyLoop` (1 методов)
  Функции: `anyio_backend`, `test_init_without_credentials`, `test_create_payment_success`, `test_create_payment_without_contacts`, `test_create_payment_returns_none_when_not_configured`, `test_create_sbp_payment_success`
- `tests/services/test_yookassa_timeout_hardening.py` — Python-модуль
  Классы: нет
  Функции: `test_apiclient_patch_helper_exists_and_runs_at_import` — Source-level pin: ``_patch_yookassa_timeout`` must be DEFINED, `test_patched_execute_passes_timeout_to_session_request` — Negative-control against upstream regression: the patched, `test_patch_idempotency_guard_exists` — The patch helper must check ``ApiClient._timeout_patched`` to, `test_patch_respects_settings_overrides` — An operator who sets YOOKASSA_HTTP_CONNECT_TIMEOUT or, `test_dedicated_executor_exists_with_bounded_max_workers` — The bug-report's "обязательное" fix #2: dedicated executor with, `test_max_workers_resolver_respects_setting` — REGRESSION: ``YOOKASSA_MAX_CONCURRENT_REQUESTS`` env var must flow, `test_max_workers_resolver_floors_at_one` — A misconfigured ``YOOKASSA_MAX_CONCURRENT_REQUESTS=0`` must NOT, `test_dedicated_executor_thread_name_prefix` — Threads in the YK executor must be identifiable in py-spy /, `test_all_run_in_executor_callsites_use_dedicated_pool` — Source-level pin: every ``run_in_executor`` in, `test_webhook_uses_wait_for_with_tight_budget` — ``process_yookassa_webhook`` cross-check of payment status must, `test_webhook_handles_timeout_with_payload_fallback` — When the API cross-check times out, the handler must NOT raise.

#### tests/services/reachability

- `tests/services/reachability/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `tests/services/reachability/conftest.py` — Python-модуль
  Классы: нет
  Функции: `session_factory`
- `tests/services/reachability/fakes.py` — Python-модуль
  Классы: `FakeClock` (3 методов), `FakeAPI` (12 методов)
  Функции: нет
- `tests/services/reachability/test_background.py` — Python-модуль
  Классы: нет
  Функции: `test_start_background_is_idempotent_and_stop_cancels`, `test_failed_background_is_restarted_on_next_start`, `test_stop_without_start_is_noop`
- `tests/services/reachability/test_batches.py` — Python-модуль
  Классы: нет
  Функции: `test_batch_crud_roundtrip`, `test_jobs_for_batch_are_ordered_and_carry_legs`, `test_chunk_targets_by_ten`, `test_estimate_minutes_grows_with_rounds_and_units`, `test_batch_status_rules`, `test_batch_cost_and_done_targets`, `make_batch`, `test_batch_driver_runs_at_most_three_jobs_at_once`, `test_cancel_batch_stops_pending_jobs_and_finishes_cancelled`, `test_sweep_resumes_unfinished_batch`
- `tests/services/reachability/test_batches_service.py` — Python-модуль
  Классы: `ManyHostsPanel` (1 методов)
  Функции: `payload`, `test_preview_batch_sums_chunks_and_estimates_time`, `test_preview_batch_rejects_empty_and_oversized_scope`, `test_create_batch_makes_one_job_per_chunk_and_spawns_driver`, `test_create_batch_refuses_when_balance_is_short`, `test_cancel_batch_before_start_finishes_it_cancelled`
- `tests/services/reachability/test_gate.py` — Python-модуль
  Классы: `FakeClock` (3 методов)
  Функции: `test_spaces_calls_by_min_interval`, `test_retries_rate_limited_with_retry_after_and_same_call`, `test_gives_up_after_max_rate_limit_retries`, `test_other_errors_pass_through_immediately`, `test_lock_is_not_held_during_the_call`
- `tests/services/reachability/test_jobs.py` — Python-модуль
  Классы: нет
  Функции: `body`, `make_job`, `make_runner`, `load`, `test_probe_happy_path_stores_result_cost_and_legs`, `test_probe_gateway_timeout_then_in_progress_then_result`, `test_probe_left_retrieving_when_result_never_comes`, `test_probe_retrieve_keeps_last_api_answer_on_job` — Пока результат не пришёл, в задаче виден последний ответ API — иначе «идёт» ничего не объясняет., `test_probe_older_than_cap_fails_instead_of_retrying_forever` — Обходчик не должен поднимать пробу вечно: старше потолка — падение с внятной причиной, без вызовов API., `test_probe_younger_than_cap_keeps_retrieving_on_resume`, `test_probe_retrieve_slows_down_after_fast_window`, `test_probe_no_dpi_on_race_fails_without_charge`, `test_probe_validation_error_fails_with_api_message`, `test_probe_transient_503_is_retried_with_same_key`, `test_probe_transient_errors_give_up_after_retries`, `test_unexpected_exception_marks_internal_error`, `test_run_skips_finished_and_missing_jobs`, `test_vless_happy_path`, `test_vless_over_cost_limit_is_cancelled_right_after_submit`, `test_vless_busy_fails_fast_and_retryable`, `test_vless_gateway_error_on_submit_is_replayed_with_same_key`, `test_vless_not_found_state_fails_job`, `test_vless_cancel_marks_phase_and_resume_finalizes_as_cancelled` — Отмена только дёргает API и ставит фазу; финал ставит поллер/обходчик по GET., `test_vless_cancelled_before_any_leg_started_is_cancelled_by_phase` — Незапущенные леги в результате отсутствуют: пустой результат при фазе cancelling — отмена., `test_cancel_tolerates_test_already_finishing`, `test_cancel_rejects_probe_and_finished_jobs`, `test_scan_happy_path`, `test_scan_cancelled_state_from_get`, `test_scan_failed_state_propagates_error_and_retryable`, `test_scan_lost_on_service_side_fails_job`, `test_scan_cancel_calls_api`, `test_poll_timeout_leaves_job_running_and_sweep_resumes_it`, `test_sweep_skips_fresh_and_active_jobs`, `test_sweeper_loop_runs_until_stopped`, `test_spawn_tracks_task_until_done`, `test_scan_progress_is_stored_while_running` — Пока скан идёт, его progress из GET лежит в задаче — кабинет показывает, сколько адресов уже проверено.
- `tests/services/reachability/test_legs.py` — Python-модуль
  Классы: нет
  Функции: `test_probe_legs_from_recorded_full_response`, `test_probe_legs_for_unknown_target_fall_back_to_api_key`, `test_probe_legs_match_api_target_case_insensitively`, `test_vless_legs_match_by_server_addr_and_compose_op_key`, `test_vless_legs_fall_back_to_server_name_then_raw_address`, `test_vless_op_key_from_leg_fields`, `test_merge_skipped_keeps_ours_and_adds_api_lists_without_mutation`
- `tests/services/reachability/test_links.py` — Python-модуль
  Классы: нет
  Функции: `test_parses_vless_with_sni_and_decoded_name`, `test_parses_other_protocols`, `test_stub_links_from_subscription_page_are_rejected`, `test_unknown_scheme_and_garbage_are_rejected_with_reason`, `test_multiple_lines_keep_order_and_skip_blank_lines`, `test_max_configs_constant_matches_api_limit`, `test_expand_raw_input_splits_lines_decodes_base64_and_keeps_urls`
- `tests/services/reachability/test_panel_links.py` — Python-модуль
  Классы: `FakePanel` (4 методов)
  Функции: `test_protected_endpoint_wins_when_it_has_links`, `test_falls_back_to_legacy_info_when_protected_fails_or_is_empty`, `test_falls_back_to_public_subscription_with_client_user_agent`, `test_everything_empty_gives_empty_list_not_error`, `test_prefer_public_takes_client_view_first_and_repeats_with_hwid_headers`, `test_decode_subscription_body_understands_xray_json`, `test_decode_subscription_body_accepts_plain_base64_and_garbage`
- `tests/services/reachability/test_partial_progress.py` — Python-модуль
  Классы: нет
  Функции: `test_compact_verdict_prefers_sni_then_tcp_then_icmp`, `test_partial_progress_keeps_order_and_marks_only_done_legs`, `test_partial_progress_tolerates_missing_fields`, `test_runner_stores_partial_progress_while_probe_runs` — Пока результат не пришёл, в задаче лежит частичный результат — кабинет показывает «проверяем…» по симкам., `test_runner_leaves_no_partial_when_api_gave_none` — Голый 409 без легов — след ответа есть, «partial» не выдумывается.
- `tests/services/reachability/test_pricing.py` — Python-модуль
  Классы: нет
  Функции: `test_credits_are_kopeks`, `test_vless_estimate_uses_last_leg_price_or_default`, `test_cost_limit_zero_means_unlimited`, `test_cost_limit_exceeded_carries_numbers`, `test_format_rubles`
- `tests/services/reachability/test_probe_cancel.py` — Python-модуль
  Классы: нет
  Функции: `test_cancel_probe_marks_cancelled_with_partial_cost`, `test_cancel_probe_when_api_says_already_finished_is_fine`, `test_cancel_probe_before_submit_is_rejected`
- `tests/services/reachability/test_requests.py` — Python-модуль
  Классы: нет
  Функции: `test_probe_request_has_targets_units_probes_and_sni_hosts`, `test_probe_request_without_sni_omits_sni_hosts_and_skips_cidr_targets`, `test_probe_request_normalizes_partial_probes_dict`, `test_probe_request_rejects_no_probes_and_no_targets`, `test_vless_request_joins_raw_links_and_limits_20`, `test_scan_request`, `test_probe_request_limits_targets_to_api_maximum`, `test_sni_hosts_prefer_sni_and_skip_bare_ip_targets` — Имя для TLS-SNI — SNI цели, а без него домен; у голого IP имени нет (RFC 6066)., `test_probe_request_with_sni_but_only_ip_targets_fails_fast_without_default`, `test_explicit_sni_hosts_win_over_target_names_and_allow_bare_ip_targets`, `test_default_sni_is_used_when_targets_have_no_names`, `test_normalize_sni_hosts_dedupes_lowercases_and_limits_to_five`, `test_scan_request_takes_explicit_names_or_default`
- `tests/services/reachability/test_resolver.py` — Python-модуль
  Классы: нет
  Функции: `test_hosts_hide_disabled_by_default_and_guess_purpose`, `test_sources_are_fetched_once_per_resolver`, `test_prefs_override_guess_and_mark_excluded`, `test_pref_with_unknown_purpose_keeps_guess`, `test_nodes_expose_icmp_target_and_linked_hosts`, `test_subscription_configs_parse_links_and_reject_stubs`, `test_resolve_mixed_items_dedups_by_target_key`, `test_resolve_host_applies_prefs`, `test_resolve_custom_link_becomes_config_target`, `test_resolve_reports_unknown_targets`, `test_subscription_configs_from_url_use_url_fetcher_and_url_refs`, `test_subscription_config_with_stale_target_key_is_rejected`, `test_url_source_without_fetcher_is_explained`
- `tests/services/reachability/test_service.py` — Python-модуль
  Классы: `FakePanel` (5 методов), `FakeClient` (5 методов)
  Функции: `make_service`, `test_disabled_integration_raises`, `test_missing_key_is_reported_as_not_configured`, `test_status_reports_balance_without_secret_and_reference`, `test_status_lists_active_jobs_and_missing_reference`, `test_auth_error_marks_integration_unhealthy_for_a_while`, `test_account_is_cached_between_calls`, `test_units_filters_locally_over_cached_catalog`, `test_hosts_nodes_and_configs_go_through_panel`, `test_panel_failure_becomes_panel_unavailable`, `test_subscription_configs_for_user_without_subscription_explains` — Пользователь без подписки панели — своя ошибка, а не жалоба на эталон из настроек., `test_subscription_configs_without_reference_raise`, `test_preview_probe_expands_units_reports_skipped_and_exact_price`, `test_preview_probe_warns_about_bs_host_without_sni_probe`, `test_preview_unknown_selector_is_rejected_before_api`, `test_preview_unknown_kind_is_rejected`, `test_preview_vless_is_an_estimate`, `test_preview_scan_uses_cidr_and_exact_price`, `test_preview_without_units_left_warns`, `test_create_job_writes_row_and_spawns_runner`, `test_create_job_returns_job_ready_for_response` — Роут сериализует созданную задачу сразу, включая ``legs``., `test_create_job_refuses_second_active_vless`, `test_create_job_enforces_cost_limit_and_units`, `test_create_job_refuses_when_balance_is_short`, `test_get_cancel_and_retrieve_jobs`, `test_retrieve_job_resumes_stuck_probe`, `test_summary_builds_matrix_from_latest_legs`, `test_summary_survives_panel_and_api_outage`, `test_update_pref_persists_and_changes_summary_purpose`, `test_background_sweeper_starts_and_stops`, `test_preview_probe_uses_explicit_sni_hosts_for_bare_ip`, `test_preview_probe_falls_back_to_built_in_default_sni`, `test_preview_scan_with_sni_takes_names_from_payload`, `test_parse_input_direct_links_become_custom_targets`, `test_parse_input_own_panel_url_resolves_through_panel_api`, `test_parse_input_foreign_url_is_fetched_and_referenced_by_url`, `test_parse_input_unreachable_url_is_rejected_not_raised`, `test_parse_input_base64_blob_expands_to_links`
- `tests/services/reachability/test_subscriptions.py` — Python-модуль
  Классы: `FakeSession` (3 методов)
  Функции: `test_is_subscription_url_and_validate_public_url`, `test_fetch_decodes_base64_body_with_client_user_agent`, `test_fetch_repeats_with_device_headers_when_panel_requires_hwid`, `test_fetch_accepts_plain_links_and_rejects_pages_errors_and_private_redirects`, `test_public_only_resolver_passes_public_and_rejects_private`, `test_fetch_uses_public_only_resolver_for_the_real_connection` — Домен, глядящий во внутреннюю сеть, отсекается настоящим aiohttp ещё до соединения.
- `tests/services/reachability/test_summary.py` — Python-модуль
  Классы: нет
  Функции: `test_rows_follow_panel_order_and_carry_purpose_and_cells`, `test_excluded_hosts_are_hidden_even_if_they_have_legs`, `test_legs_of_targets_missing_from_panel_go_last_with_pref_purpose`, `test_rows_are_new_objects_and_inputs_untouched`
- `tests/services/reachability/test_targets.py` — Python-модуль
  Классы: нет
  Функции: `test_normalize_custom_target`, `test_normalize_rejects_private_and_malformed`, `test_target_key_is_lowercase_with_optional_port`, `test_probe_api_target_keeps_port`, `test_is_reality_like`, `test_guess_purpose`, `test_cidr_helpers` — Документационные диапазоны (192.0.2.0/24 и т. п.) не глобальные — их тоже режем., `test_hosts_for_node_matches_by_inbound_then_by_address`, `test_target_round_trips_through_dict`
- `tests/services/reachability/test_units.py` — Python-модуль
  Классы: нет
  Функции: `catalog`, `test_catalog_from_response`, `test_parse_selector`, `test_parse_selector_rejects`, `test_expand_bare_operator_with_dpi_on_skips_off_units`, `test_expand_region_selector_and_latin_code`, `test_expand_any_keeps_both_groups_and_dedups`, `test_expand_empty_means_whole_fleet_by_dpi`, `test_expand_reports_unknown_selectors_instead_of_dropping_them`, `test_expand_marks_non_probeable_units_unavailable`, `test_expand_rejects_unknown_dpi_mode`, `test_cache_refetches_after_ttl_and_on_force`
- `tests/services/reachability/test_verdict.py` — Python-модуль
  Классы: нет
  Функции: `test_probe_leg_verdict_on_recorded_legs`, `test_reality_tls_blocked_without_sni_probe_is_unknown`, `test_sni_entry_in_evidence_shape_is_understood` — Проба по флоту отдаёт sni[] без поля host: имя лежит в evidence.sni, форма другая., `test_probe_leg_not_executed_is_unknown`, `test_icmp_only_probe`, `test_vless_verdicts_on_recorded_legs`, `test_vless_other_protocol_fail_reasons`, `test_matches_expectation`, `test_explicit_sni_names_any_alive_is_reachable_all_blocked_is_blocked` — Multi-SNI по своим именам: ни одно не совпадает с SNI хоста — судим по совокупности.
- `tests/services/reachability/test_xray_json.py` — Python-модуль
  Классы: нет
  Функции: `test_balancer_config_expands_into_one_link_per_outbound_with_tag_labels`, `test_trojan_and_shadowsocks_outbounds_and_ws_transport`, `test_not_json_or_no_proxies_gives_empty`

### tests/utils

- `tests/utils/__init__.py` — Python-модуль
  Классы: нет
  Функции: нет
- `tests/utils/test_chat_menu_button.py` — Python-модуль
  Классы: нет
  Функции: `test_disabled_does_not_touch_menu_button`, `test_enabled_sets_webapp_menu_button`, `test_falls_back_to_miniapp_custom_url`, `test_non_https_url_is_skipped`, `test_empty_text_defaults`
- `tests/utils/test_display_mode.py` — Python-модуль
  Классы: нет
  Функции: `test_normalize_display_mode_known_values`, `test_normalize_display_mode_fallback_to_both`, `test_visibility_matrix`, `test_next_display_mode_cycles_through_all_modes`, `test_display_mode_label_known_for_all_modes`, `test_config_defaults_are_both`, `test_settings_registered_in_info_pages_category`
- `tests/utils/test_email_alias.py` — Python-модуль
  Классы: `TestCanonicalEmail` (5 методов), `TestIsEmailAliasOf` (4 методов), `TestHelpers` (2 методов)
  Функции: нет
- `tests/utils/test_formatters_basic.py` — Python-модуль
  Классы: нет
  Функции: `test_format_datetime_handles_iso_strings` — ISO-строка должна корректно преобразовываться в отформатированный текст., `test_format_date_uses_custom_format` — Можно задавать собственный шаблон вывода., `test_format_time_ago_returns_human_readable_text` — Разница во времени должна переводиться в человеко-понятную строку., `test_format_days_declension_handles_russian_rules` — Склонение дней в русском языке зависит от числа., `test_format_days_declension_uses_russian_fallback_for_fa` — Для fa используем fallback на русские формы до полной локализации., `test_format_duration_switches_units` — В зависимости от длины интервала выбирается подходящая единица измерения., `test_format_bytes_scales_value` — Размер должен выражаться в наиболее подходящей единице., `test_format_percentage_respects_precision` — Проценты форматируются с нужным количеством знаков., `test_format_number_inserts_separators` — Разделители тысяч должны расставляться корректно как для int, так и для float., `test_truncate_text_appends_suffix` — Строки, превышающие лимит, должны обрезаться и дополняться суффиксом., `test_format_username_prefers_full_name` — Полное имя имеет приоритет, затем username, затем ID., `test_format_subscription_status_handles_active_and_expired` — Статус подписки различается для активных/просроченных случаев., `test_format_traffic_usage_supports_unlimited` — При безлимитном тарифе в строке должна появляться бесконечность., `test_format_boolean_localises_output` — Булевые значения отображаются локализованными словами., `test_format_boolean_uses_russian_fallback_for_fa` — Для fa булевы значения пока используют базовый ru fallback., `test_format_username_link_wraps_telegram_handle_in_anchor` — Rich-сообщения идут со skip_entity_detection=True — ссылка нужна явная., `test_format_username_link_does_not_double_the_at_sign` — Логин может прийти уже с собакой — в тексте она должна остаться одна., `test_format_username_link_returns_fallback_without_username` — Пустой логин отдаётся текстом-заглушкой, без собаки и без ссылки., `test_format_username_link_keeps_non_telegram_logins_as_text` — OAuth-регистрация кладёт в users.username логин Discord/Яндекса., `test_format_username_link_escapes_html_metacharacters` — Значение попадает и в href, и в текст — экранируем оба.
- `tests/utils/test_gift_links.py` — Python-модуль
  Классы: `TestBuildBotGiftClaimLink` (9 методов), `TestBuildCabinetGiftClaimLink` (5 методов), `TestBuildTelegramGiftShareUrl` (8 методов), `TestLandingGiftLinkIntegration` (1 методов), `TestBuildGiftPublicCode` (9 методов), `TestParseGiftClaimInput` (14 методов), `TestGiftClaimArtifacts` (6 методов)
  Функции: нет
- `tests/utils/test_incy_crypt1.py` — Python-модуль
  Классы: `TestEncryptIncyLink` (6 методов), `TestWrapIncyDeepLink` (12 методов)
  Функции: `decrypt` — Decrypt a crypt1 link the way the INCY client does., `incy_enabled`
- `tests/utils/test_lava_display_names.py` — Python-модуль
  Классы: нет
  Функции: `test_lava_sbp_and_card_describe_provider_not_themselves`, `test_lava_generic_method_keeps_provider_description`
- `tests/utils/test_logo_resize_tempdir.py` — Python-модуль
  Классы: нет
  Функции: `test_oversized_logo_resized_into_writable_tempdir`, `test_small_logo_returned_unchanged`
- `tests/utils/test_min_device_limit.py` — Python-модуль
  Классы: нет
  Функции: `test_default_floor_is_tariff_device_limit`, `test_opt_in_restores_previous_behaviour` — Флаг возвращает прежний минимум 1 — для тех, кому нужно самоограничение., `test_without_usable_tariff_limit_floor_is_one` — Классический режим и битые значения не должны блокировать уменьшение., `test_no_tariff_at_all_floor_is_one`, `test_keyboard_hides_values_below_tariff_limit` — Кнопки с запрещёнными значениями не должны показываться вовсе., `test_keyboard_offers_lower_values_when_opted_in`
- `tests/utils/test_panel_node_usage.py` — Python-модуль
  Классы: нет
  Функции: `test_normalizes_the_production_shape` — ЕДИНСТВЕННАЯ форма, которая реально сюда приходит: {userId, nodeUuid, total}., `test_also_tolerates_the_raw_3_0_0_keys` — Сырые ключи панели {id, totalBytes} — запас на случай, если сюда однажды, `test_accepts_the_already_normalised_shape`, `test_falls_back_to_the_requested_node_uuid`, `test_skips_non_dict_entries_and_tolerates_missing_fields`, `test_non_numeric_totals_do_not_raise`, `test_empty_input`
- `tests/utils/test_pricing_utils.py` — Python-модуль
  Классы: `TestCalculatePricePerMonth` (4 методов), `TestBuildDynamicValues` (2 методов)
  Функции: нет
- `tests/utils/test_redis_client.py` — Python-модуль
  Классы: нет
  Функции: `from_url`, `test_factory_disables_maintenance_notifications`, `test_factory_defaults_to_settings_url_and_keeps_explicit_kwargs`, `test_factory_skips_config_on_old_redis_py` — redis-py без модуля maint_notifications: лишний kwarg уронил бы from_url., `test_every_redis_client_in_app_goes_through_factory` — Сторож: прямой ``from_url``/``Redis(`` в app/ вернул бы шум и обошёл общие настройки.
- `tests/utils/test_remnawave_auto_sync.py` — Python-модуль
  Классы: нет
  Функции: `test_parse_daily_time_list`, `test_calculate_next_run_same_day`, `test_calculate_next_run_rollover`, `test_perform_sync_rebuilds_service_on_each_run`
- `tests/utils/test_rich_admin.py` — Python-модуль
  Классы: нет
  Функции: `test_rich_flag_default_is_enabled`, `test_classic_html_to_rich_conversion`, `test_classic_html_emoji_before_bold_becomes_header` — Заголовки вида «🔧 <b>ВКЛЮЧЕНИЕ ТЕХРАБОТ</b>» (эмодзи до тега) тоже выносятся в h6., `test_classic_html_without_bold_header_kept_as_is`, `test_kv_table_escapes_keys_and_keeps_value_html`, `test_traceback_details_escapes_content`, `test_try_send_passes_thread_and_markup`, `test_try_send_disabled_by_setting`, `test_try_send_unsupported_marks_latch`, `test_try_send_render_error_does_not_latch`, `test_try_send_oversized_falls_back`, `test_pre_blocks_survive_conversion` — <pre>-блоки (описание релиза из markdown) сохраняют форматирование —, `test_inline_buttons_move_into_canvas_in_private_admin_chat` — В личке админа Mini App допустим, поэтому переносится вся клавиатура., `test_web_app_button_stays_outside_in_group_admin_chat` — У группы отрицательный id, а Mini App там не откроется — клавиатура остаётся., `test_callback_buttons_move_into_canvas_in_group_admin_chat` — Обычные callback-кнопки в группе переносятся штатно.
- `tests/utils/test_rich_buttons.py` — Python-модуль
  Классы: нет
  Функции: `test_button_types_map_to_spec_tags`, `test_login_url_carries_its_flags`, `test_chosen_chat_flags_are_emitted_as_bare_attributes`, `test_rows_are_preserved_and_align_applied`, `test_long_row_is_split_by_spec_limit` — InputRichBlockButtons: «List of 1-8 buttons». Более длинный ряд сервер отвергнет., `test_unrepresentable_button_keeps_whole_keyboard_outside` — Всё или ничего: половина кнопок внутри — это потерянные кнопки., `test_web_app_is_refused_outside_private_chat` — Mini App открывается только в личке — в группе такая кнопка не сработает., `test_link_style_only_survives_on_callback_buttons` — style="link" спецификация разрешает только callback-кнопкам., `test_unknown_style_is_dropped_not_forwarded`, `test_custom_emoji_icon_moves_into_button_text` — У RichMessageButton нет icon_custom_emoji_id — иконка переносится в текст., `test_text_and_attributes_are_escaped`, `test_nothing_to_move_returns_none`
- `tests/utils/test_rich_menu.py` — Python-модуль
  Классы: `DummyTexts` (2 методов), `PremiumEmojiTexts` (2 методов), `HostileTexts` (1 методов)
  Функции: `test_rich_flag_default_is_enabled`, `test_builder_keeps_premium_emoji_from_operator_texts` — Премиум-эмодзи из текстов меню должны доезжать тегом, а не текстом., `test_builder_strips_disallowed_markup_from_operator_texts` — Из текстов пропускаем только подмножество sanitize_html, а не любой HTML., `test_unlimited_devices_shown_as_infinity_not_hidden` — device_limit = 0 (HWID выключен) — безлимит, а не «нет устройств»., `test_unlimited_devices_in_multi_tariff_table` — То же для строки расхода в таблице мультитарифа., `test_builder_single_subscription_structure`, `test_builder_links_username_used_instead_of_name` — Без имени full_name подставляет логин — показываем его ссылкой на профиль., `test_builder_keeps_plain_name_when_user_has_one` — Имя есть — шапка остаётся обычным текстом, ссылка на логин не подставляется., `test_builder_survives_user_without_username_attribute` — Шапка не должна падать на объекте без username — иначе меню молча уедет в классику., `test_builder_multi_tariff_table`, `test_builder_without_subscription`, `test_builder_hints_in_details_and_random_message_sanitized`, `test_builder_without_hints_has_no_details`, `test_input_rich_message_flags`, `test_try_send_disabled_by_setting`, `test_try_send_unsupported_server_marks_unavailable`, `test_try_send_render_error_does_not_disable_rich`, `test_try_edit_text_message_uses_edit_message_text`, `test_try_edit_photo_message_recreates_via_send`, `test_try_edit_not_modified_is_success`, `test_try_edit_unsupported_on_edit_marks_unavailable`, `test_try_edit_build_failure_falls_back`, `test_try_send_happy_path_sends_rich_message` — Успешная отправка: реальный билдер (застабены только источники контента)., `test_try_send_forbidden_is_handled_without_fallback` — Бот заблокирован: True, чтобы классический рендер не долбил тот же чат., `test_try_edit_forbidden_is_handled_without_fallback`, `test_try_edit_transient_edit_error_falls_back_without_disabling` — 'message to edit not found' — не признак старого сервера: rich остаётся включён,, `test_try_edit_photo_delete_failure_falls_back_to_classic` — deleteMessage запрещён для сообщений старше 48ч: rich не отправляется новым, `test_multi_tariff_table_is_fully_localized` — Все строки таблицы идут через texts.t — маркер-стаб не должен оставить, `test_show_main_menu_prefers_rich_and_falls_back` — Поведенческий тест ветвления show_main_menu: rich True — классика не зовётся,, `test_expired_subscription_renew_link_in_cabinet_mode` — Истёкшая подписка в cabinet-режиме получает ссылку «Продлить» в кабинет., `test_expired_subscription_no_renew_link_outside_cabinet_mode`, `test_single_mode_expired_renew_link`, `test_usage_traffic_and_devices_displayed` — Активная подписка показывает текущий трафик и лимит устройств., `test_usage_row_in_multi_tariff_table`, `test_send_passes_message_effect`, `test_rejected_effect_degrades_and_resends` — Отклонённый эффект: повтор без него, эффект отключается до рестарта., `test_logo_included_from_explicit_url`, `test_logo_auto_url_from_webhook`, `test_logo_fetch_failure_degrades_and_resends` — Telegram не скачал логотип: единственный повтор без логотипа, флаг до рестарта., `test_logo_can_be_disabled_explicitly` — Rich-меню должно работать вообще без логотипа., `test_non_http_logo_value_disables_logo_instead_of_breaking_menu` — «Подставлю не-картинку, чтобы не грузилась» не должно ронять rich в классику., `test_unknown_send_error_retries_without_logo` — Незнакомая ошибка при наличии логотипа — повтор без него, а не уход в классику., `test_unknown_send_error_without_logo_falls_back_to_classic` — Без логотипа повторять нечем — незнакомая ошибка честно уходит в классику., `test_connect_link_for_active_subscription_in_table` — Активная строка таблицы получает «кнопку» подключения — ссылку на subscription_url., `test_connect_link_hidden_when_subscription_link_hidden`, `test_connect_link_uses_happ_redirect_in_happ_mode` — В happ-режиме подключение идёт через https-обёртку редиректа, не через сырую happ://., `test_trial_offer_free_deeplink` — Новый юзер без триала: ссылка t.me/<bot>?start=trial (бесплатный триал)., `test_trial_offer_paid_opens_miniapp` — Платный триал: ссылка ведёт на оплату в миниапп (startapp=trial), не на диплинк., `test_trial_offer_absent_when_trial_used`, `test_multiple_subscriptions_collapse_into_details` — При >1 подписки таблица сворачивается в details со счётчиком в summary., `test_single_multi_tariff_subscription_stays_expanded` — Одна подписка — обычный заголовок и таблица без сворачивания., `test_collapsible_disabled_keeps_plain_table`, `test_collapsible_flag_default_is_enabled`, `test_tg_time_beyond_telegram_date_limit_falls_back_to_text` — Telegram принимает дату сущности только до «сейчас + 1098 дней», `test_strip_tg_time_keeps_inner_text` — Страховка: снимаем теги дат, но текст внутри остаётся., `test_is_rich_date_error_matches_server_code`, `test_send_retries_without_tg_time_on_date_error` — Одна отвергнутая дата не должна ронять меню целиком в классику., `test_tg_time_keeps_past_dates` — Новый лимит не должен задеть обычную истёкшую подписку., `test_tg_time_survives_extreme_datetimes` — datetime.max/min: timestamp() на них падает на части платформ — не роняем меню., `test_far_future_end_date_in_table_renders_without_tg_time` — «Вечная» подписка (например, импорт из панели с датой 2099) не роняет, `test_multi_year_subscription_renders_without_tg_time` — Репорт из поддержки: у юзера подписка на несколько лет вперёд, и /start, `test_far_future_end_date_in_single_block_renders_without_tg_time`, `test_try_send_decode_error_falls_back_to_classic` — ClientDecodeError не наследуется от TelegramAPIError и пролетал мимо всех except., `test_try_edit_decode_error_falls_back_to_classic`, `test_inline_buttons_setting_moves_keyboard_into_canvas` — Bot API 10.3: кнопки уезжают в полотно, клавиатуры под сообщением не остаётся., `test_inline_buttons_setting_off_keeps_classic_keyboard`, `test_unmovable_button_keeps_keyboard_outside` — Если перенести можно не всё — клавиатура остаётся под сообщением целиком., `test_edit_clears_old_keyboard_when_buttons_move_inside` — У editMessageText отсутствующий reply_markup означает «не трогать».
- `tests/utils/test_rich_notify.py` — Python-модуль
  Классы: `TestBuildHtml` (11 методов), `TestSend` (8 методов), `TestDeliveryIntegration` (3 методов), `TestLogoAndTimeout` (4 методов), `TestMonitoringIntegration` (4 методов), `TestBroadcastIntegration` (5 методов), `TestUnsupportedServer` (1 методов)
  Функции: нет
- `tests/utils/test_security.py` — Python-модуль
  Классы: нет
  Функции: `test_hash_api_token_default_algorithm_matches_hashlib` — Проверяем, что алгоритм по умолчанию совпадает с hashlib.sha256., `test_hash_api_token_accepts_supported_algorithms` — Каждый поддерживаемый алгоритм должен выдавать корректный результат., `test_hash_api_token_rejects_unknown_algorithm` — Некорректное имя алгоритма должно приводить к ValueError., `test_generate_api_token_respects_length_bounds` — Функция должна ограничивать длину токена безопасным диапазоном., `test_generate_api_token_produces_random_values` — Два последовательных вызова должны выдавать разные токены.
- `tests/utils/test_tag_stripping_is_linear.py` — Python-модуль
  Классы: нет
  Функции: `test_no_quadratic_tag_pattern_remains` — Шаблон `<[^>]+>` не должен вернуться ни в один модуль., `test_stripping_keeps_text_and_bare_angle_brackets`, `test_pathological_input_stays_fast` — Строка из одних «<» — ровно тот вход, на котором старый шаблон вставал., `test_visible_length_uses_the_linear_pattern` — Функция, на которую указал CodeQL, считает длину тем же способом., `test_html_validator_stays_fast_on_unclosed_tags` — Проверка HTML правовых страниц: их длина из кабинета ничем не ограничена., `test_html_validator_verdicts_unchanged` — Ускорение не должно менять вердикты на обычной разметке.
- `tests/utils/test_telegram_html.py` — Python-модуль
  Классы: нет
  Функции: `test_keeps_allowed_inline_tags`, `test_maps_tag_aliases_to_telegram_tags`, `test_strips_unsupported_tags_but_keeps_text`, `test_drops_script_and_iframe_content`, `test_paragraphs_become_blank_lines`, `test_br_becomes_newline`, `test_unordered_list_items_get_bullets`, `test_ordered_list_items_get_numbers`, `test_heading_becomes_bold_block`, `test_link_kept_only_with_http_href`, `test_oversized_href_drops_anchor_but_keeps_text`, `test_misnested_skip_closers_recover`, `test_text_entities_are_escaped`, `test_unclosed_tags_are_closed`, `test_blockquote_and_code_preserved`, `test_split_short_text_single_chunk`, `test_split_empty_returns_empty_list`, `test_split_respects_paragraph_boundaries`, `test_split_hard_splits_oversized_paragraph`, `test_split_closes_open_tags_in_each_chunk`, `test_split_never_exceeds_telegram_hard_limit`, `test_split_link_text_spanning_chunks_stays_within_hard_limit`, `test_hard_split_backs_off_incomplete_entity`, `test_faq_content_rendered_as_question_blocks`, `test_faq_content_invalid_json_returns_empty`
- `tests/utils/test_text_search_case_insensitive.py` — Python-модуль
  Классы: нет
  Функции: `test_sqlite_lower_really_is_ascii_only` — Фиксируем причину бага: без наших вариантов ILIKE по кириллице не сработал бы., `test_ascii_term_stays_a_single_pattern` — Для ASCII ILIKE справляется сам — лишние OR только замедлили бы запрос., `test_cyrillic_term_expands_to_case_variants`, `test_variants_are_deduplicated_for_single_case_terms`, `test_search_finds_capitalized_name_in_any_case` — Ровно репорт: имя записано «Позитив», ищут как угодно — находиться должно всегда., `test_search_finds_any_stored_case`, `test_multiword_name_is_found_in_lowercase`, `test_ascii_search_still_works` — Латиница не должна пострадать от изменения., `test_search_still_filters_out_non_matches` — Регистронезависимость не должна превратить поиск в «находит всё»., `test_telegram_id_search_unaffected`
- `tests/utils/test_ticket_text.py` — Python-модуль
  Классы: нет
  Функции: `test_short_ticket_stays_on_one_page`, `test_long_message_is_fully_visible_across_pages`, `test_no_page_exceeds_telegram_limit`, `test_split_never_breaks_html_entities`, `test_long_header_still_leaves_room_for_content`, `test_every_page_repeats_header`, `test_empty_ticket_returns_header_page`, `test_preview_marks_cut_text`, `test_bot_limit_matches_cabinet_and_webapi` — Ровно это расхождение и породило баг: бот резал 500, кабинет принимал 4000.
- `tests/utils/test_validators_basic.py` — Python-модуль
  Классы: нет
  Функции: `test_validate_email_handles_expected_patterns` — Проверяем типичные корректные и некорректные адреса., `test_validate_phone_strips_formatting_and_checks_pattern` — Телефон должен соответствовать стандарту E.164 после очистки., `test_validate_telegram_username_enforces_length` — Telegram-логин должен быть 5-32 символов и содержать допустимые символы., `test_validate_amount_returns_float_within_bounds` — Числа должны конвертироваться с уважением к диапазону., `test_validate_positive_integer_enforces_upper_bound` — Положительное целое число выходит за пределы — возвращаем None., `test_validate_traffic_amount_supports_units` — Валидатор трафика распознаёт разные единицы измерения и особые значения., `test_validate_subscription_period_accepts_reasonable_range` — Диапазон допустимой длительности от 1 до 3650 дней., `test_validate_uuid_detects_standard_format` — UUID должен соответствовать HEX шаблону версии 4/5., `test_validate_url_recognises_https_links` — Валидатор URL допускает http/https ссылки и отклоняет произвольные строки., `test_validate_html_tags_rejects_unknown_tags` — Неизвестные HTML теги должны приводить к отказу., `test_validate_html_structure_detects_wrong_nesting` — Неправильная вложенность тегов должна сообщаться пользователю., `test_fix_html_tags_repairs_missing_quotes` — Автоисправление должно добавлять кавычки у ссылок., `test_validate_rules_content_detects_structure_error` — При нарушении структуры должны вернуться сообщение и отсутствие подсказки., `test_validate_rules_content_accepts_supported_markup` — Корректный HTML должен проходить проверку без сообщений.

### tests/webapi

- `tests/webapi/test_ban_notification_schema.py` — Python-модуль
  Классы: нет
  Функции: `test_typed_ban_notification_types_are_accepted`, `test_unknown_typed_ban_notification_is_rejected`, `test_invalid_numeric_values_are_rejected`, `test_invalid_typed_ban_template_uses_fallback`, `test_unknown_typed_ban_type_returns_safe_error`, `test_external_values_are_escaped_before_html_send` — Имя ноды и тип сети приходят снаружи и уезжают в сообщение с parse_mode=HTML., `test_typed_ban_reason_is_escaped` — Причина бана тоже приходит снаружи — экранируем., `test_warning_text_is_escaped` — Текст предупреждения приходит по API и не должен ломать разметку., `test_revoke_uses_its_own_template` — revoke — это сброс ключей, а не бан: текст должен отличаться от punishment., `test_unexpected_error_returns_500_not_typeerror` — Неожиданная ошибка обязана превращаться в 500, а не в TypeError.
- `tests/webapi/test_broadcast_list_nullable_text.py` — Python-модуль
  Классы: нет
  Функции: `test_row_without_text_serializes` — Email-рассылка без текста отдаётся как есть, а не ломает сериализацию., `test_one_empty_row_does_not_break_the_whole_list` — Соседние рассылки обязаны доехать до ответа вместе с пустой., `test_list_endpoint_returns_rows_with_null_text` — Сам маршрут отвечает 200, а не 500, когда в выборку попала пустая строка.
- `tests/webapi/test_promocode_traffic_roundtrip.py` — Python-модуль
  Классы: нет
  Функции: `test_traffic_survives_create_and_read_back` — Созданный через API код хранит трафик и отдаёт его обратно., `test_traffic_is_updatable` — PATCH меняет трафик, а не молча отвечает 200 со старым значением., `test_traffic_only_set_is_not_created_empty` — Набор из одного трафика создаётся именно трафиком, а не пустышкой., `test_update_cannot_empty_a_live_bonus_set` — Правка не должна обнулять живой набор до кода, который ничего не даёт., `test_update_may_empty_days_when_traffic_remains` — Обнулить дни можно, если в наборе остаётся трафик — набор непустой., `test_negative_traffic_rejected` — Отрицательный трафик отклоняется и на создании, и на правке.
- `tests/webapi/test_subscription_sync_routes.py` — Python-модуль
  Классы: нет
  Функции: `test_users_subscription_trial_calls_remnawave_sync`, `test_users_subscription_paid_calls_remnawave_sync`, `test_users_search_filter_adds_internal_id_for_int32`, `test_users_search_filter_skips_internal_id_for_out_of_int32`, `test_subscriptions_extend_calls_remnawave_sync`, `test_subscriptions_extend_rolls_back_when_sync_fails`, `test_subscriptions_extend_returns_500_when_rollback_fails`, `test_users_patch_subscription_delegates_to_post` — PATCH /users/{id}/subscription is a documented alias for POST and must route, `test_users_patch_subscription_route_returns_201` — The PATCH-as-upsert alias is intentionally annotated 201 (not the REST-typical 200), `test_users_subscription_replace_existing_restores_on_sync_failure` — When replace_existing=True and Remnawave sync fails, the user's prior subscription

### tests/webserver

- `tests/webserver/test_apple_iap_webhook.py` — Python-модуль
  Классы: нет
  Функции: `test_apple_iap_webhook_rejects_unsupported_media_type`, `test_apple_iap_webhook_rejects_body_larger_than_256kb`, `test_apple_iap_webhook_maps_invalid_signature_to_403`, `test_apple_iap_webhook_maps_configuration_error_to_503`, `test_apple_iap_webhook_returns_ok_for_processed_notification`
- `tests/webserver/test_health_public.py` — Python-модуль
  Классы: нет
  Функции: `test_health_is_public`, `test_detailed_health_stays_gated`
- `tests/webserver/test_mulenpay_webhook.py` — Python-модуль
  Классы: `DummyBot`
  Функции: `mulenpay_settings`, `test_verify_accepts_valid_body_sign`, `test_verify_rejects_wrong_sign`, `test_verify_rejects_missing_sign_field`, `test_verify_rejects_tampered_amount`, `test_verify_rejects_when_secret_not_configured`, `test_verify_rejects_non_json_body`, `test_verify_rejects_json_array_payload`, `test_verify_rejects_empty_object`, `test_verify_ignores_http_headers_completely` — User can no longer trick verification by sending X-Signature., `test_verify_is_case_insensitive_for_hex_sign`, `test_verify_handles_unicode_values_in_payload`, `test_verify_handles_extra_unknown_fields` — If MulenPay adds new fields, formula should still work — SDK iterates all values., `test_route_returns_200_on_valid_sign`, `test_route_returns_401_on_invalid_sign`, `test_route_returns_401_when_sign_missing_from_body`
- `tests/webserver/test_paritypay_webhook.py` — Python-модуль
  Классы: `DummyBot`
  Функции: `paritypay_settings`, `test_valid_signature_acks_and_processes`, `test_response_does_not_wait_for_slow_processing`, `test_invalid_signature_is_rejected`, `test_tampered_amount_is_rejected` — Подмена суммы после подписи ломает проверку., `test_missing_signature_header_is_rejected`, `test_broken_json_is_rejected`, `test_route_absent_without_callback_secret` — Без ключа подписи эндпоинт не монтируется — принимать вслепую нечего.
- `tests/webserver/test_payment_webhook_routes_survive_toggle.py` — Python-модуль
  Классы: нет
  Функции: `tribute_configured`, `test_route_is_mounted_while_provider_is_switched_off` — Ровно сценарий из жалобы: перезапуск с выключенной платёжкой., `test_route_is_mounted_when_provider_is_on`, `test_unconfigured_provider_has_no_endpoint` — Без ключа подпись коллбека проверять нечем — маршрута быть не должно., `test_registration_never_looks_at_the_enable_flag` — Ни один маршрут не должен монтироваться по is_X_enabled()., `test_auto_verification_watchdog_is_not_latched` — Сторож автопроверки обязан смотреть на настройку, а не на первый результат.
- `tests/webserver/test_payments.py` — Python-модуль
  Классы: `DummyBot`
  Функции: `reset_settings`, `test_tribute_webhook_success`, `test_yookassa_unknown_ip`, `test_yookassa_forbidden_ip`, `test_yookassa_forbidden_ip_ignores_spoofed_header`, `test_yookassa_forbidden_ip_ignores_spoofed_forwarded_chain`, `test_yookassa_skip_ip_check_bypasses_ip_gate`, `test_yookassa_allowed_ip`, `test_yookassa_allowed_via_forwarded_header_when_proxy`, `test_yookassa_allowed_via_cf_connecting_ip`, `test_yookassa_allowed_via_trusted_forwarded_chain`, `test_yookassa_allowed_via_trusted_public_proxy`, `test_yookassa_webhook_success`, `test_yookassa_webhook_cancellation`, `test_yookassa_webhook_with_signature`, `test_cryptobot_missing_signature`, `test_cryptobot_invalid_signature`
- `tests/webserver/test_paypear_ip_resolution.py` — Python-модуль
  Классы: нет
  Функции: `test_direct_public_attacker_cannot_spoof_x_real_ip`, `test_direct_public_attacker_cannot_spoof_x_forwarded_for`, `test_legit_webhook_behind_local_proxy_uses_forwarded_header`, `test_legit_webhook_behind_private_proxy_uses_forwarded_header`, `test_direct_paypear_connection_without_proxy`, `test_no_peer_falls_back_to_forwarded`, `test_malformed_peer_does_not_crash_and_does_not_trust_forwarded`
- `tests/webserver/test_platega_subscription_webhook.py` — Python-модуль
  Классы: `DummyBot`
  Функции: `reset_settings`, `test_platega_subscription_payload_routes_and_always_returns_200`, `test_platega_subscription_status_prefix_routes_to_subscription_handler`, `test_platega_one_off_payload_routes_to_legacy_handler`, `test_platega_wrong_secret_rejected_before_dispatch`, `test_platega_camel_case_charge_routes_to_subscription_handler` — Форма из продового лога: списание по подписке в camelCase., `test_platega_camel_case_one_off_still_uses_legacy_handler` — Обратная сторона: обычное пополнение не должно уехать в подписки.
- `tests/webserver/test_remnawave_webhook.py` — Python-модуль
  Классы: нет
  Функции: `reset_remnawave_webhook_settings`, `test_remnawave_webhook_accepts_event_without_scope`, `test_remnawave_webhook_rejects_payload_without_event`, `test_intentional_panel_deletion_guard_marks_and_detects` — Verify that mark + is_intentional round-trip works correctly., `test_intentional_panel_deletion_guard_respects_hard_cap` — Verify that the guard stops accepting entries after hitting the cap.
- `tests/webserver/test_tabpay_webhook.py` — Python-модуль
  Классы: `DummyBot`
  Функции: `tabpay_settings`, `test_valid_signature_acks_200_and_processes`, `test_response_does_not_wait_for_slow_processing` — Обработка уходит в фон: 200 отдаётся, не дожидаясь зачисления., `test_invalid_signature_is_rejected_without_processing` — Товар не выдаётся: обработчик не вызывается вовсе., `test_stale_timestamp_is_rejected` — Перехваченный вебхук нельзя переиграть позже: метка вне окна., `test_reserialized_body_breaks_signature` — Подпись обязана считаться по сырым байтам: пробелы меняют результат., `test_legacy_v1_signature_is_not_accepted` — Принимаем только v2: подпись от одного тела не проходит., `test_broken_json_with_valid_signature_is_rejected`, `test_route_absent_without_credentials` — Ненастроенный провайдер не должен держать открытый эндпоинт.
- `tests/webserver/test_telegram.py` — Python-модуль
  Классы: нет
  Функции: `reset_webhook_settings`, `test_webhook_without_secret`, `test_webhook_with_secret`, `test_webhook_secret_mismatch`, `test_webhook_invalid_payload`, `test_webhook_invalid_content_type`, `test_webhook_uses_processor`, `test_webhook_processor_overloaded`, `test_webhook_processor_not_running`, `test_webhook_path_normalization`, `test_health_endpoint`
- `tests/webserver/test_unified_app.py` — Python-модуль
  Классы: нет
  Функции: `test_unified_app_health_reports_features`, `test_unified_app_apple_iap_only_mounts_only_apple_cabinet_routes`, `test_unified_app_health_path_without_admin`, `test_unified_app_docs_disabled`, `test_unified_app_docs_enabled_with_alias`
- `tests/webserver/test_webhook_bg_tasks_drain.py` — Python-модуль
  Классы: нет
  Функции: `test_spawned_task_is_held_until_it_finishes` — Ссылка на задачу живёт, пока та работает, и снимается после., `test_drain_waits_for_unfinished_processing` — Дренаж не отпускает остановку, пока платёж дорабатывается., `test_drain_returns_immediately_without_tasks` — Пустой набор — выкат не задерживается., `test_drain_gives_up_by_timeout_and_shouts` — Застрявшая задача не держит выкат вечно, но и не уходит молча., `test_drain_is_registered_before_the_other_shutdowns` — Дренаж обязан идти раньше остановки telegram-процессора и БД., `test_webhook_acks_before_processing_finishes` — Ответ 200 уходит НЕ дожидаясь обработки платежа.
