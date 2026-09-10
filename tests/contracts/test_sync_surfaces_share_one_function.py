"""Сторож: три синхронизации одинаковы в боте, кабинете и по расписанию.

Владелец: «есть три синхрона — полная, из панели в бота, из бота в панель; они должны
одинаково работать из кабинета и из бота, а синхрон по расписанию — это полная».
Раньше «Полная синхронизация» в боте и автосинхронизация делали только «из панели в
бота»: в панель не уезжало ничего, и тег тарифа там не появлялся.
"""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BOT_ADMIN = ROOT / 'app' / 'handlers' / 'admin' / 'remnawave.py'
BOT_USERS = ROOT / 'app' / 'handlers' / 'admin' / 'users.py'
CABINET_RW = ROOT / 'app' / 'cabinet' / 'routes' / 'admin_remnawave.py'
CABINET_USERS = ROOT / 'app' / 'cabinet' / 'routes' / 'admin_users.py'
AUTO_SYNC = ROOT / 'app' / 'services' / 'remnawave_sync_service.py'


def _function_source(path: Path, name: str) -> str:
    source = path.read_text(encoding='utf-8')
    tree = ast.parse(source)
    lines = source.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return '\n'.join(lines[node.lineno - 1 : node.end_lineno])
    raise AssertionError(f'{name} not found in {path}')


def test_full_sync_is_one_function_everywhere() -> None:
    assert 'perform_full_sync(' in _function_source(BOT_ADMIN, 'sync_all_users')
    assert 'perform_full_sync(' in _function_source(AUTO_SYNC, '_perform_sync')
    assert 'perform_full_sync(' in _function_source(CABINET_RW, 'sync_full')


def test_bot_full_sync_no_longer_imports_only() -> None:
    body = _function_source(BOT_ADMIN, 'sync_all_users')
    assert 'sync_users_from_panel(' not in body


def test_from_panel_and_to_panel_share_service_methods() -> None:
    assert 'sync_users_to_panel(' in _function_source(BOT_ADMIN, 'sync_users_to_panel')
    assert 'sync_users_to_panel(' in _function_source(CABINET_RW, 'sync_to_panel')
    assert 'sync_users_from_panel(' in _function_source(CABINET_RW, 'sync_from_panel')


def test_per_user_pushes_share_the_account_field_set() -> None:
    assert 'narrow_push_fields(' in _function_source(CABINET_USERS, 'sync_user_to_panel')
    assert 'PANEL_ACCOUNT_METADATA_FIELDS' in _function_source(BOT_USERS, '_push_narrow_change_to_panel')
