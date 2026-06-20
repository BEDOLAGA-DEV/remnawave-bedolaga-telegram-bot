"""Boundary tests for RemnaWave username construction.

Regression cover for: `Validation failed: Username must be less than 36 characters`
on cabinet purchase-tariff. Bug repro:
  email='didykmarin@yandex.ru', user_id=703, short_id='49883b',
  REMNAWAVE_USER_USERNAME_TEMPLATE='{email}_{telegram_id}'
  → 'didykmarin_email_didykmarin_703_49883b' (38 chars > 36).
"""

from __future__ import annotations

import pytest

from app.config import settings


# Note: эти тесты дёргают `format_remnawave_username` напрямую, поэтому
# template управляется через monkeypatch (а не env), чтобы не мешать другим
# тестам в той же сессии.


@pytest.fixture(autouse=True)
def _restore_template(monkeypatch: pytest.MonkeyPatch):
    """Ensure each test starts from the default template."""
    original = settings.REMNAWAVE_USER_USERNAME_TEMPLATE
    yield
    monkeypatch.setattr(settings, 'REMNAWAVE_USER_USERNAME_TEMPLATE', original, raising=False)


def test_format_remnawave_username_within_max_without_suffix() -> None:
    """Default behaviour stays bounded by REMNAWAVE_USERNAME_MAX_LENGTH."""
    name = settings.format_remnawave_username(
        full_name='Some Long Name That Could Inflate The Username',
        username='averylongnickname',
        telegram_id=12345678901,
        email='averylongemailprefix@example.com',
        user_id=999999,
    )

    assert len(name) <= settings.REMNAWAVE_USERNAME_MAX_LENGTH
    assert len(name) >= settings.REMNAWAVE_USERNAME_MIN_LENGTH


def test_format_remnawave_username_reserves_room_for_caller_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reserve_suffix_chars=N → base fits in MAX-N so caller can append safely."""
    monkeypatch.setattr(settings, 'REMNAWAVE_USER_USERNAME_TEMPLATE', '{email}_{telegram_id}', raising=False)

    suffix = '_49883b'  # 7 chars
    base = settings.format_remnawave_username(
        full_name='Марина Дидык',
        username=None,
        telegram_id=None,
        email='didykmarin@yandex.ru',
        user_id=703,
        reserve_suffix_chars=len(suffix),
    )
    final = f'{base}{suffix}'

    # The ORIGINAL bug — final length = 38. With the fix it must be ≤ 36.
    assert len(final) <= settings.REMNAWAVE_USERNAME_MAX_LENGTH
    assert final.endswith(suffix)
    # Sanity: still a valid RemnaWave identifier (alnum + underscores + dashes).
    assert all(ch.isalnum() or ch in {'_', '-'} for ch in final)


def test_format_remnawave_username_email_user_default_template() -> None:
    """Email-only user with the bundled default template still fits."""
    name = settings.format_remnawave_username(
        full_name='Марина Дидык',
        username=None,
        telegram_id=None,
        email='didykmarin@yandex.ru',
        user_id=703,
        reserve_suffix_chars=7,  # what subscription_service actually reserves
    )

    assert len(name) <= settings.REMNAWAVE_USERNAME_MAX_LENGTH - 7


def test_format_remnawave_username_does_not_go_below_min_with_huge_reserve() -> None:
    """If caller asks for more reserve than the cap allows, base falls back to MIN."""
    name = settings.format_remnawave_username(
        full_name='X',
        username='x',
        telegram_id=1,
        email=None,
        user_id=None,
        reserve_suffix_chars=settings.REMNAWAVE_USERNAME_MAX_LENGTH + 100,
    )

    assert len(name) >= settings.REMNAWAVE_USERNAME_MIN_LENGTH


def test_format_remnawave_username_repro_38_char_bug(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exact production payload from log.rw/ARVm79dH must come out ≤ 36 chars."""
    # Production .env override exposes the duplication path:
    monkeypatch.setattr(settings, 'REMNAWAVE_USER_USERNAME_TEMPLATE', '{email}_{telegram_id}', raising=False)

    suffix = '_49883b'
    base = settings.format_remnawave_username(
        full_name='Марина Дидык',
        username=None,
        telegram_id=None,
        email='didykmarin@yandex.ru',
        user_id=703,
        reserve_suffix_chars=len(suffix),
    )
    final = base + suffix

    # Before the fix: len(final) == 38 → RemnaWave 400.
    assert len(final) <= 36, f'username still too long: {final!r} ({len(final)} chars)'


# ---------------------------------------------------------------------------
# build_remnawave_subscription_username — high-level helper used by all 3
# multi-tariff create-paths (subscription_service, cabinet admin sync, bulk).
# ---------------------------------------------------------------------------


def test_build_subscription_username_production_repro(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production repro through the high-level helper used by all 3 callers."""
    monkeypatch.setattr(settings, 'REMNAWAVE_USER_USERNAME_TEMPLATE', '{email}_{telegram_id}', raising=False)

    final = settings.build_remnawave_subscription_username(
        full_name='Марина Дидык',
        username=None,
        telegram_id=None,
        email='didykmarin@yandex.ru',
        user_id=703,
        suffix='_49883b',
    )

    assert len(final) <= settings.REMNAWAVE_USERNAME_MAX_LENGTH
    assert final.endswith('_49883b')


def test_build_subscription_username_empty_suffix_is_legacy_format() -> None:
    """suffix='' → equivalent to plain format_remnawave_username (single-tariff path)."""
    plain = settings.format_remnawave_username(
        full_name='X',
        username='x',
        telegram_id=12345,
        email=None,
        user_id=1,
    )
    helper = settings.build_remnawave_subscription_username(
        full_name='X',
        username='x',
        telegram_id=12345,
        email=None,
        user_id=1,
        suffix='',
    )

    assert helper == plain
    assert len(helper) <= settings.REMNAWAVE_USERNAME_MAX_LENGTH


def test_build_subscription_username_handles_pathological_long_suffix() -> None:
    """Suffix longer than MAX_LENGTH: helper must still produce a string ≤ MAX_LENGTH.

    Regression cover for an edge case in the defensive-truncation branch where
    `keep_for_base = MAX - len(suffix)` could go negative; without `max(0, …)`
    the base-slice silently kept the tail.
    """
    huge_suffix = '_' + 'x' * 80  # 81 chars, way over MAX

    final = settings.build_remnawave_subscription_username(
        full_name='X',
        username='x',
        telegram_id=12345,
        email=None,
        user_id=1,
        suffix=huge_suffix,
    )

    assert len(final) <= settings.REMNAWAVE_USERNAME_MAX_LENGTH


# Regression: email-only cabinet users were getting the literal 'user' as their
# RemnaWave username because `user_{username}` rendered identically for every
# user without a Telegram @username — panel then rejected all but the first
# registration with 409 "username already exists". The skeleton detector below
# catches this class of degenerate template renders and falls back to
# user_<identifier>, which always carries the unique user_id / telegram_id.


def test_skeleton_detector_falls_back_when_username_template_renders_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Template `user_{username}` for email-only user (no TG username) renders to
    the constant `user` — must fall back to `user_email_<prefix>_<user_id>`."""
    monkeypatch.setattr(settings, 'REMNAWAVE_USER_USERNAME_TEMPLATE', 'user_{username}', raising=False)

    name = settings.format_remnawave_username(
        full_name='Email User',
        username=None,
        telegram_id=None,
        email='alice@example.com',
        user_id=42,
    )

    # Must NOT equal the degenerate `user` rendered by the skeleton.
    assert name != 'user'
    # Must contain the unique user_id so two email-only users get different usernames.
    assert '42' in name
    # Must still match RemnaWave's character class.
    import re as _re

    assert _re.match(r'^[A-Za-z0-9_-]+$', name)


def test_skeleton_detector_falls_back_when_template_has_no_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A template with no variables (admin misconfig) is itself degenerate —
    it would collide for every single user. Skeleton detector catches it."""
    monkeypatch.setattr(settings, 'REMNAWAVE_USER_USERNAME_TEMPLATE', 'static_user', raising=False)

    name_a = settings.format_remnawave_username(
        full_name='A', username=None, telegram_id=None, email='a@example.com', user_id=1
    )
    name_b = settings.format_remnawave_username(
        full_name='B', username=None, telegram_id=None, email='b@example.com', user_id=2
    )

    # Two different users must get two different usernames despite the broken template.
    assert name_a != name_b
    assert '1' in name_a and '2' in name_b


def test_skeleton_detector_does_not_trigger_for_telegram_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TG users with a real @username are NOT degenerate — the template renders
    their unique username. Skeleton detector must NOT fire here, otherwise the
    fix would regress every TG user to user_<telegram_id> and rename them all."""
    monkeypatch.setattr(settings, 'REMNAWAVE_USER_USERNAME_TEMPLATE', 'user_{username}', raising=False)

    name = settings.format_remnawave_username(
        full_name='TG User',
        username='alice_tg',
        telegram_id=123456,
        email=None,
        user_id=10,
    )

    # Should reflect the actual TG username, not the user_<identifier> fallback.
    assert 'alice_tg' in name


def test_skeleton_detector_uses_user_id_when_template_references_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Template that references {user_id} (which always has a value) must NOT
    trigger the fallback — the rendered username already carries unique data."""
    monkeypatch.setattr(settings, 'REMNAWAVE_USER_USERNAME_TEMPLATE', 'u_{user_id}_{username}', raising=False)

    name = settings.format_remnawave_username(
        full_name='Email User',
        username=None,
        telegram_id=None,
        email='alice@example.com',
        user_id=42,
    )

    # The {user_id} substitution gives uniqueness — fallback path must NOT fire.
    assert '42' in name
    # And the rendered result must NOT be the user_<identifier> fallback shape,
    # which would have wiped the template's own structure.
    assert name.startswith('u_42')
