"""428 «нужно согласие» не должен гасить одноразовый Telegram-credential.

Виджет и OIDC защищены от повтора: payload виджета и id_token помечаются
использованными при первом же запросе. Но гейт согласия с офертой отвечает 428
и просит кабинет повторить ТОТ ЖЕ запрос с галочками. Если credential погашен
до проверки согласия, повтор получает 401 «уже использован», и новый
пользователь не может войти через Telegram из веба вообще — только через бота.

Порядок обязан быть: подпись → кто это → согласие (428 без побочных эффектов)
→ погасить credential → создать аккаунт.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, status

from app.cabinet.routes.auth import auth_telegram_oidc, auth_telegram_widget
from app.cabinet.schemas.auth import TelegramOIDCAuthRequest, TelegramWidgetAuthRequest
from app.services.legal_consent_service import PRIVACY_POLICY, PUBLIC_OFFER, LegalConsentRequirement


H = 'a' * 64
DOCUMENTS = [PUBLIC_OFFER, PRIVACY_POLICY]


def _active_user() -> SimpleNamespace:
    return SimpleNamespace(id=1, telegram_id=123, username='a', first_name='A', last_name=None, status='active')


def _common_patches(s: ExitStack, *, replay: AsyncMock, user: SimpleNamespace | None, create_user: AsyncMock) -> None:
    auth = 'app.cabinet.routes.auth'
    s.enter_context(patch(f'{auth}.get_client_ip', MagicMock(return_value='1.2.3.4')))
    s.enter_context(patch(f'{auth}.RateLimitCache.is_ip_rate_limited', AsyncMock(return_value=False)))
    s.enter_context(patch(f'{auth}.TokenReplayCache.is_token_replayed', replay))
    s.enter_context(patch(f'{auth}.get_user_by_telegram_id', AsyncMock(return_value=user)))
    s.enter_context(patch(f'{auth}._gate_cabinet_identity', AsyncMock(return_value=None)))
    s.enter_context(patch(f'{auth}.create_user', create_user))
    s.enter_context(patch(f'{auth}._recover_cabinet_user_after_gate', AsyncMock()))
    s.enter_context(
        patch(
            f'{auth}._create_auth_response',
            AsyncMock(return_value=SimpleNamespace(refresh_token='r', campaign_bonus=None, user=None)),
        )
    )
    s.enter_context(patch(f'{auth}._store_refresh_token', AsyncMock()))
    s.enter_context(patch(f'{auth}._process_referral_code', AsyncMock()))
    s.enter_context(patch(f'{auth}._process_campaign_bonus', AsyncMock(return_value=None)))
    s.enter_context(
        patch(
            'app.services.legal_consent_service.get_requirement',
            AsyncMock(return_value=LegalConsentRequirement(required=True, prechecked=False, documents=DOCUMENTS)),
        )
    )
    s.enter_context(patch('app.services.legal_consent_service.record_consent', AsyncMock()))


def _widget_patches(s: ExitStack, **kwargs) -> None:
    _common_patches(s, **kwargs)
    s.enter_context(patch('app.cabinet.routes.auth.validate_telegram_login_widget', MagicMock(return_value=True)))


def _oidc_patches(s: ExitStack, **kwargs) -> None:
    _common_patches(s, **kwargs)
    settings_values = {'TELEGRAM_OIDC_ENABLED': 'true', 'TELEGRAM_OIDC_CLIENT_ID': '42'}

    async def get_setting(_db, key):
        return settings_values.get(key)

    claims = {'id': 123, 'name': 'A', 'exp': datetime.now(UTC).timestamp() + 600}
    s.enter_context(patch('app.cabinet.routes.auth.get_setting_value', AsyncMock(side_effect=get_setting)))
    s.enter_context(patch('app.cabinet.routes.auth.validate_telegram_oidc_token', AsyncMock(return_value=claims)))


def _widget_request(accepted: list[str] | None) -> TelegramWidgetAuthRequest:
    return TelegramWidgetAuthRequest(
        id=123, first_name='A', auth_date=1700000000, hash=H, accepted_legal_documents=accepted
    )


def _oidc_request(accepted: list[str] | None) -> TelegramOIDCAuthRequest:
    return TelegramOIDCAuthRequest(id_token='tok', accepted_legal_documents=accepted)


@pytest.mark.asyncio
async def test_widget_428_leaves_the_payload_unconsumed() -> None:
    replay = AsyncMock(return_value=False)
    create = AsyncMock()
    with ExitStack() as s:
        _widget_patches(s, replay=replay, user=None, create_user=create)
        with pytest.raises(HTTPException) as exc:
            await auth_telegram_widget(request=_widget_request(None), raw_request=MagicMock(), db=AsyncMock())

    assert exc.value.status_code == status.HTTP_428_PRECONDITION_REQUIRED
    detail = cast('dict[str, object]', exc.value.detail)
    assert detail['missing'] == DOCUMENTS
    replay.assert_not_awaited()
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_widget_retry_with_consent_consumes_payload_once_and_creates_account() -> None:
    replay = AsyncMock(return_value=False)
    create = AsyncMock(return_value=_active_user())
    with ExitStack() as s:
        _widget_patches(s, replay=replay, user=None, create_user=create)
        await auth_telegram_widget(request=_widget_request(DOCUMENTS), raw_request=MagicMock(), db=AsyncMock())

    replay.assert_awaited_once()
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_widget_existing_user_is_still_one_time() -> None:
    replay = AsyncMock(return_value=True)
    create = AsyncMock()
    with ExitStack() as s:
        _widget_patches(s, replay=replay, user=_active_user(), create_user=create)
        with pytest.raises(HTTPException) as exc:
            await auth_telegram_widget(request=_widget_request(None), raw_request=MagicMock(), db=AsyncMock())

    assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED
    replay.assert_awaited_once()
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_oidc_428_leaves_the_token_unconsumed() -> None:
    replay = AsyncMock(return_value=False)
    create = AsyncMock()
    with ExitStack() as s:
        _oidc_patches(s, replay=replay, user=None, create_user=create)
        with pytest.raises(HTTPException) as exc:
            await auth_telegram_oidc(request=_oidc_request(None), raw_request=MagicMock(), db=AsyncMock())

    assert exc.value.status_code == status.HTTP_428_PRECONDITION_REQUIRED
    detail = cast('dict[str, object]', exc.value.detail)
    assert detail['missing'] == DOCUMENTS
    replay.assert_not_awaited()
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_oidc_retry_with_consent_consumes_token_once_and_creates_account() -> None:
    replay = AsyncMock(return_value=False)
    create = AsyncMock(return_value=_active_user())
    with ExitStack() as s:
        _oidc_patches(s, replay=replay, user=None, create_user=create)
        await auth_telegram_oidc(request=_oidc_request(DOCUMENTS), raw_request=MagicMock(), db=AsyncMock())

    replay.assert_awaited_once()
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_oidc_existing_user_is_still_one_time() -> None:
    replay = AsyncMock(return_value=True)
    create = AsyncMock()
    with ExitStack() as s:
        _oidc_patches(s, replay=replay, user=_active_user(), create_user=create)
        with pytest.raises(HTTPException) as exc:
            await auth_telegram_oidc(request=_oidc_request(None), raw_request=MagicMock(), db=AsyncMock())

    assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED
    replay.assert_awaited_once()
    create.assert_not_awaited()
