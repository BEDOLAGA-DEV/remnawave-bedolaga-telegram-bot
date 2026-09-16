"""Единственный маппер «панель → подписка».

Раньше это делали шесть независимых мапперов с разными наборами полей и разными
правилами. Здесь закреплены правила, которые они должны были соблюдать все.

Владелец (2026-09-11): панель — истина, «в бота пишется истина панели». Дата,
статус, трафик, сквады и лимиты берутся из панели при любом статусе аккаунта;
от устаревшего снимка защищает его возраст, а не недоверие к полям.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.database.models import SubscriptionStatus
from app.services.panel_sync import (
    ADMIN_PULL,
    BULK_SNAPSHOT,
    WEBHOOK,
    PanelSnapshot,
    project_onto_subscription,
    read_panel_user,
)


NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def _sub(**kw):
    base = dict(
        id=1,
        status=SubscriptionStatus.ACTIVE.value,
        end_date=NOW + timedelta(days=30),
        traffic_used_gb=1.0,
        traffic_limit_gb=100,
        device_limit=3,
        connected_squads=['squad-a'],
        remnawave_short_uuid='abc',
        subscription_url='https://old',
        subscription_crypto_link='old-crypto',
        grace_candidate_reason=None,
        grace_candidate_at=None,
        grace_tail_expire_at=None,
        grace_session_open=False,
        grace_overlay_expire_at=None,
        updated_at=None,
        last_webhook_update_at=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ==================== разбор ответа панели ====================


def test_reads_the_dictionary_shape_of_the_panel():
    snapshot = read_panel_user(
        {
            'status': 'ACTIVE',
            'expireAt': '2026-10-09T12:00:00.000Z',
            'usedTrafficBytes': 2 * 1024**3,
            'activeInternalSquads': [{'uuid': 'squad-b'}, 'squad-c'],
            'shortUuid': 'short-1',
            'subscriptionUrl': 'https://panel/sub',
            'happ': {'cryptoLink': 'crypto-1'},
        }
    )

    assert snapshot.status == 'ACTIVE'
    assert snapshot.expire_at == datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    assert snapshot.traffic_used_gb == 2.0
    assert snapshot.squads == ('squad-b', 'squad-c')
    assert snapshot.short_uuid == 'short-1'
    assert snapshot.subscription_url == 'https://panel/sub'
    assert snapshot.crypto_link == 'crypto-1'


def test_reads_the_parsed_object_shape_of_the_client():
    snapshot = read_panel_user(
        SimpleNamespace(
            status='DISABLED',
            expire_at=datetime(2026, 1, 1, tzinfo=UTC),
            used_traffic_bytes=1024**3,
            active_internal_squads=[{'uuid': 'squad-d'}],
            short_uuid='short-2',
            subscription_url='https://panel/two',
            happ_crypto_link='crypto-2',
        )
    )

    assert snapshot.status == 'DISABLED'
    assert snapshot.expire_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert snapshot.traffic_used_gb == 1.0
    assert snapshot.squads == ('squad-d',)
    assert snapshot.crypto_link == 'crypto-2'


def test_reads_the_status_enum_of_the_client():
    """Клиент отдаёт статус перечислением, панель — строкой."""
    snapshot = read_panel_user(SimpleNamespace(status=SimpleNamespace(value='ACTIVE')))

    assert snapshot.status == 'ACTIVE'


def test_unparsable_date_does_not_explode():
    assert read_panel_user({'expireAt': 'позавчера'}).expire_at is None


# ==================== дата окончания ====================


def test_active_panel_moves_the_end_date_in_both_directions():
    later = _sub()
    project_onto_subscription(later, PanelSnapshot(status='ACTIVE', expire_at=NOW + timedelta(days=60)), now=NOW)
    earlier = _sub()
    project_onto_subscription(earlier, PanelSnapshot(status='ACTIVE', expire_at=NOW + timedelta(days=10)), now=NOW)

    assert later.end_date == NOW + timedelta(days=60)
    assert earlier.end_date == NOW + timedelta(days=10)


def test_disabled_panel_date_is_taken_too():
    """Панель — истина при любом статусе: у отключённого дата тоже переносится."""
    subscription = _sub()

    project_onto_subscription(subscription, PanelSnapshot(status='DISABLED', expire_at=NOW), now=NOW)

    assert subscription.end_date == NOW
    assert subscription.status == SubscriptionStatus.DISABLED.value


def test_panel_active_resurrects_an_expired_row_with_the_panel_date():
    """Продлили руками в панели — бот берёт дату и снова считает подписку живой."""
    subscription = _sub(status=SubscriptionStatus.EXPIRED.value, end_date=NOW - timedelta(days=3))

    project_onto_subscription(subscription, PanelSnapshot(status='ACTIVE', expire_at=NOW + timedelta(days=20)), now=NOW)

    assert subscription.end_date == NOW + timedelta(days=20)
    assert subscription.status == SubscriptionStatus.ACTIVE.value


def test_trial_stays_trial_when_the_panel_says_active():
    """Панель не различает триал и оплату — триал в боте остаётся триалом."""
    subscription = _sub(status=SubscriptionStatus.TRIAL.value)

    project_onto_subscription(subscription, PanelSnapshot(status='ACTIVE', expire_at=NOW + timedelta(days=2)), now=NOW)

    assert subscription.status == SubscriptionStatus.TRIAL.value
    assert subscription.end_date == NOW + timedelta(days=2)


def test_expired_in_the_panel_takes_the_panel_date_and_expires():
    subscription = _sub()

    project_onto_subscription(
        subscription, PanelSnapshot(status='EXPIRED', expire_at=NOW - timedelta(hours=1)), now=NOW
    )

    assert subscription.end_date == NOW - timedelta(hours=1)
    assert subscription.status == SubscriptionStatus.EXPIRED.value
    assert subscription.grace_candidate_reason == SubscriptionStatus.EXPIRED.value


def test_a_few_seconds_of_difference_are_ignored():
    subscription = _sub()
    original = subscription.end_date

    project_onto_subscription(
        subscription,
        PanelSnapshot(status='ACTIVE', expire_at=original + timedelta(seconds=30)),
        now=NOW,
    )

    assert subscription.end_date == original


# ==================== статус ====================


def test_limited_in_the_panel_becomes_limited_in_the_bot():
    subscription = _sub()

    project_onto_subscription(subscription, PanelSnapshot(status='LIMITED'), now=NOW)

    assert subscription.status == SubscriptionStatus.LIMITED.value
    assert subscription.grace_candidate_reason == SubscriptionStatus.LIMITED.value
    assert subscription.grace_candidate_at == NOW


def test_expired_by_date_marks_a_grace_candidate():
    subscription = _sub(status=SubscriptionStatus.TRIAL.value, end_date=NOW - timedelta(days=1))

    project_onto_subscription(subscription, PanelSnapshot(status='DISABLED', expire_at=None), now=NOW)

    assert subscription.status == SubscriptionStatus.DISABLED.value


def test_silent_panel_does_not_expire_a_subscription_that_is_active_in_the_bot():
    """Панель не сказала статус — не гадаем; истечение по своей дате доводит мониторинг."""
    subscription = _sub(status=SubscriptionStatus.ACTIVE.value, end_date=NOW - timedelta(minutes=1))

    project_onto_subscription(subscription, PanelSnapshot(status=None), now=NOW)

    assert subscription.status == SubscriptionStatus.ACTIVE.value


def test_expired_trial_becomes_expired():
    subscription = _sub(status=SubscriptionStatus.TRIAL.value, end_date=NOW - timedelta(days=1))

    project_onto_subscription(subscription, PanelSnapshot(status=None), now=NOW)

    assert subscription.status == SubscriptionStatus.EXPIRED.value


# ==================== трафик, сквады, ссылки ====================


def test_traffic_is_carried_over():
    subscription = _sub()

    project_onto_subscription(subscription, PanelSnapshot(status='ACTIVE', traffic_used_gb=7.5), now=NOW)

    assert subscription.traffic_used_gb == 7.5


def test_traffic_jitter_is_ignored():
    subscription = _sub(traffic_used_gb=1.0)

    project_onto_subscription(subscription, PanelSnapshot(status='ACTIVE', traffic_used_gb=1.005), now=NOW)

    assert subscription.traffic_used_gb == 1.0


def test_limits_are_read_from_the_panel():
    """Панель — истина и по лимитам: правка в панели приезжает в бота."""
    subscription = _sub()

    project_onto_subscription(
        subscription, PanelSnapshot(status='ACTIVE', traffic_limit_gb=500, device_limit=10), now=NOW
    )

    assert subscription.traffic_limit_gb == 500
    assert subscription.device_limit == 10


def test_missing_limits_in_the_answer_keep_the_bot_values():
    subscription = _sub()

    project_onto_subscription(subscription, PanelSnapshot(status='ACTIVE'), now=NOW)

    assert subscription.traffic_limit_gb == 100
    assert subscription.device_limit == 3


def test_squads_come_from_the_panel():
    subscription = _sub()

    project_onto_subscription(subscription, PanelSnapshot(status='ACTIVE', squads=('squad-x',)), now=NOW)

    assert subscription.connected_squads == ['squad-x']


def test_empty_squad_list_means_the_panel_does_not_know():
    subscription = _sub()

    project_onto_subscription(subscription, PanelSnapshot(status='ACTIVE', squads=()), now=NOW)

    assert subscription.connected_squads == ['squad-a']


def test_links_are_refreshed():
    subscription = _sub()

    changed = project_onto_subscription(
        subscription,
        PanelSnapshot(
            status='ACTIVE',
            short_uuid='new-short',
            subscription_url='https://new',
            crypto_link='new-crypto',
        ),
        now=NOW,
    )

    assert subscription.remnawave_short_uuid == 'new-short'
    assert subscription.subscription_url == 'https://new'
    assert subscription.subscription_crypto_link == 'new-crypto'
    assert {'remnawave_short_uuid', 'subscription_url', 'subscription_crypto_link'} <= changed


# ==================== грейс ====================


def test_open_grace_freezes_the_billing_state_but_keeps_links():
    subscription = _sub()

    changed = project_onto_subscription(
        subscription,
        PanelSnapshot(status='DISABLED', expire_at=NOW, traffic_used_gb=99.0, short_uuid='new-short'),
        now=NOW,
        grace_open=True,
    )

    assert subscription.status == SubscriptionStatus.ACTIVE.value
    assert subscription.end_date == NOW + timedelta(days=30)
    assert subscription.connected_squads == ['squad-a']
    assert subscription.remnawave_short_uuid == 'new-short'
    # Трафик и ссылки ничего не решают: показывать устаревшие цифры незачем.
    assert subscription.traffic_used_gb == 99.0
    assert changed == {'remnawave_short_uuid', 'traffic_used_gb'}


def test_status_can_be_frozen_for_a_subscription_just_touched_by_a_webhook():
    """Свежая оплата важнее любого снимка панели."""
    subscription = _sub(status=SubscriptionStatus.ACTIVE.value)

    project_onto_subscription(
        subscription,
        PanelSnapshot(status='DISABLED', traffic_used_gb=3.0),
        now=NOW,
        trust_status=False,
    )

    assert subscription.status == SubscriptionStatus.ACTIVE.value
    assert subscription.traffic_used_gb == 3.0


# ==================== снимок, которому нельзя верить на слово ====================

# Полный проход по панели выгружает весь список и применяет его минутами позже.
# К этому моменту подписка могла быть оплачена — и доверчивое применение снимка
# откатывало её в LIMITED/EXPIRED и отправляло в грейс.


def test_stale_snapshot_still_takes_the_date_of_a_live_account():
    """Продление, сделанное руками в панели, бот обязан увидеть.

    Иначе его же обратный проход затрёт панель старой датой. От снимка, который
    старше правки в боте, защищает возраст снимка, а не отказ от даты.
    """
    subscription = _sub()

    project_onto_subscription(
        subscription,
        PanelSnapshot(status='ACTIVE', expire_at=NOW + timedelta(days=90)),
        now=NOW,
        policy=BULK_SNAPSHOT,
    )

    assert subscription.end_date == NOW + timedelta(days=90)


def test_snapshot_limited_is_limited_whatever_the_bot_counted():
    """Панель — истина: «исчерпана» переносится, даже если счётчик бота отстал.

    Только что оплаченную подписку от старого снимка защищает его возраст
    (``snapshot_taken_at``, тесты ниже), а не счётчики трафика.
    """
    fresh = _sub(traffic_used_gb=1.0, traffic_limit_gb=100)

    project_onto_subscription(fresh, PanelSnapshot(status='LIMITED'), now=NOW, policy=BULK_SNAPSHOT)

    assert fresh.status == SubscriptionStatus.LIMITED.value


def test_snapshot_limited_overrides_a_locally_disabled_row():
    subscription = _sub(status=SubscriptionStatus.DISABLED.value)

    project_onto_subscription(subscription, PanelSnapshot(status='LIMITED'), now=NOW, policy=BULK_SNAPSHOT)

    assert subscription.status == SubscriptionStatus.LIMITED.value


def test_snapshot_expired_is_expired_whatever_the_bot_date_says():
    """Панель гасит аккаунт сама и знает об этом лучше бота."""
    renewed = _sub(end_date=NOW + timedelta(days=30))

    project_onto_subscription(renewed, PanelSnapshot(status='EXPIRED'), now=NOW, policy=BULK_SNAPSHOT)

    assert renewed.status == SubscriptionStatus.EXPIRED.value


def test_stale_disabled_is_applied():
    """DISABLED — решение админа в панели, и донести его больше некому.

    У многих установок вебхуков нет, и полный проход — единственный путь. От
    применения поверх свежей правки защищает не отказ от статуса, а возраст
    снимка (см. тесты ниже).
    """
    subscription = _sub()

    project_onto_subscription(subscription, PanelSnapshot(status='DISABLED'), now=NOW, policy=BULK_SNAPSHOT)

    assert subscription.status == SubscriptionStatus.DISABLED.value


# ==================== снимок старше правки ====================

# Полный проход выгружает весь список панели и применяет его минутами позже.
# Если подписку за это время оплатили или продлили, снимок про неё уже врёт.


def test_a_snapshot_older_than_the_row_does_not_touch_billing_fields():
    subscription = _sub(status=SubscriptionStatus.ACTIVE.value)
    subscription.updated_at = NOW  # правка пришла уже после снимка

    changed = project_onto_subscription(
        subscription,
        PanelSnapshot(status='DISABLED', expire_at=NOW - timedelta(days=5), traffic_used_gb=9.0),
        now=NOW,
        policy=BULK_SNAPSHOT,
        snapshot_taken_at=NOW - timedelta(minutes=3),
    )

    assert subscription.status == SubscriptionStatus.ACTIVE.value
    assert subscription.end_date == NOW + timedelta(days=30)
    assert subscription.traffic_used_gb == 9.0, 'расход всё равно показываем'
    assert changed == {'traffic_used_gb'}


def test_a_snapshot_newer_than_the_row_is_applied():
    subscription = _sub(status=SubscriptionStatus.ACTIVE.value)
    subscription.updated_at = NOW - timedelta(hours=2)

    project_onto_subscription(
        subscription,
        PanelSnapshot(status='DISABLED'),
        now=NOW,
        policy=BULK_SNAPSHOT,
        snapshot_taken_at=NOW - timedelta(minutes=3),
    )

    assert subscription.status == SubscriptionStatus.DISABLED.value


def test_a_webhook_stamp_also_counts_as_a_fresh_change():
    subscription = _sub(status=SubscriptionStatus.ACTIVE.value)
    subscription.updated_at = NOW - timedelta(hours=2)
    subscription.last_webhook_update_at = NOW

    project_onto_subscription(
        subscription,
        PanelSnapshot(status='DISABLED'),
        now=NOW,
        policy=BULK_SNAPSHOT,
        snapshot_taken_at=NOW - timedelta(minutes=3),
    )

    assert subscription.status == SubscriptionStatus.ACTIVE.value


def test_stale_snapshot_still_carries_traffic_and_links():
    subscription = _sub()

    project_onto_subscription(
        subscription,
        PanelSnapshot(status='ACTIVE', traffic_used_gb=5.0, subscription_url='https://new'),
        now=NOW,
        policy=BULK_SNAPSHOT,
    )

    assert subscription.traffic_used_gb == 5.0
    assert subscription.subscription_url == 'https://new'


# ==================== админ нажал «из панели в бота» ====================

# Здесь панель побеждает: админ этого и хочет. Единственный режим, где правка в
# панели меняет оплаченный тариф.


def test_admin_pull_takes_the_date_even_from_a_disabled_account():
    """Отключённый в панели остаётся отключённым, даже если дата прошла: так решила панель."""
    subscription = _sub()

    project_onto_subscription(
        subscription,
        PanelSnapshot(status='DISABLED', expire_at=NOW - timedelta(days=2)),
        now=NOW,
        policy=ADMIN_PULL,
    )

    assert subscription.end_date == NOW - timedelta(days=2)
    assert subscription.status == SubscriptionStatus.DISABLED.value


def test_admin_pull_takes_the_limits_from_the_panel():
    subscription = _sub()

    project_onto_subscription(
        subscription,
        PanelSnapshot(status='ACTIVE', expire_at=NOW + timedelta(days=30), traffic_limit_gb=500, device_limit=10),
        now=NOW,
        policy=ADMIN_PULL,
    )

    assert subscription.traffic_limit_gb == 500
    assert subscription.device_limit == 10


def test_all_policies_but_webhook_are_the_same_panel_truth():
    """Кнопка «из панели в бота», полный проход и фоновое чтение верят панели одинаково."""
    from app.services.panel_sync import ROUTINE

    for policy in (ROUTINE, BULK_SNAPSHOT, ADMIN_PULL):
        assert (policy.takes_date, policy.date_only_from_active, policy.status_mode) == (True, False, 'panel_truth'), (
            policy.name
        )
        assert policy.takes_traffic_limit and policy.takes_device_limit, policy.name


def test_panel_active_without_a_date_keeps_the_status():
    """Дату не разобрали — статус не трогаем, а не гасим наугад."""
    subscription = _sub()

    project_onto_subscription(subscription, PanelSnapshot(status='ACTIVE', expire_at=None), now=NOW, policy=ADMIN_PULL)

    assert subscription.status == SubscriptionStatus.ACTIVE.value


def test_reads_limits_from_both_shapes_of_the_answer():
    from_dict = read_panel_user({'trafficLimitBytes': 10 * 1024**3, 'hwidDeviceLimit': 4})
    from_object = read_panel_user(SimpleNamespace(traffic_limit_bytes=10 * 1024**3, hwid_device_limit=4))

    assert from_dict.traffic_limit_gb == 10 and from_dict.device_limit == 4
    assert from_object.traffic_limit_gb == 10 and from_object.device_limit == 4


# ==================== хвост грейса ====================
#
# После грейса в панели остаётся его дата: прошедшую дату PATCH не принимает, а
# вернуть настоящую нельзя. Бот запоминает эту дату на подписке
# (``grace_tail_expire_at``). Импорт «панель — истина», увидев в панели ровно её,
# не двигает дату окончания и статус в боте — иначе истёкшая подписка «истекала»
# заново в конец грейса, воркер видел свежее истечение и выдавал грейс снова.


def test_open_grace_marked_on_the_subscription_is_never_imported():
    """Баг 2026-09-15: мониторинг, гася истёкшую подписку, спросил панель «может,
    продлили?» — увидел ACTIVE до конца грейса и перенёс в бота дату, статус,
    сквад грейса и лимит «расход + 1 ГБ». Воркер решил «человек продлил».

    Признак открытого грейса лежит на самой подписке — ни одному вызывающему не
    нужно помнить про ``grace_open``.
    """
    grace_until = NOW + timedelta(hours=72)
    subscription = _sub(
        status=SubscriptionStatus.ACTIVE.value,
        end_date=NOW - timedelta(minutes=30),
        traffic_limit_gb=0,
        connected_squads=['tariff-squad'],
        grace_session_open=True,
    )

    for policy in (BULK_SNAPSHOT, WEBHOOK, ADMIN_PULL):
        changed = project_onto_subscription(
            subscription,
            PanelSnapshot(
                status='ACTIVE',
                expire_at=grace_until,
                traffic_used_gb=102.2,
                traffic_limit_gb=103,
                squads=('grace-squad',),
            ),
            now=NOW,
            policy=policy,
        )

        assert subscription.end_date == NOW - timedelta(minutes=30)
        assert subscription.status == SubscriptionStatus.ACTIVE.value
        assert subscription.traffic_limit_gb == 0
        assert subscription.connected_squads == ['tariff-squad']
        # Расход настоящий — его переносим и во время грейса.
        assert subscription.traffic_used_gb == 102.2
        assert changed <= {'traffic_used_gb'}


def test_grace_tail_date_is_not_imported_after_grace_ended():
    tail = NOW - timedelta(minutes=1)
    subscription = _sub(
        status=SubscriptionStatus.EXPIRED.value,
        end_date=NOW - timedelta(days=3),
        grace_tail_expire_at=tail,
    )

    changed = project_onto_subscription(
        subscription,
        PanelSnapshot(status='EXPIRED', expire_at=tail, traffic_used_gb=7.0, short_uuid='new-short'),
        now=NOW,
    )

    assert subscription.end_date == NOW - timedelta(days=3)
    assert subscription.status == SubscriptionStatus.EXPIRED.value
    assert subscription.traffic_used_gb == 7.0
    assert subscription.remnawave_short_uuid == 'new-short'
    assert changed == {'traffic_used_gb', 'remnawave_short_uuid'}


def test_grace_tail_masks_the_status_while_the_panel_is_still_closing():
    """Погашенная дата стоит на несколько минут вперёд: панель ещё ACTIVE."""
    tail = NOW + timedelta(minutes=5)
    subscription = _sub(
        status=SubscriptionStatus.EXPIRED.value,
        end_date=NOW - timedelta(days=3),
        grace_tail_expire_at=tail,
    )

    for policy in (BULK_SNAPSHOT, WEBHOOK):
        project_onto_subscription(
            subscription,
            PanelSnapshot(status='ACTIVE', expire_at=tail, traffic_used_gb=1.0),
            now=NOW,
            policy=policy,
        )

        assert subscription.status == SubscriptionStatus.EXPIRED.value
        assert subscription.end_date == NOW - timedelta(days=3)


def test_a_real_panel_renewal_after_grace_is_still_imported():
    subscription = _sub(
        status=SubscriptionStatus.EXPIRED.value,
        end_date=NOW - timedelta(days=3),
        grace_tail_expire_at=NOW - timedelta(minutes=1),
    )

    project_onto_subscription(
        subscription,
        PanelSnapshot(status='ACTIVE', expire_at=NOW + timedelta(days=30), traffic_used_gb=1.0),
        now=NOW,
    )

    assert subscription.status == SubscriptionStatus.ACTIVE.value
    assert subscription.end_date == NOW + timedelta(days=30)


def test_grace_tail_tolerates_the_panel_millisecond_rounding():
    tail = NOW - timedelta(minutes=1)
    subscription = _sub(
        status=SubscriptionStatus.EXPIRED.value,
        end_date=NOW - timedelta(days=3),
        grace_tail_expire_at=tail,
    )

    project_onto_subscription(
        subscription,
        PanelSnapshot(status='EXPIRED', expire_at=tail + timedelta(milliseconds=800)),
        now=NOW,
    )

    assert subscription.end_date == NOW - timedelta(days=3)


def test_trial_that_ended_in_grace_is_expired_by_the_panel_status_in_the_tail():
    """Стенд 2026-09-15: у триалов и суточных после грейса статус оставался «trial»/«active».

    Платные гасит мониторинг по своей дате, а триал и суточную — только импорт статуса
    панели. Хвост грейса блокировал его навсегда. Дату хвоста по-прежнему не берём,
    но «истекла» — правда: панель погасила аккаунт, и собственный срок подписки вышел.
    """
    tail = NOW - timedelta(minutes=1)
    subscription = _sub(
        status=SubscriptionStatus.TRIAL.value,
        end_date=NOW - timedelta(days=3),
        grace_tail_expire_at=tail,
    )

    changed = project_onto_subscription(
        subscription,
        PanelSnapshot(status='EXPIRED', expire_at=tail, traffic_used_gb=7.0),
        now=NOW,
        policy=BULK_SNAPSHOT,
    )

    assert subscription.status == SubscriptionStatus.EXPIRED.value
    assert subscription.end_date == NOW - timedelta(days=3), 'дата хвоста грейса не переносится'
    assert 'status' in changed
    # Этот инцидент грейс уже получил — повторно в кандидаты не метим.
    assert subscription.grace_candidate_reason is None


def test_grace_tail_never_expires_a_subscription_whose_own_term_is_still_running():
    """Продлили в боте, а в панели ещё хвост с EXPIRED (запись в панель не прошла) — не гасим."""
    tail = NOW - timedelta(minutes=1)
    subscription = _sub(
        status=SubscriptionStatus.ACTIVE.value,
        end_date=NOW + timedelta(days=30),
        grace_tail_expire_at=tail,
    )

    project_onto_subscription(subscription, PanelSnapshot(status='EXPIRED', expire_at=tail), now=NOW)

    assert subscription.status == SubscriptionStatus.ACTIVE.value
    assert subscription.end_date == NOW + timedelta(days=30)


def test_webhook_in_the_grace_tail_still_does_not_declare_expiry():
    """Вебхук истечение не объявляет (это работа мониторинга) — и в хвосте тоже."""
    tail = NOW - timedelta(minutes=1)
    subscription = _sub(
        status=SubscriptionStatus.TRIAL.value,
        end_date=NOW - timedelta(days=3),
        grace_tail_expire_at=tail,
    )

    project_onto_subscription(subscription, PanelSnapshot(status='EXPIRED', expire_at=tail), now=NOW, policy=WEBHOOK)

    assert subscription.status == SubscriptionStatus.TRIAL.value


def test_overlay_snapshot_processed_after_an_early_grace_close_is_not_imported():
    """Ревью 2026-09-15: снимок сняли при открытом грейсе, обработали после досрочного закрытия.

    Признак снят, хвост — «ближайший допустимый момент», а не дата оверлея, — и
    раньше оверлей переносился в подписку. Дата оверлея на подписке его узнаёт.
    """
    overlay_until = NOW + timedelta(hours=71)
    subscription = _sub(
        status=SubscriptionStatus.EXPIRED.value,
        end_date=NOW - timedelta(hours=1),
        traffic_limit_gb=0,
        connected_squads=['tariff-squad'],
        grace_session_open=False,
        grace_tail_expire_at=NOW + timedelta(minutes=5),
        grace_overlay_expire_at=overlay_until,
    )

    for policy in (BULK_SNAPSHOT, WEBHOOK, ADMIN_PULL):
        project_onto_subscription(
            subscription,
            PanelSnapshot(
                status='ACTIVE',
                expire_at=overlay_until + timedelta(milliseconds=700),
                traffic_used_gb=7.3,
                traffic_limit_gb=8,
                squads=('grace-squad',),
            ),
            now=NOW,
            policy=policy,
        )

        assert subscription.status == SubscriptionStatus.EXPIRED.value
        assert subscription.end_date == NOW - timedelta(hours=1)
        assert subscription.connected_squads == ['tariff-squad']
        assert subscription.traffic_limit_gb == 0


def test_stale_bulk_snapshot_does_not_roll_back_squads_either():
    """Полный проход: подписку изменили после снимка — сквады снимка тоже устарели."""
    subscription = _sub(
        connected_squads=['new-tariff-squad'],
        updated_at=NOW,
    )

    changed = project_onto_subscription(
        subscription,
        PanelSnapshot(status='ACTIVE', expire_at=NOW + timedelta(days=3), squads=('old-squad',)),
        now=NOW,
        policy=BULK_SNAPSHOT,
        snapshot_taken_at=NOW - timedelta(minutes=2),
    )

    assert subscription.connected_squads == ['new-tariff-squad']
    assert 'connected_squads' not in changed
