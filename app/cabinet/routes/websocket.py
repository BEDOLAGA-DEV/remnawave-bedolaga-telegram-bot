"""WebSocket endpoint for cabinet real-time notifications."""

from __future__ import annotations

import asyncio
import json

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.cabinet.auth.jwt_handler import get_token_payload
from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.database import AsyncSessionLocal


logger = structlog.get_logger(__name__)

router = APIRouter()


class CabinetConnectionManager:
    """Менеджер WebSocket подключений для кабинета."""

    def __init__(self):
        # user_id -> set of websocket connections
        self._user_connections: dict[int, set[WebSocket]] = {}
        # admin user_ids -> set of websocket connections
        self._admin_connections: dict[int, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, user_id: int, is_admin: bool) -> None:
        """Зарегистрировать подключение."""
        async with self._lock:
            if user_id not in self._user_connections:
                self._user_connections[user_id] = set()
            self._user_connections[user_id].add(websocket)

            if is_admin:
                if user_id not in self._admin_connections:
                    self._admin_connections[user_id] = set()
                self._admin_connections[user_id].add(websocket)

        logger.debug(
            'Cabinet WS connected: user_id is_admin total_users',
            user_id=user_id,
            is_admin=is_admin,
            user_connections_count=len(self._user_connections),
        )

    async def disconnect(self, websocket: WebSocket, user_id: int) -> None:
        """Отменить регистрацию подключения."""
        async with self._lock:
            if user_id in self._user_connections:
                self._user_connections[user_id].discard(websocket)
                if not self._user_connections[user_id]:
                    del self._user_connections[user_id]

            if user_id in self._admin_connections:
                self._admin_connections[user_id].discard(websocket)
                if not self._admin_connections[user_id]:
                    del self._admin_connections[user_id]

        logger.debug('Cabinet WS disconnected: user_id', user_id=user_id)

    async def send_to_user(self, user_id: int, message: dict) -> None:
        """Отправить сообщение конкретному пользователю."""
        # Snapshot connections under the lock to avoid mutation during iteration
        async with self._lock:
            connections = list(self._user_connections.get(user_id, set()))

        if not connections:
            return

        disconnected = set()
        data = json.dumps(message, default=str, ensure_ascii=False)

        for ws in connections:
            try:
                await ws.send_text(data)
            except Exception as e:
                logger.warning('Failed to send to user', user_id=user_id, e=e)
                disconnected.add(ws)

        # Cleanup disconnected
        if disconnected:
            async with self._lock:
                for ws in disconnected:
                    self._user_connections.get(user_id, set()).discard(ws)

    async def send_to_admins(self, message: dict) -> None:
        """Отправить сообщение всем админам."""
        # Snapshot connections under the lock to avoid mutation during iteration
        async with self._lock:
            if not self._admin_connections:
                return
            # Create a snapshot: list of (user_id, list of websockets)
            admin_snapshot = [(user_id, list(connections)) for user_id, connections in self._admin_connections.items()]

        data = json.dumps(message, default=str, ensure_ascii=False)
        disconnected_by_user: dict[int, set[WebSocket]] = {}

        for user_id, connections in admin_snapshot:
            for ws in connections:
                try:
                    await ws.send_text(data)
                except Exception as e:
                    logger.warning('Failed to send to admin', user_id=user_id, e=e)
                    if user_id not in disconnected_by_user:
                        disconnected_by_user[user_id] = set()
                    disconnected_by_user[user_id].add(ws)

        # Cleanup disconnected
        if disconnected_by_user:
            async with self._lock:
                for user_id, ws_set in disconnected_by_user.items():
                    for ws in ws_set:
                        self._admin_connections.get(user_id, set()).discard(ws)


# Глобальный менеджер подключений
cabinet_ws_manager = CabinetConnectionManager()


async def verify_cabinet_ws_token(token: str) -> tuple[int | None, bool]:
    """
    Проверить JWT токен для WebSocket.

    Returns:
        tuple[user_id, is_admin] или (None, False) если токен невалидный
    """
    if not token:
        return None, False

    payload = get_token_payload(token, expected_type='access')
    if not payload:
        return None, False

    try:
        user_id = int(payload.get('sub'))
    except (TypeError, ValueError):
        return None, False

    try:
        async with AsyncSessionLocal() as db:
            user = await get_user_by_id(db, user_id)
            if not user or user.status != 'active':
                return None, False

            is_admin = settings.is_admin(
                telegram_id=user.telegram_id, email=user.email if user.email_verified else None
            )
            return user_id, is_admin
    except (TimeoutError, OSError, ConnectionRefusedError) as e:
        logger.error('Database connection error in WS token verification', e=str(e)[:200])
        return None, False


@router.websocket('/ws')
async def cabinet_websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint для real-time уведомлений кабинета."""
    client_host = websocket.client.host if websocket.client else 'unknown'

    # Получаем токен из query params
    token = websocket.query_params.get('token')

    if not token:
        logger.debug('Cabinet WS: No token from', client_host=client_host)
        # Принимаем и сразу закрываем с кодом ошибки
        await websocket.accept()
        await websocket.close(code=1008, reason='Unauthorized: No token')
        return

    # Верифицируем токен
    user_id, is_admin = await verify_cabinet_ws_token(token)

    if not user_id:
        logger.debug('Cabinet WS: Invalid token from', client_host=client_host)
        # Принимаем и сразу закрываем с кодом ошибки
        await websocket.accept()
        await websocket.close(code=1008, reason='Unauthorized: Invalid token')
        return

    # Принимаем соединение
    try:
        await websocket.accept()
        logger.debug('Cabinet WS accepted: user_id is_admin', user_id=user_id, is_admin=is_admin)
    except Exception as e:
        logger.error('Cabinet WS: Failed to accept from', client_host=client_host, e=e)
        return

    # Регистрируем подключение
    await cabinet_ws_manager.connect(websocket, user_id, is_admin)

    try:
        # Приветственное сообщение
        await websocket.send_json(
            {
                'type': 'connected',
                'user_id': user_id,
                'is_admin': is_admin,
            }
        )

        # Обрабатываем входящие сообщения
        while True:
            try:
                data = await websocket.receive_text()
                message = json.loads(data)

                # Ping/pong для keepalive
                if message.get('type') == 'ping':
                    await websocket.send_json({'type': 'pong'})

            except json.JSONDecodeError:
                logger.warning('Cabinet WS: Invalid JSON from user', user_id=user_id)
            except WebSocketDisconnect:
                break
            except Exception as e:
                logger.exception('Cabinet WS error for user', user_id=user_id, e=e)
                break

    except WebSocketDisconnect:
        logger.debug('Cabinet WS disconnected: user_id', user_id=user_id)
    except Exception as e:
        logger.exception('Cabinet WS error', e=e)
    finally:
        await cabinet_ws_manager.disconnect(websocket, user_id)


# Функции для отправки уведомлений (используются из других модулей)
async def notify_user_ticket_reply(user_id: int, ticket_id: int, message: str) -> None:
    """Уведомить пользователя об ответе в тикете."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'ticket.admin_reply',
            'ticket_id': ticket_id,
            'message': message,
        },
    )


async def notify_admins_new_ticket(ticket_id: int, title: str, user_id: int) -> None:
    """Уведомить админов о новом тикете."""
    await cabinet_ws_manager.send_to_admins(
        {
            'type': 'ticket.new',
            'ticket_id': ticket_id,
            'title': title,
            'user_id': user_id,
        }
    )


async def notify_admins_ticket_reply(ticket_id: int, message: str, user_id: int) -> None:
    """Уведомить админов об ответе пользователя."""
    await cabinet_ws_manager.send_to_admins(
        {
            'type': 'ticket.user_reply',
            'ticket_id': ticket_id,
            'message': message,
            'user_id': user_id,
        }
    )


# ============================================================================
# Уведомления о балансе
# ============================================================================


async def notify_user_balance_topup(
    user_id: int,
    amount_kopeks: int,
    new_balance_kopeks: int,
    description: str = '',
) -> None:
    """Уведомить пользователя о пополнении баланса."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'balance.topup',
            'amount_kopeks': amount_kopeks,
            'amount_rubles': amount_kopeks / 100,
            'new_balance_kopeks': new_balance_kopeks,
            'new_balance_rubles': new_balance_kopeks / 100,
            'description': description,
        },
    )


async def notify_user_balance_change(
    user_id: int,
    amount_kopeks: int,
    new_balance_kopeks: int,
    description: str = '',
) -> None:
    """Уведомить пользователя об изменении баланса."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'balance.change',
            'amount_kopeks': amount_kopeks,
            'amount_rubles': amount_kopeks / 100,
            'new_balance_kopeks': new_balance_kopeks,
            'new_balance_rubles': new_balance_kopeks / 100,
            'description': description,
        },
    )


# ============================================================================
# Уведомления о подписке
# ============================================================================


async def notify_user_subscription_activated(
    user_id: int,
    subscription_id: int | None = None,
    expires_at: str = '',
    tariff_name: str = '',
) -> None:
    """Уведомить пользователя об активации подписки."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'subscription.activated',
            'subscription_id': subscription_id,
            'expires_at': expires_at,
            'tariff_name': tariff_name,
        },
    )


async def notify_user_subscription_expiring(
    user_id: int,
    days_left: int,
    expires_at: str,
) -> None:
    """Уведомить пользователя о скором истечении подписки."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'subscription.expiring',
            'days_left': days_left,
            'expires_at': expires_at,
        },
    )


async def notify_user_subscription_expired(user_id: int) -> None:
    """Уведомить пользователя об истечении подписки."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'subscription.expired',
        },
    )


async def notify_user_subscription_renewed(
    user_id: int,
    subscription_id: int | None = None,
    new_expires_at: str = '',
    amount_kopeks: int = 0,
) -> None:
    """Уведомить пользователя о продлении подписки."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'subscription.renewed',
            'subscription_id': subscription_id,
            'new_expires_at': new_expires_at,
            'amount_kopeks': amount_kopeks,
            'amount_rubles': amount_kopeks / 100,
        },
    )


async def notify_user_devices_purchased(
    user_id: int,
    devices_added: int,
    new_device_limit: int,
    amount_kopeks: int,
) -> None:
    """Уведомить пользователя о покупке устройств."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'subscription.devices_purchased',
            'devices_added': devices_added,
            'new_device_limit': new_device_limit,
            'amount_kopeks': amount_kopeks,
            'amount_rubles': amount_kopeks / 100,
        },
    )


async def notify_user_traffic_purchased(
    user_id: int,
    traffic_gb_added: int,
    new_traffic_limit_gb: int,
    amount_kopeks: int,
) -> None:
    """Уведомить пользователя о покупке трафика."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'subscription.traffic_purchased',
            'traffic_gb_added': traffic_gb_added,
            'new_traffic_limit_gb': new_traffic_limit_gb,
            'amount_kopeks': amount_kopeks,
            'amount_rubles': amount_kopeks / 100,
        },
    )


# ============================================================================
# Уведомления об автопродлении
# ============================================================================


async def notify_user_autopay_success(
    user_id: int,
    amount_kopeks: int,
    new_expires_at: str,
) -> None:
    """Уведомить пользователя об успешном автопродлении."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'autopay.success',
            'amount_kopeks': amount_kopeks,
            'amount_rubles': amount_kopeks / 100,
            'new_expires_at': new_expires_at,
        },
    )


async def notify_user_autopay_failed(
    user_id: int,
    reason: str = '',
) -> None:
    """Уведомить пользователя о неудачном автопродлении."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'autopay.failed',
            'reason': reason,
        },
    )


async def notify_user_autopay_insufficient_funds(
    user_id: int,
    required_kopeks: int,
    balance_kopeks: int,
) -> None:
    """Уведомить о недостатке средств для автопродления."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'autopay.insufficient_funds',
            'required_kopeks': required_kopeks,
            'required_rubles': required_kopeks / 100,
            'balance_kopeks': balance_kopeks,
            'balance_rubles': balance_kopeks / 100,
        },
    )


# ============================================================================
# Уведомления о бане/разбане
# ============================================================================


async def notify_user_ban(user_id: int, reason: str = '') -> None:
    """Уведомить пользователя о блокировке."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'account.banned',
            'reason': reason,
        },
    )


async def notify_user_unban(user_id: int) -> None:
    """Уведомить пользователя о разблокировке."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'account.unbanned',
        },
    )


async def notify_user_warning(user_id: int, message: str) -> None:
    """Уведомить пользователя о предупреждении."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'account.warning',
            'message': message,
        },
    )


# ============================================================================
# Уведомления о рефералах
# ============================================================================


async def notify_user_referral_bonus(
    user_id: int,
    bonus_kopeks: int,
    referral_name: str = '',
) -> None:
    """Уведомить пользователя о реферальном бонусе."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'referral.bonus',
            'bonus_kopeks': bonus_kopeks,
            'bonus_rubles': bonus_kopeks / 100,
            'referral_name': referral_name,
        },
    )


async def notify_user_referral_registered(
    user_id: int,
    referral_name: str = '',
) -> None:
    """Уведомить пользователя о регистрации нового реферала."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'referral.registered',
            'referral_name': referral_name,
        },
    )


# ============================================================================
# Прочие уведомления
# ============================================================================


async def notify_user_daily_debit(
    user_id: int,
    amount_kopeks: int,
    new_balance_kopeks: int,
) -> None:
    """Уведомить о ежедневном списании."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'subscription.daily_debit',
            'amount_kopeks': amount_kopeks,
            'amount_rubles': amount_kopeks / 100,
            'new_balance_kopeks': new_balance_kopeks,
            'new_balance_rubles': new_balance_kopeks / 100,
        },
    )


async def notify_user_traffic_reset(user_id: int) -> None:
    """Уведомить о сбросе трафика."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'subscription.traffic_reset',
        },
    )


async def notify_user_payment_received(
    user_id: int,
    amount_kopeks: int,
    payment_method: str = '',
) -> None:
    """Уведомить о полученном платеже."""
    await cabinet_ws_manager.send_to_user(
        user_id,
        {
            'type': 'payment.received',
            'amount_kopeks': amount_kopeks,
            'amount_rubles': amount_kopeks / 100,
            'payment_method': payment_method,
        },
    )
