"""Apple In-App Purchase cabinet routes."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from redis import asyncio as redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.services.apple_iap import AppleIAPFulfillmentService, apple_iap_fulfillment_service
from app.services.apple_iap_reconciliation_service import apple_iap_reconciliation_service

from .dependencies import get_cabinet_db, get_current_admin_user, get_current_cabinet_user
from .ip_utils import get_client_ip
from .schemas.apple_iap import AppleAccountTokenResponse, ApplePurchaseRequest, ApplePurchaseResponse


logger = structlog.get_logger(__name__)
APPLE_IAP_REDIS_STATE_KEY = 'apple_iap_redis_client'


async def _close_redis_client(client: redis.Redis) -> None:
    close = getattr(client, 'aclose', None)
    if close is not None:
        await close()
        return
    await client.close()


@asynccontextmanager
async def apple_iap_lifespan(app: FastAPI) -> AsyncIterator[None]:
    client = redis.from_url(settings.REDIS_URL)
    setattr(app.state, APPLE_IAP_REDIS_STATE_KEY, client)
    try:
        yield
    finally:
        setattr(app.state, APPLE_IAP_REDIS_STATE_KEY, None)
        try:
            await _close_redis_client(client)
        except RedisError as error:  # pragma: no cover - defensive shutdown logging
            logger.warning('Apple IAP Redis client close failed', error=error)


router = APIRouter(tags=['Cabinet Apple IAP'], lifespan=apple_iap_lifespan)


def get_apple_iap_fulfillment_service(request: Request) -> AppleIAPFulfillmentService:
    bot = getattr(request.app.state, 'bot', None)
    if bot is None:
        return apple_iap_fulfillment_service
    return AppleIAPFulfillmentService(apple_iap_fulfillment_service.apple_service, bot=bot)


def get_apple_iap_redis_client(request: Request) -> redis.Redis | None:
    return getattr(request.app.state, APPLE_IAP_REDIS_STATE_KEY, None)


def _parse_redis_counter(value: object) -> int:
    if isinstance(value, bytes):
        value = value.decode('utf-8')
    elif isinstance(value, bytearray):
        value = bytes(value).decode('utf-8')
    return int(value)


async def _increment_redis_counter(client: redis.Redis, key: str, ttl_seconds: int) -> int:
    created = await client.set(key, 1, ex=ttl_seconds, nx=True)
    if created:
        return 1
    return _parse_redis_counter(await client.incr(key))


def _rate_limit_error_allows_request() -> bool:
    return settings.APPLE_IAP_RATE_LIMIT_FAIL_OPEN


async def _check_purchase_rate_limit(client: redis.Redis | None, user_id: int, ip_address: str | None) -> bool:
    if client is None:
        allow_request = _rate_limit_error_allows_request()
        logger.error('Apple IAP Redis client is not initialized', fail_open=allow_request)
        return allow_request

    limit = max(1, settings.APPLE_IAP_PURCHASE_RATE_LIMIT_PER_MINUTE)
    failure_limit = max(1, settings.APPLE_IAP_PURCHASE_FAILURE_LIMIT_PER_HOUR)
    keys = [f'apple_iap:purchase:user:{user_id}']
    if ip_address:
        keys.append(f'apple_iap:purchase:ip:{ip_address}')

    try:
        failure_count = await client.get(f'apple_iap:purchase_fail:user:{user_id}')
        if failure_count is not None and _parse_redis_counter(failure_count) >= failure_limit:
            logger.warning('Apple IAP purchase failure limit exceeded', user_id=user_id)
            return False

        for key in keys:
            count = await _increment_redis_counter(client, key, 60)
            if count > limit:
                logger.warning('Apple IAP purchase rate limit exceeded', key=key, user_id=user_id)
                return False
    except RedisError as error:
        allow_request = _rate_limit_error_allows_request()
        logger.error('Apple IAP rate limiter unavailable', error=error, fail_open=allow_request)
        return allow_request
    except (TypeError, ValueError, UnicodeDecodeError) as error:
        allow_request = _rate_limit_error_allows_request()
        logger.error('Apple IAP rate limiter returned invalid counter', error=error, fail_open=allow_request)
        return allow_request
    return True


async def _record_purchase_failure(client: redis.Redis | None, user_id: int) -> None:
    if client is None:
        logger.warning('Apple IAP Redis client is not initialized; skipping failure limiter')
        return

    try:
        key = f'apple_iap:purchase_fail:user:{user_id}'
        await _increment_redis_counter(client, key, 3600)
    except RedisError as error:
        logger.warning('Apple IAP failure limiter unavailable', error=error)
    except (TypeError, ValueError, UnicodeDecodeError) as error:
        logger.warning('Apple IAP failure limiter returned invalid counter', error=error)


@router.get('/apple-iap/account-token', response_model=AppleAccountTokenResponse)
async def apple_iap_account_token(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    fulfillment_service: AppleIAPFulfillmentService = Depends(get_apple_iap_fulfillment_service),
):
    """Return the stable StoreKit appAccountToken UUID for the authenticated user."""
    if not settings.is_apple_iap_enabled():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Apple In-App Purchase is not enabled or not fully configured',
        )
    token = await fulfillment_service.get_account_token(db, user.id)
    return AppleAccountTokenResponse(app_account_token=token)


@router.post('/apple-purchase', response_model=ApplePurchaseResponse)
async def apple_purchase(
    request: ApplePurchaseRequest,
    http_request: Request,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    fulfillment_service: AppleIAPFulfillmentService = Depends(get_apple_iap_fulfillment_service),
    redis_client: redis.Redis | None = Depends(get_apple_iap_redis_client),
):
    """Verify an Apple consumable transaction and credit the user's internal balance."""
    if not settings.is_apple_iap_enabled():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Apple In-App Purchase is not enabled or not fully configured',
        )

    ip_address = get_client_ip(http_request)
    if not await _check_purchase_rate_limit(redis_client, user.id, ip_address):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many Apple purchase verification attempts',
        )

    result = await fulfillment_service.verify_and_fulfill_purchase(
        db,
        user,
        product_id=request.product_id,
        transaction_id=request.transaction_id,
        ip_address=ip_address,
    )
    if not result.success:
        await _record_purchase_failure(redis_client, user.id)
    return ApplePurchaseResponse(success=result.success)


@router.get('/admin/apple-iap/transactions')
async def search_apple_iap_transactions(
    q: str = Query(..., min_length=1, max_length=128),
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Support lookup for Apple IAP ledger entries."""
    rows = await apple_iap_reconciliation_service.lookup(db, q, limit=limit)
    return {
        'items': [
            {
                'id': row.id,
                'user_id': row.user_id,
                'transaction_id': row.transaction_id,
                'original_transaction_id': row.original_transaction_id,
                'product_id': row.product_id,
                'amount_kopeks': row.amount_kopeks,
                'environment': row.environment,
                'status': row.status,
                'created_at': row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
    }


@router.post('/admin/apple-iap/reconcile')
async def reconcile_apple_iap_transactions(
    limit: int = Query(100, ge=1, le=500),
    user: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Reconcile recent Apple IAP transactions against Apple's API."""
    result = await apple_iap_reconciliation_service.reconcile_recent_transactions(db, limit=limit)
    return {
        'checked': result.checked,
        'drift_count': result.drift_count,
        'notification_backlog': result.notification_backlog,
    }


apple_iap_only_router = APIRouter(
    prefix='/cabinet',
    tags=['Cabinet Apple IAP'],
    redirect_slashes=False,
)
apple_iap_only_router.include_router(router)
