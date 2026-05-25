from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException, status

from app.cabinet.routes.auth import delete_current_account
from app.cabinet.schemas.auth import AccountDeleteRequest
from app.services.account_deletion_service import AccountDeletionPanelError


def _make_raw_request(ip: str = '127.0.0.1') -> SimpleNamespace:
    return SimpleNamespace(client=SimpleNamespace(host=ip), headers={})


@pytest.mark.asyncio
async def test_delete_current_account_accepts_current_password() -> None:
    user = SimpleNamespace(id=123, password_hash='hashed-password', telegram_id=None)
    db = AsyncMock()
    delete_account = AsyncMock()

    with (
        patch('app.cabinet.routes.auth.RateLimitCache.is_ip_rate_limited', AsyncMock(return_value=False)),
        patch('app.cabinet.routes.auth.RateLimitCache.is_rate_limited', AsyncMock(return_value=False)),
        patch('app.cabinet.routes.auth.verify_password', return_value=True),
        patch('app.cabinet.routes.auth.account_deletion_service.delete_own_account', delete_account),
    ):
        response = await delete_current_account(
            AccountDeleteRequest(confirmation='DELETE', password='current-password'),
            raw_request=_make_raw_request(),
            user=user,
            db=db,
        )

    assert response.message == 'Account deletion requested'
    delete_account.assert_awaited_once_with(db, user)


@pytest.mark.asyncio
async def test_delete_current_account_records_oauth_revocation_proof_with_password() -> None:
    user = SimpleNamespace(id=123, password_hash='hashed-password', telegram_id=None, google_id='google-sub')
    db = AsyncMock()
    delete_account = AsyncMock()

    with (
        patch('app.cabinet.routes.auth.RateLimitCache.is_ip_rate_limited', AsyncMock(return_value=False)),
        patch('app.cabinet.routes.auth.RateLimitCache.is_rate_limited', AsyncMock(return_value=False)),
        patch('app.cabinet.routes.auth.verify_password', return_value=True),
        patch(
            'app.cabinet.routes.auth.get_oauth_revocation_proof',
            AsyncMock(
                return_value={
                    'user_id': 123,
                    'provider': 'google',
                    'provider_id': 'google-sub',
                    'purpose': 'delete',
                    'event_id': 88,
                }
            ),
        ),
        patch('app.cabinet.routes.auth.account_deletion_service.delete_own_account', delete_account),
    ):
        response = await delete_current_account(
            AccountDeleteRequest(
                confirmation='DELETE',
                password='current-password',
                oauth_revocation_proofs=['proof-token-abcdefghijklmnopqrstuvwxyz123456'],
            ),
            raw_request=_make_raw_request(),
            user=user,
            db=db,
        )

    assert response.message == 'Account deletion requested'
    delete_account.assert_awaited_once_with(db, user, oauth_revocation_event_ids=[88])


@pytest.mark.asyncio
async def test_delete_current_account_rejects_wrong_password() -> None:
    user = SimpleNamespace(id=123, password_hash='hashed-password', telegram_id=None)
    db = AsyncMock()
    delete_account = AsyncMock()

    with (
        patch('app.cabinet.routes.auth.RateLimitCache.is_ip_rate_limited', AsyncMock(return_value=False)),
        patch('app.cabinet.routes.auth.RateLimitCache.is_rate_limited', AsyncMock(return_value=False)),
        patch('app.cabinet.routes.auth.verify_password', return_value=False),
        patch('app.cabinet.routes.auth.account_deletion_service.delete_own_account', delete_account),
        pytest.raises(HTTPException) as exc,
    ):
        await delete_current_account(
            AccountDeleteRequest(confirmation='DELETE', password='wrong-password'),
            raw_request=_make_raw_request(),
            user=user,
            db=db,
        )

    assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert exc.value.detail == 'Invalid current password'
    delete_account.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_current_account_accepts_matching_telegram_init_data() -> None:
    user = SimpleNamespace(id=123, password_hash=None, telegram_id=555)
    db = AsyncMock()
    delete_account = AsyncMock()

    with (
        patch('app.cabinet.routes.auth.RateLimitCache.is_ip_rate_limited', AsyncMock(return_value=False)),
        patch('app.cabinet.routes.auth.RateLimitCache.is_rate_limited', AsyncMock(return_value=False)),
        patch('app.cabinet.routes.auth.validate_telegram_init_data', return_value={'id': 555}),
        patch('app.cabinet.routes.auth.account_deletion_service.delete_own_account', delete_account),
    ):
        response = await delete_current_account(
            AccountDeleteRequest(confirmation='DELETE', telegram_init_data='signed-init-data'),
            raw_request=_make_raw_request(),
            user=user,
            db=db,
        )

    assert response.message == 'Account deletion requested'
    delete_account.assert_awaited_once_with(db, user)


@pytest.mark.asyncio
async def test_delete_current_account_accepts_oauth_proof_for_telegram_linked_oauth_account() -> None:
    user = SimpleNamespace(id=123, password_hash=None, telegram_id=555, google_id='google-sub')
    db = AsyncMock()
    delete_account = AsyncMock()

    with (
        patch('app.cabinet.routes.auth.RateLimitCache.is_ip_rate_limited', AsyncMock(return_value=False)),
        patch('app.cabinet.routes.auth.RateLimitCache.is_rate_limited', AsyncMock(return_value=False)),
        patch(
            'app.cabinet.routes.auth.get_oauth_revocation_proof',
            AsyncMock(
                return_value={
                    'user_id': 123,
                    'provider': 'google',
                    'provider_id': 'google-sub',
                    'purpose': 'delete',
                    'event_id': 89,
                }
            ),
        ),
        patch('app.cabinet.routes.auth.account_deletion_service.delete_own_account', delete_account),
    ):
        response = await delete_current_account(
            AccountDeleteRequest(confirmation='DELETE', oauth_revocation_proofs=['proof-token-abcdefghijklmnopqrstuvwxyz123456']),
            raw_request=_make_raw_request(),
            user=user,
            db=db,
        )

    assert response.message == 'Account deletion requested'
    delete_account.assert_awaited_once_with(db, user, oauth_revocation_event_ids=[89])


@pytest.mark.asyncio
async def test_delete_current_account_accepts_google_and_apple_revocation_proofs() -> None:
    user = SimpleNamespace(
        id=123,
        password_hash=None,
        telegram_id=None,
        google_id='google-sub',
        apple_id='apple-sub',
    )
    db = AsyncMock()
    delete_account = AsyncMock()

    with (
        patch('app.cabinet.routes.auth.RateLimitCache.is_ip_rate_limited', AsyncMock(return_value=False)),
        patch('app.cabinet.routes.auth.RateLimitCache.is_rate_limited', AsyncMock(return_value=False)),
        patch(
            'app.cabinet.routes.auth.get_oauth_revocation_proof',
            AsyncMock(
                side_effect=[
                    {
                        'user_id': 123,
                        'provider': 'google',
                        'provider_id': 'google-sub',
                        'purpose': 'delete',
                        'event_id': 90,
                    },
                    {
                        'user_id': 123,
                        'provider': 'apple',
                        'provider_id': 'apple-sub',
                        'purpose': 'delete',
                        'event_id': 91,
                    },
                ]
            ),
        ),
        patch('app.cabinet.routes.auth.account_deletion_service.delete_own_account', delete_account),
    ):
        response = await delete_current_account(
            AccountDeleteRequest(
                confirmation='DELETE',
                oauth_revocation_proofs=[
                    'google-proof-abcdefghijklmnopqrstuvwxyz123456',
                    'apple-proof-abcdefghijklmnopqrstuvwxyz123456',
                ],
            ),
            raw_request=_make_raw_request(),
            user=user,
            db=db,
        )

    assert response.message == 'Account deletion requested'
    delete_account.assert_awaited_once_with(db, user, oauth_revocation_event_ids=[90, 91])


@pytest.mark.asyncio
async def test_delete_current_account_does_not_consume_partial_oauth_proofs() -> None:
    user = SimpleNamespace(
        id=123,
        password_hash=None,
        telegram_id=None,
        google_id='google-sub',
        apple_id='apple-sub',
    )
    db = AsyncMock()
    delete_account = AsyncMock()
    consume = AsyncMock()

    with (
        patch('app.cabinet.routes.auth.RateLimitCache.is_ip_rate_limited', AsyncMock(return_value=False)),
        patch('app.cabinet.routes.auth.RateLimitCache.is_rate_limited', AsyncMock(return_value=False)),
        patch(
            'app.cabinet.routes.auth.get_oauth_revocation_proof',
            AsyncMock(
                side_effect=[
                    {
                        'user_id': 123,
                        'provider': 'google',
                        'provider_id': 'google-sub',
                        'purpose': 'delete',
                        'event_id': 90,
                    },
                    None,
                ]
            ),
            create=True,
        ),
        patch('app.cabinet.routes.auth.consume_oauth_revocation_proof', consume),
        patch('app.cabinet.routes.auth.account_deletion_service.delete_own_account', delete_account),
        pytest.raises(HTTPException) as exc,
    ):
        await delete_current_account(
            AccountDeleteRequest(
                confirmation='DELETE',
                oauth_revocation_proofs=[
                    'google-proof-abcdefghijklmnopqrstuvwxyz123456',
                    'apple-proof-abcdefghijklmnopqrstuvwxyz123456',
                ],
            ),
            raw_request=_make_raw_request(),
            user=user,
            db=db,
        )

    assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert exc.value.detail == 'OAuth provider revocation proof is required'
    consume.assert_not_awaited()
    delete_account.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_current_account_consumes_oauth_proofs_after_successful_delete() -> None:
    events: list[str] = []
    user = SimpleNamespace(id=123, password_hash=None, telegram_id=None, google_id='google-sub', apple_id=None)
    db = AsyncMock()
    delete_account = AsyncMock(side_effect=lambda *args, **kwargs: events.append('delete'))
    consume = AsyncMock(side_effect=lambda token: events.append(f'consume:{token}'))

    with (
        patch('app.cabinet.routes.auth.RateLimitCache.is_ip_rate_limited', AsyncMock(return_value=False)),
        patch('app.cabinet.routes.auth.RateLimitCache.is_rate_limited', AsyncMock(return_value=False)),
        patch(
            'app.cabinet.routes.auth.get_oauth_revocation_proof',
            AsyncMock(
                return_value={
                    'user_id': 123,
                    'provider': 'google',
                    'provider_id': 'google-sub',
                    'purpose': 'delete',
                    'event_id': 77,
                }
            ),
            create=True,
        ),
        patch('app.cabinet.routes.auth.consume_oauth_revocation_proof', consume),
        patch('app.cabinet.routes.auth.account_deletion_service.delete_own_account', delete_account),
    ):
        response = await delete_current_account(
            AccountDeleteRequest(confirmation='DELETE', oauth_revocation_proofs=['proof-token-abcdefghijklmnopqrstuvwxyz123456']),
            raw_request=_make_raw_request(),
            user=user,
            db=db,
        )

    assert response.message == 'Account deletion requested'
    assert events == ['delete', 'consume:proof-token-abcdefghijklmnopqrstuvwxyz123456']
    delete_account.assert_awaited_once_with(db, user, oauth_revocation_event_ids=[77])


@pytest.mark.asyncio
async def test_delete_current_account_rejects_oauth_only_user_without_fresh_proof() -> None:
    user = SimpleNamespace(id=123, password_hash=None, telegram_id=None, google_id='google-user')
    db = AsyncMock()
    delete_account = AsyncMock()

    with (
        patch('app.cabinet.routes.auth.RateLimitCache.is_ip_rate_limited', AsyncMock(return_value=False)),
        patch('app.cabinet.routes.auth.RateLimitCache.is_rate_limited', AsyncMock(return_value=False)),
        patch('app.cabinet.routes.auth.account_deletion_service.delete_own_account', delete_account),
        pytest.raises(HTTPException) as exc,
    ):
        await delete_current_account(
            AccountDeleteRequest(confirmation='DELETE'),
            raw_request=_make_raw_request(),
            user=user,
            db=db,
        )

    assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert exc.value.detail == 'OAuth provider revocation proof is required'
    delete_account.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_current_account_rejects_mismatched_telegram_init_data() -> None:
    user = SimpleNamespace(id=123, password_hash=None, telegram_id=555)
    db = AsyncMock()
    delete_account = AsyncMock()

    with (
        patch('app.cabinet.routes.auth.RateLimitCache.is_ip_rate_limited', AsyncMock(return_value=False)),
        patch('app.cabinet.routes.auth.RateLimitCache.is_rate_limited', AsyncMock(return_value=False)),
        patch('app.cabinet.routes.auth.validate_telegram_init_data', return_value={'id': 777}),
        patch('app.cabinet.routes.auth.account_deletion_service.delete_own_account', delete_account),
        pytest.raises(HTTPException) as exc,
    ):
        await delete_current_account(
            AccountDeleteRequest(confirmation='DELETE', telegram_init_data='signed-init-data'),
            raw_request=_make_raw_request(),
            user=user,
            db=db,
        )

    assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert exc.value.detail == 'Invalid Telegram confirmation'
    delete_account.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_current_account_rolls_back_on_service_failure() -> None:
    user = SimpleNamespace(id=123, password_hash='hashed-password', telegram_id=None)
    db = AsyncMock()
    delete_account = AsyncMock(side_effect=RuntimeError('boom'))

    with (
        patch('app.cabinet.routes.auth.RateLimitCache.is_ip_rate_limited', AsyncMock(return_value=False)),
        patch('app.cabinet.routes.auth.RateLimitCache.is_rate_limited', AsyncMock(return_value=False)),
        patch('app.cabinet.routes.auth.verify_password', return_value=True),
        patch('app.cabinet.routes.auth.account_deletion_service.delete_own_account', delete_account),
        pytest.raises(HTTPException) as exc,
    ):
        await delete_current_account(
            AccountDeleteRequest(confirmation='DELETE', password='current-password'),
            raw_request=_make_raw_request(),
            user=user,
            db=db,
        )

    assert exc.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert exc.value.detail == 'Failed to delete account'
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_current_account_maps_panel_cleanup_failure_to_bad_gateway() -> None:
    user = SimpleNamespace(id=123, password_hash='hashed-password', telegram_id=None)
    db = AsyncMock()
    delete_account = AsyncMock(side_effect=AccountDeletionPanelError(['panel-uuid']))

    with (
        patch('app.cabinet.routes.auth.RateLimitCache.is_ip_rate_limited', AsyncMock(return_value=False)),
        patch('app.cabinet.routes.auth.RateLimitCache.is_rate_limited', AsyncMock(return_value=False)),
        patch('app.cabinet.routes.auth.verify_password', return_value=True),
        patch('app.cabinet.routes.auth.account_deletion_service.delete_own_account', delete_account),
        pytest.raises(HTTPException) as exc,
    ):
        await delete_current_account(
            AccountDeleteRequest(confirmation='DELETE', password='current-password'),
            raw_request=_make_raw_request(),
            user=user,
            db=db,
        )

    assert exc.value.status_code == status.HTTP_502_BAD_GATEWAY
    assert exc.value.detail == 'Failed to disable VPN access'
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_current_account_rate_limits_by_ip_before_verifying_password() -> None:
    user = SimpleNamespace(id=123, password_hash='hashed-password', telegram_id=None)
    db = AsyncMock()
    delete_account = AsyncMock()

    with (
        patch('app.cabinet.routes.auth.RateLimitCache.is_ip_rate_limited', AsyncMock(return_value=True)),
        patch('app.cabinet.routes.auth.RateLimitCache.is_rate_limited', AsyncMock(return_value=False)),
        patch('app.cabinet.routes.auth.verify_password') as verify_password,
        patch('app.cabinet.routes.auth.account_deletion_service.delete_own_account', delete_account),
        pytest.raises(HTTPException) as exc,
    ):
        await delete_current_account(
            AccountDeleteRequest(confirmation='DELETE', password='current-password'),
            raw_request=_make_raw_request(),
            user=user,
            db=db,
        )

    assert exc.value.status_code == status.HTTP_429_TOO_MANY_REQUESTS
    assert exc.value.headers == {'Retry-After': '300'}
    verify_password.assert_not_called()
    delete_account.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_current_account_rate_limits_by_user_before_verifying_password() -> None:
    user = SimpleNamespace(id=123, password_hash='hashed-password', telegram_id=None)
    db = AsyncMock()
    delete_account = AsyncMock()

    with (
        patch('app.cabinet.routes.auth.RateLimitCache.is_ip_rate_limited', AsyncMock(return_value=False)),
        patch('app.cabinet.routes.auth.RateLimitCache.is_rate_limited', AsyncMock(return_value=True)),
        patch('app.cabinet.routes.auth.verify_password') as verify_password,
        patch('app.cabinet.routes.auth.account_deletion_service.delete_own_account', delete_account),
        pytest.raises(HTTPException) as exc,
    ):
        await delete_current_account(
            AccountDeleteRequest(confirmation='DELETE', password='current-password'),
            raw_request=_make_raw_request(),
            user=user,
            db=db,
        )

    assert exc.value.status_code == status.HTTP_429_TOO_MANY_REQUESTS
    assert exc.value.headers == {'Retry-After': '300'}
    verify_password.assert_not_called()
    delete_account.assert_not_awaited()
