"""Existing bot FAQ stays editable without migrating IDs or language settings."""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.cabinet.dependencies import get_cabinet_db
from app.cabinet.routes import admin_info_pages, info, info_pages
from app.database.models import FaqPage, FaqSetting, InfoPage
from app.services.faq_service import FaqService
from tests.fixtures.postgres_db import postgres_session
from tests.fixtures.sqlite_memory import memory_session


async def test_legacy_faq_list_edit_keeps_public_and_bot_content_in_sync(monkeypatch):
    async with memory_session(monkeypatch, [FaqPage.__table__, FaqSetting.__table__, InfoPage.__table__]) as db:
        await check_legacy_faq(db)


@pytest.mark.postgres
async def test_legacy_faq_edit_postgres(postgres_database):
    async with postgres_session(postgres_database, [FaqPage.__table__, FaqSetting.__table__, InfoPage.__table__]) as db:
        await check_legacy_faq(db)


async def check_legacy_faq(db):
    page = await FaqService.create_page(
        db, language='ru', title='Original', content='<p>Original answer</p>', display_order=7
    )
    hidden = await FaqService.create_page(db, language='en', title='Hidden', content='Answer', is_active=False)
    await FaqService.set_enabled(db, 'en', False)
    app = FastAPI()
    app.include_router(admin_info_pages.router, prefix='/cabinet')
    app.include_router(info.router, prefix='/cabinet')
    app.include_router(info_pages.router, prefix='/cabinet')
    app.dependency_overrides[get_cabinet_db] = lambda: db
    for route in admin_info_pages.router.routes:
        for dep in route.dependant.dependencies:
            if dep.name == 'admin':
                app.dependency_overrides[dep.call] = lambda: SimpleNamespace(id=1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://cabinet.example') as client:
        result = await client.get('/cabinet/admin/info-pages/legacy-faq')
        assert result.status_code == 200
        assert {p['id'] for p in result.json()} == {page.id, hidden.id}
        edited = await client.put(
            f'/cabinet/admin/info-pages/legacy-faq/{page.id}',
            json={'title': 'Updated', 'content': '<p>Updated answer</p>', 'is_active': True, 'display_order': 7},
        )
        assert edited.status_code == 200
        public = await client.get('/cabinet/info/faq?language=ru')
        assert public.status_code == 200
        assert public.json()[0]['content'] == '<p>Updated answer</p>'
        bot = await FaqService.get_pages(db, 'ru')
        assert bot[0].id == page.id and bot[0].title == 'Updated'
        assert bot[0].display_order == 7 and bot[0].language == 'ru'
        assert (await FaqService.get_setting(db, 'en', fallback=False)).is_enabled is False
        assert hidden.is_active is False
        assert (await client.get('/cabinet/admin/info-pages?page_type=faq')).json() == []
        modern = await client.post(
            '/cabinet/admin/info-pages',
            json={
                'slug': 'modern-faq',
                'page_type': 'faq',
                'title': {'ru': 'Modern FAQ'},
                'content': {'ru': '[{"q":"Question","a":"Answer"}]'},
                'replaces_tab': 'faq',
            },
        )
        assert modern.status_code == 201
        modern_id = modern.json()['id']
        listed = await client.get('/cabinet/admin/info-pages?page_type=faq')
        assert [p['id'] for p in listed.json()] == [modern_id]
        replacement = await client.get('/cabinet/info-pages/tab-replacements')
        assert replacement.json()['faq'] == 'modern-faq'
        modern_public = await client.get('/cabinet/info-pages/modern-faq')
        assert modern_public.json()['content'] == modern.json()['content']
        assert modern_id == page.id  # Separate namespaces may have the same numeric ID.
        await client.put(
            f'/cabinet/admin/info-pages/legacy-faq/{page.id}',
            json={
                'title': 'Legacy only',
                'content': 'Legacy answer',
                'is_active': True,
                'display_order': 7,
            },
        )
        assert (await client.get(f'/cabinet/admin/info-pages/{modern_id}')).json()['title'] == {'ru': 'Modern FAQ'}

        missing = await client.put(
            '/cabinet/admin/info-pages/legacy-faq/999999',
            json={'title': 'X', 'content': 'Y', 'is_active': True, 'display_order': 0},
        )
        assert missing.status_code == 404


@pytest.mark.parametrize(
    ('method', 'path', 'permission'),
    [('GET', '/legacy-faq', 'info_pages:read'), ('PUT', '/legacy-faq/7', 'info_pages:edit')],
)
async def test_legacy_faq_requires_server_permission(monkeypatch, method, path, permission):
    from unittest.mock import AsyncMock

    from app.cabinet.dependencies import get_current_cabinet_user
    from app.services.permission_service import PermissionService

    check = AsyncMock(return_value=(False, 'not allowed'))
    monkeypatch.setattr(PermissionService, 'check_permission', check)
    monkeypatch.setattr(PermissionService, 'log_action', AsyncMock())
    app = FastAPI()
    app.include_router(admin_info_pages.router)
    app.dependency_overrides[get_cabinet_db] = lambda: AsyncMock()
    app.dependency_overrides[get_current_cabinet_user] = lambda: SimpleNamespace(id=9)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://cabinet.example') as client:
        response = await client.request(
            method,
            '/admin/info-pages' + path,
            json={
                'title': 'Question',
                'content': 'Answer',
                'is_active': True,
                'display_order': 0,
            },
        )
    assert response.status_code == 403
    assert check.await_args.args[2] == permission
