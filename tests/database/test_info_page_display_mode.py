import pytest
from pydantic import ValidationError

from app.cabinet.schemas.info_pages import (
    InfoPageCreateRequest,
    InfoPageListItem,
    InfoPageResponse,
    InfoPageUpdateRequest,
)
from app.database.crud.info_pages import _ALLOWED_UPDATE_FIELDS
from app.database.models import InfoPage


def test_model_has_display_mode_column_with_both_default():
    column = InfoPage.__table__.c.display_mode
    assert column.nullable is False
    assert column.server_default.arg == 'both'
    assert column.type.length == 10


def test_crud_update_whitelist_includes_display_mode():
    assert 'display_mode' in _ALLOWED_UPDATE_FIELDS


def test_create_request_accepts_valid_display_mode():
    request = InfoPageCreateRequest(slug='test-page', display_mode='bot')
    assert request.display_mode == 'bot'


def test_create_request_defaults_to_both():
    request = InfoPageCreateRequest(slug='test-page')
    assert request.display_mode == 'both'


def test_update_request_rejects_invalid_display_mode():
    with pytest.raises(ValidationError):
        InfoPageUpdateRequest(display_mode='hidden')


def test_response_schemas_expose_display_mode():
    assert 'display_mode' in InfoPageResponse.model_fields
    assert 'display_mode' in InfoPageListItem.model_fields
