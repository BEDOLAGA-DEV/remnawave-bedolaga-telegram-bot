"""Immutable PayHot consent and UTC scheduling; no storage or provider calls.

The merchant schedules exact purchased days. A successful late renewal starts
at acceptance, matching Subscription.extend_subscription; it does not backdate
access to a missed billing date. These helpers alone do not authorize a charge.
"""

import json
from dataclasses import InitVar, dataclass, field
from datetime import UTC, datetime, timedelta


MAX_AMOUNT_KOPEKS = 2_147_483_647
MAX_PERIOD_DAYS = (datetime.max - datetime.min).days


def _integer(value, *, minimum, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError('Invalid PayHot recurring integral term')


def _canonical_snapshot(snapshot):
    if type(snapshot) is not dict:
        raise ValueError('PayHot service snapshot must be an object')

    def validate(value, depth=0):
        if depth > 64:
            raise ValueError('PayHot service snapshot is too deep')
        if value is None or type(value) in (str, bool, int):
            return
        if type(value) is list:
            for item in value:
                validate(item, depth + 1)
            return
        if type(value) is dict and all(type(key) is str for key in value):
            for item in value.values():
                validate(item, depth + 1)
            return
        raise ValueError('PayHot service snapshot must contain exact JSON values')

    validate(snapshot)
    return json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


@dataclass(frozen=True, slots=True)
class PayHotRecurrentTerms:
    amount_kopeks: int
    period_days: int
    tariff_id: int | None
    device_limit: int
    payment_method: str
    service_snapshot: InitVar[dict]
    currency: str = 'RUB'
    is_daily: bool = False
    service_snapshot_json: str = field(init=False)

    def __post_init__(self, service_snapshot):
        _integer(self.amount_kopeks, minimum=1, maximum=MAX_AMOUNT_KOPEKS)
        _integer(self.period_days, minimum=1, maximum=MAX_PERIOD_DAYS)
        _integer(self.device_limit, minimum=0)
        if self.tariff_id is not None:
            _integer(self.tariff_id, minimum=1)
        if self.currency != 'RUB' or self.payment_method not in ('card', 'sbp'):
            raise ValueError('PayHot recurring requires RUB card or SBP')
        if type(self.is_daily) is not bool or (self.is_daily and self.period_days != 1):
            raise ValueError('PayHot daily recurring requires exactly one day')
        object.__setattr__(self, 'service_snapshot_json', _canonical_snapshot(service_snapshot))


def consent_terms_match(agreed: PayHotRecurrentTerms, fresh: PayHotRecurrentTerms) -> bool:
    """No silent repricing or entitlement change under existing consent."""
    return type(agreed) is PayHotRecurrentTerms and type(fresh) is PayHotRecurrentTerms and agreed == fresh


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('PayHot scheduling requires an aware datetime')
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise ValueError('PayHot scheduling timestamp outside datetime range') from exc


def next_charge_due_at(paid_until: datetime) -> datetime:
    """Charge at the end of paid access, never at an inferred provider interval."""
    return _utc(paid_until)


def renewed_paid_until(paid_until: datetime, *, accepted_at: datetime, terms: PayHotRecurrentTerms) -> datetime:
    """Preserve remaining time; expired access starts anew at accepted_at.

    Daily charges mirror update_daily_charge_time: ensure at least one day
    from acceptance, without accumulating another day onto existing time.
    accepted_at is the application acceptance clock, not an untrusted webhook
    timestamp. Callers must persist the result once per unique provider charge.
    """
    if type(terms) is not PayHotRecurrentTerms:
        raise ValueError('Validated PayHot recurring terms required')
    paid_end, accepted = _utc(paid_until), _utc(accepted_at)
    try:
        if terms.is_daily:
            return max(paid_end, accepted + timedelta(days=1))
        return max(paid_end, accepted) + timedelta(days=terms.period_days)
    except OverflowError as exc:
        raise ValueError('PayHot renewal exceeds supported datetime range') from exc
