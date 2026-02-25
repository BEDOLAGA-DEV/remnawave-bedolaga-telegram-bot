from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, Security, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.main_menu_button import (
    count_main_menu_buttons,
    create_main_menu_button,
    delete_main_menu_button,
    get_main_menu_button_by_id,
    get_main_menu_buttons,
    update_main_menu_button,
)
from app.database.models import MainMenuButton
from app.localization.texts import get_texts
from app.services.main_menu_button_service import MainMenuButtonService

from ..dependencies import get_db_session, require_api_token
from ..schemas.main_menu_buttons import (
    MainMenuButtonCreateRequest,
    MainMenuButtonListResponse,
    MainMenuButtonResponse,
    MainMenuButtonUpdateRequest,
)


router = APIRouter()


def _serialize(button: MainMenuButton) -> MainMenuButtonResponse:
    return MainMenuButtonResponse(
        id=button.id,
        text=button.text,
        action_type=button.action_type_enum,
        action_value=button.action_value,
        visibility=button.visibility_enum,
        is_active=button.is_active,
        display_order=button.display_order,
        created_at=button.created_at,
        updated_at=button.updated_at,
    )


@router.get('', response_model=MainMenuButtonListResponse)
async def list_main_menu_buttons(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> MainMenuButtonListResponse:
    total = await count_main_menu_buttons(db)
    buttons = await get_main_menu_buttons(db, limit=limit, offset=offset)

    return MainMenuButtonListResponse(
        items=[_serialize(button) for button in buttons],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post('', response_model=MainMenuButtonResponse, status_code=status.HTTP_201_CREATED)
async def create_main_menu_button_endpoint(
    payload: MainMenuButtonCreateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> MainMenuButtonResponse:
    button = await create_main_menu_button(
        db,
        text=payload.text,
        action_type=payload.action_type,
        action_value=payload.action_value,
        visibility=payload.visibility,
        is_active=payload.is_active,
        display_order=payload.display_order,
    )

    MainMenuButtonService.invalidate_cache()
    return _serialize(button)


@router.patch('/{button_id}', response_model=MainMenuButtonResponse)
async def update_main_menu_button_endpoint(
    button_id: int,
    payload: MainMenuButtonUpdateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> MainMenuButtonResponse:
    texts = get_texts('ru')
    button = await get_main_menu_button_by_id(db, button_id)
    if not button:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_MAIN_MENU_BUTTONS_NOT_FOUND', 'Main menu button not found'),
        )

    update_payload = payload.dict(exclude_unset=True)
    button = await update_main_menu_button(db, button, **update_payload)

    MainMenuButtonService.invalidate_cache()
    return _serialize(button)


@router.delete('/{button_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_main_menu_button_endpoint(
    button_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    texts = get_texts('ru')
    button = await get_main_menu_button_by_id(db, button_id)
    if not button:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_MAIN_MENU_BUTTONS_NOT_FOUND', 'Main menu button not found'),
        )

    await delete_main_menu_button(db, button)
    MainMenuButtonService.invalidate_cache()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
