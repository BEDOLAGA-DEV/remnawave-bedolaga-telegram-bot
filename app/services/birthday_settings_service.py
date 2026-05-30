import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import structlog


logger = structlog.get_logger(__name__)

_REWARD_TYPES = ('balance', 'subscription_days', 'promocode')
_FALLBACKS = ('balance', 'skip')


class BirthdaySettingsService:
    """Runtime-editable birthday-bonus settings stored on disk."""

    _storage_path: Path = Path('data/birthday_settings.json')
    _data: dict[str, Any] = {}
    _loaded: bool = False

    _DEFAULTS: dict[str, Any] = {
        'birthday_bonus': {
            'enabled': False,
            'reward_type': 'balance',
            'reward_amount': 10000,
            'promocode_valid_days': 7,
            'min_account_age_days': 7,
            'dob_stable_days': 7,
            'subscription_days_fallback': 'balance',
        }
    }

    @classmethod
    def _ensure_dir(cls) -> None:
        try:
            cls._storage_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # pragma: no cover
            logger.error('birthday_settings.mkdir_failed', exc=exc)

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
            logger.error('birthday_settings.load_failed', exc=exc)
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
            logger.error('birthday_settings.save_failed', exc=exc)
            return False

    @classmethod
    def _get(cls) -> dict[str, Any]:
        cls._load()
        value = cls._data.get('birthday_bonus')
        if not isinstance(value, dict):
            value = deepcopy(cls._DEFAULTS['birthday_bonus'])
            cls._data['birthday_bonus'] = value
        return value

    @classmethod
    def _set_field(cls, field: str, value: Any) -> bool:
        cls._load()
        section = cls._get()
        section[field] = value
        cls._data['birthday_bonus'] = section
        return cls._save()

    @classmethod
    def get_config(cls) -> dict[str, Any]:
        cls._load()
        return deepcopy(cls._get())

    @classmethod
    def is_enabled(cls) -> bool:
        return bool(cls._get().get('enabled', False))

    @classmethod
    def set_enabled(cls, enabled: bool) -> bool:
        return cls._set_field('enabled', bool(enabled))

    @classmethod
    def get_reward_type(cls) -> str:
        value = cls._get().get('reward_type', 'balance')
        return value if value in _REWARD_TYPES else 'balance'

    @classmethod
    def set_reward_type(cls, value: str) -> bool:
        if value not in _REWARD_TYPES:
            return False
        return cls._set_field('reward_type', value)

    @classmethod
    def get_reward_amount(cls) -> int:
        try:
            return max(0, int(cls._get().get('reward_amount', 10000)))
        except (TypeError, ValueError):
            return 10000

    @classmethod
    def set_reward_amount(cls, value: int) -> bool:
        try:
            v = int(value)
        except (TypeError, ValueError):
            return False
        if v < 0:
            return False
        return cls._set_field('reward_amount', v)

    @classmethod
    def get_promocode_valid_days(cls) -> int:
        try:
            return max(1, min(365, int(cls._get().get('promocode_valid_days', 7))))
        except (TypeError, ValueError):
            return 7

    @classmethod
    def set_promocode_valid_days(cls, value: int) -> bool:
        try:
            v = max(1, min(365, int(value)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('promocode_valid_days', v)

    @classmethod
    def get_min_account_age_days(cls) -> int:
        try:
            return max(0, min(365, int(cls._get().get('min_account_age_days', 7))))
        except (TypeError, ValueError):
            return 7

    @classmethod
    def set_min_account_age_days(cls, value: int) -> bool:
        try:
            v = max(0, min(365, int(value)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('min_account_age_days', v)

    @classmethod
    def get_dob_stable_days(cls) -> int:
        try:
            return max(0, min(365, int(cls._get().get('dob_stable_days', 7))))
        except (TypeError, ValueError):
            return 7

    @classmethod
    def set_dob_stable_days(cls, value: int) -> bool:
        try:
            v = max(0, min(365, int(value)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('dob_stable_days', v)

    @classmethod
    def get_subscription_days_fallback(cls) -> str:
        value = cls._get().get('subscription_days_fallback', 'balance')
        return value if value in _FALLBACKS else 'balance'

    @classmethod
    def set_subscription_days_fallback(cls, value: str) -> bool:
        if value not in _FALLBACKS:
            return False
        return cls._set_field('subscription_days_fallback', value)
