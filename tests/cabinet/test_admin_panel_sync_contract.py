from app.services.admin_panel_sync import (
    AdminPanelMutation,
    PanelSyncFailed,
    PanelSyncReason,
    PanelSyncSkipped,
    panel_sync_failure_message,
)


def test_typed_failures_are_bounded_and_safe():
    skipped = PanelSyncSkipped(PanelSyncReason.NOT_CONFIGURED)
    failed = PanelSyncFailed(PanelSyncReason.PANEL_API_FAILED)

    assert skipped.reason_code is PanelSyncReason.NOT_CONFIGURED
    assert failed.reason_code is PanelSyncReason.PANEL_API_FAILED

    message = panel_sync_failure_message()
    assert 'not saved' in message.lower()
    assert 'token' not in message.lower()


def test_mutation_key_identifies_one_route_action_pair():
    mutation = AdminPanelMutation(
        route='update_user_subscription',
        action='extend',
        mutation_class='extend',
        integration_path='_sync_subscription_to_panel',
        transaction_owner='update_user_subscription',
    )

    assert mutation.key == 'update_user_subscription:extend'
