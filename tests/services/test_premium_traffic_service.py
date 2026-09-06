"""Воркер премиум-трафика: подсчёт, снятие, возврат, устойчивость к сбоям."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.premium_traffic_service import PremiumTrafficService, _Target
from app.utils.premium_traffic import BYTES_IN_GB, PremiumSquadConfig


SQUAD = 'e4f819ca-2cfd-4425-9354-16a262b180c1'
NODE_A = '3ca79b63-1b0d-49ec-b2d7-6eb264a560c5'
NODE_B = '7f2c1a90-0000-4000-8000-000000000002'
PANEL_USER_ID = 42
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


class FakeRemnawaveApi:
    """Заглушка панели: отдаёт заранее заданный расход по нодам."""

    def __init__(self, usage_by_node=None, nodes=(NODE_A,), panel_user=None):
        self.usage_by_node = usage_by_node or {}
        self.nodes = list(nodes)
        self.panel_user = panel_user
        self.usage_calls: list[tuple[list[str], str, str]] = []
        self.node_calls = 0

    async def get_internal_squad_accessible_nodes(self, squad_uuid):
        self.node_calls += 1
        return [SimpleNamespace(uuid=uuid) for uuid in self.nodes]

    async def get_bandwidth_stats_nodes_usage(self, node_uuids, start_date, end_date, min_total_bytes=0):
        self.usage_calls.append((list(node_uuids), start_date, end_date))
        return {'nodes': [{'uuid': uuid, 'users': self.usage_by_node.get(uuid, [])} for uuid in node_uuids]}

    async def get_user_by_id(self, user_id):
        return self.panel_user


def _state(limit_gb=5, used_bytes=0, extra_bytes=0, is_limited=False, notified_80=False, baseline_bytes=0):
    """Лёгкий двойник состояния: воркер обращается только к этим полям."""
    limit_bytes = limit_gb * BYTES_IN_GB

    class _State:
        def __init__(self):
            self.limit_bytes = limit_bytes
            self.extra_bytes = extra_bytes
            self.used_bytes = used_bytes
            self.is_limited = is_limited
            self.notified_80 = notified_80
            # По умолчанию поправка на первые сутки уже снята: тесты решений
            # про пороги, а не про неё — у неё свой набор.
            self.baseline_bytes = baseline_bytes
            self.last_checked_at = None
            self.period_start_at = NOW
            self.panel_reset_ack_at = None

        @property
        def total_limit_bytes(self):
            return self.limit_bytes + self.extra_bytes

        @property
        def is_exhausted(self):
            return self.total_limit_bytes > 0 and self.used_bytes >= self.total_limit_bytes

    return _State()


def _target(subscription_id=1, limit_gb=5, connected=(SQUAD,), language='ru', telegram_id=555, display_name='LTE'):
    user = SimpleNamespace(telegram_id=telegram_id, language=language, remnawave_id=PANEL_USER_ID)
    subscription = SimpleNamespace(
        id=subscription_id,
        user=user,
        connected_squads=list(connected),
        start_date=NOW - timedelta(days=10),
        tariff=SimpleNamespace(traffic_reset_mode='MONTH'),
        remnawave_id=PANEL_USER_ID,
    )
    return _Target(subscription, PremiumSquadConfig(squad_uuid=SQUAD, limit_gb=limit_gb), PANEL_USER_ID, display_name)


class _Db:
    """Сессия-заглушка: воркеру от неё нужен только commit."""

    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


class TestUsageCollection:
    async def test_bytes_are_summed_across_all_nodes_of_the_squad(self):
        api = FakeRemnawaveApi(
            usage_by_node={
                NODE_A: [{'id': PANEL_USER_ID, 'totalBytes': 1000}, {'id': 7, 'totalBytes': 50}],
                NODE_B: [{'id': PANEL_USER_ID, 'totalBytes': 2000}],
            },
            nodes=(NODE_A, NODE_B),
        )
        service = PremiumTrafficService()

        usage = await service._fetch_usage(api, SQUAD, '2026-09-01', NOW)

        assert usage[PANEL_USER_ID] == 3000
        assert usage[7] == 50

    async def test_one_request_covers_all_nodes_and_users(self):
        """Стоимость прохода растёт от числа сквадов, а не пользователей."""
        api = FakeRemnawaveApi(nodes=(NODE_A, NODE_B))
        service = PremiumTrafficService()

        await service._fetch_usage(api, SQUAD, '2026-09-01', NOW)

        assert len(api.usage_calls) == 1
        assert api.usage_calls[0][0] == [NODE_A, NODE_B]

    async def test_end_date_covers_today(self):
        """Панель включает конечную дату непредсказуемо; лишние сутки не занижают."""
        api = FakeRemnawaveApi()
        service = PremiumTrafficService()

        await service._fetch_usage(api, SQUAD, '2026-09-01', NOW)

        _nodes, start, end = api.usage_calls[0]
        assert start == '2026-09-01'
        assert end == '2026-09-07'

    async def test_squad_without_nodes_reports_no_usage(self):
        api = FakeRemnawaveApi(nodes=())
        service = PremiumTrafficService()

        assert await service._fetch_usage(api, SQUAD, '2026-09-01', NOW) == {}
        assert api.usage_calls == []

    async def test_nodes_are_cached_between_passes(self):
        """Ноды сквада меняются редко, а спрашивают их на каждом проходе."""
        api = FakeRemnawaveApi()
        service = PremiumTrafficService()

        await service._fetch_usage(api, SQUAD, '2026-09-01', NOW)
        await service._fetch_usage(api, SQUAD, '2026-09-02', NOW)

        assert api.node_calls == 1

    async def test_entries_without_user_id_are_skipped(self):
        api = FakeRemnawaveApi(usage_by_node={NODE_A: [{'totalBytes': 999}, {'id': None, 'totalBytes': 5}]})
        service = PremiumTrafficService()

        assert await service._fetch_usage(api, SQUAD, '2026-09-01', NOW) == {}


class TestDecisions:
    async def _apply(self, service, target, state, used_bytes, monkeypatch, api=None):
        async def _get_state(_db, _sub_id, _squad):
            return state

        monkeypatch.setattr('app.database.crud.premium_traffic.get_state', _get_state)
        pushed = []

        async def _push(_db, _api, tgt):
            pushed.append(tgt.subscription.id)

        monkeypatch.setattr(service, '_push_squads', _push)
        outcome = await service._apply_usage(
            _Db(),
            api or FakeRemnawaveApi(),
            target,
            used_bytes=used_bytes,
            period_start=NOW,
            now=NOW,
        )
        return outcome, pushed

    async def test_exhausted_squad_is_limited_and_pushed(self, monkeypatch):
        service = PremiumTrafficService()
        state = _state(limit_gb=5)

        outcome, pushed = await self._apply(service, _target(), state, 5 * BYTES_IN_GB, monkeypatch)

        assert outcome == 'limited'
        assert state.is_limited is True
        assert pushed == [1]

    async def test_usage_below_limit_changes_nothing(self, monkeypatch):
        service = PremiumTrafficService()
        state = _state(limit_gb=5)

        outcome, pushed = await self._apply(service, _target(), state, BYTES_IN_GB, monkeypatch)

        assert outcome is None
        assert state.is_limited is False
        assert pushed == []

    async def test_warning_is_sent_once_at_eighty_percent(self, monkeypatch):
        service = PremiumTrafficService()
        state = _state(limit_gb=5)

        outcome, _ = await self._apply(service, _target(), state, 4 * BYTES_IN_GB, monkeypatch)
        assert outcome == 'warned'
        assert state.notified_80 is True

        second, _ = await self._apply(service, _target(), state, 4 * BYTES_IN_GB, monkeypatch)
        assert second is None

    async def test_topup_restores_a_limited_squad(self, monkeypatch):
        service = PremiumTrafficService()
        state = _state(limit_gb=5, used_bytes=5 * BYTES_IN_GB, is_limited=True)
        state.extra_bytes = 3 * BYTES_IN_GB

        outcome, pushed = await self._apply(service, _target(), state, 5 * BYTES_IN_GB, monkeypatch)

        assert outcome == 'restored'
        assert state.is_limited is False
        assert pushed == [1]

    async def test_recorded_usage_never_drops(self, monkeypatch):
        """Просадка выборки не должна вернуть доступ к исчерпанному скваду."""
        service = PremiumTrafficService()
        state = _state(limit_gb=5, used_bytes=5 * BYTES_IN_GB, is_limited=True)

        outcome, _ = await self._apply(service, _target(), state, 0, monkeypatch)

        assert state.used_bytes == 5 * BYTES_IN_GB
        assert state.is_limited is True
        assert outcome is None

    async def test_squad_missing_in_panel_is_resynced(self, monkeypatch):
        """Докупка снимает `is_limited` сама и сама возвращает сквад в панель.

        Если та отправка не дошла, ветки «снять»/«вернуть» сюда уже не попадут —
        флаг-то снят. Сверка с фактическим состоянием панели это добирает.
        """
        service = PremiumTrafficService()
        state = _state(limit_gb=5, extra_bytes=5 * BYTES_IN_GB, used_bytes=5 * BYTES_IN_GB)

        async def _get_state(_db, _sub_id, _squad):
            return state

        monkeypatch.setattr('app.database.crud.premium_traffic.get_state', _get_state)
        pushed = []

        async def _push(_db, _api, tgt):
            pushed.append(tgt.config.squad_uuid)

        monkeypatch.setattr(service, '_push_squads', _push)

        outcome = await service._apply_usage(
            _Db(),
            FakeRemnawaveApi(),
            _target(),
            used_bytes=5 * BYTES_IN_GB,
            period_start=NOW,
            now=NOW,
            panel_user=SimpleNamespace(active_internal_squads=[]),
        )

        assert outcome == 'restored'
        assert pushed == [SQUAD]

    async def test_squad_present_in_panel_is_left_alone(self, monkeypatch):
        """Панель отдаёт сквады объектами {uuid, name}, а не строками.

        Сравнение по строкам не находило бы совпадений никогда, и бот дёргал бы
        панель на каждом проходе.
        """
        service = PremiumTrafficService()
        state = _state(limit_gb=5, used_bytes=BYTES_IN_GB)

        async def _get_state(_db, _sub_id, _squad):
            return state

        monkeypatch.setattr('app.database.crud.premium_traffic.get_state', _get_state)
        pushed = []

        async def _push(_db, _api, tgt):
            pushed.append(tgt.config.squad_uuid)

        monkeypatch.setattr(service, '_push_squads', _push)

        outcome = await service._apply_usage(
            _Db(),
            FakeRemnawaveApi(),
            _target(),
            used_bytes=BYTES_IN_GB,
            period_start=NOW,
            now=NOW,
            panel_user=SimpleNamespace(active_internal_squads=[{'uuid': SQUAD, 'name': 'LTE'}]),
        )

        assert outcome is None
        assert pushed == []

    async def test_unknown_panel_state_does_not_trigger_a_push(self, monkeypatch):
        """Панель не ответила — сверять не с чем, трогать ничего нельзя."""
        service = PremiumTrafficService()
        state = _state(limit_gb=5, used_bytes=BYTES_IN_GB)

        async def _get_state(_db, _sub_id, _squad):
            return state

        monkeypatch.setattr('app.database.crud.premium_traffic.get_state', _get_state)
        pushed = []

        async def _push(_db, _api, tgt):
            pushed.append(tgt.config.squad_uuid)

        monkeypatch.setattr(service, '_push_squads', _push)

        for panel_user in (None, SimpleNamespace(active_internal_squads=None)):
            outcome = await service._apply_usage(
                _Db(),
                FakeRemnawaveApi(),
                _target(),
                used_bytes=BYTES_IN_GB,
                period_start=NOW,
                now=NOW,
                panel_user=panel_user,
            )
            assert outcome is None

        assert pushed == []

    async def test_missing_state_is_not_an_error(self, monkeypatch):
        service = PremiumTrafficService()

        async def _none(_db, _sub_id, _squad):
            return None

        monkeypatch.setattr('app.database.crud.premium_traffic.get_state', _none)

        outcome = await service._apply_usage(
            _Db(), FakeRemnawaveApi(), _target(), used_bytes=0, period_start=NOW, now=NOW
        )

        assert outcome is None


class TestFirstDayCorrection:
    """Статистика панели задаётся датами без времени.

    Из-за этого запрос за день начала периода приносит и трафик, потраченный до
    сброса. Если период начался не в полночь, первый замер целиком относится к
    прошлому периоду — его и вычитаем.
    """

    def test_midday_start_subtracts_the_first_measurement(self):
        state = _state(limit_gb=5, baseline_bytes=None)
        start = datetime(2026, 9, 6, 17, 9, tzinfo=UTC)

        # Первый замер: 8 ГБ, но всё это — вчерашний период.
        assert PremiumTrafficService._net_usage(state, 8 * BYTES_IN_GB, start, start) == 0
        assert state.baseline_bytes == 8 * BYTES_IN_GB

        # Дальше считается только прирост.
        later = start + timedelta(hours=2)
        assert PremiumTrafficService._net_usage(state, 11 * BYTES_IN_GB, start, later) == 3 * BYTES_IN_GB

    def test_midnight_start_needs_no_correction(self):
        """Календарные режимы начинаются в полночь — запрос точен."""
        state = _state(limit_gb=5, baseline_bytes=None)
        start = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)

        assert PremiumTrafficService._net_usage(state, 4 * BYTES_IN_GB, start, NOW) == 4 * BYTES_IN_GB
        assert state.baseline_bytes == 0

    def test_late_first_check_does_not_swallow_real_usage(self):
        """Воркер простоял сутки — вычитать уже нечего.

        Первый замер включал бы законный расход нового периода, и поправка
        подарила бы пользователю лимит.
        """
        state = _state(limit_gb=5, baseline_bytes=None)
        start = datetime(2026, 9, 6, 17, 9, tzinfo=UTC)
        much_later = start + timedelta(days=2)

        assert PremiumTrafficService._net_usage(state, 9 * BYTES_IN_GB, start, much_later) == 9 * BYTES_IN_GB
        assert state.baseline_bytes == 0

    def test_correction_is_measured_once_per_period(self):
        state = _state(limit_gb=5, baseline_bytes=None)
        start = datetime(2026, 9, 6, 17, 9, tzinfo=UTC)

        PremiumTrafficService._net_usage(state, 8 * BYTES_IN_GB, start, start)
        # Повторный замер поправку не переопределяет.
        PremiumTrafficService._net_usage(state, 12 * BYTES_IN_GB, start, start)

        assert state.baseline_bytes == 8 * BYTES_IN_GB

    def test_usage_never_goes_negative(self):
        """Панель отдала меньше, чем на момент замера поправки."""
        state = _state(limit_gb=5, baseline_bytes=None)
        start = datetime(2026, 9, 6, 17, 9, tzinfo=UTC)
        PremiumTrafficService._net_usage(state, 8 * BYTES_IN_GB, start, start)

        assert PremiumTrafficService._net_usage(state, BYTES_IN_GB, start, start) == 0


class TestIntervalSettings:
    @pytest.mark.parametrize('raw,expected', [(300, 300), (600, 600), (10, 60), ('900', 900), ('abc', 300)])
    def test_interval_is_clamped_and_coerced(self, monkeypatch, raw, expected):
        """Чаще минуты смысла нет: панель агрегирует статистику с задержкой."""
        service = PremiumTrafficService()
        monkeypatch.setattr(
            'app.services.premium_traffic_service.settings', SimpleNamespace(PREMIUM_TRAFFIC_CHECK_INTERVAL_SECONDS=raw)
        )

        assert service.get_check_interval_seconds() == expected


class TestNotifications:
    async def test_nothing_is_sent_without_a_bot(self):
        service = PremiumTrafficService()

        await service._notify_exhausted(_target(), _state())  # не должно падать

    async def test_message_names_the_server(self, monkeypatch):
        """С несколькими премиум-серверами иначе не понять, на каком кончилось."""
        service = PremiumTrafficService()
        sent = []

        class _Bot:
            async def send_message(self, chat_id, text, **_kwargs):
                sent.append(text)

        service.set_bot(_Bot())

        await service._notify_exhausted(_target(display_name='📱 Мобильный резерв 2'), _state())

        assert '📱 Мобильный резерв 2' in sent[0]

    async def test_message_falls_back_to_a_generic_label(self, monkeypatch):
        """Сервер удалили из справочника, своё название не задали."""
        service = PremiumTrafficService()
        sent = []

        class _Bot:
            async def send_message(self, chat_id, text, **_kwargs):
                sent.append(text)

        service.set_bot(_Bot())

        await service._notify_exhausted(_target(display_name=''), _state())

        assert sent and 'ремиум' in sent[0]

    async def test_message_carries_used_and_limit(self, monkeypatch):
        service = PremiumTrafficService()
        sent = []

        class _Bot:
            async def send_message(self, chat_id, text, **_kwargs):
                sent.append((chat_id, text))

        service.set_bot(_Bot())
        state = _state(limit_gb=5, used_bytes=5 * BYTES_IN_GB)

        await service._notify_exhausted(_target(), state)

        assert sent and sent[0][0] == 555
        assert '5' in sent[0][1]

    async def test_delivery_failure_does_not_propagate(self, monkeypatch):
        """Упавшее уведомление не должно валить проход воркера."""
        service = PremiumTrafficService()

        class _Bot:
            async def send_message(self, *_args, **_kwargs):
                raise RuntimeError('telegram недоступен')

        service.set_bot(_Bot())

        await service._notify_exhausted(_target(), _state())

    async def test_user_without_telegram_id_is_skipped(self):
        service = PremiumTrafficService()
        service.set_bot(object())  # обращение к нему упало бы

        await service._notify_exhausted(_target(telegram_id=None), _state())
