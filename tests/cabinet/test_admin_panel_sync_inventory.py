import ast
from pathlib import Path

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


def test_panel_relevant_admin_handlers_are_explicitly_classified():
    roots = (
        Path('app/cabinet/routes/admin_users.py'),
        Path('app/cabinet/routes/admin_bulk_actions.py'),
    )
    needles = {
        '_sync_subscription_to_panel',
        'disable_remnawave_user',
        'enable_remnawave_user',
        'reset_subscription_with_panel',
        'delete_user_account',
        'wipe_trial_subscriptions',
        'remove_device',
        'create_panel_user_grace_safe',
        'update_panel_user_grace_safe',
    }
    discovered = set()
    for path in roots:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == '_sync_subscription_to_panel':
                    continue
                called = {
                    child.func.attr if isinstance(child.func, ast.Attribute) else child.func.id
                    for child in ast.walk(node)
                    if isinstance(child, ast.Call) and isinstance(child.func, (ast.Name, ast.Attribute))
                }
                if called & needles:
                    discovered.add(node.name)

    classified = {entry.route for entry in MANDATORY_ADMIN_PANEL_MUTATIONS}
    assert discovered <= classified

    # sync_user_from_panel reads RemnaWave and updates local state from it; it
    # is intentionally not an outbound local-mutation-to-panel action.
    assert 'sync_user_from_panel' not in discovered
