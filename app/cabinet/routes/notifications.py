"""Notification settings routes for cabinet."""

from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User

from ..dependencies import get_cabinet_db, get_current_cabinet_user


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/notifications', tags=['Cabinet Notifications'])


# ============ Schemas ============


class NotificationSettingsResponse(BaseModel):
    """User notification settings."""

    subscription_expiry_enabled: bool = True
    subscription_expiry_days: int = 3
    traffic_warning_enabled: bool = True
    traffic_warning_percent: int = 80
    balance_low_enabled: bool = False
    balance_low_threshold: int = 100  # kopeks
    news_enabled: bool = True
    promo_offers_enabled: bool = True


class NotificationSettingsUpdate(BaseModel):
    """Update notification settings."""

    subscription_expiry_enabled: bool | None = None
    subscription_expiry_days: int | None = Field(None, ge=1, le=30)
    traffic_warning_enabled: bool | None = None
    traffic_warning_percent: int | None = Field(None, ge=50, le=99)
    balance_low_enabled: bool | None = None
    balance_low_threshold: int | None = Field(None, ge=0)
    news_enabled: bool | None = None
    promo_offers_enabled: bool | None = None


# ============ Helpers ============


def _get_notification_settings(user: User) -> dict[str, Any]:
    """Get notification settings from user object."""
    # Try to get from user's settings field or use defaults
    settings_data = getattr(user, 'notification_settings', None) or {}

    return {
        'subscription_expiry_enabled': settings_data.get('subscription_expiry_enabled', True),
        'subscription_expiry_days': settings_data.get('subscription_expiry_days', 3),
        'traffic_warning_enabled': settings_data.get('traffic_warning_enabled', True),
        'traffic_warning_percent': settings_data.get('traffic_warning_percent', 80),
        'balance_low_enabled': settings_data.get('balance_low_enabled', False),
        'balance_low_threshold': settings_data.get('balance_low_threshold', 100),
        'news_enabled': settings_data.get('news_enabled', True),
        'promo_offers_enabled': settings_data.get('promo_offers_enabled', True),
    }


def _update_notification_settings(user: User, updates: dict[str, Any]) -> dict[str, Any]:
    """Update notification settings on user object."""
    current_settings = _get_notification_settings(user)

    for key, value in updates.items():
        if value is not None:
            current_settings[key] = value

    return current_settings


# ============ Routes ============


@router.get('', response_model=NotificationSettingsResponse)
async def get_notification_settings(
    user: User = Depends(get_current_cabinet_user),
):
    """Get user's notification settings."""
    settings = _get_notification_settings(user)
    return NotificationSettingsResponse(**settings)


@router.patch('', response_model=NotificationSettingsResponse)
async def update_notification_settings(
    request: NotificationSettingsUpdate,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Update user's notification settings."""
    updates = request.model_dump(exclude_unset=True)

    if not updates:
        # No updates provided, return current settings
        settings = _get_notification_settings(user)
        return NotificationSettingsResponse(**settings)

    # Update settings
    new_settings = _update_notification_settings(user, updates)

    # Store in user object
    if not hasattr(user, 'notification_settings') or user.notification_settings is None:
        user.notification_settings = {}

    user.notification_settings = new_settings
    user.updated_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(user)

    return NotificationSettingsResponse(**new_settings)


@router.post('/test')
async def send_test_notification(
    user: User = Depends(get_current_cabinet_user),
):
    """Send a test notification to the user."""
    # This would typically trigger a notification via Telegram bot
    # For now, just return success
    return {
        'success': True,
        'message': 'Test notification request received. You will receive a test message shortly.',
    }


@router.get('/history')
async def get_notification_history(
    limit: int = 20,
    offset: int = 0,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get user's notification history."""
    # For now, return empty list - notification history can be implemented later
    # when there's a notification log table
    return {
        'notifications': [],
        'total': 0,
        'limit': limit,
        'offset': offset,
    }
