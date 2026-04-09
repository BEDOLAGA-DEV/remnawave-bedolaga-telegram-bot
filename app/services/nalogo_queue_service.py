"""Фоновый сервис для обработки очереди чеков NaloGO.

При временной недоступности сервиса nalog.ru (503), чеки сохраняются в Redis
и отправляются позже этим сервисом.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Bot
from dateutil.parser import isoparse

from app.config import settings
from app.services.nalogo_service import NaloGoService
from app.utils.cache import cache


logger = structlog.get_logger(__name__)


class NalogoQueueService:
    """Сервис фоновой обработки очереди чеков NaloGO."""

    def __init__(self, nalogo_service: NaloGoService | None = None):
        self._nalogo_service = nalogo_service
        self._bot: Bot | None = None
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_notification_time: datetime | None = None
        self._notification_cooldown = timedelta(hours=1)  # Не чаще раза в час
        self._had_pending_receipts = False  # Флаг для отслеживания успешной разгрузки

    def set_nalogo_service(self, service: NaloGoService) -> None:
        """Установить сервис NaloGO."""
        self._nalogo_service = service

    def set_bot(self, bot: Bot) -> None:
        """Установить бота для отправки уведомлений."""
        self._bot = bot

    def is_running(self) -> bool:
        """Проверка, запущен ли сервис."""
        return self._running and self._task is not None and not self._task.done()

    @property
    def _check_interval(self) -> int:
        """Интервал проверки очереди в секундах."""
        return getattr(settings, 'NALOGO_QUEUE_CHECK_INTERVAL', 300)

    @property
    def _receipt_delay(self) -> int:
        """Задержка между отправкой чеков в секундах."""
        return getattr(settings, 'NALOGO_QUEUE_RECEIPT_DELAY', 3)

    @property
    def _max_attempts(self) -> int:
        """Максимальное количество попыток отправки чека."""
        return getattr(settings, 'NALOGO_QUEUE_MAX_ATTEMPTS', 10)

    async def start(self) -> None:
        """Запустить фоновую обработку очереди."""
        if not self._nalogo_service or not self._nalogo_service.configured:
            logger.info('NaloGO не настроен, сервис очереди чеков не запущен')
            return

        if self.is_running():
            logger.warning('Сервис очереди чеков уже запущен')
            return

        self._running = True
        self._task = asyncio.create_task(self._process_queue_loop())
        logger.info(
            'Сервис очереди чеков NaloGO запущен (интервал: с, задержка между чеками: с)',
            _check_interval=self._check_interval,
            _receipt_delay=self._receipt_delay,
        )

    async def stop(self) -> None:
        """Остановить фоновую обработку."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info('Сервис очереди чеков NaloGO остановлен')

    async def _send_admin_notification(self, message: str, skip_cooldown: bool = False) -> None:
        """Отправить уведомление админам о чеках."""
        if not self._bot:
            return

        chat_id = settings.get_admin_notifications_chat_id()
        if not chat_id:
            return

        topic_id = settings.ADMIN_NOTIFICATIONS_NALOG_TOPIC_ID

        # Проверяем cooldown (можно пропустить для важных уведомлений)
        if not skip_cooldown:
            now = datetime.now(UTC)
            if self._last_notification_time:
                if now - self._last_notification_time < self._notification_cooldown:
                    logger.debug('Уведомление о чеках пропущено (cooldown)')
                    return

        try:
            await self._bot.send_message(
                chat_id=chat_id,
                message_thread_id=topic_id,
                text=message,
                parse_mode='HTML',
            )
            self._last_notification_time = datetime.now(UTC)
            logger.info('Отправлено уведомление о чеках NaloGO')
        except Exception as error:
            logger.error('Ошибка отправки уведомления о чеках', error=error)

    async def _process_queue_loop(self) -> None:
        """Основной цикл обработки очереди."""
        while self._running:
            try:
                await self._process_pending_receipts()
            except Exception as error:
                logger.error('Ошибка в цикле обработки очереди чеков', error=error)

            await asyncio.sleep(self._check_interval)

    async def _process_pending_receipts(self) -> None:
        """Обработать все ожидающие чеки в очереди."""
        if not self._nalogo_service:
            return

        queue_length = await self._nalogo_service.get_queue_length()
        if queue_length == 0:
            return

        logger.info('Начинаем обработку очереди чеков: шт.', queue_length=queue_length)
        self._had_pending_receipts = True

        processed = 0
        failed = 0
        skipped = 0
        total_processed_amount = 0.0
        service_unavailable = False

        while True:
            receipt_data = await self._nalogo_service.pop_receipt_from_queue()
            if not receipt_data:
                break

            attempts = receipt_data.get('attempts', 0)
            payment_id = receipt_data.get('payment_id', 'unknown')
            amount = receipt_data.get('amount', 0)

            # Логируем количество попыток (чек никогда не удаляется из очереди)
            if attempts >= 10:
                logger.warning(
                    'Чек уже много попыток, продолжаем пытаться...', payment_id=payment_id, attempts=attempts
                )

            # Пытаемся отправить чек
            try:
                # Восстанавливаем описание из сохранённых данных
                telegram_user_id = receipt_data.get('telegram_user_id')
                amount_kopeks = receipt_data.get('amount_kopeks')

                # Извлекаем время оплаты из очереди (чтобы чек был с правильным временем)
                operation_time = None
                created_at_str = receipt_data.get('created_at')
                if created_at_str:
                    try:
                        operation_time = isoparse(created_at_str)
                        if operation_time.tzinfo is None:
                            operation_time = operation_time.replace(tzinfo=UTC)
                    except (ValueError, TypeError) as parse_error:
                        logger.warning(
                            'Не удалось распарсить created_at', created_at_str=created_at_str, parse_error=parse_error
                        )

                # Формируем описание заново из настроек (если есть данные)
                if amount_kopeks is not None:
                    receipt_name = settings.get_balance_payment_description(
                        amount_kopeks, telegram_user_id=telegram_user_id
                    )
                else:
                    # Fallback на сохранённое имя
                    receipt_name = receipt_data.get(
                        'name',
                        settings.get_balance_payment_description(int(amount * 100), telegram_user_id=telegram_user_id),
                    )

                receipt_uuid = await self._nalogo_service.create_receipt(
                    name=receipt_name,
                    amount=amount,
                    quantity=receipt_data.get('quantity', 1),
                    client_info=receipt_data.get('client_info'),
                    payment_id=payment_id,
                    queue_on_failure=False,  # Не добавлять в очередь повторно автоматически
                    telegram_user_id=telegram_user_id,
                    amount_kopeks=amount_kopeks,
                    operation_time=operation_time,  # Время оплаты, а не отправки
                )

                if receipt_uuid:
                    processed += 1
                    total_processed_amount += amount

                    # Удаляем метку "в очереди" (чек создан успешно)
                    if payment_id:
                        queued_key = f'nalogo:queued:{payment_id}'
                        await cache.delete(queued_key)

                    logger.info(
                        'Чек из очереди успешно создан: (payment_id=, попытка )',
                        receipt_uuid=receipt_uuid,
                        payment_id=payment_id,
                        attempts=attempts + 1,
                    )
                else:
                    # Вернуть в очередь с увеличенным счетчиком попыток
                    await self._nalogo_service.requeue_receipt(receipt_data)
                    failed += 1
                    service_unavailable = True
                    logger.warning(
                        'Не удалось создать чек из очереди (payment_id=), возвращен в очередь (попытка /)',
                        payment_id=payment_id,
                        attempts=attempts + 1,
                        _max_attempts=self._max_attempts,
                    )
                    # Если сервис недоступен, прекращаем попытки до следующего цикла
                    break

            except Exception as error:
                await self._nalogo_service.requeue_receipt(receipt_data)
                failed += 1
                logger.error('Ошибка при создании чека из очереди (payment_id=)', payment_id=payment_id, error=error)
                # Прекращаем попытки при ошибке
                break

            # Задержка между чеками чтобы не долбить API
            await asyncio.sleep(self._receipt_delay)

        if processed > 0 or failed > 0 or skipped > 0:
            logger.info(
                'Обработка очереди завершена: успешно=, неудачно=, пропущено',
                processed=processed,
                failed=failed,
                skipped=skipped,
            )

        # Проверяем остаток в очереди
        remaining = await self._nalogo_service.get_queue_length()

        # Отправляем уведомление если есть проблемы
        if service_unavailable or failed > 0:
            if remaining > 0:
                queued = await self._nalogo_service.get_queued_receipts()
                total_queued_amount = sum(r.get('amount', 0) for r in queued)

                message = (
                    f'<b>⚠️ Проблема с отправкой чеков NaloGO</b>\n\n'
                    f'Сервис nalog.ru временно недоступен.\n\n'
                    f'📋 <b>В очереди:</b> {remaining} чек(ов)\n'
                    f'💰 <b>На сумму:</b> {total_queued_amount:,.2f} ₽\n\n'
                    f'Чеки будут отправлены автоматически когда сервис восстановится.'
                )
                await self._send_admin_notification(message)

        # Уведомление об успешной разгрузке очереди
        elif remaining == 0 and self._had_pending_receipts and processed > 0:
            self._had_pending_receipts = False
            message = (
                f'<b>✅ Очередь чеков NaloGO разгружена</b>\n\n'
                f'Все отложенные чеки успешно отправлены!\n\n'
                f'📋 <b>Отправлено:</b> {processed} чек(ов)\n'
                f'💰 <b>На сумму:</b> {total_processed_amount:,.2f} ₽'
            )
            await self._send_admin_notification(message, skip_cooldown=True)

    async def force_process(self) -> dict:
        """Принудительно обработать очередь (для ручного запуска)."""
        if not self._nalogo_service:
            return {'error': 'NaloGO сервис не настроен'}

        queue_length = await self._nalogo_service.get_queue_length()
        if queue_length == 0:
            return {'message': 'Очередь пуста', 'processed': 0}

        await self._process_pending_receipts()
        new_length = await self._nalogo_service.get_queue_length()

        return {
            'message': 'Обработка завершена',
            'was_in_queue': queue_length,
            'remaining': new_length,
            'processed': queue_length - new_length,
        }

    async def get_status(self) -> dict:
        """Получить статус сервиса и очереди."""
        queue_length = 0
        total_amount = 0.0
        queued_receipts = []
        pending_verification_count = 0
        pending_verification_amount = 0.0
        pending_verification_receipts = []

        if self._nalogo_service:
            queue_length = await self._nalogo_service.get_queue_length()
            if queue_length > 0:
                queued_receipts = await self._nalogo_service.get_queued_receipts()
                total_amount = sum(r.get('amount', 0) for r in queued_receipts)

            # Чеки ожидающие ручной проверки
            pending_verification_count = await self._nalogo_service.get_pending_verification_count()
            if pending_verification_count > 0:
                pending_verification_receipts = await self._nalogo_service.get_pending_verification_receipts()
                pending_verification_amount = sum(r.get('amount', 0) for r in pending_verification_receipts)

        return {
            'running': self.is_running(),
            'check_interval_seconds': self._check_interval,
            'receipt_delay_seconds': self._receipt_delay,
            'queue_length': queue_length,
            'total_amount': total_amount,
            'max_attempts': self._max_attempts,
            'queued_receipts': queued_receipts[:10],
            # Чеки требующие ручной проверки (таймаут после успешной авторизации)
            'pending_verification_count': pending_verification_count,
            'pending_verification_amount': pending_verification_amount,
            'pending_verification_receipts': pending_verification_receipts[:10],
        }


# Глобальный экземпляр сервиса
nalogo_queue_service = NalogoQueueService()
