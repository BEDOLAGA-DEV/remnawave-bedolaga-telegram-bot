import re
import time

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from ai_support_bot.app.core.config import ESCALATION_MARKER
from ai_support_bot.app.db import crud
from ai_support_bot.app.navigation import tool as navigation_tool
from ai_support_bot.app.services import settings_store
from ai_support_bot.app.services.knowledge_parser import compute_content_hash
from ai_support_bot.app.services.openai_client import openai_client
from ai_support_bot.app.services.rag_service import rag_service
from ai_support_bot.app.services.service_catalog import build_service_catalog, build_user_offers
from ai_support_bot.app.services.summary_service import summary_service
from ai_support_bot.app.services.user_data import build_user_context, resolve_user_language


logger = structlog.get_logger(__name__)

_ESCALATION_MARKER = ESCALATION_MARKER

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

_HEDGE_PATTERNS = (
    r'обычно',
    r'как правило',
    r'наверн\w*',
    r'скорее всего',
    r'вероятно',
    r'возможно',
    r'кажется',
    r'должно быть',
    r'по идее',
    r'вроде\b',
    r'предполага\w+',
    r'не уверен\w*',
    r'ориентировочно',
    r'примерно так',
    r'может быть',
)
_HEDGE_RE = re.compile(r'(?:' + '|'.join(_HEDGE_PATTERNS) + r')', re.IGNORECASE)

_FACTUAL_PATTERNS = (
    r'\bцен\w*', r'\bстоим\w*', r'\bстоит\b', r'\bтариф\w*', r'\bруб\w*', r'\bскидк\w*',
    r'\bсрок\w*', r'\bдней\b', r'\bдата\b', r'\bкогда\b', r'\bсколько\b',
    r'\bлимит\w*', r'\bустройств\w*', r'\bтрафик\w*', r'\bгб\b',
    r'\bвозврат\w*', r'\bоплат\w*', r'\bплатеж\w*', r'\bплатёж\w*', r'\bподписк\w*', r'\bбаланс\w*',
)
_FACTUAL_RE = re.compile(r'(?:' + '|'.join(_FACTUAL_PATTERNS) + r')', re.IGNORECASE)


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


def has_escalation_marker(answer_text: str) -> bool:
    """Marker counts only at the very end of the answer, so users can't trigger it mid-text."""
    if not answer_text:
        return False
    tail = answer_text.rstrip().rstrip('.!?)»"\'' + ' ')
    return tail.endswith(_ESCALATION_MARKER)


def strip_escalation_marker(answer_text: str) -> str:
    return (answer_text or '').replace(_ESCALATION_MARKER, '').strip()


def _role_label(role: str) -> str:
    return 'assistant' if role == 'assistant' else 'user'


def build_retrieval_query(question: str, history: list, summary: str | None, recent_count: int = 2) -> str:
    """Contextualize short follow-ups ("а для неё как?") before embedding the search query."""
    parts: list[str] = []

    if summary:
        parts.append(f'Контекст: {summary.strip()}')

    recent = history[-recent_count:] if (history and recent_count > 0) else []
    for item in recent:
        content = (getattr(item, 'content', '') or '').strip()
        if not content:
            continue
        parts.append(f'{_role_label(getattr(item, "role", "user"))}: {content[:400]}')

    parts.append(f'Текущий вопрос: {question.strip()}')
    return '\n'.join(parts)


def build_nav_query(question: str, history: list, summary: str | None, recent_count: int = 1) -> str:
    """Short follow-ups ("а где это в кабинете?") need the previous turn to resolve the section."""
    parts: list[str] = []

    if len((question or '').strip()) < 25:
        recent = history[-recent_count:] if (history and recent_count > 0) else []
        for item in recent:
            content = (getattr(item, 'content', '') or '').strip()
            if content:
                parts.append(content[:200])
        if not recent and summary:
            parts.append(summary.strip()[:200])

    parts.append((question or '').strip())
    return '\n'.join(part for part in parts if part)


def looks_hedged(answer_text: str, question: str, knowledge_used: int) -> bool:
    """True when the model answered a factual question with guess words and no knowledge backing."""
    if not answer_text or knowledge_used:
        return False
    if not _FACTUAL_RE.search(question or ''):
        return False
    return bool(_HEDGE_RE.search(answer_text))


class SupportAgent:
    def _build_system_prompt(
        self,
        knowledge: list[dict],
        user_context: str,
        summary: str | None,
        navigation: str = '',
        catalog: str = '',
    ) -> str:
        blocks = [settings_store.get('SYSTEM_PROMPT')]

        if summary:
            blocks.append(
                'Краткая сводка предыдущего диалога (справочный контекст, не активная задача):\n' + summary
            )

        if navigation:
            blocks.append(navigation)

        if catalog:
            blocks.append(catalog)

        if knowledge:
            examples = [
                f'Пример {idx}:\n{item["content"]}'
                for idx, item in enumerate(knowledge, start=1)
            ]
            blocks.append(
                'Примеры прошлых обращений и ответов поддержки (образец тона и решений, НЕ факты о клиенте):\n'
                + '\n\n'.join(examples)
            )
        else:
            blocks.append(
                'База знаний не дала релевантных примеров по этому вопросу. Опирайся только на «Данные текущего '
                'пользователя» и общеизвестные шаги. Если фактов не хватает — не гадай, эскалируй.'
            )

        if user_context:
            blocks.append('Данные текущего пользователя:\n' + user_context)

        blocks.append(
            'ПРАВИЛО ЭСКАЛАЦИИ: Если вопрос требует ручного вмешательства оператора (возврат денег, изменение '
            'подписки, жалоба, сложная техническая проблема, которую не решить советом) ИЛИ ты не знаешь точного '
            f'ответа — добавь в самом конце сообщения маркер {_ESCALATION_MARKER}. Маркер ставится строго последним '
            'символами ответа и нигде больше. Пользователю при этом напиши коротко, что вопрос уточняется.'
        )
        return '\n\n'.join(blocks)

    async def _build_navigation_block(
        self, telegram_id: int, question: str, history: list, summary: str | None
    ) -> str:
        if not settings_store.get_bool('NAVIGATION_ENABLED'):
            return ''

        min_chars = settings_store.get_int('NAVIGATION_MIN_QUESTION_CHARS') or 6
        stripped = (question or '').strip()
        if len(stripped) < min_chars:
            return ''

        query = build_nav_query(stripped, history, summary)
        language = await resolve_user_language(telegram_id)

        try:
            return await navigation_tool.build_prompt_block(
                query,
                language=language,
                limit=settings_store.get_int('NAVIGATION_TOP_K') or 3,
                depth=settings_store.get_int('NAVIGATION_DEPTH') or 2,
                max_children=settings_store.get_int('NAVIGATION_MAX_CHILDREN') or 8,
                max_chars=settings_store.get_int('NAVIGATION_MAX_CHARS') or 1400,
                ttl_seconds=settings_store.get_int('NAVIGATION_TTL'),
            )
        except Exception as error:
            logger.warning('Navigation block skipped', telegram_id=telegram_id, error=str(error))
            return ''

    async def _build_catalog_block(self, telegram_id: int) -> str:
        if not settings_store.get_bool('SERVICE_CATALOG_ENABLED'):
            return ''

        try:
            catalog = await build_service_catalog()
            offers = await build_user_offers(telegram_id)
        except Exception as error:
            logger.warning('Service catalog block skipped', telegram_id=telegram_id, error=str(error))
            return ''

        body = '\n'.join(part for part in (catalog, offers) if part)
        if not body:
            return ''

        max_chars = settings_store.get_int('SERVICE_CATALOG_MAX_CHARS') or 1600
        if max_chars > 0 and len(body) > max_chars:
            body = body[:max_chars].rstrip() + '\n  …'

        return (
            'Актуальные условия сервиса из базы (единственный достоверный источник цен, тарифов, '
            'промокодов и скидок):\n' + body
        )

    async def generate_answer(
        self, db: AsyncSession, telegram_id: int, question: str, image_url: str | None = None
    ) -> dict:
        await settings_store.load()

        max_question_chars = settings_store.get_int('MAX_QUESTION_CHARS') or 1500
        if len(question) > max_question_chars:
            question = question[:max_question_chars].rstrip() + '…'

        smalltalk = _is_smalltalk(question) and not image_url
        kb_min_chars = settings_store.get_int('KB_MIN_QUESTION_CHARS') or 6

        conversation = await crud.get_or_create_conversation(db, telegram_id)
        summary = conversation.summary

        context_limit = settings_store.get_int('CONTEXT_MESSAGES') or 12
        history = await crud.get_conversation_context(db, conversation.id, context_limit)

        knowledge: list[dict] = []
        if question.strip() and not smalltalk and len(question.strip()) >= kb_min_chars:
            recent_count = settings_store.get_int('RETRIEVAL_CONTEXT_MESSAGES')
            retrieval_query = build_retrieval_query(question, history, summary, recent_count=recent_count)
            knowledge = await rag_service.retrieve(db, retrieval_query)

        user_context = '' if smalltalk else await build_user_context(telegram_id)

        navigation_block = ''
        catalog_block = ''
        if not smalltalk:
            navigation_block = await self._build_navigation_block(telegram_id, question, history, summary)
            catalog_block = await self._build_catalog_block(telegram_id)

        system_prompt = self._build_system_prompt(
            knowledge, user_context, summary, navigation=navigation_block, catalog=catalog_block
        )

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
        if not image_url and question.strip() and not smalltalk and not history:
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

        escalate = has_escalation_marker(answer_text)
        answer_text = strip_escalation_marker(answer_text)

        hedged = False
        if not escalate and settings_store.get_bool('HEDGE_ESCALATION'):
            hedged = looks_hedged(answer_text, question, len(knowledge))
            if hedged:
                escalate = True
                notice = settings_store.get('ESCALATION_USER_NOTICE')
                answer_text = notice or 'Уточняю этот вопрос у оператора, подождите, пожалуйста.'
                logger.info('Hedged answer replaced with escalation notice', telegram_id=telegram_id)

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
            'hedged': hedged,
            'knowledge_used': len(knowledge),
            'model': model_used,
        }


support_agent = SupportAgent()
