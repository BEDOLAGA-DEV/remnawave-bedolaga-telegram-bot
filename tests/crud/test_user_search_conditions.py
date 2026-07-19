"""Unit tests for the admin user-search condition builder.

Regression guard: a numeric search term that overflows the telegram_id BigInteger
column used to be compared directly, crashing the query (PostgreSQL: value out of
range for type bigint) and spamming the logs. The builder must fall back to
text-only matching for out-of-range numbers.
"""

from __future__ import annotations

from app.database.crud.user import _BIGINT_MAX, _user_search_conditions


def _sql(condition) -> str:
    return str(condition.compile(compile_kwargs={'literal_binds': True}))


def test_in_range_number_matches_telegram_id() -> None:
    conditions = _user_search_conditions('12345')
    # 3 text columns + telegram_id
    assert len(conditions) == 4
    assert 'telegram_id' in _sql(conditions[-1])
    assert '12345' in _sql(conditions[-1])


def test_bigint_max_boundary_still_matches_telegram_id() -> None:
    conditions = _user_search_conditions(str(_BIGINT_MAX))
    assert len(conditions) == 4
    assert 'telegram_id' in _sql(conditions[-1])


def test_number_over_bigint_max_falls_back_to_text_only() -> None:
    # One past the BIGINT ceiling — would overflow the column and crash the query.
    conditions = _user_search_conditions(str(_BIGINT_MAX + 1))
    assert len(conditions) == 3
    assert all('telegram_id' not in _sql(c) for c in conditions)


def test_very_long_number_falls_back_to_text_only() -> None:
    conditions = _user_search_conditions('9' * 30)
    assert len(conditions) == 3
    assert all('telegram_id' not in _sql(c) for c in conditions)


def test_text_search_never_touches_telegram_id() -> None:
    conditions = _user_search_conditions('john_doe')
    assert len(conditions) == 3
    assert all('telegram_id' not in _sql(c) for c in conditions)
