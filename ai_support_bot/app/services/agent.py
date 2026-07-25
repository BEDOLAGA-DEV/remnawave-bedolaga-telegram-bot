import time

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from ai_support_bot.app.db import crud
from ai_support_bot.app.services import settings_store
from ai_support_bot.app.services.knowledge_parser import compute_content_hash
from ai_support_bot.app.services.openai_client import OpenAIError, openai_client
from ai_support_bot.app.services.rag_service import rag_service
from ai_support_bot.app.services.user_data import build_user_context


logger = structlog.get_logger(__name__)

_ESCALATION_MARKER = '[[ESCALATE]]'


class _ResponseCache:
    def __init__(self) -> None:
        self._store: dict[str, tuple[float, str]] = {}

    def get(self, key: str, ttl: int) -> str | None:
        entry = self._store.get(key)
        if not entry:
            return None
        created, value = entry
        if time.time() - created > ttl:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: str) -> None:
        if len(self._store) > 2000:
            self._store.clear()
        self._store[key] = (time.time(), value)


_response_cache = _ResponseCache()


import re


def convert_markdown_to_html(text: str) -> str:
    if not text:
        return text
    # Convert **text** -> <b>text</b>
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    # Convert __text__ -> <b>text</b>
    text = re.sub(r'__(.*?)__', r'<b>\1</b>', text)
    # Convert *text* -> <i>text</i>
    text = re.sub(r'(?<!\w)\*([^*]+)\*(?!\w)', r'<i>\1</i>', text)
    # Convert `code` -> <code>code</code>
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    return text


class SupportAgent:
    def _build_system_prompt(self, knowledge: list[dict], user_context: str) -> str:
        blocks = [
            settings_store.get('SYSTEM_PROMPT'),
            'ПРАВИЛО ПРИВЕТСТВИЯ: Поздоровайся (например, "Здравствуйте!") ТОЛЬКО если это самое первое сообщение в диалоге или между сообщениями прошел большой перерыв. Если в истории сообщений ниже уже есть диалог с пользователем, НЕ здоровайся повторно в каждом ответе, а сразу отвечай на вопрос.',
            'ПРАВИЛО БЕЗ ЛИШНИХ ВОПРОСОВ И НАВЯЗЧИВОСТИ: Отвечай строго по существу. Категорически НЕ добавляй в конце ответа риторические отбивки ("Чем еще помочь?", "Остались ли вопросы?", "Напишите, если возникнут сложности" и т.д.). Задавай уточняющие вопросы ТОЛЬКО тогда, когда без этой информации невозможно решить проблему.',
            'ОБЯЗАТЕЛЬНОЕ ПРАВИЛО ФОРМАТИРОВАНИЯ: Используй ТОЛЬКО HTML-теги Telegram (<b>жирный</b>, <i>курсив</i>, <code>код</code>). Категорически запрещено использовать Markdown-разметку (**звёздочки**, __подчёркивания__ или ```).',
            'КРИТИЧЕСКОЕ ПРАВИЛО БЕЗОПАСНОСТИ ССЫЛОК: Категорически запрещено брать, подставлять или выдумывать любые ссылки на подключение из раздела "Примеры прошлых обращений"! Все персональные ссылки пользователя берутся ИСКЛЮЧИТЕЛЬНО из раздела "Данные текущего пользователя" (поле "ссылка="). Если у пользователя в "Данных текущего пользователя" нет ссылки для запрашиваемого тарифа, вежливо объясни, что получить свою ссылку он может в боте в меню «Профиль» -> «Мои подключения», либо предложи позвать оператора.'
        ]

        if knowledge:
            examples = [
                f'Пример {idx} (релевантность {item["score"]}):\n{item["content"]}'
                for idx, item in enumerate(knowledge, start=1)
            ]
            blocks.append(
                'Примеры прошлых обращений и ответов поддержки (используй как образец):\n'
                + '\n\n'.join(examples)
            )

        if user_context:
            blocks.append('Данные текущего пользователя:\n' + user_context)

        blocks.append(
            'Если вопрос требует ручного вмешательства оператора (возвраты денег, сложные '
            f'технические проблемы, жалобы), добавь в конце ответа маркер {_ESCALATION_MARKER}.'
        )
        return '\n\n'.join(blocks)

    async def generate_answer(
        self, db: AsyncSession, telegram_id: int, question: str, image_url: str | None = None
    ) -> dict:
        knowledge = await rag_service.retrieve(db, question) if question.strip() else []
        user_context = await build_user_context(telegram_id)
        system_prompt = self._build_system_prompt(knowledge, user_context)

        conversation = await crud.get_or_create_conversation(db, telegram_id)
        context_limit = settings_store.get_int('CONTEXT_MESSAGES') or 8
        history = await crud.get_conversation_context(db, conversation.id, context_limit)

        messages: list[dict] = [{'role': 'system', 'content': system_prompt}]
        for item in history:
            role = 'assistant' if item.role == 'assistant' else 'user'
            messages.append({'role': role, 'content': item.content})

        vision_enabled = settings_store.get_bool('VISION_ENABLED')
        if image_url and vision_enabled:
            messages.append(
                {
                    'role': 'user',
                    'content': [
                        {
                            'type': 'text',
                            'text': question or 'Проанализируй изображение и помоги пользователю.',
                        },
                        {'type': 'image_url', 'image_url': {'url': image_url}},
                    ],
                }
            )
        else:
            messages.append({'role': 'user', 'content': question})

        model = settings_store.get('MODEL')
        max_tokens = settings_store.get_int('MAX_TOKENS') or 700
        temperature = settings_store.get_float('TEMPERATURE')

        cache_key = None
        cached_answer = None
        if not image_url and question.strip():
            cache_key = f'{telegram_id}:{compute_content_hash(question.strip().lower().encode("utf-8"))}'
            cached_answer = _response_cache.get(cache_key, ttl=900)

        tokens_prompt = None
        tokens_completion = None
        if cached_answer is not None:
            answer_text = cached_answer
            model_used = model
        else:
            logger.info('Sending prompt to OpenAI', telegram_id=telegram_id, system_prompt=system_prompt, messages=messages)
            result = await openai_client.chat_completion(
                messages=messages, model=model, max_tokens=max_tokens, temperature=temperature
            )
            answer_text = result['content']
            model_used = result['model']
            tokens_prompt = result['tokens_prompt']
            tokens_completion = result['tokens_completion']
            if cache_key and answer_text:
                _response_cache.set(cache_key, answer_text)

        escalate = _ESCALATION_MARKER in answer_text
        answer_text = answer_text.replace(_ESCALATION_MARKER, '').strip()
        answer_text = convert_markdown_to_html(answer_text)

        await crud.add_message(
            db,
            conversation_id=conversation.id,
            telegram_id=telegram_id,
            role='user',
            content=question or '[изображение]',
            has_media=bool(image_url),
            media_type='photo' if image_url else None,
        )
        await crud.add_message(
            db,
            conversation_id=conversation.id,
            telegram_id=telegram_id,
            role='assistant',
            content=answer_text,
            model=model_used,
            tokens_prompt=tokens_prompt,
            tokens_completion=tokens_completion,
            used_context=[{'score': item['score'], 'question': item['question']} for item in knowledge] or None,
        )

        if escalate:
            await crud.mark_escalated(db, conversation.id)

        history_limit = settings_store.get_int('HISTORY_LIMIT') or 100
        await crud.prune_messages(db, history_limit)
        await db.commit()

        return {
            'answer': answer_text,
            'escalate': escalate,
            'knowledge_used': len(knowledge),
            'model': model_used,
        }


support_agent = SupportAgent()
