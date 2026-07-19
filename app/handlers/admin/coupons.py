import html
from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.coupon import (
    create_coupon_batch,
    get_batch_coupon_tokens,
    get_batch_status_counts,
    get_coupon_batch_by_id,
    get_coupon_batches,
    get_coupon_batches_count,
    revoke_batch_coupons,
)
from app.database.crud.tariff import get_all_active_tariffs, get_tariff_by_id
from app.database.models import CouponBatch, CouponStatus, User
from app.keyboards.admin import get_admin_pagination_keyboard
from app.services.coupon_service import build_coupon_deeplink
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler
from app.utils.formatters import format_datetime


logger = structlog.get_logger(__name__)

MAX_COUPONS_PER_BATCH = 500
MAX_PERIOD_DAYS = 3650
MAX_WHOLESALE_PRICE_RUBLES = 10_000_000

# Admin ids with a batch-creation in flight. aiogram dispatches updates
# concurrently, so the FSM check→clear alone is not atomic against a double tap;
# the check+add below run with no await between them and close that window.
_batch_creation_in_progress: set[int] = set()

_CANCEL_KEYBOARD = types.InlineKeyboardMarkup(
    inline_keyboard=[[types.InlineKeyboardButton(text='❌ Отмена', callback_data='admin_coupons')]]
)


def _format_batch_button(batch: CouponBatch) -> str:
    revoked_mark = '⛔ ' if batch.is_revoked else ''
    return f'{revoked_mark}#{batch.id} {batch.name} • {batch.period_days} дн. • {batch.coupons_total} шт.'


def _format_batch_card(batch: CouponBatch, counts: dict[str, int]) -> str:
    active = counts.get(CouponStatus.ACTIVE.value, 0)
    redeemed = counts.get(CouponStatus.REDEEMED.value, 0)
    revoked = counts.get(CouponStatus.REVOKED.value, 0)
    tariff_name = html.escape(batch.tariff.name) if batch.tariff else '—'

    text = (
        f'🎟 <b>Партия купонов #{batch.id}</b>\n\n'
        f'📌 Название: {html.escape(batch.name)}\n'
        f'📦 Тариф: {tariff_name} — {batch.period_days} дн.\n'
        f'🎫 Купонов: {batch.coupons_total}\n'
    )

    if batch.wholesale_price_kopeks > 0:
        total_kopeks = batch.wholesale_price_kopeks * batch.coupons_total
        text += (
            f'💰 Опт: {settings.format_price(batch.wholesale_price_kopeks)}/шт '
            f'(итого {settings.format_price(total_kopeks)})\n'
        )

    text += f'\n📊 <b>Статусы:</b>\n✅ Активных: {active}\n🎫 Погашено: {redeemed}\n⛔ Отозвано: {revoked}\n\n'

    if batch.valid_until:
        text += f'⏰ Действует до: {format_datetime(batch.valid_until)}\n'
    else:
        text += '⏰ Действует: бессрочно\n'

    text += f'📅 Создана: {format_datetime(batch.created_at)}\n'

    if batch.is_revoked:
        text += '\n⛔ <b>Партия отозвана</b>\n'

    return text


def _batch_card_keyboard(batch: CouponBatch, counts: dict[str, int]) -> types.InlineKeyboardMarkup:
    keyboard = []
    if counts.get(CouponStatus.ACTIVE.value, 0) > 0:
        keyboard.append(
            [types.InlineKeyboardButton(text='📄 Файл со ссылками', callback_data=f'admin_coupon_export_{batch.id}')]
        )
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text='⛔ Отозвать непогашенные', callback_data=f'admin_coupon_revoke_{batch.id}'
                )
            ]
        )
    keyboard.append([types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_coupons')])
    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)


async def _load_batch(callback: types.CallbackQuery, db: AsyncSession) -> CouponBatch | None:
    """Parse the batch id from callback data and fetch the batch, alerting on errors."""
    try:
        batch_id = int(callback.data.split('_')[-1])
    except ValueError:
        await callback.answer('❌ Ошибка получения ID партии', show_alert=True)
        return None

    batch = await get_coupon_batch_by_id(db, batch_id)
    if not batch:
        await callback.answer('❌ Партия не найдена', show_alert=True)
        return None
    return batch


async def _show_batch_card(callback: types.CallbackQuery, db: AsyncSession, batch: CouponBatch) -> None:
    counts = await get_batch_status_counts(db, batch.id)
    await callback.message.edit_text(
        _format_batch_card(batch, counts), reply_markup=_batch_card_keyboard(batch, counts)
    )


async def _read_int(message: types.Message, lo: int, hi: int, error_text: str) -> int | None:
    """Parse an int in [lo, hi] from the message; reply with ``error_text`` and return None otherwise."""
    try:
        value = int((message.text or '').strip())
    except ValueError:
        value = None
    if value is None or not lo <= value <= hi:
        await message.answer(error_text, reply_markup=_CANCEL_KEYBOARD)
        return None
    return value


async def _render_coupons_menu(
    db: AsyncSession, language: str, page: int = 1
) -> tuple[str, types.InlineKeyboardMarkup]:
    limit = 10
    offset = (page - 1) * limit

    batches = await get_coupon_batches(db, offset=offset, limit=limit)
    total_count = await get_coupon_batches_count(db)
    total_pages = max(1, (total_count + limit - 1) // limit)

    text = (
        f'🎟 <b>Купоны</b>\n\n'
        f'Оптовая продажа подписок через партнёров: партия одноразовых ссылок '
        f'на тариф, каждая выдаёт или продлевает подписку на N дней.\n\n'
        f'📊 Партий: {total_count}'
    )
    if total_pages > 1:
        text += f' (стр. {page}/{total_pages})'

    keyboard = [
        [types.InlineKeyboardButton(text=_format_batch_button(batch), callback_data=f'admin_coupon_manage_{batch.id}')]
        for batch in batches
    ]

    if total_pages > 1:
        pagination_row = get_admin_pagination_keyboard(
            page, total_pages, 'admin_coupon_list', 'admin_coupons', language
        ).inline_keyboard[0]
        keyboard.append(pagination_row)

    keyboard.extend(
        [
            [types.InlineKeyboardButton(text='➕ Создать партию', callback_data='admin_coupon_create')],
            [types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_submenu_promo')],
        ]
    )

    return text, types.InlineKeyboardMarkup(inline_keyboard=keyboard)


@admin_required
@error_handler
async def show_coupons_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.clear()
    text, keyboard = await _render_coupons_menu(db, db_user.language)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def handle_coupon_list_page(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    try:
        page = int(callback.data.split('_')[-1])
    except ValueError:
        page = 1
    text, keyboard = await _render_coupons_menu(db, db_user.language, page=page)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def start_coupon_batch_creation(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    tariffs = await get_all_active_tariffs(db)
    if not tariffs:
        await callback.answer('❌ Нет активных тарифов. Сначала создайте тариф.', show_alert=True)
        return

    keyboard = [
        [types.InlineKeyboardButton(text=tariff.name, callback_data=f'coupon_batch_tariff_{tariff.id}')]
        for tariff in tariffs
    ]
    keyboard.append([types.InlineKeyboardButton(text='❌ Отмена', callback_data='admin_coupons')])

    await callback.message.edit_text(
        '🎟 <b>Создание партии купонов</b>\n\nВыберите тариф, который будут выдавать купоны:',
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
    )
    await callback.answer()


@admin_required
@error_handler
async def select_coupon_batch_tariff(callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession):
    try:
        tariff_id = int(callback.data.split('_')[-1])
    except ValueError:
        await callback.answer('❌ Ошибка получения ID тарифа', show_alert=True)
        return

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff or not tariff.is_active:
        await callback.answer('❌ Тариф не найден или неактивен', show_alert=True)
        return

    await state.update_data(coupon_tariff_id=tariff.id, coupon_tariff_name=tariff.name)

    await callback.message.edit_text(
        f'🎟 <b>Создание партии купонов</b>\n\n'
        f'Тариф: {html.escape(tariff.name)}\n\n'
        f'📅 Введите количество дней подписки на купон (1-{MAX_PERIOD_DAYS}):',
        reply_markup=_CANCEL_KEYBOARD,
    )
    await state.set_state(AdminStates.creating_coupon_batch_days)
    await callback.answer()


@admin_required
@error_handler
async def process_coupon_batch_days(message: types.Message, db_user: User, state: FSMContext):
    days = await _read_int(message, 1, MAX_PERIOD_DAYS, f'❌ Введите целое число дней от 1 до {MAX_PERIOD_DAYS}')
    if days is None:
        return

    await state.update_data(coupon_period_days=days)
    await message.answer(
        f'🎫 Введите количество купонов в партии (1-{MAX_COUPONS_PER_BATCH}):',
        reply_markup=_CANCEL_KEYBOARD,
    )
    await state.set_state(AdminStates.creating_coupon_batch_count)


@admin_required
@error_handler
async def process_coupon_batch_count(message: types.Message, db_user: User, state: FSMContext):
    count = await _read_int(
        message, 1, MAX_COUPONS_PER_BATCH, f'❌ Введите целое число купонов от 1 до {MAX_COUPONS_PER_BATCH}'
    )
    if count is None:
        return

    await state.update_data(coupon_count=count)
    await message.answer(
        '📌 Введите название партии (например, имя партнёра):',
        reply_markup=_CANCEL_KEYBOARD,
    )
    await state.set_state(AdminStates.creating_coupon_batch_name)


@admin_required
@error_handler
async def process_coupon_batch_name(message: types.Message, db_user: User, state: FSMContext):
    name = (message.text or '').strip()
    if not name or len(name) > 255:
        await message.answer('❌ Название должно быть от 1 до 255 символов', reply_markup=_CANCEL_KEYBOARD)
        return

    await state.update_data(coupon_batch_name=name)
    await message.answer(
        '💰 Введите оптовую цену за купон в рублях — только для учёта (0 — не указывать):',
        reply_markup=_CANCEL_KEYBOARD,
    )
    await state.set_state(AdminStates.creating_coupon_batch_price)


@admin_required
@error_handler
async def process_coupon_batch_price(message: types.Message, db_user: User, state: FSMContext):
    try:
        rubles = float((message.text or '').strip().replace(',', '.').replace(' ', ''))
    except ValueError:
        await message.answer('❌ Введите цену числом (например, 150 или 99.50)', reply_markup=_CANCEL_KEYBOARD)
        return
    # Inverted range check: also rejects NaN (all comparisons with NaN are False)
    if not 0 <= rubles <= MAX_WHOLESALE_PRICE_RUBLES:
        await message.answer(
            f'❌ Цена должна быть от 0 до {MAX_WHOLESALE_PRICE_RUBLES} рублей', reply_markup=_CANCEL_KEYBOARD
        )
        return

    await state.update_data(coupon_price_kopeks=int(round(rubles * 100)))
    await message.answer(
        '⏰ Введите срок действия купонов в днях (0 — бессрочно):',
        reply_markup=_CANCEL_KEYBOARD,
    )
    await state.set_state(AdminStates.creating_coupon_batch_expiry)


@admin_required
@error_handler
async def process_coupon_batch_expiry(message: types.Message, db_user: User, state: FSMContext):
    expiry_days = await _read_int(
        message, 0, MAX_PERIOD_DAYS, f'❌ Введите число дней от 0 до {MAX_PERIOD_DAYS} (0 — бессрочно)'
    )
    if expiry_days is None:
        return

    await state.update_data(coupon_expiry_days=expiry_days)

    data = await state.get_data()
    price_kopeks = data.get('coupon_price_kopeks', 0)
    price_line = f'💰 Опт: {settings.format_price(price_kopeks)}/шт\n' if price_kopeks > 0 else ''
    expiry_line = f'⏰ Срок: {expiry_days} дн.\n' if expiry_days > 0 else '⏰ Срок: бессрочно\n'

    await message.answer(
        f'🎟 <b>Подтвердите создание партии</b>\n\n'
        f'📌 Название: {html.escape(data.get("coupon_batch_name", ""))}\n'
        f'📦 Тариф: {html.escape(data.get("coupon_tariff_name", ""))} — {data.get("coupon_period_days")} дн.\n'
        f'🎫 Купонов: {data.get("coupon_count")}\n'
        f'{price_line}'
        f'{expiry_line}',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='✅ Создать', callback_data='admin_coupon_create_confirm')],
                [types.InlineKeyboardButton(text='❌ Отмена', callback_data='admin_coupons')],
            ]
        ),
    )


@admin_required
@error_handler
async def confirm_coupon_batch_creation(
    callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession
):
    # Synchronous double-submit guard: the check+add run with no await between
    # them, so two concurrently-dispatched taps can't both pass (the FSM
    # check→clear below is not atomic on its own).
    if db_user.id in _batch_creation_in_progress:
        await callback.answer('⏳ Партия уже создаётся, подождите', show_alert=True)
        return
    _batch_creation_in_progress.add(db_user.id)
    try:
        # An old confirmation message keeps a live button: accept it only while the
        # wizard is actually waiting on this confirmation, otherwise a stale tap
        # would create a batch from half-entered state of a NEW wizard run.
        current_state = await state.get_state()
        if current_state != AdminStates.creating_coupon_batch_expiry.state:
            await callback.answer('❌ Данные создания устарели, начните заново', show_alert=True)
            return

        data = await state.get_data()
        tariff_id = data.get('coupon_tariff_id')
        period_days = data.get('coupon_period_days')
        count = data.get('coupon_count')
        name = data.get('coupon_batch_name')
        expiry_days = data.get('coupon_expiry_days')

        if not all([tariff_id, period_days, count, name]) or expiry_days is None:
            await callback.answer('❌ Данные создания устарели, начните заново', show_alert=True)
            await state.clear()
            return

        # Consume the wizard state BEFORE the (slow) batch insert — a double-tap on
        # the confirm button must not create the batch twice.
        await state.clear()

        tariff = await get_tariff_by_id(db, tariff_id)
        if not tariff or not tariff.is_active:
            await callback.answer('❌ Тариф не найден или неактивен', show_alert=True)
            return

        valid_until = datetime.now(UTC) + timedelta(days=expiry_days) if expiry_days else None

        batch = await create_coupon_batch(
            db,
            name=name,
            tariff_id=tariff.id,
            period_days=period_days,
            coupons_count=count,
            wholesale_price_kopeks=data.get('coupon_price_kopeks', 0),
            valid_until=valid_until,
            created_by=db_user.id,
        )

        logger.info(
            'Создана партия купонов',
            batch_id=batch.id,
            tariff_id=tariff.id,
            period_days=period_days,
            count=count,
            created_by=db_user.id,
        )

        await _show_batch_card(callback, db, batch)
        await _send_batch_links_file(callback, db, batch)
        await callback.answer('✅ Партия создана')
    finally:
        _batch_creation_in_progress.discard(db_user.id)


@admin_required
@error_handler
async def show_coupon_batch(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    batch = await _load_batch(callback, db)
    if batch is None:
        return
    await _show_batch_card(callback, db, batch)
    await callback.answer()


async def _send_batch_links_file(callback: types.CallbackQuery, db: AsyncSession, batch: CouponBatch) -> bool:
    """Send a .txt with the deep links of all still-active coupons of the batch.

    Returns True if the document was sent; False if the batch has no active
    coupons (in which case the callback is already answered with an alert, so
    the caller must NOT answer it again — Telegram rejects a double answer).
    """
    tokens = await get_batch_coupon_tokens(db, batch.id, status=CouponStatus.ACTIVE.value)
    if not tokens:
        await callback.answer('❌ В партии нет активных купонов', show_alert=True)
        return False

    # The username is synced into settings at startup; get_me() is a fallback
    # to avoid an extra Bot API round trip on every export.
    bot_username = settings.get_bot_username() or (await callback.bot.get_me()).username
    content = '\n'.join(build_coupon_deeplink(bot_username, token) for token in tokens) + '\n'
    file = types.BufferedInputFile(content.encode('utf-8'), filename=f'coupons_batch_{batch.id}.txt')
    await callback.message.answer_document(
        document=file,
        caption=(
            f'🎟 Партия #{batch.id} «{html.escape(batch.name)}»: '
            f'{len(tokens)} активных купонов, {batch.period_days} дн. каждый.'
        ),
    )
    return True


@admin_required
@error_handler
async def export_coupon_batch(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    batch = await _load_batch(callback, db)
    if batch is None:
        return
    if await _send_batch_links_file(callback, db, batch):
        await callback.answer()


@admin_required
@error_handler
async def ask_revoke_coupon_batch(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    batch = await _load_batch(callback, db)
    if batch is None:
        return

    counts = await get_batch_status_counts(db, batch.id)
    active = counts.get(CouponStatus.ACTIVE.value, 0)

    await callback.message.edit_text(
        f'⛔ <b>Отзыв партии #{batch.id}</b>\n\n'
        f'«{html.escape(batch.name)}»: будет отозвано {active} непогашенных купонов. '
        f'Их ссылки перестанут работать. Действие необратимо.\n\n'
        f'Подтвердить?',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text='⛔ Да, отозвать', callback_data=f'admin_coupon_revoke_confirm_{batch.id}'
                    )
                ],
                [types.InlineKeyboardButton(text='❌ Отмена', callback_data=f'admin_coupon_manage_{batch.id}')],
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def confirm_revoke_coupon_batch(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    batch = await _load_batch(callback, db)
    if batch is None:
        return

    revoked_count = await revoke_batch_coupons(db, batch)
    logger.info(
        'Партия купонов отозвана',
        batch_id=batch.id,
        revoked_count=revoked_count,
        admin_id=db_user.id,
    )

    await _show_batch_card(callback, db, batch)
    await callback.answer(f'⛔ Отозвано купонов: {revoked_count}')


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_coupons_menu, F.data == 'admin_coupons')
    dp.callback_query.register(handle_coupon_list_page, F.data.startswith('admin_coupon_list_page_'))
    dp.callback_query.register(start_coupon_batch_creation, F.data == 'admin_coupon_create')
    dp.callback_query.register(confirm_coupon_batch_creation, F.data == 'admin_coupon_create_confirm')
    dp.callback_query.register(select_coupon_batch_tariff, F.data.startswith('coupon_batch_tariff_'))
    dp.callback_query.register(show_coupon_batch, F.data.startswith('admin_coupon_manage_'))
    dp.callback_query.register(export_coupon_batch, F.data.startswith('admin_coupon_export_'))
    # NB: register the *_revoke_confirm_ handler before the *_revoke_ one —
    # the shorter prefix also matches confirmation callbacks.
    dp.callback_query.register(confirm_revoke_coupon_batch, F.data.startswith('admin_coupon_revoke_confirm_'))
    dp.callback_query.register(ask_revoke_coupon_batch, F.data.startswith('admin_coupon_revoke_'))

    dp.message.register(process_coupon_batch_days, AdminStates.creating_coupon_batch_days)
    dp.message.register(process_coupon_batch_count, AdminStates.creating_coupon_batch_count)
    dp.message.register(process_coupon_batch_name, AdminStates.creating_coupon_batch_name)
    dp.message.register(process_coupon_batch_price, AdminStates.creating_coupon_batch_price)
    dp.message.register(process_coupon_batch_expiry, AdminStates.creating_coupon_batch_expiry)
