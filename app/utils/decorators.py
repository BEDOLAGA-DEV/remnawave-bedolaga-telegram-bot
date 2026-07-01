import functools
from collections.abc import Callable
from typing import Any

import structlog
from aiogram import types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext

from app.config import settings
from app.localization.texts import get_texts


logger = structlog.get_logger(__name__)


def admin_required(func: Callable) -> Callable:
    @functools.wraps(func)
    async def wrapper(event: types.Update, *args, **kwargs) -> Any:
        user = None
        if isinstance(event, (types.Message, types.CallbackQuery)):
            user = event.from_user

        is_admin = False
        if user:
            # Check ADMIN_IDS env
            is_admin = settings.is_admin(user.id)

            # Check BotAdminRole in DB (same as role_required but without section filter)
            if not is_admin:
                db = kwargs.get('db')
                db_user = kwargs.get('db_user')
                if db is not None and db_user is not None:
                    try:
                        from app.database.crud.bot_role import BotRoleCRUD

                        role = await BotRoleCRUD.get_bot_role(db, db_user.id)
                        if role is not None:
                            is_admin = True
                    except Exception:
                        pass

        if not is_admin:
            texts = get_texts()

            try:
                if isinstance(event, types.Message):
                    await event.answer(texts.ACCESS_DENIED)
                elif isinstance(event, types.CallbackQuery):
                    await event.answer(texts.ACCESS_DENIED, show_alert=True)
            except TelegramBadRequest as e:
                if 'query is too old' in str(e).lower():
                    logger.warning(
                        'Попытка ответить на устаревший callback query от', user_id=user.id if user else 'Unknown'
                    )
                else:
                    raise

            logger.warning('Попытка доступа к админской функции от', user_id=user.id if user else 'Unknown')
            return None

        return await func(event, *args, **kwargs)

    return wrapper


def super_admin_required(func: Callable) -> Callable:
    """Allow only superadmins (ADMIN_IDS). Role-based BotAdminRole holders are denied.

    Use for role management, where a section-admin must not be able to grant
    themselves more permissions.
    """

    @functools.wraps(func)
    async def wrapper(event: types.Update, *args, **kwargs) -> Any:
        user = None
        if isinstance(event, (types.Message, types.CallbackQuery)):
            user = event.from_user

        if user and settings.is_admin(user.id):
            return await func(event, *args, **kwargs)

        texts = get_texts()
        try:
            if isinstance(event, types.Message):
                await event.answer(texts.ACCESS_DENIED)
            elif isinstance(event, types.CallbackQuery):
                await event.answer(texts.ACCESS_DENIED, show_alert=True)
        except TelegramBadRequest as e:
            if 'query is too old' not in str(e).lower():
                raise

        logger.warning('super_admin_required: доступ запрещён', user_id=user.id if user else 'Unknown')
        return None

    return wrapper


def role_required(section: str):
    """Check that the user is a superadmin (ADMIN_IDS) or has the given section in BotAdminRole permissions.

    Usage::

        @role_required('users')
        @error_handler
        async def handler(event, ...):
            ...
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(event: types.Update, *args, **kwargs) -> Any:
            user = None
            if isinstance(event, (types.Message, types.CallbackQuery)):
                user = event.from_user

            if not user:
                return None

            # Superadmins always pass
            if settings.is_admin(user.id):
                return await func(event, *args, **kwargs)

            # Check BotAdminRole permissions via db session from kwargs
            db = kwargs.get('db')
            if db is not None:
                from app.database.crud.bot_role import BotRoleCRUD

                db_user = kwargs.get('db_user')
                uid = db_user.id if db_user else None
                if uid is not None:
                    role = await BotRoleCRUD.get_bot_role(db, uid)
                    if role and section in (role.permissions or []):
                        return await func(event, *args, **kwargs)

            texts = get_texts()
            try:
                if isinstance(event, types.Message):
                    await event.answer(texts.ACCESS_DENIED)
                elif isinstance(event, types.CallbackQuery):
                    await event.answer(texts.ACCESS_DENIED, show_alert=True)
            except TelegramBadRequest as e:
                if 'query is too old' not in str(e).lower():
                    raise

            logger.warning(
                'role_required: доступ запрещён',
                user_id=user.id,
                section=section,
            )
            return None

        return wrapper

    return decorator


def auth_required(func: Callable) -> Callable:
    """
    Простая проверка на наличие пользователя в апдейте. Middleware уже подтягивает db_user,
    но здесь страхуемся от вызовов без from_user.
    """

    @functools.wraps(func)
    async def wrapper(event: types.Update, *args, **kwargs) -> Any:
        user = None
        if isinstance(event, (types.Message, types.CallbackQuery)):
            user = event.from_user
        if not user:
            logger.warning('auth_required: нет from_user, пропускаем')
            return None
        return await func(event, *args, **kwargs)

    return wrapper


def error_handler(func: Callable) -> Callable:
    @functools.wraps(func)
    async def wrapper(*args, **kwargs) -> Any:
        try:
            return await func(*args, **kwargs)
        except TelegramBadRequest as e:
            error_message = str(e).lower()

            if 'query is too old' in error_message or 'query id is invalid' in error_message:
                event = _extract_event(args)
                if event and isinstance(event, types.CallbackQuery):
                    user_info = (
                        f'@{event.from_user.username}' if event.from_user.username else f'ID:{event.from_user.id}'
                    )
                    logger.warning(
                        '🕐 Игнорируем устаревший callback от в',
                        event_data=event.data,
                        user_info=user_info,
                        __name__=func.__name__,
                    )
                else:
                    logger.warning('🕐 Игнорируем устаревший запрос в', __name__=func.__name__, error=e)
                return None

            if 'message is not modified' in error_message:
                logger.debug('📝 Сообщение не изменено в', __name__=func.__name__)
                event = _extract_event(args)
                if event and isinstance(event, types.CallbackQuery):
                    try:
                        await event.answer()
                    except TelegramBadRequest as answer_error:
                        if 'query is too old' not in str(answer_error).lower():
                            logger.error(
                                'Ошибка при ответе на callback в', __name__=func.__name__, answer_error=answer_error
                            )
                return None

            logger.error('Telegram API error в', __name__=func.__name__, error=e)
            # Уведомление отправляется в _send_error_message
            await _send_error_message(args, kwargs, e, func.__name__)

        except Exception as e:
            logger.error('Ошибка в', __name__=func.__name__, error=e, exc_info=True)
            await _send_error_message(args, kwargs, e, func.__name__)

    return wrapper


def _extract_event(args) -> types.TelegramObject:
    for arg in args:
        if isinstance(arg, (types.Message, types.CallbackQuery)):
            return arg
    return None


async def _send_error_message(args, kwargs, original_error, func_name: str = 'unknown'):
    event = _extract_event(args)
    db_user = kwargs.get('db_user')

    # Отправляем сообщение пользователю
    try:
        if not event:
            return

        texts = get_texts(db_user.language if db_user else 'ru')

        if isinstance(event, types.Message):
            await event.answer(texts.ERROR)
        elif isinstance(event, types.CallbackQuery):
            await event.answer(texts.ERROR, show_alert=True)

    except TelegramBadRequest as e:
        if 'query is too old' in str(e).lower():
            logger.warning('Не удалось отправить сообщение об ошибке - callback query устарел')
        else:
            logger.warning('Ошибка при отправке сообщения об ошибке', error=e)
    except Exception as e:
        logger.warning('Критическая ошибка при отправке сообщения об ошибке', error=e)


def state_cleanup(func: Callable) -> Callable:
    @functools.wraps(func)
    async def wrapper(*args, **kwargs) -> Any:
        state = kwargs.get('state')

        try:
            return await func(*args, **kwargs)
        except Exception as e:
            if state and isinstance(state, FSMContext):
                await state.clear()
            raise e

    return wrapper


def typing_action(func: Callable) -> Callable:
    @functools.wraps(func)
    async def wrapper(event: types.Update, *args, **kwargs) -> Any:
        if isinstance(event, types.Message):
            try:
                await event.bot.send_chat_action(chat_id=event.chat.id, action='typing')
            except Exception as e:
                logger.warning('Не удалось отправить typing action', error=e)

        return await func(event, *args, **kwargs)

    return wrapper


def rate_limit(rate: float = 1.0, key: str = None):
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(event: types.Update, *args, **kwargs) -> Any:
            return await func(event, *args, **kwargs)

        return wrapper

    return decorator
