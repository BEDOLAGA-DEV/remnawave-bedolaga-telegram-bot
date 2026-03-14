"""
Service for managing email template overrides stored in the database.

Custom templates override the hardcoded defaults from email_templates.py.
"""

import html
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.database import AsyncSessionLocal


logger = structlog.get_logger(__name__)


async def get_template_override(
    notification_type: str,
    language: str,
    db: AsyncSession | None = None,
) -> dict[str, str] | None:
    """
    Get custom email template from the database.

    Returns:
        Dict with 'subject' and 'body_html' if found, None otherwise.
    """
    try:
        if db:
            result = await db.execute(
                text(
                    'SELECT subject, body_html FROM email_templates '
                    'WHERE notification_type = :ntype AND language = :lang AND is_active = :active'
                ),
                {'ntype': notification_type, 'lang': language, 'active': True},
            )
            row = result.fetchone()
            if row:
                return {'subject': row[0], 'body_html': row[1]}
            return None

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    'SELECT subject, body_html FROM email_templates '
                    'WHERE notification_type = :ntype AND language = :lang AND is_active = :active'
                ),
                {'ntype': notification_type, 'lang': language, 'active': True},
            )
            row = result.fetchone()
            if row:
                return {'subject': row[0], 'body_html': row[1]}
            return None

    except Exception as e:
        logger.debug(
            'Не удалось получить override шаблона /', notification_type=notification_type, language=language, e=e
        )
        return None


async def get_all_overrides(db: AsyncSession) -> list[dict[str, Any]]:
    """Get all custom template overrides from the database."""
    result = await db.execute(
        text(
            'SELECT id, notification_type, language, subject, body_html, is_active, created_at, updated_at FROM email_templates ORDER BY notification_type, language'
        )
    )
    rows = result.fetchall()
    return [
        {
            'id': row[0],
            'notification_type': row[1],
            'language': row[2],
            'subject': row[3],
            'body_html': row[4],
            'is_active': row[5],
            'created_at': str(row[6]) if row[6] else None,
            'updated_at': str(row[7]) if row[7] else None,
        }
        for row in rows
    ]


async def get_overrides_for_type(notification_type: str, db: AsyncSession) -> list[dict[str, Any]]:
    """Get all language overrides for a specific notification type."""
    result = await db.execute(
        text(
            'SELECT id, language, subject, body_html, is_active, created_at, updated_at '
            'FROM email_templates WHERE notification_type = :ntype ORDER BY language'
        ),
        {'ntype': notification_type},
    )
    rows = result.fetchall()
    return [
        {
            'id': row[0],
            'language': row[1],
            'subject': row[2],
            'body_html': row[3],
            'is_active': row[4],
            'created_at': str(row[5]) if row[5] else None,
            'updated_at': str(row[6]) if row[6] else None,
        }
        for row in rows
    ]


async def save_template_override(
    notification_type: str,
    language: str,
    subject: str,
    body_html: str,
    db: AsyncSession,
) -> dict[str, Any]:
    """Save or update a custom email template in the database."""
    # Check if exists
    existing = await db.execute(
        text('SELECT id FROM email_templates WHERE notification_type = :ntype AND language = :lang'),
        {'ntype': notification_type, 'lang': language},
    )
    row = existing.fetchone()

    now = datetime.now(UTC)

    if row:
        # Update
        await db.execute(
            text(
                'UPDATE email_templates SET subject = :subject, body_html = :body_html, '
                'is_active = :active, updated_at = :now '
                'WHERE notification_type = :ntype AND language = :lang'
            ),
            {
                'subject': subject,
                'body_html': body_html,
                'active': True,
                'now': now,
                'ntype': notification_type,
                'lang': language,
            },
        )
    else:
        # Insert
        await db.execute(
            text(
                'INSERT INTO email_templates (notification_type, language, subject, body_html, is_active, created_at, updated_at) '
                'VALUES (:ntype, :lang, :subject, :body_html, :active, :now, :now)'
            ),
            {
                'ntype': notification_type,
                'lang': language,
                'subject': subject,
                'body_html': body_html,
                'active': True,
                'now': now,
            },
        )

    await db.commit()

    return {
        'notification_type': notification_type,
        'language': language,
        'subject': subject,
        'body_html': body_html,
        'is_active': True,
    }


async def get_rendered_override(
    notification_type: str,
    language: str,
    context: dict[str, Any] | None = None,
    db: AsyncSession | None = None,
) -> tuple[str, str] | None:
    """
    Get a custom template override rendered with the base email template.

    Returns:
        Tuple of (subject, body_html) if override exists, None otherwise.
    """
    override = await get_template_override(notification_type, language, db)
    if not override:
        return None

    from .email_templates import EmailNotificationTemplates

    templates = EmailNotificationTemplates()
    body_html = override['body_html']

    # Simple variable substitution for context vars like {username}, {verification_url}, etc.
    if context:
        for key, value in context.items():
            body_html = body_html.replace(f'{{{key}}}', html.escape(str(value)))

    rendered = templates._get_base_template(body_html, language)
    subject = override['subject']

    # Also substitute in subject
    if context:
        for key, value in context.items():
            safe_value = str(value).replace('\r', '').replace('\n', '')
            subject = subject.replace(f'{{{key}}}', safe_value)

    return (subject, rendered)


async def delete_template_override(
    notification_type: str,
    language: str,
    db: AsyncSession,
) -> bool:
    """Delete a custom template override (revert to default)."""
    result = await db.execute(
        text('DELETE FROM email_templates WHERE notification_type = :ntype AND language = :lang'),
        {'ntype': notification_type, 'lang': language},
    )
    await db.commit()
    return result.rowcount > 0
