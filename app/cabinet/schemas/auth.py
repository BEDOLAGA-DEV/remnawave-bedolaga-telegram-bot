"""Authentication schemas for cabinet."""

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class TelegramAuthRequest(BaseModel):
    """Request for Telegram WebApp initData authentication."""

    init_data: str = Field(..., description='Telegram WebApp initData string')
    campaign_slug: str | None = Field(
        None, min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$', description='Campaign slug from web link'
    )
    referral_code: str | None = Field(None, max_length=32, description='Referral code of inviter')
    yandex_cid: str | None = Field(None, max_length=128, description='Yandex.Metrika ClientID')


class TelegramWidgetAuthRequest(BaseModel):
    """Request for Telegram Login Widget authentication."""

    id: int = Field(..., description='Telegram user ID')
    first_name: str = Field(..., description="User's first name")
    last_name: str | None = Field(None, description="User's last name")
    username: str | None = Field(None, description="User's username")
    photo_url: str | None = Field(None, description="User's photo URL")
    auth_date: int = Field(..., description='Unix timestamp of authentication')
    hash: str = Field(..., description='Authentication hash')
    campaign_slug: str | None = Field(
        None, min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$', description='Campaign slug from web link'
    )
    referral_code: str | None = Field(None, max_length=32, description='Referral code of inviter')
    yandex_cid: str | None = Field(None, max_length=128, description='Yandex.Metrika ClientID')


class EmailRegisterRequest(BaseModel):
    """Request to register/link email to existing Telegram account."""

    email: EmailStr = Field(..., description='Email address')
    password: str = Field(..., min_length=8, max_length=128, description='Password (min 8 chars)')


class EmailVerifyRequest(BaseModel):
    """Request to verify email with token."""

    token: str = Field(..., description='Email verification token')
    campaign_slug: str | None = Field(
        None, min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$', description='Campaign slug from web link'
    )


class EmailLoginRequest(BaseModel):
    """Request to login with email and password."""

    email: EmailStr = Field(..., description='Email address')
    password: str = Field(..., description='Password')
    campaign_slug: str | None = Field(
        None, min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$', description='Campaign slug from web link'
    )
    yandex_cid: str | None = Field(None, max_length=128, description='Yandex.Metrika ClientID')


class RefreshTokenRequest(BaseModel):
    """Request to refresh access token."""

    refresh_token: str = Field(..., description='Refresh token')


class PasswordForgotRequest(BaseModel):
    """Request to initiate password reset."""

    email: EmailStr = Field(..., description='Email address')


class PasswordResetRequest(BaseModel):
    """Request to reset password with token."""

    token: str = Field(..., description='Password reset token')
    password: str = Field(..., min_length=8, max_length=128, description='New password (min 8 chars)')


class TokenResponse(BaseModel):
    """Token pair response."""

    access_token: str
    refresh_token: str
    token_type: str = 'bearer'
    expires_in: int = Field(..., description='Access token expiration in seconds')


class UserResponse(BaseModel):
    """User data response."""

    id: int
    telegram_id: int | None = None  # Nullable для email-only пользователей
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    email_verified: bool = False
    balance_kopeks: int = 0
    balance_rubles: float = 0.0
    referral_code: str | None = None
    language: str = 'ru'
    created_at: datetime
    auth_type: str = 'telegram'  # "telegram" или "email"

    class Config:
        from_attributes = True


class EmailRegisterStandaloneRequest(BaseModel):
    """Request to register new account with email (no Telegram required)."""

    email: EmailStr = Field(..., description='Email address')
    password: str = Field(..., min_length=8, max_length=128, description='Password (min 8 chars)')
    first_name: str | None = Field(None, max_length=64, description='First name')
    language: str = Field('ru', description='Preferred language')
    referral_code: str | None = Field(None, max_length=32, description='Referral code of inviter')
    yandex_cid: str | None = Field(None, max_length=128, description='Yandex.Metrika ClientID')
    auto_verify: bool = Field(False, description='Skip email verification and mark account as verified immediately')


class CampaignBonusInfo(BaseModel):
    """Info about campaign bonus applied during auth."""

    campaign_name: str
    bonus_type: str
    balance_kopeks: int = 0
    subscription_days: int | None = None
    tariff_name: str | None = None


class AuthResponse(BaseModel):
    """Full authentication response with tokens and user."""

    access_token: str
    refresh_token: str
    token_type: str = 'bearer'
    expires_in: int
    user: UserResponse
    campaign_bonus: CampaignBonusInfo | None = None


class RegisterResponse(BaseModel):
    """Response for email registration (before verification)."""

    message: str = Field(..., description='Success message')
    email: str = Field(..., description='Email address to verify')
    requires_verification: bool = Field(True, description='Whether email verification is required')


class EmailChangeRequest(BaseModel):
    """Request to initiate email change."""

    new_email: EmailStr = Field(..., description='New email address')


class EmailChangeVerifyRequest(BaseModel):
    """Request to verify email change with code."""

    code: str = Field(..., min_length=6, max_length=6, description='6-digit verification code')


class EmailChangeResponse(BaseModel):
    """Response for email change initiation."""

    message: str = Field(..., description='Success message')
    new_email: str = Field(..., description='New email address pending verification')
    expires_in_minutes: int = Field(..., description='Code expiration time in minutes')
