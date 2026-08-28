import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from ai_support_bot.app.db import crud
from ai_support_bot.app.services import settings_store
from ai_support_bot.app.services.openai_client import OpenAIError, openai_client


logger = structlog.get_logger(__name__)

_SUMMARY_INSTRUCTION = (
    'Ты сжимаешь диалог поддержки VPN-сервиса в короткую рабочую сводку для следующего оператора. '
    'Пиши на русском, только факты, без воды, без приветствий и без форматирования. '
    'Обнови сводку строго по шаблону (пропускай пустые пункты):\n'
    '• АКТИВНАЯ ТЕМА: тема последнего вопроса пользователя одной строкой\n'
    '• ЗАКРЫТО: темы, которые уже решены (через «;» — только справочно, к ним не возвращаться)\n'
    '• ОТКРЫТО: нерешённые вопросы и что ждём от пользователя\n'
    '• ФАКТЫ: конкретика, важная для дальнейших ответов (какая подписка обсуждалась, устройство, платёж)\n'
    'Максимум 6 строк. Если тема сменилась — АКТИВНАЯ ТЕМА всегда про последний вопрос, '
    'а прежняя тема переносится в ЗАКРЫТО или ОТКРЫТО.'
)


def _role_label(role: str) -> str:
    return 'Оператор' if role == 'assistant' else 'Пользователь'


class SummaryService:
    async def build_summary(
        self,
        db: AsyncSession,
        conversation_id: int,
        previous_summary: str | None,
        new_messages: list,
    ) -> str | None:
        if not new_messages:
            return previous_summary

        transcript_lines: list[str] = []
        for msg in new_messages:
            content = (msg.content or '').strip()
            if not content:
                continue
            transcript_lines.append(f'{_role_label(msg.role)}: {content}')
        if not transcript_lines:
            return previous_summary

        parts: list[str] = [_SUMMARY_INSTRUCTION]
        if previous_summary:
            parts.append('Текущая сводка:\n' + previous_summary)
        parts.append('Новые сообщения диалога:\n' + '\n'.join(transcript_lines))

        model = settings_store.get('SUMMARY_MODEL') or settings_store.get('MODEL')
        max_tokens = settings_store.get_int('SUMMARY_MAX_TOKENS') or 220

        try:
            result = await openai_client.chat_completion(
                messages=[
                    {'role': 'system', 'content': _SUMMARY_INSTRUCTION},
                    {'role': 'user', 'content': '\n\n'.join(parts[1:]) or '\n'.join(transcript_lines)},
                ],
                model=model,
                max_tokens=max_tokens,
                temperature=0.2,
            )
        except OpenAIError as error:
            logger.warning('Failed to build conversation summary', error=str(error))
            return previous_summary

        summary = (result.get('content') or '').strip()
        return summary or previous_summary

    async def maybe_summarize(self, db: AsyncSession, conversation) -> str | None:
        if not settings_store.get_bool('SUMMARY_ENABLED'):
            return conversation.summary

        every_n = settings_store.get_int('SUMMARY_EVERY_N_TURNS') or 3
        turns = conversation.user_turns_since_summary or 0
        if turns < every_n:
            return conversation.summary

        new_messages = await crud.get_messages_after_id(db, conversation.id, conversation.summarized_up_to_id or 0)
        if not new_messages:
            return conversation.summary

        total_messages = await crud.count_conversation_messages(db, conversation.id)
        up_to_id = new_messages[-1].id
        summary = await self.build_summary(db, conversation.id, conversation.summary, new_messages)
        if summary:
            await crud.save_summary(db, conversation.id, summary, total_messages, up_to_id=up_to_id)
            logger.info(
                'Conversation summary refreshed',
                conversation_id=conversation.id,
                new_messages=len(new_messages),
            )
        return summary


summary_service = SummaryService()
