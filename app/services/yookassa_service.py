import asyncio
import uuid
from typing import Any

import structlog
from yookassa import Configuration, Payment as YooKassaPayment
from yookassa.domain.common.confirmation_type import ConfirmationType
from yookassa.domain.exceptions.not_found_error import NotFoundError as YooKassaNotFoundError
from yookassa.domain.request.payment_request_builder import PaymentRequestBuilder

from app.config import settings


logger = structlog.get_logger(__name__)


class YooKassaService:
    def __init__(
        self,
        shop_id: str | None = None,
        secret_key: str | None = None,
        configured_return_url: str | None = None,
        bot_username_for_default_return: str | None = None,
    ):
        shop_id = shop_id or getattr(settings, 'YOOKASSA_SHOP_ID', None)
        secret_key = secret_key or getattr(settings, 'YOOKASSA_SECRET_KEY', None)
        configured_return_url = configured_return_url or getattr(settings, 'YOOKASSA_RETURN_URL', None)

        self.configured = False

        if not shop_id or not secret_key:
            logger.warning(
                'YooKassa SHOP_ID или SECRET_KEY не настроены в settings. Функционал платежей будет ОТКЛЮЧЕН.'
            )
        else:
            try:
                Configuration.configure(shop_id, secret_key)
                self.configured = True
                logger.info('YooKassa SDK сконфигурирован для shop_id: ...', shop_id=shop_id[:5])
            except Exception as error:
                logger.error('Ошибка конфигурации YooKassa SDK', error=error, exc_info=True)
                self.configured = False

        if not self.configured:
            self.return_url = 'https://t.me/'
            logger.warning('YooKassa не активна, используем заглушку return_url', return_url=self.return_url)
        elif configured_return_url:
            self.return_url = configured_return_url
        elif bot_username_for_default_return:
            self.return_url = f'https://t.me/{bot_username_for_default_return}'
            logger.info('YOOKASSA_RETURN_URL не установлен, используем бота', return_url=self.return_url)
        else:
            self.return_url = 'https://t.me/'
            logger.warning(
                'КРИТИЧНО: YOOKASSA_RETURN_URL не установлен И username бота не предоставлен. Используем заглушку: . Платежи могут работать некорректно.',
                return_url=self.return_url,
            )

        logger.info('YooKassa Service return_url', return_url=self.return_url)

    async def create_payment(
        self,
        amount: float,
        currency: str,
        description: str,
        metadata: dict[str, Any],
        receipt_email: str | None = None,
        receipt_phone: str | None = None,
    ) -> dict[str, Any] | None:
        """Создает платеж в YooKassa"""

        if not self.configured:
            logger.error('YooKassa не сконфигурирован. Невозможно создать платеж.')
            return None

        customer_contact_for_receipt = {}
        if receipt_email:
            customer_contact_for_receipt['email'] = receipt_email
        elif receipt_phone:
            customer_contact_for_receipt['phone'] = receipt_phone
        elif hasattr(settings, 'YOOKASSA_DEFAULT_RECEIPT_EMAIL') and settings.YOOKASSA_DEFAULT_RECEIPT_EMAIL:
            customer_contact_for_receipt['email'] = settings.YOOKASSA_DEFAULT_RECEIPT_EMAIL
        else:
            logger.error(
                'КРИТИЧНО: Не предоставлен email/телефон для чека YooKassa и YOOKASSA_DEFAULT_RECEIPT_EMAIL не установлен.'
            )
            return {
                'error': True,
                'internal_message': 'Отсутствуют контактные данные для чека YooKassa и не настроен email по умолчанию.',
            }

        try:
            builder = PaymentRequestBuilder()
            builder.set_amount({'value': str(round(amount, 2)), 'currency': currency.upper()})
            builder.set_capture(True)
            builder.set_confirmation({'type': ConfirmationType.REDIRECT, 'return_url': self.return_url})
            builder.set_description(description)
            builder.set_metadata(metadata)

            receipt_items_list: list[dict[str, Any]] = [
                {
                    'description': description[:128],
                    'quantity': '1.00',
                    'amount': {'value': str(round(amount, 2)), 'currency': currency.upper()},
                    'vat_code': int(getattr(settings, 'YOOKASSA_VAT_CODE', 1)),
                    'payment_mode': getattr(settings, 'YOOKASSA_PAYMENT_MODE', 'full_payment'),
                    'payment_subject': getattr(settings, 'YOOKASSA_PAYMENT_SUBJECT', 'service'),
                }
            ]

            receipt_data_dict: dict[str, Any] = {'customer': customer_contact_for_receipt, 'items': receipt_items_list}

            builder.set_receipt(receipt_data_dict)

            idempotence_key = str(uuid.uuid4())
            payment_request = builder.build()

            logger.info(
                'Создание платежа YooKassa (Idempotence-Key: ). Сумма: . Метаданные: . Чек',
                idempotence_key=idempotence_key,
                amount=amount,
                currency=currency,
                metadata=metadata,
                receipt_data_dict=receipt_data_dict,
            )

            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None, lambda: YooKassaPayment.create(payment_request, idempotence_key)
            )

            logger.info(
                'Ответ YooKassa Payment.create: ID=, Status=, Paid',
                response_id=response.id,
                status=response.status,
                paid=response.paid,
            )

            return {
                'id': response.id,
                'confirmation_url': response.confirmation.confirmation_url if response.confirmation else None,
                'status': response.status,
                'metadata': response.metadata,
                'amount_value': float(response.amount.value),
                'amount_currency': response.amount.currency,
                'idempotence_key_used': idempotence_key,
                'paid': response.paid,
                'refundable': response.refundable,
                'created_at': response.created_at.isoformat()
                if hasattr(response.created_at, 'isoformat')
                else str(response.created_at),
                'description_from_yk': response.description,
                'test_mode': response.test if hasattr(response, 'test') else None,
            }
        except Exception as e:
            logger.error('Ошибка создания платежа YooKassa', error=e, exc_info=True)
            return None

    async def create_sbp_payment(
        self,
        amount: float,
        currency: str,
        description: str,
        metadata: dict[str, Any],
        receipt_email: str | None = None,
        receipt_phone: str | None = None,
    ) -> dict[str, Any] | None:
        if not self.configured:
            logger.error('YooKassa не сконфигурирован. Невозможно создать платеж через СБП.')
            return None

        customer_contact_for_receipt = {}
        if receipt_email:
            customer_contact_for_receipt['email'] = receipt_email
        elif receipt_phone:
            customer_contact_for_receipt['phone'] = receipt_phone
        elif hasattr(settings, 'YOOKASSA_DEFAULT_RECEIPT_EMAIL') and settings.YOOKASSA_DEFAULT_RECEIPT_EMAIL:
            customer_contact_for_receipt['email'] = settings.YOOKASSA_DEFAULT_RECEIPT_EMAIL
        else:
            logger.error(
                'КРИТИЧНО: Не предоставлен email/телефон для чека YooKassa и YOOKASSA_DEFAULT_RECEIPT_EMAIL не установлен.'
            )
            return {
                'error': True,
                'internal_message': 'Отсутствуют контактные данные для чека YooKassa и не настроен email по умолчанию.',
            }

        try:
            # Создаем один платеж с подтверждением через QR
            # Это позволит получить QR-код для пользователя
            builder = PaymentRequestBuilder()

            builder.set_amount({'value': str(round(amount, 2)), 'currency': currency.upper()})

            builder.set_capture(True)

            # Устанавливаем подтверждение через redirect для получения вебхуков
            builder.set_confirmation({'type': 'redirect', 'return_url': self.return_url})

            builder.set_description(description)

            builder.set_metadata(metadata)

            builder.set_payment_method_data({'type': 'sbp'})

            receipt_items_list: list[dict[str, Any]] = [
                {
                    'description': description[:128],
                    'quantity': '1.00',
                    'amount': {'value': str(round(amount, 2)), 'currency': currency.upper()},
                    'vat_code': int(getattr(settings, 'YOOKASSA_VAT_CODE', 1)),
                    'payment_mode': getattr(settings, 'YOOKASSA_PAYMENT_MODE', 'full_payment'),
                    'payment_subject': getattr(settings, 'YOOKASSA_PAYMENT_SUBJECT', 'service'),
                }
            ]

            receipt_data_dict: dict[str, Any] = {'customer': customer_contact_for_receipt, 'items': receipt_items_list}

            builder.set_receipt(receipt_data_dict)

            idempotence_key = str(uuid.uuid4())

            payment_request = builder.build()

            logger.info(
                'Создание платежа YooKassa СБП с подтверждением redirect (Idempotence-Key: ). Сумма: . Метаданные: . Чек',
                idempotence_key=idempotence_key,
                amount=amount,
                currency=currency,
                metadata=metadata,
                receipt_data_dict=receipt_data_dict,
            )

            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None, lambda: YooKassaPayment.create(payment_request, idempotence_key)
            )

            logger.info(
                'Ответ YooKassa Payment.create (СБП, redirect): ID=, Status=, Paid',
                response_id=response.id,
                status=response.status,
                paid=response.paid,
            )

            # Возвращаем данные платежа с redirect-подтверждением
            # YooKassa покажет QR на десктопе или список банков на мобильном
            return {
                'id': response.id,
                'qr_confirmation_data': response.confirmation.confirmation_data
                if response.confirmation and hasattr(response.confirmation, 'confirmation_data')
                else None,
                'confirmation_url': response.confirmation.confirmation_url
                if response.confirmation and hasattr(response.confirmation, 'confirmation_url')
                else None,
                'status': response.status,
                'metadata': response.metadata,
                'amount_value': float(response.amount.value),
                'amount_currency': response.amount.currency,
                'idempotence_key_used': idempotence_key,
                'paid': response.paid,
                'refundable': response.refundable,
                'created_at': response.created_at.isoformat()
                if hasattr(response.created_at, 'isoformat')
                else str(response.created_at),
                'description_from_yk': response.description,
                'test_mode': response.test if hasattr(response, 'test') else None,
            }
        except Exception as e:
            logger.error('Ошибка создания платежа YooKassa СБП', error=e, exc_info=True)
            return None

    async def get_payment_info(self, payment_id_in_yookassa: str) -> dict[str, Any] | None:
        if not self.configured:
            logger.error('YooKassa не сконфигурирован. Невозможно получить информацию о платеже.')
            return None

        try:
            logger.info('Получение информации о платеже YooKassa ID', payment_id_in_yookassa=payment_id_in_yookassa)

            loop = asyncio.get_running_loop()
            payment_info_yk = await loop.run_in_executor(None, lambda: YooKassaPayment.find_one(payment_id_in_yookassa))

            if payment_info_yk:
                logger.info(
                    'Информация о платеже YooKassa Status=, Paid',
                    payment_id_in_yookassa=payment_id_in_yookassa,
                    status=payment_info_yk.status,
                    paid=payment_info_yk.paid,
                )
                return {
                    'id': payment_info_yk.id,
                    'status': payment_info_yk.status,
                    'paid': payment_info_yk.paid,
                    'amount_value': float(payment_info_yk.amount.value),
                    'amount_currency': payment_info_yk.amount.currency,
                    'metadata': payment_info_yk.metadata,
                    'description': payment_info_yk.description,
                    'refundable': payment_info_yk.refundable,
                    'created_at': payment_info_yk.created_at.isoformat()
                    if hasattr(payment_info_yk.created_at, 'isoformat')
                    else str(payment_info_yk.created_at),
                    'captured_at': payment_info_yk.captured_at.isoformat()
                    if payment_info_yk.captured_at and hasattr(payment_info_yk.captured_at, 'isoformat')
                    else None,
                    'payment_method_type': payment_info_yk.payment_method.type
                    if payment_info_yk.payment_method
                    else None,
                    'test_mode': payment_info_yk.test if hasattr(payment_info_yk, 'test') else None,
                }
            logger.warning('Платеж не найден в YooKassa ID', payment_id_in_yookassa=payment_id_in_yookassa)
            return None
        except YooKassaNotFoundError:
            logger.warning(
                'Платеж не найден в YooKassa (404)',
                payment_id_in_yookassa=payment_id_in_yookassa,
            )
            return None
        except Exception as e:
            logger.error(
                'Ошибка получения информации о платеже YooKassa',
                payment_id_in_yookassa=payment_id_in_yookassa,
                error=e,
                exc_info=True,
            )
            return None
