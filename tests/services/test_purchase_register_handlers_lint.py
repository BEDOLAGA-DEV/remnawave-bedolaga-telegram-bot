"""AST-level lint for `app/handlers/subscription/purchase.py::register_handlers`.

Regression cover for 2026-05-16 production incident: a function-scoped
`from app.states import SubscriptionStates` was added inside
`register_handlers` (line 4301) alongside a legitimate lazy import for the
device-rename feature. Python's scoping rules promoted the name to a
function-local, so the earlier reference on line 4197
(`SubscriptionStates.selecting_period`) raised UnboundLocalError at bot
startup and crashed the container before handlers were registered.

The same class of bug would not show up in unit tests (none of them call
`register_handlers`), and `ruff` does not detect it. So we lint the AST:
inside `register_handlers`, NO function-local binding may exist for any
name that's also imported at module level. That catches both
`from app.states import …` and `SubscriptionStates = …` patterns.
"""

from __future__ import annotations

import ast
from pathlib import Path


PURCHASE_PATH = Path(__file__).resolve().parents[2] / 'app' / 'handlers' / 'subscription' / 'purchase.py'


def _module_level_imports(tree: ast.Module) -> set[str]:
    """Names imported at module-level (not inside any function)."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or (alias.name.split('.')[0]))
    return names


def _function_local_bindings(func: ast.FunctionDef) -> set[str]:
    """Every name bound inside `func`'s scope (imports + assignments)."""
    bound: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound.add(alias.asname or (alias.name.split('.')[0]))
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    bound.add(t.id)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)) and isinstance(node.target, ast.Name):
            bound.add(node.target.id)
    return bound


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node  # type: ignore[return-value]
    raise AssertionError(f'function {name!r} not found at module top-level')


def test_register_handlers_does_not_shadow_module_imports() -> None:
    """No name imported at module-level may also be bound inside register_handlers.

    Such shadowing makes EVERY reference to that name in the function be
    treated as local — including references that appear BEFORE the local
    binding — which raises UnboundLocalError at first call.
    """
    source = PURCHASE_PATH.read_text(encoding='utf-8')
    tree = ast.parse(source)

    module_names = _module_level_imports(tree)
    register = _find_function(tree, 'register_handlers')
    locals_inside = _function_local_bindings(register)

    # Names that are BOTH imported at module level AND re-bound in the function.
    shadows = module_names & locals_inside

    # Exception: lazy imports for symbols that are NOT also referenced before
    # the local import. We allow them only if they're not in `module_names`.
    # (i.e. truly lazy — not duplicating a module-level import.)
    assert not shadows, (
        f'register_handlers re-binds names already imported at module level: {sorted(shadows)}.\n'
        'This silently turns every reference (including earlier ones) into a local — '
        'the next call raises UnboundLocalError. Move the local import to module scope, '
        'or rename the local binding.'
    )


def test_subscription_states_is_module_level_only() -> None:
    """Explicit narrow guard for the exact 2026-05-16 incident."""
    source = PURCHASE_PATH.read_text(encoding='utf-8')
    tree = ast.parse(source)

    # Must be imported at module level.
    assert 'SubscriptionStates' in _module_level_imports(tree), (
        'SubscriptionStates expected at module level for register_handlers references'
    )

    # Must NOT be re-bound inside register_handlers.
    register = _find_function(tree, 'register_handlers')
    assert 'SubscriptionStates' not in _function_local_bindings(register), (
        'SubscriptionStates re-imported inside register_handlers — UnboundLocalError will hit at startup'
    )
