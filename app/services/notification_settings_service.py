"""Переключатели уведомлений истёкшим: «1 день после истечения», волны скидок, отписка от канала.

Это обычные настройки бота (ключи ``NOTIFICATION_*``): хранятся в базе через слой системных
настроек, видны в кабинете, читаются живьём из ``settings`` — каждый цикл мониторинга видит
свежее значение. Раньше они жили в файле ``data/notification_settings.json`` и кэшировались в
памяти процесса навсегда: второй процесс с тем же образом или потерянный/недоступный на запись
каталог ``data/`` после перезапуска возвращали волнам «включено», и предложения уходили при
выключенных переключателях. Старый файл импортируется один раз при старте
(``import_legacy_file``) и переименовывается.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings


logger = structlog.get_logger(__name__)

KEY_TRIAL_CHANNEL = 'NOTIFICATION_TRIAL_CHANNEL_UNSUBSCRIBED_ENABLED'
KEY_EXPIRED_1D = 'NOTIFICATION_EXPIRED_1D_ENABLED'
KEY_WAVE2_ENABLED = 'NOTIFICATION_EXPIRED_WAVE2_ENABLED'
KEY_WAVE2_PERCENT = 'NOTIFICATION_EXPIRED_WAVE2_DISCOUNT_PERCENT'
KEY_WAVE2_HOURS = 'NOTIFICATION_EXPIRED_WAVE2_VALID_HOURS'
KEY_WAVE3_ENABLED = 'NOTIFICATION_EXPIRED_WAVE3_ENABLED'
KEY_WAVE3_PERCENT = 'NOTIFICATION_EXPIRED_WAVE3_DISCOUNT_PERCENT'
KEY_WAVE3_HOURS = 'NOTIFICATION_EXPIRED_WAVE3_VALID_HOURS'
KEY_WAVE3_DAYS = 'NOTIFICATION_EXPIRED_WAVE3_TRIGGER_DAYS'

PERCENT_RANGE = (0, 100)
HOURS_RANGE = (1, 168)
DAYS_RANGE = (2, 60)

#: Секция и поле старого файла → ключ настройки и тип значения.
_LEGACY_FIELDS: tuple[tuple[str, str, str, type], ...] = (
    ('trial_channel_unsubscribed', 'enabled', KEY_TRIAL_CHANNEL, bool),
    ('expired_1d', 'enabled', KEY_EXPIRED_1D, bool),
    ('expired_second_wave', 'enabled', KEY_WAVE2_ENABLED, bool),
    ('expired_second_wave', 'discount_percent', KEY_WAVE2_PERCENT, int),
    ('expired_second_wave', 'valid_hours', KEY_WAVE2_HOURS, int),
    ('expired_third_wave', 'enabled', KEY_WAVE3_ENABLED, bool),
    ('expired_third_wave', 'discount_percent', KEY_WAVE3_PERCENT, int),
    ('expired_third_wave', 'valid_hours', KEY_WAVE3_HOURS, int),
    ('expired_third_wave', 'trigger_days', KEY_WAVE3_DAYS, int),
)


def _clamped(value: Any, bounds: tuple[int, int], default: int) -> int:
    """Число в границах; мусор — значение по умолчанию."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(bounds[0], min(bounds[1], number))


def _bounded(value: Any, bounds: tuple[int, int]) -> int | None:
    """Число в границах для записи; мусор — None (запись отклоняется)."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(bounds[0], min(bounds[1], number))


def _coerce_legacy(kind: type, value: Any) -> Any | None:
    if kind is bool:
        return value if isinstance(value, bool) else None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class NotificationSettingsService:
    """Фасад над ``settings``: чтение живьём, запись — через слой системных настроек (база + процесс)."""

    _legacy_path: Path = Path('data/notification_settings.json')

    # ------------------------------------------------------------ запись

    @staticmethod
    async def _store(db: AsyncSession, key: str, value: Any) -> bool:
        # Локальный импорт: слой системных настроек тянет половину конфигурации.
        from app.services.system_settings_service import bot_configuration_service

        try:
            await bot_configuration_service.set_value(db, key, value)
        except Exception as error:
            logger.error('Не удалось сохранить настройку уведомлений', key=key, error=error)
            return False
        return True

    @classmethod
    async def _store_bounded(cls, db: AsyncSession, key: str, value: Any, bounds: tuple[int, int]) -> bool:
        number = _bounded(value, bounds)
        if number is None:
            return False
        return await cls._store(db, key, number)

    # ------------------------------------------------------------ чтение

    @classmethod
    def get_config(cls) -> dict[str, dict[str, Any]]:
        return {
            'trial_channel_unsubscribed': {'enabled': cls.is_trial_channel_unsubscribed_enabled()},
            'expired_1d': {'enabled': cls.is_expired_1d_enabled()},
            'expired_second_wave': {
                'enabled': cls.is_second_wave_enabled(),
                'discount_percent': cls.get_second_wave_discount_percent(),
                'valid_hours': cls.get_second_wave_valid_hours(),
            },
            'expired_third_wave': {
                'enabled': cls.is_third_wave_enabled(),
                'discount_percent': cls.get_third_wave_discount_percent(),
                'valid_hours': cls.get_third_wave_valid_hours(),
                'trigger_days': cls.get_third_wave_trigger_days(),
            },
        }

    @classmethod
    def is_trial_channel_unsubscribed_enabled(cls) -> bool:
        return bool(settings.NOTIFICATION_TRIAL_CHANNEL_UNSUBSCRIBED_ENABLED)

    @classmethod
    async def set_trial_channel_unsubscribed_enabled(cls, db: AsyncSession, enabled: bool) -> bool:
        return await cls._store(db, KEY_TRIAL_CHANNEL, bool(enabled))

    @classmethod
    def is_expired_1d_enabled(cls) -> bool:
        return bool(settings.NOTIFICATION_EXPIRED_1D_ENABLED)

    @classmethod
    async def set_expired_1d_enabled(cls, db: AsyncSession, enabled: bool) -> bool:
        return await cls._store(db, KEY_EXPIRED_1D, bool(enabled))

    @classmethod
    def is_second_wave_enabled(cls) -> bool:
        return bool(settings.NOTIFICATION_EXPIRED_WAVE2_ENABLED)

    @classmethod
    async def set_second_wave_enabled(cls, db: AsyncSession, enabled: bool) -> bool:
        return await cls._store(db, KEY_WAVE2_ENABLED, bool(enabled))

    @classmethod
    def get_second_wave_discount_percent(cls) -> int:
        return _clamped(settings.NOTIFICATION_EXPIRED_WAVE2_DISCOUNT_PERCENT, PERCENT_RANGE, 10)

    @classmethod
    async def set_second_wave_discount_percent(cls, db: AsyncSession, percent: Any) -> bool:
        return await cls._store_bounded(db, KEY_WAVE2_PERCENT, percent, PERCENT_RANGE)

    @classmethod
    def get_second_wave_valid_hours(cls) -> int:
        return _clamped(settings.NOTIFICATION_EXPIRED_WAVE2_VALID_HOURS, HOURS_RANGE, 24)

    @classmethod
    async def set_second_wave_valid_hours(cls, db: AsyncSession, hours: Any) -> bool:
        return await cls._store_bounded(db, KEY_WAVE2_HOURS, hours, HOURS_RANGE)

    @classmethod
    def is_third_wave_enabled(cls) -> bool:
        return bool(settings.NOTIFICATION_EXPIRED_WAVE3_ENABLED)

    @classmethod
    async def set_third_wave_enabled(cls, db: AsyncSession, enabled: bool) -> bool:
        return await cls._store(db, KEY_WAVE3_ENABLED, bool(enabled))

    @classmethod
    def get_third_wave_discount_percent(cls) -> int:
        return _clamped(settings.NOTIFICATION_EXPIRED_WAVE3_DISCOUNT_PERCENT, PERCENT_RANGE, 20)

    @classmethod
    async def set_third_wave_discount_percent(cls, db: AsyncSession, percent: Any) -> bool:
        return await cls._store_bounded(db, KEY_WAVE3_PERCENT, percent, PERCENT_RANGE)

    @classmethod
    def get_third_wave_valid_hours(cls) -> int:
        return _clamped(settings.NOTIFICATION_EXPIRED_WAVE3_VALID_HOURS, HOURS_RANGE, 24)

    @classmethod
    async def set_third_wave_valid_hours(cls, db: AsyncSession, hours: Any) -> bool:
        return await cls._store_bounded(db, KEY_WAVE3_HOURS, hours, HOURS_RANGE)

    @classmethod
    def get_third_wave_trigger_days(cls) -> int:
        return _clamped(settings.NOTIFICATION_EXPIRED_WAVE3_TRIGGER_DAYS, DAYS_RANGE, 5)

    @classmethod
    async def set_third_wave_trigger_days(cls, db: AsyncSession, days: Any) -> bool:
        return await cls._store_bounded(db, KEY_WAVE3_DAYS, days, DAYS_RANGE)

    @classmethod
    def are_notifications_globally_enabled(cls) -> bool:
        return bool(getattr(settings, 'ENABLE_NOTIFICATIONS', True))

    # ------------------------------------------------------------ перенос старого файла

    @classmethod
    async def import_legacy_file(cls, db: AsyncSession) -> dict[str, Any]:
        """Один раз перенести ``data/notification_settings.json`` в базу и переименовать его.

        Значение, уже заданное в базе, главнее файла; мусор в файле пропускается; битый файл
        остаётся человеку и не трогается. Возвращает перенесённые ключи со значениями.
        """
        path = cls._legacy_path
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding='utf-8') or '{}')
        except (OSError, ValueError) as error:
            logger.error(
                'Старый файл настроек уведомлений не читается — оставлен как есть', path=str(path), error=error
            )
            return {}
        sections = raw if isinstance(raw, dict) else {}

        from app.services.system_settings_service import bot_configuration_service

        imported: dict[str, Any] = {}
        for section, field, key, kind in _LEGACY_FIELDS:
            entry = sections.get(section)
            value = _coerce_legacy(kind, entry.get(field)) if isinstance(entry, dict) else None
            if value is None or bot_configuration_service.has_override(key):
                continue
            await bot_configuration_service.set_value(db, key, value, commit=False)
            imported[key] = value
        if imported:
            await db.commit()
        try:
            path.rename(path.with_name(path.name + '.imported'))
        except OSError as error:
            logger.warning('Не удалось переименовать старый файл настроек уведомлений', path=str(path), error=error)
        logger.info('Настройки уведомлений перенесены из файла в базу', keys=sorted(imported))
        return imported
