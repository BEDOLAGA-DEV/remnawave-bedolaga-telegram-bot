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


async def test_full_sync_button_runs_shared_full_sync_and_reports_both_parts(monkeypatch) -> None:
    """Панель — истина: полная синхронизация читает панель и серверы, в панель не пишет."""
    full = AsyncMock(
        return_value=(
            {'created': 1, 'updated': 2, 'errors': 0, 'deleted': 0},
            {'created': 0, 'updated': 1, 'removed': 0, 'total': 3},
        )
    )
    monkeypatch.setattr(mod, 'perform_full_sync', full)
    monkeypatch.setattr(mod, 'RemnaWaveService', lambda: SimpleNamespace(is_configured=True))
    callback = _callback()

    await _unwrap(mod.sync_all_users)(callback, SimpleNamespace(language='ru', id=1), MagicMock())

    full.assert_awaited_once()
    final_text = callback.message.edit_text.await_args_list[-1].args[0]
    assert 'Из панели' in final_text and 'Создано: 1' in final_text and 'Обновлено: 2' in final_text
    assert '<b>В панель:</b>' not in final_text and 'обе стороны' not in final_text
    assert 'Панель — источник истины' in final_text
    assert 'Сервер' in final_text


async def test_full_sync_button_refuses_a_second_run_while_one_is_in_progress(monkeypatch) -> None:
    full = AsyncMock()
    monkeypatch.setattr(mod, 'perform_full_sync', full)
    monkeypatch.setattr(mod, 'is_full_sync_running', lambda: True)
    callback = _callback()

    await _unwrap(mod.sync_all_users)(callback, SimpleNamespace(language='ru', id=1), MagicMock())

    full.assert_not_awaited()
    callback.answer.assert_awaited()
    assert callback.answer.await_args.kwargs.get('show_alert') is True
    assert 'уже выполняется' in callback.answer.await_args.args[0]


async def test_full_sync_button_reports_a_lost_race_instead_of_crashing(monkeypatch) -> None:
    monkeypatch.setattr(mod, 'is_full_sync_running', lambda: False)
    monkeypatch.setattr(mod, 'perform_full_sync', AsyncMock(side_effect=mod.FullSyncAlreadyRunning()))
    monkeypatch.setattr(mod, 'RemnaWaveService', lambda: SimpleNamespace(is_configured=True))
    callback = _callback()

    await _unwrap(mod.sync_all_users)(callback, SimpleNamespace(language='ru', id=1), MagicMock())

    final_text = callback.message.edit_text.await_args_list[-1].args[0]
    assert 'уже выполняется' in final_text
