"""
Выключатель писем по типу: админ отключает письмо в редакторе шаблонов, и оно
не отправляется никому. Хранится в настройке EMAIL_DISABLED_TYPES (через слой
системных настроек — без миграции и с мгновенным применением).

Нельзя отключить письма, без которых пользователь не сможет войти или
получить купленное: подтверждение почты, сброс пароля, код смены почты,
доступы кабинета и доставка гостевой покупки.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

from .email_layout import EMAIL_LAYOUT_TYPE


EMAIL_DISABLED_TYPES_KEY = 'EMAIL_DISABLED_TYPES'

ALWAYS_ON_EMAIL_TYPES = frozenset(
    {
        'email_verification',
        'password_reset',
        'email_change_code',
        'guest_cabinet_credentials',
        'guest_subscription_delivered',
    }
)


def can_disable_email_type(notification_type: str) -> bool:
    return notification_type not in ALWAYS_ON_EMAIL_TYPES and notification_type != EMAIL_LAYOUT_TYPE


def is_email_type_enabled(notification_type: str) -> bool:
    """False — письмо этого типа отключено админом; отправитель молча пропускает его."""
    if not can_disable_email_type(notification_type):
        return True
    return notification_type not in settings.get_email_disabled_types()


async def set_email_type_enabled(db: AsyncSession, notification_type: str, enabled: bool) -> set[str]:
    """Включает/выключает тип письма; возвращает новый набор отключённых."""
    from app.services.system_settings_service import bot_configuration_service

    disabled = set(settings.get_email_disabled_types())
    if enabled:
        disabled.discard(notification_type)
    else:
        disabled.add(notification_type)
    await bot_configuration_service.set_value(db, EMAIL_DISABLED_TYPES_KEY, ','.join(sorted(disabled)))
    return disabled
