"""Authentication routes for cabinet."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlencode

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.campaign import (
    get_campaign_by_start_parameter,
    get_campaign_registration_by_user,
)
from app.database.crud.rbac import UserRoleCRUD
from app.database.crud.system_setting import get_setting_value
from app.database.crud.user import (
    clear_email_change_pending,
    create_user,
    create_user_by_email,
    get_user_by_id,
    get_user_by_referral_code,
    get_user_by_telegram_id,
    is_email_taken,
    set_email_change_pending,
    verify_and_apply_email_change,
)
from app.database.models import CabinetRefreshToken, User, UserStatus
from app.services.campaign_service import AdvertisingCampaignService
from app.services.disposable_email_service import disposable_email_service
from app.services.referral_service import process_referral_registration
from app.services.web_auth_service import (
    WEB_AUTH_TOKEN_TTL,
    consume_web_auth_token,
    create_web_auth_token,
    poll_web_auth_token,
)
from app.utils.cache import RateLimitCache, TokenReplayCache
from app.utils.timezone import panel_datetime_to_utc

from ..auth import (
    create_access_token,
    create_link_token,
    create_refresh_token,
    decode_link_token,
    exchange_authorization_code,
    generate_oidc_nonce,
    generate_pkce_pair,
    get_token_payload,
    hash_password,
    validate_telegram_init_data,
    validate_telegram_login_widget,
    validate_telegram_oidc_token,
    verify_password,
)
from ..auth.oauth_providers import generate_oauth_state, validate_oauth_state
from ..auth.email_verification import (
    generate_email_change_code,
    generate_password_reset_token,
    generate_verification_token,
    get_email_change_expires_at,
    get_password_reset_expires_at,
    get_verification_expires_at,
    is_token_expired,
)
from ..auth.jwt_handler import get_refresh_token_expires_at
from ..auth.merge_service import create_merge_token
from ..dependencies import _optional_cabinet_user, get_cabinet_db, get_current_cabinet_user
from ..ip_utils import get_client_ip
from ..schemas.auth import (
    AuthResponse,
    AutoLoginRequest,
    CampaignBonusInfo,
    DeepLinkPollRequest,
    DeepLinkTokenResponse,
    EmailChangeRequest,
    EmailChangeResponse,
    EmailChangeVerifyRequest,
    EmailLoginRequest,
    EmailRegisterRequest,
    EmailRegisterStandaloneRequest,
    EmailVerifyRequest,
    LinkedProvider,
    LinkedProvidersResponse,
    PasswordForgotRequest,
    PasswordResetRequest,
    RefreshTokenRequest,
    RegisterResponse,
    TelegramAuthRequest,
    TelegramOIDCAuthRequest,
    TelegramOIDCCallbackRequest,
    TelegramOIDCInitRequest,
    TelegramOIDCInitResponse,
    TelegramLinkTokenResponse,
    TelegramWidgetAuthRequest,
    TokenResponse,
    UserResponse,
)
from ..services.email_service import email_service
from ..services.email_template_overrides import get_rendered_override


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/auth', tags=['Cabinet Auth'])


def _user_to_response(user: User) -> UserResponse:
    """Convert User model to UserResponse."""
    return UserResponse(
        id=user.id,
        telegram_id=user.telegram_id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        email=user.email,
        email_verified=user.email_verified,
        balance_kopeks=user.balance_kopeks,
        balance_rubles=user.balance_rubles,
        referral_code=user.referral_code,
        language=user.language,
        created_at=user.created_at,
        auth_type=getattr(user, 'auth_type', 'telegram'),  # Поддержка старых записей
    )


async def _create_auth_response(user: User, db: AsyncSession) -> AuthResponse:
    """Create full auth response with tokens and RBAC permissions."""
    user_permissions, user_role_names, user_role_level = await UserRoleCRUD.get_user_permissions(db, user.id)

    access_token = create_access_token(
        user.id,
        user.telegram_id,
        permissions=user_permissions,
        roles=user_role_names,
        role_level=user_role_level,
    )
    refresh_token = create_refresh_token(user.id)
    expires_in = settings.get_cabinet_access_token_expire_minutes() * 60

    return AuthResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type='bearer',
        expires_in=expires_in,
        user=_user_to_response(user),
        mtproxy_url=settings.MTPROXY_URL,
    )


async def _store_refresh_token(
    db: AsyncSession,
    user_id: int,
    refresh_token: str,
    device_info: str | None = None,
) -> None:
    """Store refresh token hash in database using upsert to avoid duplicate key errors."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
    expires_at = get_refresh_token_expires_at()

    stmt = pg_insert(CabinetRefreshToken).values(
        user_id=user_id,
        token_hash=token_hash,
        device_info=device_info,
        expires_at=expires_at,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=['token_hash'],
        set_={
            'expires_at': expires_at,
            'device_info': device_info,
            'revoked_at': None,
        },
    )
    await db.execute(stmt)
    await db.commit()


async def _process_campaign_bonus(
    db: AsyncSession,
    user: User,
    campaign_slug: str | None,
    telegram_id: int | None = None,
) -> CampaignBonusInfo | None:
    """Process campaign bonus for user during auth. Never raises.

    If ``campaign_slug`` is not provided but ``telegram_id`` is given, the
    function falls back to Redis ``pending_campaign:{telegram_id}`` -- populated
    by the bot's /start handler when a user opens an advertising campaign link
    but then completes registration via the cabinet WebApp (Telegram menu
    button) instead of the bot dialog. The Redis entry is cleared after a
    successful consumption attempt.
    """
    pending_campaign_consumed = False
    if not campaign_slug and telegram_id:
        try:
            from app.services.referral_service import get_pending_campaign

            pending = await get_pending_campaign(telegram_id)
            if pending and pending.get('campaign_slug'):
                campaign_slug = pending['campaign_slug']
                pending_campaign_consumed = True
                logger.info(
                    'Resolved campaign from Redis pending_campaign (cabinet)',
                    telegram_id=telegram_id,
                    campaign_slug=campaign_slug,
                )
        except Exception as e:
            logger.warning('Failed to check pending campaign', error=e)

    if not campaign_slug:
        return None
    try:
        try:
            campaign = await get_campaign_by_start_parameter(db, campaign_slug, only_active=True)
            if not campaign:
                return None

            # Skip if user IS the campaign partner — prevent self-referral
            if campaign.partner_user_id and campaign.partner_user_id == user.id:
                logger.debug(
                    'Skipping campaign attribution: user is the campaign partner',
                    user_id=user.id,
                    campaign_id=campaign.id,
                )
                return None

            # Lock user row to prevent concurrent bonus application (race condition)
            await db.execute(select(User).where(User.id == user.id).with_for_update())

            existing = await get_campaign_registration_by_user(db, user.id)
            if existing:
                logger.debug('User already has campaign registration', user_id=user.id)
                return None

            # Привязать реферала к партнёру кампании (если партнёр назначен и юзер ещё не привязан)
            if campaign.partner_user_id and not user.referred_by_id:
                user.referred_by_id = campaign.partner_user_id
                await db.flush()
                try:
                    from app.bot_factory import create_bot

                    async with create_bot() as bot:
                        await process_referral_registration(db, user.id, campaign.partner_user_id, bot=bot)
                    logger.info(
                        'Referral set from campaign partner',
                        user_id=user.id,
                        partner_user_id=campaign.partner_user_id,
                        campaign_id=campaign.id,
                    )
                except Exception as e:
                    logger.error('Failed to process referral from campaign partner', error=e)

            service = AdvertisingCampaignService()
            result = await service.apply_campaign_bonus(db, user, campaign)
            if not result.success:
                return None

            # Refresh user to get updated balance after bonus
            await db.refresh(user)

            return CampaignBonusInfo(
                campaign_name=campaign.name,
                bonus_type=result.bonus_type or campaign.bonus_type,
                balance_kopeks=result.balance_kopeks,
                subscription_days=result.subscription_days,
                tariff_name=result.tariff_name,
            )
        except Exception:
            logger.exception('Failed to process campaign bonus', user_id=user.id, campaign_slug=campaign_slug)
            try:
                await db.rollback()
                # Re-fetch user so session stays usable for the caller
                await db.refresh(user)
            except Exception:
                logger.exception('Failed to rollback after campaign bonus error', user_id=user.id)
            return None
    finally:
        # Clear Redis pending_campaign whenever we consumed it. Done regardless
        # of success — if processing failed (already applied, race, exception),
        # we don't want to keep retrying on every subsequent login.
        if pending_campaign_consumed and telegram_id:
            try:
                from app.services.referral_service import clear_pending_campaign

                await clear_pending_campaign(telegram_id)
            except Exception:
                pass


async def _process_referral_code(
    db: AsyncSession,
    user: User,
    referral_code: str | None,
    *,
    is_new_user: bool = False,
) -> None:
    """Process referral for a newly created user. Never raises.

    Only applies to new users (is_new_user=True). Existing users cannot be
    assigned a referrer — same logic as the bot /start handler.

    Handles two cases:
    - referred_by_id already set by create_user() → fire registration event
    - referred_by_id not set (resolution failed earlier) → resolve, set, fire
    """
    if not referral_code or not is_new_user:
        return
    try:
        from app.bot_factory import create_bot

        # Lock user row to prevent concurrent referral application (TOCTOU race)
        await db.execute(select(User).where(User.id == user.id).with_for_update())
        await db.refresh(user)

        # Case 1: referred_by_id already set by create_user() — just fire the event
        if user.referred_by_id:
            async with create_bot() as bot:
                await process_referral_registration(db, user.id, user.referred_by_id, bot=bot)
            logger.info(
                'Referral registration processed for pre-set referrer',
                user_id=user.id,
                referrer_id=user.referred_by_id,
            )
            return

        # Case 2: referred_by_id not set — resolve referral code and set it
        referrer = await get_user_by_referral_code(db, referral_code)
        if not referrer:
            return
        if referrer.id == user.id:
            return
        if referrer.email and user.email and referrer.email.lower() == user.email.lower():
            return
        user.referred_by_id = referrer.id
        await db.flush()

        async with create_bot() as bot:
            await process_referral_registration(db, user.id, referrer.id, bot=bot)
        logger.info('Referral applied from code', user_id=user.id, referrer_id=referrer.id, referral_code=referral_code)
    except Exception as e:
        logger.error('Failed to process referral code', error=e, referral_code=referral_code)


async def _sync_subscription_from_panel_by_email(db: AsyncSession, user: User) -> None:
    """
    Check if user has subscription in RemnaWave panel by email and sync it.
    Called after email verification to import existing subscriptions.
    """
    if not user.email:
        return

    user_email = user.email  # Save before try block — ORM access may fail after rollback

    try:
        from app.services.remnawave_service import RemnaWaveService

        service = RemnaWaveService()
        if not service.is_configured:
            return

        async with service.get_api_client() as api:
            # Try to find user by email in panel
            panel_users = await api.get_user_by_email(user.email)

            if not panel_users:
                logger.debug('No subscription found in panel for email', email=user.email)
                return

            # In multi-tariff mode, sync ALL panel users (each = one subscription)
            # In single-tariff mode, process only the first
            from app.database.crud.subscription import get_active_subscriptions_by_user_id, get_subscription_by_user_id
            from app.database.models import Subscription, SubscriptionStatus

            panel_users_to_sync = panel_users if settings.is_multi_tariff_enabled() else panel_users[:1]

            for panel_user in panel_users_to_sync:
                logger.info('Syncing panel subscription for email', email=user.email, uuid=panel_user.uuid)

                # Check if another user already owns this remnawave_uuid
                if settings.is_multi_tariff_enabled():
                    from sqlalchemy import select as _select

                    from app.database.models import Subscription as _Subscription

                    _sub_result = await db.execute(
                        _select(_Subscription).where(_Subscription.remnawave_uuid == panel_user.uuid)
                    )
                    _existing_sub = _sub_result.scalar_one_or_none()
                    if _existing_sub and _existing_sub.user_id != user.id:
                        logger.warning(
                            'Panel UUID already owned by another user subscription, skipping',
                            email=user.email,
                            panel_uuid=panel_user.uuid,
                            existing_owner_id=_existing_sub.user_id,
                        )
                        continue
                else:
                    from app.database.crud.user import get_user_by_remnawave_uuid

                    existing_owner = await get_user_by_remnawave_uuid(db, panel_user.uuid)
                    if existing_owner and existing_owner.id != user.id:
                        logger.warning(
                            'Panel UUID already belongs to another user, skipping',
                            email=user.email,
                            panel_uuid=panel_user.uuid,
                            existing_owner_id=existing_owner.id,
                        )
                        continue

                # Link user to panel (only in single-tariff mode)
                if not settings.is_multi_tariff_enabled():
                    user.remnawave_uuid = panel_user.uuid

                # Find existing subscription
                if settings.is_multi_tariff_enabled():
                    active_subs = await get_active_subscriptions_by_user_id(db, user.id)
                    existing_sub = next(
                        (s for s in active_subs if s.remnawave_uuid == panel_user.uuid),
                        None,
                    )
                else:
                    existing_sub = await get_subscription_by_user_id(db, user.id)

                # Parse panel data
                expire_at = panel_datetime_to_utc(panel_user.expire_at)
                traffic_limit_gb = (
                    panel_user.traffic_limit_bytes // (1024**3) if panel_user.traffic_limit_bytes > 0 else 0
                )
                traffic_used_gb = panel_user.used_traffic_bytes / (1024**3) if panel_user.used_traffic_bytes > 0 else 0
                connected_squads = [
                    s.get('uuid', '') for s in (panel_user.active_internal_squads or []) if s.get('uuid')
                ]
                device_limit = panel_user.hwid_device_limit or 0

                # Determine status
                current_time = datetime.now(UTC)
                if panel_user.status.value == 'ACTIVE' and expire_at > current_time:
                    sub_status = SubscriptionStatus.ACTIVE
                elif expire_at <= current_time:
                    sub_status = SubscriptionStatus.EXPIRED
                else:
                    sub_status = SubscriptionStatus.DISABLED

                if existing_sub:
                    existing_sub.end_date = expire_at
                    existing_sub.traffic_limit_gb = traffic_limit_gb
                    existing_sub.traffic_used_gb = traffic_used_gb
                    existing_sub.status = sub_status.value
                    existing_sub.remnawave_short_uuid = panel_user.short_uuid
                    existing_sub.subscription_url = panel_user.subscription_url
                    existing_sub.subscription_crypto_link = panel_user.happ_crypto_link
                    existing_sub.connected_squads = connected_squads
                    existing_sub.device_limit = device_limit
                    existing_sub.is_trial = False
                    logger.info(
                        'Updated subscription for email user',
                        email=user.email,
                        uuid=panel_user.uuid,
                    )
                else:
                    from app.database.crud.subscription import generate_unique_short_id

                    _short_id = await generate_unique_short_id(db)
                    new_sub = Subscription(
                        user_id=user.id,
                        start_date=current_time,
                        end_date=expire_at,
                        traffic_limit_gb=traffic_limit_gb,
                        traffic_used_gb=traffic_used_gb,
                        status=sub_status.value,
                        is_trial=False,
                        remnawave_uuid=panel_user.uuid if settings.is_multi_tariff_enabled() else None,
                        remnawave_short_id=_short_id,
                        remnawave_short_uuid=panel_user.short_uuid,
                        subscription_url=panel_user.subscription_url,
                        subscription_crypto_link=panel_user.happ_crypto_link,
                        connected_squads=connected_squads,
                        device_limit=device_limit,
                    )
                    db.add(new_sub)
                    logger.info(
                        'Created subscription for email user',
                        email=user.email,
                        uuid=panel_user.uuid,
                    )

            await db.commit()

    except Exception as e:
        logger.warning('Failed to sync subscription from panel for', email=user_email, error=e)
        await db.rollback()
        # Refresh user after rollback — object is expired and lazy loads fail in async
        await db.refresh(user)


@router.post('/telegram', response_model=AuthResponse)
async def auth_telegram(
    request: TelegramAuthRequest,
    raw_request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Authenticate using Telegram WebApp initData.

    This endpoint validates the initData from Telegram WebApp and returns
    JWT tokens for authenticated access.
    """
    client_ip = get_client_ip(raw_request)
    if await RateLimitCache.is_ip_rate_limited(client_ip, 'telegram_initdata', limit=10, window=60, fail_closed=True):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many requests',
            headers={'Retry-After': '60'},
        )
    # Telegram Desktop/iOS cache initData with stale auth_date (known Telegram bug:
    # https://github.com/telegramdesktop/tdesktop/issues/28303).
    # Use generous max_age: HMAC signature proves authenticity,
    # JWT tokens handle actual session expiration after login.
    user_data = validate_telegram_init_data(request.init_data, max_age_seconds=86400 * 30)

    if not user_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid or expired Telegram authentication data',
        )

    telegram_id = user_data.get('id')
    if not telegram_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Missing Telegram user ID',
        )

    user = await get_user_by_telegram_id(db, telegram_id)

    # Get user data from initData
    tg_username = user_data.get('username')
    tg_first_name = user_data.get('first_name')
    tg_last_name = user_data.get('last_name')
    tg_language = user_data.get('language_code', 'ru')

    # Resolve referral code to referrer ID for new users
    referrer_id = None
    if request.referral_code and not user:
        try:
            referrer = await get_user_by_referral_code(db, request.referral_code)
            if referrer:
                # Self-referral protection by telegram_id (user doesn't exist yet, can't compare user.id)
                if referrer.telegram_id and referrer.telegram_id == telegram_id:
                    logger.warning(
                        'Self-referral attempt blocked via telegram_id',
                        telegram_id=telegram_id,
                        referral_code=request.referral_code,
                    )
                else:
                    referrer_id = referrer.id
        except Exception as e:
            logger.warning('Failed to resolve referral code', referral_code=request.referral_code, error=e)

    # Fallback: check Redis for pending referral from /start (user opened cabinet before completing bot registration)
    if not referrer_id and not user and telegram_id:
        try:
            from app.services.referral_service import get_pending_referral

            pending = await get_pending_referral(telegram_id)
            if pending and pending.get('referrer_id'):
                referrer_id = pending['referrer_id']
                logger.info(
                    'Resolved referral from Redis pending_referral (cabinet)',
                    telegram_id=telegram_id,
                    referrer_id=referrer_id,
                )
        except Exception as e:
            logger.warning('Failed to check pending referral', error=e)

    is_new_user = not user
    if not user:
        # Create new user from Telegram initData
        logger.info('Creating new user from cabinet (initData): telegram_id', telegram_id=telegram_id)
        user = await create_user(
            db=db,
            telegram_id=telegram_id,
            username=tg_username,
            first_name=tg_first_name,
            last_name=tg_last_name,
            language=tg_language,
            referred_by_id=referrer_id,
        )
        logger.info('User created successfully: id=, telegram_id', user_id=user.id, telegram_id=user.telegram_id)
    else:
        # Update user info from initData (like bot middleware does)
        updated = False
        if tg_username and tg_username != user.username:
            user.username = tg_username
            updated = True
        if tg_first_name and tg_first_name != user.first_name:
            user.first_name = tg_first_name
            updated = True
        if tg_last_name and tg_last_name != user.last_name:
            user.last_name = tg_last_name
            updated = True
        if updated:
            logger.info('User profile updated from initData', user_id=user.id)

    if user.status != UserStatus.ACTIVE.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='User account is not active',
        )

    # Update last login
    user.cabinet_last_login = datetime.now(UTC)
    await db.commit()

    response = await _create_auth_response(user, db)

    # Store refresh token
    await _store_refresh_token(db, user.id, response.refresh_token)

    # Process referral code (only for new users — existing users cannot be assigned a referrer)
    await _process_referral_code(db, user, request.referral_code, is_new_user=is_new_user)

    # Clear Redis pending referral after successful user creation with referral
    if referrer_id:
        try:
            from app.services.referral_service import clear_pending_referral

            await clear_pending_referral(telegram_id)
        except Exception:
            pass

    # Process campaign bonus.
    # Pass telegram_id so the function can fall back to Redis pending_campaign
    # if the user came via /start <campaign> in the bot but completed
    # registration in the WebApp without an explicit campaign_slug.
    response.campaign_bonus = await _process_campaign_bonus(db, user, request.campaign_slug, telegram_id=telegram_id)
    if response.campaign_bonus:
        response.user = _user_to_response(user)

    return response


@router.post('/telegram/widget', response_model=AuthResponse)
async def auth_telegram_widget(
    request: TelegramWidgetAuthRequest,
    raw_request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Authenticate using Telegram Login Widget data.

    This endpoint validates data from Telegram Login Widget and returns
    JWT tokens for authenticated access.
    """
    # Rate limit
    client_ip = get_client_ip(raw_request)
    if await RateLimitCache.is_ip_rate_limited(client_ip, 'telegram_widget', limit=10, window=60, fail_closed=True):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many requests',
            headers={'Retry-After': '60'},
        )

    widget_data = request.model_dump(exclude={'campaign_slug', 'referral_code'})

    # Generous max_age: Telegram caches auth data with stale auth_date
    if not validate_telegram_login_widget(widget_data, max_age_seconds=86400 * 30):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid or expired Telegram authentication data',
        )

    user = await get_user_by_telegram_id(db, request.id)

    # Resolve referral code to referrer ID for new users
    referrer_id = None
    if request.referral_code and not user:
        try:
            referrer = await get_user_by_referral_code(db, request.referral_code)
            if referrer:
                # Self-referral protection by telegram_id (user doesn't exist yet, can't compare user.id)
                if referrer.telegram_id and referrer.telegram_id == request.id:
                    logger.warning(
                        'Self-referral attempt blocked via telegram_id',
                        telegram_id=request.id,
                        referral_code=request.referral_code,
                    )
                else:
                    referrer_id = referrer.id
        except Exception as e:
            logger.warning('Failed to resolve referral code', referral_code=request.referral_code, error=e)

    is_new_user = not user
    if not user:
        # Create new user from Telegram data
        logger.info(
            'Creating new user from cabinet: telegram_id=, username', request_id=request.id, username=request.username
        )
        user = await create_user(
            db=db,
            telegram_id=request.id,
            username=request.username,
            first_name=request.first_name,
            last_name=request.last_name,
            language='ru',
            referred_by_id=referrer_id,
        )
        logger.info('User created successfully: id=, telegram_id', user_id=user.id, telegram_id=user.telegram_id)

    if user.status != UserStatus.ACTIVE.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='User account is not active',
        )

    # Update user info from widget data
    if request.username and request.username != user.username:
        user.username = request.username
    if request.first_name and request.first_name != user.first_name:
        user.first_name = request.first_name
    if request.last_name != user.last_name:
        user.last_name = request.last_name

    user.cabinet_last_login = datetime.now(UTC)
    await db.commit()

    response = await _create_auth_response(user, db)
    await _store_refresh_token(db, user.id, response.refresh_token)

    # Process referral code (only for new users — existing users cannot be assigned a referrer)
    await _process_referral_code(db, user, request.referral_code, is_new_user=is_new_user)

    # Clear Redis pending referral after successful registration
    if referrer_id and request.id:
        try:
            from app.services.referral_service import clear_pending_referral

            await clear_pending_referral(request.id)
        except Exception:
            pass

    # Process campaign bonus (pending_campaign Redis fallback for Telegram Login Widget)
    response.campaign_bonus = await _process_campaign_bonus(db, user, request.campaign_slug, telegram_id=request.id)
    if response.campaign_bonus:
        response.user = _user_to_response(user)

    return response


async def _create_or_login_user_from_oidc_claims(
    db: AsyncSession,
    claims: dict,
    *,
    campaign_slug: str | None,
    referral_code: str | None,
) -> AuthResponse:
    """Create or fetch a user from validated Telegram OIDC claims and return AuthResponse.

    Shared between the popup endpoint (`/auth/telegram/oidc`) and the Authorization Code
    callback (`/auth/telegram/oidc/callback`). Handles referral resolution, user creation,
    user-info refresh, refresh-token storage, and campaign-bonus application.
    """
    try:
        telegram_id = int(claims.get('id', claims.get('sub', 0)))
    except (ValueError, TypeError) as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid user ID in OIDC claims',
        ) from e
    if not telegram_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Missing user ID in OIDC claims',
        )

    first_name = claims.get('name', claims.get('given_name', ''))
    username = claims.get('preferred_username')
    last_name = claims.get('family_name')
    language = claims.get('locale', 'ru')[:2] if claims.get('locale') else 'ru'

    user = await get_user_by_telegram_id(db, telegram_id)

    referrer_id = None
    if referral_code and not user:
        try:
            referrer = await get_user_by_referral_code(db, referral_code)
            if referrer:
                if referrer.telegram_id and referrer.telegram_id == telegram_id:
                    logger.warning(
                        'Self-referral attempt blocked via telegram_id',
                        telegram_id=telegram_id,
                        referral_code=referral_code,
                    )
                else:
                    referrer_id = referrer.id
        except Exception as e:
            logger.warning('Failed to resolve referral code', referral_code=referral_code, error=e)

    is_new_user = not user
    if not user:
        logger.info('Creating new user from cabinet OIDC', telegram_id=telegram_id, username=username)
        user = await create_user(
            db=db,
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            language=language,
            referred_by_id=referrer_id,
        )
        logger.info('User created successfully', user_id=user.id, telegram_id=user.telegram_id)

    if user.status != UserStatus.ACTIVE.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='User account is not active',
        )

    if username and username != user.username:
        user.username = username
    if first_name and first_name != user.first_name:
        user.first_name = first_name
    if last_name is not None and last_name != user.last_name:
        user.last_name = last_name

    user.cabinet_last_login = datetime.now(UTC)
    await db.commit()

    response = await _create_auth_response(user, db)
    await _store_refresh_token(db, user.id, response.refresh_token)

    await _process_referral_code(db, user, referral_code, is_new_user=is_new_user)

    if referrer_id and telegram_id:
        try:
            from app.services.referral_service import clear_pending_referral

            await clear_pending_referral(telegram_id)
        except Exception:
            pass

    response.campaign_bonus = await _process_campaign_bonus(db, user, campaign_slug)
    if response.campaign_bonus:
        response.user = _user_to_response(user)

    return response


@router.post('/telegram/oidc', response_model=AuthResponse)
async def auth_telegram_oidc(
    request: TelegramOIDCAuthRequest,
    raw_request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Authenticate using Telegram OIDC id_token (popup flow).

    The frontend uses Telegram.Login.init() popup which returns an id_token.
    We validate it via JWKS and create/login the user.
    """
    # Rate limit
    client_ip = get_client_ip(raw_request)
    if await RateLimitCache.is_ip_rate_limited(client_ip, 'telegram_oidc', limit=10, window=60, fail_closed=True):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many requests',
            headers={'Retry-After': '60'},
        )

    # Check OIDC enabled from DB first, fallback to env
    oidc_enabled_val = await get_setting_value(db, 'TELEGRAM_OIDC_ENABLED')
    oidc_client_id_val = await get_setting_value(db, 'TELEGRAM_OIDC_CLIENT_ID')
    oidc_client_id = oidc_client_id_val or settings.TELEGRAM_OIDC_CLIENT_ID
    oidc_enabled = (
        oidc_enabled_val.lower() == 'true' if oidc_enabled_val is not None else settings.TELEGRAM_OIDC_ENABLED
    ) and bool(oidc_client_id)

    if not oidc_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Telegram OIDC is not configured',
        )

    if request.nonce is None:
        logger.info(
            'Telegram OIDC popup token received without nonce (frontend should add nonce)',
            client_ip=client_ip,
        )

    claims = await validate_telegram_oidc_token(
        request.id_token,
        oidc_client_id,
        expected_nonce=request.nonce,
    )
    if not claims:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid or expired Telegram OIDC token',
        )

    # Replay detection: reject if this exact token was already used
    token_hash = hashlib.sha256(request.id_token.encode()).hexdigest()
    token_ttl = max(int(claims.get('exp', 0) - datetime.now(UTC).timestamp()), 60)
    if await TokenReplayCache.is_token_replayed(token_hash, ttl=min(token_ttl, 600)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid or expired Telegram OIDC token',
        )

    return await _create_or_login_user_from_oidc_claims(
        db,
        claims,
        campaign_slug=request.campaign_slug,
        referral_code=request.referral_code,
    )


_OIDC_AUTHORIZE_ENDPOINT = 'https://oauth.telegram.org/auth'
_OIDC_STATE_TTL_SECONDS = 600


async def _resolve_oidc_settings(db: AsyncSession) -> tuple[bool, str, str, str]:
    """Read OIDC settings (DB override → env). Returns (enabled, client_id, client_secret, redirect_uri)."""
    enabled_val = await get_setting_value(db, 'TELEGRAM_OIDC_ENABLED')
    client_id_val = await get_setting_value(db, 'TELEGRAM_OIDC_CLIENT_ID')
    client_secret_val = await get_setting_value(db, 'TELEGRAM_OIDC_CLIENT_SECRET')
    redirect_uri_val = await get_setting_value(db, 'TELEGRAM_OIDC_REDIRECT_URI')

    client_id = client_id_val or settings.TELEGRAM_OIDC_CLIENT_ID
    client_secret = client_secret_val or settings.TELEGRAM_OIDC_CLIENT_SECRET
    redirect_uri = redirect_uri_val or settings.TELEGRAM_OIDC_REDIRECT_URI
    enabled = (
        enabled_val.lower() == 'true' if enabled_val is not None else settings.TELEGRAM_OIDC_ENABLED
    ) and bool(client_id)

    return enabled, client_id, client_secret, redirect_uri


@router.post('/telegram/oidc/init', response_model=TelegramOIDCInitResponse)
async def oidc_init(
    request: TelegramOIDCInitRequest,
    raw_request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
    user: User | None = Depends(_optional_cabinet_user),
):
    """Start the Authorization Code + PKCE flow."""
    client_ip = get_client_ip(raw_request)
    if await RateLimitCache.is_ip_rate_limited(client_ip, 'oidc_init', limit=10, window=60, fail_closed=True):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many requests',
            headers={'Retry-After': '60'},
        )

    user_id: int | None = None
    if request.mode == 'link':
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail='Linking requires authentication',
            )
        if user.telegram_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Telegram is already linked to your account',
            )
        user_id = user.id

    enabled, client_id, _, redirect_uri = await _resolve_oidc_settings(db)
    if not enabled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Telegram OIDC is not configured')
    if not redirect_uri:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Redirect URI not configured')

    code_verifier, code_challenge = generate_pkce_pair()
    nonce = generate_oidc_nonce()

    extra_data: dict[str, str] = {
        'flow': request.mode,
        'code_verifier': code_verifier,
        'nonce': nonce,
    }
    if request.campaign_slug:
        extra_data['campaign_slug'] = request.campaign_slug
    if request.referral_code:
        extra_data['referral_code'] = request.referral_code
    if user_id is not None:
        extra_data['user_id'] = str(user_id)

    state = await generate_oauth_state('telegram', extra_data=extra_data)

    params = {
        'client_id': client_id,
        'response_type': 'code',
        'scope': 'openid profile',
        'redirect_uri': redirect_uri,
        'state': state,
        'code_challenge': code_challenge,
        'code_challenge_method': 'S256',
        'nonce': nonce,
    }
    authorize_url = f'{_OIDC_AUTHORIZE_ENDPOINT}?{urlencode(params)}'

    return TelegramOIDCInitResponse(
        authorize_url=authorize_url,
        state=state,
        expires_in=_OIDC_STATE_TTL_SECONDS,
    )


@router.post('/telegram/oidc/callback')
async def oidc_callback(
    request: TelegramOIDCCallbackRequest,
    raw_request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Authorization Code + PKCE callback. Returns AuthResponse (login) or LinkCallbackResponse (link)."""
    client_ip = get_client_ip(raw_request)
    if await RateLimitCache.is_ip_rate_limited(client_ip, 'oidc_callback', limit=10, window=60, fail_closed=True):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many requests',
            headers={'Retry-After': '60'},
        )

    state_data = await validate_oauth_state(request.state, 'telegram')
    if not state_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid or expired OAuth state',
        )

    flow = state_data.get('flow')
    if flow not in ('login', 'link'):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='OAuth state flow mismatch',
        )

    code_verifier = state_data.get('code_verifier')
    nonce = state_data.get('nonce')
    if not code_verifier or not nonce:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='OAuth state is missing required fields',
        )

    enabled, client_id, client_secret, redirect_uri = await _resolve_oidc_settings(db)
    if not enabled or not redirect_uri or not client_secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Telegram OIDC is not fully configured',
        )

    id_token = await exchange_authorization_code(
        code=request.code,
        code_verifier=code_verifier,
        redirect_uri=redirect_uri,
        client_id=client_id,
        client_secret=client_secret,
    )
    if not id_token:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='Token exchange failed (upstream)',
        )

    claims = await validate_telegram_oidc_token(id_token, client_id, expected_nonce=nonce)
    if not claims:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid or expired Telegram OIDC token',
        )

    token_hash = hashlib.sha256(id_token.encode()).hexdigest()
    token_ttl = max(int(claims.get('exp', 0) - datetime.now(UTC).timestamp()), 60)
    if await TokenReplayCache.is_token_replayed(token_hash, ttl=min(token_ttl, 600)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid or expired Telegram OIDC token',
        )

    if flow == 'login':
        return await _create_or_login_user_from_oidc_claims(
            db,
            claims,
            campaign_slug=state_data.get('campaign_slug'),
            referral_code=state_data.get('referral_code'),
        )

    # flow == 'link'
    from app.cabinet.routes.account_linking import _link_telegram_to_user

    raw_user_id = state_data.get('user_id')
    try:
        state_user_id = int(raw_user_id) if raw_user_id is not None else None
    except (TypeError, ValueError):
        state_user_id = None
    if state_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='OAuth state missing user_id for link flow',
        )

    try:
        telegram_id = int(claims.get('id', claims.get('sub', 0)))
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid user ID in OIDC claims',
        ) from exc

    return await _link_telegram_to_user(
        db,
        state_user_id,
        telegram_id,
        telegram_username=claims.get('preferred_username'),
        telegram_first_name=claims.get('name', claims.get('given_name')),
        telegram_last_name=claims.get('family_name'),
    )


@router.get('/telegram/link-token', response_model=TelegramLinkTokenResponse)
async def get_telegram_link_token(
    user: User = Depends(get_current_cabinet_user),
):
    """
    Generate a temporary token for linking Telegram via bot.

    Requires valid JWT token from current session.
    """
    if user.telegram_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Telegram is already linked to this account',
        )

    token = create_link_token(user.id)
    bot_username = settings.get_bot_username()
    # LINK_TOKEN_EXPIRE_MINUTES is 10
    expires_in = 10 * 60

    bot_link = f'https://t.me/{bot_username}?start=link_{token}'

    return TelegramLinkTokenResponse(
        link_token=token,
        bot_link=bot_link,
        expires_in=expires_in,
        mtproxy_url=settings.MTPROXY_URL,
    )


@router.post('/telegram/link-widget', response_model=UserResponse)
async def link_telegram_widget(
    request: TelegramWidgetAuthRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Link Telegram account using Telegram Login Widget data.

    Requires valid JWT token from current session.
    """
    if user.telegram_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Telegram is already linked to this account',
        )

    widget_data = request.model_dump(exclude={'campaign_slug', 'referral_code'})

    if not validate_telegram_login_widget(widget_data):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid or expired Telegram authentication data',
        )

    # Check if this telegram_id is already used by another user
    existing_tg_user = await get_user_by_telegram_id(db, request.id)
    if existing_tg_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='This Telegram account is already linked to another user',
        )

    # Link Telegram data
    user.telegram_id = request.id
    if request.username:
        user.username = request.username
    if request.first_name:
        user.first_name = request.first_name
    if request.last_name:
        user.last_name = request.last_name

    # If it was email-only account, it's now a 'telegram' account too
    if getattr(user, 'auth_type', 'email') == 'email':
        user.auth_type = 'telegram'

    await db.commit()
    await db.refresh(user)

    return _user_to_response(user)


@router.post('/email/register')
async def register_email(
    request: EmailRegisterRequest,
    raw_request: Request,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Register/link email to existing Telegram account.

    Requires valid JWT token from Telegram authentication.
    Sends verification email to the provided address.
    If the email belongs to another active user, offers account merge.
    """
    # Rate limit
    client_ip = get_client_ip(raw_request)
    if await RateLimitCache.is_ip_rate_limited(client_ip, 'email_register', limit=5, window=60, fail_closed=True):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many requests',
            headers={'Retry-After': '60'},
        )

    # Check if user already has a verified email — block before doing anything else
    if user.email and user.email_verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='You already have a verified email',
        )

    # Check for disposable email
    if disposable_email_service.is_disposable(request.email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Disposable email addresses are not allowed',
        )

    # Check if email already exists (case-insensitive, exclude deleted users)
    email_lower = (request.email or '').strip().lower()
    existing_result = await db.execute(
        select(User).where(
            func.lower(User.email) == email_lower,
            User.status != UserStatus.DELETED.value,
        )
    )
    existing_email_user = existing_result.scalar_one_or_none()
    if existing_email_user:
        if existing_email_user.id == user.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='This email is already linked to your account',
            )
        # Offer account merge instead of blocking
        logger.info(
            'Email register conflict: email already linked to another user, offering merge',
            current_user_id=user.id,
            existing_user_id=existing_email_user.id,
        )
        merge_token = await create_merge_token(
            primary_user_id=user.id,
            secondary_user_id=existing_email_user.id,
            provider='email',
            provider_id=email_lower,
        )
        return {
            'message': 'Account merge required',
            'merge_required': True,
            'merge_token': merge_token,
        }

    # Update user
    user.email = request.email
    user.password_hash = hash_password(request.password)

    if not settings.is_cabinet_email_verification_enabled():
        # Верификация отключена — сразу помечаем email как verified
        user.email_verified = True
        user.email_verified_at = datetime.now(UTC)
        await db.commit()
    else:
        # Generate verification token
        verification_token = generate_verification_token()
        verification_expires = get_verification_expires_at()

        user.email_verified = False
        user.email_verification_token = verification_token
        user.email_verification_expires = verification_expires
        await db.commit()

        # Send verification email asynchronously (smtplib is blocking)
        if email_service.is_configured():
            cabinet_url = settings.CABINET_URL
            verification_url = f'{cabinet_url}/verify-email'
            lang = user.language or 'ru'
            full_url = f'{verification_url}?token={verification_token}'
            expire_hours = settings.get_cabinet_email_verification_expire_hours()

            # Check for admin template override
            override = await get_rendered_override(
                'email_verification',
                lang,
                context={
                    'username': user.first_name or '',
                    'verification_url': full_url,
                    'expire_hours': str(expire_hours),
                },
                db=db,
            )
            custom_subject, custom_body = override or (None, None)

            await asyncio.to_thread(
                email_service.send_verification_email,
                to_email=request.email,
                verification_token=verification_token,
                verification_url=verification_url,
                username=user.first_name,
                language=lang,
                custom_subject=custom_subject,
                custom_body_html=custom_body,
            )

    return {
        'message': 'Email linked successfully'
        if not settings.is_cabinet_email_verification_enabled()
        else 'Verification email sent',
        'email': request.email,
    }


@router.post('/email/register/standalone', response_model=RegisterResponse)
async def register_email_standalone(
    request: EmailRegisterStandaloneRequest,
    raw_request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Register new account with email and password.

    This endpoint creates a new user WITHOUT requiring Telegram authentication.
    An email verification link will be sent to confirm the email address.

    User must verify email before they can login.

    If TEST_EMAIL is configured, test email accounts are auto-verified.
    """
    client_ip = get_client_ip(raw_request)
    if await RateLimitCache.is_ip_rate_limited(client_ip, 'email_register', limit=5, window=60, fail_closed=True):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many requests',
            headers={'Retry-After': '60'},
        )
    # Check if email registration is enabled
    from app.cabinet.routes.branding import _is_email_registration_enabled

    if not await _is_email_registration_enabled(db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Email registration is disabled. Please use Telegram to create an account.',
        )

    # Check if this is a test email registration
    is_test_email = settings.is_test_email(request.email)

    if is_test_email:
        # Validate test email password
        if not settings.validate_test_email_password(request.email, request.password):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Invalid test email password',
            )
        logger.info('Test email registration', email=request.email)

    # Check for disposable email
    if disposable_email_service.is_disposable(request.email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Disposable email addresses are not allowed',
        )

    # Проверить что email не занят (без учёта регистра)
    email_lower = (request.email or '').strip().lower()
    existing = await db.execute(select(User).where(func.lower(User.email) == email_lower))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='This email is already registered',
        )

    # Хешировать пароль
    password_hash = hash_password(request.password)

    # Найти реферера по коду (если указан)
    referrer = None
    if request.referral_code:
        referrer = await get_user_by_referral_code(db, request.referral_code)
        if referrer:
            # Защита от самореферала - нельзя регистрироваться по своему же коду
            if referrer.email and referrer.email.lower() == request.email.lower():
                logger.warning(
                    'Self-referral attempt blocked: email=, code',
                    email=request.email,
                    referral_code=request.referral_code,
                )
                referrer = None
            else:
                logger.info(
                    'Found referrer for email registration: referrer_id=, code',
                    referrer_id=referrer.id,
                    referral_code=request.referral_code,
                )

    # Создать пользователя
    user = await create_user_by_email(
        db=db,
        email=request.email,
        password_hash=password_hash,
        first_name=request.first_name,
        language=request.language,
        referred_by_id=referrer.id if referrer else None,
    )

    # Сохранить campaign_slug для обработки при верификации email
    if request.campaign_slug:
        user.pending_campaign_slug = request.campaign_slug

    # Для тестового email или отключённой верификации - автоматически верифицировать
    if is_test_email or not settings.is_cabinet_email_verification_enabled():
        user.email_verified = True
        user.email_verified_at = datetime.now(UTC)
        await db.commit()
        logger.info('Email auto-verified (test or verification disabled)', email=request.email, user_id=user.id)
        # Sync existing panel subscription (same as manual verification flow)
        try:
            await _sync_subscription_from_panel_by_email(db, user)
        except Exception:
            logger.warning('Failed to sync panel subscription after auto-verify', user_id=user.id, exc_info=True)
        # Process campaign bonus immediately for auto-verified users
        if request.campaign_slug:
            await _process_campaign_bonus(db, user, request.campaign_slug)
            user.pending_campaign_slug = None
            await db.commit()
    else:
        # Сгенерировать токен верификации
        verification_token = generate_verification_token()
        verification_expires = get_verification_expires_at()

        user.email_verification_token = verification_token
        user.email_verification_expires = verification_expires
        await db.commit()

        # Отправить email верификации
        if settings.is_cabinet_email_verification_enabled() and email_service.is_configured():
            cabinet_url = settings.CABINET_URL
            verification_url = f'{cabinet_url}/verify-email'
            lang = user.language or request.language or 'ru'
            full_url = f'{verification_url}?token={verification_token}'
            expire_hours = settings.get_cabinet_email_verification_expire_hours()

            override = await get_rendered_override(
                'email_verification',
                lang,
                context={
                    'username': user.first_name or 'User',
                    'verification_url': full_url,
                    'expire_hours': str(expire_hours),
                },
                db=db,
            )
            custom_subject, custom_body = override or (None, None)

            await asyncio.to_thread(
                email_service.send_verification_email,
                to_email=request.email,
                verification_token=verification_token,
                verification_url=verification_url,
                username=user.first_name or 'User',
                language=lang,
                custom_subject=custom_subject,
                custom_body_html=custom_body,
            )

    # Обработать реферальную регистрацию (если есть реферер)
    if referrer:
        try:
            from app.bot_factory import create_bot

            async with create_bot() as bot:
                await process_referral_registration(db, user.id, referrer.id, bot=bot)
            logger.info(
                'Processed referral registration: user_id=, referrer_id', user_id=user.id, referrer_id=referrer.id
            )
        except Exception as e:
            logger.error('Failed to process referral registration', error=e)
            # Не прерываем регистрацию из-за ошибки реферальной системы

    # Для тестового email - сразу можно логиниться (уже verified)
    # Для обычного email - требуется верификация (если включена)
    verification_required = not is_test_email and settings.is_cabinet_email_verification_enabled()
    return RegisterResponse(
        message='Verification email sent. Please check your inbox.',
        email=request.email,
        requires_verification=verification_required,
    )


@router.post('/email/verify', response_model=AuthResponse)
async def verify_email(
    request: EmailVerifyRequest,
    raw_request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Verify email with token and return auth tokens."""
    client_ip = get_client_ip(raw_request)
    if await RateLimitCache.is_ip_rate_limited(client_ip, 'email_verify', limit=10, window=60, fail_closed=True):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many requests',
            headers={'Retry-After': '60'},
        )
    # Find user with this token
    result = await db.execute(select(User).where(User.email_verification_token == request.token))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid verification token',
        )

    if is_token_expired(user.email_verification_expires):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Verification token has expired',
        )

    # Mark email as verified
    user.email_verified = True
    user.email_verified_at = datetime.now(UTC)
    user.email_verification_token = None
    user.email_verification_expires = None
    user.cabinet_last_login = datetime.now(UTC)

    await db.commit()

    # Check if user has subscription in RemnaWave panel by email
    await _sync_subscription_from_panel_by_email(db, user)

    # Return auth tokens so user is logged in after verification
    response = await _create_auth_response(user, db)
    await _store_refresh_token(db, user.id, response.refresh_token)

    # Process campaign bonus (prefer request param, fallback to saved slug from registration)
    effective_campaign_slug = request.campaign_slug or user.pending_campaign_slug
    response.campaign_bonus = await _process_campaign_bonus(db, user, effective_campaign_slug)
    if user.pending_campaign_slug:
        user.pending_campaign_slug = None
        await db.commit()
    if response.campaign_bonus:
        response.user = _user_to_response(user)

    return response


@router.post('/email/resend')
async def resend_verification(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Resend verification email."""
    if not user.email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='No email address to verify',
        )

    if user.email_verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Email is already verified',
        )

    # Generate new token
    verification_token = generate_verification_token()
    verification_expires = get_verification_expires_at()

    user.email_verification_token = verification_token
    user.email_verification_expires = verification_expires

    await db.commit()

    # Send verification email asynchronously (smtplib is blocking)
    if settings.is_cabinet_email_verification_enabled() and email_service.is_configured():
        cabinet_url = settings.CABINET_URL
        verification_url = f'{cabinet_url}/verify-email'
        lang = user.language or 'ru'
        full_url = f'{verification_url}?token={verification_token}'
        expire_hours = settings.get_cabinet_email_verification_expire_hours()

        override = await get_rendered_override(
            'email_verification',
            lang,
            context={
                'username': user.first_name or '',
                'verification_url': full_url,
                'expire_hours': str(expire_hours),
            },
            db=db,
        )
        custom_subject, custom_body = override or (None, None)

        await asyncio.to_thread(
            email_service.send_verification_email,
            to_email=user.email,
            verification_token=verification_token,
            verification_url=verification_url,
            username=user.first_name,
            language=lang,
            custom_subject=custom_subject,
            custom_body_html=custom_body,
        )
    elif not settings.is_cabinet_email_verification_enabled():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Email verification is disabled',
        )
    elif not email_service.is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Email service is not configured',
        )

    return {'message': 'Verification email sent'}


@router.post('/email/login', response_model=AuthResponse)
async def login_email(
    request: EmailLoginRequest,
    raw_request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Login with email and password.

    Test email accounts (configured via TEST_EMAIL) bypass email verification.
    """
    client_ip = get_client_ip(raw_request)
    if await RateLimitCache.is_ip_rate_limited(client_ip, 'email_login', limit=10, window=60, fail_closed=True):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many requests',
            headers={'Retry-After': '60'},
        )

    # Check if this is a test email login
    is_test_email = settings.is_test_email(request.email)

    # Find user by email (case-insensitive)
    email_lower = (request.email or '').strip().lower()
    result = await db.execute(select(User).where(func.lower(User.email) == email_lower))
    user = result.scalar_one_or_none()

    if not user:
        # For test email - auto-create user if not exists
        if is_test_email and settings.validate_test_email_password(request.email, request.password):
            logger.info('Test email login creating new user', email=request.email)
            password_hash = hash_password(request.password)
            user = await create_user_by_email(
                db=db,
                email=request.email,
                password_hash=password_hash,
                first_name='Test User',
                language='ru',
            )
            user.email_verified = True
            user.email_verified_at = datetime.now(UTC)
            await db.commit()
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail='Invalid email or password',
            )

    if not user.password_hash:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Password login not configured for this account',
        )

    if not verify_password(request.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid email or password',
        )

    # Test email and disabled verification bypass the check
    if not user.email_verified and not is_test_email and settings.is_cabinet_email_verification_enabled():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Please verify your email first',
        )

    if user.status != UserStatus.ACTIVE.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='User account is not active',
        )

    user.cabinet_last_login = datetime.now(UTC)
    await db.commit()

    response = await _create_auth_response(user, db)
    await _store_refresh_token(db, user.id, response.refresh_token)

    # Process campaign bonus
    response.campaign_bonus = await _process_campaign_bonus(db, user, request.campaign_slug)
    if response.campaign_bonus:
        response.user = _user_to_response(user)

    return response


@router.post('/refresh', response_model=TokenResponse)
async def refresh_token(
    request: RefreshTokenRequest,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Refresh access token using refresh token."""
    payload = get_token_payload(request.refresh_token, expected_type='refresh')

    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid or expired refresh token',
        )

    try:
        user_id = int(payload.get('sub'))
    except (TypeError, ValueError) as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid token payload',
        ) from e

    # Verify token exists in database and is not revoked
    token_hash = hashlib.sha256(request.refresh_token.encode()).hexdigest()
    result = await db.execute(
        select(CabinetRefreshToken).where(
            CabinetRefreshToken.token_hash == token_hash,
            CabinetRefreshToken.revoked_at.is_(None),
        )
    )
    token_record = result.scalar_one_or_none()

    if not token_record:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Refresh token not found or revoked',
        )

    if not token_record.is_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Refresh token is no longer valid',
        )

    user = await get_user_by_id(db, user_id)

    if not user or user.status != 'active':
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='User not found or inactive',
        )

    user_permissions, user_role_names, user_role_level = await UserRoleCRUD.get_user_permissions(db, user.id)
    access_token = create_access_token(
        user.id,
        user.telegram_id,
        permissions=user_permissions,
        roles=user_role_names,
        role_level=user_role_level,
    )
    expires_in = settings.get_cabinet_access_token_expire_minutes() * 60

    # Rotate refresh token: revoke the presented token and mint a fresh one.
    # Same DB transaction commits both updates atomically — a crash before commit
    # leaves the original token still valid (caller can retry); a crash after
    # commit means the new token is the only valid one (caller must use it).
    new_refresh_token = create_refresh_token(user.id)
    new_token_hash = hashlib.sha256(new_refresh_token.encode()).hexdigest()
    token_record.revoked_at = datetime.now(UTC)
    db.add(
        CabinetRefreshToken(
            user_id=user.id,
            token_hash=new_token_hash,
            device_info=token_record.device_info,
            expires_at=get_refresh_token_expires_at(),
        )
    )
    await db.commit()

    return TokenResponse(
        access_token=access_token,
        refresh_token=new_refresh_token,
        token_type='bearer',
        expires_in=expires_in,
    )


@router.post('/logout')
async def logout(
    request: RefreshTokenRequest,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Logout and revoke refresh token."""
    token_hash = hashlib.sha256(request.refresh_token.encode()).hexdigest()

    result = await db.execute(
        select(CabinetRefreshToken).where(
            CabinetRefreshToken.token_hash == token_hash,
        )
    )
    token_record = result.scalar_one_or_none()

    if token_record:
        token_record.revoked_at = datetime.now(UTC)
        await db.commit()

    return {'message': 'Logged out successfully'}


@router.post('/login/auto', response_model=AuthResponse)
async def auto_login(
    request: AutoLoginRequest,
    raw_request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Auto-login using a short-lived JWT from guest purchase success page."""
    client_ip = get_client_ip(raw_request)
    if await RateLimitCache.is_ip_rate_limited(client_ip, 'auto_login', limit=5, window=60, fail_closed=True):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many requests',
            headers={'Retry-After': '60'},
        )

    payload = get_token_payload(request.token, expected_type='auto_login')
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid or expired auto-login token',
        )

    try:
        user_id = int(payload['sub'])
    except (KeyError, ValueError, TypeError) as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid token payload',
        ) from e

    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='User not found',
        )

    if user.status != UserStatus.ACTIVE.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Account is deactivated',
        )

    response = await _create_auth_response(user, db)
    await _store_refresh_token(db, user.id, response.refresh_token)
    user.cabinet_last_login = datetime.now(UTC)
    await db.commit()

    return response


@router.post('/password/forgot')
async def forgot_password(
    request: PasswordForgotRequest,
    raw_request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Request password reset."""
    client_ip = get_client_ip(raw_request)
    if await RateLimitCache.is_ip_rate_limited(client_ip, 'password_forgot', limit=3, window=60, fail_closed=True):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many requests',
            headers={'Retry-After': '60'},
        )
    email_lower = (request.email or '').strip().lower()
    result = await db.execute(select(User).where(func.lower(User.email) == email_lower))
    user = result.scalar_one_or_none()

    # Always return success to prevent email enumeration
    if not user:
        return {'message': 'If the email exists, a password reset link has been sent'}

    # Auto-fix guest-created email users who have a password but weren't verified
    if not user.email_verified and user.password_hash and user.auth_type == 'email':
        user.email_verified = True
        user.email_verified_at = datetime.now(UTC)
        await db.commit()

    if not user.email_verified:
        return {'message': 'If the email exists, a password reset link has been sent'}

    # Generate reset token
    reset_token = generate_password_reset_token()
    reset_expires = get_password_reset_expires_at()

    user.password_reset_token = reset_token
    user.password_reset_expires = reset_expires

    await db.commit()

    # Send reset email asynchronously (smtplib is blocking)
    if email_service.is_configured():
        cabinet_url = settings.CABINET_URL
        reset_url = f'{cabinet_url}/reset-password'
        lang = user.language or 'ru'
        full_url = f'{reset_url}?token={reset_token}'
        expire_hours = settings.get_cabinet_password_reset_expire_hours()

        override = await get_rendered_override(
            'password_reset',
            lang,
            context={'username': user.first_name or '', 'reset_url': full_url, 'expire_hours': str(expire_hours)},
            db=db,
        )
        custom_subject, custom_body = override or (None, None)

        await asyncio.to_thread(
            email_service.send_password_reset_email,
            to_email=user.email,
            reset_token=reset_token,
            reset_url=reset_url,
            username=user.first_name,
            language=lang,
            custom_subject=custom_subject,
            custom_body_html=custom_body,
        )

    return {'message': 'If the email exists, a password reset link has been sent'}


@router.post('/password/reset')
async def reset_password(
    request: PasswordResetRequest,
    raw_request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Reset password with token."""
    client_ip = get_client_ip(raw_request)
    if await RateLimitCache.is_ip_rate_limited(client_ip, 'password_reset', limit=5, window=60, fail_closed=True):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many requests',
            headers={'Retry-After': '60'},
        )
    result = await db.execute(select(User).where(User.password_reset_token == request.token))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid reset token',
        )

    if is_token_expired(user.password_reset_expires):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Reset token has expired',
        )

    # Update password
    user.password_hash = hash_password(request.password)
    user.password_reset_token = None
    user.password_reset_expires = None

    await db.commit()

    return {'message': 'Password reset successfully'}


@router.get('/me', response_model=UserResponse)
async def get_current_user(
    user: User = Depends(get_current_cabinet_user),
):
    """Get current authenticated user info."""
    return _user_to_response(user)


@router.get('/me/permissions')
async def get_my_permissions(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get current user's RBAC permissions, roles, and level."""
    from app.services.permission_service import PermissionService

    return await PermissionService.get_user_permissions(db, user.id, user=user)


@router.get('/me/is-admin')
async def check_is_admin(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Check if current user is an admin (legacy config or RBAC)."""
    # Legacy check: config-based admin list
    is_admin = settings.is_admin(telegram_id=user.telegram_id, email=user.email if user.email_verified else None)

    if not is_admin:
        # RBAC check: user has any active role with level > 0
        _permissions, _role_names, max_level = await UserRoleCRUD.get_user_permissions(db, user.id)
        if max_level > 0:
            is_admin = True

    return {'is_admin': is_admin}


@router.post('/email/change', response_model=EmailChangeResponse)
async def request_email_change(
    request: EmailChangeRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Request email change.

    For verified emails: sends a 6-digit verification code to the new email.
    For unverified emails: replaces the email directly and sends verification to the new address.
    """
    if not user.email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='No email address to change',
        )

    # Check if new email is the same as current
    if request.new_email.lower() == user.email.lower():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='New email is the same as current email',
        )

    # Check for disposable email
    if disposable_email_service.is_disposable(request.new_email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Disposable email addresses are not allowed',
        )

    # Check if new email is already taken
    if await is_email_taken(db, request.new_email, exclude_user_id=user.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='This email is already registered',
        )

    # Unverified email: replace directly and send verification to new address
    if not user.email_verified:
        old_email = user.email
        user.email = request.new_email.lower()
        user.email_verified = False

        verification_token = generate_verification_token()
        verification_expires = get_verification_expires_at()
        user.email_verification_token = verification_token
        user.email_verification_expires = verification_expires

        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='This email is already registered',
            )

        if settings.is_cabinet_email_verification_enabled() and email_service.is_configured():
            cabinet_url = settings.CABINET_URL
            verification_url = f'{cabinet_url}/verify-email'
            lang = user.language or 'ru'
            full_url = f'{verification_url}?token={verification_token}'
            expire_hours = settings.get_cabinet_email_verification_expire_hours()

            override = await get_rendered_override(
                'email_verification',
                lang,
                context={
                    'username': user.first_name or '',
                    'verification_url': full_url,
                    'expire_hours': str(expire_hours),
                },
                db=db,
            )
            custom_subject, custom_body = override or (None, None)

            try:
                await asyncio.to_thread(
                    email_service.send_verification_email,
                    to_email=request.new_email,
                    verification_token=verification_token,
                    verification_url=verification_url,
                    username=user.first_name,
                    language=lang,
                    custom_subject=custom_subject,
                    custom_body_html=custom_body,
                )
            except Exception as e:
                logger.error(
                    'Failed to send verification email to for user',
                    new_email=request.new_email,
                    user_id=user.id,
                    error=e,
                )

        logger.info(
            'Unverified email replaced for user', user_id=user.id, old_email=old_email, new_email=request.new_email
        )

        return EmailChangeResponse(
            message='Email replaced, verification sent to new address',
            new_email=request.new_email,
            expires_in_minutes=0,
        )

    # Verified email: send code to new address for confirmation
    # Generate verification code
    code = generate_email_change_code()
    expires_at = get_email_change_expires_at()
    expire_minutes = settings.get_cabinet_email_change_code_expire_minutes()

    # Save pending email change
    await set_email_change_pending(db, user, request.new_email, code, expires_at)

    # Send verification email to new address
    if email_service.is_configured():
        lang = user.language or 'ru'

        # Check for admin template override
        override = await get_rendered_override(
            'email_change_code',
            lang,
            context={
                'username': user.first_name or '',
                'code': code,
                'expire_minutes': str(expire_minutes),
            },
            db=db,
        )
        custom_subject, custom_body = override or (None, None)

        await asyncio.to_thread(
            email_service.send_email_change_code,
            to_email=request.new_email,
            code=code,
            username=user.first_name,
            language=lang,
            custom_subject=custom_subject,
            custom_body_html=custom_body,
        )
    else:
        # Clear pending change if email service is not configured
        await clear_email_change_pending(db, user)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Email service is not configured',
        )

    logger.info('Email change requested for user', user_id=user.id, email=user.email, new_email=request.new_email)

    return EmailChangeResponse(
        message='Verification code sent to new email',
        new_email=request.new_email,
        expires_in_minutes=expire_minutes,
    )


@router.post('/email/change/verify')
async def verify_email_change(
    request: EmailChangeVerifyRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Verify email change with code.

    Completes the email change process if the code is valid.
    """
    success, message = await verify_and_apply_email_change(db, user, request.code)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=message,
        )

    return {
        'message': message,
        'new_email': user.email,
    }


@router.post('/email/change/cancel')
async def cancel_email_change(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Cancel pending email change.
    """
    if not user.email_change_new:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='No pending email change',
        )

    await clear_email_change_pending(db, user)

    return {'message': 'Email change cancelled'}


@router.get('/account/linked-providers', response_model=LinkedProvidersResponse)
async def get_linked_providers(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Get a list of all authentication providers and their link status for the current user.
    """
    providers = []

    # Telegram
    providers.append(
        LinkedProvider(
            provider='telegram',
            is_linked=user.telegram_id is not None,
            provider_id=str(user.telegram_id) if user.telegram_id else None,
            display_name=user.username or user.first_name,
        )
    )

    # Email
    providers.append(
        LinkedProvider(
            provider='email',
            is_linked=user.email is not None,
            provider_id=user.email,
            display_name=user.email,
        )
    )

    # OAuth Providers
    oauth_config = settings.get_oauth_providers_config()
    for name, cfg in oauth_config.items():
        if not cfg.get('enabled'):
            continue

        column_name = f'{name}_id'
        provider_id = getattr(user, column_name, None)

        providers.append(
            LinkedProvider(
                provider=name,
                is_linked=provider_id is not None,
                provider_id=str(provider_id) if provider_id else None,
                display_name=cfg.get('display_name', name),
            )
        )

    return LinkedProvidersResponse(providers=providers)


@router.post('/account/unlink/{provider}')
async def unlink_provider(
    provider: str,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Unlink an authentication provider from the current account.
    Prevents unlinking the last remaining authentication method.
    """
    # Count linked methods
    methods = 0
    if user.telegram_id:
        methods += 1
    if user.email and user.password_hash:
        methods += 1

    oauth_config = settings.get_oauth_providers_config()
    linked_oauth = []
    for name in oauth_config:
        if getattr(user, f'{name}_id', None):
            methods += 1
            linked_oauth.append(name)

    if methods <= 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Cannot unlink the last remaining authentication method',
        )

    if provider == 'telegram':
        user.telegram_id = None
        user.username = None
    elif provider == 'email':
        user.email = None
        user.password_hash = None
        user.email_verified = False
    elif provider in oauth_config:
        setattr(user, f'{provider}_id', None)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Unknown provider: {provider}',
        )

    await db.commit()
    logger.info('User unlinked provider', user_id=user.id, provider=provider)

    return {'message': f'Provider {provider} unlinked successfully'}


@router.post('/account/link/telegram', response_model=UserResponse)
async def link_telegram_standard(
    request: TelegramAuthRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Standardize Telegram linking (similar to login but for already logged in user).
    Used for Telegram WebApp initData linking.
    """
    if user.telegram_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Telegram is already linked to this account',
        )

    if not validate_telegram_init_data(request.init_data):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid Telegram authentication data',
        )

    # Simplified parsing for linking (just need the ID)
    data = parse_qs(request.init_data)
    user_data = json.loads(data['user'][0])
    tg_id = user_data['id']

    # Check if this telegram_id is already used
    existing_tg_user = await get_user_by_telegram_id(db, tg_id)
    if existing_tg_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='This Telegram account is already linked to another user',
        )

    user.telegram_id = tg_id
    user.username = user_data.get('username')
    user.first_name = user_data.get('first_name')
    user.last_name = user_data.get('last_name')

    await db.commit()
    await db.refresh(user)

    return _user_to_response(user)


@router.get('/account/link/{provider}/init')
async def link_provider_init(
    provider: str,
    user: User = Depends(get_current_cabinet_user),
):
    """
    Initialize OAuth linking process.
    """
    from .oauth import get_oauth_authorize_url

    return {
        'pending': True,
        'new_email': user.email_change_new,
        'expires_at': user.email_change_expires.isoformat() if user.email_change_expires else None,
    }


# --- Deep link auth (fallback when oauth.telegram.org is blocked) ---


@router.post('/deeplink/request', response_model=DeepLinkTokenResponse)
async def request_deep_link_token(
    raw_request: Request,
):
    """Generate a one-time deep link auth token.

    Frontend shows t.me/{bot}?start=webauth_{token} to the user.
    No auth required (user is not logged in yet).
    """
    client_ip = get_client_ip(raw_request)
    if await RateLimitCache.is_ip_rate_limited(client_ip, 'deeplink_request', limit=10, window=60, fail_closed=True):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many requests',
            headers={'Retry-After': '60'},
        )

    try:
        token = await create_web_auth_token()
    except RuntimeError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Service temporarily unavailable',
        )

    bot_username = settings.get_bot_username()
    if not bot_username:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Bot not configured',
        )

    return DeepLinkTokenResponse(
        token=token,
        bot_username=bot_username,
        expires_in=WEB_AUTH_TOKEN_TTL,
    )


@router.post('/deeplink/poll', response_model=AuthResponse)
async def poll_deep_link_token(
    request: DeepLinkPollRequest,
    raw_request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Poll for deep link auth completion.

    Returns 202 if still pending, AuthResponse if completed, 410 if expired.
    """
    client_ip = get_client_ip(raw_request)
    if await RateLimitCache.is_ip_rate_limited(client_ip, 'deeplink_poll', limit=60, window=60, fail_closed=True):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many requests',
            headers={'Retry-After': '60'},
        )

    data = await poll_web_auth_token(request.token)

    if data is None:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail='Token expired or not found',
        )

    if data.get('status') == 'pending':
        raise HTTPException(
            status_code=status.HTTP_202_ACCEPTED,
            detail='Waiting for confirmation',
        )

    if data.get('status') != 'linked':
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail='Invalid token state',
        )

    # Token is linked - consume it atomically
    consumed = await consume_web_auth_token(request.token)
    if not consumed:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail='Token already consumed',
        )

    user_id = consumed.get('user_id')
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Invalid token data',
        )

    user = await get_user_by_id(db, int(user_id))
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='User not found',
        )

    if user.status != UserStatus.ACTIVE.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Account is deactivated',
        )

    user.cabinet_last_login = datetime.now(UTC)
    await db.commit()

    response = await _create_auth_response(user, db)
    await _store_refresh_token(db, user.id, response.refresh_token, device_info='deep_link')

    # Deep link auth is always for existing users — referral code not applicable
    # (kept for campaign bonus processing only)

    # Process campaign bonus
    response.campaign_bonus = await _process_campaign_bonus(db, user, request.campaign_slug)
    if response.campaign_bonus:
        response.user = _user_to_response(user)

    logger.info('Deep link auth successful', user_id=user.id, telegram_id=user.telegram_id)

    return response
    # We reuse the same authorize logic but we might want to flag it as "linking" in state
    # Actually, current oauth routes are prefix /cabinet/auth/oauth
    # Frontend calls /cabinet/auth/account/link/{provider}/init
    # I'll redirect to the oauth authorize url or mirror the logic

    from ..auth.oauth_providers import generate_oauth_state, get_provider

    oauth_provider = get_provider(provider)
    if not oauth_provider:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Requested OAuth provider is not available',
        )

    # Check if already linked
    if getattr(user, f'{provider}_id', None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'{provider} is already linked to this account',
        )

    auth_extra = oauth_provider.prepare_auth_state()
    # Add 'link_user_id' to state to ensure we link to the CORRECT user on callback
    state = await generate_oauth_state(provider, extra_data={'link_user_id': user.id, **(auth_extra or {})})
    authorize_url = oauth_provider.get_authorization_url(state, **auth_extra)

    return {'authorize_url': authorize_url, 'state': state}


@router.post('/account/link/{provider}/callback')
async def link_provider_callback(
    provider: str,
    request: dict,  # Use dict to be flexible with frontend data
    db: AsyncSession = Depends(get_cabinet_db),
    user: User = Depends(get_current_cabinet_user),
):
    """
    Handle OAuth callback for account linking.
    """
    from ..auth.oauth_providers import (
        OAuthUserInfo,
        get_provider,
        validate_oauth_state,
    )

    code = request.get('code')
    state = request.get('state')
    device_id = request.get('device_id')

    if not code or not state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Missing code or state',
        )

    # 1. Validate CSRF state
    state_data = await validate_oauth_state(state, provider)
    if not state_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid or expired OAuth state',
        )

    # Verify this state was meant for linking and for THIS user
    if state_data.get('link_user_id') != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='OAuth state mismatch for account linking',
        )

    # 2. Get provider instance
    oauth_provider = get_provider(provider)
    if not oauth_provider:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Requested OAuth provider is not available',
        )

    # 3. Exchange code for tokens
    exchange_kwargs: dict[str, str] = {'state': state}
    code_verifier = state_data.get('code_verifier')
    if code_verifier:
        exchange_kwargs['code_verifier'] = code_verifier
    if device_id:
        exchange_kwargs['device_id'] = device_id

    try:
        token_data = await oauth_provider.exchange_code(code, **exchange_kwargs)
    except Exception as exc:
        logger.error('OAuth code exchange failed for linking', provider=provider, exc_info=exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Failed to exchange authorization code',
        ) from exc

    # 4. Fetch user info from provider
    try:
        user_info: OAuthUserInfo = await oauth_provider.get_user_info(token_data)
    except Exception as exc:
        logger.error('OAuth user info fetch failed for linking', provider=provider, exc_info=exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Failed to fetch user information from provider',
        ) from exc

    # 5. Check if this provider ID is already used by another user
    from app.database.crud.user import (
        get_user_by_oauth_provider,
        set_user_oauth_provider_id,
    )

    existing_user = await get_user_by_oauth_provider(db, provider, user_info.provider_id)
    if existing_user and existing_user.id != user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'This {provider} account is already linked to another user',
        )

    # 6. Link provider
    await set_user_oauth_provider_id(db, user, provider, user_info.provider_id)
    await db.commit()

    logger.info('OAuth provider linked via callback', provider=provider, user_id=user.id)

    return _user_to_response(user)
