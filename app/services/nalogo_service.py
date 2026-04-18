from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog

from app.config import settings

# Используем локальную исправленную версию библиотеки
from app.lib.nalogo import Client
from app.lib.nalogo.dto.income import IncomeClient, IncomeType
from app.utils.cache import cache
from app.utils.proxy import mask_proxy_url, sanitize_proxy_error


logger = structlog.get_logger(__name__)

NALOGO_QUEUE_KEY = 'nalogo:receipt_queue'
NALOGO_PENDING_VERIFICATION_KEY = 'nalogo:pending_verification'


class NaloGoService:
    """Сервис для работы с API NaloGO (налоговая служба самозанятых)."""

    def __init__(
        self,
        inn: str | None = None,
        password: str | None = None,
        device_id: str | None = None,
        storage_path: str | None = None,
    ):
        inn = inn or getattr(settings, 'NALOGO_INN', None)
        password = password or getattr(settings, 'NALOGO_PASSWORD', None)
        device_id = device_id or getattr(settings, 'NALOGO_DEVICE_ID', None)
        storage_path = storage_path or getattr(settings, 'NALOGO_STORAGE_PATH', './nalogo_tokens.json')

        self.configured = False

        if not inn or not password:
            logger.warning('NaloGO INN или PASSWORD не настроены в settings. Функционал чеков будет ОТКЛЮЧЕН.')
        else:
            try:
                # Таймаут 30 секунд — nalog.ru иногда отвечает медленно
                timeout = getattr(settings, 'NALOGO_TIMEOUT', 30.0)
                proxy_url = settings.get_nalogo_proxy_url()
                self.client = Client(
                    base_url='https://lknpd.nalog.ru/api',
                    storage_path=storage_path,
                    device_id=device_id or 'bot-device-123',
                    timeout=timeout,
                    proxy_url=proxy_url,
                )
                self.inn = inn
                self.password = password
                self.configured = True
                if proxy_url:
                    logger.info(
                        'NaloGO клиент инициализирован с прокси', inn=inn[:5], proxy_url=mask_proxy_url(proxy_url)
                    )
                else:
                    logger.info('NaloGO клиент инициализирован для ИНН: ...', inn=inn[:5])
            except Exception as error:
                logger.error('Ошибка инициализации NaloGO клиента', error=sanitize_proxy_error(error))
                self.configured = False

    @staticmethod
    def _is_service_unavailable(error: Exception) -> bool:
        """Проверяет, является ли ошибка временной недоступностью сервиса."""
        error_str = str(error).lower()
        error_type = type(error).__name__.lower()
        return (
            '503' in error_str
            or '500' in error_str
            or 'internal server error' in error_str
            or 'внутренняя ошибка' in error_str
            or 'service temporarily unavailable' in error_str
            or 'service unavailable' in error_str
            or 'ведутся работы' in error_str
            or ('health' in error_str and 'false' in error_str)
            # Таймауты и сетевые ошибки — временные проблемы
            or 'timeout' in error_type
            or 'timeout' in error_str
            or 'readtimeout' in error_type
            or 'connecttimeout' in error_type
            or 'connectionerror' in error_type
            or 'connecterror' in error_type
        )

    async def _queue_receipt(
        self,
        name: str,
        amount: float,
        quantity: int,
        client_info: dict[str, Any] | None,
        payment_id: str | None = None,
        telegram_user_id: int | None = None,
        amount_kopeks: int | None = None,
        receipt_delivery_email: str | None = None,
        receipt_delivery_language: str | None = None,
    ) -> bool:
        """Добавить чек в очередь для отложенной отправки."""
        if payment_id:
            # Защита от дубликатов: проверяем не был ли чек уже создан
            created_key = f'nalogo:created:{payment_id}'
            already_created = await cache.get(created_key)
            if already_created:
                logger.info(
                    'Чек для payment_id= уже создан не добавляем в очередь',
                    payment_id=payment_id,
                    already_created=already_created,
                )
                return False

            # Атомарная проверка и установка флага "в очереди" (защита от race condition)
            queued_key = f'nalogo:queued:{payment_id}'
            lock_acquired = await cache.setnx(queued_key, 'queued', expire=7 * 24 * 3600)
            if not lock_acquired:
                # Ключ уже существует — чек уже в очереди
                logger.info('Чек для payment_id= уже в очереди, пропускаем дубликат', payment_id=payment_id)
                return False

        receipt_data = {
            'name': name,
            'amount': amount,
            'quantity': quantity,
            'client_info': client_info,
            'payment_id': payment_id,
            'telegram_user_id': telegram_user_id,
            'amount_kopeks': amount_kopeks,
            'receipt_delivery_email': receipt_delivery_email,
            'receipt_delivery_language': receipt_delivery_language,
            'created_at': datetime.now(UTC).isoformat(),
            'attempts': 0,
        }
        success = await cache.lpush(NALOGO_QUEUE_KEY, receipt_data)
        if success:
            queue_len = await cache.llen(NALOGO_QUEUE_KEY)
            logger.info(
                'Чек добавлен в очередь (payment_id=, сумма=₽, в очереди: )',
                payment_id=payment_id,
                amount=amount,
                queue_len=queue_len,
            )
        # Если не удалось добавить в очередь — удаляем флаг
        elif payment_id:
            queued_key = f'nalogo:queued:{payment_id}'
            await cache.delete(queued_key)
        return success

    async def _save_pending_verification(
        self,
        name: str,
        amount: float,
        quantity: int,
        client_info: dict[str, Any] | None,
        payment_id: str | None,
        telegram_user_id: int | None,
        amount_kopeks: int | None,
        receipt_delivery_email: str | None,
        receipt_delivery_language: str | None,
        error_message: str,
    ) -> bool:
        """Сохранить чек в очередь ожидающих проверки.

        Используется когда таймаут произошёл ПОСЛЕ успешной аутентификации —
        чек мог быть создан на сервере, но ответ не пришёл.
        """
        receipt_data = {
            'name': name,
            'amount': amount,
            'quantity': quantity,
            'client_info': client_info,
            'payment_id': payment_id,
            'telegram_user_id': telegram_user_id,
            'amount_kopeks': amount_kopeks,
            'receipt_delivery_email': receipt_delivery_email,
            'receipt_delivery_language': receipt_delivery_language,
            'created_at': datetime.now(UTC).isoformat(),
            'error': error_message,
            'status': 'pending_verification',
        }
        success = await cache.lpush(NALOGO_PENDING_VERIFICATION_KEY, receipt_data)
        if success:
            count = await cache.llen(NALOGO_PENDING_VERIFICATION_KEY)
            logger.warning(
                'Чек сохранён для ручной проверки (payment_id=, сумма=₽, всего ожидают проверки: )',
                payment_id=payment_id,
                amount=amount,
                count=count,
            )
        return success

    async def get_pending_verification_count(self) -> int:
        """Получить количество чеков ожидающих проверки."""
        return await cache.llen(NALOGO_PENDING_VERIFICATION_KEY)

    async def get_pending_verification_receipts(self) -> list:
        """Получить список чеков ожидающих проверки."""
        return await cache.lrange(NALOGO_PENDING_VERIFICATION_KEY)

    async def mark_pending_as_verified(
        self,
        payment_id: str,
        receipt_uuid: str | None = None,
        was_created: bool = True,
    ) -> dict[str, Any] | None:
        """Пометить чек как проверенный и удалить из очереди.

        Args:
            payment_id: ID платежа
            receipt_uuid: UUID чека если был создан в налоговой
            was_created: True если чек был создан, False если не был

        Returns:
            Данные удалённого чека или None если не найден
        """
        receipts = await self.get_pending_verification_receipts()
        updated_receipts = []
        removed_receipt = None

        for receipt in receipts:
            if receipt.get('payment_id') == payment_id:
                removed_receipt = receipt
                if was_created and receipt_uuid:
                    # Сохраняем что чек создан
                    created_key = f'nalogo:created:{payment_id}'
                    await cache.set(created_key, receipt_uuid, expire=30 * 24 * 3600)
                    logger.info('Чек помечен как созданный', payment_id=payment_id, receipt_uuid=receipt_uuid)
            else:
                updated_receipts.append(receipt)

        if removed_receipt:
            # Очищаем и перезаписываем список
            await cache.delete(NALOGO_PENDING_VERIFICATION_KEY)
            for r in reversed(updated_receipts):  # reversed чтобы сохранить порядок
                await cache.lpush(NALOGO_PENDING_VERIFICATION_KEY, r)
            logger.info('Чек удалён из очереди проверки', payment_id=payment_id)

        return removed_receipt

    async def retry_pending_receipt(self, payment_id: str) -> str | None:
        """Повторно отправить чек из очереди проверки.

        Используется когда проверили что чек НЕ был создан в налоговой.

        Returns:
            UUID созданного чека или None
        """
        receipts = await self.get_pending_verification_receipts()
        target_receipt = None

        for receipt in receipts:
            if receipt.get('payment_id') == payment_id:
                target_receipt = receipt
                break

        if not target_receipt:
            logger.warning('Чек не найден в очереди проверки', payment_id=payment_id)
            return None

        # Пытаемся создать чек
        receipt_uuid = await self.create_receipt(
            name=target_receipt.get('name', ''),
            amount=target_receipt.get('amount', 0),
            quantity=target_receipt.get('quantity', 1),
            client_info=target_receipt.get('client_info'),
            payment_id=payment_id,
            queue_on_failure=False,  # Не добавлять обратно в очередь
            telegram_user_id=target_receipt.get('telegram_user_id'),
            amount_kopeks=target_receipt.get('amount_kopeks'),
            receipt_delivery_email=target_receipt.get('receipt_delivery_email'),
            receipt_delivery_language=target_receipt.get('receipt_delivery_language'),
        )

        if receipt_uuid:
            # Удаляем из очереди проверки
            await self.mark_pending_as_verified(payment_id, receipt_uuid, was_created=True)
            logger.info('Чек успешно создан после ручной проверки', payment_id=payment_id, receipt_uuid=receipt_uuid)

            receipt_url = self.get_receipt_print_url(receipt_uuid)
            if receipt_url:
                from app.services.nalogo_receipt_delivery import deliver_nalogo_receipt

                await deliver_nalogo_receipt(
                    receipt_url=receipt_url,
                    receipt_uuid=receipt_uuid,
                    telegram_id=target_receipt.get('telegram_user_id'),
                    email=target_receipt.get('receipt_delivery_email'),
                    language=target_receipt.get('receipt_delivery_language'),
                )

        return receipt_uuid

    async def clear_pending_verification(self) -> int:
        """Очистить всю очередь проверки (после полной ручной сверки)."""
        count = await self.get_pending_verification_count()
        if count > 0:
            await cache.delete(NALOGO_PENDING_VERIFICATION_KEY)
            logger.info('Очередь проверки очищена: удалено чеков', count=count)
        return count

    async def authenticate(self) -> bool:
        """Аутентификация в сервисе NaloGO."""
        if not self.configured:
            return False

        try:
            token = await self.client.create_new_access_token(self.inn, self.password)
            await self.client.authenticate(token)
            logger.info('Успешная аутентификация в NaloGO')
            return True
        except Exception as error:
            if self._is_service_unavailable(error):
                logger.warning('NaloGO временно недоступен (техработы)', error=sanitize_proxy_error(error))
            else:
                logger.error('Ошибка аутентификации в NaloGO', error=sanitize_proxy_error(error))
            return False

    def get_receipt_print_url(self, receipt_uuid: str) -> str | None:
        """Вернуть URL печатной формы чека."""
        if not self.configured:
            return None

        try:
            return self.client.receipt().print_url(receipt_uuid)
        except Exception as error:
            logger.warning('Не удалось сформировать print URL чека', receipt_uuid=receipt_uuid, error=error)
            return None

    async def create_receipt(
        self,
        name: str,
        amount: float,
        quantity: int = 1,
        client_info: dict[str, Any] | None = None,
        payment_id: str | None = None,
        queue_on_failure: bool = True,
        telegram_user_id: int | None = None,
        amount_kopeks: int | None = None,
        operation_time: datetime | None = None,
        receipt_delivery_email: str | None = None,
        receipt_delivery_language: str | None = None,
    ) -> str | None:
        """Создание чека о доходе.

        Args:
            name: Название услуги
            amount: Сумма в рублях
            quantity: Количество
            client_info: Информация о клиенте (опционально)
            payment_id: ID платежа для логирования
            queue_on_failure: Добавить в очередь при временной недоступности
            telegram_user_id: Telegram ID пользователя для формирования описания
            amount_kopeks: Сумма в копейках для формирования описания
            operation_time: Время операции (по умолчанию текущее)
            receipt_delivery_email: Email покупателя для последующей доставки чека
            receipt_delivery_language: Язык покупателя для уведомления о чеке

        Returns:
            UUID чека или None при ошибке
        """
        if not self.configured:
            logger.warning('NaloGO не настроен, чек не создан')
            return None

        # Защита от дублей: проверяем не был ли уже создан чек для этого payment_id
        if payment_id:
            created_key = f'nalogo:created:{payment_id}'
            already_created = await cache.get(created_key)
            if already_created:
                logger.info(
                    'Чек для payment_id= уже был создан пропускаем повторное создание',
                    payment_id=payment_id,
                    already_created=already_created,
                )
                return already_created  # Возвращаем ранее созданный uuid

        # ЭТАП 1: Аутентификация
        # Если не прошла — чек точно не создавался, безопасно добавить в очередь
        try:
            if not hasattr(self.client, '_access_token') or not self.client._access_token:
                auth_success = await self.authenticate()
                if not auth_success:
                    # Аутентификация не прошла — чек не создавался, безопасно в очередь
                    if queue_on_failure:
                        await self._queue_receipt(
                            name,
                            amount,
                            quantity,
                            client_info,
                            payment_id,
                            telegram_user_id,
                            amount_kopeks,
                            receipt_delivery_email,
                            receipt_delivery_language,
                        )
                    return None
        except Exception as auth_error:
            # Ошибка аутентификации — чек не создавался, безопасно в очередь
            if self._is_service_unavailable(auth_error):
                logger.warning(
                    'NaloGO недоступен при аутентификации, чек в очередь (payment_id=, сумма=₽)',
                    payment_id=payment_id,
                    amount=amount,
                )
                if queue_on_failure:
                    await self._queue_receipt(
                        name,
                        amount,
                        quantity,
                        client_info,
                        payment_id,
                        telegram_user_id,
                        amount_kopeks,
                        receipt_delivery_email,
                        receipt_delivery_language,
                    )
            else:
                logger.error('Ошибка аутентификации NaloGO', auth_error=sanitize_proxy_error(auth_error))
            return None

        # ЭТАП 2: Создание чека
        # Если аутентификация прошла и получили таймаут — чек МОГ быть создан!
        # НЕ добавляем в очередь, требуется ручная проверка
        try:
            income_api = self.client.income()

            # Создаем клиента, если передана информация
            income_client = None
            if client_info:
                income_client = IncomeClient(
                    contact_phone=client_info.get('phone'),
                    display_name=client_info.get('name'),
                    income_type=client_info.get('income_type', IncomeType.FROM_INDIVIDUAL),
                    inn=client_info.get('inn'),
                )

            # Используем переданное время операции или текущее
            result = await income_api.create(
                name=name,
                amount=Decimal(str(amount)),
                quantity=quantity,
                operation_time=operation_time,
                client=income_client,
            )

            receipt_uuid = result.get('approvedReceiptUuid')
            if receipt_uuid:
                logger.info('Чек создан успешно: на сумму ₽', receipt_uuid=receipt_uuid, amount=amount)

                # Сохраняем в Redis чтобы предотвратить дубли (TTL 30 дней)
                if payment_id:
                    created_key = f'nalogo:created:{payment_id}'
                    await cache.set(created_key, receipt_uuid, expire=30 * 24 * 3600)

                return receipt_uuid
            logger.error('Ошибка создания чека', result=result)
            return None

        except Exception as error:
            # ВАЖНО: Аутентификация была успешной, запрос на создание чека УШЁЛ
            # При таймауте чек МОГ быть создан на сервере — НЕ добавляем в очередь!
            if self._is_service_unavailable(error):
                error_msg = sanitize_proxy_error(error)[:200]
                logger.error(
                    'ТАЙМАУТ после успешной аутентификации! Чек МОГ быть создан!',
                    payment_id=payment_id,
                    amount=amount,
                )
                # Сохраняем в очередь для ручной проверки
                await self._save_pending_verification(
                    name=name,
                    amount=amount,
                    quantity=quantity,
                    client_info=client_info,
                    payment_id=payment_id,
                    telegram_user_id=telegram_user_id,
                    amount_kopeks=amount_kopeks,
                    receipt_delivery_email=receipt_delivery_email,
                    receipt_delivery_language=receipt_delivery_language,
                    error_message=error_msg,
                )
            else:
                logger.error('Ошибка создания чека в NaloGO', error=sanitize_proxy_error(error))
            return None

    async def get_queue_length(self) -> int:
        """Получить количество чеков в очереди."""
        return await cache.llen(NALOGO_QUEUE_KEY)

    async def get_queued_receipts(self) -> list:
        """Получить список чеков в очереди (без удаления)."""
        return await cache.lrange(NALOGO_QUEUE_KEY)

    async def pop_receipt_from_queue(self) -> dict[str, Any] | None:
        """Извлечь следующий чек из очереди."""
        return await cache.rpop(NALOGO_QUEUE_KEY)

    async def requeue_receipt(self, receipt_data: dict[str, Any]) -> bool:
        """Вернуть чек обратно в очередь (при неудачной отправке)."""
        receipt_data['attempts'] = receipt_data.get('attempts', 0) + 1
        return await cache.lpush(NALOGO_QUEUE_KEY, receipt_data)

    async def find_duplicate_receipt(
        self,
        amount: float,
        created_at: datetime,
        time_window_minutes: int = 10,
    ) -> str | None:
        """Проверяет, не был ли уже создан чек с такой суммой в заданном временном окне.

        Используется для защиты от дублей при таймаутах — когда сервер создал чек,
        но ответ не вернулся.

        Args:
            amount: Сумма чека в рублях
            created_at: Время создания записи в очереди
            time_window_minutes: Окно поиска в минутах (±)

        Returns:
            UUID чека если дубликат найден, None если не найден
        """
        if not self.configured:
            return None

        try:
            # Запрашиваем чеки за день когда был создан запрос
            from_date = created_at.date()
            to_date = from_date + timedelta(days=1)

            incomes = await self.get_incomes(
                from_date=from_date,
                to_date=to_date,
                limit=50,
            )

            if not incomes:
                return None

            # Ищем чек с такой же суммой в пределах временного окна
            for income in incomes:
                income_amount = float(income.get('totalAmount', income.get('amount', 0)))

                # Проверяем сумму (с погрешностью 0.01)
                if abs(income_amount - amount) > 0.01:
                    continue

                # Проверяем время
                operation_time_str = income.get('operationTime')
                if operation_time_str:
                    try:
                        from dateutil.parser import isoparse

                        operation_time = isoparse(operation_time_str)
                        if operation_time.tzinfo is None:
                            operation_time = operation_time.replace(tzinfo=UTC)

                        time_diff = abs((operation_time - created_at).total_seconds())
                        if time_diff <= time_window_minutes * 60:
                            receipt_uuid = income.get('approvedReceiptUuid', income.get('receiptUuid'))
                            if receipt_uuid:
                                logger.info(
                                    'Найден дубликат чека: (сумма=₽, время=, разница=с)',
                                    receipt_uuid=receipt_uuid,
                                    income_amount=income_amount,
                                    operation_time=operation_time,
                                    time_diff=round(time_diff, 0),
                                )
                                return receipt_uuid
                    except Exception as parse_error:
                        logger.debug('Ошибка парсинга времени чека', parse_error=parse_error)
                        continue

            return None

        except Exception as error:
            logger.warning('Ошибка проверки дубликата чека', error=sanitize_proxy_error(error))
            return None

    async def get_incomes(
        self,
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]] | None:
        """Получить список доходов (чеков) за период.

        Args:
            from_date: Начало периода (по умолчанию 30 дней назад)
            to_date: Конец периода (по умолчанию сегодня)
            limit: Максимальное количество записей

        Returns:
            Список чеков с информацией, или None при ошибке
        """
        if not self.configured:
            logger.warning('NaloGO не настроен, невозможно получить список доходов')
            return None

        try:
            # Аутентифицируемся если нужно
            if not hasattr(self.client, '_access_token') or not self.client._access_token:
                auth_success = await self.authenticate()
                if not auth_success:
                    return []

            income_api = self.client.income()
            result = await income_api.get_list(
                from_date=from_date,
                to_date=to_date,
                limit=limit,
            )

            # API возвращает структуру с полем content или items
            incomes = result.get('content', result.get('items', []))
            logger.info('Получено доходов из NaloGO', incomes_count=len(incomes))
            return incomes

        except Exception as error:
            if self._is_service_unavailable(error):
                logger.warning('NaloGO временно недоступен', error=sanitize_proxy_error(error))
            else:
                logger.error('Ошибка получения списка доходов', error=sanitize_proxy_error(error))
            return None  # None = ошибка, [] = нет чеков
