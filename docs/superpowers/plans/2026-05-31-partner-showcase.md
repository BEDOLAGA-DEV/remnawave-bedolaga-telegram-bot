# Витрина партнёров Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Admin-управляемый список партнёрских ссылок (outbound cross-promo), показ в боте + кабинете, клик через redirect с подсчётом. За env-флагом (дефолт OFF).

**Architecture:** CMS-таблица `PartnerPromo` (mirror `InfoPage`). CRUD + https link-safety + атомарный счётчик кликов. Public redirect-эндпоинт `/partner-promo/{id}/go` инкрементит и 302-редиректит. Бот-меню + cabinet-страница показывают активные карточки, кнопки ведут на `/go`. Admin бэк-CRUD (React-UI = follow-up).

**Tech Stack:** Python 3.12, FastAPI, aiogram, SQLAlchemy async, Alembic; React/TS (nested cabinet repo); pytest.

**Spec:** `docs/superpowers/specs/2026-05-31-partner-showcase-design.md`

**Run tests:** `.venv/Scripts/python.exe -m pytest <path> -v`

---

## File Structure

- `migrations/alembic/versions/0099_create_partner_promos.py` + `app/database/models.py` — table + model (Task 1).
- `app/database/crud/partner_promo.py` — CRUD + link-safety + atomic click (Task 2).
- `app/config.py` — `PARTNER_SHOWCASE_ENABLED` (Task 2).
- `app/webserver/partner_promo.py` + `unified_app.py` — public `/partner-promo/{id}/go` redirect (Task 3).
- `app/cabinet/routes/partner_promo.py` (public list) + `admin_partner_promos.py` (admin CRUD) + registration (Task 4, 5).
- `app/handlers/` + `app/keyboards/inline.py` — bot menu (Task 6).
- nested cabinet repo: `PartnerShowcase.tsx` + api + route + nav (Task 7).

Tests: `tests/services/test_partner_promo.py` (Task 2, 3).

---

## Task 1: migration + model

**Files:**
- Create: `migrations/alembic/versions/0099_create_partner_promos.py`
- Modify: `app/database/models.py` (add `PartnerPromo`, after `InfoPage` ~line 4597)

**Context:** Mirror `InfoPage` (models.py:4582): JSONB multilingual fields, `is_active`, `sort_order`, timestamps via `AwareDateTime()`/`func.now()`. InfoPage uses `JSONB` — confirm the import and the migration JSON type it used; mirror exactly. Latest migration head = `0098`.

- [ ] **Step 1: Add the model**

In `app/database/models.py`, after `InfoPage` (~line 4597), add:
```python
class PartnerPromo(Base):
    """Outbound cross-promo card (admin-managed). Shown to users in bot + cabinet."""

    __tablename__ = 'partner_promos'
    id = Column(Integer, primary_key=True, index=True)
    title = Column(JSONB, nullable=False, server_default='{}')        # {lang: text}
    description = Column(JSONB, nullable=False, server_default='{}')   # {lang: text}
    url = Column(String(2048), nullable=False)
    image_url = Column(String(2048), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, server_default='true')
    sort_order = Column(Integer, nullable=False, default=0, server_default='0')
    click_count = Column(Integer, nullable=False, default=0, server_default='0')
    created_at = Column(AwareDateTime(), server_default=func.now())
    updated_at = Column(AwareDateTime(), server_default=func.now(), onupdate=func.now())
```
Confirm `JSONB` is imported in models.py (InfoPage uses it). If InfoPage uses a different JSON type, match it.

- [ ] **Step 2: Create migration**

Create `migrations/alembic/versions/0099_create_partner_promos.py`:
```python
"""create partner_promos table

Revision ID: 0099
Revises: 0098
Create Date: 2026-05-31

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0099'
down_revision: Union[str, None] = '0098'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'partner_promos',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('title', postgresql.JSONB(), nullable=False, server_default='{}'),
        sa.Column('description', postgresql.JSONB(), nullable=False, server_default='{}'),
        sa.Column('url', sa.String(2048), nullable=False),
        sa.Column('image_url', sa.String(2048), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('click_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table('partner_promos')
```
FIRST confirm `0098` is the single head, and check an existing InfoPage/JSONB migration to mirror the exact JSON column type (`postgresql.JSONB()` vs `sa.JSON()`).

- [ ] **Step 3: Verify**

Run: `.venv/Scripts/python.exe -c "import app.database.models; print('models OK')"`
Run: `.venv/Scripts/python.exe -c "from alembic.config import Config; from alembic.script import ScriptDirectory; s=ScriptDirectory.from_config(Config('alembic.ini')); print('heads:', s.get_heads())"` → single head `('0099',)`.

- [ ] **Step 4: Commit**

```bash
git add app/database/models.py migrations/alembic/versions/0099_create_partner_promos.py
git commit -m "feat(partner-showcase): partner_promos table + model (migration 0099)"
```

---

## Task 2: CRUD + link-safety + config

**Files:**
- Create: `app/database/crud/partner_promo.py`, `tests/services/test_partner_promo.py`
- Modify: `app/config.py`

**Context:** Mirror `app/database/crud/info_pages.py` shape (AsyncSession, select/insert/update). Atomic click increment via `update()` statement. https-only link-safety.

- [ ] **Step 1: Add config**

In `app/config.py` `class Settings`, near other feature flags, add:
```python
    PARTNER_SHOWCASE_ENABLED: bool = False  # Outbound partner showcase (cross-promo)
```

- [ ] **Step 2: Write failing tests**

Create `tests/services/test_partner_promo.py`:
```python
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.database.crud.partner_promo as crud


def test_is_safe_url_accepts_https():
    assert crud._is_safe_url('https://partner.example.com/path') is True


@pytest.mark.parametrize('bad', [
    'http://insecure.example.com',
    'javascript:alert(1)',
    'data:text/html,x',
    'ftp://x',
    '',
    'partner.example.com',
])
def test_is_safe_url_rejects(bad):
    assert crud._is_safe_url(bad) is False


@pytest.mark.asyncio
async def test_create_rejects_non_https():
    db = MagicMock(); db.add = MagicMock(); db.commit = AsyncMock()
    with pytest.raises(ValueError):
        await crud.create(db, title={'ru': 'X'}, url='http://x.com')


@pytest.mark.asyncio
async def test_create_rejects_bad_image_url():
    db = MagicMock(); db.add = MagicMock(); db.commit = AsyncMock()
    with pytest.raises(ValueError):
        await crud.create(db, title={'ru': 'X'}, url='https://ok.com', image_url='javascript:x')


@pytest.mark.asyncio
async def test_increment_click_uses_atomic_update():
    db = MagicMock(); db.execute = AsyncMock(); db.commit = AsyncMock()
    await crud.increment_click(db, 7)
    assert db.execute.await_count == 1
    db.commit.assert_awaited_once()
```

- [ ] **Step 3: Run → FAIL**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_partner_promo.py -v` → ModuleNotFoundError.

- [ ] **Step 4: Implement CRUD**

Create `app/database/crud/partner_promo.py`:
```python
from __future__ import annotations

from urllib.parse import urlparse

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import PartnerPromo


logger = structlog.get_logger(__name__)


def _is_safe_url(value: str | None) -> bool:
    if not value or not isinstance(value, str):
        return False
    try:
        parsed = urlparse(value)
    except Exception:
        return False
    return parsed.scheme == 'https' and bool(parsed.netloc)


async def list_active(db: AsyncSession) -> list[PartnerPromo]:
    result = await db.execute(
        select(PartnerPromo)
        .where(PartnerPromo.is_active == True)  # noqa: E712
        .order_by(PartnerPromo.sort_order.asc(), PartnerPromo.id.asc())
    )
    return list(result.scalars().all())


async def list_all(db: AsyncSession) -> list[PartnerPromo]:
    result = await db.execute(
        select(PartnerPromo).order_by(PartnerPromo.sort_order.asc(), PartnerPromo.id.asc())
    )
    return list(result.scalars().all())


async def get(db: AsyncSession, promo_id: int) -> PartnerPromo | None:
    result = await db.execute(select(PartnerPromo).where(PartnerPromo.id == promo_id))
    return result.scalar_one_or_none()


async def create(db: AsyncSession, *, title: dict, url: str, description: dict | None = None,
                 image_url: str | None = None, is_active: bool = True, sort_order: int = 0) -> PartnerPromo:
    if not _is_safe_url(url):
        raise ValueError('url must be https')
    if image_url is not None and image_url != '' and not _is_safe_url(image_url):
        raise ValueError('image_url must be https')
    promo = PartnerPromo(
        title=title or {}, description=description or {}, url=url,
        image_url=image_url or None, is_active=is_active, sort_order=sort_order,
    )
    db.add(promo)
    await db.commit()
    await db.refresh(promo)
    return promo


async def update_promo(db: AsyncSession, promo_id: int, **fields) -> PartnerPromo | None:
    if 'url' in fields and not _is_safe_url(fields['url']):
        raise ValueError('url must be https')
    if fields.get('image_url') and not _is_safe_url(fields['image_url']):
        raise ValueError('image_url must be https')
    promo = await get(db, promo_id)
    if promo is None:
        return None
    for k, v in fields.items():
        if hasattr(promo, k):
            setattr(promo, k, v)
    await db.commit()
    await db.refresh(promo)
    return promo


async def delete(db: AsyncSession, promo_id: int) -> bool:
    promo = await get(db, promo_id)
    if promo is None:
        return False
    await db.delete(promo)
    await db.commit()
    return True


async def increment_click(db: AsyncSession, promo_id: int) -> None:
    """Atomic click++ (no read-modify-write race)."""
    await db.execute(
        update(PartnerPromo)
        .where(PartnerPromo.id == promo_id)
        .values(click_count=PartnerPromo.click_count + 1)
    )
    await db.commit()
```

- [ ] **Step 5: Run → PASS**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_partner_promo.py -v`
Run: `.venv/Scripts/python.exe -c "import app.config; print(app.config.settings.PARTNER_SHOWCASE_ENABLED)"` → `False`.

- [ ] **Step 6: Commit**

```bash
git add app/database/crud/partner_promo.py app/config.py tests/services/test_partner_promo.py
git commit -m "feat(partner-showcase): CRUD + https link-safety + atomic click counter + config"
```

---

## Task 3: public redirect endpoint

**Files:**
- Create: `app/webserver/partner_promo.py`
- Modify: `app/webserver/unified_app.py` (register router)
- Test: add redirect tests to `tests/services/test_partner_promo.py`

**Context:** `app/webserver/unified_app.py` builds the public FastAPI app + `app.include_router(cabinet_router)` (~line 87); `RedirectResponse` already imported there. PUBLIC endpoint (no auth — opened from a bot link). Gate by `PARTNER_SHOWCASE_ENABLED`. CONFIRM how webserver handlers get a DB session — check `app/webserver/payments.py`: webserver handlers likely use `async with AsyncSessionLocal() as db:` inside the handler rather than FastAPI `Depends`. Mirror the REAL pattern.

- [ ] **Step 1: Implement the redirect router**

Create `app/webserver/partner_promo.py` (adapt DB-session acquisition to the real webserver pattern found in payments.py):
```python
from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import RedirectResponse

from app.config import settings
from app.database.crud import partner_promo as crud
from app.database.database import AsyncSessionLocal


logger = structlog.get_logger(__name__)
router = APIRouter(prefix='/partner-promo', tags=['Partner Promo'])


@router.get('/{promo_id}/go')
async def partner_promo_go(promo_id: int):
    if not settings.PARTNER_SHOWCASE_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Not found')
    async with AsyncSessionLocal() as db:
        promo = await crud.get(db, promo_id)
        if promo is None or not promo.is_active:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Not found')
        await crud.increment_click(db, promo_id)
    return RedirectResponse(url=promo.url, status_code=status.HTTP_302_FOUND)
```
Confirm `AsyncSessionLocal` import path (`from app.database.database import AsyncSessionLocal` is used elsewhere — verify). If webserver uses a Depends-based session, use that instead.

- [ ] **Step 2: Register in unified_app.py**

In `app/webserver/unified_app.py`, import the router and `app.include_router(partner_promo_router)` on the SAME app instance that serves public browser traffic (the one with `cabinet_router` + cabinet static, ~line 87). Confirm which builder function serves public traffic and register there.

- [ ] **Step 3: Add redirect tests**

Append to `tests/services/test_partner_promo.py`:
```python
@pytest.mark.asyncio
async def test_go_redirects_and_counts(monkeypatch):
    import app.webserver.partner_promo as pp
    from types import SimpleNamespace
    from contextlib import asynccontextmanager

    monkeypatch.setattr(pp.settings, 'PARTNER_SHOWCASE_ENABLED', True, raising=False)
    promo = SimpleNamespace(id=1, url='https://partner.example.com', is_active=True)
    monkeypatch.setattr(pp.crud, 'get', AsyncMock(return_value=promo))
    inc = AsyncMock()
    monkeypatch.setattr(pp.crud, 'increment_click', inc)

    @asynccontextmanager
    async def _fake_session():
        yield MagicMock()
    monkeypatch.setattr(pp, 'AsyncSessionLocal', _fake_session)

    resp = await pp.partner_promo_go(1)
    assert resp.status_code == 302
    assert resp.headers['location'] == 'https://partner.example.com'
    inc.assert_awaited_once()


@pytest.mark.asyncio
async def test_go_404_when_disabled(monkeypatch):
    import app.webserver.partner_promo as pp
    from fastapi import HTTPException
    monkeypatch.setattr(pp.settings, 'PARTNER_SHOWCASE_ENABLED', False, raising=False)
    with pytest.raises(HTTPException) as exc:
        await pp.partner_promo_go(1)
    assert exc.value.status_code == 404
```
(If `settings` instance patch fails under Pydantic v2, patch `type(pp.settings)` — mirror the repo's established pattern.)

- [ ] **Step 4: Verify + commit**

Run: `.venv/Scripts/python.exe -c "import app.webserver.unified_app; print('OK')"`
Run: `.venv/Scripts/python.exe -m pytest tests/services/test_partner_promo.py -v`
```bash
git add app/webserver/partner_promo.py app/webserver/unified_app.py tests/services/test_partner_promo.py
git commit -m "feat(partner-showcase): public click-redirect endpoint (/partner-promo/{id}/go)"
```

---

## Task 4: cabinet public list endpoint

**Files:**
- Create: `app/cabinet/routes/partner_promo.py`
- Modify: `app/cabinet/routes/__init__.py`

**Context:** Mirror a simple public-ish cabinet GET (parts of `app/cabinet/routes/info.py` — match its auth posture: if info is public, keep public). Gate by `PARTNER_SHOWCASE_ENABLED`. Routers registered in `app/cabinet/routes/__init__.py` via `router.include_router(...)`. Confirm `get_cabinet_db` import path used by sibling cabinet routers.

- [ ] **Step 1: Implement**

Create `app/cabinet/routes/partner_promo.py`:
```python
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud import partner_promo as crud

from ..dependencies import get_cabinet_db


router = APIRouter(prefix='/partner-promos', tags=['Cabinet Partner Promos'])


@router.get('')
async def list_partner_promos(db: AsyncSession = Depends(get_cabinet_db)) -> dict[str, Any]:
    if not settings.PARTNER_SHOWCASE_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Not found')
    promos = await crud.list_active(db)
    return {
        'promos': [
            {
                'id': p.id,
                'title': p.title,
                'description': p.description,
                'image_url': p.image_url,
                'go_url': f'/partner-promo/{p.id}/go',
            }
            for p in promos
        ]
    }
```
Confirm `get_cabinet_db` import path (`from ..dependencies import get_cabinet_db` or `from ...dependencies`) matches sibling routers in this dir.

- [ ] **Step 2: Register**

In `app/cabinet/routes/__init__.py`, import + `router.include_router(partner_promo_router)` (mirror info/landing registration).

- [ ] **Step 3: Verify + commit**

Run: `.venv/Scripts/python.exe -c "import app.cabinet.routes; print('OK')"`
```bash
git add app/cabinet/routes/partner_promo.py app/cabinet/routes/__init__.py
git commit -m "feat(partner-showcase): cabinet public list endpoint"
```

---

## Task 5: admin CRUD endpoints

**Files:**
- Create: `app/cabinet/routes/admin_partner_promos.py`
- Modify: `app/cabinet/routes/__init__.py`

**Context:** Mirror `app/cabinet/routes/admin_info_pages.py` (admin-auth dependency, list/create/update/delete/toggle, Pydantic request models). CRUD from Task 2. `create`/`update_promo` raise `ValueError` on non-https → map to HTTP 400.

- [ ] **Step 1: Read the admin pattern**

Read `app/cabinet/routes/admin_info_pages.py` — note admin-auth dependency, router prefix, endpoint shapes + Pydantic models. Mirror it.

- [ ] **Step 2: Implement admin CRUD**

Create `app/cabinet/routes/admin_partner_promos.py`: list (`list_all`), get, create, update, delete, toggle-active — admin-gated exactly like info-pages. Wrap CRUD calls; on `ValueError` → `HTTPException(400, detail={'code':'invalid_url','message':str(e)})`. Pydantic models: create (title dict, url, description dict optional, image_url optional, is_active, sort_order), update (all optional).

- [ ] **Step 3: Register**

In `app/cabinet/routes/__init__.py`, register the admin router (mirror `admin_info_pages`).

- [ ] **Step 4: Verify + commit**

Run: `.venv/Scripts/python.exe -c "import app.cabinet.routes; print('OK')"`
```bash
git add app/cabinet/routes/admin_partner_promos.py app/cabinet/routes/__init__.py
git commit -m "feat(partner-showcase): admin CRUD endpoints (https-validated)"
```

---

## Task 6: bot showcase menu

**Files:**
- Modify: `app/keyboards/inline.py` (main menu gated button), `app/handlers/` (handler for `nz!_partner_showcase`)

**Context:** Main menu in `app/keyboards/inline.py` (`get_main_menu_keyboard_async` ~line 34; `menu_info` button ~456). Handler: mirror the info-page list handler (find `nz!_menu_info` registration + handler). Each promo → a URL button to `{public_base}/partner-promo/{id}/go`.

- [ ] **Step 1: Read main-menu + info-list handler + public base URL helper**

Read `app/keyboards/inline.py` around the `menu_info` button (~456) and the handler responding to the info menu (grep `nz!_menu_info`). Note how a dynamic list becomes inline buttons and how the public base URL is obtained (settings helper for cabinet/site base).

- [ ] **Step 2: Add gated menu button**

In the main-menu builder, add gated on `settings.PARTNER_SHOWCASE_ENABLED` (mirror existing gated buttons), near `menu_info`:
```python
    if settings.PARTNER_SHOWCASE_ENABLED:
        buttons.append([InlineKeyboardButton(text=texts.t('MENU_PARTNERS', '🤝 Партнёры'), callback_data='nz!_partner_showcase')])
```
Match the file's real append/pairing style.

- [ ] **Step 3: Add the handler**

Add a handler for `F.data == 'nz!_partner_showcase'` (register where sibling menu handlers register). It:
- `from app.database.crud import partner_promo as crud; promos = await crud.list_active(db)`;
- empty → answer «Пока нет партнёров» / back;
- else build a message + inline keyboard: one `InlineKeyboardButton(text=<title[lang]>, url=f'{base}/partner-promo/{p.id}/go')` per promo (URL buttons → Telegram opens redirect → click counted). `base` = real public base URL helper from Step 1.
- back button to menu.
- `title[lang]` resolve: `p.title.get(user.language) or p.title.get('ru') or next(iter(p.title.values()), 'Partner')`.

- [ ] **Step 4: Verify + commit**

Run: `.venv/Scripts/python.exe -c "import app.keyboards.inline; print('OK')"` (+ the handler module).
```bash
git add app/keyboards/inline.py app/handlers/
git commit -m "feat(partner-showcase): bot «Партнёры» menu + handler"
```

---

## Task 7: cabinet React page (NESTED repo) + finalize

**Files (NESTED cabinet repo `bedolaga-cabinet`):**
- Create: `src/api/partnerPromo.ts`, `src/pages/PartnerShowcase.tsx`; Modify `src/App.tsx`, nav (`DesktopSidebar.tsx` + dock).
**Files (main repo):** Modify `.env.example`.

**Context:** Cabinet = NESTED git repo at `bedolaga-cabinet/` (own `.git`, gitignored by main). Commit frontend there on a `feat/partner-showcase` branch off its `main`. Mirror the speedtest frontend pattern (api module + lazy page + protected route + gated nav). Endpoint `GET /api/cabinet/partner-promos`; match apiClient prefix as speedtest did (`/api` base → call `/cabinet/partner-promos`). Cards link to `/partner-promo/{id}/go`.

- [ ] **Step 1: Frontend (nested repo)**

In `bedolaga-cabinet/`: `git checkout main && git checkout -b feat/partner-showcase`. Create:
- `src/api/partnerPromo.ts`: `getPromos()` → `apiClient.get('/cabinet/partner-promos')`, typed `{promos: [{id,title,description,image_url,go_url}]}`.
- `src/pages/PartnerShowcase.tsx`: fetch promos, render cards (image + title[lang] + description[lang] + «Перейти» link to `go_url`). Reuse existing Card/Button. 404 → graceful «недоступно». i18n keys with RU/EN fallback. title[lang] resolve: `title[i18n.language] || title.ru || Object.values(title)[0]`.
- `src/App.tsx`: lazy import + protected route `/partners`.
- nav: gated item in `DesktopSidebar.tsx` (+ dock/orbit) — gate like speedtest (always-show-when-authenticated; page handles 404). Existing icon.

Build: `cd bedolaga-cabinet && npx tsc --noEmit` (no new errors) + `npm run build` (succeeds). Commit in nested repo:
```bash
git add src/api/partnerPromo.ts src/pages/PartnerShowcase.tsx src/App.tsx src/components/layout/AppShell/ src/locales/ src/hooks/
git commit -m "feat(partner-showcase): cabinet PartnerShowcase page + api + route + nav"
```

- [ ] **Step 2: .env.example (main repo)**

Add near other feature flags:
```
# Partner showcase (outbound cross-promo): admin-managed partner links shown in bot + cabinet, with click counting.
PARTNER_SHOWCASE_ENABLED=false
```

- [ ] **Step 3: Final backend verify (main repo)**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_partner_promo.py -v` → PASS.
Run: `.venv/Scripts/python.exe -m pytest tests/services/ -q` → no NEW failures vs baseline (~29 pre-existing).
Run: `.venv/Scripts/python.exe -c "import app.webserver.unified_app; import app.cabinet.routes; import app.keyboards.inline; print('OK')"`.

- [ ] **Step 4: Commit (main repo)**

```bash
git add .env.example
git commit -m "docs(partner-showcase): env flag in .env.example"
```

---

## Self-Review Checklist (controller runs before final review)

- [ ] Migration 0099 single head, down_revision 0098, reversible (drop_table). JSONB type matches InfoPage.
- [ ] Model fields match migration columns.
- [ ] `_is_safe_url` rejects non-https / no-scheme / javascript:/data: ; enforced in create + update.
- [ ] `increment_click` atomic (UPDATE-increment).
- [ ] Public `/go`: 404 when disabled/inactive, else inc + 302 to https url.
- [ ] Bot menu + cabinet page gated by PARTNER_SHOWCASE_ENABLED; empty list handled.
- [ ] Click counted via /go (buttons point there, not raw url).
- [ ] Frontend committed in NESTED cabinet repo (not main).
- [ ] All gated by PARTNER_SHOWCASE_ENABLED (default OFF).

## Out of plan scope (follow-ups)

- React admin-CRUD UI (v1 = backend admin endpoints; create via API/Swagger).
- Per-day click analytics (only total click_count).
- Card rotation / targeting / A-B (only sort_order).
- Localizing showcase strings across all locale files (inline RU/EN fallback).
