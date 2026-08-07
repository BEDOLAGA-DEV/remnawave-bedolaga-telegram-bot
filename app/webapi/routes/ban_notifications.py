"""
API эндпоинты для приема уведомлений от ban системы
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ban_notification_service import ban_notification_service
from app.webapi.dependencies import get_db_session, require_api_token
from app.webapi.schemas.ban_notifications import (
    BanNotificationRequest,
    BanNotificationResponse,
)


logger = structlog.get_logger(__name__)

router = APIRouter()


@router.post(
    '/send',
    response_model=BanNotificationResponse,
    summary='Отправить уведомление от ban системы',
    description=(
        'Эндпоинт для отправки уведомлений пользователям от системы мониторинга ban. '
        'Поддерживает уведомления о блокировке, разблокировке и предупреждения.'
    ),
)
async def send_ban_notification(
    request: BanNotificationRequest,
    db: AsyncSession = Depends(get_db_session),
    _token=Depends(require_api_token),
) -> BanNotificationResponse:
    """
    Отправить уведомление пользователю от ban системы

    - **punishment**: Уведомление о блокировке за превышение лимита устройств
    - **enabled**: Уведомление о снятии блокировки
    - **warning**: Предупреждение пользователю

    Требует API ключ в заголовке X-API-Key или Authorization: Bearer <token>
    """
    logger.info(
        'Получен запрос на отправку уведомления типа для пользователя node_name',
        notification_type=request.notification_type,
        username=request.username,
        user_identifier=request.user_identifier,
        node_name=repr(request.node_name),
    )

    try:
        if request.notification_type in {'punishment', 'revoke'}:
            if request.ip_count is None or request.limit is None or request.ban_minutes is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Для типа '{request.notification_type}' требуются поля: ip_count, limit, ban_minutes",
                )

            success, message, telegram_id = await ban_notification_service.send_punishment_notification(
                db=db,
                user_identifier=request.user_identifier,
                username=request.username,
                ip_count=request.ip_count,
                limit=request.limit,
                ban_minutes=request.ban_minutes,
                node_name=request.node_name,
                revoke=request.notification_type == 'revoke',
            )

        elif request.notification_type == 'enabled':
            success, message, telegram_id = await ban_notification_service.send_enabled_notification(
                db=db,
                user_identifier=request.user_identifier,
                username=request.username,
            )

        elif request.notification_type == 'warning':
            if not request.warning_message:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="Для типа 'warning' требуется поле: warning_message"
                )

            success, message, telegram_id = await ban_notification_service.send_warning_notification(
                db=db,
                user_identifier=request.user_identifier,
                username=request.username,
                warning_message=request.warning_message,
            )

        elif request.notification_type == 'network_wifi':
            if request.ban_minutes is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Для типа 'network_wifi' требуется поле: ban_minutes",
                )

            success, message, telegram_id = await ban_notification_service.send_network_wifi_notification(
                db=db,
                user_identifier=request.user_identifier,
                username=request.username,
                ban_minutes=request.ban_minutes,
                network_type=request.network_type,
                node_name=request.node_name,
            )

        elif request.notification_type == 'network_mobile':
            if request.ban_minutes is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Для типа 'network_mobile' требуется поле: ban_minutes",
                )

            success, message, telegram_id = await ban_notification_service.send_network_mobile_notification(
                db=db,
                user_identifier=request.user_identifier,
                username=request.username,
                ban_minutes=request.ban_minutes,
                network_type=request.network_type,
                node_name=request.node_name,
            )

        elif request.notification_type in {
            'torrent',
            'hwid_limit',
            'suspicious_destination',
            'traffic_limit',
            'manual',
        }:
            if request.ban_minutes is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Для типа '{request.notification_type}' требуется поле: ban_minutes",
                )

            success, message, telegram_id = await ban_notification_service.send_typed_ban_notification(
                db=db,
                user_identifier=request.user_identifier,
                username=request.username,
                notification_type=request.notification_type,
                ban_minutes=request.ban_minutes,
                reason=request.reason,
                node_name=request.node_name,
            )

        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f'Неизвестный тип уведомления: {request.notification_type}',
            )

        return BanNotificationResponse(success=success, message=message, telegram_id=telegram_id, sent=success)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception('Ошибка при отправке уведомления', error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f'Внутренняя ошибка сервера: {e!s}'
        ) from e
