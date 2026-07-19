from datetime import UTC, datetime

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ServiceRule


logger = structlog.get_logger(__name__)


async def get_rules_by_language(db: AsyncSession, language: str = 'ru') -> ServiceRule | None:
    result = await db.execute(
        select(ServiceRule)
        .where(ServiceRule.language == language, ServiceRule.is_active == True)
        .order_by(ServiceRule.order, ServiceRule.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def create_or_update_rules(
    db: AsyncSession, content: str, language: str = 'ru', title: str = 'Правила сервиса'
) -> ServiceRule:
    existing_rules_result = await db.execute(
        select(ServiceRule).where(ServiceRule.language == language, ServiceRule.is_active == True)
    )
    existing_rules = existing_rules_result.scalars().all()

    for rule in existing_rules:
        rule.is_active = False
        rule.updated_at = datetime.now(UTC)

    new_rules = ServiceRule(title=title, content=content, language=language, is_active=True, order=0)

    db.add(new_rules)
    await db.commit()
    await db.refresh(new_rules)

    logger.info('✅ Правила обновлены', language=language, new_rules_id=new_rules.id)
    return new_rules


async def clear_all_rules(db: AsyncSession, language: str = 'ru') -> bool:
    try:
        result = await db.execute(
            update(ServiceRule)
            .where(ServiceRule.language == language, ServiceRule.is_active == True)
            .values(is_active=False, updated_at=datetime.now(UTC))
        )

        await db.commit()

        rows_affected = result.rowcount
        logger.info(
            '✅ Очищены правила для языка . Деактивировано записей', language=language, rows_affected=rows_affected
        )

        return rows_affected > 0

    except Exception as e:
        logger.error('❌ Ошибка при очистке правил для языка', language=language, error=e)
        await db.rollback()
        raise


async def get_current_rules_content(db: AsyncSession, language: str = 'ru') -> str:
    rules = await get_rules_by_language(db, language)

    if rules:
        return rules.content
    return """
🔒 <b>Правила использования сервиса</b>

1. Сервис предоставляется "как есть" без каких-либо гарантий.

2. Запрещается использование сервиса для незаконных действий.

3. Администрация оставляет за собой право заблокировать доступ пользователя при нарушении правил.

4. Возврат средств осуществляется в соответствии с политикой возврата.

5. Пользователь несет полную ответственность за безопасность своего аккаунта.

6. При возникновении вопросов обращайтесь в техническую поддержку.

Используя сервис, вы соглашаетесь с данными правилами.
"""


async def get_all_rules_versions(db: AsyncSession, language: str = 'ru', limit: int = 10) -> list[ServiceRule]:
    result = await db.execute(
        select(ServiceRule).where(ServiceRule.language == language).order_by(ServiceRule.created_at.desc()).limit(limit)
    )
    return result.scalars().all()


async def restore_rules_version(db: AsyncSession, rule_id: int, language: str = 'ru') -> ServiceRule | None:
    try:
        result = await db.execute(
            select(ServiceRule).where(ServiceRule.id == rule_id, ServiceRule.language == language)
        )
        rule_to_restore = result.scalar_one_or_none()

        if not rule_to_restore:
            logger.warning('Правило с ID не найдено для языка', rule_id=rule_id, language=language)
            return None

        await db.execute(
            update(ServiceRule)
            .where(ServiceRule.language == language, ServiceRule.is_active == True)
            .values(is_active=False, updated_at=datetime.now(UTC))
        )

        restored_rule = ServiceRule(
            title=rule_to_restore.title, content=rule_to_restore.content, language=language, is_active=True, order=0
        )

        db.add(restored_rule)
        await db.commit()
        await db.refresh(restored_rule)

        logger.info(
            '✅ Восстановлена версия правил ID как новое правило ID', rule_id=rule_id, restored_rule_id=restored_rule.id
        )
        return restored_rule

    except Exception as e:
        logger.error('❌ Ошибка при восстановлении правил ID', rule_id=rule_id, error=e)
        await db.rollback()
        raise


async def get_rules_statistics(db: AsyncSession) -> dict:
    try:
        active_result = await db.execute(select(ServiceRule).where(ServiceRule.is_active == True))
        active_rules = active_result.scalars().all()

        all_result = await db.execute(select(ServiceRule))
        all_rules = all_result.scalars().all()

        languages_stats = {}
        for rule in active_rules:
            lang = rule.language
            if lang not in languages_stats:
                languages_stats[lang] = {'active_count': 0, 'last_updated': None, 'content_length': 0}

            languages_stats[lang]['active_count'] += 1
            languages_stats[lang]['content_length'] = len(rule.content)

            if not languages_stats[lang]['last_updated'] or rule.updated_at > languages_stats[lang]['last_updated']:
                languages_stats[lang]['last_updated'] = rule.updated_at

        return {
            'total_active': len(active_rules),
            'total_all_time': len(all_rules),
            'languages': languages_stats,
            'total_languages': len(languages_stats),
        }

    except Exception as e:
        logger.error('❌ Ошибка при получении статистики правил', error=e)
        return {'total_active': 0, 'total_all_time': 0, 'languages': {}, 'total_languages': 0, 'error': str(e)}
