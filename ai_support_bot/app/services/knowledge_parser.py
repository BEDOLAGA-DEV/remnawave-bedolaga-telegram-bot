import hashlib
import re
from typing import Any


_SUPPORT_ROLE_MARKERS = (
    'поддержка',
    'support',
    'оператор',
    'operator',
    'admin',
    'администратор',
    'bot',
    'бот',
)

_WHITESPACE_RE = re.compile(r'[ \t]+')
_MULTI_NEWLINE_RE = re.compile(r'\n{3,}')


def _flatten_text(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get('type') == 'custom_emoji':
                    continue
                parts.append(str(item.get('text', '')))
        return ''.join(parts)
    if isinstance(value, dict):
        return str(value.get('text', ''))
    return str(value)


def _clean(text: str) -> str:
    text = text.replace('\r', '')
    text = _WHITESPACE_RE.sub(' ', text)
    text = _MULTI_NEWLINE_RE.sub('\n\n', text)
    return text.strip()


def _is_support(from_name: str) -> bool:
    lowered = (from_name or '').lower()
    return any(marker in lowered for marker in _SUPPORT_ROLE_MARKERS)


def _extract_messages(data: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []

    if isinstance(data.get('messages'), list):
        messages.extend(data['messages'])

    chats = data.get('chats')
    if isinstance(chats, dict):
        for chat in chats.get('list', []) or []:
            if isinstance(chat, dict) and isinstance(chat.get('messages'), list):
                messages.extend(chat['messages'])
    elif isinstance(chats, list):
        for chat in chats:
            if isinstance(chat, dict) and isinstance(chat.get('messages'), list):
                messages.extend(chat['messages'])

    return messages


def _normalize_message(raw: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    if raw.get('type') != 'message':
        return None
    text = _clean(_flatten_text(raw.get('text')))
    if not text:
        return None
    from_name = str(raw.get('from') or '')
    return {'from': from_name, 'is_support': _is_support(from_name), 'text': text}


_STUB_PATTERNS = (
    'здравствуйте', 'добрый день', 'добрый вечер', 'доброе утро', 'привет', 'приветствую',
    'минутку', 'секунду', 'секундочку', 'ожидайте', 'щас', 'сейчас', 'один момент', 'одну секунду'
)


def _is_stub_answer(text: str) -> bool:
    cleaned = _clean(text).lower()
    if len(cleaned) < 25:
        return True
    for pattern in _STUB_PATTERNS:
        cleaned = cleaned.replace(pattern, '')
    cleaned = cleaned.strip('!.,? \n\r')
    return len(cleaned) < 15


def _build_qa_pairs(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    pairs: list[dict[str, str]] = []
    pending_questions: list[str] = []
    pending_answers: list[str] = []

    def flush() -> None:
        if pending_questions and pending_answers:
            q_text = _clean('\n'.join(pending_questions))
            a_text = _clean('\n'.join(pending_answers))
            if q_text and a_text and not _is_stub_answer(a_text):
                pairs.append({'question': q_text, 'answer': a_text})

    prev_is_support: bool | None = None
    for message in messages:
        is_support = message['is_support']
        text = message['text']
        if not is_support:
            current_answer = '\n'.join(pending_answers)
            if prev_is_support and pending_answers and not _is_stub_answer(current_answer):
                flush()
                pending_questions = []
                pending_answers = []

            if len(_clean(text)) >= 2 or not pending_questions:
                pending_questions.append(text)
        else:
            pending_answers.append(text)
        prev_is_support = is_support

    flush()
    return pairs


def _try_parse_direct_qa(data: Any) -> list[dict[str, str]]:
    items = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ('faq', 'qa_pairs', 'items', 'data', 'knowledge', 'questions'):
            if isinstance(data.get(key), list):
                items = data[key]
                break

    pairs: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        q = _clean(
            _flatten_text(
                item.get('question')
                or item.get('q')
                or item.get('prompt')
                or item.get('вопрос')
                or item.get('title')
            )
        )
        a = _clean(
            _flatten_text(
                item.get('answer')
                or item.get('a')
                or item.get('response')
                or item.get('ответ')
                or item.get('content')
            )
        )
        if q and a:
            pairs.append({'question': q, 'answer': a})
    return pairs


def parse_knowledge_file(data: Any) -> tuple[list[dict[str, str]], int]:
    # 1. Try direct Q&A pairs (array or object containing FAQ items)
    direct_pairs = _try_parse_direct_qa(data)
    if direct_pairs:
        return direct_pairs, len(direct_pairs)

    # 2. Telegram Export Parsing
    if isinstance(data, dict):
        raw_messages = _extract_messages(data)
        normalized = [m for m in (_normalize_message(item) for item in raw_messages) if m]
        pairs = _build_qa_pairs(normalized)

        # Fallback if no support marker matched from_name: alternate between users
        if not pairs and len(normalized) >= 2:
            # Check if any messages were identified as support
            has_support = any(m['is_support'] for m in normalized)
            if not has_support:
                # Group by sender switching
                fallback_normalized = []
                first_sender = normalized[0]['from']
                for m in normalized:
                    m_copy = dict(m)
                    m_copy['is_support'] = m['from'] != first_sender
                    fallback_normalized.append(m_copy)
                pairs = _build_qa_pairs(fallback_normalized)

        return pairs, len(normalized)

    return [], 0


_NOISE_MARKERS = (
    'реферал', 'сотрудничеств', 'инстаграм', 'инста ', 'рилс', 'охват', 'аудитори',
    'канал', 'блогер', 'закрепля', 'промокод «', 'процент', 'выплат', 'партнёр', 'партнер',
    'бро', 'братец', 'братан', 'красавчик', 'работаем', 'сработаемся', 'взаимно',
)


def _is_low_value(question: str, answer: str) -> bool:
    q = question.lower()
    a = answer.lower()
    if _is_stub_answer(answer):
        return True
    if len(question.strip()) < 4:
        return True
    hits = sum(1 for marker in _NOISE_MARKERS if marker in q or marker in a)
    return hits >= 2


def build_chunks(pairs: list[dict[str, str]], max_chars: int, drop_low_value: bool = True) -> list[dict[str, str]]:
    chunks: list[dict[str, str]] = []
    for pair in pairs:
        question = pair['question'].strip()
        answer = pair['answer'].strip()
        if not question or not answer:
            continue
        if drop_low_value and _is_low_value(question, answer):
            continue
        if len(answer) > max_chars:
            answer = answer[:max_chars].rstrip()
        content = f'Вопрос: {question}\nОтвет: {answer}'
        chunk_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()
        chunks.append({'content': content, 'question': question, 'answer': answer, 'chunk_hash': chunk_hash})
    return chunks


def compute_content_hash(raw_bytes: bytes) -> str:
    return hashlib.sha256(raw_bytes).hexdigest()
