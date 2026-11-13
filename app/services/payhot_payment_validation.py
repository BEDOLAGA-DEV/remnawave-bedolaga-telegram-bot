"""Pure HOT Pay receipt validation shared by top-ups and enrollment.

No payment orchestration imports: ordinary and recurring receipts use the same
identity/principal checks without importing each other's dispatchers.
"""

from uuid import UUID


STATUSES = frozenset(
    (
        'requires_payment_method',
        'requires_action',
        'processing',
        'authorized',
        'partially_captured',
        'succeeded',
        'failed',
        'cancelled',
        'refunded',
        'partially_refunded',
        'manual_reconciliation',
    )
)


def is_refund_review(payment):
    return (
        payment.status in ('refunded', 'partially_refunded')
        or (payment.fulfillment_state or {}).get('reason') == 'refund_review'
    )


def _validate_remote(payment, data):
    if not isinstance(data, dict):
        raise ValueError('Invalid PayHot payment')
    # Recurring charges use payment.* events too, but must be reconciled
    # against their mandate/cycle receipt, never an ordinary balance invoice.
    if data.get('origin') == 'recurring' or any(
        data.get(key) is not None for key in ('recurring_charge_id', 'merchant_recurring_charge_reference')
    ):
        raise ValueError('PayHot recurring charge is not an ordinary payment')
    remote_id = data.get('id')
    try:
        if not isinstance(remote_id, str) or str(UUID(remote_id)) != remote_id:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError('Invalid PayHot identifier') from None
    if payment.payhot_payment_id and remote_id != payment.payhot_payment_id:
        raise ValueError('PayHot identifier mismatch')
    if data.get('merchant_order_reference') != payment.order_id:
        raise ValueError('PayHot order mismatch')
    if data.get('currency') != 'RUB' or data.get('amount_minor') != str(payment.amount_kopeks):
        raise ValueError('PayHot principal mismatch')
    if data.get('payment_method') not in (
        {'card', 'applepay', 'googlepay'} if payment.payment_method == 'card' else {payment.payment_method}
    ):
        raise ValueError('PayHot method mismatch')
    if data.get('status') not in STATUSES:
        raise ValueError('Invalid PayHot status')
    environment = data.get('environment')
    if environment is not None and environment != 'production':
        raise ValueError('PayHot environment mismatch')
    fees = data.get('fees')
    if fees is not None and (not isinstance(fees, dict) or fees.get('principal_minor') != str(payment.amount_kopeks)):
        raise ValueError('PayHot fee principal mismatch')
    return remote_id
