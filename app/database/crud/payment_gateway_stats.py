"""Per-gateway payment success-rate (paid vs total created).

Every gateway payment table inserts a row at payment INITIATION (pending) and
flips it on the provider webhook, so for a period:
    success_rate = paid / total_created.

The "paid" signal is the ``is_paid`` Boolean column on 19 of the 21 gateways;
CryptoBot and Heleket expose ``is_paid`` only as a Python property (computed from
``status``), so for those we match the status string directly in SQL.

Keeping the gateway list here (one entry per table) means a new gateway is added
in exactly one place, mirroring how REAL_PAYMENT_METHODS is derived centrally.
"""

from datetime import datetime

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AntilopayPayment,
    AuraPayPayment,
    CloudPaymentsPayment,
    CryptoBotPayment,
    DonutPayment,
    EtoplatezhiPayment,
    FreekassaPayment,
    HeleketPayment,
    JupiterPayment,
    KassaAiPayment,
    LavaPayment,
    MulenPayPayment,
    OverpayPayment,
    Pal24Payment,
    PaymentMethod,
    PayPearPayment,
    PlategaPayment,
    RioPayPayment,
    RollyPayPayment,
    SeverPayPayment,
    WataPayment,
    YooKassaPayment,
)


# (PaymentMethod value, payment model, "successfully paid" SQL predicate).
# 19 gateways use the is_paid Boolean column; CryptoBot/Heleket match status.
_GATEWAY_REGISTRY: list[tuple[str, type, object]] = [
    (PaymentMethod.YOOKASSA.value, YooKassaPayment, YooKassaPayment.is_paid.is_(True)),
    (PaymentMethod.CRYPTOBOT.value, CryptoBotPayment, CryptoBotPayment.status == 'paid'),
    (PaymentMethod.HELEKET.value, HeleketPayment, HeleketPayment.status.in_(('paid', 'paid_over'))),
    (PaymentMethod.MULENPAY.value, MulenPayPayment, MulenPayPayment.is_paid.is_(True)),
    (PaymentMethod.PAL24.value, Pal24Payment, Pal24Payment.is_paid.is_(True)),
    (PaymentMethod.WATA.value, WataPayment, WataPayment.is_paid.is_(True)),
    (PaymentMethod.PLATEGA.value, PlategaPayment, PlategaPayment.is_paid.is_(True)),
    (PaymentMethod.CLOUDPAYMENTS.value, CloudPaymentsPayment, CloudPaymentsPayment.is_paid.is_(True)),
    (PaymentMethod.FREEKASSA.value, FreekassaPayment, FreekassaPayment.is_paid.is_(True)),
    (PaymentMethod.KASSA_AI.value, KassaAiPayment, KassaAiPayment.is_paid.is_(True)),
    (PaymentMethod.RIOPAY.value, RioPayPayment, RioPayPayment.is_paid.is_(True)),
    (PaymentMethod.SEVERPAY.value, SeverPayPayment, SeverPayPayment.is_paid.is_(True)),
    (PaymentMethod.PAYPEAR.value, PayPearPayment, PayPearPayment.is_paid.is_(True)),
    (PaymentMethod.ROLLYPAY.value, RollyPayPayment, RollyPayPayment.is_paid.is_(True)),
    (PaymentMethod.OVERPAY.value, OverpayPayment, OverpayPayment.is_paid.is_(True)),
    (PaymentMethod.AURAPAY.value, AuraPayPayment, AuraPayPayment.is_paid.is_(True)),
    (PaymentMethod.ETOPLATEZHI.value, EtoplatezhiPayment, EtoplatezhiPayment.is_paid.is_(True)),
    (PaymentMethod.ANTILOPAY.value, AntilopayPayment, AntilopayPayment.is_paid.is_(True)),
    (PaymentMethod.JUPITER.value, JupiterPayment, JupiterPayment.is_paid.is_(True)),
    (PaymentMethod.DONUT.value, DonutPayment, DonutPayment.is_paid.is_(True)),
    (PaymentMethod.LAVA.value, LavaPayment, LavaPayment.is_paid.is_(True)),
]


async def get_gateway_success_rates(
    db: AsyncSession,
    period_start: datetime,
    period_end: datetime,
) -> list[dict]:
    """Per-gateway {method, total, paid, success_rate} for gateways with activity.

    Gateways with zero attempts in the period are omitted so the UI only shows
    gateways that were actually used.
    """
    rows: list[dict] = []
    for method, model, paid_predicate in _GATEWAY_REGISTRY:
        result = await db.execute(
            select(
                func.count(model.id).label('total'),
                func.count(case((paid_predicate, model.id))).label('paid'),
            ).where(
                and_(
                    model.created_at >= period_start,
                    model.created_at <= period_end,
                )
            )
        )
        row = result.one()
        total = row.total or 0
        if total == 0:
            continue
        paid = row.paid or 0
        rows.append(
            {
                'method': method,
                'total': total,
                'paid': paid,
                'success_rate': round(paid / total * 100, 1),
            }
        )
    rows.sort(key=lambda item: item['total'], reverse=True)
    return rows
