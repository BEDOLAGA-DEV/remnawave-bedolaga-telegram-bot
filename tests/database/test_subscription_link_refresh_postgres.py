"""#3105: encrypted links must not survive a change of their source URL."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.database.models import Base, Subscription, User
from app.services.panel_sync import project_onto_subscription, read_panel_user
from app.services.panel_sync.writer import _record_identity
from app.services.remnawave_service import RemnaWaveService
from app.services.remnawave_webhook_service import RemnaWaveWebhookService
from app.utils.subscription_utils import get_display_subscription_link
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres
OLD_URL = 'https://old.example/sub/test'
NEW_URL = 'https://new.example/sub/test'
OLD_CRYPTO = 'happ://crypt5/old-test-payload'
NEW_CRYPTO = 'happ://crypt5/new-test-payload'


@pytest.mark.parametrize('flow', ['projection', 'writer', 'import', 'webhook'])
@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('url', [None, OLD_URL, NEW_URL])
@pytest.mark.parametrize('crypto', [None, '', NEW_CRYPTO])
async def test_url_refresh_invalidates_only_its_stale_crypto(postgres_database, monkeypatch, flow, multi, url, crypto):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'happ_cryptolink')
    now = datetime.now(UTC)
    end = now + timedelta(days=30)
    account = SimpleNamespace(
        id=9001,
        short_uuid='test-short',
        username='test-account',
        telegram_id=101,
        email=None,
        status=SimpleNamespace(value='ACTIVE'),
        expire_at=end,
        used_traffic_bytes=0,
        traffic_limit_bytes=100 * 1024**3,
        hwid_device_limit=2,
        active_internal_squads=[],
        subscription_url=url,
        happ_crypto_link=crypto,
        tag=None,
    )
    api = SimpleNamespace(
        get_all_users_page_stream=AsyncMock(
            return_value={
                'users': [account],
                'hasMore': False,
                'nextCursor': None,
            }
        )
    )

    @asynccontextmanager
    async def client():
        yield api

    service = RemnaWaveService()
    monkeypatch.setattr(service, 'get_api_client', client)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        user = User(id=1, telegram_id=101, status='active', language='ru', remnawave_id=9001)
        db.add_all([user, User(id=2, telegram_id=202, status='active', language='ru', remnawave_id=9002)])
        for sub_id, owner, panel_id in [(10, 1, 9001), (20, 2, 9002)]:
            db.add(
                Subscription(
                    id=sub_id,
                    user_id=owner,
                    remnawave_id=panel_id,
                    remnawave_short_id=f'test-{sub_id}',
                    remnawave_short_uuid='test-short' if sub_id == 10 else 'other-short',
                    status='active',
                    is_trial=False,
                    end_date=end,
                    traffic_limit_gb=100,
                    device_limit=2,
                    connected_squads=[],
                    subscription_url=OLD_URL,
                    subscription_crypto_link=OLD_CRYPTO,
                )
            )
        await db.commit()
        sub = await db.get(Subscription, 10)
        for _ in range(2):
            if flow == 'projection':
                project_onto_subscription(sub, read_panel_user(account), now=now)
                await db.commit()
            elif flow == 'writer':
                await _record_identity(db, user, sub, account, multi_tariff=multi)
                await db.commit()
            elif flow == 'webhook':
                await RemnaWaveWebhookService(AsyncMock())._handle_user_modified(
                    db, user, sub, {'subscriptionUrl': url, 'subscriptionCryptoLink': crypto}
                )
            else:
                stats = await service.sync_users_from_panel(db, 'all')
                assert stats['errors'] == 0
            await db.refresh(sub)
            assert sub.subscription_url == (url or OLD_URL)
            if crypto:
                assert sub.subscription_crypto_link == NEW_CRYPTO
            elif url == NEW_URL:
                assert not sub.subscription_crypto_link
            elif flow == 'writer' and crypto == '':
                assert sub.subscription_crypto_link == ''
            else:
                assert sub.subscription_crypto_link == OLD_CRYPTO
            assert get_display_subscription_link(sub) == (sub.subscription_crypto_link or sub.subscription_url)
            other = await db.get(Subscription, 20, populate_existing=True)
            assert (other.subscription_url, other.subscription_crypto_link) == (OLD_URL, OLD_CRYPTO)
        assert sub.traffic_limit_gb == 100
        assert sub.end_date == end


@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('encryption_available', [False, True])
async def test_cabinet_regenerates_crypto_for_the_changed_url(
    postgres_database, monkeypatch, multi, encryption_available
):
    from app.cabinet.routes.subscription_modules import status

    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'happ_cryptolink')
    monkeypatch.setattr(status, '_load_app_config_async', AsyncMock(return_value={'platforms': {}}))

    @asynccontextmanager
    async def client(self):
        async def encrypt(url):
            # Distinct output proves which URL the real route requested.
            if not encryption_available:
                return None
            return NEW_CRYPTO if url == NEW_URL else OLD_CRYPTO

        yield SimpleNamespace(encrypt_happ_crypto_link=encrypt)

    monkeypatch.setattr(RemnaWaveService, 'get_api_client', client)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        user = User(id=1, telegram_id=101, status='active', language='ru')
        sub = Subscription(
            id=10,
            user_id=1,
            status='active',
            is_trial=False,
            end_date=datetime.now(UTC) + timedelta(days=30),
            subscription_url=OLD_URL,
            subscription_crypto_link=OLD_CRYPTO,
        )
        db.add_all([user, sub])
        await db.commit()
        project_onto_subscription(sub, read_panel_user({'subscriptionUrl': NEW_URL}))
        await db.commit()
        config = await status.get_app_config(user, db, 10)
        connection = await status.get_connection_link(user, db, 10)
        await db.refresh(sub)
        expected = NEW_CRYPTO if encryption_available else None
        assert config['subscriptionCryptoLink'] == expected
        assert connection['happ_crypto_link'] == expected
        assert sub.subscription_crypto_link == expected
        assert connection['display_link'] == (expected or NEW_URL)
