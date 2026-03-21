from __future__ import annotations

import base64
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import structlog
from app.utils.telegram_bot_factory import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot_factory import create_bot
from app.config import settings
from app.database.crud.subscription import (
    add_subscription_servers,
    extend_subscription,
)
from app.database.crud.transaction import create_transaction
from app.database.crud.user import subtract_user_balance
from app.database.models import PaymentMethod, Subscription, Transaction, TransactionType, User
from app.services.admin_notification_service import AdminNotificationService
from app.services.pricing_engine import RenewalPricing
from app.services.remnawave_service import RemnaWaveConfigurationError
from app.services.subscription_service import SubscriptionService


logger = structlog.get_logger(__name__)


class SubscriptionRenewalError(Exception):
    """Base class for subscription renewal related errors."""


class SubscriptionRenewalChargeError(SubscriptionRenewalError):
    """Raised when the balance charge step fails."""


@dataclass(slots=True)
class SubscriptionRenewalPricing:
    period_days: int
    period_id: str
    months: int
    base_original_total: int
    discounted_total: int
    final_total: int
    promo_discount_value: int
    promo_discount_percent: int
    overall_discount_percent: int
    per_month: int
    server_ids: list[int]
    details: dict[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return {
            'period_id': self.period_id,
            'period_days': self.period_days,
            'months': self.months,
            'base_original_total': self.base_original_total,
            'discounted_total': self.discounted_total,
            'final_total': self.final_total,
            'promo_discount_value': self.promo_discount_value,
            'promo_discount_percent': self.promo_discount_percent,
            'overall_discount_percent': self.overall_discount_percent,
            'per_month': self.per_month,
            'server_ids': list(self.server_ids),
            'details': dict(self.details),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> SubscriptionRenewalPricing:
        """Deserialize from dict. Supports both legacy SubscriptionRenewalPricing
        and new RenewalPricing (from PricingEngine) schemas."""
        breakdown = payload.get('breakdown') or {}
        period_days = int(payload.get('period_days', 0) or 0)
        final_total = int(payload.get('final_total', 0) or 0)

        # RenewalPricing uses 'promo_offer_discount', legacy uses 'promo_discount_value'
        promo_discount_value = int(
            payload.get('promo_discount_value', 0) or payload.get('promo_offer_discount', 0) or 0
        )

        # months: legacy has it directly, RenewalPricing needs derivation
        months = int(payload.get('months', 0) or 0)
        if not months and period_days > 0:
            months = max(1, round(period_days / 30))

        # base_original_total: legacy has it, RenewalPricing needs reconstruction
        base_original_total = int(payload.get('base_original_total', 0) or 0)
        if not base_original_total and final_total > 0:
            promo_group_discount = int(payload.get('promo_group_discount', 0) or 0)
            base_original_total = final_total + promo_group_discount + promo_discount_value

        # discounted_total: legacy has it, RenewalPricing = final + offer discount
        discounted_total = int(payload.get('discounted_total', 0) or 0)
        if not discounted_total:
            discounted_total = final_total + promo_discount_value

        # per_month
        per_month = int(payload.get('per_month', 0) or 0)
        if not per_month and months > 0:
            per_month = final_total // months

        # server_ids: legacy at top level, RenewalPricing in breakdown
        server_ids = list(payload.get('server_ids') or breakdown.get('server_ids') or [])

        # details: legacy uses 'details', RenewalPricing uses 'breakdown'
        details = dict(payload.get('details') or breakdown or {})

        # promo_discount_percent: from payload or breakdown
        promo_discount_percent = int(
            payload.get('promo_discount_percent', 0) or breakdown.get('offer_discount_pct', 0) or 0
        )

        # overall_discount_percent: derive if not present
        overall_discount_percent = int(payload.get('overall_discount_percent', 0) or 0)
        if not overall_discount_percent and base_original_total > 0 and base_original_total > final_total:
            overall_discount_percent = int(round((base_original_total - final_total) * 100 / base_original_total))

        return cls(
            period_days=period_days,
            period_id=str(payload.get('period_id') or build_renewal_period_id(period_days)),
            months=months,
            base_original_total=base_original_total,
            discounted_total=discounted_total,
            final_total=final_total,
            promo_discount_value=promo_discount_value,
            promo_discount_percent=promo_discount_percent,
            overall_discount_percent=overall_discount_percent,
            per_month=per_month,
            server_ids=server_ids,
            details=details,
        )


@dataclass(slots=True)
class SubscriptionRenewalResult:
    subscription: Subscription
    transaction: Transaction | None
    total_amount_kopeks: int
    charged_from_balance_kopeks: int
    old_end_date: datetime | None


@dataclass(slots=True)
class RenewalPaymentDescriptor:
    user_id: int
    subscription_id: int
    period_days: int
    total_amount_kopeks: int
    missing_amount_kopeks: int
    payload_id: str
    pricing_snapshot: dict[str, Any] | None = None

    @property
    def balance_component_kopeks(self) -> int:
        remaining = self.total_amount_kopeks - self.missing_amount_kopeks
        return max(0, remaining)


_PAYLOAD_PREFIX = 'subscription_renewal'


def build_renewal_period_id(period_days: int) -> str:
    return f'days:{period_days}'


def build_payment_descriptor(
    user_id: int,
    subscription_id: int,
    period_days: int,
    total_amount_kopeks: int,
    missing_amount_kopeks: int,
    *,
    pricing_snapshot: dict[str, Any] | None = None,
) -> RenewalPaymentDescriptor:
    return RenewalPaymentDescriptor(
        user_id=user_id,
        subscription_id=subscription_id,
        period_days=period_days,
        total_amount_kopeks=max(0, int(total_amount_kopeks)),
        missing_amount_kopeks=max(0, int(missing_amount_kopeks)),
        payload_id=uuid4().hex[:8],
        pricing_snapshot=pricing_snapshot or None,
    )


def encode_payment_payload(descriptor: RenewalPaymentDescriptor) -> str:
    snapshot_segment = ''
    if descriptor.pricing_snapshot:
        try:
            raw_snapshot = json.dumps(
                descriptor.pricing_snapshot,
                separators=(',', ':'),
                ensure_ascii=False,
            ).encode('utf-8')
            snapshot_segment = base64.urlsafe_b64encode(raw_snapshot).decode('ascii').rstrip('=')
        except (TypeError, ValueError):
            snapshot_segment = ''

    payload = (
        f'{_PAYLOAD_PREFIX}|{descriptor.user_id}|{descriptor.subscription_id}|'
        f'{descriptor.period_days}|{descriptor.total_amount_kopeks}|'
        f'{descriptor.missing_amount_kopeks}|{descriptor.payload_id}'
    )

    if snapshot_segment:
        payload = f'{payload}|{snapshot_segment}'

    return payload


def decode_payment_payload(payload: str, expected_user_id: int | None = None) -> RenewalPaymentDescriptor | None:
    if not payload or not payload.startswith(f'{_PAYLOAD_PREFIX}|'):
        return None

    parts = payload.split('|')
    if len(parts) < 7:
        return None

    try:
        (
            _,
            user_id_raw,
            subscription_raw,
            period_raw,
            total_raw,
            missing_raw,
            payload_id,
            *snapshot_parts,
        ) = parts
        user_id = int(user_id_raw)
        subscription_id = int(subscription_raw)
        period_days = int(period_raw)
        total_amount = int(total_raw)
        missing_amount = int(missing_raw)
    except (TypeError, ValueError):
        return None

    pricing_snapshot: dict[str, Any] | None = None
    if snapshot_parts:
        encoded_snapshot = snapshot_parts[0]
        if encoded_snapshot:
            padding = '=' * (-len(encoded_snapshot) % 4)
            try:
                decoded = base64.urlsafe_b64decode((encoded_snapshot + padding).encode('ascii'))
                snapshot_data = json.loads(decoded.decode('utf-8'))
                if isinstance(snapshot_data, dict):
                    pricing_snapshot = snapshot_data
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
                logger.warning('Failed to decode renewal pricing snapshot from payload')

    if expected_user_id is not None and user_id != expected_user_id:
        return None

    return RenewalPaymentDescriptor(
        user_id=user_id,
        subscription_id=subscription_id,
        period_days=period_days,
        total_amount_kopeks=max(0, total_amount),
        missing_amount_kopeks=max(0, missing_amount),
        payload_id=payload_id,
        pricing_snapshot=pricing_snapshot,
    )


def build_payment_metadata(descriptor: RenewalPaymentDescriptor) -> dict[str, Any]:
    return {
        'payment_purpose': _PAYLOAD_PREFIX,
        'subscription_id': str(descriptor.subscription_id),
        'period_days': str(descriptor.period_days),
        'total_amount_kopeks': str(descriptor.total_amount_kopeks),
        'missing_amount_kopeks': str(descriptor.missing_amount_kopeks),
        'payload_id': descriptor.payload_id,
        'pricing_snapshot': descriptor.pricing_snapshot or {},
    }


def parse_payment_metadata(
    metadata: dict[str, Any] | None,
    *,
    expected_user_id: int | None = None,
) -> RenewalPaymentDescriptor | None:
    if not metadata:
        return None

    if metadata.get('payment_purpose') != _PAYLOAD_PREFIX:
        return None

    try:
        subscription_id = int(metadata.get('subscription_id'))
        period_days = int(metadata.get('period_days'))
        total_amount = int(metadata.get('total_amount_kopeks'))
        missing_amount = int(metadata.get('missing_amount_kopeks'))
    except (TypeError, ValueError):
        return None

    payload_id = str(metadata.get('payload_id') or '')
    user_id = metadata.get('user_id')
    if user_id is not None:
        try:
            user_id_int = int(user_id)
        except (TypeError, ValueError):
            user_id_int = None
    else:
        user_id_int = None

    if expected_user_id is not None and user_id_int is not None and user_id_int != expected_user_id:
        return None

    pricing_snapshot = metadata.get('pricing_snapshot')
    if isinstance(pricing_snapshot, dict):
        snapshot_dict = pricing_snapshot
    else:
        snapshot_dict = None

    return RenewalPaymentDescriptor(
        user_id=user_id_int or expected_user_id or 0,
        subscription_id=subscription_id,
        period_days=period_days,
        total_amount_kopeks=max(0, total_amount),
        missing_amount_kopeks=max(0, missing_amount),
        payload_id=payload_id,
        pricing_snapshot=snapshot_dict,
    )


async def with_admin_notification_service(
    handler: Callable[[AdminNotificationService], Awaitable[Any]],
) -> None:
    if not getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False):
        return
    if not settings.BOT_TOKEN:
        logger.debug('Skipping admin notification: bot token is not configured')
        return

    bot: Bot | None = None
    try:
        bot = create_bot()
        service = AdminNotificationService(bot)
        await handler(service)
    except Exception as error:  # pragma: no cover - defensive logging
        logger.error('Failed to send admin notification from renewal service', error=error)
    finally:
        if bot is not None:
            await bot.session.close()


class SubscriptionRenewalService:
    """Shared helpers for subscription renewal pricing and processing."""

    async def finalize(
        self,
        db: AsyncSession,
        user: User,
        subscription: Subscription,
        pricing: SubscriptionRenewalPricing | RenewalPricing,
        *,
        charge_balance_amount: int | None = None,
        description: str | None = None,
        payment_method: PaymentMethod | None = None,
    ) -> SubscriptionRenewalResult:
        final_total = int(pricing.final_total)
        final_total = max(final_total, 0)

        period_days = int(pricing.period_days)
        charge_from_balance = charge_balance_amount
        if charge_from_balance is None:
            charge_from_balance = final_total
        charge_from_balance = max(0, min(charge_from_balance, final_total))

        # Support both SubscriptionRenewalPricing and RenewalPricing
        if isinstance(pricing, SubscriptionRenewalPricing):
            consume_promo_offer = bool(pricing.promo_discount_value)
        else:
            consume_promo_offer = bool(pricing.promo_offer_discount)

        description_text = description or f'Продление подписки на {period_days} дней'

        # Save promo offer state before charge so we can restore on failure
        saved_promo_percent = int(getattr(user, 'promo_offer_discount_percent', 0) or 0) if consume_promo_offer else 0
        saved_promo_source = getattr(user, 'promo_offer_discount_source', None) if consume_promo_offer else None
        saved_promo_expires = getattr(user, 'promo_offer_discount_expires_at', None) if consume_promo_offer else None

        if charge_from_balance > 0 or consume_promo_offer:
            success = await subtract_user_balance(
                db,
                user,
                charge_from_balance,
                description_text,
                consume_promo_offer=consume_promo_offer,
                mark_as_paid_subscription=True,
            )
            if not success:
                raise SubscriptionRenewalChargeError('Failed to charge balance')
            await db.refresh(user)

        # Lock subscription row to prevent double-extension race
        from sqlalchemy import select as sa_select

        from app.database.models import Subscription as SubscriptionModel

        locked_result = await db.execute(
            sa_select(SubscriptionModel)
            .where(SubscriptionModel.id == subscription.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        subscription_before = locked_result.scalar_one()
        old_end_date = subscription_before.end_date

        # Determine expired state BEFORE extend_subscription mutates the object
        now = datetime.now(UTC)
        was_expired = subscription_before.status in ('expired', 'disabled', 'limited') or (
            subscription_before.end_date is not None and subscription_before.end_date <= now
        )

        try:
            subscription_after = await extend_subscription(db, subscription_before, period_days)
        except Exception:
            # Session may be in a failed state after a broken commit — rollback first
            await db.rollback()

            # Compensate: refund the charged balance since extension failed
            if charge_from_balance > 0 or (consume_promo_offer and saved_promo_percent > 0):
                try:
                    from app.database.crud.user import add_user_balance

                    if charge_from_balance > 0:
                        refunded = await add_user_balance(
                            db,
                            user,
                            charge_from_balance,
                            'Возврат: ошибка продления подписки',
                            create_transaction=True,
                            transaction_type=TransactionType.REFUND,
                        )
                        if not refunded:
                            logger.critical(
                                'CRITICAL: add_user_balance returned False during refund',
                                charge_from_balance=charge_from_balance,
                                user_id=user.id,
                            )

                    # Restore consumed promo offer fields
                    if consume_promo_offer and saved_promo_percent > 0:
                        user.promo_offer_discount_percent = saved_promo_percent
                        user.promo_offer_discount_source = saved_promo_source
                        user.promo_offer_discount_expires_at = saved_promo_expires
                        await db.commit()
                        logger.info(
                            'Restored promo offer after failed extension',
                            user_id=user.id,
                            restored_percent=saved_promo_percent,
                        )
                except Exception as refund_error:
                    logger.critical(
                        'CRITICAL: Failed to refund kopeks to user after extension failure',
                        charge_from_balance=charge_from_balance,
                        user_id=user.id,
                        refund_error=refund_error,
                    )
            raise

        # Support both SubscriptionRenewalPricing (server_ids, details) and RenewalPricing (breakdown)
        if isinstance(pricing, SubscriptionRenewalPricing):
            server_ids = pricing.server_ids or []
            server_prices_for_period = (pricing.details or {}).get('servers_individual_prices', [])
        else:
            breakdown = pricing.breakdown or {}
            server_ids = breakdown.get('server_ids', [])
            server_prices_for_period = breakdown.get('servers_individual_prices', [])
        if server_ids:
            try:
                await add_subscription_servers(
                    db,
                    subscription_after,
                    server_ids,
                    server_prices_for_period,
                )
            except Exception as error:  # pragma: no cover - defensive logging
                logger.warning(
                    'Failed to record renewal server prices for subscription',
                    subscription_after_id=subscription_after.id,
                    error=error,
                )

        reset_traffic = was_expired and settings.RESET_TRAFFIC_ON_PAYMENT
        subscription_service = SubscriptionService()
        try:
            await db.refresh(user)
            if getattr(user, 'remnawave_uuid', None):
                await subscription_service.update_remnawave_user(
                    db,
                    subscription_after,
                    reset_traffic=reset_traffic,
                    reset_reason='subscription renewal',
                )
            else:
                await subscription_service.create_remnawave_user(
                    db,
                    subscription_after,
                    reset_traffic=reset_traffic,
                    reset_reason='subscription renewal',
                )
        except RemnaWaveConfigurationError as error:  # pragma: no cover - configuration issues
            logger.warning('RemnaWave update skipped', error=error)
        except Exception as error:  # pragma: no cover - defensive logging
            logger.error(
                'Failed to sync RemnaWave user for subscription',
                subscription_after_id=subscription_after.id,
                error=error,
            )

        transaction: Transaction | None = None
        try:
            transaction = await create_transaction(
                db=db,
                user_id=user.id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                amount_kopeks=final_total,
                description=description_text,
                payment_method=payment_method,
            )
        except Exception as error:  # pragma: no cover - defensive logging
            logger.warning(
                'Failed to create renewal transaction for subscription',
                subscription_after_id=subscription_after.id,
                error=error,
            )

        await db.refresh(user)
        await db.refresh(subscription_after)

        if transaction and old_end_date and subscription_after.end_date:
            await with_admin_notification_service(
                lambda service: service.send_subscription_extension_notification(
                    db,
                    user,
                    subscription_after,
                    transaction,
                    period_days,
                    old_end_date,
                    new_end_date=subscription_after.end_date,
                    balance_after=user.balance_kopeks,
                )
            )

        return SubscriptionRenewalResult(
            subscription=subscription_after,
            transaction=transaction,
            total_amount_kopeks=final_total,
            charged_from_balance_kopeks=charge_from_balance,
            old_end_date=old_end_date,
        )


def calculate_missing_amount(balance_kopeks: int, total_kopeks: int) -> int:
    if total_kopeks <= 0:
        return 0
    if balance_kopeks <= 0:
        return total_kopeks
    return max(0, total_kopeks - min(balance_kopeks, total_kopeks))
