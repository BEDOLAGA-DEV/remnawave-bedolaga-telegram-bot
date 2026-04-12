"""Mixin с логикой обработки Apple In-App Purchase."""

from __future__ import annotations

import structlog

from app.config import settings
from app.utils.payment_logger import payment_logger as logger


class AppleIAPPaymentMixin:
    """Mixin for Apple IAP payment processing in PaymentService."""

    async def process_apple_purchase(
        self,
        db,
        user_id: int,
        transaction_id: str,
        product_id: str,
        environment: str,
    ) -> dict | None:
        """Process an Apple IAP purchase — verify and credit balance.

        This is an alternative entry point if processing needs to happen
        through PaymentService instead of directly via the cabinet route.
        """
        from app.database.crud.apple_iap import (
            create_apple_transaction,
            get_apple_transaction_by_transaction_id,
        )
        from app.database.crud.user import add_user_balance, get_user_by_id
        from app.database.models import PaymentMethod
        from app.external.apple_iap import AppleIAPService

        if not settings.is_apple_iap_enabled():
            logger.warning('Apple IAP not enabled')
            return None

        products = settings.get_apple_iap_products()
        if product_id not in products:
            logger.warning('Unknown Apple product ID', product_id=product_id)
            return None

        amount_kopeks = products[product_id]

        # Idempotency
        existing = await get_apple_transaction_by_transaction_id(db, transaction_id)
        if existing:
            logger.info('Apple transaction already processed', transaction_id=transaction_id)
            return {'already_processed': True, 'amount_kopeks': amount_kopeks}

        # Verify with Apple
        apple_service = self.apple_iap_service or AppleIAPService()
        txn_info = await apple_service.verify_transaction(transaction_id, environment)
        if not txn_info:
            logger.warning('Apple transaction verification failed', transaction_id=transaction_id)
            return None

        validation_error = apple_service.validate_transaction_info(txn_info, product_id)
        if validation_error:
            logger.warning('Apple transaction validation failed', error=validation_error)
            return None

        user = await get_user_by_id(db, user_id)
        if not user:
            logger.error('User not found for Apple purchase', user_id=user_id)
            return None

        success = await add_user_balance(
            db=db,
            user=user,
            amount_kopeks=amount_kopeks,
            description=f'Пополнение через Apple IAP: {product_id}',
            payment_method=PaymentMethod.APPLE_IAP,
            commit=False,
        )

        if not success:
            return None

        await create_apple_transaction(
            db=db,
            user_id=user_id,
            transaction_id=transaction_id,
            original_transaction_id=txn_info.get('originalTransactionId'),
            product_id=product_id,
            bundle_id=txn_info.get('bundleId', settings.APPLE_IAP_BUNDLE_ID),
            amount_kopeks=amount_kopeks,
            environment=environment,
        )

        await db.commit()

        logger.info(
            '✅ Apple IAP purchase credited via PaymentService',
            transaction_id=transaction_id,
            product_id=product_id,
            amount_kopeks=amount_kopeks,
            user_id=user_id,
        )

        return {'amount_kopeks': amount_kopeks, 'transaction_id': transaction_id}
