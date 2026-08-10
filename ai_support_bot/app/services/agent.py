import re
import time

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from ai_support_bot.app.db import crud
from ai_support_bot.app.services import settings_store
from ai_support_bot.app.services.knowledge_parser import compute_content_hash
from ai_support_bot.app.services.openai_client import openai_client
from ai_support_bot.app.services.rag_service import rag_service
from ai_support_bot.app.services.summary_service import summary_service
from ai_support_bot.app.services.user_data import build_user_context


logger = structlog.get_logger(__name__)

_ESCALATION_MARKER = '[[ESCALATE]]'

_SMALLTALK_PATTERNS = (
    r'привет\w*', r'здравств\w*', r'хай', r'hi', r'hello', r'yo', r'здаров\w*',
    r'добр(ое|ый|ой)', r'утро', r'день', r'вечер', r'ночи',
    r'спасибо\w*', r'благодар\w*', r'спс', r'пасиб\w*', r'сяб', r'мерси',
    r'как', r'дела', r'ты', r'жизнь', r'настроение', r'сам', r'оно',
    r'что', r'нового', r'делаешь', r'поживаешь',
    r'ок\w*', r'окей', r'окай', r'k', r'понял\w*', r'ясно', r'хорошо', r'ладно',
    r'отлично', r'супер', r'класс', r'здорово', r'круто', r'бомба',
    r'пока', r'свидания', r'до', r'бб', r'споки', r'ага', r'угу', r'да', r'нет',
    r'большое', r'огромное', r'тебе', r'вам', r'вас', r'тебя', r'всем', r'братан', r'бро', r'дружище', r'друг',
    r'приятно', r'рад', r'взаимно', r'всё', r'все', r'работает', r'заработало', r'помогло',
    r'красава', r'красавчик', r'молодец', r'топ', r'машина', r'человек',
)
_SMALLTALK_RE = re.compile(r'^(?:' + '|'.join(_SMALLTALK_PATTERNS) + r')$', re.IGNORECASE)
_SMALLTALK_TOKEN_RE = re.compile(r"[a-zа-яё]+", re.IGNORECASE)


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


def convert_markdown_to_html(text: str) -> str:
    if not text:
        return text
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.*?)__', r'<b>\1</b>', text)
    text = re.sub(r'(?<!\w)\*([^*]+)\*(?!\w)', r'<i>\1</i>', text)
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    return text


def _is_smalltalk(text: str) -> bool:
    stripped = (text or '').strip()
    if not stripped or len(stripped) > 45:
        return False
    tokens = _SMALLTALK_TOKEN_RE.findall(stripped)
    if not tokens or len(tokens) > 6:
        return False
    return all(_SMALLTALK_RE.match(token) for token in tokens)


class SupportAgent:
    def _build_system_prompt(self, knowledge: list[dict], user_context: str, summary: str | None) -> str:
        blocks = [
            settings_store.get('SYSTEM_PROMPT'),
            'ПРАВИЛО ПРИВЕТСТВИЯ: Здоровайся ТОЛЬКО в первом сообщении диалога или после долгого перерыва. '
            'Если диалог уже идёт — не здоровайся повторно, сразу отвечай по сути.',
            'ПРАВИЛО КРАТКОСТИ: Не добавляй в конце дежурных фраз («Чем ещё помочь?», «Остались вопросы?» и т.п.). '
            'Уточняющий вопрос задавай, только если без него реально нельзя решить проблему — не более одного.',
            'ПРАВИЛО БЕЗОПАСНОСТИ ССЫЛОК: Персональные ссылки на подключение бери ИСКЛЮЧИТЕЛЬНО из «Данные текущего '
            'пользователя» (поле «ссылка=»). Ссылки из «Примеры прошлых обращений» — чужие, использовать запрещено. '
            'Если ссылки нет в данных пользователя — подскажи взять её в боте: «Профиль» → «Мои подключения», либо предложи оператора.',
        ]

        if summary:
            blocks.append('Краткая сводка предыдущего диалога (контекст, учитывай при ответе):\n' + summary)

        if knowledge:
            examples = [
                f'Пример {idx}:\n{item["content"]}'
                for idx, item in enumerate(knowledge, start=1)
            ]
            blocks.append(
                'Примеры прошлых обращений и ответов поддержки (образец тона и решений, НЕ факты о клиенте):\n'
                + '\n\n'.join(examples)
            )

        if user_context:
            blocks.append('Данные текущего пользователя:\n' + user_context)

        blocks.append(
            'Если вопрос требует ручного вмешательства оператора (возврат денег, изменение подписки, '
            f'жалоба, сложная техническая проблема, которую не решить советом) — добавь в самом конце маркер {_ESCALATION_MARKER}.'
        )
        return '\n\n'.join(blocks)

    async def generate_answer(
        self, db: AsyncSession, telegram_id: int, question: str, image_url: str | None = None
    ) -> dict:
        await settings_store.load()

        max_question_chars = settings_store.get_int('MAX_QUESTION_CHARS') or 1500
        if len(question) > max_question_chars:
            question = question[:max_question_chars].rstrip() + '…'

        smalltalk = _is_smalltalk(question) and not image_url
        kb_min_chars = settings_store.get_int('KB_MIN_QUESTION_CHARS') or 6

        knowledge: list[dict] = []
        if question.strip() and not smalltalk and len(question.strip()) >= kb_min_chars:
            knowledge = await rag_service.retrieve(db, question)

        user_context = '' if smalltalk else await build_user_context(telegram_id)

        conversation = await crud.get_or_create_conversation(db, telegram_id)
        summary = conversation.summary

        system_prompt = self._build_system_prompt(knowledge, user_context, summary)

        context_limit = settings_store.get_int('CONTEXT_MESSAGES') or 6
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
        cache_ttl = settings_store.get_int('RESPONSE_CACHE_TTL') or 900
        if not image_url and question.strip() and not smalltalk:
            cache_key = f'{telegram_id}:{compute_content_hash(question.strip().lower().encode("utf-8"))}'
            cached_answer = _response_cache.get(cache_key, ttl=cache_ttl)

        tokens_prompt = None
        tokens_completion = None
        if cached_answer is not None:
            answer_text = cached_answer
            model_used = model
        else:
            logger.info('Sending prompt to OpenAI', telegram_id=telegram_id, smalltalk=smalltalk, kb=len(knowledge))
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

        await crud.bump_user_turn(db, conversation.id)
        await db.flush()
        await summary_service.maybe_summarize(db, conversation)

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
