import html
from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.coupon import (
    create_coupon_batch,
    delete_coupon_batch,
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
from app.localization.texts import get_texts
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


def _cancel_keyboard(language: str = 'ru') -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=get_texts(language).t('ADMIN_COUPONS_UI_START_COUPON_BATCH_CREATION_020', '❌ Отмена'),
                    callback_data='admin_coupons',
                ),
            ]
        ]
    )


def _format_batch_button(batch: CouponBatch, language: str = 'ru') -> str:
    texts = get_texts(language)
    revoked_mark = '⛔ ' if batch.is_revoked else ''
    return texts.t(
        'ADMIN_COUPONS_UI_FORMAT_BATCH_BUTTON_000', '{value1}#{value2} {value3} • {value4} дн. • {value5} шт.'
    ).format(
        value1=revoked_mark, value2=batch.id, value3=batch.name, value4=batch.period_days, value5=batch.coupons_total
    )


def _format_batch_card(batch: CouponBatch, counts: dict[str, int], language: str = 'ru') -> str:
    texts = get_texts(language)
    active = counts.get(CouponStatus.ACTIVE.value, 0)
    redeemed = counts.get(CouponStatus.REDEEMED.value, 0)
    revoked = counts.get(CouponStatus.REVOKED.value, 0)
    tariff_name = html.escape(batch.tariff.name) if batch.tariff else '—'

    text = texts.t(
        'ADMIN_COUPONS_UI_FORMAT_BATCH_CARD_001',
        '🎟 <b>Партия купонов #{value1}</b>\n\n📌 Название: {value2}\n📦 Тариф: {value3} — {value4} дн.\n🎫 Купонов: {value5}\n',
    ).format(
        value1=batch.id,
        value2=html.escape(batch.name),
        value3=tariff_name,
        value4=batch.period_days,
        value5=batch.coupons_total,
    )

    if batch.wholesale_price_kopeks > 0:
        total_kopeks = batch.wholesale_price_kopeks * batch.coupons_total
        text += texts.t('ADMIN_COUPONS_UI_FORMAT_BATCH_CARD_002', '💰 Опт: {value1}/шт (итого {value2})\n').format(
            value1=settings.format_price(batch.wholesale_price_kopeks), value2=settings.format_price(total_kopeks)
        )

    text += texts.t(
        'ADMIN_COUPONS_UI_FORMAT_BATCH_CARD_003',
        '\n📊 <b>Статусы:</b>\n✅ Активных: {value1}\n🎫 Погашено: {value2}\n⛔ Отозвано: {value3}\n\n',
    ).format(value1=active, value2=redeemed, value3=revoked)

    max_per_user = getattr(batch, 'max_per_user', 0) or 0
    if max_per_user > 0:
        text += texts.t('ADMIN_COUPONS_UI_FORMAT_BATCH_CARD_004', '👤 На пользователя: не более {value1} шт.\n').format(
            value1=max_per_user
        )

    if batch.valid_until:
        text += texts.t('ADMIN_COUPONS_UI_FORMAT_BATCH_CARD_005', '⏰ Действует до: {value1}\n').format(
            value1=format_datetime(batch.valid_until)
        )
    else:
        text += texts.t('ADMIN_COUPONS_UI_FORMAT_BATCH_CARD_006', '⏰ Действует: бессрочно\n')

    text += texts.t('ADMIN_COUPONS_UI_FORMAT_BATCH_CARD_007', '📅 Создана: {value1}\n').format(
        value1=format_datetime(batch.created_at)
    )

    if batch.is_revoked:
        text += texts.t('ADMIN_COUPONS_UI_FORMAT_BATCH_CARD_008', '\n⛔ <b>Партия отозвана</b>\n')

    return text


def _batch_card_keyboard(
    batch: CouponBatch, counts: dict[str, int], language: str = 'ru'
) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = []
    if counts.get(CouponStatus.ACTIVE.value, 0) > 0:
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_COUPONS_UI_BATCH_CARD_KEYBOARD_009', '📄 Файл со ссылками'),
                    callback_data=f'admin_coupon_export_{batch.id}',
                )
            ]
        )
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_COUPONS_UI_BATCH_CARD_KEYBOARD_010', '⛔ Отозвать непогашенные'),
                    callback_data=f'admin_coupon_revoke_{batch.id}',
                )
            ]
        )
    # Удаление доступно всегда (в т.ч. у отозванной/использованной партии) —
    # это способ убрать запись совсем, отзыв лишь гасит ссылки.
    keyboard.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_COUPONS_UI_BATCH_CARD_KEYBOARD_011', '🗑 Удалить партию'),
                callback_data=f'admin_coupon_delete_{batch.id}',
            )
        ]
    )
    keyboard.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_COUPONS_UI_BATCH_CARD_KEYBOARD_012', '⬅️ Назад'), callback_data='admin_coupons'
            )
        ]
    )
    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)


async def _load_batch(callback: types.CallbackQuery, db: AsyncSession, language: str = 'ru') -> CouponBatch | None:
    """Parse the batch id from callback data and fetch the batch, alerting on errors."""
    texts = get_texts(language)
    try:
        batch_id = int(callback.data.split('_')[-1])
    except ValueError:
        await callback.answer(
            texts.t('ADMIN_COUPONS_UI_LOAD_BATCH_013', '❌ Ошибка получения ID партии'), show_alert=True
        )
        return None

    batch = await get_coupon_batch_by_id(db, batch_id)
    if not batch:
        await callback.answer(texts.t('ADMIN_COUPONS_UI_LOAD_BATCH_014', '❌ Партия не найдена'), show_alert=True)
        return None
    return batch


async def _show_batch_card(
    callback: types.CallbackQuery, db: AsyncSession, batch: CouponBatch, language: str = 'ru'
) -> None:
    counts = await get_batch_status_counts(db, batch.id)
    await callback.message.edit_text(
        _format_batch_card(batch, counts, language=language),
        reply_markup=_batch_card_keyboard(batch, counts, language=language),
    )


async def _read_int(message: types.Message, lo: int, hi: int, error_text: str, language: str = 'ru') -> int | None:
    """Parse an int in [lo, hi] from the message; reply with ``error_text`` and return None otherwise."""
    try:
        value = int((message.text or '').strip())
    except ValueError:
        value = None
    if value is None or not lo <= value <= hi:
        await message.answer(error_text, reply_markup=_cancel_keyboard(language))
        return None
    return value


async def _render_coupons_menu(
    db: AsyncSession, language: str, page: int = 1
) -> tuple[str, types.InlineKeyboardMarkup]:
    texts = get_texts(language)
    limit = 10
    offset = (page - 1) * limit

    batches = await get_coupon_batches(db, offset=offset, limit=limit)
    total_count = await get_coupon_batches_count(db)
    total_pages = max(1, (total_count + limit - 1) // limit)

    text = texts.t(
        'ADMIN_COUPONS_UI_RENDER_COUPONS_MENU_015',
        '🎟 <b>Купоны</b>\n\nОптовая продажа подписок через партнёров: партия одноразовых ссылок на тариф, каждая выдаёт или продлевает подписку на N дней.\n\n📊 Партий: {value1}',
    ).format(value1=total_count)
    if total_pages > 1:
        text += texts.t('ADMIN_COUPONS_UI_RENDER_COUPONS_MENU_016', ' (стр. {value1}/{value2})').format(
            value1=page, value2=total_pages
        )

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=_format_batch_button(batch, language=language), callback_data=f'admin_coupon_manage_{batch.id}'
            )
        ]
        for batch in batches
    ]

    if total_pages > 1:
        pagination_row = get_admin_pagination_keyboard(
            page, total_pages, 'admin_coupon_list', 'admin_coupons', language
        ).inline_keyboard[0]
        keyboard.append(pagination_row)

    keyboard.extend(
        [
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_COUPONS_UI_RENDER_COUPONS_MENU_017', '➕ Создать партию'),
                    callback_data='admin_coupon_create',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_COUPONS_UI_BATCH_CARD_KEYBOARD_012', '⬅️ Назад'),
                    callback_data='admin_submenu_promo',
                )
            ],
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
    texts = get_texts(db_user.language)
    tariffs = await get_all_active_tariffs(db)
    if not tariffs:
        await callback.answer(
            texts.t(
                'ADMIN_COUPONS_UI_START_COUPON_BATCH_CREATION_019', '❌ Нет активных тарифов. Сначала создайте тариф.'
            ),
            show_alert=True,
        )
        return

    keyboard = [
        [types.InlineKeyboardButton(text=tariff.name, callback_data=f'coupon_batch_tariff_{tariff.id}')]
        for tariff in tariffs
    ]
    keyboard.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_COUPONS_UI_START_COUPON_BATCH_CREATION_020', '❌ Отмена'),
                callback_data='admin_coupons',
            )
        ]
    )

    await callback.message.edit_text(
        texts.t(
            'ADMIN_COUPONS_UI_START_COUPON_BATCH_CREATION_021',
            '🎟 <b>Создание партии купонов</b>\n\nВыберите тариф, который будут выдавать купоны:',
        ),
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
    )
    await callback.answer()


@admin_required
@error_handler
async def select_coupon_batch_tariff(callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    try:
        tariff_id = int(callback.data.split('_')[-1])
    except ValueError:
        await callback.answer(
            texts.t('ADMIN_COUPONS_UI_SELECT_COUPON_BATCH_TARIFF_022', '❌ Ошибка получения ID тарифа'), show_alert=True
        )
        return

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff or not tariff.is_active:
        await callback.answer(
            texts.t('ADMIN_COUPONS_UI_SELECT_COUPON_BATCH_TARIFF_023', '❌ Тариф не найден или неактивен'),
            show_alert=True,
        )
        return

    await state.update_data(coupon_tariff_id=tariff.id, coupon_tariff_name=tariff.name)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_COUPONS_UI_SELECT_COUPON_BATCH_TARIFF_024',
            '🎟 <b>Создание партии купонов</b>\n\nТариф: {value1}\n\n📅 Введите количество дней подписки на купон (1-{value2}):',
        ).format(value1=html.escape(tariff.name), value2=MAX_PERIOD_DAYS),
        reply_markup=_cancel_keyboard(db_user.language),
    )
    await state.set_state(AdminStates.creating_coupon_batch_days)
    await callback.answer()


@admin_required
@error_handler
async def process_coupon_batch_days(message: types.Message, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    days = await _read_int(
        message,
        1,
        MAX_PERIOD_DAYS,
        texts.t(
            'ADMIN_COUPONS_UI_PROCESS_COUPON_BATCH_DAYS_025', '❌ Введите целое число дней от 1 до {value1}'
        ).format(value1=MAX_PERIOD_DAYS),
        language=db_user.language,
    )
    if days is None:
        return

    await state.update_data(coupon_period_days=days)
    await message.answer(
        texts.t(
            'ADMIN_COUPONS_UI_PROCESS_COUPON_BATCH_DAYS_026', '🎫 Введите количество купонов в партии (1-{value1}):'
        ).format(value1=MAX_COUPONS_PER_BATCH),
        reply_markup=_cancel_keyboard(db_user.language),
    )
    await state.set_state(AdminStates.creating_coupon_batch_count)


@admin_required
@error_handler
async def process_coupon_batch_count(message: types.Message, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    count = await _read_int(
        message,
        1,
        MAX_COUPONS_PER_BATCH,
        texts.t(
            'ADMIN_COUPONS_UI_PROCESS_COUPON_BATCH_COUNT_027', '❌ Введите целое число купонов от 1 до {value1}'
        ).format(value1=MAX_COUPONS_PER_BATCH),
        language=db_user.language,
    )
    if count is None:
        return

    await state.update_data(coupon_count=count)
    await message.answer(
        texts.t(
            'ADMIN_COUPONS_UI_PROCESS_COUPON_BATCH_COUNT_028', '📌 Введите название партии (например, имя партнёра):'
        ),
        reply_markup=_cancel_keyboard(db_user.language),
    )
    await state.set_state(AdminStates.creating_coupon_batch_name)


@admin_required
@error_handler
async def process_coupon_batch_name(message: types.Message, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    name = (message.text or '').strip()
    if not name or len(name) > 255:
        await message.answer(
            texts.t('ADMIN_COUPONS_UI_PROCESS_COUPON_BATCH_NAME_029', '❌ Название должно быть от 1 до 255 символов'),
            reply_markup=_cancel_keyboard(db_user.language),
        )
        return

    await state.update_data(coupon_batch_name=name)
    await message.answer(
        texts.t(
            'ADMIN_COUPONS_UI_PROCESS_COUPON_BATCH_NAME_030',
            '💰 Введите оптовую цену за купон в рублях — только для учёта (0 — не указывать):',
        ),
        reply_markup=_cancel_keyboard(db_user.language),
    )
    await state.set_state(AdminStates.creating_coupon_batch_price)


@admin_required
@error_handler
async def process_coupon_batch_price(message: types.Message, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    try:
        rubles = float((message.text or '').strip().replace(',', '.').replace(' ', ''))
    except ValueError:
        await message.answer(
            texts.t(
                'ADMIN_COUPONS_UI_PROCESS_COUPON_BATCH_PRICE_031', '❌ Введите цену числом (например, 150 или 99.50)'
            ),
            reply_markup=_cancel_keyboard(db_user.language),
        )
        return
    # Inverted range check: also rejects NaN (all comparisons with NaN are False)
    if not 0 <= rubles <= MAX_WHOLESALE_PRICE_RUBLES:
        await message.answer(
            texts.t(
                'ADMIN_COUPONS_UI_PROCESS_COUPON_BATCH_PRICE_032', '❌ Цена должна быть от 0 до {value1} рублей'
            ).format(value1=MAX_WHOLESALE_PRICE_RUBLES),
            reply_markup=_cancel_keyboard(db_user.language),
        )
        return

    await state.update_data(coupon_price_kopeks=int(round(rubles * 100)))
    await message.answer(
        texts.t(
            'ADMIN_COUPONS_UI_PROCESS_COUPON_BATCH_PRICE_033',
            '⏰ Введите срок действия купонов в днях (0 — бессрочно):',
        ),
        reply_markup=_cancel_keyboard(db_user.language),
    )
    await state.set_state(AdminStates.creating_coupon_batch_expiry)


@admin_required
@error_handler
async def process_coupon_batch_expiry(message: types.Message, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    expiry_days = await _read_int(
        message,
        0,
        MAX_PERIOD_DAYS,
        texts.t(
            'ADMIN_COUPONS_UI_PROCESS_COUPON_BATCH_EXPIRY_034', '❌ Введите число дней от 0 до {value1} (0 — бессрочно)'
        ).format(value1=MAX_PERIOD_DAYS),
        language=db_user.language,
    )
    if expiry_days is None:
        return

    await state.update_data(coupon_expiry_days=expiry_days)
    await message.answer(
        texts.t(
            'ADMIN_COUPONS_UI_PROCESS_COUPON_BATCH_EXPIRY_035',
            '👤 Сколько купонов из партии может активировать ОДИН пользователь?\n\n<i>0 — без ограничения. Для раздач и конкурсов ставьте 1, чтобы один человек не забрал всю партию.</i>',
        ),
        reply_markup=_cancel_keyboard(db_user.language),
    )
    await state.set_state(AdminStates.creating_coupon_batch_per_user)


@admin_required
@error_handler
async def process_coupon_batch_per_user(message: types.Message, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    max_per_user = await _read_int(
        message,
        0,
        MAX_COUPONS_PER_BATCH,
        texts.t('ADMIN_COUPONS_UI_PROCESS_COUPON_BATCH_PER_USER_036', '❌ Введите число от 0 (без ограничения)'),
        language=db_user.language,
    )
    if max_per_user is None:
        return

    await state.update_data(coupon_max_per_user=max_per_user)

    data = await state.get_data()
    expiry_days = data.get('coupon_expiry_days', 0)
    price_kopeks = data.get('coupon_price_kopeks', 0)
    price_line = (
        texts.t('ADMIN_COUPONS_UI_PROCESS_COUPON_BATCH_PER_USER_037', '💰 Опт: {value1}/шт\n').format(
            value1=settings.format_price(price_kopeks)
        )
        if price_kopeks > 0
        else ''
    )
    expiry_line = (
        texts.t('ADMIN_COUPONS_UI_PROCESS_COUPON_BATCH_PER_USER_038', '⏰ Срок: {value1} дн.\n').format(
            value1=expiry_days
        )
        if expiry_days > 0
        else texts.t('ADMIN_COUPONS_UI_PROCESS_COUPON_BATCH_PER_USER_039', '⏰ Срок: бессрочно\n')
    )
    per_user_line = (
        texts.t('ADMIN_COUPONS_UI_PROCESS_COUPON_BATCH_PER_USER_040', '👤 На пользователя: {value1} шт.\n').format(
            value1=max_per_user
        )
        if max_per_user > 0
        else texts.t('ADMIN_COUPONS_UI_PROCESS_COUPON_BATCH_PER_USER_041', '👤 На пользователя: без ограничения\n')
    )

    await message.answer(
        texts.t(
            'ADMIN_COUPONS_UI_PROCESS_COUPON_BATCH_PER_USER_042',
            '🎟 <b>Подтвердите создание партии</b>\n\n📌 Название: {value1}\n📦 Тариф: {value2} — {value3} дн.\n🎫 Купонов: {value4}\n{value5}{value6}{value7}',
        ).format(
            value1=html.escape(data.get('coupon_batch_name', '')),
            value2=html.escape(data.get('coupon_tariff_name', '')),
            value3=data.get('coupon_period_days'),
            value4=data.get('coupon_count'),
            value5=price_line,
            value6=expiry_line,
            value7=per_user_line,
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_COUPONS_UI_PROCESS_COUPON_BATCH_PER_USER_043', '✅ Создать'),
                        callback_data='admin_coupon_create_confirm',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_COUPONS_UI_START_COUPON_BATCH_CREATION_020', '❌ Отмена'),
                        callback_data='admin_coupons',
                    )
                ],
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
    texts = get_texts(db_user.language)
    if db_user.id in _batch_creation_in_progress:
        await callback.answer(
            texts.t('ADMIN_COUPONS_UI_CONFIRM_COUPON_BATCH_CREATION_045', '⏳ Партия уже создаётся, подождите'),
            show_alert=True,
        )
        return
    _batch_creation_in_progress.add(db_user.id)
    try:
        # An old confirmation message keeps a live button: accept it only while the
        # wizard is actually waiting on this confirmation, otherwise a stale tap
        # would create a batch from half-entered state of a NEW wizard run.
        current_state = await state.get_state()
        if current_state != AdminStates.creating_coupon_batch_expiry.state:
            await callback.answer(
                texts.t(
                    'ADMIN_COUPONS_UI_CONFIRM_COUPON_BATCH_CREATION_046', '❌ Данные создания устарели, начните заново'
                ),
                show_alert=True,
            )
            return

        data = await state.get_data()
        tariff_id = data.get('coupon_tariff_id')
        period_days = data.get('coupon_period_days')
        count = data.get('coupon_count')
        name = data.get('coupon_batch_name')
        expiry_days = data.get('coupon_expiry_days')

        if not all([tariff_id, period_days, count, name]) or expiry_days is None:
            await callback.answer(
                texts.t(
                    'ADMIN_COUPONS_UI_CONFIRM_COUPON_BATCH_CREATION_046', '❌ Данные создания устарели, начните заново'
                ),
                show_alert=True,
            )
            await state.clear()
            return

        # Consume the wizard state BEFORE the (slow) batch insert — a double-tap on
        # the confirm button must not create the batch twice.
        await state.clear()

        tariff = await get_tariff_by_id(db, tariff_id)
        if not tariff or not tariff.is_active:
            await callback.answer(
                texts.t('ADMIN_COUPONS_UI_SELECT_COUPON_BATCH_TARIFF_023', '❌ Тариф не найден или неактивен'),
                show_alert=True,
            )
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
            max_per_user=data.get('coupon_max_per_user', 0),
        )

        logger.info(
            'Создана партия купонов',
            batch_id=batch.id,
            tariff_id=tariff.id,
            period_days=period_days,
            count=count,
            created_by=db_user.id,
        )

        await _show_batch_card(callback, db, batch, language=db_user.language)
        await _send_batch_links_file(callback, db, batch, language=db_user.language)
        await callback.answer(texts.t('ADMIN_COUPONS_UI_CONFIRM_COUPON_BATCH_CREATION_049', '✅ Партия создана'))
    finally:
        _batch_creation_in_progress.discard(db_user.id)


@admin_required
@error_handler
async def show_coupon_batch(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    batch = await _load_batch(callback, db, language=db_user.language)
    if batch is None:
        return
    await _show_batch_card(callback, db, batch, language=db_user.language)
    await callback.answer()


async def _send_batch_links_file(
    callback: types.CallbackQuery, db: AsyncSession, batch: CouponBatch, language: str = 'ru'
) -> bool:
    """Send a .txt with the deep links of all still-active coupons of the batch.

    Returns True if the document was sent; False if the batch has no active
    coupons (in which case the callback is already answered with an alert, so
    the caller must NOT answer it again — Telegram rejects a double answer).
    """
    texts = get_texts(language)
    tokens = await get_batch_coupon_tokens(db, batch.id, status=CouponStatus.ACTIVE.value)
    if not tokens:
        await callback.answer(
            texts.t('ADMIN_COUPONS_UI_SEND_BATCH_LINKS_FILE_050', '❌ В партии нет активных купонов'), show_alert=True
        )
        return False

    # The username is synced into settings at startup; get_me() is a fallback
    # to avoid an extra Bot API round trip on every export.
    bot_username = settings.get_bot_username() or (await callback.bot.get_me()).username
    content = '\n'.join(build_coupon_deeplink(bot_username, token) for token in tokens) + '\n'
    file = types.BufferedInputFile(content.encode('utf-8'), filename=f'coupons_batch_{batch.id}.txt')
    await callback.message.answer_document(
        document=file,
        caption=(
            texts.t(
                'ADMIN_COUPONS_UI_SEND_BATCH_LINKS_FILE_051',
                '🎟 Партия #{value1} «{value2}»: {value3} активных купонов, {value4} дн. каждый.',
            ).format(value1=batch.id, value2=html.escape(batch.name), value3=len(tokens), value4=batch.period_days)
        ),
    )
    return True


@admin_required
@error_handler
async def export_coupon_batch(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    batch = await _load_batch(callback, db, language=db_user.language)
    if batch is None:
        return
    if await _send_batch_links_file(callback, db, batch, language=db_user.language):
        await callback.answer()


@admin_required
@error_handler
async def ask_revoke_coupon_batch(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    batch = await _load_batch(callback, db, language=db_user.language)
    if batch is None:
        return

    counts = await get_batch_status_counts(db, batch.id)
    active = counts.get(CouponStatus.ACTIVE.value, 0)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_COUPONS_UI_ASK_REVOKE_COUPON_BATCH_052',
            '⛔ <b>Отзыв партии #{value1}</b>\n\n«{value2}»: будет отозвано {value3} непогашенных купонов. Их ссылки перестанут работать. Действие необратимо.\n\nПодтвердить?',
        ).format(value1=batch.id, value2=html.escape(batch.name), value3=active),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_COUPONS_UI_ASK_REVOKE_COUPON_BATCH_053', '⛔ Да, отозвать'),
                        callback_data=f'admin_coupon_revoke_confirm_{batch.id}',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_COUPONS_UI_START_COUPON_BATCH_CREATION_020', '❌ Отмена'),
                        callback_data=f'admin_coupon_manage_{batch.id}',
                    )
                ],
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def confirm_revoke_coupon_batch(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    batch = await _load_batch(callback, db, language=db_user.language)
    if batch is None:
        return

    revoked_count = await revoke_batch_coupons(db, batch)
    logger.info(
        'Партия купонов отозвана',
        batch_id=batch.id,
        revoked_count=revoked_count,
        admin_id=db_user.id,
    )

    await _show_batch_card(callback, db, batch, language=db_user.language)
    await callback.answer(
        texts.t('ADMIN_COUPONS_UI_CONFIRM_REVOKE_COUPON_BATCH_055', '⛔ Отозвано купонов: {value1}').format(
            value1=revoked_count
        )
    )


@admin_required
@error_handler
async def ask_delete_coupon_batch(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    batch = await _load_batch(callback, db, language=db_user.language)
    if batch is None:
        return

    counts = await get_batch_status_counts(db, batch.id)
    redeemed = counts.get(CouponStatus.REDEEMED.value, 0)
    total = sum(counts.values())

    warning = ''
    if redeemed:
        warning = texts.t(
            'ADMIN_COUPONS_UI_ASK_DELETE_COUPON_BATCH_056',
            '\n⚠️ Среди них {value1} уже погашенных: пропадёт история, кто чем воспользовался. Выданные подписки при этом НЕ отзываются.\n',
        ).format(value1=redeemed)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_COUPONS_UI_ASK_DELETE_COUPON_BATCH_057',
            '🗑 <b>Удаление партии #{value1}</b>\n\n«{value2}»: будет удалено {value3} купонов вместе с самой партией.\n{value4}\nЕсли нужно просто закрыть раздачу — используйте «Отозвать»: он гасит ссылки, но сохраняет историю.\n\nПодтвердить удаление?',
        ).format(value1=batch.id, value2=html.escape(batch.name), value3=total, value4=warning),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_COUPONS_UI_ASK_DELETE_COUPON_BATCH_058', '🗑 Да, удалить'),
                        callback_data=f'admin_coupon_delete_confirm_{batch.id}',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_COUPONS_UI_START_COUPON_BATCH_CREATION_020', '❌ Отмена'),
                        callback_data=f'admin_coupon_manage_{batch.id}',
                    )
                ],
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def confirm_delete_coupon_batch(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    batch = await _load_batch(callback, db, language=db_user.language)
    if batch is None:
        return

    batch_id = batch.id
    total = await delete_coupon_batch(db, batch)
    logger.info('Админ удалил партию купонов', admin_id=db_user.id, batch_id=batch_id, deleted_coupons=total)

    await callback.answer(
        texts.t('ADMIN_COUPONS_UI_CONFIRM_DELETE_COUPON_BATCH_060', '🗑 Партия #{value1} удалена').format(
            value1=batch_id
        ),
        show_alert=True,
    )
    await show_coupons_menu(callback, db_user, db, None)


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
    # confirm-обработчик регистрируется РАНЬШЕ общего префикса: 'admin_coupon_delete_'
    # является префиксом 'admin_coupon_delete_confirm_'.
    dp.callback_query.register(confirm_delete_coupon_batch, F.data.startswith('admin_coupon_delete_confirm_'))
    dp.callback_query.register(ask_delete_coupon_batch, F.data.startswith('admin_coupon_delete_'))

    dp.message.register(process_coupon_batch_days, AdminStates.creating_coupon_batch_days)
    dp.message.register(process_coupon_batch_count, AdminStates.creating_coupon_batch_count)
    dp.message.register(process_coupon_batch_name, AdminStates.creating_coupon_batch_name)
    dp.message.register(process_coupon_batch_price, AdminStates.creating_coupon_batch_price)
    dp.message.register(process_coupon_batch_expiry, AdminStates.creating_coupon_batch_expiry)
    dp.message.register(process_coupon_batch_per_user, AdminStates.creating_coupon_batch_per_user)
