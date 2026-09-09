"""Дата окончания в панели у истёкшей подписки не переписывается «сейчас плюс минута».

Из отчёта: у всех expired и disabled подписок в панели дата стала «Истекла N дней
назад», а после ручной синхронизации из бота — «Истекла минуту назад». В боте даты
при этом прежние: затиралась только копия в панели.

Причина: доступ истёкшей подписке закрывают статусом DISABLED, но вместе со
статусом отправляли и дату — а прошедшую дату панель не принимает, поэтому её
подменяли ближайшим будущим. Настоящая дата окончания при этом терялась.

Правило теперь одно на все точки записи: живой подписке — её дата, истёкшей при
обновлении поле не отправляется вовсе, и только при СОЗДАНИИ аккаунта, где без
даты нельзя, остаётся допустимый минимум.
"""

from __future__ import annotations

import ast
import pathlib
import re
from datetime import UTC, datetime, timedelta

import pytest

from app.services.panel_expiry import panel_expire_at


NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
PAST = NOW - timedelta(days=30)
FUTURE = NOW + timedelta(days=30)


def test_live_subscription_keeps_its_own_date():
    assert panel_expire_at(FUTURE, is_active=True, creating=False, now=NOW) == FUTURE
    assert panel_expire_at(FUTURE, is_active=True, creating=True, now=NOW) == FUTURE


def test_expired_subscription_does_not_touch_the_date_on_update():
    """Главное свойство: поле не отправляется, панель хранит настоящую дату."""
    assert panel_expire_at(PAST, is_active=False, creating=False, now=NOW) is None


def test_new_panel_account_gets_the_real_date_even_if_it_has_passed():
    """При СОЗДАНИИ панель принимает прошедшую дату — выдумывать не надо.

    Запрет «дата не может быть в прошлом» стоит только в UpdateUserCommand;
    в CreateUserCommand у поля нет проверки — одинаково в 3.0.0 и 3.4.3
    (libs/contract/commands/users/*.command.ts). Раньше сюда уходило «сейчас
    плюс минута», и заведённая синхронизацией истёкшая подписка появлялась в
    панели как «истекла минуту назад» вместо своей настоящей даты.
    """
    assert panel_expire_at(PAST, is_active=False, creating=True, now=NOW) == PAST


def test_future_date_survives_even_for_an_inactive_subscription():
    """Заблокированный пользователь с ещё не истёкшей подпиской: дату не занижаем."""
    assert panel_expire_at(FUTURE, is_active=False, creating=True, now=NOW) == FUTURE


def test_blocked_but_not_expired_subscription_still_pushes_its_real_date():
    """Блокировка — не истечение: дата в будущем, панель её примет и должна знать."""
    assert panel_expire_at(FUTURE, is_active=False, creating=False, now=NOW) == FUTURE


# ==================== панель разошлась с ботом ====================

# Отчёт владельца: в панели подписка активна, в боте истекла. Синхронизация
# «из бота в панель» гасила пользователя статусом, а дата окончания в панели
# оставалась прежней — будущей. Панель продолжала показывать живую подписку, и
# настоящая дата из бота туда не попадала никогда.


def test_expired_subscription_extinguishes_a_future_date_in_the_panel():
    """Панель держит будущее — гасим ближайшим допустимым моментом."""
    assert panel_expire_at(PAST, is_active=False, creating=False, now=NOW, panel_current=FUTURE) == NOW + timedelta(
        minutes=1
    )


def test_expired_subscription_leaves_a_past_date_in_the_panel_alone():
    """В панели уже прошлое — это и есть настоящая история, переписывать нечего."""
    panel_says = NOW - timedelta(days=3)

    assert panel_expire_at(PAST, is_active=False, creating=False, now=NOW, panel_current=panel_says) is None


def test_expired_subscription_without_a_known_panel_date_is_left_alone():
    """Что стоит в панели, неизвестно — молчим, как и раньше."""
    assert panel_expire_at(PAST, is_active=False, creating=False, now=NOW, panel_current=None) is None


def test_naive_panel_date_is_read_as_utc():
    """Панель отдаёт UTC; наивное значение нельзя считать локальным временем."""
    naive_future = FUTURE.replace(tzinfo=None)

    assert panel_expire_at(
        PAST, is_active=False, creating=False, now=NOW, panel_current=naive_future
    ) == NOW + timedelta(minutes=1)


# ==================== сторож на все точки записи ====================

WRITERS = (
    'app/services/subscription_service.py',
    'app/services/monitoring_service.py',
    'app/services/remnawave_service.py',
    'app/cabinet/routes/admin_users.py',
    # Согласователь грейса — тоже писатель, и именно его я пропустил в первый
    # раз: он приводит панель к биллинговой реальности раз в минуту.
    'app/services/grace_access_runtime.py',
)

#: Прежняя формула. Любая её копия — это ещё одно место, которое затирает дату.
OLD_FORMULA = re.compile(r'(now|current_time|datetime\.now\(UTC\))\s*\+\s*timedelta\(minutes=1\)')


@pytest.mark.parametrize('path', WRITERS)
def test_no_writer_builds_the_date_by_hand(path):
    source = pathlib.Path(path).read_text(encoding='utf-8')

    assert not OLD_FORMULA.search(source), (
        f'{path}: дата окончания для панели снова считается на месте — правило живёт в app/services/panel_expiry.py'
    )


#: Грейс строит цель сам (у него своя проверка совпадения с панелью), поэтому
#: общий помощник ему не нужен — но считать дату на месте нельзя и ему.
USES_SHARED_RULE = tuple(p for p in WRITERS if p != 'app/services/grace_access_runtime.py')


@pytest.mark.parametrize('path', USES_SHARED_RULE)
def test_every_writer_uses_the_shared_rule(path):
    tree = ast.parse(pathlib.Path(path).read_text(encoding='utf-8'))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == 'app.services.panel_expiry'
        for alias in node.names
    }

    assert 'panel_expire_at' in imported, f'{path} пишет дату в панель мимо общего правила'


# ==================== грейс-доступ ====================

# Полная синхронизация помечает истёкшие подписки кандидатами в грейс, а
# согласователь грейса раз в минуту приводит панель к «биллинговой реальности» —
# и раньше на этом шаге снова проставлял «сейчас плюс минута». Именно поэтому
# после нажатия «Полная синхронизация» дата в панели становилась «истекла минуту
# назад», хотя сама синхронизация панель не пишет.


def _billing_state(**overrides):
    from app.services.grace_access_runtime import GraceBillingState

    base = dict(
        subscription_id=1,
        remnawave_id=42,
        status='expired',
        user_status='active',
        end_at=PAST,
        traffic_limit_bytes=0,
        used_traffic_bytes=0,
        squad_uuids=(),
        external_squad_uuid=None,
        device_limit=None,
    )
    base.update(overrides)
    return GraceBillingState(**base)


def test_billing_target_leaves_the_date_alone_for_disabled():
    from app.services.grace_access_runtime import _build_billing_target

    target = _build_billing_target(_billing_state(), now=NOW)

    assert target.expire_at is None, 'дата отключённой подписки в панели не переписывается'


def test_billing_target_keeps_the_real_date_for_a_live_subscription():
    from app.services.grace_access_runtime import _build_billing_target

    target = _build_billing_target(_billing_state(status='active', end_at=FUTURE), now=NOW)

    assert target.expire_at == FUTURE


def test_restore_target_leaves_the_date_alone_for_disabled():
    from app.services.grace_access_runtime import GracePanelSnapshot, _build_restore_target

    snapshot = GracePanelSnapshot(
        remnawave_id=42,
        status='expired',
        expire_at=PAST,
        traffic_limit_bytes=0,
        used_traffic_bytes=0,
        squad_uuids=(),
        external_squad_uuid=None,
        traffic_is_known=True,
        last_traffic_reset_at=None,
    )

    assert _build_restore_target(snapshot, now=NOW).expire_at is None


def test_payload_without_a_date_does_not_carry_it_from_the_base():
    """Базовый набор собран для другого перехода: оставленная дата затёрла бы настоящую."""
    from app.external.remnawave_api import UserStatus
    from app.services.grace_access_runtime import _PanelTarget, _serialize_panel_target

    target = _PanelTarget(
        status=UserStatus.DISABLED,
        expire_at=None,
        traffic_limit_bytes=0,
        squad_uuids=(),
        external_squad_uuid=None,
    )

    payload = _serialize_panel_target(42, target, base_kwargs={'expire_at': NOW})

    assert 'expire_at' not in payload
