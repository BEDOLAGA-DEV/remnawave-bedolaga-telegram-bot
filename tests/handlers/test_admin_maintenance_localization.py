"""Maintenance copy changes preserve service actions and operator-entered data."""

import pytest

from app.handlers.admin import maintenance


@pytest.mark.parametrize('language', ['en', 'ua', 'zh', 'fa'])
def test_panel_summary_localizes_known_status(language):
    summary = {'status': 'online', 'description': '🟢 Панель работает нормально'}
    assert maintenance._panel_description(summary, language) != summary['description']
    assert summary['description'] == '🟢 Панель работает нормально'


def test_panel_summary_preserves_unknown_description():
    assert (
        maintenance._panel_description({'status': 'custom', 'description': 'Operator {label}'}, 'fa')
        == 'Operator {label}'
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
async def test_maintenance_panel_uses_account_language_and_escapes_reason(monkeypatch, language):
    import inspect
    import re
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.services import remnawave_service

    status = {
        'is_active': True,
        'api_status': True,
        'monitoring_active': False,
        'enabled_at': datetime(2026, 1, 1, tzinfo=UTC),
        'reason': 'Operator <reason>',
        'last_check': None,
        'consecutive_failures': 0,
        'monitoring_configured': False,
        'check_interval': 60,
        'auto_enable_configured': False,
    }
    monkeypatch.setattr(maintenance.maintenance_service, 'get_status_info', lambda: status)
    panel = SimpleNamespace(
        get_panel_status_summary=AsyncMock(
            return_value={
                'status': 'maintenance',
                'description': '🔧 Панель на обслуживании',
                'has_issues': False,
            }
        )
    )
    monkeypatch.setattr(remnawave_service, 'RemnaWaveService', lambda: panel)
    callback = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
    await inspect.unwrap(maintenance.show_maintenance_panel)(callback, SimpleNamespace(language=language), None, None)
    result = callback.message.edit_text.await_args.args[0]
    assert 'Operator &lt;reason&gt;' in result
    if language in {'en', 'zh', 'fa'}:
        assert not re.search('[А-Яа-яЁё]', result)


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
async def test_maintenance_reason_preserves_service_arguments_and_env_warning(monkeypatch, language):
    import inspect
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    enable = AsyncMock(return_value=True)
    persist = AsyncMock()
    monkeypatch.setattr(maintenance.maintenance_service, 'enable_maintenance', enable)
    monkeypatch.setattr(maintenance.maintenance_service, 'get_status_info', dict)
    monkeypatch.setattr(maintenance, '_persist_maintenance_mode', persist)
    monkeypatch.setattr(maintenance, '_maintenance_env_locked', lambda: True)
    state = SimpleNamespace(
        get_state=AsyncMock(return_value=maintenance.MaintenanceStates.waiting_for_reason), clear=AsyncMock()
    )
    message = SimpleNamespace(text='Operator <reason>', answer=AsyncMock())
    await inspect.unwrap(maintenance.process_maintenance_reason)(
        message, SimpleNamespace(language=language), None, state
    )
    enable.assert_awaited_once_with(reason='Operator <reason>', auto=False)
    persist.assert_awaited_once_with(None, True)
    result = message.answer.await_args_list[0].args[0]
    assert 'Operator &lt;reason&gt;' in result
    assert 'MAINTENANCE_MODE' in result and '.env' in result
