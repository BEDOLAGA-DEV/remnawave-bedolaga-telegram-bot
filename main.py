import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import structlog


sys.path.append(str(Path(__file__).parent))

from app.bot import setup_bot
from app.config import settings
from app.database.database import sync_postgres_sequences
from app.database.migrations import run_alembic_upgrade
from app.database.models import PaymentMethod
from app.localization.loader import ensure_locale_templates
from app.logging_config import setup_logging
from app.services.backup_service import backup_service
from app.services.ban_notification_service import ban_notification_service
from app.services.broadcast_service import broadcast_service
from app.services.contest_rotation_service import contest_rotation_service
from app.services.daily_subscription_service import daily_subscription_service
from app.services.external_admin_service import ensure_external_admin_token
from app.services.log_rotation_service import log_rotation_service
from app.services.maintenance_service import maintenance_service
from app.services.monitoring_service import monitoring_service
from app.services.nalogo_queue_service import nalogo_queue_service
from app.services.payment_service import PaymentService
from app.services.payment_verification_service import (
    PENDING_MAX_AGE,
    SUPPORTED_MANUAL_CHECK_METHODS,
    auto_payment_verification_service,
    get_enabled_auto_methods,
    method_display_name,
)
from app.services.referral_contest_service import referral_contest_service
from app.services.remnawave_sync_service import remnawave_sync_service
from app.services.reporting_service import reporting_service
from app.services.system_settings_service import bot_configuration_service
from app.services.traffic_monitoring_service import traffic_monitoring_scheduler
from app.services.version_service import version_service
from app.services.web_api_token_service import ensure_default_web_api_token
from app.utils.log_handlers import ExcludePaymentFilter, LevelFilterHandler
from app.utils.payment_logger import configure_payment_logger
from app.utils.startup_timeline import StartupTimeline
from app.webapi.server import WebAPIServer
from app.webserver.unified_app import create_unified_app


class GracefulExit:
    def __init__(self):
        self.exit = False

    def exit_gracefully(self, signum, frame):
        structlog.get_logger(__name__).info('Получен сигнал, корректное завершение работы', signum=signum)
        self.exit = True


async def main():
    file_formatter, console_formatter, telegram_notifier = setup_logging()

    log_handlers = []

    # === Инициализация системы логирования ===
    if settings.is_log_rotation_enabled():
        # Новая система: разделение по уровням + отдельный лог платежей
        await log_rotation_service.initialize()

        log_dir = log_rotation_service.current_dir
        log_dir.mkdir(parents=True, exist_ok=True)

        # 1. Общий лог (bot.log) - все уровни, без платежей
        bot_handler = logging.FileHandler(log_dir / 'bot.log', encoding='utf-8')
        bot_handler.setFormatter(file_formatter)
        bot_handler.addFilter(ExcludePaymentFilter())
        log_handlers.append(bot_handler)

        # 2. INFO лог - только INFO уровень
        info_handler = LevelFilterHandler(
            str(log_dir / settings.LOG_INFO_FILE),
            min_level=logging.INFO,
            max_level=logging.INFO,
        )
        info_handler.setFormatter(file_formatter)
        info_handler.addFilter(ExcludePaymentFilter())
        log_handlers.append(info_handler)

        # 3. WARNING лог - WARNING и выше
        warning_handler = LevelFilterHandler(
            str(log_dir / settings.LOG_WARNING_FILE),
            min_level=logging.WARNING,
        )
        warning_handler.setFormatter(file_formatter)
        warning_handler.addFilter(ExcludePaymentFilter())
        log_handlers.append(warning_handler)

        # 4. ERROR лог - только ERROR и CRITICAL
        error_handler = LevelFilterHandler(
            str(log_dir / settings.LOG_ERROR_FILE),
            min_level=logging.ERROR,
        )
        error_handler.setFormatter(file_formatter)
        error_handler.addFilter(ExcludePaymentFilter())
        log_handlers.append(error_handler)

        # 5. Payment лог - отдельный файл для платежей
        payment_handler = logging.FileHandler(
            log_dir / settings.LOG_PAYMENTS_FILE,
            encoding='utf-8',
        )
        configure_payment_logger(payment_handler, file_formatter)

        # 6. Консольный вывод
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(console_formatter)
        log_handlers.append(stream_handler)

        logging.basicConfig(
            level=getattr(logging, settings.LOG_LEVEL),
            handlers=log_handlers,
            force=True,
        )

        # Регистрируем хэндлеры для управления при ротации
        log_rotation_service.register_handlers(log_handlers)

    else:
        # Старое поведение: один файл лога
        file_handler = logging.FileHandler(settings.LOG_FILE, encoding='utf-8')
        file_handler.setFormatter(file_formatter)
        log_handlers.append(file_handler)

        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(console_formatter)
        log_handlers.append(stream_handler)

        logging.basicConfig(
            level=getattr(logging, settings.LOG_LEVEL),
            handlers=log_handlers,
            force=True,
        )

    # NOTE: TelegramNotifierProcessor and noisy logger suppression are
    # handled inside setup_logging() / logging_config.py.

    logger = structlog.get_logger(__name__)
    timeline = StartupTimeline(logger, 'Bedolaga Remnawave Bot')
    timeline.log_banner(
        [
            ('Уровень логирования', settings.LOG_LEVEL),
            ('Режим БД', settings.DATABASE_MODE),
        ]
    )

    async with timeline.stage('Подготовка локализаций', '🗂️', success_message='Шаблоны локализаций готовы') as stage:
        try:
            ensure_locale_templates()
        except Exception as error:
            stage.warning(f'Не удалось подготовить шаблоны локализаций: {error}')
            logger.warning('Failed to prepare locale templates', error=error)

    killer = GracefulExit()
    signal.signal(signal.SIGINT, killer.exit_gracefully)
    signal.signal(signal.SIGTERM, killer.exit_gracefully)

    web_app = None
    monitoring_task = None
    maintenance_task = None
    version_check_task = None
    traffic_monitoring_task = None
    daily_subscription_task = None
    polling_task = None
    web_api_server = None
    telegram_webhook_enabled = False
    polling_enabled = True
    payment_webhooks_enabled = False

    summary_logged = False

    try:
        skip_migration = os.getenv('SKIP_MIGRATION', 'false').lower() == 'true'

        if not skip_migration:
            async with timeline.stage(
                'Миграция базы данных (Alembic)',
                '🧬',
                success_message='Миграция завершена успешно',
            ) as stage:
                try:
                    await run_alembic_upgrade()
                    stage.success('Миграция завершена успешно')
                except Exception as migration_error:
                    allow_failure = os.getenv('ALLOW_MIGRATION_FAILURE', 'false').lower() == 'true'
                    logger.error('Ошибка выполнения миграции', migration_error=migration_error)
                    if not allow_failure:
                        raise
                    stage.warning(f'Ошибка миграции: {migration_error} (ALLOW_MIGRATION_FAILURE=true)')
        else:
            timeline.add_manual_step(
                'Миграция базы данных (Alembic)',
                '⏭️',
                'Пропущено',
                'SKIP_MIGRATION=true',
            )

        async with timeline.stage(
            'Инициализация базы данных',
            '🗄️',
            success_message='База данных готова',
        ) as stage:
            seq_ok = await sync_postgres_sequences()
            token_ok = await ensure_default_web_api_token()
            if not seq_ok:
                stage.warning('Не удалось синхронизировать последовательности PostgreSQL')
            if not token_ok:
                stage.warning('Не удалось создать/проверить дефолтный веб-API токен')

        async with timeline.stage(
            'RBAC bootstrap',
            '🔐',
            success_message='RBAC roles and superadmins ready',
        ) as stage:
            try:
                from app.database.database import AsyncSessionLocal
                from app.services.rbac_bootstrap_service import bootstrap_superadmins

                async with AsyncSessionLocal() as db:
                    await bootstrap_superadmins(db)
            except Exception as error:
                stage.warning(f'RBAC bootstrap warning: {error}')
                logger.error('RBAC bootstrap failed', error=error)

        async with timeline.stage(
            'Синхронизация тарифов из конфига',
            '💰',
            success_message='Тарифы синхронизированы',
        ) as stage:
            try:
                from app.database.crud.tariff import ensure_tariffs_synced
                from app.database.database import AsyncSessionLocal

                async with AsyncSessionLocal() as db:
                    await ensure_tariffs_synced(db)
            except Exception as error:
                stage.warning(f'Не удалось синхронизировать тарифы: {error}')
                logger.error('❌ Не удалось синхронизировать тарифы', error=error)

        async with timeline.stage(
            'Синхронизация серверов из RemnaWave',
            '🖥️',
            success_message='Серверы синхронизированы',
        ) as stage:
            try:
                from app.database.crud.server_squad import ensure_servers_synced
                from app.database.database import AsyncSessionLocal

                async with AsyncSessionLocal() as db:
                    await ensure_servers_synced(db)
            except Exception as error:
                stage.warning(f'Не удалось синхронизировать серверы: {error}')
                logger.error('❌ Не удалось синхронизировать серверы', error=error)

        async with timeline.stage(
            'Инициализация платёжных методов',
            '💳',
            success_message='Платёжные методы инициализированы',
        ) as stage:
            try:
                from app.database.database import AsyncSessionLocal
                from app.services.payment_method_config_service import ensure_payment_method_configs

                async with AsyncSessionLocal() as db:
                    await ensure_payment_method_configs(db)
            except Exception as error:
                stage.warning(f'Не удалось инициализировать платёжные методы: {error}')
                logger.error('❌ Не удалось инициализировать платёжные методы', error=error)

        async with timeline.stage(
            'Загрузка конфигурации из БД',
            '⚙️',
            success_message='Конфигурация загружена',
        ) as stage:
            try:
                await bot_configuration_service.initialize()
            except Exception as error:
                stage.warning(f'Не удалось загрузить конфигурацию: {error}')
                logger.error('❌ Не удалось загрузить конфигурацию', error=error)

        bot = None
        dp = None
        async with timeline.stage('Настройка бота', '🤖', success_message='Бот настроен') as stage:
            bot, dp = await setup_bot()
            stage.log('Кеш и FSM подготовлены')

        monitoring_service.bot = bot
        maintenance_service.set_bot(bot)
        broadcast_service.set_bot(bot)
        ban_notification_service.set_bot(bot)
        traffic_monitoring_scheduler.set_bot(bot)
        daily_subscription_service.set_bot(bot)
        telegram_notifier.set_bot(bot)

        from app.services.channel_subscription_service import channel_subscription_service

        channel_subscription_service.bot = bot

        # Initialize email broadcast service
        from app.cabinet.services.email_service import email_service
        from app.services.broadcast_service import email_broadcast_service

        email_broadcast_service.set_email_service(email_service)

        from app.services.admin_notification_service import AdminNotificationService

        async with timeline.stage(
            'Интеграция сервисов',
            '🔗',
            success_message='Сервисы подключены',
        ) as stage:
            admin_notification_service = AdminNotificationService(bot)
            version_service.bot = bot
            version_service.set_notification_service(admin_notification_service)
            referral_contest_service.set_bot(bot)
            stage.log(f'Репозиторий версий: {version_service.repo}')
            stage.log(f'Текущая версия: {version_service.current_version}')
            stage.success('Мониторинг, уведомления и рассылки подключены')

        async with timeline.stage(
            'Сервис бекапов',
            '🗄️',
            success_message='Сервис бекапов инициализирован',
        ) as stage:
            try:
                backup_service.bot = bot
                settings_obj = await backup_service.get_backup_settings()
                if settings_obj.auto_backup_enabled:
                    await backup_service.start_auto_backup()
                    stage.log(
                        'Автобекапы включены: интервал '
                        f'{settings_obj.backup_interval_hours}ч, запуск {settings_obj.backup_time}'
                    )
                else:
                    stage.log('Автобекапы отключены настройками')
                stage.success('Сервис бекапов инициализирован')
            except Exception as e:
                stage.warning(f'Ошибка инициализации сервиса бекапов: {e}')
                logger.error('❌ Ошибка инициализации сервиса бекапов', error=e)

        async with timeline.stage(
            'Сервис отчетов',
            '📊',
            success_message='Сервис отчетов готов',
        ) as stage:
            try:
                reporting_service.set_bot(bot)
                await reporting_service.start()
            except Exception as e:
                stage.warning(f'Ошибка запуска сервиса отчетов: {e}')
                logger.error('❌ Ошибка запуска сервиса отчетов', error=e)

        async with timeline.stage(
            'Реферальные конкурсы',
            '🏆',
            success_message='Сервис конкурсов готов',
        ) as stage:
            try:
                await referral_contest_service.start()
                if referral_contest_service.is_running():
                    stage.log('Автосводки по конкурсам запущены')
                else:
                    stage.skip('Сервис конкурсов выключен настройками')
            except Exception as e:
                stage.warning(f'Ошибка запуска сервиса конкурсов: {e}')
                logger.error('❌ Ошибка запуска сервиса конкурсов', error=e)

        async with timeline.stage(
            'Ротация игр',
            '🎲',
            success_message='Мини-игры готовы',
        ) as stage:
            try:
                contest_rotation_service.set_bot(bot)
                await contest_rotation_service.start()
                if contest_rotation_service.is_running():
                    stage.log('Ротационные игры запущены')
                else:
                    stage.skip('Ротация игр выключена настройками')
            except Exception as e:
                stage.warning(f'Ошибка запуска ротации игр: {e}')
                logger.error('❌ Ошибка запуска ротации игр', error=e)

        if settings.is_log_rotation_enabled():
            async with timeline.stage(
                'Ротация логов',
                '📋',
                success_message='Сервис ротации логов готов',
            ) as stage:
                try:
                    log_rotation_service.set_bot(bot)
                    await log_rotation_service.start()
                    status = log_rotation_service.get_status()
                    stage.log(f'Время ротации: {status.rotation_time}')
                    stage.log(f'Хранение архивов: {status.keep_days} дней')
                    if status.send_to_telegram:
                        stage.log('Отправка в Telegram: включена')
                    if status.next_rotation:
                        from datetime import datetime

                        next_dt = datetime.fromisoformat(status.next_rotation)
                        stage.log(f'Следующая ротация: {next_dt.strftime("%d.%m.%Y %H:%M")}')
                except Exception as e:
                    stage.warning(f'Ошибка запуска сервиса ротации логов: {e}')
                    logger.error('❌ Ошибка запуска сервиса ротации логов', error=e)

        async with timeline.stage(
            'Автосинхронизация RemnaWave',
            '🔄',
            success_message='Сервис автосинхронизации готов',
        ) as stage:
            try:
                await remnawave_sync_service.initialize()
                status = remnawave_sync_service.get_status()
                if status.enabled:
                    times_text = ', '.join(t.strftime('%H:%M') for t in status.times) or '—'
                    if status.next_run:
                        next_run_text = status.next_run.strftime('%d.%m.%Y %H:%M')
                        stage.log(f'Активирована: расписание {times_text}, ближайший запуск {next_run_text}')
                    else:
                        stage.log(f'Активирована: расписание {times_text}')
                else:
                    stage.log('Автосинхронизация отключена настройками')
            except Exception as e:
                stage.warning(f'Ошибка запуска автосинхронизации: {e}')
                logger.error('❌ Ошибка запуска автосинхронизации RemnaWave', error=e)

        payment_service = PaymentService(bot)
        auto_payment_verification_service.set_payment_service(payment_service)

        # Настройка сервиса очереди чеков NaloGO
        if payment_service.nalogo_service:
            nalogo_queue_service.set_nalogo_service(payment_service.nalogo_service)
            nalogo_queue_service.set_bot(bot)

        verification_providers: list[str] = []
        auto_verification_active = False
        async with timeline.stage(
            'Сервис проверки пополнений',
            '💳',
            success_message='Ручная проверка активна',
        ) as stage:
            for method in SUPPORTED_MANUAL_CHECK_METHODS:
                if method == PaymentMethod.YOOKASSA and settings.is_yookassa_enabled():
                    verification_providers.append('YooKassa')
                elif method == PaymentMethod.MULENPAY and settings.is_mulenpay_enabled():
                    verification_providers.append(settings.get_mulenpay_display_name())
                elif method == PaymentMethod.PAL24 and settings.is_pal24_enabled():
                    verification_providers.append('PayPalych')
                elif method == PaymentMethod.WATA and settings.is_wata_enabled():
                    verification_providers.append('WATA')
                elif method == PaymentMethod.HELEKET and settings.is_heleket_enabled():
                    verification_providers.append('Heleket')
                elif method == PaymentMethod.CRYPTOBOT and settings.is_cryptobot_enabled():
                    verification_providers.append('CryptoBot')

            if verification_providers:
                hours = int(PENDING_MAX_AGE.total_seconds() // 3600)
                stage.log(f'Ожидающие пополнения автоматически отбираются не старше {hours}ч')
                stage.log('Доступна ручная проверка для: ' + ', '.join(sorted(verification_providers)))
                stage.success(f'Активно провайдеров: {len(verification_providers)}')
            else:
                stage.skip('Нет активных провайдеров для ручной проверки')

            if settings.is_payment_verification_auto_check_enabled():
                auto_methods = get_enabled_auto_methods()
                if auto_methods:
                    interval_minutes = settings.get_payment_verification_auto_check_interval()
                    auto_labels = ', '.join(sorted(method_display_name(method) for method in auto_methods))
                    stage.log(f'Автопроверка каждые {interval_minutes} мин: {auto_labels}')
                else:
                    stage.log('Автопроверка включена, но нет активных провайдеров')
            else:
                stage.log('Автопроверка отключена настройками')

            await auto_payment_verification_service.start()
            auto_verification_active = auto_payment_verification_service.is_running()
            if auto_verification_active:
                stage.log('Фоновая автопроверка запущена')

        async with timeline.stage(
            'Очередь чеков NaloGO',
            '🧾',
            success_message='Сервис очереди чеков запущен',
        ) as stage:
            if settings.is_nalogo_enabled():
                try:
                    await nalogo_queue_service.start()
                    if nalogo_queue_service.is_running():
                        queue_len = await payment_service.nalogo_service.get_queue_length()
                        if queue_len > 0:
                            stage.log(f'В очереди ожидает {queue_len} чек(ов)')
                        stage.success('Фоновая обработка чеков активна')
                    else:
                        stage.skip('Сервис не запущен')
                except Exception as e:
                    stage.warning(f'Ошибка запуска очереди чеков: {e}')
                    logger.error('❌ Ошибка запуска очереди чеков NaloGO', error=e)
            else:
                stage.skip('NaloGO отключен настройками')

        async with timeline.stage(
            'Внешняя админка',
            '🛡️',
            success_message='Токен внешней админки готов',
        ) as stage:
            try:
                bot_user = await bot.get_me()
                token = await ensure_external_admin_token(
                    bot_user.username,
                    bot_user.id,
                )
                if token:
                    stage.log('Токен синхронизирован')
                else:
                    stage.warning('Не удалось получить токен внешней админки')
            except Exception as error:  # pragma: no cover - защитный блок
                stage.warning(f'Ошибка подготовки внешней админки: {error}')
                logger.error('❌ Ошибка подготовки внешней админки', error=error)

        bot_run_mode = settings.get_bot_run_mode()
        polling_enabled = bot_run_mode == 'polling'
        telegram_webhook_enabled = bot_run_mode == 'webhook'

        payment_webhooks_enabled = any(
            [
                settings.TRIBUTE_ENABLED,
                settings.is_cryptobot_enabled(),
                settings.is_mulenpay_enabled(),
                settings.is_yookassa_enabled(),
                settings.is_pal24_enabled(),
                settings.is_wata_enabled(),
                settings.is_heleket_enabled(),
            ]
        )

        async with timeline.stage(
            'Единый веб-сервер',
            '🌐',
            success_message='Веб-сервер запущен',
        ) as stage:
            should_start_web_app = (
                settings.is_web_api_enabled()
                or telegram_webhook_enabled
                or payment_webhooks_enabled
                or settings.get_miniapp_static_path().exists()
            )

            if should_start_web_app:
                web_app = create_unified_app(
                    bot,
                    dp,
                    payment_service,
                    enable_telegram_webhook=telegram_webhook_enabled,
                )

                web_api_server = WebAPIServer(app=web_app)
                await web_api_server.start()

                base_url = settings.WEBHOOK_URL or f'http://{settings.WEB_API_HOST}:{settings.WEB_API_PORT}'
                stage.log(f'Базовый URL: {base_url}')

                features: list[str] = []
                if settings.is_web_api_enabled():
                    features.append('админка')
                if payment_webhooks_enabled:
                    features.append('платежные webhook-и')
                if telegram_webhook_enabled:
                    features.append('Telegram webhook')
                if settings.get_miniapp_static_path().exists():
                    features.append('статические файлы миниаппа')

                if features:
                    stage.log('Активные сервисы: ' + ', '.join(features))
                stage.success('HTTP-сервисы активны')
            else:
                stage.skip('HTTP-сервисы отключены настройками')

        async with timeline.stage(
            'Telegram webhook',
            '🤖',
            success_message='Telegram webhook настроен',
        ) as stage:
            if telegram_webhook_enabled:
                webhook_url = settings.get_telegram_webhook_url()
                if not webhook_url:
                    stage.warning('WEBHOOK_URL не задан, пропускаем настройку webhook')
                else:
                    allowed_updates = dp.resolve_used_update_types()
                    await bot.set_webhook(
                        url=webhook_url,
                        secret_token=settings.WEBHOOK_SECRET_TOKEN,
                        drop_pending_updates=False,  # Обрабатываем накопившиеся обновления
                        allowed_updates=allowed_updates,
                    )
                    stage.log(f'Webhook установлен: {webhook_url}')
                    stage.log(f'Allowed updates: {", ".join(sorted(allowed_updates)) if allowed_updates else "all"}')
                    stage.success('Telegram webhook активен')
            else:
                stage.skip('Режим webhook отключен')

        async with timeline.stage(
            'Служба мониторинга',
            '📈',
            success_message='Служба мониторинга запущена',
        ) as stage:
            monitoring_task = asyncio.create_task(monitoring_service.start_monitoring())
            stage.log(f'Интервал опроса: {settings.MONITORING_INTERVAL}с')

        async with timeline.stage(
            'Служба техработ',
            '🛡️',
            success_message='Служба техработ запущена',
        ) as stage:
            if not settings.is_maintenance_monitoring_enabled():
                maintenance_task = None
                stage.skip('Мониторинг техработ отключен настройками')
            elif not maintenance_service._check_task or maintenance_service._check_task.done():
                maintenance_task = asyncio.create_task(maintenance_service.start_monitoring())
                stage.log(f'Интервал проверки: {settings.MAINTENANCE_CHECK_INTERVAL}с')
                stage.log(f'Повторных попыток проверки: {settings.get_maintenance_retry_attempts()}')
            else:
                maintenance_task = None
                stage.skip('Служба техработ уже активна')

        async with timeline.stage(
            'Мониторинг трафика',
            '📊',
            success_message='Мониторинг трафика запущен',
        ) as stage:
            if traffic_monitoring_scheduler.is_enabled():
                traffic_monitoring_task = asyncio.create_task(traffic_monitoring_scheduler.start_monitoring())
                # Показываем информацию о новом мониторинге v2
                status_info = traffic_monitoring_scheduler.get_status_info()
                stage.log(status_info)
            else:
                traffic_monitoring_task = None
                stage.skip('Мониторинг трафика отключен настройками')

        async with timeline.stage(
            'Суточные подписки',
            '💳',
            success_message='Сервис суточных подписок запущен',
        ) as stage:
            if daily_subscription_service.is_enabled():
                daily_subscription_task = asyncio.create_task(daily_subscription_service.start_monitoring())
                interval_minutes = daily_subscription_service.get_check_interval_minutes()
                stage.log(f'Интервал проверки: {interval_minutes} мин')
            else:
                daily_subscription_task = None
                stage.skip('Суточные подписки отключены настройками')

        async with timeline.stage(
            'Сервис проверки версий',
            '📄',
            success_message='Проверка версий запущена',
        ) as stage:
            if settings.is_version_check_enabled():
                version_check_task = asyncio.create_task(version_service.start_periodic_check())
                stage.log(f'Интервал проверки: {settings.VERSION_CHECK_INTERVAL_HOURS}ч')
            else:
                version_check_task = None
                stage.skip('Проверка версий отключена настройками')

        async with timeline.stage(
            'Запуск polling',
            '🤖',
            success_message='Aiogram polling запущен',
        ) as stage:
            if polling_enabled:
                polling_task = asyncio.create_task(dp.start_polling(bot, skip_updates=False))
                stage.log('skip_updates=False — накопившиеся обновления будут обработаны')
            else:
                polling_task = None
                stage.skip('Polling отключен режимом работы')

        webhook_lines: list[str] = []
        base_url = settings.WEBHOOK_URL or f'http://{settings.WEB_API_HOST}:{settings.WEB_API_PORT}'

        def _fmt(path: str) -> str:
            return f'{base_url}{path if path.startswith("/") else "/" + path}'

        telegram_webhook_url = settings.get_telegram_webhook_url()
        if telegram_webhook_enabled and telegram_webhook_url:
            webhook_lines.append(f'Telegram: {telegram_webhook_url}')
        if settings.TRIBUTE_ENABLED:
            webhook_lines.append(f'Tribute: {_fmt(settings.TRIBUTE_WEBHOOK_PATH)}')
        if settings.is_mulenpay_enabled():
            webhook_lines.append(f'{settings.get_mulenpay_display_name()}: {_fmt(settings.MULENPAY_WEBHOOK_PATH)}')
        if settings.is_cryptobot_enabled():
            webhook_lines.append(f'CryptoBot: {_fmt(settings.CRYPTOBOT_WEBHOOK_PATH)}')
        if settings.is_yookassa_enabled():
            webhook_lines.append(f'YooKassa: {_fmt(settings.YOOKASSA_WEBHOOK_PATH)}')
        if settings.is_pal24_enabled():
            webhook_lines.append(f'PayPalych: {_fmt(settings.PAL24_WEBHOOK_PATH)}')
        if settings.is_wata_enabled():
            webhook_lines.append(f'WATA: {_fmt(settings.WATA_WEBHOOK_PATH)}')
        if settings.is_heleket_enabled():
            webhook_lines.append(f'Heleket: {_fmt(settings.HELEKET_WEBHOOK_PATH)}')
        if settings.is_platega_enabled():
            webhook_lines.append(f'Platega: {_fmt(settings.PLATEGA_WEBHOOK_PATH)}')
        if settings.is_cloudpayments_enabled():
            webhook_lines.append(f'CloudPayments: {_fmt(settings.CLOUDPAYMENTS_WEBHOOK_PATH)}')
        if settings.is_freekassa_enabled():
            webhook_lines.append(f'Freekassa: {_fmt(settings.FREEKASSA_WEBHOOK_PATH)}')
        if settings.is_kassa_ai_enabled():
            webhook_lines.append(f'Kassa.ai: {_fmt(settings.KASSA_AI_WEBHOOK_PATH)}')
        if settings.is_remnawave_webhook_enabled():
            webhook_lines.append(f'RemnaWave: {_fmt(settings.REMNAWAVE_WEBHOOK_PATH)}')

        timeline.log_section(
            'Активные webhook endpoints',
            webhook_lines if webhook_lines else ['Нет активных endpoints'],
            icon='🎯',
        )

        services_lines = [
            f'Мониторинг: {"Включен" if monitoring_task else "Отключен"}',
            f'Техработы: {"Включен" if maintenance_task else "Отключен"}',
            f'Мониторинг трафика: {"Включен" if traffic_monitoring_task else "Отключен"}',
            f'Суточные подписки: {"Включен" if daily_subscription_task else "Отключен"}',
            f'Проверка версий: {"Включен" if version_check_task else "Отключен"}',
            f'Отчеты: {"Включен" if reporting_service.is_running() else "Отключен"}',
        ]
        services_lines.append('Проверка пополнений: ' + ('Включена' if verification_providers else 'Отключена'))
        services_lines.append(
            'Автопроверка пополнений: '
            + ('Включена' if auto_payment_verification_service.is_running() else 'Отключена')
        )
        timeline.log_section('Активные фоновые сервисы', services_lines, icon='📄')

        timeline.log_summary()
        summary_logged = True

        # Отправляем стартовое уведомление в админский чат
        try:
            from app.services.startup_notification_service import send_bot_startup_notification

            await send_bot_startup_notification(bot)
        except Exception as startup_notify_error:
            logger.warning('Не удалось отправить стартовое уведомление', startup_notify_error=startup_notify_error)

        try:
            while not killer.exit:
                await asyncio.sleep(1)

                if monitoring_task.done():
                    exception = monitoring_task.exception()
                    if exception:
                        logger.error('Служба мониторинга завершилась с ошибкой', error=exception)
                        monitoring_task = asyncio.create_task(monitoring_service.start_monitoring())

                if maintenance_task and maintenance_task.done():
                    exception = maintenance_task.exception()
                    if exception:
                        logger.error('Служба техработ завершилась с ошибкой', error=exception)
                        maintenance_task = asyncio.create_task(maintenance_service.start_monitoring())

                if version_check_task and version_check_task.done():
                    exception = version_check_task.exception()
                    if exception:
                        logger.error('Сервис проверки версий завершился с ошибкой', error=exception)
                        if settings.is_version_check_enabled():
                            logger.info('🔄 Перезапуск сервиса проверки версий...')
                            version_check_task = asyncio.create_task(version_service.start_periodic_check())

                if traffic_monitoring_task and traffic_monitoring_task.done():
                    exception = traffic_monitoring_task.exception()
                    if exception:
                        logger.error('Мониторинг трафика завершился с ошибкой', error=exception)
                        if traffic_monitoring_scheduler.is_enabled():
                            logger.info('🔄 Перезапуск мониторинга трафика...')
                            traffic_monitoring_task = asyncio.create_task(
                                traffic_monitoring_scheduler.start_monitoring()
                            )

                if daily_subscription_task and daily_subscription_task.done():
                    exception = daily_subscription_task.exception()
                    if exception:
                        logger.error('Сервис суточных подписок завершился с ошибкой', error=exception)
                        if daily_subscription_service.is_enabled():
                            logger.info('🔄 Перезапуск сервиса суточных подписок...')
                            daily_subscription_task = asyncio.create_task(daily_subscription_service.start_monitoring())

                if auto_verification_active and not auto_payment_verification_service.is_running():
                    logger.warning('Сервис автопроверки пополнений остановился, пробуем перезапустить...')
                    await auto_payment_verification_service.start()
                    auto_verification_active = auto_payment_verification_service.is_running()

                if polling_task and polling_task.done():
                    exception = polling_task.exception()
                    if exception:
                        logger.error('Polling завершился с ошибкой', error=exception)
                        break

        except Exception as e:
            logger.error('Ошибка в основном цикле', error=e)

    except Exception as e:
        logger.error('❌ Критическая ошибка при запуске', error=e)
        raise

    finally:
        if not summary_logged:
            timeline.log_summary()
            summary_logged = True
        logger.info('🛑 Начинается корректное завершение работы...')

        logger.info('ℹ️ Остановка сервиса автопроверки пополнений...')
        try:
            await auto_payment_verification_service.stop()
        except Exception as error:
            logger.error('Ошибка остановки сервиса автопроверки пополнений', error=error)

        if monitoring_task and not monitoring_task.done():
            logger.info('ℹ️ Остановка службы мониторинга...')
            monitoring_service.stop_monitoring()
            monitoring_task.cancel()
            try:
                await monitoring_task
            except asyncio.CancelledError:
                pass

        if maintenance_task and not maintenance_task.done():
            logger.info('ℹ️ Остановка службы техработ...')
            await maintenance_service.stop_monitoring()
            maintenance_task.cancel()
            try:
                await maintenance_task
            except asyncio.CancelledError:
                pass

        if version_check_task and not version_check_task.done():
            logger.info('ℹ️ Остановка сервиса проверки версий...')
            version_check_task.cancel()
            try:
                await version_check_task
            except asyncio.CancelledError:
                pass

        if traffic_monitoring_task and not traffic_monitoring_task.done():
            logger.info('ℹ️ Остановка мониторинга трафика...')
            traffic_monitoring_scheduler.stop_monitoring()
            traffic_monitoring_task.cancel()
            try:
                await traffic_monitoring_task
            except asyncio.CancelledError:
                pass

        if daily_subscription_task and not daily_subscription_task.done():
            logger.info('ℹ️ Остановка сервиса суточных подписок...')
            daily_subscription_service.stop_monitoring()
            daily_subscription_task.cancel()
            try:
                await daily_subscription_task
            except asyncio.CancelledError:
                pass

        logger.info('ℹ️ Остановка сервиса отчетов...')
        try:
            await reporting_service.stop()
        except Exception as e:
            logger.error('Ошибка остановки сервиса отчетов', error=e)

        logger.info('ℹ️ Остановка сервиса конкурсов...')
        try:
            await referral_contest_service.stop()
        except Exception as e:
            logger.error('Ошибка остановки сервиса конкурсов', error=e)

        logger.info('ℹ️ Остановка сервиса автосинхронизации RemnaWave...')
        try:
            await remnawave_sync_service.stop()
        except Exception as e:
            logger.error('Ошибка остановки автосинхронизации RemnaWave', error=e)

        logger.info('ℹ️ Остановка ротации игр...')
        try:
            await contest_rotation_service.stop()
        except Exception as e:
            logger.error('Ошибка остановки ротации игр', error=e)

        if settings.is_log_rotation_enabled():
            logger.info('ℹ️ Остановка сервиса ротации логов...')
            try:
                await log_rotation_service.stop()
            except Exception as e:
                logger.error('Ошибка остановки сервиса ротации логов', error=e)

        logger.info('ℹ️ Остановка очереди чеков NaloGO...')
        try:
            await nalogo_queue_service.stop()
        except Exception as e:
            logger.error('Ошибка остановки очереди чеков NaloGO', error=e)

        logger.info('ℹ️ Остановка сервиса бекапов...')
        try:
            await backup_service.stop_auto_backup()
        except Exception as e:
            logger.error('Ошибка остановки сервиса бекапов', error=e)

        if polling_task and not polling_task.done():
            logger.info('ℹ️ Остановка polling...')
            polling_task.cancel()
            try:
                await polling_task
            except asyncio.CancelledError:
                pass

        if telegram_webhook_enabled and 'bot' in locals():
            logger.info('ℹ️ Снятие Telegram webhook...')
            try:
                await bot.delete_webhook(drop_pending_updates=False)
                logger.info('✅ Telegram webhook удалён')
            except Exception as error:
                logger.error('Ошибка удаления Telegram webhook', error=error)

        if web_api_server:
            try:
                await web_api_server.stop()
                logger.info('✅ Административное веб-API остановлено')
            except Exception as error:
                logger.error('Ошибка остановки веб-API', error=error)

        if 'bot' in locals():
            try:
                await bot.session.close()
                logger.info('✅ Сессия бота закрыта')
            except Exception as e:
                logger.error('Ошибка закрытия сессии бота', error=e)

        logger.info('✅ Завершение работы бота завершено')


async def _send_crash_notification_on_error(error: Exception) -> None:
    """Отправляет уведомление о падении бота в админский чат."""
    import traceback

    from app.config import settings

    if not getattr(settings, 'BOT_TOKEN', None):
        return

    try:
        from aiogram import Bot

        from app.services.startup_notification_service import send_crash_notification

        bot = Bot(token=settings.BOT_TOKEN)
        try:
            traceback_str = traceback.format_exc()
            await send_crash_notification(bot, error, traceback_str)
        finally:
            await bot.session.close()
    except Exception as notify_error:
        print(f'⚠️ Не удалось отправить уведомление о падении: {notify_error}')


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\n🛑 Бот остановлен пользователем')
    except Exception as e:
        print(f'❌ Критическая ошибка: {e}')
        import traceback

        traceback.print_exc()
        # Пытаемся отправить уведомление о падении
        try:
            asyncio.run(_send_crash_notification_on_error(e))
        except Exception:
            pass
        sys.exit(1)
