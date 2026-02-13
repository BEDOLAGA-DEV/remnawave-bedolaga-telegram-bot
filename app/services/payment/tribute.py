"""Mixin для платежей Tribute — простая вспомогательная обвязка."""

from __future__ import annotations

import hashlib
import hmac

from app.config import settings
from app.utils.payment_logger import payment_logger as logger


class TributePaymentMixin:
    """Содержит методы создания платежей и проверки webhook от Tribute."""

    async def create_tribute_payment(
        self,
        amount_kopeks: int,
        user_id: int,
        description: str,
    ) -> str:
        """Формирует URL оплаты для Tribute и логирует результат."""
        if not settings.TRIBUTE_ENABLED:
            raise ValueError('Tribute payments are disabled')

        try:
            # Сохраняем полезную информацию для метрик и отладки.
            payment_data = {
                'amount': amount_kopeks,
                'currency': 'RUB',
                'description': description,
                'user_id': user_id,
                'callback_url': f'{settings.WEBHOOK_URL}/tribute/callback',
            }
            del payment_data  # данные пока не отправляются вовне, но оставляем структуру для будущего API.

            payment_url = f'https://tribute.ru/pay?amount={amount_kopeks}&user={user_id}'

            logger.info(
                'Создан Tribute платеж на %s₽ для пользователя %s',
                amount_kopeks / 100,
                user_id,
            )
            return payment_url

        except Exception as error:
            logger.error('Ошибка создания Tribute платежа: %s', error)
            raise

    def verify_tribute_webhook(self, data: dict[str, object], signature: str) -> bool:
        """Проверяет подпись запроса, присланного Tribute."""
        if not settings.TRIBUTE_API_KEY:
            return False

        try:
            message = str(data).encode()
            expected_signature = hmac.new(
                settings.TRIBUTE_API_KEY.encode(),
                message,
                hashlib.sha256,
            ).hexdigest()

            return hmac.compare_digest(signature, expected_signature)

        except Exception as error:
            logger.error('Ошибка проверки Tribute webhook: %s', error)
            return False
