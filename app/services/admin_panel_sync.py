"""Typed contract and inventory for admin mutations that require panel sync.

The inventory deliberately names the current handlers, integration path, and
future transaction owner.  Later atomicity tasks consume it when moving the
commit boundary out of panel-sync helpers.
"""

from dataclasses import dataclass
from enum import StrEnum


class PanelSyncReason(StrEnum):
    NOT_CONFIGURED = 'not_configured'
    MISSING_SUBSCRIPTION_UUID = 'missing_subscription_uuid'
    PANEL_API_FAILED = 'panel_api_failed'
    PANEL_RESPONSE_INVALID = 'panel_response_invalid'
    PANEL_TIMEOUT_UNKNOWN = 'panel_timeout_unknown'


class PanelSyncTarget(StrEnum):
    """Required RemnaWave target when multi-tariff mode is enabled."""

    EXACT_SUBSCRIPTION_UUID = 'subscription.remnawave_uuid'
    EACH_EXACT_SUBSCRIPTION_UUID = 'each subscription.remnawave_uuid'


class PanelSyncError(RuntimeError):
    def __init__(self, reason_code: PanelSyncReason) -> None:
        if not isinstance(reason_code, PanelSyncReason):
            raise TypeError('reason_code must be a PanelSyncReason')
        super().__init__(reason_code.value)
        self.reason_code = reason_code


class PanelSyncSkipped(PanelSyncError):
    pass


class PanelSyncFailed(PanelSyncError):
    pass


@dataclass(frozen=True, slots=True)
class AdminPanelMutation:
    route: str
    action: str
    mutation_class: str
    integration_path: str
    transaction_owner: str
    multi_tariff_target: PanelSyncTarget
    classification: str = 'mandatory-sync'

    def __post_init__(self) -> None:
        if not isinstance(self.multi_tariff_target, PanelSyncTarget):
            raise TypeError('multi_tariff_target must be a PanelSyncTarget')

    @property
    def key(self) -> str:
        return f'{self.route}:{self.action}'


def _mutation(
    route: str,
    action: str,
    mutation_class: str,
    integration_path: str,
    multi_tariff_target: PanelSyncTarget = PanelSyncTarget.EXACT_SUBSCRIPTION_UUID,
) -> AdminPanelMutation:
    return AdminPanelMutation(
        route=route,
        action=action,
        mutation_class=mutation_class,
        integration_path=integration_path,
        transaction_owner=route,
        multi_tariff_target=multi_tariff_target,
    )


MANDATORY_ADMIN_PANEL_MUTATIONS = (
    # Single-subscription admin route.
    _mutation('update_user_subscription', 'create', 'create', '_sync_subscription_to_panel'),
    _mutation('update_user_subscription', 'extend', 'extend', '_sync_subscription_to_panel'),
    _mutation('update_user_subscription', 'shorten', 'extend', '_sync_subscription_to_panel'),
    _mutation('update_user_subscription', 'set_end_date', 'set_end_date', '_sync_subscription_to_panel'),
    _mutation('update_user_subscription', 'change_tariff', 'change_tariff', '_sync_subscription_to_panel'),
    _mutation('update_user_subscription', 'set_traffic', 'set_traffic', '_sync_subscription_to_panel'),
    _mutation('update_user_subscription', 'cancel', 'cancel', '_sync_subscription_to_panel'),
    _mutation('update_user_subscription', 'reset', 'reset', 'reset_subscription_with_panel'),
    _mutation('update_user_subscription', 'activate', 'activate', '_sync_subscription_to_panel'),
    _mutation(
        'update_user_subscription', 'add_traffic', 'set_traffic', '_sync_subscription_to_panel + enable_remnawave_user'
    ),
    _mutation('update_user_subscription', 'remove_traffic', 'set_traffic', '_sync_subscription_to_panel'),
    _mutation('update_user_subscription', 'set_device_limit', 'set_devices', '_sync_subscription_to_panel'),
    # Direct single-user panel operations.
    _mutation('delete_user_device', 'delete_device', 'set_devices', 'RemnaWaveService.remove_device'),
    _mutation('reset_user_devices', 'reset_devices', 'reset', 'RemnaWaveService.remove_device'),
    _mutation(
        'full_delete_user',
        'delete_user',
        'delete_user',
        'UserService.delete_user_account',
        PanelSyncTarget.EACH_EXACT_SUBSCRIPTION_UUID,
    ),
    _mutation(
        'reset_user_trial',
        'reset_trial',
        'delete_subscription',
        'wipe_trial_subscriptions',
        PanelSyncTarget.EACH_EXACT_SUBSCRIPTION_UUID,
    ),
    _mutation(
        'reset_user_subscription',
        'reset_subscription',
        'reset',
        'SubscriptionService.disable_remnawave_user',
        PanelSyncTarget.EACH_EXACT_SUBSCRIPTION_UUID,
    ),
    _mutation(
        'disable_user',
        'disable',
        'disable_user',
        'SubscriptionService.disable_remnawave_user',
        PanelSyncTarget.EACH_EXACT_SUBSCRIPTION_UUID,
    ),
    _mutation(
        'block_user',
        'block',
        'disable_user',
        'UserService.block_user -> SubscriptionService.disable_remnawave_user',
        PanelSyncTarget.EACH_EXACT_SUBSCRIPTION_UUID,
    ),
    _mutation(
        'unblock_user',
        'unblock',
        'activate',
        'UserService.unblock_user -> SubscriptionService.update_remnawave_user',
        PanelSyncTarget.EACH_EXACT_SUBSCRIPTION_UUID,
    ),
    _mutation('sync_user_to_panel', 'sync_to_panel', 'sync', 'RemnaWaveService direct API'),
    # Bulk action handlers behind POST /admin/bulk/execute.
    _mutation('_do_extend_subscription', 'extend_subscription', 'extend', '_sync_subscription_to_panel'),
    _mutation('_do_cancel_subscription', 'cancel_subscription', 'cancel', '_sync_subscription_to_panel'),
    _mutation('_do_activate_subscription', 'activate_subscription', 'activate', '_sync_subscription_to_panel'),
    _mutation('_do_change_tariff', 'change_tariff', 'change_tariff', '_sync_subscription_to_panel'),
    _mutation('_do_add_traffic', 'add_traffic', 'set_traffic', '_sync_subscription_to_panel + enable_remnawave_user'),
    _mutation('_do_set_devices', 'set_devices', 'set_devices', '_sync_subscription_to_panel'),
    _mutation(
        '_do_delete_subscription',
        'delete_subscription',
        'delete_subscription',
        'SubscriptionService.disable_remnawave_user',
    ),
    _mutation('_do_delete_user', 'delete_user', 'delete_user', 'UserService.delete_user_account'),
    _mutation('_do_grant_subscription', 'grant_subscription', 'create', '_sync_subscription_to_panel'),
)


BEST_EFFORT_ADMIN_PANEL_MUTATIONS: tuple[AdminPanelMutation, ...] = ()


def panel_sync_failure_message() -> str:
    return 'The local mutation was not saved because panel synchronization did not complete.'
