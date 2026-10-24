"""Merchant-paid gateway fees; dated policies never change customer balances."""

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import PaymentMethodConfig, Transaction


class FeeRate(BaseModel):
    percent: Decimal = Field(default=Decimal(0), ge=0, le=100, max_digits=7, decimal_places=4)
    fixed_kopeks: int = Field(default=0, ge=0, le=100_000_000)

    def calculate(self, amount: int) -> int:
        value = Decimal(amount) * self.percent / 100 + self.fixed_kopeks
        return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


class FeePolicy(BaseModel):
    effective_from: datetime
    default: FeeRate | None = None
    sub_methods: dict[str, FeeRate] = Field(default_factory=dict, max_length=30)

    @field_validator('effective_from')
    @classmethod
    def aware_time(cls, value):
        if value.tzinfo is None:
            raise ValueError('effective_from must include a timezone')
        return value.astimezone(UTC)

    @field_validator('sub_methods')
    @classmethod
    def valid_keys(cls, value):
        if any(not key or len(key) > 50 or key != key.strip().lower() for key in value):
            raise ValueError('Sub-method keys must be nonempty lowercase identifiers (up to 50 characters)')
        return value


def normalize_policies(value: list[dict] | None) -> list[dict]:
    policies = [FeePolicy.model_validate(item) for item in (value or [])]
    if len(policies) > 100:
        raise ValueError('At most 100 fee policies are allowed')
    dates = [item.effective_from for item in policies]
    if len(dates) != len(set(dates)):
        raise ValueError('Fee policy effective_from values must be unique')
    return [item.model_dump(mode='json') for item in sorted(policies, key=lambda item: item.effective_from)]


def policy_at(policies: list[FeePolicy], moment: datetime) -> FeePolicy | None:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max((p for p in policies if p.effective_from <= moment), key=lambda p: p.effective_from, default=None)


async def _sub_methods(db: AsyncSession, provider: str, transaction_query) -> dict[int, str]:
    # Join via the accounting transaction, never via description or provider order ID.
    from app.database.crud.payment_gateway_stats import _GATEWAY_REGISTRY

    model = next((model for method, model, _ in _GATEWAY_REGISTRY if method == provider), None)
    if model is None:
        return {}
    column = next(
        (
            getattr(model, name)
            for name in ('payment_method_code', 'payment_method_type', 'payment_method', 'payment_system_id')
            if hasattr(model, name)
        ),
        None,
    )
    if column is None:
        return {}
    rows = await db.execute(select(model.transaction_id, column).where(model.transaction_id.in_(transaction_query)))
    # Conflicting gateway records must not pick an arbitrary cheaper rate.
    result = {}
    for transaction_id, value in rows:
        key = str(value).strip().lower() if value is not None else ''
        if provider == 'yookassa' and key == 'bank_card':
            key = 'card'
        if transaction_id in result and result[transaction_id] != key:
            result[transaction_id] = ''
        else:
            result[transaction_id] = key
    return result


async def get_merchant_revenue(db: AsyncSession, start: datetime, end: datetime) -> dict:
    from app.database.crud.transaction import REAL_PAYMENT_METHODS

    query = select(Transaction.id).where(
        Transaction.is_completed.is_(True),
        Transaction.type.in_(('deposit', 'subscription_payment')),
        Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
        Transaction.created_at >= start,
        Transaction.created_at <= end,
    )
    configs = (await db.execute(select(PaymentMethodConfig.method_id, PaymentMethodConfig.merchant_fee_policies))).all()
    policies = {method: [FeePolicy.model_validate(p) for p in (rules or [])] for method, rules in configs}
    sub_methods = {}
    providers = (
        (await db.execute(select(Transaction.payment_method).where(Transaction.id.in_(query)).distinct()))
        .scalars()
        .all()
    )
    for provider in providers:
        provider_query = query.where(Transaction.payment_method == provider)
        sub_methods[provider] = await _sub_methods(db, provider, provider_query)

    def empty():
        return dict(
            count=0, gross_kopeks=0, fee_kopeks=0, net_kopeks=0, unconfigured_count=0, unknown_sub_method_count=0
        )

    total, groups = empty(), {}
    rows = await db.stream(
        select(Transaction.id, Transaction.payment_method, Transaction.amount_kopeks, Transaction.created_at).where(
            Transaction.id.in_(query)
        )
    )
    async for transaction_id, provider, amount, created_at in rows:
        gross = abs(amount)
        policy = policy_at(policies.get(provider, []), created_at)
        sub = sub_methods.get(provider, {}).get(transaction_id, '')
        rate = policy.sub_methods.get(sub, policy.default) if policy is not None else None
        fee = rate.calculate(gross) if rate is not None else 0
        group = groups.setdefault((provider, sub), empty())
        for accumulator in (group, total):
            accumulator['count'] += 1
            accumulator['gross_kopeks'] += gross
            accumulator['fee_kopeks'] += fee
            accumulator['net_kopeks'] += gross - fee
            accumulator['unconfigured_count'] += int(rate is None)
            accumulator['unknown_sub_method_count'] += int(bool(policy and policy.sub_methods and not sub))
    return {
        **total,
        'by_method': [
            dict(provider=provider, sub_method=sub or None, **values)
            for (provider, sub), values in sorted(groups.items())
        ],
    }
