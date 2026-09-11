from types import SimpleNamespace

import pytest

from app.cabinet.routes import auth
from app.cabinet.schemas.auth import EmailRegisterStandaloneRequest
from app.services.registration_access_service import RegistrationChannel


class GateObserved(RuntimeError):
    pass


@pytest.mark.parametrize(
    ('referral_code', 'campaign_slug', 'expected_start_parameter'),
    [
        ('REF_CODE', None, 'REF_CODE'),
        (None, 'campaign-slug', 'campaign-slug'),
        ('REF_CODE', 'campaign-slug', 'REF_CODE'),
    ],
)
async def test_standalone_email_forwards_invite_evidence_to_registration_gate(
    monkeypatch,
    referral_code,
    campaign_slug,
    expected_start_parameter,
):
    async def no_op(*args, **kwargs):
        return None

    async def capture_gate(db, **kwargs):
        assert kwargs['channel'] is RegistrationChannel.CABINET_EMAIL
        assert kwargs['start_parameter'] == expected_start_parameter
        raise GateObserved

    monkeypatch.setattr(auth, 'require_email_auth_enabled', no_op)
    monkeypatch.setattr(auth, 'enforce_email_registration_throttle', no_op)
    monkeypatch.setattr(auth, 'get_client_ip', lambda request: '127.0.0.1')
    monkeypatch.setattr(auth, 'evaluate_public_registration', capture_gate)

    request = EmailRegisterStandaloneRequest(
        email='new@example.com',
        password='strong-password',
        referral_code=referral_code,
        campaign_slug=campaign_slug,
    )

    with pytest.raises(GateObserved):
        await auth.register_email_standalone(
            request=request,
            raw_request=SimpleNamespace(),
            db=SimpleNamespace(),
        )
