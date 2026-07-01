from datetime import UTC, datetime

from aiogram import types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.keyboards.inline import get_manage_protocols_keyboard
from app.localization.texts import get_texts

from . import common
from .common import logger


def validate_protocol_selection(selected: list[str]) -> bool:
    """At least one non-empty protocol must remain selected."""
    return len([s for s in (selected or []) if s]) >= 1


async def _build_protocol_pool(db: AsyncSession, promo_group_id, current: list[str] | None) -> list[dict]:
    """Visible squads (by promo group) plus any currently-active squad that fell out of view."""
    from app.database.crud.server_squad import (
        get_available_server_squads,
        get_server_squads_by_uuids,
    )

    squads = await get_available_server_squads(db, promo_group_id=promo_group_id)
    pool = [{'uuid': s.squad_uuid, 'name': s.display_name} for s in squads if s.squad_uuid]

    known = {p['uuid'] for p in pool}
    extra_uuids = [u for u in (current or []) if u and u not in known]
    if extra_uuids:
        for s in await get_server_squads_by_uuids(db, extra_uuids):
            pool.append({'uuid': s.squad_uuid, 'name': s.display_name})

    return pool


async def handle_manage_protocols(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    from app.database.crud.server_squad import (
        get_default_protocol_squad_uuid,
        resolve_effective_squads,
    )

    texts = get_texts(db_user.language)
    subscription, sub_id = await common.resolve_subscription_from_context(callback, db_user, db, state)
    if subscription is None:
        return

    default_uuid = await get_default_protocol_squad_uuid(db)
    current = resolve_effective_squads(subscription.connected_squads, default_uuid)
    pool = await _build_protocol_pool(db, db_user.promo_group_id, current)

    await state.update_data(protocols=list(current))

    text = texts.t(
        'PROTOCOLS_SCREEN_TITLE',
        '🧩 <b>Протоколы</b>\n\nВыберите активные протоколы (минимум один):',
    )
    await callback.message.edit_text(
        text,
        reply_markup=get_manage_protocols_keyboard(pool, list(current), db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


async def handle_toggle_protocol(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    texts = get_texts(db_user.language)
    uuid = callback.data.split('nz!_protocol_toggle_', 1)[1]

    data = await state.get_data()
    selected = list(data.get('protocols', []))

    pool = await _build_protocol_pool(db, db_user.promo_group_id, selected)
    allowed = {p['uuid'] for p in pool}
    if uuid not in allowed:
        await callback.answer(
            texts.t('PROTOCOL_NOT_AVAILABLE', '❌ Протокол недоступен'),
            show_alert=True,
        )
        return

    if uuid in selected:
        if len([s for s in selected if s]) <= 1:
            await callback.answer(
                texts.t('PROTOCOLS_MIN_ONE_ALERT', '❌ Нужен хотя бы один протокол'),
                show_alert=True,
            )
            return
        selected.remove(uuid)
    else:
        selected.append(uuid)

    await state.update_data(protocols=selected)

    try:
        await callback.message.edit_reply_markup(
            reply_markup=get_manage_protocols_keyboard(pool, selected, db_user.language)
        )
    except Exception as e:
        logger.error('Ошибка обновления клавиатуры протоколов', error=e)

    await callback.answer()


async def apply_protocols_changes(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    texts = get_texts(db_user.language)
    subscription, sub_id = await common.resolve_subscription_from_context(callback, db_user, db, state)
    if subscription is None:
        return

    data = await state.get_data()
    raw_selected = [u for u in data.get('protocols', []) if u]

    pool = await _build_protocol_pool(db, db_user.promo_group_id, raw_selected)
    allowed = {p['uuid'] for p in pool}
    selected = list(dict.fromkeys(u for u in raw_selected if u in allowed))

    if not validate_protocol_selection(selected):
        await callback.answer(
            texts.t('PROTOCOLS_MIN_ONE_ALERT', '❌ Нужен хотя бы один протокол'),
            show_alert=True,
        )
        return

    subscription.connected_squads = selected
    subscription.updated_at = datetime.now(UTC)
    await db.commit()

    from app.services.subscription_service import SubscriptionService

    service = SubscriptionService()
    try:
        await service.update_remnawave_user(db, subscription, sync_squads=True)
    except Exception as rw_err:
        logger.error('Ошибка синхронизации протоколов с RemnaWave', error=rw_err)
        from app.services.remnawave_retry_queue import remnawave_retry_queue

        remnawave_retry_queue.enqueue(
            subscription_id=subscription.id,
            user_id=subscription.user_id,
            action='update',
        )

    await db.refresh(subscription)

    await state.update_data(protocols=list(selected))
    try:
        await callback.message.edit_reply_markup(
            reply_markup=get_manage_protocols_keyboard(pool, selected, db_user.language)
        )
    except Exception as e:
        logger.error('Ошибка обновления клавиатуры протоколов', error=e)

    await callback.answer(
        texts.t('PROTOCOLS_UPDATED', '✅ Протоколы обновлены'),
        show_alert=True,
    )
