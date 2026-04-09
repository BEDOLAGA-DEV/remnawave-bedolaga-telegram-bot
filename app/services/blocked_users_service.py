"""
Сервис для проверки пользователей, заблокировавших бота.

Проверяет возможность отправки сообщений пользователям и позволяет
очистить БД и панель Remnawave от неактивных пользователей.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import (
    AdvertisingCampaignRegistration,
    ButtonClickLog,
    CabinetRefreshToken,
    CloudPaymentsPayment,
    ContestAttempt,
    CryptoBotPayment,
    DiscountOffer,
    FreekassaPayment,
    HeleketPayment,
    KassaAiPayment,
    MulenPayPayment,
    Pal24Payment,
    PlategaPayment,
    PollResponse,
    PromoCodeUse,
    ReferralContestEvent,
    ReferralEarning,
    SentNotification,
    Subscription,
    SubscriptionConversion,
    SubscriptionEvent,
    SubscriptionServer,
    Ticket,
    TicketMessage,
    TicketNotification,
    Transaction,
    User,
    UserPromoGroup,
    UserStatus,
    WataPayment,
    WheelSpin,
    WithdrawalRequest,
    YooKassaPayment,
)
from app.services.remnawave_service import RemnaWaveService


logger = structlog.get_logger(__name__)


class BlockCheckStatus(Enum):
    """Статус проверки блокировки пользователя."""

    BLOCKED = 'blocked'
    ACTIVE = 'active'
    NO_TELEGRAM_ID = 'no_telegram_id'
    ERROR = 'error'


class BlockedUserAction(Enum):
    """Действия над заблокированными пользователями."""

    DELETE_FROM_DB = 'delete_from_db'
    DELETE_FROM_REMNAWAVE = 'delete_from_remnawave'
    DELETE_BOTH = 'delete_both'
    MARK_AS_BLOCKED = 'mark_as_blocked'


@dataclass
class BlockCheckResult:
    """Результат проверки одного пользователя."""

    user_id: int
    telegram_id: int | None
    username: str | None
    full_name: str
    status: BlockCheckStatus
    error_message: str | None = None
    remnawave_uuid: str | None = None
    remnawave_uuids: list[str] = field(default_factory=list)


@dataclass
class BlockedUsersScanResult:
    """Результат сканирования пользователей на блокировку."""

    total_checked: int = 0
    blocked_users: list[BlockCheckResult] = field(default_factory=list)
    active_users: int = 0
    errors: int = 0
    skipped_no_telegram: int = 0
    scan_duration_seconds: float = 0.0

    @property
    def blocked_count(self) -> int:
        return len(self.blocked_users)


@dataclass
class CleanupResult:
    """Результат очистки заблокированных пользователей."""

    deleted_from_db: int = 0
    deleted_from_remnawave: int = 0
    marked_as_blocked: int = 0
    errors: list[str] = field(default_factory=list)


class BlockedUsersService:
    """Сервис проверки и очистки заблокированных пользователей."""

    # Задержка между проверками для избежания rate limit
    CHECK_DELAY_SECONDS: float = 0.05
    # Максимальное количество параллельных проверок
    MAX_CONCURRENT_CHECKS: int = 10
    # Задержка между API запросами к Remnawave (rate limit protection)
    API_DELAY_SECONDS: float = 0.15

    def __init__(self, bot: Bot):
        self.bot = bot
        self.remnawave_service = RemnaWaveService()

    async def check_user_blocked(self, telegram_id: int) -> BlockCheckStatus:
        """
        Проверяет, заблокировал ли пользователь бота.

        Отправляет ChatAction.TYPING - это не создает видимого сообщения,
        но позволяет определить блокировку.
        """
        try:
            await self.bot.send_chat_action(chat_id=telegram_id, action='typing')
            return BlockCheckStatus.ACTIVE
        except TelegramForbiddenError:
            # Пользователь заблокировал бота
            return BlockCheckStatus.BLOCKED
        except TelegramBadRequest as e:
            error_lower = str(e).lower()
            if 'chat not found' in error_lower or 'user not found' in error_lower:
                # Пользователь удалил аккаунт или никогда не начинал диалог
                return BlockCheckStatus.BLOCKED
            logger.warning('TelegramBadRequest при проверке', telegram_id=telegram_id, error=e)
            return BlockCheckStatus.ERROR
        except TelegramAPIError as e:
            logger.warning('TelegramAPIError при проверке', telegram_id=telegram_id, error=e)
            return BlockCheckStatus.ERROR
        except Exception as e:
            logger.error('Неожиданная ошибка при проверке', telegram_id=telegram_id, error=e)
            return BlockCheckStatus.ERROR

    async def _check_single_user(self, user: User) -> BlockCheckResult:
        """Проверяет одного пользователя."""
        sub_uuids = [s.remnawave_uuid for s in (getattr(user, 'subscriptions', None) or []) if s.remnawave_uuid]
        remnawave_uuids = sub_uuids or ([user.remnawave_uuid] if user.remnawave_uuid else [])

        if not user.telegram_id:
            return BlockCheckResult(
                user_id=user.id,
                telegram_id=None,
                username=user.username,
                full_name=user.full_name,
                status=BlockCheckStatus.NO_TELEGRAM_ID,
                remnawave_uuid=user.remnawave_uuid,
                remnawave_uuids=remnawave_uuids,
            )

        status = await self.check_user_blocked(user.telegram_id)

        return BlockCheckResult(
            user_id=user.id,
            telegram_id=user.telegram_id,
            username=user.username,
            full_name=user.full_name,
            status=status,
            remnawave_uuid=user.remnawave_uuid,
            remnawave_uuids=remnawave_uuids,
        )

    async def scan_all_users(
        self,
        db: AsyncSession,
        *,
        only_active: bool = True,
        batch_size: int = 100,
        progress_callback: Callable | None = None,
    ) -> BlockedUsersScanResult:
        """
        Сканирует всех пользователей на предмет блокировки бота.

        Args:
            db: Сессия БД
            only_active: Проверять только активных пользователей
            batch_size: Размер батча для загрузки из БД
            progress_callback: Callback для отчета о прогрессе (checked, total)

        Returns:
            Результат сканирования
        """
        start_time = datetime.now(tz=UTC)
        result = BlockedUsersScanResult()

        # Формируем запрос
        query = select(User).options(selectinload(User.subscriptions).selectinload(Subscription.tariff))
        if only_active:
            query = query.where(User.status == UserStatus.ACTIVE.value)
        query = query.where(User.telegram_id.isnot(None))

        # Получаем всех пользователей
        users_result = await db.execute(query)
        all_users = users_result.scalars().all()
        total_users = len(all_users)

        logger.info('Начинаем проверку пользователей на блокировку бота', total_users=total_users)

        # Проверяем пользователей батчами с ограничением параллелизма
        semaphore = asyncio.Semaphore(self.MAX_CONCURRENT_CHECKS)

        async def check_with_semaphore(user: User) -> BlockCheckResult:
            async with semaphore:
                check_result = await self._check_single_user(user)
                await asyncio.sleep(self.CHECK_DELAY_SECONDS)
                return check_result

        checked = 0
        for i in range(0, total_users, batch_size):
            batch = all_users[i : i + batch_size]
            tasks = [check_with_semaphore(user) for user in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for check_result in batch_results:
                if isinstance(check_result, Exception):
                    result.errors += 1
                    logger.error('Ошибка при проверке пользователя', check_result=check_result)
                    continue

                result.total_checked += 1

                if check_result.status == BlockCheckStatus.BLOCKED:
                    result.blocked_users.append(check_result)
                elif check_result.status == BlockCheckStatus.ACTIVE:
                    result.active_users += 1
                elif check_result.status == BlockCheckStatus.NO_TELEGRAM_ID:
                    result.skipped_no_telegram += 1
                else:
                    result.errors += 1

            checked += len(batch)
            if progress_callback:
                await progress_callback(checked, total_users)

        result.scan_duration_seconds = (datetime.now(tz=UTC) - start_time).total_seconds()

        logger.info(
            'Сканирование завершено: заблокированных из проверенных за с',
            blocked_count=result.blocked_count,
            total_checked=result.total_checked,
            scan_duration_seconds=round(result.scan_duration_seconds, 1),
        )

        return result

    async def delete_user_from_remnawave(self, remnawave_uuid: str) -> bool:
        """Удаляет пользователя из панели Remnawave."""
        if not remnawave_uuid:
            return False

        try:
            if not self.remnawave_service.is_configured:
                logger.warning('Remnawave API не настроен')
                return False

            async with self.remnawave_service.get_api_client() as api:
                await api.delete_user(remnawave_uuid)
                logger.info('Удален пользователь из Remnawave', remnawave_uuid=remnawave_uuid)
                return True
        except Exception as e:
            error_msg = str(e).lower()
            if 'not found' in error_msg or '404' in error_msg:
                logger.info('Пользователь уже удален из Remnawave', remnawave_uuid=remnawave_uuid)
                return True
            logger.error('Ошибка удаления из Remnawave', remnawave_uuid=remnawave_uuid, error=e)
            return False

    async def delete_user_from_db(self, db: AsyncSession, user_id: int) -> bool:
        """
        Полностью удаляет пользователя из БД со всеми связанными данными.
        """
        try:
            # Получаем пользователя
            user_result = await db.execute(
                select(User)
                .options(selectinload(User.subscriptions).selectinload(Subscription.tariff))
                .where(User.id == user_id)
            )
            user = user_result.scalar_one_or_none()

            if not user:
                logger.warning('Пользователь не найден в БД', user_id=user_id)
                return False

            user_display = user.telegram_id or user.email or f'#{user.id}'

            # Удаляем связанные записи (порядок важен из-за foreign keys)

            # 1. Платежные системы (до транзакций, т.к. ссылаются на них)
            await db.execute(delete(YooKassaPayment).where(YooKassaPayment.user_id == user.id))
            await db.execute(delete(CryptoBotPayment).where(CryptoBotPayment.user_id == user.id))
            await db.execute(delete(HeleketPayment).where(HeleketPayment.user_id == user.id))
            await db.execute(delete(MulenPayPayment).where(MulenPayPayment.user_id == user.id))
            await db.execute(delete(Pal24Payment).where(Pal24Payment.user_id == user.id))
            await db.execute(delete(WataPayment).where(WataPayment.user_id == user.id))
            await db.execute(delete(PlategaPayment).where(PlategaPayment.user_id == user.id))
            await db.execute(delete(CloudPaymentsPayment).where(CloudPaymentsPayment.user_id == user.id))
            await db.execute(delete(FreekassaPayment).where(FreekassaPayment.user_id == user.id))
            await db.execute(delete(KassaAiPayment).where(KassaAiPayment.user_id == user.id))

            # 2. Транзакции (после платежей)
            await db.execute(delete(Transaction).where(Transaction.user_id == user.id))

            # 3. Подписки — cleanup ALL subscriptions' servers, then delete all subscriptions
            for sub in getattr(user, 'subscriptions', None) or []:
                await db.execute(delete(SubscriptionServer).where(SubscriptionServer.subscription_id == sub.id))
            await db.execute(delete(Subscription).where(Subscription.user_id == user.id))
            await db.execute(delete(SubscriptionConversion).where(SubscriptionConversion.user_id == user.id))
            await db.execute(delete(SubscriptionEvent).where(SubscriptionEvent.user_id == user.id))

            # 4. Тикеты (сначала зависимые)
            await db.execute(delete(TicketNotification).where(TicketNotification.user_id == user.id))
            await db.execute(delete(TicketMessage).where(TicketMessage.user_id == user.id))
            await db.execute(delete(Ticket).where(Ticket.user_id == user.id))

            # 5. Остальные связи
            await db.execute(delete(ReferralEarning).where(ReferralEarning.user_id == user.id))
            await db.execute(delete(ReferralEarning).where(ReferralEarning.referral_id == user.id))
            await db.execute(delete(WithdrawalRequest).where(WithdrawalRequest.user_id == user.id))
            await db.execute(delete(PromoCodeUse).where(PromoCodeUse.user_id == user.id))
            await db.execute(delete(DiscountOffer).where(DiscountOffer.user_id == user.id))
            await db.execute(delete(SentNotification).where(SentNotification.user_id == user.id))
            await db.execute(delete(PollResponse).where(PollResponse.user_id == user.id))
            await db.execute(delete(ContestAttempt).where(ContestAttempt.user_id == user.id))
            await db.execute(delete(ReferralContestEvent).where(ReferralContestEvent.referrer_id == user.id))
            await db.execute(delete(ReferralContestEvent).where(ReferralContestEvent.referral_id == user.id))
            await db.execute(
                delete(AdvertisingCampaignRegistration).where(AdvertisingCampaignRegistration.user_id == user.id)
            )
            await db.execute(delete(UserPromoGroup).where(UserPromoGroup.user_id == user.id))
            await db.execute(delete(CabinetRefreshToken).where(CabinetRefreshToken.user_id == user.id))
            await db.execute(delete(ButtonClickLog).where(ButtonClickLog.user_id == user.id))
            await db.execute(delete(WheelSpin).where(WheelSpin.user_id == user.id))

            # Обнуляем referred_by_id у рефералов этого пользователя
            referrals_query = select(User).where(User.referred_by_id == user.id)
            referrals_result = await db.execute(referrals_query)
            for referral in referrals_result.scalars().all():
                referral.referred_by_id = None

            # Удаляем пользователя
            await db.delete(user)
            await db.commit()

            logger.info('Пользователь полностью удален из БД', user_display=user_display)
            return True

        except Exception as e:
            logger.error('Ошибка удаления пользователя из БД', user_id=user_id, error=e)
            await db.rollback()
            return False

    async def mark_user_as_blocked(self, db: AsyncSession, user_id: int) -> bool:
        """Помечает пользователя как заблокированного в БД."""
        try:
            user_result = await db.execute(select(User).where(User.id == user_id))
            user = user_result.scalar_one_or_none()

            if not user:
                return False

            user.status = UserStatus.BLOCKED.value
            user.updated_at = datetime.now(tz=UTC)
            await db.commit()

            logger.info('Пользователь помечен как заблокированный', telegram_id=user.telegram_id or user.id)
            return True

        except Exception as e:
            logger.error('Ошибка пометки пользователя', user_id=user_id, error=e)
            await db.rollback()
            return False

    async def cleanup_blocked_users(
        self,
        db: AsyncSession,
        blocked_users: list[BlockCheckResult],
        action: BlockedUserAction,
        *,
        progress_callback: Callable | None = None,
    ) -> CleanupResult:
        """
        Выполняет очистку заблокированных пользователей.

        Args:
            db: Сессия БД
            blocked_users: Список заблокированных пользователей
            action: Действие для выполнения
            progress_callback: Callback для отчета о прогрессе

        Returns:
            Результат очистки
        """
        result = CleanupResult()
        total = len(blocked_users)

        for i, user_result in enumerate(blocked_users):
            try:
                if action in (BlockedUserAction.DELETE_FROM_REMNAWAVE, BlockedUserAction.DELETE_BOTH):
                    uuids_to_delete = user_result.remnawave_uuids or (
                        [user_result.remnawave_uuid] if user_result.remnawave_uuid else []
                    )
                    for rw_uuid in uuids_to_delete:
                        success = await self.delete_user_from_remnawave(rw_uuid)
                        if success:
                            result.deleted_from_remnawave += 1
                        else:
                            result.errors.append(
                                f'Ошибка удаления {user_result.telegram_id} (uuid={rw_uuid}) из Remnawave'
                            )
                        # Задержка для избежания rate limit
                        await asyncio.sleep(self.API_DELAY_SECONDS)

                if action in (BlockedUserAction.DELETE_FROM_DB, BlockedUserAction.DELETE_BOTH):
                    success = await self.delete_user_from_db(db, user_result.user_id)
                    if success:
                        result.deleted_from_db += 1
                    else:
                        result.errors.append(f'Ошибка удаления {user_result.telegram_id} из БД')

                if action == BlockedUserAction.MARK_AS_BLOCKED:
                    success = await self.mark_user_as_blocked(db, user_result.user_id)
                    if success:
                        result.marked_as_blocked += 1
                    else:
                        result.errors.append(f'Ошибка пометки {user_result.telegram_id}')

                if progress_callback:
                    await progress_callback(i + 1, total)

            except Exception as e:
                error_msg = f'Ошибка обработки {user_result.telegram_id}: {e}'
                result.errors.append(error_msg)
                logger.error(error_msg)

        return result
