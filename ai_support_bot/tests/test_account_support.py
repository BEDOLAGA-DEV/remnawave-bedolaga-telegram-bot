from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ai_support_bot.app.services import account_support


@pytest_asyncio.fixture
async def accounts(monkeypatch, tmp_path):
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "accounts.db"}')
    Session = async_sessionmaker(engine)
    async with engine.begin() as db:
        for sql in [
            'CREATE TABLE users (id INTEGER PRIMARY KEY, telegram_id INTEGER, email TEXT)',
            'CREATE TABLE promocodes (id INTEGER PRIMARY KEY, code TEXT, is_active BOOLEAN, current_uses INTEGER, max_uses INTEGER, valid_from TEXT, valid_until TEXT)',
            'CREATE TABLE wheel_spins (id INTEGER PRIMARY KEY, user_id INTEGER, generated_promocode_id INTEGER, prize_type TEXT, created_at TEXT, is_applied BOOLEAN)',
            'CREATE TABLE subscriptions (id INTEGER PRIMARY KEY, user_id INTEGER, status TEXT, end_date TEXT, is_trial BOOLEAN, created_at TEXT, subscription_url TEXT)',
        ]:
            await db.execute(text(sql))
        await db.execute(text("INSERT INTO users VALUES (1, 42, 'owner@example.com'), (2, 99, 'other@example.com')"))
        for uid, code in [(1, 'CUSTOM_PERSONAL_A'), (2, 'WHEEL_FOREIGN_B')]:
            await db.execute(text('INSERT INTO promocodes VALUES (:id, :code, true, 0, 1, :start, :end)'), {
                'id': uid, 'code': code, 'start': (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                'end': (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            })
            await db.execute(text("INSERT INTO wheel_spins VALUES (:id, :id, :id, 'promocode', :now, true)"),
                             {'id': uid, 'now': datetime.now(UTC).isoformat()})

    async def session():
        return Session()
    monkeypatch.setattr(account_support, 'get_main_session', session)
    yield Session
    await engine.dispose()


@pytest.mark.asyncio
async def test_rewards_use_spin_ownership_instead_of_prefix(accounts):
    result = await account_support.read_account_support(42, 'wheel')
    assert [r['code'] for r in result['rewards']] == ['CUSTOM_PERSONAL_A']
    answer, handoff = account_support.wheel_reward_answer(result)
    assert 'CUSTOM_PERSONAL_A' in answer and 'WHEEL_FOREIGN_B' not in answer
    assert not handoff


@pytest.mark.asyncio
@pytest.mark.parametrize('email', ['other@example.com', 'unknown@example.com'])
async def test_unbound_email_cannot_reveal_codes(accounts, email):
    result = await account_support.read_account_support(42, 'wheel', email)
    assert result == {'status': 'unverified'}
    answer, handoff = account_support.wheel_reward_answer(result)
    assert handoff and 'WHEEL_' not in answer and 'CUSTOM_' not in answer


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [
    'UPDATE promocodes SET current_uses = 1 WHERE id = 1',
    "UPDATE promocodes SET valid_until = '2000-01-01' WHERE id = 1",
    'UPDATE promocodes SET is_active = false WHERE id = 1',
])
async def test_unusable_reward_is_not_promised_as_active(accounts, change):
    async with accounts() as db:
        await db.execute(text(change))
        await db.commit()
    result = await account_support.read_account_support(42, 'wheel')
    assert not result['rewards'][0]['usable']
    answer, handoff = account_support.wheel_reward_answer(result)
    assert handoff and 'недоступен' in answer


@pytest.mark.asyncio
async def test_missing_or_unapplied_reward_is_handed_off(accounts):
    async with accounts() as db:
        await db.execute(text('UPDATE wheel_spins SET is_applied = false WHERE user_id = 1'))
        await db.commit()
    result = await account_support.read_account_support(42, 'wheel')
    assert result['rewards'] == []
    assert account_support.wheel_reward_answer(result)[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(('status', 'trial', 'offset', 'active'), [
    ('active', False, 1, True), ('active', False, -1, False),
    ('active', True, 1, False), ('expired', False, 1, False),
])
async def test_paid_access_requires_current_nontrial_subscription(accounts, status, trial, offset, active):
    async with accounts() as db:
        await db.execute(text('INSERT INTO subscriptions (id, user_id, status, end_date, is_trial, created_at) VALUES (1, 1, :status, :end, :trial, :now)'), {
            'status': status, 'end': (datetime.now(UTC) + timedelta(days=offset)).isoformat(),
            'trial': trial, 'now': datetime.now(UTC).isoformat(),
        })
        await db.commit()
    result = await account_support.read_account_support(42, 'access')
    assert result['active_paid_subscription'] is active


@pytest.mark.asyncio
async def test_identity_hosts_are_only_from_verified_owner_and_never_include_key(accounts):
    async with accounts() as db:
        await db.execute(text('INSERT INTO subscriptions (id, user_id, subscription_url) VALUES (1, 1, :own), (2, 2, :foreign)'),
                         {'own': 'https://custom.example/private-key?secret=123', 'foreign': 'https://foreign.example/private-other'})
        await db.commit()
    result = await account_support.read_account_support(42, 'identity')
    assert result == {'status': 'ok', 'subscription_hosts': ['custom.example']}
    assert await account_support.read_account_support(42, 'identity', 'other@example.com') == {'status': 'unverified'}
