import pytest

from app.services.admin_panel_sync import (
    MANDATORY_ADMIN_PANEL_MUTATIONS,
    AdminPanelMutation,
    PanelSyncFailed,
    PanelSyncReason,
    PanelSyncSkipped,
    PanelSyncTarget,
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
        multi_tariff_target=PanelSyncTarget.EXACT_SUBSCRIPTION_UUID,
    )

    assert mutation.key == 'update_user_subscription:extend'


def test_multi_tariff_inventory_targets_exact_subscription_uuid_without_user_fallback():
    assert {entry.multi_tariff_target for entry in MANDATORY_ADMIN_PANEL_MUTATIONS} <= {
        PanelSyncTarget.EXACT_SUBSCRIPTION_UUID,
        PanelSyncTarget.EACH_EXACT_SUBSCRIPTION_UUID,
    }
    assert all(
        'user.remnawave_uuid' not in entry.multi_tariff_target.value for entry in MANDATORY_ADMIN_PANEL_MUTATIONS
    )


@pytest.mark.parametrize('reason_code', ['panel-token-secret', 'unknown_reason', object()])
def test_typed_failures_reject_unbounded_reason_values(reason_code):
    with pytest.raises(TypeError, match='PanelSyncReason') as error:
        PanelSyncFailed(reason_code)  # type: ignore[arg-type]

    assert 'panel-token-secret' not in str(error.value)
