"""Audience rules, counts, and delivery use one selection path."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.routes.admin_broadcasts import (
    CUSTOM_FILTER_GROUPS,
    EMAIL_FILTER_GROUPS,
    FILTER_GROUPS,
    create_combined_broadcast,
    preview_audience,
    search_audience_users,
)
from app.cabinet.schemas.broadcasts import (
    BroadcastAudience,
    BroadcastAudienceCondition,
    BroadcastAudiencePreviewRequest,
    CombinedBroadcastCreateRequest,
)
from app.database.models import BroadcastHistory, Subscription, SubscriptionStatus, Tariff, User, UserStatus
from app.services import broadcast_service
from app.services.broadcast_audience import (
    EMAIL_FIELDS,
    TELEGRAM_FIELDS,
    audience_user_query,
    preview_audience_users,
    select_audience_users,
    validate_audience,
)
from tests.fixtures.sqlite_memory import memory_session


TABLES = (User.__table__, Tariff.__table__, Subscription.__table__)


def test_every_existing_dropdown_option_is_available_as_a_condition() -> None:
    for key, group in {**FILTER_GROUPS, **CUSTOM_FILTER_GROUPS}.items():
        assert key in TELEGRAM_FIELDS[group]
    for key, group in EMAIL_FILTER_GROUPS.items():
        assert key in EMAIL_FIELDS[group]


def rule(field: str, value: str, *, operator: str = 'eq', join: str | None = None) -> BroadcastAudienceCondition:
    return BroadcastAudienceCondition(field=field, value=value, operator=operator, join=join)


def _user(telegram_id: int, **values) -> User:
    return User(telegram_id=telegram_id, status=UserStatus.ACTIVE.value, **values)


def _sub(user: User, *, tariff_id: int | None = None, trial: bool = False, traffic: float = 1) -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        user_id=user.id,
        tariff_id=tariff_id,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=trial,
        start_date=now - timedelta(days=1),
        end_date=now + timedelta(days=30),
        traffic_used_gb=traffic,
        remnawave_short_id=f'audience-{user.id}-{tariff_id or 0}',
    )


@pytest.mark.asyncio
async def test_rows_are_evaluated_strictly_from_top_to_bottom(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        now = datetime.now(UTC)
        recent_without_sub = _user(1001, created_at=now)
        referred_with_sub = _user(1002, created_at=now - timedelta(days=40), referred_by_id=99)
        recent_with_sub = _user(1003, created_at=now)
        referred_without_sub = _user(1004, created_at=now - timedelta(days=40), referred_by_id=99)
        active_only = _user(1005, created_at=now - timedelta(days=40))
        db.add_all([recent_without_sub, referred_with_sub, recent_with_sub, referred_without_sub, active_only])
        await db.flush()
        db.add_all([_sub(referred_with_sub), _sub(recent_with_sub), _sub(active_only)])
        await db.commit()

        audience = BroadcastAudience(
            conditions=[
                rule('registration', 'custom_today'),
                rule('source', 'custom_referrals', join='or'),
                rule('subscription', 'active', join='and'),
            ]
        )
        users = await select_audience_users(db, audience, 'telegram', 'system')

    assert [user.telegram_id for user in users] == [1002, 1003]


@pytest.mark.asyncio
async def test_long_mixed_audience_keeps_top_down_results_and_compiles(monkeypatch) -> None:
    fields = (
        ('registration', 'custom_today'),
        ('source', 'custom_referrals'),
        ('activity', 'custom_active_today'),
        ('activity', 'custom_inactive_week'),
    )
    conditions = [
        rule(
            *fields[index % len(fields)],
            operator='ne' if index % 5 == 0 else 'eq',
            join=None if index == 0 else 'or' if index % 2 else 'and',
        )
        for index in range(250)
    ]
    audience = BroadcastAudience(conditions=conditions)

    # The production dialect must compile the whole audience without relying
    # on Python's recursion limit, including alternating AND/OR joins.
    audience_user_query(audience, 'telegram').compile(dialect=postgresql.dialect())

    facts = {
        1061: (True, False, True, False),
        1062: (False, True, False, True),
        1063: (True, True, False, False),
        1064: (False, False, True, False),
    }

    def expected_for(user_id: int) -> bool:
        flags = facts[user_id]
        selected = False
        for index, condition in enumerate(conditions):
            matches = flags[index % len(flags)]
            if condition.operator == 'ne':
                matches = not matches
            if index == 0:
                selected = matches
            elif condition.join == 'or':
                selected = selected or matches
            else:
                selected = selected and matches
        return selected

    async with memory_session(monkeypatch, TABLES) as db:
        now = datetime.now(UTC)
        users = [
            _user(1061, created_at=now, last_activity=now),
            _user(1062, created_at=now - timedelta(days=30), last_activity=now - timedelta(days=30), referred_by_id=99),
            _user(1063, created_at=now, referred_by_id=99),
            _user(1064, created_at=now - timedelta(days=30), last_activity=now),
        ]
        db.add_all(users)
        await db.flush()
        await db.execute(update(User).where(User.telegram_id == 1063).values(last_activity=None))
        await db.commit()
        selected_users = await select_audience_users(db, audience, 'telegram', 'system')
        preview_count, preview_page = await preview_audience_users(db, audience, 'telegram', 'system', 1, 2)

    expected_ids = [user_id for user_id in facts if expected_for(user_id)]
    assert [user.telegram_id for user in selected_users] == expected_ids
    assert preview_count == len(expected_ids)
    assert [user.telegram_id for user in preview_page] == expected_ids[1:3]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('value', 'expected_equal', 'expected_not_equal'),
    [
        ('custom_active_today', [1052], [1051, 1053]),
        ('custom_inactive_week', [1053], [1051, 1052]),
    ],
)
async def test_not_equal_activity_includes_users_without_activity(
    monkeypatch, value: str, expected_equal: list[int], expected_not_equal: list[int]
) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        now = datetime.now(UTC)
        missing = _user(1051)
        recent = _user(1052, last_activity=now)
        old = _user(1053, last_activity=now - timedelta(days=30))
        db.add_all([missing, recent, old])
        await db.flush()
        # Explicit UPDATE is required: the model's INSERT default fills a
        # missing last_activity with the current time.
        await db.execute(update(User).where(User.id == missing.id).values(last_activity=None))
        await db.commit()

        equal = BroadcastAudience(conditions=[rule('activity', value)])
        not_equal = BroadcastAudience(conditions=[rule('activity', value, operator='ne')])
        equal_users = await select_audience_users(db, equal, 'telegram', 'system')
        not_equal_users = await select_audience_users(db, not_equal, 'telegram', 'system')

    assert [user.telegram_id for user in equal_users] == expected_equal
    assert [user.telegram_id for user in not_equal_users] == expected_not_equal


@pytest.mark.asyncio
async def test_not_equal_tariff_excludes_every_user_with_any_matching_subscription(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([Tariff(id=1, name='X'), Tariff(id=2, name='Y')])
        mixed = _user(1101)
        other = _user(1102)
        no_subscription = _user(1103)
        trial_tariff = _user(1104)
        db.add_all([mixed, other, no_subscription, trial_tariff])
        await db.flush()
        db.add_all([_sub(mixed, tariff_id=1), _sub(mixed, tariff_id=2), _sub(other, tariff_id=2)])
        trial = _sub(trial_tariff, tariff_id=1, trial=True)
        trial.status = SubscriptionStatus.TRIAL.value
        db.add(trial)
        await db.commit()

        audience = BroadcastAudience(conditions=[rule('tariff', 'tariff_1', operator='ne')])
        validate_audience(audience, 'telegram', {1, 2})
        users = await select_audience_users(db, audience, 'telegram', 'system')

    assert [user.telegram_id for user in users] == [1102, 1103]


@pytest.mark.asyncio
async def test_expiring_preserves_daily_tariff_exclusion(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([Tariff(id=1, name='Daily', is_daily=True), Tariff(id=2, name='Regular', is_daily=False)])
        daily = _user(1151)
        paused_daily = _user(1152)
        regular = _user(1153)
        db.add_all([daily, paused_daily, regular])
        await db.flush()
        for user, tariff_id, paused in ((daily, 1, False), (paused_daily, 1, True), (regular, 2, False)):
            subscription = _sub(user, tariff_id=tariff_id)
            subscription.end_date = datetime.now(UTC) + timedelta(days=1)
            subscription.is_daily_paused = paused
            db.add(subscription)
        await db.commit()

        audience = BroadcastAudience(conditions=[rule('subscription', 'expiring')])
        users = await select_audience_users(db, audience, 'telegram', 'system')

    assert [user.telegram_id for user in users] == [1152, 1153]


@pytest.mark.asyncio
async def test_preview_matches_telegram_and_email_delivery_after_preferences(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        allowed = _user(1201, email='allowed@example.com', email_verified=True, notification_settings={})
        opted_out = _user(
            1202,
            email='out@example.com',
            email_verified=True,
            notification_settings={'news_enabled': False},
        )
        no_email = _user(1203, email='unverified@example.com', email_verified=False)
        db.add_all([allowed, opted_out, no_email])
        await db.flush()
        db.add_all([_sub(allowed, traffic=0), _sub(opted_out, traffic=0), _sub(no_email, traffic=0)])
        await db.commit()

        bind = db.bind

        @asynccontextmanager
        async def sessions():
            async with AsyncSession(bind, expire_on_commit=False) as session:
                yield session

        monkeypatch.setattr(broadcast_service, 'AsyncSessionLocal', sessions)

        telegram_audience = BroadcastAudience(conditions=[rule('traffic', 'zero')])
        tg_preview = await preview_audience(
            BroadcastAudiencePreviewRequest(channel='telegram', category='news', audience=telegram_audience),
            admin=allowed,
            db=db,
        )
        tg_second_page = await preview_audience(
            BroadcastAudiencePreviewRequest(
                channel='telegram', category='news', audience=telegram_audience, offset=1, limit=1
            ),
            admin=allowed,
            db=db,
        )
        system_page = await preview_audience(
            BroadcastAudiencePreviewRequest(
                channel='telegram', category='system', audience=telegram_audience, offset=1, limit=1
            ),
            admin=allowed,
            db=db,
        )
        tg_delivery = await broadcast_service.broadcast_service._fetch_recipients('audience', 'news', telegram_audience)

        email_audience = BroadcastAudience(conditions=[rule('basic', 'all_email')])
        email_preview = await preview_audience(
            BroadcastAudiencePreviewRequest(channel='email', category='news', audience=email_audience),
            admin=allowed,
            db=db,
        )
        email_delivery = await broadcast_service.email_broadcast_service._fetch_email_recipients(
            'audience', 'news', email_audience
        )

        # The preview is not a frozen recipient snapshot. Delivery resolves again.
        allowed.notification_settings = {'news_enabled': False}
        await db.commit()
        delivery_after_change = await broadcast_service.broadcast_service._fetch_recipients(
            'audience', 'news', telegram_audience
        )

    assert tg_preview.count == len(tg_delivery) == 2
    assert [user.telegram_id for user in tg_preview.users] == tg_delivery == [1201, 1203]
    assert tg_second_page.count == 2
    assert [user.telegram_id for user in tg_second_page.users] == [1203]
    assert system_page.count == 3
    assert [user.telegram_id for user in system_page.users] == [1202]
    assert email_preview.count == len(email_delivery) == 1
    assert (
        [user.email for user in email_preview.users]
        == [recipient.email for recipient in email_delivery]
        == ['allowed@example.com']
    )
    assert delivery_after_change == [1203]


@pytest.mark.asyncio
async def test_send_stores_rules_and_passes_them_to_delivery(monkeypatch) -> None:
    tables = (*TABLES, BroadcastHistory.__table__)
    async with memory_session(monkeypatch, tables) as db:
        admin = _user(1251, username='admin')
        db.add(admin)
        await db.commit()
        captured = []

        async def start_broadcast(_broadcast_id, config):
            captured.append(config)

        monkeypatch.setattr(broadcast_service.broadcast_service, 'start_broadcast', start_broadcast)
        audience = BroadcastAudience(conditions=[rule('traffic', 'zero')])
        result = await create_combined_broadcast(
            CombinedBroadcastCreateRequest(channel='telegram', audience=audience, message_text='Hello'),
            admin=admin,
            db=db,
        )
        stored = await db.get(BroadcastHistory, result.id)

    assert stored is not None
    assert stored.audience == audience.model_dump(mode='json')
    assert result.audience == audience
    assert captured[0].audience == audience


@pytest.mark.asyncio
async def test_email_send_uses_its_own_audience(monkeypatch) -> None:
    tables = (*TABLES, BroadcastHistory.__table__)
    async with memory_session(monkeypatch, tables) as db:
        admin = _user(1261, username='admin')
        db.add(admin)
        await db.commit()
        captured = []

        async def start_broadcast(_broadcast_id, config):
            captured.append(config)

        monkeypatch.setattr(broadcast_service.email_broadcast_service, 'start_broadcast', start_broadcast)
        audience = BroadcastAudience(conditions=[rule('auth_type', 'email_only')])
        result = await create_combined_broadcast(
            CombinedBroadcastCreateRequest(
                channel='email',
                audience=audience,
                email_subject='Subject',
                email_html_content='<p>Body</p>',
            ),
            admin=admin,
            db=db,
        )

    assert result.channel == 'email'
    assert result.audience == audience
    assert captured[0].audience == audience


@pytest.mark.asyncio
async def test_preview_rejects_mismatched_field_and_value(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        audience = BroadcastAudience(conditions=[rule('traffic', 'active')])
        with pytest.raises(HTTPException) as error:
            await preview_audience(
                BroadcastAudiencePreviewRequest(channel='telegram', audience=audience), admin=_user(1301), db=db
            )
    assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_atomic_subscription_conditions_can_match_different_subscriptions(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([Tariff(id=1, name='X'), Tariff(id=2, name='Y')])
        selected = _user(1401, email='selected@example.com', email_verified=True)
        only_trial = _user(1402, email='trial@example.com', email_verified=True)
        db.add_all([selected, only_trial])
        await db.flush()
        db.add_all(
            [
                _sub(selected, tariff_id=1, trial=False),
                _sub(selected, tariff_id=2, trial=True),
                _sub(only_trial, tariff_id=2, trial=True),
            ]
        )
        await db.commit()

        audience = BroadcastAudience(
            conditions=[
                rule('subscription_status', 'active'),
                rule('subscription_type', 'trial', join='and'),
                rule('tariff', 'tariff_1', join='and'),
            ]
        )
        validate_audience(audience, 'email', {1, 2})
        selected_users = await select_audience_users(db, audience, 'email', 'system')

    assert [user.email for user in selected_users] == ['selected@example.com']


@pytest.mark.asyncio
async def test_expired_status_excludes_any_live_subscription_including_trial(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        expired_only = _user(1411)
        mixed = _user(1412)
        db.add_all([expired_only, mixed])
        await db.flush()
        for user in (expired_only, mixed):
            subscription = _sub(user, tariff_id=1)
            subscription.status = SubscriptionStatus.EXPIRED.value
            subscription.end_date = datetime.now(UTC) - timedelta(days=1)
            db.add(subscription)
        trial = _sub(mixed, tariff_id=2, trial=True)
        trial.status = SubscriptionStatus.TRIAL.value
        db.add(trial)
        await db.commit()
        audience = BroadcastAudience(conditions=[rule('subscription_status', 'expired')])
        selected = await select_audience_users(db, audience, 'telegram', 'system')

    assert [user.telegram_id for user in selected] == [1411]


@pytest.mark.asyncio
async def test_traffic_thresholds_dates_and_zero_are_independent(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = _user(1421, created_at=datetime(2026, 9, 20, tzinfo=UTC))
        db.add(user)
        await db.flush()
        db.add_all([_sub(user, tariff_id=1, traffic=0), _sub(user, tariff_id=2, traffic=15)])
        await db.commit()
        audience = BroadcastAudience(
            conditions=[
                rule('traffic_zero', 'zero'),
                rule('traffic_gt', '10', join='and'),
                rule('traffic_lt', '1', join='and'),
                BroadcastAudienceCondition(
                    field='registration_date', operator='between', value='2026-09-20', value_to='2026-09-21', join='and'
                ),
            ]
        )
        validate_audience(audience, 'telegram', set())
        selected = await select_audience_users(db, audience, 'telegram', 'system')

    assert [person.telegram_id for person in selected] == [1421]


@pytest.mark.asyncio
async def test_user_autocomplete_matches_any_part_and_uses_stable_id(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(12347890, username='one', email='alpha478@example.com', email_verified=True),
                _user(478123, username='second', email='other@example.com', email_verified=True),
                _user(9999, username='not478here', email='third@example.com', email_verified=True),
            ]
        )
        await db.commit()
        ids = await search_audience_users('telegram_id', '478', 0, 20, admin=_user(1), db=db)
        names = await search_audience_users('telegram_username', '478', 0, 20, admin=_user(1), db=db)
        emails = await search_audience_users('email_user', '478', 0, 20, admin=_user(1), db=db)
        assert [user.telegram_id for user in ids.users] == [12347890, 478123]
        assert [user.telegram_id for user in names.users] == [9999]
        assert [user.email for user in emails.users] == ['alpha478@example.com']
        audience = BroadcastAudience(conditions=[rule('email_user', str(emails.users[0].id))])
        validate_audience(audience, 'email', set())
        selected = await select_audience_users(db, audience, 'email', 'system')
        selected_before_change = [user.email for user in selected]
        await db.execute(update(User).where(User.id == emails.users[0].id).values(email='new@example.com'))
        await db.commit()
        selected_after_change = await select_audience_users(db, audience, 'email', 'system')

    assert selected_before_change == ['alpha478@example.com']
    assert [user.email for user in selected_after_change] == ['new@example.com']


@pytest.mark.parametrize(
    ('condition', 'channel'),
    [
        (
            BroadcastAudienceCondition(
                field='registration_date', operator='between', value='2026-09-25', value_to='2026-09-24'
            ),
            'telegram',
        ),
        (BroadcastAudienceCondition(field='activity_date', operator='before', value='2026-02-30'), 'telegram'),
        (rule('traffic_gt', 'NaN'), 'telegram'),
        (rule('telegram_id', '2147483648'), 'telegram'),
        (rule('telegram_id', '1'), 'email'),
        (rule('email_user', '1'), 'telegram'),
    ],
)
def test_atomic_audience_rejects_invalid_values_and_cross_channel_fields(
    condition: BroadcastAudienceCondition, channel: str
) -> None:
    with pytest.raises(ValueError):
        validate_audience(BroadcastAudience(conditions=[condition]), channel, set())
