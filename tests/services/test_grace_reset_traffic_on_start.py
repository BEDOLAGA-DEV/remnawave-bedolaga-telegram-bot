"""Grace может обнулять счётчик трафика при выдаче (issue #3275).

Лимит grace по умолчанию — «текущий расход + квота», и панель вместе с
клиентом показывают «64.76 GiB из 65.76 GiB — 98%», хотя доступен ровно
гигабайт. GRACE_ACCESS_RESET_TRAFFIC_ON_START обнуляет счётчик, лимит
становится равен самой квоте. Только истёкшая подписка с безлимитом: там
счётчик информационный; LIMITED обнулять нельзя — вернулась бы квота тарифа.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.external.remnawave_api import UserStatus
from app.services.grace_access_codec import _overlay_from_json, _overlay_to_json
from app.services.grace_access_runtime import GracePanelError, RemnawaveGracePanelGateway
from app.services.grace_access_service import (
    GracePanelOverlay,
    GraceReason,
    GraceSessionState,
    GraceStartDecision,
    should_reset_used_traffic,
)
from tests.services.test_grace_access_runtime import (
    GRACE_SQUAD,
    PANEL_ID,
    REGULAR_SQUAD,
    FakeRemnawaveApi,
    install_fake_api,
    make_panel_user,
)
from tests.services.test_grace_access_service import (
    EXPIRED_SQUAD,
    GIB,
    FakePanelGateway,
    MutableClock,
    make_billing,
    make_policy,
    make_service,
    make_snapshot,
)


NOW = datetime(2026, 9, 23, 12, tzinfo=UTC)


class ResettingPanelGateway(FakePanelGateway):
    """Панель, которая умеет обнулять счётчик, и может на этом сломаться."""

    def __init__(self, snapshot) -> None:
        super().__init__(snapshot)
        self.fail_reset_attempts = 0

    async def apply_overlay(self, remnawave_id, overlay) -> None:
        await super().apply_overlay(remnawave_id, overlay)
        if not overlay.reset_used_traffic:
            return
        if self.fail_reset_attempts > 0:
            self.fail_reset_attempts -= 1
            raise RuntimeError('reset-traffic failed')
        self.snapshot = replace(self.snapshot, used_traffic_bytes=0)


def _unlimited_expired():
    billing = make_billing(
        status='expired',
        end_at=NOW - timedelta(days=1),
        traffic_limit_bytes=0,
        used_traffic_bytes=int(64.76 * GIB),
    )
    snapshot = replace(
        make_snapshot(expire_at=billing.end_at, traffic_limit_bytes=0, used_traffic_bytes=int(64.76 * GIB)),
        status='EXPIRED',
    )
    return billing, snapshot


def _service(billing, snapshot, policy):
    service, store, _, billing_gateway = make_service(
        billing=billing, snapshot=snapshot, clock=MutableClock(NOW), policy=policy
    )
    panel = ResettingPanelGateway(snapshot)
    service._panel = panel
    return service, store, panel


@pytest.mark.asyncio
async def test_expired_unlimited_grace_resets_counter_and_limit_equals_quota() -> None:
    billing, snapshot = _unlimited_expired()
    service, store, panel = _service(billing, snapshot, make_policy(reset_traffic_on_start=True))

    result = await service.start_if_eligible(billing, GraceReason.EXPIRED)

    assert result.decision is GraceStartDecision.STARTED
    overlay = store.only_session().overlay
    assert overlay.reset_used_traffic is True
    assert overlay.traffic_limit_bytes == GIB
    assert overlay.squad_uuids == (EXPIRED_SQUAD,)
    # «0 из 1 ГБ», а не «64.76 из 65.76».
    assert panel.snapshot.used_traffic_bytes == 0
    assert panel.snapshot.traffic_limit_bytes == GIB


@pytest.mark.asyncio
async def test_setting_off_keeps_usage_plus_quota() -> None:
    billing, snapshot = _unlimited_expired()
    service, store, panel = _service(billing, snapshot, make_policy())

    await service.start_if_eligible(billing, GraceReason.EXPIRED)

    overlay = store.only_session().overlay
    assert overlay.reset_used_traffic is False
    assert overlay.traffic_limit_bytes == snapshot.used_traffic_bytes + GIB
    assert panel.snapshot.used_traffic_bytes == snapshot.used_traffic_bytes


def test_reset_is_limited_to_expired_unlimited_subscriptions() -> None:
    policy = make_policy(reset_traffic_on_start=True)
    billing, snapshot = _unlimited_expired()

    assert should_reset_used_traffic(billing, snapshot, GraceReason.EXPIRED, policy) is True
    assert should_reset_used_traffic(billing, snapshot, GraceReason.EXPIRED, make_policy()) is False
    # Упёршаяся в лимит: обнуление вернуло бы исчерпанную квоту тарифа.
    limited = replace(billing, status='limited', traffic_limit_bytes=50 * GIB)
    limited_snapshot = replace(snapshot, status='LIMITED', traffic_limit_bytes=50 * GIB)
    assert should_reset_used_traffic(limited, limited_snapshot, GraceReason.LIMITED, policy) is False
    # Истёкшая, но с конечным лимитом: расход не информационный.
    finite = replace(billing, traffic_limit_bytes=100 * GIB)
    assert should_reset_used_traffic(finite, snapshot, GraceReason.EXPIRED, policy) is False
    finite_panel = replace(snapshot, traffic_limit_bytes=100 * GIB)
    assert should_reset_used_traffic(billing, finite_panel, GraceReason.EXPIRED, policy) is False


@pytest.mark.asyncio
async def test_limited_grace_never_resets_even_with_setting_on() -> None:
    billing = make_billing(
        status='limited',
        end_at=NOW + timedelta(days=20),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
    )
    snapshot = replace(
        make_snapshot(expire_at=billing.end_at, traffic_limit_bytes=10 * GIB, used_traffic_bytes=10 * GIB),
        status='LIMITED',
    )
    service, store, panel = _service(billing, snapshot, make_policy(reset_traffic_on_start=True))

    await service.start_if_eligible(billing, GraceReason.LIMITED)

    overlay = store.only_session().overlay
    assert overlay.reset_used_traffic is False
    assert overlay.traffic_limit_bytes == 11 * GIB
    assert panel.snapshot.used_traffic_bytes == 10 * GIB


@pytest.mark.asyncio
async def test_failed_reset_after_overlay_is_retried_instead_of_leaving_zero_traffic() -> None:
    billing, snapshot = _unlimited_expired()
    service, store, panel = _service(billing, snapshot, make_policy(reset_traffic_on_start=True))
    panel.fail_reset_attempts = 1

    with pytest.raises(RuntimeError, match='reset-traffic'):
        await service.start_if_eligible(billing, GraceReason.EXPIRED)

    # Оверлей в панели уже стоит (лимит 1 ГБ), а расход старый — доступного трафика ноль.
    assert store.only_session().state is GraceSessionState.PENDING
    assert panel.snapshot.traffic_limit_bytes == GIB
    assert panel.snapshot.used_traffic_bytes > GIB

    result = await service.start_if_eligible(billing, GraceReason.EXPIRED)

    assert result.decision is GraceStartDecision.RETRIED
    assert store.only_session().state is GraceSessionState.ACTIVE
    assert panel.snapshot.used_traffic_bytes == 0
    assert len(panel.applied_overlays) == 2


def _reset_overlay() -> GracePanelOverlay:
    return GracePanelOverlay(
        status='ACTIVE',
        expire_at=NOW + timedelta(days=3),
        traffic_limit_bytes=GIB,
        squad_uuids=(GRACE_SQUAD,),
        external_squad_uuid=None,
        reset_used_traffic=True,
    )


def _fake_api_with_reset(*, resets_to: int) -> FakeRemnawaveApi:
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.EXPIRED,
            expire_at=datetime.now(UTC) - timedelta(days=1),
            traffic_limit_bytes=0,
            squad_uuids=(REGULAR_SQUAD,),
        )
    )
    calls: list[str] = []
    original_update = api.update_user

    async def update_user(**kwargs):
        calls.append('update')
        return await original_update(**kwargs)

    async def reset_user_traffic(user_id):
        calls.append('reset')
        assert user_id == PANEL_ID
        api.user.used_traffic_bytes = resets_to
        return api.user

    api.update_user = update_user
    api.reset_user_traffic = reset_user_traffic
    api.calls = calls
    return api


@pytest.mark.asyncio
async def test_gateway_resets_only_after_the_restricting_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    overlay = replace(_reset_overlay(), expire_at=datetime.now(UTC) + timedelta(days=3))
    api = _fake_api_with_reset(resets_to=0)
    install_fake_api(monkeypatch, api)

    await RemnawaveGracePanelGateway().apply_overlay(PANEL_ID, overlay)

    # Сброс до PATCH снял бы у LIMITED-пользователя статус при старых сквадах.
    assert api.calls == ['update', 'update', 'reset']
    assert api.user.used_traffic_bytes == 0


@pytest.mark.asyncio
async def test_gateway_fails_when_panel_did_not_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    overlay = replace(_reset_overlay(), expire_at=datetime.now(UTC) + timedelta(days=3))
    api = _fake_api_with_reset(resets_to=10 * GIB)
    install_fake_api(monkeypatch, api)

    with pytest.raises(GracePanelError, match='reset used traffic'):
        await RemnawaveGracePanelGateway().apply_overlay(PANEL_ID, overlay)


@pytest.mark.asyncio
async def test_gateway_without_flag_never_resets(monkeypatch: pytest.MonkeyPatch) -> None:
    overlay = replace(
        _reset_overlay(),
        expire_at=datetime.now(UTC) + timedelta(days=3),
        reset_used_traffic=False,
        traffic_limit_bytes=11 * GIB,
    )
    api = _fake_api_with_reset(resets_to=0)
    install_fake_api(monkeypatch, api)

    await RemnawaveGracePanelGateway().apply_overlay(PANEL_ID, overlay)

    assert 'reset' not in api.calls


def test_codec_round_trips_flag_and_reads_old_sessions_as_not_reset() -> None:
    overlay = _reset_overlay()
    assert _overlay_from_json(_overlay_to_json(overlay)) == overlay

    legacy = _overlay_to_json(overlay)
    del legacy['reset_used_traffic']
    assert _overlay_from_json(legacy).reset_used_traffic is False


@pytest.mark.asyncio
async def test_traffic_reset_webhook_does_not_notify_during_expired_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import remnawave_webhook_service as module

    service = module.RemnaWaveWebhookService.__new__(module.RemnaWaveWebhookService)
    service._stamp_webhook_update = lambda subscription: None
    service._notify_user = AsyncMock()
    service._get_subscription_keyboard = lambda user: None
    monkeypatch.setattr(module, 'update_subscription_usage', AsyncMock())
    monkeypatch.setattr(module, 'reactivate_subscription', AsyncMock())
    open_ids = AsyncMock(return_value={7})
    monkeypatch.setattr(module, 'get_open_grace_subscription_ids', open_ids)

    user = SimpleNamespace(id=1)
    expired = SimpleNamespace(id=7, status='expired')
    await service._handle_user_traffic_reset(AsyncMock(), user, expired, {})
    service._notify_user.assert_not_awaited()

    # Без открытого grace, и у активной подписки — уведомление как раньше.
    open_ids.return_value = set()
    await service._handle_user_traffic_reset(AsyncMock(), user, expired, {})
    active = SimpleNamespace(id=8, status='active')
    await service._handle_user_traffic_reset(AsyncMock(), user, active, {})
    assert service._notify_user.await_count == 2
