import asyncio
import html
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from app.config import settings
from app.external.remnawave_api import RemnaWaveAPI, test_api_connection
from app.localization.texts import Texts, get_texts
from app.utils.cache import cache
from app.utils.timezone import format_local_datetime


logger = structlog.get_logger(__name__)


def _display_maintenance_reason(texts: Texts, reason: str | None) -> str:
    known = {
        'Автоматическое включение': 'MAINT_REASON_AUTO',
        'Включено администратором': 'MAINT_REASON_ADMIN',
        'Включено из системных настроек': 'MAINT_REASON_SETTINGS',
    }
    match = re.fullmatch(r'Автоматическое включение после (\d+) неудачных проверок API', reason or '')
    if match:
        return texts.t('MAINT_REASON_FAILURES', 'Автоматическое включение после {count} неудачных проверок API').format(
            count=match.group(1)
        )
    return html.escape(texts.t(known[reason], reason) if reason in known else (reason or ''))


@dataclass
class MaintenanceStatus:
    is_active: bool
    enabled_at: datetime | None = None
    last_check: datetime | None = None
    reason: str | None = None
    auto_enabled: bool = False
    api_status: bool = True
    consecutive_failures: int = 0


class MaintenanceService:
    def __init__(self):
        self._status = MaintenanceStatus(is_active=False)
        self._check_task: asyncio.Task | None = None
        self._is_checking = False
        self._max_consecutive_failures = 3
        self._bot = None
        self._last_notification_sent = None

    def set_bot(self, bot):
        self._bot = bot
        if settings.is_maintenance_mode() and not self._status.is_active:
            asyncio.create_task(self.enable_maintenance(reason='Включено из системных настроек', auto=False))
        logger.info('Бот установлен для maintenance_service')

    @property
    def status(self) -> MaintenanceStatus:
        return self._status

    def is_maintenance_active(self) -> bool:
        return self._status.is_active

    def get_maintenance_message(self, language: str | None = None) -> str:
        texts = get_texts(language or settings.DEFAULT_LANGUAGE)
        if self._status.auto_enabled:
            last_check_display = format_local_datetime(
                self._status.last_check, '%H:%M:%S', texts.t('MAINT_UNKNOWN_TIME', 'неизвестно')
            )
            return texts.t(
                'MAINT_AUTO_MESSAGE',
                '\n🔧 Технические работы!\n\nСервис временно недоступен из-за проблем с подключением к серверам.\n\n⏰ Мы работаем над восстановлением. Попробуйте через несколько минут.\n\n🔄 Последняя проверка: {time}\n',
            ).format(time=last_check_display)
        message = settings.get_maintenance_message()
        default_message = type(settings).model_fields['MAINTENANCE_MESSAGE'].default
        if message == default_message:
            return texts.t('MAINT_DEFAULT_MESSAGE', default_message)
        return message

    async def _send_admin_notification(self, message: str | Callable[[Texts], str], alert_type: str = 'info'):
        if not self._bot:
            logger.warning('Бот не установлен, уведомления не могут быть отправлены')
            return False

        try:
            from app.services.admin_notification_service import AdminNotificationService, NotificationCategory

            notification_service = AdminNotificationService(self._bot)

            if not notification_service.is_enabled:
                logger.debug('Уведомления администраторов отключены')
                return False

            emoji_map = {'error': '🚨', 'warning': '⚠️', 'success': '✅', 'info': 'ℹ️'}
            emoji = emoji_map.get(alert_type, 'ℹ️')

            timestamp = format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S %Z')
            texts = await notification_service._notification_texts()
            body = message(texts) if callable(message) else message
            formatted_message = texts.t(
                'MAINT_ADMIN_WRAPPER', '{emoji} <b>ТЕХНИЧЕСКИЕ РАБОТЫ</b>\n\n{message}\n\n⏰ <i>{time}</i>'
            ).format(emoji=emoji, message=body, time=timestamp)

            return await notification_service.send_admin_notification(
                formatted_message, category=NotificationCategory.INFRASTRUCTURE
            )

        except Exception as e:
            logger.error('Ошибка отправки уведомления через AdminNotificationService', error=e)
            return False

    async def _notify_admins(self, message: str | Callable[[Texts], str], alert_type: str = 'info'):
        if not self._bot:
            logger.warning('Бот не установлен, уведомления не могут быть отправлены')
            return

        notification_sent = await self._send_admin_notification(message, alert_type)

        if notification_sent:
            logger.info('Уведомление успешно отправлено через AdminNotificationService')
            return

        logger.info('Отправляем уведомление напрямую администраторам')

        cache_key = f'maintenance_notification_{alert_type}'
        if await cache.get(cache_key):
            return

        admin_ids = settings.get_admin_ids()
        if not admin_ids:
            logger.warning('Список администраторов пуст')
            return

        emoji_map = {'error': '🚨', 'warning': '⚠️', 'success': '✅', 'info': 'ℹ️'}
        emoji = emoji_map.get(alert_type, 'ℹ️')

        success_count = 0
        for admin_id in admin_ids:
            try:
                texts = get_texts(settings.DEFAULT_LANGUAGE)
                try:
                    from app.database.crud.user import get_user_by_telegram_id
                    from app.database.database import AsyncSessionLocal

                    async with AsyncSessionLocal() as db:
                        admin = await get_user_by_telegram_id(db, admin_id)
                        texts = get_texts(getattr(admin, 'language', None) or settings.DEFAULT_LANGUAGE)
                except Exception:
                    logger.debug('Не удалось определить язык администратора техработ')
                body = message(texts) if callable(message) else message
                formatted_message = texts.t(
                    'MAINT_ADMIN_DIRECT_WRAPPER', '{emoji} <b>Служба технических работ</b>\n\n{message}'
                ).format(emoji=emoji, message=body)
                await self._bot.send_message(chat_id=admin_id, text=formatted_message, parse_mode='HTML')
                success_count += 1
                await asyncio.sleep(0.1)

            except Exception as e:
                logger.error('Ошибка отправки уведомления админу', admin_id=admin_id, error=e)

        if success_count > 0:
            logger.info('Уведомление отправлено администраторам', success_count=success_count)
            await cache.set(cache_key, True, expire=300)
        else:
            logger.error('Не удалось отправить уведомления ни одному администратору')

    async def enable_maintenance(self, reason: str | None = None, auto: bool = False) -> bool:
        try:
            if self._status.is_active:
                logger.warning('Режим техработ уже включен')
                return True

            self._status.is_active = True
            self._status.enabled_at = datetime.now(UTC)
            self._status.reason = reason or ('Автоматическое включение' if auto else 'Включено администратором')
            self._status.auto_enabled = auto

            await self._save_status_to_cache()

            notification_reason = self._status.reason
            enabled_time = format_local_datetime(self._status.enabled_at, '%d.%m.%Y %H:%M:%S %Z')

            def notification_msg(texts: Texts) -> str:
                return texts.t(
                    'MAINT_NOTICE_149',
                    'Режим технических работ ВКЛЮЧЕН\n\n📋 <b>Причина:</b> {value1}\n🤖 <b>Автоматически:</b> {value2}\n🕐 <b>Время:</b> {value3}\n\nОбычные пользователи временно не смогут использовать бота.',
                ).format(
                    value1=_display_maintenance_reason(texts, notification_reason),
                    value2=texts.t('YES' if auto else 'NO', ''),
                    value3=html.escape(str(enabled_time)),
                )

            await self._notify_admins(notification_msg, 'warning' if auto else 'info')

            logger.warning('🔧 Режим техработ ВКЛЮЧЕН', reason=self._status.reason)
            return True

        except Exception as e:
            logger.error('Ошибка включения режима техработ', error=e)
            return False

    async def disable_maintenance(self) -> bool:
        try:
            if not self._status.is_active:
                logger.info('Режим техработ уже выключен')
                return True

            was_auto = self._status.auto_enabled
            duration = None
            if self._status.enabled_at:
                duration = datetime.now(UTC) - self._status.enabled_at

            self._status.is_active = False
            self._status.enabled_at = None
            self._status.reason = None
            self._status.auto_enabled = False
            self._status.consecutive_failures = 0

            await self._save_status_to_cache()

            duration_str = ''
            if duration:
                hours = int(duration.total_seconds() // 3600)
                minutes = int((duration.total_seconds() % 3600) // 60)
                if hours > 0:

                    def duration_str(texts: Texts) -> str:
                        return texts.t('MAINT_DURATION_190', '\n⏱️ <b>Длительность:</b> {value1}ч {value2}мин').format(
                            value1=html.escape(str(hours)), value2=html.escape(str(minutes))
                        )
                else:

                    def duration_str(texts: Texts) -> str:
                        return texts.t('MAINT_DURATION_192', '\n⏱️ <b>Длительность:</b> {value1}мин').format(
                            value1=html.escape(str(minutes))
                        )

            notification_time = format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S %Z')

            def notification_msg(texts: Texts) -> str:
                return texts.t(
                    'MAINT_NOTICE_195',
                    'Режим технических работ ВЫКЛЮЧЕН\n\n🤖 <b>Автоматически:</b> {value1}\n🕐 <b>Время:</b> {value2}\n{value3}\n\nСервис снова доступен для пользователей.',
                ).format(
                    value1=texts.t('YES' if was_auto else 'NO', ''),
                    value2=html.escape(str(notification_time)),
                    value3=duration_str(texts) if callable(duration_str) else duration_str,
                )

            await self._notify_admins(notification_msg, 'success')

            logger.info('✅ Режим техработ ВЫКЛЮЧЕН')
            return True

        except Exception as e:
            logger.error('Ошибка выключения режима техработ', error=e)
            return False

    async def sync_with_settings(self) -> bool:
        if settings.is_maintenance_mode():
            return await self.enable_maintenance(reason='Включено из системных настроек', auto=False)
        return await self.disable_maintenance()

    async def start_monitoring(self) -> bool:
        try:
            if self._check_task and not self._check_task.done():
                logger.warning('Мониторинг уже запущен')
                return True

            await self._load_status_from_cache()

            self._check_task = asyncio.create_task(self._monitoring_loop())
            logger.info(
                '🔄 Запущен мониторинг API Remnawave',
                get_maintenance_check_interval=settings.get_maintenance_check_interval(),
                get_maintenance_retry_attempts=settings.get_maintenance_retry_attempts(),
            )

            # Сообщение о запуске мониторинга убрано - теперь используется
            # единое стартовое уведомление через StartupNotificationService

            return True

        except Exception as e:
            logger.error('Ошибка запуска мониторинга', error=e)
            return False

    async def stop_monitoring(self) -> bool:
        try:
            if self._check_task and not self._check_task.done():
                self._check_task.cancel()
                try:
                    await self._check_task
                except asyncio.CancelledError:
                    pass

            await self._notify_admins(
                lambda texts: texts.t('MAINT_NOTICE_250', 'Мониторинг технических работ остановлен'), 'info'
            )
            logger.info('ℹ️ Мониторинг API остановлен')
            return True

        except Exception as e:
            logger.error('Ошибка остановки мониторинга', error=e)
            return False

    async def check_api_status(self) -> bool:
        try:
            if self._is_checking:
                return self._status.api_status

            self._is_checking = True
            self._status.last_check = datetime.now(UTC)

            auth_params = settings.get_remnawave_auth_params()
            base_url = (auth_params.get('base_url') or '').strip()
            api_key = (auth_params.get('api_key') or '').strip()
            secret_key = (auth_params.get('secret_key') or '').strip() or None
            username = (auth_params.get('username') or '').strip() or None
            password = (auth_params.get('password') or '').strip() or None
            caddy_token = (auth_params.get('caddy_token') or '').strip() or None
            auth_type = (auth_params.get('auth_type') or 'api_key').strip()

            if not base_url:
                logger.error('REMNAWAVE_API_URL не настроен, пропускаем проверку API')
                self._status.api_status = False
                self._status.consecutive_failures = 0
                return False

            if not api_key:
                logger.error('REMNAWAVE_API_KEY не настроен, пропускаем проверку API')
                self._status.api_status = False
                self._status.consecutive_failures = 0
                return False

            api = RemnaWaveAPI(
                base_url=base_url,
                api_key=api_key,
                secret_key=secret_key,
                username=username,
                password=password,
                caddy_token=caddy_token,
                auth_type=auth_type,
            )

            attempts = settings.get_maintenance_retry_attempts()

            async with api:
                for attempt in range(1, attempts + 1):
                    is_connected = await test_api_connection(api)

                    if is_connected:
                        if attempt > 1:
                            logger.info('API Remnawave ответило с попытки', attempt=attempt)

                        if not self._status.api_status:
                            recovery_time = format_local_datetime(self._status.last_check, '%H:%M:%S %Z')
                            await self._notify_admins(
                                lambda texts: texts.t(
                                    'MAINT_NOTICE_309',
                                    'API Remnawave восстановлено!\n\n✅ <b>Статус:</b> Доступно\n🕐 <b>Время восстановления:</b> {value1}\n🔄 <b>Неудачных попыток было:</b> {value2}\n\nAPI снова отвечает на запросы.',
                                ).format(
                                    value1=html.escape(str(recovery_time)),
                                    value2=html.escape(str(self._status.consecutive_failures)),
                                ),
                                'success',
                            )

                        self._status.api_status = True
                        self._status.consecutive_failures = 0

                        if self._status.is_active and self._status.auto_enabled:
                            await self.disable_maintenance()
                            logger.info('✅ API восстановился, режим техработ автоматически отключен')

                        return True

                    if attempt < attempts:
                        logger.warning('API Remnawave недоступно (попытка /)', attempt=attempt, attempts=attempts)
                        await asyncio.sleep(1)

                was_available = self._status.api_status
                self._status.api_status = False
                self._status.consecutive_failures += 1

                if was_available:
                    detection_time = format_local_datetime(self._status.last_check, '%H:%M:%S %Z')
                    await self._notify_admins(
                        lambda texts: texts.t(
                            'MAINT_NOTICE_339',
                            'API Remnawave недоступно!\n\n❌ <b>Статус:</b> Недоступно\n🕐 <b>Время обнаружения:</b> {value1}\n🔄 <b>Попытка:</b> {value2}\n\nНачалась серия неудачных проверок API.',
                        ).format(
                            value1=html.escape(str(detection_time)),
                            value2=html.escape(str(self._status.consecutive_failures)),
                        ),
                        'error',
                    )

                if (
                    self._status.consecutive_failures >= self._max_consecutive_failures
                    and not self._status.is_active
                    and settings.is_maintenance_auto_enable()
                ):
                    await self.enable_maintenance(
                        reason=(
                            f'Автоматическое включение после {self._status.consecutive_failures} неудачных проверок API'
                        ),
                        auto=True,
                    )

                return False

        except Exception as e:
            logger.error('Ошибка проверки API', error=e)

            if self._status.api_status:
                error_time = format_local_datetime(datetime.now(UTC), '%H:%M:%S %Z')
                escaped_error = html.escape(str(e))
                await self._notify_admins(
                    lambda texts: texts.t(
                        'MAINT_NOTICE_369',
                        'Ошибка при проверке API Remnawave\n\n❌ <b>Ошибка:</b> {value1}\n🕐 <b>Время:</b> {value2}\n\nНе удалось выполнить проверку доступности API.',
                    ).format(value1=escaped_error, value2=html.escape(str(error_time))),
                    'error',
                )

            self._status.api_status = False
            self._status.consecutive_failures += 1
            return False
        finally:
            self._is_checking = False
            await self._save_status_to_cache()

    async def _monitoring_loop(self):
        while True:
            try:
                await self.check_api_status()
                await asyncio.sleep(settings.get_maintenance_check_interval())

            except asyncio.CancelledError:
                logger.info('Мониторинг отменен')
                break
            except Exception as e:
                logger.error('Ошибка в цикле мониторинга', error=e)
                await asyncio.sleep(30)

    async def _save_status_to_cache(self):
        try:
            status_data = {
                'is_active': self._status.is_active,
                'enabled_at': self._status.enabled_at.isoformat() if self._status.enabled_at else None,
                'reason': self._status.reason,
                'auto_enabled': self._status.auto_enabled,
                'consecutive_failures': self._status.consecutive_failures,
                'last_check': self._status.last_check.isoformat() if self._status.last_check else None,
            }

            await cache.set('maintenance_status', status_data, expire=3600)

        except Exception as e:
            logger.error('Ошибка сохранения состояния в кеш', error=e)

    async def _load_status_from_cache(self):
        try:
            status_data = await cache.get('maintenance_status')
            if not status_data:
                return

            cached_active = status_data.get('is_active', False)
            cached_auto = status_data.get('auto_enabled', False)

            # Кэш нужен, чтобы перезапуск во время аварии не снимал АВТОМАТИЧЕСКИ
            # включённые техработы. Но для ручного режима источник истины —
            # сохранённая настройка: если она говорит «выключено», протухшая запись
            # в кэше (TTL час) не имеет права включить режим обратно.
            if cached_active and not cached_auto and not settings.is_maintenance_mode():
                logger.info('Кэш техработ противоречит настройке — ручной режим не восстанавливаем')
                return

            self._status.is_active = cached_active
            self._status.reason = status_data.get('reason')
            self._status.auto_enabled = status_data.get('auto_enabled', False)
            self._status.consecutive_failures = status_data.get('consecutive_failures', 0)

            if status_data.get('enabled_at'):
                dt = datetime.fromisoformat(status_data['enabled_at'])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                self._status.enabled_at = dt

            if status_data.get('last_check'):
                dt = datetime.fromisoformat(status_data['last_check'])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                self._status.last_check = dt

            logger.info('🔥 Состояние техработ загружено из кеша: активен', is_active=self._status.is_active)

        except Exception as e:
            logger.error('Ошибка загрузки состояния из кеша', error=e)

    def get_status_info(self) -> dict[str, Any]:
        return {
            'is_active': self._status.is_active,
            'enabled_at': self._status.enabled_at,
            'last_check': self._status.last_check,
            'reason': self._status.reason,
            'auto_enabled': self._status.auto_enabled,
            'api_status': self._status.api_status,
            'consecutive_failures': self._status.consecutive_failures,
            'monitoring_active': self._check_task is not None and not self._check_task.done(),
            'monitoring_configured': settings.is_maintenance_monitoring_enabled(),
            'auto_enable_configured': settings.is_maintenance_auto_enable(),
            'check_interval': settings.get_maintenance_check_interval(),
            'bot_connected': self._bot is not None,
        }

    async def force_api_check(self) -> dict[str, Any]:
        start_time = datetime.now(UTC)

        try:
            api_status = await self.check_api_status()
            end_time = datetime.now(UTC)
            response_time = (end_time - start_time).total_seconds()

            return {
                'success': True,
                'api_available': api_status,
                'response_time': round(response_time, 2),
                'checked_at': end_time,
                'consecutive_failures': self._status.consecutive_failures,
            }

        except Exception as e:
            end_time = datetime.now(UTC)
            response_time = (end_time - start_time).total_seconds()

            return {
                'success': False,
                'api_available': False,
                'error': str(e),
                'response_time': round(response_time, 2),
                'checked_at': end_time,
                'consecutive_failures': self._status.consecutive_failures,
            }

    async def send_remnawave_status_notification(self, status: str, details: str = '') -> bool:
        try:
            status_emojis = {'online': '🟢', 'offline': '🔴', 'warning': '🟡', 'error': '⚠️'}

            emoji = status_emojis.get(status, 'ℹ️')

            def message(texts: Texts) -> str:
                return texts.t(
                    'MAINT_PANEL_STATUS',
                    'Статус панели Remnawave изменился\n\n{value1} <b>Статус:</b> {value2}\n🔗 <b>URL:</b> {value3}\n{value4}',
                ).format(
                    value1=html.escape(str(emoji)),
                    value2=html.escape(texts.t('MAINT_STATUS_' + status.upper(), status.upper())),
                    value3=html.escape(str(settings.REMNAWAVE_API_URL)),
                    value4=html.escape(str(details)),
                )

            alert_type = 'error' if status in ['offline', 'error'] else 'info'
            await self._notify_admins(message, alert_type)

            logger.info('Отправлено уведомление о статусе Remnawave', status=status)
            return True

        except Exception as e:
            logger.error('Ошибка отправки уведомления о статусе Remnawave', error=e)
            return False


maintenance_service = MaintenanceService()
