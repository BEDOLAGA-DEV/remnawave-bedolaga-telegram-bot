"""Документы кабинета на языке, для которого текста нет.

Кабинет передаёт язык интерфейса в ``/cabinet/info/*`` (#570 в bedolaga-cabinet).
Раньше он всегда слал язык по умолчанию, и две дыры бэкенда не были видны:

* редактор в админке сохраняет строку документа на каждый язык, в том числе пустую;
  fallback сервисов срабатывает только на отсутствие строки, поэтому для zh/fa
  отдавалась встроенная заглушка вместо политики, оферты и рекуррентных платежей;
* ``/rules`` вообще не откатывался на язык по умолчанию.

Тесты фиксируют: пустой или отсутствующий документ → документ языка по умолчанию;
заполненный документ на запрошенном языке отдаётся как есть; если текста нет и на
языке по умолчанию — поведение прежнее (заглушка).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.cabinet.routes import info


DB = object()


def _doc(content: str) -> SimpleNamespace:
    return SimpleNamespace(content=content, updated_at=None)


def _by_language(docs: dict[str, SimpleNamespace | None]) -> AsyncMock:
    async def lookup(_db, language, fallback=False):
        return docs.get(language)

    return AsyncMock(side_effect=lookup)


DOCUMENT_ROUTES = [
    ('get_privacy_policy', 'PrivacyPolicyService', 'get_policy', '# Политика конфиденциальности'),
    ('get_public_offer', 'PublicOfferService', 'get_offer', '# Публичная оферта'),
    ('get_recurrent_payments', 'RecurrentPaymentsService', 'get_document', '# Рекуррентные платежи'),
]


@pytest.fixture(autouse=True)
def _visible_and_default_ru():
    with (
        patch.object(info, 'is_visible_in_web', return_value=True),
        patch.object(info.settings, 'DEFAULT_LANGUAGE', 'ru'),
    ):
        yield


@pytest.mark.parametrize(('route', 'service', 'method', 'stub'), DOCUMENT_ROUTES)
async def test_empty_row_falls_back_to_default_language(route, service, method, stub):
    """Пустая строка на zh (или одни пробелы) не должна подменять собой документ на ru."""
    lookup = _by_language({'zh': _doc('  \n '), 'ru': _doc('ru text')})
    with patch.object(getattr(info, service), method, lookup):
        response = await getattr(info, route)(language='zh', db=DB)

    assert response.content == 'ru text'


@pytest.mark.parametrize(('route', 'service', 'method', 'stub'), DOCUMENT_ROUTES)
async def test_filled_document_is_returned_as_is(route, service, method, stub):
    """Заполненный документ на запрошенном языке не трогается."""
    lookup = _by_language({'en': _doc('en text'), 'ru': _doc('ru text')})
    with patch.object(getattr(info, service), method, lookup):
        response = await getattr(info, route)(language='en', db=DB)

    assert response.content == 'en text'
    assert lookup.await_count == 1


@pytest.mark.parametrize(('route', 'service', 'method', 'stub'), DOCUMENT_ROUTES)
async def test_no_text_anywhere_keeps_stub(route, service, method, stub):
    """Нет текста и на языке по умолчанию — прежнее поведение, встроенная заглушка."""
    lookup = _by_language({'zh': _doc(''), 'ru': None})
    with patch.object(getattr(info, service), method, lookup):
        response = await getattr(info, route)(language='zh', db=DB)

    assert response.content.startswith(stub)


async def test_rules_without_language_row_use_default_language():
    """Правил на zh нет — берутся правила ru, а не встроенный текст."""
    rules = {'ru': SimpleNamespace(updated_at=None)}
    get_rules = AsyncMock(side_effect=lambda _db, language: rules.get(language))
    get_content = AsyncMock(return_value='ru rules')
    with (
        patch.object(info, 'get_rules_by_language', get_rules),
        patch.object(info, 'get_current_rules_content', get_content),
    ):
        response = await info.get_rules(language='zh', db=DB)

    assert response.content == 'ru rules'
    get_content.assert_awaited_once_with(DB, 'ru')


async def test_rules_in_requested_language_are_kept():
    rules = {'en': SimpleNamespace(updated_at=None), 'ru': SimpleNamespace(updated_at=None)}
    get_rules = AsyncMock(side_effect=lambda _db, language: rules.get(language))
    get_content = AsyncMock(return_value='en rules')
    with (
        patch.object(info, 'get_rules_by_language', get_rules),
        patch.object(info, 'get_current_rules_content', get_content),
    ):
        response = await info.get_rules(language='en', db=DB)

    assert response.content == 'en rules'
    get_content.assert_awaited_once_with(DB, 'en')
