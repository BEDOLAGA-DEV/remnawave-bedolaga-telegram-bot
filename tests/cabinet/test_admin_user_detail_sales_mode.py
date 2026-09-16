"""Карточка пользователя знает режим продаж бота.

Плитки над вкладками зависят от режима: в классике тарифа нет — подписи «· Командный»
быть не должно; в мультитарифе у человека несколько подписок, и плитки «до / трафик /
устройства» одной из них вводили в заблуждение — там нужна сводка по подпискам.
"""

from __future__ import annotations

import pytest

from app.cabinet.routes.admin_users import _sales_mode_fields
from app.cabinet.schemas.users import UserDetailResponse
from app.config import settings


@pytest.mark.parametrize(
    ('sales_mode', 'multi', 'expected'),
    [
        ('classic', False, {'sales_mode': 'classic', 'multi_tariff_enabled': False}),
        ('tariffs', False, {'sales_mode': 'tariffs', 'multi_tariff_enabled': False}),
        ('tariffs', True, {'sales_mode': 'tariffs', 'multi_tariff_enabled': True}),
        # Мультитариф без тарифов не работает — флаг не должен врать.
        ('classic', True, {'sales_mode': 'classic', 'multi_tariff_enabled': False}),
    ],
)
def test_sales_mode_fields_follow_settings(
    monkeypatch: pytest.MonkeyPatch, sales_mode: str, multi: bool, expected: dict
) -> None:
    monkeypatch.setattr(settings, 'SALES_MODE', sales_mode)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)

    assert _sales_mode_fields() == expected


def test_detail_response_declares_mode_fields() -> None:
    fields = UserDetailResponse.model_fields
    assert fields['sales_mode'].default == 'tariffs'
    assert fields['multi_tariff_enabled'].default is False
