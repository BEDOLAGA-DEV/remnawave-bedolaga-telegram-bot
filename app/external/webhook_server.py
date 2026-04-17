import base64
import hashlib
import hmac
import json
from collections.abc import Iterable

import structlog
from aiogram import Bot
from aiohttp import web

from app.config import settings
from app.database.database import get_db
from app.services.payment_service import PaymentService
from app.services.tribute_service import TributeService


logger = structlog.get_logger(__name__)


class WebhookServer:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.app = None
        self.runner = None
        self.site = None
        self.tribute_service = TributeService(bot)

    async def create_app(self) -> web.Application:
        self.app = web.Application()

        self.app.router.add_post(settings.TRIBUTE_WEBHOOK_PATH, self._tribute_webhook_handler)

        if settings.is_mulenpay_enabled():
            self.app.router.add_post(settings.MULENPAY_WEBHOOK_PATH, self._mulenpay_webhook_handler)

        if settings.is_cryptobot_enabled():
            self.app.router.add_post(settings.CRYPTOBOT_WEBHOOK_PATH, self._cryptobot_webhook_handler)

        if settings.is_freekassa_enabled():
            self.app.router.add_post(settings.FREEKASSA_WEBHOOK_PATH, self._freekassa_webhook_handler)
        # Диагностика почему Freekassa не включена
        elif settings.FREEKASSA_ENABLED:
            missing = []
            if settings.FREEKASSA_SHOP_ID is None:
                missing.append('FREEKASSA_SHOP_ID')
            if settings.FREEKASSA_API_KEY is None:
                missing.append('FREEKASSA_API_KEY')
            if settings.FREEKASSA_SECRET_WORD_1 is None:
                missing.append('FREEKASSA_SECRET_WORD_1')
            if settings.FREEKASSA_SECRET_WORD_2 is None:
                missing.append('FREEKASSA_SECRET_WORD_2')
            if missing:
                logger.warning(
                    'Freekassa ENABLED=true, но webhook не зарегистрирован. Отсутствуют параметры',
                    value=', '.join(missing),
                )

        self.app.router.add_get('/health', self._health_check)

        if settings.is_apple_iap_enabled():
            self.app.router.add_post(settings.APPLE_IAP_WEBHOOK_PATH, self._apple_iap_webhook_handler)

        self.app.router.add_options(settings.TRIBUTE_WEBHOOK_PATH, self._options_handler)
        if settings.is_mulenpay_enabled():
            self.app.router.add_options(settings.MULENPAY_WEBHOOK_PATH, self._options_handler)
        if settings.is_cryptobot_enabled():
            self.app.router.add_options(settings.CRYPTOBOT_WEBHOOK_PATH, self._options_handler)
        if settings.is_freekassa_enabled():
            self.app.router.add_options(settings.FREEKASSA_WEBHOOK_PATH, self._options_handler)
        if settings.is_apple_iap_enabled():
            self.app.router.add_options(settings.APPLE_IAP_WEBHOOK_PATH, self._options_handler)

        logger.info('Webhook сервер настроен:')
        logger.info('Tribute webhook: POST', TRIBUTE_WEBHOOK_PATH=settings.TRIBUTE_WEBHOOK_PATH)
        if settings.is_mulenpay_enabled():
            mulenpay_name = settings.get_mulenpay_display_name()
            logger.info(
                '- webhook: POST', mulenpay_name=mulenpay_name, MULENPAY_WEBHOOK_PATH=settings.MULENPAY_WEBHOOK_PATH
            )
        if settings.is_cryptobot_enabled():
            logger.info('CryptoBot webhook: POST', CRYPTOBOT_WEBHOOK_PATH=settings.CRYPTOBOT_WEBHOOK_PATH)
        if settings.is_freekassa_enabled():
            logger.info('Freekassa webhook: POST', FREEKASSA_WEBHOOK_PATH=settings.FREEKASSA_WEBHOOK_PATH)
        if settings.is_apple_iap_enabled():
            logger.info('Apple IAP webhook: POST', APPLE_IAP_WEBHOOK_PATH=settings.APPLE_IAP_WEBHOOK_PATH)
        logger.info('  - Health check: GET /health')

        return self.app

    async def start(self):
        try:
            if not self.app:
                await self.create_app()

            self.runner = web.AppRunner(self.app)
            await self.runner.setup()

            self.site = web.TCPSite(self.runner, host=settings.TRIBUTE_WEBHOOK_HOST, port=settings.TRIBUTE_WEBHOOK_PORT)

            await self.site.start()

            logger.info(
                'Webhook сервер запущен на',
                TRIBUTE_WEBHOOK_HOST=settings.TRIBUTE_WEBHOOK_HOST,
                TRIBUTE_WEBHOOK_PORT=settings.TRIBUTE_WEBHOOK_PORT,
            )
            logger.info(
                'Tribute webhook URL: http://',
                TRIBUTE_WEBHOOK_HOST=settings.TRIBUTE_WEBHOOK_HOST,
                TRIBUTE_WEBHOOK_PORT=settings.TRIBUTE_WEBHOOK_PORT,
                TRIBUTE_WEBHOOK_PATH=settings.TRIBUTE_WEBHOOK_PATH,
            )
            if settings.is_mulenpay_enabled():
                mulenpay_name = settings.get_mulenpay_display_name()
                logger.info(
                    'webhook URL: http://',
                    mulenpay_name=mulenpay_name,
                    TRIBUTE_WEBHOOK_HOST=settings.TRIBUTE_WEBHOOK_HOST,
                    TRIBUTE_WEBHOOK_PORT=settings.TRIBUTE_WEBHOOK_PORT,
                    MULENPAY_WEBHOOK_PATH=settings.MULENPAY_WEBHOOK_PATH,
                )
            if settings.is_cryptobot_enabled():
                logger.info(
                    'CryptoBot webhook URL: http://',
                    TRIBUTE_WEBHOOK_HOST=settings.TRIBUTE_WEBHOOK_HOST,
                    TRIBUTE_WEBHOOK_PORT=settings.TRIBUTE_WEBHOOK_PORT,
                    CRYPTOBOT_WEBHOOK_PATH=settings.CRYPTOBOT_WEBHOOK_PATH,
                )

        except Exception as e:
            logger.error('Ошибка запуска webhook сервера', error=e)
            raise

    async def stop(self):
        try:
            if self.site:
                await self.site.stop()
                logger.info('Webhook сайт остановлен')

            if self.runner:
                await self.runner.cleanup()
                logger.info('Webhook runner очищен')

        except Exception as e:
            logger.error('Ошибка остановки webhook сервера', error=e)

    async def _options_handler(self, request: web.Request) -> web.Response:
        return web.Response(
            status=200,
            headers={
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type, trbt-signature, Crypto-Pay-API-Signature, X-MulenPay-Signature, Authorization',
            },
        )

    async def _mulenpay_webhook_handler(self, request: web.Request) -> web.Response:
        try:
            mulenpay_name = settings.get_mulenpay_display_name()
            logger.info('webhook', mulenpay_name=mulenpay_name, method=request.method, request_path=request.path)
            logger.info('webhook headers', mulenpay_name=mulenpay_name, headers=dict(request.headers))
            raw_body = await request.read()

            if not raw_body:
                logger.warning('Пустой webhook', mulenpay_name=mulenpay_name)
                return web.json_response({'status': 'error', 'reason': 'empty_body'}, status=400)

            # Временно отключаем проверку подписи для отладки
            # TODO: Включить обратно после настройки MulenPay
            if not self._verify_mulenpay_signature(request, raw_body):
                logger.warning(
                    'webhook signature verification failed, but processing anyway for debugging',
                    mulenpay_name=mulenpay_name,
                )
                # return web.json_response({"status": "error", "reason": "invalid_signature"}, status=401)

            try:
                payload = json.loads(raw_body.decode('utf-8'))
            except json.JSONDecodeError as error:
                logger.error('Ошибка парсинга webhook', mulenpay_name=mulenpay_name, error=error)
                return web.json_response({'status': 'error', 'reason': 'invalid_json'}, status=400)

            payment_service = PaymentService(self.bot)

            # Получаем соединение с БД
            db_generator = get_db()
            db = await db_generator.__anext__()

            try:
                success = await payment_service.process_mulenpay_callback(db, payload)
                if success:
                    return web.json_response({'status': 'ok'}, status=200)
                return web.json_response({'status': 'error', 'reason': 'processing_failed'}, status=400)
            except Exception as error:
                logger.error('Ошибка обработки webhook', mulenpay_name=mulenpay_name, error=error, exc_info=True)
                return web.json_response({'status': 'error', 'reason': 'internal_error'}, status=500)
            finally:
                try:
                    await db_generator.__anext__()
                except StopAsyncIteration:
                    pass

        except Exception as error:
            mulenpay_name = settings.get_mulenpay_display_name()
            logger.error('Критическая ошибка webhook', mulenpay_name=mulenpay_name, error=error, exc_info=True)
            return web.json_response({'status': 'error', 'reason': 'internal_error', 'message': str(error)}, status=500)

    @staticmethod
    def _extract_mulenpay_header(request: web.Request, header_names: Iterable[str]) -> str | None:
        for header_name in header_names:
            value = request.headers.get(header_name)
            if value:
                return value.strip()
        return None

    @staticmethod
    def _verify_mulenpay_signature(request: web.Request, raw_body: bytes) -> bool:
        secret_key = settings.MULENPAY_SECRET_KEY
        display_name = settings.get_mulenpay_display_name()
        if not secret_key:
            logger.error('secret key is not configured', display_name=display_name)
            return False

        # Логируем все заголовки для отладки
        logger.info('webhook headers for signature verification', display_name=display_name)
        for header_name, header_value in request.headers.items():
            if any(keyword in header_name.lower() for keyword in ['signature', 'sign', 'token', 'auth']):
                logger.info('log event', header_name=header_name, header_value=header_value)

        signature = WebhookServer._extract_mulenpay_header(
            request,
            (
                'X-MulenPay-Signature',
                'X-Mulenpay-Signature',
                'X-MULENPAY-SIGNATURE',
                'X-MulenPay-Webhook-Signature',
                'X-Mulenpay-Webhook-Signature',
                'X-MULENPAY-WEBHOOK-SIGNATURE',
                'X-Signature',
                'Signature',
                'X-MulenPay-Sign',
                'X-Mulenpay-Sign',
                'X-MULENPAY-SIGN',
                'MulenPay-Signature',
                'Mulenpay-Signature',
                'MULENPAY-SIGNATURE',
                'signature',
                'sign',
            ),
        )
        if signature:
            normalized_signature = signature
            if normalized_signature.lower().startswith('sha256='):
                normalized_signature = normalized_signature.split('=', 1)[1].strip()

            hmac_digest = hmac.new(
                secret_key.encode('utf-8'),
                raw_body,
                hashlib.sha256,
            ).digest()
            expected_hex_signature = hmac_digest.hex()
            expected_base64_signature = base64.b64encode(hmac_digest).decode('utf-8').strip()
            expected_urlsafe_base64_signature = base64.urlsafe_b64encode(hmac_digest).decode('utf-8').strip()

            normalized_signature_lower = normalized_signature.lower()
            if hmac.compare_digest(normalized_signature_lower, expected_hex_signature.lower()):
                return True

            normalized_signature_no_padding = normalized_signature.rstrip('=')
            if hmac.compare_digest(normalized_signature_no_padding, expected_base64_signature.rstrip('=')):
                return True

            if hmac.compare_digest(normalized_signature_no_padding, expected_urlsafe_base64_signature.rstrip('=')):
                return True

            logger.error('Неверная подпись webhook', display_name=display_name)
            return False

        authorization_header = request.headers.get('Authorization')
        if authorization_header:
            scheme, _, value = authorization_header.partition(' ')
            scheme_lower = scheme.lower()
            token = value.strip() if value else scheme.strip()

            if scheme_lower in ('bearer', 'token'):
                if hmac.compare_digest(token, secret_key):
                    return True

                logger.error('Неверный токен webhook', scheme=scheme, display_name=display_name)
                return False

            if not value and hmac.compare_digest(token, secret_key):
                return True

        fallback_token = WebhookServer._extract_mulenpay_header(
            request,
            (
                'X-MulenPay-Token',
                'X-Mulenpay-Token',
                'X-Webhook-Token',
            ),
        )
        if fallback_token and hmac.compare_digest(fallback_token, secret_key):
            return True

        logger.info(
            '%s webhook headers received: %s',
            display_name,
            {key: value for key, value in request.headers.items() if 'authorization' not in key.lower()},
        )

        logger.error('Отсутствует подпись webhook', display_name=display_name)
        return False

    async def _tribute_webhook_handler(self, request: web.Request) -> web.Response:
        try:
            logger.info('Получен Tribute webhook', method=request.method, path=request.path)
            logger.info('Headers', value=dict(request.headers))

            raw_body = await request.read()

            if not raw_body:
                logger.warning('Получен пустой webhook от Tribute')
                return web.json_response({'status': 'error', 'reason': 'empty_body'}, status=400)

            payload = raw_body.decode('utf-8')
            logger.info('Payload', payload=payload)

            try:
                webhook_data = json.loads(payload)
                logger.info('Распарсенные данные', webhook_data=webhook_data)
            except json.JSONDecodeError as e:
                logger.error('Ошибка парсинга JSON', error=e)
                return web.json_response({'status': 'error', 'reason': 'invalid_json'}, status=400)

            signature = request.headers.get('trbt-signature')
            logger.info('Signature', signature=signature)

            if not signature:
                logger.error('Отсутствует заголовок подписи Tribute webhook')
                return web.json_response({'status': 'error', 'reason': 'missing_signature'}, status=401)

            if settings.TRIBUTE_API_KEY:
                from app.external.tribute import TributeService as TributeAPI

                tribute_api = TributeAPI()
                if not tribute_api.verify_webhook_signature(payload, signature):
                    logger.error('Неверная подпись Tribute webhook')
                    return web.json_response({'status': 'error', 'reason': 'invalid_signature'}, status=401)

            result = await self.tribute_service.process_webhook(payload)

            if result:
                logger.info('Tribute webhook обработан успешно', result=result)
                return web.json_response({'status': 'ok', 'result': result}, status=200)
            logger.error('Ошибка обработки Tribute webhook')
            return web.json_response({'status': 'error', 'reason': 'processing_failed'}, status=400)

        except Exception as e:
            logger.error('Критическая ошибка обработки Tribute webhook', error=e, exc_info=True)
            return web.json_response({'status': 'error', 'reason': 'internal_error', 'message': str(e)}, status=500)

    async def _cryptobot_webhook_handler(self, request: web.Request) -> web.Response:
        try:
            logger.info('Получен CryptoBot webhook', method=request.method, path=request.path)
            logger.info('Headers', value=dict(request.headers))

            raw_body = await request.read()

            if not raw_body:
                logger.warning('Получен пустой CryptoBot webhook')
                return web.json_response({'status': 'error', 'reason': 'empty_body'}, status=400)

            payload = raw_body.decode('utf-8')
            logger.info('CryptoBot Payload', payload=payload)

            try:
                webhook_data = json.loads(payload)
                logger.info('CryptoBot данные', webhook_data=webhook_data)
            except json.JSONDecodeError as e:
                logger.error('Ошибка парсинга CryptoBot JSON', error=e)
                return web.json_response({'status': 'error', 'reason': 'invalid_json'}, status=400)

            signature = request.headers.get('Crypto-Pay-API-Signature')
            logger.info('CryptoBot Signature', signature=signature)

            if settings.CRYPTOBOT_API_TOKEN:
                if not signature:
                    logger.error('CryptoBot webhook без подписи')
                    return web.json_response({'status': 'error', 'reason': 'missing_signature'}, status=401)
                from app.external.cryptobot import CryptoBotService

                cryptobot_service = CryptoBotService()
                if not cryptobot_service.verify_webhook_signature(payload, signature):
                    logger.error('Неверная подпись CryptoBot webhook')
                    return web.json_response({'status': 'error', 'reason': 'invalid_signature'}, status=401)

            from app.database.database import AsyncSessionLocal
            from app.services.payment_service import PaymentService

            payment_service = PaymentService(self.bot)

            async with AsyncSessionLocal() as db:
                result = await payment_service.process_cryptobot_webhook(db, webhook_data)

            if result:
                logger.info('CryptoBot webhook обработан успешно')
                return web.json_response({'status': 'ok'}, status=200)
            logger.error('Ошибка обработки CryptoBot webhook')
            return web.json_response({'status': 'error', 'reason': 'processing_failed'}, status=400)

        except Exception as e:
            logger.error('Критическая ошибка обработки CryptoBot webhook', error=e, exc_info=True)
            return web.json_response({'status': 'error', 'reason': 'internal_error', 'message': str(e)}, status=500)

    async def _health_check(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                'status': 'ok',
                'service': 'payment-webhooks',
                'tribute_enabled': settings.TRIBUTE_ENABLED,
                'cryptobot_enabled': settings.is_cryptobot_enabled(),
                'freekassa_enabled': settings.is_freekassa_enabled(),
                'port': settings.TRIBUTE_WEBHOOK_PORT,
                'tribute_path': settings.TRIBUTE_WEBHOOK_PATH,
                'cryptobot_path': settings.CRYPTOBOT_WEBHOOK_PATH if settings.is_cryptobot_enabled() else None,
                'freekassa_path': settings.FREEKASSA_WEBHOOK_PATH if settings.is_freekassa_enabled() else None,
            }
        )

    async def _freekassa_webhook_handler(self, request: web.Request) -> web.Response:
        """
        Обработчик webhook от Freekassa.

        Freekassa отправляет POST запрос с form-data:
        - MERCHANT_ID: ID магазина
        - AMOUNT: Сумма платежа
        - MERCHANT_ORDER_ID: Наш order_id
        - SIGN: Подпись MD5(shop_id:amount:secret2:order_id)
        - intid: ID транзакции Freekassa
        - CUR_ID: ID валюты/платежной системы
        """
        try:
            logger.info('Получен Freekassa webhook', method=request.method, path=request.path)

            # Получаем IP клиента
            client_ip = request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
            if not client_ip:
                client_ip = request.remote or 'unknown'
            logger.info('Freekassa webhook IP', client_ip=client_ip)

            # Freekassa отправляет form-data
            try:
                form_data = await request.post()
            except Exception as e:
                logger.error('Ошибка парсинга Freekassa form-data', error=e)
                return web.Response(text='NO', status=400)

            logger.info('Freekassa webhook data', value=dict(form_data))

            # Извлекаем параметры
            merchant_id = int(form_data.get('MERCHANT_ID', 0))
            amount = float(form_data.get('AMOUNT', 0))
            order_id = form_data.get('MERCHANT_ORDER_ID', '')
            sign = form_data.get('SIGN', '')
            intid = form_data.get('intid', '')
            cur_id = form_data.get('CUR_ID')

            if not order_id or not sign:
                logger.warning('Freekassa webhook: отсутствуют обязательные параметры')
                return web.Response(text='NO', status=400)

            # Обрабатываем платеж через PaymentService
            from app.database.database import AsyncSessionLocal
            from app.services.payment_service import PaymentService

            payment_service = PaymentService(self.bot)

            async with AsyncSessionLocal() as db:
                success = await payment_service.process_freekassa_webhook(
                    db=db,
                    merchant_id=merchant_id,
                    amount=amount,
                    order_id=order_id,
                    sign=sign,
                    intid=intid,
                    cur_id=int(cur_id) if cur_id else None,
                    client_ip=client_ip,
                )

            if success:
                logger.info('Freekassa webhook обработан успешно: order_id', order_id=order_id)
                # Freekassa ожидает YES в ответе
                return web.Response(text='YES', status=200)
            logger.error('Ошибка обработки Freekassa webhook: order_id', order_id=order_id)
            return web.Response(text='NO', status=400)

        except Exception as e:
            logger.error('Критическая ошибка обработки Freekassa webhook', error=e, exc_info=True)
            return web.Response(text='NO', status=500)

    async def _apple_iap_webhook_handler(self, request: web.Request) -> web.Response:
        """Handle Apple App Store Server Notifications V2."""
        try:
            logger.info('Получен Apple IAP webhook', method=request.method, path=request.path)

            raw_body = await request.read()
            if not raw_body:
                logger.warning('Пустой Apple IAP webhook')
                return web.Response(status=400)

            try:
                body = json.loads(raw_body.decode('utf-8'))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                logger.error('Ошибка парсинга Apple IAP webhook', error=e)
                return web.Response(status=400)

            signed_payload = body.get('signedPayload')
            if not signed_payload:
                logger.warning('No signedPayload in Apple webhook')
                return web.Response(status=400)

            # Verify and decode the notification
            from app.external.apple_iap import AppleIAPService

            apple_service = AppleIAPService()
            notification = apple_service.verify_notification(signed_payload)
            if not notification:
                logger.warning('Apple webhook signature verification failed')
                return web.Response(status=403)

            notification_type = notification.get('notificationType', '')
            subtype = notification.get('subtype', '')

            # Verify notification environment matches our config
            notif_env = notification.get('data', {}).get('environment', '')
            expected_envs = {'Production', 'Sandbox'}  # accept both during review
            if settings.APPLE_IAP_ENVIRONMENT == 'Production':
                expected_envs = {'Production', 'Sandbox'}  # Sandbox for App Review
            else:
                expected_envs = {'Sandbox'}
            if notif_env and notif_env not in expected_envs:
                logger.warning(
                    'Apple webhook environment mismatch',
                    expected=settings.APPLE_IAP_ENVIRONMENT,
                    received=notif_env,
                )
                return web.Response(status=200)  # ACK but ignore

            logger.info(
                'Apple notification received',
                notification_type=notification_type,
                subtype=subtype,
                environment=notif_env,
            )

            # Handle notification types
            if notification_type == 'TEST':
                logger.info('Apple TEST notification received — OK')
                return web.Response(status=200)

            if notification_type == 'REFUND':
                await self._handle_apple_refund(notification, apple_service)
                return web.Response(status=200)

            if notification_type == 'REFUND_REVERSED':
                await self._handle_apple_refund_reversed(notification)
                return web.Response(status=200)

            if notification_type == 'CONSUMPTION_REQUEST':
                await self._handle_apple_consumption_request(notification, apple_service)
                return web.Response(status=200)

            if notification_type in ('ONE_TIME_CHARGE', 'REFUND_DECLINED'):
                logger.info('Apple notification logged', notification_type=notification_type)
                return web.Response(status=200)

            logger.info('Unhandled Apple notification type', notification_type=notification_type)
            return web.Response(status=200)

        except Exception as e:
            logger.error('Критическая ошибка обработки Apple IAP webhook', error=e, exc_info=True)
            return web.Response(status=500)

    async def _handle_apple_refund(self, notification: dict, apple_service) -> None:
        """Handle REFUND notification — deduct credited balance."""
        try:
            data = notification.get('data', {})
            signed_txn_info = data.get('signedTransactionInfo')
            if not signed_txn_info:
                logger.warning('No signedTransactionInfo in REFUND notification')
                return

            txn_info = apple_service._verify_and_decode_jws(signed_txn_info)
            if not txn_info:
                logger.warning('Failed to verify REFUND transaction info')
                return

            apple_txn_id = str(txn_info.get('transactionId', ''))
            original_txn_id = str(txn_info.get('originalTransactionId', ''))
            product_id = txn_info.get('productId', '')

            from app.database.crud.apple_iap import (
                get_apple_transaction_by_transaction_id,
                mark_apple_transaction_refunded,
            )
            from app.database.crud.transaction import create_transaction
            from app.database.crud.user import get_user_by_id, subtract_user_balance
            from app.database.database import AsyncSessionLocal
            from app.database.models import PaymentMethod, TransactionType

            lookup_id = original_txn_id or apple_txn_id

            async with AsyncSessionLocal() as db:
                apple_txn = await get_apple_transaction_by_transaction_id(db, lookup_id)
                if not apple_txn:
                    # Try the other ID
                    apple_txn = await get_apple_transaction_by_transaction_id(db, apple_txn_id)

                if not apple_txn:
                    logger.warning(
                        'Apple REFUND: transaction not found',
                        transaction_id=apple_txn_id,
                        original_transaction_id=original_txn_id,
                    )
                    return

                if apple_txn.status == 'refunded':
                    logger.info('Apple REFUND: already refunded', transaction_id=lookup_id)
                    return

                if apple_txn.environment == 'Sandbox' and settings.APPLE_IAP_ENVIRONMENT == 'Production':
                    logger.info(
                        'Apple REFUND: ignoring sandbox refund on production',
                        transaction_id=lookup_id,
                        user_id=apple_txn.user_id,
                    )
                    return

                user = await get_user_by_id(db, apple_txn.user_id)
                if not user:
                    logger.error('Apple REFUND: user not found', user_id=apple_txn.user_id)
                    return

                # Cap deduction to current balance to prevent negative balance
                refund_amount = min(apple_txn.amount_kopeks, user.balance_kopeks)
                if refund_amount < apple_txn.amount_kopeks:
                    logger.warning(
                        'Apple REFUND: partial balance deduction (user already spent funds)',
                        full_amount=apple_txn.amount_kopeks,
                        deducted=refund_amount,
                        user_balance=user.balance_kopeks,
                        user_id=user.id,
                    )

                    # Disable active subscriptions — funds were spent and refunded
                    from app.database.crud.subscription import (
                        deactivate_subscription,
                        get_active_subscriptions_by_user_id,
                    )

                    active_subs = await get_active_subscriptions_by_user_id(db, user.id)
                    for sub in active_subs:
                        await deactivate_subscription(db, sub, commit=False)
                        logger.warning(
                            'Apple REFUND: disabled subscription due to insufficient balance',
                            subscription_id=sub.id,
                            user_id=user.id,
                        )

                if refund_amount > 0:
                    await subtract_user_balance(
                        db=db,
                        user=user,
                        amount_kopeks=refund_amount,
                        description=f'Возврат Apple IAP: {product_id}',
                        create_transaction=True,
                        payment_method=PaymentMethod.APPLE_IAP,
                        transaction_type=TransactionType.REFUND,
                        commit=False,
                    )

                await mark_apple_transaction_refunded(db, apple_txn.transaction_id)
                await db.commit()

                logger.info(
                    '✅ Apple REFUND processed',
                    transaction_id=apple_txn.transaction_id,
                    amount_kopeks=apple_txn.amount_kopeks,
                    user_id=user.id,
                )

        except Exception as e:
            logger.error('Error handling Apple REFUND', error=e, exc_info=True)

    async def _handle_apple_refund_reversed(self, notification: dict) -> None:
        """Handle REFUND_REVERSED — re-credit balance that was previously deducted."""
        try:
            data = notification.get('data', {})
            signed_txn_info = data.get('signedTransactionInfo')
            if not signed_txn_info:
                logger.warning('No signedTransactionInfo in REFUND_REVERSED notification')
                return

            from app.external.apple_iap import AppleIAPService

            apple_service = AppleIAPService()
            txn_info = apple_service._verify_and_decode_jws(signed_txn_info)
            if not txn_info:
                logger.warning('Failed to verify REFUND_REVERSED transaction info')
                return

            apple_txn_id = str(txn_info.get('transactionId', ''))
            original_txn_id = str(txn_info.get('originalTransactionId', ''))
            product_id = txn_info.get('productId', '')

            from app.database.crud.apple_iap import get_apple_transaction_by_transaction_id
            from app.database.crud.user import add_user_balance, get_user_by_id
            from app.database.database import AsyncSessionLocal
            from app.database.models import PaymentMethod

            lookup_id = original_txn_id or apple_txn_id

            async with AsyncSessionLocal() as db:
                apple_txn = await get_apple_transaction_by_transaction_id(db, lookup_id)
                if not apple_txn:
                    apple_txn = await get_apple_transaction_by_transaction_id(db, apple_txn_id)

                if not apple_txn:
                    logger.warning(
                        'Apple REFUND_REVERSED: transaction not found',
                        transaction_id=apple_txn_id,
                    )
                    return

                if apple_txn.status != 'refunded':
                    logger.info(
                        'Apple REFUND_REVERSED: transaction not in refunded state',
                        transaction_id=lookup_id,
                        status=apple_txn.status,
                    )
                    return

                if apple_txn.environment == 'Sandbox' and settings.APPLE_IAP_ENVIRONMENT == 'Production':
                    logger.info(
                        'Apple REFUND_REVERSED: ignoring sandbox on production',
                        transaction_id=lookup_id,
                    )
                    return

                user = await get_user_by_id(db, apple_txn.user_id)
                if not user:
                    logger.error('Apple REFUND_REVERSED: user not found', user_id=apple_txn.user_id)
                    return

                # Re-credit the balance
                await add_user_balance(
                    db=db,
                    user=user,
                    amount_kopeks=apple_txn.amount_kopeks,
                    description=f'Отмена возврата Apple IAP: {product_id}',
                    payment_method=PaymentMethod.APPLE_IAP,
                    commit=False,
                )

                apple_txn.status = 'verified'
                apple_txn.refunded_at = None
                await db.commit()

                logger.info(
                    '✅ Apple REFUND_REVERSED processed — balance re-credited',
                    transaction_id=lookup_id,
                    amount_kopeks=apple_txn.amount_kopeks,
                    user_id=user.id,
                )

        except Exception as e:
            logger.error('Error handling Apple REFUND_REVERSED', error=e, exc_info=True)

    async def _handle_apple_consumption_request(self, notification: dict, apple_service) -> None:
        """Handle CONSUMPTION_REQUEST — send consumption info to Apple."""
        try:
            data = notification.get('data', {})
            signed_txn_info = data.get('signedTransactionInfo')
            if not signed_txn_info:
                logger.warning('No signedTransactionInfo in CONSUMPTION_REQUEST')
                return

            txn_info = apple_service._verify_and_decode_jws(signed_txn_info)
            if not txn_info:
                logger.warning('Failed to verify CONSUMPTION_REQUEST transaction info')
                return

            apple_txn_id = str(txn_info.get('transactionId', ''))
            environment = txn_info.get('environment', settings.APPLE_IAP_ENVIRONMENT)

            from app.database.crud.apple_iap import get_apple_transaction_by_transaction_id
            from app.database.database import AsyncSessionLocal

            async with AsyncSessionLocal() as db:
                apple_txn = await get_apple_transaction_by_transaction_id(db, apple_txn_id)

            # Determine if balance was consumed (spent on subscriptions)
            # consumptionStatus: 0 = undeclared, 1 = not consumed, 2 = partially consumed, 3 = fully consumed
            consumption_status = 0
            if apple_txn and apple_txn.status == 'verified':
                consumption_status = 3  # Balance was credited and likely spent

            # customerConsented must be false — we cannot prompt the user
            # in a server-to-server webhook.  Apple accepts the response
            # regardless, but the consumption data weight may be lower.
            await apple_service.send_consumption_info(
                transaction_id=apple_txn_id,
                customer_consented=False,
                consumption_status=consumption_status,
                delivery_status=0,  # 0 = delivered
                platform=1,  # 1 = Apple
                environment=environment,
            )

            logger.info('Apple CONSUMPTION_REQUEST handled', transaction_id=apple_txn_id)

        except Exception as e:
            logger.error('Error handling Apple CONSUMPTION_REQUEST', error=e, exc_info=True)
