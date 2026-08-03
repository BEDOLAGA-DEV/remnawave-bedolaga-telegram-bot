import ast
import sys
from enum import StrEnum
from pathlib import Path

import pytest

from app.cabinet.schemas.users import UserStatusEnum
from app.services.admin_panel_sync import (
    BEST_EFFORT_ADMIN_PANEL_MUTATIONS,
    MANDATORY_ADMIN_PANEL_MUTATIONS,
    PanelSyncTarget,
)
from tests.cabinet.admin_panel_sync_case_manifest import (
    FAILED_CASE_KEYS,
    SKIPPED_CASE_KEYS,
    SUCCESS_CASE_KEYS,
)


REQUIRED_MUTATION_CLASSES = {
    'create',
    'extend',
    'set_end_date',
    'activate',
    'cancel',
    'reset',
    'change_tariff',
    'set_traffic',
    'set_devices',
    'delete_subscription',
    'delete_user',
    'disable_user',
}


def test_r2_inventory_is_complete_and_has_no_best_effort_entries():
    assert BEST_EFFORT_ADMIN_PANEL_MUTATIONS == ()
    assert {entry.mutation_class for entry in MANDATORY_ADMIN_PANEL_MUTATIONS} >= REQUIRED_MUTATION_CLASSES
    assert all(entry.classification == 'mandatory-sync' for entry in MANDATORY_ADMIN_PANEL_MUTATIONS)
    assert len({entry.key for entry in MANDATORY_ADMIN_PANEL_MUTATIONS}) == len(MANDATORY_ADMIN_PANEL_MUTATIONS)
    assert all(
        entry.route and entry.action and entry.integration_path and entry.transaction_owner
        for entry in MANDATORY_ADMIN_PANEL_MUTATIONS
    )


def _assert_every_inventory_key_has_outcome_contracts(
    success_keys=SUCCESS_CASE_KEYS,
    skipped_keys=SKIPPED_CASE_KEYS,
    failed_keys=FAILED_CASE_KEYS,
):
    required = {entry.key for entry in MANDATORY_ADMIN_PANEL_MUTATIONS}

    assert required == success_keys
    assert required == skipped_keys
    assert required == failed_keys


def test_every_inventory_key_has_success_skipped_and_failed_contract_coverage():
    """Each key set comes only from rows executed by its outcome parametrization."""
    _assert_every_inventory_key_has_outcome_contracts()


@pytest.mark.parametrize('outcome', ['success', 'skipped', 'failed'])
def test_deleting_any_outcome_row_breaks_inventory_equality(outcome):
    """Mutation proof: one deleted executable row makes the real equality guard fail."""
    outcome_sets = {
        'success': SUCCESS_CASE_KEYS,
        'skipped': SKIPPED_CASE_KEYS,
        'failed': FAILED_CASE_KEYS,
    }
    missing_one = outcome_sets[outcome] - {next(iter(outcome_sets[outcome]))}
    arguments = {
        'success_keys': SUCCESS_CASE_KEYS,
        'skipped_keys': SKIPPED_CASE_KEYS,
        'failed_keys': FAILED_CASE_KEYS,
    }
    arguments[f'{outcome}_keys'] = missing_one

    with pytest.raises(AssertionError):
        _assert_every_inventory_key_has_outcome_contracts(**arguments)


def test_tariff_sync_has_only_the_reviewed_fail_closed_implementation():
    source = Path('app/cabinet/routes/admin_tariffs.py').read_text()
    tree = ast.parse(source)
    functions = _functions(tree)

    assert '_background_sync_squads' not in functions
    sync_body = functions['sync_tariff_squads'].body
    assert (
        sum(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == '_sync_tariff_squads_atomically'
            for node in ast.walk(functions['sync_tariff_squads'])
        )
        == 1
    )
    assert isinstance(sync_body[-1], ast.Return)


PANEL_SERVICE_CLASSES = {'RemnaWaveService', 'SubscriptionService'}
READ_ONLY_PANEL_METHOD_PREFIXES = ('get_', 'list_', 'find_', 'fetch_')
# ``bulk_execute`` is a request dispatcher, not an atomic mutation leaf: its
# executable children are the individually inventoried ``_do_*`` handlers.
BULK_ACTION_DISPATCHERS = {'bulk_execute'}
USER_STATUS_READ_ONLY_SYNC_HANDLERS = {'sync_user_from_panel'}


def _functions(tree: ast.AST) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {node.name: node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _imported_names(function: ast.AST) -> dict[str, str]:
    names = {}
    for node in ast.walk(function):
        if isinstance(node, ast.ImportFrom) and node.module:
            for imported in node.names:
                names[imported.asname or imported.name] = f'{node.module}.{imported.name}'
    return names


def _panel_calls(function: ast.AST) -> bool:
    imported = _imported_names(function)
    service_names = {name for name, source in imported.items() if source.rsplit('.', 1)[-1] in PANEL_SERVICE_CLASSES}
    subscription_service_functions = {
        name for name, source in imported.items() if source.rsplit('.', 1)[0] == 'app.services.subscription_service'
    }
    grace_functions = {
        name for name, source in imported.items() if source.rsplit('.', 1)[0] == 'app.services.grace_access_runtime'
    }
    service_instances = set()
    api_instances = set()

    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
            if node.value.func.id in service_names:
                service_instances.update(target.id for target in node.targets if isinstance(target, ast.Name))
        if isinstance(node, ast.AsyncWith):
            for item in node.items:
                if (
                    isinstance(item.context_expr, ast.Call)
                    and isinstance(item.context_expr.func, ast.Attribute)
                    and isinstance(item.context_expr.func.value, ast.Name)
                    and item.context_expr.func.value.id in service_instances
                    and item.context_expr.func.attr == 'get_api_client'
                    and isinstance(item.optional_vars, ast.Name)
                ):
                    api_instances.add(item.optional_vars.id)

    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            if node.func.id in subscription_service_functions:
                return True
            if node.func.id in grace_functions and any(
                isinstance(argument, ast.Name) and argument.id in api_instances for argument in node.args
            ):
                return True
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            receiver = node.func.value.id
            if receiver in service_instances and node.func.attr != 'get_api_client':
                return True
            if receiver in api_instances and not node.func.attr.startswith(READ_ONLY_PANEL_METHOD_PREFIXES):
                return True
    return False


def _local_panel_functions(tree: ast.AST) -> set[str]:
    functions = _functions(tree)
    panel_functions = {name for name, function in functions.items() if _panel_calls(function)}
    while True:
        callers = {
            name
            for name, function in functions.items()
            if any(
                isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in panel_functions
                for node in ast.walk(function)
            )
        }
        updated = panel_functions | callers
        if updated == panel_functions:
            return panel_functions
        panel_functions = updated


def _public_callers(tree: ast.AST, leaves: set[str]) -> set[str]:
    """Walk local wrappers to their public route rather than inventorying helpers."""
    functions = _functions(tree)
    reachable = set(leaves)
    while True:
        callers = {
            name
            for name, function in functions.items()
            if any(
                isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in reachable
                for node in ast.walk(function)
            )
        }
        updated = reachable | callers
        if updated == reachable:
            return {name for name in updated if not name.startswith('_')}
        reachable = updated


def _service_panel_methods(tree: ast.AST) -> set[str]:
    return _local_panel_functions(tree)


PANEL_RELEVANT_SUBSCRIPTION_FIELDS = {
    'status',
    'end_date',
    'tariff_id',
    'traffic_limit_gb',
    'device_limit',
    'connected_squads',
}


def _compared_string_values(function: ast.AST, variable: str) -> set[str]:
    values = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Compare) or not isinstance(node.left, ast.Name) or node.left.id != variable:
            continue
        for comparator in node.comparators:
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                values.add(comparator.value)
            elif (
                isinstance(comparator, ast.Attribute)
                and comparator.attr == 'value'
                and isinstance(comparator.value, ast.Attribute)
            ):
                values.add(comparator.value.attr.lower())
    return values


def _semantic_mutation_keys(tree: ast.AST) -> set[str]:
    """Discover bounded local mutations without depending on a panel call.

    Patterns are intentionally narrow: public status routes assigning
    ``user.status`` and ``request.action`` branches assigning one of the
    allowlisted subscription billing/panel fields on ``subscription``/``sub``.
    Read-sync routes are excluded explicitly to avoid classifying inbound state.
    """
    keys = set()
    for name, function in _functions(tree).items():
        if name.startswith('_') or name in USER_STATUS_READ_ONLY_SYNC_HANDLERS:
            continue
        assignments = [node for node in ast.walk(function) if isinstance(node, (ast.Assign, ast.AnnAssign))]
        writes_user_status = any(
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == 'user'
            and target.attr == 'status'
            for node in assignments
            for target in (node.targets if isinstance(node, ast.Assign) else (node.target,))
        )
        has_status_request = any(
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == 'new_status' for target in node.targets)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == 'value'
            and isinstance(node.value.value, ast.Attribute)
            and node.value.value.attr == 'status'
            and isinstance(node.value.value.value, ast.Name)
            and node.value.value.value.id == 'request'
            for node in assignments
        )
        if writes_user_status and has_status_request:
            actions = {status.value for status in UserStatusEnum} | _compared_string_values(function, 'new_status')
            keys |= {f'{name}:status_{action}' for action in actions}

        for node in ast.walk(function):
            if not (
                isinstance(node, ast.If)
                and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Attribute)
                and isinstance(node.test.left.value, ast.Name)
                and node.test.left.value.id == 'request'
                and node.test.left.attr == 'action'
                and len(node.test.comparators) == 1
                and isinstance(node.test.comparators[0], ast.Constant)
                and isinstance(node.test.comparators[0].value, str)
            ):
                continue
            has_local_subscription_write = any(
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id in {'subscription', 'sub'}
                and target.attr in PANEL_RELEVANT_SUBSCRIPTION_FIELDS
                for assignment in ast.walk(node)
                if isinstance(assignment, (ast.Assign, ast.AnnAssign))
                for target in (assignment.targets if isinstance(assignment, ast.Assign) else (assignment.target,))
            )
            if has_local_subscription_write:
                keys.add(f'{name}:{node.test.comparators[0].value}')
    return keys


def _route_panel_handlers(route_tree: ast.AST, user_service_tree: ast.AST) -> set[str]:
    functions = _functions(route_tree)
    panel_functions = _local_panel_functions(route_tree)
    user_service_methods = _service_panel_methods(user_service_tree)
    handlers = _public_callers(route_tree, panel_functions)
    user_service_leaves = set()

    for name, function in functions.items():
        imported = _imported_names(function)
        user_service_names = {
            alias for alias, source in imported.items() if source == 'app.services.user_service.UserService'
        }
        user_service_instances = {
            target.id
            for node in ast.walk(function)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id in user_service_names
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in user_service_instances
            and node.func.attr in user_service_methods
            for node in ast.walk(function)
        ):
            user_service_leaves.add(name)
    return handlers | _public_callers(route_tree, user_service_leaves)


def _panel_actions(route_tree: ast.AST, handler_name: str) -> set[str]:
    functions = _functions(route_tree)
    function = functions[handler_name]
    panel_functions = _local_panel_functions(route_tree)
    actions = set()
    for node in ast.walk(function):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Attribute)
            and isinstance(node.test.left.value, ast.Name)
            and node.test.left.value.id == 'request'
            and node.test.left.attr == 'action'
            and len(node.test.ops) == len(node.test.comparators) == 1
            and isinstance(node.test.ops[0], ast.Eq)
            and isinstance(node.test.comparators[0], ast.Constant)
            and isinstance(node.test.comparators[0].value, str)
            and (
                _panel_calls(node)
                or any(
                    isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in panel_functions
                    for call in ast.walk(node)
                )
            )
        ):
            actions.add(node.test.comparators[0].value)
    return actions


def _assert_classified(discovered: set[str], classified: set[str]) -> None:
    missing = discovered - classified
    assert not missing, f'panel mutations missing inventory entries: {sorted(missing)}'


def _transitive_function_callers(tree: ast.AST, roots: set[str]) -> set[str]:
    """Find every same-module function that can reach a hidden mutation root."""
    functions = _functions(tree)
    reachable = set(roots)
    while True:
        callers = {
            name
            for name, function in functions.items()
            if any(
                isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in reachable
                for node in ast.walk(function)
            )
        }
        updated = reachable | callers
        if updated == reachable:
            return updated
        reachable = updated


def _hidden_crud_mutation_keys(route_tree: ast.AST, hidden_crud_functions: set[str]) -> set[str]:
    """Map called CRUD side effects back to their public action/inventory key."""
    keys = set()
    for name, function in _functions(route_tree).items():
        if name == 'update_user_subscription':
            for node in ast.walk(function):
                if not (
                    isinstance(node, ast.If)
                    and isinstance(node.test, ast.Compare)
                    and isinstance(node.test.left, ast.Attribute)
                    and isinstance(node.test.left.value, ast.Name)
                    and node.test.left.value.id == 'request'
                    and node.test.left.attr == 'action'
                    and len(node.test.comparators) == 1
                    and isinstance(node.test.comparators[0], ast.Constant)
                ):
                    continue
                if any(
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id in hidden_crud_functions
                    for call in ast.walk(node)
                ):
                    keys.add(f'{name}:{node.test.comparators[0].value}')
        elif any(
            isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in hidden_crud_functions
            for call in ast.walk(function)
        ):
            keys.add(name)
    return keys


def test_panel_relevant_admin_handlers_and_called_services_are_explicitly_classified():
    user_service_tree = ast.parse(Path('app/services/user_service.py').read_text())
    discovered = set()
    for route_path in Path('app/cabinet/routes').glob('admin_*.py'):
        route_tree = ast.parse(route_path.read_text())
        discovered |= _route_panel_handlers(route_tree, user_service_tree)
        semantic_keys = _semantic_mutation_keys(route_tree)
        _assert_classified(semantic_keys, {entry.key for entry in MANDATORY_ADMIN_PANEL_MUTATIONS})
    discovered -= BULK_ACTION_DISPATCHERS
    classified = {entry.route for entry in MANDATORY_ADMIN_PANEL_MUTATIONS}

    _assert_classified(discovered, classified)
    assert {
        'block_user',
        'unblock_user',
        'sync_tariff_squads',
        'update_existing_tariff',
        'update_user_status',
    } <= discovered

    # sync_user_from_panel only reads the panel before updating the local DB.
    assert 'sync_user_from_panel' not in discovered


def test_called_crud_trial_side_effects_require_each_exact_target_inventory_contract():
    """CRUD helpers that disable sibling trials cannot remain hidden behind one-target rows."""
    crud_tree = ast.parse(Path('app/database/crud/subscription.py').read_text())
    hidden_callers = _transitive_function_callers(crud_tree, {'deactivate_user_trial_subscriptions'})
    assert {
        '_revive_paid_subscription',
        '_convert_trial_subscription_to_paid',
        'create_paid_subscription',
        'extend_subscription',
    } <= hidden_callers

    unified_tree = ast.parse(Path('app/cabinet/routes/admin_users.py').read_text())
    bulk_tree = ast.parse(Path('app/cabinet/routes/admin_bulk_actions.py').read_text())
    unified_keys = _hidden_crud_mutation_keys(unified_tree, hidden_callers)
    bulk_routes = _hidden_crud_mutation_keys(bulk_tree, hidden_callers)
    discovered_keys = {key for key in unified_keys if ':' in key}
    inventory = {entry.key: entry for entry in MANDATORY_ADMIN_PANEL_MUTATIONS}

    _assert_classified(discovered_keys, set(inventory))
    _assert_classified(bulk_routes, {entry.route for entry in MANDATORY_ADMIN_PANEL_MUTATIONS})
    assert {
        'update_user_subscription:create',
        'update_user_subscription:extend',
        'update_user_subscription:shorten',
    } <= discovered_keys
    assert {'_do_extend_subscription', '_do_grant_subscription'} <= bulk_routes

    affected_entries = [
        entry for entry in MANDATORY_ADMIN_PANEL_MUTATIONS if entry.key in discovered_keys or entry.route in bulk_routes
    ]
    assert affected_entries
    assert all(entry.multi_tariff_target is PanelSyncTarget.EACH_EXACT_SUBSCRIPTION_UUID for entry in affected_entries)
    assert all('deactivate_user_trial_subscriptions' in entry.integration_path for entry in affected_entries)


def test_new_called_crud_trial_side_effect_fails_inventory_guard():
    crud_tree = ast.parse("""
async def deactivate_user_trial_subscriptions():
    pass

async def new_paid_helper():
    await deactivate_user_trial_subscriptions()
""")
    route_tree = ast.parse("""
async def new_admin_paid_mutation():
    await new_paid_helper()
""")
    hidden_callers = _transitive_function_callers(crud_tree, {'deactivate_user_trial_subscriptions'})
    discovered = _hidden_crud_mutation_keys(route_tree, hidden_callers)

    with pytest.raises(AssertionError, match='new_admin_paid_mutation'):
        _assert_classified(discovered, set())


def test_each_shared_subscription_action_that_syncs_to_panel_has_an_inventory_entry():
    route_tree = ast.parse(Path('app/cabinet/routes/admin_users.py').read_text())
    discovered_keys = {
        f'update_user_subscription:{action}' for action in _panel_actions(route_tree, 'update_user_subscription')
    }
    inventory_keys = {entry.key for entry in MANDATORY_ADMIN_PANEL_MUTATIONS}

    _assert_classified(discovered_keys, inventory_keys)


def test_new_non_allowlisted_direct_panel_mutation_fails_inventory_guard():
    route_tree = ast.parse("""
async def new_admin_mutation():
    from app.services.remnawave_service import RemnaWaveService
    service = RemnaWaveService()
    async with service.get_api_client() as api:
        await api.rotate_connection_identity('user-uuid')
""")

    with pytest.raises(AssertionError, match='new_admin_mutation'):
        _assert_classified(_route_panel_handlers(route_tree, ast.parse('')), set())


def test_new_shared_sync_action_fails_inventory_guard():
    route_tree = ast.parse("""
async def _sync_subscription_to_panel():
    from app.services.remnawave_service import RemnaWaveService
    service = RemnaWaveService()
    async with service.get_api_client() as api:
        await api.rotate_connection_identity('user-uuid')

async def update_user_subscription(request):
    if request.action == 'unlisted_sync_action':
        await _sync_subscription_to_panel()
""")

    discovered_keys = {
        f'update_user_subscription:{action}' for action in _panel_actions(route_tree, 'update_user_subscription')
    }
    with pytest.raises(AssertionError, match='unlisted_sync_action'):
        _assert_classified(discovered_keys, {'update_user_subscription:extend'})


def test_real_status_enum_extension_fails_semantic_inventory_guard(monkeypatch):
    """A generic status route must inventory every value accepted by its request enum."""

    class ExtendedUserStatusEnum(StrEnum):
        ACTIVE = 'active'
        BLOCKED = 'blocked'
        DELETED = 'deleted'
        SUSPENDED = 'suspended'

    monkeypatch.setattr(sys.modules[__name__], 'UserStatusEnum', ExtendedUserStatusEnum)
    route_tree = ast.parse(Path('app/cabinet/routes/admin_users.py').read_text())
    with pytest.raises(AssertionError, match='status_suspended'):
        _assert_classified(
            _semantic_mutation_keys(route_tree),
            {
                'update_user_status:status_active',
                'update_user_status:status_blocked',
                'update_user_status:status_deleted',
            },
        )


def test_new_local_subscription_action_fails_real_semantic_inventory_guard():
    route_tree = ast.parse("""
async def update_user_subscription(request, subscription):
    if request.action == 'suspend_locally':
        subscription.status = 'disabled'
""")
    with pytest.raises(AssertionError, match='suspend_locally'):
        _assert_classified(
            _semantic_mutation_keys(route_tree),
            {'update_user_subscription:extend'},
        )
