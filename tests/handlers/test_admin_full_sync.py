"""«Полная синхронизация» в боте — та же функция, что и по расписанию: импорт, экспорт, серверы."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import app.handlers.admin.remnawave as mod


def _unwrap(fn):
    while hasattr(fn, '__wrapped__'):
        fn = fn.__wrapped__
    return fn


def _callback():
    callback = MagicMock()
    callback.data = 'sync_all_users'
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    return callback


async def test_full_sync_button_runs_shared_full_sync_and_reports_all_three_parts(monkeypatch) -> None:
    full = AsyncMock(
        return_value=(
            {
                'created': 1,
                'updated': 2,
                'errors': 0,
                'deleted': 0,
                'to_panel': {'created': 0, 'updated': 7, 'errors': 1},
            },
            {'created': 0, 'updated': 1, 'removed': 0, 'total': 3},
        )
    )
    monkeypatch.setattr(mod, 'perform_full_sync', full)
    monkeypatch.setattr(mod, 'RemnaWaveService', lambda: SimpleNamespace(is_configured=True))
    callback = _callback()

    await _unwrap(mod.sync_all_users)(callback, SimpleNamespace(language='ru', id=1), MagicMock())

    full.assert_awaited_once()
    final_text = callback.message.edit_text.await_args_list[-1].args[0]
    assert 'Из панели' in final_text and 'Создано: 1' in final_text
    assert 'В панель' in final_text and 'Обновлено: 7' in final_text and 'Ошибок: 1' in final_text
    assert 'Сервер' in final_text
