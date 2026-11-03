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


def _yes_no(value: bool, language: str = 'ru') -> str:
    return (
        get_texts(language).t('ADMIN_TARIFF_HELPER_YES', '✅ Да')
        if value
        else get_texts(language).t('ADMIN_TARIFF_HELPER_NO', '❌ Нет')
    )


def _tag_display(tariff: Tariff, language: str = 'ru') -> str:
    return html.escape(
        getattr(tariff, 'panel_tag', None)
        or get_texts(language).t('ADMIN_TARIFF_HELPER_GLOBAL_TAG', 'общий из настроек')
    )


def _ext_squad_display(tariff: Tariff, language: str = 'ru') -> str:
    return html.escape(
        getattr(tariff, 'external_squad_uuid', None) or get_texts(language).t('ADMIN_TARIFF_HELPER_NONE', 'нет')
    )


def _lava_display(tariff: Tariff, language: str = 'ru') -> str:
    return html.escape(
        getattr(tariff, 'lava_product_id', None) or get_texts(language).t('ADMIN_TARIFF_HELPER_UNSET_LOWER', 'не задан')
    )


def format_panel_settings(tariff: Tariff, language: str = 'ru') -> str:
    """Блок для карточки тарифа."""
    return (
        get_texts(language)
        .t(
            'ADMIN_TARIFF_HELPER_FORMAT_PANEL_SETTINGS_1',
            '• Тег панели: {v0}\n• Внешний сквад: {v1}\n• Продукт Lava: {v2}\n• В подарках: {v3}\n• Докупка трафика разрешена: {v4}',
        )
        .format(
            v0=_tag_display(tariff, language=language),
            v1=_ext_squad_display(tariff, language=language),
            v2=_lava_display(tariff, language=language),
            v3=_yes_no(getattr(tariff, 'show_in_gift', True), language=language),
            v4=_yes_no(getattr(tariff, 'allow_traffic_topup', True), language=language),
        )
    )


def render_panel_settings(tariff: Tariff, language: str = 'ru') -> str:
    return (
        get_texts(language)
        .t(
            'ADMIN_TARIFF_HELPER_RENDER_PANEL_SETTINGS_1',
            '⚙️ <b>Ещё настройки</b>\n\nТариф: <b>{v0}</b>\n\n{v1}\n• Порядок в списке: {v2}\n\nТег панели показывается у пользователя в списке панели Remnawave — по нему видно тариф. Пусто — общий тег из настроек (триальный/платный).',
        )
        .format(
            v0=html.escape(tariff.name),
            v1=format_panel_settings(tariff, language=language),
            v2=getattr(tariff, 'display_order', 0),
        )
    )


def get_panel_settings_keyboard(tariff: Tariff, language: str) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    gift = getattr(tariff, 'show_in_gift', True)
    allow_topup = getattr(tariff, 'allow_traffic_topup', True)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=get_texts(language).t('ADMIN_TARIFF_HELPER_TAG_BUTTON', '🏷️ Тег панели'),
                    callback_data=f'admin_tariff_edit_panel_tag:{tariff.id}',
                ),
                InlineKeyboardButton(
                    text=get_texts(language).t('ADMIN_TARIFF_HELPER_SQUAD_BUTTON', '🌐 Внешний сквад'),
                    callback_data=f'admin_tariff_edit_ext_squad:{tariff.id}',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=get_texts(language).t('ADMIN_TARIFF_HELPER_LAVA_BUTTON', '💳 Продукт Lava'),
                    callback_data=f'admin_tariff_edit_lava:{tariff.id}',
                ),
                InlineKeyboardButton(
                    text=get_texts(language).t('ADMIN_TARIFF_HELPER_ORDER_BUTTON', '🔢 Порядок'),
                    callback_data=f'admin_tariff_edit_order:{tariff.id}',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=get_texts(language).t('ADMIN_TARIFF_HELPER_GIFT_DISABLE_BUTTON', '🎁 В подарках: выключить')
                    if gift
                    else get_texts(language).t('ADMIN_TARIFF_HELPER_GIFT_ENABLE_BUTTON', '🎁 В подарках: включить'),
                    callback_data=f'admin_tariff_toggle_gift:{tariff.id}',
                )
            ],
            [
                InlineKeyboardButton(
                    text=get_texts(language).t('ADMIN_TARIFF_HELPER_TOPUP_DISABLE_BUTTON', '📈 Докупка: запретить')
                    if allow_topup
                    else get_texts(language).t('ADMIN_TARIFF_HELPER_TOPUP_ENABLE_BUTTON', '📈 Докупка: разрешить'),
                    callback_data=f'admin_tariff_toggle_allow_topup:{tariff.id}',
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff.id}')],
        ]
    )


async def _show(target: types.Message, tariff: Tariff, language: str, *, edit: bool, prefix: str = '') -> None:
    text = f'{prefix}{render_panel_settings(tariff, language=language)}'
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
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return
    await _show(callback.message, tariff, db_user.language, edit=True)
    await callback.answer()


# ---- переключатели ----


@admin_required
@error_handler
async def toggle_show_in_gift(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    tariff = await get_tariff_by_id(db, int(callback.data.split(':')[1]))
    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return
    tariff = await update_tariff(db, tariff, show_in_gift=not getattr(tariff, 'show_in_gift', True))
    await callback.answer(
        get_texts(db_user.language).t('ADMIN_TARIFF_HELPER_GIFT_ENABLED', 'Тариф показывается в подарках')
        if tariff.show_in_gift
        else get_texts(db_user.language).t('ADMIN_TARIFF_HELPER_GIFT_DISABLED', 'Тариф скрыт из подарков')
    )
    await _show(callback.message, tariff, db_user.language, edit=True)


@admin_required
@error_handler
async def toggle_allow_traffic_topup(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    tariff = await get_tariff_by_id(db, int(callback.data.split(':')[1]))
    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return
    tariff = await update_tariff(db, tariff, allow_traffic_topup=not getattr(tariff, 'allow_traffic_topup', True))
    await callback.answer(
        get_texts(db_user.language).t('ADMIN_TARIFF_HELPER_TOPUP_ALLOWED', 'Докупка трафика разрешена')
        if tariff.allow_traffic_topup
        else get_texts(db_user.language).t('ADMIN_TARIFF_HELPER_TOPUP_PROHIBITED', 'Докупка трафика запрещена')
    )
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
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_HELPER_START_FIELD_EDIT_1',
            '{v0}\n\nТариф: <b>{v1}</b>\nТекущее значение: <b>{v2}</b>\n\n{v3}',
        )
        .format(v0=title, v1=html.escape(tariff.name), v2=current_value, v3=prompt),
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
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return
    await _start_field_edit(
        callback,
        db_user,
        state,
        tariff,
        state_value=AdminStates.editing_tariff_panel_tag,
        title=get_texts(db_user.language).t('ADMIN_TARIFF_HELPER_TAG_TITLE', '🏷️ <b>Тег панели</b>'),
        current_value=_tag_display(tariff, language=db_user.language),
        prompt=get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_HELPER_START_EDIT_PANEL_TAG_1',
            'Введите тег ({v0}) или <code>-</code>, чтобы вернуть общий тег из настроек.',
        )
        .format(v0=get_texts(db_user.language).t('ADMIN_TARIFF_VALIDATION_PANEL_TAG_RULES', PANEL_TAG_RULES)),
    )


@admin_required
@error_handler
async def start_edit_lava_product(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    tariff = await get_tariff_by_id(db, int(callback.data.split(':')[1]))
    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return
    await _start_field_edit(
        callback,
        db_user,
        state,
        tariff,
        state_value=AdminStates.editing_tariff_lava_product,
        title=get_texts(db_user.language).t('ADMIN_TARIFF_HELPER_LAVA_TITLE', '💳 <b>Продукт Lava (автопродление)</b>'),
        current_value=_lava_display(tariff, language=db_user.language),
        prompt=get_texts(db_user.language).t(
            'ADMIN_TARIFF_HELPER_LAVA_PROMPT',
            'Введите UUID продукта из кабинета Lava или <code>-</code>, чтобы отвязать.',
        ),
    )


@admin_required
@error_handler
async def start_edit_display_order(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    tariff = await get_tariff_by_id(db, int(callback.data.split(':')[1]))
    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return
    await _start_field_edit(
        callback,
        db_user,
        state,
        tariff,
        state_value=AdminStates.editing_tariff_display_order,
        title=get_texts(db_user.language).t('ADMIN_TARIFF_HELPER_ORDER_TITLE', '🔢 <b>Порядок в списке</b>'),
        current_value=str(getattr(tariff, 'display_order', 0)),
        prompt=get_texts(db_user.language).t(
            'ADMIN_TARIFF_HELPER_ORDER_PROMPT', 'Введите порядок целым числом от 0 — меньше число, выше тариф в списке.'
        ),
    )


async def _load_tariff_from_state(
    message: types.Message, db: AsyncSession, state: FSMContext, language: str = 'ru'
) -> Tariff | None:
    tariff_id = (await state.get_data()).get('tariff_id')
    tariff = await get_tariff_by_id(db, tariff_id) if tariff_id is not None else None
    if tariff is None:
        await message.answer(get_texts(language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'))
        await state.clear()
    return tariff


async def _finish(message: types.Message, db_user: User, state: FSMContext, tariff: Tariff, confirmation: str) -> None:
    await state.clear()
    await _show(message, tariff, db_user.language, edit=False, prefix=f'{confirmation}\n\n')


@admin_required
@error_handler
async def process_panel_tag_input(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    tariff = await _load_tariff_from_state(message, db, state, language=db_user.language)
    if tariff is None:
        return
    raw = (message.text or '').strip()
    try:
        tag = None if raw == '-' else normalize_panel_tag(raw)
    except ValueError:
        await message.answer(
            get_texts(db_user.language)
            .t('ADMIN_TARIFF_HELPER_PROCESS_PANEL_TAG_INPUT_2', '❌ {v0}. Попробуйте ещё раз:')
            .format(
                v0=get_texts(db_user.language).t(
                    'ADMIN_TARIFF_VALIDATION_PANEL_TAG_ERROR', f'Тег панели: {PANEL_TAG_RULES}'
                )
            )
        )
        return
    tariff = await update_tariff(db, tariff, panel_tag=tag)
    confirmation = (
        get_texts(db_user.language)
        .t('ADMIN_TARIFF_HELPER_PROCESS_PANEL_TAG_INPUT_1', '✅ Тег панели: {v0}')
        .format(v0=html.escape(tag))
        if tag
        else get_texts(db_user.language).t('ADMIN_TARIFF_HELPER_TAG_REMOVED', '✅ Тег панели снят — общий из настроек')
    )
    await _finish(message, db_user, state, tariff, confirmation)


@admin_required
@error_handler
async def process_lava_product_input(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    tariff = await _load_tariff_from_state(message, db, state, language=db_user.language)
    if tariff is None:
        return
    raw = (message.text or '').strip()
    # Пустая строка для CRUD значит «отвязать» — так же, как в кабинете.
    value = '' if raw == '-' else raw
    tariff = await update_tariff(db, tariff, lava_product_id=value)
    confirmation = (
        get_texts(db_user.language)
        .t('ADMIN_TARIFF_HELPER_PROCESS_LAVA_PRODUCT_INPUT_1', '✅ Продукт Lava: {v0}')
        .format(v0=html.escape(value))
        if value
        else get_texts(db_user.language).t('ADMIN_TARIFF_HELPER_LAVA_REMOVED', '✅ Продукт Lava отвязан')
    )
    await _finish(message, db_user, state, tariff, confirmation)


@admin_required
@error_handler
async def process_display_order_input(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    tariff = await _load_tariff_from_state(message, db, state, language=db_user.language)
    if tariff is None:
        return
    try:
        order = int((message.text or '').strip())
        if order < 0:
            raise ValueError
    except ValueError:
        await message.answer(
            get_texts(db_user.language).t(
                'ADMIN_TARIFF_HELPER_INVALID_ORDER', '❌ Введите целое число от 0. Попробуйте ещё раз:'
            )
        )
        return
    tariff = await update_tariff(db, tariff, display_order=order)
    await _finish(
        message,
        db_user,
        state,
        tariff,
        get_texts(db_user.language)
        .t('ADMIN_TARIFF_HELPER_PROCESS_DISPLAY_ORDER_INPUT_1', '✅ Порядок в списке: {v0}')
        .format(v0=order),
    )


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
                text=get_texts(language)
                .t('ADMIN_TARIFF_HELPER_EXTERNAL_SQUAD_KEYBOARD_1', '{v0} Без внешнего сквада')
                .format(v0='✅' if current is None else '⬜'),
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


def _external_squad_text(tariff: Tariff, squads: list, language: str = 'ru') -> str:
    note = (
        ''
        if squads
        else get_texts(language).t(
            'ADMIN_TARIFF_HELPER_NO_EXTERNAL_SQUADS', '\n\n⚠️ Список сквадов из панели пуст или панель недоступна.'
        )
    )
    return (
        get_texts(language)
        .t(
            'ADMIN_TARIFF_HELPER_EXTERNAL_SQUAD_TEXT_1',
            '🌐 <b>Внешний сквад Remnawave</b>\n\nТариф: <b>{v0}</b>\nСейчас: <b>{v1}</b>\n\nПользователи тарифа попадают в выбранный сквад; смена применяется ко всем живым подпискам тарифа.{v2}',
        )
        .format(v0=html.escape(tariff.name), v1=_ext_squad_display(tariff, language=language), v2=note)
    )


@admin_required
@error_handler
async def start_edit_external_squad(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.clear()
    tariff = await get_tariff_by_id(db, int(callback.data.split(':')[1]))
    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return
    squads = await load_external_squads()
    await callback.message.edit_text(
        _external_squad_text(tariff, squads, language=db_user.language),
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
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    new_uuid = None if chosen == _NO_EXTERNAL_SQUAD else chosen
    if new_uuid == getattr(tariff, 'external_squad_uuid', None):
        await callback.answer(get_texts(db_user.language).t('ADMIN_TARIFF_HELPER_UNCHANGED', 'Без изменений'))
    else:
        tariff = await update_tariff(db, tariff, external_squad_uuid=new_uuid)
        # Как в кабинете: живые подписки тарифа получают новый сквад в фоне.
        schedule_tariff_squad_sync(tariff.id, db_user.id)
        await callback.answer(
            get_texts(db_user.language).t(
                'ADMIN_TARIFF_HELPER_SQUAD_UPDATED', 'Внешний сквад обновлён, подписки синхронизируются в фоне'
            )
        )

    squads = await load_external_squads()
    await callback.message.edit_text(
        _external_squad_text(tariff, squads, language=db_user.language),
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
