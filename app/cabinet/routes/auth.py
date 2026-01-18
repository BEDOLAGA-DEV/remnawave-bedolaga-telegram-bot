"""Authentication routes for cabinet."""

import hashlib
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database.models import User, CabinetRefreshToken
from app.database.crud.user import get_user_by_telegram_id, get_user_by_id, create_user
from app.config import settings

from ..dependencies import get_cabinet_db, get_current_cabinet_user
from ..schemas.auth import (
    TelegramAuthRequest,
    TelegramWidgetAuthRequest,
    EmailRegisterRequest,
    EmailVerifyRequest,
    EmailLoginRequest,
    RefreshTokenRequest,
    PasswordForgotRequest,
    PasswordResetRequest,
    TokenResponse,
    UserResponse,
    AuthResponse,
)
from ..auth import (
    validate_telegram_login_widget,
    validate_telegram_init_data,
    create_access_token,
    create_refresh_token,
    get_token_payload,
    hash_password,
    verify_password,
)
from ..auth.jwt_handler import get_refresh_token_expires_at
from ..auth.email_verification import (
    generate_verification_token,
    generate_password_reset_token,
    get_verification_expires_at,
    get_password_reset_expires_at,
    is_token_expired,
)
from ..services.email_service import email_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Cabinet Auth"])


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
    )


def _create_auth_response(user: User) -> AuthResponse:
    """Create full auth response with tokens."""
    access_token = create_access_token(user.id, user.telegram_id)
    refresh_token = create_refresh_token(user.id)
    expires_in = settings.get_cabinet_access_token_expire_minutes() * 60

    return AuthResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=expires_in,
        user=_user_to_response(user),
    )


async def _store_refresh_token(
    db: AsyncSession,
    user_id: int,
    refresh_token: str,
    device_info: Optional[str] = None,
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
    await db.commit()


@router.post("/telegram", response_model=AuthResponse)
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
            detail="Invalid or expired Telegram authentication data",
        )

    telegram_id = user_data.get("id")
    if not telegram_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Telegram user ID",
        )

    user = await get_user_by_telegram_id(db, telegram_id)

    # Get user data from initData
    tg_username = user_data.get("username")
    tg_first_name = user_data.get("first_name")
    tg_last_name = user_data.get("last_name")
    tg_language = user_data.get("language_code", "ru")

    if not user:
        # Create new user from Telegram initData
        logger.info(f"Creating new user from cabinet (initData): telegram_id={telegram_id}")
        user = await create_user(
            db=db,
            telegram_id=telegram_id,
            username=tg_username,
            first_name=tg_first_name,
            last_name=tg_last_name,
            language=tg_language,
        )
        logger.info(f"User created successfully: id={user.id}, telegram_id={user.telegram_id}")
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
            logger.info(f"User {user.id} profile updated from initData")

    if user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is not active",
        )

    # Update last login
    user.cabinet_last_login = datetime.utcnow()
    await db.commit()

    response = _create_auth_response(user)

    # Store refresh token
    await _store_refresh_token(db, user.id, response.refresh_token)

    return response


@router.post("/telegram/widget", response_model=AuthResponse)
async def auth_telegram_widget(
    request: TelegramWidgetAuthRequest,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Authenticate using Telegram Login Widget data.

    This endpoint validates data from Telegram Login Widget and returns
    JWT tokens for authenticated access.
    """
    widget_data = request.model_dump()

    if not validate_telegram_login_widget(widget_data):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired Telegram authentication data",
        )

    user = await get_user_by_telegram_id(db, request.id)

    if not user:
        # Create new user from Telegram data
        logger.info(f"Creating new user from cabinet: telegram_id={request.id}, username={request.username}")
        user = await create_user(
            db=db,
            telegram_id=request.id,
            username=request.username,
            first_name=request.first_name,
            last_name=request.last_name,
            language="ru",
        )
        logger.info(f"User created successfully: id={user.id}, telegram_id={user.telegram_id}")

    if user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is not active",
        )

    # Update user info from widget data
    if request.username and request.username != user.username:
        user.username = request.username
    if request.first_name and request.first_name != user.first_name:
        user.first_name = request.first_name
    if request.last_name != user.last_name:
        user.last_name = request.last_name

    user.cabinet_last_login = datetime.utcnow()
    await db.commit()

    response = _create_auth_response(user)
    await _store_refresh_token(db, user.id, response.refresh_token)

    return response


@router.post("/email/register")
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
    # Check if email already exists
    existing_user = await db.execute(
        select(User).where(User.email == request.email)
    )
    if existing_user.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This email is already registered",
        )

    # Check if user already has email
    if user.email and user.email_verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You already have a verified email",
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

    # Send verification email
    if email_service.is_configured():
        verification_url = f"{settings.CABINET_URL}/verify-email"
        email_service.send_verification_email(
            to_email=request.email,
            verification_token=verification_token,
            verification_url=verification_url,
            username=user.first_name,
        )

    return {
        "message": "Verification email sent",
        "email": request.email,
    }


@router.post("/email/verify")
async def verify_email(
    request: EmailVerifyRequest,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Verify email with token."""
    # Find user with this token
    result = await db.execute(
        select(User).where(User.email_verification_token == request.token)
    )
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid verification token",
        )

    if is_token_expired(user.email_verification_expires):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Verification token has expired",
        )

    # Mark email as verified
    user.email_verified = True
    user.email_verified_at = datetime.utcnow()
    user.email_verification_token = None
    user.email_verification_expires = None

    await db.commit()

    return {"message": "Email verified successfully"}


@router.post("/email/resend")
async def resend_verification(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Resend verification email."""
    if not user.email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No email address to verify",
        )

    if user.email_verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email is already verified",
        )

    # Generate new token
    verification_token = generate_verification_token()
    verification_expires = get_verification_expires_at()

    user.email_verification_token = verification_token
    user.email_verification_expires = verification_expires

    await db.commit()

    # Send verification email
    if email_service.is_configured():
        verification_url = f"{settings.CABINET_URL}/verify-email"
        email_service.send_verification_email(
            to_email=user.email,
            verification_token=verification_token,
            verification_url=verification_url,
            username=user.first_name,
        )

    return {"message": "Verification email sent"}


@router.post("/email/login", response_model=AuthResponse)
async def login_email(
    request: EmailLoginRequest,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Login with email and password."""
    # Find user by email
    result = await db.execute(
        select(User).where(User.email == request.email)
    )
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    if not user.password_hash:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Password login not configured for this account",
        )

    if not verify_password(request.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    if not user.email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Please verify your email first",
        )

    if user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is not active",
        )

    user.cabinet_last_login = datetime.utcnow()
    await db.commit()

    response = _create_auth_response(user)
    await _store_refresh_token(db, user.id, response.refresh_token)

    return response


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    request: RefreshTokenRequest,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Refresh access token using refresh token."""
    payload = get_token_payload(request.refresh_token, expected_type="refresh")

    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    try:
        user_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
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
            detail="Refresh token not found or revoked",
        )

    if not token_record.is_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token is no longer valid",
        )

    user = await get_user_by_id(db, user_id)

    if not user or user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )

    access_token = create_access_token(user.id, user.telegram_id)
    expires_in = settings.get_cabinet_access_token_expire_minutes() * 60

    return TokenResponse(
        access_token=access_token,
        refresh_token=request.refresh_token,
        token_type="bearer",
        expires_in=expires_in,
    )


@router.post("/logout")
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
        token_record.revoked_at = datetime.utcnow()
        await db.commit()

    return {"message": "Logged out successfully"}


@router.post("/password/forgot")
async def forgot_password(
    request: PasswordForgotRequest,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Request password reset."""
    result = await db.execute(
        select(User).where(User.email == request.email)
    )
    user = result.scalar_one_or_none()

    # Always return success to prevent email enumeration
    if not user or not user.email_verified:
        return {"message": "If the email exists, a password reset link has been sent"}

    # Generate reset token
    reset_token = generate_password_reset_token()
    reset_expires = get_password_reset_expires_at()

    user.password_reset_token = reset_token
    user.password_reset_expires = reset_expires

    await db.commit()

    # Send reset email
    if email_service.is_configured():
        reset_url = f"{settings.CABINET_URL}/reset-password"
        email_service.send_password_reset_email(
            to_email=user.email,
            reset_token=reset_token,
            reset_url=reset_url,
            username=user.first_name,
        )

    return {"message": "If the email exists, a password reset link has been sent"}


@router.post("/password/reset")
async def reset_password(
    request: PasswordResetRequest,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Reset password with token."""
    result = await db.execute(
        select(User).where(User.password_reset_token == request.token)
    )
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid reset token",
        )

    if is_token_expired(user.password_reset_expires):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reset token has expired",
        )

    # Update password
    user.password_hash = hash_password(request.password)
    user.password_reset_token = None
    user.password_reset_expires = None

    await db.commit()

    return {"message": "Password reset successfully"}


@router.get("/me", response_model=UserResponse)
async def get_current_user(
    user: User = Depends(get_current_cabinet_user),
):
    """Get current authenticated user info."""
    return _user_to_response(user)


@router.get("/me/is-admin")
async def check_is_admin(
    user: User = Depends(get_current_cabinet_user),
):
    """Check if current user is an admin."""
    is_admin = settings.is_admin(user.telegram_id)
    return {"is_admin": is_admin}
