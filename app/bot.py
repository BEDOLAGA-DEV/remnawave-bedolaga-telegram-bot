import redis.asyncio as redis
import structlog
from aiogram import Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage

from app.config import settings
from app.utils.telegram_bot_factory import Bot
from app.handlers import (
    balance,
    common,
    contests as user_contests,
    menu,
    polls as user_polls,
    promocode,
    referral,
    server_status,
    simple_subscription,
    start,
    subscription,
    support,
    tickets,
)
from app.handlers.admin import (
    backup as admin_backup,
    blacklist as admin_blacklist,
    blocked_users as admin_blocked_users,
    bot_configuration as admin_bot_configuration,
    bulk_ban as admin_bulk_ban,
    campaigns as admin_campaigns,
    contests as admin_contests,
    daily_contests as admin_daily_contests,
    faq as admin_faq,
    main as admin_main,
    maintenance as admin_maintenance,
    messages as admin_messages,
    monitoring as admin_monitoring,
    payments as admin_payments,
    polls as admin_polls,
    pricing as admin_pricing,
    privacy_policy as admin_privacy_policy,
    promo_groups as admin_promo_groups,
    promo_offers as admin_promo_offers,
    promocodes as admin_promocodes,
    public_offer as admin_public_offer,
    referrals as admin_referrals,
    remnawave as admin_remnawave,
    reports as admin_reports,
    required_channels as admin_required_channels,
    rules as admin_rules,
    servers as admin_servers,
    statistics as admin_statistics,
    subscriptions as admin_subscriptions,
    system_logs as admin_system_logs,
    tariffs as admin_tariffs,
    tickets as admin_tickets,
    trials as admin_trials,
    updates as admin_updates,
    user_messages as admin_user_messages,
    users as admin_users,
    welcome_text as admin_welcome_text,
)
from app.handlers.channel_member import register_handlers as register_channel_member_handlers
from app.handlers.gift_activation import register_handlers as register_gift_activation_handlers
from app.handlers.stars_payments import register_stars_handlers
from app.middlewares.auth import AuthMiddleware
from app.middlewares.blacklist import BlacklistMiddleware
from app.middlewares.button_stats import ButtonStatsMiddleware
from app.middlewares.chat_type_filter import ChatTypeFilterMiddleware
from app.middlewares.context_binding import ContextVarsMiddleware
from app.middlewares.global_error import GlobalErrorMiddleware
from app.middlewares.logging import LoggingMiddleware
from app.middlewares.maintenance import MaintenanceMiddleware
from app.middlewares.subscription_checker import SubscriptionStatusMiddleware
from app.middlewares.throttling import ThrottlingMiddleware
from app.services.maintenance_service import maintenance_service
from app.utils.cache import cache
from app.utils.message_patch import patch_message_methods


patch_message_methods()

logger = structlog.get_logger(__name__)


async def debug_callback_handler(callback: types.CallbackQuery):
    logger.info('🔍 DEBUG CALLBACK:')
    logger.info('Data', callback_data=callback.data)
    logger.info('User', from_user_id=callback.from_user.id)
    logger.info('Username', username=callback.from_user.username)


async def setup_bot() -> tuple[Bot, Dispatcher]:
    try:
        await cache.connect()
        logger.info('Кеш инициализирован')
    except Exception as e:
        logger.warning('Кеш не инициализирован', error=e)

    from app.bot_factory import create_bot

    bot = create_bot()

    proxy_url = settings.get_proxy_url()
    nalogo_proxy_url = settings.get_nalogo_proxy_url()

    if proxy_url or nalogo_proxy_url:
        from app.utils.proxy import mask_proxy_url

        if proxy_url:
            logger.info('Proxy configured', proxy_url=mask_proxy_url(proxy_url))
        if nalogo_proxy_url:
            source = 'NALOGO_PROXY_URL' if settings.NALOGO_PROXY_URL else 'PROXY_URL (fallback)'
            logger.info('Nalogo proxy configured', proxy_url=mask_proxy_url(nalogo_proxy_url), source=source)

    maintenance_service.set_bot(bot)
    logger.info('Бот установлен в maintenance_service')

    try:
        redis_client = redis.from_url(settings.REDIS_URL)
        await redis_client.ping()
        storage = RedisStorage(redis_client)
        logger.info('Подключено к Redis для FSM storage')
    except Exception as e:
        logger.warning('Не удалось подключиться к Redis', error=e)
        logger.info('Используется MemoryStorage для FSM')
        storage = MemoryStorage()

    dp = Dispatcher(storage=storage)

    dp.message.middleware(ContextVarsMiddleware())
    dp.callback_query.middleware(ContextVarsMiddleware())
    dp.pre_checkout_query.middleware(ContextVarsMiddleware())
    chat_type_filter = ChatTypeFilterMiddleware()
    dp.message.middleware(chat_type_filter)
    dp.callback_query.middleware(chat_type_filter)
    dp.message.middleware(GlobalErrorMiddleware())
    dp.callback_query.middleware(GlobalErrorMiddleware())
    dp.pre_checkout_query.middleware(GlobalErrorMiddleware())
    dp.message.middleware(LoggingMiddleware())
    dp.callback_query.middleware(LoggingMiddleware())
    dp.message.middleware(MaintenanceMiddleware())
    dp.callback_query.middleware(MaintenanceMiddleware())
    blacklist_middleware = BlacklistMiddleware()
    dp.message.middleware(blacklist_middleware)
    dp.callback_query.middleware(blacklist_middleware)
    dp.pre_checkout_query.middleware(blacklist_middleware)
    throttling_middleware = ThrottlingMiddleware()
    dp.message.middleware(throttling_middleware)
    dp.callback_query.middleware(throttling_middleware)

    # Middleware для автоматического логирования кликов по кнопкам
    if settings.MENU_LAYOUT_ENABLED:
        button_stats_middleware = ButtonStatsMiddleware()
        dp.callback_query.middleware(button_stats_middleware)
        logger.info('📊 ButtonStatsMiddleware активирован')

    from app.middlewares.channel_checker import ChannelCheckerMiddleware

    channel_checker = ChannelCheckerMiddleware()
    dp.message.middleware(channel_checker)
    dp.callback_query.middleware(channel_checker)
    dp.message.middleware(AuthMiddleware())
    dp.callback_query.middleware(AuthMiddleware())
    dp.pre_checkout_query.middleware(AuthMiddleware())
    dp.message.middleware(SubscriptionStatusMiddleware())
    dp.callback_query.middleware(SubscriptionStatusMiddleware())
    start.register_handlers(dp)
    menu.register_handlers(dp)
    subscription.register_handlers(dp)
    balance.register_balance_handlers(dp)
    promocode.register_handlers(dp)
    referral.register_handlers(dp)
    support.register_handlers(dp)
    server_status.register_handlers(dp)
    tickets.register_handlers(dp)
    admin_main.register_handlers(dp)
    admin_users.register_handlers(dp)
    admin_subscriptions.register_handlers(dp)
    admin_servers.register_handlers(dp)
    admin_promocodes.register_handlers(dp)
    admin_messages.register_handlers(dp)
    admin_monitoring.register_handlers(dp)
    admin_referrals.register_handlers(dp)
    admin_rules.register_handlers(dp)
    admin_remnawave.register_handlers(dp)
    admin_statistics.register_handlers(dp)
    admin_polls.register_handlers(dp)
    admin_promo_groups.register_handlers(dp)
    admin_campaigns.register_handlers(dp)
    admin_contests.register_handlers(dp)
    admin_daily_contests.register_handlers(dp)
    admin_promo_offers.register_handlers(dp)
    admin_maintenance.register_handlers(dp)
    admin_user_messages.register_handlers(dp)
    admin_updates.register_handlers(dp)
    admin_backup.register_handlers(dp)
    admin_system_logs.register_handlers(dp)
    admin_welcome_text.register_welcome_text_handlers(dp)
    admin_tickets.register_handlers(dp)
    admin_reports.register_handlers(dp)
    admin_bot_configuration.register_handlers(dp)
    admin_pricing.register_handlers(dp)
    admin_privacy_policy.register_handlers(dp)
    admin_public_offer.register_handlers(dp)
    admin_faq.register_handlers(dp)
    admin_payments.register_handlers(dp)
    admin_trials.register_handlers(dp)
    admin_tariffs.register_handlers(dp)
    admin_bulk_ban.register_bulk_ban_handlers(dp)
    admin_blacklist.register_blacklist_handlers(dp)
    admin_blocked_users.register_handlers(dp)
    admin_required_channels.register_handlers(dp)
    register_channel_member_handlers(dp)
    register_gift_activation_handlers(dp)
    common.register_handlers(dp)
    register_stars_handlers(dp)
    user_contests.register_handlers(dp)
    user_polls.register_handlers(dp)
    simple_subscription.register_simple_subscription_handlers(dp)
    logger.info('⭐ Зарегистрированы обработчики Telegram Stars платежей')
    logger.info('⚡ Зарегистрированы обработчики простой покупки')
    logger.info('⚡ Зарегистрированы обработчики простой подписки')

    if settings.is_maintenance_monitoring_enabled():
        try:
            await maintenance_service.start_monitoring()
            logger.info('Мониторинг техработ запущен')
        except Exception as e:
            logger.error('Ошибка запуска мониторинга техработ', error=e)
    else:
        logger.info('Мониторинг техработ отключен настройками')

    logger.info('🛡️ GlobalErrorMiddleware активирован - бот защищен от устаревших callback queries')

    # Validate CONNECT_BUTTON_MODE dependencies
    if not settings.get_happ_cryptolink_redirect_template():
        if settings.CONNECT_BUTTON_MODE == 'happ_cryptolink':
            logger.warning(
                '⚠️ CONNECT_BUTTON_MODE=happ_cryptolink, но HAPP_CRYPTOLINK_REDIRECT_TEMPLATE не задан! '
                'Кнопка "Подключиться" не будет отображаться.'
            )
        elif settings.CONNECT_BUTTON_MODE == 'guide':
            logger.warning(
                '⚠️ CONNECT_BUTTON_MODE=guide, но HAPP_CRYPTOLINK_REDIRECT_TEMPLATE не задан! '
                'Кнопка "Подключиться" в гайдах не будет работать — Telegram не поддерживает '
                'кастомные схемы (happ://, v2ray://) в inline-кнопках без HTTPS-редиректа.'
            )
    if settings.CONNECT_BUTTON_MODE == 'miniapp_custom' and not settings.MINIAPP_CUSTOM_URL:
        logger.warning(
            '⚠️ CONNECT_BUTTON_MODE=miniapp_custom, но MINIAPP_CUSTOM_URL не задан! '
            'Кнопка "Подключиться" не будет работать.'
        )
    if settings.is_cabinet_mode() and not settings.MINIAPP_CUSTOM_URL:
        logger.warning(
            '⚠️ MAIN_MENU_MODE=cabinet, но MINIAPP_CUSTOM_URL не задан! '
            'Кнопки кабинета не смогут открывать разделы MiniApp. '
            'Установите MINIAPP_CUSTOM_URL.'
        )
    elif settings.is_cabinet_mode():
        logger.info('🏠 Режим Cabinet активен, базовый URL', MINIAPP_CUSTOM_URL=settings.MINIAPP_CUSTOM_URL)

    # Load per-section button styles cache and menu layout cache
    if settings.is_cabinet_mode():
        try:
            from app.utils.button_styles_cache import load_button_styles_cache

            await load_button_styles_cache()
        except Exception as e:
            logger.warning('Failed to load button styles cache', error=e)

        try:
            from app.utils.menu_layout_cache import load_menu_layout_cache

            await load_menu_layout_cache()
        except Exception as e:
            logger.warning('Failed to load menu layout cache', error=e)

    logger.info('Бот успешно настроен')

    return bot, dp


async def shutdown_bot():
    try:
        await maintenance_service.stop_monitoring()
        logger.info('Мониторинг техработ остановлен')
    except Exception as e:
        logger.error('Ошибка остановки мониторинга', error=e)

    try:
        await cache.close()
        logger.info('Соединения с кешем закрыты')
    except Exception as e:
        logger.error('Ошибка закрытия кеша', error=e)
