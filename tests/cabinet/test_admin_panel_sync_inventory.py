import ast
from pathlib import Path

import pytest

from app.services.admin_panel_sync import (
    BEST_EFFORT_ADMIN_PANEL_MUTATIONS,
    MANDATORY_ADMIN_PANEL_MUTATIONS,
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


PANEL_SERVICE_CLASSES = {'RemnaWaveService', 'SubscriptionService'}
READ_ONLY_PANEL_METHOD_PREFIXES = ('get_', 'list_', 'find_', 'fetch_')


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


def _service_panel_methods(tree: ast.AST) -> set[str]:
    return _local_panel_functions(tree)


def _route_panel_handlers(route_tree: ast.AST, user_service_tree: ast.AST) -> set[str]:
    functions = _functions(route_tree)
    panel_functions = _local_panel_functions(route_tree)
    user_service_methods = _service_panel_methods(user_service_tree)
    handlers = {name for name in panel_functions if name != '_sync_subscription_to_panel'}

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
            handlers.add(name)
    return handlers


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


def test_panel_relevant_admin_handlers_and_called_services_are_explicitly_classified():
    route_tree = ast.parse(Path('app/cabinet/routes/admin_users.py').read_text())
    bulk_tree = ast.parse(Path('app/cabinet/routes/admin_bulk_actions.py').read_text())
    user_service_tree = ast.parse(Path('app/services/user_service.py').read_text())

    discovered = _route_panel_handlers(route_tree, user_service_tree) | _route_panel_handlers(
        bulk_tree, user_service_tree
    )
    classified = {entry.route for entry in MANDATORY_ADMIN_PANEL_MUTATIONS}

    _assert_classified(discovered, classified)
    assert {'block_user', 'unblock_user'} <= discovered

    # sync_user_from_panel only reads the panel before updating the local DB.
    assert 'sync_user_from_panel' not in discovered


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
