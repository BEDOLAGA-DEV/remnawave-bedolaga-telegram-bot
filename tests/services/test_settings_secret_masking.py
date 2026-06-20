"""Regression tests for settings-API secret masking.

The cabinet/web-API settings endpoints must never echo plaintext secrets (payment keys,
SMTP/panel passwords, API tokens) — but the name heuristic (TOKEN/SECRET/PASSWORD/KEY) also
matches non-secret *numeric* settings such as ``CABINET_ACCESS_TOKEN_EXPIRE_MINUTES`` and
``WATA_PUBLIC_KEY_CACHE_SECONDS``, which must stay visible and editable. These tests pin both
sides so a future change can't (a) leak a real string secret or (b) re-mask a numeric setting.
"""

from __future__ import annotations

import pytest

from app.services.system_settings_service import bot_configuration_service as svc


MASK = svc.SECRET_MASK


@pytest.mark.parametrize(
    'key',
    [
        'YOOKASSA_SECRET_KEY',
        'CRYPTOBOT_API_TOKEN',
        'SMTP_PASSWORD',
        'REMNAWAVE_API_KEY',
        'BOT_TOKEN',
        'OVERPAY_P12_PASSPHRASE',
    ],
)
def test_is_secret_key_matches_secret_names(key: str) -> None:
    assert svc.is_secret_key(key) is True


@pytest.mark.parametrize(
    'key',
    [
        'CHANNEL_LINK',
        'SUPPORT_USERNAME',
        'TRIAL_DURATION_DAYS',
        'DEFAULT_LANGUAGE',
    ],
)
def test_is_secret_key_ignores_plain_names(key: str) -> None:
    assert svc.is_secret_key(key) is False


def test_string_secret_is_masked() -> None:
    assert svc.is_masked_secret('YOOKASSA_SECRET_KEY', 'live_abc123') is True
    assert svc.mask_secret_value('YOOKASSA_SECRET_KEY', 'live_abc123') == MASK


def test_unset_secret_is_not_masked() -> None:
    # None / empty must pass through (renders as empty, not as the mask sentinel).
    assert svc.is_masked_secret('YOOKASSA_SECRET_KEY', None) is False
    assert svc.mask_secret_value('YOOKASSA_SECRET_KEY', None) is None
    assert svc.mask_secret_value('YOOKASSA_SECRET_KEY', '') == ''


@pytest.mark.parametrize(
    ('key', 'value'),
    [
        ('CABINET_ACCESS_TOKEN_EXPIRE_MINUTES', 30),
        ('CABINET_REFRESH_TOKEN_EXPIRE_DAYS', 7),
        ('CABINET_PASSWORD_RESET_EXPIRE_HOURS', 24),
        ('WATA_PUBLIC_KEY_CACHE_SECONDS', 3600),
    ],
)
def test_numeric_settings_with_secretish_names_are_not_masked(key: str, value: int) -> None:
    # Name matches the heuristic, but an int value must never be masked — it would hide and
    # (via the update skip-on-mask guard) freeze a legitimate numeric setting.
    assert svc.is_secret_key(key) is True
    assert svc.is_masked_secret(key, value) is False
    assert svc.mask_secret_value(key, value) == value


def test_no_nonstring_definition_value_is_ever_masked() -> None:
    """Sweep every real setting definition: a masked value must always be a string."""
    leaked_nonstring: list[tuple[str, object]] = []
    for category_key, _, _ in svc.get_categories():
        for definition in svc.get_settings_for_category(category_key):
            raw = svc.get_current_value(definition.key)
            if svc.is_masked_secret(definition.key, raw) and not isinstance(raw, str):
                leaked_nonstring.append((definition.key, raw))
    assert leaked_nonstring == []
