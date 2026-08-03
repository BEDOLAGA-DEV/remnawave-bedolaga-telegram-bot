"""Side-effect-free outcome matrices for admin panel-sync route contracts.

This module deliberately imports neither application code nor test modules.
Each tuple is consumed by a real parameterized route test; inventory coverage
uses only the derived key sets below, so removing an outcome row fails the
inventory guard rather than silently updating a copied aggregate.
"""

UNIFIED_ACTIONS = (
    'create',
    'extend',
    'shorten',
    'set_end_date',
    'change_tariff',
    'set_traffic',
    'cancel',
    'reset',
    'activate',
    'add_traffic',
    'remove_traffic',
    'set_device_limit',
)
UNIFIED_CASES = tuple((f'update_user_subscription:{action}', action) for action in UNIFIED_ACTIONS)

BULK_ACTIONS = (
    'extend_subscription',
    'cancel_subscription',
    'activate_subscription',
    'change_tariff',
    'add_traffic',
    'set_devices',
    'delete_subscription',
    'delete_user',
    'grant_subscription',
)
BULK_CASES = tuple((f'_do_{action}:{action}', action) for action in BULK_ACTIONS)

# Existing focused public-route tests exercise these direct-route rows.
DIRECT_SUCCESS_CASES = (
    ('delete_user_device:delete_device', 'delete device'),
    ('reset_user_devices:reset_devices', 'reset devices'),
    ('full_delete_user:delete_user', 'full delete'),
    ('delete_user:delete_user', 'delete user'),
    ('reset_user_trial:reset_trial', 'trial reset'),
    ('reset_user_subscription:reset_subscription', 'subscription reset'),
    ('disable_user:disable', 'disable user'),
    ('block_user:block', 'block user'),
    ('unblock_user:unblock', 'unblock user'),
    ('sync_user_to_panel:sync_to_panel', 'direct sync'),
)
DIRECT_SKIPPED_CASES = DIRECT_SUCCESS_CASES
DIRECT_FAILED_CASES = DIRECT_SUCCESS_CASES

# These rows are each used by an outcome-specific public-route parametrization
# in ``test_admin_tariff_panel_sync_contract.py``.
TARIFF_SUCCESS_CASES = (
    ('update_existing_tariff:tariff_update_sync_squads', 'update'),
    ('sync_tariff_squads:sync_squads', 'manual-sync'),
)
TARIFF_SKIPPED_CASES = TARIFF_SUCCESS_CASES
TARIFF_FAILED_CASES = TARIFF_SUCCESS_CASES

STANDALONE_SUCCESS_CASES = DIRECT_SUCCESS_CASES + TARIFF_SUCCESS_CASES
STANDALONE_SKIPPED_CASES = DIRECT_SKIPPED_CASES + TARIFF_SKIPPED_CASES
STANDALONE_FAILED_CASES = DIRECT_FAILED_CASES + TARIFF_FAILED_CASES

SUCCESS_CASE_KEYS = frozenset(key for key, _ in UNIFIED_CASES + BULK_CASES + STANDALONE_SUCCESS_CASES)
SKIPPED_CASE_KEYS = frozenset(key for key, _ in UNIFIED_CASES + BULK_CASES + STANDALONE_SKIPPED_CASES)
FAILED_CASE_KEYS = frozenset(key for key, _ in UNIFIED_CASES + BULK_CASES + STANDALONE_FAILED_CASES)
