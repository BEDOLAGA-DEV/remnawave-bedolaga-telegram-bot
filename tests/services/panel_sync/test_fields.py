"""Узкий PATCH в панель: один набор «полей аккаунта» для кабинета и бота.

Кнопка «в панель» на карточке пользователя шлёт только то, что админ разрешил,
плюс поля, описывающие сам аккаунт. Тег панели — такое же поле аккаунта, как
описание: без него тег тарифа не доезжал до панели через поштучный синк.
"""

from app.services.panel_sync.fields import PANEL_ACCOUNT_METADATA_FIELDS, narrow_push_fields


def test_account_metadata_includes_tag_alongside_description():
    assert {'description', 'hwid_device_limit', 'external_squad_uuid', 'tag'} <= PANEL_ACCOUNT_METADATA_FIELDS


def test_narrow_push_without_flags_sends_only_account_metadata():
    assert narrow_push_fields() == set(PANEL_ACCOUNT_METADATA_FIELDS)


def test_flags_add_exactly_their_fields():
    fields = narrow_push_fields(status=True, expire_date=True, traffic_limit=True, squads=True)
    assert fields - PANEL_ACCOUNT_METADATA_FIELDS == {
        'status',
        'expire_at',
        'traffic_limit_bytes',
        'traffic_limit_strategy',
        'active_internal_squads',
    }
    assert narrow_push_fields(squads=True) - PANEL_ACCOUNT_METADATA_FIELDS == {'active_internal_squads'}


def test_extra_fields_are_merged_without_losing_metadata():
    assert narrow_push_fields(extra={'traffic_limit_bytes'}) == set(PANEL_ACCOUNT_METADATA_FIELDS) | {
        'traffic_limit_bytes'
    }
