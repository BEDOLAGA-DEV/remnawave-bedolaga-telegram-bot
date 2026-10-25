"""Uploaded news media must be reachable through the cabinet API proxy."""

from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image

from app.cabinet.routes import admin_news_media
from app.webserver.unified_app import _mount_uploads_static


async def test_upload_is_available_via_api_proxy_and_legacy_path(tmp_path, monkeypatch):
    monkeypatch.setattr(type(admin_news_media.settings), 'get_media_upload_path', lambda self: tmp_path)
    app = FastAPI()
    app.include_router(admin_news_media.router, prefix='/cabinet')
    for route in admin_news_media.router.routes:
        for dep in route.dependant.dependencies:
            if dep.name == 'admin':
                app.dependency_overrides[dep.call] = lambda: SimpleNamespace(id=1)
    _mount_uploads_static(app)
    stream = BytesIO()
    Image.new('RGB', (24, 16), '#227799').save(stream, format='PNG')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://backend.example') as client:
        response = await client.post(
            '/cabinet/admin/news/media/upload', files={'file': ('image.png', stream.getvalue(), 'image/png')}
        )
        assert response.status_code == 201
        uploaded = response.json()
        # Browser resolves the API path using its configured /api base URL.
        original_path = httpx.URL(uploaded['url']).path
        assert original_path.startswith('/uploads/')
        path = '/cabinet' + original_path
        result = await client.get(path)
        assert result.status_code == 200
        assert result.headers['content-type'] == 'image/jpeg'
        image = Image.open(BytesIO(result.content))
        assert image.size == (24, 16)
        legacy = await client.get(path.removeprefix('/cabinet'))
        assert legacy.content == result.content
        missing = await client.get('/cabinet/uploads/images/missing.jpg')
        assert missing.status_code == 404
        traversal = await client.get('/cabinet/uploads/images/%2e%2e/%2e%2e/private.txt')
        assert traversal.status_code == 404


@pytest.mark.postgres
async def test_news_upload_save_reload_through_api_proxy(postgres_database, tmp_path, monkeypatch):
    from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
    from app.cabinet.routes import admin_news, news
    from app.database.models import Base, User
    from tests.fixtures.postgres_db import postgres_session

    monkeypatch.setattr(type(admin_news_media.settings), 'get_media_upload_path', lambda self: tmp_path)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        user = User(id=7, telegram_id=7007, first_name='Masked', language='ru', status='active')
        db.add(user)
        await db.commit()
        app = FastAPI()
        for router in [admin_news_media.router, admin_news.router, news.router]:
            app.include_router(router, prefix='/cabinet')
            for route in router.routes:
                for dep in route.dependant.dependencies:
                    if dep.name == 'admin':
                        app.dependency_overrides[dep.call] = lambda: user
        app.dependency_overrides[get_current_cabinet_user] = lambda: user
        app.dependency_overrides[get_cabinet_db] = lambda: db
        _mount_uploads_static(app)
        proxy = FastAPI()
        proxy.mount('/api', app)
        image_bytes = BytesIO()
        Image.new('RGB', (24, 16), '#227799').save(image_bytes, format='PNG')
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy), base_url='http://cabinet.example'
        ) as client:
            uploaded = await client.post(
                '/api/cabinet/admin/news/media/upload',
                files={'file': ('masked.png', image_bytes.getvalue(), 'image/png')},
            )
            assert uploaded.status_code == 201
            # The frontend resolves the API path with apiClient.getUri before saving.
            media_url = 'http://cabinet.example/api/cabinet' + httpx.URL(uploaded.json()['url']).path
            created = await client.post(
                '/api/cabinet/admin/news',
                json={
                    'title': 'Masked article',
                    'slug': 'masked-article',
                    'category': 'News',
                    'content': f'<p>Image</p><img src="{media_url}">',
                    'featured_image_url': media_url,
                    'is_published': True,
                },
            )
            assert created.status_code == 201, created.text
            db.expunge_all()
            reloaded = await client.get('/api/cabinet/news/masked-article')
            assert reloaded.status_code == 200, reloaded.text
            assert reloaded.json()['featured_image_url'] == media_url
            assert media_url in reloaded.json()['content']
            fetched = await client.get(reloaded.json()['featured_image_url'])
            assert fetched.status_code == 200
            assert fetched.headers['content-type'] == 'image/jpeg'
            assert Image.open(BytesIO(fetched.content)).size == (24, 16)
