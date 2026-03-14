"""API эндпоинты для конструктора меню."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Response, Security, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.menu_layout_service import (
    MenuContext,
    MenuLayoutService,
)


logger = structlog.get_logger(__name__)

from ..dependencies import get_db_session, require_api_token
from ..schemas.menu_layout import (
    AddCustomButtonRequest,
    AddRowRequest,
    AvailableCallback,
    AvailableCallbacksResponse,
    BuiltinButtonInfo,
    BuiltinButtonsListResponse,
    ButtonClickStats,
    ButtonClickStatsResponse,
    ButtonConditions,
    ButtonTypeStats,
    ButtonTypeStatsResponse,
    ButtonUpdateRequest,
    DynamicPlaceholder,
    DynamicPlaceholdersResponse,
    HourlyStats,
    HourlyStatsResponse,
    MenuButtonConfig,
    MenuClickStatsResponse,
    MenuLayoutExportResponse,
    MenuLayoutHistoryEntry,
    MenuLayoutHistoryResponse,
    MenuLayoutImportRequest,
    MenuLayoutImportResponse,
    MenuLayoutResponse,
    MenuLayoutUpdateRequest,
    MenuLayoutValidateRequest,
    MenuLayoutValidateResponse,
    MenuPreviewButton,
    MenuPreviewRequest,
    MenuPreviewResponse,
    MenuPreviewRow,
    MenuRowConfig,
    MoveButtonResponse,
    MoveButtonToRowRequest,
    PeriodComparisonResponse,
    ReorderButtonsInRowRequest,
    ReorderButtonsResponse,
    RowsReorderRequest,
    SwapButtonsRequest,
    SwapButtonsResponse,
    TopUsersResponse,
    TopUserStats,
    UserClickSequence,
    UserClickSequencesResponse,
    ValidationError,
    WeekdayStats,
    WeekdayStatsResponse,
)


router = APIRouter()


def _serialize_config(config: dict, is_enabled: bool, updated_at) -> MenuLayoutResponse:
    """Сериализовать конфигурацию в response."""
    rows = []
    for row_data in config.get('rows', []):
        rows.append(
            MenuRowConfig(
                id=row_data['id'],
                buttons=row_data.get('buttons', []),
                conditions=ButtonConditions(**row_data['conditions']) if row_data.get('conditions') else None,
                max_per_row=row_data.get('max_per_row', 2),
            )
        )

    buttons = {}
    for btn_id, btn_data in config.get('buttons', {}).items():
        buttons[btn_id] = MenuButtonConfig(
            type=btn_data['type'],
            builtin_id=btn_data.get('builtin_id'),
            text=btn_data.get('text', {}),
            icon=btn_data.get('icon'),
            action=btn_data.get('action', ''),
            enabled=btn_data.get('enabled', True),
            visibility=btn_data.get('visibility', 'all'),
            conditions=ButtonConditions(**btn_data['conditions']) if btn_data.get('conditions') else None,
            dynamic_text=btn_data.get('dynamic_text', False),
            open_mode=btn_data.get('open_mode', 'callback'),
            webapp_url=btn_data.get('webapp_url'),
            description=btn_data.get('description'),
            sort_order=btn_data.get('sort_order'),
        )

    return MenuLayoutResponse(
        version=config.get('version', 1),
        rows=rows,
        buttons=buttons,
        is_enabled=is_enabled,
        updated_at=updated_at,
    )


@router.get('', response_model=MenuLayoutResponse)
async def get_menu_layout(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> MenuLayoutResponse:
    """Получить текущую конфигурацию меню."""
    config = await MenuLayoutService.get_config(db)
    updated_at = await MenuLayoutService.get_config_updated_at(db)
    return _serialize_config(config, settings.MENU_LAYOUT_ENABLED, updated_at)


@router.put('', response_model=MenuLayoutResponse)
async def update_menu_layout(
    payload: MenuLayoutUpdateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> MenuLayoutResponse:
    """Обновить конфигурацию меню полностью."""
    config = await MenuLayoutService.get_config(db)
    config = config.copy()

    if payload.rows is not None:
        config['rows'] = [row.model_dump() for row in payload.rows]

    if payload.buttons is not None:
        buttons_config = {}
        for btn_id, btn in payload.buttons.items():
            btn_dict = btn.model_dump()
            # Автоматически определяем наличие плейсхолдеров, если dynamic_text не установлен
            if not btn_dict.get('dynamic_text', False):
                from app.services.menu_layout.service import MenuLayoutService

                btn_dict['dynamic_text'] = MenuLayoutService._text_has_placeholders(btn_dict.get('text', {}))
            buttons_config[btn_id] = btn_dict
        config['buttons'] = buttons_config

    await MenuLayoutService.save_config(db, config)
    updated_at = await MenuLayoutService.get_config_updated_at(db)
    return _serialize_config(config, settings.MENU_LAYOUT_ENABLED, updated_at)


@router.post('/reset', response_model=MenuLayoutResponse)
async def reset_menu_layout(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> MenuLayoutResponse:
    """Сбросить конфигурацию к дефолтной."""
    config = await MenuLayoutService.reset_to_default(db)
    updated_at = await MenuLayoutService.get_config_updated_at(db)
    return _serialize_config(config, settings.MENU_LAYOUT_ENABLED, updated_at)


@router.get('/builtin-buttons', response_model=BuiltinButtonsListResponse)
async def list_builtin_buttons(
    _: Any = Security(require_api_token),
) -> BuiltinButtonsListResponse:
    """Получить список встроенных кнопок."""
    items = []
    for btn_info in MenuLayoutService.get_builtin_buttons_info():
        items.append(
            BuiltinButtonInfo(
                id=btn_info['id'],
                default_text=btn_info['default_text'],
                callback_data=btn_info['callback_data'],
                default_conditions=ButtonConditions(**btn_info['default_conditions'])
                if btn_info.get('default_conditions')
                else None,
                supports_dynamic_text=btn_info.get('supports_dynamic_text', False),
                supports_direct_open=btn_info.get('supports_direct_open', False),
            )
        )

    return BuiltinButtonsListResponse(items=items, total=len(items))


@router.patch('/buttons/{button_id}')
async def update_button(
    button_id: str,
    payload: ButtonUpdateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> MenuButtonConfig:
    """Обновить конфигурацию отдельной кнопки."""
    try:
        updates = payload.model_dump(exclude_unset=True)
        # Конвертируем visibility в строку если есть
        if 'visibility' in updates and updates['visibility'] is not None:
            if hasattr(updates['visibility'], 'value'):
                updates['visibility'] = updates['visibility'].value
        # Конвертируем open_mode в строку если есть
        if 'open_mode' in updates and updates['open_mode'] is not None:
            if hasattr(updates['open_mode'], 'value'):
                updates['open_mode'] = updates['open_mode'].value
        # Конвертируем conditions - убираем None значения если это dict
        if 'conditions' in updates and updates['conditions'] is not None:
            if isinstance(updates['conditions'], dict):
                updates['conditions'] = {k: v for k, v in updates['conditions'].items() if v is not None}
            elif hasattr(updates['conditions'], 'model_dump'):
                updates['conditions'] = updates['conditions'].model_dump(exclude_none=True)

        button = await MenuLayoutService.update_button(db, button_id, updates)

        return MenuButtonConfig(
            type=button['type'],
            builtin_id=button.get('builtin_id'),
            text=button.get('text', {}),
            icon=button.get('icon'),
            action=button.get('action', ''),
            enabled=button.get('enabled', True),
            visibility=button.get('visibility', 'all'),
            conditions=ButtonConditions(**button['conditions']) if button.get('conditions') else None,
            dynamic_text=button.get('dynamic_text', False),
            open_mode=button.get('open_mode', 'callback'),
            webapp_url=button.get('webapp_url'),
            description=button.get('description'),
        )
    except KeyError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e


@router.post('/rows/reorder', response_model=list[MenuRowConfig])
async def reorder_rows(
    payload: RowsReorderRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> list[MenuRowConfig]:
    """Изменить порядок строк."""
    try:
        rows = await MenuLayoutService.reorder_rows(db, payload.ordered_ids)
        return [
            MenuRowConfig(
                id=row['id'],
                buttons=row.get('buttons', []),
                conditions=ButtonConditions(**row['conditions']) if row.get('conditions') else None,
                max_per_row=row.get('max_per_row', 2),
            )
            for row in rows
        ]
    except KeyError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e


@router.post('/rows', response_model=MenuRowConfig, status_code=status.HTTP_201_CREATED)
async def add_row(
    payload: AddRowRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> MenuRowConfig:
    """Добавить новую строку."""
    try:
        row_config = {
            'id': payload.id,
            'buttons': payload.buttons,
            'conditions': payload.conditions.model_dump(exclude_none=True) if payload.conditions else None,
            'max_per_row': payload.max_per_row,
        }
        row = await MenuLayoutService.add_row(db, row_config, payload.position)

        return MenuRowConfig(
            id=row['id'],
            buttons=row.get('buttons', []),
            conditions=ButtonConditions(**row['conditions']) if row.get('conditions') else None,
            max_per_row=row.get('max_per_row', 2),
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.delete('/rows/{row_id}', status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_row(
    row_id: str,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    """Удалить строку."""
    try:
        await MenuLayoutService.delete_row(db, row_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except KeyError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e


@router.post('/buttons', response_model=MenuButtonConfig, status_code=status.HTTP_201_CREATED)
async def add_custom_button(
    payload: AddCustomButtonRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> MenuButtonConfig:
    """Добавить кастомную кнопку (URL, MiniApp или callback)."""
    try:
        # Автоматически определяем наличие плейсхолдеров, если dynamic_text не установлен
        dynamic_text = payload.dynamic_text
        if not dynamic_text:
            from app.services.menu_layout.service import MenuLayoutService

            dynamic_text = MenuLayoutService._text_has_placeholders(payload.text)

        button_config = {
            'type': payload.type.value,
            'text': payload.text,
            'icon': payload.icon,
            'action': payload.action,
            'visibility': payload.visibility.value,
            'conditions': payload.conditions.model_dump(exclude_none=True) if payload.conditions else None,
            'dynamic_text': dynamic_text,
            'description': payload.description,
        }
        button = await MenuLayoutService.add_custom_button(db, payload.id, button_config, payload.row_id)

        return MenuButtonConfig(
            type=button['type'],
            builtin_id=button.get('builtin_id'),
            text=button.get('text', {}),
            icon=button.get('icon'),
            action=button.get('action', ''),
            enabled=button.get('enabled', True),
            visibility=button.get('visibility', 'all'),
            conditions=ButtonConditions(**button['conditions']) if button.get('conditions') else None,
            dynamic_text=button.get('dynamic_text', False),
            open_mode=button.get('open_mode', 'callback'),
            webapp_url=button.get('webapp_url'),
            description=button.get('description'),
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.delete('/buttons/{button_id}', status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_custom_button(
    button_id: str,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    """Удалить кастомную кнопку."""
    try:
        await MenuLayoutService.delete_custom_button(db, button_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except KeyError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.post('/preview', response_model=MenuPreviewResponse)
async def preview_menu(
    payload: MenuPreviewRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> MenuPreviewResponse:
    """Предпросмотр меню для указанного контекста пользователя."""
    context = MenuContext(
        language=payload.language,
        is_admin=payload.is_admin,
        is_moderator=payload.is_moderator,
        has_active_subscription=payload.has_active_subscription,
        subscription_is_active=payload.subscription_is_active,
        balance_kopeks=payload.balance_kopeks,
    )

    preview_rows = await MenuLayoutService.preview_keyboard(db, context)

    rows = []
    total_buttons = 0
    for row_data in preview_rows:
        buttons = [
            MenuPreviewButton(
                text=btn['text'],
                action=btn['action'],
                type=btn['type'],
            )
            for btn in row_data['buttons']
        ]
        total_buttons += len(buttons)
        rows.append(MenuPreviewRow(buttons=buttons))

    return MenuPreviewResponse(rows=rows, total_buttons=total_buttons)


# --- Эндпоинты для перемещения кнопок ---


@router.post('/buttons/{button_id}/move-up', response_model=MoveButtonResponse)
async def move_button_up(
    button_id: str,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> MoveButtonResponse:
    """Переместить кнопку вверх (в предыдущую строку или на позицию выше в текущей строке)."""
    try:
        result = await MenuLayoutService.move_button_up(db, button_id)
        return MoveButtonResponse(
            button_id=button_id,
            new_row_index=result.get('new_row_index'),
            position=result.get('new_position'),
        )
    except KeyError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.post('/buttons/{button_id}/move-down', response_model=MoveButtonResponse)
async def move_button_down(
    button_id: str,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> MoveButtonResponse:
    """Переместить кнопку вниз (в следующую строку или на позицию ниже в текущей строке)."""
    try:
        result = await MenuLayoutService.move_button_down(db, button_id)
        return MoveButtonResponse(
            button_id=button_id,
            new_row_index=result.get('new_row_index'),
            position=result.get('new_position'),
        )
    except KeyError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.post('/buttons/{button_id}/move-to-row', response_model=MoveButtonResponse)
async def move_button_to_row(
    button_id: str,
    payload: MoveButtonToRowRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> MoveButtonResponse:
    """Переместить кнопку в указанную строку."""
    try:
        result = await MenuLayoutService.move_button_to_row(db, button_id, payload.target_row_id, payload.position)
        return MoveButtonResponse(
            button_id=button_id,
            target_row_id=payload.target_row_id,
            position=result.get('new_position'),
        )
    except KeyError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.post('/rows/{row_id}/reorder-buttons', response_model=ReorderButtonsResponse)
async def reorder_buttons_in_row(
    row_id: str,
    payload: ReorderButtonsInRowRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> ReorderButtonsResponse:
    """Изменить порядок кнопок в строке."""
    try:
        result = await MenuLayoutService.reorder_buttons_in_row(db, row_id, payload.ordered_button_ids)
        return ReorderButtonsResponse(
            row_id=row_id,
            buttons=result['buttons'],
        )
    except KeyError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.post('/buttons/swap', response_model=SwapButtonsResponse)
async def swap_buttons(
    payload: SwapButtonsRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> SwapButtonsResponse:
    """Обменять местами две кнопки (даже из разных строк)."""
    try:
        result = await MenuLayoutService.swap_buttons(db, payload.button_id_1, payload.button_id_2)
        return SwapButtonsResponse(
            button_1=result['button_1'],
            button_2=result['button_2'],
        )
    except KeyError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


# --- Новые эндпоинты ---


@router.get('/available-callbacks', response_model=AvailableCallbacksResponse)
async def list_available_callbacks(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> AvailableCallbacksResponse:
    """Получить список всех доступных callback_data для создания кнопок."""
    callbacks = await MenuLayoutService.get_available_callbacks(db)

    items = [
        AvailableCallback(
            callback_data=cb['callback_data'],
            name=cb['name'],
            description=cb.get('description'),
            category=cb['category'],
            default_text=cb.get('default_text'),
            default_icon=cb.get('default_icon'),
            requires_subscription=cb.get('requires_subscription', False),
            is_in_menu=cb.get('is_in_menu', False),
        )
        for cb in callbacks
    ]

    categories = list({cb['category'] for cb in callbacks})

    return AvailableCallbacksResponse(
        items=items,
        total=len(items),
        categories=sorted(categories),
    )


@router.get('/placeholders', response_model=DynamicPlaceholdersResponse)
async def list_dynamic_placeholders(
    _: Any = Security(require_api_token),
) -> DynamicPlaceholdersResponse:
    """Получить список доступных динамических плейсхолдеров для текста кнопок."""
    placeholders = MenuLayoutService.get_dynamic_placeholders()

    items = [
        DynamicPlaceholder(
            placeholder=p['placeholder'],
            description=p['description'],
            example=p['example'],
            category=p['category'],
        )
        for p in placeholders
    ]

    return DynamicPlaceholdersResponse(items=items, total=len(items))


@router.get('/export', response_model=MenuLayoutExportResponse)
async def export_menu_layout(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> MenuLayoutExportResponse:
    """Экспортировать конфигурацию меню."""

    export_data = await MenuLayoutService.export_config(db)

    rows = []
    for row_data in export_data.get('rows', []):
        rows.append(
            MenuRowConfig(
                id=row_data['id'],
                buttons=row_data.get('buttons', []),
                conditions=ButtonConditions(**row_data['conditions']) if row_data.get('conditions') else None,
                max_per_row=row_data.get('max_per_row', 2),
            )
        )

    buttons = {}
    for btn_id, btn_data in export_data.get('buttons', {}).items():
        buttons[btn_id] = MenuButtonConfig(
            type=btn_data['type'],
            builtin_id=btn_data.get('builtin_id'),
            text=btn_data.get('text', {}),
            icon=btn_data.get('icon'),
            action=btn_data.get('action', ''),
            enabled=btn_data.get('enabled', True),
            visibility=btn_data.get('visibility', 'all'),
            conditions=ButtonConditions(**btn_data['conditions']) if btn_data.get('conditions') else None,
            dynamic_text=btn_data.get('dynamic_text', False),
            open_mode=btn_data.get('open_mode', 'callback'),
            webapp_url=btn_data.get('webapp_url'),
            description=btn_data.get('description'),
        )

    return MenuLayoutExportResponse(
        version=export_data.get('version', 1),
        rows=rows,
        buttons=buttons,
        exported_at=datetime.now(UTC),
    )


@router.post('/import', response_model=MenuLayoutImportResponse)
async def import_menu_layout(
    payload: MenuLayoutImportRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> MenuLayoutImportResponse:
    """Импортировать конфигурацию меню."""
    import_data = {
        'version': payload.version,
        'rows': [row.model_dump() for row in payload.rows],
        'buttons': {btn_id: btn.model_dump() for btn_id, btn in payload.buttons.items()},
    }

    result = await MenuLayoutService.import_config(db, import_data, payload.merge_mode)

    return MenuLayoutImportResponse(
        success=result['success'],
        imported_rows=result['imported_rows'],
        imported_buttons=result['imported_buttons'],
        warnings=result['warnings'],
    )


@router.post('/validate', response_model=MenuLayoutValidateResponse)
async def validate_menu_layout(
    payload: MenuLayoutValidateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> MenuLayoutValidateResponse:
    """Валидировать конфигурацию меню без сохранения."""
    # Если данные не переданы, валидируем текущую конфигурацию
    if payload.rows is None and payload.buttons is None:
        config = await MenuLayoutService.get_config(db)
    else:
        config = {
            'rows': [row.model_dump() for row in payload.rows] if payload.rows else [],
            'buttons': {btn_id: btn.model_dump() for btn_id, btn in payload.buttons.items()} if payload.buttons else {},
        }

    result = MenuLayoutService.validate_config(config)

    return MenuLayoutValidateResponse(
        is_valid=result['is_valid'],
        errors=[
            ValidationError(
                field=e['field'],
                message=e['message'],
                severity=e['severity'],
            )
            for e in result['errors']
        ],
        warnings=[
            ValidationError(
                field=w['field'],
                message=w['message'],
                severity=w['severity'],
            )
            for w in result['warnings']
        ],
    )


# --- Эндпоинты истории изменений ---


@router.get('/history', response_model=MenuLayoutHistoryResponse)
async def get_menu_layout_history(
    limit: int = 50,
    offset: int = 0,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> MenuLayoutHistoryResponse:
    """Получить историю изменений меню."""
    entries = await MenuLayoutService.get_history(db, limit, offset)
    total = await MenuLayoutService.get_history_count(db)

    return MenuLayoutHistoryResponse(
        items=[
            MenuLayoutHistoryEntry(
                id=entry['id'],
                created_at=entry['created_at'],
                action=entry['action'],
                changes_summary=entry['changes_summary'] or '',
                user_info=entry['user_info'],
            )
            for entry in entries
        ],
        total=total,
    )


@router.get('/history/{history_id}')
async def get_history_entry(
    history_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Получить конкретную запись истории с полной конфигурацией."""
    entry = await MenuLayoutService.get_history_entry(db, history_id)
    if not entry:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'History entry {history_id} not found')

    return {
        'id': entry['id'],
        'action': entry['action'],
        'changes_summary': entry['changes_summary'],
        'user_info': entry['user_info'],
        'created_at': entry['created_at'].isoformat() if entry['created_at'] else None,
        'config': entry['config'],
    }


@router.post('/history/{history_id}/rollback', response_model=MenuLayoutResponse)
async def rollback_to_history(
    history_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> MenuLayoutResponse:
    """Откатить конфигурацию к записи из истории."""
    try:
        config = await MenuLayoutService.rollback_to_history(db, history_id)
        updated_at = await MenuLayoutService.get_config_updated_at(db)
        return _serialize_config(config, settings.MENU_LAYOUT_ENABLED, updated_at)
    except KeyError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e


# --- Эндпоинты статистики кликов ---


@router.get('/stats', response_model=MenuClickStatsResponse)
async def get_menu_click_stats(
    days: int = 30,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> MenuClickStatsResponse:
    """Получить общую статистику кликов по всем кнопкам."""

    stats = await MenuLayoutService.get_all_buttons_stats(db, days)
    total_clicks = await MenuLayoutService.get_total_clicks(db, days)

    now = datetime.now(UTC)
    period_start = now - timedelta(days=days)

    return MenuClickStatsResponse(
        items=[
            ButtonClickStats(
                button_id=s['button_id'],
                clicks_total=s['clicks_total'],
                clicks_today=s.get('clicks_today', 0),
                clicks_week=s.get('clicks_week', 0),
                clicks_month=s.get('clicks_month', 0),
                unique_users=s['unique_users'],
                last_click_at=s['last_click_at'],
            )
            for s in stats
        ],
        total_clicks=total_clicks,
        period_start=period_start,
        period_end=now,
    )


@router.get('/stats/buttons/{button_id}', response_model=ButtonClickStatsResponse)
async def get_button_click_stats(
    button_id: str,
    days: int = 30,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> ButtonClickStatsResponse:
    """Получить статистику кликов по конкретной кнопке."""
    stats = await MenuLayoutService.get_button_stats(db, button_id, days)
    clicks_by_day = await MenuLayoutService.get_button_clicks_by_day(db, button_id, days)

    return ButtonClickStatsResponse(
        button_id=button_id,
        stats=ButtonClickStats(
            button_id=stats['button_id'],
            clicks_total=stats['clicks_total'],
            clicks_today=stats['clicks_today'],
            clicks_week=stats['clicks_week'],
            clicks_month=stats['clicks_month'],
            unique_users=stats['unique_users'],
            last_click_at=stats['last_click_at'],
        ),
        clicks_by_day=clicks_by_day,
    )


@router.post('/stats/log-click')
async def log_button_click(
    button_id: str,
    user_id: int | None = None,
    callback_data: str | None = None,
    button_type: str | None = None,
    button_text: str | None = None,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Записать клик по кнопке (для внешней интеграции)."""
    await MenuLayoutService.log_button_click(
        db,
        button_id=button_id,
        user_id=user_id,
        callback_data=callback_data,
        button_type=button_type,
        button_text=button_text,
    )
    return {'success': True}


@router.get('/stats/by-type', response_model=ButtonTypeStatsResponse)
async def get_stats_by_button_type(
    days: int = 30,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> ButtonTypeStatsResponse:
    """Получить статистику кликов по типам кнопок (builtin, callback, url, mini_app)."""
    try:
        stats = await MenuLayoutService.get_stats_by_button_type(db, days)
        total_clicks = sum(s['clicks_total'] for s in stats)

        return ButtonTypeStatsResponse(
            items=[
                ButtonTypeStats(
                    button_type=s['button_type'],
                    clicks_total=s['clicks_total'],
                    unique_users=s['unique_users'],
                )
                for s in stats
            ],
            total_clicks=total_clicks,
        )
    except Exception as e:
        logger.error('Error getting stats by type', error=e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'Internal server error: {e!s}')


@router.get('/stats/by-hour', response_model=HourlyStatsResponse)
async def get_clicks_by_hour(
    button_id: str | None = None,
    days: int = 30,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> HourlyStatsResponse:
    """Получить статистику кликов по часам дня (0-23)."""
    stats = await MenuLayoutService.get_clicks_by_hour(db, button_id, days)

    return HourlyStatsResponse(
        items=[HourlyStats(hour=s['hour'], count=s['count']) for s in stats],
        button_id=button_id,
    )


@router.get('/stats/by-weekday', response_model=WeekdayStatsResponse)
async def get_clicks_by_weekday(
    button_id: str | None = None,
    days: int = 30,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> WeekdayStatsResponse:
    """Получить статистику кликов по дням недели."""
    stats = await MenuLayoutService.get_clicks_by_weekday(db, button_id, days)

    return WeekdayStatsResponse(
        items=[WeekdayStats(weekday=s['weekday'], weekday_name=s['weekday_name'], count=s['count']) for s in stats],
        button_id=button_id,
    )


@router.get('/stats/top-users', response_model=TopUsersResponse)
async def get_top_users(
    button_id: str | None = None,
    limit: int = 10,
    days: int = 30,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> TopUsersResponse:
    """Получить топ пользователей по количеству кликов."""
    try:
        stats = await MenuLayoutService.get_top_users(db, button_id, limit, days)

        return TopUsersResponse(
            items=[
                TopUserStats(
                    user_id=s['user_id'],
                    clicks_count=s['clicks_count'],
                    last_click_at=s['last_click_at'],
                )
                for s in stats
            ],
            button_id=button_id,
            limit=limit,
        )
    except Exception as e:
        logger.error('Error getting top users', error=e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'Internal server error: {e!s}')


@router.get('/stats/compare', response_model=PeriodComparisonResponse)
async def get_period_comparison(
    button_id: str | None = None,
    current_days: int = 7,
    previous_days: int = 7,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PeriodComparisonResponse:
    """Сравнить статистику текущего и предыдущего периода."""
    try:
        comparison = await MenuLayoutService.get_period_comparison(db, button_id, current_days, previous_days)

        logger.debug(
            'Period comparison: button_id=, current_days=, previous_days=, trend',
            button_id=button_id,
            current_days=current_days,
            previous_days=previous_days,
            get=comparison.get('change', {}).get('trend'),
        )

        return PeriodComparisonResponse(
            current_period=comparison['current_period'],
            previous_period=comparison['previous_period'],
            change=comparison['change'],
            button_id=button_id,
        )
    except Exception as e:
        logger.error('Error getting period comparison', error=e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'Internal server error: {e!s}')


@router.get('/stats/users/{user_id}/sequences', response_model=UserClickSequencesResponse)
async def get_user_click_sequences(
    user_id: int,
    limit: int = 50,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> UserClickSequencesResponse:
    """Получить последовательности кликов пользователя."""
    try:
        sequences = await MenuLayoutService.get_user_click_sequences(db, user_id, limit)

        logger.debug(
            'User sequences: user_id=, limit=, found= sequences',
            user_id=user_id,
            limit=limit,
            sequences_count=len(sequences),
        )

        return UserClickSequencesResponse(
            user_id=user_id,
            items=[
                UserClickSequence(
                    button_id=s['button_id'],
                    button_text=s['button_text'],
                    clicked_at=s['clicked_at'],
                )
                for s in sequences
            ],
            total=len(sequences),
        )
    except Exception as e:
        logger.error('Error getting user sequences: user_id=, error', user_id=user_id, error=e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'Internal server error: {e!s}')
