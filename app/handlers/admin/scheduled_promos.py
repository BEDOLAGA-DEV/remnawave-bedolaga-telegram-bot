from datetime import UTC, datetime

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.scheduled_promo import ScheduledPromoCRUD
from app.database.models import User
from app.localization.texts import get_texts
from app.states import ScheduledPromoStates
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


def _promo_status(promo) -> str:
    now = datetime.now(UTC)
    if not promo.is_active:
        return '🔴 Неактивна'
    if promo.start_at > now:
        return '🟡 Запланирована'
    if promo.end_at < now:
        return '⚫ Истекла'
    return '🟢 Активна'


def _promos_list_keyboard(promos, language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)
    rows = []
    for p in promos:
        status = _promo_status(p)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f'{status} {p.name} (-{p.discount_percent}%)',
                    callback_data=f'spromo_view:{p.id}',
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text='➕ Создать акцию', callback_data='spromo_create')]
    )
    rows.append(
        [InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@admin_required
@error_handler
async def admin_scheduled_promos(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession
):
    promos = await ScheduledPromoCRUD.get_all_promos(db)
    text = '🎁 <b>Акции (скидки по расписанию)</b>\n\nУправляйте временными скидками на тарифы.'
    if not promos:
        text += '\n\nПока акций нет.'
    await callback.message.edit_text(
        text,
        parse_mode='HTML',
        reply_markup=_promos_list_keyboard(promos, db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def spromo_view(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession
):
    promo_id = int(callback.data.split(':')[1])
    promos = await ScheduledPromoCRUD.get_all_promos(db)
    promo = next((p for p in promos if p.id == promo_id), None)
    if not promo:
        await callback.answer('Акция не найдена', show_alert=True)
        return

    texts = get_texts(db_user.language)
    status = _promo_status(promo)
    tariffs_text = ', '.join(str(t) for t in (promo.tariff_ids or [])) or 'Все тарифы'
    start = promo.start_at.strftime('%d.%m.%Y %H:%M') if promo.start_at else '?'
    end = promo.end_at.strftime('%d.%m.%Y %H:%M') if promo.end_at else '?'

    text = (
        f'🎁 <b>{promo.name}</b>\n\n'
        f'<b>Статус:</b> {status}\n'
        f'<b>Скидка:</b> {promo.discount_percent}%\n'
        f'<b>Тарифы:</b> {tariffs_text}\n'
        f'<b>Начало:</b> {start}\n'
        f'<b>Конец:</b> {end}\n'
    )
    if promo.promo_text:
        text += f'\n<b>Баннер:</b> {promo.promo_text}'

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text='🗑️ Удалить',
                    callback_data=f'spromo_delete:{promo.id}',
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_scheduled_promos')],
        ]
    )
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=kb)
    await callback.answer()


@admin_required
@error_handler
async def spromo_create(
    callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession
):
    await state.set_state(ScheduledPromoStates.waiting_for_name)
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        '🎁 <b>Создание акции</b>\n\nВведите название акции:',
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=texts.BACK, callback_data='admin_scheduled_promos')]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def spromo_name_received(
    message: types.Message, db_user: User, state: FSMContext, db: AsyncSession
):
    name = message.text.strip()[:255]
    await state.update_data(promo_name=name)
    await state.set_state(ScheduledPromoStates.waiting_for_discount)
    await message.answer('Введите процент скидки (5-90):')


@admin_required
@error_handler
async def spromo_discount_received(
    message: types.Message, db_user: User, state: FSMContext, db: AsyncSession
):
    try:
        discount = int(message.text.strip())
    except ValueError:
        await message.answer('Введите число от 5 до 90.')
        return

    if not (5 <= discount <= 90):
        await message.answer('Процент должен быть от 5 до 90.')
        return

    await state.update_data(promo_discount=discount)
    await state.set_state(ScheduledPromoStates.waiting_for_start_date)
    await message.answer(
        'Введите дату начала акции в формате <b>ДД.ММ.ГГГГ ЧЧ:ММ</b>\n\n'
        'Например: <code>15.04.2026 10:00</code>',
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def spromo_start_date_received(
    message: types.Message, db_user: User, state: FSMContext, db: AsyncSession
):
    try:
        start_at = datetime.strptime(message.text.strip(), '%d.%m.%Y %H:%M').replace(tzinfo=UTC)
    except ValueError:
        await message.answer('Неверный формат. Используйте: ДД.ММ.ГГГГ ЧЧ:ММ')
        return

    await state.update_data(promo_start_at=start_at.isoformat())
    await state.set_state(ScheduledPromoStates.waiting_for_end_date)
    await message.answer(
        'Введите дату окончания акции в формате <b>ДД.ММ.ГГГГ ЧЧ:ММ</b>\n\n'
        'Например: <code>30.04.2026 23:59</code>',
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def spromo_end_date_received(
    message: types.Message, db_user: User, state: FSMContext, db: AsyncSession
):
    try:
        end_at = datetime.strptime(message.text.strip(), '%d.%m.%Y %H:%M').replace(tzinfo=UTC)
    except ValueError:
        await message.answer('Неверный формат. Используйте: ДД.ММ.ГГГГ ЧЧ:ММ')
        return

    data = await state.get_data()
    start_at = datetime.fromisoformat(data['promo_start_at'])

    if end_at <= start_at:
        await message.answer('Дата окончания должна быть позже даты начала.')
        return

    await state.clear()

    promo = await ScheduledPromoCRUD.create_promo(
        db,
        name=data['promo_name'],
        discount_percent=data['promo_discount'],
        start_at=start_at,
        end_at=end_at,
        created_by=db_user.id,
    )
    await db.commit()

    await message.answer(
        f'🎁 Акция <b>{promo.name}</b> создана!\n'
        f'Скидка: {promo.discount_percent}%\n'
        f'Период: {start_at.strftime("%d.%m.%Y %H:%M")} — {end_at.strftime("%d.%m.%Y %H:%M")}',
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def spromo_delete(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession
):
    promo_id = int(callback.data.split(':')[1])
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        f'🗑️ Удалить акцию #{promo_id}?',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text='✅ Да, удалить',
                        callback_data=f'spromo_delete_confirm:{promo_id}',
                    ),
                    InlineKeyboardButton(text=texts.BACK, callback_data=f'spromo_view:{promo_id}'),
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def spromo_delete_confirm(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession
):
    promo_id = int(callback.data.split(':')[1])
    deleted = await ScheduledPromoCRUD.delete_promo(db, promo_id)
    await db.commit()

    if deleted:
        await callback.answer('Акция удалена!', show_alert=True)
    else:
        await callback.answer('Акция не найдена.', show_alert=True)

    promos = await ScheduledPromoCRUD.get_all_promos(db)
    await callback.message.edit_text(
        '🎁 <b>Акции (скидки по расписанию)</b>\n\nУправляйте временными скидками на тарифы.',
        parse_mode='HTML',
        reply_markup=_promos_list_keyboard(promos, db_user.language),
    )


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(admin_scheduled_promos, F.data == 'admin_scheduled_promos')
    dp.callback_query.register(spromo_view, F.data.startswith('spromo_view:'))
    dp.callback_query.register(spromo_create, F.data == 'spromo_create')
    dp.callback_query.register(
        spromo_delete,
        F.data.startswith('spromo_delete:') & ~F.data.startswith('spromo_delete_confirm:'),
    )
    dp.callback_query.register(
        spromo_delete_confirm, F.data.startswith('spromo_delete_confirm:')
    )
    dp.message.register(spromo_name_received, ScheduledPromoStates.waiting_for_name)
    dp.message.register(spromo_discount_received, ScheduledPromoStates.waiting_for_discount)
    dp.message.register(spromo_start_date_received, ScheduledPromoStates.waiting_for_start_date)
    dp.message.register(spromo_end_date_received, ScheduledPromoStates.waiting_for_end_date)
