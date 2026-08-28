from types import SimpleNamespace

import pytest

from app.services.registration_access_service import (
    RegistrationAccessDecision,
    RegistrationAccessReason,
    RegistrationInviteEvidence,
    RegistrationInviteKind,
)


def test_registration_invite_payload_preserves_original_start_parameter():
    from app.handlers.start import _registration_invite_payload

    assert _registration_invite_payload({'registration_invite_payload': 'campaign'}, None) == 'campaign'
    assert _registration_invite_payload({'pending_gift_token': 'a' * 48}, None) == 'GIFT_' + 'a' * 48
    assert _registration_invite_payload({'referral_code': 'ref'}, None) == 'ref'


@pytest.mark.asyncio
async def test_telegram_access_evaluation_forwards_identity_and_lock(monkeypatch):
    from app.handlers import start

    calls = []

    class FakeService:
        async def evaluate(self, db, context):
            calls.append(context)
            return RegistrationAccessDecision(True, RegistrationAccessReason.INVITE_GRANTED)

    monkeypatch.setattr(start, '_registration_access_service', FakeService())
    monkeypatch.setattr(type(start.settings), 'is_admin', lambda self, telegram_id: telegram_id == 42)
    tg = SimpleNamespace(id=42)
    user = SimpleNamespace(id=7, status='deleted')

    decision = await start._evaluate_telegram_registration_access(
        object(), tg, existing_user=user, start_parameter='invite', lock_limited=True
    )

    assert decision.allowed is True
    assert calls[0].identity.user_id == 7
    assert calls[0].identity.verified_admin is True
    assert calls[0].lock_limited_invite is True


@pytest.mark.asyncio
async def test_create_user_with_registration_invite_commits_gift_atomically(monkeypatch):
    from app.handlers import start

    events = []
    user = SimpleNamespace(id=11)
    evidence = RegistrationInviteEvidence(RegistrationInviteKind.GIFT, locked_gift=object())
    decision = RegistrationAccessDecision(True, RegistrationAccessReason.INVITE_GRANTED, evidence)

    class DB:
        def __init__(self):
            self.commits = 0
            self.rollbacks = 0

        async def commit(self):
            self.commits += 1

        async def rollback(self):
            self.rollbacks += 1

        async def refresh(self, *args, **kwargs):
            pass

    db = DB()

    async def create(**kwargs):
        events.append('create')
        return user

    async def bind(db_arg, *, evidence, user):
        events.append('bind')

    async def emit(db_arg, user_arg):
        events.append('emit')

    monkeypatch.setattr(start, 'create_user_no_commit', create)
    monkeypatch.setattr(start._registration_invite_service, 'bind_locked_gift', bind)
    monkeypatch.setattr(start, 'emit_user_created_event', emit)

    result = await start._create_user_with_registration_invite(
        db,
        decision=decision,
        telegram_id=42,
        username='user',
        first_name='First',
        last_name='Last',
        language='ru',
        referred_by_id=None,
        referral_code='ref-new',
    )

    assert result is user
    assert events == ['create', 'bind', 'emit']
    assert db.commits == 1
    assert db.rollbacks == 0


@pytest.mark.asyncio
async def test_invite_denial_includes_support_button_when_contact_is_configured(monkeypatch):
    from app.handlers import start

    answer = __import__('unittest.mock', fromlist=['AsyncMock']).AsyncMock()
    texts = SimpleNamespace(
        language='ru',
        t=lambda key, default=None: {
            'registration_invite_required': 'invite required',
            'registration_contact_support': 'Contact support',
        }.get(key, default),
    )
    monkeypatch.setattr(
        start,
        'settings',
        SimpleNamespace(get_support_contact_url=lambda: 'https://t.me/support'),
    )

    await start._answer_registration_denial(
        answer,
        texts,
        RegistrationAccessDecision(False, RegistrationAccessReason.INVITE_REQUIRED),
    )

    kwargs = answer.await_args.kwargs
    keyboard = kwargs['reply_markup']
    assert keyboard.inline_keyboard[0][0].text == 'Contact support'
    assert keyboard.inline_keyboard[0][0].url == 'https://t.me/support'


@pytest.mark.asyncio
async def test_invite_denial_renders_without_button_when_contact_is_empty(monkeypatch):
    from app.handlers import start

    answer = __import__('unittest.mock', fromlist=['AsyncMock']).AsyncMock()
    texts = SimpleNamespace(language='en', t=lambda key, default=None: default)
    monkeypatch.setattr(start, 'settings', SimpleNamespace(get_support_contact_url=lambda: None))

    await start._answer_registration_denial(
        answer,
        texts,
        RegistrationAccessDecision(False, RegistrationAccessReason.INVITE_REQUIRED),
    )

    assert answer.await_args.kwargs.get('reply_markup') is None


@pytest.mark.asyncio
async def test_pending_gift_drain_delegates_to_the_shared_claim_service(monkeypatch):
    """Активация из FSM идёт через общий claim-сервис, а не через собственный запрос."""
    from unittest.mock import AsyncMock

    from app.handlers import start

    gift = SimpleNamespace(
        id=1,
        token='T' * 64,
        is_gift=True,
        status='delivered',
        tariff=SimpleNamespace(name='Gift'),
        period_days=30,
    )
    claim = AsyncMock(return_value=gift)
    monkeypatch.setattr(start, 'claim_gift_for_user', claim)

    state = SimpleNamespace(get_data=AsyncMock(return_value={'pending_gift_token': 'T' * 48}))
    db = SimpleNamespace()
    answer = AsyncMock()
    user = SimpleNamespace(id=22, language='ru')

    await start._activate_pending_gift_after_registration(db, state, user, answer)

    claim.assert_awaited_once_with(
        db,
        claimant_user_id=user.id,
        claim_input='T' * 48,
        allow_legacy_short=False,
    )
    assert 'активирован' in answer.await_args.args[0]


@pytest.mark.asyncio
async def test_already_claimed_gift_is_reported_instead_of_ignored(monkeypatch):
    """Ссылка, которую уже забрал другой человек, обязана отвечать, а не молчать."""
    from unittest.mock import AsyncMock

    from app.handlers import start
    from app.services.gift_claim_service import GiftClaimAlreadyOwnedError

    claim = AsyncMock(side_effect=GiftClaimAlreadyOwnedError('taken'))
    monkeypatch.setattr(start, 'claim_gift_for_user', claim)

    state = SimpleNamespace(get_data=AsyncMock(return_value={'pending_gift_token': 'T' * 48}))
    answer = AsyncMock()

    await start._activate_pending_gift_after_registration(
        SimpleNamespace(), state, SimpleNamespace(id=22, language='ru'), answer
    )

    assert 'уже был активирован' in answer.await_args.args[0]
