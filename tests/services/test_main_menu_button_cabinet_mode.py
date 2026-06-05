"""Regression tests for the "Главное меню" button in cabinet-mode notifications.

Production incident (2026-05-18): in ``MAIN_MENU_MODE=cabinet``, the
"💸 Пополнение успешно" notification's last button (labelled
"🏠 Главное меню") opened the cabinet WebApp instead of returning the
user to the bot's main menu. Root cause:
``build_miniapp_or_callback_button(callback_data='back_to_menu')`` saw
``back_to_menu`` mapped to ``/`` in ``CALLBACK_TO_CABINET_PATH`` and
silently swapped the callback button for a WebApp launcher.

UX impact: user in cabinet mode taps "Главное меню" → cabinet root
loads again → user is stuck in the cabinet with no obvious escape to
the bot.

Two-layer defence:

  1. ``back_to_menu`` removed from ``CALLBACK_TO_CABINET_PATH``.
     Even if a caller wrongly passes it through
     ``build_miniapp_or_callback_button``, the helper falls through
     to a normal callback button.
  2. New dedicated helper ``build_main_menu_button(text)`` in
     ``app/utils/miniapp_buttons.py`` always returns a callback button,
     making the intent explicit at every call site.

These tests pin both layers.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.utils.miniapp_buttons import (
    BUTTON_KEY_TO_CABINET_PATH,
    CALLBACK_TO_CABINET_PATH,
    CALLBACK_TO_CABINET_STYLE,
    build_main_menu_button,
    build_miniapp_or_callback_button,
)


# ---------------------------------------------------------------------------
# Layer 1: mapping defence.
# ---------------------------------------------------------------------------


def test_back_to_menu_is_not_in_cabinet_path_mapping() -> None:
    """REGRESSION: ``back_to_menu`` must NOT be a key in
    ``CALLBACK_TO_CABINET_PATH``. Its presence was the root cause of
    the bug: ``build_miniapp_or_callback_button`` consulted the
    mapping and silently swapped the callback for a WebApp launcher
    pointing at the cabinet root.

    Other callbacks like ``menu_balance`` legitimately ARE in the
    mapping because they semantically open a cabinet section. But
    ``back_to_menu`` semantically means "return to bot menu" and must
    never be cabinet-routed.
    """
    assert 'back_to_menu' not in CALLBACK_TO_CABINET_PATH, (
        'back_to_menu must NOT be in CALLBACK_TO_CABINET_PATH — the callback '
        "semantically means 'return to bot main menu', not 'open cabinet root'. "
        'Adding it back here will re-introduce the cabinet-mode UX trap where '
        'the user is stuck in an infinite "хочу в бот → попадаю в кабинет" loop.'
    )


def test_back_to_menu_is_not_in_cabinet_style_mapping() -> None:
    """Dead config caught: if ``back_to_menu`` were styled per-section
    here, the styling would be applied only when the WebApp path was
    used — which we've now disabled. Removing it from style mapping
    keeps the two configs in sync."""
    assert 'back_to_menu' not in CALLBACK_TO_CABINET_STYLE


def test_build_miniapp_or_callback_button_falls_through_for_back_to_menu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belt-and-suspenders: even in cabinet mode, calling
    ``build_miniapp_or_callback_button(callback_data='back_to_menu')``
    must produce a callback button — never a WebApp launcher.

    A future contributor who doesn't know about ``build_main_menu_button``
    might use the generic helper. The mapping omission guarantees they
    can't accidentally re-introduce the bug.
    """
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'cabinet', raising=False)
    # Set a cabinet URL — without it the helper falls through anyway,
    # so we'd be testing the wrong defence layer. The point of this
    # test is that EVEN WITH cabinet mode fully configured, back_to_menu
    # produces a callback button.
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'https://cabinet.example.com', raising=False)

    button = build_miniapp_or_callback_button(
        text='🏠 Главное меню',
        callback_data='back_to_menu',
    )

    assert isinstance(button, InlineKeyboardButton)
    assert button.callback_data == 'back_to_menu', (
        'In cabinet mode with cabinet URL configured, back_to_menu must STILL '
        'produce a callback button. WebApp launcher would re-introduce the '
        'incident where the user gets stuck in cabinet root.'
    )
    assert button.web_app is None, (
        'back_to_menu button must NOT have a WebAppInfo attached — that would '
        'open the cabinet root instead of firing the bot callback'
    )


# ---------------------------------------------------------------------------
# Layer 2: dedicated helper.
# ---------------------------------------------------------------------------


def test_build_main_menu_button_returns_callback_button() -> None:
    """The dedicated helper always returns a callback button. No mode
    detection, no URL check — pure intent expression."""
    button = build_main_menu_button('🏠 Главное меню')

    assert isinstance(button, InlineKeyboardButton)
    assert button.text == '🏠 Главное меню'
    assert button.callback_data == 'back_to_menu'
    assert button.web_app is None
    assert button.url is None


def test_build_main_menu_button_body_does_not_reference_cabinet_mode() -> None:
    """Pin the design contract: ``build_main_menu_button`` ignores
    ``MAIN_MENU_MODE`` / ``MINIAPP_CUSTOM_URL`` / ``is_cabinet_mode``
    entirely.

    Source-level check rather than mock-based because pydantic
    ``Settings`` forbids attribute mutation on instances — we can't
    patch ``settings.is_cabinet_mode`` to a Mock to assert
    ``not_called``. Inspecting the function source is the equivalent
    static contract: if the body ever references the cabinet-mode
    detector, this test fails. Strictly stronger than the previous
    monkeypatch-based version (which was vacuous: the helper never
    read the settings the test was patching).
    """
    import ast
    import inspect
    import textwrap

    src = textwrap.dedent(inspect.getsource(build_main_menu_button))
    # Parse the function and walk its body sans-docstring — the design
    # rationale in the docstring legitimately MENTIONS the forbidden
    # tokens as 'what NOT to do', and we don't want to false-positive
    # on documentation.
    tree = ast.parse(src)
    func = tree.body[0]
    assert isinstance(func, ast.FunctionDef)
    body_nodes = func.body
    if (
        body_nodes
        and isinstance(body_nodes[0], ast.Expr)
        and isinstance(body_nodes[0].value, ast.Constant)
        and isinstance(body_nodes[0].value.value, str)
    ):
        # First node is the docstring — skip it.
        body_nodes = body_nodes[1:]

    body_src = '\n'.join(ast.unparse(node) for node in body_nodes)
    forbidden_tokens = [
        'is_cabinet_mode',
        'MAIN_MENU_MODE',
        'MINIAPP_CUSTOM_URL',
        'CALLBACK_TO_CABINET_PATH',
        'build_cabinet_url',
        'WebAppInfo',
        'web_app',
    ]
    for token in forbidden_tokens:
        assert token not in body_src, (
            f'build_main_menu_button body references {token!r}. The helper must '
            'be pure: always return a callback button regardless of cabinet mode. '
            'Adding mode-detection here re-introduces the bug class this helper '
            'was created to prevent.'
        )

    # Behavioural sanity check: the helper still works.
    button = build_main_menu_button('🏠 Main menu')
    assert isinstance(button, InlineKeyboardButton)
    assert button.callback_data == 'back_to_menu'
    assert button.web_app is None


# ---------------------------------------------------------------------------
# Producer: top-up success keyboard uses the dedicated helper.
# ---------------------------------------------------------------------------


def test_topup_success_keyboard_main_menu_button_is_callback() -> None:
    """Source-level pin: ``app/services/payment/common.py`` must use
    ``build_main_menu_button(texts.MAIN_MENU_BUTTON)`` for the Main
    Menu row, NOT ``build_miniapp_or_callback_button``.

    Whitespace-robust positive assertion only — the previous version
    had a literal-string negative match keyed to a specific 20-space
    indent, which would silently pass after any reformat that changed
    the indentation. We rely on the AST-based scan in
    ``test_no_other_callsite_wraps_back_to_menu_in_miniapp_helper``
    to catch the buggy pattern (it's resilient to formatting).
    """
    from pathlib import Path

    common_path = Path(__file__).resolve().parents[2] / 'app' / 'services' / 'payment' / 'common.py'
    source = common_path.read_text(encoding='utf-8')

    # The corrected form must be present.
    assert 'build_main_menu_button(texts.MAIN_MENU_BUTTON)' in source, (
        'build_topup_success_keyboard must call build_main_menu_button() for '
        'the Главное меню row to guarantee bot-callback semantics regardless '
        'of MAIN_MENU_MODE. AST scan below catches the buggy pattern.'
    )


# ---------------------------------------------------------------------------
# Convention pin: no other call site secretly wraps back_to_menu through
# build_miniapp_or_callback_button.
# ---------------------------------------------------------------------------


def test_no_other_callsite_wraps_back_to_menu_in_miniapp_helper() -> None:
    """AST-based scan: no callsite anywhere in ``app/`` may invoke
    ``build_miniapp_or_callback_button(..., callback_data='back_to_menu')``.

    The previous regex-based scan had a nested-paren blind spot — a
    contributor writing ``build_miniapp_or_callback_button(text=f'x {fn()} y',
    callback_data='back_to_menu')`` would slip past because the
    ``[^)]*?`` lookahead stopped at the first ``)``. AST walk handles
    nested calls naturally.
    """
    import ast
    from pathlib import Path

    app_root = Path(__file__).resolve().parents[2] / 'app'
    offenders: list[tuple[str, int]] = []

    # Skip the helper module itself — its docstring legitimately
    # references the anti-pattern as an example of what NOT to write.
    skip_files = {'miniapp_buttons.py'}

    class _BackToMenuMisuseFinder(ast.NodeVisitor):
        def __init__(self, file_path: Path) -> None:
            self.file_path = file_path

        def visit_Call(self, node: ast.Call) -> None:
            func_name: str | None = None
            if isinstance(node.func, ast.Name):
                func_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                func_name = node.func.attr

            if func_name == 'build_miniapp_or_callback_button':
                for kw in node.keywords:
                    if (
                        kw.arg == 'callback_data'
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value == 'back_to_menu'
                    ):
                        offenders.append((str(self.file_path), node.lineno))
                        break
            # Always recurse so nested calls are inspected.
            self.generic_visit(node)

    for py_file in app_root.rglob('*.py'):
        if py_file.name in skip_files:
            continue
        try:
            tree = ast.parse(py_file.read_text(encoding='utf-8'))
        except SyntaxError:
            # If a file has bad syntax it's a separate failure mode;
            # don't mask it with a vague test error here.
            continue
        _BackToMenuMisuseFinder(py_file).visit(tree)

    assert not offenders, (
        'AST scan found build_miniapp_or_callback_button(callback_data="back_to_menu") '
        f'at {offenders}. This wrapper turns the "Главное меню" button into a '
        'WebApp launcher in cabinet mode, trapping the user in the cabinet. '
        'Use build_main_menu_button(text) instead.'
    )


def test_home_button_key_is_not_in_broadcast_cabinet_path_mapping() -> None:
    """Structural pin (per architect-review top priority): the foot-gun
    ``BUTTON_KEY_TO_CABINET_PATH['home'] = '/'`` was REMOVED.

    Previously, the entry was inert because ``CABINET_MINIAPP_BUTTON_KEYS``
    in ``admin/messages.py`` gated which broadcast keys could reach the
    cabinet-routing branch. But ``home`` not being in that set was an
    implicit invariant — adding it back later would silently re-introduce
    the cabinet-mode "Главное меню" trap for admin broadcast buttons.

    Removing the mapping entry is the structural defence: even if a
    future hand adds ``'home'`` to ``CABINET_MINIAPP_BUTTON_KEYS``,
    ``BUTTON_KEY_TO_CABINET_PATH.get('home', '')`` returns empty string
    and ``build_miniapp_or_callback_button`` falls through to a callback
    button.
    """
    assert 'home' not in BUTTON_KEY_TO_CABINET_PATH, (
        "'home' must NOT be in BUTTON_KEY_TO_CABINET_PATH — admin broadcasts "
        'use ``home`` as the bot-menu vocabulary key, and routing it to cabinet '
        "root would trap users in cabinet mode. If you need a 'open cabinet "
        "home page' button, add a distinct key like 'cabinet_home' so the "
        'intent is explicit at the broadcast-config layer.'
    )


def test_home_button_key_is_not_in_cabinet_miniapp_button_keys() -> None:
    """Belt-and-suspenders set-membership pin.

    Even though the mapping entry was removed
    (test_home_button_key_is_not_in_broadcast_cabinet_path_mapping),
    we ALSO pin that the admin gating set does not include 'home'.
    Two layers: if a future contributor adds 'home' to either side,
    one of these tests fails loudly.
    """
    from app.handlers.admin import messages as admin_messages

    cabinet_keys = getattr(admin_messages, 'CABINET_MINIAPP_BUTTON_KEYS', None)
    assert cabinet_keys is not None, 'CABINET_MINIAPP_BUTTON_KEYS expected in admin.messages'
    assert 'home' not in cabinet_keys, (
        "'home' key MUST NOT be added to CABINET_MINIAPP_BUTTON_KEYS — see "
        'test_home_button_key_is_not_in_broadcast_cabinet_path_mapping for the '
        'structural-defence rationale.'
    )


# ---------------------------------------------------------------------------
# Behavioural integration test: invoke build_topup_success_keyboard and
# inspect the resulting InlineKeyboardMarkup. Source-level pins above are
# defence-in-depth, but this one exercises the actual production code path
# the user clicks — closing the "test passes but bug exists" gap flagged
# by the test-automator review.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_topup_success_keyboard_renders_callback_main_menu_button_in_cabinet_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REGRESSION (behavioural): ``build_topup_success_keyboard``,
    invoked in cabinet mode with cabinet URL configured, must produce
    an ``InlineKeyboardMarkup`` whose LAST row contains a callback
    button with ``callback_data='back_to_menu'`` and ``web_app is None``.

    This is the keyboard the user actually receives in the
    "💸 Пополнение успешно" notification. Source-level pins protect
    against the literal anti-pattern; this test protects against any
    refactor that breaks the user-visible outcome regardless of how
    the keyboard is assembled internally.
    """
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'cabinet', raising=False)
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'https://cabinet.example.com', raising=False)

    from types import SimpleNamespace

    from app.services.payment.common import PaymentCommonMixin

    # Minimal user stub. No active subscription = simplest branch
    # (no subscription-extend row), no saved cart, no checkout draft.
    user = SimpleNamespace(
        id=42,
        language='ru',
        subscription=None,
    )

    # Mock cart-helpers so the test does not hit Redis / DB. These are
    # decorations on the keyboard, not the Main Menu row we care about.
    mixin_instance = type('_TestMixin', (PaymentCommonMixin,), {})()

    with (
        patch(
            'app.services.payment.common.user_cart_service.has_user_cart',
            AsyncMock(return_value=False),
        ),
        patch(
            'app.services.payment.common.has_subscription_checkout_draft',
            AsyncMock(return_value=False),
        ),
    ):
        keyboard = await mixin_instance.build_topup_success_keyboard(user)

    assert isinstance(keyboard, InlineKeyboardMarkup)
    assert keyboard.inline_keyboard, 'Keyboard must contain at least one row'

    # The Main Menu button is the LAST row of the keyboard
    # (after first_button, optional cart-restore row, and balance row).
    last_row = keyboard.inline_keyboard[-1]
    assert len(last_row) == 1, f'Main Menu row should contain exactly one button, got {last_row}'

    main_menu_button = last_row[0]
    assert main_menu_button.callback_data == 'back_to_menu', (
        f'LAST button must have callback_data="back_to_menu", got '
        f'callback_data={main_menu_button.callback_data!r}, web_app={main_menu_button.web_app!r}. '
        'If web_app is set, the user is stuck in cabinet root — the exact bug '
        'reported on 2026-05-18.'
    )
    assert main_menu_button.web_app is None, (
        f'LAST button MUST NOT have a WebAppInfo attached, got web_app={main_menu_button.web_app!r}. '
        'In MAIN_MENU_MODE=cabinet with MINIAPP_CUSTOM_URL configured, the previous '
        'buggy code produced a WebApp launcher here — the user-visible regression.'
    )
