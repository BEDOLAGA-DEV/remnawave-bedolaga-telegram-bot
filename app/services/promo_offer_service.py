from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.crud.promo_offer_log import log_promo_offer_action
from app.database.models import (
    DiscountOffer,
    Subscription,
    SubscriptionTemporaryAccess,
    User,
)
from app.services.subscription_service import SubscriptionService


logger = structlog.get_logger(__name__)


class PromoOfferService:
    def __init__(self) -> None:
        self.subscription_service = SubscriptionService()

    async def grant_test_access(
        self,
        db: AsyncSession,
        user: User,
        offer: DiscountOffer,
    ) -> tuple[bool, list[str] | None, datetime | None, str]:
        subscription = getattr(user, 'subscription', None)
        if not subscription:
            return False, None, None, 'subscription_missing'

        payload = offer.extra_data or {}
        raw_squads = payload.get('test_squad_uuids') or payload.get('squads') or []
        if isinstance(raw_squads, str):
            candidates = [raw_squads]
        else:
            try:
                candidates = list(raw_squads)
            except TypeError:
                candidates = []

        squad_uuids: Sequence[str] = [str(item) for item in candidates if item]
        if not squad_uuids:
            return False, None, None, 'squads_missing'

        squad_uuids = list(dict.fromkeys(squad_uuids))

        connected = {str(item) for item in subscription.connected_squads or []}
        if squad_uuids and set(squad_uuids).issubset(connected):
            return False, None, None, 'already_connected'

        try:
            duration_hours = int(payload.get('test_duration_hours') or payload.get('duration_hours') or 24)
        except (TypeError, ValueError):
            duration_hours = 24

        if duration_hours <= 0:
            duration_hours = 24

        now = datetime.now(UTC)
        expires_at = now + timedelta(hours=duration_hours)

        original_connected = set(connected)
        newly_added: list[str] = []
        changes_made = False

        for squad_uuid in squad_uuids:
            normalized_uuid = str(squad_uuid)
            existing_result = await db.execute(
                select(SubscriptionTemporaryAccess)
                .where(
                    SubscriptionTemporaryAccess.offer_id == offer.id,
                    SubscriptionTemporaryAccess.squad_uuid == normalized_uuid,
                )
                .order_by(SubscriptionTemporaryAccess.id.desc())
            )
            existing_access = existing_result.scalars().first()
            if existing_access and existing_access.is_active:
                if existing_access.expires_at < expires_at:
                    existing_access.expires_at = expires_at
                    changes_made = True
                continue

            was_already_connected = normalized_uuid in connected
            if not was_already_connected:
                connected.add(normalized_uuid)
                newly_added.append(normalized_uuid)
                changes_made = True

            access_entry = SubscriptionTemporaryAccess(
                subscription_id=subscription.id,
                offer_id=offer.id,
                squad_uuid=normalized_uuid,
                expires_at=expires_at,
                is_active=True,
                was_already_connected=was_already_connected,
            )
            db.add(access_entry)
            changes_made = True

        connected_changed = connected != original_connected

        if newly_added:
            subscription.connected_squads = list(connected)
            subscription.updated_at = now
            changes_made = True

        if connected_changed:
            remnawave_user = await self.subscription_service.update_remnawave_user(
                db,
                subscription,
                sync_squads=True,
            )
            if remnawave_user is None:
                await db.rollback()
                await db.refresh(subscription)
                logger.error(
                    'Не удалось синхронизировать временный доступ подписки с RemnaWave', subscription_id=subscription.id
                )
                return False, None, None, 'remnawave_sync_failed'

            await db.refresh(subscription)
        elif changes_made:
            await db.commit()
            await db.refresh(subscription)

        return True, newly_added, expires_at, 'ok'

    async def cleanup_expired_test_access(self, db: AsyncSession) -> int:
        now = datetime.now(UTC)
        result = await db.execute(
            select(SubscriptionTemporaryAccess)
            .options(
                selectinload(SubscriptionTemporaryAccess.subscription),
                selectinload(SubscriptionTemporaryAccess.offer),
            )
            .where(
                SubscriptionTemporaryAccess.is_active == True,
                SubscriptionTemporaryAccess.expires_at <= now,
            )
        )
        entries = result.scalars().all()
        if not entries:
            return 0

        subscriptions_updates: dict[int, tuple[Subscription, set[str]]] = {}
        log_payloads: list[dict[str, object]] = []

        for entry in entries:
            entry.is_active = False
            entry.deactivated_at = now
            subscription = entry.subscription
            if not subscription:
                continue

            bucket = subscriptions_updates.setdefault(subscription.id, (subscription, set()))
            if not entry.was_already_connected:
                bucket[1].add(entry.squad_uuid)

            user_id = subscription.user_id
            if user_id:
                offer = entry.offer
                log_payloads.append(
                    {
                        'user_id': user_id,
                        'offer_id': entry.offer_id,
                        'source': getattr(offer, 'notification_type', None),
                        'percent': getattr(offer, 'discount_percent', None),
                        'effect_type': getattr(offer, 'effect_type', 'test_access'),
                        'details': {
                            'reason': 'test_access_expired',
                            'squad_uuid': entry.squad_uuid,
                        },
                    }
                )

        for subscription, squads_to_remove in subscriptions_updates.values():
            if not squads_to_remove:
                continue
            current = set(subscription.connected_squads or [])
            updated = current.difference(squads_to_remove)
            if updated != current:
                subscription.connected_squads = list(updated)
                subscription.updated_at = now
                try:
                    await self.subscription_service.update_remnawave_user(db, subscription, sync_squads=True)
                except Exception as exc:  # pragma: no cover - defensive logging
                    logger.error(
                        'Ошибка обновления Remnawave при отзыве тестового доступа подписки',
                        subscription_id=subscription.id,
                        exc=exc,
                    )

        await db.commit()
        for payload in log_payloads:
            try:
                await log_promo_offer_action(
                    db,
                    user_id=payload['user_id'],
                    offer_id=payload.get('offer_id'),
                    action='disabled',
                    source=payload.get('source'),
                    percent=payload.get('percent'),
                    effect_type=payload.get('effect_type'),
                    details=payload.get('details'),
                )
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.warning(
                    'Failed to record promo offer test access disable log for user',
                    payload=payload.get('user_id'),
                    exc=exc,
                )
                try:
                    await db.rollback()
                except Exception as rollback_error:  # pragma: no cover - defensive logging
                    logger.warning(
                        'Failed to rollback session after promo offer test access log failure',
                        rollback_error=rollback_error,
                    )
        return len(entries)


promo_offer_service = PromoOfferService()
