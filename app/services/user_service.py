from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from aiogram import Bot, types
from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.crud.promo_group import get_promo_group_by_id
from app.database.crud.subscription import get_subscription_by_user_id
from app.database.crud.transaction import get_user_transactions_count
from app.database.crud.user import (
    add_user_balance,
    get_inactive_users,
    get_referrals,
    get_user_by_id,
    get_users_count,
    get_users_list,
    get_users_spending_stats,
    get_users_statistics,
    subtract_user_balance,
    update_user,
)
from app.database.models import (
    AdvertisingCampaign,
    AdvertisingCampaignRegistration,
    BroadcastHistory,
    CloudPaymentsPayment,
    CryptoBotPayment,
    FreekassaPayment,
    HeleketPayment,
    KassaAiPayment,
    MulenPayPayment,
    Pal24Payment,
    PaymentMethod,
    PlategaPayment,
    PromoCode,
    PromoCodeUse,
    PromoGroup,
    ReferralEarning,
    SentNotification,
    Subscription,
    SubscriptionConversion,
    SubscriptionServer,
    Transaction,
    User,
    UserMessage,
    UserStatus,
    WataPayment,
    WelcomeText,
    YooKassaPayment,
)
from app.localization.texts import get_texts
from app.services.notification_delivery_service import (
    NotificationType,
    notification_delivery_service,
)


logger = structlog.get_logger(__name__)


@dataclass
class DeleteUserResult:
    """Результат удаления пользователя."""

    bot_deleted: bool = False
    panel_deleted: bool = False
    panel_error: str | None = None


class UserService:
    async def send_topup_success_to_user(
        self,
        bot: Bot,
        user: User,
        amount_kopeks: int,
        subscription: Subscription | None = None,
    ) -> bool:
        """
        Отправляет пользователю уведомление об успешном пополнении баланса.
        Если подписки нет - показывает БОЛЬШОЕ предупреждение что нужно активировать.
        Поддерживает как Telegram, так и email-only пользователей.
        """
        texts = get_texts(user.language)

        has_active_subscription = subscription is not None and subscription.status in {'active', 'trial'}

        if has_active_subscription:
            # У пользователя есть активная подписка - обычное сообщение
            message = (
                f'✅ <b>Баланс пополнен на {settings.format_price(amount_kopeks)}!</b>\n\n'
                f'💳 Текущий баланс: {settings.format_price(user.balance_kopeks)}\n\n'
                f'Спасибо за использование нашего сервиса! 🎉'
            )
            extend_callback = 'menu_subscription' if settings.is_multi_tariff_enabled() else 'subscription_extend'
            keyboard = types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('SUBSCRIPTION_EXTEND', '💎 Продлить подписку'),
                            callback_data=extend_callback,
                        )
                    ]
                ]
            )
        else:
            # НЕТ активной подписки - БОЛЬШОЕ ПРЕДУПРЕЖДЕНИЕ
            message = (
                f'✅ <b>Баланс пополнен на {settings.format_price(amount_kopeks)}!</b>\n\n'
                f'💳 Текущий баланс: {settings.format_price(user.balance_kopeks)}\n\n'
                f'{"─" * 25}\n\n'
                f'⚠️ <b>ВАЖНО!</b> ⚠️\n\n'
                f'🔴 <b>ПОДПИСКА НЕ АКТИВНА!</b>\n\n'
                f'Пополнение баланса НЕ активирует подписку автоматически!\n\n'
                f'👇 <b>Выберите действие:</b>'
            )
            extend_callback = 'menu_subscription' if settings.is_multi_tariff_enabled() else 'subscription_extend'
            keyboard = types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='🚀 АКТИВИРОВАТЬ ПОДПИСКУ', callback_data='subscription_buy')],
                    [types.InlineKeyboardButton(text='💎 ПРОДЛИТЬ ПОДПИСКУ', callback_data=extend_callback)],
                    [
                        types.InlineKeyboardButton(
                            text='📱 ДОБАВИТЬ УСТРОЙСТВА', callback_data='subscription_add_devices'
                        )
                    ],
                ]
            )

        # Use unified notification delivery service
        return await notification_delivery_service.notify_balance_topup(
            user=user,
            amount_kopeks=amount_kopeks,
            new_balance_kopeks=user.balance_kopeks,
            bot=bot,
            telegram_message=message,
            telegram_markup=keyboard,
        )

    async def _send_balance_notification(self, bot: Bot, user: User, amount_kopeks: int, admin_name: str) -> bool:
        """
        Отправляет уведомление пользователю о пополнении/списании баланса.
        Поддерживает как Telegram, так и email-only пользователей.
        """
        if amount_kopeks > 0:
            # Пополнение
            emoji = '💰'
            amount_text = f'+{settings.format_price(amount_kopeks)}'
            message = (
                f'{emoji} <b>Баланс пополнен!</b>\n\n'
                f'💵 <b>Сумма:</b> {amount_text}\n'
                f'💳 <b>Текущий баланс:</b> {settings.format_price(user.balance_kopeks)}\n\n'
                f'Спасибо за использование нашего сервиса! 🎉'
            )
        else:
            # Списание
            emoji = '💸'
            amount_text = f'-{settings.format_price(abs(amount_kopeks))}'
            message = (
                f'{emoji} <b>Средства списаны с баланса</b>\n\n'
                f'💵 <b>Сумма:</b> {amount_text}\n'
                f'💳 <b>Текущий баланс:</b> {settings.format_price(user.balance_kopeks)}\n\n'
                f'Если у вас есть вопросы, обратитесь в поддержку.'
            )

        keyboard_rows = []
        subs = getattr(user, 'subscriptions', None) or []
        has_extendable = any(sub.status in {'active', 'expired', 'trial'} for sub in subs)
        if has_extendable:
            extend_callback = 'menu_subscription' if settings.is_multi_tariff_enabled() else 'subscription_extend'
            keyboard_rows.append(
                [
                    types.InlineKeyboardButton(
                        text=get_texts(user.language).t('SUBSCRIPTION_EXTEND', '💎 Продлить подписку'),
                        callback_data=extend_callback,
                    )
                ]
            )

        reply_markup = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows) if keyboard_rows else None

        # Use unified notification delivery service
        context = {
            'amount_kopeks': amount_kopeks,
            'amount_rubles': amount_kopeks / 100,
            'new_balance_kopeks': user.balance_kopeks,
            'new_balance_rubles': user.balance_kopeks / 100,
            'formatted_amount': settings.format_price(amount_kopeks),
            'formatted_balance': settings.format_price(user.balance_kopeks),
            # No description - don't expose admin name to user
        }

        return await notification_delivery_service.send_notification(
            user=user,
            notification_type=NotificationType.BALANCE_CHANGE,
            context=context,
            bot=bot,
            telegram_message=message,
            telegram_markup=reply_markup,
        )

    async def get_user_profile(self, db: AsyncSession, user_id: int) -> dict[str, Any] | None:
        try:
            user = await get_user_by_id(db, user_id)
            if not user:
                return None

            if settings.is_multi_tariff_enabled():
                from app.database.crud.subscription import get_active_subscriptions_by_user_id

                active_subs = await get_active_subscriptions_by_user_id(db, user_id)
                if active_subs:
                    _non_daily = [s for s in active_subs if not getattr(s, 'is_daily_tariff', False)]
                    _pool = _non_daily or active_subs
                    subscription = max(_pool, key=lambda s: s.days_left)
                else:
                    subscription = None
            else:
                subscription = await get_subscription_by_user_id(db, user_id)
            transactions_count = await get_user_transactions_count(db, user_id)

            return {
                'user': user,
                'subscription': subscription,
                'transactions_count': transactions_count,
                'is_admin': settings.is_admin(user.telegram_id, user.email),
                'registration_days': (datetime.now(UTC) - user.created_at).days,
            }

        except Exception as e:
            logger.error('Ошибка получения профиля пользователя', user_id=user_id, error=e)
            return None

    async def search_users(self, db: AsyncSession, query: str, page: int = 1, limit: int = 20) -> dict[str, Any]:
        try:
            offset = (page - 1) * limit

            users = await get_users_list(db, offset=offset, limit=limit, search=query)
            total_count = await get_users_count(db, search=query)

            total_pages = (total_count + limit - 1) // limit

            return {
                'users': users,
                'current_page': page,
                'total_pages': total_pages,
                'total_count': total_count,
                'has_next': page < total_pages,
                'has_prev': page > 1,
            }

        except Exception as e:
            logger.error('Ошибка поиска пользователей', error=e)
            return {
                'users': [],
                'current_page': 1,
                'total_pages': 1,
                'total_count': 0,
                'has_next': False,
                'has_prev': False,
            }

    async def get_users_page(
        self,
        db: AsyncSession,
        page: int = 1,
        limit: int = 20,
        status: UserStatus | None = None,
        order_by_balance: bool = False,
        order_by_traffic: bool = False,
        order_by_last_activity: bool = False,
        order_by_total_spent: bool = False,
        order_by_purchase_count: bool = False,
    ) -> dict[str, Any]:
        try:
            offset = (page - 1) * limit

            users = await get_users_list(
                db,
                offset=offset,
                limit=limit,
                status=status,
                order_by_balance=order_by_balance,
                order_by_traffic=order_by_traffic,
                order_by_last_activity=order_by_last_activity,
                order_by_total_spent=order_by_total_spent,
                order_by_purchase_count=order_by_purchase_count,
            )
            total_count = await get_users_count(db, status=status)

            total_pages = (total_count + limit - 1) // limit

            return {
                'users': users,
                'current_page': page,
                'total_pages': total_pages,
                'total_count': total_count,
                'has_next': page < total_pages,
                'has_prev': page > 1,
            }

        except Exception as e:
            logger.error('Ошибка получения страницы пользователей', error=e)
            return {
                'users': [],
                'current_page': 1,
                'total_pages': 1,
                'total_count': 0,
                'has_next': False,
                'has_prev': False,
            }

    async def get_users_ready_to_renew(
        self,
        db: AsyncSession,
        min_balance_kopeks: int,
        page: int = 1,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Возвращает пользователей с истекшей подпиской и достаточным балансом."""
        try:
            offset = (page - 1) * limit
            now = datetime.now(UTC)

            base_filters = [
                User.balance_kopeks >= min_balance_kopeks,
                Subscription.end_date.isnot(None),
                Subscription.end_date <= now,
            ]

            query = (
                select(User)
                .options(selectinload(User.subscriptions).selectinload(Subscription.tariff))
                .join(Subscription, Subscription.user_id == User.id)
                .where(*base_filters)
                .order_by(User.balance_kopeks.desc(), Subscription.end_date.asc())
                .offset(offset)
                .limit(limit)
            )
            result = await db.execute(query)
            users = result.scalars().unique().all()

            count_query = (
                select(func.count(User.id)).join(Subscription, Subscription.user_id == User.id).where(*base_filters)
            )
            total_count = (await db.execute(count_query)).scalar() or 0
            total_pages = (total_count + limit - 1) // limit if total_count else 0

            return {
                'users': users,
                'current_page': page,
                'total_pages': total_pages,
                'total_count': total_count,
            }

        except Exception as e:
            logger.error('Ошибка получения пользователей для продления', error=e)
            return {
                'users': [],
                'current_page': 1,
                'total_pages': 1,
                'total_count': 0,
            }

    async def get_potential_customers(
        self,
        db: AsyncSession,
        min_balance_kopeks: int,
        page: int = 1,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Возвращает пользователей без активной подписки с достаточным балансом."""
        try:
            offset = (page - 1) * limit

            # Фильтры: нет активной подписки И баланс >= порога
            base_filters = [
                User.balance_kopeks >= min_balance_kopeks,
            ]

            # Subquery: user has at least one active/trial subscription
            active_sub_exists = exists().where(
                Subscription.user_id == User.id,
                Subscription.status.in_(['active', 'trial']),
            )

            # Основной запрос: пользователи БЕЗ активных подписок
            query = (
                select(User)
                .options(selectinload(User.subscriptions).selectinload(Subscription.tariff))
                .where(
                    *base_filters,
                    ~active_sub_exists,
                )
                .order_by(User.balance_kopeks.desc(), User.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
            result = await db.execute(query)
            users = result.scalars().unique().all()

            # Запрос для подсчета общего количества
            count_query = select(func.count(User.id)).where(
                *base_filters,
                ~active_sub_exists,
            )
            total_count = (await db.execute(count_query)).scalar() or 0
            total_pages = (total_count + limit - 1) // limit if total_count else 0

            return {
                'users': users,
                'current_page': page,
                'total_pages': total_pages,
                'total_count': total_count,
            }

        except Exception as e:
            logger.error('Ошибка получения потенциальных клиентов', error=e)
            return {
                'users': [],
                'current_page': 1,
                'total_pages': 1,
                'total_count': 0,
            }

    async def get_user_spending_stats_map(self, db: AsyncSession, user_ids: list[int]) -> dict[int, dict[str, int]]:
        try:
            return await get_users_spending_stats(db, user_ids)
        except Exception as e:
            logger.error('Ошибка получения статистики трат пользователей', error=e)
            return {}

    async def get_users_by_campaign_page(self, db: AsyncSession, page: int = 1, limit: int = 20) -> dict[str, Any]:
        try:
            offset = (page - 1) * limit

            campaign_ranked = select(
                AdvertisingCampaignRegistration.user_id.label('user_id'),
                AdvertisingCampaignRegistration.campaign_id.label('campaign_id'),
                AdvertisingCampaignRegistration.created_at.label('created_at'),
                func.row_number()
                .over(
                    partition_by=AdvertisingCampaignRegistration.user_id,
                    order_by=AdvertisingCampaignRegistration.created_at.desc(),
                )
                .label('rn'),
            ).cte('campaign_ranked')

            latest_campaign = (
                select(
                    campaign_ranked.c.user_id,
                    campaign_ranked.c.campaign_id,
                    campaign_ranked.c.created_at,
                )
                .where(campaign_ranked.c.rn == 1)
                .subquery()
            )

            query = (
                select(
                    User,
                    AdvertisingCampaign.name.label('campaign_name'),
                    latest_campaign.c.created_at,
                )
                .join(latest_campaign, latest_campaign.c.user_id == User.id)
                .join(
                    AdvertisingCampaign,
                    AdvertisingCampaign.id == latest_campaign.c.campaign_id,
                )
                .options(selectinload(User.subscriptions).selectinload(Subscription.tariff))
                .order_by(
                    AdvertisingCampaign.name.asc(),
                    latest_campaign.c.created_at.desc(),
                )
                .offset(offset)
                .limit(limit)
            )

            result = await db.execute(query)
            rows = result.all()

            users = [row[0] for row in rows]
            campaign_map = {
                row[0].id: {
                    'campaign_name': row[1],
                    'registered_at': row[2],
                }
                for row in rows
            }

            total_stmt = select(func.count()).select_from(latest_campaign)
            total_result = await db.execute(total_stmt)
            total_count = total_result.scalar() or 0
            total_pages = (total_count + limit - 1) // limit if total_count else 1

            return {
                'users': users,
                'campaigns': campaign_map,
                'current_page': page,
                'total_pages': total_pages,
                'total_count': total_count,
                'has_next': page < total_pages,
                'has_prev': page > 1,
            }

        except Exception as e:
            logger.error('Ошибка получения пользователей по кампаниям', error=e)
            return {
                'users': [],
                'campaigns': {},
                'current_page': 1,
                'total_pages': 1,
                'total_count': 0,
                'has_next': False,
                'has_prev': False,
            }

    async def update_user_balance(
        self,
        db: AsyncSession,
        user_id: int,
        amount_kopeks: int,
        description: str,
        admin_id: int,
        bot: Bot | None = None,
        admin_name: str | None = None,
    ) -> bool:
        try:
            user = await get_user_by_id(db, user_id)
            if not user:
                return False

            # Сохраняем старый баланс для уведомления

            if amount_kopeks > 0:
                await add_user_balance(
                    db, user, amount_kopeks, description=description, payment_method=PaymentMethod.MANUAL
                )
                logger.info(
                    'Админ пополнил баланс пользователя на ₽',
                    admin_id=admin_id,
                    user_id=user_id,
                    amount_kopeks=amount_kopeks / 100,
                )
                success = True
            else:
                success = await subtract_user_balance(
                    db,
                    user,
                    abs(amount_kopeks),
                    description,
                    create_transaction=True,
                    payment_method=PaymentMethod.MANUAL,
                )
                if success:
                    logger.info(
                        'Админ списал с баланса пользователя ₽',
                        admin_id=admin_id,
                        user_id=user_id,
                        value=abs(amount_kopeks) / 100,
                    )

            # Отправляем уведомление пользователю, если операция прошла успешно
            if success and bot:
                # Обновляем пользователя для получения нового баланса
                await db.refresh(user)

                # Получаем имя администратора
                if not admin_name:
                    admin_user = await get_user_by_id(db, admin_id)
                    admin_name = admin_user.full_name if admin_user else f'Админ #{admin_id}'

                # Отправляем уведомление (не блокируем операцию если не удалось отправить)
                await self._send_balance_notification(bot, user, amount_kopeks, admin_name)

            return success

        except Exception as e:
            logger.error('Ошибка изменения баланса пользователя', error=e)
            return False

    async def update_user_promo_group(
        self, db: AsyncSession, user_id: int, promo_group_id: int
    ) -> tuple[bool, User | None, PromoGroup | None, PromoGroup | None]:
        try:
            user = await get_user_by_id(db, user_id)
            if not user:
                return False, None, None, None

            old_group = user.promo_group

            promo_group = await get_promo_group_by_id(db, promo_group_id)
            if not promo_group:
                return False, None, None, old_group

            user.promo_group_id = promo_group.id
            user.promo_group = promo_group
            user.updated_at = datetime.now(UTC)

            await db.commit()
            await db.refresh(user)

            logger.info(
                "👥 Промогруппа пользователя обновлена на ''",
                telegram_id=user.telegram_id,
                promo_group_name=promo_group.name,
            )

            return True, user, promo_group, old_group

        except Exception as e:
            await db.rollback()
            logger.error('Ошибка обновления промогруппы пользователя', user_id=user_id, error=e)
            return False, None, None, None

    async def update_user_referrals(
        self,
        db: AsyncSession,
        user_id: int,
        referral_user_ids: list[int],
        admin_id: int,
    ) -> tuple[bool, dict[str, int]]:
        try:
            user = await get_user_by_id(db, user_id)
            if not user:
                return False, {'error': 'user_not_found'}

            unique_ids: list[int] = []
            for referral_id in referral_user_ids:
                if referral_id == user_id:
                    continue
                if referral_id not in unique_ids:
                    unique_ids.append(referral_id)

            current_referrals = await get_referrals(db, user_id)
            current_ids = {ref.id for ref in current_referrals}

            to_assign = unique_ids
            to_remove = [rid for rid in current_ids if rid not in unique_ids]
            to_add = [rid for rid in unique_ids if rid not in current_ids]

            if to_assign:
                await db.execute(update(User).where(User.id.in_(to_assign)).values(referred_by_id=user_id))

            if to_remove:
                await db.execute(update(User).where(User.id.in_(to_remove)).values(referred_by_id=None))

            await db.commit()

            logger.info(
                'Админ обновил рефералов пользователя : добавлено , удалено , всего',
                admin_id=admin_id,
                user_id=user_id,
                to_add_count=len(to_add),
                to_remove_count=len(to_remove),
                unique_ids_count=len(unique_ids),
            )

            return True, {
                'added': len(to_add),
                'removed': len(to_remove),
                'total': len(unique_ids),
            }

        except Exception as e:
            await db.rollback()
            logger.error('Ошибка обновления рефералов пользователя', user_id=user_id, e=e)
            return False, {'error': 'update_failed'}

    async def block_user(
        self, db: AsyncSession, user_id: int, admin_id: int, reason: str = 'Заблокирован администратором'
    ) -> bool:
        try:
            user = await get_user_by_id(db, user_id)
            if not user:
                return False

            from app.database.crud.subscription import deactivate_subscription, is_active_paid_subscription

            subs = getattr(user, 'subscriptions', None) or []
            has_active_paid = any(is_active_paid_subscription(sub) for sub in subs)

            if has_active_paid:
                logger.info(
                    '⏭️ Пропуск отключения RemnaWave и подписки: у пользователя активная оплаченная подписка',
                    user_id=user_id,
                    remnawave_uuid=user.remnawave_uuid,
                )
            else:
                from app.services.subscription_service import SubscriptionService

                subscription_service = SubscriptionService()

                if settings.is_multi_tariff_enabled():
                    # In multi-tariff mode, disable each subscription's panel user individually
                    for sub in subs:
                        panel_uuid = sub.remnawave_uuid
                        if panel_uuid:
                            try:
                                await subscription_service.disable_remnawave_user(panel_uuid)
                                logger.info(
                                    '✅ RemnaWave пользователь деактивирован при блокировке',
                                    remnawave_uuid=panel_uuid,
                                    subscription_id=sub.id,
                                )
                            except Exception as e:
                                logger.error(
                                    '❌ Ошибка деактивации RemnaWave при блокировке',
                                    error=e,
                                    subscription_id=sub.id,
                                )
                elif user.remnawave_uuid:
                    try:
                        await subscription_service.disable_remnawave_user(user.remnawave_uuid)
                        logger.info(
                            '✅ RemnaWave пользователь деактивирован при блокировке',
                            remnawave_uuid=user.remnawave_uuid,
                        )
                    except Exception as e:
                        logger.error('❌ Ошибка деактивации RemnaWave пользователя при блокировке', error=e)

                for sub in subs:
                    if sub.status in ['active', 'trial']:
                        await deactivate_subscription(db, sub)

            await update_user(db, user, status=UserStatus.BLOCKED.value)

            logger.info('Админ заблокировал пользователя', admin_id=admin_id, user_id=user_id, reason=reason)
            return True

        except Exception as e:
            logger.error('Ошибка блокировки пользователя', error=e)
            return False

    async def unblock_user(self, db: AsyncSession, user_id: int, admin_id: int) -> bool:
        try:
            user = await get_user_by_id(db, user_id)
            if not user:
                return False

            await update_user(db, user, status=UserStatus.ACTIVE.value)

            from app.database.models import SubscriptionStatus

            now = datetime.now(UTC)
            for sub in getattr(user, 'subscriptions', None) or []:
                if sub.end_date and sub.end_date > now and sub.status != SubscriptionStatus.ACTIVE.value:
                    sub.status = SubscriptionStatus.ACTIVE.value
                    try:
                        from app.services.subscription_service import SubscriptionService

                        subscription_service = SubscriptionService()
                        await subscription_service.update_remnawave_user(db, sub)
                        logger.info(
                            '✅ RemnaWave подписка восстановлена при разблокировке',
                            subscription_id=sub.id,
                            remnawave_uuid=sub.remnawave_uuid
                            if settings.is_multi_tariff_enabled()
                            else user.remnawave_uuid,
                        )
                    except Exception as e:
                        logger.error(
                            '❌ Ошибка восстановления RemnaWave подписки при разблокировке',
                            subscription_id=sub.id,
                            error=e,
                        )
            await db.commit()

            logger.info('Админ разблокировал пользователя', admin_id=admin_id, user_id=user_id)
            return True

        except Exception as e:
            logger.error('Ошибка разблокировки пользователя', error=e)
            return False

    async def delete_user_account(
        self, db: AsyncSession, user_id: int, admin_id: int, *, force_panel_delete: bool = False
    ) -> DeleteUserResult:
        """Полное удаление пользователя из бота и (опционально) из панели RemnaWave.

        force_panel_delete=True: пропускает проверку активной подписки и принудительно
        удаляет (не деактивирует) пользователя из панели RemnaWave. Используется
        при полном удалении через кабинет администратора.
        """
        result = DeleteUserResult()
        try:
            user = await get_user_by_id(db, user_id)
            if not user:
                logger.warning('Пользователь не найден для удаления', user_id=user_id)
                return result

            user_id_display = user.telegram_id or user.email or f'#{user.id}'
            logger.info(
                '🗑️ Начинаем полное удаление пользователя (ID: )', user_id=user_id, user_id_display=user_id_display
            )

            from app.config import settings
            from app.database.crud.subscription import is_active_paid_subscription

            # Collect all panel UUIDs to process
            subs = getattr(user, 'subscriptions', None) or []
            if settings.is_multi_tariff_enabled():
                panel_uuids = [sub.remnawave_uuid for sub in subs if sub.remnawave_uuid]
            else:
                panel_uuids = [user.remnawave_uuid] if user.remnawave_uuid else []

            if panel_uuids:
                if not force_panel_delete and any(is_active_paid_subscription(sub) for sub in subs):
                    logger.info(
                        '⏭️ Пропуск отключения RemnaWave при удалении: у пользователя активная оплаченная подписка',
                        user_id=user_id,
                    )
                else:
                    delete_mode = 'delete' if force_panel_delete else settings.get_remnawave_user_delete_mode()

                    # Помечаем ВСЕ UUID до цикла, чтобы webhook от первого удаления
                    # не пришёл раньше чем помечены остальные
                    if delete_mode == 'delete':
                        from app.services.remnawave_webhook_service import RemnaWaveWebhookService

                        RemnaWaveWebhookService.mark_intentional_panel_deletion(
                            panel_uuids=panel_uuids,
                            telegram_id=int(user.telegram_id) if user.telegram_id else None,
                        )

                    for panel_uuid in panel_uuids:
                        try:
                            from app.services.remnawave_service import RemnaWaveService

                            remnawave_service = RemnaWaveService()

                            if delete_mode == 'delete':
                                async with remnawave_service.get_api_client() as api:
                                    delete_success = await api.delete_user(panel_uuid)
                                    if delete_success:
                                        result.panel_deleted = True
                                        logger.info(
                                            '✅ RemnaWave пользователь удален из панели',
                                            remnawave_uuid=panel_uuid,
                                        )
                                    else:
                                        result.panel_error = 'Remnawave API вернул ошибку удаления'
                                        logger.warning(
                                            '⚠️ Не удалось удалить пользователя из панели Remnawave',
                                            remnawave_uuid=panel_uuid,
                                        )
                            else:
                                from app.services.subscription_service import SubscriptionService

                                subscription_service = SubscriptionService()
                                disabled = await subscription_service.disable_remnawave_user(panel_uuid)
                                result.panel_deleted = disabled
                                if disabled:
                                    logger.info(
                                        '✅ RemnaWave пользователь деактивирован',
                                        remnawave_uuid=panel_uuid,
                                        delete_mode=delete_mode,
                                    )
                                else:
                                    result.panel_error = 'disable_remnawave_user вернул False'
                                    logger.warning(
                                        '⚠️ Не удалось деактивировать пользователя в RemnaWave',
                                        remnawave_uuid=panel_uuid,
                                        delete_mode=delete_mode,
                                    )

                        except Exception as e:
                            result.panel_error = 'Ошибка обработки пользователя в Remnawave'
                            logger.warning(
                                '⚠️ Ошибка обработки пользователя в Remnawave',
                                delete_mode=delete_mode,
                                remnawave_uuid=panel_uuid,
                                error=e,
                            )
                            if delete_mode == 'delete':
                                try:
                                    from app.services.subscription_service import SubscriptionService

                                    subscription_service = SubscriptionService()
                                    disabled = await subscription_service.disable_remnawave_user(panel_uuid)
                                    if disabled:
                                        result.panel_deleted = True
                                        result.panel_error = 'Удаление не удалось, пользователь деактивирован'
                                        logger.info(
                                            '✅ RemnaWave пользователь деактивирован как fallback',
                                            remnawave_uuid=panel_uuid,
                                        )
                                except Exception as fallback_e:
                                    logger.error('❌ Ошибка деактивации RemnaWave как fallback', fallback_e=fallback_e)

            try:
                async with db.begin_nested():
                    sent_notifications_result = await db.execute(
                        select(SentNotification).where(SentNotification.user_id == user_id)
                    )
                    sent_notifications = sent_notifications_result.scalars().all()

                    if sent_notifications:
                        logger.info('🔄 Удаляем уведомлений', sent_notifications_count=len(sent_notifications))
                        await db.execute(delete(SentNotification).where(SentNotification.user_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления уведомлений', error=e)

            try:
                async with db.begin_nested():
                    user_messages_result = await db.execute(
                        update(UserMessage).where(UserMessage.created_by == user_id).values(created_by=None)
                    )
                    if user_messages_result.rowcount > 0:
                        logger.info('🔄 Обновлено пользовательских сообщений', rowcount=user_messages_result.rowcount)
                    await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка обновления пользовательских сообщений', error=e)

            try:
                async with db.begin_nested():
                    promocodes_result = await db.execute(
                        update(PromoCode).where(PromoCode.created_by == user_id).values(created_by=None)
                    )
                    if promocodes_result.rowcount > 0:
                        logger.info('🔄 Обновлено промокодов', rowcount=promocodes_result.rowcount)
                    await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка обновления промокодов', error=e)

            try:
                async with db.begin_nested():
                    welcome_texts_result = await db.execute(
                        update(WelcomeText).where(WelcomeText.created_by == user_id).values(created_by=None)
                    )
                    if welcome_texts_result.rowcount > 0:
                        logger.info('🔄 Обновлено приветственных текстов', rowcount=welcome_texts_result.rowcount)
                    await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка обновления приветственных текстов', error=e)

            try:
                async with db.begin_nested():
                    referrals_result = await db.execute(
                        update(User).where(User.referred_by_id == user_id).values(referred_by_id=None)
                    )
                    if referrals_result.rowcount > 0:
                        logger.info('🔗 Очищены реферальные ссылки у рефералов', rowcount=referrals_result.rowcount)
                    await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка очистки реферальных ссылок', error=e)

            try:
                async with db.begin_nested():
                    yookassa_result = await db.execute(
                        select(YooKassaPayment).where(YooKassaPayment.user_id == user_id)
                    )
                    yookassa_payments = yookassa_result.scalars().all()

                    if yookassa_payments:
                        logger.info('🔄 Удаляем YooKassa платежей', yookassa_payments_count=len(yookassa_payments))
                        await db.execute(
                            update(YooKassaPayment)
                            .where(YooKassaPayment.user_id == user_id)
                            .values(transaction_id=None)
                        )
                        await db.flush()
                        await db.execute(delete(YooKassaPayment).where(YooKassaPayment.user_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления YooKassa платежей', error=e)

            try:
                async with db.begin_nested():
                    cryptobot_result = await db.execute(
                        select(CryptoBotPayment).where(CryptoBotPayment.user_id == user_id)
                    )
                    cryptobot_payments = cryptobot_result.scalars().all()

                    if cryptobot_payments:
                        logger.info('🔄 Удаляем CryptoBot платежей', cryptobot_payments_count=len(cryptobot_payments))
                        await db.execute(
                            update(CryptoBotPayment)
                            .where(CryptoBotPayment.user_id == user_id)
                            .values(transaction_id=None)
                        )
                        await db.flush()
                        await db.execute(delete(CryptoBotPayment).where(CryptoBotPayment.user_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления CryptoBot платежей', error=e)

            try:
                async with db.begin_nested():
                    platega_result = await db.execute(select(PlategaPayment).where(PlategaPayment.user_id == user_id))
                    platega_payments = platega_result.scalars().all()

                    if platega_payments:
                        logger.info('🔄 Удаляем Platega платежей', platega_payments_count=len(platega_payments))
                        await db.execute(
                            update(PlategaPayment).where(PlategaPayment.user_id == user_id).values(transaction_id=None)
                        )
                        await db.flush()
                        await db.execute(delete(PlategaPayment).where(PlategaPayment.user_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления Platega платежей', error=e)

            try:
                async with db.begin_nested():
                    mulenpay_result = await db.execute(
                        select(MulenPayPayment).where(MulenPayPayment.user_id == user_id)
                    )
                    mulenpay_payments = mulenpay_result.scalars().all()

                    if mulenpay_payments:
                        mulenpay_name = settings.get_mulenpay_display_name()
                        logger.info(
                            '🔄 Удаляем платежей',
                            mulenpay_payments_count=len(mulenpay_payments),
                            mulenpay_name=mulenpay_name,
                        )
                        await db.execute(
                            update(MulenPayPayment)
                            .where(MulenPayPayment.user_id == user_id)
                            .values(transaction_id=None)
                        )
                        await db.flush()
                        await db.execute(delete(MulenPayPayment).where(MulenPayPayment.user_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error(
                    '❌ Ошибка удаления платежей',
                    get_mulenpay_display_name=settings.get_mulenpay_display_name(),
                    error=e,
                )

            try:
                async with db.begin_nested():
                    pal24_result = await db.execute(select(Pal24Payment).where(Pal24Payment.user_id == user_id))
                    pal24_payments = pal24_result.scalars().all()

                    if pal24_payments:
                        logger.info('🔄 Удаляем Pal24 платежей', pal24_payments_count=len(pal24_payments))
                        await db.execute(
                            update(Pal24Payment).where(Pal24Payment.user_id == user_id).values(transaction_id=None)
                        )
                        await db.flush()
                        await db.execute(delete(Pal24Payment).where(Pal24Payment.user_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления Pal24 платежей', error=e)

            try:
                async with db.begin_nested():
                    heleket_result = await db.execute(select(HeleketPayment).where(HeleketPayment.user_id == user_id))
                    heleket_payments = heleket_result.scalars().all()

                    if heleket_payments:
                        logger.info('🔄 Удаляем Heleket платежей', heleket_payments_count=len(heleket_payments))
                        await db.execute(
                            update(HeleketPayment).where(HeleketPayment.user_id == user_id).values(transaction_id=None)
                        )
                        await db.flush()
                        await db.execute(delete(HeleketPayment).where(HeleketPayment.user_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления Heleket платежей', error=e)

            # Удаляем Freekassa платежи
            try:
                async with db.begin_nested():
                    freekassa_payments_result = await db.execute(
                        select(FreekassaPayment).where(FreekassaPayment.user_id == user_id)
                    )
                    freekassa_payments = freekassa_payments_result.scalars().all()

                    if freekassa_payments:
                        logger.info('🔄 Удаляем Freekassa платежей', freekassa_payments_count=len(freekassa_payments))
                        await db.execute(
                            update(FreekassaPayment)
                            .where(FreekassaPayment.user_id == user_id)
                            .values(transaction_id=None)
                        )
                        await db.flush()
                        await db.execute(delete(FreekassaPayment).where(FreekassaPayment.user_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления Freekassa платежей', error=e)

            # Удаляем Wata платежи (до транзакций, т.к. wata_payments.transaction_id -> transactions.id)
            try:
                async with db.begin_nested():
                    wata_payments_result = await db.execute(select(WataPayment).where(WataPayment.user_id == user_id))
                    wata_payments = wata_payments_result.scalars().all()

                    if wata_payments:
                        logger.info('🔄 Удаляем Wata платежей', wata_payments_count=len(wata_payments))
                        await db.execute(
                            update(WataPayment).where(WataPayment.user_id == user_id).values(transaction_id=None)
                        )
                        await db.flush()
                        await db.execute(delete(WataPayment).where(WataPayment.user_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления Wata платежей', error=e)

            # Удаляем CloudPayments платежи
            try:
                async with db.begin_nested():
                    cloudpayments_result = await db.execute(
                        select(CloudPaymentsPayment).where(CloudPaymentsPayment.user_id == user_id)
                    )
                    cloudpayments_payments = cloudpayments_result.scalars().all()

                    if cloudpayments_payments:
                        logger.info(
                            '🔄 Удаляем CloudPayments платежей',
                            cloudpayments_payments_count=len(cloudpayments_payments),
                        )
                        await db.execute(
                            update(CloudPaymentsPayment)
                            .where(CloudPaymentsPayment.user_id == user_id)
                            .values(transaction_id=None)
                        )
                        await db.flush()
                        await db.execute(delete(CloudPaymentsPayment).where(CloudPaymentsPayment.user_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления CloudPayments платежей', error=e)

            # Удаляем KassaAi платежи
            try:
                async with db.begin_nested():
                    kassa_ai_result = await db.execute(select(KassaAiPayment).where(KassaAiPayment.user_id == user_id))
                    kassa_ai_payments = kassa_ai_result.scalars().all()

                    if kassa_ai_payments:
                        logger.info('🔄 Удаляем KassaAi платежей', kassa_ai_payments_count=len(kassa_ai_payments))
                        await db.execute(
                            update(KassaAiPayment).where(KassaAiPayment.user_id == user_id).values(transaction_id=None)
                        )
                        await db.flush()
                        await db.execute(delete(KassaAiPayment).where(KassaAiPayment.user_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления KassaAi платежей', error=e)

            try:
                async with db.begin_nested():
                    transactions_result = await db.execute(select(Transaction).where(Transaction.user_id == user_id))
                    transactions = transactions_result.scalars().all()

                    if transactions:
                        logger.info('🔄 Удаляем транзакций', transactions_count=len(transactions))
                        await db.execute(delete(Transaction).where(Transaction.user_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления транзакций', error=e)

            try:
                async with db.begin_nested():
                    promocode_uses_result = await db.execute(
                        select(PromoCodeUse).where(PromoCodeUse.user_id == user_id)
                    )
                    promocode_uses = promocode_uses_result.scalars().all()

                    if promocode_uses:
                        logger.info('🔄 Удаляем использований промокодов', promocode_uses_count=len(promocode_uses))
                        await db.execute(delete(PromoCodeUse).where(PromoCodeUse.user_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления использований промокодов', error=e)

            try:
                async with db.begin_nested():
                    referral_earnings_result = await db.execute(
                        select(ReferralEarning).where(ReferralEarning.user_id == user_id)
                    )
                    referral_earnings = referral_earnings_result.scalars().all()

                    if referral_earnings:
                        logger.info('🔄 Удаляем реферальных доходов', referral_earnings_count=len(referral_earnings))
                        await db.execute(delete(ReferralEarning).where(ReferralEarning.user_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления реферальных доходов', error=e)

            try:
                async with db.begin_nested():
                    referral_records_result = await db.execute(
                        select(ReferralEarning).where(ReferralEarning.referral_id == user_id)
                    )
                    referral_records = referral_records_result.scalars().all()

                    if referral_records:
                        logger.info('🔄 Удаляем записей о рефералах', referral_records_count=len(referral_records))
                        await db.execute(delete(ReferralEarning).where(ReferralEarning.referral_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления записей о рефералах', error=e)

            try:
                async with db.begin_nested():
                    conversions_result = await db.execute(
                        select(SubscriptionConversion).where(SubscriptionConversion.user_id == user_id)
                    )
                    conversions = conversions_result.scalars().all()

                    if conversions:
                        logger.info('🔄 Удаляем записей конверсий', conversions_count=len(conversions))
                        await db.execute(
                            delete(SubscriptionConversion).where(SubscriptionConversion.user_id == user_id)
                        )
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления записей конверсий', error=e)

            try:
                async with db.begin_nested():
                    broadcast_history_result = await db.execute(
                        select(BroadcastHistory).where(BroadcastHistory.admin_id == user_id)
                    )
                    broadcast_history = broadcast_history_result.scalars().all()

                    if broadcast_history:
                        logger.info(
                            '🔄 Удаляем записей истории рассылок', broadcast_history_count=len(broadcast_history)
                        )
                        await db.execute(delete(BroadcastHistory).where(BroadcastHistory.admin_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления истории рассылок', error=e)

            try:
                async with db.begin_nested():
                    campaigns_result = await db.execute(
                        select(AdvertisingCampaign).where(AdvertisingCampaign.created_by == user_id)
                    )
                    campaigns = campaigns_result.scalars().all()

                    if campaigns:
                        logger.info('🔄 Очищаем создателя у рекламных кампаний', campaigns_count=len(campaigns))
                        await db.execute(
                            update(AdvertisingCampaign)
                            .where(AdvertisingCampaign.created_by == user_id)
                            .values(created_by=None)
                        )
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка обновления рекламных кампаний', error=e)

            try:
                async with db.begin_nested():
                    subs = getattr(user, 'subscriptions', None) or []
                    if subs:
                        all_squad_ids: set[str] = set()
                        for sub in subs:
                            logger.info('🔄 Удаляем подписку', subscription_id=sub.id)
                            if sub.connected_squads:
                                all_squad_ids.update(sub.connected_squads)
                            await db.execute(
                                delete(SubscriptionServer).where(SubscriptionServer.subscription_id == sub.id)
                            )

                        # Delete all subscriptions for this user
                        # Lock order: subscriptions -> server_squads (matches webhook order)
                        await db.execute(delete(Subscription).where(Subscription.user_id == user_id))
                        await db.flush()

                        # Decrement server_squads.current_users AFTER subscription delete
                        # to match lock ordering with webhook and avoid deadlocks
                        if all_squad_ids:
                            try:
                                from app.database.crud.server_squad import (
                                    get_server_ids_by_uuids,
                                    remove_user_from_servers,
                                )

                                int_squad_ids = await get_server_ids_by_uuids(db, list(all_squad_ids))
                                if int_squad_ids:
                                    await remove_user_from_servers(db, int_squad_ids)
                            except Exception as sq_err:
                                logger.warning('⚠️ Не удалось уменьшить счётчик серверов', error=sq_err)
            except Exception as e:
                logger.error('❌ Ошибка удаления подписок', error=e)

            try:
                from app.database.models import (
                    AccessPolicy,
                    AdminAuditLog,
                    AdminRole,
                    RioPayPayment,
                    SavedPaymentMethod,
                    UserRole,
                    WithdrawalRequest,
                )

                await db.execute(delete(SavedPaymentMethod).where(SavedPaymentMethod.user_id == user_id))
                await db.execute(delete(RioPayPayment).where(RioPayPayment.user_id == user_id))
                await db.execute(delete(AdminAuditLog).where(AdminAuditLog.user_id == user_id))
                await db.execute(delete(WithdrawalRequest).where(WithdrawalRequest.user_id == user_id))
                await db.execute(
                    update(WithdrawalRequest).where(WithdrawalRequest.processed_by == user_id).values(processed_by=None)
                )
                await db.execute(update(AdminRole).where(AdminRole.created_by == user_id).values(created_by=None))
                await db.execute(update(UserRole).where(UserRole.assigned_by == user_id).values(assigned_by=None))
                await db.execute(update(AccessPolicy).where(AccessPolicy.created_by == user_id).values(created_by=None))
                await db.execute(delete(User).where(User.id == user_id))
                await db.commit()
                logger.info('✅ Пользователь окончательно удален из базы', user_id=user_id)
            except Exception as e:
                logger.error('❌ Ошибка финального удаления пользователя', error=e)
                await db.rollback()
                return result

            result.bot_deleted = True
            logger.info(
                '✅ Пользователь (ID: ) полностью удален администратором',
                user_id_display=user_id_display,
                user_id=user_id,
                admin_id=admin_id,
            )
            return result

        except Exception as e:
            logger.error('❌ Критическая ошибка удаления пользователя', user_id=user_id, error=e)
            await db.rollback()
            return result

    async def get_user_statistics(self, db: AsyncSession) -> dict[str, Any]:
        try:
            stats = await get_users_statistics(db)
            return stats

        except Exception as e:
            logger.error('Ошибка получения статистики пользователей', error=e)
            return {
                'total_users': 0,
                'active_users': 0,
                'blocked_users': 0,
                'new_today': 0,
                'new_week': 0,
                'new_month': 0,
            }

    async def cleanup_inactive_users(self, db: AsyncSession, months: int = None) -> tuple[int, int]:
        """Clean up inactive users, skipping those with active subscriptions.

        Returns:
            Tuple of (deleted_count, skipped_active_sub_count).
        """
        try:
            if months is None:
                months = settings.INACTIVE_USER_DELETE_MONTHS

            inactive_users = await get_inactive_users(db, months)
            deleted_count = 0
            skipped_active_sub = 0

            for user in inactive_users:
                # Skip users with active paid subscriptions
                if any(sub.is_active for sub in (getattr(user, 'subscriptions', None) or [])):
                    skipped_active_sub += 1
                    continue

                delete_result = await self.delete_user_account(db, user.id, 0)
                if delete_result.bot_deleted:
                    deleted_count += 1

            if skipped_active_sub > 0:
                logger.info(
                    'Пропущено неактивных пользователей с активной подпиской', skipped_active_sub=skipped_active_sub
                )
            logger.info('Удалено неактивных пользователей', deleted_count=deleted_count)
            return deleted_count, skipped_active_sub

        except Exception as e:
            logger.error('Ошибка очистки неактивных пользователей', e=e)
            return 0, 0

    async def get_user_activity_summary(self, db: AsyncSession, user_id: int) -> dict[str, Any]:
        try:
            user = await get_user_by_id(db, user_id)
            if not user:
                return {}

            if settings.is_multi_tariff_enabled():
                from app.database.crud.subscription import get_active_subscriptions_by_user_id

                active_subs = await get_active_subscriptions_by_user_id(db, user_id)
                if active_subs:
                    _non_daily = [s for s in active_subs if not getattr(s, 'is_daily_tariff', False)]
                    _pool = _non_daily or active_subs
                    subscription = max(_pool, key=lambda s: s.days_left)
                else:
                    subscription = None
            else:
                subscription = await get_subscription_by_user_id(db, user_id)
            transactions_count = await get_user_transactions_count(db, user_id)

            days_since_registration = (datetime.now(UTC) - user.created_at).days

            days_since_activity = (datetime.now(UTC) - user.last_activity).days if user.last_activity else None

            return {
                'user_id': user.id,
                'telegram_id': user.telegram_id,
                'username': user.username,
                'full_name': user.full_name,
                'status': user.status,
                'language': user.language,
                'balance_kopeks': user.balance_kopeks,
                'registration_date': user.created_at,
                'last_activity': user.last_activity,
                'days_since_registration': days_since_registration,
                'days_since_activity': days_since_activity,
                'has_subscription': subscription is not None,
                'subscription_active': subscription.is_active if subscription else False,
                'subscription_trial': subscription.is_trial if subscription else False,
                'transactions_count': transactions_count,
                'referrer_id': user.referred_by_id,
                'referral_code': user.referral_code,
            }

        except Exception as e:
            logger.error('Ошибка получения сводки активности пользователя', user_id=user_id, error=e)
            return {}

    async def get_users_by_criteria(self, db: AsyncSession, criteria: dict[str, Any]) -> list[User]:
        try:
            status = criteria.get('status')
            criteria.get('has_subscription')
            criteria.get('is_trial')
            min_balance = criteria.get('min_balance', 0)
            max_balance = criteria.get('max_balance')
            days_inactive = criteria.get('days_inactive')

            registered_after = criteria.get('registered_after')
            registered_before = criteria.get('registered_before')

            users = await get_users_list(db, offset=0, limit=10000, status=status)

            filtered_users = []
            for user in users:
                if user.balance_kopeks < min_balance:
                    continue
                if max_balance and user.balance_kopeks > max_balance:
                    continue

                if registered_after and user.created_at < registered_after:
                    continue
                if registered_before and user.created_at > registered_before:
                    continue

                if days_inactive and user.last_activity:
                    inactive_threshold = datetime.now(UTC) - timedelta(days=days_inactive)
                    if user.last_activity > inactive_threshold:
                        continue

                filtered_users.append(user)

            return filtered_users

        except Exception as e:
            logger.error('Ошибка получения пользователей по критериям', error=e)
            return []
