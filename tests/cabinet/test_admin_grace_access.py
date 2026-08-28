"""Кабинетный раздел grace-доступа.

Проверяется то, что иначе всплывёт только в проде: сохранение режима ``true`` с
пустым сквадом (бот запустится с выключенным grace и скажет об этом одной строкой
в логе), молчаливая потеря правки в поле, закреплённом в ``.env``, и подмена формы
целиком там, где менялось одно поле.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_grace_access as route
from app.services.system_settings_service import bot_configuration_service


VALID_UUID = '3fa85f64-5717-4562-b3fc-2c963f66afa6'
OTHER_UUID = '17b2c1de-9f47-4a3d-8c11-5b6a0f9e2d34'


def test_routes_registered(registered_paths):
    assert '/cabinet/admin/grace-access' in registered_paths
    assert '/cabinet/admin/grace-access/sessions' in registered_paths
    assert '/cabinet/admin/grace-access/squads' in registered_paths


@pytest.mark.parametrize(
    ('method', 'path', 'expected'),
    [
        ('GET', '/admin/grace-access', 'get_grace_access_overview'),
        ('PUT', '/admin/grace-access', 'update_grace_access'),
        ('GET', '/admin/grace-access/sessions', 'list_grace_sessions'),
        ('GET', '/admin/grace-access/squads', 'list_grace_squads'),
    ],
)
def test_each_url_reaches_its_own_handler(method, path, expected):
    for candidate in route.router.routes:
        if method not in candidate.methods:
            continue
        match, _scope = candidate.matches({'type': 'http', 'method': method, 'path': path, 'headers': []})
        if match.name == 'FULL':
            assert candidate.endpoint.__name__ == expected
            return
    raise AssertionError(f'{method} {path} не совпал ни с одним маршрутом')


def _required_permissions(endpoint_name: str) -> set[str]:
    """Права, которые маршрут реально требует, — из его зависимостей, а не из текста.

    ``require_permission`` возвращает замыкание, поэтому запрошенные строки лежат
    в его ячейках. Проверка по исходнику зеленела бы и на закомментированном коде.
    """
    for candidate in route.router.routes:
        if candidate.endpoint.__name__ != endpoint_name:
            continue
        found: set[str] = set()
        for dependant in candidate.dependant.dependencies:
            for cell in getattr(dependant.call, '__closure__', None) or ():
                value = cell.cell_contents
                if isinstance(value, tuple) and value and all(isinstance(item, str) and ':' in item for item in value):
                    found.update(value)
        return found
    raise AssertionError(f'Маршрут {endpoint_name} не найден')


def test_sessions_endpoint_also_requires_users_read():
    """Список отдаёт чужие telegram_id, @логины и имена.

    Право на настройки бота не должно быть обходным путём к списку абонентов:
    во всей остальной админке за такие данные отвечает users:read.
    """
    assert _required_permissions('list_grace_sessions') == {'settings:read', 'users:read'}


def test_configuration_endpoints_stay_on_settings_permissions():
    assert _required_permissions('get_grace_access_overview') == {'settings:read'}
    assert _required_permissions('update_grace_access') == {'settings:edit'}
    assert _required_permissions('list_grace_squads') == {'settings:read'}


@pytest.fixture
def config(monkeypatch):
    """Живые настройки grace с валидной конфигурацией; правки не утекают в другие тесты."""
    from app.config import settings

    defaults = {
        'GRACE_ACCESS_MODE': 'false',
        'GRACE_ACCESS_DURATION_HOURS': 72,
        'GRACE_ACCESS_EXPIRED_SQUAD_UUID': VALID_UUID,
        'GRACE_ACCESS_LIMITED_SQUAD_UUID': OTHER_UUID,
        'GRACE_ACCESS_EXTERNAL_SQUAD_UUID': '',
        'GRACE_ACCESS_TRAFFIC_GB': 1,
        'GRACE_ACCESS_TRIAL_ENABLED': False,
        'GRACE_ACCESS_DAILY_ENABLED': False,
        'GRACE_ACCESS_FREE_ENABLED': False,
        'GRACE_ACCESS_RECONCILE_INTERVAL_SECONDS': 60,
        'GRACE_ACCESS_RECONCILE_BATCH_SIZE': 200,
        'GRACE_ACCESS_CANDIDATE_LOOKBACK_MINUTES': 30,
    }
    for key, value in defaults.items():
        monkeypatch.setattr(settings, key, value)
    return settings


@pytest.fixture
def saved(monkeypatch, config):
    """Перехват записи настроек: значение сразу видно и в ``settings``, как в проде."""
    from app.config import settings

    written: list[tuple[str, object]] = []

    async def fake_set_value(_db, key, value, **_kwargs):
        written.append((key, value))
        setattr(settings, key, value)

    monkeypatch.setattr(bot_configuration_service, 'set_value', fake_set_value)
    monkeypatch.setattr(bot_configuration_service, 'is_env_locked', lambda _key: False)
    return written


@pytest.fixture
def empty_db():
    db = AsyncMock()
    db.commit = AsyncMock()
    return db


@pytest.fixture
def status_snapshot(monkeypatch):
    """Счётчики сессий подменяются: раздел читает их из общего сборщика."""
    snapshot = {
        'open': 0,
        'open_errors': 0,
        'completed_errors': 0,
        'with_errors': 0,
        'states': {},
        'recent_errors': [],
    }

    async def fake_collect(_db, *, error_limit=20):
        return snapshot

    monkeypatch.setattr(route, 'collect_grace_status', fake_collect)
    return snapshot


ADMIN = SimpleNamespace(id=1, telegram_id=1)


class _ApiClient:
    """Асинхронный контекст, как у RemnaWaveService.get_api_client()."""

    def __init__(self, squads=None):
        self._squads = (
            squads
            if squads is not None
            else [
                SimpleNamespace(uuid=VALID_UUID, name='Grace', members_count=4),
                SimpleNamespace(uuid=None, name='без uuid', members_count=0),
            ]
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get_internal_squads(self):
        return self._squads


async def _update(db, **fields):
    return await route.update_grace_access(
        route.GraceAccessUpdate(**fields),
        admin=ADMIN,
        db=db,
    )


class TestEnabling:
    @pytest.mark.asyncio
    async def test_enabling_without_expired_squad_is_rejected(self, saved, empty_db, status_snapshot, config):
        """Бот молча стартует с выключенным grace — узнать об этом можно только из лога."""
        config.GRACE_ACCESS_EXPIRED_SQUAD_UUID = ''

        with pytest.raises(HTTPException) as excinfo:
            await _update(empty_db, mode='true')

        assert excinfo.value.status_code == 400
        assert 'expired_squad_uuid' in excinfo.value.detail
        assert saved == []

    @pytest.mark.asyncio
    async def test_enabling_with_malformed_squad_is_rejected(self, saved, empty_db, status_snapshot, config):
        config.GRACE_ACCESS_LIMITED_SQUAD_UUID = 'не-uuid'

        with pytest.raises(HTTPException) as excinfo:
            await _update(empty_db, mode='true')

        assert excinfo.value.status_code == 400
        assert saved == []

    @pytest.mark.asyncio
    async def test_enabling_without_traffic_is_rejected(self, saved, empty_db, status_snapshot, config):
        with pytest.raises(HTTPException) as excinfo:
            await _update(empty_db, mode='true', traffic_gb=0)

        assert excinfo.value.status_code == 400
        assert saved == []

    @pytest.mark.asyncio
    async def test_enabling_with_complete_config_is_saved(self, saved, empty_db, status_snapshot):
        overview = await _update(empty_db, mode='true')

        assert ('GRACE_ACCESS_MODE', 'true') in saved
        assert overview.config.mode == 'true'

    @pytest.mark.asyncio
    async def test_turning_off_never_blocked_by_incomplete_config(self, saved, empty_db, status_snapshot, config):
        """Иначе неверная конфигурация запирала бы админа в включённом режиме."""
        config.GRACE_ACCESS_MODE = 'true'
        config.GRACE_ACCESS_EXPIRED_SQUAD_UUID = ''

        await _update(empty_db, mode='false')

        assert ('GRACE_ACCESS_MODE', 'false') in saved

    @pytest.mark.asyncio
    async def test_drain_allowed_with_incomplete_config(self, saved, empty_db, status_snapshot, config):
        """Слив — путь выхода: он обязан работать именно тогда, когда что-то не так."""
        config.GRACE_ACCESS_LIMITED_SQUAD_UUID = ''

        await _update(empty_db, mode='drain')

        assert ('GRACE_ACCESS_MODE', 'drain') in saved


class TestRejectedInput:
    @pytest.mark.asyncio
    async def test_explicit_null_is_refused(self, saved, empty_db, status_snapshot):
        """null проходил валидацию и уезжал в БД: настройка становилась NULL и переживала рестарт."""
        with pytest.raises(HTTPException) as excinfo:
            await route.update_grace_access(
                route.GraceAccessUpdate.model_validate({'mode': None, 'duration_hours': None}),
                admin=ADMIN,
                db=empty_db,
            )

        assert excinfo.value.status_code == 400
        assert 'duration_hours' in excinfo.value.detail
        assert saved == []

    @pytest.mark.asyncio
    async def test_broken_config_refused_while_the_worker_still_runs(
        self, monkeypatch, saved, empty_db, status_snapshot, config
    ):
        """Сохранённый drain не останавливает воркер до перезапуска — политика ещё живая."""
        monkeypatch.setattr(route.grace_access_runtime, '_mode', route.GraceAccessMode.ACTIVE, raising=False)
        config.GRACE_ACCESS_MODE = 'drain'

        with pytest.raises(HTTPException) as excinfo:
            await _update(empty_db, traffic_gb=0)

        assert excinfo.value.status_code == 400
        assert saved == []

    @pytest.mark.asyncio
    async def test_same_write_passes_once_the_worker_is_down(
        self, monkeypatch, saved, empty_db, status_snapshot, config
    ):
        monkeypatch.setattr(route.grace_access_runtime, '_mode', route.GraceAccessMode.DISABLED, raising=False)
        config.GRACE_ACCESS_MODE = 'drain'

        await _update(empty_db, traffic_gb=0)

        assert saved == [('GRACE_ACCESS_TRAFFIC_GB', 0)]


class TestPartialUpdate:
    @pytest.mark.asyncio
    async def test_only_changed_fields_are_written(self, saved, empty_db, status_snapshot):
        """Экран отправляет форму целиком; запись всех полей затирала бы правки из бота."""
        await _update(
            empty_db,
            mode='false',
            duration_hours=48,
            expired_squad_uuid=VALID_UUID,
            limited_squad_uuid=OTHER_UUID,
            traffic_gb=1,
        )

        assert saved == [('GRACE_ACCESS_DURATION_HOURS', 48)]

    @pytest.mark.asyncio
    async def test_squad_uuid_is_trimmed(self, saved, empty_db, status_snapshot):
        """Скопированный из панели UUID почти всегда приезжает с пробелом."""
        await _update(empty_db, expired_squad_uuid=f'  {OTHER_UUID} ')

        assert saved == [('GRACE_ACCESS_EXPIRED_SQUAD_UUID', OTHER_UUID)]

    @pytest.mark.asyncio
    async def test_nothing_written_when_nothing_changed(self, saved, empty_db, status_snapshot):
        await _update(empty_db, mode='false')

        assert saved == []


class TestEnvLock:
    @pytest.mark.asyncio
    async def test_changing_env_pinned_field_is_refused(self, monkeypatch, saved, empty_db, status_snapshot):
        """Запись легла бы в БД, а после перезапуска победил бы .env — админ считал бы режим включённым."""
        monkeypatch.setattr(bot_configuration_service, 'is_env_locked', lambda key: key == 'GRACE_ACCESS_MODE')

        with pytest.raises(HTTPException) as excinfo:
            await _update(empty_db, mode='observe')

        assert excinfo.value.status_code == 409
        assert saved == []

    @pytest.mark.asyncio
    async def test_untouched_env_pinned_field_does_not_block_the_form(
        self, monkeypatch, saved, empty_db, status_snapshot
    ):
        """Форма отправляется целиком: закреплённый режим не должен запрещать правку срока."""
        monkeypatch.setattr(bot_configuration_service, 'is_env_locked', lambda key: key == 'GRACE_ACCESS_MODE')

        await _update(empty_db, mode='false', duration_hours=24)

        assert saved == [('GRACE_ACCESS_DURATION_HOURS', 24)]

    @pytest.mark.asyncio
    async def test_overview_reports_env_pinned_fields(self, monkeypatch, empty_db, status_snapshot, config):
        monkeypatch.setattr(bot_configuration_service, 'is_env_locked', lambda key: key == 'GRACE_ACCESS_TRAFFIC_GB')

        overview = await route.get_grace_access_overview(admin=ADMIN, db=empty_db)

        assert overview.env_locked == ['traffic_gb']


class TestOverview:
    @pytest.mark.asyncio
    async def test_broken_config_is_reported_before_it_is_switched_on(
        self, monkeypatch, empty_db, status_snapshot, config
    ):
        """Пустой сквад виден и при выключенном grace, но как замечание, а не авария."""
        monkeypatch.setattr(bot_configuration_service, 'is_env_locked', lambda _key: False)
        monkeypatch.setattr(route.grace_access_runtime, '_mode', route.GraceAccessMode.DISABLED, raising=False)
        config.GRACE_ACCESS_EXPIRED_SQUAD_UUID = ''

        overview = await route.get_grace_access_overview(admin=ADMIN, db=empty_db)

        assert overview.config.mode == 'false'
        assert [(issue.field, issue.code, issue.severity) for issue in overview.issues] == [
            ('expired_squad_uuid', 'squad_required', 'warning')
        ]

    @pytest.mark.asyncio
    async def test_same_gap_is_an_error_once_grace_runs(self, monkeypatch, empty_db, status_snapshot, config):
        """Работающий воркер собирает политику из этих же ключей на каждом проходе."""
        monkeypatch.setattr(bot_configuration_service, 'is_env_locked', lambda _key: False)
        monkeypatch.setattr(route.grace_access_runtime, '_mode', route.GraceAccessMode.ACTIVE, raising=False)
        config.GRACE_ACCESS_MODE = 'drain'
        config.GRACE_ACCESS_EXPIRED_SQUAD_UUID = ''

        overview = await route.get_grace_access_overview(admin=ADMIN, db=empty_db)

        assert [issue.severity for issue in overview.issues if issue.code == 'squad_required'] == ['error']

    @pytest.mark.asyncio
    async def test_unparseable_stored_mode_is_reported_not_fatal(self, monkeypatch, empty_db, status_snapshot, config):
        """Раздел обязан открыться: иначе починить настройку было бы негде."""
        monkeypatch.setattr(bot_configuration_service, 'is_env_locked', lambda _key: False)
        config.GRACE_ACCESS_MODE = 'yes-please'

        overview = await route.get_grace_access_overview(admin=ADMIN, db=empty_db)

        assert overview.config.mode == 'yes-please'
        assert ('mode', 'mode_invalid', 'error') in [
            (issue.field, issue.code, issue.severity) for issue in overview.issues
        ]

    @pytest.mark.asyncio
    async def test_stored_mode_is_folded_like_the_runtime_folds_it(
        self, monkeypatch, empty_db, status_snapshot, config
    ):
        """GraceAccessMode.parse приводит к нижнему регистру, значит 'TRUE' реально работает."""
        monkeypatch.setattr(bot_configuration_service, 'is_env_locked', lambda _key: False)
        config.GRACE_ACCESS_MODE = ' TRUE '

        overview = await route.get_grace_access_overview(admin=ADMIN, db=empty_db)

        assert overview.config.mode == 'true'
        assert [issue.code for issue in overview.issues] == []

    @pytest.mark.asyncio
    async def test_keep_is_a_valid_external_squad(self, monkeypatch, empty_db, status_snapshot, config):
        """'keep' — не UUID, а команда «оставить как есть»; ошибкой она не является."""
        monkeypatch.setattr(bot_configuration_service, 'is_env_locked', lambda _key: False)
        config.GRACE_ACCESS_EXTERNAL_SQUAD_UUID = 'keep'

        overview = await route.get_grace_access_overview(admin=ADMIN, db=empty_db)

        assert overview.issues == []

    @pytest.mark.asyncio
    async def test_keep_is_recognised_regardless_of_case(self, monkeypatch, empty_db, status_snapshot, config):
        """Рантайм сравнивает 'keep' в нижнем регистре — здесь оно не должно считаться кривым UUID."""
        monkeypatch.setattr(bot_configuration_service, 'is_env_locked', lambda _key: False)
        config.GRACE_ACCESS_EXTERNAL_SQUAD_UUID = 'Keep'

        overview = await route.get_grace_access_overview(admin=ADMIN, db=empty_db)

        assert overview.issues == []

    @pytest.mark.asyncio
    async def test_open_sessions_without_a_worker_are_flagged(self, monkeypatch, empty_db, status_snapshot, config):
        """Незакрытые сессии при неактивном режиме остаются с наложенным оверлеем в панели."""
        monkeypatch.setattr(bot_configuration_service, 'is_env_locked', lambda _key: False)
        monkeypatch.setattr(route.grace_access_runtime, '_mode', route.GraceAccessMode.DISABLED, raising=False)
        status_snapshot['open'] = 3

        overview = await route.get_grace_access_overview(admin=ADMIN, db=empty_db)

        assert ('mode', 'open_sessions_stranded', 'warning') in [
            (issue.field, issue.code, issue.severity) for issue in overview.issues
        ]

    @pytest.mark.asyncio
    async def test_restart_required_when_running_mode_differs(self, monkeypatch, empty_db, status_snapshot, config):
        """Режим читается только на старте — сохранённое значение до перезапуска ничего не делает."""
        monkeypatch.setattr(bot_configuration_service, 'is_env_locked', lambda _key: False)
        monkeypatch.setattr(route.grace_access_runtime, '_mode', route.GraceAccessMode.DISABLED, raising=False)
        config.GRACE_ACCESS_MODE = 'true'

        overview = await route.get_grace_access_overview(admin=ADMIN, db=empty_db)

        assert overview.runtime.running_mode == 'false'
        assert overview.runtime.configured_mode == 'true'
        assert overview.runtime.restart_required is True
        assert 'mode' in overview.restart_only

    @pytest.mark.asyncio
    async def test_no_restart_banner_when_modes_match(self, monkeypatch, empty_db, status_snapshot, config):
        monkeypatch.setattr(bot_configuration_service, 'is_env_locked', lambda _key: False)
        monkeypatch.setattr(route.grace_access_runtime, '_mode', route.GraceAccessMode.DISABLED, raising=False)

        overview = await route.get_grace_access_overview(admin=ADMIN, db=empty_db)

        assert overview.runtime.restart_required is False


class TestSquadPicker:
    @pytest.mark.asyncio
    async def test_unreachable_panel_degrades_to_manual_entry(self, monkeypatch):
        """Пустой список и «панель недоступна» — разные вещи: во втором случае поле остаётся ручным."""
        from app.services import remnawave_service

        class _Broken:
            is_configured = True

            def get_api_client(self):
                raise RuntimeError('panel is down')

        monkeypatch.setattr(remnawave_service, 'RemnaWaveService', _Broken)

        response = await route.list_grace_squads(admin=ADMIN)

        assert response.available is False
        assert response.items == []

    @pytest.mark.asyncio
    async def test_unconfigured_panel_is_not_an_error(self, monkeypatch):
        from app.services import remnawave_service

        class _Unconfigured:
            is_configured = False

        monkeypatch.setattr(remnawave_service, 'RemnaWaveService', _Unconfigured)

        response = await route.list_grace_squads(admin=ADMIN)

        assert response.available is False

    @pytest.mark.asyncio
    async def test_panel_with_no_squads_is_not_reported_as_unreachable(self, monkeypatch):
        """get_all_squads глотает ошибки и отдаёт [], поэтому список берётся у клиента напрямую."""
        from app.services import remnawave_service

        class _Empty:
            is_configured = True

            def get_api_client(self):
                return _ApiClient(squads=[])

        monkeypatch.setattr(remnawave_service, 'RemnaWaveService', _Empty)

        response = await route.list_grace_squads(admin=ADMIN)

        assert response.available is True
        assert response.items == []

    @pytest.mark.asyncio
    async def test_squads_without_uuid_are_dropped(self, monkeypatch):
        from app.services import remnawave_service

        class _Panel:
            is_configured = True

            def get_api_client(self):
                return _ApiClient()

        monkeypatch.setattr(remnawave_service, 'RemnaWaveService', _Panel)

        response = await route.list_grace_squads(admin=ADMIN)

        assert response.available is True
        assert [item.uuid for item in response.items] == [VALID_UUID]
        assert response.items[0].members_count == 4
