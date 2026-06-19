import html
import io

import structlog
from aiogram import Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.database.database import AsyncSessionLocal
from app.services import overpay_certificate_service as cert_service
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)

router = Router(name='admin_overpay_certificate')

ALLOWED_EXTENSIONS = ('.p12', '.pfx')

ENV_LOCK_NOTE = (
    '⚠️ OVERPAY_P12_PATH или OVERPAY_P12_PASSPHRASE заданы через переменные окружения — значения из БД не применяются.'
)


class OverpayCertStates(StatesGroup):
    waiting_for_file = State()
    waiting_for_passphrase = State()


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='◀️ Отмена', callback_data='overpay_cert')]])


def _back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text='◀️ К сертификату', callback_data='overpay_cert')]]
    )


def _status_view() -> tuple[str, InlineKeyboardMarkup]:
    status = cert_service.get_status()
    lines = ['📜 <b>Сертификат Overpay</b>', '']

    if not status['uploaded']:
        lines.append('Статус: ❌ не загружен')
    elif status['valid']:
        lines.append('Статус: ✅ загружен')
        lines.append(f'Субъект: <code>{html.escape(status["subject"])}</code>')
        lines.append(f'Действует до: <code>{status["not_valid_after"]}</code>')
    else:
        lines.append('Статус: ⚠️ файл найден, но не читается с текущим паролем')

    if status['uploaded']:
        lines.append(f'Путь: <code>{html.escape(status["path"])}</code>')

    if status['env_locked_path'] or status['env_locked_passphrase']:
        lines.append('')
        lines.append(ENV_LOCK_NOTE)

    buttons = [[InlineKeyboardButton(text='📎 Загрузить', callback_data='overpay_cert:upload')]]
    if status['uploaded']:
        buttons.append([InlineKeyboardButton(text='🗑 Удалить', callback_data='overpay_cert:delete')])
    buttons.append([InlineKeyboardButton(text='◀️ Назад', callback_data='admin_submenu_settings')])

    return '\n'.join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == 'overpay_cert')
@admin_required
@error_handler
async def show_certificate_status(callback: CallbackQuery, state: FSMContext, **kwargs) -> None:
    await state.clear()
    text, keyboard = _status_view()
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == 'overpay_cert:upload')
@admin_required
@error_handler
async def start_certificate_upload(callback: CallbackQuery, state: FSMContext, **kwargs) -> None:
    await state.set_state(OverpayCertStates.waiting_for_file)
    await callback.message.edit_text(
        '📎 <b>Загрузка сертификата Overpay</b>\n\nОтправьте файл сертификата (.p12 или .pfx) размером до 1 МБ.',
        reply_markup=_cancel_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == 'overpay_cert:delete')
@admin_required
@error_handler
async def confirm_certificate_delete(callback: CallbackQuery, **kwargs) -> None:
    await callback.message.edit_text(
        '🗑 <b>Удаление сертификата Overpay</b>\n\n'
        'Файл будет удалён, настройки пути и пароля очищены. Платежи через Overpay перестанут работать. Продолжить?',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text='✅ Удалить', callback_data='overpay_cert:delete_confirm'),
                    InlineKeyboardButton(text='◀️ Отмена', callback_data='overpay_cert'),
                ]
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == 'overpay_cert:delete_confirm')
@admin_required
@error_handler
async def delete_certificate(callback: CallbackQuery, state: FSMContext, **kwargs) -> None:
    await state.clear()
    async with AsyncSessionLocal() as db:
        await cert_service.delete_certificate(db)
    await callback.answer('Сертификат удалён', show_alert=True)
    text, keyboard = _status_view()
    await callback.message.edit_text(text, reply_markup=keyboard)


@router.message(OverpayCertStates.waiting_for_file)
@admin_required
@error_handler
async def process_certificate_file(message: Message, state: FSMContext, **kwargs) -> None:
    document = message.document
    if not document:
        await message.answer(
            '❌ Отправьте файл сертификата документом (.p12 или .pfx).',
            reply_markup=_cancel_keyboard(),
        )
        return

    file_name = document.file_name or ''
    if not file_name.lower().endswith(ALLOWED_EXTENSIONS):
        await message.answer(
            '❌ Неподдерживаемый формат файла. Загрузите .p12 или .pfx.',
            reply_markup=_cancel_keyboard(),
        )
        return

    if document.file_size and document.file_size > cert_service.MAX_P12_SIZE:
        await message.answer(
            '❌ Файл слишком большой (максимум 1 МБ).',
            reply_markup=_cancel_keyboard(),
        )
        return

    await state.update_data(overpay_cert_file_id=document.file_id)
    await state.set_state(OverpayCertStates.waiting_for_passphrase)
    await message.answer(
        '🔑 Отправьте пароль от контейнера P12.\n'
        'Если пароля нет — отправьте <code>-</code>.\n\n'
        'Сообщение с паролем будет удалено.',
        reply_markup=_cancel_keyboard(),
    )


@router.message(OverpayCertStates.waiting_for_passphrase)
@admin_required
@error_handler
async def process_certificate_passphrase(message: Message, state: FSMContext, **kwargs) -> None:
    if not message.text:
        await message.answer(
            '❌ Отправьте пароль текстовым сообщением (или <code>-</code>, если пароля нет).',
            reply_markup=_cancel_keyboard(),
        )
        return

    passphrase = '' if message.text.strip() == '-' else message.text

    try:
        await message.delete()
    except TelegramAPIError:
        pass

    data = await state.get_data()
    file_id = data.get('overpay_cert_file_id')
    await state.clear()

    if not file_id:
        await message.answer('❌ Файл не найден. Начните загрузку заново.', reply_markup=_back_keyboard())
        return

    buffer = io.BytesIO()
    try:
        await message.bot.download(file_id, destination=buffer)
    except TelegramBadRequest as error:
        logger.warning('Overpay: не удалось скачать файл сертификата', error=error)
        await message.answer('❌ Не удалось скачать файл. Начните загрузку заново.', reply_markup=_back_keyboard())
        return

    async with AsyncSessionLocal() as db:
        try:
            metadata = await cert_service.store_certificate(db, buffer.getvalue(), passphrase)
        except ValueError as error:
            await message.answer(f'❌ {error}', reply_markup=_back_keyboard())
            return

    lines = [
        '✅ <b>Сертификат Overpay сохранён</b>',
        '',
        f'Субъект: <code>{html.escape(metadata["subject"])}</code>',
        f'Действует до: <code>{metadata["not_valid_after"]}</code>',
    ]
    if metadata['warning']:
        lines.append('')
        lines.append(f'⚠️ {metadata["warning"]}')

    await message.answer('\n'.join(lines), reply_markup=_back_keyboard())


def register_handlers(dp: Dispatcher) -> None:
    dp.include_router(router)
