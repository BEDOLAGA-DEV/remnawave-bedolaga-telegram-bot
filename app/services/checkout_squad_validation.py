"""Reject an unavailable tariff before charging, only with fresh panel evidence."""

import asyncio
from collections.abc import Sequence

import structlog

from app.services.remnawave_service import RemnaWaveService


logger = structlog.get_logger(__name__)
PREFLIGHT_TIMEOUT_SECONDS = 3.0


class CheckoutSquadsUnavailableError(ValueError):
    code = 'tariff_servers_unavailable'
    message = 'Серверы этого тарифа сейчас недоступны. Выберите другой тариф или обратитесь в поддержку.'

    def __init__(self):
        super().__init__(self.message)


async def validate_checkout_squads(squad_uuids: Sequence[str] | None) -> None:
    """Do not charge when every explicitly selected squad has been deleted.

    An empty selection retains its existing "all servers / not configured"
    meaning. Callers with resolved effective squads should supply those. Partial
    survival is handled by panel_sync without rewriting paid local intent here.
    A timeout, malformed catalog or outage is not proof of deletion: existing
    checkout and durable panel retry semantics remain in effect in those cases.
    """
    if not squad_uuids:
        return
    try:
        async with asyncio.timeout(PREFLIGHT_TIMEOUT_SECONDS):
            async with RemnaWaveService().get_api_client() as api:
                catalog = await api.get_internal_squads()
        if not isinstance(catalog, list) or any(
            not isinstance(getattr(squad, 'uuid', None), str) or not squad.uuid for squad in catalog
        ):
            logger.warning('Checkout squad preflight received invalid catalog')
            return
        available = {squad.uuid for squad in catalog}
    except Exception as error:
        logger.warning(
            'Checkout squad preflight unavailable; keeping normal panel retry', error_type=type(error).__name__
        )
        return
    if not available.intersection(squad_uuids):
        raise CheckoutSquadsUnavailableError


async def validate_trial_checkout_squads(db) -> None:
    """Check a configured trial tariff before legacy paths debit its activation.

    These paths resolve the full trial parameters after charging. Read only the
    chosen tariff here; no tariff or an empty assignment keeps the legacy random
    / all-servers semantics. Callers that already resolved squads use the generic
    validator instead.
    """
    from app.config import settings
    from app.database.crud.tariff import get_tariff_by_id, get_trial_tariff

    tariff = await get_trial_tariff(db)
    if tariff is None:
        tariff_id = settings.get_trial_tariff_id()
        if tariff_id > 0:
            tariff = await get_tariff_by_id(db, tariff_id)
    if tariff is not None:
        await validate_checkout_squads(tariff.allowed_squads)
