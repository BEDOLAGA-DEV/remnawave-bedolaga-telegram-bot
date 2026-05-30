import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import structlog


logger = structlog.get_logger(__name__)


class FreezeSettingsService:
    """Runtime-editable subscription-freeze settings stored on disk."""

    _storage_path: Path = Path('data/freeze_settings.json')
    _data: dict[str, Any] = {}
    _loaded: bool = False

    _DEFAULTS: dict[str, Any] = {
        'subscription_freeze': {
            'enabled': False,
            'max_days_per_year': 30,
            'min_subscription_age_days': 7,
            'cooldown_days': 7,
            'min_freeze_days': 3,
            'max_single_freeze_days': 30,
        }
    }

    @classmethod
    def _ensure_dir(cls) -> None:
        try:
            cls._storage_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # pragma: no cover
            logger.error('freeze_settings.mkdir_failed', exc=exc)

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
            logger.error('freeze_settings.load_failed', exc=exc)
            cls._data = {}
        if cls._apply_defaults():
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
            for dk, dv in defaults.items():
                if dk not in current:
                    current[dk] = dv
                    changed = True
        return changed

    @classmethod
    def _save(cls) -> bool:
        cls._ensure_dir()
        try:
            cls._storage_path.write_text(
                json.dumps(cls._data, ensure_ascii=False, indent=2), encoding='utf-8'
            )
            return True
        except Exception as exc:
            logger.error('freeze_settings.save_failed', exc=exc)
            return False

    @classmethod
    def _get(cls) -> dict[str, Any]:
        cls._load()
        value = cls._data.get('subscription_freeze')
        if not isinstance(value, dict):
            value = deepcopy(cls._DEFAULTS['subscription_freeze'])
            cls._data['subscription_freeze'] = value
        return value

    @classmethod
    def _set_field(cls, field: str, value: Any) -> bool:
        cls._load()
        section = cls._get()
        section[field] = value
        cls._data['subscription_freeze'] = section
        return cls._save()

    @classmethod
    def get_config(cls) -> dict[str, Any]:
        cls._load()
        return deepcopy(cls._get())

    # --- enabled ---

    @classmethod
    def is_enabled(cls) -> bool:
        return bool(cls._get().get('enabled', False))

    @classmethod
    def set_enabled(cls, enabled: bool) -> bool:
        return cls._set_field('enabled', bool(enabled))

    # --- max_days_per_year ---

    @classmethod
    def get_max_days_per_year(cls) -> int:
        try:
            return max(0, min(365, int(cls._get().get('max_days_per_year', 30))))
        except (TypeError, ValueError):
            return 30

    @classmethod
    def set_max_days_per_year(cls, value: int) -> bool:
        try:
            v = max(0, min(365, int(value)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('max_days_per_year', v)

    # --- min_subscription_age_days ---

    @classmethod
    def get_min_subscription_age_days(cls) -> int:
        try:
            return max(0, min(365, int(cls._get().get('min_subscription_age_days', 7))))
        except (TypeError, ValueError):
            return 7

    @classmethod
    def set_min_subscription_age_days(cls, value: int) -> bool:
        try:
            v = max(0, min(365, int(value)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('min_subscription_age_days', v)

    # --- cooldown_days ---

    @classmethod
    def get_cooldown_days(cls) -> int:
        try:
            return max(0, min(365, int(cls._get().get('cooldown_days', 7))))
        except (TypeError, ValueError):
            return 7

    @classmethod
    def set_cooldown_days(cls, value: int) -> bool:
        try:
            v = max(0, min(365, int(value)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('cooldown_days', v)

    # --- min_freeze_days ---

    @classmethod
    def get_min_freeze_days(cls) -> int:
        try:
            return max(1, min(365, int(cls._get().get('min_freeze_days', 3))))
        except (TypeError, ValueError):
            return 3

    @classmethod
    def set_min_freeze_days(cls, value: int) -> bool:
        try:
            v = max(1, min(365, int(value)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('min_freeze_days', v)

    # --- max_single_freeze_days ---

    @classmethod
    def get_max_single_freeze_days(cls) -> int:
        try:
            return max(1, min(365, int(cls._get().get('max_single_freeze_days', 30))))
        except (TypeError, ValueError):
            return 30

    @classmethod
    def set_max_single_freeze_days(cls, value: int) -> bool:
        try:
            v = max(1, min(365, int(value)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('max_single_freeze_days', v)
