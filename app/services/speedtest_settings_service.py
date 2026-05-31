import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import structlog


logger = structlog.get_logger(__name__)

_HOST_RE = re.compile(r'^[a-zA-Z0-9.-]+$')


def _sanitize_host(value) -> str | None:
    raw = (value or '').strip() if isinstance(value, str) else ''
    if not raw:
        return None
    if '://' in raw:
        raw = raw.split('://', 1)[1]
    raw = raw.split('/', 1)[0].split(':', 1)[0].split('?', 1)[0].strip()
    if not raw or not _HOST_RE.match(raw):
        return None
    return raw


class SpeedtestSettingsService:
    """Runtime-editable speedtest settings stored on disk."""

    _storage_path: Path = Path('data/speedtest_settings.json')
    _data: dict[str, Any] = {}
    _loaded: bool = False

    _DEFAULTS: dict[str, Any] = {'speedtest': {'enabled': False, 'host_mapping': {}}}

    @classmethod
    def _ensure_dir(cls) -> None:
        try:
            cls._storage_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # pragma: no cover
            logger.error('speedtest_settings.mkdir_failed', exc=exc)

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
            logger.error('speedtest_settings.load_failed', exc=exc)
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
            logger.error('speedtest_settings.save_failed', exc=exc)
            return False

    @classmethod
    def _get(cls) -> dict[str, Any]:
        cls._load()
        value = cls._data.get('speedtest')
        if not isinstance(value, dict):
            value = deepcopy(cls._DEFAULTS['speedtest'])
            cls._data['speedtest'] = value
        return value

    @classmethod
    def _set_field(cls, field: str, value: Any) -> bool:
        cls._load()
        section = cls._get()
        section[field] = value
        cls._data['speedtest'] = section
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

    # --- host_mapping ---

    @classmethod
    def get_host_mapping(cls) -> dict:
        value = cls._get().get('host_mapping', {})
        if not isinstance(value, dict):
            return {}
        return value

    @classmethod
    def set_host_mapping(cls, mapping) -> bool:
        if not isinstance(mapping, dict):
            return False
        cleaned: dict[str, str] = {}
        for k, v in mapping.items():
            h = _sanitize_host(v)
            if h:
                cleaned[str(k)] = h
        return cls._set_field('host_mapping', cleaned)
