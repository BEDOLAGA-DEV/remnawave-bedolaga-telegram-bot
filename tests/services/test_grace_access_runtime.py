from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from app.database.models import GraceAccessSessionModel
from app.external.remnawave_api import (
    RemnaWaveInvalidUserIdError,
    UserStatus,
    coerce_panel_user_id,
)
from app.services.grace_access_runtime import (
    GraceSnapshotError,
    RemnawaveGracePanelGateway,
    _build_billing_target,
    _model_to_session,
    _panel_matches_target,
    _PanelTarget,
    _serialize_panel_target,
    _session_to_model,
    _session_values,
)
from app.services.grace_access_service import (
    GraceBillingState,
    GracePanelOverlay,
    GracePanelSnapshot,
    GracePanelTransitionConflict,
    GracePanelTransitionPending,
    GraceReason,
    GraceRestoreOutcome,
    GraceSessionState,
)


GIB = 1024**3
# Remnawave 3.0.0 адресует пользователя числовым id; поля uuid у записи нет.
PANEL_ID = 4242
# Историческое значение колонки remnawave_uuid: встречается только в
# доапгрейдных строках и НЕ является идентификатором запроса.
LEGACY_PANEL_UUID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
GRACE_SQUAD = '11111111-1111-1111-1111-111111111111'
REGULAR_SQUAD = '22222222-2222-2222-2222-222222222222'
OTHER_SQUAD = '33333333-3333-3333-3333-333333333333'
EXTERNAL_SQUAD = '44444444-4444-4444-4444-444444444444'
NOW = datetime.now(UTC).replace(microsecond=0)


class FakeRemnawaveApi:
    """Двойник клиента 3.0.0: идентификатор всегда числовой и коерсится."""

    def __init__(self, user: SimpleNamespace) -> None:
        self.user = user
        self.updates: list[dict[str, Any]] = []
        self.reads: list[int] = []

    async def get_user_by_id(self, user_id: int) -> SimpleNamespace | None:
        # Как и настоящий клиент: непригодный локальный идентификатор — это
        # исключение на границе, а не «пользователя нет».
        panel_user_id = coerce_panel_user_id(user_id)
        self.reads.append(panel_user_id)
        return self.user if panel_user_id == self.user.id else None

    async def update_user(self, *, user_id: int, **kwargs: Any) -> SimpleNamespace:
        # Ключевое слово именно user_id: тело PATCH в 3.0.0 — {'id': ...},
        # а поля uuid схема запроса не содержит вовсе.
        panel_user_id = coerce_panel_user_id(user_id)
        self.updates.append({'user_id': panel_user_id, **kwargs})
        if status := kwargs.get('status'):
            self.user.status = status
        if 'expire_at' in kwargs:
            self.user.expire_at = kwargs['expire_at']
        if 'traffic_limit_bytes' in kwargs:
            self.user.traffic_limit_bytes = kwargs['traffic_limit_bytes']
        if 'active_internal_squads' in kwargs:
            self.user.active_internal_squads = [{'uuid': squad_uuid} for squad_uuid in kwargs['active_internal_squads']]
        if 'external_squad_uuid' in kwargs:
            self.user.external_squad_uuid = kwargs['external_squad_uuid']
        if 'hwid_device_limit' in kwargs:
            self.user.hwid_device_limit = kwargs['hwid_device_limit']
        return self.user


def make_panel_user(
    *,
    status: UserStatus,
    expire_at: datetime,
    traffic_limit_bytes: int,
    squad_uuids: tuple[str, ...],
    external_squad_uuid: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=PANEL_ID,
        status=status,
        expire_at=expire_at,
        traffic_limit_bytes=traffic_limit_bytes,
        used_traffic_bytes=10 * GIB,
        active_internal_squads=[{'uuid': value} for value in squad_uuids],
        external_squad_uuid=external_squad_uuid,
        user_traffic=SimpleNamespace(used_traffic_bytes=10 * GIB),
        last_traffic_reset_at=None,
        hwid_device_limit=2,
    )


def make_overlay() -> GracePanelOverlay:
    return GracePanelOverlay(
        status='ACTIVE',
        expire_at=NOW + timedelta(days=3),
        traffic_limit_bytes=11 * GIB,
        squad_uuids=(GRACE_SQUAD,),
        external_squad_uuid=None,
    )


def make_limited_billing() -> GraceBillingState:
    return GraceBillingState(
        subscription_id=42,
        remnawave_id=PANEL_ID,
        status='limited',
        end_at=NOW + timedelta(days=20),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
        device_limit=4,
        squad_uuids=(REGULAR_SQUAD,),
        external_squad_uuid=EXTERNAL_SQUAD,
    )


def make_limited_snapshot() -> GracePanelSnapshot:
    return GracePanelSnapshot(
        remnawave_id=PANEL_ID,
        status='LIMITED',
        expire_at=NOW + timedelta(days=20),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
        squad_uuids=(REGULAR_SQUAD,),
        external_squad_uuid=EXTERNAL_SQUAD,
    )


def make_v2_session_row(*, remnawave_id: int | None = PANEL_ID) -> GraceAccessSessionModel:
    """Строка, записанная до апгрейда панели: snapshot_version=2 и uuid в JSON.

    Числового id блобы не знают — панель 2.8.x его не отдавала, поэтому он есть
    только в бэкфилленной колонке строки.
    """
    started_at = NOW - timedelta(hours=1)
    end_at = NOW - timedelta(days=1)
    return GraceAccessSessionModel(
        id='11111111-2222-3333-4444-555555555555',
        subscription_id=42,
        remnawave_id=remnawave_id,
        remnawave_uuid=LEGACY_PANEL_UUID,
        reason=GraceReason.EXPIRED.value,
        incident_key=f'expired:{end_at.isoformat()}',
        state=GraceSessionState.ACTIVE.value,
        snapshot_version=2,
        billing_before={
            'subscription_id': 42,
            'remnawave_uuid': LEGACY_PANEL_UUID,
            'status': 'expired',
            'end_at': end_at.isoformat(),
            'traffic_limit_bytes': 10 * GIB,
            'used_traffic_bytes': 10 * GIB,
            'device_limit': 4,
            'squad_uuids': [REGULAR_SQUAD],
            'external_squad_uuid': EXTERNAL_SQUAD,
            'is_trial': False,
            'is_daily': False,
            'is_free_tariff': False,
            'user_status': 'active',
            'grace_suppressed_until': None,
        },
        panel_before={
            'remnawave_uuid': LEGACY_PANEL_UUID,
            'status': 'EXPIRED',
            'expire_at': end_at.isoformat(),
            'traffic_limit_bytes': 10 * GIB,
            'used_traffic_bytes': 10 * GIB,
            'squad_uuids': [REGULAR_SQUAD],
            'external_squad_uuid': EXTERNAL_SQUAD,
            'traffic_is_known': True,
            'last_traffic_reset_at': None,
        },
        overlay={
            'status': 'ACTIVE',
            'expire_at': (NOW + timedelta(days=3)).isoformat(),
            'traffic_limit_bytes': 11 * GIB,
            'squad_uuids': [GRACE_SQUAD],
            'external_squad_uuid': None,
        },
        started_at=started_at,
        grace_until=NOW + timedelta(days=3),
        updated_at=started_at,
        completion_reason=None,
        completed_at=None,
        last_error=None,
        version=1,
    )


def install_fake_api(monkeypatch: pytest.MonkeyPatch, api: FakeRemnawaveApi) -> None:
    from app.services.remnawave_service import remnawave_service

    @asynccontextmanager
    async def get_api_client():
        yield api

    monkeypatch.setattr(remnawave_service, 'get_api_client', get_api_client)


def assert_no_derived_status_writes(api: FakeRemnawaveApi) -> None:
    assert all(update.get('status') not in {UserStatus.LIMITED, UserStatus.EXPIRED} for update in api.updates)


@pytest.mark.parametrize('derived_status', [UserStatus.LIMITED, UserStatus.EXPIRED])
def test_panel_target_serializer_removes_derived_statuses(
    derived_status: UserStatus,
) -> None:
    target = _PanelTarget(
        status=derived_status,
        expire_at=NOW + timedelta(days=20),
        traffic_limit_bytes=10 * GIB,
        squad_uuids=(REGULAR_SQUAD,),
        external_squad_uuid=EXTERNAL_SQUAD,
        device_limit=4,
    )

    payload = _serialize_panel_target(
        PANEL_ID,
        target,
        base_kwargs={'status': derived_status, 'description': 'preserved'},
    )

    assert 'status' not in payload
    assert payload['description'] == 'preserved'
    # Панель 3.0.0 адресуется числовым id; ключ uuid схема PATCH срезает молча.
    assert payload['user_id'] == PANEL_ID
    assert 'uuid' not in payload


@pytest.mark.asyncio
async def test_apply_limited_billing_restores_canonical_fields_without_writing_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    billing = make_limited_billing()
    overlay = make_overlay()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.LIMITED,
            expire_at=overlay.expire_at,
            traffic_limit_bytes=overlay.traffic_limit_bytes,
            squad_uuids=overlay.squad_uuids,
        )
    )
    install_fake_api(monkeypatch, api)

    await RemnawaveGracePanelGateway().apply_billing_state(
        billing,
        expected_overlay=overlay,
    )

    assert_no_derived_status_writes(api)
    assert api.user.status is UserStatus.LIMITED
    assert api.user.expire_at == billing.end_at
    assert api.user.traffic_limit_bytes == billing.traffic_limit_bytes
    assert api.user.hwid_device_limit == billing.device_limit
    assert api.user.active_internal_squads == [{'uuid': REGULAR_SQUAD}]
    assert api.user.external_squad_uuid == EXTERNAL_SQUAD


@pytest.mark.asyncio
async def test_apply_limited_billing_keeps_grace_routing_until_panel_derives_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    billing = make_limited_billing()
    overlay = make_overlay()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.ACTIVE,
            expire_at=overlay.expire_at,
            traffic_limit_bytes=overlay.traffic_limit_bytes,
            squad_uuids=overlay.squad_uuids,
        )
    )
    install_fake_api(monkeypatch, api)
    gateway = RemnawaveGracePanelGateway()

    with pytest.raises(GracePanelTransitionPending):
        await gateway.apply_billing_state(billing, expected_overlay=overlay)

    assert_no_derived_status_writes(api)
    assert api.user.status is UserStatus.ACTIVE
    assert api.user.expire_at == billing.end_at
    assert api.user.traffic_limit_bytes == billing.traffic_limit_bytes
    assert api.user.hwid_device_limit == billing.device_limit
    assert api.user.active_internal_squads == [{'uuid': GRACE_SQUAD}]
    assert api.user.external_squad_uuid is None

    api.user.status = UserStatus.LIMITED
    await gateway.apply_billing_state(billing, expected_overlay=overlay)

    assert_no_derived_status_writes(api)
    assert api.user.active_internal_squads == [{'uuid': REGULAR_SQUAD}]
    assert api.user.external_squad_uuid == EXTERNAL_SQUAD


@pytest.mark.asyncio
async def test_restore_limited_snapshot_recognizes_safe_active_intermediate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = make_limited_snapshot()
    overlay = make_overlay()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.ACTIVE,
            expire_at=overlay.expire_at,
            traffic_limit_bytes=overlay.traffic_limit_bytes,
            squad_uuids=overlay.squad_uuids,
        )
    )
    install_fake_api(monkeypatch, api)
    gateway = RemnawaveGracePanelGateway()

    with pytest.raises(GracePanelTransitionPending):
        await gateway.restore_snapshot(PANEL_ID, snapshot, overlay)

    assert_no_derived_status_writes(api)
    assert api.user.status is UserStatus.ACTIVE
    assert api.user.expire_at == snapshot.expire_at
    assert api.user.traffic_limit_bytes == snapshot.traffic_limit_bytes
    assert api.user.active_internal_squads == [{'uuid': GRACE_SQUAD}]
    assert api.user.external_squad_uuid is None

    api.user.status = UserStatus.LIMITED
    outcome = await gateway.restore_snapshot(PANEL_ID, snapshot, overlay)

    assert outcome is GraceRestoreOutcome.RESTORED
    assert_no_derived_status_writes(api)
    assert api.user.active_internal_squads == [{'uuid': REGULAR_SQUAD}]
    assert api.user.external_squad_uuid == EXTERNAL_SQUAD


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('status', 'squad_uuids'),
    [
        (UserStatus.DISABLED, (GRACE_SQUAD,)),
        (UserStatus.ACTIVE, (OTHER_SQUAD,)),
    ],
)
async def test_restore_does_not_overwrite_manual_or_unrelated_panel_state(
    monkeypatch: pytest.MonkeyPatch,
    status: UserStatus,
    squad_uuids: tuple[str, ...],
) -> None:
    snapshot = make_limited_snapshot()
    overlay = make_overlay()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=status,
            expire_at=overlay.expire_at,
            traffic_limit_bytes=overlay.traffic_limit_bytes,
            squad_uuids=squad_uuids,
        )
    )
    install_fake_api(monkeypatch, api)

    outcome = await RemnawaveGracePanelGateway().restore_snapshot(
        PANEL_ID,
        snapshot,
        overlay,
    )

    assert outcome is GraceRestoreOutcome.CONFLICT
    assert api.updates == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('status', 'squad_uuids'),
    [
        (UserStatus.DISABLED, (GRACE_SQUAD,)),
        (UserStatus.ACTIVE, (OTHER_SQUAD,)),
    ],
)
async def test_apply_limited_billing_does_not_overwrite_manual_or_unrelated_panel_state(
    monkeypatch: pytest.MonkeyPatch,
    status: UserStatus,
    squad_uuids: tuple[str, ...],
) -> None:
    overlay = make_overlay()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=status,
            expire_at=overlay.expire_at,
            traffic_limit_bytes=overlay.traffic_limit_bytes,
            squad_uuids=squad_uuids,
        )
    )
    install_fake_api(monkeypatch, api)

    with pytest.raises(GracePanelTransitionConflict, match='changed outside grace'):
        await RemnawaveGracePanelGateway().apply_billing_state(
            make_limited_billing(),
            expected_overlay=overlay,
        )

    assert api.updates == []


@pytest.mark.asyncio
async def test_apply_limited_billing_updates_device_limit_even_when_other_fields_already_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    billing = make_limited_billing()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.LIMITED,
            expire_at=billing.end_at,
            traffic_limit_bytes=billing.traffic_limit_bytes,
            squad_uuids=billing.squad_uuids,
            external_squad_uuid=billing.external_squad_uuid,
        )
    )
    install_fake_api(monkeypatch, api)

    await RemnawaveGracePanelGateway().apply_billing_state(
        billing,
        expected_overlay=make_overlay(),
    )

    assert api.user.hwid_device_limit == billing.device_limit
    # Ровно один PATCH и ровно по числовому идентификатору: тело с ключом
    # uuid панель 3.0.0 отвергает (в схеме запроса такого поля нет).
    assert api.updates == [
        {
            'user_id': PANEL_ID,
            'hwid_device_limit': billing.device_limit,
        }
    ]
    assert_no_derived_status_writes(api)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('billing_status', 'billing_end_at', 'expected_status'),
    [
        ('active', NOW + timedelta(days=20), UserStatus.ACTIVE),
        ('disabled', NOW + timedelta(days=20), UserStatus.DISABLED),
    ],
)
async def test_apply_non_derived_billing_status_remains_one_phase(
    monkeypatch: pytest.MonkeyPatch,
    billing_status: str,
    billing_end_at: datetime,
    expected_status: UserStatus,
) -> None:
    overlay = make_overlay()
    billing = GraceBillingState(
        subscription_id=42,
        remnawave_id=PANEL_ID,
        status=billing_status,
        end_at=billing_end_at,
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=3 * GIB,
        device_limit=4,
        squad_uuids=(REGULAR_SQUAD,),
        external_squad_uuid=EXTERNAL_SQUAD,
    )
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.ACTIVE,
            expire_at=overlay.expire_at,
            traffic_limit_bytes=overlay.traffic_limit_bytes,
            squad_uuids=overlay.squad_uuids,
        )
    )
    install_fake_api(monkeypatch, api)

    await RemnawaveGracePanelGateway().apply_billing_state(
        billing,
        expected_overlay=overlay,
    )

    assert len(api.updates) == 1
    assert api.updates[0]['status'] is expected_status
    assert api.updates[0]['user_id'] == PANEL_ID
    assert_no_derived_status_writes(api)
    assert api.user.active_internal_squads == [{'uuid': REGULAR_SQUAD}]
    assert api.user.external_squad_uuid == EXTERNAL_SQUAD


@pytest.mark.asyncio
async def test_apply_overlay_detaches_external_squad_first_and_addresses_the_numeric_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay = make_overlay()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.EXPIRED,
            expire_at=NOW - timedelta(days=1),
            traffic_limit_bytes=10 * GIB,
            squad_uuids=(REGULAR_SQUAD,),
            external_squad_uuid=EXTERNAL_SQUAD,
        )
    )
    install_fake_api(monkeypatch, api)

    await RemnawaveGracePanelGateway().apply_overlay(PANEL_ID, overlay)

    # Отцепление внешнего сквада — отдельный первый PATCH: ретрай A039 без
    # externalSquadUuid не должен случайно выдать неограниченный доступ.
    assert api.updates[0] == {'user_id': PANEL_ID, 'external_squad_uuid': None}
    assert [update['user_id'] for update in api.updates] == [PANEL_ID, PANEL_ID]
    assert api.updates[1]['status'] is UserStatus.ACTIVE
    assert api.user.active_internal_squads == [{'uuid': GRACE_SQUAD}]
    assert api.user.external_squad_uuid is None


@pytest.mark.asyncio
async def test_read_snapshot_returns_the_numeric_panel_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.LIMITED,
            expire_at=NOW + timedelta(days=20),
            traffic_limit_bytes=10 * GIB,
            squad_uuids=(REGULAR_SQUAD,),
            external_squad_uuid=EXTERNAL_SQUAD,
        )
    )
    install_fake_api(monkeypatch, api)

    snapshot = await RemnawaveGracePanelGateway().read_snapshot(PANEL_ID)

    assert snapshot is not None
    assert snapshot.remnawave_id == PANEL_ID
    assert api.reads == [PANEL_ID]


@pytest.mark.asyncio
async def test_read_snapshot_rejects_a_legacy_uuid_instead_of_reporting_no_panel_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Маршруты 3.0.0 параметризованы числом, поэтому uuid даёт 400, а не 404.
    # Ответить на это None значило бы «панельного юзера нет» — и вызывающий
    # завёл бы дубль вместо того, чтобы починить битую связь в наших данных.
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.ACTIVE,
            expire_at=NOW + timedelta(days=20),
            traffic_limit_bytes=10 * GIB,
            squad_uuids=(REGULAR_SQUAD,),
        )
    )
    install_fake_api(monkeypatch, api)

    with pytest.raises(RemnaWaveInvalidUserIdError):
        await RemnawaveGracePanelGateway().read_snapshot(LEGACY_PANEL_UUID)

    assert api.updates == []


def test_v2_snapshot_row_stays_readable_after_the_identity_backfill() -> None:
    # list_open молча выбрасывает сессии, которые не смог разобрать. Откажись
    # ридер от v2 — такие оверлеи остались бы открытыми навсегда и без ошибки.
    session = _model_to_session(make_v2_session_row())

    assert session.remnawave_id == PANEL_ID
    # panel_before знает только исторический uuid, поэтому числовой id берётся
    # из колонки строки — бэкфил заполнил её из той же подписки.
    assert session.panel_before.remnawave_id == PANEL_ID
    assert session.panel_before.external_squad_uuid == EXTERNAL_SQUAD
    # В billing_before идентичность ни на одно решение не влияет, а лежавший там
    # uuid в 3.0.0 непригоден — значит None, а не строка 'aaaaaaaa-...'.
    assert session.billing_before.remnawave_id is None
    assert session.billing_before.device_limit == 4
    assert session.overlay.squad_uuids == (GRACE_SQUAD,)
    assert session.state is GraceSessionState.ACTIVE
    assert session.reason is GraceReason.EXPIRED


def test_v2_row_without_a_backfilled_id_fails_loudly_instead_of_closing_silently() -> None:
    # Пустая колонка — разорванная связь в наших данных, а не «в панели юзера
    # нет»: закрыть такую сессию без отката оверлея нельзя.
    with pytest.raises(GraceSnapshotError, match='remnawave_id'):
        _model_to_session(make_v2_session_row(remnawave_id=None))


def test_unsupported_snapshot_version_is_rejected_instead_of_guessed() -> None:
    row = make_v2_session_row()
    row.snapshot_version = 1

    with pytest.raises(GraceSnapshotError, match='Unsupported grace snapshot version'):
        _model_to_session(row)


def test_saving_a_v2_row_upgrades_it_to_v3_without_erasing_the_historical_uuid() -> None:
    session = _model_to_session(make_v2_session_row())

    values = _session_values(session)

    assert values['snapshot_version'] == 3
    assert values['remnawave_id'] == PANEL_ID
    # UPDATE не должен трогать историческую колонку: новый код uuid не знает, и
    # запись None стёрла бы единственный аудиторский след доапгрейдной сессии.
    assert 'remnawave_uuid' not in values

    upgraded = _model_to_session(_session_to_model(session))

    assert upgraded.remnawave_id == PANEL_ID
    assert upgraded.panel_before.remnawave_id == PANEL_ID


# ---- create_panel_user_grace_safe: подхват обязан ПРИМЕНИТЬ payload ----


@pytest.mark.asyncio
async def test_adopt_or_create_patches_the_adopted_panel_user(monkeypatch):
    """Подхватить аккаунт мало — вызывающий просил привести панель к состоянию.

    Регрессия: хелпер возвращал найденного пользователя без PATCH, поэтому
    админское «продлить»/«синхронизировать в панель» рапортовало успех, а в
    панели оставались старые статус, дата и лимиты.
    """
    from app.services.grace_access_runtime import _adopt_or_create

    adopted = SimpleNamespace(id=8812)
    patched = SimpleNamespace(id=8812)
    api = AsyncMock()
    api.get_user_by_short_uuid.return_value = adopted
    api.update_user.return_value = patched

    result = await _adopt_or_create(
        api, 'aBcD12', {'username': 'user_1_abc', 'status': 'ACTIVE', 'traffic_limit_bytes': 42}
    )

    assert result is patched
    api.create_user.assert_not_awaited()
    kwargs = api.update_user.await_args.kwargs
    assert kwargs['user_id'] == 8812
    assert kwargs['traffic_limit_bytes'] == 42
    assert 'username' not in kwargs, 'username — create-only, переименовывать аккаунт нельзя'


@pytest.mark.asyncio
async def test_adopt_or_create_creates_when_panel_denies_the_short_uuid(monkeypatch):
    from app.services.grace_access_runtime import _adopt_or_create

    created = SimpleNamespace(id=9001)
    api = AsyncMock()
    api.get_user_by_short_uuid.return_value = None
    api.create_user.return_value = created

    result = await _adopt_or_create(api, 'gone', {'username': 'u', 'status': 'ACTIVE'})

    assert result is created
    api.update_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_adopt_or_create_propagates_a_non_404_panel_error(monkeypatch):
    """Проглотить 5xx и создать нового — это и есть дубль рядом с живым аккаунтом."""
    from app.external.remnawave_api import RemnaWaveAPIError
    from app.services.grace_access_runtime import _adopt_or_create

    api = AsyncMock()
    api.get_user_by_short_uuid.side_effect = RemnaWaveAPIError('Bad Gateway', 502, {})

    with pytest.raises(RemnaWaveAPIError):
        await _adopt_or_create(api, 'aBcD12', {'username': 'u'})

    api.create_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_adopt_does_not_wipe_squads_when_the_local_list_is_empty():
    """Пустой список сквадов НЕ должен уходить в PATCH.

    `create_user` пропускает пустой список, `update_user` — только None, а в
    контракте «не прислать» = не трогать, «прислать []» = снять все сквады.
    Переслав create-тело как есть, подхват снимал у живого оплаченного
    аккаунта все инбаунды: он оставался ACTIVE, но ссылка на подписку отдавала
    ноль конфигов. Состояние достижимо после «сброса подписки» и после
    удаления сквада из панели.
    """
    from app.services.grace_access_runtime import _adopt_or_create

    api = AsyncMock()
    api.get_user_by_short_uuid.return_value = SimpleNamespace(id=8812)
    api.update_user.return_value = SimpleNamespace(id=8812)

    await _adopt_or_create(api, 'aBcD12', {'username': 'u', 'status': 'ACTIVE', 'active_internal_squads': []})

    kwargs = api.update_user.await_args.kwargs
    assert 'active_internal_squads' not in kwargs


@pytest.mark.asyncio
async def test_adopt_forwards_a_non_empty_squad_list():
    """Обратная сторона: реальный список обязан доехать."""
    from app.services.grace_access_runtime import _adopt_or_create

    api = AsyncMock()
    api.get_user_by_short_uuid.return_value = SimpleNamespace(id=8812)
    api.update_user.return_value = SimpleNamespace(id=8812)

    await _adopt_or_create(api, 'aBcD12', {'username': 'u', 'active_internal_squads': ['squad-1', 'squad-2']})

    assert api.update_user.await_args.kwargs['active_internal_squads'] == ['squad-1', 'squad-2']


@pytest.mark.asyncio
async def test_open_session_without_panel_id_is_repaired_from_the_subscription(monkeypatch):
    """Сессия с пустым `remnawave_id` должна чиниться, а не жить вечно.

    Такую строку оставил старый код: колонки тогда не было. Читать её нельзя —
    `_model_to_session` бросает `GraceSnapshotError`, `get_open` роняет продление
    и разбор платежа, фоновой цикл пишет ошибку каждый проход, а уникальный
    индекс на открытую сессию не даёт открыть новую. При этом ответ лежит рядом:
    подписка уже связана бэкфилом.
    """
    from app.database.models import Subscription as SubModel, User as UserModel
    from app.services.grace_access_runtime import SQLAlchemyGraceSessionStore
    from tests.fixtures.sqlite_memory import memory_session

    tables = [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    async with memory_session(monkeypatch, tables) as db:
        db.add(UserModel(id=1, telegram_id=100, remnawave_id=None))
        db.add(
            SubModel(
                id=42,
                user_id=1,
                status='expired',
                end_date=NOW - timedelta(days=1),
                remnawave_id=PANEL_ID,
                remnawave_short_id='sid42',
            )
        )
        # Ровно то, что оставил старый код: валидный снапшот, но колонка пуста.
        db.add(make_v2_session_row(remnawave_id=None))
        await db.commit()

        store = SQLAlchemyGraceSessionStore(db)
        sessions = await store.list_open(limit=10)

        assert len(sessions) == 1, 'сессия должна стать читаемой, а не пропускаться каждый цикл'
        assert sessions[0].remnawave_id == PANEL_ID

        db.expunge_all()
        row = await db.get(GraceAccessSessionModel, '11111111-2222-3333-4444-555555555555')
        assert row.remnawave_id == PANEL_ID, 'починка должна сохраниться, а не повторяться каждый проход'


@pytest.mark.asyncio
async def test_get_open_no_longer_explodes_on_a_session_without_panel_id(monkeypatch):
    """`get_open` вызывают продление и разбор платежа — он не должен падать."""
    from app.database.models import Subscription as SubModel, User as UserModel
    from app.services.grace_access_runtime import SQLAlchemyGraceSessionStore
    from tests.fixtures.sqlite_memory import memory_session

    tables = [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    async with memory_session(monkeypatch, tables) as db:
        db.add(UserModel(id=1, telegram_id=100, remnawave_id=None))
        db.add(
            SubModel(
                id=42,
                user_id=1,
                status='expired',
                end_date=NOW - timedelta(days=1),
                remnawave_id=PANEL_ID,
                remnawave_short_id='sid42',
            )
        )
        db.add(make_v2_session_row(remnawave_id=None))
        await db.commit()

        session = await SQLAlchemyGraceSessionStore(db).get_open(42)

        assert session is not None
        assert session.remnawave_id == PANEL_ID


# ==================== истёкший снимок: без DISABLED, панель гасит сама ====================
#
# Жалоба владельца (2026-09-14): «грейс не работает от слова совсем». На стенде
# (remnawave/backend:3 = 3.4.3) выдача работала, а вот конец грейса — нет:
# восстановление истёкшего снимка слало ``status=DISABLED``. Если согласователь
# успевал раньше планировщика панели, аккаунт становился DISABLED, вебхук
# ``user.modified`` переносил это в бота как «отключена администратором», и
# кабинет отказывал в продлении. Если панель гасила первой, PATCH DISABLED на
# EXPIRED панель молча игнорировала — но дата грейса оставалась в панели,
# импорт «панель — истина» двигал дату окончания в боте на конец грейса, воркер
# видел «только что истекла» и выдавал грейс заново. Бесконечно, раз в 72 часа.
#
# Правило теперь общее с panel_sync: истёкшей подписке статус не шлём, панель
# гасит аккаунт сама; бот восстанавливает лимит и сквады и ждёт (RESTORING),
# пока панель не выведет EXPIRED. Дата, которую грейс оставил в панели,
# запоминается на подписке (``grace_tail_expire_at``) и в бота не импортируется.


def make_expired_snapshot() -> GracePanelSnapshot:
    return GracePanelSnapshot(
        remnawave_id=PANEL_ID,
        status='EXPIRED',
        expire_at=NOW - timedelta(days=1),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
        squad_uuids=(REGULAR_SQUAD,),
        external_squad_uuid=EXTERNAL_SQUAD,
    )


def make_timed_out_overlay() -> GracePanelOverlay:
    """Оверлей, срок которого только что прошёл: планировщик панели ещё не сработал."""
    return GracePanelOverlay(
        status='ACTIVE',
        expire_at=NOW - timedelta(seconds=30),
        traffic_limit_bytes=11 * GIB,
        squad_uuids=(GRACE_SQUAD,),
        external_squad_uuid=None,
    )


def make_expired_billing() -> GraceBillingState:
    return GraceBillingState(
        subscription_id=42,
        remnawave_id=PANEL_ID,
        status='expired',
        end_at=NOW - timedelta(days=1),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=10 * GIB,
        device_limit=4,
        squad_uuids=(REGULAR_SQUAD,),
        external_squad_uuid=EXTERNAL_SQUAD,
    )


def assert_no_disable_writes(api: FakeRemnawaveApi) -> None:
    assert all(update.get('status') is not UserStatus.DISABLED for update in api.updates), api.updates


@pytest.mark.asyncio
async def test_restore_expired_snapshot_never_disables_and_waits_for_the_panel_to_expire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = make_expired_snapshot()
    overlay = make_timed_out_overlay()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.ACTIVE,
            expire_at=overlay.expire_at,
            traffic_limit_bytes=overlay.traffic_limit_bytes,
            squad_uuids=overlay.squad_uuids,
        )
    )
    install_fake_api(monkeypatch, api)
    gateway = RemnawaveGracePanelGateway()

    with pytest.raises(GracePanelTransitionPending):
        await gateway.restore_snapshot(PANEL_ID, snapshot, overlay)

    assert_no_disable_writes(api)
    assert_no_derived_status_writes(api)
    assert len(api.updates) == 1
    assert 'expire_at' not in api.updates[0], 'дата уже прошла — двигать её нечего'
    assert api.user.status is UserStatus.ACTIVE
    assert api.user.traffic_limit_bytes == snapshot.traffic_limit_bytes
    assert api.user.active_internal_squads == [{'uuid': REGULAR_SQUAD}]
    assert api.user.external_squad_uuid == EXTERNAL_SQUAD

    api.user.status = UserStatus.EXPIRED
    outcome = await gateway.restore_snapshot(PANEL_ID, snapshot, overlay)

    assert outcome is GraceRestoreOutcome.ALREADY_RESTORED
    assert len(api.updates) == 1


@pytest.mark.asyncio
async def test_restore_expired_snapshot_from_a_panel_that_already_expired_is_one_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = make_expired_snapshot()
    overlay = make_timed_out_overlay()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.EXPIRED,
            expire_at=overlay.expire_at,
            traffic_limit_bytes=overlay.traffic_limit_bytes,
            squad_uuids=overlay.squad_uuids,
        )
    )
    install_fake_api(monkeypatch, api)

    outcome = await RemnawaveGracePanelGateway().restore_snapshot(PANEL_ID, snapshot, overlay)

    assert outcome is GraceRestoreOutcome.RESTORED
    assert_no_disable_writes(api)
    assert_no_derived_status_writes(api)
    assert len(api.updates) == 1
    assert 'status' not in api.updates[0]
    assert api.user.status is UserStatus.EXPIRED
    assert api.user.active_internal_squads == [{'uuid': REGULAR_SQUAD}]
    assert api.user.traffic_limit_bytes == snapshot.traffic_limit_bytes
    assert api.user.external_squad_uuid == EXTERNAL_SQUAD


@pytest.mark.asyncio
async def test_early_restore_of_expired_snapshot_extinguishes_the_far_future_grace_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Аварийный restore-all и конфликты закрывают грейс до срока: панель ещё ACTIVE
    с датой через несколько дней. Прошедшую дату PATCH не примет, DISABLED — это
    «отключена админом»; остаётся общее правило гашения: ближайший допустимый момент."""
    snapshot = make_expired_snapshot()
    overlay = make_overlay()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.ACTIVE,
            expire_at=overlay.expire_at,
            traffic_limit_bytes=overlay.traffic_limit_bytes,
            squad_uuids=overlay.squad_uuids,
        )
    )
    install_fake_api(monkeypatch, api)

    with pytest.raises(GracePanelTransitionPending):
        await RemnawaveGracePanelGateway().restore_snapshot(PANEL_ID, snapshot, overlay)

    assert_no_disable_writes(api)
    assert len(api.updates) == 1
    sent = api.updates[0]['expire_at']
    assert timedelta(minutes=4) <= sent - datetime.now(UTC) <= timedelta(minutes=6)
    assert api.user.active_internal_squads == [{'uuid': REGULAR_SQUAD}]


@pytest.mark.asyncio
async def test_restore_of_a_truly_disabled_snapshot_still_disables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = GracePanelSnapshot(
        remnawave_id=PANEL_ID,
        status='DISABLED',
        expire_at=NOW + timedelta(days=20),
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=1 * GIB,
        squad_uuids=(REGULAR_SQUAD,),
        external_squad_uuid=None,
    )
    overlay = make_overlay()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.ACTIVE,
            expire_at=overlay.expire_at,
            traffic_limit_bytes=overlay.traffic_limit_bytes,
            squad_uuids=overlay.squad_uuids,
        )
    )
    install_fake_api(monkeypatch, api)

    outcome = await RemnawaveGracePanelGateway().restore_snapshot(PANEL_ID, snapshot, overlay)

    assert outcome is GraceRestoreOutcome.RESTORED
    assert api.updates[0]['status'] is UserStatus.DISABLED


@pytest.mark.asyncio
async def test_restore_expired_snapshot_does_not_touch_an_unrelated_panel_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = make_expired_snapshot()
    overlay = make_timed_out_overlay()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.ACTIVE,
            expire_at=NOW + timedelta(days=40),
            traffic_limit_bytes=overlay.traffic_limit_bytes,
            squad_uuids=(OTHER_SQUAD,),
        )
    )
    install_fake_api(monkeypatch, api)

    outcome = await RemnawaveGracePanelGateway().restore_snapshot(PANEL_ID, snapshot, overlay)

    assert outcome is GraceRestoreOutcome.CONFLICT
    assert api.updates == []


@pytest.mark.asyncio
async def test_restore_expired_snapshot_records_the_grace_tail_on_the_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Дата, оставшаяся в панели после грейса, запоминается на подписке — по ней
    импорт отличит хвост грейса от настоящего продления в панели."""
    from sqlalchemy import select

    from app.database.models import Base, Subscription, User
    from tests.fixtures.sqlite_memory import memory_session

    snapshot = make_expired_snapshot()
    overlay = make_timed_out_overlay()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.EXPIRED,
            expire_at=overlay.expire_at,
            traffic_limit_bytes=overlay.traffic_limit_bytes,
            squad_uuids=overlay.squad_uuids,
        )
    )
    install_fake_api(monkeypatch, api)

    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        db.add(User(id=1, telegram_id=1001, first_name='U', language='ru', status='active', balance_kopeks=0))
        db.add(
            Subscription(
                id=42,
                remnawave_short_id='sub42',
                user_id=1,
                status='expired',
                is_trial=False,
                start_date=NOW - timedelta(days=31),
                end_date=NOW - timedelta(days=1),
                traffic_limit_gb=10,
                traffic_used_gb=10.0,
                device_limit=4,
                connected_squads=[REGULAR_SQUAD],
            )
        )
        await db.commit()

        outcome = await RemnawaveGracePanelGateway(db=db, subscription_id=42).restore_snapshot(
            PANEL_ID, snapshot, overlay
        )
        await db.commit()

        assert outcome is GraceRestoreOutcome.RESTORED
        stored = (await db.execute(select(Subscription.grace_tail_expire_at).where(Subscription.id == 42))).scalar_one()
        assert stored is not None
        assert abs((stored.replace(tzinfo=UTC) - overlay.expire_at).total_seconds()) < 1
        end_date = (await db.execute(select(Subscription.end_date).where(Subscription.id == 42))).scalar_one()
        assert end_date.replace(tzinfo=UTC) == NOW - timedelta(days=1), 'дата окончания в боте не трогается'


@pytest.mark.asyncio
async def test_apply_expired_billing_state_never_disables_and_waits_for_the_panel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Конфликт посреди грейса у истёкшей подписки: канон панели для неё — EXPIRED,
    который панель выводит сама; DISABLED значил бы «отключена админом»."""
    billing = make_expired_billing()
    overlay = make_overlay()
    api = FakeRemnawaveApi(
        make_panel_user(
            status=UserStatus.ACTIVE,
            expire_at=overlay.expire_at,
            traffic_limit_bytes=overlay.traffic_limit_bytes,
            squad_uuids=overlay.squad_uuids,
        )
    )
    install_fake_api(monkeypatch, api)
    gateway = RemnawaveGracePanelGateway()

    with pytest.raises(GracePanelTransitionPending):
        await gateway.apply_billing_state(billing, expected_overlay=overlay)

    assert_no_disable_writes(api)
    assert_no_derived_status_writes(api)
    assert len(api.updates) == 1
    sent = api.updates[0]['expire_at']
    assert timedelta(minutes=4) <= sent - datetime.now(UTC) <= timedelta(minutes=6), 'дата грейса гасится'
    assert api.user.active_internal_squads == [{'uuid': REGULAR_SQUAD}]
    assert api.user.traffic_limit_bytes == billing.traffic_limit_bytes
    assert api.user.external_squad_uuid == EXTERNAL_SQUAD
    assert api.user.hwid_device_limit == billing.device_limit

    api.user.status = UserStatus.EXPIRED
    await gateway.apply_billing_state(billing, expected_overlay=overlay)

    assert len(api.updates) == 1


@pytest.mark.parametrize(
    ('billing_status', 'user_status', 'end_at', 'expected'),
    [
        ('expired', 'active', NOW - timedelta(days=1), UserStatus.EXPIRED),
        ('active', 'active', NOW - timedelta(days=1), UserStatus.EXPIRED),
        ('expired', 'blocked', NOW - timedelta(days=1), UserStatus.DISABLED),
        ('disabled', 'active', NOW + timedelta(days=1), UserStatus.DISABLED),
        ('limited', 'active', NOW + timedelta(days=1), UserStatus.LIMITED),
        ('active', 'active', NOW + timedelta(days=1), UserStatus.ACTIVE),
    ],
)
def test_billing_target_status_follows_the_shared_panel_rule(
    billing_status: str,
    user_status: str,
    end_at: datetime,
    expected: UserStatus,
) -> None:
    billing = GraceBillingState(
        subscription_id=42,
        remnawave_id=PANEL_ID,
        status=billing_status,
        end_at=end_at,
        traffic_limit_bytes=10 * GIB,
        used_traffic_bytes=1 * GIB,
        device_limit=4,
        squad_uuids=(REGULAR_SQUAD,),
        external_squad_uuid=None,
        user_status=user_status,
    )

    assert _build_billing_target(billing, now=NOW).status is expected


def test_expired_target_matches_only_an_expired_panel_and_ignores_the_date() -> None:
    target = _PanelTarget(
        status=UserStatus.EXPIRED,
        expire_at=None,
        traffic_limit_bytes=10 * GIB,
        squad_uuids=(REGULAR_SQUAD,),
        external_squad_uuid=None,
    )

    def snapshot(status: str) -> GracePanelSnapshot:
        return GracePanelSnapshot(
            remnawave_id=PANEL_ID,
            status=status,
            expire_at=NOW + timedelta(days=3),
            traffic_limit_bytes=10 * GIB,
            used_traffic_bytes=1 * GIB,
            squad_uuids=(REGULAR_SQUAD,),
            external_squad_uuid=None,
        )

    assert _panel_matches_target(snapshot('EXPIRED'), target)
    assert not _panel_matches_target(snapshot('ACTIVE'), target)
    assert not _panel_matches_target(snapshot('DISABLED'), target), 'DISABLED — чужое решение, не наш EXPIRED'


# ==================== уведомления из рантайма ====================


class _StubCore:
    def __init__(self, *, start=None, reconcile=None) -> None:
        self._start = start
        self._reconcile = reconcile

    async def start_if_eligible(self, billing, reason):
        return self._start

    async def reconcile(self, *, limit=None):
        return self._reconcile

    async def drain(self, *, limit=None, force_restore=False):
        return self._reconcile


@pytest_asyncio.fixture
async def runtime_lab(monkeypatch):
    """Рантайм на реальной SQLite с подменённым ядром: проверяем только обвязку."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.database.models import Base, Subscription, User
    from app.services import grace_access_runtime as rt
    from app.services.grace_access_service import GraceAccessMode
    from tests.fixtures.sqlite_memory import ensure_real_aiosqlite

    ensure_real_aiosqlite(monkeypatch)
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=list(Base.metadata.sorted_tables)))
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with maker() as db:
        db.add(User(id=1, telegram_id=1001, first_name='U', language='ru', status='active', balance_kopeks=0))
        await db.flush()
        db.add(
            Subscription(
                id=42,
                remnawave_short_id='sub42',
                user_id=1,
                status='expired',
                is_trial=False,
                start_date=NOW - timedelta(days=31),
                end_date=NOW - timedelta(days=1),
                traffic_limit_gb=10,
                traffic_used_gb=1.0,
                device_limit=3,
                connected_squads=[REGULAR_SQUAD],
                remnawave_id=PANEL_ID,
            )
        )
        await db.commit()
    monkeypatch.setattr(rt, 'AsyncSessionLocal', maker)
    announce = AsyncMock()
    monkeypatch.setattr(rt, 'announce_grace_event', announce)
    runtime = rt.GraceAccessRuntime()
    runtime._mode = GraceAccessMode.ACTIVE
    runtime.bot = object()
    # Хук после коммита берёт бота с общего синглтона — как в проде (main.py).
    monkeypatch.setattr(rt.grace_access_runtime, 'bot', runtime.bot)
    try:
        yield SimpleNamespace(rt=rt, runtime=runtime, announce=announce)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_fresh_grant_is_announced_after_the_commit(runtime_lab, monkeypatch):
    from app.services.grace_access_service import GraceStartDecision, GraceStartResult

    session = SimpleNamespace(state=GraceSessionState.ACTIVE)
    monkeypatch.setattr(
        runtime_lab.rt,
        '_build_core',
        lambda db, subscription_id=None: _StubCore(start=GraceStartResult(GraceStartDecision.STARTED, session)),
    )

    await runtime_lab.runtime.consider_candidate(42, GraceReason.EXPIRED, source='worker')

    runtime_lab.announce.assert_awaited_once_with(runtime_lab.runtime.bot, 42, 'granted')


@pytest.mark.asyncio
async def test_a_repeated_incident_is_not_announced_again(runtime_lab, monkeypatch):
    from app.services.grace_access_service import GraceStartDecision, GraceStartResult

    monkeypatch.setattr(
        runtime_lab.rt,
        '_build_core',
        lambda db, subscription_id=None: _StubCore(start=GraceStartResult(GraceStartDecision.ALREADY_GRANTED, None)),
    )

    await runtime_lab.runtime.consider_candidate(42, GraceReason.EXPIRED, source='webhook')

    runtime_lab.announce.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('outcome', 'event'),
    [
        ({'activated': 1}, 'granted'),
        ({'paid': 1}, 'ended'),
        ({'timed_out': 1}, 'ended'),
        ({'drained': 1}, 'ended'),
        ({'revoked': 1}, 'ended'),
        ({'conflicts': 1}, 'ended'),
    ],
)
async def test_reconciliation_outcomes_are_announced(runtime_lab, monkeypatch, outcome, event):
    from app.services.grace_access_service import GraceReconcileResult

    monkeypatch.setattr(
        runtime_lab.rt,
        '_build_core',
        lambda db, subscription_id=None: _StubCore(reconcile=GraceReconcileResult(inspected=1, **outcome)),
    )

    await runtime_lab.runtime._process_open(42, drain=False, force_restore=False)

    runtime_lab.announce.assert_awaited_once_with(runtime_lab.runtime.bot, 42, event)


@pytest.mark.asyncio
async def test_an_unchanged_reconciliation_is_silent(runtime_lab, monkeypatch):
    from app.services.grace_access_service import GraceReconcileResult

    monkeypatch.setattr(
        runtime_lab.rt,
        '_build_core',
        lambda db, subscription_id=None: _StubCore(reconcile=GraceReconcileResult(inspected=1, unchanged=1)),
    )

    await runtime_lab.runtime._process_open(42, drain=False, force_restore=False)

    runtime_lab.announce.assert_not_awaited()


@pytest.mark.asyncio
async def test_renewal_announces_grace_end_only_after_the_callers_commit(runtime_lab):
    """Продление закрывает grace внутри чужой транзакции: объявлять — после её коммита."""
    import asyncio

    rt = runtime_lab.rt
    async with rt.AsyncSessionLocal() as db:
        rt.announce_grace_event_after_commit(db, 42, 'ended')
        await db.execute(rt.select(rt.Subscription.id).where(rt.Subscription.id == 42))
        await asyncio.sleep(0)
        runtime_lab.announce.assert_not_awaited()

        await db.commit()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    runtime_lab.announce.assert_awaited_once_with(runtime_lab.runtime.bot, 42, 'ended')

    # Хук одноразовый: следующий коммит той же сессии ничего не объявляет.
    async with rt.AsyncSessionLocal() as db:
        await db.commit()
        await asyncio.sleep(0)
    runtime_lab.announce.assert_awaited_once()


@pytest.mark.asyncio
async def test_rolled_back_renewal_announces_nothing(runtime_lab):
    import asyncio

    rt = runtime_lab.rt
    async with rt.AsyncSessionLocal() as db:
        rt.announce_grace_event_after_commit(db, 42, 'ended')
        await db.execute(rt.select(rt.Subscription.id).where(rt.Subscription.id == 42))
        await db.rollback()
        await asyncio.sleep(0)

    runtime_lab.announce.assert_not_awaited()
