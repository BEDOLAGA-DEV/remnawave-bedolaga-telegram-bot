from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.cabinet.routes.admin_payment_methods import PaymentMethodConfigUpdateRequest
from app.cabinet.routes.admin_stats import merchant_revenue


def test_fee_schema_keeps_independent_sbp_card_crypto_rates():
    request = PaymentMethodConfigUpdateRequest(
        merchant_fee_policies=[
            {
                'effective_from': '2025-01-01T00:00:00Z',
                'sub_methods': {'sbp': {'percent': '2'}, 'card': {'percent': '4'}, 'crypto': {'percent': '1'}},
            }
        ]
    )
    rates = request.merchant_fee_policies[0].sub_methods
    assert rates['sbp'].calculate(10000) == 200
    assert rates['card'].calculate(10000) == 400
    assert rates['crypto'].calculate(10000) == 100


def test_duplicate_policy_dates_rejected_at_api_boundary():
    with pytest.raises(ValidationError):
        PaymentMethodConfigUpdateRequest(merchant_fee_policies=[{'effective_from': '2025-01-01T00:00:00Z'}] * 2)


@pytest.mark.asyncio
async def test_invalid_report_range_does_not_query_database():
    with pytest.raises(HTTPException) as error:
        await merchant_revenue(
            start=datetime(2025, 2, 1, tzinfo=UTC), end=datetime(2025, 1, 1, tzinfo=UTC), admin=None, db=None
        )
    assert error.value.status_code == 422
