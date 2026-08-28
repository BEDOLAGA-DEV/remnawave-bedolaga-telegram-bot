"""Admin routes for managing partners in cabinet."""

from datetime import UTC, datetime
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.referral_reward_level import (
    LEVELS_MODE_CHAIN,
    LEVELS_MODE_TIERS,
    MAX_SUPPORTED_LEVEL,
    delete_reward_level,
    get_all_reward_levels,
    get_reward_level,
    upsert_reward_level,
)
from app.database.models import (
    AdvertisingCampaign,
    PartnerApplication,
    PartnerStatus,
    ReferralEarning,
    ReferralRewardMode,
    ReferralRewardTrigger,
    Tariff,
    User,
)
from app.services.partner_application_service import partner_application_service
from app.services.partner_stats_service import PartnerStatsService
from app.services.system_settings_service import bot_configuration_service

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.partners import (
    AdminApproveRequest,
    AdminPartnerApplicationItem,
    AdminPartnerApplicationsResponse,
    AdminPartnerDetailResponse,
    AdminPartnerItem,
    AdminPartnerListResponse,
    AdminRejectRequest,
    AdminUpdateCommissionRequest,
    CampaignSummary,
)
from ..schemas.referral import (
    ReferralDepthUpdateRequest,
    ReferralLevelsModeUpdateRequest,
    ReferralRewardLevelResponse,
    ReferralRewardLevelsResponse,
    ReferralRewardLevelUpdateRequest,
    ReferralRewardTariffOption,
    ReferralSchemeUpdateRequest,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/partners', tags=['Cabinet Admin Partners'])


# ==================== Settings ====================


class PartnerSettingsResponse(BaseModel):
    withdrawal_enabled: bool
    withdrawal_min_amount_kopeks: int
    withdrawal_cooldown_days: int
    withdrawal_requisites_text: str
    partner_section_visible: bool
    referral_program_enabled: bool
    first_payment_commission_percent: int | None = None
    recurring_commission_tiers: str = ''


class PartnerSettingsUpdateRequest(BaseModel):
    withdrawal_enabled: bool | None = None
    withdrawal_min_amount_kopeks: int | None = Field(None, ge=0, le=100_000_000)
    withdrawal_cooldown_days: int | None = Field(None, ge=0, le=365)
    withdrawal_requisites_text: str | None = Field(None, max_length=2000)
    partner_section_visible: bool | None = None
    referral_program_enabled: bool | None = None
    first_payment_commission_percent: int | None = Field(None, ge=0, le=100)
    recurring_commission_tiers: str | None = Field(None, max_length=500)


def _build_partner_settings_response() -> PartnerSettingsResponse:
    return PartnerSettingsResponse(
        withdrawal_enabled=settings.REFERRAL_WITHDRAWAL_ENABLED,
        withdrawal_min_amount_kopeks=settings.REFERRAL_WITHDRAWAL_MIN_AMOUNT_KOPEKS,
        withdrawal_cooldown_days=settings.REFERRAL_WITHDRAWAL_COOLDOWN_DAYS,
        withdrawal_requisites_text=settings.REFERRAL_WITHDRAWAL_REQUISITES_TEXT,
        partner_section_visible=settings.REFERRAL_PARTNER_SECTION_VISIBLE,
        referral_program_enabled=settings.REFERRAL_PROGRAM_ENABLED,
        first_payment_commission_percent=settings.REFERRAL_FIRST_PAYMENT_COMMISSION_PERCENT,
        recurring_commission_tiers=settings.REFERRAL_RECURRING_COMMISSION_TIERS,
    )


@router.get('/settings', response_model=PartnerSettingsResponse)
async def get_partner_settings(
    admin: User = Depends(require_permission('partners:settings')),
):
    """Get partner system settings."""
    return _build_partner_settings_response()


@router.patch('/settings', response_model=PartnerSettingsResponse)
async def update_partner_settings(
    request: PartnerSettingsUpdateRequest,
    admin: User = Depends(require_permission('partners:settings')),
):
    """Update partner system settings."""
    import asyncio
    from pathlib import Path

    # Update in-memory settings
    if request.withdrawal_enabled is not None:
        settings.REFERRAL_WITHDRAWAL_ENABLED = request.withdrawal_enabled
    if request.withdrawal_min_amount_kopeks is not None:
        settings.REFERRAL_WITHDRAWAL_MIN_AMOUNT_KOPEKS = request.withdrawal_min_amount_kopeks
    if request.withdrawal_cooldown_days is not None:
        settings.REFERRAL_WITHDRAWAL_COOLDOWN_DAYS = request.withdrawal_cooldown_days
    if request.withdrawal_requisites_text is not None:
        settings.REFERRAL_WITHDRAWAL_REQUISITES_TEXT = request.withdrawal_requisites_text
    if request.partner_section_visible is not None:
        settings.REFERRAL_PARTNER_SECTION_VISIBLE = request.partner_section_visible
    if request.referral_program_enabled is not None:
        settings.REFERRAL_PROGRAM_ENABLED = request.referral_program_enabled
    if request.first_payment_commission_percent is not None:
        settings.REFERRAL_FIRST_PAYMENT_COMMISSION_PERCENT = request.first_payment_commission_percent
    if request.recurring_commission_tiers is not None:
        settings.REFERRAL_RECURRING_COMMISSION_TIERS = request.recurring_commission_tiers

    # Persist to .env file
    try:
        env_file = Path('.env')
        if await asyncio.to_thread(env_file.exists):
            lines = (await asyncio.to_thread(env_file.read_text)).splitlines()
            updates: dict[str, str] = {}

            if request.withdrawal_enabled is not None:
                updates['REFERRAL_WITHDRAWAL_ENABLED'] = str(request.withdrawal_enabled).lower()
            if request.withdrawal_min_amount_kopeks is not None:
                updates['REFERRAL_WITHDRAWAL_MIN_AMOUNT_KOPEKS'] = str(request.withdrawal_min_amount_kopeks)
            if request.withdrawal_cooldown_days is not None:
                updates['REFERRAL_WITHDRAWAL_COOLDOWN_DAYS'] = str(request.withdrawal_cooldown_days)
            if request.withdrawal_requisites_text is not None:
                # Sanitize: replace newlines to prevent .env injection
                sanitized = (
                    request.withdrawal_requisites_text.replace('\r\n', ' ').replace('\n', ' ').replace('\r', ' ')
                )
                updates['REFERRAL_WITHDRAWAL_REQUISITES_TEXT'] = sanitized
            if request.partner_section_visible is not None:
                updates['REFERRAL_PARTNER_SECTION_VISIBLE'] = str(request.partner_section_visible).lower()
            if request.referral_program_enabled is not None:
                updates['REFERRAL_PROGRAM_ENABLED'] = str(request.referral_program_enabled).lower()
            if request.first_payment_commission_percent is not None:
                updates['REFERRAL_FIRST_PAYMENT_COMMISSION_PERCENT'] = str(request.first_payment_commission_percent)
            if request.recurring_commission_tiers is not None:
                sanitized_tiers = (
                    request.recurring_commission_tiers.replace('\r\n', '').replace('\n', '').replace('\r', '')
                )
                updates['REFERRAL_RECURRING_COMMISSION_TIERS'] = sanitized_tiers

            new_lines = []
            updated_keys: set[str] = set()

            for line in lines:
                updated = False
                for key, value in updates.items():
                    if line.startswith(f'{key}='):
                        new_lines.append(f'{key}={value}')
                        updated_keys.add(key)
                        updated = True
                        break
                if not updated:
                    new_lines.append(line)

            for key, value in updates.items():
                if key not in updated_keys:
                    new_lines.append(f'{key}={value}')

            await asyncio.to_thread(env_file.write_text, '\n'.join(new_lines) + '\n')
            logger.info('Updated partner settings in .env file', admin_id=admin.id)
    except Exception as e:
        logger.warning('Failed to update .env file', error=e)

    return _build_partner_settings_response()


# ==================== Applications (static paths first) ====================


@router.get('/applications', response_model=AdminPartnerApplicationsResponse)
async def list_applications(
    application_status: Literal['pending', 'approved', 'rejected', 'none'] | None = Query(None, alias='status'),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    admin: User = Depends(require_permission('partners:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """List partner applications."""
    applications, total = await partner_application_service.get_all_applications(
        db, status=application_status, limit=limit, offset=offset
    )

    # Batch-fetch users to avoid N+1
    user_ids = list({app.user_id for app in applications})
    if user_ids:
        users_result = await db.execute(select(User).where(User.id.in_(user_ids)))
        users_map = {u.id: u for u in users_result.scalars().all()}
    else:
        users_map = {}

    items = []
    for app in applications:
        user = users_map.get(app.user_id)
        items.append(
            AdminPartnerApplicationItem(
                id=app.id,
                user_id=app.user_id,
                username=user.username if user else None,
                first_name=user.first_name if user else None,
                telegram_id=user.telegram_id if user else None,
                company_name=app.company_name,
                website_url=app.website_url,
                telegram_channel=app.telegram_channel,
                description=app.description,
                expected_monthly_referrals=app.expected_monthly_referrals,
                desired_commission_percent=app.desired_commission_percent,
                status=app.status,
                admin_comment=app.admin_comment,
                approved_commission_percent=app.approved_commission_percent,
                created_at=app.created_at,
                processed_at=app.processed_at,
            )
        )

    return AdminPartnerApplicationsResponse(items=items, total=total)


@router.post('/applications/{application_id}/approve')
async def approve_application(
    application_id: int,
    request: AdminApproveRequest,
    admin: User = Depends(require_permission('partners:approve')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Approve a partner application."""
    success, error = await partner_application_service.approve_application(
        db,
        application_id=application_id,
        admin_id=admin.id,
        commission_percent=request.commission_percent,
        comment=request.comment,
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error,
        )

    # Notify user about approval
    try:
        from app.bot_factory import create_bot
        from app.config import settings
        from app.services.notification_delivery_service import notification_delivery_service

        if settings.BOT_TOKEN:
            application = await db.get(PartnerApplication, application_id)
            user = await db.get(User, application.user_id) if application else None
            if user:
                comment_text = f'\n{request.comment}' if request.comment else ''
                tg_message = (
                    f'✅ Ваша заявка на партнёрство одобрена!\nКомиссия: {request.commission_percent}%{comment_text}'
                )
                bot = create_bot()
                try:
                    await notification_delivery_service.notify_partner_approved(
                        user=user,
                        commission_percent=request.commission_percent,
                        comment=request.comment,
                        bot=bot,
                        telegram_message=tg_message,
                    )
                finally:
                    await bot.session.close()
    except Exception as e:
        logger.error('Failed to send partner approval notification', error=e)

    return {'success': True}


@router.post('/applications/{application_id}/reject')
async def reject_application(
    application_id: int,
    request: AdminRejectRequest,
    admin: User = Depends(require_permission('partners:approve')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Reject a partner application."""
    success, error = await partner_application_service.reject_application(
        db,
        application_id=application_id,
        admin_id=admin.id,
        comment=request.comment,
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error,
        )

    # Notify user about rejection
    try:
        from app.bot_factory import create_bot
        from app.config import settings
        from app.services.notification_delivery_service import notification_delivery_service

        if settings.BOT_TOKEN:
            application = await db.get(PartnerApplication, application_id)
            user = await db.get(User, application.user_id) if application else None
            if user:
                comment_text = f'\nПричина: {request.comment}' if request.comment else ''
                tg_message = f'❌ Ваша заявка на партнёрство отклонена.{comment_text}'
                bot = create_bot()
                try:
                    await notification_delivery_service.notify_partner_rejected(
                        user=user,
                        comment=request.comment,
                        bot=bot,
                        telegram_message=tg_message,
                    )
                finally:
                    await bot.session.close()
    except Exception as e:
        logger.error('Failed to send partner rejection notification', error=e)

    return {'success': True}


# ==================== Stats (static paths) ====================


@router.get('/stats')
async def get_partner_stats(
    admin: User = Depends(require_permission('partners:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get overall partner statistics."""
    total_partners = await db.execute(
        select(func.count()).select_from(User).where(User.partner_status == PartnerStatus.APPROVED.value)
    )
    pending_apps = await db.execute(
        select(func.count())
        .select_from(PartnerApplication)
        .where(PartnerApplication.status == PartnerStatus.PENDING.value)
    )
    total_referrals = await db.execute(select(func.count()).select_from(User).where(User.referred_by_id.isnot(None)))
    total_earnings = await db.execute(select(func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0)))

    return {
        'total_partners': total_partners.scalar() or 0,
        'pending_applications': pending_apps.scalar() or 0,
        'total_referrals': total_referrals.scalar() or 0,
        'total_earnings_kopeks': total_earnings.scalar() or 0,
    }


# ==================== Partners list ====================


@router.get('', response_model=AdminPartnerListResponse)
async def list_partners(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    admin: User = Depends(require_permission('partners:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """List approved partners."""
    count_result = await db.execute(
        select(func.count()).select_from(User).where(User.partner_status == PartnerStatus.APPROVED.value)
    )
    total = count_result.scalar() or 0

    result = await db.execute(
        select(User)
        .where(User.partner_status == PartnerStatus.APPROVED.value)
        .order_by(desc(User.created_at))
        .offset(offset)
        .limit(limit)
    )
    partners = result.scalars().all()

    # Batch-fetch earnings and referral counts to avoid N+1
    partner_ids = [u.id for u in partners]
    earnings_map: dict[int, int] = {}
    referral_count_map: dict[int, int] = {}

    if partner_ids:
        earnings_result = await db.execute(
            select(ReferralEarning.user_id, func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0))
            .where(ReferralEarning.user_id.in_(partner_ids))
            .group_by(ReferralEarning.user_id)
        )
        earnings_map = {row[0]: int(row[1]) for row in earnings_result.all()}

        referral_result = await db.execute(
            select(User.referred_by_id, func.count())
            .where(User.referred_by_id.in_(partner_ids))
            .group_by(User.referred_by_id)
        )
        referral_count_map = {row[0]: row[1] for row in referral_result.all()}

    items = []
    for user in partners:
        items.append(
            AdminPartnerItem(
                user_id=user.id,
                username=user.username,
                first_name=user.first_name,
                telegram_id=user.telegram_id,
                commission_percent=user.referral_commission_percent,
                total_referrals=referral_count_map.get(user.id, 0),
                total_earnings_kopeks=earnings_map.get(user.id, 0),
                balance_kopeks=user.balance_kopeks,
                partner_status=user.partner_status,
                created_at=user.created_at,
            )
        )

    return AdminPartnerListResponse(items=items, total=total)


# ==================== Partner detail (parametric paths last) ====================


# ---------------------------------------------------------------------------
# Уровни реферальных наград
# ---------------------------------------------------------------------------
#
# Живут под тем же правом ``partners:settings``, что и остальные настройки
# партнёрской программы. Заводить отдельную секцию прав пришлось бы вместе с
# записью в PERMISSION_REGISTRY, иначе ``require_permission`` проверял бы строку,
# которой нет в реестре, и роль с таким правом невозможно было бы выдать через UI.


async def _levels_payload(db: AsyncSession) -> ReferralRewardLevelsResponse:
    """Уровни вместе с названиями тарифов.

    Названия резолвятся здесь, а не на фронте: идентификатор тарифа сам по себе
    не отвечает на вопрос «куда попадут дни», ради которого поле и существует.
    """
    levels = await get_all_reward_levels(db)

    tariff_ids = {lvl.referrer_tariff_id for lvl in levels if lvl.referrer_tariff_id}
    tariff_ids |= {lvl.referee_tariff_id for lvl in levels if lvl.referee_tariff_id}
    tariff_names: dict[int, str] = {}
    if tariff_ids:
        result = await db.execute(select(Tariff.id, Tariff.name).where(Tariff.id.in_(tariff_ids)))
        tariff_names = {row.id: row.name for row in result.all()}

    tariff_options = await db.execute(
        select(Tariff.id, Tariff.name).where(Tariff.is_active.is_(True)).order_by(Tariff.display_order, Tariff.id)
    )

    return ReferralRewardLevelsResponse(
        scheme='levels' if settings.is_referral_levels_scheme() else 'legacy',
        scheme_locked_by_env=bot_configuration_service.is_env_locked('REFERRAL_REWARD_SCHEME'),
        levels_mode=settings.get_referral_levels_mode(),
        levels_mode_locked_by_env=bot_configuration_service.is_env_locked('REFERRAL_LEVELS_MODE'),
        multi_tariff_enabled=settings.is_multi_tariff_enabled(),
        max_level_depth_locked_by_env=bot_configuration_service.is_env_locked('REFERRAL_MAX_LEVEL_DEPTH'),
        max_level_depth=settings.get_referral_max_level_depth(),
        max_supported_level=MAX_SUPPORTED_LEVEL,
        available_tariffs=[ReferralRewardTariffOption(id=row.id, name=row.name) for row in tariff_options.all()],
        levels=[
            ReferralRewardLevelResponse(
                level=lvl.level,
                is_active=bool(lvl.is_active),
                reward_mode=lvl.reward_mode,
                trigger=lvl.trigger,
                referrer_percent=lvl.referrer_percent,
                referrer_fixed_kopeks=lvl.referrer_fixed_kopeks,
                referrer_days=int(lvl.referrer_days or 0),
                referrer_tariff_id=lvl.referrer_tariff_id,
                referrer_tariff_name=tariff_names.get(lvl.referrer_tariff_id),
                referee_fixed_kopeks=lvl.referee_fixed_kopeks,
                referee_days=int(lvl.referee_days or 0),
                referee_tariff_id=lvl.referee_tariff_id,
                referee_tariff_name=tariff_names.get(lvl.referee_tariff_id),
                max_payments=int(lvl.max_payments or 0),
                required_referrals=int(getattr(lvl, 'required_referrals', 0) or 0),
                required_referrals_active_only=bool(getattr(lvl, 'required_referrals_active_only', True)),
            )
            for lvl in levels
        ],
    )


@router.get('/referral-levels', response_model=ReferralRewardLevelsResponse)
async def list_referral_levels(
    admin: User = Depends(require_permission('partners:settings')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Список уровней реферальных наград и текущая схема."""
    return await _levels_payload(db)


@router.put('/referral-levels/{level}', response_model=ReferralRewardLevelsResponse)
async def upsert_referral_level(
    level: int,
    request: ReferralRewardLevelUpdateRequest,
    admin: User = Depends(require_permission('partners:settings')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Создать или обновить правило уровня.

    Присылаются только изменённые поля: экран правит их по одному, и отправка
    всего объекта ради одной галочки затирала бы правку, сделанную параллельно из
    бота — оба интерфейса ходят в одну таблицу.
    """
    values = request.model_dump(exclude_unset=True)

    if 'reward_mode' in values and values['reward_mode'] not in {mode.value for mode in ReferralRewardMode}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Unknown reward_mode')
    if 'trigger' in values and values['trigger'] not in {trigger.value for trigger in ReferralRewardTrigger}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Unknown trigger')

    for field in ('referrer_tariff_id', 'referee_tariff_id'):
        tariff_id = values.get(field)
        if tariff_id:
            exists = await db.execute(select(Tariff.id).where(Tariff.id == tariff_id))
            if exists.scalar_one_or_none() is None:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f'Unknown tariff for {field}')

    try:
        await upsert_reward_level(db, level, **values)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error

    logger.info('Правило реферального уровня обновлено из кабинета', admin_id=admin.id, level=level)
    return await _levels_payload(db)


@router.delete('/referral-levels/{level}', response_model=ReferralRewardLevelsResponse)
async def remove_referral_level(
    level: int,
    admin: User = Depends(require_permission('partners:settings')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Удалить правило уровня."""
    if not await delete_reward_level(db, level):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Level not found')

    logger.info('Правило реферального уровня удалено из кабинета', admin_id=admin.id, level=level)
    return await _levels_payload(db)


@router.patch('/referral-depth', response_model=ReferralRewardLevelsResponse)
async def update_referral_depth(
    request: ReferralDepthUpdateRequest,
    admin: User = Depends(require_permission('partners:settings')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Сколько звеньев цепочки получают награду.

    Настройка живёт в общем списке конфигурации, и добраться до неё из редактора
    уровней было нельзя: правила глубже неё помечались как неплатящие, а способа
    поднять предел экран не давал.

    Верхняя граница — число заводимых уровней: глубже них обходить нечего, зато
    каждый лишний шаг это запрос пользователя на пустое звено при каждом пополнении.
    """
    depth = int(request.max_level_depth)
    if depth < 1 or depth > MAX_SUPPORTED_LEVEL:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'max_level_depth must be between 1 and {MAX_SUPPORTED_LEVEL}',
        )

    if bot_configuration_service.is_env_locked('REFERRAL_MAX_LEVEL_DEPTH'):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='REFERRAL_MAX_LEVEL_DEPTH is pinned in .env and cannot be changed from the cabinet',
        )

    await bot_configuration_service.set_value(db, 'REFERRAL_MAX_LEVEL_DEPTH', depth)
    logger.info('Глубина реферальной цепочки изменена из кабинета', admin_id=admin.id, depth=depth)
    return await _levels_payload(db)


@router.post('/referral-levels/import-legacy', response_model=ReferralRewardLevelsResponse)
async def import_legacy_referral_settings(
    admin: User = Depends(require_permission('partners:settings')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Перенести действующие настройки ``REFERRAL_*`` в уровень 1.

    Отката к ``REFERRAL_COMMISSION_PERCENT`` в расчёте нет, поэтому включение
    схемы на пустой таблице не платит ничего. Это действие делает переход явным:
    прежняя конфигурация становится видимым правилом, которое можно прочитать.

    Повод — «первое пополнение»: в классической схеме фиксированные бонусы
    разовые, а повод у уровня один на всё правило. Перенос с «каждым пополнением»
    превратил бы оба разовых бонуса в регулярную выплату. Правило создаётся
    ВЫКЛЮЧЕННЫМ — включает его админ, прочитав.
    """
    if await get_reward_level(db, 1) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Level 1 already exists')

    from app.services.referral_reward_service import legacy_percent_for_import

    percent, notes = legacy_percent_for_import()
    await upsert_reward_level(
        db,
        1,
        is_active=False,
        reward_mode=ReferralRewardMode.MONEY.value,
        trigger=ReferralRewardTrigger.FIRST_TOPUP.value,
        referrer_percent=percent,
        referrer_fixed_kopeks=settings.REFERRAL_INVITER_BONUS_KOPEKS or None,
        referee_fixed_kopeks=settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS or None,
        max_payments=settings.REFERRAL_MAX_COMMISSION_PAYMENTS,
    )
    logger.info('Легаси-настройки перенесены в уровень 1 из кабинета', admin_id=admin.id, notes=notes)
    payload = await _levels_payload(db)
    payload.import_notes = notes
    return payload


@router.patch('/referral-levels-mode', response_model=ReferralRewardLevelsResponse)
async def update_referral_levels_mode(
    request: ReferralLevelsModeUpdateRequest,
    admin: User = Depends(require_permission('partners:settings')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Что означает номер уровня: глубина цепочки или ранг партнёра.

    Переключение меняет и получателей награды, и число сработавших правил на
    одном пополнении, поэтому оно отдельное действие, а не поле в правке уровня.

    Значение проверяется по белому списку: неизвестная строка молча трактовалась
    бы как 'chain' при чтении, и кабинет показывал бы «сохранено» на настройке,
    которая не применилась.
    """
    mode = str(request.levels_mode or '').strip().lower()
    if mode not in (LEVELS_MODE_CHAIN, LEVELS_MODE_TIERS):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"levels_mode must be '{LEVELS_MODE_CHAIN}' or '{LEVELS_MODE_TIERS}'",
        )

    if bot_configuration_service.is_env_locked('REFERRAL_LEVELS_MODE'):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='REFERRAL_LEVELS_MODE is pinned in .env and cannot be changed from the cabinet',
        )

    await bot_configuration_service.set_value(db, 'REFERRAL_LEVELS_MODE', mode)
    logger.info('Режим уровней реферальной программы изменён из кабинета', admin_id=admin.id, levels_mode=mode)
    return await _levels_payload(db)


@router.patch('/referral-scheme', response_model=ReferralRewardLevelsResponse)
async def update_referral_scheme(
    request: ReferralSchemeUpdateRequest,
    admin: User = Depends(require_permission('partners:settings')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Переключить схему наград.

    Ключ, заданный в ``.env``, попадает в ``ENV_OVERRIDE_KEYS``: запись легла бы
    в БД и не применилась, а после перезапуска победило бы значение из файла.
    Молча принять такую правку хуже, чем отказать — админ считал бы схему
    переключённой.
    """
    scheme = (request.scheme or '').strip().lower()
    if scheme not in ('legacy', 'levels'):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='scheme must be legacy or levels')

    if bot_configuration_service.is_env_locked('REFERRAL_REWARD_SCHEME'):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='REFERRAL_REWARD_SCHEME is pinned in .env and cannot be changed from the cabinet',
        )

    await bot_configuration_service.set_value(db, 'REFERRAL_REWARD_SCHEME', scheme)
    logger.info('Схема реферальных наград переключена из кабинета', admin_id=admin.id, scheme=scheme)
    return await _levels_payload(db)


# ВНИМАНИЕ: всё, что ниже, объявлено ПОСЛЕ параметризованных путей вида
# '/{user_id}'. Литеральные сегменты обязаны идти раньше — FastAPI выбирает первый
# совпавший маршрут, и '/{user_id}' перехватит любой литерал, отдав 422 при
# разборе его как int. Новые литеральные пути добавляйте ВЫШЕ этой строки.


@router.get('/{user_id}', response_model=AdminPartnerDetailResponse)
async def get_partner_detail(
    user_id: int,
    admin: User = Depends(require_permission('partners:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get detailed partner info."""
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Пользователь не найден',
        )

    stats = await PartnerStatsService.get_referrer_detailed_stats(db, user_id)

    # Get assigned campaigns with per-campaign stats
    campaigns_result = await db.execute(
        select(AdvertisingCampaign).where(AdvertisingCampaign.partner_user_id == user_id)
    )
    campaigns = campaigns_result.scalars().all()

    campaign_ids = [c.id for c in campaigns]
    per_campaign_stats = await PartnerStatsService.get_per_campaign_stats(db, user_id, campaign_ids)

    campaign_list = [
        CampaignSummary(
            id=c.id,
            name=c.name,
            start_parameter=c.start_parameter,
            is_active=c.is_active,
            registrations_count=per_campaign_stats.get(c.id, {}).get('registrations_count', 0),
            referrals_count=per_campaign_stats.get(c.id, {}).get('referrals_count', 0),
            earnings_kopeks=per_campaign_stats.get(c.id, {}).get('earnings_kopeks', 0),
        )
        for c in campaigns
    ]

    summary = stats['summary']
    earnings = stats['earnings']

    return AdminPartnerDetailResponse(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        telegram_id=user.telegram_id,
        commission_percent=user.referral_commission_percent,
        partner_status=user.partner_status,
        balance_kopeks=user.balance_kopeks,
        total_referrals=summary['total_referrals'],
        paid_referrals=summary['paid_referrals'],
        active_referrals=summary['active_referrals'],
        earnings_all_time=earnings['all_time_kopeks'],
        earnings_today=earnings['today_kopeks'],
        earnings_week=earnings['week_kopeks'],
        earnings_month=earnings['month_kopeks'],
        earnings_all_time_days=earnings.get('all_time_days', 0),
        earnings_month_days=earnings.get('month_days', 0),
        earnings_by_level=stats.get('earnings_by_level') or [],
        conversion_to_paid=summary['conversion_to_paid_percent'],
        campaigns=campaign_list,
        created_at=user.created_at,
    )


@router.patch('/{user_id}/commission')
async def update_commission(
    user_id: int,
    request: AdminUpdateCommissionRequest,
    admin: User = Depends(require_permission('partners:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Update partner commission percent."""
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Пользователь не найден',
        )

    if user.partner_status != PartnerStatus.APPROVED.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Пользователь не является партнёром',
        )

    old_commission = user.referral_commission_percent
    user.referral_commission_percent = request.commission_percent
    await db.commit()

    logger.info(
        'Комиссия партнёра обновлена',
        user_id=user_id,
        old_commission=old_commission,
        new_commission=request.commission_percent,
        admin_id=admin.id,
    )

    return {'success': True, 'commission_percent': request.commission_percent}


@router.post('/{user_id}/revoke')
async def revoke_partner(
    user_id: int,
    admin: User = Depends(require_permission('partners:revoke')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Revoke partner status."""
    success, error = await partner_application_service.revoke_partner(db, user_id=user_id, admin_id=admin.id)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error,
        )

    return {'success': True}


@router.post('/{user_id}/campaigns/{campaign_id}/assign')
async def assign_campaign(
    user_id: int,
    campaign_id: int,
    admin: User = Depends(require_permission('partners:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Assign a campaign to a partner."""
    campaign = await db.get(AdvertisingCampaign, campaign_id)
    if not campaign:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Кампания не найдена',
        )

    user = await db.get(User, user_id)
    if not user or user.partner_status != PartnerStatus.APPROVED.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Пользователь не является партнёром',
        )

    # Atomic check-and-set to prevent race conditions
    result = await db.execute(
        update(AdvertisingCampaign)
        .where(
            AdvertisingCampaign.id == campaign_id,
            or_(
                AdvertisingCampaign.partner_user_id.is_(None),
                AdvertisingCampaign.partner_user_id == user_id,
            ),
        )
        .values(partner_user_id=user_id, updated_at=datetime.now(UTC))
    )
    if result.rowcount == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Кампания уже привязана к другому партнёру',
        )
    await db.commit()

    logger.info(
        'Кампания привязана к партнёру',
        campaign_id=campaign_id,
        partner_user_id=user_id,
        admin_id=admin.id,
    )
    return {'success': True}


@router.post('/{user_id}/campaigns/{campaign_id}/unassign')
async def unassign_campaign(
    user_id: int,
    campaign_id: int,
    admin: User = Depends(require_permission('partners:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Unassign a campaign from a partner."""
    # Atomic check-and-unset to prevent race conditions
    result = await db.execute(
        update(AdvertisingCampaign)
        .where(
            AdvertisingCampaign.id == campaign_id,
            AdvertisingCampaign.partner_user_id == user_id,
        )
        .values(partner_user_id=None, updated_at=datetime.now(UTC))
    )
    if result.rowcount == 0:
        campaign = await db.get(AdvertisingCampaign, campaign_id)
        if not campaign:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Кампания не найдена',
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Кампания не привязана к этому партнёру',
        )
    await db.commit()

    logger.info(
        'Кампания откреплена от партнёра',
        campaign_id=campaign_id,
        partner_user_id=user_id,
        admin_id=admin.id,
    )
    return {'success': True}
