import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types import CallbackQuery, Message, TelegramObject, User as TgUser
from sqlalchemy.exc import InterfaceError, OperationalError

from app.config import settings
from app.database.crud.user import get_user_by_telegram_id
from app.database.database import AsyncSessionLocal
from app.services.remnawave_service import RemnaWaveService
from app.states import RegistrationStates
from app.utils.check_reg_process import is_registration_process
from app.utils.validators import sanitize_telegram_name


logger = structlog.get_logger(__name__)


async def _refresh_remnawave_description(remnawave_uuid: str, description: str, telegram_id: int) -> None:
    try:
        remnawave_service = RemnaWaveService()
        async with remnawave_service.get_api_client() as api:
            await api.update_user(uuid=remnawave_uuid, description=description)
        logger.info('✅ [Middleware] Описание пользователя обновлено в RemnaWave', telegram_id=telegram_id)
    except Exception as remnawave_error:
        logger.error(
            '❌ [Middleware] Ошибка обновления RemnaWave для', telegram_id=telegram_id, remnawave_error=remnawave_error
        )


def _build_migration_keyboard() -> InlineKeyboardMarkup | None:
    target_username = settings.get_migration_target_bot_username()
    if not target_username:
        return None

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text='Открыть нового бота',
                    url=f'https://t.me/{target_username}?start=migrated',
                )
            ]
        ]
    )


class AuthMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Callback с недоступным сообщением (>48ч) — пропускаем к хендлерам,
        # они сами отправят новое сообщение через edit_or_answer_photo
        # if isinstance(event, CallbackQuery) and isinstance(event.message, InaccessibleMessage):
        #     pass  # Раньше здесь был return None, теперь пропускаем дальше

        user: TgUser = None
        if isinstance(event, (Message, CallbackQuery)):
            user = event.from_user

        if not user:
            return await handler(event, data)

        if user.is_bot:
            return await handler(event, data)

        bot_obj = data.get('bot')
        current_bot_id = getattr(bot_obj, 'id', None)
        migration_keyboard = _build_migration_keyboard()

        async with AsyncSessionLocal() as db:
            try:
                db_user = await get_user_by_telegram_id(db, user.id)

                if not db_user:
                    state: FSMContext = data.get('state')
                    current_state = None

                    if state:
                        current_state = await state.get_state()

                    is_reg_process = is_registration_process(event, current_state)

                    is_channel_check = isinstance(event, CallbackQuery) and event.data == 'sub_channel_check'

                    is_start_command = isinstance(event, Message) and event.text and event.text.startswith('/start')

                    if is_reg_process or is_channel_check or is_start_command:
                        if is_start_command:
                            logger.info('🚀 Пропускаем команду /start от пользователя', user_id=user.id)
                        elif is_channel_check:
                            logger.info(
                                '🔍 Пропускаем незарегистрированного пользователя для проверки канала', user_id=user.id
                            )
                        else:
                            logger.info('🔍 Пропускаем пользователя в процессе регистрации', user_id=user.id)
                        data['db'] = db
                        data['db_user'] = None
                        data['is_admin'] = False
                        result = await handler(event, data)
                        await db.commit()
                        return result
                    if isinstance(event, Message):
                        await event.answer('▶️ Для начала работы необходимо выполнить команду /start')
                    elif isinstance(event, CallbackQuery):
                        await event.answer('▶️ Необходимо начать с команды /start', show_alert=True)
                    logger.info('🚫 Заблокирован незарегистрированный пользователь', user_id=user.id)
                    return None
                from app.database.models import UserStatus

                if db_user.status == UserStatus.BLOCKED.value:
                    if isinstance(event, Message):
                        await event.answer('🚫 Ваш аккаунт заблокирован администратором.')
                    elif isinstance(event, CallbackQuery):
                        await event.answer('🚫 Ваш аккаунт заблокирован администратором.', show_alert=True)
                    logger.info('🚫 Заблокированный пользователь попытался использовать бота', user_id=user.id)
                    return None

                if db_user.status == UserStatus.DELETED.value:
                    state: FSMContext = data.get('state')
                    current_state = None

                    if state:
                        current_state = await state.get_state()

                    registration_states = [
                        RegistrationStates.waiting_for_language.state,
                        RegistrationStates.waiting_for_rules_accept.state,
                        RegistrationStates.waiting_for_privacy_policy_accept.state,
                        RegistrationStates.waiting_for_referral_code.state,
                    ]

                    is_start_or_registration = (
                        (isinstance(event, Message) and event.text and event.text.startswith('/start'))
                        or (current_state in registration_states)
                        or (
                            isinstance(event, CallbackQuery)
                            and event.data
                            and (
                                event.data
                                in [
                                    'rules_accept',
                                    'rules_decline',
                                    'privacy_policy_accept',
                                    'privacy_policy_decline',
                                    'referral_skip',
                                ]
                                or event.data.startswith('language_select:')
                            )
                        )
                    )

                    if is_start_or_registration:
                        logger.info('🔄 Удаленный пользователь начинает повторную регистрацию', user_id=user.id)
                        data['db'] = db
                        data['db_user'] = None
                        data['is_admin'] = False
                        result = await handler(event, data)
                        await db.commit()
                        return result
                    if isinstance(event, Message):
                        await event.answer(
                            '❌ Ваш аккаунт был удален.\n🔄 Для повторной регистрации выполните команду /start'
                        )
                    elif isinstance(event, CallbackQuery):
                        await event.answer(
                            '❌ Ваш аккаунт был удален. Для повторной регистрации выполните /start', show_alert=True
                        )
                    logger.info('❌ Удаленный пользователь попытался использовать бота без /start', user_id=user.id)
                    return None

                if settings.is_legacy_bot_readonly_enabled(current_bot_id=current_bot_id, user_id=user.id):
                    if settings.is_migration_notice_enabled():
                        notice_text = settings.get_migration_notice_text()
                        if isinstance(event, Message):
                            await event.answer(notice_text, reply_markup=migration_keyboard)
                        elif isinstance(event, CallbackQuery):
                            await event.answer(notice_text, show_alert=True)
                    return None

                profile_updated = False

                if db_user.username != user.username:
                    old_username = db_user.username
                    db_user.username = user.username
                    logger.info(
                        '🔄 [Middleware] Username обновлен для',
                        user_id=user.id,
                        old_username=old_username,
                        username=db_user.username,
                    )
                    profile_updated = True

                safe_first = sanitize_telegram_name(user.first_name)
                safe_last = sanitize_telegram_name(user.last_name)
                if db_user.first_name != safe_first:
                    old_first_name = db_user.first_name
                    db_user.first_name = safe_first
                    logger.info(
                        '🔄 [Middleware] Имя обновлено для',
                        user_id=user.id,
                        old_first_name=old_first_name,
                        first_name=db_user.first_name,
                    )
                    profile_updated = True

                if db_user.last_name != safe_last:
                    old_last_name = db_user.last_name
                    db_user.last_name = safe_last
                    logger.info(
                        '🔄 [Middleware] Фамилия обновлена для',
                        user_id=user.id,
                        old_last_name=old_last_name,
                        last_name=db_user.last_name,
                    )
                    profile_updated = True

                db_user.last_activity = datetime.now(UTC)
                if current_bot_id is not None and getattr(db_user, 'last_bot_id', None) != current_bot_id:
                    db_user.last_bot_id = current_bot_id
                    profile_updated = True

                if profile_updated:
                    db_user.updated_at = datetime.now(UTC)
                    logger.info('💾 [Middleware] Профиль пользователя обновлен в middleware', user_id=user.id)

                    if db_user.remnawave_uuid:
                        description = settings.format_remnawave_user_description(
                            full_name=db_user.full_name, username=db_user.username, telegram_id=db_user.telegram_id
                        )
                        asyncio.create_task(
                            _refresh_remnawave_description(
                                remnawave_uuid=db_user.remnawave_uuid,
                                description=description,
                                telegram_id=db_user.telegram_id,
                            )
                        )

                data['db'] = db
                data['db_user'] = db_user
                data['is_admin'] = settings.is_admin(user.id)

                result = await handler(event, data)
                try:
                    await db.commit()
                except (InterfaceError, OperationalError) as conn_err:
                    # Соединение закрылось (таймаут после долгой операции) - просто логируем
                    logger.warning('⚠️ Соединение с БД закрыто после обработки, пропускаем commit', conn_err=conn_err)
                except Exception as commit_err:
                    # Transaction aborted (e.g. handler swallowed a ProgrammingError) — rollback
                    logger.warning('⚠️ Не удалось commit после обработки, rollback', commit_err=commit_err)
                    try:
                        await db.rollback()
                    except Exception:
                        pass
                return result

            except (InterfaceError, OperationalError) as conn_err:
                # Соединение с БД закрылось - не пытаемся rollback
                logger.error('Ошибка соединения с БД в AuthMiddleware', conn_err=conn_err)
                logger.error('Event type', event_type=type(event))
                if hasattr(event, 'data'):
                    logger.error('Callback data', event_data=event.data)
                raise
            except TelegramForbiddenError:
                # User blocked the bot — normal, not an error
                logger.debug('AuthMiddleware: bot blocked by user, skipping')
                return None
            except TelegramBadRequest as e:
                if 'query is too old' in str(e):
                    logger.debug('AuthMiddleware: callback query expired, skipping')
                    return None
                raise
            except Exception as e:
                logger.error('Ошибка в AuthMiddleware', error=e)
                logger.error('Event type', event_type=type(event))
                if hasattr(event, 'data'):
                    logger.error('Callback data', event_data=event.data)
                try:
                    await db.rollback()
                except (InterfaceError, OperationalError):
                    pass  # Соединение уже закрыто
                raise
