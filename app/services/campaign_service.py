from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.campaign import (
    get_campaign_by_start_parameter,
    get_campaign_registration_by_user,
    record_campaign_registration,
)
from app.database.crud.subscription import (
    create_paid_subscription,
    get_subscription_by_user_id,
)
from app.database.crud.tariff import get_tariff_by_id
from app.database.crud.user import add_user_balance
from app.database.models import AdvertisingCampaign, User
from app.services.subscription_service import SubscriptionService


logger = structlog.get_logger(__name__)


def _format_user_log(user: User) -> str:
    """Format user identifier for logging (supports email-only users)."""
    if user.telegram_id:
        return str(user.telegram_id)
    if user.email:
        return f'{user.id} ({user.email})'
    return f'#{user.id}'


@dataclass
class CampaignBonusResult:
    success: bool
    bonus_type: str | None = None
    balance_kopeks: int = 0
    subscription_days: int | None = None
    subscription_traffic_gb: int | None = None
    subscription_device_limit: int | None = None
    subscription_squads: list[str] | None = None
    # Поля для tariff
    tariff_id: int | None = None
    tariff_name: str | None = None
    tariff_duration_days: int | None = None
    # Имя кампании, к которой привязали юзера. Заполняется attribute_campaign,
    # чтобы caller не ходил за кампанией второй раз ради текста уведомления.
    campaign_name: str | None = None
    # True если запись в advertising_campaign_registrations была создана этим вызовом
    # (а не вернулась как existing). Используется caller'ом, чтобы понять, нужно ли
    # слать админу уведомление о регистрации (один раз на первую успешную).
    is_new_registration: bool = False


class AdvertisingCampaignService:
    def __init__(self) -> None:
        self.subscription_service = SubscriptionService()

    async def attribute_campaign(
        self,
        db: AsyncSession,
        user: User,
        campaign_slug: str | None,
    ) -> CampaignBonusResult | None:
        """Bind a user to an advertising campaign and grant its bonus.

        Shared by every entry point that can produce a campaign-attributed
        user: cabinet auth, the bot's /start handler and guest purchases made
        on a landing page. Returns ``None`` when there is nothing to attribute
        — unknown or inactive campaign, the user is the campaign's own partner,
        the user already belongs to some campaign, or the bonus did not apply.

        Never raises: attribution is a side effect and must not take down the
        flow that called it.
        """
        if not campaign_slug:
            return None

        try:
            campaign = await get_campaign_by_start_parameter(db, campaign_slug, only_active=True)
            if not campaign:
                return None

            # Партнёр не может привести сам себя по собственной ссылке.
            if campaign.partner_user_id and campaign.partner_user_id == user.id:
                logger.debug(
                    'Skipping campaign attribution: user is the campaign partner',
                    user_id=user.id,
                    campaign_id=campaign.id,
                )
                return None

            # Блокируем строку юзера: два параллельных входа иначе успеют
            # выдать бонус дважды до того, как сработает UNIQUE в регистрации.
            await db.execute(select(User).where(User.id == user.id).with_for_update())

            existing = await get_campaign_registration_by_user(db, user.id)
            if existing:
                logger.debug('User already has campaign registration', user_id=user.id)
                return None

            await self._link_partner_referral(db, user, campaign)

            result = await self.apply_campaign_bonus(db, user, campaign)
            if not result.success:
                return None

            result.campaign_name = campaign.name
            result.bonus_type = result.bonus_type or campaign.bonus_type

            # Обновляем юзера, чтобы вызывающий увидел начисленный баланс.
            await db.refresh(user)
            return result
        except Exception:
            logger.exception(
                'Failed to attribute campaign',
                user_id=getattr(user, 'id', None),
                campaign_slug=campaign_slug,
            )
            try:
                await db.rollback()
                # Перечитываем юзера, чтобы сессия осталась пригодной для caller'а.
                await db.refresh(user)
            except Exception:
                logger.exception('Failed to rollback after campaign attribution error')
            return None

    async def _link_partner_referral(
        self,
        db: AsyncSession,
        user: User,
        campaign: AdvertisingCampaign,
    ) -> None:
        """Attach the campaign's partner as the user's referrer, if unset."""
        if not campaign.partner_user_id or user.referred_by_id:
            return

        user.referred_by_id = campaign.partner_user_id
        await db.flush()
        try:
            from app.bot_factory import create_bot
            from app.services.referral_service import process_referral_registration

            async with create_bot() as bot:
                await process_referral_registration(db, user.id, campaign.partner_user_id, bot=bot)
            logger.info(
                'Referral set from campaign partner',
                user_id=user.id,
                partner_user_id=campaign.partner_user_id,
                campaign_id=campaign.id,
            )
        except Exception as e:
            logger.error('Failed to process referral from campaign partner', error=e)

    async def apply_campaign_bonus(
        self,
        db: AsyncSession,
        user: User,
        campaign: AdvertisingCampaign,
    ) -> CampaignBonusResult:
        # Пользователь мог прийти с протухшими (expired) атрибутами: любой
        # rollback выше по флоу регистрации (реферал, промокод, phantom-merge)
        # экспайрит инстансы даже при expire_on_commit=False, а последующий
        # sync-доступ к user.id здесь дёргает ленивую подгрузку вне greenlet →
        # MissingGreenlet и бонус кампании не начисляется (прод-репорт
        # 2026-07-13, регистрация по рекламной кампании). Перечитываем
        # атрибуты асинхронно ДО первого sync-доступа.
        try:
            await db.refresh(user)
        except Exception as refresh_error:
            # user.id здесь читаем через __dict__: атрибут может быть expired,
            # и обычный доступ сам бы дёрнул ленивую подгрузку → повторный
            # MissingGreenlet уже внутри обработчика.
            logger.warning(
                '⚠️ Не удалось освежить пользователя перед бонусом кампании',
                user_id=user.__dict__.get('id'),
                campaign_id=campaign.id,
                error=refresh_error,
            )

        if not campaign.is_active:
            logger.warning('⚠️ Попытка выдать бонус по неактивной кампании', campaign_id=campaign.id)
            return CampaignBonusResult(success=False)

        # Prevent partner from being attributed to their own campaign
        if campaign.partner_user_id and campaign.partner_user_id == user.id:
            logger.info(
                'Skipping campaign bonus: user is the campaign partner',
                user_id=user.id,
                campaign_id=campaign.id,
            )
            return CampaignBonusResult(success=False)

        if campaign.is_balance_bonus:
            return await self._apply_balance_bonus(db, user, campaign)

        if campaign.is_subscription_bonus:
            return await self._apply_subscription_bonus(db, user, campaign)

        if campaign.is_none_bonus:
            return await self._apply_none_bonus(db, user, campaign)

        if campaign.is_tariff_bonus:
            return await self._apply_tariff_bonus(db, user, campaign)

        logger.error('❌ Неизвестный тип бонуса кампании', bonus_type=campaign.bonus_type)
        return CampaignBonusResult(success=False)

    async def _apply_balance_bonus(
        self,
        db: AsyncSession,
        user: User,
        campaign: AdvertisingCampaign,
    ) -> CampaignBonusResult:
        amount = campaign.balance_bonus_kopeks or 0
        if amount <= 0:
            logger.info('ℹ️ Кампания не имеет бонуса на баланс', campaign_id=campaign.id)
            return CampaignBonusResult(success=False)

        # Регистрируем ДО начисления баланса, чтобы при повторном /start (created=False)
        # не накрутить бонус второй раз. UNIQUE constraint в record_campaign_registration
        # + savepoint защищают и от concurrent race conditions.
        _, created = await record_campaign_registration(
            db,
            campaign_id=campaign.id,
            user_id=user.id,
            bonus_type='balance',
            balance_bonus_kopeks=amount,
        )

        if not created:
            logger.info(
                'ℹ️ Балансный бонус уже был начислен по этой кампании ранее, пропускаем',
                format_user_log=_format_user_log(user),
                campaign_id=campaign.id,
            )
            return CampaignBonusResult(
                success=True,
                bonus_type='balance',
                balance_kopeks=amount,
                is_new_registration=False,
            )

        description = f"Бонус за регистрацию по кампании '{campaign.name}'"
        success = await add_user_balance(
            db,
            user,
            amount,
            description=description,
        )

        if not success:
            # Маркер регистрации остался — баланс не начислился. Это лучше, чем
            # начислить деньги без записи в БД (откатить запись теперь нельзя).
            logger.error(
                '❌ Регистрация записана, но баланс не начислился',
                format_user_log=_format_user_log(user),
                campaign_id=campaign.id,
                amount_kopeks=amount,
            )
            return CampaignBonusResult(success=False)

        logger.info(
            '💰 Пользователю начислен бонус ₽ по кампании',
            format_user_log=_format_user_log(user),
            amount=amount / 100,
            campaign_id=campaign.id,
        )

        return CampaignBonusResult(
            success=True,
            bonus_type='balance',
            balance_kopeks=amount,
            is_new_registration=created,
        )

    async def _apply_subscription_bonus(
        self,
        db: AsyncSession,
        user: User,
        campaign: AdvertisingCampaign,
    ) -> CampaignBonusResult:
        if settings.is_multi_tariff_enabled():
            from app.database.crud.subscription import get_active_subscriptions_by_user_id

            active_subs = await get_active_subscriptions_by_user_id(db, user.id)
            if active_subs:
                # Multi-tariff: extend the best existing subscription instead of blocking
                _non_daily = [s for s in active_subs if not getattr(s, 'is_daily_tariff', False)]
                _pool = _non_daily or active_subs
                existing_subscription = max(_pool, key=lambda s: s.days_left)
            else:
                existing_subscription = None
        else:
            existing_subscription = await get_subscription_by_user_id(db, user.id)
            if existing_subscription:
                logger.warning(
                    '⚠️ У пользователя уже есть подписка, бонус кампании пропущен',
                    format_user_log=_format_user_log(user),
                    campaign_id=campaign.id,
                )
                return CampaignBonusResult(success=False)

        duration_days = campaign.subscription_duration_days or 0
        if duration_days <= 0:
            logger.info('ℹ️ Кампания не содержит корректной длительности подписки', campaign_id=campaign.id)
            return CampaignBonusResult(success=False)

        traffic_limit = campaign.subscription_traffic_gb
        device_limit = campaign.subscription_device_limit
        if device_limit is None:
            device_limit = settings.DEFAULT_DEVICE_LIMIT
        try:
            from app.database.crud.server_squad import get_effective_tariff_squad_uuids

            squads = await get_effective_tariff_squad_uuids(db, campaign.subscription_squads)
        except Exception as error:
            logger.error('Не удалось подобрать сквады для кампании', campaign_id=campaign.id, error=error)
            squads = list(campaign.subscription_squads or [])

        if existing_subscription:
            # Multi-tariff: extend the best existing subscription
            from app.database.crud.subscription import extend_subscription

            await extend_subscription(db, existing_subscription, duration_days)
            try:
                await self.subscription_service.update_remnawave_user(db, existing_subscription)
            except Exception as error:
                logger.error(
                    '❌ Ошибка синхронизации RemnaWave при продлении кампании', campaign_id=campaign.id, error=error
                )

            logger.info(
                '🎁 Подписка пользователя продлена по кампании на дней',
                format_user_log=_format_user_log(user),
                campaign_id=campaign.id,
                duration_days=duration_days,
                subscription_id=existing_subscription.id,
            )
        else:
            new_subscription = await create_paid_subscription(
                db=db,
                user_id=user.id,
                duration_days=duration_days,
                traffic_limit_gb=traffic_limit or 0,
                device_limit=device_limit,
                connected_squads=squads,
                update_server_counters=True,
                is_trial=True,
            )

            try:
                await self.subscription_service.create_remnawave_user(db, new_subscription)
            except Exception as error:
                logger.error('❌ Ошибка синхронизации RemnaWave для кампании', campaign_id=campaign.id, error=error)

            logger.info(
                '🎁 Пользователю выдана подписка по кампании на дней',
                format_user_log=_format_user_log(user),
                campaign_id=campaign.id,
                duration_days=duration_days,
            )

        _, created = await record_campaign_registration(
            db,
            campaign_id=campaign.id,
            user_id=user.id,
            bonus_type='subscription',
            subscription_duration_days=duration_days,
        )

        return CampaignBonusResult(
            success=True,
            bonus_type='subscription',
            subscription_days=duration_days,
            subscription_traffic_gb=traffic_limit or 0,
            subscription_device_limit=device_limit,
            subscription_squads=squads,
            is_new_registration=created,
        )

    async def _apply_none_bonus(
        self,
        db: AsyncSession,
        user: User,
        campaign: AdvertisingCampaign,
    ) -> CampaignBonusResult:
        """Обычная ссылка без награды - только регистрация для отслеживания."""
        _, created = await record_campaign_registration(
            db,
            campaign_id=campaign.id,
            user_id=user.id,
            bonus_type='none',
        )

        logger.info(
            '📊 Пользователь зарегистрирован по ссылке кампании (без награды)',
            format_user_log=_format_user_log(user),
            campaign_id=campaign.id,
        )

        return CampaignBonusResult(
            success=True,
            bonus_type='none',
            is_new_registration=created,
        )

    async def _apply_tariff_bonus(
        self,
        db: AsyncSession,
        user: User,
        campaign: AdvertisingCampaign,
    ) -> CampaignBonusResult:
        """Выдача тарифа на определённое время."""
        existing_subscription = None
        if settings.is_multi_tariff_enabled():
            from app.database.crud.subscription import get_active_subscriptions_by_user_id

            active_subs = await get_active_subscriptions_by_user_id(db, user.id)
            if active_subs and campaign.tariff_id:
                # Multi-tariff: only check for THIS specific tariff
                same_tariff_subs = [s for s in active_subs if s.tariff_id == campaign.tariff_id]
                if same_tariff_subs:
                    existing_subscription = max(same_tariff_subs, key=lambda s: s.days_left)
                # If no sub for this tariff, existing_subscription stays None -> create new
        else:
            existing_subscription = await get_subscription_by_user_id(db, user.id)
            if existing_subscription:
                logger.warning(
                    '⚠️ У пользователя уже есть подписка, бонус тарифа кампании пропущен',
                    format_user_log=_format_user_log(user),
                    campaign_id=campaign.id,
                )
                return CampaignBonusResult(success=False)

        if not campaign.tariff_id:
            logger.error('❌ Кампания не имеет указанного тарифа для выдачи', campaign_id=campaign.id)
            return CampaignBonusResult(success=False)

        duration_days = campaign.tariff_duration_days or 0
        if duration_days <= 0:
            logger.error('❌ Кампания не имеет указанной длительности тарифа', campaign_id=campaign.id)
            return CampaignBonusResult(success=False)

        # Получаем тариф для извлечения параметров
        tariff = await get_tariff_by_id(db, campaign.tariff_id)
        if not tariff:
            logger.error('❌ Тариф не найден для кампании', tariff_id=campaign.tariff_id, campaign_id=campaign.id)
            return CampaignBonusResult(success=False)

        if not tariff.is_active:
            logger.warning('⚠️ Тариф неактивен, бонус кампании пропущен', tariff_id=tariff.id, campaign_id=campaign.id)
            return CampaignBonusResult(success=False)

        traffic_limit = tariff.traffic_limit_gb
        device_limit = tariff.device_limit
        try:
            from app.database.crud.server_squad import get_effective_tariff_squad_uuids

            squads = await get_effective_tariff_squad_uuids(db, tariff.allowed_squads)
        except Exception as error:
            logger.error('Не удалось подобрать сквады для тарифа кампании', campaign_id=campaign.id, error=error)
            squads = list(tariff.allowed_squads or [])

        if existing_subscription:
            # Multi-tariff: extend the existing subscription for this tariff
            from app.database.crud.subscription import extend_subscription

            await extend_subscription(db, existing_subscription, duration_days, tariff_id=tariff.id)
            try:
                await self.subscription_service.update_remnawave_user(db, existing_subscription)
            except Exception as error:
                logger.error(
                    '❌ Ошибка синхронизации RemnaWave при продлении тарифа кампании',
                    campaign_id=campaign.id,
                    error=error,
                )

            logger.info(
                '🎁 Подписка пользователя продлена по тарифу кампании на дней',
                format_user_log=_format_user_log(user),
                tariff_name=tariff.name,
                campaign_id=campaign.id,
                duration_days=duration_days,
                subscription_id=existing_subscription.id,
            )
        else:
            # Создаём подписку как платную (не trial) с привязкой к тарифу
            new_subscription = await create_paid_subscription(
                db=db,
                user_id=user.id,
                duration_days=duration_days,
                traffic_limit_gb=traffic_limit or 0,
                device_limit=device_limit,
                connected_squads=squads,
                update_server_counters=True,
                is_trial=False,
                tariff_id=tariff.id,
            )

            try:
                await self.subscription_service.create_remnawave_user(db, new_subscription)
            except Exception as error:
                logger.error(
                    '❌ Ошибка синхронизации RemnaWave для тарифа кампании', campaign_id=campaign.id, error=error
                )

            logger.info(
                '🎁 Пользователю выдан тариф по кампании на дней',
                format_user_log=_format_user_log(user),
                tariff_name=tariff.name,
                campaign_id=campaign.id,
                duration_days=duration_days,
            )

        _, created = await record_campaign_registration(
            db,
            campaign_id=campaign.id,
            user_id=user.id,
            bonus_type='tariff',
            tariff_id=tariff.id,
            tariff_duration_days=duration_days,
        )

        return CampaignBonusResult(
            success=True,
            bonus_type='tariff',
            tariff_id=tariff.id,
            tariff_name=tariff.name,
            tariff_duration_days=duration_days,
            subscription_traffic_gb=traffic_limit or 0,
            subscription_device_limit=device_limit,
            subscription_squads=squads,
            is_new_registration=created,
        )
