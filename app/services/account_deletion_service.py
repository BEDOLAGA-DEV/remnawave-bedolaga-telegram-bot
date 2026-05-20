from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.subscription import decrement_subscription_server_counts, get_all_subscriptions_by_user_id
from app.database.models import (
    AccountDeletionRequest,
    AccountDeletionRequestStatus,
    CabinetRefreshToken,
    SavedPaymentMethod,
    Subscription,
    SubscriptionStatus,
    User,
    UserStatus,
)
from app.services.remnawave_webhook_service import RemnaWaveWebhookService
from app.services.subscription_service import SubscriptionService
from app.services.user_cart_service import UserCartService


logger = structlog.get_logger(__name__)

_OAUTH_FIELDS = ('google_id', 'yandex_id', 'discord_id', 'vk_id', 'apple_id')
_CANCELLABLE_SUBSCRIPTION_STATUSES = {
    SubscriptionStatus.ACTIVE.value,
    SubscriptionStatus.TRIAL.value,
    SubscriptionStatus.LIMITED.value,
    SubscriptionStatus.PENDING.value,
}
_SERVER_COUNTED_STATUSES = {
    SubscriptionStatus.ACTIVE.value,
    SubscriptionStatus.TRIAL.value,
    SubscriptionStatus.LIMITED.value,
}


@dataclass(frozen=True)
class AccountDeletionResult:
    user_id: int
    subscriptions_disabled: int = 0
    panel_users_deleted: int = 0
    panel_users_disabled: int = 0
    refresh_tokens_revoked: int = 0
    saved_payment_methods_deactivated: int = 0
    cleanup_request_id: int | None = None
    cart_deleted: bool = False


@dataclass(frozen=True)
class AccountDeletionCleanupStats:
    processed: int = 0
    completed: int = 0
    retried: int = 0
    failed: int = 0
    failed_request_ids: tuple[int, ...] = ()


class AccountDeletionPanelError(RuntimeError):
    def __init__(self, failed_panel_uuids: list[str]) -> None:
        failed = ', '.join(failed_panel_uuids) or 'unknown'
        super().__init__(f'Failed to remove RemnaWave users during account deletion: {failed}')
        self.failed_panel_uuids = failed_panel_uuids


class AccountDeletionService:
    async def delete_own_account(self, db: AsyncSession, user: User) -> AccountDeletionResult:
        """Deactivate a user's account while preserving audit/finance rows."""
        now = datetime.now(UTC)
        user_id = user.id
        telegram_id = int(user.telegram_id) if user.telegram_id is not None else None
        subscriptions = await get_all_subscriptions_by_user_id(db, user_id)
        panel_uuids = self._collect_panel_uuids(user, subscriptions)

        subscriptions_disabled = await self._disable_subscriptions(db, subscriptions, now)
        refresh_tokens_revoked = await self._revoke_refresh_tokens(db, user_id, now)
        saved_payment_methods_deactivated = await self._deactivate_saved_payment_methods(db, user_id, now)
        await self._unlink_referrals(db, user_id)
        cleanup_request = self._create_cleanup_request(user_id, panel_uuids, telegram_id, now)
        db.add(cleanup_request)
        self._anonymize_user(user, now)
        await db.flush()

        await db.commit()

        cart_deleted = await UserCartService().delete_user_cart(user_id)

        logger.info(
            'User self-deleted account',
            user_id=user_id,
            subscriptions_disabled=subscriptions_disabled,
            refresh_tokens_revoked=refresh_tokens_revoked,
            saved_payment_methods_deactivated=saved_payment_methods_deactivated,
            cleanup_request_id=cleanup_request.id,
            panel_cleanup_queued=bool(panel_uuids),
            cart_deleted=cart_deleted,
        )

        return AccountDeletionResult(
            user_id=user_id,
            subscriptions_disabled=subscriptions_disabled,
            refresh_tokens_revoked=refresh_tokens_revoked,
            saved_payment_methods_deactivated=saved_payment_methods_deactivated,
            cleanup_request_id=cleanup_request.id,
            cart_deleted=cart_deleted,
        )

    @staticmethod
    def _collect_panel_uuids(user: User, subscriptions: list[Subscription]) -> list[str]:
        seen: set[str] = set()
        panel_uuids: list[str] = []
        for panel_uuid in [user.remnawave_uuid, *(sub.remnawave_uuid for sub in subscriptions)]:
            normalized = (panel_uuid or '').strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                panel_uuids.append(normalized)
        return panel_uuids

    async def _remove_panel_users(self, panel_uuids: list[str], *, telegram_id: int | None) -> tuple[int, int]:
        if not panel_uuids:
            return 0, 0

        RemnaWaveWebhookService.mark_intentional_panel_deletion(panel_uuids=panel_uuids, telegram_id=telegram_id)

        subscription_service = SubscriptionService()
        deleted_count = 0
        disabled_count = 0
        failed_panel_uuids: list[str] = []
        for panel_uuid in panel_uuids:
            deleted = await subscription_service.delete_remnawave_user(panel_uuid)
            if deleted:
                deleted_count += 1
                continue

            disabled = await subscription_service.disable_remnawave_user(panel_uuid)
            if disabled:
                disabled_count += 1
            else:
                failed_panel_uuids.append(panel_uuid)

        if failed_panel_uuids:
            logger.error(
                'Failed to remove RemnaWave users during self-delete',
                failed_panel_uuids=failed_panel_uuids,
            )
            raise AccountDeletionPanelError(failed_panel_uuids)

        return deleted_count, disabled_count

    def _create_cleanup_request(
        self,
        user_id: int,
        panel_uuids: list[str],
        telegram_id: int | None,
        now: datetime,
    ) -> AccountDeletionRequest:
        has_panel_work = bool(panel_uuids)
        return AccountDeletionRequest(
            user_id=user_id,
            status=(
                AccountDeletionRequestStatus.PENDING.value
                if has_panel_work
                else AccountDeletionRequestStatus.COMPLETED.value
            ),
            panel_uuids=panel_uuids,
            telegram_id=telegram_id,
            next_retry_at=now,
            completed_at=None if has_panel_work else now,
        )

    async def process_pending_panel_cleanup(
        self,
        db: AsyncSession,
        *,
        limit: int = 10,
    ) -> AccountDeletionCleanupStats:
        now = datetime.now(UTC)
        result = await db.execute(
            select(AccountDeletionRequest)
            .where(
                AccountDeletionRequest.status.in_(
                    [
                        AccountDeletionRequestStatus.PENDING.value,
                        AccountDeletionRequestStatus.PROCESSING.value,
                    ]
                ),
                or_(
                    AccountDeletionRequest.next_retry_at.is_(None),
                    AccountDeletionRequest.next_retry_at <= now,
                ),
            )
            .order_by(AccountDeletionRequest.created_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        requests = list(result.scalars().all())
        stats = AccountDeletionCleanupStats()

        for deletion_request in requests:
            stats = await self._process_cleanup_request(db, deletion_request, stats, now)

        return stats

    async def _process_cleanup_request(
        self,
        db: AsyncSession,
        deletion_request: AccountDeletionRequest,
        stats: AccountDeletionCleanupStats,
        now: datetime,
    ) -> AccountDeletionCleanupStats:
        deletion_request.status = AccountDeletionRequestStatus.PROCESSING.value
        deletion_request.updated_at = now
        await db.flush()

        try:
            panel_uuids = list(deletion_request.panel_uuids or [])
            await self._remove_panel_users(panel_uuids, telegram_id=deletion_request.telegram_id)
        except Exception as error:
            exhausted = self._schedule_cleanup_retry(deletion_request, error, now)
            logger.warning(
                'Account deletion panel cleanup retry scheduled' if not exhausted else 'Account deletion panel cleanup failed',
                deletion_request_id=deletion_request.id,
                attempt_count=deletion_request.attempt_count,
                max_attempts=deletion_request.max_attempts,
                next_retry_at=deletion_request.next_retry_at,
                error=error,
            )
            return AccountDeletionCleanupStats(
                processed=stats.processed + 1,
                completed=stats.completed,
                retried=stats.retried + (0 if exhausted else 1),
                failed=stats.failed + (1 if exhausted else 0),
                failed_request_ids=(
                    (*stats.failed_request_ids, deletion_request.id)
                    if exhausted and deletion_request.id is not None
                    else stats.failed_request_ids
                ),
            )

        deletion_request.status = AccountDeletionRequestStatus.COMPLETED.value
        deletion_request.completed_at = now
        deletion_request.updated_at = now
        deletion_request.last_error = None
        logger.info('Account deletion panel cleanup completed', deletion_request_id=deletion_request.id)

        return AccountDeletionCleanupStats(
            processed=stats.processed + 1,
            completed=stats.completed + 1,
            retried=stats.retried,
            failed=stats.failed,
            failed_request_ids=stats.failed_request_ids,
        )

    def _schedule_cleanup_retry(
        self,
        deletion_request: AccountDeletionRequest,
        error: Exception,
        now: datetime,
    ) -> bool:
        deletion_request.attempt_count = (deletion_request.attempt_count or 0) + 1
        deletion_request.last_error = str(error)
        deletion_request.updated_at = now

        if deletion_request.attempt_count >= deletion_request.max_attempts:
            deletion_request.status = AccountDeletionRequestStatus.FAILED.value
            deletion_request.next_retry_at = now
            return True

        deletion_request.status = AccountDeletionRequestStatus.PENDING.value
        backoff_seconds = min(60 * (2 ** (deletion_request.attempt_count - 1)), 3600)
        deletion_request.next_retry_at = now + timedelta(seconds=backoff_seconds)
        return False

    async def _disable_subscriptions(
        self,
        db: AsyncSession,
        subscriptions: list[Subscription],
        now: datetime,
    ) -> int:
        disabled_count = 0
        for subscription in subscriptions:
            previous_status = subscription.status
            subscription.autopay_enabled = False

            if previous_status in _CANCELLABLE_SUBSCRIPTION_STATUSES:
                if previous_status in _SERVER_COUNTED_STATUSES:
                    await decrement_subscription_server_counts(db, subscription)

                subscription.status = SubscriptionStatus.DISABLED.value
                subscription.end_date = now
                subscription.updated_at = now
                disabled_count += 1
            else:
                subscription.updated_at = now

        return disabled_count

    async def _revoke_refresh_tokens(self, db: AsyncSession, user_id: int, now: datetime) -> int:
        result = await db.execute(
            update(CabinetRefreshToken)
            .where(
                CabinetRefreshToken.user_id == user_id,
                CabinetRefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        return int(result.rowcount or 0)

    async def _deactivate_saved_payment_methods(self, db: AsyncSession, user_id: int, now: datetime) -> int:
        result = await db.execute(
            update(SavedPaymentMethod)
            .where(
                SavedPaymentMethod.user_id == user_id,
                SavedPaymentMethod.is_active == True,
            )
            .values(is_active=False, updated_at=now)
        )
        return int(result.rowcount or 0)

    async def _unlink_referrals(self, db: AsyncSession, user_id: int) -> None:
        await db.execute(update(User).where(User.referred_by_id == user_id).values(referred_by_id=None))

    @staticmethod
    def _anonymize_user(user: User, now: datetime) -> None:
        user.status = UserStatus.DELETED.value
        user.telegram_id = None
        user.username = None
        user.first_name = None
        user.last_name = None
        user.email = None
        user.email_verified = False
        user.email_verified_at = None
        user.email_verification_source = None
        user.email_verification_token = None
        user.email_verification_expires = None
        user.email_change_new = None
        user.email_change_code = None
        user.email_change_expires = None
        user.password_hash = None
        user.password_reset_token = None
        user.password_reset_expires = None
        user.pending_campaign_slug = None
        user.referral_code = None
        user.referred_by_id = None
        user.remnawave_uuid = None
        user.trojan_password = None
        user.vless_uuid = None
        user.ss_password = None
        for field in _OAUTH_FIELDS:
            setattr(user, field, None)
        user.updated_at = now


account_deletion_service = AccountDeletionService()
