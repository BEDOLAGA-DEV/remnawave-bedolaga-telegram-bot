import structlog
from aiogram import Dispatcher, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.database.database import AsyncSessionLocal
from app.localization.texts import get_texts
from app.services.payment_method_config_service import (
    DEFAULT_QUICK_AMOUNTS,
    MAX_QUICK_AMOUNT_KOPEKS,
    MAX_QUICK_AMOUNTS,
    _get_method_defaults,
    get_all_configs,
    get_config_by_method_id,
    update_config,
)
from app.utils.decorators import admin_required


logger = structlog.get_logger(__name__)

router = Router(name='admin_quick_amounts')


class QuickAmountsStates(StatesGroup):
    waiting_amounts = State()


def _format_rubles(amount_kopeks: int) -> str:
    if amount_kopeks % 100 == 0:
        return str(amount_kopeks // 100)
    return f'{amount_kopeks / 100:.2f}'


def _format_amounts_line(quick_amounts: list[int] | None, language: str = 'ru') -> str:
    texts = get_texts(language)
    if quick_amounts:
        return ', '.join(f'{_format_rubles(amount)} ₽' for amount in quick_amounts)
    if quick_amounts is not None:
        # Пустой список — кнопки отключены админом (None = дефолты)
        return texts.t('ADMIN_QUICK_DISABLED')
    defaults = ', '.join(f'{_format_rubles(amount)} ₽' for amount in DEFAULT_QUICK_AMOUNTS)
    return texts.t('ADMIN_QUICK_DEFAULT').format(amounts=defaults)


def _method_title(config, defaults: dict) -> str:
    method_def = defaults.get(config.method_id, {})
    return config.display_name or method_def.get('default_display_name', config.method_id)


def _list_keyboard(configs: list, defaults: dict, language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)
    buttons = []
    for config in configs:
        if config.quick_amounts:
            marker = '⚙️'
        elif config.quick_amounts is not None:
            marker = '🚫'
        else:
            marker = '▫️'
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f'{marker} {_method_title(config, defaults)}',
                    callback_data=f'qamounts:view:{config.method_id}',
                )
            ]
        )
    buttons.append([InlineKeyboardButton(text=texts.t('ADMIN_QUICK_BACK'), callback_data='admin_submenu_settings')])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _view_keyboard(method_id: str, quick_amounts: list[int] | None, language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)
    buttons = [[InlineKeyboardButton(text=texts.t('ADMIN_QUICK_EDIT'), callback_data=f'qamounts:edit:{method_id}')]]
    if quick_amounts != []:
        buttons.append(
            [InlineKeyboardButton(text=texts.t('ADMIN_QUICK_DISABLE'), callback_data=f'qamounts:disable:{method_id}')]
        )
    if quick_amounts is not None:
        buttons.append(
            [InlineKeyboardButton(text=texts.t('ADMIN_QUICK_RESET'), callback_data=f'qamounts:reset:{method_id}')]
        )
    buttons.append([InlineKeyboardButton(text=texts.t('ADMIN_QUICK_LIST_BACK'), callback_data='qamounts:list')])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _view_text(config, defaults: dict, language: str = 'ru') -> str:
    texts = get_texts(language)
    return texts.t('ADMIN_QUICK_VIEW').format(
        method=_method_title(config, defaults), amounts=_format_amounts_line(config.quick_amounts, language)
    )


@router.callback_query(F.data == 'qamounts:list')
@admin_required
async def show_quick_amounts_list(callback: CallbackQuery, state: FSMContext, **kwargs) -> None:
    texts = get_texts(getattr(kwargs.get('db_user'), 'language', 'ru'))
    await state.clear()
    async with AsyncSessionLocal() as db:
        configs = await get_all_configs(db)
    defaults = _get_method_defaults()
    text = texts.t('ADMIN_QUICK_OVERVIEW')
    await callback.message.edit_text(text, reply_markup=_list_keyboard(configs, defaults, texts.language))
    await callback.answer()


@router.callback_query(F.data.startswith('qamounts:view:'))
@admin_required
async def view_quick_amounts(callback: CallbackQuery, state: FSMContext, **kwargs) -> None:
    texts = get_texts(getattr(kwargs.get('db_user'), 'language', 'ru'))
    await state.clear()
    method_id = callback.data.split(':', 2)[2]
    async with AsyncSessionLocal() as db:
        config = await get_config_by_method_id(db, method_id)
    if not config:
        await callback.answer(texts.t('ADMIN_QUICK_NOT_FOUND'), show_alert=True)
        return
    defaults = _get_method_defaults()
    await callback.message.edit_text(
        _view_text(config, defaults, texts.language),
        reply_markup=_view_keyboard(method_id, config.quick_amounts, texts.language),
    )
    await callback.answer()


@router.callback_query(F.data.startswith('qamounts:disable:'))
@admin_required
async def disable_quick_amounts(callback: CallbackQuery, **kwargs) -> None:
    """Полностью убирает кнопки быстрых сумм: пользователь вводит сумму вручную."""
    texts = get_texts(getattr(kwargs.get('db_user'), 'language', 'ru'))
    method_id = callback.data.split(':', 2)[2]
    async with AsyncSessionLocal() as db:
        config = await update_config(db, method_id, {'quick_amounts': []})
    if not config:
        await callback.answer(texts.t('ADMIN_QUICK_NOT_FOUND'), show_alert=True)
        return
    await callback.answer(texts.t('ADMIN_QUICK_DISABLED_NOTICE'), show_alert=True)
    defaults = _get_method_defaults()
    await callback.message.edit_text(
        _view_text(config, defaults, texts.language),
        reply_markup=_view_keyboard(method_id, config.quick_amounts, texts.language),
    )


@router.callback_query(F.data.startswith('qamounts:edit:'))
@admin_required
async def start_edit_quick_amounts(callback: CallbackQuery, state: FSMContext, **kwargs) -> None:
    texts = get_texts(getattr(kwargs.get('db_user'), 'language', 'ru'))
    method_id = callback.data.split(':', 2)[2]
    await state.set_state(QuickAmountsStates.waiting_amounts)
    await state.update_data(quick_amounts_method_id=method_id)
    await callback.message.edit_text(
        texts.t('ADMIN_QUICK_PROMPT').format(limit=MAX_QUICK_AMOUNTS),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=texts.t('ADMIN_QUICK_CANCEL'), callback_data=f'qamounts:view:{method_id}')]
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith('qamounts:reset:'))
@admin_required
async def reset_quick_amounts(callback: CallbackQuery, **kwargs) -> None:
    texts = get_texts(getattr(kwargs.get('db_user'), 'language', 'ru'))
    method_id = callback.data.split(':', 2)[2]
    async with AsyncSessionLocal() as db:
        config = await update_config(db, method_id, {'quick_amounts': None})
    if not config:
        await callback.answer(texts.t('ADMIN_QUICK_NOT_FOUND'), show_alert=True)
        return
    await callback.answer(texts.t('ADMIN_QUICK_RESET_NOTICE'), show_alert=True)
    defaults = _get_method_defaults()
    await callback.message.edit_text(
        _view_text(config, defaults, texts.language),
        reply_markup=_view_keyboard(method_id, config.quick_amounts, texts.language),
    )


@router.message(QuickAmountsStates.waiting_amounts)
@admin_required
async def process_quick_amounts(message: Message, state: FSMContext, **kwargs) -> None:
    texts = get_texts(getattr(kwargs.get('db_user'), 'language', 'ru'))
    if not message.text:
        await message.answer(texts.t('ADMIN_QUICK_TEXT_REQUIRED'))
        return

    amounts_kopeks: list[int] = []
    try:
        for part in message.text.split(','):
            cleaned = part.strip().replace(' ', '')
            if not cleaned:
                continue
            kopeks = round(float(cleaned) * 100)
            if kopeks <= 0 or kopeks > MAX_QUICK_AMOUNT_KOPEKS:
                raise ValueError(cleaned)
            amounts_kopeks.append(kopeks)
        if not amounts_kopeks or len(amounts_kopeks) > MAX_QUICK_AMOUNTS:
            raise ValueError(message.text)
    except (ValueError, OverflowError):
        await message.answer(
            texts.t('ADMIN_QUICK_INVALID').format(limit=MAX_QUICK_AMOUNTS, maximum=MAX_QUICK_AMOUNT_KOPEKS // 100)
        )
        return

    data = await state.get_data()
    method_id = data.get('quick_amounts_method_id')

    if not method_id:
        await state.clear()
        await message.answer(texts.t('ADMIN_QUICK_NO_METHOD'))
        return

    async with AsyncSessionLocal() as db:
        try:
            config = await update_config(db, method_id, {'quick_amounts': amounts_kopeks})
        except ValueError as error:
            logger.warning('Некорректные быстрые суммы', method_id=method_id, error=error)
            await message.answer(texts.t('ADMIN_QUICK_SAVE_ERROR'))
            return

    await state.clear()

    if not config:
        await message.answer(texts.t('ADMIN_QUICK_MISSING'))
        return

    defaults = _get_method_defaults()
    await message.answer(
        texts.t('ADMIN_QUICK_SUCCESS').format(
            method=_method_title(config, defaults), amounts=_format_amounts_line(config.quick_amounts, texts.language)
        ),
        reply_markup=_view_keyboard(method_id, config.quick_amounts, texts.language),
    )


def register_handlers(dp: Dispatcher) -> None:
    dp.include_router(router)
