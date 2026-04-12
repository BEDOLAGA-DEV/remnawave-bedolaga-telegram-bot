"""Apple In-App Purchase cabinet route."""

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.apple_iap import (
    create_apple_transaction,
    get_apple_transaction_by_transaction_id,
)
from app.database.crud.user import add_user_balance
from app.database.models import PaymentMethod, User
from app.external.apple_iap import AppleIAPService

from ..dependencies import get_cabinet_db, get_current_cabinet_user
from ..schemas.apple_iap import ApplePurchaseRequest, ApplePurchaseResponse


logger = structlog.get_logger(__name__)

router = APIRouter(tags=['Cabinet Apple IAP'])

apple_iap_service = AppleIAPService()


@router.post('/apple-purchase', response_model=ApplePurchaseResponse)
async def apple_purchase(
    request: ApplePurchaseRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Verify an Apple In-App Purchase and credit the user's balance.

    The iOS app calls this endpoint after a successful StoreKit transaction.
    If the backend returns success=false, the iOS app will NOT finish the
    transaction and will retry on next launch.
    """
    if not settings.is_apple_iap_enabled():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Apple In-App Purchase is not enabled',
        )

    # Validate product ID
    products = settings.get_apple_iap_products()
    if request.product_id not in products:
        logger.warning(
            'Unknown Apple product ID',
            product_id=request.product_id,
            user_id=user.id,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Unknown product ID',
        )

    amount_kopeks = products[request.product_id]

    # Idempotency check — already processed?
    existing = await get_apple_transaction_by_transaction_id(db, request.transaction_id)
    if existing:
        logger.info(
            'Apple transaction already processed (idempotent)',
            transaction_id=request.transaction_id,
            user_id=user.id,
        )
        return ApplePurchaseResponse(success=True)

    # Verify transaction with Apple Server API
    txn_info = await apple_iap_service.verify_transaction(
        request.transaction_id, request.environment
    )
    if not txn_info:
        logger.warning(
            'Apple transaction verification failed',
            transaction_id=request.transaction_id,
            user_id=user.id,
        )
        return ApplePurchaseResponse(success=False)

    # Validate transaction fields
    validation_error = apple_iap_service.validate_transaction_info(txn_info, request.product_id)
    if validation_error:
        logger.warning(
            'Apple transaction validation failed',
            error=validation_error,
            transaction_id=request.transaction_id,
            user_id=user.id,
        )
        return ApplePurchaseResponse(success=False)

    # Credit the user's balance
    success = await add_user_balance(
        db=db,
        user=user,
        amount_kopeks=amount_kopeks,
        description=f'Пополнение через Apple IAP: {request.product_id}',
        payment_method=PaymentMethod.APPLE_IAP,
        commit=False,
    )

    if not success:
        logger.error(
            'Failed to credit balance for Apple purchase',
            transaction_id=request.transaction_id,
            user_id=user.id,
            amount_kopeks=amount_kopeks,
        )
        return ApplePurchaseResponse(success=False)

    # Store Apple transaction record
    await create_apple_transaction(
        db=db,
        user_id=user.id,
        transaction_id=request.transaction_id,
        original_transaction_id=txn_info.get('originalTransactionId'),
        product_id=request.product_id,
        bundle_id=txn_info.get('bundleId', settings.APPLE_IAP_BUNDLE_ID),
        amount_kopeks=amount_kopeks,
        environment=request.environment,
    )

    await db.commit()

    logger.info(
        '✅ Apple IAP purchase credited',
        transaction_id=request.transaction_id,
        product_id=request.product_id,
        amount_kopeks=amount_kopeks,
        user_id=user.id,
    )

    return ApplePurchaseResponse(success=True)
