import logging
from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot, types
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.crud.promo_group import get_promo_group_by_id
from app.database.crud.subscription import (
    decrement_subscription_server_counts,
    get_subscription_by_user_id,
)
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
    CryptoBotPayment,
    FreekassaPayment,
    HeleketPayment,
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


logger = logging.getLogger(__name__)


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
            keyboard = types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('SUBSCRIPTION_EXTEND', '💎 Продлить подписку'),
                            callback_data='subscription_extend',
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
            keyboard = types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='🚀 АКТИВИРОВАТЬ ПОДПИСКУ', callback_data='subscription_buy')],
                    [types.InlineKeyboardButton(text='💎 ПРОДЛИТЬ ПОДПИСКУ', callback_data='subscription_extend')],
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
        if getattr(user, 'subscription', None) and user.subscription.status in {
            'active',
            'expired',
            'trial',
        }:
            keyboard_rows.append(
                [
                    types.InlineKeyboardButton(
                        text=get_texts(user.language).t('SUBSCRIPTION_EXTEND', '💎 Продлить подписку'),
                        callback_data='subscription_extend',
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

            subscription = await get_subscription_by_user_id(db, user_id)
            transactions_count = await get_user_transactions_count(db, user_id)

            return {
                'user': user,
                'subscription': subscription,
                'transactions_count': transactions_count,
                'is_admin': settings.is_admin(user.telegram_id, user.email),
                'registration_days': (datetime.utcnow() - user.created_at).days,
            }

        except Exception as e:
            logger.error(f'Ошибка получения профиля пользователя {user_id}: {e}')
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
            logger.error(f'Ошибка поиска пользователей: {e}')
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
            logger.error(f'Ошибка получения страницы пользователей: {e}')
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
            now = datetime.utcnow()

            base_filters = [
                User.balance_kopeks >= min_balance_kopeks,
                Subscription.end_date.isnot(None),
                Subscription.end_date <= now,
            ]

            query = (
                select(User)
                .options(selectinload(User.subscription))
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
            logger.error(f'Ошибка получения пользователей для продления: {e}')
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

            # Основной запрос с LEFT JOIN для поддержки пользователей без подписки
            query = (
                select(User)
                .options(selectinload(User.subscription))
                .outerjoin(Subscription, Subscription.user_id == User.id)
                .where(
                    *base_filters,
                    or_(
                        User.subscription == None,
                        ~Subscription.status.in_(['active', 'trial']),
                    ),
                )
                .order_by(User.balance_kopeks.desc(), User.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
            result = await db.execute(query)
            users = result.scalars().unique().all()

            # Запрос для подсчета общего количества
            count_query = (
                select(func.count(User.id))
                .outerjoin(Subscription, Subscription.user_id == User.id)
                .where(
                    *base_filters,
                    or_(
                        User.subscription == None,
                        ~Subscription.status.in_(['active', 'trial']),
                    ),
                )
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
            logger.error(f'Ошибка получения потенциальных клиентов: {e}')
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
            logger.error(f'Ошибка получения статистики трат пользователей: {e}')
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
            logger.error(f'Ошибка получения пользователей по кампаниям: {e}')
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
                logger.info(f'Админ {admin_id} пополнил баланс пользователя {user_id} на {amount_kopeks / 100}₽')
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
                    logger.info(f'Админ {admin_id} списал с баланса пользователя {user_id} {abs(amount_kopeks) / 100}₽')

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
            logger.error(f'Ошибка изменения баланса пользователя: {e}')
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
            user.updated_at = datetime.utcnow()

            await db.commit()
            await db.refresh(user)

            logger.info(
                "👥 Промогруппа пользователя %s обновлена на '%s'",
                user.telegram_id,
                promo_group.name,
            )

            return True, user, promo_group, old_group

        except Exception as e:
            await db.rollback()
            logger.error(f'Ошибка обновления промогруппы пользователя {user_id}: {e}')
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
                'Админ %s обновил рефералов пользователя %s: добавлено %s, удалено %s, всего %s',
                admin_id,
                user_id,
                len(to_add),
                len(to_remove),
                len(unique_ids),
            )

            return True, {
                'added': len(to_add),
                'removed': len(to_remove),
                'total': len(unique_ids),
            }

        except Exception as e:
            await db.rollback()
            logger.error(
                'Ошибка обновления рефералов пользователя %s: %s',
                user_id,
                e,
            )
            return False, {'error': 'update_failed'}

    async def block_user(
        self, db: AsyncSession, user_id: int, admin_id: int, reason: str = 'Заблокирован администратором'
    ) -> bool:
        try:
            user = await get_user_by_id(db, user_id)
            if not user:
                return False

            if user.remnawave_uuid:
                try:
                    from app.services.subscription_service import SubscriptionService

                    subscription_service = SubscriptionService()
                    await subscription_service.disable_remnawave_user(user.remnawave_uuid)
                    logger.info(f'✅ RemnaWave пользователь {user.remnawave_uuid} деактивирован при блокировке')
                except Exception as e:
                    logger.error(f'❌ Ошибка деактивации RemnaWave пользователя при блокировке: {e}')

            if user.subscription:
                from app.database.crud.subscription import deactivate_subscription

                await deactivate_subscription(db, user.subscription)

            await update_user(db, user, status=UserStatus.BLOCKED.value)

            logger.info(f'Админ {admin_id} заблокировал пользователя {user_id}: {reason}')
            return True

        except Exception as e:
            logger.error(f'Ошибка блокировки пользователя: {e}')
            return False

    async def unblock_user(self, db: AsyncSession, user_id: int, admin_id: int) -> bool:
        try:
            user = await get_user_by_id(db, user_id)
            if not user:
                return False

            await update_user(db, user, status=UserStatus.ACTIVE.value)

            if user.subscription:
                from datetime import datetime

                from app.database.models import SubscriptionStatus

                if user.subscription.end_date > datetime.utcnow():
                    user.subscription.status = SubscriptionStatus.ACTIVE.value
                    await db.commit()
                    await db.refresh(user.subscription)
                    logger.info(f'🔄 Подписка пользователя {user_id} восстановлена')

                    if user.remnawave_uuid:
                        try:
                            from app.services.subscription_service import SubscriptionService

                            subscription_service = SubscriptionService()
                            await subscription_service.update_remnawave_user(db, user.subscription)
                            logger.info(
                                f'✅ RemnaWave пользователь {user.remnawave_uuid} восстановлен при разблокировке'
                            )
                        except Exception as e:
                            logger.error(f'❌ Ошибка восстановления RemnaWave пользователя при разблокировке: {e}')
                else:
                    logger.info(f'⏰ Подписка пользователя {user_id} истекла, восстановление невозможно')

            logger.info(f'Админ {admin_id} разблокировал пользователя {user_id}')
            return True

        except Exception as e:
            logger.error(f'Ошибка разблокировки пользователя: {e}')
            return False

    async def delete_user_account(self, db: AsyncSession, user_id: int, admin_id: int) -> bool:
        try:
            user = await get_user_by_id(db, user_id)
            if not user:
                logger.warning(f'Пользователь {user_id} не найден для удаления')
                return False

            user_id_display = user.telegram_id or user.email or f'#{user.id}'
            logger.info(f'🗑️ Начинаем полное удаление пользователя {user_id} (ID: {user_id_display})')

            if user.remnawave_uuid:
                from app.config import settings

                delete_mode = settings.get_remnawave_user_delete_mode()

                try:
                    from app.services.remnawave_service import RemnaWaveService

                    remnawave_service = RemnaWaveService()

                    if delete_mode == 'delete':
                        # Удаляем пользователя из панели Remnawave
                        async with remnawave_service.get_api_client() as api:
                            delete_success = await api.delete_user(user.remnawave_uuid)
                            if delete_success:
                                logger.info(f'✅ RemnaWave пользователь {user.remnawave_uuid} удален из панели')
                            else:
                                logger.warning(
                                    f'⚠️ Не удалось удалить пользователя {user.remnawave_uuid} из панели Remnawave'
                                )
                    else:
                        # Деактивируем пользователя в панели Remnawave
                        from app.services.subscription_service import SubscriptionService

                        subscription_service = SubscriptionService()
                        await subscription_service.disable_remnawave_user(user.remnawave_uuid)
                        logger.info(
                            f'✅ RemnaWave пользователь {user.remnawave_uuid} деактивирован (режим: {delete_mode})'
                        )

                except Exception as e:
                    logger.warning(f'⚠️ Ошибка обработки пользователя в Remnawave (режим: {delete_mode}): {e}')
                    # Если основное действие не удалось, попытаемся хотя бы деактивировать
                    if delete_mode == 'delete':
                        try:
                            from app.services.subscription_service import SubscriptionService

                            subscription_service = SubscriptionService()
                            await subscription_service.disable_remnawave_user(user.remnawave_uuid)
                            logger.info(f'✅ RemnaWave пользователь {user.remnawave_uuid} деактивирован как fallback')
                        except Exception as fallback_e:
                            logger.error(f'❌ Ошибка деактивации RemnaWave как fallback: {fallback_e}')

            try:
                sent_notifications_result = await db.execute(
                    select(SentNotification).where(SentNotification.user_id == user_id)
                )
                sent_notifications = sent_notifications_result.scalars().all()

                if sent_notifications:
                    logger.info(f'🔄 Удаляем {len(sent_notifications)} уведомлений')
                    await db.execute(delete(SentNotification).where(SentNotification.user_id == user_id))
                    await db.flush()
            except Exception as e:
                logger.error(f'❌ Ошибка удаления уведомлений: {e}')

            try:
                if user.subscription:
                    subscription_servers_result = await db.execute(
                        select(SubscriptionServer).where(SubscriptionServer.subscription_id == user.subscription.id)
                    )
                    subscription_servers = subscription_servers_result.scalars().all()

                    await decrement_subscription_server_counts(
                        db,
                        user.subscription,
                        subscription_servers=subscription_servers,
                    )

                    if subscription_servers:
                        logger.info(f'🔄 Удаляем {len(subscription_servers)} связей подписка-сервер')
                        await db.execute(
                            delete(SubscriptionServer).where(SubscriptionServer.subscription_id == user.subscription.id)
                        )
                        await db.flush()
            except Exception as e:
                logger.error(f'❌ Ошибка удаления связей подписка-сервер: {e}')

            try:
                user_messages_result = await db.execute(
                    update(UserMessage).where(UserMessage.created_by == user_id).values(created_by=None)
                )
                if user_messages_result.rowcount > 0:
                    logger.info(f'🔄 Обновлено {user_messages_result.rowcount} пользовательских сообщений')
                await db.flush()
            except Exception as e:
                logger.error(f'❌ Ошибка обновления пользовательских сообщений: {e}')

            try:
                promocodes_result = await db.execute(
                    update(PromoCode).where(PromoCode.created_by == user_id).values(created_by=None)
                )
                if promocodes_result.rowcount > 0:
                    logger.info(f'🔄 Обновлено {promocodes_result.rowcount} промокодов')
                await db.flush()
            except Exception as e:
                logger.error(f'❌ Ошибка обновления промокодов: {e}')

            try:
                welcome_texts_result = await db.execute(
                    update(WelcomeText).where(WelcomeText.created_by == user_id).values(created_by=None)
                )
                if welcome_texts_result.rowcount > 0:
                    logger.info(f'🔄 Обновлено {welcome_texts_result.rowcount} приветственных текстов')
                await db.flush()
            except Exception as e:
                logger.error(f'❌ Ошибка обновления приветственных текстов: {e}')

            try:
                referrals_result = await db.execute(
                    update(User).where(User.referred_by_id == user_id).values(referred_by_id=None)
                )
                if referrals_result.rowcount > 0:
                    logger.info(f'🔗 Очищены реферальные ссылки у {referrals_result.rowcount} рефералов')
                await db.flush()
            except Exception as e:
                logger.error(f'❌ Ошибка очистки реферальных ссылок: {e}')

            try:
                yookassa_result = await db.execute(select(YooKassaPayment).where(YooKassaPayment.user_id == user_id))
                yookassa_payments = yookassa_result.scalars().all()

                if yookassa_payments:
                    logger.info(f'🔄 Удаляем {len(yookassa_payments)} YooKassa платежей')
                    await db.execute(
                        update(YooKassaPayment).where(YooKassaPayment.user_id == user_id).values(transaction_id=None)
                    )
                    await db.flush()
                    await db.execute(delete(YooKassaPayment).where(YooKassaPayment.user_id == user_id))
                    await db.flush()
            except Exception as e:
                logger.error(f'❌ Ошибка удаления YooKassa платежей: {e}')

            try:
                cryptobot_result = await db.execute(select(CryptoBotPayment).where(CryptoBotPayment.user_id == user_id))
                cryptobot_payments = cryptobot_result.scalars().all()

                if cryptobot_payments:
                    logger.info(f'🔄 Удаляем {len(cryptobot_payments)} CryptoBot платежей')
                    await db.execute(
                        update(CryptoBotPayment).where(CryptoBotPayment.user_id == user_id).values(transaction_id=None)
                    )
                    await db.flush()
                    await db.execute(delete(CryptoBotPayment).where(CryptoBotPayment.user_id == user_id))
                    await db.flush()
            except Exception as e:
                logger.error(f'❌ Ошибка удаления CryptoBot платежей: {e}')

            try:
                platega_result = await db.execute(select(PlategaPayment).where(PlategaPayment.user_id == user_id))
                platega_payments = platega_result.scalars().all()

                if platega_payments:
                    logger.info(f'🔄 Удаляем {len(platega_payments)} Platega платежей')
                    await db.execute(
                        update(PlategaPayment).where(PlategaPayment.user_id == user_id).values(transaction_id=None)
                    )
                    await db.flush()
                    await db.execute(delete(PlategaPayment).where(PlategaPayment.user_id == user_id))
                    await db.flush()
            except Exception as e:
                logger.error(f'❌ Ошибка удаления Platega платежей: {e}')

            try:
                mulenpay_result = await db.execute(select(MulenPayPayment).where(MulenPayPayment.user_id == user_id))
                mulenpay_payments = mulenpay_result.scalars().all()

                if mulenpay_payments:
                    mulenpay_name = settings.get_mulenpay_display_name()
                    logger.info(f'🔄 Удаляем {len(mulenpay_payments)} {mulenpay_name} платежей')
                    await db.execute(
                        update(MulenPayPayment).where(MulenPayPayment.user_id == user_id).values(transaction_id=None)
                    )
                    await db.flush()
                    await db.execute(delete(MulenPayPayment).where(MulenPayPayment.user_id == user_id))
                    await db.flush()
            except Exception as e:
                logger.error(f'❌ Ошибка удаления {settings.get_mulenpay_display_name()} платежей: {e}')

            try:
                pal24_result = await db.execute(select(Pal24Payment).where(Pal24Payment.user_id == user_id))
                pal24_payments = pal24_result.scalars().all()

                if pal24_payments:
                    logger.info(f'🔄 Удаляем {len(pal24_payments)} Pal24 платежей')
                    await db.execute(
                        update(Pal24Payment).where(Pal24Payment.user_id == user_id).values(transaction_id=None)
                    )
                    await db.flush()
                    await db.execute(delete(Pal24Payment).where(Pal24Payment.user_id == user_id))
                    await db.flush()
            except Exception as e:
                logger.error(f'❌ Ошибка удаления Pal24 платежей: {e}')

            try:
                heleket_result = await db.execute(select(HeleketPayment).where(HeleketPayment.user_id == user_id))
                heleket_payments = heleket_result.scalars().all()

                if heleket_payments:
                    logger.info(f'🔄 Удаляем {len(heleket_payments)} Heleket платежей')
                    await db.execute(
                        update(HeleketPayment).where(HeleketPayment.user_id == user_id).values(transaction_id=None)
                    )
                    await db.flush()
                    await db.execute(delete(HeleketPayment).where(HeleketPayment.user_id == user_id))
                    await db.flush()
            except Exception as e:
                logger.error(f'❌ Ошибка удаления Heleket платежей: {e}')

            # Удаляем Freekassa платежи
            try:
                freekassa_payments_result = await db.execute(
                    select(FreekassaPayment).where(FreekassaPayment.user_id == user_id)
                )
                freekassa_payments = freekassa_payments_result.scalars().all()

                if freekassa_payments:
                    logger.info(f'🔄 Удаляем {len(freekassa_payments)} Freekassa платежей')
                    await db.execute(
                        update(FreekassaPayment).where(FreekassaPayment.user_id == user_id).values(transaction_id=None)
                    )
                    await db.flush()
                    await db.execute(delete(FreekassaPayment).where(FreekassaPayment.user_id == user_id))
                    await db.flush()
            except Exception as e:
                logger.error(f'❌ Ошибка удаления Freekassa платежей: {e}')

            try:
                transactions_result = await db.execute(select(Transaction).where(Transaction.user_id == user_id))
                transactions = transactions_result.scalars().all()

                if transactions:
                    logger.info(f'🔄 Удаляем {len(transactions)} транзакций')
                    await db.execute(delete(Transaction).where(Transaction.user_id == user_id))
                    await db.flush()
            except Exception as e:
                logger.error(f'❌ Ошибка удаления транзакций: {e}')

            try:
                promocode_uses_result = await db.execute(select(PromoCodeUse).where(PromoCodeUse.user_id == user_id))
                promocode_uses = promocode_uses_result.scalars().all()

                if promocode_uses:
                    logger.info(f'🔄 Удаляем {len(promocode_uses)} использований промокодов')
                    await db.execute(delete(PromoCodeUse).where(PromoCodeUse.user_id == user_id))
                    await db.flush()
            except Exception as e:
                logger.error(f'❌ Ошибка удаления использований промокодов: {e}')

            try:
                referral_earnings_result = await db.execute(
                    select(ReferralEarning).where(ReferralEarning.user_id == user_id)
                )
                referral_earnings = referral_earnings_result.scalars().all()

                if referral_earnings:
                    logger.info(f'🔄 Удаляем {len(referral_earnings)} реферальных доходов')
                    await db.execute(delete(ReferralEarning).where(ReferralEarning.user_id == user_id))
                    await db.flush()
            except Exception as e:
                logger.error(f'❌ Ошибка удаления реферальных доходов: {e}')

            try:
                referral_records_result = await db.execute(
                    select(ReferralEarning).where(ReferralEarning.referral_id == user_id)
                )
                referral_records = referral_records_result.scalars().all()

                if referral_records:
                    logger.info(f'🔄 Удаляем {len(referral_records)} записей о рефералах')
                    await db.execute(delete(ReferralEarning).where(ReferralEarning.referral_id == user_id))
                    await db.flush()
            except Exception as e:
                logger.error(f'❌ Ошибка удаления записей о рефералах: {e}')

            try:
                conversions_result = await db.execute(
                    select(SubscriptionConversion).where(SubscriptionConversion.user_id == user_id)
                )
                conversions = conversions_result.scalars().all()

                if conversions:
                    logger.info(f'🔄 Удаляем {len(conversions)} записей конверсий')
                    await db.execute(delete(SubscriptionConversion).where(SubscriptionConversion.user_id == user_id))
                    await db.flush()
            except Exception as e:
                logger.error(f'❌ Ошибка удаления записей конверсий: {e}')

            try:
                broadcast_history_result = await db.execute(
                    select(BroadcastHistory).where(BroadcastHistory.admin_id == user_id)
                )
                broadcast_history = broadcast_history_result.scalars().all()

                if broadcast_history:
                    logger.info(f'🔄 Удаляем {len(broadcast_history)} записей истории рассылок')
                    await db.execute(delete(BroadcastHistory).where(BroadcastHistory.admin_id == user_id))
                    await db.flush()
            except Exception as e:
                logger.error(f'❌ Ошибка удаления истории рассылок: {e}')

            try:
                campaigns_result = await db.execute(
                    select(AdvertisingCampaign).where(AdvertisingCampaign.created_by == user_id)
                )
                campaigns = campaigns_result.scalars().all()

                if campaigns:
                    logger.info(f'🔄 Очищаем создателя у {len(campaigns)} рекламных кампаний')
                    await db.execute(
                        update(AdvertisingCampaign)
                        .where(AdvertisingCampaign.created_by == user_id)
                        .values(created_by=None)
                    )
                    await db.flush()
            except Exception as e:
                logger.error(f'❌ Ошибка обновления рекламных кампаний: {e}')

            try:
                if user.subscription:
                    logger.info(f'🔄 Удаляем подписку {user.subscription.id}')
                    await db.execute(delete(Subscription).where(Subscription.user_id == user_id))
                    await db.flush()
            except Exception as e:
                logger.error(f'❌ Ошибка удаления подписки: {e}')

            try:
                wata_payments_result = await db.execute(select(WataPayment).where(WataPayment.user_id == user_id))
                wata_payments = wata_payments_result.scalars().all()

                if wata_payments:
                    logger.info(f'🔄 Удаляем {len(wata_payments)} Wata платежей')
                    await db.execute(delete(WataPayment).where(WataPayment.user_id == user_id))
                    await db.flush()
            except Exception as e:
                logger.error(f'❌ Ошибка удаления Wata платежей: {e}')

            try:
                await db.execute(delete(User).where(User.id == user_id))
                await db.commit()
                logger.info(f'✅ Пользователь {user_id} окончательно удален из базы')
            except Exception as e:
                logger.error(f'❌ Ошибка финального удаления пользователя: {e}')
                await db.rollback()
                return False

            logger.info(
                f'✅ Пользователь {user_id_display} (ID: {user_id}) полностью удален администратором {admin_id}'
            )
            return True

        except Exception as e:
            logger.error(f'❌ Критическая ошибка удаления пользователя {user_id}: {e}')
            await db.rollback()
            return False

    async def get_user_statistics(self, db: AsyncSession) -> dict[str, Any]:
        try:
            stats = await get_users_statistics(db)
            return stats

        except Exception as e:
            logger.error(f'Ошибка получения статистики пользователей: {e}')
            return {
                'total_users': 0,
                'active_users': 0,
                'blocked_users': 0,
                'new_today': 0,
                'new_week': 0,
                'new_month': 0,
            }

    async def cleanup_inactive_users(self, db: AsyncSession, months: int = None) -> int:
        try:
            if months is None:
                months = settings.INACTIVE_USER_DELETE_MONTHS

            inactive_users = await get_inactive_users(db, months)
            deleted_count = 0

            for user in inactive_users:
                success = await self.delete_user_account(db, user.id, 0)
                if success:
                    deleted_count += 1

            logger.info(f'Удалено {deleted_count} неактивных пользователей')
            return deleted_count

        except Exception as e:
            logger.error(f'Ошибка очистки неактивных пользователей: {e}')
            return 0

    async def get_user_activity_summary(self, db: AsyncSession, user_id: int) -> dict[str, Any]:
        try:
            user = await get_user_by_id(db, user_id)
            if not user:
                return {}

            subscription = await get_subscription_by_user_id(db, user_id)
            transactions_count = await get_user_transactions_count(db, user_id)

            days_since_registration = (datetime.utcnow() - user.created_at).days

            days_since_activity = (datetime.utcnow() - user.last_activity).days if user.last_activity else None

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
            logger.error(f'Ошибка получения сводки активности пользователя {user_id}: {e}')
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
                    inactive_threshold = datetime.utcnow() - timedelta(days=days_inactive)
                    if user.last_activity > inactive_threshold:
                        continue

                filtered_users.append(user)

            return filtered_users

        except Exception as e:
            logger.error(f'Ошибка получения пользователей по критериям: {e}')
            return []
