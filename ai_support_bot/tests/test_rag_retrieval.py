import os

import pytest

os.environ.setdefault('AISUP_DATABASE_URL', 'sqlite+aiosqlite:///./data/ai_support_test.db')
os.environ.setdefault('AISUP_MAIN_DATABASE_URL', '')
os.environ.setdefault('AISUP_INCLUDE_REMNAWAVE_DATA', 'false')

from ai_support_bot.app.services import rag_service as rag_mod


class _Chunk:
    def __init__(self, question: str, embedding: list[float]) -> None:
        self.question = question
        self.answer = f'ответ: {question}'
        self.content = f'Вопрос: {question}\nОтвет: {self.answer}'
        self.embedding = embedding


@pytest.fixture(autouse=True)
def _clean_caches():
    rag_mod.rag_service.invalidate_cache()
    rag_mod._embedding_cache._store.clear()
    yield
    rag_mod.rag_service.invalidate_cache()
    rag_mod._embedding_cache._store.clear()


def _patch_store(monkeypatch, values: dict):
    monkeypatch.setattr(rag_mod.settings_store, 'get', lambda key: str(values.get(key, '')))
    monkeypatch.setattr(rag_mod.settings_store, 'get_int', lambda key: int(values.get(key, 0)))
    monkeypatch.setattr(rag_mod.settings_store, 'get_float', lambda key: float(values.get(key, 0.0)))
    monkeypatch.setattr(rag_mod.settings_store, 'get_bool', lambda key: bool(values.get(key, False)))


@pytest.mark.asyncio
async def test_in_memory_retrieval_ranks_by_cosine_similarity(monkeypatch):
    _patch_store(monkeypatch, {
        'EMBEDDING_MODEL': 'text-embedding-3-small',
        'EMBEDDING_CACHE_TTL': 0,
        'TOP_K': 2,
        'MIN_SCORE': 0.1,
        'PGVECTOR_ENABLED': False,
    })

    chunks = [
        _Chunk('как продлить подписку', [1.0, 0.0, 0.0]),
        _Chunk('как добавить устройство', [0.0, 1.0, 0.0]),
        _Chunk('не работает оплата', [0.0, 0.0, 1.0]),
    ]

    async def fake_get_active_chunks(db):
        return chunks

    async def fake_embed(text, model):
        return [0.9, 0.1, 0.0]

    monkeypatch.setattr(rag_mod.crud, 'get_active_chunks', fake_get_active_chunks)
    monkeypatch.setattr(rag_mod.openai_client, 'create_embedding', fake_embed)

    matches = await rag_mod.rag_service.retrieve(None, 'хочу продлить подписку')

    assert len(matches) == 2
    assert matches[0]['question'] == 'как продлить подписку'
    assert matches[0]['score'] > matches[1]['score']


@pytest.mark.asyncio
async def test_min_score_filters_irrelevant_chunks(monkeypatch):
    _patch_store(monkeypatch, {
        'EMBEDDING_MODEL': 'text-embedding-3-small',
        'EMBEDDING_CACHE_TTL': 0,
        'TOP_K': 5,
        'MIN_SCORE': 0.95,
        'PGVECTOR_ENABLED': False,
    })

    async def fake_get_active_chunks(db):
        return [_Chunk('не работает оплата', [0.0, 0.0, 1.0])]

    async def fake_embed(text, model):
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr(rag_mod.crud, 'get_active_chunks', fake_get_active_chunks)
    monkeypatch.setattr(rag_mod.openai_client, 'create_embedding', fake_embed)

    assert await rag_mod.rag_service.retrieve(None, 'вопрос про подписку') == []


@pytest.mark.asyncio
async def test_embedding_cache_avoids_repeated_api_calls(monkeypatch):
    _patch_store(monkeypatch, {
        'EMBEDDING_MODEL': 'text-embedding-3-small',
        'EMBEDDING_CACHE_TTL': 3600,
        'TOP_K': 1,
        'MIN_SCORE': 0.0,
        'PGVECTOR_ENABLED': False,
    })
    calls = {'n': 0}

    async def fake_get_active_chunks(db):
        return [_Chunk('как продлить подписку', [1.0, 0.0])]

    async def fake_embed(text, model):
        calls['n'] += 1
        return [1.0, 0.0]

    monkeypatch.setattr(rag_mod.crud, 'get_active_chunks', fake_get_active_chunks)
    monkeypatch.setattr(rag_mod.openai_client, 'create_embedding', fake_embed)

    await rag_mod.rag_service.retrieve(None, 'как продлить подписку')
    await rag_mod.rag_service.retrieve(None, 'Как Продлить Подписку  ')

    assert calls['n'] == 1


@pytest.mark.asyncio
async def test_embedding_failure_returns_empty_result(monkeypatch):
    _patch_store(monkeypatch, {
        'EMBEDDING_MODEL': 'text-embedding-3-small',
        'EMBEDDING_CACHE_TTL': 0,
        'TOP_K': 5,
        'MIN_SCORE': 0.1,
        'PGVECTOR_ENABLED': False,
    })

    async def failing_embed(text, model):
        raise rag_mod.OpenAIError('rate limited')

    monkeypatch.setattr(rag_mod.openai_client, 'create_embedding', failing_embed)

    assert await rag_mod.rag_service.retrieve(None, 'вопрос про оплату') == []


@pytest.mark.asyncio
async def test_pgvector_failure_falls_back_to_in_memory(monkeypatch):
    _patch_store(monkeypatch, {
        'EMBEDDING_MODEL': 'text-embedding-3-small',
        'EMBEDDING_CACHE_TTL': 0,
        'TOP_K': 1,
        'MIN_SCORE': 0.0,
        'PGVECTOR_ENABLED': True,
    })
    monkeypatch.setattr(rag_mod.db_module, 'pgvector_ready', True)

    async def fake_get_active_chunks(db):
        return [_Chunk('как продлить подписку', [1.0, 0.0])]

    async def fake_embed(text, model):
        return [1.0, 0.0]

    async def failing_pgvector(db, query_embedding, top_k, min_score):
        rag_mod.db_module.pgvector_ready = False
        return None

    monkeypatch.setattr(rag_mod.crud, 'get_active_chunks', fake_get_active_chunks)
    monkeypatch.setattr(rag_mod.openai_client, 'create_embedding', fake_embed)
    monkeypatch.setattr(rag_mod.rag_service, '_retrieve_pgvector', failing_pgvector)

    matches = await rag_mod.rag_service.retrieve(None, 'как продлить подписку')

    assert len(matches) == 1
    assert matches[0]['question'] == 'как продлить подписку'


def test_vector_literal_format():
    assert rag_mod._to_vector_literal([0.5, -1.25, 0.0]) == '[0.5,-1.25,0]'


@pytest.mark.asyncio
async def test_large_knowledge_base_uses_vectorized_search(monkeypatch):
    _patch_store(monkeypatch, {
        'EMBEDDING_MODEL': 'text-embedding-3-small',
        'EMBEDDING_CACHE_TTL': 0,
        'TOP_K': 5,
        'MIN_SCORE': 0.0,
        'PGVECTOR_ENABLED': False,
    })

    dims = 32
    chunks = []
    for index in range(2000):
        vector = [0.0] * dims
        vector[index % dims] = 1.0
        chunks.append(_Chunk(f'вопрос {index}', vector))

    async def fake_get_active_chunks(db):
        return chunks

    async def fake_embed(text, model):
        vector = [0.0] * dims
        vector[3] = 1.0
        return vector

    monkeypatch.setattr(rag_mod.crud, 'get_active_chunks', fake_get_active_chunks)
    monkeypatch.setattr(rag_mod.openai_client, 'create_embedding', fake_embed)

    matches = await rag_mod.rag_service.retrieve(None, 'тестовый вопрос про подписку')

    assert len(matches) == 5
    assert all(match['score'] == pytest.approx(1.0, abs=1e-3) for match in matches)
