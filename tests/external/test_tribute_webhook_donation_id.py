"""Ключ идемпотентности Tribute-донатов: per-событие, а не per-ссылка.

У new_donation НЕТ уникального id платежа: donation_request_id — это id
переиспользуемой донат-ссылки, общий для всех платежей через неё (в отличие от
цифровых товаров с purchase_id). Регрессия: после ужесточения дедупа (8b5b8c28)
первый донат навсегда занимал external_id=donation_<request_id>, и все
последующие пополнения любых юзеров молча бракавались как «повторный webhook».

Ключ собирается из donation_request_id + telegram_user_id + amount + created_at:
реплеи одного события несут тот же created_at (дедуп ловит), разные донаты —
разный (начисляются).
"""

from __future__ import annotations

import pytest

from app.external.tribute import TributeService


def _donation_webhook(*, created_at: str, telegram_user_id: int = 111, amount: int = 15000) -> dict:
    return {
        'name': 'new_donation',
        'created_at': created_at,
        'sent_at': '2026-07-27T16:23:23Z',
        'payload': {
            'donation_request_id': 156031,
            'donation_name': 'Пополнение баланса',
            'period': 'once',
            'amount': amount,
            'currency': 'rub',
            'telegram_user_id': telegram_user_id,
            'trb_user_id': 'trb_abc',
        },
    }


@pytest.mark.asyncio
async def test_distinct_donations_via_same_link_get_distinct_payment_ids():
    """Два разных доната через одну ссылку не должны схлопываться в один платёж."""
    service = TributeService()

    first = await service.process_webhook(_donation_webhook(created_at='2026-07-26T10:00:00Z'))
    second = await service.process_webhook(_donation_webhook(created_at='2026-07-27T16:23:22Z'))

    assert first and second
    assert first['payment_id'] != second['payment_id']
    assert first['external_id'] != second['external_id']


@pytest.mark.asyncio
async def test_replayed_webhook_keeps_identical_payment_id():
    """Повторная доставка того же события обязана дать тот же ключ (дедуп реплеев)."""
    service = TributeService()
    webhook = _donation_webhook(created_at='2026-07-27T16:23:22Z')

    first = await service.process_webhook(dict(webhook))
    replay = await service.process_webhook(dict(webhook))

    assert first and replay
    assert first['payment_id'] == replay['payment_id']
    assert first['external_id'] == replay['external_id']


@pytest.mark.asyncio
async def test_same_second_donations_from_different_users_stay_distinct():
    """Одновременные донаты разных юзеров не должны делить ключ идемпотентности."""
    service = TributeService()

    first = await service.process_webhook(_donation_webhook(created_at='2026-07-27T16:23:22Z', telegram_user_id=111))
    second = await service.process_webhook(_donation_webhook(created_at='2026-07-27T16:23:22Z', telegram_user_id=222))

    assert first and second
    assert first['payment_id'] != second['payment_id']
