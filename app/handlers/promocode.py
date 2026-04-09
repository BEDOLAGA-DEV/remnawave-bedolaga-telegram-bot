import structlog
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import InaccessibleMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.keyboards.inline import get_back_keyboard
from app.localization.texts import get_texts
from app.services.admin_notification_service import AdminNotificationService
from app.services.promocode_service import PromoCodeService
from app.states import PromoCodeStates
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)


@error_handler
async def show_promocode_menu(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)

    # Если сообщение недоступно, отправляем новое
    if isinstance(callback.message, InaccessibleMessage):
        await callback.message.answer(texts.PROMOCODE_ENTER, reply_markup=get_back_keyboard(db_user.language))
    else:
        try:
            await callback.message.edit_text(texts.PROMOCODE_ENTER, reply_markup=get_back_keyboard(db_user.language))
        except TelegramBadRequest as error:
            error_message = str(error).lower()
            if 'there is no text in the message to edit' in error_message:
                await callback.message.answer(texts.PROMOCODE_ENTER, reply_markup=get_back_keyboard(db_user.language))
            else:
                raise

    # Сохраняем предыдущее состояние, чтобы восстановить после промокода
    previous_state = await state.get_state()
    previous_data = await state.get_data()

    # Не перезаписываем сохранённое состояние при повторном входе в промо-флоу
    if previous_state == PromoCodeStates.waiting_for_code.state:
        await callback.answer()
        return

    # Убираем мета-ключи чтобы не создавать вложенность
    previous_data.pop('_prev_state', None)
    previous_data.pop('_prev_data', None)

    await state.set_state(PromoCodeStates.waiting_for_code)
    await state.update_data(_prev_state=previous_state, _prev_data=previous_data)
    await callback.answer()


async def activate_promocode_for_registration(
    db: AsyncSession, user_id: int, code: str, bot: Bot = None, *, subscription_id: int | None = None
) -> dict:
    """
    Активирует промокод для пользователя.
    Возвращает результат активации без отправки сообщений.
    """
    promocode_service = PromoCodeService()
    result = await promocode_service.activate_promocode(db, user_id, code, subscription_id=subscription_id)

    if result['success']:
        logger.info('✅ Пользователь активировал промокод при регистрации', user_id=user_id, code=code)

        # Отправляем уведомление админу, если бот доступен
        if bot:
            try:
                from app.database.crud.user import get_user_by_id

                user = await get_user_by_id(db, user_id)
                if user:
                    notification_service = AdminNotificationService(bot)
                    await notification_service.send_promocode_activation_notification(
                        db,
                        user,
                        result.get('promocode', {'code': code}),
                        result['description'],
                        result.get('balance_before_kopeks'),
                        result.get('balance_after_kopeks'),
                    )
            except Exception as notify_error:
                logger.error(
                    'Ошибка отправки админ уведомления об активации промокода', code=code, notify_error=notify_error
                )

    return result


_NO_SAVED_STATE = object()


async def _restore_previous_state(state: FSMContext) -> None:
    """Восстанавливает FSM-состояние, которое было до входа в промокод-флоу."""
    data = await state.get_data()
    prev_state = data.get('_prev_state', _NO_SAVED_STATE)
    prev_data = data.get('_prev_data') or {}
    if prev_state is _NO_SAVED_STATE:
        # Не было сохранённого состояния — defensive clear
        await state.clear()
    else:
        # Восстанавливаем предыдущее состояние (включая None = меню без FSM)
        await state.set_state(prev_state)
        await state.set_data(prev_data)


@error_handler
async def process_promocode(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)

    code = message.text.strip()

    if not code:
        await message.answer(
            texts.t(
                'PROMOCODE_EMPTY_INPUT',
                '❌ Введите корректный промокод',
            ),
            reply_markup=get_back_keyboard(db_user.language),
        )
        return

    from app.utils.promo_rate_limiter import promo_limiter, validate_promo_format

    # Валидация формата
    if not validate_promo_format(code):
        await message.answer(texts.PROMOCODE_INVALID, reply_markup=get_back_keyboard(db_user.language))
        return

    # Rate-limit на перебор
    if promo_limiter.is_blocked(message.from_user.id):
        cooldown = promo_limiter.get_block_cooldown(message.from_user.id)
        await message.answer(
            texts.t(
                'PROMO_RATE_LIMITED',
                '⏳ Слишком много попыток. Попробуйте через {cooldown} сек.',
            ).format(cooldown=cooldown),
            reply_markup=get_back_keyboard(db_user.language),
        )
        await _restore_previous_state(state)
        return

    # Лимит на стакинг (макс активаций в день)
    if not promo_limiter.can_activate(message.from_user.id):
        await message.answer(
            texts.t(
                'PROMO_DAILY_LIMIT',
                '❌ Достигнут лимит активаций промокодов на сегодня. Попробуйте завтра.',
            ),
            reply_markup=get_back_keyboard(db_user.language),
        )
        await _restore_previous_state(state)
        return

    result = await activate_promocode_for_registration(db, db_user.id, code, message.bot)

    if result['success']:
        promo_limiter.record_activation(message.from_user.id)
        await message.answer(
            texts.PROMOCODE_SUCCESS.format(description=result['description']),
            reply_markup=get_back_keyboard(db_user.language),
        )
        await _restore_previous_state(state)
    elif result.get('error') == 'select_subscription':
        # Multi-tariff: user needs to choose which subscription to apply days to
        eligible = result.get('eligible_subscriptions', [])
        promo_code = result.get('code', code)
        buttons = []
        for sub in eligible:
            name = sub.get('tariff_name', f'#{sub["id"]}')
            days = sub.get('days_left', 0)
            buttons.append(
                [
                    types.InlineKeyboardButton(
                        text=f'{name} ({days} дн.)',
                        callback_data=f'promo_sub:{sub["id"]}:{promo_code}',
                    )
                ]
            )
        buttons.append([types.InlineKeyboardButton(text='❌ Отмена', callback_data='back_to_menu')])
        await message.answer(
            texts.t(
                'PROMOCODE_SELECT_SUBSCRIPTION',
                '🎟️ К какой подписке применить промокод?',
            ),
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        await _restore_previous_state(state)
    else:
        # Записываем неудачную попытку только для not_found (перебор)
        if result['error'] == 'not_found':
            promo_limiter.record_failed_attempt(message.from_user.id)
            promo_limiter.cleanup()

        error_messages = {
            'not_found': texts.PROMOCODE_INVALID,
            'expired': texts.PROMOCODE_EXPIRED,
            'inactive': texts.t('PROMOCODE_INACTIVE', '❌ Промокод деактивирован'),
            'not_yet_valid': texts.t('PROMOCODE_NOT_YET_VALID', '❌ Промокод ещё не начал действовать'),
            'used': texts.PROMOCODE_USED,
            'already_used_by_user': texts.PROMOCODE_USED,
            'not_first_purchase': texts.t(
                'PROMOCODE_NOT_FIRST_PURCHASE', '❌ Этот промокод доступен только для первой покупки'
            ),
            'active_discount_exists': texts.t(
                'PROMOCODE_ACTIVE_DISCOUNT_EXISTS',
                '❌ У вас уже есть активная скидка. Используйте её перед активацией новой.',
            ),
            'no_subscription_for_days': texts.t(
                'PROMOCODE_NO_SUBSCRIPTION',
                '❌ Для активации этого промокода необходима подписка (активная или просроченная).',
            ),
            'daily_limit': texts.t(
                'PROMO_DAILY_LIMIT',
                '❌ Достигнут лимит активаций промокодов на сегодня. Попробуйте завтра.',
            ),
            'server_error': texts.ERROR,
        }

        error_text = error_messages.get(result['error'], texts.PROMOCODE_INVALID)
        await message.answer(error_text, reply_markup=get_back_keyboard(db_user.language))
        await _restore_previous_state(state)


async def handle_promo_subscription_select(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    """Handle subscription selection for promocode with days in multi-tariff."""
    parts = (callback.data or '').split(':')
    if len(parts) < 3:
        await callback.answer('Неверный формат', show_alert=True)
        return

    try:
        sub_id = int(parts[1])
        code = ':'.join(parts[2:])  # code may contain colons
    except (ValueError, IndexError):
        await callback.answer('Ошибка', show_alert=True)
        return

    texts = get_texts(db_user.language)
    result = await activate_promocode_for_registration(db, db_user.id, code, callback.bot, subscription_id=sub_id)

    if result['success']:
        from app.utils.promo_rate_limiter import promo_limiter

        promo_limiter.record_activation(callback.from_user.id)
        if callback.message:
            await callback.message.edit_text(
                texts.PROMOCODE_SUCCESS.format(description=result['description']),
                reply_markup=get_back_keyboard(db_user.language),
            )
    else:
        error_text = texts.PROMOCODE_INVALID
        if result.get('error') == 'subscription_not_found':
            error_text = texts.t('PROMOCODE_SUBSCRIPTION_NOT_FOUND', '❌ Подписка не найдена')
        if callback.message:
            await callback.message.edit_text(error_text, reply_markup=get_back_keyboard(db_user.language))
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_promocode_menu, F.data == 'menu_promocode')
    dp.callback_query.register(handle_promo_subscription_select, F.data.startswith('promo_sub:'))

    dp.message.register(process_promocode, PromoCodeStates.waiting_for_code)
