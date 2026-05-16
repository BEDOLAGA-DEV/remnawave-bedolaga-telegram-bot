"""Security regression test for the /email/login DELETED-user oracle.

Pre-fix, a DELETED user logging in with a correct password got a 403
with `code: account_deleted` payload (bot username, deep link). That's
a strictly worse enumeration oracle than the standard 401: it confirms
to an attacker with a credential dump that
  (a) the email exists,
  (b) the password is correct, AND
  (c) the row is in DELETED state (i.e. recoverable, valuable target).

Post-fix, DELETED falls through to the same generic 401 returned for
wrong-password / no-such-email. The friendly revival screen is only
served behind a Telegram-signature path where identity proof exists.

We pin this by source-grepping the endpoint rather than spinning up
the full HTTP stack: a regression that re-introduces the structured
`account_deleted` reply to /email/login would be visible immediately.
"""

from __future__ import annotations

from pathlib import Path


AUTH_FILE = Path(__file__).resolve().parents[2] / 'app' / 'cabinet' / 'routes' / 'auth.py'


def _extract_login_email_body(source: str) -> str:
    """Pull just the /email/login handler so we don't false-positive
    on `account_deleted` references in /telegram or admin handlers."""
    marker = "@router.post('/email/login'"
    idx = source.find(marker)
    assert idx >= 0, '/email/login endpoint marker missing'
    # End at the next @router decorator or end-of-file
    next_route = source.find('@router.', idx + 1)
    return source[idx : next_route if next_route > 0 else len(source)]


def test_email_login_does_not_return_account_deleted_code() -> None:
    """REGRESSION: /email/login must not expose `code: account_deleted`.

    Returning that code after correct-password is an enumeration oracle
    (security audit finding, MEDIUM). A generic 401 for any non-ACTIVE
    status keeps email-login symmetric with bad-password.
    """
    login_body = _extract_login_email_body(AUTH_FILE.read_text(encoding='utf-8'))

    assert "'account_deleted'" not in login_body, (
        '/email/login must not emit `code: account_deleted` — that disclosure plus correct '
        'password forms an enumeration oracle for recoverable accounts. '
        'Return the generic 401 instead and let the cabinet dependencies guard '
        '(which has a Telegram-signature identity proof) serve the friendly screen.'
    )
    assert 'telegram_deep_link' not in login_body, (
        '/email/login must not echo the bot deep-link — same oracle reasoning'
    )


def test_email_login_returns_generic_401_for_deleted_users() -> None:
    """The DELETED branch in /email/login must raise 401 with 'Invalid email or password'.

    This is the generic message used for wrong-password and missing-email,
    so the three cases are indistinguishable from the client side.
    """
    login_body = _extract_login_email_body(AUTH_FILE.read_text(encoding='utf-8'))

    # Confirm we still branch on UserStatus.ACTIVE explicitly (the
    # check exists), and that the raise is HTTP_401 with the generic
    # message.
    assert 'user.status != UserStatus.ACTIVE.value' in login_body, (
        'login_email must still branch on user.status — without it, DELETED rows would silently authenticate'
    )
    # The exact phrasing must match the generic credential-error string
    # so the response is indistinguishable from wrong-password.
    assert "detail='Invalid email or password'" in login_body, (
        "Generic 401 with detail='Invalid email or password' must be raised for non-ACTIVE users to avoid enum oracles"
    )


def test_email_login_status_check_runs_before_email_verification_gate() -> None:
    """REGRESSION: status check must come before the email-verification gate.

    Pre-fix, a DELETED-but-unverified user hit the "Please verify your
    email first" branch instead of the status branch — confusing UX
    and inconsistent ordering. Post-fix, status is checked first so
    DELETED rows return the generic 401 regardless of verification.
    """
    login_body = _extract_login_email_body(AUTH_FILE.read_text(encoding='utf-8'))

    status_idx = login_body.find('user.status != UserStatus.ACTIVE.value')
    verify_idx = login_body.find('not user.email_verified')

    assert status_idx >= 0 and verify_idx >= 0, 'both checks must exist in login_email'
    assert status_idx < verify_idx, (
        'Status check must precede email-verification gate so a DELETED user with '
        'email_verified=False gets the generic 401, not the "verify your email" 403'
    )
