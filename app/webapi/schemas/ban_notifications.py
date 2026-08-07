from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class BanNotificationRequest(BaseModel):
    """Запрос на отправку уведомления о бане пользователю"""

    notification_type: Literal[
        'punishment',
        'revoke',
        'enabled',
        'warning',
        'network_wifi',
        'network_mobile',
        'torrent',
        'hwid_limit',
        'suspicious_destination',
        'traffic_limit',
        'manual',
    ] = Field(
        description=(
            'Тип уведомления: punishment (бан за устройства), revoke (сброс ключей), enabled (разбан), '
            'warning (предупреждение), '
            'network_wifi/network_mobile (бан за сеть), torrent, hwid_limit, suspicious_destination, '
            'traffic_limit или manual (типизированные ручные баны)'
        )
    )
    user_identifier: str = Field(
        min_length=1, max_length=255, description='Email или user_id пользователя из Remnawave Panel'
    )
    username: str = Field(min_length=1, max_length=255, description='Имя пользователя для отображения')

    # Данные для punishment
    ip_count: int | None = Field(None, ge=0, description='Количество устройств')
    limit: int | None = Field(None, ge=0, description='Лимит устройств')
    ban_minutes: int | None = Field(None, ge=1, le=10080, description='Длительность бана в минутах')
    reason: str | None = Field(
        None,
        max_length=1000,
        description='Причина типизированного ручного бана',
    )

    # Данные для warning
    warning_message: str | None = Field(None, max_length=1000, description='Текст предупреждения')

    # Данные для network_wifi/network_mobile и punishment
    network_type: str | None = Field(None, max_length=64, description='Тип сети (WiFi/Mobile)')
    node_name: str | None = Field(None, max_length=255, description='Название ноды/сервера с которой пришел бан')

    class Config:
        json_schema_extra = {
            'example': {
                'notification_type': 'punishment',
                'user_identifier': 'user@example.com',
                'username': 'john_doe',
                'ip_count': 5,
                'limit': 3,
                'ban_minutes': 30,
                'node_name': 'DE-Server-1',
            }
        }


class BanNotificationResponse(BaseModel):
    """Ответ на запрос отправки уведомления"""

    success: bool = Field(description='Успешно ли отправлено уведомление')
    message: str = Field(description='Сообщение о результате')
    telegram_id: int | None = Field(None, description='Telegram ID получателя')
    sent: bool = Field(False, description='Было ли фактически отправлено сообщение')

    class Config:
        json_schema_extra = {
            'example': {'success': True, 'message': 'Уведомление отправлено', 'telegram_id': 123456789, 'sent': True}
        }
