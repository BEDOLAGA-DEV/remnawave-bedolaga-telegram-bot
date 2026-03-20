from decimal import ROUND_HALF_UP, Decimal

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.user import get_user_by_telegram_id
from app.external.telegram_stars import TelegramStarsService
from app.localization.loader import DEFAULT_LANGUAGE
from app.localization.texts import get_texts
from app.services.payment_service import PaymentService


logger = structlog.get_logger(__name__)


async def _handle_wheel_spin_payment(
    message: types.Message,
    db: AsyncSession,
    user,
    stars_amount: int,
    payload: str,
    texts,
):
    """Обработка Stars платежа для колеса удачи."""
    from app.database.crud.wheel import get_or_create_wheel_config, get_wheel_prizes
    from app.services.wheel_service import wheel_service

    try:
        config = await get_or_create_wheel_config(db)

        if not config.is_enabled:
            await message.answer(
                '❌ Колесо удачи временно недоступно. Звезды будут возвращены.',
            )
            return False

        # Проверяем наличие активной подписки
        from app.database.crud.subscription import get_subscription_by_user_id

        subscription = await get_subscription_by_user_id(db, user.id)
        if not subscription or not subscription.is_active:
            # Конвертируем Stars в баланс как компенсацию
            rubles_fallback = TelegramStarsService.calculate_rubles_from_stars(stars_amount)
            kopeks_fallback = int((rubles_fallback * Decimal(100)).to_integral_value(rounding=ROUND_HALF_UP))
            from app.database.crud.user import add_user_balance
            from app.database.models import TransactionType

            await add_user_balance(
                db,
                user,
                kopeks_fallback,
                f'Возврат за спин колеса без подписки ({stars_amount} Stars)',
                transaction_type=TransactionType.REFUND,
            )
            await db.commit()
            await message.answer(
                '❌ Для использования колеса удачи необходима активная подписка.\n'
                f'💰 {stars_amount} Stars возвращены на баланс в виде {kopeks_fallback / 100:.0f} ₽.',
            )
            logger.warning(
                'Wheel spin without subscription, refunded to balance',
                user_id=user.id,
                stars_amount=stars_amount,
                refund_kopeks=kopeks_fallback,
            )
            return False

        # Выполняем спин напрямую (оплата уже прошла через Stars)
        prizes = await get_wheel_prizes(db, config.id, active_only=True)

        if not prizes:
            await message.answer(
                '❌ Призы не настроены. Обратитесь в поддержку.',
            )
            return False

        # Рассчитываем стоимость в копейках для статистики
        rubles_amount = TelegramStarsService.calculate_rubles_from_stars(stars_amount)
        payment_value_kopeks = int((rubles_amount * Decimal(100)).to_integral_value(rounding=ROUND_HALF_UP))

        # Рассчитываем вероятности и выбираем приз
        prizes_with_probs = wheel_service.calculate_prize_probabilities(config, prizes, payment_value_kopeks)
        selected_prize = wheel_service._select_prize(prizes_with_probs)

        # Применяем приз
        generated_promocode = await wheel_service._apply_prize(db, user, selected_prize, config)

        # Создаем запись спина
        from app.database.crud.wheel import create_wheel_spin
        from app.database.models import WheelSpinPaymentType

        promocode_id = None
        if generated_promocode:
            from sqlalchemy import text

            result = await db.execute(
                text('SELECT id FROM promocodes WHERE code = :code'), {'code': generated_promocode}
            )
            row = result.fetchone()
            if row:
                promocode_id = row[0]

        logger.info(
            '🎰 Creating wheel spin: user.id=, user.telegram_id=, prize',
            user_id=user.id,
            telegram_id=user.telegram_id,
            display_name=selected_prize.display_name,
        )

        spin = await create_wheel_spin(
            db=db,
            user_id=user.id,
            prize_id=selected_prize.id,
            payment_type=WheelSpinPaymentType.TELEGRAM_STARS.value,
            payment_amount=stars_amount,
            payment_value_kopeks=payment_value_kopeks,
            prize_type=selected_prize.prize_type,
            prize_value=selected_prize.prize_value,
            prize_display_name=selected_prize.display_name,
            prize_value_kopeks=selected_prize.prize_value_kopeks,
            generated_promocode_id=promocode_id,
            is_applied=True,
        )

        logger.info('🎰 Wheel spin created: spin.id=, spin.user_id', spin_id=spin.id, user_id=spin.user_id)

        # Ensure all changes are committed (subscription days, traffic GB, etc.)
        await db.commit()

        # Отправляем результат
        prize_message = wheel_service._get_prize_message(selected_prize, generated_promocode)

        emoji = selected_prize.emoji or '🎁'
        await message.answer(
            f'🎰 <b>Колесо удачи!</b>\n\n'
            f'{emoji} <b>{selected_prize.display_name}</b>\n\n'
            f'{prize_message}\n\n'
            f'⭐ Потрачено: {stars_amount} Stars',
            parse_mode='HTML',
        )

        logger.info(
            '🎰 Wheel spin via Stars: user=, prize=, stars',
            user_id=user.id,
            display_name=selected_prize.display_name,
            stars_amount=stars_amount,
        )
        return True

    except Exception as e:
        logger.error('Ошибка обработки wheel spin payment', error=e, exc_info=True)
        await message.answer(
            '❌ Произошла ошибка при обработке спина. Обратитесь в поддержку.',
        )
        return False


async def _handle_trial_payment(
    message: types.Message,
    db: AsyncSession,
    user,
    stars_amount: int,
    payload: str,
    texts,
):
    """Обработка Stars платежа для платного триала."""
    from app.database.crud.subscription import activate_pending_trial_subscription
    from app.database.crud.transaction import create_transaction
    from app.database.models import PaymentMethod, TransactionType
    from app.services.admin_notification_service import AdminNotificationService
    from app.services.subscription_service import SubscriptionService

    try:
        # Парсим payload: trial_{subscription_id}
        parts = payload.split('_')
        if len(parts) < 2:
            logger.error('Невалидный trial payload', payload=payload)
            await message.answer(
                '❌ Ошибка: неверный формат платежа. Обратитесь в поддержку.',
            )
            return False

        try:
            subscription_id = int(parts[1])
        except ValueError:
            logger.error('Невалидный subscription_id в trial payload', payload=payload)
            await message.answer(
                '❌ Ошибка: неверный ID подписки. Обратитесь в поддержку.',
            )
            return False

        # Рассчитываем стоимость в копейках
        rubles_amount = TelegramStarsService.calculate_rubles_from_stars(stars_amount)
        amount_kopeks = int((rubles_amount * Decimal(100)).to_integral_value(rounding=ROUND_HALF_UP))

        # Создаём транзакцию
        await create_transaction(
            db=db,
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=amount_kopeks,
            description=f'Оплата пробной подписки через Telegram Stars ({stars_amount} ⭐)',
            payment_method=PaymentMethod.TELEGRAM_STARS,
            external_id=f'trial_stars_{subscription_id}',
            is_completed=True,
        )

        # Активируем pending триальную подписку
        subscription = await activate_pending_trial_subscription(
            db=db,
            subscription_id=subscription_id,
            user_id=user.id,
        )

        if not subscription:
            logger.error(
                'Не удалось активировать триальную подписку для пользователя',
                subscription_id=subscription_id,
                user_id=user.id,
            )
            # Возвращаем деньги на баланс
            from app.database.crud.user import add_user_balance

            await add_user_balance(
                db,
                user,
                amount_kopeks,
                'Возврат за неудачную активацию триала',
                transaction_type=TransactionType.REFUND,
            )
            await message.answer(
                '❌ Не удалось активировать пробную подписку. Средства возвращены на баланс.',
            )
            return False

        # Создаем пользователя в RemnaWave
        subscription_service = SubscriptionService()
        try:
            await subscription_service.create_remnawave_user(db, subscription)
        except Exception as rw_error:
            logger.error('Ошибка создания пользователя RemnaWave для триала', rw_error=rw_error)
            # Не откатываем подписку, просто логируем - RemnaWave может быть временно недоступен

        await db.commit()
        await db.refresh(user)

        # Отправляем уведомление админам
        try:
            admin_notification_service = AdminNotificationService(message.bot)
            await admin_notification_service.send_trial_activation_notification(
                user=user,
                subscription=subscription,
                paid_amount=amount_kopeks,
                payment_method='Telegram Stars',
            )
        except Exception as admin_error:
            logger.warning('Ошибка отправки уведомления админам о триале', admin_error=admin_error)

        # Отправляем сообщение пользователю
        await message.answer(
            f'🎉 <b>Пробная подписка активирована!</b>\n\n'
            f'⭐ Потрачено: {stars_amount} Stars\n'
            f'📅 Период: {settings.TRIAL_DURATION_DAYS} дней\n'
            f'📱 Устройств: {subscription.device_limit}\n\n'
            f'Используйте меню для подключения к VPN.',
            parse_mode='HTML',
        )

        logger.info(
            '✅ Платный триал активирован через Stars: user=, subscription=, stars',
            user_id=user.id,
            subscription_id=subscription.id,
            stars_amount=stars_amount,
        )
        return True

    except Exception as e:
        logger.error('Ошибка обработки trial payment', error=e, exc_info=True)
        await message.answer(
            '❌ Произошла ошибка при активации пробной подписки. Обратитесь в поддержку.',
        )
        return False


_PURCHASE_TOKEN_RE = __import__('re').compile(r'^[A-Za-z0-9_\-]{10,100}$')


async def _handle_guest_purchase_payment(
    message: types.Message,
    db: AsyncSession,
    user,
    stars_amount: int,
    payload: str,
    telegram_payment_charge_id: str,
):
    """Обработка Stars платежа для гостевой покупки (подарочная подписка из кабинета)."""
    from app.database.crud.landing import get_purchase_by_token
    from app.services.payment.common import try_fulfill_guest_purchase

    try:
        purchase_token = payload[len('guest_purchase_') :]
        if not purchase_token or not _PURCHASE_TOKEN_RE.match(purchase_token):
            logger.error('Invalid purchase_token format in guest_purchase payload', payload=payload)
            await message.answer('❌ Ошибка: неверный формат платежа.')
            return

        # Verify Stars amount matches expected price (±5% tolerance for conversion rounding)
        existing = await get_purchase_by_token(db, purchase_token)
        if existing and existing.amount_kopeks:
            expected_stars = max(1, settings.rubles_to_stars(existing.amount_kopeks / 100))
            tolerance = max(1, round(expected_stars * 0.05))
            if abs(stars_amount - expected_stars) > tolerance:
                logger.error(
                    'Stars amount mismatch for guest purchase',
                    paid_stars=stars_amount,
                    expected_stars=expected_stars,
                    purchase_token_prefix=purchase_token[:5],
                )
                await message.answer('❌ Сумма оплаты не совпадает с ожидаемой.')
                return

        # Calculate kopeks from stars
        rubles_amount = TelegramStarsService.calculate_rubles_from_stars(stars_amount)
        amount_kopeks = int((rubles_amount * Decimal(100)).to_integral_value(rounding=ROUND_HALF_UP))

        # Build metadata matching what other providers use
        metadata = {
            'purpose': 'guest_purchase',
            'purchase_token': purchase_token,
        }

        result = await try_fulfill_guest_purchase(
            db,
            metadata=metadata,
            payment_amount_kopeks=amount_kopeks,
            provider_payment_id=telegram_payment_charge_id,
            provider_name='telegram_stars',
            skip_amount_check=True,
        )

        if result is True:
            await message.answer(
                '🎁 <b>Подарочная подписка успешно оплачена!</b>\n\n'
                f'⭐ Потрачено: {stars_amount} Stars\n\n'
                'Подарок будет доставлен получателю.',
                parse_mode='HTML',
            )
            logger.info(
                '✅ Guest purchase fulfilled via Stars',
                user_id=user.id,
                stars_amount=stars_amount,
                purchase_token_prefix=purchase_token[:5],
            )
        else:
            logger.error('try_fulfill_guest_purchase returned None for Stars gift', payload=payload)
            await message.answer('❌ Ошибка обработки платежа. Обратитесь в поддержку.')

    except Exception as e:
        logger.error('Error handling guest purchase Stars payment', error=e, exc_info=True)
        await message.answer(
            '❌ Произошла ошибка при обработке подарочной подписки. Обратитесь в поддержку.',
        )


async def handle_pre_checkout_query(query: types.PreCheckoutQuery):
    texts = get_texts(DEFAULT_LANGUAGE)

    try:
        logger.info(
            '📋 Pre-checkout query от XTR, payload',
            from_user_id=query.from_user.id,
            total_amount=query.total_amount,
            invoice_payload=query.invoice_payload,
        )

        allowed_prefixes = ('balance_', 'admin_stars_test_', 'simple_sub_', 'wheel_spin_', 'trial_', 'guest_purchase_')

        if not query.invoice_payload or not query.invoice_payload.startswith(allowed_prefixes):
            logger.warning('Невалидный payload', invoice_payload=query.invoice_payload)
            await query.answer(
                ok=False,
                error_message=texts.t(
                    'STARS_PRECHECK_INVALID_PAYLOAD',
                    'Ошибка валидации платежа. Попробуйте еще раз.',
                ),
            )
            return

        try:
            from app.database.database import AsyncSessionLocal

            async with AsyncSessionLocal() as db:
                user = await get_user_by_telegram_id(db, query.from_user.id)
                if not user:
                    logger.warning('Пользователь не найден в БД', from_user_id=query.from_user.id)
                    await query.answer(
                        ok=False,
                        error_message=texts.t(
                            'STARS_PRECHECK_USER_NOT_FOUND',
                            'Пользователь не найден. Обратитесь в поддержку.',
                        ),
                    )
                    return
                texts = get_texts(user.language or DEFAULT_LANGUAGE)
        except Exception as db_error:
            logger.error('Ошибка подключения к БД в pre_checkout_query', db_error=db_error)
            await query.answer(
                ok=False,
                error_message=texts.t(
                    'STARS_PRECHECK_TECHNICAL_ERROR',
                    'Техническая ошибка. Попробуйте позже.',
                ),
            )
            return

        await query.answer(ok=True)
        logger.info('✅ Pre-checkout одобрен для пользователя', from_user_id=query.from_user.id)

    except Exception as e:
        logger.error('Ошибка в pre_checkout_query', error=e, exc_info=True)
        await query.answer(
            ok=False,
            error_message=texts.t(
                'STARS_PRECHECK_TECHNICAL_ERROR',
                'Техническая ошибка. Попробуйте позже.',
            ),
        )


async def handle_successful_payment(message: types.Message, db: AsyncSession, state: FSMContext, **kwargs):
    texts = get_texts(DEFAULT_LANGUAGE)

    try:
        payment = message.successful_payment
        user_id = message.from_user.id

        logger.info(
            '💳 Успешный Stars платеж от XTR, payload: charge_id',
            user_id=user_id,
            total_amount=payment.total_amount,
            invoice_payload=payment.invoice_payload,
            telegram_payment_charge_id=payment.telegram_payment_charge_id,
        )

        user = await get_user_by_telegram_id(db, user_id)
        texts = get_texts(user.language if user and user.language else DEFAULT_LANGUAGE)

        if not user:
            logger.error('Пользователь не найден при обработке Stars платежа', user_id=user_id)
            await message.answer(
                texts.t(
                    'STARS_PAYMENT_USER_NOT_FOUND',
                    '❌ Ошибка: пользователь не найден. Обратитесь в поддержку.',
                )
            )
            return

        # Обработка оплаты спина колеса удачи
        if payment.invoice_payload and payment.invoice_payload.startswith('wheel_spin_'):
            await _handle_wheel_spin_payment(
                message=message,
                db=db,
                user=user,
                stars_amount=payment.total_amount,
                payload=payment.invoice_payload,
                texts=texts,
            )
            return

        # Обработка оплаты платного триала
        if payment.invoice_payload and payment.invoice_payload.startswith('trial_'):
            await _handle_trial_payment(
                message=message,
                db=db,
                user=user,
                stars_amount=payment.total_amount,
                payload=payment.invoice_payload,
                texts=texts,
            )
            return

        # Обработка оплаты гостевой покупки (подарочная подписка из кабинета)
        if payment.invoice_payload and payment.invoice_payload.startswith('guest_purchase_'):
            await _handle_guest_purchase_payment(
                message=message,
                db=db,
                user=user,
                stars_amount=payment.total_amount,
                payload=payment.invoice_payload,
                telegram_payment_charge_id=payment.telegram_payment_charge_id,
            )
            return

        payment_service = PaymentService(message.bot)

        state_data = await state.get_data()
        prompt_message_id = state_data.get('stars_prompt_message_id')
        prompt_chat_id = state_data.get('stars_prompt_chat_id', message.chat.id)
        invoice_message_id = state_data.get('stars_invoice_message_id')
        invoice_chat_id = state_data.get('stars_invoice_chat_id', message.chat.id)

        for chat_id, message_id, label in [
            (prompt_chat_id, prompt_message_id, 'запрос суммы'),
            (invoice_chat_id, invoice_message_id, 'инвойс Stars'),
        ]:
            if message_id:
                try:
                    await message.bot.delete_message(chat_id, message_id)
                except Exception as delete_error:  # pragma: no cover - зависит от прав бота
                    logger.warning(
                        'Не удалось удалить сообщение после оплаты Stars', label=label, delete_error=delete_error
                    )

        success = await payment_service.process_stars_payment(
            db=db,
            user_id=user.id,
            stars_amount=payment.total_amount,
            payload=payment.invoice_payload,
            telegram_payment_charge_id=payment.telegram_payment_charge_id,
        )

        await state.update_data(
            stars_prompt_message_id=None,
            stars_prompt_chat_id=None,
            stars_invoice_message_id=None,
            stars_invoice_chat_id=None,
        )

        if success:
            rubles_amount = TelegramStarsService.calculate_rubles_from_stars(payment.total_amount)
            amount_kopeks = int((rubles_amount * Decimal(100)).to_integral_value(rounding=ROUND_HALF_UP))
            amount_text = settings.format_price(amount_kopeks).replace(' ₽', '')

            keyboard = await payment_service.build_topup_success_keyboard(user)

            transaction_id_short = payment.telegram_payment_charge_id[:8]

            await message.answer(
                texts.t(
                    'STARS_PAYMENT_SUCCESS',
                    '🎉 <b>Платеж успешно обработан!</b>\n\n'
                    '⭐ Потрачено звезд: {stars_spent}\n'
                    '💰 Зачислено на баланс: {amount} ₽\n'
                    '🆔 ID транзакции: {transaction_id}...\n\n'
                    'Спасибо за пополнение! 🚀',
                ).format(
                    stars_spent=payment.total_amount,
                    amount=amount_text,
                    transaction_id=transaction_id_short,
                ),
                parse_mode='HTML',
                reply_markup=keyboard,
            )

            logger.info(
                '✅ Stars платеж успешно обработан: пользователь , звезд →',
                user_id=user.id,
                total_amount=payment.total_amount,
                format_price=settings.format_price(amount_kopeks),
            )
        else:
            logger.error('Ошибка обработки Stars платежа для пользователя', user_id=user.id)
            await message.answer(
                texts.t(
                    'STARS_PAYMENT_ENROLLMENT_ERROR',
                    '❌ Произошла ошибка при зачислении средств. '
                    'Обратитесь в поддержку, платеж будет проверен вручную.',
                )
            )

    except Exception as e:
        logger.error('Ошибка в successful_payment', error=e, exc_info=True)
        await message.answer(
            texts.t(
                'STARS_PAYMENT_PROCESSING_ERROR',
                '❌ Техническая ошибка при обработке платежа. Обратитесь в поддержку для решения проблемы.',
            )
        )


def register_stars_handlers(dp: Dispatcher):
    dp.pre_checkout_query.register(handle_pre_checkout_query, F.currency == 'XTR')

    dp.message.register(handle_successful_payment, F.successful_payment)

    logger.info('🌟 Зарегистрированы обработчики Telegram Stars платежей')
