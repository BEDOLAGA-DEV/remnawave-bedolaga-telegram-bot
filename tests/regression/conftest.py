"""Shared fixtures for regression tests."""
import sys
import types

import pytest


# Global tests/conftest.py stubs `redis` + `redis.asyncio` but not the
# `redis.exceptions` submodule. Some app modules (e.g. app.utils.cache)
# import `from redis.exceptions import NoScriptError` at top level, which
# crashes collection of any regression test that pulls in app code through
# that chain. Stub it here so the regression suite is self-sufficient.
if 'redis.exceptions' not in sys.modules:
    _redis_exc = types.ModuleType('redis.exceptions')

    class NoScriptError(Exception):
        """Stub for redis.exceptions.NoScriptError (regression-suite only)."""

    _redis_exc.NoScriptError = NoScriptError
    sys.modules['redis.exceptions'] = _redis_exc

    # Make sure parent `redis` package exposes the submodule too — some
    # importers do `import redis.exceptions` rather than `from … import …`.
    _redis = sys.modules.get('redis')
    if _redis is not None and not hasattr(_redis, 'exceptions'):
        _redis.exceptions = _redis_exc


# Note: `pytest_plugins` is intentionally not declared here. pytest 9
# forbids `pytest_plugins` in non-top-level conftest files, and
# pytest-asyncio is auto-discovered via its entry point so an explicit
# declaration is unnecessary.
