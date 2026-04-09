import hashlib
import hmac
import json
from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


class CryptoBotService:
    def __init__(self):
        self.api_token = settings.CRYPTOBOT_API_TOKEN
        self.base_url = settings.get_cryptobot_base_url()

    async def _make_request(
        self,
        method: str,
        endpoint: str,
        data: dict | None = None,
    ) -> dict[str, Any] | None:
        if not self.api_token:
            logger.error('CryptoBot API token не настроен')
            return None

        url = f'{self.base_url}/api/{endpoint}'
        headers = {'Crypto-Pay-API-Token': self.api_token, 'Content-Type': 'application/json'}

        try:
            async with aiohttp.ClientSession() as session:
                request_kwargs: dict[str, Any] = {'headers': headers}

                if method.upper() == 'GET':
                    if data:
                        request_kwargs['params'] = data
                elif data:
                    request_kwargs['json'] = data

                async with session.request(
                    method,
                    url,
                    **request_kwargs,
                ) as response:
                    response_data = await response.json()

                    if response.status == 200 and response_data.get('ok'):
                        return response_data.get('result')
                    logger.error('CryptoBot API ошибка', response_data=response_data)
                    return None

        except Exception as e:
            logger.error('Ошибка запроса к CryptoBot API', error=e)
            return None

    async def get_me(self) -> dict[str, Any] | None:
        return await self._make_request('GET', 'getMe')

    async def create_invoice(
        self,
        amount: str,
        asset: str = 'USDT',
        description: str | None = None,
        payload: str | None = None,
        expires_in: int | None = None,
    ) -> dict[str, Any] | None:
        data = {'currency_type': 'crypto', 'asset': asset, 'amount': amount}

        if description:
            data['description'] = description

        if payload:
            data['payload'] = payload

        if expires_in:
            data['expires_in'] = expires_in

        result = await self._make_request('POST', 'createInvoice', data)

        if result:
            logger.info('Создан CryptoBot invoice на', get=result.get('invoice_id'), amount=amount, asset=asset)

        return result

    async def get_invoices(
        self,
        asset: str | None = None,
        status: str | None = None,
        offset: int = 0,
        count: int = 100,
        invoice_ids: list | None = None,
    ) -> list | None:
        data = {'offset': offset, 'count': count}

        if asset:
            data['asset'] = asset

        if status:
            data['status'] = status

        if invoice_ids:
            data['invoice_ids'] = invoice_ids

        result = await self._make_request('GET', 'getInvoices', data)

        if isinstance(result, dict):
            items = result.get('items')
            return items if isinstance(items, list) else []

        if isinstance(result, list):
            return result

        return []

    async def get_balance(self) -> list | None:
        return await self._make_request('GET', 'getBalance')

    async def get_exchange_rates(self) -> list | None:
        return await self._make_request('GET', 'getExchangeRates')

    def verify_webhook_signature(self, body: str, signature: str) -> bool:
        # По документации CryptoBot, ключ ВСЕГДА SHA256 от API токена
        token = self.api_token
        if not token:
            logger.error('CryptoBot API token не настроен, отклоняем webhook')
            return False

        try:
            secret_hash = hashlib.sha256(token.encode()).digest()

            # 1. Raw body — CryptoBot шлёт compact JSON
            expected = hmac.new(secret_hash, body.encode('utf-8'), hashlib.sha256).hexdigest()
            if hmac.compare_digest(signature, expected):
                logger.info('CryptoBot webhook подпись валидна (raw body)')
                return True

            # 2. Fallback: re-serialize compact JSON
            parsed = json.loads(body)
            check_string = json.dumps(parsed, separators=(',', ':'), ensure_ascii=False)
            expected_reserialized = hmac.new(secret_hash, check_string.encode('utf-8'), hashlib.sha256).hexdigest()
            if hmac.compare_digest(signature, expected_reserialized):
                logger.info('CryptoBot webhook подпись валидна (re-serialized)')
                return True

            # 3. Fallback: ensure_ascii=True
            check_string_ascii = json.dumps(parsed, separators=(',', ':'), ensure_ascii=True)
            expected_ascii = hmac.new(secret_hash, check_string_ascii.encode('utf-8'), hashlib.sha256).hexdigest()
            if hmac.compare_digest(signature, expected_ascii):
                logger.info('CryptoBot webhook подпись валидна (ascii-escaped)')
                return True

            logger.error(
                'Неверная подпись CryptoBot webhook',
                received_signature=signature,
                expected_raw=expected,
                expected_reserialized=expected_reserialized,
                body_length=len(body),
                token_length=len(token),
                token_prefix=token[:4] + '...',
            )
            return False

        except Exception as e:
            logger.error('Ошибка проверки подписи CryptoBot webhook', error=e)
            return False

    async def process_webhook(self, webhook_data: dict[str, Any]) -> dict[str, Any] | None:
        try:
            update_type = webhook_data.get('update_type')

            if update_type == 'invoice_paid':
                invoice_data = webhook_data.get('payload', {})

                return {
                    'event_type': 'payment',
                    'payment_id': str(invoice_data.get('invoice_id')),
                    'amount': invoice_data.get('amount'),
                    'asset': invoice_data.get('asset'),
                    'status': 'paid',
                    'user_payload': invoice_data.get('payload'),
                    'paid_at': invoice_data.get('paid_at'),
                    'payment_system': 'cryptobot',
                }

            logger.warning('Неизвестный тип CryptoBot webhook', update_type=update_type)
            return None

        except Exception as e:
            logger.error('Ошибка обработки CryptoBot webhook', error=e)
            return None
