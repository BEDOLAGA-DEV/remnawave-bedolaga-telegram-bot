"""Пакет административного веб-API."""

from typing import Any


__all__ = ['WebAPIServer', 'create_web_api_app']


def __getattr__(name: str) -> Any:
    if name == 'create_web_api_app':
        from .app import create_web_api_app

        return create_web_api_app
    if name == 'WebAPIServer':
        from .server import WebAPIServer

        return WebAPIServer
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
