"""Снятие дней админом не откатывается эхом панели.

Неживой подписке ``update_remnawave_user`` гасит дату в панели. Раньше он не
знал, что там стоит, и слал гашение вторым PATCH. Эхо первого — ``user.modified``
ещё со старой датой — вебхук переносил в бота вместе со статусом ACTIVE. Эхо
второго двое суток после оплаты не принималось («срок короче оплаченного»), а
повтор из очереди отправлял в панель вернувшийся оплаченный срок: «−N дней»
сразу после оплаты откатывалось целиком — и в боте, и в панели.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

# Только модулем: тест подменяет его атрибуты (get_user_by_id и другие), и второй
# импорт тех же имён через `from` разошёлся бы с подменой (py/import-and-import-from).
import app.services.remnawave_webhook_service as webhook_mod
import app.services.subscription_service as subscription_service_mod
from app.config import Settings
from app.database.models import SubscriptionStatus


PANEL_ID = 777


class _Panel:
    """Один аккаунт панели: дату меняет только PATCH, каждый PATCH даёт эхо ``user.modified``."""

    def __init__(self, expire_at: datetime, *, readable: bool = True) -> None:
        self.expire_at = expire_at
        self.readable = readable
        self.reads = 0
        self.patches: list[dict] = []
        self.echoes: list[dict] = []

    async def get_user_by_id(self, user_id):
        self.reads += 1
        if not self.readable:
            raise RuntimeError('panel read failed')
        return SimpleNamespace(id=user_id, expire_at=self.expire_at)

    async def update_user(self, **kwargs):
        self.patches.append(kwargs)
        if 'expire_at' in kwargs:
            self.expire_at = kwargs['expire_at']
        # Панель гасит аккаунт сама, когда дата пройдёт; до тех пор он ACTIVE.
        self.echoes.append({'status': 'ACTIVE', 'expireAt': self.expire_at.isoformat()})
        return SimpleNamespace(
            id=kwargs['user_id'],
            expire_at=self.expire_at,
            subscription_url='https://sub.example/u',
            happ_crypto_link=None,
            short_uuid=None,
        )

    async def create_user(self, **_kwargs):
        raise AssertionError('аккаунт в панели есть — создавать нечего')


def _user() -> SimpleNamespace:
    return SimpleNamespace(
        id=1, telegram_id=100, username='u', full_name='User', email=None, remnawave_id=PANEL_ID, status='active'
    )


def _subscription(**overrides) -> SimpleNamespace:
    base = {
        'id': 11,
        'user_id': 1,
        'status': SubscriptionStatus.ACTIVE.value,
        'end_date': datetime.now(UTC) + timedelta(days=30),
        'traffic_limit_gb': 0,
        'traffic_used_gb': 0.0,
        'connected_squads': [],
        'tariff': None,
        'remnawave_id': PANEL_ID,
        'remnawave_short_uuid': None,
        'is_trial': False,
        'updated_at': None,
        'last_webhook_update_at': None,
        'subscription_url': None,
        'subscription_crypto_link': None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _db() -> AsyncMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    result.first.return_value = None
    result.scalars.return_value.all.return_value = []
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _service(monkeypatch, panel: _Panel, user) -> subscription_service_mod.SubscriptionService:
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    service = subscription_service_mod.SubscriptionService()
    monkeypatch.setattr(subscription_service_mod, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(subscription_service_mod, 'resolve_hwid_device_limit_for_payload', lambda s: None)

    @asynccontextmanager
    async def fake_client():
        yield panel

    monkeypatch.setattr(service, 'get_api_client', fake_client)
    return service


def _shortened_past_now(subscription) -> None:
    """Что оставляет «−N дней» админа, когда срок ушёл в прошлое."""
    subscription.end_date = datetime.now(UTC) - timedelta(days=2)
    subscription.status = SubscriptionStatus.EXPIRED.value


async def test_shortening_right_after_payment_survives_the_panel_echo(monkeypatch):
    paid_until = datetime.now(UTC) + timedelta(days=30)
    panel = _Panel(paid_until)
    user = _user()
    service = _service(monkeypatch, panel, user)
    subscription = _subscription(end_date=paid_until)
    _shortened_past_now(subscription)

    # Оплата только что: вебхук держит оплаченный срок от «устаревшей» панели.
    monkeypatch.setattr(
        'app.database.crud.transaction.get_last_subscription_payment_at',
        AsyncMock(return_value=datetime.now(UTC) - timedelta(minutes=3)),
    )
    retries: list[dict] = []
    monkeypatch.setattr(
        'app.services.remnawave_retry_queue.remnawave_retry_queue.enqueue', lambda **kwargs: retries.append(kwargs)
    )

    assert await service.update_remnawave_user(_db(), subscription) is not None

    webhook = webhook_mod.RemnaWaveWebhookService(MagicMock())
    webhook._stamp_webhook_update = MagicMock()
    for echo in panel.echoes:
        await webhook._handle_user_modified(AsyncMock(), user, subscription, echo)

    assert subscription.end_date < datetime.now(UTC) + timedelta(minutes=20), 'эхо вернуло оплаченный срок'
    assert retries == [], 'повтор отправил бы в панель оплаченный срок'
    assert panel.expire_at < datetime.now(UTC) + timedelta(minutes=20)


async def test_expired_update_extinguishes_the_date_in_one_patch(monkeypatch):
    panel = _Panel(datetime.now(UTC) + timedelta(days=30))
    service = _service(monkeypatch, panel, _user())
    subscription = _subscription()
    _shortened_past_now(subscription)

    await service.update_remnawave_user(_db(), subscription)

    assert len(panel.patches) == 1, 'второй PATCH даёт второе эхо — со старой датой в первом'
    written = panel.patches[0].get('expire_at')
    assert written is not None
    assert datetime.now(UTC) < written < datetime.now(UTC) + timedelta(minutes=20)


async def test_live_update_does_not_read_the_panel(monkeypatch):
    end_date = datetime.now(UTC) + timedelta(days=30)
    panel = _Panel(datetime.now(UTC) - timedelta(days=1))
    service = _service(monkeypatch, panel, _user())

    await service.update_remnawave_user(_db(), _subscription(end_date=end_date))

    assert panel.reads == 0  # у живой подписки дата уходит первым запросом и так
    assert [patch.get('expire_at') for patch in panel.patches] == [end_date]


async def test_unreadable_panel_falls_back_to_a_second_patch(monkeypatch):
    panel = _Panel(datetime.now(UTC) + timedelta(days=30), readable=False)
    service = _service(monkeypatch, panel, _user())
    subscription = _subscription()
    _shortened_past_now(subscription)

    assert await service.update_remnawave_user(_db(), subscription) is not None

    assert len(panel.patches) == 2
    assert 'expire_at' not in panel.patches[0]
    assert datetime.now(UTC) < panel.patches[1]['expire_at'] < datetime.now(UTC) + timedelta(minutes=20)
