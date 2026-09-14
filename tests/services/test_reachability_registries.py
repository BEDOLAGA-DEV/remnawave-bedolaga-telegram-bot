"""Реестры, без которых раздел «Доступность из РФ» не соберётся.

Право, которого нет в PERMISSION_REGISTRY, редактор ролей отвергает с 400, а
настройка без категории не показывается в кабинете. Здесь закреплены имена и
дефолты интеграции: префикс BSCHEK_, категория BSCHEK, секция прав reachability.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.cabinet.routes.admin_roles import _validate_permissions
from app.config import settings
from app.services.permission_service import PERMISSION_REGISTRY, get_all_permissions
from app.services.rbac_bootstrap_service import _PRESET_ROLES
from app.services.system_settings_service import BotConfigurationService


registry = BotConfigurationService

BSCHEK_KEYS = (
    'BSCHEK_ENABLED',
    'BSCHEK_API_URL',
    'BSCHEK_API_KEY',
    'BSCHEK_REQUEST_TIMEOUT',
    'BSCHEK_REFERENCE_SUBSCRIPTION',
    'BSCHEK_JOB_COST_LIMIT_KOPEKS',
)


def test_permission_section_registered() -> None:
    assert PERMISSION_REGISTRY['reachability'] == ['read', 'run']


@pytest.mark.parametrize('permission', ['reachability:read', 'reachability:run'])
def test_permission_is_grantable(permission: str) -> None:
    assert permission in get_all_permissions()
    _validate_permissions([permission])


def test_wildcard_from_bootstrap_survives_a_role_save() -> None:
    _validate_permissions(['reachability:*'])


def test_admin_preset_gets_wildcard() -> None:
    admin = next(role for role in _PRESET_ROLES if role['name'] == 'Admin')
    assert 'reachability:*' in admin['permissions']


def test_settings_defaults() -> None:
    assert settings.BSCHEK_REQUEST_TIMEOUT == 200
    assert settings.BSCHEK_JOB_COST_LIMIT_KOPEKS == 0
    assert settings.get_bschek_api_url() == 'https://bsbord.com/v1'
    assert settings.is_bschek_configured() is bool(settings.BSCHEK_API_KEY)


@pytest.mark.parametrize('key', BSCHEK_KEYS)
def test_settings_land_in_bschek_category(key: str) -> None:
    assert registry.get_definition(key).category_key == 'BSCHEK'


def test_category_has_title_and_description() -> None:
    assert 'BSCHEK' in registry.CATEGORY_TITLES
    assert 'BSCHEK' in registry.CATEGORY_DESCRIPTIONS


def test_api_key_is_masked_and_numbers_are_not() -> None:
    assert registry.is_masked_secret('BSCHEK_API_KEY', 'bsk_live_x') is True
    assert registry.is_masked_secret('BSCHEK_JOB_COST_LIMIT_KOPEKS', 0) is False
    assert registry.is_masked_secret('BSCHEK_REQUEST_TIMEOUT', 200) is False


def test_env_example_block_is_commented_out() -> None:
    """Раскомментированный BSCHEK_* в .env затеняет значение, заданное из кабинета."""
    text = Path('.env.example').read_text(encoding='utf-8')
    for key in BSCHEK_KEYS:
        assert f'# {key}=' in text, key
        assert f'\n{key}=' not in text, key
