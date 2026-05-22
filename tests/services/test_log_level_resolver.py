"""Regression tests for `app.logging_config._resolve_log_level`.

The bot used to crash at startup with::

    KeyError: <function warning at 0x...>
      File "/app/app/logging_config.py", line 146
      structlog.make_filtering_bound_logger(
          getattr(logging, settings.LOG_LEVEL, logging.INFO),
      )

Reproduction: ``LOG_LEVEL=warning`` (lowercase) in ``.env``.

Why it crashed:

  * ``logging.WARNING`` is the integer constant ``30``.
  * ``logging.warning`` is the bound LOGGER FUNCTION (``def warning(msg)``).
  * ``getattr`` is case-sensitive, so ``getattr(logging, "warning")``
    returns the FUNCTION — not the constant.
  * ``structlog.make_filtering_bound_logger`` indexes ``LEVEL_TO_FILTERING_LOGGER``
    by integer level; a function key explodes with ``KeyError``.

The resolver fixes both halves of the footgun:

  1. ``.upper()`` so ``"warning"``, ``"warning"`` (with whitespace), and
     ``"Warning"`` all resolve to the WARNING constant (30).
  2. ``isinstance(level, int)`` so even if ``getattr`` returns
     a function or some other non-int sentinel for a malformed
     ``LOG_LEVEL`` value, we fall back to the default.
"""

from __future__ import annotations

import logging

import pytest

from app.logging_config import _resolve_log_level


# ---------------------------------------------------------------------------
# Common levels — case-insensitive (the original repro).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'value,expected',
    [
        ('DEBUG', logging.DEBUG),
        ('INFO', logging.INFO),
        ('WARNING', logging.WARNING),
        ('ERROR', logging.ERROR),
        ('CRITICAL', logging.CRITICAL),
    ],
)
def test_resolves_canonical_uppercase_names(value: str, expected: int) -> None:
    assert _resolve_log_level(value) == expected
    assert isinstance(_resolve_log_level(value), int)


@pytest.mark.parametrize(
    'value,expected',
    [
        # The EXACT repro from the 2026-05-16 incident.
        ('warning', logging.WARNING),
        ('debug', logging.DEBUG),
        ('info', logging.INFO),
        ('error', logging.ERROR),
        ('critical', logging.CRITICAL),
    ],
)
def test_resolves_lowercase_names(value: str, expected: int) -> None:
    """REGRESSION: ``LOG_LEVEL=warning`` from .env must NOT return
    ``logging.warning`` (the function). It must return ``logging.WARNING``
    (the int 30).
    """
    result = _resolve_log_level(value)
    assert result == expected
    assert isinstance(result, int), (
        f'Resolver returned {result!r} (type {type(result).__name__}); '
        f'must return an int — passing a function to structlog crashes startup with KeyError'
    )


@pytest.mark.parametrize('value', ['Warning', 'WaRnInG', '  warning  ', '\tWARNING\n'])
def test_resolves_mixed_case_and_whitespace(value: str) -> None:
    """Whitespace and mixed-case variants normalize to the canonical level."""
    assert _resolve_log_level(value) == logging.WARNING


# ---------------------------------------------------------------------------
# Defense against the function-vs-constant ambiguity.
# ---------------------------------------------------------------------------


def test_lowercase_does_not_return_the_logger_function() -> None:
    """The exact failure mode: the ``logging`` module has BOTH
    ``logging.WARNING`` (int 30) and ``logging.warning`` (a function).

    If the resolver fell back to ``getattr(logging, value, default)``
    without uppercase, ``LOG_LEVEL=warning`` would silently return
    the function — which then explodes inside structlog.
    """
    result = _resolve_log_level('warning')
    assert not callable(result), (
        'resolver returned a callable; the original bug was returning '
        'logging.warning (function) instead of logging.WARNING (int 30)'
    )


# ---------------------------------------------------------------------------
# Fallback behavior for invalid input.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('value', ['', '   ', 'fooBar', 'NOTASEVERITY', 'WARN1NG'])
def test_unknown_or_empty_falls_back_to_default(value: str) -> None:
    assert _resolve_log_level(value) == logging.INFO
    assert _resolve_log_level(value, default=logging.ERROR) == logging.ERROR


@pytest.mark.parametrize('value', [None, 42, 30, 3.14, [], {}, object()])
def test_non_string_input_falls_back_to_default(value: object) -> None:
    """The resolver accepts only str input. Anything else → default.

    This guards against a future refactor that accidentally passes
    a number, a Settings field stub, or None.
    """
    assert _resolve_log_level(value) == logging.INFO


def test_default_argument_is_respected() -> None:
    """Custom default values flow through the fallback paths."""
    assert _resolve_log_level('nonsense', default=logging.DEBUG) == logging.DEBUG
    assert _resolve_log_level(None, default=logging.CRITICAL) == logging.CRITICAL
    assert _resolve_log_level('', default=logging.ERROR) == logging.ERROR


# ---------------------------------------------------------------------------
# End-to-end smoke: feed the resolver's output straight to structlog and
# confirm no KeyError. This is the actual failure surface from the bug
# report — if structlog can build a filter, the bot can start.
# ---------------------------------------------------------------------------


def test_resolver_output_is_acceptable_to_structlog() -> None:
    """REGRESSION smoke: ``make_filtering_bound_logger`` must accept
    every value the resolver produces. This is the codepath that
    crashed at startup with ``KeyError: <function warning at 0x...>``.
    """
    import structlog

    for raw in ['debug', 'info', 'warning', 'error', 'critical', 'DEBUG', 'Warning']:
        level = _resolve_log_level(raw)
        # If this line raises KeyError, the original startup crash is back.
        wrapper_class = structlog.make_filtering_bound_logger(level)
        assert wrapper_class is not None
