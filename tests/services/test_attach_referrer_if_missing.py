"""Tests for `app.services.referral_service.attach_referrer_if_missing`.

Background — the bug this helper fixes
--------------------------------------
A new user clicks ``t.me/bot?start=ref_XYZ``, then immediately taps the
Telegram menu's "Open Cabinet" WebApp button. The cabinet's auth route
fires before the bot's /start handler finishes, so:

  1. cabinet creates the user row with ``referred_by_id=None``
     (pending_referral Redis key is not yet populated)
  2. /start handler runs LATER, sees ``db_user`` already exists, and
     used to skip the ``save_pending_referral`` call entirely
  3. Result: referrer is permanently dropped

The helper closes that race by exposing a single retroactive-attach
entry point used by every login path (bot /start, cabinet
initData / widget / OIDC). It must be:

  * **Idempotent** — calling it twice for the same user must not
    create duplicate ``referral_earning`` rows (the event fires only
    on the call that actually performs the attachment).
  * **Self-referral-safe** — checks ID, telegram_id, and email.
  * **Resilient** — a Redis or DB hiccup must not crash the caller.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.referral_service import attach_referrer_if_missing


def _user(
    *,
    user_id: int = 100,
    telegram_id: int | None = 555,
    referred_by_id: int | None = None,
    email: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        telegram_id=telegram_id,
        referred_by_id=referred_by_id,
        email=email,
    )


def _referrer(
    *,
    user_id: int = 200,
    telegram_id: int | None = 888,
    email: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        telegram_id=telegram_id,
        email=email,
    )


@pytest.fixture
def db() -> AsyncMock:
    session = AsyncMock()
    session.commit = AsyncMock(return_value=None)
    session.refresh = AsyncMock(return_value=None)
    session.rollback = AsyncMock(return_value=None)
    return session


class _BotCtxMgr:
    """Async context manager stand-in for `app.bot_factory.create_bot()`.

    The helper lazy-creates a bot via `create_bot()` when the caller
    omits one (cabinet routes don't have a bot in scope). Without
    mocking it, tests crash inside aiogram's real Bot init ("Token is
    invalid!"). Yielding a plain AsyncMock keeps the downstream
    `process_referral_registration` call working under the existing
    mocks.
    """

    def __init__(self, bot: AsyncMock | None = None) -> None:
        self._bot = bot or AsyncMock(name='lazy_bot')

    async def __aenter__(self) -> AsyncMock:
        return self._bot

    async def __aexit__(self, *_a: object) -> None:
        return None


@pytest.fixture(autouse=True)
def _mock_lazy_bot(monkeypatch: pytest.MonkeyPatch):
    """Replace `app.bot_factory.create_bot` with a no-op context manager
    so the helper's lazy-bot branch doesn't try to instantiate a real
    aiogram Bot. Tests that need to inspect the lazy creation override
    this with their own patch (see `test_helper_lazy_creates_bot_when_caller_omits_it`).
    """
    import app.bot_factory

    monkeypatch.setattr(app.bot_factory, 'create_bot', lambda: _BotCtxMgr(), raising=False)
    yield


# ---------------------------------------------------------------------------
# Idempotency — never double-attach, never double-fire the registration event.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_op_when_user_already_has_referrer(db: AsyncMock) -> None:
    user = _user(referred_by_id=999)

    with (
        patch('app.services.referral_service.get_pending_referral', AsyncMock(return_value=None)) as _gpr,
        patch('app.services.referral_service.process_referral_registration', AsyncMock()) as fire,
    ):
        result = await attach_referrer_if_missing(db, user, source='unit_test')

    assert result is None
    assert user.referred_by_id == 999, 'must not overwrite an existing referrer'
    db.commit.assert_not_called()
    fire.assert_not_called(), 'registration event must NOT fire when no attachment happens'


@pytest.mark.asyncio
async def test_no_op_when_no_pending_and_no_code(db: AsyncMock) -> None:
    user = _user()

    with (
        patch('app.services.referral_service.get_pending_referral', AsyncMock(return_value=None)),
        patch('app.services.referral_service.process_referral_registration', AsyncMock()) as fire,
    ):
        result = await attach_referrer_if_missing(db, user, source='unit_test')

    assert result is None
    assert user.referred_by_id is None
    fire.assert_not_called()


# ---------------------------------------------------------------------------
# Happy paths — explicit code, Redis fallback, both at once.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attaches_referrer_from_explicit_code(db: AsyncMock) -> None:
    user = _user()
    referrer = _referrer(user_id=200, telegram_id=888)

    with (
        patch('app.database.crud.user.get_user_by_referral_code', AsyncMock(return_value=referrer)),
        patch('app.services.referral_service.get_pending_referral', AsyncMock(return_value=None)) as gpr,
        patch('app.services.referral_service.clear_pending_referral', AsyncMock()),
        patch('app.services.referral_service.process_referral_registration', AsyncMock()) as fire,
    ):
        result = await attach_referrer_if_missing(db, user, referral_code='ABCD-EFGH', source='unit_test')

    assert result == 200
    assert user.referred_by_id == 200
    db.commit.assert_awaited_once()
    fire.assert_awaited_once(), 'registration event must fire exactly once on attach'
    gpr.assert_not_called(), 'Redis fallback should be skipped when explicit code resolves'


@pytest.mark.asyncio
async def test_attaches_referrer_from_redis_pending_when_no_code(db: AsyncMock) -> None:
    """REGRESSION: this is the exact race the user reported.

    Miniapp opened before /start finished → user row created with no
    referrer → /start later wrote pending_referral to Redis → on the
    NEXT cabinet request, the eager-attach helper picks it up.
    """
    user = _user()
    referrer = _referrer(user_id=200)

    with (
        patch(
            'app.services.referral_service.get_pending_referral',
            AsyncMock(return_value={'referrer_id': 200, 'referral_code': 'ABCD'}),
        ),
        patch('app.services.referral_service.get_user_by_id', AsyncMock(return_value=referrer)),
        patch('app.services.referral_service.clear_pending_referral', AsyncMock()) as clear,
        patch('app.services.referral_service.process_referral_registration', AsyncMock()) as fire,
    ):
        result = await attach_referrer_if_missing(db, user, source='unit_test')

    assert result == 200
    assert user.referred_by_id == 200
    db.commit.assert_awaited_once()
    fire.assert_awaited_once()
    clear.assert_awaited_once_with(555), 'pending_referral must be cleared after attach'


@pytest.mark.asyncio
async def test_explicit_code_takes_precedence_over_redis(db: AsyncMock) -> None:
    """Explicit URL/state-provided code wins over a stale Redis entry."""
    user = _user()
    code_referrer = _referrer(user_id=300, telegram_id=900)

    with (
        patch('app.database.crud.user.get_user_by_referral_code', AsyncMock(return_value=code_referrer)),
        patch(
            'app.services.referral_service.get_pending_referral',
            AsyncMock(return_value={'referrer_id': 999, 'referral_code': 'stale'}),
        ) as gpr,
        patch('app.services.referral_service.get_user_by_id', AsyncMock()) as gubi,
        patch('app.services.referral_service.clear_pending_referral', AsyncMock()),
        patch('app.services.referral_service.process_referral_registration', AsyncMock()),
    ):
        result = await attach_referrer_if_missing(db, user, referral_code='EXPLICIT-CODE', source='unit_test')

    assert result == 300, 'explicit code must take precedence over Redis pending'
    gpr.assert_not_called()
    gubi.assert_not_called()


# ---------------------------------------------------------------------------
# Self-referral guards — ID, telegram_id, email all must be checked.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejects_self_referral_by_id(db: AsyncMock) -> None:
    user = _user(user_id=100)
    self_referrer = _referrer(user_id=100, telegram_id=555)

    with (
        patch('app.database.crud.user.get_user_by_referral_code', AsyncMock(return_value=self_referrer)),
        patch('app.services.referral_service.process_referral_registration', AsyncMock()) as fire,
    ):
        result = await attach_referrer_if_missing(db, user, referral_code='X', source='unit_test')

    assert result is None
    assert user.referred_by_id is None
    fire.assert_not_called()


@pytest.mark.asyncio
async def test_rejects_self_referral_by_telegram_id(db: AsyncMock) -> None:
    """Different DB user IDs but same Telegram account → still self-referral."""
    user = _user(user_id=100, telegram_id=555)
    self_referrer = _referrer(user_id=200, telegram_id=555)

    with (
        patch('app.database.crud.user.get_user_by_referral_code', AsyncMock(return_value=self_referrer)),
        patch('app.services.referral_service.process_referral_registration', AsyncMock()) as fire,
    ):
        result = await attach_referrer_if_missing(db, user, referral_code='X', source='unit_test')

    assert result is None
    assert user.referred_by_id is None
    fire.assert_not_called()


@pytest.mark.asyncio
async def test_rejects_self_referral_by_email(db: AsyncMock) -> None:
    user = _user(email='Alice@Example.com')
    self_referrer = _referrer(user_id=200, email='alice@example.com')

    with (
        patch('app.database.crud.user.get_user_by_referral_code', AsyncMock(return_value=self_referrer)),
        patch('app.services.referral_service.process_referral_registration', AsyncMock()) as fire,
    ):
        result = await attach_referrer_if_missing(db, user, referral_code='X', source='unit_test')

    assert result is None
    assert user.referred_by_id is None
    fire.assert_not_called()


# ---------------------------------------------------------------------------
# Resilience — Redis / DB failures must not crash the caller.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_failure_rolls_back_and_returns_none(db: AsyncMock) -> None:
    """If the DB commit fails, the helper rolls back and reports None.

    The caller continues normally; the user is not stuck in a
    half-attached state.
    """
    user = _user()
    referrer = _referrer(user_id=200)
    db.commit = AsyncMock(side_effect=RuntimeError('connection lost'))

    with (
        patch('app.database.crud.user.get_user_by_referral_code', AsyncMock(return_value=referrer)),
        patch('app.services.referral_service.process_referral_registration', AsyncMock()) as fire,
    ):
        result = await attach_referrer_if_missing(db, user, referral_code='X', source='unit_test')

    assert result is None
    db.rollback.assert_awaited()
    fire.assert_not_called(), 'event must not fire when the commit failed'


@pytest.mark.asyncio
async def test_registration_event_failure_still_keeps_attachment(db: AsyncMock) -> None:
    """If process_referral_registration raises, the referrer attachment survives.

    The attach is the load-bearing part; losing the notification/event
    is a softer failure than losing the referrer link itself.
    """
    user = _user()
    referrer = _referrer(user_id=200)

    with (
        patch('app.database.crud.user.get_user_by_referral_code', AsyncMock(return_value=referrer)),
        patch('app.services.referral_service.clear_pending_referral', AsyncMock()),
        patch(
            'app.services.referral_service.process_referral_registration',
            AsyncMock(side_effect=RuntimeError('notification service down')),
        ),
    ):
        result = await attach_referrer_if_missing(db, user, referral_code='X', source='unit_test')

    assert result == 200, 'attach must be reported as successful even if event firing failed'
    assert user.referred_by_id == 200


@pytest.mark.asyncio
async def test_user_without_telegram_id_skips_redis_fallback(db: AsyncMock) -> None:
    """Email-only user (no telegram_id) must not query Redis."""
    user = _user(telegram_id=None)

    with (
        patch('app.services.referral_service.get_pending_referral', AsyncMock()) as gpr,
        patch('app.services.referral_service.process_referral_registration', AsyncMock()),
    ):
        result = await attach_referrer_if_missing(db, user, source='unit_test')

    assert result is None
    gpr.assert_not_called(), 'Redis pending key is telegram_id-scoped; no point in querying without one'


@pytest.mark.asyncio
async def test_invalid_pending_referrer_id_type_is_handled(db: AsyncMock) -> None:
    """Malformed Redis payload (referrer_id is a string that can't int())
    must not crash — fall through to None."""
    user = _user()

    with (
        patch(
            'app.services.referral_service.get_pending_referral',
            AsyncMock(return_value={'referrer_id': 'not-an-int', 'referral_code': 'X'}),
        ),
        patch('app.services.referral_service.get_user_by_id', AsyncMock()) as gubi,
        patch('app.services.referral_service.process_referral_registration', AsyncMock()),
    ):
        result = await attach_referrer_if_missing(db, user, source='unit_test')

    assert result is None
    gubi.assert_not_called()


# ---------------------------------------------------------------------------
# Cross-session TOCTOU: cabinet and bot use different AsyncSessionLocal
# instances. If they both fire concurrently, both pass the
# ``referred_by_id is None`` guard. The duplicate-protection lives in
# `process_referral_registration` itself (SELECT before INSERT). These
# tests pin that contract — without them, a regression that drops the
# SELECT could silently start creating duplicate `referral_earning`
# audit rows in production.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_referral_registration_skips_duplicate_pending_row() -> None:
    """REGRESSION: a second call for the same (referrer, referral) must NOT
    insert another `referral_registration_pending` row.

    This is the cross-session race guard. Two concurrent attaches both
    pass the in-memory idempotency check but only one can win at the
    audit-row layer.
    """
    from app.services.referral_service import process_referral_registration

    db = AsyncMock()

    new_user = SimpleNamespace(id=10, telegram_id=1, referred_by_id=20, language='ru', first_name='', email=None)
    referrer = SimpleNamespace(id=20, telegram_id=2, language='ru', first_name='Inviter', email=None)

    # SELECT existing pending row → returns a row (it already exists).
    existing_row = AsyncMock()
    existing_row.scalar_one_or_none = lambda: 999  # pretend earning #999 exists
    db.execute = AsyncMock(return_value=existing_row)

    with (
        patch('app.services.referral_service.get_user_by_id', AsyncMock(side_effect=[new_user, referrer])),
        patch('app.services.referral_service.create_referral_earning', AsyncMock()) as create_earning,
    ):
        result = await process_referral_registration(db, new_user_id=10, referrer_id=20, bot=None)

    assert result is True, 'second call must report success (idempotent), not failure'
    create_earning.assert_not_called(), 'must NOT insert a second pending row when one already exists'


@pytest.mark.asyncio
async def test_process_referral_registration_inserts_first_pending_row() -> None:
    """Negative-control: when no existing pending row, INSERT proceeds normally."""
    from app.services.referral_service import process_referral_registration

    db = AsyncMock()

    new_user = SimpleNamespace(id=10, telegram_id=1, referred_by_id=20, language='ru', first_name='', email=None)
    referrer = SimpleNamespace(id=20, telegram_id=None, language='ru', first_name='Inviter', email=None)
    # No telegram_id on referrer → notification path short-circuits and
    # doesn't try to call bot.send_message; keeps the test simple.

    empty_row = AsyncMock()
    empty_row.scalar_one_or_none = lambda: None
    db.execute = AsyncMock(return_value=empty_row)

    # `referral_contest_service` is imported INSIDE the function body
    # (not at module scope), so we must patch the source module, not
    # `app.services.referral_service.referral_contest_service`.
    with (
        patch('app.services.referral_service.get_user_by_id', AsyncMock(side_effect=[new_user, referrer])),
        patch('app.services.referral_service.get_user_campaign_id', AsyncMock(return_value=None)),
        patch('app.services.referral_service.create_referral_earning', AsyncMock()) as create_earning,
        patch(
            'app.services.referral_contest_service.referral_contest_service.on_referral_registration',
            AsyncMock(),
        ),
    ):
        await process_referral_registration(db, new_user_id=10, referrer_id=20, bot=None)

    # The function returns implicitly after the insert branch; the
    # load-bearing assertion is the INSERT call itself.
    create_earning.assert_awaited_once()


# ---------------------------------------------------------------------------
# Cabinet uses bot=None — verify the helper lazy-creates one via
# `create_bot()` so referrer Telegram notifications still fire.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_helper_lazy_creates_bot_when_caller_omits_it(db: AsyncMock) -> None:
    """Cabinet endpoints don't have a bot in scope; the helper must
    spin one up via `create_bot()` so the referrer still gets the
    Telegram notification. Pre-fix the cabinet path silently passed
    bot=None all the way through, suppressing all notifications."""
    user = _user()
    referrer = _referrer(user_id=200)

    fake_bot = AsyncMock(name='fake_bot')

    class _CtxMgr:
        async def __aenter__(self) -> AsyncMock:
            return fake_bot

        async def __aexit__(self, *_a: object) -> None:
            return None

    with (
        patch('app.database.crud.user.get_user_by_referral_code', AsyncMock(return_value=referrer)),
        patch('app.services.referral_service.clear_pending_referral', AsyncMock()),
        patch('app.bot_factory.create_bot', return_value=_CtxMgr()),
        patch('app.services.referral_service.process_referral_registration', AsyncMock()) as fire,
    ):
        result = await attach_referrer_if_missing(db, user, referral_code='X', source='cabinet_unit_test')

    assert result == 200
    fire.assert_awaited_once()
    _args, kwargs = fire.call_args
    assert kwargs.get('bot') is fake_bot, (
        'cabinet path must lazy-create a bot via create_bot() so the referrer Telegram notification fires'
    )


# ---------------------------------------------------------------------------
# Security pin: cabinet retroactive call sites MUST NOT pass the
# client-controlled `request.referral_code`. Doing so would let an
# attacker POST any referrer code via the cabinet auth endpoint and
# self-attach it to their orphan (no-referrer) account — monetizing
# the multi-account self-referral attack post-registration.
# ---------------------------------------------------------------------------


def test_cabinet_retroactive_calls_pass_none_for_referral_code() -> None:
    """Source-level pin: the three retroactive attach call sites in
    `app/cabinet/routes/auth.py` must NOT forward `request.referral_code`.

    The Redis pending_referral key is the only trusted retroactive
    source — it's provably written by the bot's /start handler only
    for the telegram_id who actually clicked the link. Honouring the
    request body's referral_code on the retroactive path lets any
    attacker rewrite their own account's referrer at will.
    """
    from pathlib import Path

    auth_path = Path(__file__).resolve().parents[2] / 'app' / 'cabinet' / 'routes' / 'auth.py'
    source = auth_path.read_text(encoding='utf-8')

    forbidden = ['cabinet_telegram_retroactive', 'cabinet_widget_retroactive', 'cabinet_oidc_retroactive']
    for src_tag in forbidden:
        idx = source.find(src_tag)
        assert idx >= 0, f'expected to find a retroactive call site tagged {src_tag!r}'
        # Inspect a window around the call to confirm it passes
        # `referral_code=None`, not `referral_code=request.referral_code`.
        window_start = source.rfind('attach_referrer_if_missing', 0, idx)
        window = source[window_start : idx + len(src_tag) + 50]
        assert 'referral_code=None' in window, (
            f'cabinet retroactive call site {src_tag!r} must pass referral_code=None — '
            f'forwarding request.referral_code is a security bug (client-controlled referrer override)'
        )
        assert 'referral_code=request.referral_code' not in window, (
            f'cabinet retroactive call site {src_tag!r} MUST NOT forward request.referral_code'
        )


# ---------------------------------------------------------------------------
# Concurrent-attach race: the helper uses a conditional UPDATE
# (WHERE referred_by_id IS NULL) so cross-session races can't flip an
# already-attached referrer.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_attach_uses_conditional_update_not_unconditional_write(db: AsyncMock) -> None:
    """REGRESSION: the helper must use ``UPDATE ... WHERE referred_by_id IS NULL``
    so a concurrent session can't displace an already-attached referrer.

    Pre-fix: ``user.referred_by_id = X; await commit()`` was a
    last-write-wins flip. Attacker with two concurrent sessions
    (different codes) could clobber legitimate attribution.
    """
    user = _user()
    referrer = _referrer(user_id=200)

    captured_stmts: list[object] = []

    async def _capture_execute(stmt, *args, **kwargs):
        captured_stmts.append(stmt)
        result = AsyncMock()
        result.rowcount = 1  # pretend we won the race
        return result

    db.execute = _capture_execute

    with (
        patch('app.database.crud.user.get_user_by_referral_code', AsyncMock(return_value=referrer)),
        patch('app.services.referral_service.clear_pending_referral', AsyncMock()),
        patch('app.services.referral_service.process_referral_registration', AsyncMock()),
    ):
        await attach_referrer_if_missing(db, user, referral_code='X', source='unit_test')

    # The captured statement should be a conditional UPDATE — the
    # compiled SQL must reference the IS NULL guard.
    assert captured_stmts, 'helper must execute at least one statement to perform the conditional UPDATE'
    update_stmt = captured_stmts[0]
    compiled = str(update_stmt.compile(compile_kwargs={'literal_binds': True}))
    assert 'UPDATE' in compiled.upper()
    assert 'referred_by_id IS NULL' in compiled, (
        'attach UPDATE must include `WHERE referred_by_id IS NULL` so a concurrent winner '
        'cannot be displaced. Compiled SQL was: ' + compiled
    )


@pytest.mark.asyncio
async def test_concurrent_attach_loser_does_not_fire_event(db: AsyncMock) -> None:
    """When ``rowcount == 0`` (another session already attached), the
    helper must return None and NOT fire the registration event.

    Without this guard, the loser would emit a phantom registration
    notification to the wrong audience and create a duplicate audit
    row (caught only by the partial UNIQUE index at the DB layer).
    """
    user = _user()
    referrer = _referrer(user_id=200)

    async def _execute_zero_rowcount(*_a, **_kw):
        result = AsyncMock()
        result.rowcount = 0  # someone else won
        return result

    db.execute = _execute_zero_rowcount

    with (
        patch('app.database.crud.user.get_user_by_referral_code', AsyncMock(return_value=referrer)),
        patch('app.services.referral_service.clear_pending_referral', AsyncMock()) as clear,
        patch('app.services.referral_service.process_referral_registration', AsyncMock()) as fire,
    ):
        result = await attach_referrer_if_missing(db, user, referral_code='X', source='unit_test')

    assert result is None, 'lost race must report None, not the attempted referrer_id'
    fire.assert_not_called(), 'event must NOT fire when we lost the race (winner fired it already)'
    clear.assert_not_called(), 'do not clear pending — winner may still need it for their own clear'


@pytest.mark.asyncio
async def test_helper_uses_caller_supplied_bot_when_provided(db: AsyncMock) -> None:
    """When the bot caller already has a bot (start.py passes message.bot),
    the helper must use it directly — no need to spin up a second bot."""
    user = _user()
    referrer = _referrer(user_id=200)
    caller_bot = AsyncMock(name='caller_bot')

    with (
        patch('app.database.crud.user.get_user_by_referral_code', AsyncMock(return_value=referrer)),
        patch('app.services.referral_service.clear_pending_referral', AsyncMock()),
        patch('app.bot_factory.create_bot') as factory,
        patch('app.services.referral_service.process_referral_registration', AsyncMock()) as fire,
    ):
        result = await attach_referrer_if_missing(db, user, referral_code='X', bot=caller_bot, source='bot_unit_test')

    assert result == 200
    factory.assert_not_called(), 'must reuse the caller-supplied bot, never lazy-create when given'
    fire.assert_awaited_once()
    _args, kwargs = fire.call_args
    assert kwargs.get('bot') is caller_bot
