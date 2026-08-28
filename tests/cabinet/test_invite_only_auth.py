from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services.registration_access_service import (
    RegistrationAccessDecision,
    RegistrationAccessReason,
    RegistrationChannel,
)


def test_http_mapping_distinguishes_policy_and_infrastructure_errors():
    from app.cabinet.auth.registration_access import raise_for_registration_decision

    with pytest.raises(HTTPException) as denied:
        raise_for_registration_decision(RegistrationAccessDecision(False, RegistrationAccessReason.CHANNEL_NOT_ALLOWED))
    assert denied.value.status_code == 403
    assert denied.value.detail['code'] == 'registration_invite_required'

    with pytest.raises(HTTPException) as unavailable:
        raise_for_registration_decision(RegistrationAccessDecision(False, RegistrationAccessReason.CHECK_UNAVAILABLE))
    assert unavailable.value.status_code == 503
    assert unavailable.value.detail['code'] == 'registration_check_unavailable'


@pytest.mark.asyncio
async def test_public_adapter_short_circuits_when_invite_only_is_disabled(monkeypatch):
    from app.cabinet.auth import registration_access

    class FailIfCalled:
        async def evaluate(self, db, context):
            raise AssertionError('central policy must not query DB when invite-only is disabled')

    monkeypatch.setattr(registration_access.settings, 'INVITE_ONLY_ENABLED', False)
    monkeypatch.setattr(registration_access, '_registration_access_service', FailIfCalled())

    decision = await registration_access.evaluate_public_registration(
        object(),
        channel=RegistrationChannel.CABINET_OAUTH,
    )

    assert decision == RegistrationAccessDecision(
        True,
        RegistrationAccessReason.INVITE_ONLY_DISABLED,
    )


@pytest.mark.asyncio
async def test_cabinet_gate_forwards_verified_admin_identity(monkeypatch):
    from app.cabinet.auth import registration_access

    monkeypatch.setattr(registration_access.settings, 'INVITE_ONLY_ENABLED', True)
    calls = []

    class FakeService:
        async def evaluate(self, db, context):
            calls.append(context)
            return RegistrationAccessDecision(True, RegistrationAccessReason.VERIFIED_ADMIN)

    monkeypatch.setattr(registration_access, '_registration_access_service', FakeService())
    user = SimpleNamespace(id=7, status='deleted')

    decision = await registration_access.evaluate_public_registration(
        object(),
        channel=RegistrationChannel.CABINET_TELEGRAM_INIT_DATA,
        existing_user=user,
        telegram_id=42,
        verified_admin=True,
    )

    assert decision.allowed is True
    assert calls[0].identity.user_id == 7
    assert calls[0].identity.verified_admin is True


@pytest.mark.asyncio
async def test_every_signed_telegram_arm_revives_its_own_deleted_account(monkeypatch):
    """initData, widget and OIDC all prove the same identity, so all three must revive."""
    from unittest.mock import AsyncMock

    from app.cabinet.routes import auth

    revive = AsyncMock()
    monkeypatch.setattr('app.services.user_revival_service.revive_deleted_user', revive)

    sources = (
        'cabinet_telegram_login',
        'cabinet_telegram_widget_login',
        'cabinet_telegram_oidc_login',
    )
    for source in sources:
        user = SimpleNamespace(status='deleted')
        await auth._recover_cabinet_user_after_gate(object(), user, None, source=source)

    assert [call.kwargs['source'] for call in revive.await_args_list] == list(sources)


@pytest.mark.asyncio
async def test_non_deleted_inactive_account_without_admin_proof_is_refused():
    from app.cabinet.routes import auth

    user = SimpleNamespace(status='blocked', telegram_id=None, email=None, email_verified=False)

    with pytest.raises(HTTPException) as refused:
        await auth._recover_cabinet_user_after_gate(object(), user, None, source='cabinet_telegram_widget_login')

    assert refused.value.status_code == 403
    assert user.status == 'blocked'
