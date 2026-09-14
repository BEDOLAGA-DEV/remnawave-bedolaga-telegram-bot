"""Телеграм-редактор: экран «Ещё настройки» тарифа.

Тег панели, внешний сквад Remnawave, продукт Lava, порядок в списке, показ в подарках и
разрешение докупки — поля, которые раньше правились только из кабинета.
"""

import html

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.tariff import get_tariff_by_id, update_tariff
from app.database.models import Tariff, User
from app.localization.texts import get_texts
from app.services.tariff_squad_sync import schedule_tariff_squad_sync
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler
from app.utils.panel_tag import PANEL_TAG_RULES, normalize_panel_tag


logger = structlog.get_logger(__name__)

_NO_EXTERNAL_SQUAD = '-'


def _yes_no(value: bool) -> str:
    return '✅ Да' if value else '❌ Нет'


def _tag_display(tariff: Tariff) -> str:
    return html.escape(getattr(tariff, 'panel_tag', None) or 'общий из настроек')


def _ext_squad_display(tariff: Tariff) -> str:
    return html.escape(getattr(tariff, 'external_squad_uuid', None) or 'нет')


def _lava_display(tariff: Tariff) -> str:
    return html.escape(getattr(tariff, 'lava_product_id', None) or 'не задан')


def format_panel_settings(tariff: Tariff) -> str:
    """Блок для карточки тарифа."""
    return (
        f'• Тег панели: {_tag_display(tariff)}\n'
        f'• Внешний сквад: {_ext_squad_display(tariff)}\n'
        f'• Продукт Lava: {_lava_display(tariff)}\n'
        f'• В подарках: {_yes_no(getattr(tariff, "show_in_gift", True))}\n'
        f'• Докупка трафика разрешена: {_yes_no(getattr(tariff, "allow_traffic_topup", True))}'
    )


def render_panel_settings(tariff: Tariff) -> str:
    return (
        f'⚙️ <b>Ещё настройки</b>\n\n'
        f'Тариф: <b>{html.escape(tariff.name)}</b>\n\n'
        f'{format_panel_settings(tariff)}\n'
        f'• Порядок в списке: {getattr(tariff, "display_order", 0)}\n\n'
        'Тег панели показывается у пользователя в списке панели Remnawave — по нему видно тариф. '
        'Пусто — общий тег из настроек (триальный/платный).'
    )


def get_panel_settings_keyboard(tariff: Tariff, language: str) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    gift = getattr(tariff, 'show_in_gift', True)
    allow_topup = getattr(tariff, 'allow_traffic_topup', True)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text='🏷️ Тег панели', callback_data=f'admin_tariff_edit_panel_tag:{tariff.id}'),
                InlineKeyboardButton(text='🌐 Внешний сквад', callback_data=f'admin_tariff_edit_ext_squad:{tariff.id}'),
            ],
            [
                InlineKeyboardButton(text='💳 Продукт Lava', callback_data=f'admin_tariff_edit_lava:{tariff.id}'),
                InlineKeyboardButton(text='🔢 Порядок', callback_data=f'admin_tariff_edit_order:{tariff.id}'),
            ],
            [
                InlineKeyboardButton(
                    text='🎁 В подарках: выключить' if gift else '🎁 В подарках: включить',
                    callback_data=f'admin_tariff_toggle_gift:{tariff.id}',
                )
            ],
            [
                InlineKeyboardButton(
                    text='📈 Докупка: запретить' if allow_topup else '📈 Докупка: разрешить',
                    callback_data=f'admin_tariff_toggle_allow_topup:{tariff.id}',
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff.id}')],
        ]
    )


async def _show(target: types.Message, tariff: Tariff, language: str, *, edit: bool, prefix: str = '') -> None:
    text = f'{prefix}{render_panel_settings(tariff)}'
    keyboard = get_panel_settings_keyboard(tariff, language)
    if edit:
        await target.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    else:
        await target.answer(text, reply_markup=keyboard, parse_mode='HTML')


@admin_required
@error_handler
async def show_panel_settings(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.clear()
    tariff = await get_tariff_by_id(db, int(callback.data.split(':')[1]))
    if not tariff:
        await callback.answer('Тариф не найден', show_alert=True)
        return
    await _show(callback.message, tariff, db_user.language, edit=True)
    await callback.answer()


# ---- переключатели ----


@admin_required
@error_handler
async def toggle_show_in_gift(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    tariff = await get_tariff_by_id(db, int(callback.data.split(':')[1]))
    if not tariff:
        await callback.answer('Тариф не найден', show_alert=True)
        return
    tariff = await update_tariff(db, tariff, show_in_gift=not getattr(tariff, 'show_in_gift', True))
    await callback.answer('Тариф показывается в подарках' if tariff.show_in_gift else 'Тариф скрыт из подарков')
    await _show(callback.message, tariff, db_user.language, edit=True)


@admin_required
@error_handler
async def toggle_allow_traffic_topup(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    tariff = await get_tariff_by_id(db, int(callback.data.split(':')[1]))
    if not tariff:
        await callback.answer('Тариф не найден', show_alert=True)
        return
    tariff = await update_tariff(db, tariff, allow_traffic_topup=not getattr(tariff, 'allow_traffic_topup', True))
    await callback.answer('Докупка трафика разрешена' if tariff.allow_traffic_topup else 'Докупка трафика запрещена')
    await _show(callback.message, tariff, db_user.language, edit=True)


# ---- текстовые поля ----


async def _start_field_edit(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    tariff: Tariff,
    *,
    state_value: State,
    title: str,
    current_value: str,
    prompt: str,
) -> None:
    await state.set_state(state_value)
    await state.update_data(tariff_id=tariff.id, language=db_user.language)
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        f'{title}\n\nТариф: <b>{html.escape(tariff.name)}</b>\nТекущее значение: <b>{current_value}</b>\n\n{prompt}',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_edit_more:{tariff.id}')]
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def start_edit_panel_tag(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    tariff = await get_tariff_by_id(db, int(callback.data.split(':')[1]))
    if not tariff:
        await callback.answer('Тариф не найден', show_alert=True)
        return
    await _start_field_edit(
        callback,
        db_user,
        state,
        tariff,
        state_value=AdminStates.editing_tariff_panel_tag,
        title='🏷️ <b>Тег панели</b>',
        current_value=_tag_display(tariff),
        prompt=f'Введите тег ({PANEL_TAG_RULES}) или <code>-</code>, чтобы вернуть общий тег из настроек.',
    )


@admin_required
@error_handler
async def start_edit_lava_product(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    tariff = await get_tariff_by_id(db, int(callback.data.split(':')[1]))
    if not tariff:
        await callback.answer('Тариф не найден', show_alert=True)
        return
    await _start_field_edit(
        callback,
        db_user,
        state,
        tariff,
        state_value=AdminStates.editing_tariff_lava_product,
        title='💳 <b>Продукт Lava (автопродление)</b>',
        current_value=_lava_display(tariff),
        prompt='Введите UUID продукта из кабинета Lava или <code>-</code>, чтобы отвязать.',
    )


@admin_required
@error_handler
async def start_edit_display_order(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    tariff = await get_tariff_by_id(db, int(callback.data.split(':')[1]))
    if not tariff:
        await callback.answer('Тариф не найден', show_alert=True)
        return
    await _start_field_edit(
        callback,
        db_user,
        state,
        tariff,
        state_value=AdminStates.editing_tariff_display_order,
        title='🔢 <b>Порядок в списке</b>',
        current_value=str(getattr(tariff, 'display_order', 0)),
        prompt='Введите порядок целым числом от 0 — меньше число, выше тариф в списке.',
    )


async def _load_tariff_from_state(message: types.Message, db: AsyncSession, state: FSMContext) -> Tariff | None:
    tariff_id = (await state.get_data()).get('tariff_id')
    tariff = await get_tariff_by_id(db, tariff_id) if tariff_id is not None else None
    if tariff is None:
        await message.answer('Тариф не найден')
        await state.clear()
    return tariff


async def _finish(message: types.Message, db_user: User, state: FSMContext, tariff: Tariff, confirmation: str) -> None:
    await state.clear()
    await _show(message, tariff, db_user.language, edit=False, prefix=f'{confirmation}\n\n')


@admin_required
@error_handler
async def process_panel_tag_input(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    tariff = await _load_tariff_from_state(message, db, state)
    if tariff is None:
        return
    raw = (message.text or '').strip()
    try:
        tag = None if raw == '-' else normalize_panel_tag(raw)
    except ValueError as error:
        await message.answer(f'❌ {error}. Попробуйте ещё раз:')
        return
    tariff = await update_tariff(db, tariff, panel_tag=tag)
    confirmation = f'✅ Тег панели: {html.escape(tag)}' if tag else '✅ Тег панели снят — общий из настроек'
    await _finish(message, db_user, state, tariff, confirmation)


@admin_required
@error_handler
async def process_lava_product_input(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    tariff = await _load_tariff_from_state(message, db, state)
    if tariff is None:
        return
    raw = (message.text or '').strip()
    # Пустая строка для CRUD значит «отвязать» — так же, как в кабинете.
    value = '' if raw == '-' else raw
    tariff = await update_tariff(db, tariff, lava_product_id=value)
    confirmation = f'✅ Продукт Lava: {html.escape(value)}' if value else '✅ Продукт Lava отвязан'
    await _finish(message, db_user, state, tariff, confirmation)


@admin_required
@error_handler
async def process_display_order_input(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    tariff = await _load_tariff_from_state(message, db, state)
    if tariff is None:
        return
    try:
        order = int((message.text or '').strip())
        if order < 0:
            raise ValueError
    except ValueError:
        await message.answer('❌ Введите целое число от 0. Попробуйте ещё раз:')
        return
    tariff = await update_tariff(db, tariff, display_order=order)
    await _finish(message, db_user, state, tariff, f'✅ Порядок в списке: {order}')


# ---- внешний сквад ----


async def load_external_squads() -> list:
    """Внешние сквады из панели; при сбое — пустой список (выбор «без сквада» остаётся)."""
    from app.services.remnawave_service import RemnaWaveService

    try:
        service = RemnaWaveService()
        async with service.get_api_client() as api:
            return list(await api.get_external_squads())
    except Exception as error:
        logger.warning('Не удалось получить внешние сквады из панели', error=str(error))
        return []


def _external_squad_keyboard(tariff: Tariff, squads: list, language: str) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    current = getattr(tariff, 'external_squad_uuid', None)
    buttons = [
        [
            InlineKeyboardButton(
                text=f'{"✅" if current is None else "⬜"} Без внешнего сквада',
                callback_data=f'admin_tariff_set_ext_squad:{tariff.id}:{_NO_EXTERNAL_SQUAD}',
            )
        ]
    ]
    for squad in squads:
        prefix = '✅' if squad.uuid == current else '⬜'
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f'{prefix} {squad.name}', callback_data=f'admin_tariff_set_ext_squad:{tariff.id}:{squad.uuid}'
                )
            ]
        )
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_edit_more:{tariff.id}')])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _external_squad_text(tariff: Tariff, squads: list) -> str:
    note = '' if squads else '\n\n⚠️ Список сквадов из панели пуст или панель недоступна.'
    return (
        f'🌐 <b>Внешний сквад Remnawave</b>\n\nТариф: <b>{html.escape(tariff.name)}</b>\n'
        f'Сейчас: <b>{_ext_squad_display(tariff)}</b>\n\n'
        'Пользователи тарифа попадают в выбранный сквад; смена применяется ко всем живым подпискам тарифа.'
        f'{note}'
    )


@admin_required
@error_handler
async def start_edit_external_squad(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.clear()
    tariff = await get_tariff_by_id(db, int(callback.data.split(':')[1]))
    if not tariff:
        await callback.answer('Тариф не найден', show_alert=True)
        return
    squads = await load_external_squads()
    await callback.message.edit_text(
        _external_squad_text(tariff, squads),
        reply_markup=_external_squad_keyboard(tariff, squads, db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def set_external_squad(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    _, tariff_id, chosen = callback.data.split(':', 2)
    tariff = await get_tariff_by_id(db, int(tariff_id))
    if not tariff:
        await callback.answer('Тариф не найден', show_alert=True)
        return

    new_uuid = None if chosen == _NO_EXTERNAL_SQUAD else chosen
    if new_uuid == getattr(tariff, 'external_squad_uuid', None):
        await callback.answer('Без изменений')
    else:
        tariff = await update_tariff(db, tariff, external_squad_uuid=new_uuid)
        # Как в кабинете: живые подписки тарифа получают новый сквад в фоне.
        schedule_tariff_squad_sync(tariff.id, db_user.id)
        await callback.answer('Внешний сквад обновлён, подписки синхронизируются в фоне')

    squads = await load_external_squads()
    await callback.message.edit_text(
        _external_squad_text(tariff, squads),
        reply_markup=_external_squad_keyboard(tariff, squads, db_user.language),
        parse_mode='HTML',
    )


def register_panel_settings_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(show_panel_settings, F.data.startswith('admin_tariff_edit_more:'))
    dp.callback_query.register(toggle_show_in_gift, F.data.startswith('admin_tariff_toggle_gift:'))
    dp.callback_query.register(toggle_allow_traffic_topup, F.data.startswith('admin_tariff_toggle_allow_topup:'))
    dp.callback_query.register(start_edit_panel_tag, F.data.startswith('admin_tariff_edit_panel_tag:'))
    dp.callback_query.register(start_edit_lava_product, F.data.startswith('admin_tariff_edit_lava:'))
    dp.callback_query.register(start_edit_display_order, F.data.startswith('admin_tariff_edit_order:'))
    dp.callback_query.register(start_edit_external_squad, F.data.startswith('admin_tariff_edit_ext_squad:'))
    dp.callback_query.register(set_external_squad, F.data.startswith('admin_tariff_set_ext_squad:'))
    dp.message.register(process_panel_tag_input, AdminStates.editing_tariff_panel_tag)
    dp.message.register(process_lava_product_input, AdminStates.editing_tariff_lava_product)
    dp.message.register(process_display_order_input, AdminStates.editing_tariff_display_order)
