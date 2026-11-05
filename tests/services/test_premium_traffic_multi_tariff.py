"""Премиум-трафик в режиме мультиподписок.

В этом режиме у одного человека несколько подписок, и **у каждой свой аккаунт в
панели** — имя строится с суффиксом подписки именно ради этого. Значит и расход
у них раздельный, и снятие сквада на одной подписке не должно задевать другую.

Отдельный набор, потому что ошибиться здесь легко и незаметно: в обычном режиме
подписка одна, и подстановка «не того» идентификатора даёт тот же результат.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

from app.database.crud.premium_traffic import get_limited_squad_uuids, get_or_create_state
from app.database.models import SubscriptionPremiumTraffic
from app.services.premium_traffic_service import PremiumTrafficService
from app.utils.premium_traffic import BYTES_IN_GB, effective_panel_squads
from tests.fixtures.sqlite_memory import memory_session


TABLES = (SubscriptionPremiumTraffic.__table__,)

SQUAD = 'e4f819ca-2cfd-4425-9354-16a262b180c1'
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def _subscription(sub_id: int, panel_id: int | None, user_panel_id: int | None = 999):
    return SimpleNamespace(
        id=sub_id,
        remnawave_id=panel_id,
        user=SimpleNamespace(remnawave_id=user_panel_id),
    )


def _multi(enabled: bool):
    return SimpleNamespace(is_multi_tariff_enabled=lambda: enabled)


class TestPanelAccountPerSubscription:
    def test_multi_tariff_takes_the_subscription_account(self, monkeypatch):
        """У каждой подписки свой аккаунт: подстановка пользовательского увела бы
        расход и снятие сквада на чужой тариф."""
        monkeypatch.setattr('app.services.premium_traffic_service.settings', _multi(True))

        assert PremiumTrafficService._panel_user_id(_subscription(1, panel_id=11)) == 11

    def test_multi_tariff_never_falls_back_to_the_user_account(self, monkeypatch):
        """Подписка ещё не заведена в панели — пропускаем, а не берём чужой id.

        Иначе первая же непросинхронизированная подписка начала бы списывать
        премиум-трафик с аккаунта соседнего тарифа.
        """
        monkeypatch.setattr('app.services.premium_traffic_service.settings', _multi(True))

        assert PremiumTrafficService._panel_user_id(_subscription(1, panel_id=None)) is None

    def test_single_tariff_falls_back_to_the_user_account(self, monkeypatch):
        """В обычном режиме аккаунт один и живёт на пользователе."""
        monkeypatch.setattr('app.services.premium_traffic_service.settings', _multi(False))

        assert PremiumTrafficService._panel_user_id(_subscription(1, panel_id=None)) == 999


class TestStateIsolation:
    async def test_same_squad_in_two_subscriptions_keeps_separate_state(self, monkeypatch):
        """Один и тот же премиум-сервер может входить в оба тарифа человека."""
        async with memory_session(monkeypatch, TABLES) as db:
            first = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            second = await get_or_create_state(db, 2, SQUAD, limit_bytes=10 * BYTES_IN_GB, period_start_at=NOW)
            first.used_bytes = 5 * BYTES_IN_GB
            first.is_limited = True
            await db.commit()

            assert first.id != second.id
            assert second.used_bytes == 0
            assert second.is_limited is False

    async def test_limit_on_one_subscription_does_not_touch_the_other(self, monkeypatch):
        """Снятие сквада на исчерпанной подписке не должно закрыть его на второй."""
        async with memory_session(monkeypatch, TABLES) as db:
            limited = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            await get_or_create_state(db, 2, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            limited.is_limited = True
            await db.commit()

            assert await get_limited_squad_uuids(db, 1) == {SQUAD}
            assert await get_limited_squad_uuids(db, 2) == set()

    async def test_filter_uses_the_asked_subscription(self, monkeypatch):
        """Фильтр отправки обязан смотреть на свою подписку, а не на любую."""
        async with memory_session(monkeypatch, TABLES) as db:
            limited = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            await get_or_create_state(db, 2, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            limited.is_limited = True
            await db.commit()

            assert await effective_panel_squads(1, [SQUAD], db=db) == []
            assert await effective_panel_squads(2, [SQUAD], db=db) == [SQUAD]


class TestUsageAttribution:
    async def test_usage_is_read_per_panel_account(self):
        """Статистика приходит на весь сквад: каждая подписка берёт свою строку.

        В мультиподписках это два разных аккаунта одного человека, и перепутать
        их значило бы посчитать чужой расход.
        """

        class _Api:
            async def get_internal_squad_accessible_nodes(self, squad_uuid):
                return [SimpleNamespace(uuid='node-1')]

            async def get_bandwidth_stats_nodes_usage(self, node_uuids, start_date, end_date, min_total_bytes=0):
                return {
                    'nodes': [
                        {
                            'uuid': 'node-1',
                            'users': [
                                {'id': 11, 'totalBytes': 3 * BYTES_IN_GB},
                                {'id': 22, 'totalBytes': 9 * BYTES_IN_GB},
                            ],
                        }
                    ]
                }

        usage = await PremiumTrafficService()._fetch_usage(_Api(), SQUAD, '2026-09-01', NOW)

        assert usage[11] == 3 * BYTES_IN_GB
        assert usage[22] == 9 * BYTES_IN_GB
