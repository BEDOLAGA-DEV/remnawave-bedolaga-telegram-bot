import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import date
from types import SimpleNamespace

from app.cabinet.routes.admin_landings import get_landing_stats
from app.database.models import LandingPage, TransactionType


@pytest.mark.asyncio
async def test_get_landing_stats_renewals_and_revenue() -> None:
    # 1. Mock landing page
    landing_mock = LandingPage(
        id=42,
        slug="test-landing",
        title="Test Landing",
        subtitle="Subtitle",
        is_active=True,
    )

    # 2. Setup mock DB execute returns
    db = AsyncMock()

    # Mock the individual result objects returned by db.execute
    def mock_execute(query, *args, **kwargs):
        query_str = str(query).lower()
        result = MagicMock()

        if "total_created" in query_str:
            # Summary stats query
            row = MagicMock()
            row.total_created = 10
            row.total_successful = 8
            row.total_revenue_kopeks = 800000  # 8000.00
            row.total_gifts = 1
            row.total_gifts_claimed = 1
            result.one = MagicMock(return_value=row)
            return result

        elif "transactions" in query_str and "sum" in query_str and "group by" not in query_str:
            # Total renewals revenue query
            result.scalar = MagicMock(return_value=500000)  # 5000.00
            return result

        elif "saved_payment_methods" in query_str and "1000" in query_str and "not" not in query_str:
            # yoo_trial_query
            result.scalar = MagicMock(return_value=2)
            return result

        elif "saved_payment_methods" in query_str and "1000" in query_str and "not" in query_str:
            # yoo_regular_query
            result.scalar = MagicMock(return_value=3)
            return result

        elif "antilopay_recurrents" in query_str and "1000" in query_str and "not" not in query_str:
            # anti_trial_query
            result.scalar = MagicMock(return_value=1)
            return result

        elif "antilopay_recurrents" in query_str and "1000" in query_str and "not" in query_str:
            # anti_regular_query
            result.scalar = MagicMock(return_value=1)
            return result

        elif "transactions" in query_str and "date" in query_str:
            # renewals_daily_result (daily renewals counts and revenue)
            row1 = SimpleNamespace(
                day=date(2026, 7, 8),
                count=5,
                revenue_kopeks=500000,
            )
            result.all = MagicMock(return_value=[row1])
            return result

        elif "date" in query_str and "created" in query_str and "transactions" not in query_str:
            # created_result (daily created counts)
            row1 = SimpleNamespace(day=date(2026, 7, 8), created=3)
            result.all = MagicMock(return_value=[row1])
            return result

        elif "date" in query_str and "transactions" not in query_str and "created" not in query_str:
            # daily_result (initial purchases daily stats)
            row1 = SimpleNamespace(
                day=date(2026, 7, 8),
                purchases=2,
                revenue_kopeks=200000,
                gifts=0,
                trials=0,
                regular=2,
            )
            result.all = MagicMock(return_value=[row1])
            return result

        elif "transactions" in query_str and "tariff_id" in query_str:
            # renewals_tariff_result
            row1 = SimpleNamespace(tariff_id=1, revenue_kopeks=500000)
            result.all = MagicMock(return_value=[row1])
            return result

        elif "tariff" in query_str and "transactions" not in query_str:
            # tariff_result (initial tariff stats)
            row1 = SimpleNamespace(tariff_id=1, tariff_name="Premium", purchases=3, revenue_kopeks=300000)
            result.all = MagicMock(return_value=[row1])
            return result

        elif "payment_method" in query_str:
            # pm_result
            row1 = SimpleNamespace(method="yookassa", purchases=5, revenue_kopeks=500000)
            result.all = MagicMock(return_value=[row1])
            return result

        elif "referrer" in query_str:
            # source_result
            row1 = SimpleNamespace(referrer="https://google.com", purchases=4)
            result.all = MagicMock(return_value=[row1])
            return result

        elif "distinct" in query_str and "transactions" in query_str:
            # renewals_query (total renewals unique user count)
            result.scalar = MagicMock(return_value=4)
            return result

        return result

    db.execute.side_effect = mock_execute

    # 3. Call get_landing_stats using patches to bypass get_landing_by_id
    with patch("app.cabinet.routes.admin_landings.get_landing_by_id", AsyncMock(return_value=landing_mock)):
        response = await get_landing_stats(landing_id=42, admin=MagicMock(), db=db)

    # 4. Assert totals are correct and include renewal revenue
    # Initial revenue is 800,000 kopeks. Total renewals revenue is 500,000 kopeks.
    # Total revenue must be 1,300,000 kopeks (13,000.00 Rubles).
    assert response.total_revenue_kopeks == 1300000
    assert response.avg_purchase_kopeks == 1300000 // 8  # total_successful is 8
    assert response.renewals_count == 4

    # 5. Assert tariff stats include renewals revenue
    # Initial is 300,000 + renewal is 500,000 = 800,000
    assert len(response.tariff_stats) == 1
    assert response.tariff_stats[0].revenue_kopeks == 800000
    assert response.tariff_stats[0].tariff_name == "Premium"

    # 6. Assert daily stats includes renewals revenue and count
    # For date 2026-07-08:
    # Initial purchases = 2, initial revenue = 200,000
    # Renewals count = 5, renewal revenue = 500,000
    # Expected daily renewals = 5
    # Expected daily revenue = 700,000
    day_stat = next((s for s in response.daily_stats if s.date == "2026-07-08"), None)
    assert day_stat is not None
    assert day_stat.purchases == 2
    assert day_stat.renewals == 5
    assert day_stat.revenue_kopeks == 700000
