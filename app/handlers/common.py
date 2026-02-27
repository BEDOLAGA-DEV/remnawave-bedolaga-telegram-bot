import structlog
from aiogram import Dispatcher, F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.keyboards.inline import get_back_keyboard
from app.localization.texts import get_rules, get_texts


logger = structlog.get_logger(__name__)


async def handle_delete_ban_notification(
    callback: types.CallbackQuery,
):
    """Удаляет уведомление о бане при нажатии на кнопку"""
    language = callback.from_user.language_code if callback.from_user and callback.from_user.language_code else 'ru'
    texts = get_texts(language)

    try:
        await callback.message.delete()
        await callback.answer(
            texts.t(
                'BAN_NOTIFICATION_DELETED_ALERT',
                'Уведомление удалено',
            )
        )
    except Exception as e:
        logger.warning('Не удалось удалить уведомление', error=e)
        await callback.answer(
            texts.t(
                'BAN_NOTIFICATION_DELETE_FAILED_ALERT',
                'Не удалось удалить',
            ),
            show_alert=False,
        )


async def handle_webhook_notification_close(
    callback: types.CallbackQuery,
):
    """Удаляет webhook-уведомление при нажатии кнопки Закрыть."""
    try:
        await callback.answer()
    except Exception:
        pass
    try:
        await callback.message.delete()
    except Exception as e:
        logger.warning('Не удалось удалить webhook-уведомление', e=e)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass


async def handle_unknown_callback(callback: types.CallbackQuery, db_user: User):
    texts = get_texts(db_user.language if db_user else 'ru')

    await callback.answer(
        texts.t(
            'UNKNOWN_CALLBACK_ALERT',
            '❓ Неизвестная команда. Попробуйте ещё раз.',
        ),
        show_alert=True,
    )

    logger.warning(
        'Неизвестный callback: от пользователя', callback_data=callback.data, from_user_id=callback.from_user.id
    )


async def handle_noop(callback: types.CallbackQuery, db_user: User):
    try:
        await callback.answer()
    except Exception:
        pass


async def handle_current_page(callback: types.CallbackQuery, db_user: User):
    try:
        await callback.answer()
    except Exception:
        pass


async def handle_cancel(callback: types.CallbackQuery, state: FSMContext, db_user: User):
    texts = get_texts(db_user.language)

    await state.clear()
    await callback.message.edit_text(texts.OPERATION_CANCELLED, reply_markup=get_back_keyboard(db_user.language))
    await callback.answer()


async def handle_unknown_message(
    message: types.Message,
    db_user: User | None = None,
):
    texts = get_texts(db_user.language if db_user else 'ru')

    await message.answer(
        texts.t(
            'UNKNOWN_COMMAND_MESSAGE',
            '❓ Не понимаю эту команду. Используйте кнопки меню.',
        ),
        reply_markup=get_back_keyboard(db_user.language if db_user else 'ru'),
    )


async def show_rules(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    get_texts(db_user.language)

    rules_text = await get_rules(db_user.language)

    await callback.message.edit_text(rules_text, reply_markup=get_back_keyboard(db_user.language))
    await callback.answer()


def register_handlers(dp: Dispatcher):
    # Удаление уведомлений
    dp.callback_query.register(handle_delete_ban_notification, F.data == 'ban_notify:delete')
    dp.callback_query.register(handle_webhook_notification_close, F.data == 'webhook:close')

    dp.callback_query.register(show_rules, F.data == 'menu_rules')

    # No-op utility handlers used in many keyboards
    dp.callback_query.register(handle_noop, F.data == 'noop')
    dp.callback_query.register(handle_current_page, F.data == 'current_page')

    dp.callback_query.register(handle_cancel, F.data.in_(['cancel', 'subscription_cancel']))

    # Самый последний: ловим любые неизвестные текстовые сообщения
    # Исключаем специальные сервисные события (например, успешные платежи),
    # чтобы их обработка не прерывалась общим хендлером неизвестных сообщений
    dp.message.register(
        handle_unknown_message,
        StateFilter(None),
        F.successful_payment.is_(None),
        F.text.is_not(None),
        ~F.text.startswith('/'),
    )
