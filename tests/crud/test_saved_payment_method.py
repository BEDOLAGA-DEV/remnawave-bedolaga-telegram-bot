"""Тесты CRUD сохранённых платёжных методов (provider-agnostic)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.database.crud.saved_payment_method import (
    DEFAULT_PROVIDER,
    _resolve_provider_args,
)


def test_resolve_provider_args_explicit() -> None:
    provider, token = _resolve_provider_args('etoplatezhi', '1079', None)
    assert provider == 'etoplatezhi'
    assert token == '1079'


def test_resolve_provider_args_defaults_to_yookassa() -> None:
    provider, token = _resolve_provider_args(None, 'pm_abc', None)
    assert provider == DEFAULT_PROVIDER == 'yookassa'
    assert token == 'pm_abc'


def test_resolve_provider_args_legacy_yookassa_kwarg() -> None:
    """Existing callers that still pass ``yookassa_payment_method_id`` must
    keep working — it is treated as a YooKassa token."""
    provider, token = _resolve_provider_args(None, None, 'pm_legacy')
    assert provider == 'yookassa'
    assert token == 'pm_legacy'


def test_resolve_provider_args_legacy_kwarg_with_explicit_provider_name() -> None:
    """If a caller passes both an explicit provider and only the legacy id, the
    explicit provider wins (lets future callers say ``provider='etoplatezhi'``
    with a raw token)."""
    provider, token = _resolve_provider_args('etoplatezhi', None, 'recurring_1')
    assert provider == 'etoplatezhi'
    assert token == 'recurring_1'


def test_resolve_provider_args_rejects_missing_token() -> None:
    with pytest.raises(ValueError):
        _resolve_provider_args(None, None, None)
