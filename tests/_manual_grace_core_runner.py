"""Dependency-free local runner for the pure grace state-machine tests."""

from __future__ import annotations

import asyncio
import inspect
import re
import runpy
import sys
import tempfile
import types
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _Raises:
    def __init__(self, expected, match: str | None = None) -> None:
        self.expected = expected
        self.match = match

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, _traceback) -> bool:
        if exc_type is None or not issubclass(exc_type, self.expected):
            raise AssertionError(f'Expected {self.expected}, got {exc_type}')
        if self.match and not re.search(self.match, str(exc)):
            raise AssertionError(f'{exc!s} does not match {self.match!r}')
        return True


class _Mark:
    @property
    def asyncio(self):
        return lambda function: function


pytest_stub = types.ModuleType('pytest')
pytest_stub.mark = _Mark()
pytest_stub.raises = lambda expected, match=None: _Raises(expected, match)
sys.modules.setdefault('pytest', pytest_stub)


class _Logger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


structlog_stub = types.ModuleType('structlog')
structlog_stub.get_logger = lambda *_args, **_kwargs: _Logger()
sys.modules.setdefault('structlog', structlog_stub)


namespace = runpy.run_path('tests/services/test_grace_access_service.py')
tests = sorted((name, value) for name, value in namespace.items() if name.startswith('test_') and callable(value))
sqlite_namespace = runpy.run_path('tests/services/test_grace_access_sqlite_safety.py')


async def _run() -> None:
    for name, function in tests:
        result = function()
        if inspect.isawaitable(result):
            await result
        print(f'PASS {name}')
    print(f'PASS {len(tests)} grace core scenarios')

    sqlite_tests = (
        'test_sqlite_delete_guard_preserves_open_snapshot_and_cascades_completed_history',
        'test_sqlite_predelete_noop_write_blocks_a_concurrent_pending_insert',
        'test_sqlite_user_lock_blocks_a_new_subscription_during_full_delete',
        'test_sqlite_delete_guard_also_blocks_user_cascade',
    )
    for name in sqlite_tests:
        with tempfile.TemporaryDirectory() as directory:
            sqlite_namespace[name](Path(directory))
        print(f'PASS {name}')
    print(f'PASS {len(sqlite_tests)} SQLite safety scenarios')


asyncio.run(_run())
