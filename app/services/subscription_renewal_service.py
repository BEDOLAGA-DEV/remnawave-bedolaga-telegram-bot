from __future__ import annotations

import base64
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

import structlog
from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.server_squad import get_server_ids_by_uuids, get_server_squads_by_uuids
from app.database.crud.subscription import (
    add_subscription_servers,
    calculate_subscription_total_cost,
    extend_subscription,
)
from app.database.crud.transaction import create_transaction
from app.database.crud.user import subtract_user_balance
from app.database.models import PaymentMethod, Subscription, Transaction, TransactionType, User
from app.services.admin_notification_service import AdminNotificationService
from app.services.remnawave_service import RemnaWaveConfigurationError
from app.services.subscription_service import SubscriptionService
from app.utils.pricing_utils import (
    apply_percentage_discount,
    calculate_months_from_days,
    format_period_description,
    validate_pricing_calculation,
)


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
        return cls(
            period_days=int(payload.get('period_days', 0) or 0),
            period_id=str(payload.get('period_id') or build_renewal_period_id(int(payload.get('period_days', 0) or 0))),
            months=int(payload.get('months', 0) or 0),
            base_original_total=int(payload.get('base_original_total', 0) or 0),
            discounted_total=int(payload.get('discounted_total', 0) or 0),
            final_total=int(payload.get('final_total', 0) or 0),
            promo_discount_value=int(payload.get('promo_discount_value', 0) or 0),
            promo_discount_percent=int(payload.get('promo_discount_percent', 0) or 0),
            overall_discount_percent=int(payload.get('overall_discount_percent', 0) or 0),
            per_month=int(payload.get('per_month', 0) or 0),
            server_ids=list(payload.get('server_ids', []) or []),
            details=dict(payload.get('details', {}) or {}),
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
        bot = Bot(token=settings.BOT_TOKEN)
        service = AdminNotificationService(bot)
        await handler(service)
    except Exception as error:  # pragma: no cover - defensive logging
        logger.error('Failed to send admin notification from renewal service', error=error)
    finally:
        if bot is not None:
            await bot.session.close()


class SubscriptionRenewalService:
    """Shared helpers for subscription renewal pricing and processing."""

    async def calculate_pricing(
        self,
        db: AsyncSession,
        user: User,
        subscription: Subscription,
        period_days: int,
    ) -> SubscriptionRenewalPricing:
        connected_uuids = [str(uuid) for uuid in list(subscription.connected_squads or [])]
        server_ids: list[int] = []
        if connected_uuids:
            server_ids = await get_server_ids_by_uuids(db, connected_uuids)

            # Валидация: проверяем доступность серверов для промогруппы пользователя
            await self._validate_servers_for_user_promo_group(db, user, connected_uuids)

        # В режиме fixed_with_topup при продлении используем фиксированный лимит
        purchased_traffic = subscription.purchased_traffic_gb or 0
        if settings.is_traffic_fixed():
            traffic_limit = settings.get_fixed_traffic_limit()
        # Separate base traffic from purchased to avoid wrong tier lookup
        # e.g. 25GB base + 100GB purchased = 125GB total → would round up to 250GB tier
        elif purchased_traffic > 0:
            base_traffic = (subscription.traffic_limit_gb or 0) - purchased_traffic
            if base_traffic <= 0:
                logger.warning(
                    'Purchased traffic >= total limit, pricing purchased portion only',
                    subscription_id=subscription.id,
                    traffic_limit_gb=subscription.traffic_limit_gb,
                    purchased_traffic_gb=purchased_traffic,
                )
                # All traffic is purchased; pass it as sole traffic_limit
                # and clear purchased_traffic to avoid double-counting below
                traffic_limit = purchased_traffic
                purchased_traffic = 0
            else:
                traffic_limit = base_traffic
        else:
            traffic_limit = subscription.traffic_limit_gb
            if traffic_limit is None:
                traffic_limit = settings.DEFAULT_TRAFFIC_LIMIT_GB

        devices_limit = subscription.device_limit
        if devices_limit is None:
            devices_limit = settings.DEFAULT_DEVICE_LIMIT

        total_cost, details = await calculate_subscription_total_cost(
            db,
            period_days,
            int(traffic_limit or 0),
            server_ids,
            int(devices_limit or 0),
            user=user,
        )

        months = details.get('months_in_period') or calculate_months_from_days(period_days)

        # Add purchased traffic cost separately (uses its own tier price, same discount %)
        if purchased_traffic > 0 and not settings.is_traffic_fixed():
            purchased_price_per_month = settings.get_traffic_price(purchased_traffic)
            traffic_discount_pct = details.get('traffic_discount_percent', 0)
            purchased_disc_per_month = purchased_price_per_month * traffic_discount_pct // 100
            discounted_purchased_per_month = purchased_price_per_month - purchased_disc_per_month
            purchased_total = discounted_purchased_per_month * months
            purchased_disc_total = purchased_disc_per_month * months

            total_cost += purchased_total
            details['traffic_price_per_month'] = details.get('traffic_price_per_month', 0) + purchased_price_per_month
            details['total_traffic_price'] = details.get('total_traffic_price', 0) + purchased_total
            details['traffic_discount_total'] = details.get('traffic_discount_total', 0) + purchased_disc_total

        base_original_total = (
            details.get('base_price_original', 0)
            + details.get('traffic_price_per_month', 0) * months
            + details.get('servers_price_per_month', 0) * months
            + details.get('devices_price_per_month', 0) * months
        )

        discounted_total = total_cost

        monthly_additions = 0
        if months > 0:
            monthly_additions = (
                details.get('total_servers_price', 0) // months
                + details.get('total_devices_price', 0) // months
                + details.get('total_traffic_price', 0) // months
            )

        if not validate_pricing_calculation(
            details.get('base_price', 0),
            monthly_additions,
            months,
            discounted_total,
        ):
            logger.warning(
                'Renewal pricing validation failed for subscription (period)',
                subscription_id=subscription.id,
                period_days=period_days,
            )

        from app.utils.promo_offer import get_user_active_promo_discount_percent

        promo_percent = get_user_active_promo_discount_percent(user)

        final_total = discounted_total
        promo_discount_value = 0
        if promo_percent > 0 and discounted_total > 0:
            final_total, promo_discount_value = apply_percentage_discount(
                discounted_total,
                promo_percent,
            )

        overall_discount_value = max(0, base_original_total - final_total)
        overall_discount_percent = 0
        if base_original_total > 0 and overall_discount_value > 0:
            overall_discount_percent = int(round(overall_discount_value * 100 / base_original_total))

        per_month = final_total // months if months else final_total

        return SubscriptionRenewalPricing(
            period_days=period_days,
            period_id=build_renewal_period_id(period_days),
            months=months,
            base_original_total=base_original_total,
            discounted_total=discounted_total,
            final_total=final_total,
            promo_discount_value=promo_discount_value,
            promo_discount_percent=promo_percent if promo_discount_value else 0,
            overall_discount_percent=overall_discount_percent,
            per_month=per_month,
            server_ids=list(server_ids),
            details=details,
        )

    async def finalize(
        self,
        db: AsyncSession,
        user: User,
        subscription: Subscription,
        pricing: SubscriptionRenewalPricing,
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

        consume_promo_offer = bool(pricing.promo_discount_value)

        description_text = description or f'Продление подписки на {period_days} дней'

        if charge_from_balance > 0 or consume_promo_offer:
            success = await subtract_user_balance(
                db,
                user,
                charge_from_balance,
                description_text,
                consume_promo_offer=consume_promo_offer,
            )
            if not success:
                raise SubscriptionRenewalChargeError('Failed to charge balance')
            await db.refresh(user)

        subscription_before = subscription
        old_end_date = subscription_before.end_date

        try:
            subscription_after = await extend_subscription(db, subscription_before, period_days)
        except Exception:
            # Compensate: refund the charged balance since extension failed
            if charge_from_balance > 0:
                try:
                    from app.database.crud.user import add_user_balance

                    await add_user_balance(
                        db,
                        user,
                        charge_from_balance,
                        'Возврат: ошибка продления подписки',
                        create_transaction=True,
                        transaction_type=TransactionType.REFUND,
                    )
                except Exception as refund_error:
                    logger.critical(
                        'CRITICAL: Failed to refund kopeks to user after extension failure',
                        charge_from_balance=charge_from_balance,
                        user_id=user.id,
                        refund_error=refund_error,
                    )
            raise

        server_ids = pricing.server_ids or []
        server_prices_for_period = pricing.details.get('servers_individual_prices', [])
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

        subscription_service = SubscriptionService()
        try:
            await subscription_service.update_remnawave_user(
                db,
                subscription_after,
                reset_traffic=settings.RESET_TRAFFIC_ON_PAYMENT,
                reset_reason='subscription renewal',
            )
        except RemnaWaveConfigurationError as error:  # pragma: no cover - configuration issues
            logger.warning('RemnaWave update skipped', error=error)
        except Exception as error:  # pragma: no cover - defensive logging
            logger.error(
                'Failed to update RemnaWave user for subscription',
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

    async def _validate_servers_for_user_promo_group(
        self,
        db: AsyncSession,
        user: User,
        server_uuids: list[str],
    ) -> None:
        """
        Проверяет, что все серверы подписки доступны для промогруппы пользователя.
        Логирует предупреждения если серверы недоступны.
        """
        if not server_uuids:
            return

        try:
            await db.refresh(user, ['user_promo_groups', 'promo_group'])
        except Exception:
            pass

        user_promo_group = user.get_primary_promo_group() if user else None
        if not user_promo_group:
            return

        servers = await get_server_squads_by_uuids(db, server_uuids)
        unavailable_servers = []

        for server in servers:
            if server.allowed_promo_groups:
                allowed_ids = {pg.id for pg in server.allowed_promo_groups}
                if user_promo_group.id not in allowed_ids:
                    unavailable_servers.append(server.display_name or server.squad_uuid)

        if unavailable_servers:
            user_identifier = user.telegram_id or user.email or f'user#{user.id}'
            logger.warning(
                '⚠️ Пользователь (promo_group=) продлевает подписку с серверами, недоступными для его промогруппы: . Это может привести к неправильному расчёту цены!',
                user_identifier=user_identifier,
                user_promo_group_name=user_promo_group.name,
                value=', '.join(unavailable_servers),
            )

    def build_option_payload(
        self,
        pricing: SubscriptionRenewalPricing,
        *,
        language: str,
    ) -> dict[str, Any]:
        label = format_period_description(pricing.period_days, language)
        price_label = settings.format_price(pricing.final_total)
        original_label = None
        if pricing.base_original_total and pricing.base_original_total != pricing.final_total:
            original_label = settings.format_price(pricing.base_original_total)

        per_month_label = settings.format_price(pricing.per_month)

        payload = {
            'id': pricing.period_id,
            'days': pricing.period_days,
            'months': pricing.months,
            'price_kopeks': pricing.final_total,
            'price_label': price_label,
            'original_price_kopeks': pricing.base_original_total,
            'original_price_label': original_label,
            'discount_percent': pricing.overall_discount_percent,
            'price_per_month_kopeks': pricing.per_month,
            'price_per_month_label': per_month_label,
            'title': label,
        }

        return payload


def calculate_missing_amount(balance_kopeks: int, total_kopeks: int) -> int:
    if total_kopeks <= 0:
        return 0
    if balance_kopeks <= 0:
        return total_kopeks
    return max(0, total_kopeks - min(balance_kopeks, total_kopeks))
