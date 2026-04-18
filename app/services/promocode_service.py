from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.promo_group import get_promo_group_by_id
from app.database.crud.promocode import (
    check_user_promocode_usage,
    create_promocode_use,
    get_active_discount_promocode_for_user,
    get_promocode_by_code,
)
from app.database.crud.subscription import extend_subscription, get_subscription_by_user_id
from app.database.crud.user import add_user_balance, get_user_by_id
from app.database.crud.user_promo_group import add_user_to_promo_group, has_user_promo_group
from app.database.models import PromoCode, PromoCodeType, SubscriptionStatus, User
from app.services.remnawave_service import RemnaWaveService
from app.services.subscription_service import SubscriptionService


logger = structlog.get_logger(__name__)


class _SelectSubscriptionRequired(Exception):
    """Raised when multi-tariff promo requires user to select a subscription."""

    def __init__(self, eligible_subscriptions: list[dict], code: str):
        self.eligible_subscriptions = eligible_subscriptions
        self.code = code
        super().__init__('select_subscription')


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

    async def activate_promocode(
        self, db: AsyncSession, user_id: int, code: str, *, subscription_id: int | None = None
    ) -> dict[str, Any]:
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
                if not promocode.is_active:
                    return {'success': False, 'error': 'inactive'}
                from app.database.models import _aware

                now = datetime.now(UTC)
                aware_from = _aware(promocode.valid_from)
                if aware_from is not None and aware_from > now:
                    return {'success': False, 'error': 'not_yet_valid'}
                return {'success': False, 'error': 'expired'}

            existing_use = await check_user_promocode_usage(db, user_id, promocode.id)
            if existing_use:
                return {'success': False, 'error': 'already_used_by_user'}

            # Лимит на количество активаций за день (анти-стакинг)
            from app.database.crud.promocode import count_user_recent_activations

            recent_count = await count_user_recent_activations(db, user_id, hours=24)
            if recent_count >= 5:
                logger.warning(
                    'Promo stacking limit: user has activations in 24h',
                    _format_user_log=self._format_user_log(user),
                    recent_count=recent_count,
                )
                return {'success': False, 'error': 'daily_limit'}

            # Проверка "только для первой покупки"
            if getattr(promocode, 'first_purchase_only', False):
                if getattr(user, 'has_had_paid_subscription', False):
                    return {'success': False, 'error': 'not_first_purchase'}

            balance_before_kopeks = user.balance_kopeks

            # Резервируем запись использования ДО применения эффектов (защита от race condition)
            promo_use = await create_promocode_use(db, promocode.id, user_id)
            if promo_use is None:
                return {'success': False, 'error': 'already_used_by_user'}

            try:
                result_description = await self._apply_promocode_effects(
                    db, user, promocode, subscription_id=subscription_id
                )
            except _SelectSubscriptionRequired as e:
                # Мульти-тариф: нужен выбор подписки — откатываем использование и коммитим
                await db.delete(promo_use)
                await db.commit()
                return {
                    'success': False,
                    'error': 'select_subscription',
                    'eligible_subscriptions': e.eligible_subscriptions,
                    'code': e.code,
                }
            except ValueError as e:
                # Эффекты не применены — удаляем зарезервированную запись использования и коммитим
                await db.delete(promo_use)
                await db.commit()
                error_key = str(e)
                if error_key in (
                    'active_discount_exists',
                    'no_subscription_for_days',
                    'subscription_not_found',
                ):
                    return {'success': False, 'error': error_key}
                raise
            balance_after_kopeks = user.balance_kopeks

            if promocode.type == PromoCodeType.SUBSCRIPTION_DAYS.value and promocode.subscription_days > 0:
                from app.utils.user_utils import mark_user_as_had_paid_subscription

                await mark_user_as_had_paid_subscription(db, user)

                logger.info(
                    '🎯 Пользователь получил платную подписку через промокод',
                    _format_user_log=self._format_user_log(user),
                    code=code,
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
                                db, user_id, promocode.promo_group_id, assigned_by='promocode', commit=False
                            )

                            logger.info(
                                '🎯 Пользователю назначена промогруппа (приоритет: ) через промокод',
                                _format_user_log=self._format_user_log(user),
                                promo_group_name=promo_group.name,
                                priority=promo_group.priority,
                                code=code,
                            )

                            # Add to result description
                            result_description += f'\n🎁 Назначена промогруппа: {promo_group.name}'
                        else:
                            logger.warning(
                                '⚠️ Промогруппа ID не найдена для промокода',
                                promo_group_id=promocode.promo_group_id,
                                code=code,
                            )
                    else:
                        logger.info(
                            'ℹ️ Пользователь уже имеет промогруппу ID',
                            _format_user_log=self._format_user_log(user),
                            promo_group_id=promocode.promo_group_id,
                        )
                except Exception as pg_error:
                    logger.error(
                        '❌ Ошибка назначения промогруппы для пользователя при активации промокода',
                        _format_user_log=self._format_user_log(user),
                        code=code,
                        pg_error=pg_error,
                    )
                    # Don't fail the whole promocode activation if promo group assignment fails

            from sqlalchemy import update as sql_update

            await db.execute(
                sql_update(PromoCode)
                .where(PromoCode.id == promocode.id)
                .values(current_uses=PromoCode.current_uses + 1)
            )
            await db.commit()

            logger.info('✅ Пользователь активировал промокод', _format_user_log=self._format_user_log(user), code=code)

            promocode_data = {
                'code': promocode.code,
                'type': promocode.type,
                'balance_bonus_kopeks': promocode.balance_bonus_kopeks,
                'subscription_days': promocode.subscription_days,
                'max_uses': promocode.max_uses,
                'current_uses': promocode.current_uses + 1,  # +1 because we just incremented atomically
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
            logger.error('Ошибка активации промокода для пользователя', code=code, user_id=user_id, error=e)
            await db.rollback()
            return {'success': False, 'error': 'server_error'}

    async def _apply_promocode_effects(
        self, db: AsyncSession, user: User, promocode: PromoCode, *, subscription_id: int | None = None
    ) -> str:
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
            # Проверка на наличие активной скидки
            current_discount = getattr(user, 'promo_offer_discount_percent', 0) or 0
            expires_at = getattr(user, 'promo_offer_discount_expires_at', None)

            # Если есть активная скидка (процент > 0 и срок не истек)
            if current_discount > 0:
                if expires_at is None or expires_at > datetime.now(UTC):
                    logger.warning(
                        '⚠️ Пользователь попытался активировать промокод но у него уже есть активная скидка до',
                        _format_user_log=self._format_user_log(user),
                        code=promocode.code,
                        current_discount=current_discount,
                        expires_at=expires_at,
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
                user.promo_offer_discount_expires_at = datetime.now(UTC) + timedelta(hours=discount_hours)
                effects.append(f'💸 Получена скидка {discount_percent}% (действует {discount_hours} ч.)')
            else:
                # 0 часов = бессрочно до первой покупки
                user.promo_offer_discount_expires_at = None
                effects.append(f'💸 Получена скидка {discount_percent}% до первой покупки')

            await db.flush()

            logger.info(
                '✅ Пользователю назначена скидка (срок: ч.) по промокоду',
                _format_user_log=self._format_user_log(user),
                discount_percent=discount_percent,
                discount_hours=discount_hours,
                code=promocode.code,
            )

        if promocode.type == PromoCodeType.BALANCE.value and promocode.balance_bonus_kopeks > 0:
            await add_user_balance(db, user, promocode.balance_bonus_kopeks, f'Бонус по промокоду {promocode.code}')

            balance_bonus_rubles = promocode.balance_bonus_kopeks / 100
            effects.append(f'💰 Баланс пополнен на {balance_bonus_rubles}₽')

        if promocode.type == PromoCodeType.SUBSCRIPTION_DAYS.value and promocode.subscription_days > 0:
            if settings.is_multi_tariff_enabled():
                from app.database.crud.subscription import get_active_subscriptions_by_user_id

                active_subs = await get_active_subscriptions_by_user_id(db, user.id)
            else:
                single_sub = await get_subscription_by_user_id(db, user.id)
                active_subs = [single_sub] if single_sub else []

            if not active_subs:
                raise ValueError('no_subscription_for_days')

            # Multi-tariff: require subscription selection if >1 non-daily subscriptions
            non_daily = [s for s in active_subs if not (s.tariff and getattr(s.tariff, 'is_daily', False))]
            eligible = non_daily or active_subs

            if subscription_id:
                target_sub = next((s for s in eligible if s.id == subscription_id), None)
                if not target_sub:
                    raise ValueError('subscription_not_found')
            elif len(eligible) == 1:
                target_sub = eligible[0]
            elif len(eligible) > 1 and settings.is_multi_tariff_enabled():
                # Need user to choose — raise with eligible subscriptions list
                raise _SelectSubscriptionRequired(
                    eligible_subscriptions=[
                        {'id': s.id, 'tariff_name': s.tariff.name if s.tariff else f'#{s.id}', 'days_left': s.days_left}
                        for s in eligible
                    ],
                    code=promocode.code,
                )
            # Prefer non-daily subscription with most days remaining
            elif eligible:
                target_sub = max(eligible, key=lambda s: s.days_left)
            else:
                # eligible = non_daily or active_subs, active_subs is guaranteed non-empty (guard above)
                # This branch is unreachable, but defend against future changes
                raise ValueError('no_subscription_for_days')
            # Конвертация триала в платную подписку при активации промокода на дни
            if target_sub.is_trial:
                target_sub.is_trial = False
                if target_sub.status == SubscriptionStatus.TRIAL.value:
                    target_sub.status = SubscriptionStatus.ACTIVE.value
                target_sub.updated_at = datetime.now(UTC)
                logger.info(
                    '🎓 Промокод: конвертация триала в платную подписку',
                    subscription_id=target_sub.id,
                    code=promocode.code,
                )

            await extend_subscription(db, target_sub, promocode.subscription_days)
            await self.subscription_service.update_remnawave_user(db, target_sub)

            tariff_label = ''
            if settings.is_multi_tariff_enabled() and getattr(target_sub, 'tariff', None):
                tariff_label = f' «{target_sub.tariff.name}»'
            effects.append(f'⏰ Подписка{tariff_label} продлена на {promocode.subscription_days} дней')
            logger.info(
                '✅ Подписка пользователя продлена на дней в RemnaWave',
                _format_user_log=self._format_user_log(user),
                subscription_days=promocode.subscription_days,
                subscription_id=target_sub.id,
            )

        if promocode.type == PromoCodeType.TRIAL_SUBSCRIPTION.value:
            from app.database.crud.subscription import create_trial_subscription

            # Determine trial tariff — use promocode.tariff_id if set, else system default
            trial_tariff = None
            tariff_id_for_trial = None
            trial_traffic_limit = None
            trial_device_limit = None
            trial_squads: list[str] = []

            try:
                from app.database.crud.tariff import get_tariff_by_id as get_tariff, get_trial_tariff

                if promocode.tariff_id:
                    trial_tariff = await get_tariff(db, promocode.tariff_id)
                else:
                    trial_tariff = await get_trial_tariff(db)
                    if not trial_tariff:
                        trial_tariff_id = settings.get_trial_tariff_id()
                        if trial_tariff_id > 0:
                            trial_tariff = await get_tariff(db, trial_tariff_id)

                if trial_tariff:
                    trial_traffic_limit = trial_tariff.traffic_limit_gb
                    trial_device_limit = trial_tariff.device_limit
                    tariff_id_for_trial = trial_tariff.id
                    if trial_tariff.allowed_squads:
                        trial_squads = trial_tariff.allowed_squads
            except Exception as e:
                logger.error('Ошибка получения тарифа для триального промокода', error=e)

            # In multi-tariff mode a targeted trial promo may create a separate
            # subscription even if the user already owns this tariff.
            existing_same_tariff_sub = None
            can_create_new = True
            if settings.is_multi_tariff_enabled():
                from app.database.crud.subscription import get_active_subscriptions_by_user_id

                active_subs = await get_active_subscriptions_by_user_id(db, user.id)
                if not tariff_id_for_trial:
                    # No tariff configured — block if any subscription exists
                    can_create_new = len(active_subs) == 0
            else:
                existing_sub = await get_subscription_by_user_id(db, user.id)
                if existing_sub:
                    if tariff_id_for_trial and existing_sub.tariff_id == tariff_id_for_trial:
                        existing_same_tariff_sub = existing_sub
                    else:
                        can_create_new = False

            trial_days = (
                promocode.subscription_days if promocode.subscription_days > 0 else settings.TRIAL_DURATION_DAYS
            )
            # Override with tariff trial_duration_days if available
            tariff_trial_days = getattr(trial_tariff, 'trial_duration_days', None) if trial_tariff else None
            if tariff_trial_days and promocode.subscription_days <= 0:
                trial_days = tariff_trial_days

            if existing_same_tariff_sub:
                # User already has this tariff — extend it
                await extend_subscription(db, existing_same_tariff_sub, trial_days)
                await self.subscription_service.update_remnawave_user(db, existing_same_tariff_sub)

                effects.append(
                    f'⏰ Подписка «{trial_tariff.name if trial_tariff else ""}» продлена на {trial_days} дней'
                )
                logger.info(
                    '✅ Триал промокод: продлена существующая подписка',
                    _format_user_log=self._format_user_log(user),
                    trial_days=trial_days,
                    subscription_id=existing_same_tariff_sub.id,
                )
            elif can_create_new:
                if trial_device_limit is None and not settings.is_devices_selection_enabled():
                    trial_device_limit = settings.get_disabled_mode_device_limit()

                trial_subscription = await create_trial_subscription(
                    db,
                    user.id,
                    duration_days=trial_days,
                    traffic_limit_gb=trial_traffic_limit,
                    device_limit=trial_device_limit,
                    connected_squads=trial_squads or None,
                    tariff_id=tariff_id_for_trial,
                )

                await self.subscription_service.create_remnawave_user(db, trial_subscription)

                effects.append(f'🎁 Активирована тестовая подписка на {trial_days} дней')
                logger.info(
                    '✅ Создана триал подписка для пользователя на дней',
                    _format_user_log=self._format_user_log(user),
                    trial_days=trial_days,
                    tariff_id=tariff_id_for_trial,
                )
            else:
                effects.append('ℹ️ У вас уже есть активная подписка')

        return '\n'.join(effects) if effects else '✅ Промокод активирован'

    async def deactivate_discount_promocode(
        self,
        db: AsyncSession,
        user_id: int,
        *,
        admin_initiated: bool = False,
    ) -> dict[str, Any]:
        """
        Деактивирует активный промокод на процентную скидку у пользователя.

        Действия:
        - Сбрасывает promo_offer_discount_percent / source / expires_at на пользователе
        - Удаляет запись PromoCodeUse (чтобы промокод мог быть повторно использован, если max_uses > current_uses)
        - Декрементирует current_uses на промокоде
        - Если промокод назначил промогруппу -- снимает её с пользователя

        Args:
            db: Сессия БД
            user_id: ID пользователя
            admin_initiated: True если деактивацию инициировал админ

        Returns:
            dict с ключами success, error (опционально), deactivated_code (опционально)
        """
        try:
            user = await get_user_by_id(db, user_id)
            if not user:
                return {'success': False, 'error': 'user_not_found'}

            current_discount = getattr(user, 'promo_offer_discount_percent', 0) or 0
            source = getattr(user, 'promo_offer_discount_source', None)

            if current_discount <= 0 or not source or not source.startswith('promocode:'):
                return {'success': False, 'error': 'no_active_discount_promocode'}

            expires_at = getattr(user, 'promo_offer_discount_expires_at', None)
            # Если скидка уже истекла по времени -- тоже нечего деактивировать
            if expires_at is not None and expires_at <= datetime.now(UTC):
                # Просто зачистим протухшие данные
                user.promo_offer_discount_percent = 0
                user.promo_offer_discount_source = None
                user.promo_offer_discount_expires_at = None
                user.updated_at = datetime.now(UTC)
                await db.commit()
                return {'success': False, 'error': 'discount_already_expired'}

            promocode, promo_use = await get_active_discount_promocode_for_user(db, user_id)

            deactivated_code = source.split(':', 1)[1]

            # 1. Сбрасываем скидку на пользователе
            user.promo_offer_discount_percent = 0
            user.promo_offer_discount_source = None
            user.promo_offer_discount_expires_at = None
            user.updated_at = datetime.now(UTC)

            # 2. Откатываем использование промокода (если нашли запись)
            if promocode and promo_use:
                await db.delete(promo_use)
                if promocode.current_uses > 0:
                    promocode.current_uses -= 1
                    promocode.updated_at = datetime.now(UTC)

                # 3. Если промокод назначал промогруппу -- снимаем её
                if promocode.promo_group_id:
                    from app.database.crud.user_promo_group import (
                        has_user_promo_group,
                        remove_user_from_promo_group,
                    )

                    has_group = await has_user_promo_group(db, user_id, promocode.promo_group_id)
                    if has_group:
                        await remove_user_from_promo_group(db, user_id, promocode.promo_group_id, commit=False)
                        logger.info(
                            'Снята промогруппа ID у пользователя при деактивации промокода',
                            promo_group_id=promocode.promo_group_id,
                            _format_user_log=self._format_user_log(user),
                            deactivated_code=deactivated_code,
                        )

            await db.commit()

            initiator = 'администратором' if admin_initiated else 'пользователем'
            logger.info(
                'Промокод (скидка %) деактивирован для пользователя',
                deactivated_code=deactivated_code,
                current_discount=current_discount,
                initiator=initiator,
                _format_user_log=self._format_user_log(user),
            )

            return {
                'success': True,
                'deactivated_code': deactivated_code,
                'discount_percent': current_discount,
            }

        except Exception as e:
            logger.error('Ошибка деактивации промокода для пользователя', user_id=user_id, error=e)
            await db.rollback()
            return {'success': False, 'error': 'server_error'}
