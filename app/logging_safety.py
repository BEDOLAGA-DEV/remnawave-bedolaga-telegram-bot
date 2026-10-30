"""Safe serialization at log output boundaries; never mutate live exceptions."""

from __future__ import annotations

import re
import traceback
from collections.abc import Mapping
from typing import Any

import aiohttp
import httpx


_SENSITIVE_KEY = re.compile(
    r'^(?:authorization|proxy.authorization|x.secret|x.merchantid|.*(?:api.?key|token|password|secret))$', re.IGNORECASE
)
# Defense for already interpolated header dictionaries / exception repr strings.
_HEADER_VALUE = re.compile(
    r"""(?i)(['"]?(?:authorization|proxy-authorization|x-secret|x-merchantid|api[_-]?key)['"]?\s*[:=]\s*)(['"])(.*?)\2"""
)


def redact_text(text: str) -> str:
    return _HEADER_VALUE.sub(lambda match: f'{match[1]}{match[2]}[REDACTED]{match[2]}', text)


def safe_exception_summary(error: BaseException) -> str:
    if isinstance(error, (aiohttp.ClientError, httpx.HTTPError)):
        status = getattr(error, 'status', None)
        suffix = f' (status={status})' if isinstance(status, int) else ''
        return f'{type(error).__name__}{suffix}'
    return redact_text(str(error))


def safe_value(value: Any, *, depth: int = 0) -> Any:
    """Copy containers so nested exceptions cannot fall back to credential-bearing repr."""
    if depth >= 12:
        return '[nested value omitted]'
    if isinstance(value, BaseException):
        return safe_exception_summary(value)
    if isinstance(value, aiohttp.RequestInfo):
        return '[HTTP request details omitted]'
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            str(key): '[REDACTED]' if _SENSITIVE_KEY.fullmatch(str(key)) else safe_value(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list, set, frozenset)):
        return [safe_value(item, depth=depth + 1) for item in value]
    return value


def safe_event(event: dict[str, Any]) -> dict[str, Any]:
    # exc_info remains raw for internal exception classification, never for rendering.
    return {
        key: value if key in {'exc_info', '_record', '_from_structlog'} else safe_value({key: value})[key]
        for key, value in event.items()
    }


def safe_traceback(exc_info: tuple) -> str:
    """Keep frames, causes and exception groups, without HTTP request metadata."""
    rendered = traceback.TracebackException(*exc_info, capture_locals=False)
    seen: set[int] = set()

    def clean(node: traceback.TracebackException, error: BaseException) -> None:
        if id(node) in seen:
            return
        seen.add(id(node))
        # TracebackException has no public setter for the captured exception text.
        # Change only this detached rendering object, not the original exception.
        node._str = safe_exception_summary(error)
        if node.__notes__:
            node.__notes__ = [redact_text(note) for note in node.__notes__]
        for attr in ('__cause__', '__context__'):
            child, original = getattr(node, attr), getattr(error, attr)
            if child is not None and original is not None:
                clean(child, original)
        if isinstance(error, BaseExceptionGroup):
            for child, original in zip(node.exceptions or (), error.exceptions, strict=True):
                clean(child, original)

    if exc_info[1] is not None:
        clean(rendered, exc_info[1])
    return redact_text(''.join(rendered.format()))


def sanitize_log_event(logger: Any, method_name: str, event: dict[str, Any]) -> dict[str, Any]:
    return safe_event(event)


def render_safe_exception(logger: Any, method_name: str, event: dict[str, Any]) -> dict[str, Any]:
    exc_info = event.pop('exc_info', None)
    if isinstance(exc_info, BaseException):
        exc_info = (type(exc_info), exc_info, exc_info.__traceback__)
    if isinstance(exc_info, tuple) and exc_info[0] is not None:
        event['exception'] = safe_traceback(exc_info)
    return safe_event(event)
