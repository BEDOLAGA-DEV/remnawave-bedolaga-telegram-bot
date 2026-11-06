import html
import io

import structlog
from aiogram import Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.database.database import AsyncSessionLocal
from app.localization.texts import get_texts
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


def _cancel_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=texts.t('ADMIN_OVERPAY_CERT_022'), callback_data='overpay_cert')]]
    )


def _back_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=texts.t('ADMIN_OVERPAY_CERT_023'), callback_data='overpay_cert')]]
    )


def _status_view(language: str = 'ru') -> tuple[str, InlineKeyboardMarkup]:
    texts = get_texts(language)
    status = cert_service.get_status()
    lines = [texts.t('ADMIN_OVERPAY_CERT_001'), '']

    if not status['uploaded']:
        lines.append(texts.t('ADMIN_OVERPAY_CERT_005'))
    elif status['valid']:
        lines.append(texts.t('ADMIN_OVERPAY_CERT_011'))
        lines.append(texts.t('ADMIN_OVERPAY_CERT_003').format(value1=html.escape(status['subject'])))
        lines.append(texts.t('ADMIN_OVERPAY_CERT_004').format(value1=status['not_valid_after']))
    else:
        lines.append(texts.t('ADMIN_OVERPAY_CERT_012'))

    if status['uploaded']:
        lines.append(texts.t('ADMIN_OVERPAY_CERT_006').format(value1=html.escape(status['path'])))

    if status['env_locked_path'] or status['env_locked_passphrase']:
        lines.append('')
        lines.append(texts.t('ADMIN_OVERPAY_CERT_000', ENV_LOCK_NOTE))

    buttons = [[InlineKeyboardButton(text=texts.t('ADMIN_OVERPAY_CERT_018'), callback_data='overpay_cert:upload')]]
    if status['uploaded']:
        buttons.append(
            [InlineKeyboardButton(text=texts.t('ADMIN_OVERPAY_CERT_021'), callback_data='overpay_cert:delete')]
        )
    buttons.append(
        [InlineKeyboardButton(text=texts.t('ADMIN_OVERPAY_CERT_019'), callback_data='admin_submenu_settings')]
    )

    return '\n'.join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == 'overpay_cert')
@admin_required
@error_handler
async def show_certificate_status(callback: CallbackQuery, state: FSMContext, **kwargs) -> None:
    texts = get_texts(getattr(kwargs.get('db_user'), 'language', 'ru'))
    await state.clear()
    text, keyboard = _status_view(texts.language)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == 'overpay_cert:upload')
@admin_required
@error_handler
async def start_certificate_upload(callback: CallbackQuery, state: FSMContext, **kwargs) -> None:
    texts = get_texts(getattr(kwargs.get('db_user'), 'language', 'ru'))
    await state.set_state(OverpayCertStates.waiting_for_file)
    await callback.message.edit_text(
        texts.t('ADMIN_OVERPAY_CERT_007'),
        reply_markup=_cancel_keyboard(texts.language),
    )
    await callback.answer()


@router.callback_query(F.data == 'overpay_cert:delete')
@admin_required
@error_handler
async def confirm_certificate_delete(callback: CallbackQuery, **kwargs) -> None:
    texts = get_texts(getattr(kwargs.get('db_user'), 'language', 'ru'))
    await callback.message.edit_text(
        texts.t('ADMIN_OVERPAY_CERT_008'),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_OVERPAY_CERT_024'), callback_data='overpay_cert:delete_confirm'
                    ),
                    InlineKeyboardButton(text=texts.t('ADMIN_OVERPAY_CERT_022'), callback_data='overpay_cert'),
                ]
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == 'overpay_cert:delete_confirm')
@admin_required
@error_handler
async def delete_certificate(callback: CallbackQuery, state: FSMContext, **kwargs) -> None:
    texts = get_texts(getattr(kwargs.get('db_user'), 'language', 'ru'))
    await state.clear()
    async with AsyncSessionLocal() as db:
        await cert_service.delete_certificate(db)
    await callback.answer(texts.t('ADMIN_OVERPAY_CERT_009'), show_alert=True)
    text, keyboard = _status_view(texts.language)
    await callback.message.edit_text(text, reply_markup=keyboard)


@router.message(OverpayCertStates.waiting_for_file)
@admin_required
@error_handler
async def process_certificate_file(message: Message, state: FSMContext, **kwargs) -> None:
    texts = get_texts(getattr(kwargs.get('db_user'), 'language', 'ru'))
    document = message.document
    if not document:
        await message.answer(
            texts.t('ADMIN_OVERPAY_CERT_013'),
            reply_markup=_cancel_keyboard(texts.language),
        )
        return

    file_name = document.file_name or ''
    if not file_name.lower().endswith(ALLOWED_EXTENSIONS):
        await message.answer(
            texts.t('ADMIN_OVERPAY_CERT_014'),
            reply_markup=_cancel_keyboard(texts.language),
        )
        return

    if document.file_size and document.file_size > cert_service.MAX_P12_SIZE:
        await message.answer(
            texts.t('ADMIN_OVERPAY_CERT_015'),
            reply_markup=_cancel_keyboard(texts.language),
        )
        return

    await state.update_data(overpay_cert_file_id=document.file_id)
    await state.set_state(OverpayCertStates.waiting_for_passphrase)
    await message.answer(
        texts.t('ADMIN_OVERPAY_CERT_010'),
        reply_markup=_cancel_keyboard(texts.language),
    )


@router.message(OverpayCertStates.waiting_for_passphrase)
@admin_required
@error_handler
async def process_certificate_passphrase(message: Message, state: FSMContext, **kwargs) -> None:
    texts = get_texts(getattr(kwargs.get('db_user'), 'language', 'ru'))
    if not message.text:
        await message.answer(
            texts.t('ADMIN_OVERPAY_CERT_016'),
            reply_markup=_cancel_keyboard(texts.language),
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
        await message.answer(texts.t('ADMIN_OVERPAY_CERT_017'), reply_markup=_back_keyboard(texts.language))
        return

    buffer = io.BytesIO()
    try:
        await message.bot.download(file_id, destination=buffer)
    except TelegramBadRequest as error:
        logger.warning('Overpay: не удалось скачать файл сертификата', error=error)
        await message.answer(texts.t('ADMIN_OVERPAY_CERT_020'), reply_markup=_back_keyboard(texts.language))
        return

    async with AsyncSessionLocal() as db:
        try:
            metadata = await cert_service.store_certificate(db, buffer.getvalue(), passphrase)
        except ValueError as error:
            await message.answer(
                f'❌ {html.escape(_certificate_error_text(str(error), texts))}',
                reply_markup=_back_keyboard(texts.language),
            )
            return

    lines = [
        texts.t('ADMIN_OVERPAY_CERT_002'),
        '',
        texts.t('ADMIN_OVERPAY_CERT_003').format(value1=html.escape(metadata['subject'])),
        texts.t('ADMIN_OVERPAY_CERT_004').format(value1=metadata['not_valid_after']),
    ]
    if metadata['warning']:
        lines.append('')
        warning = metadata['warning']
        if warning == cert_service.ENV_LOCK_WARNING.format(path=metadata['path']):
            warning = texts.t('ADMIN_OVERPAY_CERT_ENV_WARNING').format(path=metadata['path'])
        lines.append(f'⚠️ {html.escape(warning)}')

    await message.answer('\n'.join(lines), reply_markup=_back_keyboard(texts.language))


def _certificate_error_text(error: str, texts) -> str:
    keys = {
        'Файл сертификата больше 1 МБ': 'ADMIN_OVERPAY_CERT_ERROR_SIZE',
        'Не удалось прочитать P12: неверный пароль или повреждённый файл': 'ADMIN_OVERPAY_CERT_ERROR_READ',
        'P12 не содержит приватный ключ и сертификат': 'ADMIN_OVERPAY_CERT_ERROR_CONTENT',
    }
    key = keys.get(error)
    return texts.t(key) if key else error


def register_handlers(dp: Dispatcher) -> None:
    dp.include_router(router)
