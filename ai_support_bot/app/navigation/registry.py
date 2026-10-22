from __future__ import annotations

import asyncio
import time

import structlog

from ai_support_bot.app.navigation.builder import build_navigation_tree
from ai_support_bot.app.navigation.schema import NavTree


logger = structlog.get_logger(__name__)

DEFAULT_LANGUAGE = 'ru'

_trees: dict[str, NavTree] = {}
_locks: dict[str, asyncio.Lock] = {}
_global_lock = asyncio.Lock()


def _normalize(language: str | None) -> str:
    code = (language or DEFAULT_LANGUAGE).strip().lower()
    return code or DEFAULT_LANGUAGE


async def _lock_for(language: str) -> asyncio.Lock:
    async with _global_lock:
        lock = _locks.get(language)
        if lock is None:
            lock = asyncio.Lock()
            _locks[language] = lock
        return lock


def peek(language: str | None = None) -> NavTree | None:
    return _trees.get(_normalize(language))


def is_ready(language: str | None = None) -> bool:
    return _normalize(language) in _trees


def stats() -> dict[str, dict[str, object]]:
    return {
        code: {
            'nodes': tree.size,
            'sources': list(tree.sources),
            'built_at': tree.built_at,
        }
        for code, tree in _trees.items()
    }


async def refresh(language: str | None = None) -> NavTree:
    code = _normalize(language)
    lock = await _lock_for(code)
    async with lock:
        tree = await build_navigation_tree(code)
        _trees[code] = tree
        return tree


async def get_tree(language: str | None = None, ttl_seconds: int = 0) -> NavTree:
    code = _normalize(language)
    cached = _trees.get(code)
    if cached is not None:
        if ttl_seconds <= 0 or (time.time() - cached.built_at) < ttl_seconds:
            return cached
    try:
        return await refresh(code)
    except Exception as error:
        logger.warning('Navigation tree refresh failed', language=code, error=str(error))
        if cached is not None:
            return cached
        raise


async def warmup(languages: list[str] | None = None) -> None:
    codes = [_normalize(item) for item in (languages or [DEFAULT_LANGUAGE])]
    for code in dict.fromkeys(codes):
        try:
            await refresh(code)
        except Exception as error:
            logger.warning('Navigation tree warmup failed', language=code, error=str(error))


def reset() -> None:
    _trees.clear()
    _locks.clear()
