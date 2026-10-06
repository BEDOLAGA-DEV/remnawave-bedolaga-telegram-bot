import math
import time

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_support_bot.app.core.config import settings
from ai_support_bot.app.db import crud, database as db_module
from ai_support_bot.app.db.models import KnowledgeChunk
from ai_support_bot.app.services import settings_store
from ai_support_bot.app.services.knowledge_parser import build_chunks, compute_content_hash, parse_knowledge_file
from ai_support_bot.app.services.openai_client import OpenAIError, openai_client


logger = structlog.get_logger(__name__)

_EMBED_BATCH_SIZE = 64
_CHUNK_CACHE_TTL = 120

try:
    import numpy as _np
except ImportError:
    _np = None


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def _to_vector_literal(embedding: list[float]) -> str:
    return '[' + ','.join(f'{float(value):.7g}' for value in embedding) + ']'


class _EmbeddingCache:
    def __init__(self) -> None:
        self._store: dict[str, tuple[float, list[float]]] = {}

    def get(self, key: str, ttl: int) -> list[float] | None:
        if ttl <= 0:
            return None
        entry = self._store.get(key)
        if not entry:
            return None
        created, value = entry
        if time.time() - created > ttl:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: list[float]) -> None:
        if len(self._store) > 1000:
            self._store.clear()
        self._store[key] = (time.time(), value)


class _ChunkMatrixCache:
    def __init__(self) -> None:
        self._created = 0.0
        self._rows: list[dict] | None = None
        self._matrix = None

    def invalidate(self) -> None:
        self._created = 0.0
        self._rows = None
        self._matrix = None

    def get(self):
        if self._rows is None or time.time() - self._created > _CHUNK_CACHE_TTL:
            return None, None
        return self._rows, self._matrix

    def set(self, rows: list[dict], matrix) -> None:
        self._rows = rows
        self._matrix = matrix
        self._created = time.time()


_embedding_cache = _EmbeddingCache()
_chunk_cache = _ChunkMatrixCache()


class RAGService:
    def invalidate_cache(self) -> None:
        _chunk_cache.invalidate()

    async def _embed_query(self, query: str) -> list[float] | None:
        embedding_model = settings_store.get('EMBEDDING_MODEL') or settings.EMBEDDING_MODEL
        cache_ttl = settings_store.get_int('EMBEDDING_CACHE_TTL')
        cache_key = f'{embedding_model}:{compute_content_hash(query.strip().lower().encode("utf-8"))}'

        cached = _embedding_cache.get(cache_key, ttl=cache_ttl)
        if cached is not None:
            return cached

        try:
            embedding = await openai_client.create_embedding(query, embedding_model)
        except OpenAIError as error:
            logger.warning('Failed to embed query', error=str(error))
            return None

        if embedding:
            _embedding_cache.set(cache_key, embedding)
        return embedding

    async def ingest_file(self, db: AsyncSession, filename: str, raw_bytes: bytes, parsed_data: dict) -> dict:
        content_hash = compute_content_hash(raw_bytes)
        existing_source = await crud.get_source_by_hash(db, content_hash)
        if existing_source:
            return {
                'status': 'duplicate',
                'source_id': existing_source.id,
                'chunk_count': existing_source.chunk_count,
                'message_count': existing_source.message_count,
            }

        pairs, message_count = parse_knowledge_file(parsed_data)
        chunk_max_chars = settings_store.get_int('CHUNK_MAX_CHARS') or 1200
        drop_low_value = settings_store.get_bool('KB_DROP_LOW_VALUE')
        candidate_chunks = build_chunks(pairs, chunk_max_chars, drop_low_value=drop_low_value)
        if not candidate_chunks:
            raise ValueError('В файле не найдено пар вопрос-ответ для базы знаний')

        existing_hashes = await crud.get_existing_chunk_hashes(db)
        unique_chunks: list[dict] = []
        seen: set[str] = set()
        for chunk in candidate_chunks:
            h = chunk['chunk_hash']
            if h in existing_hashes or h in seen:
                continue
            seen.add(h)
            unique_chunks.append(chunk)

        title = parsed_data.get('about') if isinstance(parsed_data.get('about'), str) else None
        if title:
            title = title[:250]

        source = await crud.create_source(db, filename=filename, content_hash=content_hash, title=title)

        embedding_model = settings_store.get('EMBEDDING_MODEL') or settings.EMBEDDING_MODEL or 'text-embedding-3-small'
        stored = 0
        for start in range(0, len(unique_chunks), _EMBED_BATCH_SIZE):
            batch = unique_chunks[start : start + _EMBED_BATCH_SIZE]
            texts = [item['content'] for item in batch]
            embeddings = await openai_client.create_embeddings(texts, embedding_model)
            models = [
                KnowledgeChunk(
                    source_id=source.id,
                    chunk_hash=item['chunk_hash'],
                    content=item['content'],
                    question=item['question'],
                    answer=item['answer'],
                    embedding=embedding,
                )
                for item, embedding in zip(batch, embeddings)
            ]
            stored += await crud.add_chunks(db, models)
            await self._sync_vector_column(db, models)

        await crud.update_source_counts(db, source.id, chunk_count=stored, message_count=message_count)
        await db.commit()
        self.invalidate_cache()

        logger.info(
            'knowledge ingested', filename=filename, chunks=stored, skipped=len(candidate_chunks) - stored
        )
        return {
            'status': 'ok',
            'source_id': source.id,
            'chunk_count': stored,
            'message_count': message_count,
            'skipped_duplicates': len(candidate_chunks) - stored,
        }

    async def _sync_vector_column(self, db: AsyncSession, chunks: list[KnowledgeChunk]) -> None:
        if not self._pgvector_active() or not chunks:
            return
        try:
            for chunk in chunks:
                if not chunk.embedding:
                    continue
                await db.execute(
                    text(
                        'UPDATE knowledge_chunks SET embedding_vec = CAST(:vec AS vector) WHERE id = :cid'
                    ),
                    {'vec': _to_vector_literal(chunk.embedding), 'cid': chunk.id},
                )
        except Exception as error:
            logger.warning('Failed to sync pgvector column', error=str(error))

    def _pgvector_active(self) -> bool:
        return bool(db_module.pgvector_ready and settings_store.get_bool('PGVECTOR_ENABLED'))

    async def _retrieve_pgvector(
        self, db: AsyncSession, query_embedding: list[float], top_k: int, min_score: float
    ) -> list[dict] | None:
        try:
            result = await db.execute(
                text(
                    'SELECT c.question, c.answer, c.content, '
                    '1 - (c.embedding_vec <=> CAST(:vec AS vector)) AS score '
                    'FROM knowledge_chunks c '
                    'JOIN knowledge_sources s ON s.id = c.source_id '
                    'WHERE s.is_active IS TRUE AND c.embedding_vec IS NOT NULL '
                    'ORDER BY c.embedding_vec <=> CAST(:vec AS vector) '
                    'LIMIT :limit'
                ),
                {'vec': _to_vector_literal(query_embedding), 'limit': max(top_k, 1)},
            )
            rows = result.mappings().all()
        except Exception as error:
            logger.warning('pgvector search failed, falling back to in-memory search', error=str(error))
            db_module.pgvector_ready = False
            return None

        return [
            {
                'score': round(float(row['score']), 4),
                'question': row['question'],
                'answer': row['answer'],
                'content': row['content'],
            }
            for row in rows
            if float(row['score']) >= min_score
        ]

    async def _retrieve_in_memory(
        self, db: AsyncSession, query_embedding: list[float], top_k: int, min_score: float
    ) -> list[dict]:
        rows, matrix = _chunk_cache.get()
        if rows is None:
            chunks = await crud.get_active_chunks(db)
            rows = [
                {
                    'question': chunk.question,
                    'answer': chunk.answer,
                    'content': chunk.content,
                    'embedding': chunk.embedding or [],
                }
                for chunk in chunks
                if chunk.embedding
            ]
            matrix = None
            if _np is not None and rows:
                dims = len(rows[0]['embedding'])
                usable = [row for row in rows if len(row['embedding']) == dims]
                if len(usable) == len(rows):
                    matrix = _np.asarray([row['embedding'] for row in rows], dtype=_np.float32)
                    norms = _np.linalg.norm(matrix, axis=1, keepdims=True)
                    norms[norms == 0] = 1.0
                    matrix = matrix / norms
            _chunk_cache.set(rows, matrix)

        if not rows:
            return []

        scored: list[tuple[float, dict]] = []
        if matrix is not None:
            query_vec = _np.asarray(query_embedding, dtype=_np.float32)
            query_norm = float(_np.linalg.norm(query_vec)) or 1.0
            similarities = matrix @ (query_vec / query_norm)
            limit = min(max(top_k, 1), len(rows))
            best_indices = _np.argpartition(-similarities, limit - 1)[:limit]
            for index in best_indices:
                score = float(similarities[int(index)])
                if score >= min_score:
                    scored.append((score, rows[int(index)]))
        else:
            for row in rows:
                score = _cosine(query_embedding, row['embedding'])
                if score >= min_score:
                    scored.append((score, row))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            {
                'score': round(score, 4),
                'question': row['question'],
                'answer': row['answer'],
                'content': row['content'],
            }
            for score, row in scored[:top_k]
        ]

    async def retrieve(self, db: AsyncSession, query: str) -> list[dict]:
        if not query.strip():
            return []

        query_embedding = await self._embed_query(query)
        if not query_embedding:
            return []

        started = time.monotonic()
        min_score = settings_store.get_float('MIN_SCORE')
        top_k = settings_store.get_int('TOP_K') or 5

        matches: list[dict] | None = None
        backend = 'memory'
        if self._pgvector_active():
            matches = await self._retrieve_pgvector(db, query_embedding, top_k, min_score)
            if matches is not None:
                backend = 'pgvector'

        if matches is None:
            matches = await self._retrieve_in_memory(db, query_embedding, top_k, min_score)

        logger.debug(
            'retrieval done',
            backend=backend,
            matched=len(matches),
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
        )
        return matches


rag_service = RAGService()
