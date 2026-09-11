"""Единственная сборка запроса к панели.

До консолидации этот набор полей собирался руками в тринадцати местах, причём
четыре из них были буквально одинаковыми. Расхождения, которые здесь закрыты:
пустой список сквадов (панель трактует ``[]`` как «снять все инбаунды»), запасной
суффикс имени в мультитарифе и перевод гигабайтов в байты.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.database.models import SubscriptionStatus
from app.external.remnawave_api import UserStatus
from app.services.panel_sync import build_panel_payload
from app.services.panel_sync.expiry import _MINIMUM_FUTURE as MARGIN


NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def _user(**kw):
    base = dict(
        id=10,
        telegram_id=555,
        username='tg',
        full_name='Иван Пример',
        email=None,
        status='active',
        remnawave_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _sub(**kw):
    base = dict(
        id=101,
        status=SubscriptionStatus.ACTIVE.value,
        end_date=NOW + timedelta(days=30),
        traffic_limit_gb=50,
        connected_squads=['squad-a'],
        tariff=None,
        remnawave_id=None,
        remnawave_short_id='ab12cd',
        device_limit=3,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_gigabytes_become_bytes():
    payload = build_panel_payload(_user(), _sub(), multi_tariff=False, now=NOW)

    assert payload.traffic_limit_bytes == 50 * 1024**3


def test_zero_gigabytes_mean_unlimited():
    payload = build_panel_payload(_user(), _sub(traffic_limit_gb=0), multi_tariff=False, now=NOW)

    assert payload.traffic_limit_bytes == 0


def test_empty_squads_are_never_sent_on_update():
    """Пустой список для панели значит «снять все инбаунды» — так подписку глушили."""
    payload = build_panel_payload(_user(), _sub(connected_squads=[]), multi_tariff=False, now=NOW)

    assert 'active_internal_squads' not in payload.update_kwargs(user_id=7)


def test_empty_squads_are_sent_as_empty_list_on_create():
    """У нового аккаунта снимать нечего, а поле обязательно."""
    payload = build_panel_payload(_user(), _sub(connected_squads=[]), multi_tariff=False, now=NOW)

    assert payload.create_kwargs()['active_internal_squads'] == []


def test_multi_tariff_username_carries_the_subscription_suffix():
    payload = build_panel_payload(_user(), _sub(), multi_tariff=True, now=NOW)

    assert payload.username.endswith('_ab12cd')


def test_multi_tariff_username_falls_back_to_subscription_id():
    """Пустой short_id: без запасного суффикса два тарифа получали одно имя,
    панель отдавала одного пользователя, и лимит устройств становился общим."""
    payload = build_panel_payload(_user(), _sub(remnawave_short_id=''), multi_tariff=True, now=NOW)

    assert payload.username.endswith('_sub101')


def test_single_tariff_username_has_no_subscription_suffix():
    payload = build_panel_payload(_user(), _sub(), multi_tariff=False, now=NOW)

    assert not payload.username.endswith('_ab12cd')


def test_blocked_user_gets_disabled_status():
    payload = build_panel_payload(_user(status='blocked'), _sub(), multi_tariff=False, now=NOW)

    assert payload.status is UserStatus.DISABLED


def test_live_subscription_gets_active_status_and_its_own_date():
    subscription = _sub()
    payload = build_panel_payload(_user(), subscription, multi_tariff=False, now=NOW)

    assert payload.status is UserStatus.ACTIVE
    assert payload.update_kwargs(user_id=7, now=NOW)['expire_at'] == subscription.end_date


def test_update_of_an_expired_subscription_extinguishes_a_future_panel_date():
    payload = build_panel_payload(
        _user(),
        _sub(status=SubscriptionStatus.EXPIRED.value, end_date=NOW - timedelta(days=5)),
        multi_tariff=False,
        now=NOW,
    )

    kwargs = payload.update_kwargs(user_id=7, panel_current=NOW + timedelta(days=100), now=NOW)

    assert kwargs['expire_at'] == NOW + MARGIN


def test_update_of_an_expired_subscription_keeps_a_past_panel_date():
    payload = build_panel_payload(
        _user(),
        _sub(status=SubscriptionStatus.EXPIRED.value, end_date=NOW - timedelta(days=5)),
        multi_tariff=False,
        now=NOW,
    )

    kwargs = payload.update_kwargs(user_id=7, panel_current=NOW - timedelta(days=5), now=NOW)

    assert 'expire_at' not in kwargs


def test_create_of_an_expired_subscription_carries_its_real_date():
    """POST панель принимает с прошедшей датой — выдумывать «минуту вперёд» не надо."""
    end_date = NOW - timedelta(days=5)
    payload = build_panel_payload(
        _user(),
        _sub(status=SubscriptionStatus.EXPIRED.value, end_date=end_date),
        multi_tariff=False,
        now=NOW,
    )

    assert payload.create_kwargs(now=NOW)['expire_at'] == end_date


def test_external_squad_is_taken_from_the_tariff():
    payload = build_panel_payload(
        _user(),
        _sub(tariff=SimpleNamespace(external_squad_uuid='ext-1', traffic_reset_mode=None, is_daily=False)),
        multi_tariff=False,
        now=NOW,
    )

    assert payload.update_kwargs(user_id=7)['external_squad_uuid'] == 'ext-1'


def test_null_external_squad_is_never_sent():
    """Панель отвечает ошибкой A039 на null в externalSquadUuid."""
    payload = build_panel_payload(_user(), _sub(), multi_tariff=False, now=NOW)

    assert 'external_squad_uuid' not in payload.update_kwargs(user_id=7)
    assert 'external_squad_uuid' not in payload.create_kwargs()


def test_update_payload_never_carries_username():
    """PATCH с username переименовал бы аккаунт в панели."""
    payload = build_panel_payload(_user(), _sub(), multi_tariff=False, now=NOW)

    assert 'username' not in payload.update_kwargs(user_id=7)


def test_update_payload_carries_the_panel_user_id():
    payload = build_panel_payload(_user(), _sub(), multi_tariff=False, now=NOW)

    assert payload.update_kwargs(user_id=7)['user_id'] == 7


def test_only_fields_filter_keeps_the_addressee():
    """Узкие правки (описание, сквады) не должны тащить в панель соседние поля."""
    payload = build_panel_payload(_user(), _sub(), multi_tariff=False, now=NOW)

    kwargs = payload.update_kwargs(user_id=7, only_fields={'description'})

    assert set(kwargs) == {'user_id', 'description'}


def test_tag_is_sent_only_when_given():
    without = build_panel_payload(_user(), _sub(), multi_tariff=False, now=NOW)
    with_tag = build_panel_payload(_user(), _sub(), multi_tariff=False, user_tag='VIP', now=NOW)

    assert 'tag' not in without.update_kwargs(user_id=7)
    assert with_tag.update_kwargs(user_id=7)['tag'] == 'VIP'


# ==================== статус: что панель решает сама ====================

# В PATCH панель принимает только ACTIVE и DISABLED (контракт 3.4.3, ошибка A089:
# «LIMITED и EXPIRED нельзя выставить вручную»). DISABLED для панели — решение
# админа, а истечение по дате и исчерпанный трафик она выводит сама. Отправляя
# истёкшей подписке DISABLED, бот превращал «истекла» в «отключена
# администратором» у аккаунта, который панель ещё считала живым, и это
# «отключена» импорт нёс обратно в бота, где такую подписку нельзя продлить из
# кабинета. На уже истёкшем аккаунте панель 3.4.3 такой PATCH игнорирует
# (проверено на стенде) — но отправлять его всё равно незачем.


def test_expired_subscription_of_an_active_user_sends_no_status_on_update():
    payload = build_panel_payload(
        _user(),
        _sub(status=SubscriptionStatus.EXPIRED.value, end_date=NOW - timedelta(days=5)),
        multi_tariff=False,
        now=NOW,
    )

    assert 'status' not in payload.update_kwargs(user_id=7, now=NOW)


def test_active_column_past_its_date_sends_no_status_on_update():
    """Мониторинг ещё не успел поставить EXPIRED — для панели это всё равно истечение, не отключение."""
    payload = build_panel_payload(
        _user(),
        _sub(status=SubscriptionStatus.ACTIVE.value, end_date=NOW - timedelta(hours=1)),
        multi_tariff=False,
        now=NOW,
    )

    assert 'status' not in payload.update_kwargs(user_id=7, now=NOW)


def test_limited_subscription_sends_no_status_on_update():
    """Исчерпанный трафик панель считает сама; DISABLED сверху не снимался бы её же сбросом трафика."""
    payload = build_panel_payload(
        _user(),
        _sub(status=SubscriptionStatus.LIMITED.value),
        multi_tariff=False,
        now=NOW,
    )

    assert 'status' not in payload.update_kwargs(user_id=7, now=NOW)


def test_disabled_subscription_sends_disabled_on_update():
    """Отключение в боте (обнуление админом) — настоящее решение, оно обязано доехать."""
    payload = build_panel_payload(
        _user(),
        _sub(status=SubscriptionStatus.DISABLED.value, end_date=NOW - timedelta(days=5)),
        multi_tariff=False,
        now=NOW,
    )

    assert payload.update_kwargs(user_id=7, now=NOW)['status'] is UserStatus.DISABLED


def test_blocked_user_with_expired_subscription_sends_disabled_on_update():
    """Блокировка пользователя важнее истечения: панель обязана держать его выключенным."""
    payload = build_panel_payload(
        _user(status='blocked'),
        _sub(status=SubscriptionStatus.EXPIRED.value, end_date=NOW - timedelta(days=5)),
        multi_tariff=False,
        now=NOW,
    )

    assert payload.update_kwargs(user_id=7, now=NOW)['status'] is UserStatus.DISABLED


def test_expired_column_with_a_future_date_still_sends_disabled_on_update():
    """Противоречивое состояние (статус «истекла», дата в будущем) — гасим, как и раньше."""
    payload = build_panel_payload(
        _user(),
        _sub(status=SubscriptionStatus.EXPIRED.value, end_date=NOW + timedelta(days=5)),
        multi_tariff=False,
        now=NOW,
    )

    assert payload.update_kwargs(user_id=7, now=NOW)['status'] is UserStatus.DISABLED


def test_create_of_an_expired_subscription_sends_expired_status():
    """При создании панель принимает и EXPIRED — заведённый аккаунт сразу истёкший, а не отключённый."""
    payload = build_panel_payload(
        _user(),
        _sub(status=SubscriptionStatus.EXPIRED.value, end_date=NOW - timedelta(days=5)),
        multi_tariff=False,
        now=NOW,
    )

    assert payload.create_kwargs(now=NOW)['status'] is UserStatus.EXPIRED


def test_create_of_a_limited_subscription_sends_limited_status():
    payload = build_panel_payload(
        _user(),
        _sub(status=SubscriptionStatus.LIMITED.value),
        multi_tariff=False,
        now=NOW,
    )

    assert payload.create_kwargs(now=NOW)['status'] is UserStatus.LIMITED
