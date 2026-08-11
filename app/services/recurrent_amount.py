"""Согласование суммы провайдерских рекуррентов с реальной ценой продления.

Проблема: и Platega, и Lava фиксируют сумму списания в момент привязки —
Platega берёт её из ``create_subscription`` (в их API есть только create/get/
cancel, обновления суммы нет), Lava вовсе не принимает сумму, она задана
ПРОДУКТОМ в кабинете провайдера. Поэтому докупка устройств или трафика,
которая меняет цену продления (см. ``PricingEngine.calculate_renewal_price``:
в тарифном режиме на цену влияют доп. устройства, в классическом — ещё и
трафик), не отражается в регулярном списании: подписка продлевается по
устаревшей цене бесконечно.

Изменить сумму у уже созданной привязки нельзя ни у одного провайдера, поэтому
единственный корректный вариант — погасить устаревшую привязку и предложить
подключить автопродление заново: новая привязка создастся уже по актуальной
цене. Молча продолжать списывать старую сумму хуже: расхождение растёт с
каждым продлением и не видно ни пользователю, ни оператору.
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)

# Расхождение в пределах копейки — округление, а не смена цены.
AMOUNT_TOLERANCE_KOPEKS = 1


async def resolve_true_renewal_amount(
    db: AsyncSession,
    subscription,
    charge_days: int,
) -> int | None:
    """Цена продления подписки за ``charge_days`` для сверки с суммой привязки.

    Считается БЕЗ пользователя намеренно: рекуррент не замораживает временные
    промо-скидки (см. ``create_platega_sbp_subscription``), а если учитывать их
    здесь, то истечение промо-оффера выглядело бы как «цена изменилась» и
    гасило бы живую привязку на ровном месте.

    ``None`` — посчитать не удалось (тогда привязку не трогаем).
    """
    try:
        from app.services.pricing_engine import pricing_engine

        pricing = await pricing_engine.calculate_renewal_price(db, subscription, charge_days)
        return int(pricing.final_total)
    except Exception as error:  # pragma: no cover - defensive
        logger.warning(
            'Не удалось рассчитать актуальную цену продления для рекуррента',
            subscription_id=getattr(subscription, 'id', None),
            charge_days=charge_days,
            error=str(error),
        )
        return None


async def sync_recurrent_bindings_after_price_change(db: AsyncSession, subscription_id: int) -> None:
    """Гасит привязки, чья сумма больше не соответствует цене продления.

    Вызывается после докупки устройств/трафика. Best-effort: никогда не бросает
    наружу — докупка уже оплачена и не должна падать из-за рекуррента.
    """
    try:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from app.database.models import Subscription

        # Тариф грузим явно: calculate_renewal_price читает subscription.tariff,
        # а ленивый доступ в async-сессии падает MissingGreenlet.
        result = await db.execute(
            select(Subscription).where(Subscription.id == subscription_id).options(selectinload(Subscription.tariff))
        )
        subscription = result.scalar_one_or_none()
        if subscription is None:
            return

        from app.database.crud import lava_subscription as lava_crud, platega_subscription as platega_crud

        providers = (
            ('platega', await platega_crud.get_active_platega_subscription_by_subscription(db, subscription_id)),
            ('lava', await lava_crud.get_active_lava_subscription_by_subscription(db, subscription_id)),
        )

        for provider, record in providers:
            if record is None:
                continue

            true_amount = await resolve_true_renewal_amount(db, subscription, record.charge_days)
            if true_amount is None or true_amount <= 0:
                continue
            if abs(true_amount - record.amount_kopeks) <= AMOUNT_TOLERANCE_KOPEKS:
                continue

            logger.warning(
                'Цена продления изменилась — гасим устаревшую привязку автопродления',
                provider=provider,
                subscription_id=subscription_id,
                local_id=record.id,
                bound_amount_kopeks=record.amount_kopeks,
                actual_amount_kopeks=true_amount,
            )

            if provider == 'platega':
                from app.services.payment.platega import (
                    _PlategaSbpAgent,
                    cancel_platega_recurring_for_subscription_safe,
                )

                await cancel_platega_recurring_for_subscription_safe(db, subscription_id)
                agent = _PlategaSbpAgent()
            else:
                from app.services.payment.lava import _LavaRecurrentAgent, cancel_lava_recurring_for_subscription_safe

                await cancel_lava_recurring_for_subscription_safe(db, subscription_id)
                agent = _LavaRecurrentAgent()

            # Уведомление best-effort: у модульного агента нет бота, поэтому
            # доедет WS-событие в кабинет; бот-сообщение уйдёт там, где агент
            # несёт bot (полный PaymentService).
            try:
                notify = agent._notify_sbp_recurring if provider == 'platega' else agent._notify_lava_recurring
                await notify(db, record, 'cancelled')
            except Exception as notify_error:  # pragma: no cover - best-effort
                logger.warning(
                    'Не удалось уведомить об отключении автопродления после смены цены',
                    provider=provider,
                    error=str(notify_error),
                )
    except Exception as error:  # pragma: no cover - best-effort
        logger.warning(
            'Не удалось согласовать сумму автопродления после изменения подписки',
            subscription_id=subscription_id,
            error=str(error),
        )
