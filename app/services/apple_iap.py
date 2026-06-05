"""Domain services for Apple IAP consumable balance top-ups."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.apple_iap import (
    create_apple_abuse_event,
    create_apple_notification,
    create_apple_transaction,
    get_apple_iap_account_by_token,
    get_apple_notification_by_payload_hash,
    get_apple_notification_by_uuid,
    get_apple_transaction_by_transaction_id,
    get_apple_transaction_by_transaction_id_for_update,
    get_apple_transaction_by_web_order_line_item_id,
    get_or_create_apple_iap_account,
    mark_apple_notification_processed,
    mark_apple_transaction_refunded,
)
from app.database.crud.transaction import create_transaction, emit_transaction_side_effects
from app.database.crud.user import lock_user_for_pricing, lock_user_for_update
from app.database.database import AsyncSessionLocal
from app.database.models import AppleNotification, AppleTransaction, PaymentMethod, Transaction, TransactionType, User
from app.external.apple_iap import AppleIAPConfigurationError, AppleIAPService, parse_apple_timestamp
from app.utils.user_utils import format_referrer_info


logger = structlog.get_logger(__name__)


_RETRYABLE_NOTIFICATION_REASONS = {
    'missing_transaction',
    'transaction_not_found',
    'unknown_account_token',
    'user_not_found',
    'refund_reversal_credit_failed',
    'signed_transaction_verification_failed',
}


@dataclass(slots=True)
class AppleFulfillmentResult:
    success: bool
    reason: str = 'ok'
    apple_transaction: AppleTransaction | None = None
    transaction: Transaction | None = None


def _payload_hash(raw_payload: bytes | str) -> str:
    if isinstance(raw_payload, str):
        raw_payload = raw_payload.encode('utf-8')
    return hashlib.sha256(raw_payload).hexdigest()


def _safe_metadata(txn_info: dict[str, Any]) -> dict[str, Any]:
    return {
        key: txn_info.get(key)
        for key in (
            'transactionId',
            'originalTransactionId',
            'webOrderLineItemId',
            'bundleId',
            'productId',
            'type',
            'environment',
            'storefront',
            'currency',
            'price',
            'purchaseDate',
            'revocationDate',
            'revocationReason',
            'transactionReason',
            'inAppOwnershipType',
        )
        if txn_info.get(key) is not None
    }


def _transaction_fields(txn_info: dict[str, Any]) -> dict[str, Any]:
    price = txn_info.get('price')
    try:
        price_micros = int(price) if price is not None else None
    except (TypeError, ValueError):
        price_micros = None

    return {
        'transaction_id': str(txn_info.get('transactionId') or ''),
        'original_transaction_id': str(txn_info.get('originalTransactionId') or '') or None,
        'web_order_line_item_id': str(txn_info.get('webOrderLineItemId') or '') or None,
        'bundle_id': str(txn_info.get('bundleId') or settings.APPLE_IAP_BUNDLE_ID),
        'environment': str(txn_info.get('environment') or settings.get_apple_iap_environment()),
        'app_account_token': str(txn_info.get('appAccountToken') or '') or None,
        'storefront': str(txn_info.get('storefront') or '') or None,
        'currency': str(txn_info.get('currency') or '') or None,
        'price_micros': price_micros,
        'purchase_date': parse_apple_timestamp(txn_info.get('purchaseDate')),
        'revocation_date': parse_apple_timestamp(txn_info.get('revocationDate')),
        'revocation_reason': str(txn_info.get('revocationReason') or '') or None,
        'signed_transaction_hash': txn_info.get('signedTransactionInfoHash'),
        'metadata_json': _safe_metadata(txn_info),
    }


class AppleIAPFulfillmentService:
    def __init__(self, apple_service: AppleIAPService | None = None, bot: Any = None):
        self.apple_service = apple_service or AppleIAPService()
        self.bot = bot

    async def get_account_token(self, db: AsyncSession, user_id: int) -> str:
        account = await get_or_create_apple_iap_account(db, user_id)
        await db.commit()
        return account.account_token_uuid

    async def verify_and_fulfill_purchase(
        self,
        db: AsyncSession,
        user: User,
        *,
        product_id: str,
        transaction_id: str,
        ip_address: str | None = None,
    ) -> AppleFulfillmentResult:
        if not settings.is_apple_iap_enabled():
            return AppleFulfillmentResult(False, 'disabled')

        products = settings.get_apple_iap_products()
        if product_id not in products:
            await create_apple_abuse_event(
                db,
                'unknown_product',
                user_id=user.id,
                product_id=product_id,
                transaction_id=transaction_id,
                ip_address=ip_address,
            )
            await db.commit()
            return AppleFulfillmentResult(False, 'unknown_product')

        txn_info = await self.apple_service.verify_transaction(
            transaction_id,
            settings.get_apple_iap_environment(),
            allow_environment_fallback=settings.APPLE_IAP_ALLOW_SANDBOX_ON_PRODUCTION,
        )
        if not txn_info:
            await create_apple_abuse_event(
                db,
                'verification_failed',
                user_id=user.id,
                product_id=product_id,
                transaction_id=transaction_id,
                ip_address=ip_address,
            )
            await db.commit()
            return AppleFulfillmentResult(False, 'verification_failed')

        account = await get_or_create_apple_iap_account(db, user.id)
        return await self.fulfill_verified_transaction(
            db,
            user_id=user.id,
            product_id=product_id,
            txn_info=txn_info,
            expected_app_account_token=account.account_token_uuid,
            ip_address=ip_address,
        )

    async def fulfill_verified_transaction(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        product_id: str,
        txn_info: dict[str, Any],
        expected_app_account_token: str | None,
        ip_address: str | None = None,
    ) -> AppleFulfillmentResult:
        products = settings.get_apple_iap_products()
        amount_kopeks = products.get(product_id)
        transaction_id = str(txn_info.get('transactionId') or '')

        if not transaction_id or amount_kopeks is None:
            return AppleFulfillmentResult(False, 'invalid_transaction')

        validation_error = self.apple_service.validate_transaction_info(txn_info, product_id)
        if validation_error:
            await create_apple_abuse_event(
                db,
                'transaction_validation_failed',
                user_id=user_id,
                severity='warning',
                transaction_id=transaction_id,
                product_id=product_id,
                ip_address=ip_address,
                details_json={'error': validation_error},
            )
            await db.commit()
            return AppleFulfillmentResult(False, 'validation_failed')

        app_account_token = str(txn_info.get('appAccountToken') or '')
        if not app_account_token or app_account_token != expected_app_account_token:
            await create_apple_abuse_event(
                db,
                'app_account_token_mismatch',
                user_id=user_id,
                severity='critical',
                transaction_id=transaction_id,
                product_id=product_id,
                ip_address=ip_address,
                details_json={'has_token': bool(app_account_token)},
            )
            await db.commit()
            return AppleFulfillmentResult(False, 'account_token_mismatch')

        configured_environment = settings.get_apple_iap_environment()
        actual_environment = str(txn_info.get('environment') or configured_environment)
        if actual_environment != configured_environment:
            if (
                actual_environment == 'Sandbox'
                and configured_environment == 'Production'
                and settings.APPLE_IAP_ALLOW_SANDBOX_ON_PRODUCTION
            ):
                return await self._record_sandbox_on_production(
                    db,
                    user_id=user_id,
                    product_id=product_id,
                    amount_kopeks=amount_kopeks,
                    txn_info=txn_info,
                )
            await create_apple_abuse_event(
                db,
                'transaction_environment_mismatch',
                user_id=user_id,
                severity='critical',
                transaction_id=transaction_id,
                product_id=product_id,
                ip_address=ip_address,
                details_json={'configured': configured_environment, 'actual': actual_environment},
            )
            await db.commit()
            return AppleFulfillmentResult(False, 'environment_mismatch')

        existing = await get_apple_transaction_by_transaction_id(db, transaction_id)
        if existing:
            if existing.user_id == user_id and existing.status in {
                'verified',
                'credited',
                'sandbox_recorded',
                'refunded',
            }:
                return AppleFulfillmentResult(True, 'already_processed', existing)
            await create_apple_abuse_event(
                db,
                'transaction_owner_mismatch',
                user_id=user_id,
                severity='critical',
                transaction_id=transaction_id,
                product_id=product_id,
                ip_address=ip_address,
                details_json={'existing_user_id': existing.user_id},
            )
            await db.commit()
            return AppleFulfillmentResult(False, 'owner_mismatch')

        fields = _transaction_fields(txn_info)
        web_order_line_item_id = fields.get('web_order_line_item_id')
        if web_order_line_item_id:
            existing = await get_apple_transaction_by_web_order_line_item_id(db, web_order_line_item_id)
            if existing:
                if existing.user_id == user_id and existing.status in {
                    'verified',
                    'credited',
                    'sandbox_recorded',
                    'refunded',
                }:
                    return AppleFulfillmentResult(True, 'already_processed', existing)
                await create_apple_abuse_event(
                    db,
                    'web_order_line_item_owner_mismatch',
                    user_id=user_id,
                    severity='critical',
                    transaction_id=transaction_id,
                    product_id=product_id,
                    ip_address=ip_address,
                    details_json={
                        'existing_user_id': existing.user_id,
                        'web_order_line_item_id': web_order_line_item_id,
                    },
                )
                await db.commit()
                return AppleFulfillmentResult(False, 'owner_mismatch')

        user = await lock_user_for_update(db, User(id=user_id))
        old_balance = user.balance_kopeks
        was_first_topup = not user.has_made_first_topup

        apple_txn = None
        try:
            async with db.begin_nested():
                apple_txn = await create_apple_transaction(
                    db=db,
                    user_id=user_id,
                    product_id=product_id,
                    amount_kopeks=amount_kopeks,
                    status='verified',
                    is_paid=True,
                    **fields,
                )
        except IntegrityError:
            existing = await get_apple_transaction_by_transaction_id(db, transaction_id)
            if existing:
                if existing.user_id == user_id:
                    await db.commit()
                    return AppleFulfillmentResult(True, 'already_processed', existing)
                await create_apple_abuse_event(
                    db,
                    'transaction_owner_mismatch',
                    user_id=user_id,
                    severity='critical',
                    transaction_id=transaction_id,
                    product_id=product_id,
                    ip_address=ip_address,
                    details_json={'existing_user_id': existing.user_id},
                )
                await db.commit()
                return AppleFulfillmentResult(False, 'owner_mismatch')
            if web_order_line_item_id:
                existing = await get_apple_transaction_by_web_order_line_item_id(db, web_order_line_item_id)
                if existing:
                    if existing.user_id == user_id:
                        await db.commit()
                        return AppleFulfillmentResult(True, 'already_processed', existing)
                    await create_apple_abuse_event(
                        db,
                        'web_order_line_item_owner_mismatch',
                        user_id=user_id,
                        severity='critical',
                        transaction_id=transaction_id,
                        product_id=product_id,
                        ip_address=ip_address,
                        details_json={
                            'existing_user_id': existing.user_id,
                            'web_order_line_item_id': web_order_line_item_id,
                        },
                    )
                    await db.commit()
                    return AppleFulfillmentResult(False, 'owner_mismatch')
            await db.commit()
            return AppleFulfillmentResult(False, 'duplicate_conflict')

        transaction = await create_transaction(
            db=db,
            user_id=user_id,
            type=TransactionType.DEPOSIT,
            amount_kopeks=amount_kopeks,
            description=f'Пополнение через Apple IAP: {product_id}',
            payment_method=PaymentMethod.APPLE_IAP,
            external_id=transaction_id,
            is_completed=True,
            commit=False,
        )

        if apple_txn:
            apple_txn.transaction_id_fk = transaction.id
            apple_txn.status = 'credited'
            apple_txn.credited_at = datetime.now(UTC)
            apple_txn.updated_at = datetime.now(UTC)

        user.balance_kopeks += amount_kopeks
        user.updated_at = datetime.now(UTC)

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, 'subscription', None)
        referrer_info = format_referrer_info(user)
        topup_status = 'Первое пополнение' if was_first_topup else 'Пополнение'

        await db.commit()

        await self._emit_credit_side_effects(
            db,
            user,
            transaction,
            amount_kopeks=amount_kopeks,
            external_id=transaction_id,
            old_balance=old_balance,
            topup_status=topup_status,
            referrer_info=referrer_info,
            subscription=subscription,
            promo_group=promo_group,
            was_first_topup=was_first_topup,
        )

        logger.info(
            'Apple IAP purchase credited', transaction_id=transaction_id, user_id=user_id, amount_kopeks=amount_kopeks
        )
        return AppleFulfillmentResult(True, 'credited', apple_txn, transaction)

    async def _record_sandbox_on_production(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        product_id: str,
        amount_kopeks: int,
        txn_info: dict[str, Any],
    ) -> AppleFulfillmentResult:
        fields = _transaction_fields(txn_info)
        fields['environment'] = 'Sandbox'
        try:
            async with db.begin_nested():
                apple_txn = await create_apple_transaction(
                    db=db,
                    user_id=user_id,
                    product_id=product_id,
                    amount_kopeks=amount_kopeks,
                    status='sandbox_recorded',
                    is_paid=False,
                    **fields,
                )
        except IntegrityError:
            apple_txn = await get_apple_transaction_by_transaction_id(db, fields['transaction_id'])

        await db.commit()
        return AppleFulfillmentResult(True, 'sandbox_recorded', apple_txn)

    async def _emit_credit_side_effects(
        self,
        db: AsyncSession,
        user: User,
        transaction: Transaction,
        *,
        amount_kopeks: int,
        external_id: str,
        old_balance: int,
        topup_status: str,
        referrer_info: str,
        subscription,
        promo_group,
        was_first_topup: bool,
    ) -> None:
        try:
            await emit_transaction_side_effects(
                db,
                transaction,
                amount_kopeks=amount_kopeks,
                user_id=user.id,
                type=TransactionType.DEPOSIT,
                payment_method=PaymentMethod.APPLE_IAP,
                external_id=external_id,
            )
        except Exception as error:
            logger.error('Ошибка emit_transaction_side_effects Apple IAP', error=error, exc_info=True)

        try:
            from app.services.referral_service import process_referral_topup

            await process_referral_topup(db, user.id, amount_kopeks, bot=self.bot)
        except Exception as error:
            logger.error('Ошибка обработки реферального пополнения Apple IAP', error=error, exc_info=True)

        if was_first_topup and not user.has_made_first_topup and not user.referred_by_id:
            user.has_made_first_topup = True
            await db.commit()

        await db.refresh(user)

        if self.bot is None:
            logger.debug('Apple IAP bot is not configured; skipping bot-dependent notifications', user_id=user.id)
            return

        try:
            from app.services.admin_notification_service import AdminNotificationService

            notification_service = AdminNotificationService(self.bot)
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
            logger.error('Ошибка отправки админ уведомления Apple IAP', error=error, exc_info=True)

        try:
            from app.services.payment.common import send_cart_notification_after_topup

            await send_cart_notification_after_topup(user, amount_kopeks, db, self.bot)
        except Exception as error:
            logger.error(
                'Ошибка при работе с сохраненной корзиной Apple IAP', user_id=user.id, error=error, exc_info=True
            )


class AppleIAPNotificationService:
    def __init__(
        self,
        apple_service: AppleIAPService | None = None,
        fulfillment_service: AppleIAPFulfillmentService | None = None,
        bot: Any = None,
    ):
        self.apple_service = apple_service or AppleIAPService()
        self.fulfillment_service = fulfillment_service or AppleIAPFulfillmentService(self.apple_service, bot=bot)

    async def process_signed_payload(self, signed_payload: str, raw_body: bytes) -> tuple[bool, str]:
        try:
            notification = self.apple_service.verify_notification(signed_payload)
        except AppleIAPConfigurationError as error:
            logger.error('Apple IAP notification configuration error', error=str(error), exc_info=True)
            return False, 'configuration_error'

        if not notification:
            return False, 'invalid_signature'

        notification_type = str(notification.get('notificationType') or '')
        subtype = str(notification.get('subtype') or '') or None
        notification_uuid = str(notification.get('notificationUUID') or '')
        if not notification_uuid:
            return False, 'missing_notification_uuid'

        data = notification.get('data') or {}
        environment = str(data.get('environment') or '')
        if not self._environment_allowed(environment):
            logger.warning('Apple notification environment ignored', environment=environment)
            return True, 'environment_ignored'

        signed_txn = data.get('signedTransactionInfo')
        txn_info = self.apple_service.verify_signed_transaction_info(signed_txn, environment) if signed_txn else None
        if signed_txn and txn_info:
            if environment:
                txn_info.setdefault('environment', environment)
            txn_info['signedTransactionInfoHash'] = _payload_hash(signed_txn)

        payload_hash = _payload_hash(raw_body)
        async with AsyncSessionLocal() as db:
            existing = await get_apple_notification_by_uuid(db, notification_uuid)
            if existing and existing.status == 'processed':
                return True, 'duplicate'

            apple_notification = existing
            if apple_notification is None:
                existing_payload = await get_apple_notification_by_payload_hash(db, payload_hash)
                if existing_payload:
                    logger.warning(
                        'Apple notification replay detected by payload hash',
                        notification_uuid=notification_uuid,
                        existing_notification_uuid=existing_payload.notification_uuid,
                    )
                    return True, 'payload_replay'
                try:
                    async with db.begin_nested():
                        apple_notification = await create_apple_notification(
                            db,
                            notification_uuid=notification_uuid,
                            notification_type=notification_type,
                            subtype=subtype,
                            environment=environment or None,
                            transaction_id=str((txn_info or {}).get('transactionId') or '') or None,
                            original_transaction_id=str((txn_info or {}).get('originalTransactionId') or '') or None,
                            payload_hash=payload_hash,
                            metadata_json={
                                'notificationType': notification_type,
                                'subtype': subtype,
                                'signedPayloadHash': notification.get('signedPayloadHash'),
                            },
                        )
                except IntegrityError:
                    apple_notification = await get_apple_notification_by_uuid(db, notification_uuid)
                    if apple_notification and apple_notification.status == 'processed':
                        return True, 'duplicate'
                    existing_payload = await get_apple_notification_by_payload_hash(db, payload_hash)
                    if existing_payload:
                        logger.warning(
                            'Apple notification replay detected by payload hash after insert race',
                            notification_uuid=notification_uuid,
                            existing_notification_uuid=existing_payload.notification_uuid,
                        )
                        return True, 'payload_replay'
                    if apple_notification is None:
                        raise

            if signed_txn and txn_info is None:
                await mark_apple_notification_processed(
                    db,
                    apple_notification,
                    status='failed',
                    error='signed_transaction_verification_failed',
                )
                await db.commit()
                return False, 'signed_transaction_verification_failed'

            try:
                reason = await self._dispatch(db, notification_type, txn_info, apple_notification)
            except Exception as error:
                logger.error('Apple notification processing failed', error=error, exc_info=True)
                await mark_apple_notification_processed(db, apple_notification, status='failed', error=str(error)[:500])
                await db.commit()
                return False, 'processing_failed'

            if reason in _RETRYABLE_NOTIFICATION_REASONS:
                await mark_apple_notification_processed(db, apple_notification, status='failed', error=reason)
                await db.commit()
                return False, reason

            await mark_apple_notification_processed(db, apple_notification, status='processed')
            await db.commit()
            return True, reason

    def _environment_allowed(self, environment: str) -> bool:
        configured = settings.get_apple_iap_environment()
        if configured == 'Production':
            return (
                environment in {'', 'Production', 'Sandbox'}
                if settings.APPLE_IAP_ALLOW_SANDBOX_ON_PRODUCTION
                else environment in {'', 'Production'}
            )
        return environment in {'', 'Sandbox'}

    async def _dispatch(
        self,
        db: AsyncSession,
        notification_type: str,
        txn_info: dict[str, Any] | None,
        apple_notification: AppleNotification,
    ) -> str:
        if notification_type == 'TEST':
            return 'test'
        if notification_type == 'ONE_TIME_CHARGE':
            return await self._handle_one_time_charge(db, txn_info)
        if notification_type == 'REFUND':
            return await self._handle_refund(db, txn_info)
        if notification_type == 'REFUND_REVERSED':
            return await self._handle_refund_reversed(db, txn_info)
        if notification_type == 'CONSUMPTION_REQUEST':
            return await self._handle_consumption_request(txn_info)

        logger.info(
            'Unhandled Apple notification type',
            notification_type=notification_type,
            notification_uuid=apple_notification.notification_uuid,
        )
        return 'ignored'

    async def _validate_stored_transaction_matches_notification(
        self,
        db: AsyncSession,
        *,
        apple_txn: AppleTransaction,
        txn_info: dict[str, Any],
        notification_type: str,
    ) -> str | None:
        signed_transaction_id = str(txn_info.get('transactionId') or '')
        signed_original_transaction_id = str(txn_info.get('originalTransactionId') or '')
        stored_transaction_ids = {
            value for value in (apple_txn.transaction_id, apple_txn.original_transaction_id) if value
        }
        signed_transaction_ids = {value for value in (signed_transaction_id, signed_original_transaction_id) if value}
        if not stored_transaction_ids.intersection(signed_transaction_ids):
            await create_apple_abuse_event(
                db,
                'notification_transaction_id_mismatch',
                user_id=apple_txn.user_id,
                severity='critical',
                transaction_id=signed_transaction_id or None,
                product_id=str(txn_info.get('productId') or '') or None,
                details_json={
                    'notification': notification_type,
                    'stored_transaction_id': apple_txn.transaction_id,
                    'signed_original_transaction_id': signed_original_transaction_id or None,
                },
            )
            return 'transaction_id_mismatch'

        signed_app_account_token = str(txn_info.get('appAccountToken') or '')
        if not apple_txn.app_account_token or signed_app_account_token != apple_txn.app_account_token:
            await create_apple_abuse_event(
                db,
                'notification_app_account_token_mismatch',
                user_id=apple_txn.user_id,
                severity='critical',
                transaction_id=apple_txn.transaction_id,
                product_id=apple_txn.product_id,
                details_json={
                    'notification': notification_type,
                    'has_signed_token': bool(signed_app_account_token),
                    'has_stored_token': bool(apple_txn.app_account_token),
                },
            )
            return 'account_token_mismatch'

        signed_environment = str(txn_info.get('environment') or '')
        if signed_environment and signed_environment != apple_txn.environment:
            await create_apple_abuse_event(
                db,
                'notification_environment_mismatch',
                user_id=apple_txn.user_id,
                severity='critical',
                transaction_id=apple_txn.transaction_id,
                product_id=apple_txn.product_id,
                details_json={
                    'notification': notification_type,
                    'stored_environment': apple_txn.environment,
                    'signed_environment': signed_environment,
                },
            )
            return 'environment_mismatch'

        signed_bundle_id = str(txn_info.get('bundleId') or '')
        if signed_bundle_id and signed_bundle_id != apple_txn.bundle_id:
            await create_apple_abuse_event(
                db,
                'notification_bundle_id_mismatch',
                user_id=apple_txn.user_id,
                severity='critical',
                transaction_id=apple_txn.transaction_id,
                product_id=apple_txn.product_id,
                details_json={
                    'notification': notification_type,
                    'stored_bundle_id': apple_txn.bundle_id,
                    'signed_bundle_id': signed_bundle_id,
                },
            )
            return 'bundle_id_mismatch'

        signed_product_id = str(txn_info.get('productId') or '')
        if signed_product_id and signed_product_id != apple_txn.product_id:
            await create_apple_abuse_event(
                db,
                'notification_product_id_mismatch',
                user_id=apple_txn.user_id,
                severity='critical',
                transaction_id=apple_txn.transaction_id,
                product_id=signed_product_id,
                details_json={
                    'notification': notification_type,
                    'stored_product_id': apple_txn.product_id,
                    'signed_product_id': signed_product_id,
                },
            )
            return 'product_id_mismatch'

        return None

    async def _handle_one_time_charge(self, db: AsyncSession, txn_info: dict[str, Any] | None) -> str:
        if not txn_info:
            return 'missing_transaction'
        account_token = str(txn_info.get('appAccountToken') or '')
        account = await get_apple_iap_account_by_token(db, account_token)
        if not account:
            await create_apple_abuse_event(
                db,
                'notification_unknown_app_account_token',
                severity='critical',
                transaction_id=str(txn_info.get('transactionId') or '') or None,
                product_id=str(txn_info.get('productId') or '') or None,
                details_json={'notification': 'ONE_TIME_CHARGE'},
            )
            return 'unknown_account_token'

        result = await self.fulfillment_service.fulfill_verified_transaction(
            db,
            user_id=account.user_id,
            product_id=str(txn_info.get('productId') or ''),
            txn_info=txn_info,
            expected_app_account_token=account.account_token_uuid,
        )
        return result.reason

    async def _handle_refund(self, db: AsyncSession, txn_info: dict[str, Any] | None) -> str:
        if not txn_info:
            return 'missing_transaction'

        transaction_id = str(txn_info.get('transactionId') or '')
        original_transaction_id = str(txn_info.get('originalTransactionId') or '')
        lookup_id = original_transaction_id or transaction_id
        apple_txn = await get_apple_transaction_by_transaction_id_for_update(db, lookup_id)
        if not apple_txn and transaction_id:
            apple_txn = await get_apple_transaction_by_transaction_id_for_update(db, transaction_id)
        if not apple_txn:
            return 'transaction_not_found'
        validation_error = await self._validate_stored_transaction_matches_notification(
            db,
            apple_txn=apple_txn,
            txn_info=txn_info,
            notification_type='REFUND',
        )
        if validation_error:
            return f'refund_{validation_error}'
        if apple_txn.status == 'refunded':
            return 'already_refunded'
        if apple_txn.environment == 'Sandbox' and settings.get_apple_iap_environment() == 'Production':
            return 'sandbox_ignored'

        user = await lock_user_for_pricing(db, apple_txn.user_id)
        refund_amount = min(apple_txn.amount_kopeks, user.balance_kopeks)
        if refund_amount < apple_txn.amount_kopeks:
            from app.database.crud.subscription import deactivate_subscription, get_active_subscriptions_by_user_id

            active_subs = await get_active_subscriptions_by_user_id(db, user.id)
            for sub in active_subs:
                await deactivate_subscription(db, sub, commit=False)
            await create_apple_abuse_event(
                db,
                'refund_after_funds_spent',
                user_id=user.id,
                severity='critical',
                transaction_id=apple_txn.transaction_id,
                product_id=apple_txn.product_id,
                details_json={'credited': apple_txn.amount_kopeks, 'debited': refund_amount},
            )

        if refund_amount > 0:
            from app.database.crud.user import subtract_user_balance

            await subtract_user_balance(
                db=db,
                user=user,
                amount_kopeks=refund_amount,
                description=f'Возврат Apple IAP: {apple_txn.product_id}',
                create_transaction=True,
                payment_method=PaymentMethod.APPLE_IAP,
                transaction_type=TransactionType.REFUND,
                commit=False,
            )

        await mark_apple_transaction_refunded(db, apple_txn.transaction_id)
        return 'refunded'

    async def _handle_refund_reversed(self, db: AsyncSession, txn_info: dict[str, Any] | None) -> str:
        if not txn_info:
            return 'missing_transaction'

        transaction_id = str(txn_info.get('transactionId') or '')
        original_transaction_id = str(txn_info.get('originalTransactionId') or '')
        lookup_id = original_transaction_id or transaction_id
        apple_txn = await get_apple_transaction_by_transaction_id_for_update(db, lookup_id)
        if not apple_txn and transaction_id:
            apple_txn = await get_apple_transaction_by_transaction_id_for_update(db, transaction_id)
        if not apple_txn:
            return 'transaction_not_found'
        validation_error = await self._validate_stored_transaction_matches_notification(
            db,
            apple_txn=apple_txn,
            txn_info=txn_info,
            notification_type='REFUND_REVERSED',
        )
        if validation_error:
            return f'refund_reversed_{validation_error}'
        if apple_txn.status != 'refunded':
            return 'not_refunded'
        if apple_txn.environment == 'Sandbox' and settings.get_apple_iap_environment() == 'Production':
            return 'sandbox_ignored'

        from app.database.crud.user import add_user_balance, get_user_by_id

        user = await get_user_by_id(db, apple_txn.user_id)
        if not user:
            return 'user_not_found'
        credited = await add_user_balance(
            db=db,
            user=user,
            amount_kopeks=apple_txn.amount_kopeks,
            description=f'Отмена возврата Apple IAP: {apple_txn.product_id}',
            payment_method=PaymentMethod.APPLE_IAP,
            commit=False,
        )
        if not credited:
            return 'refund_reversal_credit_failed'
        apple_txn.status = 'credited'
        apple_txn.refunded_at = None
        apple_txn.refund_reversed_at = datetime.now(UTC)
        await db.flush()
        return 'refund_reversed'

    async def _handle_consumption_request(self, txn_info: dict[str, Any] | None) -> str:
        if not txn_info:
            return 'missing_transaction'
        logger.info(
            'Apple CONSUMPTION_REQUEST received without recorded user consent; not sending consumption data',
            transaction_id=txn_info.get('transactionId'),
        )
        return 'consent_missing'


apple_iap_fulfillment_service = AppleIAPFulfillmentService()
apple_iap_notification_service = AppleIAPNotificationService(
    apple_service=apple_iap_fulfillment_service.apple_service,
    fulfillment_service=apple_iap_fulfillment_service,
)
