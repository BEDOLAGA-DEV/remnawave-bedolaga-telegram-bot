from typing import List, Optional
import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, and_, or_, update, func
from sqlalchemy.orm import selectinload
from datetime import datetime

from app.database.models import Ticket, TicketMessage, TicketStatus, User, SupportAuditLog

logger = logging.getLogger(__name__)


class TicketCRUD:
    """CRUD операции для работы с тикетами"""
    
    @staticmethod
    async def create_ticket(
        db: AsyncSession,
        user_id: int,
        title: str,
        message_text: str,
        priority: str = "normal",
        *,
        media_type: Optional[str] = None,
        media_file_id: Optional[str] = None,
        media_caption: Optional[str] = None,
    ) -> Ticket:
        """Создать новый тикет с первым сообщением"""
        ticket = Ticket(
            user_id=user_id,
            title=title,
            status=TicketStatus.OPEN.value,
            priority=priority
        )
        db.add(ticket)
        await db.flush()  # Получаем ID тикета
        
        # Создаем первое сообщение
        message = TicketMessage(
            ticket_id=ticket.id,
            user_id=user_id,
            message_text=message_text,
            is_from_admin=False,
            has_media=bool(media_type and media_file_id),
            media_type=media_type,
            media_file_id=media_file_id,
            media_caption=media_caption,
        )
        db.add(message)
        
        await db.commit()
        await db.refresh(ticket)
        
        # Отправляем событие о создании тикета
        try:
            from app.services.event_emitter import event_emitter
            await event_emitter.emit(
                "ticket.created",
                {
                    "ticket_id": ticket.id,
                    "user_id": user_id,
                    "title": title,
                    "status": ticket.status,
                    "priority": priority,
                    "has_media": bool(media_type and media_file_id),
                },
                db=db,
            )
        except Exception as error:
            logger.warning("Failed to emit ticket.created event: %s", error)
        
        return ticket
    
    @staticmethod
    async def get_ticket_by_id(
        db: AsyncSession,
        ticket_id: int,
        load_messages: bool = True,
        load_user: bool = False
    ) -> Optional[Ticket]:
        """Получить тикет по ID"""
        query = select(Ticket).where(Ticket.id == ticket_id)
        
        if load_user:
            query = query.options(selectinload(Ticket.user))
        
        if load_messages:
            query = query.options(selectinload(Ticket.messages))
        
        result = await db.execute(query)
        return result.scalar_one_or_none()
    
    @staticmethod
    async def get_user_tickets(
        db: AsyncSession,
        user_id: int,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0
    ) -> List[Ticket]:
        """Получить тикеты пользователя"""
        query = select(Ticket).where(Ticket.user_id == user_id)
        
        if status:
            query = query.where(Ticket.status == status)
        
        query = query.order_by(desc(Ticket.updated_at)).offset(offset).limit(limit)
        
        result = await db.execute(query)
        return result.scalars().all()

    @staticmethod
    async def count_user_tickets_by_statuses(
        db: AsyncSession,
        user_id: int,
        statuses: List[str]
    ) -> int:
        """Подсчитать количество тикетов пользователя по списку статусов"""
        query = select(func.count()).select_from(Ticket).where(Ticket.user_id == user_id)
        if statuses:
            query = query.where(Ticket.status.in_(statuses))
        result = await db.execute(query)
        return int(result.scalar() or 0)

    @staticmethod
    async def get_user_tickets_by_statuses(
        db: AsyncSession,
        user_id: int,
        statuses: List[str],
        limit: int = 20,
        offset: int = 0
    ) -> List[Ticket]:
        """Получить тикеты пользователя по списку статусов с пагинацией"""
        query = (
            select(Ticket)
            .where(Ticket.user_id == user_id)
            .order_by(desc(Ticket.updated_at))
            .offset(offset)
            .limit(limit)
        )
        if statuses:
            query = query.where(Ticket.status.in_(statuses))
        result = await db.execute(query)
        return result.scalars().all()

    @staticmethod
    async def user_has_active_ticket(
        db: AsyncSession,
        user_id: int
    ) -> bool:
        """Проверить, есть ли у пользователя активный (не закрытый) тикет"""
        query = (
            select(Ticket.id)
            .where(
                Ticket.user_id == user_id,
                Ticket.status.in_([TicketStatus.OPEN.value, TicketStatus.ANSWERED.value])
            )
            .limit(1)
        )
        result = await db.execute(query)
        return result.scalar_one_or_none() is not None

    @staticmethod
    async def is_user_globally_blocked(
        db: AsyncSession,
        user_id: int
    ) -> Optional[datetime]:
        """Проверить, заблокирован ли пользователь для создания/ответов по любому тикету.
        Возвращает дату окончания блокировки, если активна, или None.
        """
        query = select(Ticket).where(
            Ticket.user_id == user_id,
            or_(Ticket.user_reply_block_permanent == True, Ticket.user_reply_block_until.isnot(None))
        ).order_by(desc(Ticket.updated_at)).limit(10)
        result = await db.execute(query)
        tickets = result.scalars().all()
        if not tickets:
            return None
        from datetime import datetime
        # Если есть вечная блокировка в любом тикете — блок активен без срока
        for t in tickets:
            if t.user_reply_block_permanent:
                return datetime.max
        # Иначе ищем максимальный срок блокировки, если он в будущем
        future_until = [t.user_reply_block_until for t in tickets if t.user_reply_block_until]
        if not future_until:
            return None
        max_until = max(future_until)
        return max_until if max_until > datetime.utcnow() else None
    
    @staticmethod
    async def get_all_tickets(
        db: AsyncSession,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        limit: int = 50,
        offset: int = 0
    ) -> List[Ticket]:
        """Получить все тикеты (для админов)"""
        query = select(Ticket).options(selectinload(Ticket.user))
        
        conditions = []
        if status:
            conditions.append(Ticket.status == status)
        if priority:
            conditions.append(Ticket.priority == priority)
        
        if conditions:
            query = query.where(and_(*conditions))
        
        query = query.order_by(desc(Ticket.updated_at)).offset(offset).limit(limit)
        
        result = await db.execute(query)
        return result.scalars().all()

    @staticmethod
    async def get_tickets_by_statuses(
        db: AsyncSession,
        statuses: List[str],
        limit: int = 50,
        offset: int = 0
    ) -> List[Ticket]:
        query = select(Ticket).options(selectinload(Ticket.user))
        if statuses:
            query = query.where(Ticket.status.in_(statuses))
        query = query.order_by(desc(Ticket.updated_at)).offset(offset).limit(limit)
        result = await db.execute(query)
        return result.scalars().all()

    @staticmethod
    async def count_tickets(
        db: AsyncSession,
        status: Optional[str] = None
    ) -> int:
        query = select(func.count()).select_from(Ticket)
        if status:
            query = query.where(Ticket.status == status)
        result = await db.execute(query)
        return int(result.scalar() or 0)

    @staticmethod
    async def count_tickets_by_statuses(
        db: AsyncSession,
        statuses: List[str]
    ) -> int:
        query = select(func.count()).select_from(Ticket)
        if statuses:
            query = query.where(Ticket.status.in_(statuses))
        result = await db.execute(query)
        return int(result.scalar() or 0)
    
    @staticmethod
    async def update_ticket_status(
        db: AsyncSession,
        ticket_id: int,
        status: str,
        closed_at: Optional[datetime] = None
    ) -> bool:
        """Обновить статус тикета"""
        ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=False)
        if not ticket:
            return False
        
        ticket.status = status
        ticket.updated_at = datetime.utcnow()
        
        if status == TicketStatus.CLOSED.value and closed_at:
            ticket.closed_at = closed_at
        
        await db.commit()
        
        # Отправляем событие об изменении статуса тикета
        try:
            from app.services.event_emitter import event_emitter
            await event_emitter.emit(
                "ticket.status_changed",
                {
                    "ticket_id": ticket_id,
                    "user_id": ticket.user_id,
                    "old_status": ticket.status,  # На самом деле это уже новый статус, но для простоты оставим так
                    "new_status": status,
                    "closed_at": closed_at.isoformat() if closed_at else None,
                },
                db=db,
            )
        except Exception as error:
            logger.warning("Failed to emit ticket.status_changed event: %s", error)
        
        return True

    @staticmethod
    async def set_user_reply_block(
        db: AsyncSession,
        ticket_id: int,
        permanent: bool,
        until: Optional[datetime]
    ) -> bool:
        ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=False)
        if not ticket:
            return False
        ticket.user_reply_block_permanent = bool(permanent)
        ticket.user_reply_block_until = until
        ticket.updated_at = datetime.utcnow()
        await db.commit()
        return True
    
    @staticmethod
    async def close_ticket(
        db: AsyncSession,
        ticket_id: int
    ) -> bool:
        """Закрыть тикет"""
        return await TicketCRUD.update_ticket_status(
            db, ticket_id, TicketStatus.CLOSED.value, datetime.utcnow()
        )

    @staticmethod
    async def close_all_open_tickets(
        db: AsyncSession,
    ) -> List[int]:
        """Закрыть все открытые тикеты. Возвращает список идентификаторов закрытых тикетов."""
        open_statuses = [TicketStatus.OPEN.value, TicketStatus.ANSWERED.value]
        result = await db.execute(
            select(Ticket.id).where(Ticket.status.in_(open_statuses))
        )
        ticket_ids = result.scalars().all()

        if not ticket_ids:
            return []

        now = datetime.utcnow()
        await db.execute(
            update(Ticket)
            .where(Ticket.id.in_(ticket_ids))
            .values(status=TicketStatus.CLOSED.value, closed_at=now, updated_at=now)
        )
        await db.commit()

        return ticket_ids

    @staticmethod
    async def add_support_audit(
        db: AsyncSession,
        *,
        actor_user_id: Optional[int],
        actor_telegram_id: int,
        is_moderator: bool,
        action: str,
        ticket_id: Optional[int] = None,
        target_user_id: Optional[int] = None,
        details: Optional[dict] = None,
    ) -> None:
        try:
            log = SupportAuditLog(
                actor_user_id=actor_user_id,
                actor_telegram_id=actor_telegram_id,
                is_moderator=bool(is_moderator),
                action=action,
                ticket_id=ticket_id,
                target_user_id=target_user_id,
                details=details or {},
            )
            db.add(log)
            await db.commit()
        except Exception:
            await db.rollback()
            # не мешаем основной логике
            pass

    @staticmethod
    async def list_support_audit(
        db: AsyncSession,
        *,
        limit: int = 50,
        offset: int = 0,
        action: Optional[str] = None,
    ) -> List[SupportAuditLog]:
        from sqlalchemy import select, desc

        query = select(SupportAuditLog).order_by(desc(SupportAuditLog.created_at))

        if action:
            query = query.where(SupportAuditLog.action == action)

        result = await db.execute(query.offset(offset).limit(limit))
        return result.scalars().all()

    @staticmethod
    async def count_support_audit(db: AsyncSession, action: Optional[str] = None) -> int:
        from sqlalchemy import select, func

        query = select(func.count()).select_from(SupportAuditLog)

        if action:
            query = query.where(SupportAuditLog.action == action)

        result = await db.execute(query)
        return int(result.scalar() or 0)

    @staticmethod
    async def list_support_audit_actions(db: AsyncSession) -> List[str]:
        from sqlalchemy import select

        result = await db.execute(
            select(SupportAuditLog.action)
            .where(SupportAuditLog.action.isnot(None))
            .distinct()
            .order_by(SupportAuditLog.action)
        )

        return [row[0] for row in result.fetchall()]
    
    @staticmethod
    async def get_open_tickets_count(db: AsyncSession) -> int:
        """Получить количество открытых тикетов"""
        query = select(Ticket).where(Ticket.status.in_([
            TicketStatus.OPEN.value,
            TicketStatus.ANSWERED.value
        ]))
        result = await db.execute(query)
        return len(result.scalars().all())


class TicketMessageCRUD:
    """CRUD операции для работы с сообщениями тикетов"""
    
    @staticmethod
    async def add_message(
        db: AsyncSession,
        ticket_id: int,
        user_id: int,
        message_text: str,
        is_from_admin: bool = False,
        media_type: Optional[str] = None,
        media_file_id: Optional[str] = None,
        media_caption: Optional[str] = None
    ) -> TicketMessage:
        """Добавить сообщение в тикет"""
        message = TicketMessage(
            ticket_id=ticket_id,
            user_id=user_id,
            message_text=message_text,
            is_from_admin=is_from_admin,
            has_media=bool(media_type and media_file_id),
            media_type=media_type,
            media_file_id=media_file_id,
            media_caption=media_caption
        )
        
        db.add(message)
        
        # Обновляем статус тикета
        ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=False)
        if ticket:
            # Если тикет закрыт, запрещаем изменение статуса при сообщении пользователя
            if not is_from_admin and ticket.status == TicketStatus.CLOSED.value:
                return message
            if is_from_admin:
                # Админ ответил - тикет отвечен
                ticket.status = TicketStatus.ANSWERED.value
            else:
                # Пользователь ответил - тикет открыт
                ticket.status = TicketStatus.OPEN.value
                # Сбросить отметку последнего SLA-напоминания, чтобы снова напоминать от времени нового сообщения
                try:
                    from sqlalchemy import inspect as sa_inspect
                    # если колонка существует в модели
                    if hasattr(ticket, 'last_sla_reminder_at'):
                        ticket.last_sla_reminder_at = None
                except Exception:
                    pass
            
            ticket.updated_at = datetime.utcnow()
        
        await db.commit()
        await db.refresh(message)
        
        # Отправляем событие о новом сообщении в тикете
        try:
            from app.services.event_emitter import event_emitter
            await event_emitter.emit(
                "ticket.message_added",
                {
                    "ticket_id": ticket_id,
                    "message_id": message.id,
                    "user_id": user_id,
                    "is_from_admin": is_from_admin,
                    "message_text": message_text[:200],  # Ограничиваем длину для события
                    "has_media": bool(media_type and media_file_id),
                    "status": ticket.status if ticket else None,
                },
                db=db,
            )
        except Exception as error:
            logger.warning("Failed to emit ticket.message_added event: %s", error)
        
        return message
    
    @staticmethod
    async def get_ticket_messages(
        db: AsyncSession,
        ticket_id: int,
        limit: int = 50,
        offset: int = 0
    ) -> List[TicketMessage]:
        """Получить сообщения тикета"""
        query = (
            select(TicketMessage)
            .where(TicketMessage.ticket_id == ticket_id)
            .order_by(TicketMessage.created_at)
            .offset(offset)
            .limit(limit)
        )
        
        result = await db.execute(query)
        return result.scalars().all()
    
    @staticmethod
    async def get_last_message(
        db: AsyncSession,
        ticket_id: int
    ) -> Optional[TicketMessage]:
        """Получить последнее сообщение в тикете"""
        query = (
            select(TicketMessage)
            .where(TicketMessage.ticket_id == ticket_id)
            .order_by(desc(TicketMessage.created_at))
            .limit(1)
        )
        
        result = await db.execute(query)
        return result.scalar_one_or_none()
