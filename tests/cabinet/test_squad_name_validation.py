"""Имя внутреннего сквада проверяется на нашей границе по правилам панели.

Remnawave 3.4.3 (POST/PATCH /api/internal-squads): 2–30 символов, только латиница, цифры,
пробел, дефис и подчёркивание. Схемы кабинета и веб-API раньше пропускали 1–255 любых
символов, панель отвечала 400, а админ видел безликое «не удалось».
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.cabinet.schemas.remnawave import SquadActionRequest, SquadCreateRequest, SquadUpdateRequest
from app.external.remnawave_api import (
    INTERNAL_SQUAD_NAME_MAX_LENGTH,
    INTERNAL_SQUAD_NAME_MIN_LENGTH,
    is_valid_internal_squad_name,
)
from app.webapi.schemas.remnawave import (
    RemnaWaveSquadActionRequest,
    RemnaWaveSquadCreateRequest,
    RemnaWaveSquadUpdateRequest,
)


BAD_NAMES = ['Сквад', 'x', 'a' * 31, 'name!', 'name/1', '']
GOOD_NAMES = ['EU', 'My Squad-1_x', 'a' * 30]


def test_limits_match_panel_contract() -> None:
    assert (INTERNAL_SQUAD_NAME_MIN_LENGTH, INTERNAL_SQUAD_NAME_MAX_LENGTH) == (2, 30)


@pytest.mark.parametrize('name', BAD_NAMES)
def test_helper_rejects_names_the_panel_rejects(name: str) -> None:
    assert is_valid_internal_squad_name(name) is False


@pytest.mark.parametrize('name', GOOD_NAMES)
def test_helper_accepts_panel_valid_names(name: str) -> None:
    assert is_valid_internal_squad_name(name) is True


@pytest.mark.parametrize('name', BAD_NAMES)
@pytest.mark.parametrize(
    'model',
    [SquadCreateRequest, RemnaWaveSquadCreateRequest],
    ids=['cabinet', 'webapi'],
)
def test_create_schemas_reject_invalid_names(model: type, name: str) -> None:
    with pytest.raises(ValidationError):
        model(name=name)


@pytest.mark.parametrize('name', BAD_NAMES)
@pytest.mark.parametrize(
    'model',
    [SquadUpdateRequest, RemnaWaveSquadUpdateRequest],
    ids=['cabinet', 'webapi'],
)
def test_update_schemas_reject_invalid_names(model: type, name: str) -> None:
    with pytest.raises(ValidationError):
        model(name=name)


@pytest.mark.parametrize('name', BAD_NAMES)
@pytest.mark.parametrize(
    'model',
    [SquadActionRequest, RemnaWaveSquadActionRequest],
    ids=['cabinet', 'webapi'],
)
def test_rename_action_rejects_invalid_names(model: type, name: str) -> None:
    with pytest.raises(ValidationError):
        model(action='rename', name=name)


@pytest.mark.parametrize('name', GOOD_NAMES)
def test_schemas_accept_panel_valid_names(name: str) -> None:
    assert SquadCreateRequest(name=name).name == name
    assert RemnaWaveSquadCreateRequest(name=name).name == name
    assert SquadUpdateRequest(name=name).name == name
    assert RemnaWaveSquadActionRequest(action='rename', name=name).name == name


def test_update_and_action_still_allow_omitting_name() -> None:
    assert SquadUpdateRequest().name is None
    assert RemnaWaveSquadUpdateRequest().name is None
    assert SquadActionRequest(action='delete').name is None
