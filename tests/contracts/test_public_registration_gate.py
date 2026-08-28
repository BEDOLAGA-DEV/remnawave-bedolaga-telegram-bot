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


# The two registration-completion twins (message- and callback-driven) admit a user
# through the same three branches: DELETED restore, phantom claim, existing-user
# reactivation. Each branch must bind the locked gift exactly once before mutating
# the account, or the FOR UPDATE lock taken by the gate is released by the commit
# with the gift still unbound — a concurrent claimer can then take it.
REGISTRATION_TWINS = ('complete_registration', 'complete_registration_from_callback')
# The twins never call bind_locked_gift directly — the guarded wrapper turns a lost
# race for the gift into an ordinary denial instead of an unhandled exception.
BIND_CALL = '_bind_registration_invite'


def _bind_call_counts_by_function(path: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for function in _functions(path):
        counts[function.name] = sum(
            1 for node in ast.walk(function) if isinstance(node, ast.Call) and _call_name(node) == BIND_CALL
        )
    return counts


def test_registration_twins_bind_the_locked_gift_symmetrically() -> None:
    counts = _bind_call_counts_by_function(ROOT / 'app/handlers/start.py')
    missing = [name for name in REGISTRATION_TWINS if name not in counts]
    assert not missing, f'Registration twin disappeared from start.py: {missing}'

    observed = {name: counts[name] for name in REGISTRATION_TWINS}
    assert len(set(observed.values())) == 1, (
        'Registration twins bind the locked gift a different number of times — one of the '
        f'admission branches is missing its invite binding: {observed}'
    )
    assert observed[REGISTRATION_TWINS[0]] == 3, (
        'Expected one invite binding per admission branch (DELETED restore, phantom claim, '
        f'existing-user reactivation): {observed}'
    )


def test_no_admission_branch_binds_the_locked_gift_twice() -> None:
    tree = ast.parse((ROOT / 'app/handlers/start.py').read_text(encoding='utf-8'))
    offenders: list[str] = []

    for node in ast.walk(tree):
        body = getattr(node, 'body', None)
        if not isinstance(body, list):
            continue
        previous: str | None = None
        for statement in body:
            current = ast.dump(statement) if isinstance(statement, ast.Expr) else None
            calls = (
                {_call_name(call) for call in ast.walk(statement) if isinstance(call, ast.Call)}
                if current is not None
                else set()
            )
            if current is not None and current == previous and BIND_CALL in calls:
                offenders.append(ast.unparse(statement))
            previous = current

    assert not offenders, 'Duplicated invite-binding statement:\n' + '\n'.join(offenders)


def test_registration_twins_never_bind_the_gift_unguarded() -> None:
    """A raw bind_locked_gift in a twin would surface a lost race as a 500, not a denial."""
    counts = {}
    for function in _functions(ROOT / 'app/handlers/start.py'):
        if function.name not in REGISTRATION_TWINS:
            continue
        counts[function.name] = sum(
            1 for node in ast.walk(function) if isinstance(node, ast.Call) and _call_name(node) == 'bind_locked_gift'
        )

    assert counts and not any(counts.values()), f'Unguarded bind_locked_gift in a registration twin: {counts}'
