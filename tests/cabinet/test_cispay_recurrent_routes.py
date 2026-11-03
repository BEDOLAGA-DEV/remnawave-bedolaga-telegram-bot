from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException


async def test_cancel_still_available_with_new_checkout_disabled(monkeypatch):
    from app.cabinet.routes.subscription_modules import cispay_recurrent as route
    from app.config import settings
    from app.services import cispay_recurrent_service as service

    monkeypatch.setattr(settings, 'CISPAY_RECURRENT_ENABLED', False)
    cancel = AsyncMock(return_value={'status': 'CANCEL_PENDING'})
    monkeypatch.setattr(service, 'cancel_cispay_agreement', cancel)
    response = await route.cancel_cispay_recurrent(subscription_id=7, user=SimpleNamespace(id=1), db=None)
    assert response['status'] == 'CANCEL_PENDING'
    cancel.assert_awaited_once_with(None, user_id=1, subscription_id=7, reason='customer')


async def test_status_maps_foreign_subscription_to_404(monkeypatch):
    from app.cabinet.routes.subscription_modules import cispay_recurrent as route
    from app.services import cispay_recurrent_checkout as checkout

    monkeypatch.setattr(
        checkout, 'get_cispay_agreement_state', AsyncMock(side_effect=ValueError('subscription_not_found'))
    )
    with pytest.raises(HTTPException) as error:
        await route.get_cispay_recurrent(subscription_id=7, user=SimpleNamespace(id=1), db=None)
    assert error.value.status_code == 404


@pytest.mark.parametrize('reset', [False, True])
async def test_credential_guard_is_a_clear_conflict_not_internal_error(monkeypatch, reset):
    from app.cabinet.routes import admin_settings as route

    config = route.bot_configuration_service
    monkeypatch.setattr(config, 'is_env_locked', lambda _: False)
    monkeypatch.setattr(config, 'set_value', AsyncMock(side_effect=ValueError('Disable CisPay first')))
    monkeypatch.setattr(config, 'reset_value', AsyncMock(side_effect=ValueError('Disable CisPay first')))
    with pytest.raises(HTTPException) as error:
        if reset:
            await route.reset_setting('CISPAY_SHOP_ID', admin=SimpleNamespace(id=1), db=None)
        else:
            await route.update_setting(
                'CISPAY_SHOP_ID', route.SettingUpdateRequest(value='changed'), admin=SimpleNamespace(id=1), db=None
            )
    assert error.value.status_code == 409
    assert error.value.detail == 'Disable CisPay first'
