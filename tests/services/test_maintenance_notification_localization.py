from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.localization.texts import get_texts
from app.services import maintenance_service as module
from app.services.maintenance_service import MaintenanceService


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_owned_maintenance_defaults_localize_and_custom_text_is_unchanged(monkeypatch, language):
    service = MaintenanceService()
    monkeypatch.setattr(settings, 'MAINTENANCE_MESSAGE', type(settings).model_fields['MAINTENANCE_MESSAGE'].default)
    assert service.get_maintenance_message(language) == get_texts(language).t('MAINT_DEFAULT_MESSAGE', '')
    custom = '  Operator <b>message</b> & raw\n '
    monkeypatch.setattr(settings, 'MAINTENANCE_MESSAGE', custom)
    assert service.get_maintenance_message(language) == custom
    service._status.auto_enabled = True
    assert service.get_maintenance_message(language) == get_texts(language).t('MAINT_AUTO_MESSAGE', '').format(
        time=get_texts(language).t('MAINT_UNKNOWN_TIME', '')
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
async def test_actual_maintenance_admin_rendering_keeps_state_and_escapes_data(monkeypatch, language):
    service = MaintenanceService()
    service._bot = object()
    service._save_status_to_cache = AsyncMock()
    sent = AsyncMock(return_value=True)
    notifier = SimpleNamespace(
        is_enabled=True, _notification_texts=AsyncMock(return_value=get_texts(language)), send_admin_notification=sent
    )
    monkeypatch.setattr('app.services.admin_notification_service.AdminNotificationService', lambda bot: notifier)
    reason = 'Operator <reason> & details'
    assert await service.enable_maintenance(reason=reason, auto=False)
    assert service.status.is_active is True
    assert service.status.reason == reason
    message = sent.await_args.args[0]
    assert 'Operator &lt;reason&gt; &amp; details' in message
    expected = get_texts(language).t('MAINT_NOTICE_149', '').split('\n')[0]
    assert expected and expected in message
    assert await service.disable_maintenance()
    assert service.status.is_active is False
    assert service.status.reason is None
    assert get_texts(language).t('MAINT_NOTICE_195', '').split('\n')[0] in sent.await_args.args[0]
    assert await service.send_remnawave_status_notification('offline', 'Provider <detail> & unchanged')
    message = sent.await_args.args[0]
    assert 'Provider &lt;detail&gt; &amp; unchanged' in message
    assert get_texts(language).t('MAINT_STATUS_OFFLINE', '') in message
    assert service._save_status_to_cache.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
async def test_api_availability_notifications_keep_counters_and_raw_error(monkeypatch, language):
    service = MaintenanceService()
    service._bot = object()
    service._save_status_to_cache = AsyncMock()
    sent = AsyncMock(return_value=True)
    notifier = SimpleNamespace(
        is_enabled=True, _notification_texts=AsyncMock(return_value=get_texts(language)), send_admin_notification=sent
    )
    monkeypatch.setattr('app.services.admin_notification_service.AdminNotificationService', lambda bot: notifier)
    monkeypatch.setattr(
        type(settings),
        'get_remnawave_auth_params',
        lambda self: {'base_url': 'https://panel.example', 'api_key': 'test-key'},
    )
    monkeypatch.setattr(type(settings), 'get_maintenance_retry_attempts', lambda self: 1)
    monkeypatch.setattr(type(settings), 'is_maintenance_auto_enable', lambda self: False)

    class API:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(module, 'RemnaWaveAPI', API)
    connection = AsyncMock(return_value=False)
    monkeypatch.setattr(module, 'test_api_connection', connection)
    assert await service.check_api_status() is False
    assert service.status.consecutive_failures == 1
    expected = get_texts(language).t('MAINT_NOTICE_339', '').split('\n')[0]
    assert expected and expected in sent.await_args.args[0]
    connection.return_value = True
    assert await service.check_api_status() is True
    assert service.status.consecutive_failures == 0
    expected = get_texts(language).t('MAINT_NOTICE_309', '').split('\n')[0]
    assert expected and expected in sent.await_args.args[0]
    connection.side_effect = RuntimeError('Provider <detail> & raw')
    assert await service.check_api_status() is False
    assert service.status.consecutive_failures == 1
    assert 'Provider &lt;detail&gt; &amp; raw' in sent.await_args.args[0]
    assert service._is_checking is False
    assert service._save_status_to_cache.await_count == 3


@pytest.mark.asyncio
async def test_authenticated_miniapp_maintenance_status_uses_user_language(monkeypatch):
    from app.webapi.routes import miniapp
    from app.webapi.schemas.miniapp import MiniAppSubscriptionRequest

    user = SimpleNamespace(language='fa')
    resolve = AsyncMock(return_value=(user, {}))
    monkeypatch.setattr(miniapp, '_resolve_user_from_init_data', resolve)
    service = MaintenanceService()
    service._status.auto_enabled = True
    monkeypatch.setattr(miniapp, 'maintenance_service', service)
    payload = MiniAppSubscriptionRequest(initData='test-data')
    response = await miniapp.get_maintenance_status(payload, object())
    assert response.message == service.get_maintenance_message('fa')
    resolve.assert_awaited_once()


@pytest.mark.asyncio
async def test_api_error_renderer_survives_exception_scope(monkeypatch):
    service = MaintenanceService()
    service._save_status_to_cache = AsyncMock()
    service._notify_admins = AsyncMock()
    monkeypatch.setattr(
        type(settings),
        'get_remnawave_auth_params',
        lambda self: (_ for _ in ()).throw(RuntimeError('Provider <detail> & raw')),
    )
    assert await service.check_api_status() is False
    renderer = service._notify_admins.await_args.args[0]
    for language in ['ru', 'en', 'ua', 'zh', 'fa']:
        assert 'Provider &lt;detail&gt; &amp; raw' in renderer(get_texts(language))
