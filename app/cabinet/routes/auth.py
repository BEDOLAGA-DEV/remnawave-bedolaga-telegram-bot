"""Authentication routes for cabinet."""

import asyncio
import hashlib
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.campaign import (
    get_campaign_by_start_parameter,
    get_campaign_registration_by_user,
)
from app.database.crud.rbac import UserRoleCRUD
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
from app.database.models import CabinetRefreshToken, User
from app.services.campaign_service import AdvertisingCampaignService
from app.services.disposable_email_service import disposable_email_service
from app.services.referral_service import process_referral_registration
from app.utils.timezone import panel_datetime_to_utc

from ..auth import (
    create_access_token,
    create_refresh_token,
    get_token_payload,
    hash_password,
    validate_telegram_init_data,
    validate_telegram_login_widget,
    verify_password,
)
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
from ..dependencies import get_cabinet_db, get_current_cabinet_user
from ..schemas.auth import (
    AuthResponse,
    CampaignBonusInfo,
    EmailChangeRequest,
    EmailChangeResponse,
    EmailChangeVerifyRequest,
    EmailLoginRequest,
    EmailRegisterRequest,
    EmailRegisterStandaloneRequest,
    EmailVerifyRequest,
    PasswordForgotRequest,
    PasswordResetRequest,
    RefreshTokenRequest,
    RegisterResponse,
    TelegramAuthRequest,
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
    )


async def _store_refresh_token(
    db: AsyncSession,
    user_id: int,
    refresh_token: str,
    device_info: str | None = None,
) -> None:
    """Store refresh token hash in database."""
    token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
    expires_at = get_refresh_token_expires_at()

    token_record = CabinetRefreshToken(
        user_id=user_id,
        token_hash=token_hash,
        device_info=device_info,
        expires_at=expires_at,
    )
    db.add(token_record)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        logger.debug('Refresh token already exists (duplicate)', user_id=user_id)


async def _process_campaign_bonus(
    db: AsyncSession,
    user: User,
    campaign_slug: str | None,
) -> CampaignBonusInfo | None:
    """Process campaign bonus for user during auth. Never raises."""
    if not campaign_slug:
        return None
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
                await process_referral_registration(db, user.id, campaign.partner_user_id, bot=None)
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


async def _process_referral_code(
    db: AsyncSession,
    user: User,
    referral_code: str | None,
) -> None:
    """Set referred_by_id for user if referral_code is valid. Never raises."""
    if not referral_code or user.referred_by_id:
        return
    try:
        referrer = await get_user_by_referral_code(db, referral_code)
        if not referrer:
            return
        if referrer.id == user.id:
            return
        if referrer.email and user.email and referrer.email.lower() == user.email.lower():
            return
        user.referred_by_id = referrer.id
        await db.flush()
        await process_referral_registration(db, user.id, referrer.id, bot=None)
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

            # Take first user if multiple found
            panel_user = panel_users[0]
            logger.info('Found subscription in panel for email', email=user.email, uuid=panel_user.uuid)

            # Link user to panel
            user.remnawave_uuid = panel_user.uuid

            # Create or update subscription
            from app.database.crud.subscription import get_subscription_by_user_id
            from app.database.models import Subscription, SubscriptionStatus

            existing_sub = await get_subscription_by_user_id(db, user.id)

            # Parse panel data — panel returns local time with misleading +00:00 offset
            expire_at = panel_datetime_to_utc(panel_user.expire_at)
            traffic_limit_gb = panel_user.traffic_limit_bytes // (1024**3) if panel_user.traffic_limit_bytes > 0 else 0
            traffic_used_gb = panel_user.used_traffic_bytes / (1024**3) if panel_user.used_traffic_bytes > 0 else 0

            # Extract squad UUIDs from active_internal_squads
            connected_squads = [s.get('uuid', '') for s in (panel_user.active_internal_squads or []) if s.get('uuid')]

            # Device limit from panel
            device_limit = panel_user.hwid_device_limit or 1

            # Determine status — expire_at is now naive UTC
            current_time = datetime.now(UTC)

            if panel_user.status.value == 'ACTIVE' and expire_at > current_time:
                sub_status = SubscriptionStatus.ACTIVE
            elif expire_at <= current_time:
                sub_status = SubscriptionStatus.EXPIRED
            else:
                sub_status = SubscriptionStatus.DISABLED

            if existing_sub:
                # Update existing subscription (expire_at already naive UTC)
                existing_sub.end_date = expire_at
                existing_sub.traffic_limit_gb = traffic_limit_gb
                existing_sub.traffic_used_gb = traffic_used_gb
                existing_sub.status = sub_status.value
                existing_sub.remnawave_short_uuid = panel_user.short_uuid
                existing_sub.subscription_url = panel_user.subscription_url
                existing_sub.subscription_crypto_link = panel_user.happ_crypto_link
                existing_sub.connected_squads = connected_squads
                existing_sub.device_limit = device_limit
                existing_sub.is_trial = False  # Panel subscription is not trial
                logger.info(
                    'Updated subscription for email user squads: devices',
                    email=user.email,
                    connected_squads=connected_squads,
                    device_limit=device_limit,
                )
            else:
                # Create new subscription (expire_at and current_time already naive UTC)
                new_sub = Subscription(
                    user_id=user.id,
                    start_date=current_time,
                    end_date=expire_at,
                    traffic_limit_gb=traffic_limit_gb,
                    traffic_used_gb=traffic_used_gb,
                    status=sub_status.value,
                    is_trial=False,
                    remnawave_short_uuid=panel_user.short_uuid,
                    subscription_url=panel_user.subscription_url,
                    subscription_crypto_link=panel_user.happ_crypto_link,
                    connected_squads=connected_squads,
                    device_limit=device_limit,
                )
                db.add(new_sub)
                logger.info(
                    'Created subscription for email user squads: devices',
                    email=user.email,
                    connected_squads=connected_squads,
                    device_limit=device_limit,
                )

            await db.commit()

    except Exception as e:
        logger.warning('Failed to sync subscription from panel for', email=user.email, error=e)
        # Don't rollback - it detaches user object and breaks subsequent operations
        # The sync is non-critical, main verification already succeeded


@router.post('/telegram', response_model=AuthResponse)
async def auth_telegram(
    request: TelegramAuthRequest,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Authenticate using Telegram WebApp initData.

    This endpoint validates the initData from Telegram WebApp and returns
    JWT tokens for authenticated access.
    """
    user_data = validate_telegram_init_data(request.init_data)

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
                referrer_id = referrer.id
        except Exception as e:
            logger.warning('Failed to resolve referral code', referral_code=request.referral_code, error=e)

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

    if user.status != 'active':
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

    # Process referral code (before campaign bonus, which may also set referrer)
    await _process_referral_code(db, user, request.referral_code)

    # Process campaign bonus
    response.campaign_bonus = await _process_campaign_bonus(db, user, request.campaign_slug)
    if response.campaign_bonus:
        response.user = _user_to_response(user)

    return response


@router.post('/telegram/widget', response_model=AuthResponse)
async def auth_telegram_widget(
    request: TelegramWidgetAuthRequest,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Authenticate using Telegram Login Widget data.

    This endpoint validates data from Telegram Login Widget and returns
    JWT tokens for authenticated access.
    """
    widget_data = request.model_dump(exclude={'campaign_slug', 'referral_code'})

    if not validate_telegram_login_widget(widget_data):
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
                referrer_id = referrer.id
        except Exception as e:
            logger.warning('Failed to resolve referral code', referral_code=request.referral_code, error=e)

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

    if user.status != 'active':
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

    # Process referral code (before campaign bonus, which may also set referrer)
    await _process_referral_code(db, user, request.referral_code)

    # Process campaign bonus
    response.campaign_bonus = await _process_campaign_bonus(db, user, request.campaign_slug)
    if response.campaign_bonus:
        response.user = _user_to_response(user)

    return response


@router.post('/email/register')
async def register_email(
    request: EmailRegisterRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Register/link email to existing Telegram account.

    Requires valid JWT token from Telegram authentication.
    Sends verification email to the provided address.
    """
    # Check for disposable email
    if disposable_email_service.is_disposable(request.email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Disposable email addresses are not allowed',
        )

    # Check if email already exists
    existing_user = await db.execute(select(User).where(User.email == request.email))
    if existing_user.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='This email is already registered',
        )

    # Check if user already has email
    if user.email and user.email_verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='You already have a verified email',
        )

    # Generate verification token
    verification_token = generate_verification_token()
    verification_expires = get_verification_expires_at()

    # Update user
    user.email = request.email
    user.email_verified = False
    user.password_hash = hash_password(request.password)
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
        custom_subject, custom_body = override if override else (None, None)

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
        'message': 'Verification email sent',
        'email': request.email,
    }


@router.post('/email/register/standalone', response_model=RegisterResponse)
async def register_email_standalone(
    request: EmailRegisterStandaloneRequest,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Register new account with email and password.

    This endpoint creates a new user WITHOUT requiring Telegram authentication.
    An email verification link will be sent to confirm the email address.

    User must verify email before they can login.

    If TEST_EMAIL is configured, test email accounts are auto-verified.
    """
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

    # Проверить что email не занят
    existing = await db.execute(select(User).where(User.email == request.email))
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

    # Для тестового email - автоматически верифицировать
    if is_test_email:
        user.email_verified = True
        user.email_verified_at = datetime.now(UTC)
        await db.commit()
        logger.info('Test email auto-verified: user_id', email=request.email, user_id=user.id)
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
            custom_subject, custom_body = override if override else (None, None)

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
            await process_referral_registration(db, user.id, referrer.id, bot=None)
            logger.info(
                'Processed referral registration: user_id=, referrer_id', user_id=user.id, referrer_id=referrer.id
            )
        except Exception as e:
            logger.error('Failed to process referral registration', error=e)
            # Не прерываем регистрацию из-за ошибки реферальной системы

    # Для тестового email - сразу можно логиниться (уже verified)
    # Для обычного email - требуется верификация
    return RegisterResponse(
        message='Verification email sent. Please check your inbox.',
        email=request.email,
        requires_verification=not is_test_email,
    )


@router.post('/email/verify', response_model=AuthResponse)
async def verify_email(
    request: EmailVerifyRequest,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Verify email with token and return auth tokens."""
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

    # Process campaign bonus
    response.campaign_bonus = await _process_campaign_bonus(db, user, request.campaign_slug)
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
        custom_subject, custom_body = override if override else (None, None)

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
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Login with email and password.

    Test email accounts (configured via TEST_EMAIL) bypass email verification.
    """
    # Check if this is a test email login
    is_test_email = settings.is_test_email(request.email)

    # Find user by email
    result = await db.execute(select(User).where(User.email == request.email))
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

    # Test email bypasses verification check
    if not user.email_verified and not is_test_email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Please verify your email first',
        )

    if user.status != 'active':
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
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid token payload',
        )

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

    return TokenResponse(
        access_token=access_token,
        refresh_token=request.refresh_token,
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


@router.post('/password/forgot')
async def forgot_password(
    request: PasswordForgotRequest,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Request password reset."""
    result = await db.execute(select(User).where(User.email == request.email))
    user = result.scalar_one_or_none()

    # Always return success to prevent email enumeration
    if not user or not user.email_verified:
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
        custom_subject, custom_body = override if override else (None, None)

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
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Reset password with token."""
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
            custom_subject, custom_body = override if override else (None, None)

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
        custom_subject, custom_body = override if override else (None, None)

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


@router.get('/email/change/status')
async def get_email_change_status(
    user: User = Depends(get_current_cabinet_user),
):
    """
    Get pending email change status.
    """
    if not user.email_change_new:
        return {
            'pending': False,
            'new_email': None,
            'expires_at': None,
        }

    return {
        'pending': True,
        'new_email': user.email_change_new,
        'expires_at': user.email_change_expires.isoformat() if user.email_change_expires else None,
    }
