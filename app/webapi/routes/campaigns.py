from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Security, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.campaign import (
    create_campaign,
    delete_campaign,
    get_campaign_by_id,
    get_campaigns_count,
    get_campaigns_list,
    update_campaign,
)
from app.localization.texts import get_texts

from ..dependencies import get_db_session, require_api_token
from ..schemas.campaigns import (
    CampaignCreateRequest,
    CampaignListResponse,
    CampaignResponse,
    CampaignUpdateRequest,
)


router = APIRouter()


def _serialize_campaign(campaign) -> CampaignResponse:
    registrations_attr = None
    if isinstance(getattr(campaign, '__dict__', None), dict):
        registrations_attr = campaign.__dict__.get('registrations')
    registrations = registrations_attr or []
    squads = list(campaign.subscription_squads or [])

    # Получаем название тарифа если есть
    tariff_name = None
    if campaign.tariff_id and hasattr(campaign, 'tariff') and campaign.tariff:
        tariff_name = campaign.tariff.name

    return CampaignResponse(
        id=campaign.id,
        name=campaign.name,
        start_parameter=campaign.start_parameter,
        bonus_type=campaign.bonus_type,
        balance_bonus_kopeks=campaign.balance_bonus_kopeks or 0,
        balance_bonus_rubles=round((campaign.balance_bonus_kopeks or 0) / 100, 2),
        subscription_duration_days=campaign.subscription_duration_days,
        subscription_traffic_gb=campaign.subscription_traffic_gb,
        subscription_device_limit=campaign.subscription_device_limit,
        subscription_squads=squads,
        tariff_id=campaign.tariff_id,
        tariff_duration_days=campaign.tariff_duration_days,
        tariff_name=tariff_name,
        is_active=campaign.is_active,
        created_by=campaign.created_by,
        created_at=campaign.created_at,
        updated_at=campaign.updated_at,
        registrations_count=len(registrations),
    )


@router.post(
    '',
    response_model=CampaignResponse,
    status_code=status.HTTP_201_CREATED,
    summary='Создать рекламную кампанию',
)
async def create_campaign_endpoint(
    payload: CampaignCreateRequest,
    token: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> CampaignResponse:
    texts = get_texts('ru')
    created_by = getattr(token, 'id', None)

    try:
        campaign = await create_campaign(
            db,
            name=payload.name,
            start_parameter=payload.start_parameter,
            bonus_type=payload.bonus_type,
            created_by=created_by,
            balance_bonus_kopeks=payload.balance_bonus_kopeks,
            subscription_duration_days=payload.subscription_duration_days,
            subscription_traffic_gb=payload.subscription_traffic_gb,
            subscription_device_limit=payload.subscription_device_limit,
            subscription_squads=payload.subscription_squads,
            tariff_id=payload.tariff_id,
            tariff_duration_days=payload.tariff_duration_days,
            is_active=payload.is_active,
        )
    except IntegrityError as exc:  # duplicate start_parameter
        await db.rollback()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t(
                'WEBAPI_CAMPAIGNS_DUPLICATE_START_PARAMETER',
                'Campaign with this start_parameter already exists',
            ),
        ) from exc

    return _serialize_campaign(campaign)


@router.get(
    '',
    response_model=CampaignListResponse,
    summary='Список рекламных кампаний',
)
async def list_campaigns(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    include_inactive: bool = Query(True, description='Включать неактивные кампании'),
) -> CampaignListResponse:
    total = await get_campaigns_count(db, is_active=None if include_inactive else True)
    campaigns = await get_campaigns_list(
        db,
        offset=offset,
        limit=limit,
        include_inactive=include_inactive,
    )

    return CampaignListResponse(
        items=[_serialize_campaign(campaign) for campaign in campaigns],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.delete(
    '/{campaign_id}',
    status_code=status.HTTP_204_NO_CONTENT,
    summary='Удалить рекламную кампанию',
)
async def delete_campaign_endpoint(
    campaign_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
):
    texts = get_texts('ru')
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_CAMPAIGNS_NOT_FOUND', 'Campaign not found'),
        )

    await delete_campaign(db, campaign)


@router.patch(
    '/{campaign_id}',
    response_model=CampaignResponse,
    summary='Обновить рекламную кампанию',
)
async def update_campaign_endpoint(
    campaign_id: int,
    payload: CampaignUpdateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> CampaignResponse:
    texts = get_texts('ru')
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_CAMPAIGNS_NOT_FOUND', 'Campaign not found'),
        )

    update_fields = payload.dict(exclude_unset=True)
    if not update_fields:
        return _serialize_campaign(campaign)

    try:
        campaign = await update_campaign(db, campaign, **update_fields)
    except IntegrityError as exc:
        await db.rollback()
        if 'start_parameter' in str(exc.orig):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                texts.t(
                    'WEBAPI_CAMPAIGNS_DUPLICATE_START_PARAMETER',
                    'Campaign with this start_parameter already exists',
                ),
            ) from exc
        raise

    return _serialize_campaign(campaign)
