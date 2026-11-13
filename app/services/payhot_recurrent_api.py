"""Explicit production recurring operations; callers own consent and durable idempotency."""

import re
import secrets
import string

from app.services.payhot_service import PayHotAPIError, PayHotConfig, PayHotService, _identifier, _minor


_REFERENCE = re.compile(
    r'^[A-Za-z][A-Za-z0-9]{0,31}[-_:](?!(?:[^0-9]*[0-9]){12})(?!.*[A-Za-z0-9]{20})[A-Za-z0-9][A-Za-z0-9._:-]{0,94}$'
)
_METHODS = {'card', 'sbp'}
_REASONS = {'merchant_request', 'consent_withdrawn', 'instrument_replaced', 'suspected_compromise'}


def validate_opaque_reference(value: str) -> str:
    if not isinstance(value, str) or not 3 <= len(value) <= 128 or not _REFERENCE.fullmatch(value):
        raise ValueError('Invalid PayHot recurring reference')
    return value


def generate_opaque_reference(prefix: str = 'renew') -> str:
    if not isinstance(prefix, str) or not re.fullmatch('[A-Za-z]{1,16}', prefix):
        raise ValueError('Invalid PayHot recurring reference prefix')
    # 32 random letters (~150 bits), split so neither PAN-like digits nor a long token can match.
    random_part = '.'.join(''.join(secrets.choice(string.ascii_lowercase) for _ in range(8)) for _ in range(4))
    return validate_opaque_reference(f'{prefix}-{random_part}')


def _method_currency(value):
    if (
        not isinstance(value, dict)
        or value.get('currency') != 'RUB'
        or not isinstance(value.get('payment_method'), str)
        or value['payment_method'] not in _METHODS
    ):
        raise ValueError('PayHot recurring requires RUB card or SBP')


def validate_charge_payload(value):
    _method_currency(value)
    validate_opaque_reference(value.get('merchant_recurring_charge_reference'))
    _identifier(value.get('mandate_id'))
    if not _minor(value.get('amount_minor')):
        raise ValueError('Invalid PayHot recurring principal')


def _charge_response(result):
    value = result['recurring_charge']
    validate_charge_payload(value)
    _identifier(value['id'])
    if value.get('status') not in {'processing', 'succeeded', 'failed', 'cancelled'}:
        raise ValueError('Invalid PayHot charge status')


def _mandate_response(result):
    value = result['mandate']
    _method_currency(value)
    _identifier(value['id'])
    validate_opaque_reference(value.get('merchant_mandate_reference'))
    if value.get('status') not in {'active', 'processing', 'inactive', 'revoked'}:
        raise ValueError('Invalid PayHot mandate status')


def _revoked_response(result):
    _mandate_response(result)
    if result['mandate']['status'] != 'revoked' or type(result.get('already_revoked')) is not bool:
        raise ValueError('PayHot mandate revocation not confirmed')


def _matching(validator, envelope, expected):
    def validate(result):
        validator(result)
        if any(result[envelope].get(key) != value for key, value in expected.items()):
            raise ValueError('PayHot response does not match requested operation')

    return validate


class PayHotRecurrentAPI:
    def __init__(self):
        self._transport = PayHotService()

    async def create_enrollment_payment(self, payload: dict, idempotency_key: str, *, config: PayHotConfig):
        _method_currency(payload)
        if payload.get('recurring_enrollment') != {} or type(payload.get('recurring_enrollment')) is not dict:
            raise ValueError('Explicit PayHot enrollment consent required')
        ordinary = {key: value for key, value in payload.items() if key != 'recurring_enrollment'}
        self._transport._validate_payload(ordinary)
        customer = payload.get('customer')
        if (
            not isinstance(customer, dict)
            or not isinstance(customer.get('id'), str)
            or not 1 <= len(customer['id'].encode('utf-8')) <= 50
        ):
            raise ValueError('PayHot enrollment requires a stable customer identifier up to 50 bytes')
        result = await self._transport._request(
            'POST',
            self._transport._prefix(config),
            config=config,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        if any(
            result['payment'].get(key) != payload[key]
            for key in ('merchant_order_reference', 'amount_minor', 'payment_method', 'currency')
        ):
            raise PayHotAPIError(201)
        return result

    async def get_mandate(self, mandate_id: str, *, config: PayHotConfig):
        self._transport._prefix(config)
        return await self._transport._request(
            'GET',
            f'/mandates/{_identifier(mandate_id)}',
            config=config,
            response_validator=_matching(_mandate_response, 'mandate', {'id': mandate_id}),
            success_statuses=(200,),
        )

    async def revoke_mandate(self, mandate_id: str, reason: str, *, config: PayHotConfig):
        self._transport._prefix(config)
        if not isinstance(reason, str) or reason not in _REASONS:
            raise ValueError('Invalid PayHot revocation reason')
        return await self._transport._request(
            'POST',
            f'/mandates/{_identifier(mandate_id)}/revoke',
            config=config,
            payload={'reason': reason},
            response_validator=_matching(_revoked_response, 'mandate', {'id': mandate_id}),
            success_statuses=(200,),
        )

    async def create_recurring_charge(self, payload: dict, idempotency_key: str, *, config: PayHotConfig):
        self._transport._prefix(config)
        validate_charge_payload(payload)
        return await self._transport._request(
            'POST',
            '/recurring-charges',
            config=config,
            payload=payload,
            idempotency_key=idempotency_key,
            response_validator=_matching(
                _charge_response,
                'recurring_charge',
                {
                    key: payload[key]
                    for key in (
                        'merchant_recurring_charge_reference',
                        'mandate_id',
                        'amount_minor',
                        'currency',
                        'payment_method',
                    )
                },
            ),
            success_statuses=(202,),
        )

    async def get_recurring_charge(self, charge_id: str, *, config: PayHotConfig):
        self._transport._prefix(config)
        return await self._transport._request(
            'GET',
            f'/recurring-charges/{_identifier(charge_id)}',
            config=config,
            response_validator=_matching(_charge_response, 'recurring_charge', {'id': charge_id}),
            success_statuses=(200,),
        )
