import hashlib
import hmac
import json
import re
from typing import Any

import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

# Цифровой товар Tribute: покупка `new_digital_product`, возврат `digital_product_refunded`
# (в списке событий вики те же события названы newDigitalProduct / digitalProductRefund —
# сравниваем без регистра и подчёркиваний, чтобы принять оба написания).
DIGITAL_PURCHASE = 'purchase'
DIGITAL_REFUND = 'refund'
_DIGITAL_EVENT_KINDS = {
    'newdigitalproduct': DIGITAL_PURCHASE,
    'digitalproductrefunded': DIGITAL_REFUND,
    'digitalproductrefund': DIGITAL_REFUND,
}
# Зачисляем только рубли: amount у Tribute — в наименьших единицах валюты (копейки/центы),
# курс бот не знает, поэтому 5 USD нельзя записать как 5 ₽.
DIGITAL_ACCEPTED_CURRENCY = 'rub'


def digital_product_event_kind(event_name: Any) -> str | None:
    """'purchase' / 'refund' для событий цифрового товара, иначе None."""
    if not isinstance(event_name, str):
        return None
    return _DIGITAL_EVENT_KINDS.get(event_name.replace('_', '').lower())


def parse_product_links(raw: str | None) -> dict[int, str]:
    """`100=https://…,300=https://…` (сумма в рублях) → `{копейки: ссылка}`, по возрастанию суммы.

    Разделители — запятая, точка с запятой, пробел, перенос строки (в ссылках их нет). Битые записи
    пропускаются с предупреждением: одна опечатка не должна ронять оплату целиком.
    """
    links: dict[int, str] = {}
    for item in re.split(r'[,;\s]+', raw.strip() if isinstance(raw, str) else ''):
        if not item:
            continue
        amount_part, separator, url = item.partition('=')
        try:
            rubles = int(amount_part)
        except ValueError:
            rubles = 0
        if not separator or rubles <= 0 or not url.startswith('https://'):
            logger.warning('Tribute: запись TRIBUTE_PRODUCT_LINKS пропущена, ожидается «рубли=https://ссылка»')
            continue
        links[rubles * 100] = url
    return dict(sorted(links.items()))


def get_product_links() -> dict[int, str]:
    return parse_product_links(settings.TRIBUTE_PRODUCT_LINKS)


def format_product_amounts(links: dict[int, str]) -> str:
    return ', '.join(str(kopeks // 100) for kopeks in sorted(links))


class TributeService:
    def __init__(self):
        self.api_key = settings.TRIBUTE_API_KEY
        self.donate_link = settings.TRIBUTE_DONATE_LINK

    async def create_payment_link(
        self, user_id: int, amount_kopeks: int = 0, description: str = 'Пополнение баланса'
    ) -> str | None:
        if not settings.TRIBUTE_ENABLED:
            logger.warning('Tribute платежи отключены')
            return None

        products = get_product_links()
        if products:
            # цифровой товар: цена фиксирована, ссылка выбирается по точной сумме; нет такой суммы — None
            payment_url = products.get(int(amount_kopeks or 0))
            if not payment_url:
                logger.warning('Tribute: нет товара для суммы', amount_kopeks=amount_kopeks)
            return payment_url

        try:
            payment_url = f'{self.donate_link}&user_id={user_id}'

            logger.info('Создана ссылка Tribute для пользователя', user_id=user_id)
            return payment_url

        except Exception as e:
            logger.error('Ошибка создания Tribute ссылки', error=e)
            return None

    def verify_webhook_signature(self, payload: str, signature: str) -> bool:
        if not self.api_key:
            logger.error('Tribute API key не настроен — отклоняем webhook')
            return False

        try:
            expected_signature = hmac.new(self.api_key.encode(), payload.encode(), hashlib.sha256).hexdigest()

            is_valid = hmac.compare_digest(signature, expected_signature)

            if is_valid:
                logger.info('✅ Подпись Tribute webhook проверена успешно')
            else:
                logger.error('❌ Неверная подпись Tribute webhook')

            return is_valid

        except Exception as e:
            logger.error('Ошибка проверки подписи webhook', error=e)
            return False

    @staticmethod
    def _build_digital_product_result(kind: str, data: dict[str, Any]) -> dict[str, Any] | None:
        """Разбор `new_digital_product` / `digital_product_refunded`.

        Ключ идемпотентности — `purchase_id` (одинаков у покупки и её возврата, стабилен при ретраях).
        Без `purchase_id`, `telegram_user_id` (покупатель по email: trb_user_id `W-…`) или с неверной
        суммой возвращаем None — сервис поднимет тревогу «зачислить вручную».
        """
        purchase_id = data.get('purchase_id')
        trb_user_id = data.get('trb_user_id')
        telegram_user_id = data.get('telegram_user_id')

        if purchase_id is None:
            logger.error('Tribute: у события цифрового товара нет purchase_id', trb_user_id=trb_user_id)
            return None
        if not telegram_user_id:
            logger.error(
                'Tribute: у события цифрового товара нет telegram_user_id',
                purchase_id=purchase_id,
                trb_user_id=trb_user_id,
            )
            return None
        try:
            telegram_user_id = int(telegram_user_id)
            amount_kopeks = int(data.get('amount') or 0)
        except (ValueError, TypeError):
            logger.error('Tribute: некорректные поля цифрового товара', purchase_id=purchase_id)
            return None
        if amount_kopeks <= 0:
            logger.error('Tribute: нулевая сумма цифрового товара', purchase_id=purchase_id)
            return None

        currency = str(data.get('currency') or '').lower()
        payment_id = f'digital_{purchase_id}'

        if currency != DIGITAL_ACCEPTED_CURRENCY:
            status = 'unsupported_currency'
        elif kind == DIGITAL_REFUND:
            status = 'refunded'
        else:
            status = 'paid'

        return {
            'event_type': 'refund' if kind == DIGITAL_REFUND else 'payment',
            'payment_id': payment_id,
            'user_id': telegram_user_id,
            'trb_user_id': trb_user_id,
            'amount_kopeks': amount_kopeks,
            'currency': currency,
            'status': status,
            'external_id': f'donation_{payment_id}',
            'payment_system': 'tribute',
            'product_id': data.get('product_id'),
            'product_name': data.get('product_name'),
        }

    async def process_webhook(self, payload_or_data) -> dict[str, Any] | None:
        try:
            logger.info('🔄 Начинаем обработку Tribute webhook')

            if isinstance(payload_or_data, str):
                try:
                    webhook_data = json.loads(payload_or_data)
                    logger.info('📊 Распарсенные данные', webhook_data=webhook_data)
                except json.JSONDecodeError as e:
                    logger.error('❌ Ошибка парсинга JSON', error=e)
                    return None
            else:
                webhook_data = payload_or_data

            payment_id = None
            status = None
            amount_kopeks = 0
            telegram_user_id = None
            trb_user_id = None

            payment_id = webhook_data.get('id') or webhook_data.get('payment_id')
            status = webhook_data.get('status')
            amount_kopeks = webhook_data.get('amount', 0)
            telegram_user_id = webhook_data.get('telegram_user_id')
            trb_user_id = webhook_data.get('trb_user_id')

            if not payment_id and 'payload' in webhook_data:
                data = webhook_data['payload']
                payment_id = data.get('id') or data.get('payment_id')
                status = data.get('status')
                amount_kopeks = data.get('amount', 0)
                telegram_user_id = data.get('telegram_user_id')
                trb_user_id = data.get('trb_user_id')

            if not payment_id and 'name' in webhook_data:
                event_name = webhook_data.get('name')
                data = webhook_data.get('payload', {})

                digital_kind = digital_product_event_kind(event_name)
                if digital_kind:
                    digital_result = self._build_digital_product_result(digital_kind, data)
                    # не `event=`: в structlog это имя занято первым аргументом (TypeError «multiple values»)
                    logger.info(
                        'Tribute webhook цифрового товара разобран', event_name=event_name, result=digital_result
                    )
                    return digital_result

                amount_kopeks = data.get('amount', 0)
                telegram_user_id = data.get('telegram_user_id')
                trb_user_id = data.get('trb_user_id')

                # У new_donation НЕТ id платежа: donation_request_id — id донат-ссылки,
                # общий для всех платежей через неё (purchase_id есть только у цифровых
                # товаров). Ключ идемпотентности собираем per-событие: created_at из
                # конверта стабилен при ретраях доставки и различен у разных донатов;
                # user и amount добавлены на случай одинакового created_at.
                donation_request_id = data.get('donation_request_id')
                created_at = webhook_data.get('created_at')
                if donation_request_id is not None and created_at:
                    payment_id = f'{donation_request_id}_{telegram_user_id}_{amount_kopeks}_{created_at}'
                elif donation_request_id is not None:
                    # Без created_at ключ не уникален — уходим в fallback tribute_<tg>_<amount>,
                    # который дедуп-слой считает неуникальным и не блокирует повторные донаты.
                    payment_id = None

                if event_name in ('new_donation', 'recurrent_donation'):
                    status = 'paid'
                elif event_name == 'cancelled_subscription':
                    status = 'cancelled'
                else:
                    status = 'unknown'

            logger.info(
                '📝 Извлеченные данные',
                payment_id=payment_id,
                status=status,
                amount_kopeks=amount_kopeks,
                telegram_user_id=telegram_user_id,
                trb_user_id=trb_user_id,
            )

            if not telegram_user_id:
                logger.error(
                    '❌ Не найден telegram_user_id в webhook данных',
                    trb_user_id=trb_user_id,
                )
                logger.error(
                    '🔍 Полные данные для отладки', dumps=json.dumps(webhook_data, ensure_ascii=False, indent=2)
                )
                return None

            try:
                telegram_user_id = int(telegram_user_id)
            except (ValueError, TypeError):
                logger.error('❌ Некорректный telegram_user_id', telegram_user_id=telegram_user_id)
                return None

            result = {
                'event_type': 'payment',
                'payment_id': payment_id or f'tribute_{telegram_user_id}_{amount_kopeks}',
                'user_id': telegram_user_id,
                'trb_user_id': trb_user_id,
                'amount_kopeks': int(amount_kopeks) if amount_kopeks else 0,
                'status': status or 'paid',
                'external_id': f'donation_{payment_id or "unknown"}',
                'payment_system': 'tribute',
            }

            logger.info('✅ Tribute webhook обработан успешно', result=result)
            return result

        except Exception as e:
            logger.error('❌ Ошибка обработки Tribute webhook', error=e, exc_info=True)
            logger.error('🔍 Webhook data для отладки', dumps=json.dumps(webhook_data, ensure_ascii=False, indent=2))
            return None

    async def get_payment_status(self, payment_id: str) -> dict[str, Any] | None:
        try:
            logger.info('Запрос статуса платежа', payment_id=payment_id)
            return {'status': 'unknown', 'payment_id': payment_id}
        except Exception as e:
            logger.error('Ошибка получения статуса платежа', error=e)
            return None

    async def refund_payment(
        self, payment_id: str, amount_kopeks: int | None = None, reason: str = 'Возврат по запросу'
    ) -> dict[str, Any] | None:
        try:
            logger.info('Создание возврата для платежа', payment_id=payment_id)
            return {'refund_id': f'refund_{payment_id}', 'status': 'pending'}
        except Exception as e:
            logger.error('Ошибка создания возврата', error=e)
            return None
