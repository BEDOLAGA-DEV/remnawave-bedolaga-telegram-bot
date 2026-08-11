"""Самолечение при удалённом из панели юзере: PATCH /api/users отвечает
«User not found» (404 / A018 / A063), когда подписка в боте живая, — сервисы
должны пересоздать панель-юзера, а не падать в ошибку (кейс: админ удалил
пользователя из RemnaWave вручную, бот об этом не знает)."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.monitoring_service as monitoring_service_mod
import app.services.subscription_service as subscription_service_mod
from app.config import Settings
from app.database.models import SubscriptionStatus
from app.external.remnawave_api import (
    RemnaWaveAPIError,
    RemnaWaveInvalidUserIdError,
    coerce_panel_user_id,
    is_user_not_found_error,
)
from app.services.monitoring_service import MonitoringService
from app.services.subscription_service import SubscriptionService


# ---- is_user_not_found_error: маркеры «панель-юзера больше нет» ----


def test_not_found_by_status_404():
    assert is_user_not_found_error(RemnaWaveAPIError('User not found', 404, {}))


def test_not_found_by_error_code_without_404():
    # Разные версии RemnaWave отвечают A018/A063 и не всегда со статусом 404
    assert is_user_not_found_error(RemnaWaveAPIError('x', 400, {'errorCode': 'A018'}))
    assert is_user_not_found_error(RemnaWaveAPIError('x', 500, {'errorCode': 'A063'}))


def test_other_errors_are_not_not_found():
    assert not is_user_not_found_error(RemnaWaveAPIError('fk violation', 400, {'errorCode': 'A039'}))
    assert not is_user_not_found_error(RemnaWaveAPIError('internal', 500, {}))
    assert not is_user_not_found_error(RemnaWaveAPIError('не настроен'))  # без статуса и response_data


def test_plain_400_is_not_not_found():
    """3.0.0: user-маршруты параметризованы ``z.coerce.number().positive()``, поэтому
    непригодный идентификатор даёт 400 VALIDATION, а не 404. Ослабление функции до
    «любая 400 = юзера нет» отправляло бы каждый промах идентификатора в ветку
    пересоздания и плодило бы дубли панельных аккаунтов."""
    assert not is_user_not_found_error(RemnaWaveAPIError('Validation failed', 400, {}))
    assert not is_user_not_found_error(RemnaWaveAPIError('Validation failed', 400, {'errorCode': 'A001'}))


def test_invalid_user_id_error_is_never_not_found():
    """``RemnaWaveInvalidUserIdError`` — баг в данных бота (протухший UUID/None в
    колонке), а не «панель потеряла юзера». Даже со статусом 404 в конверте она
    НЕ должна открывать ветку пересоздания."""
    assert not is_user_not_found_error(RemnaWaveInvalidUserIdError('Invalid panel user id: None'))
    assert not is_user_not_found_error(RemnaWaveInvalidUserIdError('x', 404, {'errorCode': 'A018'}))


def test_coerce_panel_user_id_rejects_non_numeric_identifiers():
    """Мусорный идентификатор отсекается на границе клиента и не доходит до сети."""
    assert coerce_panel_user_id(42) == 42
    assert coerce_panel_user_id(' 42 ') == 42  # BigInteger может прийти строкой из FSM/JSON
    # '4_2' и '+42' int() молча превращает в 42 — то есть в id ДРУГОГО
    # пользователя; '²'/'٥' проходят str.isdigit(), но десятичными не являются.
    # Граничную проверку можно только сужать, поэтому все они — ошибка.
    for bad in (
        None,
        '',
        'dead-uuid',
        '3fa85f64-5717-4562-b3fc-2c963f66afa6',
        0,
        -1,
        True,
        '4_2',
        '+42',
        '²',
        '٥',
        '１２',
    ):
        try:
            coerce_panel_user_id(bad)
        except RemnaWaveInvalidUserIdError:
            continue
        raise AssertionError(f'coerce_panel_user_id({bad!r}) должен был бросить RemnaWaveInvalidUserIdError')


# ---- общие фикстуры-строители ----


def _make_user():
    return SimpleNamespace(
        id=1,
        telegram_id=100,
        username='u',
        full_name='User',
        email=None,
        remnawave_id=777,  # 3.0.0: панельный юзер адресуется числовым id
    )


def _make_subscription(*, status=SubscriptionStatus.ACTIVE.value, days_left=10):
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=11,
        user_id=1,
        status=status,
        end_date=now + timedelta(days=days_left),
        traffic_limit_gb=100,
        connected_squads=[],
        tariff=None,
        remnawave_id=None,
        is_trial=False,
        last_webhook_update_at=None,
    )


def _patch_api_client(monkeypatch, service, api):
    @asynccontextmanager
    async def fake_client():
        yield api

    monkeypatch.setattr(service, 'get_api_client', fake_client)


# ---- recreate_deleted_panel_user: гейт «только живые подписки» ----


async def test_recreate_active_delegates_to_create_flow():
    service = SubscriptionService()
    recreated = object()
    service.create_remnawave_user = AsyncMock(return_value=recreated)

    result = await service.recreate_deleted_panel_user(
        AsyncMock(), _make_subscription(), reset_traffic=True, reset_reason='renewal'
    )

    assert result is recreated
    service.create_remnawave_user.assert_awaited_once()
    assert service.create_remnawave_user.await_args.kwargs == {'reset_traffic': True, 'reset_reason': 'renewal'}


async def test_recreate_trial_is_also_alive():
    service = SubscriptionService()
    service.create_remnawave_user = AsyncMock(return_value=object())

    result = await service.recreate_deleted_panel_user(
        AsyncMock(), _make_subscription(status=SubscriptionStatus.TRIAL.value)
    )

    assert result is not None
    service.create_remnawave_user.assert_awaited_once()


async def test_recreate_skips_expired_status():
    """Истёкшую подписку не пересоздаём: админ удалил панель-юзера намеренно."""
    service = SubscriptionService()
    service.create_remnawave_user = AsyncMock()

    result = await service.recreate_deleted_panel_user(
        AsyncMock(), _make_subscription(status=SubscriptionStatus.EXPIRED.value, days_left=-3)
    )

    assert result is None
    service.create_remnawave_user.assert_not_awaited()


async def test_recreate_skips_active_status_with_past_end_date():
    """Статус ACTIVE, но end_date уже прошёл (scheduled job ещё не отработал) — не пересоздаём."""
    service = SubscriptionService()
    service.create_remnawave_user = AsyncMock()

    result = await service.recreate_deleted_panel_user(AsyncMock(), _make_subscription(days_left=-1))

    assert result is None
    service.create_remnawave_user.assert_not_awaited()


# ---- SubscriptionService.update_remnawave_user: рекавери вместо ошибки ----


def _setup_subscription_service(monkeypatch, api):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    service = SubscriptionService()
    monkeypatch.setattr(subscription_service_mod, 'get_user_by_id', AsyncMock(return_value=_make_user()))
    monkeypatch.setattr(subscription_service_mod, 'resolve_hwid_device_limit_for_payload', lambda s: None)
    _patch_api_client(monkeypatch, service, api)
    return service


async def test_update_addresses_panel_by_numeric_id(monkeypatch):
    """3.0.0: гейт «обновлять или создавать» и сам PATCH идут по числовому
    ``users.remnawave_id``. Регресс на remnawave_uuid дал бы 400 VALIDATION и
    (при ослабленной is_user_not_found_error) дубль панельного аккаунта."""
    updated_user = SimpleNamespace(subscription_url='https://sub.example/u', happ_crypto_link=None)
    api = AsyncMock()
    api.update_user.return_value = updated_user
    service = _setup_subscription_service(monkeypatch, api)
    service.create_remnawave_user = AsyncMock()

    db = AsyncMock()
    result = await service.update_remnawave_user(db, _make_subscription())

    assert result is updated_user
    assert api.update_user.await_args.kwargs['user_id'] == 777
    service.create_remnawave_user.assert_not_awaited()


async def test_update_skips_panel_when_no_panel_id(monkeypatch):
    """Пустой ``remnawave_id`` — обновлять нечего: запроса в панель быть не должно
    (иначе клиент отбил бы его RemnaWaveInvalidUserIdError)."""
    api = AsyncMock()
    service = _setup_subscription_service(monkeypatch, api)
    user_without_panel = _make_user()
    user_without_panel.remnawave_id = None
    monkeypatch.setattr(subscription_service_mod, 'get_user_by_id', AsyncMock(return_value=user_without_panel))
    service.create_remnawave_user = AsyncMock()

    result = await service.update_remnawave_user(AsyncMock(), _make_subscription())

    assert result is None
    api.update_user.assert_not_awaited()
    service.create_remnawave_user.assert_not_awaited()


async def test_update_recreates_deleted_panel_user(monkeypatch):
    api = AsyncMock()
    api.update_user.side_effect = RemnaWaveAPIError('User not found', 404, {'errorCode': 'A018'})
    service = _setup_subscription_service(monkeypatch, api)

    recreated = object()
    service.create_remnawave_user = AsyncMock(return_value=recreated)

    result = await service.update_remnawave_user(AsyncMock(), _make_subscription())

    assert result is recreated
    api.update_user.assert_awaited_once()
    service.create_remnawave_user.assert_awaited_once()


async def test_update_does_not_recreate_on_other_api_errors(monkeypatch):
    api = AsyncMock()
    api.update_user.side_effect = RemnaWaveAPIError('internal error', 500, {})
    service = _setup_subscription_service(monkeypatch, api)
    service.create_remnawave_user = AsyncMock()

    result = await service.update_remnawave_user(AsyncMock(), _make_subscription())

    assert result is None
    service.create_remnawave_user.assert_not_awaited()


async def test_update_does_not_recreate_on_invalid_panel_user_id(monkeypatch):
    """Битый локальный идентификатор (клиент отсёк его до сети) — НЕ повод считать,
    что панельного юзера нет: пересоздание завело бы второй аккаунт в панели поверх
    живого, к которому мы просто потеряли ссылку."""
    api = AsyncMock()
    api.update_user.side_effect = RemnaWaveInvalidUserIdError('Invalid panel user id: None')
    service = _setup_subscription_service(monkeypatch, api)
    service.create_remnawave_user = AsyncMock()

    result = await service.update_remnawave_user(AsyncMock(), _make_subscription())

    assert result is None
    service.create_remnawave_user.assert_not_awaited()


async def test_update_does_not_recreate_on_plain_400(monkeypatch):
    """Голая 400 VALIDATION от панели (3.0.0 отвечает так на непригодный id) тоже
    не должна уводить в пересоздание — иначе дубли на каждой покупке."""
    api = AsyncMock()
    api.update_user.side_effect = RemnaWaveAPIError('Validation failed', 400, {})
    service = _setup_subscription_service(monkeypatch, api)
    service.create_remnawave_user = AsyncMock()

    result = await service.update_remnawave_user(AsyncMock(), _make_subscription())

    assert result is None
    service.create_remnawave_user.assert_not_awaited()


async def test_update_recreates_on_a063_without_404(monkeypatch):
    """A063 без статуса 404 — тоже маркер «панель-юзера нет»: пересоздаём."""
    api = AsyncMock()
    api.update_user.side_effect = RemnaWaveAPIError('x', 500, {'errorCode': 'A063'})
    service = _setup_subscription_service(monkeypatch, api)

    recreated = object()
    service.create_remnawave_user = AsyncMock(return_value=recreated)

    result = await service.update_remnawave_user(AsyncMock(), _make_subscription())

    assert result is recreated
    service.create_remnawave_user.assert_awaited_once()


async def test_update_does_not_recreate_on_a039_fk_violation(monkeypatch):
    """A039 — FK violation на externalSquadUuid (панель жива, стейловый сквад),
    а не «юзера нет». Пересоздавать нельзя."""
    api = AsyncMock()
    api.update_user.side_effect = RemnaWaveAPIError('fk violation', 400, {'errorCode': 'A039'})
    service = _setup_subscription_service(monkeypatch, api)
    service.create_remnawave_user = AsyncMock()

    result = await service.update_remnawave_user(AsyncMock(), _make_subscription())

    assert result is None
    service.create_remnawave_user.assert_not_awaited()


async def test_update_does_not_recreate_for_expired_subscription(monkeypatch):
    """Пуш DISABLED в удалённого панель-юзера: молча пропускаем, панель не засоряем."""
    api = AsyncMock()
    api.update_user.side_effect = RemnaWaveAPIError('User not found', 404, {})
    service = _setup_subscription_service(monkeypatch, api)
    service.create_remnawave_user = AsyncMock()

    result = await service.update_remnawave_user(
        AsyncMock(), _make_subscription(status=SubscriptionStatus.EXPIRED.value, days_left=-3)
    )

    assert result is None
    service.create_remnawave_user.assert_not_awaited()


# ---- Гейт «обновлять или создавать»: дубль панельного юзера ----
#
# Самый дорогой промах миграции. `_create_or_update_remnawave_user_multi/_single`
# решают, PATCH-ить существующего панельного юзера или создать нового, по
# truthiness колонки идентичности. Если гейт остался на мёртвой `remnawave_uuid`
# (после 0104 она NULL для всех новых строк), ветка «создать» открывается на
# КАЖДОМ синке живой подписки — панель наполняется дублями, а HWID-лимиты и
# трафик размазываются по двум аккаунтам.
#
# Проверено мутацией: до появления этих тестов подмена гейта на
# `if subscription.remnawave_uuid:` оставляла весь прогон (2791 тест) зелёным.


def _gate_user(*, remnawave_id=777):
    # Отдельный двойник: гейт (в отличие от update_remnawave_user) читает user.status,
    # чтобы выбрать ACTIVE/DISABLED для панели.
    user = _make_user()
    user.status = 'active'
    user.remnawave_id = remnawave_id
    return user


def _gate_subscription(*, remnawave_id, legacy_uuid=None, sub_id=11):
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=sub_id,
        user_id=1,
        status=SubscriptionStatus.ACTIVE.value,
        actual_status=SubscriptionStatus.ACTIVE.value,
        end_date=now + timedelta(days=10),
        traffic_limit_gb=100,
        connected_squads=[],
        tariff=None,
        remnawave_id=remnawave_id,
        # Историческая колонка: после 0104 её читать нельзя. Заполняем как приманку —
        # гейт, соскользнувший на неё, немедленно расходится с remnawave_id.
        remnawave_uuid=legacy_uuid,
        remnawave_short_id='ab12cd',
        is_trial=False,
    )


def _gate_kwargs():
    return dict(user_tag=None, hwid_limit=None, ext_squad_uuid=None, reset_traffic=False, reset_reason=None)


async def test_multi_tariff_gate_updates_existing_panel_user_instead_of_creating_a_duplicate(monkeypatch):
    """Multi-tariff: у подписки есть числовой ``remnawave_id`` → PATCH этого юзера.

    ``remnawave_uuid`` намеренно None: гейт, читающий легаси-колонку, ушёл бы в
    ветку создания и завёл второго панельного юзера поверх живого.
    """
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    existing = SimpleNamespace(id=5001)
    api = AsyncMock()
    api.get_user_by_id.return_value = existing
    api.update_user.return_value = SimpleNamespace(id=5001)

    result = await SubscriptionService()._create_or_update_remnawave_user_multi(
        api,
        _gate_user(),
        _gate_subscription(remnawave_id=5001, legacy_uuid=None),
        **_gate_kwargs(),
    )

    assert result.id == 5001
    api.get_user_by_id.assert_awaited_once_with(5001)
    assert api.update_user.await_args.kwargs['user_id'] == 5001
    api.create_user.assert_not_awaited()


async def test_multi_tariff_gate_ignores_the_legacy_uuid_and_creates_when_no_numeric_id(monkeypatch):
    """Обратное направление: сама по себе легаси-колонка НЕ является привязкой.

    Строка с одним лишь историческим uuid панельного юзера не адресует — её надо
    заводить заново (панельную идентичность восстанавливает бэкфилл, не этот путь).
    """
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    api = AsyncMock()
    api.create_user.return_value = SimpleNamespace(id=6001)

    result = await SubscriptionService()._create_or_update_remnawave_user_multi(
        api,
        _gate_user(),
        _gate_subscription(remnawave_id=None, legacy_uuid='3fa85f64-5717-4562-b3fc-2c963f66afa6'),
        **_gate_kwargs(),
    )

    assert result.id == 6001
    api.get_user_by_id.assert_not_awaited()  # легаси-uuid не отправляем в панель — это 400 VALIDATION
    api.create_user.assert_awaited_once()
    api.update_user.assert_not_awaited()


async def test_multi_tariff_gate_does_not_create_on_invalid_local_panel_id(monkeypatch):
    """Битый локальный id (клиент отбил его до сети) обязан пробросить исключение.

    Проглотить его общим ``except`` — значит уйти в ветку создания и получить
    дубль на каждом проходе синка, ровно то, что запрещает контракт 3.0.0.
    """
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    api = AsyncMock()
    api.get_user_by_id.side_effect = RemnaWaveInvalidUserIdError('Invalid panel user id: dead-uuid')

    service = SubscriptionService()
    try:
        await service._create_or_update_remnawave_user_multi(
            api, _gate_user(), _gate_subscription(remnawave_id=5001), **_gate_kwargs()
        )
    except RemnaWaveInvalidUserIdError:
        pass
    else:
        raise AssertionError('RemnaWaveInvalidUserIdError должна пробрасываться, а не открывать ветку создания')

    api.create_user.assert_not_awaited()


async def test_single_tariff_gate_updates_by_user_panel_id_without_searching(monkeypatch):
    """Single-tariff: привязка живёт в ``users.remnawave_id``.

    При живой привязке поиск по стрим-фильтрам не нужен, а create — тем более.
    """
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    api = AsyncMock()
    api.get_user_by_id.return_value = SimpleNamespace(id=777)
    api.update_user.return_value = SimpleNamespace(id=777)

    result = await SubscriptionService()._create_or_update_remnawave_user_single(
        api, _gate_user(), _gate_subscription(remnawave_id=None), **_gate_kwargs()
    )

    assert result.id == 777
    api.get_user_by_id.assert_awaited_once_with(777)
    assert api.update_user.await_args.kwargs['user_id'] == 777
    api.create_user.assert_not_awaited()
    api.find_users_by_telegram_id.assert_not_awaited()


async def test_single_tariff_gate_falls_back_to_stream_search_before_creating(monkeypatch):
    """Привязки нет → сперва ищем существующего юзера в панели, только потом создаём.

    Маршруты by-telegram-id/by-email в 3.0.0 удалены; их роль играют стрим-фильтры.
    Пропуск этого шага = дубль для каждого юзера, потерявшего локальную привязку.
    """
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    user = _gate_user(remnawave_id=None)
    api = AsyncMock()
    api.find_users_by_telegram_id.return_value = [SimpleNamespace(id=888)]
    api.update_user.return_value = SimpleNamespace(id=888)

    result = await SubscriptionService()._create_or_update_remnawave_user_single(
        api, user, _gate_subscription(remnawave_id=None), **_gate_kwargs()
    )

    assert result.id == 888
    api.find_users_by_telegram_id.assert_awaited_once_with(100)
    assert api.update_user.await_args.kwargs['user_id'] == 888
    api.create_user.assert_not_awaited()


# ---- Репозиторный инвариант: мёртвая колонка не участвует в гейтах ----


# Гейтов «обновлять или создавать» в app/ около тридцати (cabinet-роуты, webapi,
# хендлеры, сервисы), и каждый решает вопрос по truthiness колонки идентичности.
# Покрывать все тридцать поведенческими тестами нереально, а цена промаха —
# дубль панельного пользователя на каждой покупке.
#
# Поэтому пиним сам класс регресса: после 0104 читать ``remnawave_uuid`` вправе
# только два модуля — тот, что её осознанно гасит при мерже аккаунтов, и бэкфилл,
# для которого легаси-uuid и есть входные данные. Любой новый ``getattr(user,
# 'remnawave_uuid')`` или ``subscription.remnawave_uuid`` в гейте роняет этот тест.
#
# Замер до появления пина: подмена пяти cabinet-гейтов на мёртвую колонку
# оставляла весь прогон зелёным.

_LEGACY_COLUMN_READERS_ALLOWED = {
    # Тумбстоун вторичного аккаунта: историческую unique-колонку надо освободить.
    'app/services/account_merge_service.py',
    # Бэкфилл: легаси-uuid — это его ВХОД, по нему и восстанавливается числовой id.
    'app/services/remnawave_identity_backfill.py',
}


def _legacy_column_touch_sites() -> list[str]:
    import ast
    from pathlib import Path

    app_root = Path(__file__).resolve().parents[2] / 'app'
    sites: list[str] = []
    for path in sorted(app_root.rglob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        rel = path.relative_to(app_root.parent).as_posix()

        # Обнуление колонки разрешено где угодно: опасность этого теста —
        # ЧТЕНИЕ мёртвого uuid как гейта «создавать или обновлять», а `= None`
        # наоборот убирает протухшее значение. Оставлять его нельзя: строка с
        # uuid удалённого аккаунта и id нового ломает карту бэкфила.
        cleared: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and node.value.value is None:
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and target.attr == 'remnawave_uuid':
                        cleared.add(id(target))

        for node in ast.walk(tree):
            if id(node) in cleared:
                continue
            touched = isinstance(node, ast.Attribute) and node.attr == 'remnawave_uuid'
            if not touched and isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                # getattr(user, 'remnawave_uuid', None) — имя колонки прячется в литерале
                touched = (
                    node.func.id in ('getattr', 'hasattr', 'setattr')
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value == 'remnawave_uuid'
                )
            if touched:
                sites.append(f'{rel}:{node.lineno}')
    return sites


def test_legacy_panel_uuid_column_is_read_only_by_the_two_modules_that_own_it():
    offenders = sorted(
        {site for site in _legacy_column_touch_sites() if site.rsplit(':', 1)[0] not in _LEGACY_COLUMN_READERS_ALLOWED}
    )

    assert not offenders, (
        'Панельная идентичность в 3.0.0 — числовой remnawave_id. Эти места читают/пишут '
        'мёртвую колонку remnawave_uuid; если это гейт «создавать или обновлять», он уйдёт '
        f'в ветку создания и заведёт дубль панельного юзера: {offenders}'
    )


# ---- MonitoringService.update_remnawave_user: тот же рекавери в рутинном синке ----


def _setup_monitoring_service(monkeypatch, api):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    service = MonitoringService()
    service.subscription_service._config_error = None  # is_configured → True

    monkeypatch.setattr(monitoring_service_mod, 'get_user_by_id', AsyncMock(return_value=_make_user()))
    monkeypatch.setattr(monitoring_service_mod, 'resolve_hwid_device_limit_for_payload', lambda s: None)
    _patch_api_client(monkeypatch, service.subscription_service, api)
    return service


async def test_monitoring_update_happy_path_reaches_panel(monkeypatch):
    """Регрессия: self._gb_to_bytes не существовал у MonitoringService — метод падал
    AttributeError-ом до запроса в панель и молча возвращал None из общего except."""
    updated_user = SimpleNamespace(subscription_url='https://sub.example/u', happ_crypto_link=None)
    api = AsyncMock()
    api.update_user.return_value = updated_user
    service = _setup_monitoring_service(monkeypatch, api)

    db = AsyncMock()
    result = await service.update_remnawave_user(db, _make_subscription())

    assert result is updated_user
    assert api.update_user.await_args.kwargs['traffic_limit_bytes'] == 100 * 1024**3
    # 3.0.0: панель адресуется числовым id пользователя — ровно тем, что лежит
    # в users.remnawave_id (single-tariff), а не short_uuid/vless_uuid.
    assert api.update_user.await_args.kwargs['user_id'] == 777
    db.commit.assert_awaited()


async def test_monitoring_update_recreates_deleted_panel_user(monkeypatch):
    api = AsyncMock()
    api.update_user.side_effect = RemnaWaveAPIError('User not found', 404, {'errorCode': 'A063'})
    service = _setup_monitoring_service(monkeypatch, api)

    recreated = object()
    service.subscription_service.recreate_deleted_panel_user = AsyncMock(return_value=recreated)

    result = await service.update_remnawave_user(AsyncMock(), _make_subscription())

    assert result is recreated
    api.update_user.assert_awaited_once()
    service.subscription_service.recreate_deleted_panel_user.assert_awaited_once()


async def test_monitoring_update_does_not_recreate_on_other_api_errors(monkeypatch):
    api = AsyncMock()
    api.update_user.side_effect = RemnaWaveAPIError('internal error', 500, {})
    service = _setup_monitoring_service(monkeypatch, api)

    service.subscription_service.recreate_deleted_panel_user = AsyncMock()

    result = await service.update_remnawave_user(AsyncMock(), _make_subscription())

    assert result is None
    service.subscription_service.recreate_deleted_panel_user.assert_not_awaited()


async def test_monitoring_update_does_not_recreate_on_invalid_panel_user_id(monkeypatch):
    """Тот же контракт в рутинном синке: битый локальный id — не «юзера нет».
    Уход в пересоздание здесь особенно дорог: монитор проходит по всем подпискам
    и наплодил бы дубль на каждой битой ссылке."""
    api = AsyncMock()
    api.update_user.side_effect = RemnaWaveInvalidUserIdError('Invalid panel user id: None')
    service = _setup_monitoring_service(monkeypatch, api)

    service.subscription_service.recreate_deleted_panel_user = AsyncMock()

    result = await service.update_remnawave_user(AsyncMock(), _make_subscription())

    assert result is None
    service.subscription_service.recreate_deleted_panel_user.assert_not_awaited()


async def test_monitoring_update_does_not_recreate_on_plain_400(monkeypatch):
    api = AsyncMock()
    api.update_user.side_effect = RemnaWaveAPIError('Validation failed', 400, {})
    service = _setup_monitoring_service(monkeypatch, api)

    service.subscription_service.recreate_deleted_panel_user = AsyncMock()

    result = await service.update_remnawave_user(AsyncMock(), _make_subscription())

    assert result is None
    service.subscription_service.recreate_deleted_panel_user.assert_not_awaited()


async def test_monitoring_update_recreates_on_a018_without_404(monkeypatch):
    """A018 (без статуса 404) — маркер удалённого панель-юзера: пересоздаём."""
    api = AsyncMock()
    api.update_user.side_effect = RemnaWaveAPIError('x', 500, {'errorCode': 'A018'})
    service = _setup_monitoring_service(monkeypatch, api)

    recreated = object()
    service.subscription_service.recreate_deleted_panel_user = AsyncMock(return_value=recreated)

    result = await service.update_remnawave_user(AsyncMock(), _make_subscription())

    assert result is recreated
    service.subscription_service.recreate_deleted_panel_user.assert_awaited_once()


# ---- Апгрейд на 3.0.0: строка ещё без числового id, но с живым shortUuid ----
#
# Между `alembic upgrade head` и прогоном бэкфила у КАЖДОЙ старой строки
# remnawave_id IS NULL, а remnawave_short_uuid заполнен. Все три места ниже
# раньше трактовали это как «панельного юзера нет» — и заводили дубль либо
# уничтожали ключ восстановления.


def _user_for_multi():
    """`_make_user` не несёт status — мульти-ветка его читает."""
    user = _make_user()
    user.status = 'active'
    return user


def _sub_for_multi(sub=None):
    """Мульти-ветка читает actual_status (в модели это property)."""
    sub = sub or _make_subscription()
    sub.actual_status = sub.status
    return sub


def _sub_awaiting_backfill():
    sub = _sub_for_multi()
    sub.remnawave_id = None
    sub.remnawave_short_uuid = 'aBcD12'
    sub.remnawave_short_id = 'sid11'
    sub.subscription_url = 'https://sub.example/aBcD12'
    sub.subscription_crypto_link = ''
    return sub


async def test_multi_tariff_adopts_panel_user_by_short_uuid_instead_of_duplicating(monkeypatch):
    """Гейт создания читает только remnawave_id; до бэкфила он пуст у всех строк.

    Без подхвата по shortUuid каждое продление заводило бы ВТОРОЙ панельный
    аккаунт, а оплаченный оригинал осиротел бы, продолжая занимать лицензию.
    """
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    existing = SimpleNamespace(id=8812, subscription_url='https://sub.example/aBcD12', happ_crypto_link=None)
    api = AsyncMock()
    api.get_user_by_short_uuid.return_value = existing
    api.update_user.return_value = existing

    service = SubscriptionService()
    sub = _sub_awaiting_backfill()

    result = await service._create_or_update_remnawave_user_multi(
        api,
        _user_for_multi(),
        sub,
        user_tag=None,
        hwid_limit=None,
        ext_squad_uuid=None,
        reset_traffic=False,
        reset_reason=None,
    )

    api.create_user.assert_not_awaited()
    api.get_user_by_short_uuid.assert_awaited_once_with('aBcD12')
    assert api.update_user.await_args.kwargs['user_id'] == 8812
    assert sub.remnawave_id == 8812
    assert result is existing


async def test_multi_tariff_still_creates_when_panel_does_not_know_the_short_uuid(monkeypatch):
    """Обратная сторона: подхват не должен блокировать законное создание."""
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    created = SimpleNamespace(id=9001, subscription_url='https://sub.example/new', happ_crypto_link=None)
    api = AsyncMock()
    api.get_user_by_short_uuid.return_value = None
    api.create_user.return_value = created

    service = SubscriptionService()
    result = await service._create_or_update_remnawave_user_multi(
        api,
        _user_for_multi(),
        _sub_awaiting_backfill(),
        user_tag=None,
        hwid_limit=None,
        ext_squad_uuid=None,
        reset_traffic=False,
        reset_reason=None,
    )

    api.create_user.assert_awaited_once()
    assert result is created


async def test_transient_panel_failure_never_creates_a_duplicate(monkeypatch):
    """Таймаут панели — это не «юзера нет».

    Раньше `except Exception` уводил транзиентную ошибку в ветку создания:
    панель моргнула, POST прошёл на ретрае — и рядом с живым аккаунтом появился
    второй.
    """
    from app.external.remnawave_api import RemnaWaveTransientError

    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    api = AsyncMock()
    api.get_user_by_id.side_effect = RemnaWaveTransientError('Request timed out')

    sub = _sub_for_multi()
    sub.remnawave_id = 8812

    service = SubscriptionService()
    with pytest.raises(RemnaWaveTransientError):
        await service._create_or_update_remnawave_user_multi(
            api,
            _user_for_multi(),
            sub,
            user_tag=None,
            hwid_limit=None,
            ext_squad_uuid=None,
            reset_traffic=False,
            reset_reason=None,
        )

    api.create_user.assert_not_awaited()


# ---- validate_and_clean_subscription: сохранить или уничтожить ключ восстановления ----
#
# Самое разрушительное решение во всей миграции. `remnawave_short_uuid` —
# единственный точный ключ, которым бэкфил восстанавливает связь строки с
# панельным аккаунтом. Ветка «есть short_uuid, нет числового id» после 0104
# истинна у КАЖДОЙ старой строки, поэтому ошибка здесь делает их неразрешимыми
# навсегда — и все тесты подхвата (fix B) при этом остаются зелёными, потому что
# валидация отрабатывает раньше и ключа к их моменту уже нет.


def _validation_subject():
    sub = _make_subscription()
    sub.remnawave_id = None
    sub.remnawave_short_uuid = 'aBcD12'
    sub.subscription_url = 'https://sub.example/aBcD12'
    sub.subscription_crypto_link = 'happ://crypt4/x'
    user = _make_user()
    user.remnawave_id = None
    return sub, user


async def test_validation_adopts_panel_id_and_keeps_the_recovery_key(monkeypatch):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    api = AsyncMock()
    api.get_user_by_short_uuid.return_value = SimpleNamespace(id=8812)
    service = SubscriptionService()
    _patch_api_client(monkeypatch, service, api)
    sub, user = _validation_subject()
    db = AsyncMock()
    # Никакая другая подписка этот аккаунт не держит — защита индекса пропускает.
    # У AsyncMock дочерние атрибуты тоже асинхронные, поэтому результат execute
    # подменяем обычным моком: `scalar_one_or_none()` вызывается без await.
    db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

    assert await service.validate_and_clean_subscription(db, sub, user) is True

    assert sub.remnawave_id == 8812
    assert user.remnawave_id == 8812
    assert sub.remnawave_short_uuid == 'aBcD12', 'ключ восстановления обязан уцелеть'
    assert sub.subscription_url == 'https://sub.example/aBcD12'


async def test_validation_cleans_only_when_the_panel_denies_the_short_uuid(monkeypatch):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    api = AsyncMock()
    api.get_user_by_short_uuid.return_value = None  # 404 — аккаунта действительно нет
    service = SubscriptionService()
    _patch_api_client(monkeypatch, service, api)
    sub, user = _validation_subject()
    db = AsyncMock()

    assert await service.validate_and_clean_subscription(db, sub, user) is True

    assert sub.remnawave_short_uuid is None
    assert sub.subscription_url == ''


async def test_validation_never_cleans_on_a_panel_error(monkeypatch):
    """Таймаут — не доказательство отсутствия аккаунта.

    Очистка здесь означала бы массовую безвозвратную расстыковку всех
    непробэкфиленных строк, которых коснулось создание, пока панель деградирует.
    """
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    api = AsyncMock()
    api.get_user_by_short_uuid.side_effect = RemnaWaveAPIError('boom', 503, {})
    service = SubscriptionService()
    _patch_api_client(monkeypatch, service, api)
    sub, user = _validation_subject()
    db = AsyncMock()

    assert await service.validate_and_clean_subscription(db, sub, user) is False

    assert sub.remnawave_short_uuid == 'aBcD12'
    assert sub.subscription_url == 'https://sub.example/aBcD12'


async def test_adoption_does_not_swallow_a_non_404_panel_error(monkeypatch):
    """Только 404 доказывает, что аккаунта нет.

    Проглотить 503/429/таймаут и вернуть None здесь значит уйти в ветку
    создания: панель через несколько секунд уже поднялась, POST проходит — и
    рядом с живым оплаченным аккаунтом появляется дубль, а shortUuid оригинала
    затирается новым.
    """
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    api = AsyncMock()
    api.get_user_by_short_uuid.side_effect = RemnaWaveAPIError('Service Unavailable', 503, {})

    service = SubscriptionService()
    with pytest.raises(RemnaWaveAPIError):
        await service._create_or_update_remnawave_user_multi(
            api,
            _user_for_multi(),
            _sub_awaiting_backfill(),
            user_tag=None,
            hwid_limit=None,
            ext_squad_uuid=None,
            reset_traffic=False,
            reset_reason=None,
        )

    api.create_user.assert_not_awaited()


# ---- update_remnawave_user: платный апдейт не должен исчезать в окне бэкфила ----


async def test_update_adopts_panel_id_by_short_uuid_instead_of_giving_up(monkeypatch):
    """Между 0104 и бэкфилом `remnawave_id` пуст у всех доапгрейдных строк.

    Вызывающие `update_remnawave_user` (докупка трафика и устройств, смена
    сквадов) результат НЕ проверяют: раньше метод возвращал None, деньги уже
    были списаны и закоммичены, а в панель не уезжало ничего.
    """
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    updated = SimpleNamespace(subscription_url='https://s/x', happ_crypto_link=None)
    api = AsyncMock()
    api.get_user_by_short_uuid.return_value = SimpleNamespace(id=8812)
    api.update_user.return_value = updated

    service = SubscriptionService()
    _patch_api_client(monkeypatch, service, api)
    monkeypatch.setattr(subscription_service_mod, 'get_user_by_id', AsyncMock(return_value=_make_user()))

    sub = _sub_for_multi()
    sub.remnawave_id = None
    sub.remnawave_short_uuid = 'aBcD12'

    db = AsyncMock()
    # Аккаунт свободен — защита частично-уникального индекса пропускает запись.
    db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    result = await service.update_remnawave_user(db, sub)

    assert result is updated
    api.get_user_by_short_uuid.assert_awaited_once_with('aBcD12')
    assert sub.remnawave_id == 8812, 'подхваченный id обязан сохраниться на строке'
    assert api.update_user.await_args.kwargs['user_id'] == 8812


async def test_update_still_gives_up_when_the_panel_does_not_know_the_short_uuid(monkeypatch):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    api = AsyncMock()
    api.get_user_by_short_uuid.return_value = None

    service = SubscriptionService()
    _patch_api_client(monkeypatch, service, api)
    monkeypatch.setattr(subscription_service_mod, 'get_user_by_id', AsyncMock(return_value=_make_user()))

    sub = _sub_for_multi()
    sub.remnawave_id = None
    sub.remnawave_short_uuid = 'gone'

    assert await service.update_remnawave_user(AsyncMock(), sub) is None
    api.update_user.assert_not_awaited()


async def test_update_gives_up_when_the_panel_is_unreachable(monkeypatch):
    """Транзиент — не повод выдумать идентичность и записать её на строку."""
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    api = AsyncMock()
    api.get_user_by_short_uuid.side_effect = RemnaWaveAPIError('Bad Gateway', 502, {})

    service = SubscriptionService()
    _patch_api_client(monkeypatch, service, api)
    monkeypatch.setattr(subscription_service_mod, 'get_user_by_id', AsyncMock(return_value=_make_user()))

    sub = _sub_for_multi()
    sub.remnawave_id = None
    sub.remnawave_short_uuid = 'aBcD12'

    assert await service.update_remnawave_user(AsyncMock(), sub) is None
    api.update_user.assert_not_awaited()
    assert sub.remnawave_id is None, 'битую идентичность записывать нельзя'


async def test_single_mode_prefers_exact_keys_over_ambiguous_telegram_search(monkeypatch):
    """Точный ключ обязан побеждать неоднозначный поиск по telegramId.

    Бэкфилл оставляет `users.remnawave_id` пустым РОВНО потому, что телеграм-поиск
    вернул несколько аккаунтов. Если после этого синк пойдёт тем же телеграм-поиском
    и возьмёт первый попавшийся, он запишет в чужой аккаунт — хотя shortUuid и
    `subscriptions.remnawave_id` называют нужный точно.
    """
    user = _make_user()
    user.remnawave_id = None  # состояние после «частичного» бэкфила
    user.status = 'active'
    subscription = _make_subscription()
    subscription.remnawave_short_uuid = 'exact'
    subscription.actual_status = SubscriptionStatus.ACTIVE.value
    subscription.traffic_used_gb = 0
    subscription.device_limit = None
    subscription.subscription_url = ''
    subscription.subscription_crypto_link = ''

    right = SimpleNamespace(id=555, short_uuid='exact', subscription_url='https://s/ok', happ_crypto_link=None)
    wrong_a = SimpleNamespace(id=901, short_uuid='a', subscription_url='https://s/a', happ_crypto_link=None)
    wrong_b = SimpleNamespace(id=902, short_uuid='b', subscription_url='https://s/b', happ_crypto_link=None)

    api = AsyncMock()
    api.get_user_by_short_uuid.return_value = right
    api.find_users_by_telegram_id.return_value = [wrong_a, wrong_b]
    api.update_user.return_value = right

    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    service = SubscriptionService()
    monkeypatch.setattr(subscription_service_mod, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(subscription_service_mod, 'resolve_hwid_device_limit_for_payload', lambda s: None)
    _patch_api_client(monkeypatch, service, api)

    async with service.get_api_client() as client:
        await service._create_or_update_remnawave_user_single(
            client,
            user,
            subscription,
            user_tag=None,
            hwid_limit=None,
            ext_squad_uuid=None,
            reset_traffic=False,
            reset_reason=None,
        )

    assert api.update_user.await_count == 1
    assert api.update_user.await_args.kwargs['user_id'] == 555, (
        f'записали в аккаунт {api.update_user.await_args.kwargs["user_id"]} вместо точного 555'
    )


async def test_single_mode_uses_the_subscription_own_panel_id(monkeypatch):
    """`subscriptions.remnawave_id` — тоже точный адрес, и в single-tariff его не спрашивали.

    Бэкфилл заполняет эту колонку и в однотарифном режиме (например, перенося id
    на живую строку), тогда как `users.remnawave_id` мог остаться пустым. Без
    этого источника синк уходил в телеграм-поиск и мог записать в чужой аккаунт.
    """
    user = _make_user()
    user.remnawave_id = None
    user.status = 'active'
    subscription = _make_subscription()
    subscription.remnawave_id = 606
    subscription.remnawave_short_uuid = None
    subscription.actual_status = SubscriptionStatus.ACTIVE.value
    subscription.traffic_used_gb = 0
    subscription.device_limit = None
    subscription.subscription_url = ''
    subscription.subscription_crypto_link = ''

    right = SimpleNamespace(id=606, short_uuid='s606', subscription_url='https://s/ok', happ_crypto_link=None)
    api = AsyncMock()
    api.get_user_by_id.return_value = right
    api.find_users_by_telegram_id.return_value = [
        SimpleNamespace(id=901, short_uuid='a', subscription_url='https://s/a', happ_crypto_link=None)
    ]
    api.update_user.return_value = right

    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    service = SubscriptionService()
    monkeypatch.setattr(subscription_service_mod, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(subscription_service_mod, 'resolve_hwid_device_limit_for_payload', lambda s: None)
    _patch_api_client(monkeypatch, service, api)

    async with service.get_api_client() as client:
        await service._create_or_update_remnawave_user_single(
            client,
            user,
            subscription,
            user_tag=None,
            hwid_limit=None,
            ext_squad_uuid=None,
            reset_traffic=False,
            reset_reason=None,
        )

    api.find_users_by_telegram_id.assert_not_awaited()
    assert api.update_user.await_args.kwargs['user_id'] == 606


@pytest.mark.asyncio
async def test_create_does_not_break_on_the_partial_unique_index(monkeypatch):
    """Соседняя подписка уже держит этот панельный аккаунт — запись обязана уступить.

    `uq_subscriptions_remnawave_id` частично уникален, а в single-tariff все
    подписки одного человека адресуют ОДИН аккаунт — ровно то состояние, которое
    штатно оставляет бэкфилл. Безусловная запись давала IntegrityError уже ПОСЛЕ
    успешного PATCH в панель, и откат уносил вместе с собой оплату.
    """
    from app.database.models import Subscription as SubModel, User as UserModel
    from tests.fixtures.sqlite_memory import memory_session

    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)

    async with memory_session(monkeypatch, [UserModel.__table__, SubModel.__table__]) as db:
        db.add(UserModel(id=1, telegram_id=100, status='active'))
        now = datetime.now(UTC)
        db.add(
            SubModel(
                id=10,
                user_id=1,
                status='active',
                end_date=now + timedelta(days=5),
                remnawave_id=77,
                remnawave_short_id='sid10',
            )
        )
        db.add(
            SubModel(
                id=20,
                user_id=1,
                status='active',
                end_date=now + timedelta(days=5),
                remnawave_short_id='sid20',
            )
        )
        await db.commit()

        service = SubscriptionService()
        target = await db.get(SubModel, 20)
        updated = SimpleNamespace(id=77, short_uuid='s77', subscription_url='https://s/u', happ_crypto_link=None)

        monkeypatch.setattr(
            subscription_service_mod, 'get_user_by_id', AsyncMock(return_value=await db.get(UserModel, 1))
        )
        monkeypatch.setattr(subscription_service_mod, 'resolve_hwid_device_limit_for_payload', lambda s: None)
        monkeypatch.setattr(service, 'validate_and_clean_subscription', AsyncMock(return_value=True))
        monkeypatch.setattr(service, '_create_or_update_remnawave_user_single', AsyncMock(return_value=updated))
        monkeypatch.setattr(service, '_create_or_update_remnawave_user_multi', AsyncMock(return_value=updated))
        _patch_api_client(monkeypatch, service, AsyncMock())

        result = await service.create_remnawave_user(db, target)

        assert result is updated, 'создание не должно падать из-за занятого соседом id'
        db.expunge_all()
        assert (await db.get(SubModel, 20)).remnawave_id is None, 'нельзя дублировать id второй строке'
        assert (await db.get(SubModel, 10)).remnawave_id == 77, 'у соседа id забирать тоже нельзя'
        assert (await db.get(UserModel, 1)).remnawave_id == 77, 'адресация остаётся через пользователя'


async def test_stale_panel_id_still_gets_rescued_by_short_uuid(monkeypatch):
    """Протухший числовой id не должен отменять спасение по shortUuid.

    Гейт спасения раньше зависел от «id не было вовсе». Стоило добавить фолбэк
    `user.remnawave_id or subscription.remnawave_id`, как строка с НЕВЕРНЫМ id
    (панель его не знает) теряла последний шанс: очистка стирала
    `remnawave_short_uuid` — единственный точный ключ восстановления связи, —
    хотя панель по нему аккаунт прекрасно находит.
    """
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    api = AsyncMock()
    api.get_user_by_id.return_value = None  # id протух: панель его не знает
    api.get_user_by_short_uuid.return_value = SimpleNamespace(id=8812)
    service = SubscriptionService()
    _patch_api_client(monkeypatch, service, api)

    sub, user = _validation_subject()
    sub.remnawave_id = 4242
    user.remnawave_id = None
    db = AsyncMock()
    db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

    assert await service.validate_and_clean_subscription(db, sub, user) is True

    api.get_user_by_short_uuid.assert_awaited_once()
    assert sub.remnawave_short_uuid == 'aBcD12', 'ключ восстановления обязан уцелеть'
    assert sub.remnawave_id == 8812, 'строку надо перепривязать к найденному аккаунту'


async def test_short_uuid_adoption_respects_the_partial_unique_index(monkeypatch):
    """Привязка по shortUuid обязана уступить, если аккаунт держит соседняя строка.

    Это второй писатель в частично-уникальную `subscriptions.remnawave_id`, и
    защиту к нему сначала не применили. После бэкфила состояние «сосед держит
    тот же аккаунт» штатно: в single-tariff все подписки человека адресуют один
    панельный аккаунт. Безусловная запись падала бы на IntegrityError.
    """
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    api = AsyncMock()
    api.get_user_by_short_uuid.return_value = SimpleNamespace(id=8812)
    service = SubscriptionService()
    _patch_api_client(monkeypatch, service, api)

    sub, user = _validation_subject()
    db = AsyncMock()
    # Аккаунт 8812 уже держит подписка #10 того же человека.
    db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=10))

    assert await service.validate_and_clean_subscription(db, sub, user) is True

    assert sub.remnawave_id is None, 'нельзя дублировать id второй строке'
    assert user.remnawave_id == 8812, 'адресация остаётся через пользователя'
    assert sub.remnawave_short_uuid == 'aBcD12', 'ключ восстановления обязан уцелеть'


async def test_degraded_short_uuid_endpoint_does_not_abort_a_resolvable_sync(monkeypatch):
    """Деградация точного ключа не должна ронять то, что решается другим ключом.

    Проброс любого не-404 из `_adopt_panel_user_by_short_uuid` был безопасен,
    пока шаг стоял последним. Подняв его вперёд ради точности, мы сделали 5xx на
    этом одном эндпоинте фатальным для всех строк без числового id — то есть для
    всех доапгрейдных строк до конца бэкфила.
    """
    user = _make_user()
    user.remnawave_id = None
    user.status = 'active'
    subscription = _make_subscription()
    subscription.remnawave_short_uuid = 'exact'
    subscription.actual_status = SubscriptionStatus.ACTIVE.value
    subscription.traffic_used_gb = 0
    subscription.device_limit = None
    subscription.subscription_url = ''
    subscription.subscription_crypto_link = ''

    only = SimpleNamespace(id=555, short_uuid='exact', subscription_url='https://s/ok', happ_crypto_link=None)
    api = AsyncMock()
    api.get_user_by_short_uuid.side_effect = RemnaWaveAPIError('panel restarting', 503, {})
    api.find_users_by_telegram_id.return_value = [only]
    api.update_user.return_value = only

    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    service = SubscriptionService()
    monkeypatch.setattr(subscription_service_mod, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(subscription_service_mod, 'resolve_hwid_device_limit_for_payload', lambda s: None)
    _patch_api_client(monkeypatch, service, api)

    async with service.get_api_client() as client:
        await service._create_or_update_remnawave_user_single(
            client,
            user,
            subscription,
            user_tag=None,
            hwid_limit=None,
            ext_squad_uuid=None,
            reset_traffic=False,
            reset_reason=None,
        )

    assert api.update_user.await_args.kwargs['user_id'] == 555


async def test_degraded_short_uuid_endpoint_still_refuses_to_create_a_duplicate(monkeypatch):
    """Но если больше НИЧЕМ не опознали — создавать нельзя, надо честно упасть."""
    user = _make_user()
    user.remnawave_id = None
    user.status = 'active'
    user.telegram_id = None
    user.email = None
    subscription = _make_subscription()
    subscription.remnawave_short_uuid = 'exact'
    subscription.actual_status = SubscriptionStatus.ACTIVE.value
    subscription.traffic_used_gb = 0
    subscription.device_limit = None
    subscription.subscription_url = ''
    subscription.subscription_crypto_link = ''

    api = AsyncMock()
    api.get_user_by_short_uuid.side_effect = RemnaWaveAPIError('panel restarting', 503, {})

    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    service = SubscriptionService()
    monkeypatch.setattr(subscription_service_mod, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(subscription_service_mod, 'resolve_hwid_device_limit_for_payload', lambda s: None)
    _patch_api_client(monkeypatch, service, api)

    with pytest.raises(RemnaWaveAPIError):
        async with service.get_api_client() as client:
            await service._create_or_update_remnawave_user_single(
                client,
                user,
                subscription,
                user_tag=None,
                hwid_limit=None,
                ext_squad_uuid=None,
                reset_traffic=False,
                reset_reason=None,
            )

    api.create_user.assert_not_awaited()


async def test_foreign_account_is_cleaned_not_re_anchored(monkeypatch):
    """Несовпадение владельца обязано вести в очистку, а не в перепривязку.

    `needs_cleanup` ставят ДВА разных условия: «панель не знает id» и «аккаунт
    принадлежит другому telegram_id». Стоило завязать спасение по shortUuid на
    общий флаг, как второй случай начал уходить в перепривязку: панель знает
    shortUuid — значит «нашли», — и бот оставлял себе ЧУЖОЙ аккаунт.
    """
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    foreign = SimpleNamespace(id=777, telegram_id=999)
    api = AsyncMock()
    api.get_user_by_id.return_value = foreign  # панель id знает...
    api.get_user_by_short_uuid.return_value = foreign  # ...и shortUuid тоже
    service = SubscriptionService()
    _patch_api_client(monkeypatch, service, api)

    sub, user = _validation_subject()
    sub.remnawave_id = 777
    user.remnawave_id = 777
    user.telegram_id = 100  # а аккаунт принадлежит 999
    db = AsyncMock()
    db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

    assert await service.validate_and_clean_subscription(db, sub, user) is True

    assert sub.remnawave_id is None, 'чужой аккаунт нельзя оставлять привязанным'
    assert sub.remnawave_short_uuid is None, 'ключ на чужой аккаунт тоже держать нельзя'


async def test_short_uuid_rescue_refuses_a_foreign_account(monkeypatch):
    """И сама перепривязка обязана проверять владельца.

    Даже когда повод законный (панель забыла числовой id), найденный по
    shortUuid аккаунт может принадлежать другому человеку — привязывать его
    нельзя, иначе проверка владельца обходится с другой стороны.
    """
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    api = AsyncMock()
    api.get_user_by_id.return_value = None  # id протух
    api.get_user_by_short_uuid.return_value = SimpleNamespace(id=8812, telegram_id=999)
    service = SubscriptionService()
    _patch_api_client(monkeypatch, service, api)

    sub, user = _validation_subject()
    sub.remnawave_id = 4242
    user.remnawave_id = None
    user.telegram_id = 100
    db = AsyncMock()
    db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

    assert await service.validate_and_clean_subscription(db, sub, user) is True

    assert sub.remnawave_id is None
    assert user.remnawave_id is None, 'чужой аккаунт нельзя записывать и на пользователя'


async def test_ownership_mismatch_is_not_rescued_by_a_telegram_less_account(monkeypatch):
    """Несовпадение владельца нельзя «спасать» аккаунтом без telegramId.

    Внутренняя проверка владельца молчит, когда у панельного аккаунта telegramId
    пуст, — поэтому решает внешний гейт: перепривязка допустима только когда
    панель ЗАБЫЛА id, а не когда она вернула чужой аккаунт. Иначе строка с
    доказанным несовпадением остаётся привязанной.
    """
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    api = AsyncMock()
    api.get_user_by_id.return_value = SimpleNamespace(id=777, telegram_id=999)  # чужой
    api.get_user_by_short_uuid.return_value = SimpleNamespace(id=8812, telegram_id=None)
    service = SubscriptionService()
    _patch_api_client(monkeypatch, service, api)

    sub, user = _validation_subject()
    sub.remnawave_id = 777
    user.remnawave_id = 777
    user.telegram_id = 100
    db = AsyncMock()
    db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

    assert await service.validate_and_clean_subscription(db, sub, user) is True

    api.get_user_by_short_uuid.assert_not_awaited()
    assert sub.remnawave_id is None
    assert sub.remnawave_short_uuid is None


async def test_adoption_refuses_when_a_sibling_owns_the_account(monkeypatch):
    """Занятый аккаунт — повод отменить операцию, а не только пропустить запись.

    Возвращённый id вызывающий тут же отправляет в панель PATCH-ом. Если
    аккаунт держит соседняя подписка, такой PATCH перепишет ЕЁ срок, лимиты и
    сквады — и это необратимо, в отличие от отката транзакции. Поэтому
    `_adopt_panel_id_for_update` обязан вернуть None.
    """
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    api = AsyncMock()
    api.get_user_by_short_uuid.return_value = SimpleNamespace(id=8812)
    service = SubscriptionService()
    _patch_api_client(monkeypatch, service, api)

    sub, user = _validation_subject()
    db = AsyncMock()
    # Аккаунт 8812 уже держит подписка #99 того же человека.
    db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=99))

    result = await service._adopt_panel_id_for_update(db, sub, user, True)

    assert result is None, 'операция должна быть отменена целиком'
    assert sub.remnawave_id is None
    api.update_user.assert_not_awaited()
