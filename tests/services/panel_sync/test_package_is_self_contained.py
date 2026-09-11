"""Пакет panel_sync не зависит от сервиса подписок.

Репорт с 4.9.1: смена лимита устройств в админке падала с ImportError на строке
``from app.services.subscription_service import link_subscription_panel_identity``
внутри ``writer._record_identity``. На чистом коде функция есть, но пакет,
объявленный единственным местом правил синхронизации, двумя ленивыми импортами
лез обратно в сервис на 76 КБ — и любое расхождение в том файле (локальная
правка, частичный деплой, заглушка модуля в тестах) роняло запись в панель уже
ПОСЛЕ успешного PATCH: аккаунт обновлён, а связь строки с ним потеряна.

Правила пакета живут в пакете. Сторож — по разбору кода, на любой глубине.
"""

from __future__ import annotations

import ast
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


PACKAGE = Path(__file__).resolve().parents[3] / 'app' / 'services' / 'panel_sync'
FORBIDDEN_MODULE = 'app.services.subscription_service'


def _imports_of(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or '').startswith(FORBIDDEN_MODULE):
            found.append((node.lineno, node.module or ''))
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names if alias.name.startswith(FORBIDDEN_MODULE))
    return found


def test_no_module_in_panel_sync_imports_subscription_service():
    """Ни на уровне модуля, ни лениво внутри функций."""
    offenders = []
    for path in sorted(PACKAGE.glob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        offenders.extend(f'{path.name}:{lineno} ({module})' for lineno, module in _imports_of(tree))
    assert not offenders, f'panel_sync тянет сервис подписок: {offenders}'


@pytest.mark.asyncio
async def test_record_identity_links_row_even_without_subscription_service(monkeypatch):
    """Сценарий репорта: модуль сервиса на месте, но нужного имени в нём нет — связь всё равно пишется."""
    from app.services.panel_sync import writer

    monkeypatch.setitem(sys.modules, FORBIDDEN_MODULE, types.ModuleType(FORBIDDEN_MODULE))

    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    user = SimpleNamespace(remnawave_id=777)
    subscription = SimpleNamespace(
        id=1,
        remnawave_id=None,
        remnawave_short_uuid=None,
        subscription_url=None,
        subscription_crypto_link=None,
    )
    panel_user = SimpleNamespace(
        id=777, short_uuid='abc', subscription_url='https://sub.example/u', happ_crypto_link=None
    )

    await writer._record_identity(db, user, subscription, panel_user, multi_tariff=False)

    assert subscription.remnawave_id == 777
    assert subscription.remnawave_short_uuid == 'abc'


def test_subscription_service_reexports_moved_helpers():
    """Внешний код, импортировавший помощники из сервиса подписок, получает те же объекты."""
    from app.services import subscription_service
    from app.services.panel_sync import identity, traffic_strategy

    assert subscription_service.get_traffic_reset_strategy is traffic_strategy.get_traffic_reset_strategy
    assert subscription_service.link_subscription_panel_identity is identity.link_subscription_panel_identity
    assert subscription_service.panel_id_is_free_for is identity.panel_id_is_free_for
