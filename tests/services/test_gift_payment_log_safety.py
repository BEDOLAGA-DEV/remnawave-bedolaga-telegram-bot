"""Static invariants preventing gift credentials from leaking through payment logs."""

import inspect

from app.services.payment.common import try_fulfill_guest_purchase
from app.services.payment_service import PaymentService


def test_gift_payment_paths_do_not_log_purchase_token_fragments():
    sources = (
        inspect.getsource(try_fulfill_guest_purchase),
        inspect.getsource(PaymentService.create_guest_payment),
    )

    for source in sources:
        assert 'purchase_token_prefix' not in source
