from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from ipaddress import (
    IPv4Address,
    IPv4Network,
    IPv6Address,
    IPv6Network,
    ip_address,
    ip_network,
)
from typing import TYPE_CHECKING, Union

import structlog
from aiohttp import web

from app.config import settings
from app.database.database import AsyncSessionLocal


if TYPE_CHECKING:
    from app.services.payment_service import PaymentService

logger = structlog.get_logger(__name__)


IPAddress = Union[IPv4Address, IPv6Address]
IPNetwork = Union[IPv4Network, IPv6Network]

YOOKASSA_ALLOWED_IP_NETWORKS: tuple[IPNetwork, ...] = (
    ip_network('185.71.76.0/27'),
    ip_network('185.71.77.0/27'),
    ip_network('77.75.153.0/25'),
    ip_network('77.75.154.128/25'),
    ip_network('77.75.156.11/32'),
    ip_network('77.75.156.35/32'),
    ip_network('2a02:5180::/32'),
)


CLOUDFLARE_TRUSTED_NETWORKS: tuple[IPNetwork, ...] = (
    ip_network('173.245.48.0/20'),
    ip_network('103.21.244.0/22'),
    ip_network('103.22.200.0/22'),
    ip_network('103.31.4.0/22'),
    ip_network('141.101.64.0/18'),
    ip_network('108.162.192.0/18'),
    ip_network('190.93.240.0/20'),
    ip_network('188.114.96.0/20'),
    ip_network('197.234.240.0/22'),
    ip_network('198.41.128.0/17'),
    ip_network('162.158.0.0/15'),
    ip_network('104.16.0.0/13'),
    ip_network('104.24.0.0/14'),
    ip_network('172.64.0.0/13'),
    ip_network('131.0.72.0/22'),
    ip_network('2400:cb00::/32'),
    ip_network('2606:4700::/32'),
    ip_network('2803:f800::/32'),
    ip_network('2405:b500::/32'),
    ip_network('2405:8100::/32'),
    ip_network('2a06:98c0::/29'),
    ip_network('2c0f:f248::/32'),
)


YOOKASSA_ALLOWED_EVENTS: tuple[str, ...] = (
    'payment.succeeded',
    'payment.waiting_for_capture',
    'payment.canceled',
)


def collect_yookassa_ip_candidates(*values: str | None) -> list[str]:
    candidates: list[str] = []
    for value in values:
        if not value:
            continue
        for part in value.split(','):
            normalized = part.strip()
            if normalized:
                candidates.append(normalized)
    return candidates


def _parse_candidate_ip(candidate: str) -> IPAddress | None:
    value = candidate.strip()
    if not value:
        return None

    if value.startswith('[') and ']' in value:
        value = value[1 : value.index(']')]

    if '%' in value:
        value = value.split('%', 1)[0]

    if value.count(':') == 1 and '.' in value:
        host, _, port = value.rpartition(':')
        if port.isdigit():
            value = host

    try:
        return ip_address(value)
    except ValueError:
        return None


def _should_trust_forwarded_headers(remote_ip: IPAddress | None) -> bool:
    if remote_ip is None:
        return True

    if _is_trusted_proxy_ip(remote_ip):
        return True

    return any(
        getattr(remote_ip, attribute) for attribute in ('is_private', 'is_loopback', 'is_link_local', 'is_reserved')
    )


_TRUSTED_PROXY_NETWORKS_CACHE: tuple[str, tuple[IPNetwork, ...]] = ('', ())


def _get_trusted_proxy_networks() -> tuple[IPNetwork, ...]:
    global _TRUSTED_PROXY_NETWORKS_CACHE

    raw_value = getattr(settings, 'YOOKASSA_TRUSTED_PROXY_NETWORKS', '') or ''
    cached_raw, cached_networks = _TRUSTED_PROXY_NETWORKS_CACHE

    if raw_value == cached_raw:
        return cached_networks

    networks: list[IPNetwork] = []
    for part in raw_value.split(','):
        candidate = part.strip()
        if not candidate:
            continue

        try:
            networks.append(ip_network(candidate, strict=False))
        except ValueError:
            logger.warning('Неверная сеть доверенного прокси YooKassa', candidate=candidate)

    cached_networks = tuple(networks)
    _TRUSTED_PROXY_NETWORKS_CACHE = (raw_value, cached_networks)
    return cached_networks


def _is_trusted_proxy_ip(ip_object: IPAddress) -> bool:
    if any(
        getattr(ip_object, attribute) for attribute in ('is_private', 'is_loopback', 'is_link_local', 'is_reserved')
    ):
        return True

    if any(ip_object in network for network in CLOUDFLARE_TRUSTED_NETWORKS):
        return True

    return any(ip_object in network for network in _get_trusted_proxy_networks())


def resolve_yookassa_ip(
    candidates: Iterable[str],
    *,
    remote: str | None = None,
) -> IPAddress | None:
    remote_ip = _parse_candidate_ip(remote) if remote else None

    if remote_ip is not None and remote_ip.is_global and not _is_trusted_proxy_ip(remote_ip):
        return remote_ip

    candidate_list = list(candidates)

    if _should_trust_forwarded_headers(remote_ip):
        last_hop = remote_ip
        for candidate in reversed(candidate_list):
            ip_object = _parse_candidate_ip(candidate)
            if ip_object is not None:
                if last_hop is None or _is_trusted_proxy_ip(last_hop):
                    if _is_trusted_proxy_ip(ip_object):
                        last_hop = ip_object
                        continue
                    return ip_object
                break

        if last_hop is not None and not _is_trusted_proxy_ip(last_hop):
            return last_hop

    return (
        remote_ip
        if remote_ip is not None
        else next(
            (ip for ip in (_parse_candidate_ip(value) for value in candidate_list) if ip is not None),
            None,
        )
    )


def is_yookassa_ip_allowed(ip_object: IPAddress) -> bool:
    return any(ip_object in network for network in YOOKASSA_ALLOWED_IP_NETWORKS)


class YooKassaWebhookHandler:
    def __init__(self, payment_service: PaymentService):
        self.payment_service = payment_service

    async def handle_webhook(self, request: web.Request) -> web.Response:
        try:
            logger.info('📥 Получен YooKassa webhook', method=request.method, path=request.path)

            header_ip_candidates = collect_yookassa_ip_candidates(
                request.headers.get('X-Forwarded-For'),
                request.headers.get('X-Real-IP'),
                request.headers.get('Cf-Connecting-Ip'),
            )
            client_ip = resolve_yookassa_ip(
                header_ip_candidates,
                remote=request.remote,
            )

            if client_ip is None:
                logger.warning(
                    '🚫 Не удалось определить IP-адрес отправителя YooKassa webhook. Кандидаты',
                    header_ip_candidates=header_ip_candidates + ([request.remote] if request.remote else []),
                )
                return web.Response(status=403, text='Forbidden')

            if not is_yookassa_ip_allowed(client_ip):
                logger.warning(
                    '🚫 YooKassa webhook отклонён: IP %s не входит в доверенные диапазоны (%s)',
                    client_ip,
                    ', '.join(str(network) for network in YOOKASSA_ALLOWED_IP_NETWORKS),
                )
                return web.Response(status=403, text='Forbidden')

            logger.info('🌐 IP-адрес YooKassa подтверждён', client_ip=client_ip)

            body = await request.text()

            if not body:
                logger.warning('⚠️ Получен пустой webhook от YooKassa')
                return web.Response(status=400, text='Empty body')

            logger.debug('📄 Body received', length=len(body))

            signature = request.headers.get('Signature') or request.headers.get('X-YooKassa-Signature')
            if signature:
                logger.info('ℹ️ Получена подпись YooKassa', signature=signature)

            try:
                webhook_data = json.loads(body)
            except json.JSONDecodeError as e:
                logger.error('❌ Ошибка парсинга JSON webhook YooKassa', error=e)
                return web.Response(status=400, text='Invalid JSON')

            logger.info('📊 Обработка webhook YooKassa', get=webhook_data.get('event', 'unknown_event'))
            logger.debug('🔍 Полные данные webhook', webhook_data=webhook_data)

            event_type = webhook_data.get('event')
            if not event_type:
                logger.warning('⚠️ Webhook YooKassa без типа события')
                return web.Response(status=400, text='No event type')

            # Извлекаем ID платежа из вебхука для предотвращения дублирования
            yookassa_payment_id = webhook_data.get('object', {}).get('id')
            if not yookassa_payment_id:
                logger.warning('⚠️ Webhook YooKassa без ID платежа')
                return web.Response(status=400, text='No payment id')

            if event_type not in YOOKASSA_ALLOWED_EVENTS:
                logger.info('ℹ️ Игнорируем событие YooKassa', event_type=event_type)
                return web.Response(status=200, text='OK')

            async with AsyncSessionLocal() as db:
                try:
                    # Проверяем, не обрабатывается ли этот платеж уже (защита от дублирования)
                    from app.database.crud.transaction import get_transaction_by_external_id
                    from app.database.models import PaymentMethod

                    existing_transaction = None
                    if yookassa_payment_id and hasattr(db, 'execute'):
                        existing_transaction = await get_transaction_by_external_id(
                            db, yookassa_payment_id, PaymentMethod.YOOKASSA
                        )

                    if existing_transaction and event_type == 'payment.succeeded':
                        logger.info(
                            'ℹ️ Платеж YooKassa уже был обработан. Пропускаем дублирующий вебхук.',
                            yookassa_payment_id=yookassa_payment_id,
                        )
                        return web.Response(status=200, text='OK')

                    success = await self.payment_service.process_yookassa_webhook(db, webhook_data)

                    if success:
                        await db.commit()
                        logger.info(
                            '✅ Успешно обработан webhook YooKassa: для платежа',
                            event_type=event_type,
                            yookassa_payment_id=yookassa_payment_id,
                        )
                        return web.Response(status=200, text='OK')
                    await db.rollback()
                    logger.error(
                        '❌ Ошибка обработки webhook YooKassa: для платежа',
                        event_type=event_type,
                        yookassa_payment_id=yookassa_payment_id,
                    )
                    return web.Response(status=500, text='Processing error')

                except Exception as e:
                    await db.rollback()
                    logger.error('❌ Ошибка обработки webhook YooKassa', error=e, exc_info=True)
                    return web.Response(status=500, text='Processing error')

        except Exception as e:
            logger.error('❌ Критическая ошибка обработки webhook YooKassa', error=e, exc_info=True)
            return web.Response(status=500, text='Internal server error')

    def setup_routes(self, app: web.Application) -> None:
        webhook_path = settings.YOOKASSA_WEBHOOK_PATH
        app.router.add_post(webhook_path, self.handle_webhook)
        app.router.add_get(webhook_path, self._get_handler)
        app.router.add_options(webhook_path, self._options_handler)

        logger.info('✅ Настроен YooKassa webhook на пути: POST', webhook_path=webhook_path)

    async def _get_handler(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                'status': 'ok',
                'message': 'YooKassa webhook endpoint is working',
                'method': 'GET',
                'path': request.path,
                'note': 'Use POST method for actual webhooks',
            }
        )

    async def _options_handler(self, request: web.Request) -> web.Response:
        return web.Response(
            status=200,
            headers={
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type, X-YooKassa-Signature',
            },
        )


def create_yookassa_webhook_app(payment_service: PaymentService) -> web.Application:
    app = web.Application()

    webhook_handler = YooKassaWebhookHandler(payment_service)
    webhook_handler.setup_routes(app)

    async def health_check(request):
        return web.json_response(
            {
                'status': 'ok',
                'service': 'yookassa_webhook',
                'port': settings.YOOKASSA_WEBHOOK_PORT,
                'path': settings.YOOKASSA_WEBHOOK_PATH,
                'enabled': settings.is_yookassa_enabled(),
            }
        )

    app.router.add_get('/health', health_check)

    return app


async def start_yookassa_webhook_server(payment_service: PaymentService) -> None:
    if not settings.is_yookassa_enabled():
        logger.info('ℹ️ YooKassa отключена, webhook сервер не запускается')
        return

    try:
        app = create_yookassa_webhook_app(payment_service)

        runner = web.AppRunner(app)
        await runner.setup()

        site = web.TCPSite(runner, host=settings.YOOKASSA_WEBHOOK_HOST, port=settings.YOOKASSA_WEBHOOK_PORT)

        await site.start()

        logger.info(
            '✅ YooKassa webhook сервер запущен на',
            YOOKASSA_WEBHOOK_HOST=settings.YOOKASSA_WEBHOOK_HOST,
            YOOKASSA_WEBHOOK_PORT=settings.YOOKASSA_WEBHOOK_PORT,
        )
        logger.info(
            '🎯 YooKassa webhook URL: http://',
            YOOKASSA_WEBHOOK_HOST=settings.YOOKASSA_WEBHOOK_HOST,
            YOOKASSA_WEBHOOK_PORT=settings.YOOKASSA_WEBHOOK_PORT,
            YOOKASSA_WEBHOOK_PATH=settings.YOOKASSA_WEBHOOK_PATH,
        )

        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            logger.info('🛑 YooKassa webhook сервер получил сигнал остановки')
        finally:
            await site.stop()
            await runner.cleanup()
            logger.info('✅ YooKassa webhook сервер остановлен')

    except Exception as e:
        logger.error('❌ Ошибка запуска YooKassa webhook сервера', error=e, exc_info=True)
        raise
