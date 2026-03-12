import structlog
from aiogram import Bot
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.referral import create_referral_earning, get_commission_payment_count, get_user_campaign_id
from app.database.crud.user import add_user_balance, get_user_by_id
from app.database.models import ReferralEarning, TransactionType, User
from app.services.bot_router import resolve_bot_for_user
from app.services.notification_delivery_service import (
    notification_delivery_service,
)
from app.utils.user_utils import get_effective_referral_commission_percent


logger = structlog.get_logger(__name__)


async def _is_commission_limit_reached(db: AsyncSession, referrer_id: int, referral_id: int) -> bool:
    """Проверяет, исчерпан ли лимит комиссионных платежей для пары реферер-реферал."""
    if settings.REFERRAL_MAX_COMMISSION_PAYMENTS <= 0:
        return False
    paid_count = await get_commission_payment_count(db, referrer_id, referral_id)
    if paid_count >= settings.REFERRAL_MAX_COMMISSION_PAYMENTS:
        logger.info(
            'Лимит комиссионных платежей исчерпан',
            referrer_id=referrer_id,
            referral_id=referral_id,
            paid_count=paid_count,
            max_payments=settings.REFERRAL_MAX_COMMISSION_PAYMENTS,
        )
        return True
    return False


async def send_referral_notification(
    bot: Bot,
    telegram_id: int | None,
    message: str,
    user: User | None = None,
    bonus_kopeks: int = 0,
    referral_name: str = '',
):
    """
    Отправляет реферальное уведомление в Telegram или по email.

    Args:
        bot: Telegram Bot instance
        telegram_id: Telegram user ID (может быть None для email-пользователей)
        message: Текст уведомления
        user: User object (для email-only пользователей)
        bonus_kopeks: Сумма бонуса в копейках
        referral_name: Имя реферала
    """
    # Handle email-only users via notification delivery service
    if telegram_id is None:
        if user is not None:
            success = await notification_delivery_service.notify_referral_bonus(
                user=user,
                bonus_kopeks=bonus_kopeks,
                referral_name=referral_name,
                telegram_message=message,
            )
            if success:
                logger.info('✅ Email уведомление о реферале отправлено пользователю', user_id=user.id)
            else:
                logger.warning('⚠️ Не удалось отправить email уведомление пользователю', user_id=user.id)
        else:
            logger.debug('Пропуск уведомления: пользователь без telegram_id и без User object')
        return

    try:
        target_bot = await resolve_bot_for_user(
            user=user,
            telegram_id=telegram_id,
            fallback_bot=bot,
        )
        if target_bot is None:
            logger.warning('Не удалось определить бота для реферального уведомления', telegram_id=telegram_id)
            return

        await target_bot.send_message(telegram_id, message, parse_mode='HTML')
        logger.info('✅ Уведомление отправлено пользователю', telegram_id=telegram_id)
    except Exception as e:
        logger.error('❌ Ошибка отправки уведомления пользователю', telegram_id=telegram_id, error=e)


async def process_referral_registration(db: AsyncSession, new_user_id: int, referrer_id: int, bot: Bot = None):
    try:
        if new_user_id == referrer_id:
            logger.warning('Self-referral blocked in process_referral_registration', user_id=new_user_id)
            return False

        new_user = await get_user_by_id(db, new_user_id)
        referrer = await get_user_by_id(db, referrer_id)

        if not new_user or not referrer:
            logger.error(
                'Пользователи не найдены: new_user_id=, referrer_id', new_user_id=new_user_id, referrer_id=referrer_id
            )
            return False

        if new_user.referred_by_id != referrer_id:
            logger.error('Пользователь не привязан к рефереру', new_user_id=new_user_id, referrer_id=referrer_id)
            return False

        campaign_id = await get_user_campaign_id(db, new_user_id)
        await create_referral_earning(
            db=db,
            user_id=referrer_id,
            referral_id=new_user_id,
            amount_kopeks=0,
            reason='referral_registration_pending',
            campaign_id=campaign_id,
        )

        try:
            from app.services.referral_contest_service import referral_contest_service

            await referral_contest_service.on_referral_registration(db, new_user_id)
        except Exception as exc:
            logger.debug('Не удалось записать конкурсную регистрацию', exc=exc)

        if bot:
            commission_percent = get_effective_referral_commission_percent(referrer)
            referral_notification = (
                f'🎉 <b>Добро пожаловать!</b>\n\n'
                f'Вы перешли по реферальной ссылке пользователя <b>{referrer.full_name}</b>!'
            )
            if settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS > 0:
                referral_notification += (
                    f'\n\n💰 При первом пополнении от {settings.format_price(settings.REFERRAL_MINIMUM_TOPUP_KOPEKS)} '
                    f'вы получите бонус {settings.format_price(settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS)}!'
                )
            await send_referral_notification(bot, new_user.telegram_id, referral_notification, user=new_user)

            inviter_notification = (
                f'👥 <b>Новый реферал!</b>\n\n'
                f'По вашей ссылке зарегистрировался пользователь <b>{new_user.full_name}</b>!\n\n'
                f'💰 Когда он пополнит баланс от {settings.format_price(settings.REFERRAL_MINIMUM_TOPUP_KOPEKS)}, '
            )
            if settings.REFERRAL_INVITER_BONUS_KOPEKS > 0:
                inviter_notification += (
                    f'вы получите минимум {settings.format_price(settings.REFERRAL_INVITER_BONUS_KOPEKS)} или '
                    f'{commission_percent}% от суммы (что больше).\n\n'
                )
            else:
                inviter_notification += f'вы получите {commission_percent}% от суммы.\n\n'
            inviter_notification += (
                f'📈 С каждого последующего пополнения вы будете получать {commission_percent}% комиссии.'
            )
            await send_referral_notification(
                bot, referrer.telegram_id, inviter_notification, user=referrer, referral_name=new_user.full_name
            )

        logger.info(
            '✅ Зарегистрирован реферал для . Бонусы будут выданы после пополнения.',
            new_user_id=new_user_id,
            referrer_id=referrer_id,
        )
        return True

    except Exception as e:
        logger.error('Ошибка обработки реферальной регистрации', error=e)
        return False


async def process_referral_topup(db: AsyncSession, user_id: int, topup_amount_kopeks: int, bot: Bot = None):
    try:
        user = await get_user_by_id(db, user_id)
        if not user or not user.referred_by_id:
            logger.debug('Пользователь не является рефералом, пропуск комиссии', user_id=user_id)
            return True

        referrer = await get_user_by_id(db, user.referred_by_id)
        if not referrer:
            logger.error(
                'Реферер не найден, комиссия не начислена', referred_by_id=user.referred_by_id, user_id=user_id
            )
            return False

        campaign_id = await get_user_campaign_id(db, user.id)
        commission_percent = get_effective_referral_commission_percent(referrer)

        logger.info(
            'Обработка реферального пополнения',
            user_id=user_id,
            referrer_id=referrer.id,
            topup_amount_kopeks=topup_amount_kopeks,
            campaign_id=campaign_id,
            commission_percent=commission_percent,
            has_made_first_topup=user.has_made_first_topup,
        )
        qualifies_for_first_bonus = topup_amount_kopeks >= settings.REFERRAL_MINIMUM_TOPUP_KOPEKS
        commission_amount = 0
        if commission_percent > 0:
            commission_amount = int(topup_amount_kopeks * commission_percent / 100)

        if not user.has_made_first_topup:
            if not qualifies_for_first_bonus:
                logger.info(
                    'Пополнение на ₽ меньше минимума для первого бонуса, но комиссия будет начислена',
                    user_id=user_id,
                    topup_amount_kopeks=topup_amount_kopeks / 100,
                )

                if commission_amount > 0 and await _is_commission_limit_reached(db, referrer.id, user.id):
                    return True

                if commission_amount > 0:
                    balance_ok = await add_user_balance(
                        db,
                        referrer,
                        commission_amount,
                        f'Комиссия {commission_percent}% с пополнения {user.full_name}',
                        transaction_type=TransactionType.REFERRAL_REWARD,
                        bot=bot,
                    )

                    if balance_ok:
                        await create_referral_earning(
                            db=db,
                            user_id=referrer.id,
                            referral_id=user.id,
                            amount_kopeks=commission_amount,
                            reason='referral_commission_topup',
                            campaign_id=campaign_id,
                        )

                        logger.info(
                            '💰 Комиссия с пополнения: получил ₽ (до первого бонуса)',
                            telegram_id=referrer.telegram_id,
                            commission_amount=commission_amount / 100,
                        )

                        if bot:
                            commission_notification = (
                                f'💰 <b>Реферальная комиссия!</b>\n\n'
                                f'Ваш реферал <b>{user.full_name}</b> пополнил баланс на '
                                f'{settings.format_price(topup_amount_kopeks)}\n\n'
                                f'🎁 Ваша комиссия ({commission_percent}%): '
                                f'{settings.format_price(commission_amount)}\n\n'
                                f'💎 Средства зачислены на ваш баланс.'
                            )
                            await send_referral_notification(
                                bot,
                                referrer.telegram_id,
                                commission_notification,
                                user=referrer,
                                bonus_kopeks=commission_amount,
                                referral_name=user.full_name,
                            )
                    else:
                        logger.error(
                            'Не удалось начислить комиссию на баланс, ReferralEarning не создан',
                            referrer_id=referrer.id,
                            commission_amount=commission_amount,
                        )

                return True

            user.has_made_first_topup = True
            await db.commit()

            try:
                await db.execute(
                    delete(ReferralEarning).where(
                        ReferralEarning.user_id == referrer.id,
                        ReferralEarning.referral_id == user.id,
                        ReferralEarning.reason == 'referral_registration_pending',
                    )
                )
                await db.commit()
                logger.info("🗑️ Удалена запись 'ожидание пополнения' для реферала", user_id=user.id)
            except Exception as e:
                logger.error('Ошибка удаления записи ожидания', error=e)

            if settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS > 0:
                bonus_ok = await add_user_balance(
                    db,
                    user,
                    settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS,
                    'Бонус за первое пополнение по реферальной программе',
                    transaction_type=TransactionType.REFERRAL_REWARD,
                    bot=bot,
                )
                if bonus_ok:
                    logger.info(
                        '💰 Реферал получил бонус ₽',
                        user_id=user.id,
                        REFERRAL_FIRST_TOPUP_BONUS_KOPEKS=settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS / 100,
                    )

                    if bot:
                        bonus_notification = (
                            f'🎉 <b>Бонус получен!</b>\n\n'
                            f'За первое пополнение вы получили бонус '
                            f'{settings.format_price(settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS)}!\n\n'
                            f'💎 Средства зачислены на ваш баланс.'
                        )
                        await send_referral_notification(
                            bot,
                            user.telegram_id,
                            bonus_notification,
                            user=user,
                            bonus_kopeks=settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS,
                        )
                else:
                    logger.error(
                        'Не удалось начислить бонус за первое пополнение',
                        user_id=user.id,
                        bonus_kopeks=settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS,
                    )

            commission_amount = int(topup_amount_kopeks * commission_percent / 100)
            inviter_bonus = max(settings.REFERRAL_INVITER_BONUS_KOPEKS, commission_amount)

            if inviter_bonus > 0:
                balance_ok = await add_user_balance(
                    db,
                    referrer,
                    inviter_bonus,
                    f'Бонус за первое пополнение реферала {user.full_name}',
                    transaction_type=TransactionType.REFERRAL_REWARD,
                    bot=bot,
                )

                if balance_ok:
                    await create_referral_earning(
                        db=db,
                        user_id=referrer.id,
                        referral_id=user.id,
                        amount_kopeks=inviter_bonus,
                        reason='referral_first_topup',
                        campaign_id=campaign_id,
                    )

                    referrer_id = referrer.telegram_id or referrer.email or f'user#{referrer.id}'
                    logger.info(
                        '💰 Реферер получил бонус ₽', referrer_id=referrer_id, inviter_bonus=inviter_bonus / 100
                    )

                    if bot:
                        inviter_bonus_notification = (
                            f'💰 <b>Реферальная награда!</b>\n\n'
                            f'Ваш реферал <b>{user.full_name}</b> сделал первое пополнение!\n\n'
                            f'🎁 Вы получили награду: {settings.format_price(inviter_bonus)}\n\n'
                            f'📈 Теперь с каждого его пополнения вы будете получать {commission_percent}% комиссии.'
                        )
                        await send_referral_notification(
                            bot,
                            referrer.telegram_id,
                            inviter_bonus_notification,
                            user=referrer,
                            bonus_kopeks=inviter_bonus,
                            referral_name=user.full_name,
                        )
                else:
                    logger.error(
                        'Не удалось начислить бонус на баланс, ReferralEarning не создан',
                        referrer_id=referrer.id,
                        inviter_bonus=inviter_bonus,
                    )

        elif commission_amount > 0:
            if await _is_commission_limit_reached(db, referrer.id, user.id):
                return True

            balance_ok = await add_user_balance(
                db,
                referrer,
                commission_amount,
                f'Комиссия {commission_percent}% с пополнения {user.full_name}',
                transaction_type=TransactionType.REFERRAL_REWARD,
                bot=bot,
            )

            if balance_ok:
                await create_referral_earning(
                    db=db,
                    user_id=referrer.id,
                    referral_id=user.id,
                    amount_kopeks=commission_amount,
                    reason='referral_commission_topup',
                    campaign_id=campaign_id,
                )

                referrer_id = referrer.telegram_id or referrer.email or f'user#{referrer.id}'
                logger.info(
                    '💰 Комиссия с пополнения: получил ₽',
                    referrer_id=referrer_id,
                    commission_amount=commission_amount / 100,
                )

                if bot:
                    commission_notification = (
                        f'💰 <b>Реферальная комиссия!</b>\n\n'
                        f'Ваш реферал <b>{user.full_name}</b> пополнил баланс на '
                        f'{settings.format_price(topup_amount_kopeks)}\n\n'
                        f'🎁 Ваша комиссия ({commission_percent}%): '
                        f'{settings.format_price(commission_amount)}\n\n'
                        f'💎 Средства зачислены на ваш баланс.'
                    )
                    await send_referral_notification(
                        bot,
                        referrer.telegram_id,
                        commission_notification,
                        user=referrer,
                        bonus_kopeks=commission_amount,
                        referral_name=user.full_name,
                    )
            else:
                logger.error(
                    'Не удалось начислить комиссию на баланс, ReferralEarning не создан',
                    referrer_id=referrer.id,
                    commission_amount=commission_amount,
                )

        return True

    except Exception as e:
        logger.error('Ошибка обработки пополнения реферала', error=e)
        return False


async def process_referral_purchase(
    db: AsyncSession, user_id: int, purchase_amount_kopeks: int, transaction_id: int = None, bot: Bot = None
):
    """Process referral commission for balance-based subscription purchases.

    INTENTIONALLY UNUSED. This function is NOT called from subscription purchase flows.
    Commission is only earned when referred users make actual payments through payment
    providers (via process_referral_topup). Balance-based subscription purchases
    (from admin credits, campaign bonuses, or promo codes) do NOT trigger commission,
    because the partner already received commission at the time the user topped up
    their balance. Calling this would cause double-commission.

    Kept for potential future use cases where balance-independent purchase tracking
    is needed (e.g. audit trail records with zero commission).
    """
    try:
        user = await get_user_by_id(db, user_id)
        if not user or not user.referred_by_id:
            return True

        referrer = await get_user_by_id(db, user.referred_by_id)
        if not referrer:
            logger.error('Реферер не найден', referred_by_id=user.referred_by_id)
            return False

        commission_percent = get_effective_referral_commission_percent(referrer)

        commission_amount = int(purchase_amount_kopeks * commission_percent / 100)

        if commission_amount > 0:
            await add_user_balance(
                db, referrer, commission_amount, f'Комиссия {commission_percent}% с покупки {user.full_name}', bot=bot
            )

            campaign_id = await get_user_campaign_id(db, user.id)
            await create_referral_earning(
                db=db,
                user_id=referrer.id,
                referral_id=user.id,
                amount_kopeks=commission_amount,
                reason='referral_commission',
                referral_transaction_id=transaction_id,
                campaign_id=campaign_id,
            )

            referrer_id = referrer.telegram_id or referrer.email or f'user#{referrer.id}'
            logger.info(
                '💰 Комиссия с покупки: получил ₽', referrer_id=referrer_id, commission_amount=commission_amount / 100
            )

            if bot:
                purchase_commission_notification = (
                    f'💰 <b>Комиссия с покупки!</b>\n\n'
                    f'Ваш реферал <b>{user.full_name}</b> совершил покупку на '
                    f'{settings.format_price(purchase_amount_kopeks)}\n\n'
                    f'🎁 Ваша комиссия ({commission_percent}%): '
                    f'{settings.format_price(commission_amount)}\n\n'
                    f'💎 Средства зачислены на ваш баланс.'
                )
                await send_referral_notification(
                    bot,
                    referrer.telegram_id,
                    purchase_commission_notification,
                    user=referrer,
                    bonus_kopeks=commission_amount,
                    referral_name=user.full_name,
                )

        if not user.has_had_paid_subscription:
            user.has_had_paid_subscription = True
            await db.commit()
            logger.info('✅ Пользователь отмечен как имевший платную подписку', user_id=user_id)

        return True

    except Exception as e:
        logger.error('Ошибка обработки покупки реферала', error=e)
        import traceback

        logger.error('Полный traceback', format_exc=traceback.format_exc())
        return False
