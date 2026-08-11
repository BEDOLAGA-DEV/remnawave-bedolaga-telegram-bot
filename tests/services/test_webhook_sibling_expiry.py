"""Regression: a RemnaWave user.deleted webhook (fired when one subscription's
panel user is deleted) must NOT expire the user's OTHER subscriptions unless the
panel POSITIVELY confirms their panel user is gone.

Bug: a user had a pre-MultiTariff subscription (its panel identity stored on
user.remnawave_id), then bought + deleted a second one. Deleting the second
sub's panel user fired a user.deleted webhook whose sibling-expiry sweep
force-expired the ORIGINAL (still active, end_date far in the future) and wiped
its connected_squads — because the liveness check skipped the original
(other_sub panel identity was empty in multi-tariff) and fell open to "expire".

RemnaWave 3.0.0: панельный пользователь опознаётся числовым `id`, поля `uuid`
больше нет — ни в payload вебхука (`data['id']`), ни в клиенте
(`api.get_user_by_id`), ни в колонках бота (`remnawave_id`).

Ужесточённое правило, которое здесь закреплено: «удалён» = ТОЛЬКО явный 404,
который клиент отдаёт как `None`. Любой другой исход — непригодный локальный
идентификатор (`RemnaWaveInvalidUserIdError`), 400 VALIDATION от панели, 5xx,
сетевой сбой — означает «не проверяемо» и НЕ ДОЛЖЕН гасить живую подписку.
Маршруты 3.0.0 параметризованы `z.coerce.number().positive()`, поэтому промах
идентификатора даёт 400, а не 404: без этого различия один битый id молча
отключил бы платящего пользователя.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.services.remnawave_webhook_service as rw
from app.database.models import SubscriptionStatus
from app.external.remnawave_api import (
    RemnaWaveAPIError,
    RemnaWaveInvalidUserIdError,
    coerce_panel_user_id,
)


def _sub(sub_id: int, status: str, *, end_days: int, remnawave_id, squads: list[str]):
    return SimpleNamespace(
        id=sub_id,
        status=status,
        end_date=datetime.now(UTC) + timedelta(days=end_days),
        remnawave_id=remnawave_id,
        remnawave_short_uuid='short',
        connected_squads=list(squads),
        subscription_url='https://x',
        subscription_crypto_link='crypto',
        updated_at=datetime.now(UTC),
        is_trial=False,
    )


def _service():
    # These guards are class-level dicts shared across instances — reset per test.
    rw.RemnaWaveWebhookService._recent_recreations.clear()
    rw.RemnaWaveWebhookService._intentional_panel_deletions_by_id.clear()
    rw.RemnaWaveWebhookService._intentional_panel_deletions_by_telegram_id.clear()
    svc = rw.RemnaWaveWebhookService(MagicMock())
    svc._notify_user = AsyncMock()
    svc._get_renew_keyboard = MagicMock(return_value=None)
    svc._stamp_webhook_update = MagicMock()
    return svc


def _panel_service(panel_user=None, error: Exception | None = None):
    api = MagicMock()

    async def _get_user_by_id(user_id):
        # Клиент 3.0.0 отсекает непригодный идентификатор на своей границе,
        # ДО сети — воспроизводим это, чтобы тест ловил реальное поведение,
        # а не удобную заглушку.
        coerce_panel_user_id(user_id)
        if error is not None:
            raise error
        return panel_user

    api.get_user_by_id = AsyncMock(side_effect=_get_user_by_id)

    @asynccontextmanager
    async def _client():
        yield api

    inst = MagicMock()
    inst.is_configured = True
    inst.get_api_client = _client
    return patch('app.services.subscription_service.SubscriptionService', return_value=inst), api


@asynccontextmanager
async def _drive(svc, user, deleted, panel_user=None, error=None):
    sub_patch, api = _panel_service(panel_user=panel_user, error=error)
    with (
        patch.object(type(rw.settings), 'is_multi_tariff_enabled', MagicMock(return_value=True)),
        sub_patch,
        patch.object(rw, 'decrement_subscription_server_counts', AsyncMock()),
    ):
        # 3.0.0: конверт вебхука несёт числовой `id`, поля `uuid` больше нет.
        await svc._handle_user_deleted(AsyncMock(), user, deleted, {'id': deleted.remnawave_id})
        yield api


@pytest.mark.asyncio
async def test_pre_multitariff_sibling_with_future_end_date_not_expired():
    deleted = _sub(1, SubscriptionStatus.EXPIRED.value, end_days=-10, remnawave_id=101, squads=[])
    sibling = _sub(2, SubscriptionStatus.ACTIVE.value, end_days=900, remnawave_id=None, squads=['sq1', 'sq2'])
    user = SimpleNamespace(id=7, telegram_id=123, language='ru', remnawave_id=555, subscriptions=[deleted, sibling])

    svc = _service()
    async with _drive(svc, user, deleted, panel_user=None) as api:
        pass

    assert sibling.status == SubscriptionStatus.ACTIVE.value  # the reported bug — must stay active
    assert sibling.connected_squads == ['sq1', 'sq2']
    api.get_user_by_id.assert_not_awaited()  # future end_date short-circuits before any panel call


@pytest.mark.asyncio
async def test_sibling_alive_in_panel_via_user_panel_id_fallback_not_expired():
    deleted = _sub(1, SubscriptionStatus.EXPIRED.value, end_days=-10, remnawave_id=101, squads=[])
    sibling = _sub(2, SubscriptionStatus.ACTIVE.value, end_days=-1, remnawave_id=None, squads=['sq1'])
    user = SimpleNamespace(id=7, telegram_id=123, language='ru', remnawave_id=555, subscriptions=[deleted, sibling])

    svc = _service()
    async with _drive(svc, user, deleted, panel_user=MagicMock()) as api:
        pass

    assert sibling.status == SubscriptionStatus.ACTIVE.value
    assert sibling.connected_squads == ['sq1']
    api.get_user_by_id.assert_awaited_with(555)  # fell back to user.remnawave_id


@pytest.mark.asyncio
async def test_sibling_not_expired_on_transient_api_error():
    deleted = _sub(1, SubscriptionStatus.EXPIRED.value, end_days=-10, remnawave_id=101, squads=[])
    sibling = _sub(2, SubscriptionStatus.ACTIVE.value, end_days=-1, remnawave_id=202, squads=['sq1'])
    user = SimpleNamespace(id=7, telegram_id=123, language='ru', remnawave_id=None, subscriptions=[deleted, sibling])

    svc = _service()
    async with _drive(svc, user, deleted, error=RuntimeError('panel down')) as api:
        pass

    api.get_user_by_id.assert_awaited_with(202)
    assert sibling.status == SubscriptionStatus.ACTIVE.value  # fail-safe: unknown != gone
    assert sibling.connected_squads == ['sq1']


@pytest.mark.asyncio
async def test_sibling_not_expired_on_panel_validation_400():
    """400 VALIDATION — не «пользователя нет».

    В 3.0.0 user-маршруты параметризованы `z.coerce.number().positive()`, и
    промах идентификатора возвращает 400, а не 404. Если бы sweep считал любой
    RemnaWaveAPIError доказательством удаления, одна валидационная ошибка тихо
    гасила бы живую оплаченную подписку и стирала её сквады.
    """
    deleted = _sub(1, SubscriptionStatus.EXPIRED.value, end_days=-10, remnawave_id=101, squads=[])
    sibling = _sub(2, SubscriptionStatus.ACTIVE.value, end_days=-1, remnawave_id=202, squads=['sq1', 'sq2'])
    user = SimpleNamespace(id=7, telegram_id=123, language='ru', remnawave_id=None, subscriptions=[deleted, sibling])

    svc = _service()
    async with _drive(
        svc,
        user,
        deleted,
        error=RemnaWaveAPIError('Validation failed', status_code=400, response_data={'errorCode': 'A019'}),
    ) as api:
        pass

    api.get_user_by_id.assert_awaited_with(202)
    assert sibling.status == SubscriptionStatus.ACTIVE.value
    assert sibling.connected_squads == ['sq1', 'sq2']
    assert sibling.remnawave_id == 202  # панельная идентичность не должна обнуляться
    assert sibling.subscription_url == 'https://x'


@pytest.mark.asyncio
async def test_sibling_not_expired_when_local_panel_id_is_unusable():
    """Битый локальный идентификатор — баг данных бота, а не «юзера нет».

    Протухший UUID из до-3.0.0 колонки (sqlite не типизирует колонки, поэтому
    такое значение реально доживает до рантайма) отсекается на границе клиента
    как RemnaWaveInvalidUserIdError. Запрос до панели не доходит вообще, значит
    подтверждения удаления нет — подписку трогать нельзя.
    """
    deleted = _sub(1, SubscriptionStatus.EXPIRED.value, end_days=-10, remnawave_id=101, squads=[])
    sibling = _sub(
        2,
        SubscriptionStatus.ACTIVE.value,
        end_days=-1,
        remnawave_id='0f2a5f6c-1f4e-4f0c-9b3a-6f2d1c8e7a10',
        squads=['sq1'],
    )
    user = SimpleNamespace(id=7, telegram_id=123, language='ru', remnawave_id=None, subscriptions=[deleted, sibling])

    svc = _service()
    async with _drive(svc, user, deleted, panel_user=None) as api:
        pass

    # Sweep дошёл до проверки живости и передал битый id в клиент...
    api.get_user_by_id.assert_awaited_with('0f2a5f6c-1f4e-4f0c-9b3a-6f2d1c8e7a10')
    # ...а клиент поднял именно RemnaWaveInvalidUserIdError, а не вернул None
    # (panel_user=None в этом тесте — ловушка: если бы id проскочил, подписка
    # была бы погашена).
    with pytest.raises(RemnaWaveInvalidUserIdError):
        coerce_panel_user_id('0f2a5f6c-1f4e-4f0c-9b3a-6f2d1c8e7a10')

    assert sibling.status == SubscriptionStatus.ACTIVE.value
    assert sibling.connected_squads == ['sq1']
    assert sibling.remnawave_id == '0f2a5f6c-1f4e-4f0c-9b3a-6f2d1c8e7a10'


@pytest.mark.asyncio
async def test_sibling_genuinely_gone_is_still_expired():
    """Don't break legitimate expiry: panel says gone (None == 404) + past end_date -> expire."""
    deleted = _sub(1, SubscriptionStatus.EXPIRED.value, end_days=-10, remnawave_id=101, squads=[])
    sibling = _sub(2, SubscriptionStatus.ACTIVE.value, end_days=-1, remnawave_id=202, squads=['sq1'])
    user = SimpleNamespace(id=7, telegram_id=123, language='ru', remnawave_id=None, subscriptions=[deleted, sibling])

    svc = _service()
    async with _drive(svc, user, deleted, panel_user=None) as api:
        pass

    api.get_user_by_id.assert_awaited_with(202)
    assert sibling.status == SubscriptionStatus.EXPIRED.value
    assert sibling.connected_squads == []
    assert sibling.remnawave_id is None  # multi-tariff чистит протухшую идентичность


@pytest.mark.asyncio
async def test_intentional_panel_deletion_suppresses_sibling_sweep():
    svc = _service()  # resets class-level guards first
    rw.RemnaWaveWebhookService.mark_intentional_panel_deletion(panel_user_ids=[101])
    try:
        deleted = _sub(1, SubscriptionStatus.EXPIRED.value, end_days=-10, remnawave_id=101, squads=[])
        sibling = _sub(2, SubscriptionStatus.ACTIVE.value, end_days=-1, remnawave_id=202, squads=['sq1'])
        user = SimpleNamespace(
            id=7, telegram_id=123, language='ru', remnawave_id=None, subscriptions=[deleted, sibling]
        )

        await svc._handle_user_deleted(AsyncMock(), user, deleted, {'id': 101})

        assert sibling.status == SubscriptionStatus.ACTIVE.value  # handler returned early, nothing swept
        svc._notify_user.assert_not_awaited()
    finally:
        rw.RemnaWaveWebhookService._intentional_panel_deletions_by_id.clear()


@asynccontextmanager
async def _drive_single(svc, user, deleted, *, by_id=None, by_short_uuid=None):
    """Однотарифный прогон: панель отвечает и по id, и по shortUuid."""
    api = MagicMock()

    async def _get_user_by_id(user_id):
        coerce_panel_user_id(user_id)
        return by_id

    api.get_user_by_id = AsyncMock(side_effect=_get_user_by_id)
    api.get_user_by_short_uuid = AsyncMock(return_value=by_short_uuid)

    @asynccontextmanager
    async def _client():
        yield api

    inst = MagicMock()
    inst.is_configured = True
    inst.get_api_client = _client

    with (
        patch.object(type(rw.settings), 'is_multi_tariff_enabled', MagicMock(return_value=False)),
        patch('app.services.subscription_service.SubscriptionService', return_value=inst),
        patch.object(rw, 'decrement_subscription_server_counts', AsyncMock()),
    ):
        await svc._handle_user_deleted(AsyncMock(), user, deleted, {'id': deleted.remnawave_id})
        yield api


@pytest.mark.asyncio
async def test_single_tariff_sibling_with_its_own_live_account_is_not_expired():
    """Однотарифный режим не покрывался ни одним тестом.

    Сосед без своего числового id проверялся по id УДАЛЁННОГО аккаунта — тот по
    определению отвечает «нет», и проверка превращалась в штамп: сосед истекал,
    хотя его собственный shortUuid панель прекрасно знает.
    """
    deleted = _sub(1, SubscriptionStatus.EXPIRED.value, end_days=-10, remnawave_id=101, squads=[])
    sibling = _sub(2, SubscriptionStatus.ACTIVE.value, end_days=-1, remnawave_id=None, squads=['sq1'])
    user = SimpleNamespace(
        id=7, telegram_id=100, remnawave_id=101, remnawave_uuid='legacy', subscriptions=[deleted, sibling]
    )
    svc = _service()

    async with _drive_single(svc, user, deleted, by_id=None, by_short_uuid=SimpleNamespace(id=777)):
        pass

    assert sibling.status == SubscriptionStatus.ACTIVE.value, 'у соседа живой аккаунт — истекать нельзя'
    assert sibling.connected_squads == ['sq1']


@pytest.mark.asyncio
async def test_single_tariff_sibling_without_any_live_account_is_expired():
    """А если и его собственный ключ панель не знает — истекаем, это и есть смысл цикла."""
    deleted = _sub(1, SubscriptionStatus.EXPIRED.value, end_days=-10, remnawave_id=101, squads=[])
    sibling = _sub(2, SubscriptionStatus.ACTIVE.value, end_days=-1, remnawave_id=None, squads=['sq1'])
    user = SimpleNamespace(
        id=7, telegram_id=100, remnawave_id=101, remnawave_uuid='legacy', subscriptions=[deleted, sibling]
    )
    svc = _service()

    async with _drive_single(svc, user, deleted, by_id=None, by_short_uuid=None):
        pass

    assert sibling.status == SubscriptionStatus.EXPIRED.value
