"""Модули Cashera и живого меню не лежат на кольцах импортов.

CodeQL py/cyclic-import на PR #3292: хуки отмены автопродления стоят в
crud.subscription и subscription_service, а payment.cashera дотягивался обратно через
monitoring_service, user_utils и routes.websocket. Импорты внутри функций учитываются —
CodeQL их тоже считает.
"""

from __future__ import annotations

import pytest

from tests.services.user_reminders.test_import_cycle import _graph, _path


@pytest.mark.parametrize(
    'module',
    [
        'app.services.payment.cashera',
        'app.services.cashera_recurring_cancel',
        'app.services.cashera_recurrent',
        'app.services.cashera_service',
        'app.database.crud.cashera_subscription',
        'app.services.autopay_period',
        'app.cabinet.ws_manager',
        'app.services.live_menu_service',
    ],
)
def test_module_is_not_on_import_cycle(module: str) -> None:
    graph = _graph()
    for neighbour in sorted(graph.get(module, ())):
        chain = _path(graph, neighbour, module)
        assert chain is None, f'{module} -> ' + ' -> '.join(chain)
