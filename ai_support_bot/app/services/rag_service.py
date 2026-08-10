import math
import time

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from ai_support_bot.app.core.config import settings
from ai_support_bot.app.db import crud
from ai_support_bot.app.db.models import KnowledgeChunk
from ai_support_bot.app.services import settings_store
from ai_support_bot.app.services.knowledge_parser import build_chunks, compute_content_hash, parse_knowledge_file
from ai_support_bot.app.services.openai_client import OpenAIError, openai_client


logger = structlog.get_logger(__name__)

_EMBED_BATCH_SIZE = 64


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


class RAGService:
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

        await crud.update_source_counts(db, source.id, chunk_count=stored, message_count=message_count)
        await db.commit()

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

    async def retrieve(self, db: AsyncSession, query: str) -> list[dict]:
        if not query.strip():
            return []
        try:
            embedding_model = settings_store.get('EMBEDDING_MODEL')
            query_embedding = await openai_client.create_embedding(query, embedding_model)
        except OpenAIError as error:
            logger.warning('Failed to embed query', error=str(error))
            return []

        started = time.monotonic()
        chunks = await crud.get_active_chunks(db)
        if not chunks:
            return []

        min_score = settings_store.get_float('MIN_SCORE')
        top_k = settings_store.get_int('TOP_K') or 5

        scored: list[tuple[float, KnowledgeChunk]] = []
        for chunk in chunks:
            score = _cosine(query_embedding, chunk.embedding or [])
            if score >= min_score:
                scored.append((score, chunk))

        scored.sort(key=lambda item: item[0], reverse=True)
        top = scored[:top_k]

        logger.debug(
            'retrieval done',
            total=len(chunks),
            matched=len(top),
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
        )
        return [
            {'score': round(score, 4), 'question': chunk.question, 'answer': chunk.answer, 'content': chunk.content}
            for score, chunk in top
        ]


rag_service = RAGService()
