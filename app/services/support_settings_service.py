"""Настройки поддержки: режим (тикеты / контакт / оба), меню, уведомления о тикетах, SLA,
модераторы, тексты «о поддержке» по языкам.

Обычные настройки бота (ключи ``SUPPORT_*``): база через слой системных настроек, кабинет,
живое чтение из ``settings``. Раньше — файл ``data/support_settings.json`` с вечным кэшем в
памяти процесса (тот же класс дефекта, что у переключателей уведомлений истёкшим). Старый файл
импортируется один раз при старте (``import_legacy_file``) и переименовывается.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.settings_store import import_legacy_values, read_legacy_json, store_setting


logger = structlog.get_logger(__name__)

MODES = frozenset({'tickets', 'contact', 'both'})
SLA_MINUTES_DEFAULT = 60

KEY_MODE = 'SUPPORT_SYSTEM_MODE'
KEY_MENU = 'SUPPORT_MENU_ENABLED'
KEY_SLA_ENABLED = 'SUPPORT_TICKET_SLA_ENABLED'
KEY_SLA_MINUTES = 'SUPPORT_TICKET_SLA_MINUTES'
KEY_ADMIN_TICKET_NOTIFICATIONS = 'SUPPORT_ADMIN_TICKET_NOTIFICATIONS_ENABLED'
KEY_USER_TICKET_NOTIFICATIONS = 'SUPPORT_USER_TICKET_NOTIFICATIONS_ENABLED'
KEY_CABINET_USER_NOTIFICATIONS = 'SUPPORT_CABINET_USER_NOTIFICATIONS_ENABLED'
KEY_CABINET_ADMIN_NOTIFICATIONS = 'SUPPORT_CABINET_ADMIN_NOTIFICATIONS_ENABLED'
KEY_MODERATORS = 'SUPPORT_MODERATOR_IDS'

#: Булевы поля старого файла → ключи настроек.
_LEGACY_FLAGS: tuple[tuple[str, str], ...] = (
    ('menu_enabled', KEY_MENU),
    ('admin_ticket_notifications_enabled', KEY_ADMIN_TICKET_NOTIFICATIONS),
    ('user_ticket_notifications_enabled', KEY_USER_TICKET_NOTIFICATIONS),
    ('ticket_sla_enabled', KEY_SLA_ENABLED),
    ('cabinet_user_notifications_enabled', KEY_CABINET_USER_NOTIFICATIONS),
    ('cabinet_admin_notifications_enabled', KEY_CABINET_ADMIN_NOTIFICATIONS),
)


def _normalize_mode(value: Any) -> str | None:
    mode = str(value or '').strip().lower()
    return mode if mode in MODES else None


def _language_key(language: str | None) -> str:
    lang = (language or settings.DEFAULT_LANGUAGE).split('-')[0].lower()
    return f'SUPPORT_INFO_TEXT_{lang.upper()}'


def _parse_ids(raw: Any) -> list[int]:
    """Telegram ID из строки через запятую или списка; мусор пропускается поштучно."""
    items = raw.split(',') if isinstance(raw, str) else raw if isinstance(raw, list | tuple) else []
    ids: list[int] = []
    for item in items:
        try:
            ids.append(int(str(item).strip()))
        except (TypeError, ValueError):
            continue
    return ids


def _ids_csv(ids: set[int]) -> str:
    return ','.join(str(item) for item in sorted(ids))


class SupportSettingsService:
    """Фасад над ``settings``: чтение живьём, запись — через слой системных настроек (база + процесс)."""

    _legacy_path: Path = Path('data/support_settings.json')

    # ------------------------------------------------------------ режим и меню

    @classmethod
    def get_system_mode(cls) -> str:
        return settings.get_support_system_mode()

    @classmethod
    async def set_system_mode(cls, db: AsyncSession, mode: str) -> bool:
        mode_clean = _normalize_mode(mode)
        if mode_clean is None:
            return False
        return await store_setting(db, KEY_MODE, mode_clean)

    @classmethod
    def is_support_menu_enabled(cls) -> bool:
        return bool(settings.SUPPORT_MENU_ENABLED)

    @classmethod
    async def set_support_menu_enabled(cls, db: AsyncSession, enabled: bool) -> bool:
        return await store_setting(db, KEY_MENU, bool(enabled))

    @classmethod
    def is_tickets_enabled(cls) -> bool:
        return cls.get_system_mode() in {'tickets', 'both'}

    @classmethod
    def is_contact_enabled(cls) -> bool:
        return cls.get_system_mode() in {'contact', 'both'}

    # ------------------------------------------------------------ тексты «о поддержке»

    @classmethod
    def get_support_info_text(cls, language: str) -> str:
        text = getattr(settings, _language_key(language), '')
        if isinstance(text, str) and text.strip():
            return text
        from app.localization.texts import get_texts

        lang = (language or settings.DEFAULT_LANGUAGE).split('-')[0].lower()
        return get_texts(lang).SUPPORT_INFO

    @classmethod
    async def set_support_info_text(cls, db: AsyncSession, language: str, text: str) -> bool:
        key = _language_key(language)
        if not hasattr(settings, key):
            logger.warning('Для языка нет поля текста поддержки', language=language, key=key)
            return False
        return await store_setting(db, key, text or '')

    # ------------------------------------------------------------ уведомления и SLA

    @classmethod
    def get_admin_ticket_notifications_enabled(cls) -> bool:
        return bool(settings.SUPPORT_ADMIN_TICKET_NOTIFICATIONS_ENABLED)

    @classmethod
    async def set_admin_ticket_notifications_enabled(cls, db: AsyncSession, enabled: bool) -> bool:
        return await store_setting(db, KEY_ADMIN_TICKET_NOTIFICATIONS, bool(enabled))

    @classmethod
    def get_user_ticket_notifications_enabled(cls) -> bool:
        # Общий выключатель уведомлений пользователям главнее.
        return bool(settings.SUPPORT_USER_TICKET_NOTIFICATIONS_ENABLED) and bool(
            getattr(settings, 'ENABLE_NOTIFICATIONS', True)
        )

    @classmethod
    async def set_user_ticket_notifications_enabled(cls, db: AsyncSession, enabled: bool) -> bool:
        return await store_setting(db, KEY_USER_TICKET_NOTIFICATIONS, bool(enabled))

    @classmethod
    def get_sla_enabled(cls) -> bool:
        return bool(settings.SUPPORT_TICKET_SLA_ENABLED)

    @classmethod
    async def set_sla_enabled(cls, db: AsyncSession, enabled: bool) -> bool:
        return await store_setting(db, KEY_SLA_ENABLED, bool(enabled))

    @classmethod
    def get_sla_minutes(cls) -> int:
        try:
            minutes = int(settings.SUPPORT_TICKET_SLA_MINUTES)
        except (TypeError, ValueError):
            return SLA_MINUTES_DEFAULT
        return minutes if minutes > 0 else SLA_MINUTES_DEFAULT

    @classmethod
    async def set_sla_minutes(cls, db: AsyncSession, minutes: Any) -> bool:
        try:
            minutes_int = int(minutes)
        except (TypeError, ValueError):
            return False
        if minutes_int <= 0:
            return False
        return await store_setting(db, KEY_SLA_MINUTES, minutes_int)

    # ------------------------------------------------------------ модераторы

    @classmethod
    def get_moderators(cls) -> list[int]:
        return _parse_ids(settings.SUPPORT_MODERATOR_IDS)

    @classmethod
    def is_moderator(cls, telegram_id: Any) -> bool:
        try:
            return int(telegram_id) in cls.get_moderators()
        except (TypeError, ValueError):
            return False

    @classmethod
    async def add_moderator(cls, db: AsyncSession, telegram_id: Any) -> bool:
        try:
            tid = int(telegram_id)
        except (TypeError, ValueError):
            return False
        return await store_setting(db, KEY_MODERATORS, _ids_csv({*cls.get_moderators(), tid}))

    @classmethod
    async def remove_moderator(cls, db: AsyncSession, telegram_id: Any) -> bool:
        try:
            tid = int(telegram_id)
        except (TypeError, ValueError):
            return False
        moderators = set(cls.get_moderators())
        if tid not in moderators:
            return True
        moderators.discard(tid)
        return await store_setting(db, KEY_MODERATORS, _ids_csv(moderators))

    # ------------------------------------------------------------ уведомления в кабинет

    @classmethod
    def get_cabinet_user_notifications_enabled(cls) -> bool:
        """Уведомления пользователям в кабинет об ответе на тикет."""
        return bool(settings.SUPPORT_CABINET_USER_NOTIFICATIONS_ENABLED)

    @classmethod
    async def set_cabinet_user_notifications_enabled(cls, db: AsyncSession, enabled: bool) -> bool:
        return await store_setting(db, KEY_CABINET_USER_NOTIFICATIONS, bool(enabled))

    @classmethod
    def get_cabinet_admin_notifications_enabled(cls) -> bool:
        """Уведомления администраторам в кабинет о новых тикетах."""
        return bool(settings.SUPPORT_CABINET_ADMIN_NOTIFICATIONS_ENABLED)

    @classmethod
    async def set_cabinet_admin_notifications_enabled(cls, db: AsyncSession, enabled: bool) -> bool:
        return await store_setting(db, KEY_CABINET_ADMIN_NOTIFICATIONS, bool(enabled))

    # ------------------------------------------------------------ перенос старого файла

    @classmethod
    async def import_legacy_file(cls, db: AsyncSession) -> dict[str, Any]:
        """Один раз перенести ``data/support_settings.json`` в базу; мусор пропускается, база главнее."""
        raw = read_legacy_json(cls._legacy_path)
        if raw is None:
            return {}
        values: dict[str, Any] = {}
        mode = _normalize_mode(raw.get('system_mode'))
        if mode is not None:
            values[KEY_MODE] = mode
        for legacy_key, key in _LEGACY_FLAGS:
            if isinstance(raw.get(legacy_key), bool):
                values[key] = raw[legacy_key]
        minutes = raw.get('ticket_sla_minutes')
        if isinstance(minutes, int) and not isinstance(minutes, bool) and minutes > 0:
            values[KEY_SLA_MINUTES] = minutes
        texts = raw.get('support_info_texts')
        if isinstance(texts, dict):
            for language, text in texts.items():
                key = _language_key(str(language))
                if hasattr(settings, key) and isinstance(text, str) and text.strip():
                    values[key] = text
        moderators = _parse_ids(raw.get('moderators'))
        if moderators:
            values[KEY_MODERATORS] = _ids_csv(set(moderators))
        return await import_legacy_values(db, cls._legacy_path, values)
