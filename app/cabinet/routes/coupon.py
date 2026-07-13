"""Cabinet coupon redemption: authorized redeem + public status by token.

Coupons are one-time wholesale links (see app/services/coupon_service.py).
Telegram users normally redeem them via the bot deep link; this route is the
redemption path for email-auth cabinet users and the data source for the
public "you've got a coupon" page.
"""

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.ip_utils import get_client_ip
from app.config import settings
from app.database.crud.coupon import get_coupon_by_token
from app.database.models import CouponStatus, User
from app.services.coupon_service import (
    CouponRedemptionError,
    build_coupon_deeplink,
    is_coupon_token,
    redeem_coupon,
)
from app.services.notification_delivery_service import NotificationType, notification_delivery_service
from app.utils.cache import RateLimitCache

from ..dependencies import get_cabinet_db, get_current_cabinet_user
from ..schemas.coupons import CouponRedeemRequest, CouponRedeemResponse, CouponStatusResponse


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/coupon', tags=['Cabinet Coupon'])

# Stable machine codes → English messages; the frontend maps codes to localized
# strings (same contract as promocode activation).
_ERROR_MESSAGES = {
    'invalid': 'Coupon not found or already used',
    'expired': 'Coupon has expired',
    'already_redeemed_by_you': 'You have already redeemed this coupon',
    'internal': 'Server error occurred',
}


@router.post('/redeem', response_model=CouponRedeemResponse)
async def redeem_coupon_endpoint(
    request: CouponRedeemRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> CouponRedeemResponse:
    """Redeem a one-time coupon for the current cabinet user."""
    try:
        result = await redeem_coupon(db, request.token, user)
    except CouponRedemptionError as error:
        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR if error.code == 'internal' else status.HTTP_400_BAD_REQUEST
        raise HTTPException(
            status_code=status_code,
            detail={'code': error.code, 'message': _ERROR_MESSAGES.get(error.code, _ERROR_MESSAGES['invalid'])},
        ) from None

    # Email-only users get the email + cabinet-WS notification; telegram users
    # are notified by the bot flows (mirrors subscription_modules/purchase.py)
    if not user.telegram_id and user.email and user.email_verified:
        try:
            notification_type = (
                NotificationType.SUBSCRIPTION_RENEWED if result.renewed else NotificationType.SUBSCRIPTION_ACTIVATED
            )
            end_date_str = result.end_date.strftime('%d.%m.%Y') if result.end_date else ''
            await notification_delivery_service.send_notification(
                user=user,
                notification_type=notification_type,
                context={
                    'expires_at': end_date_str,  # for SUBSCRIPTION_ACTIVATED
                    'new_expires_at': end_date_str,  # for SUBSCRIPTION_RENEWED
                    'traffic_limit_gb': result.traffic_limit_gb,
                    'device_limit': result.device_limit,
                    'tariff_name': result.tariff_name,
                },
                bot=None,
            )
        except Exception as notif_error:
            logger.warning('Failed to send coupon redemption notification', email=user.email, notif_error=notif_error)

    return CouponRedeemResponse(
        success=True,
        tariff_name=result.tariff_name,
        period_days=result.period_days,
        renewed=result.renewed,
        end_date=result.end_date,
    )


@router.get('/{token}/status', response_model=CouponStatusResponse)
async def coupon_status(
    token: str,
    raw_request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
) -> CouponStatusResponse:
    """Public info about a still-redeemable coupon (no authentication).

    The token itself is the secret. Missing, redeemed, revoked and expired
    coupons all answer the same 404 — the endpoint is not an oracle.
    """
    client_ip = get_client_ip(raw_request)
    if await RateLimitCache.is_ip_rate_limited(client_ip, 'coupon_status', limit=30, window=60, fail_closed=True):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail='Too many requests')

    normalized = token.strip().lower()
    if not is_coupon_token(normalized):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Coupon not found')

    coupon = await get_coupon_by_token(db, normalized)
    if coupon is None or coupon.status != CouponStatus.ACTIVE.value or coupon.batch is None or coupon.batch.is_expired:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Coupon not found')

    bot_username = settings.get_bot_username()
    return CouponStatusResponse(
        tariff_name=coupon.batch.tariff.name if coupon.batch.tariff else '',
        period_days=coupon.batch.period_days,
        valid_until=coupon.batch.valid_until,
        bot_link=(build_coupon_deeplink(bot_username, normalized) if bot_username else None),
    )
