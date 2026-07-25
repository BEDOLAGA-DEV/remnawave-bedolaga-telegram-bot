from typing import Any

from sqlalchemy import select

from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.db.models import RuntimeSetting


_DEFAULTS: dict[str, Any] = {}
_cache: dict[str, str] = {}


def _base_defaults() -> dict[str, Any]:
    return {
        'SYSTEM_PROMPT': settings.SYSTEM_PROMPT,
        'MODEL': settings.MODEL,
        'EMBEDDING_MODEL': settings.EMBEDDING_MODEL,
        'MAX_TOKENS': str(settings.MAX_TOKENS),
        'TEMPERATURE': str(settings.TEMPERATURE),
        'TOP_K': str(settings.TOP_K),
        'MIN_SCORE': str(settings.MIN_SCORE),
        'CONTEXT_MESSAGES': str(settings.CONTEXT_MESSAGES),
        'HISTORY_LIMIT': str(settings.HISTORY_LIMIT),
        'VISION_ENABLED': '1' if settings.VISION_ENABLED else '0',
        'INCLUDE_REMNAWAVE_DATA': '1' if settings.INCLUDE_REMNAWAVE_DATA else '0',
    }


async def load() -> None:
    global _DEFAULTS
    _DEFAULTS = _base_defaults()
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(RuntimeSetting))
        for row in result.scalars().all():
            if row.value is not None:
                _cache[row.key] = row.value


def get(key: str) -> str:
    if key in _cache:
        return _cache[key]
    return str(_DEFAULTS.get(key, ''))


def get_int(key: str) -> int:
    try:
        return int(float(get(key)))
    except (ValueError, TypeError):
        return int(_DEFAULTS.get(key, 0) or 0)


def get_float(key: str) -> float:
    try:
        return float(get(key))
    except (ValueError, TypeError):
        return float(_DEFAULTS.get(key, 0.0) or 0.0)


def get_bool(key: str) -> bool:
    return get(key) in {'1', 'true', 'True', 'yes', 'on'}


async def set_value(key: str, value: str) -> None:
    async with AsyncSessionLocal() as session:
        existing = await session.get(RuntimeSetting, key)
        if existing:
            existing.value = value
        else:
            session.add(RuntimeSetting(key=key, value=value))
        await session.commit()
    _cache[key] = value


def all_settings() -> dict[str, str]:
    merged = {k: str(v) for k, v in _DEFAULTS.items()}
    merged.update(_cache)
    return merged
