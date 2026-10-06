"""Read-only payment method names for API-key integrations."""

from fastapi import APIRouter, Depends, HTTPException, Query, Security, status
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.models import Transaction
from app.services.payment_method_config_service import get_enabled_methods_for_user

from ..dependencies import get_db_session, require_api_token


router = APIRouter()


@router.get('', response_model=list[str])
async def list_payment_methods(
    _: object = Security(require_api_token),
    user_id: int | None = Query(default=None, gt=0),
    db: AsyncSession = Depends(get_db_session),
) -> list[str]:
    """Return current payment method names without amounts, IDs or payment links.

    With user_id, apply the cabinet's user-type, first-top-up and promo-group
    filters. Without user_id, return the globally enabled provider catalog.
    Available sub-options are expanded as 'option name (provider name)'.
    """
    user = None
    is_first_topup = None
    if user_id is not None:
        user = await get_user_by_id(db, user_id)
        if user is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, 'User not found')
        has_completed_topup = await db.scalar(
            select(
                exists().where(
                    Transaction.user_id == user.id,
                    Transaction.type == 'deposit',
                    Transaction.is_completed == True,
                )
            )
        )
        is_first_topup = not has_completed_topup

    methods = await get_enabled_methods_for_user(db, user=user, is_first_topup=is_first_topup)
    names = []
    for method in methods:
        options = method.get('options')
        if options:
            for option in options:
                name = option['name']
                if method['id'] == 'platega' and option['id'].isdigit():
                    definition = settings.get_platega_method_definitions().get(int(option['id']), {})
                    name = definition.get('name') or name
                names.append(f'{name} ({method["name"]})')
        else:
            names.append(method['name'])
    return names
