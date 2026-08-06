from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PUBLIC_MODULES = {
    'app/handlers/start.py',
    'app/cabinet/routes/auth.py',
    'app/cabinet/routes/oauth.py',
    'app/cabinet/dependencies.py',
    'app/cabinet/routes/support_ws.py',
    'app/cabinet/routes/landing.py',
    'app/services/guest_purchase_service.py',
    'app/webapi/routes/users.py',
}
MUTATION_CALLS = {
    'create_user',
    'create_user_no_commit',
    'create_user_by_email',
    'create_user_by_oauth',
    'revive_deleted_user',
    '_find_or_create_user',
}
GATE_CALLS = {
    '_evaluate_telegram_registration_access',
    '_prepare_telegram_completion_access',
    '_gate_cabinet_identity',
    '_gate_oauth_identity',
    'evaluate_public_registration',
    'evaluate_guest_purchase_registration',
}
# Exact, narrow wrappers or trusted administrative entrypoints. No module-wide exemptions.
TRUSTED_FUNCTIONS = {
    ('app/handlers/start.py', '_create_user_with_registration_invite'),
    ('app/cabinet/routes/auth.py', '_recover_cabinet_user_after_gate'),
    ('app/services/guest_purchase_service.py', '_find_or_create_user'),
    ('app/webapi/routes/users.py', 'create_user_endpoint'),  # API-token protected administration
}


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _functions(path: Path):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def test_every_public_user_mutation_is_gated_or_narrowly_trusted() -> None:
    offenders: list[str] = []
    for relative in sorted(PUBLIC_MODULES):
        path = ROOT / relative
        for function in _functions(path):
            calls = {_call_name(node) for node in ast.walk(function) if isinstance(node, ast.Call)}
            mutations = sorted(MUTATION_CALLS & calls)
            if not mutations:
                continue
            if (relative, function.name) in TRUSTED_FUNCTIONS:
                continue
            if not (GATE_CALLS & calls):
                offenders.append(f'{relative}:{function.name} -> {mutations}')

    assert not offenders, 'Public User mutation without invite-only gate:\n' + '\n'.join(offenders)


def test_legacy_guest_find_or_create_wrapper_cannot_reappear_in_public_routes() -> None:
    offenders: list[str] = []
    for relative in sorted(PUBLIC_MODULES - {'app/services/guest_purchase_service.py'}):
        path = ROOT / relative
        for function in _functions(path):
            calls = {_call_name(node) for node in ast.walk(function) if isinstance(node, ast.Call)}
            if '_find_or_create_user' in calls and not (GATE_CALLS & calls):
                offenders.append(f'{relative}:{function.name}')

    assert not offenders, 'Legacy find-or-create used without an explicit gate:\n' + '\n'.join(offenders)
