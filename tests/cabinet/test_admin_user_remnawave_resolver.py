"""Contract tests for exact subscription-level Remnawave resolution.

Remnawave 3.0.0 идентифицирует панельного пользователя числовым ``id``, поэтому
резолвер принимает либо число (``subscriptions.remnawave_id``), либо ``shortUuid``
(``subscriptions.remnawave_short_uuid``) — вторую строку, которую видно в панели.
Гарантия та же, что и до миграции: только точное совпадение по подписке, без
эвристик по пользовательским полям.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_users


PANEL_USER_ID = 4242
SHORT_UUID = 'aBcDeF0123456789'


def _resolver():
    resolver = getattr(admin_users, 'get_user_by_remnawave_identifier', None)
    assert resolver is not None, 'The exact subscription resolver route must exist'
    return resolver


def _db_with_matches(*matches):
    result = MagicMock()
    result.scalars.return_value.all.return_value = list(matches)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _subscription(
    *,
    subscription_id: int,
    user_id: int,
    remnawave_id: int | None = PANEL_USER_ID,
    remnawave_short_uuid: str | None = None,
):
    return SimpleNamespace(
        id=subscription_id,
        user_id=user_id,
        remnawave_id=remnawave_id,
        remnawave_short_uuid=remnawave_short_uuid,
    )


def test_resolver_route_is_registered_before_user_id_route(registered_paths) -> None:
    assert 'GET' in registered_paths.get('/cabinet/admin/users/by-remnawave/{remnawave_identifier}', set())


def test_resolver_requires_users_read_permission() -> None:
    resolver = _resolver()
    route = next(route for route in admin_users.router.routes if route.endpoint is resolver)
    permission_dependency = route.dependant.dependencies[0].call
    closure_values = [cell.cell_contents for cell in permission_dependency.__closure__ or ()]
    assert ('users:read',) in closure_values


@pytest.mark.parametrize(
    ('subscription_id', 'user_id'),
    [(101, 7), (202, 7)],
    ids=['primary-subscription', 'non-primary-subscription'],
)
async def test_resolver_returns_the_exact_matching_subscription(subscription_id: int, user_id: int) -> None:
    """Would fail if the resolver returned a user-level or primary subscription ID."""
    subscription = _subscription(subscription_id=subscription_id, user_id=user_id)
    db = _db_with_matches(subscription)

    response = await _resolver()(str(PANEL_USER_ID), admin=SimpleNamespace(id=1), db=db)

    assert response.model_dump() == {
        'user_id': user_id,
        'subscription_id': subscription_id,
        'matched_remnawave_id': PANEL_USER_ID,
    }
    query = str(db.execute.await_args.args[0])
    assert 'subscriptions.remnawave_id' in query


async def test_resolver_accepts_a_short_uuid_for_subscriptions_without_a_panel_id() -> None:
    """Would fail if the resolver only understood numeric panel ids.

    shortUuid пережил 3.0.0 и остаётся единственным идентификатором у подписок,
    которым числовой id ещё не проставлен.
    """
    subscription = _subscription(
        subscription_id=303,
        user_id=9,
        remnawave_id=None,
        remnawave_short_uuid=SHORT_UUID,
    )
    db = _db_with_matches(subscription)

    response = await _resolver()(SHORT_UUID, admin=SimpleNamespace(id=1), db=db)

    assert response.model_dump() == {
        'user_id': 9,
        'subscription_id': 303,
        'matched_remnawave_id': None,
    }
    query = str(db.execute.await_args.args[0])
    assert 'subscriptions.remnawave_short_uuid' in query


@pytest.mark.parametrize(
    'path_value',
    [
        None,
        '',
        '   ',
        '0',
        str(2**63),
    ],
    ids=['null', 'empty', 'blank', 'zero', 'out-of-range'],
)
async def test_resolver_rejects_unusable_identifiers_without_any_lookup(path_value) -> None:
    """Would fail if garbage input were guessed from user data or hit the database."""
    db = _db_with_matches()

    with pytest.raises(HTTPException) as exc:
        await _resolver()(path_value, admin=SimpleNamespace(id=1), db=db)

    assert exc.value.status_code == 400
    db.execute.assert_not_awaited()


async def test_resolver_rejects_an_identifier_present_only_on_the_legacy_user_field() -> None:
    """Would fail if the route reused legacy user-level resolution."""
    db = _db_with_matches()

    with pytest.raises(HTTPException) as exc:
        await _resolver()(str(PANEL_USER_ID), admin=SimpleNamespace(id=1), db=db)

    assert exc.value.status_code == 404
    query = str(db.execute.await_args.args[0])
    assert 'subscriptions.remnawave_id' in query
    assert 'users.remnawave_id' not in query


async def test_resolver_treats_a_physically_absent_deleted_subscription_as_not_found() -> None:
    """Would fail if absent/deleted records were accidentally resolved."""
    db = _db_with_matches()

    with pytest.raises(HTTPException) as exc:
        await _resolver()(str(PANEL_USER_ID), admin=SimpleNamespace(id=1), db=db)

    assert exc.value.status_code == 404


@pytest.mark.parametrize(
    ('identifier', 'matches'),
    [
        (
            str(PANEL_USER_ID),
            (
                _subscription(subscription_id=101, user_id=7),
                _subscription(subscription_id=202, user_id=8),
            ),
        ),
        (
            SHORT_UUID,
            (
                _subscription(subscription_id=101, user_id=7, remnawave_id=None, remnawave_short_uuid=SHORT_UUID),
                _subscription(subscription_id=202, user_id=8, remnawave_id=None, remnawave_short_uuid=SHORT_UUID),
            ),
        ),
    ],
    ids=['panel-id', 'short-uuid'],
)
async def test_resolver_rejects_duplicate_subscription_mappings_as_a_conflict(identifier, matches) -> None:
    """Would fail if corrupted mappings silently selected one subscription."""
    db = _db_with_matches(*matches)

    with pytest.raises(HTTPException) as exc:
        await _resolver()(identifier, admin=SimpleNamespace(id=1), db=db)

    assert exc.value.status_code == 409
