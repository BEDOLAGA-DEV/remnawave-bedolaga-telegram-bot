"""Multi-tariff HWID device-limit bleed: each tariff is its own RemnaWave panel
user, so device reads must resolve the SUBSCRIPTION's panel user and must NOT
fall back to the user-level one in multi-tariff mode (the fallback showed/shared
another tariff's devices, making the limit look "counted by the smallest tariff").

API 3.0.0: панельный пользователь адресуется числовым `remnawave_id`, а не UUID.
Резолверы обязаны отдавать именно id — легаси-колонка `remnawave_uuid` осталась
в БД как исторические данные и читаться не должна (иначе клиент получит мусорный
идентификатор и запрос отвалится валидацией на границе, а не 404).
"""

from __future__ import annotations

from app.cabinet.routes.subscription_modules.devices import _resolve_panel_user_id
from app.config import Settings
from app.handlers.subscription.devices import _get_panel_user_id


USER_PANEL_ID = 101
SUB_B_PANEL_ID = 202


class _Obj:
    """Носитель панельной привязки: числовой id 3.0.0 + легаси-uuid-ловушка."""

    def __init__(self, panel_id: int | None, legacy_uuid: str = 'legacy-uuid-must-not-be-read'):
        self.remnawave_id = panel_id
        self.remnawave_uuid = legacy_uuid


def _set_multi(monkeypatch, value: bool) -> None:
    # Settings methods are patched on the class, fields on the instance (pydantic).
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: value)


# ---- bot handler: _get_panel_user_id ----


def test_bot_multi_tariff_uses_subscription_panel_id(monkeypatch):
    _set_multi(monkeypatch, True)
    assert _get_panel_user_id(_Obj(SUB_B_PANEL_ID), _Obj(USER_PANEL_ID)) == SUB_B_PANEL_ID


def test_bot_multi_tariff_null_sub_does_not_fall_back_to_user(monkeypatch):
    """The bleed: a null sub id must NOT borrow the user's (another tariff's) panel user."""
    _set_multi(monkeypatch, True)
    assert _get_panel_user_id(_Obj(None), _Obj(USER_PANEL_ID)) is None


def test_bot_single_tariff_falls_back_to_user(monkeypatch):
    _set_multi(monkeypatch, False)
    assert _get_panel_user_id(_Obj(None), _Obj(USER_PANEL_ID)) == USER_PANEL_ID


def test_bot_no_subscription_uses_user_panel_id(monkeypatch):
    _set_multi(monkeypatch, True)
    assert _get_panel_user_id(None, _Obj(USER_PANEL_ID)) == USER_PANEL_ID


def test_bot_never_returns_legacy_uuid(monkeypatch):
    """Пустой remnawave_id не должен подмениться легаси-uuid'ом ни в одном режиме."""
    for multi in (True, False):
        _set_multi(monkeypatch, multi)
        resolved = _get_panel_user_id(_Obj(None), _Obj(None))
        assert resolved is None, f'multi={multi}: резолвер вернул {resolved!r} вместо None'


# ---- cabinet route: _resolve_panel_user_id ----


def test_cabinet_multi_tariff_uses_subscription_panel_id(monkeypatch):
    _set_multi(monkeypatch, True)
    assert _resolve_panel_user_id(_Obj(SUB_B_PANEL_ID), _Obj(USER_PANEL_ID)) == SUB_B_PANEL_ID


def test_cabinet_multi_tariff_null_sub_no_user_fallback(monkeypatch):
    _set_multi(monkeypatch, True)
    assert _resolve_panel_user_id(_Obj(None), _Obj(USER_PANEL_ID)) is None


def test_cabinet_single_tariff_uses_user_panel_id(monkeypatch):
    _set_multi(monkeypatch, False)
    assert _resolve_panel_user_id(_Obj(None), _Obj(USER_PANEL_ID)) == USER_PANEL_ID


def test_cabinet_no_subscription_uses_user_panel_id(monkeypatch):
    _set_multi(monkeypatch, True)
    assert _resolve_panel_user_id(None, _Obj(USER_PANEL_ID)) == USER_PANEL_ID


def test_cabinet_never_returns_legacy_uuid(monkeypatch):
    for multi in (True, False):
        _set_multi(monkeypatch, multi)
        resolved = _resolve_panel_user_id(_Obj(None), _Obj(None))
        assert resolved is None, f'multi={multi}: резолвер вернул {resolved!r} вместо None'


def test_both_resolvers_agree_and_return_numeric_ids(monkeypatch):
    """В multi-tariff кабинет и бот обязаны резолвить одинаково и отдавать именно число.

    Расхождение здесь = кабинет и бот показывают разные списки устройств для одной
    и той же подписки. Тип проверяем явно: клиент 3.0.0 ждёт int, строковый UUID он
    отвергает ещё до сети (RemnaWaveInvalidUserIdError).
    """
    _set_multi(monkeypatch, True)
    sub, user = _Obj(SUB_B_PANEL_ID), _Obj(USER_PANEL_ID)

    bot_value = _get_panel_user_id(sub, user)
    cabinet_value = _resolve_panel_user_id(sub, user)

    assert bot_value == cabinet_value == SUB_B_PANEL_ID
    assert isinstance(bot_value, int) and isinstance(cabinet_value, int)


# ---- deterministic per-subscription panel username suffix (collision guard) ----


def test_deterministic_suffix_distinct_per_subscription_when_short_id_empty():
    """Two tariffs of one user with empty/legacy short_id must still build DISTINCT
    panel usernames (else they collapse onto one panel user = shared HWID limit)."""

    def suffix(remnawave_short_id, sub_id):
        # mirrors subscription_service._create_or_update_remnawave_user_multi
        return f'_{remnawave_short_id or f"sub{sub_id}"}'

    assert suffix('', 101) != suffix('', 202)
    assert suffix('', 101) == '_sub101'
    # a real short_id is preferred over the id fallback
    assert suffix('a1b2c3', 101) == '_a1b2c3'
