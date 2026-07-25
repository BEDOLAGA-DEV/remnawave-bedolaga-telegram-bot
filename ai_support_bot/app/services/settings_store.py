from typing import Any

from sqlalchemy import select

from ai_support_bot.app.core.config import settings
from ai_support_bot.app.db.database import AsyncSessionLocal
from ai_support_bot.app.db.models import RuntimeSetting


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
        'DAILY_MESSAGE_LIMIT': str(settings.DAILY_MESSAGE_LIMIT),
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
    global _DEFAULTS
    if not _DEFAULTS:
        _DEFAULTS = _base_defaults()
    val = _cache.get(key)
    if val is not None and val.strip():
        return val.strip()
    return str(_DEFAULTS.get(key, '') or '')


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
    cleaned = value.strip() if value else ''
    async with AsyncSessionLocal() as session:
        existing = await session.get(RuntimeSetting, key)
        if cleaned:
            if existing:
                existing.value = cleaned
            else:
                session.add(RuntimeSetting(key=key, value=cleaned))
        elif existing:
            await session.delete(existing)
        await session.commit()
    if cleaned:
        _cache[key] = cleaned
    else:
        _cache.pop(key, None)


def all_settings() -> dict[str, str]:
    merged = {k: str(v) for k, v in _base_defaults().items()}
    for k, v in _cache.items():
        if v and v.strip():
            merged[k] = v.strip()
    return merged
