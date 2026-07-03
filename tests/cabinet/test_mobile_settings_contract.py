from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException, status

from app.cabinet.routes import admin_mobile
from app.cabinet.routes.admin_settings import SettingUpdateRequest


class _FakeConfigService:
    SECRET_MASK = '********'

    def __init__(self, env_locked: bool = False) -> None:
        self.env_locked = env_locked

    def get_definition(self, key: str):
        if key not in {'WEB_API_ALLOWED_ORIGINS', 'CABINET_ALLOWED_ORIGINS'}:
            raise KeyError(key)
        return SimpleNamespace(key=key)

    def is_env_locked(self, key: str) -> bool:
        return self.env_locked and key == 'WEB_API_ALLOWED_ORIGINS'

    def is_secret_key(self, _key: str) -> bool:
        return False


@pytest.mark.asyncio
async def test_cors_contract_marks_pre_auth_as_operator_guidance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin_mobile, 'bot_configuration_service', _FakeConfigService(env_locked=True))

    response = await admin_mobile.get_mobile_cors_contract()

    assert response.pre_auth_behavior == 'local/operator-guidance-only'
    assert response.server_side_edit.startswith('allowed only after cabinet JWT')
    by_key = {item.key: item for item in response.allowed_keys}
    assert by_key['WEB_API_ALLOWED_ORIGINS'].mode == 'read-only/operator-guidance'
    assert by_key['CABINET_ALLOWED_ORIGINS'].mode == 'editable-after-auth'


@pytest.mark.asyncio
async def test_mobile_cors_update_rejects_keys_outside_contract() -> None:
    with pytest.raises(HTTPException) as exc:
        await admin_mobile.update_mobile_cors_setting(
            key='BOT_TOKEN',
            payload=SettingUpdateRequest(value='secret'),
            admin=SimpleNamespace(telegram_id=1),
            db=object(),
        )

    assert exc.value.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_mobile_cors_update_delegates_env_lock_and_secret_handling(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object]] = []

    async def fake_update_setting(key, payload, admin, db):
        calls.append((key, payload.value))
        return {'key': key, 'current': payload.value, 'env_locked': False, 'is_secret': False}

    monkeypatch.setattr(admin_mobile, 'update_setting', fake_update_setting)

    response = await admin_mobile.update_mobile_cors_setting(
        key='CABINET_ALLOWED_ORIGINS',
        payload=SettingUpdateRequest(value='https://mobile.example.test'),
        admin=SimpleNamespace(telegram_id=1),
        db=object(),
    )

    assert calls == [('CABINET_ALLOWED_ORIGINS', 'https://mobile.example.test')]
    assert response['key'] == 'CABINET_ALLOWED_ORIGINS'
