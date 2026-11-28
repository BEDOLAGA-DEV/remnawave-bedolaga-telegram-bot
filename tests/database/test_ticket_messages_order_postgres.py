"""Порядок сообщений тикета, загруженных вместе с тикетом.

Бот показывает переписку тикета из связи ``Ticket.messages``, которая грузится
через ``selectinload``. Без ORDER BY PostgreSQL возвращает строки в порядке их
размещения в таблице, а он не обязан совпадать с порядком отправки: строка,
записанная на место, освобождённое очисткой, читается раньше старых. Тогда
новое сообщение оказывается в начале переписки.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.database.crud.ticket import TicketCRUD
from app.database.models import Base, Ticket, TicketMessage, User
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


async def test_ticket_messages_load_oldest_first_whatever_the_row_placement(postgres_database):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=2) as (writer, reader):
        writer.add(User(id=1, telegram_id=42, language='ru'))
        writer.add(Ticket(id=1, user_id=1, title='Сайты не открываются'))
        await writer.flush()

        sent = datetime(2026, 11, 27, 8, 20, tzinfo=UTC)
        # Строки пишутся от новых к старым, поэтому в таблице лежат в обратном
        # порядке. Два старых сообщения отправлены в одну секунду — между ними
        # решает id.
        for message_id, created_at in ((3, sent + timedelta(hours=15)), (2, sent), (1, sent)):
            writer.add(
                TicketMessage(
                    id=message_id,
                    ticket_id=1,
                    user_id=1,
                    message_text=f'Сообщение {message_id}',
                    created_at=created_at,
                )
            )
            await writer.flush()
        await writer.commit()

        placement = (await reader.execute(text('SELECT id FROM ticket_messages ORDER BY ctid'))).scalars().all()
        assert placement == [3, 2, 1]

        ticket = await TicketCRUD.get_ticket_by_id(reader, 1, load_messages=True)

        assert [message.id for message in ticket.messages] == [1, 2, 3]
