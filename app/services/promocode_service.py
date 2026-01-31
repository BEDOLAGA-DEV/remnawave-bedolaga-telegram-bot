import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.promo_group import get_promo_group_by_id
from app.database.crud.promocode import (
    check_user_promocode_usage,
    create_promocode_use,
    get_promocode_by_code,
)
from app.database.crud.subscription import extend_subscription, get_subscription_by_user_id
from app.database.crud.user import add_user_balance, get_user_by_id
from app.database.crud.user_promo_group import add_user_to_promo_group, has_user_promo_group
from app.database.models import PromoCode, PromoCodeType, User
from app.services.remnawave_service import RemnaWaveService
from app.services.subscription_service import SubscriptionService


logger = logging.getLogger(__name__)


class PromoCodeService:
    def __init__(self):
        self.remnawave_service = RemnaWaveService()
        self.subscription_service = SubscriptionService()

    @staticmethod
    def _format_user_log(user: User) -> str:
        """Форматирует идентификатор пользователя для логов (поддержка email-only users)."""
        if user.telegram_id:
            return str(user.telegram_id)
        if user.email:
            return f'{user.id} ({user.email})'
        return f'#{user.id}'

    async def activate_promocode(self, db: AsyncSession, user_id: int, code: str) -> dict[str, Any]:
        try:
            user = await get_user_by_id(db, user_id)
            if not user:
                return {'success': False, 'error': 'user_not_found'}

            promocode = await get_promocode_by_code(db, code)
            if not promocode:
                return {'success': False, 'error': 'not_found'}

            if not promocode.is_valid:
                if promocode.current_uses >= promocode.max_uses:
                    return {'success': False, 'error': 'used'}
                return {'success': False, 'error': 'expired'}

            existing_use = await check_user_promocode_usage(db, user_id, promocode.id)
            if existing_use:
                return {'success': False, 'error': 'already_used_by_user'}

            # Проверка "только для первой покупки"
            if getattr(promocode, 'first_purchase_only', False):
                if getattr(user, 'has_had_paid_subscription', False):
                    return {'success': False, 'error': 'not_first_purchase'}

            balance_before_kopeks = user.balance_kopeks

            try:
                result_description = await self._apply_promocode_effects(db, user, promocode)
            except ValueError as e:
                if str(e) == 'active_discount_exists':
                    return {'success': False, 'error': 'active_discount_exists'}
                raise
            balance_after_kopeks = user.balance_kopeks

            if promocode.type == PromoCodeType.SUBSCRIPTION_DAYS.value and promocode.subscription_days > 0:
                from app.utils.user_utils import mark_user_as_had_paid_subscription

                await mark_user_as_had_paid_subscription(db, user)

                logger.info(
                    f'🎯 Пользователь {self._format_user_log(user)} получил платную подписку через промокод {code}'
                )

            # Assign promo group if promocode has one
            if promocode.promo_group_id:
                try:
                    # Check if user already has this promo group
                    has_group = await has_user_promo_group(db, user_id, promocode.promo_group_id)

                    if not has_group:
                        # Get promo group details
                        promo_group = await get_promo_group_by_id(db, promocode.promo_group_id)

                        if promo_group:
                            # Add promo group to user
                            await add_user_to_promo_group(
                                db, user_id, promocode.promo_group_id, assigned_by='promocode'
                            )

                            logger.info(
                                f"🎯 Пользователю {self._format_user_log(user)} назначена промогруппа '{promo_group.name}' "
                                f'(приоритет: {promo_group.priority}) через промокод {code}'
                            )

                            # Add to result description
                            result_description += f'\n🎁 Назначена промогруппа: {promo_group.name}'
                        else:
                            logger.warning(
                                f'⚠️ Промогруппа ID {promocode.promo_group_id} не найдена для промокода {code}'
                            )
                    else:
                        logger.info(
                            f'ℹ️ Пользователь {self._format_user_log(user)} уже имеет промогруппу ID {promocode.promo_group_id}'
                        )
                except Exception as pg_error:
                    logger.error(
                        f'❌ Ошибка назначения промогруппы для пользователя {self._format_user_log(user)} '
                        f'при активации промокода {code}: {pg_error}'
                    )
                    # Don't fail the whole promocode activation if promo group assignment fails

            await create_promocode_use(db, promocode.id, user_id)

            promocode.current_uses += 1
            await db.commit()

            logger.info(f'✅ Пользователь {self._format_user_log(user)} активировал промокод {code}')

            promocode_data = {
                'code': promocode.code,
                'type': promocode.type,
                'balance_bonus_kopeks': promocode.balance_bonus_kopeks,
                'subscription_days': promocode.subscription_days,
                'max_uses': promocode.max_uses,
                'current_uses': promocode.current_uses,
                'valid_until': promocode.valid_until,
                'promo_group_id': promocode.promo_group_id,
            }

            return {
                'success': True,
                'description': result_description,
                'promocode': promocode_data,
                'balance_before_kopeks': balance_before_kopeks,
                'balance_after_kopeks': balance_after_kopeks,
            }

        except Exception as e:
            logger.error(f'Ошибка активации промокода {code} для пользователя {user_id}: {e}')
            await db.rollback()
            return {'success': False, 'error': 'server_error'}

    async def _apply_promocode_effects(self, db: AsyncSession, user: User, promocode: PromoCode) -> str:
        """
        Применяет эффекты промокода к пользователю.

        Args:
            db: Сессия базы данных
            user: Пользователь
            promocode: Промокод

        Returns:
            Описание примененных эффектов

        Raises:
            ValueError: Если у пользователя уже есть активная скидка (для DISCOUNT типа)
        """
        effects = []

        # Обработка DISCOUNT типа (одноразовая скидка)
        if promocode.type == PromoCodeType.DISCOUNT.value:
            from datetime import datetime, timedelta

            # Проверка на наличие активной скидки
            current_discount = getattr(user, 'promo_offer_discount_percent', 0) or 0
            expires_at = getattr(user, 'promo_offer_discount_expires_at', None)

            # Если есть активная скидка (процент > 0 и срок не истек)
            if current_discount > 0:
                if expires_at is None or expires_at > datetime.utcnow():
                    logger.warning(
                        f'⚠️ Пользователь {self._format_user_log(user)} попытался активировать промокод {promocode.code}, '
                        f'но у него уже есть активная скидка {current_discount}% до {expires_at}'
                    )
                    raise ValueError('active_discount_exists')

            # balance_bonus_kopeks хранит процент скидки (1-100)
            discount_percent = promocode.balance_bonus_kopeks
            # subscription_days хранит срок действия скидки в часах (0 = бессрочно до первой покупки)
            discount_hours = promocode.subscription_days

            # Устанавливаем процент скидки
            user.promo_offer_discount_percent = discount_percent
            user.promo_offer_discount_source = f'promocode:{promocode.code}'

            # Устанавливаем срок действия скидки
            if discount_hours > 0:
                user.promo_offer_discount_expires_at = datetime.utcnow() + timedelta(hours=discount_hours)
                effects.append(f'💸 Получена скидка {discount_percent}% (действует {discount_hours} ч.)')
            else:
                # 0 часов = бессрочно до первой покупки
                user.promo_offer_discount_expires_at = None
                effects.append(f'💸 Получена скидка {discount_percent}% до первой покупки')

            await db.flush()

            logger.info(
                f'✅ Пользователю {self._format_user_log(user)} назначена скидка {discount_percent}% '
                f'(срок: {discount_hours} ч.) по промокоду {promocode.code}'
            )

        if promocode.type == PromoCodeType.BALANCE.value and promocode.balance_bonus_kopeks > 0:
            await add_user_balance(db, user, promocode.balance_bonus_kopeks, f'Бонус по промокоду {promocode.code}')

            balance_bonus_rubles = promocode.balance_bonus_kopeks / 100
            effects.append(f'💰 Баланс пополнен на {balance_bonus_rubles}₽')

        if promocode.type == PromoCodeType.SUBSCRIPTION_DAYS.value and promocode.subscription_days > 0:
            from app.config import settings

            subscription = await get_subscription_by_user_id(db, user.id)

            if subscription:
                await extend_subscription(db, subscription, promocode.subscription_days)

                await self.subscription_service.update_remnawave_user(db, subscription)

                effects.append(f'⏰ Подписка продлена на {promocode.subscription_days} дней')
                logger.info(
                    f'✅ Подписка пользователя {self._format_user_log(user)} продлена на {promocode.subscription_days} дней в RemnaWave с текущими сквадами'
                )

            else:
                from app.database.crud.subscription import create_paid_subscription

                trial_squads = []
                try:
                    from app.database.crud.server_squad import get_random_trial_squad_uuid

                    trial_uuid = await get_random_trial_squad_uuid(db)
                    if trial_uuid:
                        trial_squads = [trial_uuid]
                except Exception as error:
                    logger.error(
                        'Не удалось подобрать сквад для подписки по промокоду %s: %s',
                        promocode.code,
                        error,
                    )

                forced_devices = None
                if not settings.is_devices_selection_enabled():
                    forced_devices = settings.get_disabled_mode_device_limit()

                device_limit = settings.DEFAULT_DEVICE_LIMIT
                if forced_devices is not None:
                    device_limit = forced_devices

                new_subscription = await create_paid_subscription(
                    db=db,
                    user_id=user.id,
                    duration_days=promocode.subscription_days,
                    traffic_limit_gb=0,
                    device_limit=device_limit,
                    connected_squads=trial_squads,
                    update_server_counters=True,
                )

                try:
                    await self.subscription_service.create_remnawave_user(db, new_subscription)
                except Exception as rw_error:
                    logger.error(
                        '❌ Ошибка создания RemnaWave для промокода подписки пользователя %s: %s',
                        self._format_user_log(user),
                        rw_error,
                    )
                    # Удаляем созданную подписку, так как VPN не будет работать
                    await db.delete(new_subscription)
                    await db.commit()
                    raise ValueError(f'Не удалось активировать подписку в VPN-системе: {rw_error}') from rw_error

                effects.append(f'🎉 Получена подписка на {promocode.subscription_days} дней')
                logger.info(
                    f'✅ Создана новая подписка для пользователя {self._format_user_log(user)} на {promocode.subscription_days} дней с триал сквадом {trial_squads}'
                )

        if promocode.type == PromoCodeType.TRIAL_SUBSCRIPTION.value:
            from app.config import settings
            from app.database.crud.subscription import create_trial_subscription

            subscription = await get_subscription_by_user_id(db, user.id)

            if not subscription:
                trial_days = (
                    promocode.subscription_days if promocode.subscription_days > 0 else settings.TRIAL_DURATION_DAYS
                )

                forced_devices = None
                if not settings.is_devices_selection_enabled():
                    forced_devices = settings.get_disabled_mode_device_limit()

                trial_subscription = await create_trial_subscription(
                    db,
                    user.id,
                    duration_days=trial_days,
                    device_limit=forced_devices,
                )

                try:
                    await self.subscription_service.create_remnawave_user(db, trial_subscription)
                except Exception as rw_error:
                    logger.error(
                        '❌ Ошибка создания RemnaWave для триал промокода пользователя %s: %s',
                        self._format_user_log(user),
                        rw_error,
                    )
                    # Удаляем созданную подписку, так как VPN не будет работать
                    await db.delete(trial_subscription)
                    await db.commit()
                    raise ValueError(f'Не удалось активировать подписку в VPN-системе: {rw_error}') from rw_error

                effects.append(f'🎁 Активирована тестовая подписка на {trial_days} дней')
                logger.info(
                    f'✅ Создана триал подписка для пользователя {self._format_user_log(user)} на {trial_days} дней'
                )
            else:
                effects.append('ℹ️ У вас уже есть активная подписка')

        return '\n'.join(effects) if effects else '✅ Промокод активирован'
