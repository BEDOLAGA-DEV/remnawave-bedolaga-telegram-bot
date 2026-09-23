"""Модули напоминаний не должны дотягиваться импортами до мониторинга.

Мониторинг запускает проход напоминаний, а сам достижим почти из любого модуля
(crud.subscription → payment.platega → monitoring_service). Любой импорт отсюда в
CRUD или сервисы замыкает кольцо — CodeQL py/cyclic-import на PR #3280. Учитываются
и импорты внутри функций: CodeQL их тоже считает.
"""

from __future__ import annotations

import ast
from collections import deque
from functools import cache
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
MONITORING = 'app.services.monitoring_service'


def _module_name(path: Path) -> str:
    name = '.'.join(path.relative_to(ROOT).with_suffix('').parts)
    return name.removesuffix('.__init__')


@cache
def _graph() -> dict[str, set[str]]:
    files = {_module_name(path): path for path in (ROOT / 'app').rglob('*.py')}
    graph: dict[str, set[str]] = {}
    for module, path in files.items():
        package = module if path.name == '__init__.py' else module.rpartition('.')[0]
        edges: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ''
                if node.level:
                    parts = package.split('.')[: len(package.split('.')) - node.level + 1]
                    base = '.'.join([*parts, base] if base else parts)
                targets = [base, *(f'{base}.{alias.name}' for alias in node.names)]
            else:
                continue
            edges.update(target for target in targets if target in files and target != module)
        graph[module] = edges
    return graph


def _path(graph: dict[str, set[str]], start: str, goal: str) -> list[str] | None:
    previous: dict[str, str | None] = {start: None}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        if current == goal:
            chain = [goal]
            while previous[chain[-1]] is not None:
                chain.append(previous[chain[-1]])
            return chain[::-1]
        for nxt in graph.get(current, ()):
            if nxt not in previous:
                previous[nxt] = current
                queue.append(nxt)
    return None


@pytest.mark.parametrize(
    'module',
    [
        'app.services.user_reminders.conditions',
        'app.services.user_reminders.dispatcher',
        'app.services.user_reminders.texts',
        'app.database.crud.user_reminder',
        'app.database.auth_methods',
        'app.database.constants',
    ],
)
def test_reminder_modules_do_not_reach_monitoring(module: str) -> None:
    chain = _path(_graph(), module, MONITORING)
    assert chain is None, 'кольцо импортов: ' + ' → '.join(chain or [])


@pytest.mark.asyncio
async def test_bot_delivery_sends_through_the_given_service() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.services.notification_types import NotificationType
    from app.services.user_reminders.dispatcher import bot_delivery

    service = SimpleNamespace(send_notification=AsyncMock(return_value=True))
    reminder = SimpleNamespace(
        id=7, texts={'ru': {'title': 't', 'body': 'b'}}, button_kind='none', button_target=None, category='service'
    )
    user = SimpleNamespace(language='ru')

    assert await bot_delivery(service)(user, reminder, bot='bot') is True

    args, kwargs = service.send_notification.await_args
    assert args[:3] == (user, NotificationType.USER_REMINDER, {'reminder_id': 7})
    assert kwargs['bot'] == 'bot' and kwargs['use_websocket'] is False
