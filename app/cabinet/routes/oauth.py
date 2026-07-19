"""OAuth 2.0 authentication routes for cabinet."""

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.user import (
    create_user_by_oauth,
    get_user_by_email,
    get_user_by_oauth_provider,
    get_user_by_referral_code,
    set_user_oauth_provider_id,
)
from app.database.models import User, UserStatus
from app.services.user_revival_service import revive_deleted_user

from ..auth.oauth_providers import (
    OAuthUserInfo,
    generate_oauth_state,
    get_provider,
    validate_oauth_state,
)
from ..dependencies import get_cabinet_db
from ..routes.account_linking import OAuthProviderName
from ..schemas.auth import AuthResponse
from .auth import _create_auth_response, _process_campaign_bonus, _store_refresh_token


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/auth/oauth', tags=['Cabinet OAuth'])


async def _finalize_oauth_login(
    db: AsyncSession,
    user: User,
    provider: str,
    campaign_slug: str | None = None,
    referral_code: str | None = None,
    *,
    is_new_user: bool = False,
) -> AuthResponse:
    """Update last login, create tokens, store refresh token."""
    user.cabinet_last_login = datetime.now(UTC)
    await db.commit()
    auth_response = await _create_auth_response(user, db)
    await _store_refresh_token(db, user.id, auth_response.refresh_token, device_info=f'oauth:{provider}')

    # Process referral code (only for new users — existing users cannot be assigned a referrer)
    from .auth import _process_referral_code, _user_to_response

    await _process_referral_code(db, user, referral_code, is_new_user=is_new_user)

    auth_response.campaign_bonus = await _process_campaign_bonus(db, user, campaign_slug)
    if auth_response.campaign_bonus:
        auth_response.user = _user_to_response(user)
    return auth_response


# --- Schemas ---


class OAuthProviderInfo(BaseModel):
    name: str
    display_name: str


class OAuthProvidersResponse(BaseModel):
    providers: list[OAuthProviderInfo]


class OAuthAuthorizeResponse(BaseModel):
    authorize_url: str
    state: str


class OAuthCallbackRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=2048, description='Authorization code from provider')
    state: str = Field(..., min_length=1, max_length=128, description='CSRF state token')
    device_id: str | None = Field(None, max_length=256, description='Device ID from VK ID callback')
    campaign_slug: str | None = Field(
        None, min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$', description='Campaign slug from web link'
    )
    referral_code: str | None = Field(
        None, max_length=32, pattern=r'^[a-zA-Z0-9_-]+$', description='Referral code of inviter'
    )


# --- Endpoints ---


@router.get('/providers', response_model=OAuthProvidersResponse)
async def get_oauth_providers():
    """Get list of enabled OAuth providers."""
    providers_config = settings.get_oauth_providers_config()
    providers = [
        OAuthProviderInfo(name=name, display_name=cfg['display_name'])
        for name, cfg in providers_config.items()
        if cfg['enabled']
    ]
    return OAuthProvidersResponse(providers=providers)


@router.get('/{provider}/authorize', response_model=OAuthAuthorizeResponse)
async def get_oauth_authorize_url(provider: OAuthProviderName):
    """Get authorization URL for an OAuth provider."""
    oauth_provider = get_provider(provider)
    if not oauth_provider:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Requested OAuth provider is not available',
        )

    # Generate extra state data (e.g., PKCE code_verifier for VK)
    auth_extra = oauth_provider.prepare_auth_state()
    state = await generate_oauth_state(provider, extra_data=auth_extra or None)
    # Only pass URL-safe params (prefixed with _) to authorize URL; exclude secrets like code_verifier
    url_params = {k: v for k, v in auth_extra.items() if k.startswith('_')} if auth_extra else {}
    authorize_url = oauth_provider.get_authorization_url(state, **url_params)

    return OAuthAuthorizeResponse(authorize_url=authorize_url, state=state)


@router.post('/{provider}/callback', response_model=AuthResponse)
async def oauth_callback(
    provider: OAuthProviderName,
    request: OAuthCallbackRequest,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Handle OAuth callback: exchange code, find/create user, return JWT."""
    # 1. Validate CSRF state and retrieve stored data (e.g., PKCE code_verifier)
    state_data = await validate_oauth_state(request.state, provider)
    if not state_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid or expired OAuth state',
        )

    # 1b. Reject linking-flow state tokens (must use link_provider_callback instead)
    if state_data.get('linking') == 'true':
        logger.warning('Linking-flow state token used in login callback', provider=provider)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='OAuth state was initiated for account linking, not login',
        )

    # 2. Get provider instance
    oauth_provider = get_provider(provider)
    if not oauth_provider:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Requested OAuth provider is not available',
        )

    # 3. Exchange code for tokens (pass PKCE code_verifier and device_id if present)
    exchange_kwargs: dict[str, str] = {'state': request.state}
    code_verifier = state_data.get('code_verifier')
    if code_verifier:
        exchange_kwargs['code_verifier'] = code_verifier
    if request.device_id:
        exchange_kwargs['device_id'] = request.device_id

    try:
        token_data = await oauth_provider.exchange_code(request.code, **exchange_kwargs)
    except Exception as exc:
        logger.error('OAuth code exchange failed', provider=provider, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Failed to exchange authorization code',
        ) from exc

    # 4. Fetch user info from provider
    try:
        user_info: OAuthUserInfo = await oauth_provider.get_user_info(token_data)
    except Exception as exc:
        logger.error('OAuth user info fetch failed', provider=provider, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Failed to fetch user information from provider',
        ) from exc

    # 5. Find user by provider ID
    user = await get_user_by_oauth_provider(db, provider, user_info.provider_id)
    if user:
        # If the previously-linked account was soft-deleted for
        # inactivity, the OAuth provider's signed JWT/userinfo is enough
        # identity proof to revive: same provider_id == same human.
        # Without revival we'd let DELETED-status linger and the cabinet
        # dependencies guard would 403 right after login.
        was_deleted = user.status == UserStatus.DELETED.value
        if was_deleted:
            await revive_deleted_user(db, user, source=f'oauth_{provider}_provider_id')
        logger.info('OAuth login for existing user', provider=provider, user_id=user.id, revived=was_deleted)
        return await _finalize_oauth_login(db, user, provider, request.campaign_slug, request.referral_code)

    # 6. Find user by email — link provider only when BOTH the IdP and
    # the local row attest the email is verified. Without the local
    # `user.email_verified` guard, an attacker who controls the email
    # at the IdP could take over a never-confirmed local account (one
    # registered via password-flow but never opened the verification
    # mail) — the IdP's verification covers IdP-side ownership, not the
    # local row's history. Both must agree.
    if user_info.email and user_info.email_verified:
        user = await get_user_by_email(db, user_info.email)
        if user and not user.email_verified:
            # Same email exists locally but verification was never
            # completed. Falling through to step 8 would attempt to
            # INSERT a duplicate email and hit the `User.email UNIQUE`
            # constraint → opaque 500. Refuse with a friendly 409
            # explaining the user needs to finish verification (or use
            # the bot) before linking an OAuth provider with that
            # address. Security audit follow-up.
            logger.info(
                'OAuth email-merge blocked: local row email is not verified yet',
                provider=provider,
                local_user_id=user.id,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    'code': 'email_unverified_local',
                    'message': (
                        'An account with this email exists but its email has not been verified yet. '
                        'Finish email verification (or use the Telegram bot) before linking a social login.'
                    ),
                },
            )
        if user and user.email_verified:
            # A DELETED row found by verified-email match is the SAME
            # human returning via a fresh OAuth provider; reactivate
            # rather than letting the OAuth flow fall through and
            # silently create a duplicate account.
            was_deleted = user.status == UserStatus.DELETED.value
            if was_deleted:
                await revive_deleted_user(db, user, source=f'oauth_{provider}_email_merge')
            await set_user_oauth_provider_id(db, user, provider, user_info.provider_id)
            logger.info(
                'OAuth provider linked to existing email user',
                provider=provider,
                user_id=user.id,
                revived=was_deleted,
            )
            return await _finalize_oauth_login(db, user, provider, request.campaign_slug, request.referral_code)

    # 7. Resolve referral code for new user
    referrer_id = None
    if request.referral_code:
        try:
            referrer = await get_user_by_referral_code(db, request.referral_code)
            if referrer:
                # Self-referral protection by email
                if (
                    user_info.email
                    and user_info.email_verified
                    and referrer.email
                    and referrer.email.lower() == user_info.email.lower()
                ):
                    logger.warning(
                        'Self-referral attempt blocked via OAuth',
                        referral_code=request.referral_code,
                        email=user_info.email,
                    )
                else:
                    referrer_id = referrer.id
        except Exception:
            logger.warning(
                'Failed to resolve referral code during OAuth', referral_code=request.referral_code, exc_info=True
            )

    # 8. Create new user
    # email_verified отражает подтверждение провайдера (флаг Google; для Yandex/VK —
    # bool(email)): verified-for-UX, чтобы пользователю приходили email-уведомления и
    # работали password recovery / panel sync. Защита от admin escalation завязана НЕ
    # на этот булев флаг, а на email_verification_source: 'oauth_vk'/'oauth_yandex' не
    # входят в TRUSTED_EMAIL_VERIFICATION_SOURCES, поэтому match с ADMIN_EMAILS от
    # недоверенного провайдера не выдаёт Superadmin.
    user = await create_user_by_oauth(
        db=db,
        provider=provider,
        provider_id=user_info.provider_id,
        email=user_info.email,
        email_verified=user_info.email_verified,
        first_name=user_info.first_name,
        last_name=user_info.last_name,
        username=user_info.username,
        referred_by_id=referrer_id,
    )
    logger.info('New OAuth user created', provider=provider, user_id=user.id)

    # Commit user before panel sync (sync does its own commit/rollback)
    await db.commit()

    # Sync existing panel subscriptions by email (if verified)
    if user_info.email and user_info.email_verified:
        try:
            from app.cabinet.routes.auth import _sync_subscription_from_panel_by_email

            await _sync_subscription_from_panel_by_email(db, user)
        except Exception:
            logger.warning('Failed to sync panel subscription for new OAuth user', user_id=user.id, exc_info=True)

    return await _finalize_oauth_login(
        db, user, provider, request.campaign_slug, request.referral_code, is_new_user=True
    )
