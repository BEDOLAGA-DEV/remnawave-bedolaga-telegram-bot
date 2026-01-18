"""Authentication schemas for cabinet."""

from datetime import datetime
from typing import Optional, Dict, Any
from pydantic import BaseModel, EmailStr, Field


class TelegramAuthRequest(BaseModel):
    """Request for Telegram WebApp initData authentication."""
    init_data: str = Field(..., description="Telegram WebApp initData string")


class TelegramWidgetAuthRequest(BaseModel):
    """Request for Telegram Login Widget authentication."""
    id: int = Field(..., description="Telegram user ID")
    first_name: str = Field(..., description="User's first name")
    last_name: Optional[str] = Field(None, description="User's last name")
    username: Optional[str] = Field(None, description="User's username")
    photo_url: Optional[str] = Field(None, description="User's photo URL")
    auth_date: int = Field(..., description="Unix timestamp of authentication")
    hash: str = Field(..., description="Authentication hash")


class EmailRegisterRequest(BaseModel):
    """Request to register/link email to existing Telegram account."""
    email: EmailStr = Field(..., description="Email address")
    password: str = Field(..., min_length=8, max_length=128, description="Password (min 8 chars)")
    first_name: Optional[str] = Field(None, description="First Name of the user")
    last_name: Optional[str] = Field(None, description="Last Name of the user")


class EmailVerifyRequest(BaseModel):
    """Request to verify email with token."""
    token: str = Field(..., description="Email verification token")


class EmailLoginRequest(BaseModel):
    """Request to login with email and password."""
    email: EmailStr = Field(..., description="Email address")
    password: str = Field(..., description="Password")


class RefreshTokenRequest(BaseModel):
    """Request to refresh access token."""
    refresh_token: str = Field(..., description="Refresh token")


class PasswordForgotRequest(BaseModel):
    """Request to initiate password reset."""
    email: EmailStr = Field(..., description="Email address")


class PasswordResetRequest(BaseModel):
    """Request to reset password with token."""
    token: str = Field(..., description="Password reset token")
    password: str = Field(..., min_length=8, max_length=128, description="New password (min 8 chars)")


class TokenResponse(BaseModel):
    """Token pair response."""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="Access token expiration in seconds")


class UserResponse(BaseModel):
    """User data response."""
    id: int
    telegram_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    email_verified: bool = False
    balance_kopeks: int = 0
    balance_rubles: float = 0.0
    referral_code: Optional[str] = None
    language: str = "ru"
    created_at: datetime

    class Config:
        from_attributes = True


class AuthResponse(BaseModel):
    """Full authentication response with tokens and user."""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse
