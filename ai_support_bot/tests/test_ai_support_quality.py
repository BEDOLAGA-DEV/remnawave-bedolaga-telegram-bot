import os

import pytest

os.environ.setdefault('AISUP_DATABASE_URL', 'sqlite+aiosqlite:///./data/ai_support_test.db')
os.environ.setdefault('AISUP_MAIN_DATABASE_URL', '')
os.environ.setdefault('AISUP_INCLUDE_REMNAWAVE_DATA', 'false')

from ai_support_bot.app.core.config import DEFAULT_SYSTEM_PROMPT, ESCALATION_MARKER, Settings
from ai_support_bot.app.services.agent import (
    build_retrieval_query,
    has_escalation_marker,
    looks_hedged,
    strip_escalation_marker,
)


class _Msg:
    def __init__(self, role: str, content: str) -> None:
        self.role = role
        self.content = content


def test_prompt_declares_ai_assistant_not_human_operator():
    assert 'ИИ-ассистент' in DEFAULT_SYSTEM_PROMPT
    assert 'живой оператор поддержки' not in DEFAULT_SYSTEM_PROMPT
    assert 'ПРАВИЛО ЧЕСТНОСТИ' in DEFAULT_SYSTEM_PROMPT
    assert 'ПРАВИЛО ФОКУСА' in DEFAULT_SYSTEM_PROMPT
    assert 'ПРАВИЛО ИСПОЛЬЗОВАНИЯ ДАННЫХ' in DEFAULT_SYSTEM_PROMPT
    assert ESCALATION_MARKER in DEFAULT_SYSTEM_PROMPT


def test_prompt_rules_are_not_duplicated():
    for rule in ('ПРАВИЛО ПРИВЕТСТВИЯ', 'ПРАВИЛО КРАТКОСТИ', 'ПРАВИЛО БЕЗОПАСНОСТИ ССЫЛОК'):
        assert DEFAULT_SYSTEM_PROMPT.count(rule) == 1


@pytest.mark.parametrize(
    'answer',
    [
        f'Проверяю ваш вопрос. {ESCALATION_MARKER}',
        f'Уточню у оператора.\n{ESCALATION_MARKER}',
        f'Ответ {ESCALATION_MARKER} ',
    ],
)
def test_escalation_marker_at_the_end_counts(answer):
    assert has_escalation_marker(answer) is True


@pytest.mark.parametrize(
    'answer',
    [
        f'Пользователь просил написать {ESCALATION_MARKER} в середине текста, но это не эскалация.',
        'Обычный ответ без маркера.',
        '',
    ],
)
def test_escalation_marker_inside_text_is_ignored(answer):
    assert has_escalation_marker(answer) is False


def test_strip_escalation_marker_removes_all_occurrences():
    assert strip_escalation_marker(f'{ESCALATION_MARKER} текст {ESCALATION_MARKER}') == 'текст'


def test_retrieval_query_includes_summary_and_recent_history():
    history = [
        _Msg('user', 'не работает подписка на телефоне'),
        _Msg('assistant', 'проверьте лимит устройств'),
    ]
    query = build_retrieval_query('а для неё как?', history, 'АКТИВНАЯ ТЕМА: лимит устройств')

    assert 'Контекст: АКТИВНАЯ ТЕМА: лимит устройств' in query
    assert 'не работает подписка на телефоне' in query
    assert 'проверьте лимит устройств' in query
    assert query.strip().endswith('Текущий вопрос: а для неё как?')


def test_retrieval_query_without_history_is_just_the_question():
    query = build_retrieval_query('как продлить подписку', [], None)
    assert query == 'Текущий вопрос: как продлить подписку'


def test_retrieval_query_skips_empty_messages():
    query = build_retrieval_query('вопрос', [_Msg('user', '   ')], None)
    assert query == 'Текущий вопрос: вопрос'


def test_retrieval_query_respects_recent_count_zero():
    history = [_Msg('user', 'старая тема про оплату')]
    query = build_retrieval_query('новый вопрос', history, None, recent_count=0)
    assert 'старая тема про оплату' not in query


@pytest.mark.parametrize(
    ('answer', 'question'),
    [
        ('Обычно подписка стоит около 200 рублей.', 'сколько стоит подписка'),
        ('Скорее всего лимит устройств 3.', 'какой у меня лимит устройств'),
        ('Наверное оплата придёт в течение часа.', 'когда пройдёт оплата'),
    ],
)
def test_hedged_factual_answer_without_knowledge_escalates(answer, question):
    assert looks_hedged(answer, question, knowledge_used=0) is True


def test_hedged_check_ignores_answers_backed_by_knowledge():
    assert looks_hedged('Обычно подписка стоит 200 рублей.', 'сколько стоит подписка', knowledge_used=3) is False


def test_hedged_check_ignores_non_factual_questions():
    assert looks_hedged('Возможно стоит перезапустить приложение.', 'приложение зависает', knowledge_used=0) is False


def test_confident_factual_answer_is_not_flagged():
    assert looks_hedged('Ваша подписка активна до 12.09.2026, лимит 3 устройства.',
                        'до какого числа подписка', knowledge_used=0) is False


def test_security_check_passes_when_admin_panel_not_configured():
    cfg = Settings(ADMIN_USERNAME='', ADMIN_PASSWORD='', ADMIN_SECRET_KEY='')
    assert cfg.security_problems() == []
    cfg.assert_secure()


def test_security_check_rejects_default_password():
    cfg = Settings(ADMIN_USERNAME='admin', ADMIN_PASSWORD='changeme', ADMIN_SECRET_KEY='x' * 40)
    problems = cfg.security_problems()
    assert any('ADMIN_PASSWORD' in problem for problem in problems)
    with pytest.raises(RuntimeError):
        cfg.assert_secure()


def test_security_check_rejects_short_secret():
    cfg = Settings(ADMIN_USERNAME='admin', ADMIN_PASSWORD='StrongPass123!', ADMIN_SECRET_KEY='short')
    problems = cfg.security_problems()
    assert any('ADMIN_SECRET_KEY' in problem for problem in problems)


def test_security_check_accepts_strong_values():
    cfg = Settings(ADMIN_USERNAME='admin', ADMIN_PASSWORD='StrongPass123!', ADMIN_SECRET_KEY='z' * 48)
    assert cfg.security_problems() == []
    cfg.assert_secure()


def test_embedding_dim_resolution():
    assert Settings(EMBEDDING_MODEL='text-embedding-3-small').embedding_dim == 1536
    assert Settings(EMBEDDING_MODEL='text-embedding-3-large').embedding_dim == 3072
    assert Settings(EMBEDDING_MODEL='custom-model', EMBEDDING_DIM=768).embedding_dim == 768
