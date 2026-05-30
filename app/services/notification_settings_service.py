import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


class NotificationSettingsService:
    """Runtime-editable notification settings stored on disk."""

    _storage_path: Path = Path('data/notification_settings.json')
    _data: dict[str, dict[str, Any]] = {}
    _loaded: bool = False

    _DEFAULTS: dict[str, dict[str, Any]] = {
        'trial_channel_unsubscribed': {'enabled': True},
        'expired_1d': {'enabled': True},
        'expired_second_wave': {
            'enabled': True,
            'discount_percent': 10,
            'valid_hours': 24,
        },
        'expired_third_wave': {
            'enabled': True,
            'discount_percent': 20,
            'valid_hours': 24,
            'trigger_days': 5,
        },
        'prerenew_save': {
            'enabled': False,
            'discount_percent': 15,
            'valid_hours': 24,
            'trigger_hours': 36,
        },
    }

    @classmethod
    def _ensure_dir(cls) -> None:
        try:
            cls._storage_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # pragma: no cover - filesystem guard
            logger.error('Failed to create notification settings dir', exc=exc)

    @classmethod
    def _load(cls) -> None:
        if cls._loaded:
            return

        cls._ensure_dir()
        try:
            if cls._storage_path.exists():
                raw = cls._storage_path.read_text(encoding='utf-8')
                cls._data = json.loads(raw) if raw.strip() else {}
            else:
                cls._data = {}
        except Exception as exc:
            logger.error('Failed to load notification settings', exc=exc)
            cls._data = {}

        changed = cls._apply_defaults()
        if changed:
            cls._save()
        cls._loaded = True

    @classmethod
    def _apply_defaults(cls) -> bool:
        changed = False
        for key, defaults in cls._DEFAULTS.items():
            current = cls._data.get(key)
            if not isinstance(current, dict):
                cls._data[key] = deepcopy(defaults)
                changed = True
                continue

            for def_key, def_value in defaults.items():
                if def_key not in current:
                    current[def_key] = def_value
                    changed = True
        return changed

    @classmethod
    def _save(cls) -> bool:
        cls._ensure_dir()
        try:
            cls._storage_path.write_text(
                json.dumps(cls._data, ensure_ascii=False, indent=2),
                encoding='utf-8',
            )
            return True
        except Exception as exc:
            logger.error('Failed to save notification settings', exc=exc)
            return False

    @classmethod
    def _get(cls, key: str) -> dict[str, Any]:
        cls._load()
        value = cls._data.get(key)
        if not isinstance(value, dict):
            value = deepcopy(cls._DEFAULTS.get(key, {}))
            cls._data[key] = value
        return value

    @classmethod
    def get_config(cls) -> dict[str, dict[str, Any]]:
        cls._load()
        return deepcopy(cls._data)

    @classmethod
    def _set_field(cls, key: str, field: str, value: Any) -> bool:
        cls._load()
        section = cls._get(key)
        section[field] = value
        cls._data[key] = section
        return cls._save()

    @classmethod
    def set_enabled(cls, key: str, enabled: bool) -> bool:
        return cls._set_field(key, 'enabled', bool(enabled))

    @classmethod
    def is_enabled(cls, key: str) -> bool:
        return bool(cls._get(key).get('enabled', True))

    @classmethod
    def is_trial_channel_unsubscribed_enabled(cls) -> bool:
        return cls.is_enabled('trial_channel_unsubscribed')

    @classmethod
    def set_trial_channel_unsubscribed_enabled(cls, enabled: bool) -> bool:
        return cls.set_enabled('trial_channel_unsubscribed', enabled)

    # Expired subscription notifications
    @classmethod
    def is_expired_1d_enabled(cls) -> bool:
        return cls.is_enabled('expired_1d')

    @classmethod
    def set_expired_1d_enabled(cls, enabled: bool) -> bool:
        return cls.set_enabled('expired_1d', enabled)

    @classmethod
    def is_second_wave_enabled(cls) -> bool:
        return cls.is_enabled('expired_second_wave')

    @classmethod
    def set_second_wave_enabled(cls, enabled: bool) -> bool:
        return cls.set_enabled('expired_second_wave', enabled)

    @classmethod
    def get_second_wave_discount_percent(cls) -> int:
        value = cls._get('expired_second_wave').get('discount_percent', 10)
        try:
            return max(0, min(100, int(value)))
        except (TypeError, ValueError):
            return 10

    @classmethod
    def set_second_wave_discount_percent(cls, percent: int) -> bool:
        try:
            percent_int = max(0, min(100, int(percent)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('expired_second_wave', 'discount_percent', percent_int)

    @classmethod
    def get_second_wave_valid_hours(cls) -> int:
        value = cls._get('expired_second_wave').get('valid_hours', 24)
        try:
            return max(1, min(168, int(value)))
        except (TypeError, ValueError):
            return 24

    @classmethod
    def set_second_wave_valid_hours(cls, hours: int) -> bool:
        try:
            hours_int = max(1, min(168, int(hours)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('expired_second_wave', 'valid_hours', hours_int)

    @classmethod
    def is_prerenew_save_enabled(cls) -> bool:
        return cls.is_enabled('prerenew_save')

    @classmethod
    def set_prerenew_save_enabled(cls, enabled: bool) -> bool:
        return cls.set_enabled('prerenew_save', enabled)

    @classmethod
    def get_prerenew_save_discount_percent(cls) -> int:
        value = cls._get('prerenew_save').get('discount_percent', 15)
        try:
            return max(0, min(100, int(value)))
        except (TypeError, ValueError):
            return 15

    @classmethod
    def set_prerenew_save_discount_percent(cls, percent: int) -> bool:
        try:
            percent_int = max(0, min(100, int(percent)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('prerenew_save', 'discount_percent', percent_int)

    @classmethod
    def get_prerenew_save_valid_hours(cls) -> int:
        value = cls._get('prerenew_save').get('valid_hours', 24)
        try:
            return max(1, min(168, int(value)))
        except (TypeError, ValueError):
            return 24

    @classmethod
    def set_prerenew_save_valid_hours(cls, hours: int) -> bool:
        try:
            hours_int = max(1, min(168, int(hours)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('prerenew_save', 'valid_hours', hours_int)

    @classmethod
    def get_prerenew_save_trigger_hours(cls) -> int:
        value = cls._get('prerenew_save').get('trigger_hours', 36)
        try:
            return max(1, min(168, int(value)))
        except (TypeError, ValueError):
            return 36

    @classmethod
    def set_prerenew_save_trigger_hours(cls, hours: int) -> bool:
        try:
            hours_int = max(1, min(168, int(hours)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('prerenew_save', 'trigger_hours', hours_int)

    @classmethod
    def is_third_wave_enabled(cls) -> bool:
        return cls.is_enabled('expired_third_wave')

    @classmethod
    def set_third_wave_enabled(cls, enabled: bool) -> bool:
        return cls.set_enabled('expired_third_wave', enabled)

    @classmethod
    def get_third_wave_discount_percent(cls) -> int:
        value = cls._get('expired_third_wave').get('discount_percent', 20)
        try:
            return max(0, min(100, int(value)))
        except (TypeError, ValueError):
            return 20

    @classmethod
    def set_third_wave_discount_percent(cls, percent: int) -> bool:
        try:
            percent_int = max(0, min(100, int(percent)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('expired_third_wave', 'discount_percent', percent_int)

    @classmethod
    def get_third_wave_valid_hours(cls) -> int:
        value = cls._get('expired_third_wave').get('valid_hours', 24)
        try:
            return max(1, min(168, int(value)))
        except (TypeError, ValueError):
            return 24

    @classmethod
    def set_third_wave_valid_hours(cls, hours: int) -> bool:
        try:
            hours_int = max(1, min(168, int(hours)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('expired_third_wave', 'valid_hours', hours_int)

    @classmethod
    def get_third_wave_trigger_days(cls) -> int:
        value = cls._get('expired_third_wave').get('trigger_days', 5)
        try:
            return max(2, min(60, int(value)))
        except (TypeError, ValueError):
            return 5

    @classmethod
    def set_third_wave_trigger_days(cls, days: int) -> bool:
        try:
            days_int = max(2, min(60, int(days)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('expired_third_wave', 'trigger_days', days_int)

    @classmethod
    def are_notifications_globally_enabled(cls) -> bool:
        return bool(getattr(settings, 'ENABLE_NOTIFICATIONS', True))
