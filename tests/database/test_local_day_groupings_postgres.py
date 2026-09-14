"""Дым: каждая группировка по локальному дню исполняется на настоящем PostgreSQL (#3136).

Прод 2026-09-12: статистика продаж в кабинете падала с «column must appear in
the GROUP BY clause» — CI это не поймал, потому что единственный тест на
PostgreSQL переиспользовал одно выражение, а боевые запросы строят его
отдельно для SELECT и GROUP BY. Здесь вызывается каждый потребитель
``local_date_expr`` (список — из графа вызовов) на полной схеме: пустые
таблицы достаточно, чтобы PostgreSQL разобрал и выполнил запросы.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.cabinet.routes import admin_sales_stats
from app.services.menu_layout.stats_service import MenuLayoutStatsService
from app.services.partner_stats_service import PartnerStatsService
from tests.fixtures.local_day import reset_local_timezone_cache, use_timezone  # noqa: F401
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres

ADMIN = SimpleNamespace(id=1, username='admin')

SALES_ROUTES = [
    admin_sales_stats.get_sales_summary,
    admin_sales_stats.get_trials_stats,
    admin_sales_stats.get_sales_stats,
    admin_sales_stats.get_renewals_stats,
    admin_sales_stats.get_addons_stats,
    admin_sales_stats.get_deposits_stats,
    admin_sales_stats.get_payment_health,
]


@pytest.mark.asyncio
@pytest.mark.parametrize('route', SALES_ROUTES, ids=lambda route: route.__name__)
async def test_sales_stats_route_groups_by_local_day(route, postgres_database, monkeypatch, reset_local_timezone_cache):
    use_timezone(monkeypatch, 'Europe/Moscow')

    async with postgres_session(postgres_database) as db:
        await route(days=30, start_date=None, end_date=None, admin=ADMIN, db=db)


@pytest.mark.asyncio
async def test_sales_stats_custom_period_groups_by_local_day(
    postgres_database, monkeypatch, reset_local_timezone_cache
):
    use_timezone(monkeypatch, 'Europe/Moscow')

    async with postgres_session(postgres_database) as db:
        await admin_sales_stats.get_sales_stats(
            days=None, start_date='2026-09-01', end_date='2026-09-10', admin=ADMIN, db=db
        )


@pytest.mark.asyncio
async def test_partner_and_menu_stats_group_by_local_day(postgres_database, monkeypatch, reset_local_timezone_cache):
    use_timezone(monkeypatch, 'Europe/Moscow')

    async with postgres_session(postgres_database) as db:
        await PartnerStatsService.get_referrer_daily_stats(db, 1, 30)
        await PartnerStatsService.get_global_daily_stats(db, 30)
        await PartnerStatsService.get_campaign_detailed_stats(db, 1, 1)
        await PartnerStatsService.get_admin_campaign_chart_data(db, 1)
        await MenuLayoutStatsService.get_button_clicks_by_day(db, 'main_menu', 30)


@pytest.mark.asyncio
async def test_landing_stats_group_by_local_day(postgres_database, monkeypatch, reset_local_timezone_cache):
    from app.cabinet.routes.admin_landings import get_landing_stats
    from app.database.models import LandingPage

    use_timezone(monkeypatch, 'Europe/Moscow')

    async with postgres_session(postgres_database, [LandingPage.__table__]) as db:
        landing = LandingPage(slug='smoke')
        db.add(landing)
        await db.commit()

        await get_landing_stats(landing_id=landing.id, admin=ADMIN, db=db)
