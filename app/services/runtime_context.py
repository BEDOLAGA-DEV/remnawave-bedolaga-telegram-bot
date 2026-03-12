from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


_current_bot_id: ContextVar[int | None] = ContextVar('current_bot_id', default=None)


def get_current_bot_id() -> int | None:
    return _current_bot_id.get()


@contextmanager
def bind_current_bot_id(bot_id: int | None) -> Iterator[None]:
    token = _current_bot_id.set(bot_id)
    try:
        yield
    finally:
        _current_bot_id.reset(token)
