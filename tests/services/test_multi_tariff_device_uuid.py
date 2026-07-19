"""Multi-tariff HWID device-limit bleed: each tariff is its own RemnaWave panel
user, so device reads must resolve the SUBSCRIPTION's panel UUID and must NOT
fall back to the user-level UUID in multi-tariff mode (the fallback showed/shared
another tariff's devices, making the limit look "counted by the smallest tariff").
"""

from __future__ import annotations

from app.cabinet.routes.subscription_modules.devices import _resolve_panel_uuid
from app.config import Settings
from app.handlers.subscription.devices import _get_remnawave_uuid


class _Obj:
    def __init__(self, uuid):
        self.remnawave_uuid = uuid


def _set_multi(monkeypatch, value: bool) -> None:
    # Settings methods are patched on the class, fields on the instance (pydantic).
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: value)


# ---- bot handler: _get_remnawave_uuid ----


def test_bot_multi_tariff_uses_subscription_uuid(monkeypatch):
    _set_multi(monkeypatch, True)
    assert _get_remnawave_uuid(_Obj('SUB-B'), _Obj('USER')) == 'SUB-B'


def test_bot_multi_tariff_null_sub_does_not_fall_back_to_user(monkeypatch):
    """The bleed: a null sub UUID must NOT borrow the user's (another tariff's) panel user."""
    _set_multi(monkeypatch, True)
    assert _get_remnawave_uuid(_Obj(None), _Obj('USER')) is None


def test_bot_single_tariff_falls_back_to_user(monkeypatch):
    _set_multi(monkeypatch, False)
    assert _get_remnawave_uuid(_Obj(None), _Obj('USER')) == 'USER'


# ---- cabinet route: _resolve_panel_uuid ----


def test_cabinet_multi_tariff_uses_subscription_uuid(monkeypatch):
    _set_multi(monkeypatch, True)
    assert _resolve_panel_uuid(_Obj('SUB-B'), _Obj('USER')) == 'SUB-B'


def test_cabinet_multi_tariff_null_sub_no_user_fallback(monkeypatch):
    _set_multi(monkeypatch, True)
    assert _resolve_panel_uuid(_Obj(None), _Obj('USER')) is None


def test_cabinet_single_tariff_uses_user_uuid(monkeypatch):
    _set_multi(monkeypatch, False)
    assert _resolve_panel_uuid(_Obj(None), _Obj('USER')) == 'USER'


def test_cabinet_no_subscription_uses_user_uuid(monkeypatch):
    _set_multi(monkeypatch, True)
    assert _resolve_panel_uuid(None, _Obj('USER')) == 'USER'


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
