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
from app.database.models import User

from ..auth.oauth_providers import (
    OAuthUserInfo,
    generate_oauth_state,
    get_provider,
    validate_oauth_state,
)
from ..dependencies import get_cabinet_db
from ..routes.account_linking import OAuthProviderName
from ..schemas.auth import AuthResponse
from .auth import (
    _create_auth_response,
    _process_campaign_bonus,
    _process_referral_code,
    _process_yandex_cid,
    _store_refresh_token,
    _user_to_response,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/auth/oauth', tags=['Cabinet OAuth'])


async def _finalize_oauth_login(
    db: AsyncSession,
    user: User,
    provider: str,
    campaign_slug: str | None = None,
    referral_code: str | None = None,
    yandex_cid: str | None = None,
) -> AuthResponse:
    """Update last login, create tokens, store refresh token."""
    user.cabinet_last_login = datetime.now(UTC)
    await db.commit()
    auth_response = await _create_auth_response(user, db)
    await _store_refresh_token(db, user.id, auth_response.refresh_token, device_info=f'oauth:{provider}')

    await _process_referral_code(db, user, referral_code)

    # Store Yandex.Metrika CID and fire registration event
    if yandex_cid:
        await _process_yandex_cid(db, user, yandex_cid, source='web')

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
    yandex_cid: str | None = Field(None, max_length=128, description='Yandex.Metrika ClientID')


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
        logger.info('OAuth login for existing user', provider=provider, user_id=user.id)
        return await _finalize_oauth_login(db, user, provider, request.campaign_slug, request.referral_code, request.yandex_cid)

    # 6. Find user by email (if verified) and link provider
    if user_info.email and user_info.email_verified:
        user = await get_user_by_email(db, user_info.email)
        if user:
            await set_user_oauth_provider_id(db, user, provider, user_info.provider_id)
            logger.info('OAuth provider linked to existing email user', provider=provider, user_id=user.id)
            return await _finalize_oauth_login(db, user, provider, request.campaign_slug, request.referral_code, request.yandex_cid)

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
    user = await create_user_by_oauth(
        db=db,
        provider=provider,
        provider_id=user_info.provider_id,
        email=user_info.email if user_info.email_verified else None,
        email_verified=user_info.email_verified,
        first_name=user_info.first_name,
        last_name=user_info.last_name,
        username=user_info.username,
        referred_by_id=referrer_id,
    )
    logger.info('New OAuth user created', provider=provider, user_id=user.id)
    return await _finalize_oauth_login(db, user, provider, request.campaign_slug, request.referral_code, request.yandex_cid)
