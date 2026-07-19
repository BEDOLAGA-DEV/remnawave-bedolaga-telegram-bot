import structlog
from aiogram import Dispatcher, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.database.database import AsyncSessionLocal
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


def _format_amounts_line(quick_amounts: list[int] | None) -> str:
    if quick_amounts:
        return ', '.join(f'{_format_rubles(amount)} ₽' for amount in quick_amounts)
    defaults = ', '.join(f'{_format_rubles(amount)} ₽' for amount in DEFAULT_QUICK_AMOUNTS)
    return f'{defaults} (по умолчанию)'


def _method_title(config, defaults: dict) -> str:
    method_def = defaults.get(config.method_id, {})
    return config.display_name or method_def.get('default_display_name', config.method_id)


def _list_keyboard(configs: list, defaults: dict) -> InlineKeyboardMarkup:
    buttons = []
    for config in configs:
        marker = '⚙️' if config.quick_amounts else '▫️'
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f'{marker} {_method_title(config, defaults)}',
                    callback_data=f'qamounts:view:{config.method_id}',
                )
            ]
        )
    buttons.append([InlineKeyboardButton(text='◀️ Назад', callback_data='admin_submenu_settings')])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _view_keyboard(method_id: str, has_custom: bool) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text='✏️ Изменить', callback_data=f'qamounts:edit:{method_id}')]]
    if has_custom:
        buttons.append(
            [InlineKeyboardButton(text='♻️ Сбросить к умолчанию', callback_data=f'qamounts:reset:{method_id}')]
        )
    buttons.append([InlineKeyboardButton(text='◀️ К списку', callback_data='qamounts:list')])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _view_text(config, defaults: dict) -> str:
    return (
        f'💸 <b>Быстрые суммы: {_method_title(config, defaults)}</b>\n\n'
        f'<b>Текущие суммы:</b> {_format_amounts_line(config.quick_amounts)}\n\n'
        'Кнопки с этими суммами показываются пользователю при пополнении баланса.'
    )


@router.callback_query(F.data == 'qamounts:list')
@admin_required
async def show_quick_amounts_list(callback: CallbackQuery, state: FSMContext, **kwargs) -> None:
    await state.clear()
    async with AsyncSessionLocal() as db:
        configs = await get_all_configs(db)
    defaults = _get_method_defaults()
    text = (
        '💸 <b>Быстрые суммы пополнения</b>\n\n'
        'Выберите способ оплаты, чтобы настроить кнопки быстрого выбора суммы.\n'
        '⚙️ — заданы свои суммы, ▫️ — значения по умолчанию.'
    )
    await callback.message.edit_text(text, reply_markup=_list_keyboard(configs, defaults))
    await callback.answer()


@router.callback_query(F.data.startswith('qamounts:view:'))
@admin_required
async def view_quick_amounts(callback: CallbackQuery, state: FSMContext, **kwargs) -> None:
    await state.clear()
    method_id = callback.data.split(':', 2)[2]
    async with AsyncSessionLocal() as db:
        config = await get_config_by_method_id(db, method_id)
    if not config:
        await callback.answer('Способ оплаты не найден', show_alert=True)
        return
    defaults = _get_method_defaults()
    await callback.message.edit_text(
        _view_text(config, defaults),
        reply_markup=_view_keyboard(method_id, bool(config.quick_amounts)),
    )
    await callback.answer()


@router.callback_query(F.data.startswith('qamounts:edit:'))
@admin_required
async def start_edit_quick_amounts(callback: CallbackQuery, state: FSMContext, **kwargs) -> None:
    method_id = callback.data.split(':', 2)[2]
    await state.set_state(QuickAmountsStates.waiting_amounts)
    await state.update_data(quick_amounts_method_id=method_id)
    await callback.message.edit_text(
        '💸 <b>Новые быстрые суммы</b>\n\n'
        'Отправьте суммы в рублях через запятую, например: <code>100, 300, 500, 1000</code>\n'
        f'Не более {MAX_QUICK_AMOUNTS} значений. Дробные суммы — через точку.',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text='◀️ Отмена', callback_data=f'qamounts:view:{method_id}')]]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith('qamounts:reset:'))
@admin_required
async def reset_quick_amounts(callback: CallbackQuery, **kwargs) -> None:
    method_id = callback.data.split(':', 2)[2]
    async with AsyncSessionLocal() as db:
        config = await update_config(db, method_id, {'quick_amounts': None})
    if not config:
        await callback.answer('Способ оплаты не найден', show_alert=True)
        return
    await callback.answer('Суммы сброшены к значениям по умолчанию', show_alert=True)
    defaults = _get_method_defaults()
    await callback.message.edit_text(
        _view_text(config, defaults),
        reply_markup=_view_keyboard(method_id, bool(config.quick_amounts)),
    )


@router.message(QuickAmountsStates.waiting_amounts)
@admin_required
async def process_quick_amounts(message: Message, state: FSMContext, **kwargs) -> None:
    if not message.text:
        await message.answer('Отправьте текстовое сообщение с суммами через запятую.')
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
            f'❌ Неверный формат. Отправьте до {MAX_QUICK_AMOUNTS} положительных сумм в рублях через запятую '
            f'(не более {MAX_QUICK_AMOUNT_KOPEKS // 100} ₽ каждая), '
            'например: <code>100, 300, 500, 1000</code>'
        )
        return

    data = await state.get_data()
    method_id = data.get('quick_amounts_method_id')

    if not method_id:
        await state.clear()
        await message.answer('❌ Способ оплаты не выбран. Откройте раздел заново.')
        return

    async with AsyncSessionLocal() as db:
        try:
            config = await update_config(db, method_id, {'quick_amounts': amounts_kopeks})
        except ValueError as error:
            logger.warning('Некорректные быстрые суммы', method_id=method_id, error=error)
            await message.answer('❌ Не удалось сохранить суммы. Проверьте формат и попробуйте ещё раз.')
            return

    await state.clear()

    if not config:
        await message.answer('❌ Способ оплаты не найден.')
        return

    defaults = _get_method_defaults()
    await message.answer(
        f'✅ Быстрые суммы для <b>{_method_title(config, defaults)}</b> обновлены: '
        f'{_format_amounts_line(config.quick_amounts)}',
        reply_markup=_view_keyboard(method_id, bool(config.quick_amounts)),
    )


def register_handlers(dp: Dispatcher) -> None:
    dp.include_router(router)
