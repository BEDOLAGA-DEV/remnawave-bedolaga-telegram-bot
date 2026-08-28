"""Роуты кабинета для GeoCheck ноды (Remnawave 3.3.0).

Проверка асинхронная: POST ставит задачу и отдаёт ``job_id``, GET по этому id
отдаёт статус. Здесь закреплены три вещи, которые легко потерять:

  * права: запуск — ``remnawave:manage``, чтение статуса — ``remnawave:read``;
  * ip и interface взаимоисключающи, и оба валидируются на границе (иначе в
    панель уедет мусор из формы);
  * ошибку панели видно админу: на старой панели эндпоинта нет (404), и ответ
    должен называть требуемую версию, а не превращаться в безликое «не вышло».
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.cabinet.routes import admin_remnawave
from app.cabinet.schemas.remnawave import GeocheckRequest
from app.external.remnawave_api import RemnaWaveAPIError


NODE_UUID = '11111111-1111-1111-1111-111111111111'
ADMIN = SimpleNamespace(telegram_id=1)


def _service(**methods):
    service = SimpleNamespace(is_configured=True, configuration_error=None)
    for name, value in methods.items():
        setattr(service, name, value)
    return service


@pytest.fixture
def patched_service(monkeypatch):
    """Подменяет фабрику сервиса в роутере на заранее собранный дубль."""

    def _apply(service):
        monkeypatch.setattr(admin_remnawave, '_get_service', lambda: service)
        return service

    return _apply


# ============== Регистрация и права ==============


def test_geocheck_routes_are_registered(registered_paths) -> None:
    assert 'POST' in registered_paths.get('/cabinet/admin/remnawave/nodes/{node_uuid}/geocheck', set())
    assert 'GET' in registered_paths.get('/cabinet/admin/remnawave/geocheck/{job_id}', set())


@pytest.mark.parametrize(
    ('endpoint_name', 'permission'),
    [
        ('start_node_geocheck', 'remnawave:manage'),
        ('get_node_geocheck', 'remnawave:read'),
    ],
)
def test_geocheck_routes_require_expected_permission(endpoint_name: str, permission: str) -> None:
    endpoint = getattr(admin_remnawave, endpoint_name)
    route = next(route for route in admin_remnawave.router.routes if route.endpoint is endpoint)
    dependency = route.dependant.dependencies[0].call
    closure_values = [cell.cell_contents for cell in dependency.__closure__ or ()]
    assert (permission,) in closure_values


# ============== Валидация входа ==============


def test_geocheck_request_rejects_ip_and_interface_together() -> None:
    with pytest.raises(ValidationError):
        GeocheckRequest(ip='1.2.3.4', interface='ens3')


@pytest.mark.parametrize('value', ['not-an-ip', '999.1.1.1', '1.2.3.4/24', '1.2.3.4 ; rm -rf /'])
def test_geocheck_request_rejects_malformed_ip(value: str) -> None:
    with pytest.raises(ValidationError):
        GeocheckRequest(ip=value)


@pytest.mark.parametrize('value', ['1.2.3.4', '  213.176.77.249  ', '2a0b:4141:820:140d::2'])
def test_geocheck_request_accepts_ipv4_and_ipv6(value: str) -> None:
    assert GeocheckRequest(ip=value).ip == value.strip()


@pytest.mark.parametrize('value', ['bad iface', 'eth0;reboot', 'a' * 33, '../etc'])
def test_geocheck_request_rejects_malformed_interface(value: str) -> None:
    with pytest.raises(ValidationError):
        GeocheckRequest(interface=value)


@pytest.mark.parametrize('value', ['ens3', 'eth0', 'wg0', 'br-lan', 'enp0s31f6'])
def test_geocheck_request_accepts_interface_names(value: str) -> None:
    assert GeocheckRequest(interface=value).interface == value


def test_geocheck_request_blank_strings_mean_default_route() -> None:
    payload = GeocheckRequest(ip='   ', interface='')
    assert payload.ip is None
    assert payload.interface is None


# ============== Запуск проверки ==============


async def test_start_geocheck_returns_job_id(patched_service) -> None:
    service = patched_service(_service(request_node_geocheck=AsyncMock(return_value='job-1')))

    response = await admin_remnawave.start_node_geocheck(NODE_UUID, GeocheckRequest(), admin=ADMIN)

    assert response.job_id == 'job-1'
    service.request_node_geocheck.assert_awaited_once_with(NODE_UUID, ip=None, interface=None)


async def test_start_geocheck_forwards_selected_route(patched_service) -> None:
    service = patched_service(_service(request_node_geocheck=AsyncMock(return_value='job-1')))

    await admin_remnawave.start_node_geocheck(NODE_UUID, GeocheckRequest(interface='ens3'), admin=ADMIN)

    service.request_node_geocheck.assert_awaited_once_with(NODE_UUID, ip=None, interface='ens3')


async def test_start_geocheck_on_old_panel_names_required_version(patched_service) -> None:
    """404 = у панели нет такого эндпоинта; админ должен узнать почему, а не «ошибка»."""
    patched_service(_service(request_node_geocheck=AsyncMock(side_effect=RemnaWaveAPIError('Not found', 404))))

    with pytest.raises(HTTPException) as exc:
        await admin_remnawave.start_node_geocheck(NODE_UUID, GeocheckRequest(), admin=ADMIN)

    assert exc.value.status_code == 400
    assert '3.3.0' in exc.value.detail


async def test_start_geocheck_propagates_panel_error_message(patched_service) -> None:
    patched_service(_service(request_node_geocheck=AsyncMock(side_effect=RemnaWaveAPIError('Node is offline', 400))))

    with pytest.raises(HTTPException) as exc:
        await admin_remnawave.start_node_geocheck(NODE_UUID, GeocheckRequest(), admin=ADMIN)

    assert exc.value.status_code == 400
    assert 'Node is offline' in exc.value.detail


# ============== Опрос результата ==============


async def test_get_geocheck_maps_running_job(patched_service) -> None:
    patched_service(
        _service(
            get_node_geocheck_result=AsyncMock(return_value={'isCompleted': False, 'isFailed': False, 'result': None})
        )
    )

    response = await admin_remnawave.get_node_geocheck('job-1', admin=ADMIN)

    assert (response.job_id, response.is_completed, response.is_failed) == ('job-1', False, False)
    assert response.result is None


async def test_get_geocheck_maps_completed_result_to_snake_case(patched_service) -> None:
    patched_service(
        _service(
            get_node_geocheck_result=AsyncMock(
                return_value={
                    'isCompleted': True,
                    'isFailed': False,
                    'result': {
                        'success': True,
                        'nodeUuid': NODE_UUID,
                        'image': {
                            'format': 'svg',
                            'media_type': 'image/svg+xml',
                            'encoding': 'base64',
                            'data': 'PHN2Zy8+',
                        },
                        'rawReport': {'tool': 'geocheck', 'duration_ms': 10900},
                        'message': None,
                    },
                }
            )
        )
    )

    response = await admin_remnawave.get_node_geocheck('job-1', admin=ADMIN)

    assert response.is_completed is True
    assert response.result.success is True
    assert response.result.node_uuid == NODE_UUID
    assert response.result.image.data == 'PHN2Zy8+'
    assert response.result.raw_report['tool'] == 'geocheck'


async def test_get_geocheck_maps_failed_job(patched_service) -> None:
    patched_service(
        _service(
            get_node_geocheck_result=AsyncMock(
                return_value={
                    'isCompleted': True,
                    'isFailed': True,
                    'result': {
                        'success': False,
                        'nodeUuid': NODE_UUID,
                        'image': None,
                        'rawReport': None,
                        'message': 'node did not answer',
                    },
                }
            )
        )
    )

    response = await admin_remnawave.get_node_geocheck('job-1', admin=ADMIN)

    assert response.is_failed is True
    assert response.result.success is False
    assert response.result.image is None
    assert response.result.message == 'node did not answer'


async def test_get_geocheck_unknown_job_is_404(patched_service) -> None:
    patched_service(_service(get_node_geocheck_result=AsyncMock(side_effect=RemnaWaveAPIError('Not found', 404))))

    with pytest.raises(HTTPException) as exc:
        await admin_remnawave.get_node_geocheck('nope', admin=ADMIN)

    assert exc.value.status_code == 404
