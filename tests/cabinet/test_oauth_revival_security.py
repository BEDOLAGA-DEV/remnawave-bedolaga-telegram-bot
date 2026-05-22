"""Security tests for OAuth callback DELETED-user revival.

HIGH-severity regression from the security audit: the email-merge path
in `cabinet/routes/oauth.py` would happily reactivate a DELETED row by
verified-IdP-email alone. If Alice originally registered through the
password flow, never confirmed her address, and was later soft-deleted,
an attacker controlling alice@example.com at any IdP could "merge" into
her dormant row and inherit balance/subs.

Fix verified here: the merge now requires BOTH
  * `user_info.email_verified == True` (IdP attests it owns the email)
  * `user.email_verified == True` (local row confirms historical
    control)
A local row with `email_verified=False` falls through to fresh-account
creation instead of being taken over.
"""

from __future__ import annotations

import inspect
from pathlib import Path


OAUTH_FILE = Path(__file__).resolve().parents[2] / 'app' / 'cabinet' / 'routes' / 'oauth.py'


def test_email_merge_requires_local_user_email_verified() -> None:
    """Source-level guard: the email-merge branch checks user.email_verified.

    A literal AST/source check is enough — the branch is small and a
    full integration test would need to mock the entire OAuth stack
    (state cache, provider HTTP, JWT signing, finalize). The
    source-level pin is sufficient to catch a regression where someone
    deletes the `user.email_verified` guard and reopens the bug.
    """
    source = OAUTH_FILE.read_text(encoding='utf-8')

    # Find the email-merge branch block.
    assert 'get_user_by_email(db, user_info.email)' in source, (
        'email-merge branch helper call missing — test obsolete or oauth.py refactored'
    )

    # The line establishing the local user must also enforce
    # `user.email_verified` before any provider-link or revival action.
    # We assert the precise condition shape — adding the local guard
    # was the security audit fix.
    assert 'if user and user.email_verified:' in source, (
        'oauth.py email-merge branch must guard on local user.email_verified '
        '— without it, an IdP-verified attacker email can take over an '
        'unverified-email DELETED row (HIGH severity audit finding).'
    )


def test_revived_log_field_uses_pre_revival_snapshot() -> None:
    """`revived=<bool>` in the logger.info call must come from a snapshot
    taken BEFORE the mutation, not from `user.status == ACTIVE` post-revival.

    Pre-fix the audit log always read `revived=True` for any user
    reaching the email-merge branch (since revive flips status in place).
    Post-fix we capture `was_deleted = user.status == UserStatus.DELETED.value`
    BEFORE the revive call and pass `revived=was_deleted` to the logger.
    """
    source = OAUTH_FILE.read_text(encoding='utf-8')

    assert 'was_deleted = user.status == UserStatus.DELETED.value' in source, (
        'oauth.py must capture was_deleted BEFORE revive_deleted_user mutates status'
    )
    # No more `revived=user.status == UserStatus.ACTIVE.value` (the broken pattern).
    assert 'revived=user.status == UserStatus.ACTIVE.value' not in source, (
        'oauth.py must NOT compute `revived` from post-mutation status — use the captured was_deleted snapshot instead'
    )


def test_revive_called_without_commit_kwarg() -> None:
    """Architect's call: revive_deleted_user no longer accepts `commit=`.

    The kwarg leaked transaction control into business logic. Callers
    now always own the commit. This pin keeps oauth.py from regressing
    back to the two-mode pattern.
    """
    source = OAUTH_FILE.read_text(encoding='utf-8')
    revive_calls = [line for line in source.splitlines() if 'revive_deleted_user(' in line]
    assert revive_calls, 'oauth.py is expected to call revive_deleted_user — test obsolete'
    for call_line in revive_calls:
        assert 'commit=' not in call_line, (
            f'revive_deleted_user must NOT be called with a commit= kwarg ({call_line.strip()!r}); '
            'the parameter was removed in favour of caller-owns-commit'
        )


def test_revive_service_does_not_commit() -> None:
    """Hard pin: revive_deleted_user implementation does not commit.

    Stronger than the unit-test version because it inspects the actual
    function body (with docstring stripped) for `await db.commit()` /
    `await db.refresh()` call patterns.
    """
    from app.services import user_revival_service

    src = inspect.getsource(user_revival_service.revive_deleted_user)
    # Strip the docstring so the architectural rationale that legitimately
    # mentions "db.commit" doesn't trip the call-pattern grep.
    if '"""' in src:
        first = src.find('"""')
        second = src.find('"""', first + 3)
        if second > first:
            src = src[:first] + src[second + 3 :]

    assert 'await db.commit' not in src, 'revive_deleted_user must not commit — caller owns the transaction'
    assert 'await db.refresh' not in src, 'revive_deleted_user must not refresh — caller owns the session'
