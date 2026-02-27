from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Security, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.localization.texts import get_texts
from app.services.system_settings_service import (
    ReadOnlySettingError,
    bot_configuration_service,
)

from ..dependencies import get_db_session, require_api_token
from ..schemas.config import (
    SettingCategoryRef,
    SettingCategorySummary,
    SettingChoice,
    SettingDefinition,
    SettingUpdateRequest,
)


router = APIRouter()


def _coerce_value(key: str, value: Any, texts: Any) -> Any:
    definition = bot_configuration_service.get_definition(key)

    if value is None:
        if definition.is_optional:
            return None
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t('WEBAPI_CONFIG_VALUE_REQUIRED', 'Value is required'),
        )

    python_type = definition.python_type

    try:
        if python_type is bool:
            if isinstance(value, bool):
                normalized = value
            elif isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in {'true', '1', 'yes', 'on', 'да'}:
                    normalized = True
                elif lowered in {'false', '0', 'no', 'off', 'нет'}:
                    normalized = False
                else:
                    raise ValueError('invalid bool')
            else:
                raise ValueError('invalid bool')

        elif python_type is int:
            normalized = int(value)
        elif python_type is float:
            normalized = float(value)
        else:
            normalized = str(value)
    except ValueError:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t('WEBAPI_CONFIG_INVALID_VALUE_TYPE', 'Invalid value type'),
        ) from None

    choices = bot_configuration_service.get_choice_options(key)
    if choices:
        allowed_values = {option.value for option in choices}
        if normalized not in allowed_values:
            readable = ', '.join(bot_configuration_service.format_value(opt.value) for opt in choices)
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=texts.t(
                    'WEBAPI_CONFIG_VALUE_MUST_BE_ONE_OF',
                    'Value must be one of: {readable}',
                    readable=readable,
                ),
            )

    return normalized


def _serialize_definition(definition, include_choices: bool = True) -> SettingDefinition:
    current = bot_configuration_service.get_current_value(definition.key)
    original = bot_configuration_service.get_original_value(definition.key)
    has_override = bot_configuration_service.has_override(definition.key)

    choices: list[SettingChoice] = []
    if include_choices:
        choices = [
            SettingChoice(
                value=option.value,
                label=option.label,
                description=option.description,
            )
            for option in bot_configuration_service.get_choice_options(definition.key)
        ]

    return SettingDefinition(
        key=definition.key,
        name=definition.display_name,
        category=SettingCategoryRef(
            key=definition.category_key,
            label=definition.category_label,
        ),
        type=definition.type_label,
        is_optional=definition.is_optional,
        current=current,
        original=original,
        has_override=has_override,
        read_only=bot_configuration_service.is_read_only(definition.key),
        choices=choices,
    )


@router.get('/categories', response_model=list[SettingCategorySummary])
async def list_categories(
    _: object = Security(require_api_token),
) -> list[SettingCategorySummary]:
    categories = bot_configuration_service.get_categories()
    return [SettingCategorySummary(key=key, label=label, items=count) for key, label, count in categories]


@router.get('', response_model=list[SettingDefinition])
async def list_settings(
    _: object = Security(require_api_token),
    category: str | None = Query(default=None, alias='category_key'),
) -> list[SettingDefinition]:
    items: list[SettingDefinition] = []
    if category:
        definitions = bot_configuration_service.get_settings_for_category(category)
        items.extend(_serialize_definition(defn) for defn in definitions)
        return items

    for category_key, _, _ in bot_configuration_service.get_categories():
        definitions = bot_configuration_service.get_settings_for_category(category_key)
        items.extend(_serialize_definition(defn) for defn in definitions)

    return items


@router.get('/{key}', response_model=SettingDefinition)
async def get_setting(
    key: str,
    _: object = Security(require_api_token),
) -> SettingDefinition:
    texts = get_texts('ru')
    try:
        definition = bot_configuration_service.get_definition(key)
    except KeyError as error:  # pragma: no cover - защита от некорректного ключа
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_CONFIG_SETTING_NOT_FOUND', 'Setting not found'),
        ) from error

    return _serialize_definition(definition)


@router.put('/{key}', response_model=SettingDefinition)
async def update_setting(
    key: str,
    payload: SettingUpdateRequest,
    _: object = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> SettingDefinition:
    texts = get_texts('ru')
    try:
        definition = bot_configuration_service.get_definition(key)
    except KeyError as error:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_CONFIG_SETTING_NOT_FOUND', 'Setting not found'),
        ) from error

    value = _coerce_value(key, payload.value, texts)
    try:
        await bot_configuration_service.set_value(db, key, value)
    except ReadOnlySettingError as error:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(error)) from error
    await db.commit()

    return _serialize_definition(definition)


@router.delete('/{key}', response_model=SettingDefinition)
async def reset_setting(
    key: str,
    _: object = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> SettingDefinition:
    texts = get_texts('ru')
    try:
        definition = bot_configuration_service.get_definition(key)
    except KeyError as error:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_CONFIG_SETTING_NOT_FOUND', 'Setting not found'),
        ) from error

    try:
        await bot_configuration_service.reset_value(db, key)
    except ReadOnlySettingError as error:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(error)) from error
    await db.commit()
    return _serialize_definition(definition)
