import asyncio
import math
import os
import random
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault('AISUP_DATABASE_URL', 'sqlite+aiosqlite:///./data/ai_support_test.db')
os.environ.setdefault('AISUP_MAIN_DATABASE_URL', '')
os.environ.setdefault('AISUP_INCLUDE_REMNAWAVE_DATA', 'false')

from ai_support_bot.app.db import crud
from ai_support_bot.app.db.models import Base, KnowledgeChunk, KnowledgeSource
from ai_support_bot.app.services import rag_service as rag_mod


class _Chunk:
    def __init__(self, question: str, embedding: list[float]) -> None:
        self.question = question
        self.answer = f'ответ: {question}'
        self.content = f'Вопрос: {question}\nОтвет: {self.answer}'
        self.embedding = embedding


@pytest.fixture(autouse=True)
def _clean_caches(monkeypatch):
    values = {
        'EMBEDDING_MODEL': 'text-embedding-3-small',
        'EMBEDDING_CACHE_TTL': 0,
        'TOP_K': 3,
        'MIN_SCORE': 0.0,
        'PGVECTOR_ENABLED': False,
    }
    monkeypatch.setattr(rag_mod.settings_store, 'get', lambda key: str(values.get(key, '')))
    monkeypatch.setattr(rag_mod.settings_store, 'get_int', lambda key: int(values.get(key, 0)))
    monkeypatch.setattr(rag_mod.settings_store, 'get_float', lambda key: float(values.get(key, 0.0)))
    monkeypatch.setattr(rag_mod.settings_store, 'get_bool', lambda key: bool(values.get(key, False)))
    rag_mod.rag_service.invalidate_cache()
    rag_mod._embedding_cache._store.clear()
    yield values
    rag_mod.rag_service.invalidate_cache()
    rag_mod._embedding_cache._store.clear()


@pytest.fixture
def query(monkeypatch):
    """Вектор вопроса задаётся тестом: query['vec'] = [...]."""
    state = {'vec': [1.0, 0.0, 0.0]}

    async def fake_embed(text, model):
        return state['vec']

    monkeypatch.setattr(rag_mod.openai_client, 'create_embedding', fake_embed)
    return state


@pytest.fixture
def loads(monkeypatch):
    """Считает полные перечитывания кусков и проверки подписи."""
    calls = {'chunks': 0, 'signature': 0}
    original_chunks = crud.get_active_chunks
    original_signature = rag_mod.RAGService._knowledge_signature

    async def counting_chunks(db):
        calls['chunks'] += 1
        return await original_chunks(db)

    async def counting_signature(self, db):
        calls['signature'] += 1
        return await original_signature(self, db)

    monkeypatch.setattr(rag_mod.crud, 'get_active_chunks', counting_chunks)
    monkeypatch.setattr(rag_mod.RAGService, '_knowledge_signature', counting_signature)
    return calls


@pytest_asyncio.fixture
async def kb(tmp_path):
    """База знаний в SQLite; admin — отдельный движок, как админка основного бота в другом процессе."""
    path = tmp_path / 'kb.db'
    engine = create_async_engine(f'sqlite+aiosqlite:///{path}')
    admin_engine = create_async_engine(f'sqlite+aiosqlite:///{path}')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    statements: list[str] = []

    @event.listens_for(engine.sync_engine, 'before_cursor_execute')
    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    class KB:
        Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        Admin = async_sessionmaker(admin_engine, class_=AsyncSession, expire_on_commit=False)
        sql = statements

        async def add_source(self, name: str, vectors: dict[str, list[float]], updated_at=None) -> int:
            async with self.Admin() as db:
                source = await crud.create_source(db, filename=f'{name}.json', content_hash=name, title=name)
                if updated_at is not None:
                    source.created_at = updated_at
                    source.updated_at = updated_at
                await crud.add_chunks(db, [
                    KnowledgeChunk(
                        source_id=source.id,
                        chunk_hash=f'{name}:{question}',
                        content=f'Вопрос: {question}',
                        question=question,
                        answer=f'ответ: {question}',
                        embedding=vector,
                    )
                    for question, vector in vectors.items()
                ])
                await db.commit()
                return source.id

        async def retrieve(self, text: str = 'вопрос') -> list[dict]:
            async with self.Session() as db:
                return await rag_mod.rag_service.retrieve(db, text)

    yield KB()
    await engine.dispose()
    await admin_engine.dispose()


def _expire_cache() -> None:
    rag_mod._chunk_cache._checked -= rag_mod._CHUNK_CACHE_TTL + 1


def _questions(matches: list[dict]) -> list[str]:
    return [match['question'] for match in matches]


_BASE = {
    'как продлить подписку': [1.0, 0.0, 0.0],
    'как добавить устройство': [0.0, 1.0, 0.0],
    'не работает оплата': [0.0, 0.0, 1.0],
}


@pytest.mark.asyncio
async def test_cache_keeps_vectors_only_in_matrix(kb, loads, query):
    await kb.add_source('base', _BASE)

    matches = await kb.retrieve()

    assert matches[0]['question'] == 'как продлить подписку'
    rows, matrix = rag_mod._chunk_cache.get()
    assert len(rows) == 3
    assert all(set(row) == {'question', 'answer', 'content'} for row in rows)
    assert matrix.shape == (3, 3)
    assert matrix.dtype == rag_mod._np.float32


@pytest.mark.asyncio
async def test_unchanged_knowledge_base_is_not_reloaded(kb, loads, query):
    await kb.add_source('base', _BASE)

    first = await kb.retrieve()
    second = await kb.retrieve()
    assert loads == {'chunks': 1, 'signature': 1}
    assert first == second

    _expire_cache()
    kb.sql.clear()
    third = await kb.retrieve()

    assert loads == {'chunks': 1, 'signature': 2}
    assert third == first
    # После TTL — один лёгкий запрос, векторы не читаются.
    assert len(kb.sql) == 1
    assert 'embedding' not in kb.sql[0]

    await kb.retrieve()
    assert loads == {'chunks': 1, 'signature': 2}


@pytest.mark.asyncio
async def test_new_source_from_other_process_triggers_reload(kb, loads, query):
    await kb.add_source('base', _BASE)
    await kb.retrieve()

    await kb.add_source('extra', {'как сменить сервер': [0.6, 0.0, 0.8]})
    query['vec'] = [0.6, 0.0, 0.8]

    assert _questions(await kb.retrieve())[0] != 'как сменить сервер'  # в пределах TTL кэш прежний
    _expire_cache()
    matches = await kb.retrieve()

    assert loads['chunks'] == 2
    assert matches[0]['question'] == 'как сменить сервер'
    assert matches[0]['score'] == pytest.approx(1.0, abs=1e-4)


@pytest.mark.asyncio
async def test_disabled_source_gives_cached_empty_result(kb, loads, query):
    source_id = await kb.add_source('base', _BASE)
    assert await kb.retrieve() != []

    async with kb.Admin() as db:
        await crud.set_source_active(db, source_id, False)
    _expire_cache()

    assert await kb.retrieve() == []
    assert loads['chunks'] == 2
    assert rag_mod._chunk_cache.get() == ([], None)

    # Пустая база не перечитывается ни на каждом вопросе, ни после TTL.
    assert await kb.retrieve() == []
    _expire_cache()
    assert await kb.retrieve() == []
    assert loads['chunks'] == 2

    async with kb.Admin() as db:
        await crud.set_source_active(db, source_id, True)
    _expire_cache()

    assert _questions(await kb.retrieve())[0] == 'как продлить подписку'
    assert loads['chunks'] == 3


@pytest.mark.asyncio
async def test_reuploaded_source_with_reused_ids_triggers_reload(kb, loads, query):
    # SQLite без AUTOINCREMENT отдаёт удалённые id заново: число кусков и max(id) совпадут.
    old_id = await kb.add_source('old', {'старый ответ': [1.0, 0.0, 0.0]}, updated_at=datetime(2026, 1, 1))
    assert _questions(await kb.retrieve()) == ['старый ответ']

    async with kb.Admin() as db:
        assert await crud.delete_source(db, old_id)
    new_id = await kb.add_source('new', {'новый ответ': [1.0, 0.0, 0.0]})
    assert new_id == old_id
    _expire_cache()

    assert _questions(await kb.retrieve()) == ['новый ответ']
    assert loads['chunks'] == 2


@pytest.mark.asyncio
async def test_toggling_one_source_off_and_another_on_triggers_reload(kb, loads, query):
    first_id = await kb.add_source('first', {'первый': [1.0, 0.0, 0.0]})
    second_id = await kb.add_source('second', {'второй': [1.0, 0.0, 0.0]})
    async with kb.Admin() as db:
        await crud.set_source_active(db, second_id, False)
    assert _questions(await kb.retrieve()) == ['первый']

    async with kb.Admin() as db:
        # Переключаем оба источника, не трогая updated_at: число кусков то же.
        await db.execute(update(KnowledgeSource).where(KnowledgeSource.id == first_id).values(
            is_active=False, updated_at=KnowledgeSource.updated_at))
        await db.execute(update(KnowledgeSource).where(KnowledgeSource.id == second_id).values(
            is_active=True, updated_at=KnowledgeSource.updated_at))
        await db.commit()
    _expire_cache()

    assert _questions(await kb.retrieve()) == ['второй']
    assert loads['chunks'] == 2


@pytest.mark.asyncio
async def test_invalidate_cache_forces_reload(kb, loads, query):
    await kb.add_source('base', _BASE)
    await kb.retrieve()

    rag_mod.rag_service.invalidate_cache()
    await kb.retrieve()

    assert loads['chunks'] == 2


@pytest.mark.asyncio
async def test_signature_failure_falls_back_to_reload_after_ttl(monkeypatch, query):
    calls = {'n': 0}

    async def fake_get_active_chunks(db):
        calls['n'] += 1
        return [_Chunk('как продлить подписку', [1.0, 0.0, 0.0])]

    monkeypatch.setattr(rag_mod.crud, 'get_active_chunks', fake_get_active_chunks)

    # db=None: подпись не читается, работает прежнее поведение «перечитать после TTL».
    await rag_mod.rag_service.retrieve(None, 'вопрос')
    await rag_mod.rag_service.retrieve(None, 'вопрос')
    assert calls['n'] == 1
    _expire_cache()
    assert _questions(await rag_mod.rag_service.retrieve(None, 'вопрос')) == ['как продлить подписку']
    assert calls['n'] == 2


@pytest.mark.asyncio
async def test_concurrent_questions_load_chunks_once(monkeypatch, query):
    monkeypatch.setattr(rag_mod, '_chunk_refresh_lock', asyncio.Lock())
    calls = {'n': 0}

    async def slow_get_active_chunks(db):
        calls['n'] += 1
        await asyncio.sleep(0.01)
        return [_Chunk(question, vector) for question, vector in _BASE.items()]

    monkeypatch.setattr(rag_mod.crud, 'get_active_chunks', slow_get_active_chunks)

    results = await asyncio.gather(*(rag_mod.rag_service.retrieve(None, 'вопрос') for _ in range(5)))

    assert calls['n'] == 1
    assert all(_questions(result)[0] == 'как продлить подписку' for result in results)


# --- Совпадение с прежней реализацией -------------------------------------------------------


def _legacy_cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a)
    norm_b = sum(y * y for y in b)
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def _legacy_search(chunks, query_embedding, top_k, min_score, np):
    """Поиск в памяти до этапа 1 (rag_service._retrieve_in_memory), без кэша."""
    rows = [
        {'question': c.question, 'answer': c.answer, 'content': c.content, 'embedding': c.embedding or []}
        for c in chunks
        if c.embedding
    ]
    matrix = None
    if np is not None and rows:
        dims = len(rows[0]['embedding'])
        if all(len(row['embedding']) == dims for row in rows):
            matrix = np.asarray([row['embedding'] for row in rows], dtype=np.float32)
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            matrix = matrix / norms
    if not rows:
        return []
    scored = []
    if matrix is not None:
        query_vec = np.asarray(query_embedding, dtype=np.float32)
        query_norm = float(np.linalg.norm(query_vec)) or 1.0
        similarities = matrix @ (query_vec / query_norm)
        limit = min(max(top_k, 1), len(rows))
        for index in np.argpartition(-similarities, limit - 1)[:limit]:
            score = float(similarities[int(index)])
            if score >= min_score:
                scored.append((score, rows[int(index)]))
    else:
        for row in rows:
            score = _legacy_cosine(query_embedding, row['embedding'])
            if score >= min_score:
                scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        {'score': round(score, 4), 'question': row['question'], 'answer': row['answer'], 'content': row['content']}
        for score, row in scored[:top_k]
    ]


def _random_chunks(count: int, dims: int, seed: int, mixed_dims: bool = False) -> list[_Chunk]:
    rng = random.Random(seed)
    chunks = []
    for index in range(count):
        size = dims + (index % 2 if mixed_dims else 0)
        vector = [rng.uniform(-1.0, 1.0) for _ in range(size)]
        if index == 7:
            vector = [0.0] * size  # нулевой вектор
        chunks.append(_Chunk(f'вопрос {index}', vector))
    chunks.append(_Chunk('без вектора', []))
    return chunks


@pytest.mark.asyncio
@pytest.mark.parametrize('variant', ['numpy', 'mixed_dims', 'no_numpy'])
async def test_results_match_legacy_implementation(monkeypatch, _clean_caches, query, variant):
    chunks = _random_chunks(400, 48, seed=11, mixed_dims=variant == 'mixed_dims')
    np_module = None if variant == 'no_numpy' else rag_mod._np
    monkeypatch.setattr(rag_mod, '_np', np_module)

    async def fake_get_active_chunks(db):
        return chunks

    monkeypatch.setattr(rag_mod.crud, 'get_active_chunks', fake_get_active_chunks)

    rng = random.Random(5)
    for top_k, min_score in [(5, 0.0), (3, 0.2), (8, -1.0), (1, 0.0)]:
        _clean_caches['TOP_K'] = top_k
        _clean_caches['MIN_SCORE'] = min_score
        for _ in range(5):
            query['vec'] = [rng.uniform(-1.0, 1.0) for _ in range(48)]
            expected = _legacy_search(chunks, query['vec'], top_k, min_score, np_module)
            assert await rag_mod.rag_service.retrieve(None, 'вопрос') == expected

    rows, matrix = rag_mod._chunk_cache.get()
    if variant == 'numpy':
        assert matrix is not None and all('embedding' not in row for row in rows)
    else:
        assert matrix is None and all('embedding' in row for row in rows)
