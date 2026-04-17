"""Apple In-App Purchase cabinet route."""

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.apple_iap import (
    create_apple_transaction,
)
from app.database.crud.transaction import create_transaction as create_trans
from app.database.crud.user import lock_user_for_update
from app.database.models import PaymentMethod, TransactionType, User
from app.external.apple_iap import AppleIAPService
from app.utils.user_utils import format_referrer_info

from ..dependencies import get_cabinet_db, get_current_cabinet_user
from ..schemas.apple_iap import ApplePurchaseRequest, ApplePurchaseResponse


logger = structlog.get_logger(__name__)

router = APIRouter(tags=['Cabinet Apple IAP'])


def get_apple_iap_service() -> AppleIAPService:
    return AppleIAPService()


@router.post('/apple-purchase', response_model=ApplePurchaseResponse)
async def apple_purchase(
    request: ApplePurchaseRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    apple_iap_service: AppleIAPService = Depends(get_apple_iap_service),
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

    # Verify transaction with Apple Server API (no DB lock needed).
    # verify_transaction automatically falls back Sandbox↔Production.
    txn_info = await apple_iap_service.verify_transaction(
        request.transaction_id, settings.APPLE_IAP_ENVIRONMENT
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

    # If the iOS app set appAccountToken, verify it matches the authenticated user.
    # The iOS client should set appAccountToken to str(user.id) in StoreKit.
    app_account_token = txn_info.get('appAccountToken')
    if app_account_token:
        if app_account_token != str(user.id):
            logger.warning(
                'Apple appAccountToken mismatch — possible replay',
                expected=str(user.id),
                received=app_account_token,
                transaction_id=request.transaction_id,
                user_id=user.id,
            )
            return ApplePurchaseResponse(success=False)

    # Detect sandbox transactions — store actual environment from Apple's response
    actual_environment = txn_info.get('environment', settings.APPLE_IAP_ENVIRONMENT)
    is_sandbox = actual_environment == 'Sandbox'

    if is_sandbox and settings.APPLE_IAP_ENVIRONMENT == 'Production':
        # Sandbox transaction on a production server (e.g. App Review).
        # Record it for audit but do NOT credit real balance.
        logger.info(
            'Apple sandbox transaction on production — storing without balance credit',
            transaction_id=request.transaction_id,
            product_id=request.product_id,
            user_id=user.id,
        )
        try:
            async with db.begin_nested():
                await create_apple_transaction(
                    db=db,
                    user_id=user.id,
                    transaction_id=request.transaction_id,
                    original_transaction_id=txn_info.get('originalTransactionId'),
                    product_id=request.product_id,
                    bundle_id=txn_info.get('bundleId', settings.APPLE_IAP_BUNDLE_ID),
                    amount_kopeks=amount_kopeks,
                    environment='Sandbox',
                )
        except IntegrityError:
            pass  # already stored
        await db.commit()
        return ApplePurchaseResponse(success=True)

    # Atomically insert transaction record — unique constraint on transaction_id
    # prevents double-spend even under concurrent requests.
    try:
        async with db.begin_nested():
            await create_apple_transaction(
                db=db,
                user_id=user.id,
                transaction_id=request.transaction_id,
                original_transaction_id=txn_info.get('originalTransactionId'),
                product_id=request.product_id,
                bundle_id=txn_info.get('bundleId', settings.APPLE_IAP_BUNDLE_ID),
                amount_kopeks=amount_kopeks,
                environment=actual_environment,
            )
    except IntegrityError:
        logger.info(
            'Apple transaction already processed (idempotent)',
            transaction_id=request.transaction_id,
            user_id=user.id,
        )
        return ApplePurchaseResponse(success=True)

    # Create financial transaction record
    transaction = await create_trans(
        db=db,
        user_id=user.id,
        type=TransactionType.DEPOSIT,
        amount_kopeks=amount_kopeks,
        description=f'Пополнение через Apple IAP: {request.product_id}',
        payment_method=PaymentMethod.APPLE_IAP,
        external_id=request.transaction_id,
        is_completed=True,
        commit=False,
    )

    # Lock user row and credit balance
    user = await lock_user_for_update(db, user)
    old_balance = user.balance_kopeks
    was_first_topup = not user.has_made_first_topup

    user.balance_kopeks += amount_kopeks

    promo_group = user.get_primary_promo_group()
    subscription = getattr(user, 'subscription', None)
    referrer_info = format_referrer_info(user)
    topup_status = 'Первое пополнение' if was_first_topup else 'Пополнение'

    await db.commit()

    # --- Post-payment side-effects (after atomic commit) ---

    from app.database.crud.transaction import emit_transaction_side_effects

    try:
        await emit_transaction_side_effects(
            db,
            transaction,
            amount_kopeks=amount_kopeks,
            user_id=user.id,
            type=TransactionType.DEPOSIT,
            payment_method=PaymentMethod.APPLE_IAP,
            external_id=request.transaction_id,
        )
    except Exception as error:
        logger.error('Ошибка emit_transaction_side_effects Apple IAP', error=error)

    try:
        from app.services.referral_service import process_referral_topup

        await process_referral_topup(db, user.id, amount_kopeks, bot=None)
    except Exception as error:
        logger.error('Ошибка обработки реферального пополнения Apple IAP', error=error)

    if was_first_topup and not user.has_made_first_topup and not user.referred_by_id:
        user.has_made_first_topup = True
        await db.commit()

    await db.refresh(user)

    # Admin notification + cart auto-purchase
    try:
        from app.bot_factory import create_bot

        bot = create_bot()
        try:
            from app.services.admin_notification_service import AdminNotificationService

            notification_service = AdminNotificationService(bot)
            await notification_service.send_balance_topup_notification(
                user,
                transaction,
                old_balance,
                topup_status=topup_status,
                referrer_info=referrer_info,
                subscription=subscription,
                promo_group=promo_group,
                db=db,
            )
        except Exception as error:
            logger.error('Ошибка отправки админ уведомления Apple IAP', error=error)

        try:
            from app.services.payment.common import send_cart_notification_after_topup

            await send_cart_notification_after_topup(user, amount_kopeks, db, bot)
        except Exception as error:
            logger.error('Ошибка при работе с сохраненной корзиной Apple IAP', user_id=user.id, error=error)
        finally:
            await bot.session.close()
    except Exception as error:
        logger.error('Ошибка создания бота для уведомлений Apple IAP', error=error)

    logger.info(
        '✅ Apple IAP purchase credited',
        transaction_id=request.transaction_id,
        product_id=request.product_id,
        amount_kopeks=amount_kopeks,
        user_id=user.id,
    )

    return ApplePurchaseResponse(success=True)
