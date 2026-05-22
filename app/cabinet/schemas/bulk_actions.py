"""Schemas for admin bulk actions."""

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class BulkActionType(StrEnum):
    EXTEND_SUBSCRIPTION = 'extend_subscription'
    CANCEL_SUBSCRIPTION = 'cancel_subscription'
    ACTIVATE_SUBSCRIPTION = 'activate_subscription'
    CHANGE_TARIFF = 'change_tariff'
    ADD_DAYS = 'add_days'
    ADD_TRAFFIC = 'add_traffic'
    ADD_BALANCE = 'add_balance'
    ASSIGN_PROMO_GROUP = 'assign_promo_group'
    GRANT_SUBSCRIPTION = 'grant_subscription'
    SET_DEVICES = 'set_devices'
    DELETE_SUBSCRIPTION = 'delete_subscription'
    DELETE_USER = 'delete_user'


class BulkActionParams(BaseModel):
    days: int | None = Field(None, ge=1, le=3650)
    tariff_id: int | None = Field(None, gt=0)
    traffic_gb: int | None = Field(None, ge=1, le=10000)
    amount_kopeks: int | None = Field(None, ge=1, le=2_000_000_000)
    balance_description: str = Field(default='Массовое начисление баланса', max_length=500)
    promo_group_id: int | None = None
    device_limit: int | None = Field(None, ge=1, le=50)
    delete_from_panel: bool = Field(default=True)
    force_delete_active_paid: bool = Field(default=False)


class BulkSubscriptionInfo(BaseModel):
    id: int
    tariff_id: int | None = None
    tariff_name: str | None = None
    status: str
    days_remaining: int
    traffic_used_gb: float = 0
    traffic_limit_gb: int = 0
    device_limit: int = 0


class BulkExecuteRequest(BaseModel):
    action: BulkActionType
    user_ids: list[int] | None = Field(None, min_length=1, max_length=500)
    subscription_ids: list[int] | None = Field(None, min_length=1, max_length=2000)
    params: BulkActionParams = Field(default_factory=BulkActionParams)
    dry_run: bool = Field(default=False, description='Preview only, no mutations')

    @model_validator(mode='after')
    def _exactly_one_target(self):
        has_users = self.user_ids is not None
        has_subs = self.subscription_ids is not None
        if has_users == has_subs:
            raise ValueError('Exactly one of user_ids or subscription_ids must be provided')
        return self


class BulkUserResult(BaseModel):
    user_id: int
    subscription_id: int | None = None
    success: bool
    message: str
    username: str | None = None
    subscriptions: list[BulkSubscriptionInfo] | None = None


class BulkExecuteResponse(BaseModel):
    action: str
    total: int
    success_count: int
    error_count: int
    skipped_count: int
    dry_run: bool
    results: list[BulkUserResult]
