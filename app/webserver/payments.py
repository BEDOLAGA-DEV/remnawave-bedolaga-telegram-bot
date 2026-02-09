from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from collections.abc import Iterable

from aiogram import Bot
from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse

from app.config import settings
from app.database.database import get_db
from app.external import yookassa_webhook as yookassa_webhook_module
from app.external.heleket_webhook import HeleketWebhookHandler
from app.external.pal24_client import Pal24APIError
from app.external.tribute import TributeService as TributeAPI
from app.external.wata_webhook import WataWebhookHandler
from app.services.pal24_service import Pal24Service
from app.services.payment_service import PaymentService
from app.services.tribute_service import TributeService


logger = logging.getLogger(__name__)


def _create_cors_response() -> Response:
    return Response(
        status_code=status.HTTP_200_OK,
        headers={
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type, trbt-signature, Crypto-Pay-API-Signature, X-MulenPay-Signature, Authorization',
        },
    )


def _extract_header(request: Request, header_names: Iterable[str]) -> str | None:
    for header_name in header_names:
        value = request.headers.get(header_name)
        if value:
            return value.strip()
    return None


def _verify_mulenpay_signature(request: Request, raw_body: bytes) -> bool:
    secret_key = settings.MULENPAY_SECRET_KEY
    display_name = settings.get_mulenpay_display_name()

    if not secret_key:
        logger.warning('%s secret key is not configured', display_name)
        return False

    signature = _extract_header(
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

        hmac_digest = hmac.new(secret_key.encode('utf-8'), raw_body, hashlib.sha256).digest()
        expected_hex = hmac_digest.hex()
        expected_base64 = base64.b64encode(hmac_digest).decode('utf-8').strip()
        expected_urlsafe = base64.urlsafe_b64encode(hmac_digest).decode('utf-8').strip()

        normalized_lower = normalized_signature.lower()
        if hmac.compare_digest(normalized_lower, expected_hex.lower()):
            return True

        normalized_no_padding = normalized_signature.rstrip('=')
        if hmac.compare_digest(normalized_no_padding, expected_base64.rstrip('=')):
            return True
        if hmac.compare_digest(normalized_no_padding, expected_urlsafe.rstrip('=')):
            return True

        logger.warning('Неверная подпись %s webhook', display_name)
        return False

    authorization_header = request.headers.get('Authorization')
    if authorization_header:
        scheme, _, value = authorization_header.partition(' ')
        scheme_lower = scheme.lower()
        token = value.strip() if value else scheme.strip()

        if scheme_lower in {'bearer', 'token'}:
            if hmac.compare_digest(token, secret_key):
                return True
            logger.warning('Неверный %s токен %s webhook', scheme, display_name)
            return False

        if not value and hmac.compare_digest(token, secret_key):
            return True

    fallback_token = _extract_header(
        request,
        (
            'X-MulenPay-Token',
            'X-Mulenpay-Token',
            'X-Webhook-Token',
        ),
    )
    if fallback_token and hmac.compare_digest(fallback_token, secret_key):
        return True

    logger.warning('Отсутствует подпись %s webhook', display_name)
    return False


async def _process_payment_service_callback(
    payment_service: PaymentService,
    payload: dict,
    method_name: str,
) -> bool:
    db_generator = get_db()
    try:
        db = await db_generator.__anext__()
    except StopAsyncIteration:  # pragma: no cover - defensive guard
        return False

    try:
        process_callback = getattr(payment_service, method_name)
        return await process_callback(db, payload)
    finally:
        try:
            await db_generator.__anext__()
        except StopAsyncIteration:
            pass


async def _parse_pal24_payload(request: Request) -> dict[str, str]:
    try:
        if request.headers.get('content-type', '').startswith('application/json'):
            data = await request.json()
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
    except json.JSONDecodeError:
        logger.debug('Pal24 webhook JSON payload не удалось распарсить')

    form = await request.form()
    if form:
        return {str(k): str(v) for k, v in form.multi_items()}

    raw_body = (await request.body()).decode('utf-8')
    if raw_body:
        try:
            data = json.loads(raw_body)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except json.JSONDecodeError:
            logger.debug('Pal24 webhook body не удалось распарсить как JSON: %s', raw_body)

    return {}


def create_payment_router(bot: Bot, payment_service: PaymentService) -> APIRouter | None:
    router = APIRouter()
    routes_registered = False

    if settings.TRIBUTE_ENABLED:
        tribute_service = TributeService(bot)
        tribute_api = TributeAPI()

        @router.options(settings.TRIBUTE_WEBHOOK_PATH)
        async def tribute_options() -> Response:
            return _create_cors_response()

        @router.post(settings.TRIBUTE_WEBHOOK_PATH)
        async def tribute_webhook(request: Request) -> JSONResponse:
            raw_body = await request.body()
            if not raw_body:
                return JSONResponse(
                    {'status': 'error', 'reason': 'empty_body'}, status_code=status.HTTP_400_BAD_REQUEST
                )

            payload = raw_body.decode('utf-8')

            signature = request.headers.get('trbt-signature')
            if not signature:
                return JSONResponse(
                    {'status': 'error', 'reason': 'missing_signature'},
                    status_code=status.HTTP_401_UNAUTHORIZED,
                )

            if settings.TRIBUTE_API_KEY and not tribute_api.verify_webhook_signature(payload, signature):
                return JSONResponse(
                    {'status': 'error', 'reason': 'invalid_signature'},
                    status_code=status.HTTP_401_UNAUTHORIZED,
                )

            try:
                json.loads(payload)
            except json.JSONDecodeError:
                return JSONResponse(
                    {'status': 'error', 'reason': 'invalid_json'},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            try:
                result = await tribute_service.process_webhook(payload)
                if result:
                    return JSONResponse({'status': 'ok', 'result': result})

                error = ValueError('Tribute webhook processing returned empty result')
                logger.error('Tribute webhook processing failed: %s', error)
                return JSONResponse(
                    {'status': 'error', 'reason': 'processing_failed'},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            except Exception as e:
                logger.exception('Tribute webhook processing error: %s', e)
                return JSONResponse(
                    {'status': 'error', 'reason': 'processing_failed'},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

        routes_registered = True

    if settings.is_mulenpay_enabled():

        @router.options(settings.MULENPAY_WEBHOOK_PATH)
        async def mulenpay_options() -> Response:
            return _create_cors_response()

        @router.post(settings.MULENPAY_WEBHOOK_PATH)
        async def mulenpay_webhook(request: Request) -> JSONResponse:
            raw_body = await request.body()
            if not raw_body:
                return JSONResponse(
                    {'status': 'error', 'reason': 'empty_body'}, status_code=status.HTTP_400_BAD_REQUEST
                )

            if not _verify_mulenpay_signature(request, raw_body):
                return JSONResponse(
                    {'status': 'error', 'reason': 'invalid_signature'},
                    status_code=status.HTTP_401_UNAUTHORIZED,
                )

            try:
                payload = json.loads(raw_body.decode('utf-8'))
            except json.JSONDecodeError:
                return JSONResponse(
                    {'status': 'error', 'reason': 'invalid_json'},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            try:
                success = await _process_payment_service_callback(
                    payment_service,
                    payload,
                    'process_mulenpay_callback',
                )
                if success:
                    return JSONResponse({'status': 'ok'})

                logger.error('MulenPay webhook processing failed: %s', payload)
                return JSONResponse(
                    {'status': 'error', 'reason': 'processing_failed'},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            except Exception as e:
                logger.exception('MulenPay webhook processing error: %s', e)
                return JSONResponse(
                    {'status': 'error', 'reason': 'processing_failed'},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

        routes_registered = True

    if settings.is_cryptobot_enabled():

        @router.options(settings.CRYPTOBOT_WEBHOOK_PATH)
        async def cryptobot_options() -> Response:
            return _create_cors_response()

        @router.post(settings.CRYPTOBOT_WEBHOOK_PATH)
        async def cryptobot_webhook(request: Request) -> JSONResponse:
            raw_body = await request.body()
            if not raw_body:
                return JSONResponse(
                    {'status': 'error', 'reason': 'empty_body'}, status_code=status.HTTP_400_BAD_REQUEST
                )

            payload_text = raw_body.decode('utf-8')
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError:
                return JSONResponse(
                    {'status': 'error', 'reason': 'invalid_json'},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            signature = request.headers.get('Crypto-Pay-API-Signature')
            secret = settings.CRYPTOBOT_WEBHOOK_SECRET
            if secret:
                if not signature:
                    return JSONResponse(
                        {'status': 'error', 'reason': 'missing_signature'},
                        status_code=status.HTTP_401_UNAUTHORIZED,
                    )

                from app.external.cryptobot import CryptoBotService

                if not CryptoBotService().verify_webhook_signature(payload_text, signature):
                    return JSONResponse(
                        {'status': 'error', 'reason': 'invalid_signature'},
                        status_code=status.HTTP_401_UNAUTHORIZED,
                    )

            try:
                success = await _process_payment_service_callback(
                    payment_service,
                    payload,
                    'process_cryptobot_webhook',
                )
                if success:
                    return JSONResponse({'status': 'ok'})

                logger.error(
                    'CryptoBot webhook processing failed: invoice_id=%s',
                    payload.get('payload', {}).get('invoice_id'),
                )
                return JSONResponse(
                    {'status': 'error', 'reason': 'processing_failed'},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            except Exception as e:
                logger.exception('CryptoBot webhook processing error: %s', e)
                return JSONResponse(
                    {'status': 'error', 'reason': 'processing_failed'},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

        routes_registered = True

    if settings.is_yookassa_enabled():

        @router.options(settings.YOOKASSA_WEBHOOK_PATH)
        async def yookassa_options() -> Response:
            return Response(
                status_code=status.HTTP_200_OK,
                headers={
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
                    'Access-Control-Allow-Headers': 'Content-Type, X-YooKassa-Signature, Signature',
                },
            )

        @router.get(settings.YOOKASSA_WEBHOOK_PATH)
        async def yookassa_health() -> JSONResponse:
            return JSONResponse(
                {
                    'status': 'ok',
                    'service': 'yookassa_webhook',
                    'enabled': settings.is_yookassa_enabled(),
                }
            )

        @router.post(settings.YOOKASSA_WEBHOOK_PATH)
        async def yookassa_webhook(request: Request) -> JSONResponse:
            header_ip_candidates = yookassa_webhook_module.collect_yookassa_ip_candidates(
                request.headers.get('X-Forwarded-For'),
                request.headers.get('X-Real-IP'),
                request.headers.get('Cf-Connecting-Ip'),
            )
            remote_ip = request.client.host if request.client else None
            client_ip = yookassa_webhook_module.resolve_yookassa_ip(
                header_ip_candidates,
                remote=remote_ip,
            )

            if client_ip is None:
                return JSONResponse(
                    {
                        'status': 'error',
                        'reason': 'unknown_ip',
                        'candidates': header_ip_candidates + ([remote_ip] if remote_ip else []),
                    },
                    status_code=status.HTTP_403_FORBIDDEN,
                )

            if not yookassa_webhook_module.is_yookassa_ip_allowed(client_ip):
                return JSONResponse(
                    {
                        'status': 'error',
                        'reason': 'forbidden_ip',
                        'ip': str(client_ip),
                    },
                    status_code=status.HTTP_403_FORBIDDEN,
                )

            body_bytes = await request.body()
            if not body_bytes:
                return JSONResponse(
                    {'status': 'error', 'reason': 'empty_body'}, status_code=status.HTTP_400_BAD_REQUEST
                )

            body = body_bytes.decode('utf-8')

            signature = request.headers.get('Signature') or request.headers.get('X-YooKassa-Signature')
            if signature:
                logger.info('ℹ️ Получена подпись YooKassa: %s', signature)

            try:
                webhook_data = json.loads(body)
            except json.JSONDecodeError:
                return JSONResponse(
                    {'status': 'error', 'reason': 'invalid_json'},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            event_type = webhook_data.get('event')
            if not event_type:
                return JSONResponse(
                    {'status': 'error', 'reason': 'missing_event'},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            if event_type not in {
                'payment.succeeded',
                'payment.waiting_for_capture',
                'payment.canceled',
            }:
                return JSONResponse({'status': 'ok', 'ignored': event_type})

            try:
                success = await _process_payment_service_callback(
                    payment_service,
                    webhook_data,
                    'process_yookassa_webhook',
                )
                if success:
                    return JSONResponse({'status': 'ok'})

                payment_id = webhook_data.get('object', {}).get('id', 'unknown')
                logger.error('YooKassa webhook processing failed: payment_id=%s', payment_id)
                return JSONResponse(
                    {'status': 'error', 'reason': 'processing_failed'},
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
            except Exception as e:
                logger.exception('YooKassa webhook processing error: %s', e)
                return JSONResponse(
                    {'status': 'error', 'reason': 'processing_failed'},
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

        routes_registered = True

    if settings.is_wata_enabled():
        wata_handler = WataWebhookHandler(payment_service)

        @router.options(settings.WATA_WEBHOOK_PATH)
        async def wata_options() -> Response:
            return Response(
                status_code=status.HTTP_200_OK,
                headers={
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
                    'Access-Control-Allow-Headers': 'Content-Type, X-Signature',
                },
            )

        @router.get(settings.WATA_WEBHOOK_PATH)
        async def wata_health() -> JSONResponse:
            return JSONResponse(
                {
                    'status': 'ok',
                    'service': 'wata_webhook',
                    'enabled': settings.is_wata_enabled(),
                }
            )

        @router.post(settings.WATA_WEBHOOK_PATH)
        async def wata_webhook(request: Request) -> JSONResponse:
            raw_body = await request.body()
            if not raw_body:
                return JSONResponse(
                    {'status': 'error', 'reason': 'empty_body'}, status_code=status.HTTP_400_BAD_REQUEST
                )

            signature = request.headers.get('X-Signature') or ''
            if not await wata_handler._verify_signature(raw_body.decode('utf-8'), signature):  # type: ignore[attr-defined]
                return JSONResponse(
                    {'status': 'error', 'reason': 'invalid_signature'},
                    status_code=status.HTTP_401_UNAUTHORIZED,
                )

            try:
                payload = json.loads(raw_body.decode('utf-8'))
            except json.JSONDecodeError:
                return JSONResponse(
                    {'status': 'error', 'reason': 'invalid_json'},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            try:
                success = await _process_payment_service_callback(
                    payment_service,
                    payload,
                    'process_wata_webhook',
                )
                if success:
                    return JSONResponse({'status': 'ok'})

                order_id = payload.get('orderId') or payload.get('order_id') or 'unknown'
                logger.error('Wata webhook processing failed: order_id=%s, payload=%s', order_id, payload)
                return JSONResponse(
                    {'status': 'error', 'reason': 'not_processed'},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            except Exception as e:
                logger.exception('Wata webhook processing error: %s', e)
                return JSONResponse(
                    {'status': 'error', 'reason': 'not_processed'},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

        routes_registered = True

    if settings.is_heleket_enabled():
        heleket_handler = HeleketWebhookHandler(payment_service)

        @router.options(settings.HELEKET_WEBHOOK_PATH)
        async def heleket_options() -> Response:
            return Response(
                status_code=status.HTTP_200_OK,
                headers={
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
                    'Access-Control-Allow-Headers': 'Content-Type, Authorization',
                },
            )

        @router.get(settings.HELEKET_WEBHOOK_PATH)
        async def heleket_health() -> JSONResponse:
            return JSONResponse(
                {
                    'status': 'ok',
                    'service': 'heleket_webhook',
                    'enabled': settings.is_heleket_enabled(),
                }
            )

        @router.post(settings.HELEKET_WEBHOOK_PATH)
        async def heleket_webhook(request: Request) -> JSONResponse:
            try:
                payload = await request.json()
            except json.JSONDecodeError:
                return JSONResponse(
                    {'status': 'error', 'reason': 'invalid_json'},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            if not heleket_handler.service.verify_webhook_signature(payload):
                return JSONResponse(
                    {'status': 'error', 'reason': 'invalid_signature'},
                    status_code=status.HTTP_401_UNAUTHORIZED,
                )

            try:
                success = await _process_payment_service_callback(
                    payment_service,
                    payload,
                    'process_heleket_webhook',
                )
                if success:
                    return JSONResponse({'status': 'ok'})

                uuid_val = payload.get('uuid', 'unknown')
                logger.error('Heleket webhook processing failed: uuid=%s', uuid_val)
                return JSONResponse(
                    {'status': 'error', 'reason': 'not_processed'},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            except Exception as e:
                logger.exception('Heleket webhook processing error: %s', e)
                return JSONResponse(
                    {'status': 'error', 'reason': 'not_processed'},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

        routes_registered = True

    if settings.is_pal24_enabled():
        pal24_service = Pal24Service()

        @router.options(settings.PAL24_WEBHOOK_PATH)
        async def pal24_options() -> Response:
            return Response(
                status_code=status.HTTP_200_OK,
                headers={
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
                    'Access-Control-Allow-Headers': 'Content-Type',
                },
            )

        @router.get(settings.PAL24_WEBHOOK_PATH)
        async def pal24_health() -> JSONResponse:
            return JSONResponse(
                {
                    'status': 'ok',
                    'service': 'pal24_webhook',
                    'enabled': settings.is_pal24_enabled(),
                }
            )

        @router.post(settings.PAL24_WEBHOOK_PATH)
        async def pal24_webhook(request: Request) -> JSONResponse:
            if not pal24_service.is_configured:
                return JSONResponse(
                    {'status': 'error', 'reason': 'service_not_configured'},
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                )

            payload = await _parse_pal24_payload(request)
            if not payload:
                return JSONResponse(
                    {'status': 'error', 'reason': 'empty_payload'},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            try:
                parsed_payload = pal24_service.parse_callback(payload)
            except Pal24APIError as error:
                return JSONResponse(
                    {'status': 'error', 'reason': str(error)},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            try:
                success = await _process_payment_service_callback(
                    payment_service,
                    parsed_payload,
                    'process_pal24_callback',
                )
                if success:
                    return JSONResponse({'status': 'ok'})

                bill_id = parsed_payload.get('bill_id', 'unknown')
                logger.error('Pal24 webhook processing failed: bill_id=%s', bill_id)
                return JSONResponse(
                    {'status': 'error', 'reason': 'not_processed'},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            except Exception as e:
                logger.exception('Pal24 webhook processing error: %s', e)
                return JSONResponse(
                    {'status': 'error', 'reason': 'not_processed'},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

        routes_registered = True

    if settings.is_platega_enabled():

        @router.get(settings.PLATEGA_WEBHOOK_PATH)
        async def platega_health() -> JSONResponse:
            return JSONResponse(
                {
                    'status': 'ok',
                    'service': 'platega_webhook',
                    'enabled': settings.is_platega_enabled(),
                }
            )

        @router.post(settings.PLATEGA_WEBHOOK_PATH)
        async def platega_webhook(request: Request) -> JSONResponse:
            merchant_id = request.headers.get('X-MerchantId', '')
            secret = request.headers.get('X-Secret', '')
            if merchant_id != (settings.PLATEGA_MERCHANT_ID or '') or secret != (settings.PLATEGA_SECRET or ''):
                return JSONResponse(
                    {'status': 'error', 'reason': 'unauthorized'},
                    status_code=status.HTTP_401_UNAUTHORIZED,
                )

            try:
                payload = await request.json()
            except json.JSONDecodeError:
                return JSONResponse(
                    {'status': 'error', 'reason': 'invalid_json'},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            try:
                success = await _process_payment_service_callback(
                    payment_service,
                    payload,
                    'process_platega_webhook',
                )
                if success:
                    return JSONResponse({'status': 'ok'})

                transaction_id = payload.get('transactionId', 'unknown')
                logger.error('Platega webhook processing failed: transactionId=%s', transaction_id)
                return JSONResponse(
                    {'status': 'error', 'reason': 'not_processed'},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            except Exception as e:
                logger.exception('Platega webhook processing error: %s', e)
                return JSONResponse(
                    {'status': 'error', 'reason': 'not_processed'},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

        routes_registered = True

    if settings.is_cloudpayments_enabled():
        from app.services.cloudpayments_service import CloudPaymentsService

        cloudpayments_service = CloudPaymentsService()

        @router.options(settings.CLOUDPAYMENTS_WEBHOOK_PATH)
        async def cloudpayments_options() -> Response:
            return Response(
                status_code=status.HTTP_200_OK,
                headers={
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
                    'Access-Control-Allow-Headers': 'Content-Type, X-Content-HMAC',
                },
            )

        @router.get(settings.CLOUDPAYMENTS_WEBHOOK_PATH)
        async def cloudpayments_health() -> JSONResponse:
            return JSONResponse(
                {
                    'status': 'ok',
                    'service': 'cloudpayments_webhook',
                    'enabled': settings.is_cloudpayments_enabled(),
                }
            )

        # CloudPayments Check webhook (перед списанием)
        @router.post(settings.CLOUDPAYMENTS_WEBHOOK_PATH + '/check')
        async def cloudpayments_check_webhook(request: Request) -> JSONResponse:
            """Check webhook - вызывается перед списанием, можно отклонить платёж."""
            try:
                raw_body = await request.body()

                # Логируем для диагностики
                logger.info(
                    'CloudPayments check webhook received, body_len=%d, all_headers=%s',
                    len(raw_body),
                    dict(request.headers),
                )

                # Проверяем подпись только если она пришла и API_SECRET настроен
                # CloudPayments использует заголовок X-Content-HMAC или Content-HMAC
                signature = request.headers.get('X-Content-HMAC') or request.headers.get('Content-HMAC') or ''
                if settings.CLOUDPAYMENTS_API_SECRET and signature:
                    if not cloudpayments_service.verify_webhook_signature(
                        raw_body, signature, settings.CLOUDPAYMENTS_API_SECRET
                    ):
                        logger.warning(
                            'CloudPayments check webhook: invalid signature, sig=%s...',
                            signature[:20] if signature else 'empty',
                        )
                        return JSONResponse({'code': 13})  # Отклонить
                elif settings.CLOUDPAYMENTS_API_SECRET and not signature:
                    # Подпись не пришла, но API_SECRET настроен - пропускаем проверку с предупреждением
                    logger.warning('CloudPayments check webhook: no signature header, skipping verification')

                # Разрешаем платёж
                logger.info('CloudPayments check webhook: allowing payment, returning code=0')
                return JSONResponse({'code': 0})
            except Exception as e:
                logger.exception('CloudPayments check webhook error: %s', e)
                # В случае ошибки всё равно разрешаем платёж
                return JSONResponse({'code': 0})

        # CloudPayments Pay webhook (успешная оплата)
        @router.post(settings.CLOUDPAYMENTS_WEBHOOK_PATH + '/pay')
        async def cloudpayments_pay_webhook(request: Request) -> JSONResponse:
            """Pay webhook - вызывается после успешной оплаты."""
            raw_body = await request.body()

            # Проверяем подпись только если она пришла и API_SECRET настроен
            signature = request.headers.get('X-Content-HMAC') or request.headers.get('Content-HMAC') or ''
            if settings.CLOUDPAYMENTS_API_SECRET and signature:
                if not cloudpayments_service.verify_webhook_signature(
                    raw_body, signature, settings.CLOUDPAYMENTS_API_SECRET
                ):
                    logger.warning('CloudPayments pay webhook: invalid signature')
                    return JSONResponse({'code': 13})

            # Парсим данные формы
            try:
                form_data = await request.form()
                webhook_data = cloudpayments_service.parse_webhook_data(dict(form_data))
            except Exception as error:
                logger.error('CloudPayments pay webhook parse error: %s', error)
                return JSONResponse({'code': 0})  # Возвращаем 0, чтобы не было повторов

            # Обрабатываем платёж
            await _process_payment_service_callback(
                payment_service,
                webhook_data,
                'process_cloudpayments_pay_webhook',
            )

            return JSONResponse({'code': 0})

        # CloudPayments Fail webhook (неуспешная оплата)
        @router.post(settings.CLOUDPAYMENTS_WEBHOOK_PATH + '/fail')
        async def cloudpayments_fail_webhook(request: Request) -> JSONResponse:
            """Fail webhook - вызывается при неуспешной оплате."""
            raw_body = await request.body()

            # Проверяем подпись только если она пришла и API_SECRET настроен
            signature = request.headers.get('X-Content-HMAC') or request.headers.get('Content-HMAC') or ''
            if settings.CLOUDPAYMENTS_API_SECRET and signature:
                if not cloudpayments_service.verify_webhook_signature(
                    raw_body, signature, settings.CLOUDPAYMENTS_API_SECRET
                ):
                    logger.warning('CloudPayments fail webhook: invalid signature')
                    return JSONResponse({'code': 13})

            # Парсим данные формы
            try:
                form_data = await request.form()
                webhook_data = cloudpayments_service.parse_webhook_data(dict(form_data))
            except Exception as error:
                logger.error('CloudPayments fail webhook parse error: %s', error)
                return JSONResponse({'code': 0})

            # Обрабатываем неуспешный платёж
            await _process_payment_service_callback(
                payment_service,
                webhook_data,
                'process_cloudpayments_fail_webhook',
            )

            return JSONResponse({'code': 0})

        # Универсальный endpoint для всех webhooks
        @router.post(settings.CLOUDPAYMENTS_WEBHOOK_PATH)
        async def cloudpayments_webhook(request: Request) -> JSONResponse:
            """Универсальный webhook endpoint."""
            try:
                raw_body = await request.body()

                # Логируем для диагностики
                logger.info(
                    'CloudPayments universal webhook received, body_len=%d, headers=%s',
                    len(raw_body),
                    dict(request.headers),
                )

                # Проверяем подпись только если она пришла и API_SECRET настроен
                signature = request.headers.get('X-Content-HMAC') or request.headers.get('Content-HMAC') or ''
                if settings.CLOUDPAYMENTS_API_SECRET and signature:
                    if not cloudpayments_service.verify_webhook_signature(
                        raw_body, signature, settings.CLOUDPAYMENTS_API_SECRET
                    ):
                        logger.warning('CloudPayments webhook: invalid signature')
                        return JSONResponse({'code': 13})

                # Парсим данные формы
                try:
                    form_data = await request.form()
                    webhook_data = cloudpayments_service.parse_webhook_data(dict(form_data))
                    logger.info('CloudPayments webhook parsed data: %s', webhook_data)
                except Exception as error:
                    logger.error('CloudPayments webhook parse error: %s', error)
                    # Может быть это Check уведомление - просто разрешаем
                    return JSONResponse({'code': 0})

                # Определяем тип webhook по статусу и наличию полей
                status_value = webhook_data.get('status', '')
                reason = webhook_data.get('reason')
                auth_code = webhook_data.get('auth_code')

                # Pay notification имеет Reason="Approved" или AuthCode
                # Check notification НЕ имеет этих полей
                is_pay_notification = bool(reason or auth_code)

                if status_value in ('Declined', 'Cancelled'):
                    # Неуспешная оплата (Fail notification)
                    await _process_payment_service_callback(
                        payment_service,
                        webhook_data,
                        'process_cloudpayments_fail_webhook',
                    )
                elif status_value in ('Completed', 'Authorized') and is_pay_notification:
                    # Успешная оплата (Pay notification) - есть Reason или AuthCode
                    logger.info(
                        'CloudPayments Pay notification: invoice=%s, reason=%s, auth_code=%s',
                        webhook_data.get('invoice_id'),
                        reason,
                        auth_code,
                    )
                    await _process_payment_service_callback(
                        payment_service,
                        webhook_data,
                        'process_cloudpayments_pay_webhook',
                    )
                else:
                    # Check notification или другой тип - просто разрешаем (code=0)
                    # Check приходит ДО оплаты для валидации, не зачисляем баланс
                    logger.info(
                        'CloudPayments Check/other notification: status=%s, reason=%s, '
                        'auth_code=%s - allowing (code=0), NOT crediting balance',
                        status_value,
                        reason,
                        auth_code,
                    )

                return JSONResponse({'code': 0})
            except Exception as e:
                logger.exception('CloudPayments universal webhook error: %s', e)
                return JSONResponse({'code': 0})

        routes_registered = True

    if settings.is_freekassa_enabled():

        @router.options(settings.FREEKASSA_WEBHOOK_PATH)
        async def freekassa_options() -> Response:
            return Response(
                status_code=status.HTTP_200_OK,
                headers={
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
                    'Access-Control-Allow-Headers': 'Content-Type',
                },
            )

        @router.get(settings.FREEKASSA_WEBHOOK_PATH)
        async def freekassa_health() -> JSONResponse:
            return JSONResponse(
                {
                    'status': 'ok',
                    'service': 'freekassa_webhook',
                    'enabled': settings.is_freekassa_enabled(),
                }
            )

        @router.post(settings.FREEKASSA_WEBHOOK_PATH)
        async def freekassa_webhook(request: Request) -> Response:
            # Получаем IP клиента с учетом прокси
            x_forwarded_for = request.headers.get('X-Forwarded-For')
            if x_forwarded_for:
                client_ip = x_forwarded_for.split(',')[0].strip()
            else:
                real_ip = request.headers.get('X-Real-IP')
                if real_ip:
                    client_ip = real_ip.strip()
                else:
                    client_ip = request.client.host if request.client else '127.0.0.1'

            # Получаем данные формы
            try:
                form_data = await request.form()
            except Exception as form_error:
                logger.error('Freekassa webhook: не удалось прочитать данные формы: %s', form_error)
                return Response('Error reading form data', status_code=status.HTTP_400_BAD_REQUEST)

            # Извлекаем параметры
            merchant_id = form_data.get('MERCHANT_ID')
            amount = form_data.get('AMOUNT')
            order_id = form_data.get('MERCHANT_ORDER_ID')
            sign = form_data.get('SIGN')
            intid = form_data.get('intid')
            cur_id = form_data.get('CUR_ID')

            if not all([merchant_id, amount, order_id, sign, intid]):
                logger.warning('Freekassa webhook: отсутствуют обязательные параметры')
                return Response('Missing parameters', status_code=status.HTTP_400_BAD_REQUEST)

            # Преобразуем типы
            try:
                merchant_id_int = int(merchant_id)
                amount_float = float(amount)
                cur_id_int = int(cur_id) if cur_id else None
            except ValueError:
                logger.warning('Freekassa webhook: неверный формат параметров')
                return Response('Invalid parameters format', status_code=status.HTTP_400_BAD_REQUEST)

            # Обрабатываем callback
            db_generator = get_db()
            try:
                db = await db_generator.__anext__()
            except StopAsyncIteration:
                return Response('DB Error', status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

            try:
                success = await payment_service.process_freekassa_webhook(
                    db,
                    merchant_id=merchant_id_int,
                    amount=amount_float,
                    order_id=order_id,
                    sign=sign,
                    intid=intid,
                    cur_id=cur_id_int,
                    client_ip=client_ip,
                )
                if success:
                    return Response('YES', status_code=status.HTTP_200_OK)

                logger.error('Freekassa webhook processing failed: order_id=%s, intid=%s', order_id, intid)
                return Response('Error', status_code=status.HTTP_400_BAD_REQUEST)
            except Exception as e:
                logger.exception('Freekassa webhook processing error: %s', e)
                return Response('Error', status_code=status.HTTP_400_BAD_REQUEST)
            finally:
                try:
                    await db_generator.__anext__()
                except StopAsyncIteration:
                    pass

        routes_registered = True

    # KassaAI webhook
    if settings.is_kassa_ai_enabled():

        @router.get(settings.KASSA_AI_WEBHOOK_PATH)
        async def kassa_ai_health() -> JSONResponse:
            return JSONResponse(
                {
                    'status': 'ok',
                    'service': 'kassa_ai_webhook',
                    'enabled': settings.is_kassa_ai_enabled(),
                }
            )

        @router.post(settings.KASSA_AI_WEBHOOK_PATH)
        async def kassa_ai_webhook(request: Request) -> Response:
            # Получаем данные формы
            try:
                form_data = await request.form()
            except Exception as form_error:
                logger.error('KassaAI webhook: не удалось прочитать данные формы: %s', form_error)
                return Response('Error reading form data', status_code=status.HTTP_400_BAD_REQUEST)

            # Извлекаем параметры (те же что и у Freekassa)
            merchant_id = form_data.get('MERCHANT_ID')
            amount = form_data.get('AMOUNT')
            order_id = form_data.get('MERCHANT_ORDER_ID')
            sign = form_data.get('SIGN')
            intid = form_data.get('intid')
            cur_id = form_data.get('CUR_ID')

            if not all([merchant_id, amount, order_id, sign, intid]):
                logger.warning('KassaAI webhook: отсутствуют обязательные параметры')
                return Response('Missing parameters', status_code=status.HTTP_400_BAD_REQUEST)

            try:
                merchant_id_int = int(merchant_id)
                amount_float = float(amount)
                cur_id_int = int(cur_id) if cur_id else None
            except (ValueError, TypeError) as e:
                logger.error('KassaAI webhook: некорректные параметры - %s', e)
                return Response('Invalid parameters', status_code=status.HTTP_400_BAD_REQUEST)

            # Обрабатываем webhook
            db_generator = get_db()
            try:
                db = await db_generator.__anext__()
            except StopAsyncIteration:
                return Response('DB Error', status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

            try:
                success = await payment_service.process_kassa_ai_webhook(
                    db,
                    merchant_id=merchant_id_int,
                    amount=amount_float,
                    order_id=order_id,
                    sign=sign,
                    intid=intid,
                    cur_id=cur_id_int,
                )
                if success:
                    return Response('YES', status_code=status.HTTP_200_OK)

                logger.error('KassaAI webhook processing failed: order_id=%s, intid=%s', order_id, intid)
                return Response('Error', status_code=status.HTTP_400_BAD_REQUEST)
            except Exception as e:
                logger.exception('KassaAI webhook processing error: %s', e)
                return Response('Error', status_code=status.HTTP_400_BAD_REQUEST)
            finally:
                try:
                    await db_generator.__anext__()
                except StopAsyncIteration:
                    pass

        routes_registered = True

    if routes_registered:

        @router.get('/health/payment-webhooks')
        async def payment_webhooks_health() -> JSONResponse:
            return JSONResponse(
                {
                    'status': 'ok',
                    'tribute_enabled': settings.TRIBUTE_ENABLED,
                    'mulenpay_enabled': settings.is_mulenpay_enabled(),
                    'cryptobot_enabled': settings.is_cryptobot_enabled(),
                    'yookassa_enabled': settings.is_yookassa_enabled(),
                    'wata_enabled': settings.is_wata_enabled(),
                    'heleket_enabled': settings.is_heleket_enabled(),
                    'pal24_enabled': settings.is_pal24_enabled(),
                    'platega_enabled': settings.is_platega_enabled(),
                    'cloudpayments_enabled': settings.is_cloudpayments_enabled(),
                    'freekassa_enabled': settings.is_freekassa_enabled(),
                    'kassa_ai_enabled': settings.is_kassa_ai_enabled(),
                }
            )

    return router if routes_registered else None
