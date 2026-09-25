"""Таймлайн активности пользователя: бот, кабинет и мини-апп в одной ленте.

Fan-in по существующим таблицам: транзакции, события подписки, промокоды,
купоны, обращения, колесо, опросы, подарки, реферальные начисления, выводы,
входы в кабинет и клики по кнопкам. Слой жил в кабинетном роуте админки —
вынесен сюда, чтобы той же лентой мог пользоваться и внешний Web API, не
копируя правила дедупликации.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    ButtonClickLog,
    CabinetRefreshToken,
    Coupon,
    GuestPurchase,
    PollResponse,
    PromoCode,
    PromoCodeUse,
    ReferralEarning,
    SubscriptionEvent,
    Ticket,
    Transaction,
    WheelSpin,
    WithdrawalRequest,
)
from app.services.user_action_log_service import CLICK_PREFIX, SCREEN_PREFIX


class UserActivityItem(BaseModel):
    """Одна запись в таймлайне активности пользователя (бот + кабинет).

    ``type`` — источник записи (transaction, event, promocode, coupon, ticket,
    wheel_spin, poll, gift_sent, gift_received, referral_earning, cabinet_login,
    withdrawal); ``subtype`` уточняет его (тип транзакции, event_type события,
    статус тикета и т.п.). ``title`` — сырой человекочитаемый текст источника
    (описание транзакции, код промокода, название тикета) — локализованный
    заголовок строит фронт по type/subtype.
    """

    type: str
    subtype: str | None = None
    source: str | None = None  # 'bot' | 'cabinet' — где произошло действие, если известно
    title: str | None = None
    amount_kopeks: int | None = None
    timestamp: datetime
    meta: dict[str, Any] | None = None


class UserActivityResponse(BaseModel):
    """Paginated user activity timeline."""

    items: list[UserActivityItem]
    total: int
    offset: int = 0
    limit: int = 50


class UnknownActivityTypes(ValueError):
    """Запрошены типы записей, которых нет среди источников."""

    def __init__(self, unknown: set[str]) -> None:
        self.unknown = sorted(unknown)
        super().__init__(f'Unknown activity types: {", ".join(self.unknown)}')


def activity_sources(user_id: int) -> dict[str, tuple]:
    """Источники таймлайна активности: type -> (select, count_select, mapper).

    Дедупликация пересечений:
    - транзакции, на которые ссылается SubscriptionEvent.transaction_id или
      ReferralEarning.referral_transaction_id, исключаются (событие/начисление
      богаче: message/reason);
    - события promocode_activation исключаются — PromoCodeUse полнее (события
      пишутся только вместе с админ-уведомлениями).
    """
    event_referenced = select(SubscriptionEvent.transaction_id).where(
        SubscriptionEvent.user_id == user_id,
        SubscriptionEvent.transaction_id.is_not(None),
    )
    earning_referenced = select(ReferralEarning.referral_transaction_id).where(
        ReferralEarning.user_id == user_id,
        ReferralEarning.referral_transaction_id.is_not(None),
    )
    transactions_where = and_(
        Transaction.user_id == user_id,
        Transaction.id.not_in(event_referenced),
        Transaction.id.not_in(earning_referenced),
    )
    events_where = and_(
        SubscriptionEvent.user_id == user_id,
        SubscriptionEvent.event_type != 'promocode_activation',
    )

    def _map_transaction(t: Transaction) -> UserActivityItem:
        return UserActivityItem(
            type='transaction',
            subtype=t.type,
            title=t.description,
            amount_kopeks=t.amount_kopeks,
            timestamp=t.created_at,
            meta={'payment_method': t.payment_method, 'is_completed': t.is_completed},
        )

    def _map_event(e: SubscriptionEvent) -> UserActivityItem:
        return UserActivityItem(
            type='event',
            subtype=e.event_type,
            title=e.message,
            amount_kopeks=e.amount_kopeks,
            timestamp=e.occurred_at,
            meta=e.extra if isinstance(e.extra, dict) else None,
        )

    def _map_promocode(row) -> UserActivityItem:
        use, code = row
        return UserActivityItem(type='promocode', source='bot', title=code, timestamp=use.used_at)

    def _map_coupon(c: Coupon) -> UserActivityItem:
        return UserActivityItem(type='coupon', subtype=c.status, title=c.token, timestamp=c.redeemed_at)

    def _map_ticket(t: Ticket) -> UserActivityItem:
        return UserActivityItem(
            type='ticket',
            subtype=t.status,
            title=t.title,
            timestamp=t.created_at,
            meta={'ticket_id': t.id},
        )

    def _map_wheel(w: WheelSpin) -> UserActivityItem:
        return UserActivityItem(
            type='wheel_spin',
            subtype=w.prize_type,
            source='bot',
            title=w.prize_display_name,
            amount_kopeks=w.prize_value_kopeks,
            timestamp=w.created_at,
        )

    def _map_poll(p: PollResponse) -> UserActivityItem:
        return UserActivityItem(
            type='poll',
            source='bot',
            amount_kopeks=p.reward_amount_kopeks if p.reward_given else None,
            timestamp=p.completed_at,
        )

    def _map_gift_sent(g: GuestPurchase) -> UserActivityItem:
        return UserActivityItem(
            type='gift_sent',
            subtype=g.status,
            title=g.gift_recipient_value,
            amount_kopeks=g.amount_kopeks,
            timestamp=g.paid_at or g.created_at,
        )

    def _map_gift_received(g: GuestPurchase) -> UserActivityItem:
        return UserActivityItem(
            type='gift_received',
            subtype=g.status,
            amount_kopeks=g.amount_kopeks,
            timestamp=g.delivered_at or g.created_at,
        )

    def _map_earning(e: ReferralEarning) -> UserActivityItem:
        return UserActivityItem(
            type='referral_earning',
            subtype=e.reason,
            amount_kopeks=e.amount_kopeks,
            timestamp=e.created_at,
        )

    def _map_login(token: CabinetRefreshToken) -> UserActivityItem:
        return UserActivityItem(
            type='cabinet_login',
            source='cabinet',
            title=token.device_info,
            timestamp=token.created_at,
        )

    def _map_withdrawal(w: WithdrawalRequest) -> UserActivityItem:
        return UserActivityItem(
            type='withdrawal',
            subtype=w.status,
            amount_kopeks=w.amount_kopeks,
            timestamp=w.created_at,
        )

    def _map_button_click(c: ButtonClickLog) -> UserActivityItem:
        return UserActivityItem(
            type='button_click',
            subtype=c.button_type if c.button_type in ('command', 'payment', 'message') else None,
            source='bot',
            title=c.button_text or c.callback_data or c.button_id,
            timestamp=c.clicked_at,
            meta={'callback_data': c.callback_data} if c.callback_data else None,
        )

    def _web_action(c: ButtonClickLog, *, type_: str, source: str) -> UserActivityItem:
        # Открытие экрана хранится как 'SCREEN <путь>', нажатие — как
        # 'CLICK <подпись>' — в таймлайне это отдельные подтипы, а не
        # «действие» с техническим заголовком.
        subtype = None
        title = c.button_id
        for prefix, name in ((SCREEN_PREFIX, 'screen'), (CLICK_PREFIX, 'click')):
            if c.button_id.startswith(prefix):
                subtype, title = name, c.button_id[len(prefix) :]
                break
        return UserActivityItem(
            type=type_,
            subtype=subtype,
            source=source,
            title=title,
            timestamp=c.clicked_at,
            meta={'path': c.callback_data} if c.callback_data else None,
        )

    def _map_cabinet_action(c: ButtonClickLog) -> UserActivityItem:
        return _web_action(c, type_='cabinet_action', source='cabinet')

    def _map_miniapp_action(c: ButtonClickLog) -> UserActivityItem:
        return _web_action(c, type_='miniapp_action', source='miniapp')

    # button_click_logs делится на три источника: нажатия кнопок бота (пишет
    # ButtonStatsMiddleware), действия в кабинете (button_type='cabinet') и
    # действия в Mini App (button_type='miniapp') — оба пишет
    # user_action_log_service. Раньше третьего не было вовсе, и человек,
    # живущий в Mini App, выглядел в таймлайне неактивным.
    _WEB_SURFACES = ('cabinet', 'miniapp')
    bot_clicks_where = and_(
        ButtonClickLog.user_id == user_id,
        or_(ButtonClickLog.button_type.is_(None), ButtonClickLog.button_type.not_in(_WEB_SURFACES)),
    )
    cabinet_actions_where = and_(ButtonClickLog.user_id == user_id, ButtonClickLog.button_type == 'cabinet')
    miniapp_actions_where = and_(ButtonClickLog.user_id == user_id, ButtonClickLog.button_type == 'miniapp')

    return {
        'transaction': (
            select(Transaction).where(transactions_where),
            select(func.count(Transaction.id)).where(transactions_where),
            Transaction.created_at,
            _map_transaction,
        ),
        'event': (
            select(SubscriptionEvent).where(events_where),
            select(func.count(SubscriptionEvent.id)).where(events_where),
            SubscriptionEvent.occurred_at,
            _map_event,
        ),
        'promocode': (
            select(PromoCodeUse, PromoCode.code)
            .join(PromoCode, PromoCode.id == PromoCodeUse.promocode_id)
            .where(PromoCodeUse.user_id == user_id),
            select(func.count(PromoCodeUse.id)).where(PromoCodeUse.user_id == user_id),
            PromoCodeUse.used_at,
            _map_promocode,
        ),
        'coupon': (
            select(Coupon).where(Coupon.redeemed_by == user_id, Coupon.redeemed_at.is_not(None)),
            select(func.count(Coupon.id)).where(Coupon.redeemed_by == user_id, Coupon.redeemed_at.is_not(None)),
            Coupon.redeemed_at,
            _map_coupon,
        ),
        'ticket': (
            select(Ticket).where(Ticket.user_id == user_id),
            select(func.count(Ticket.id)).where(Ticket.user_id == user_id),
            Ticket.created_at,
            _map_ticket,
        ),
        'wheel_spin': (
            select(WheelSpin).where(WheelSpin.user_id == user_id),
            select(func.count(WheelSpin.id)).where(WheelSpin.user_id == user_id),
            WheelSpin.created_at,
            _map_wheel,
        ),
        'poll': (
            select(PollResponse).where(PollResponse.user_id == user_id, PollResponse.completed_at.is_not(None)),
            select(func.count(PollResponse.id)).where(
                PollResponse.user_id == user_id, PollResponse.completed_at.is_not(None)
            ),
            PollResponse.completed_at,
            _map_poll,
        ),
        'gift_sent': (
            select(GuestPurchase).where(GuestPurchase.buyer_user_id == user_id, GuestPurchase.is_gift.is_(True)),
            select(func.count(GuestPurchase.id)).where(
                GuestPurchase.buyer_user_id == user_id, GuestPurchase.is_gift.is_(True)
            ),
            GuestPurchase.created_at,
            _map_gift_sent,
        ),
        'gift_received': (
            select(GuestPurchase).where(GuestPurchase.user_id == user_id, GuestPurchase.is_gift.is_(True)),
            select(func.count(GuestPurchase.id)).where(
                GuestPurchase.user_id == user_id, GuestPurchase.is_gift.is_(True)
            ),
            GuestPurchase.created_at,
            _map_gift_received,
        ),
        'referral_earning': (
            select(ReferralEarning).where(ReferralEarning.user_id == user_id),
            select(func.count(ReferralEarning.id)).where(ReferralEarning.user_id == user_id),
            ReferralEarning.created_at,
            _map_earning,
        ),
        'cabinet_login': (
            select(CabinetRefreshToken).where(CabinetRefreshToken.user_id == user_id),
            select(func.count(CabinetRefreshToken.id)).where(CabinetRefreshToken.user_id == user_id),
            CabinetRefreshToken.created_at,
            _map_login,
        ),
        'withdrawal': (
            select(WithdrawalRequest).where(WithdrawalRequest.user_id == user_id),
            select(func.count(WithdrawalRequest.id)).where(WithdrawalRequest.user_id == user_id),
            WithdrawalRequest.created_at,
            _map_withdrawal,
        ),
        'button_click': (
            select(ButtonClickLog).where(bot_clicks_where),
            select(func.count(ButtonClickLog.id)).where(bot_clicks_where),
            ButtonClickLog.clicked_at,
            _map_button_click,
        ),
        'cabinet_action': (
            select(ButtonClickLog).where(cabinet_actions_where),
            select(func.count(ButtonClickLog.id)).where(cabinet_actions_where),
            ButtonClickLog.clicked_at,
            _map_cabinet_action,
        ),
        'miniapp_action': (
            select(ButtonClickLog).where(miniapp_actions_where),
            select(func.count(ButtonClickLog.id)).where(miniapp_actions_where),
            ButtonClickLog.clicked_at,
            _map_miniapp_action,
        ),
    }


async def collect_user_activity(
    db: AsyncSession,
    user_id: int,
    *,
    offset: int = 0,
    limit: int = 50,
    types: str | None = None,
) -> UserActivityResponse:
    """Собрать страницу таймлайна.

    Из каждого источника берутся первые ``offset + limit`` записей по времени,
    затем всё сливается и сортируется — глубокая пагинация дороже, но limit
    ограничен, а таймлайн листают сверху. ``types`` — CSV-фильтр по ``type``;
    неизвестный тип приводит к :class:`UnknownActivityTypes`.
    """
    sources = activity_sources(user_id)
    if types:
        requested = {t.strip() for t in types.split(',') if t.strip()}
        unknown = requested - sources.keys()
        if unknown:
            raise UnknownActivityTypes(unknown)
        sources = {key: value for key, value in sources.items() if key in requested}

    window = offset + limit
    merged: list[UserActivityItem] = []
    total = 0
    for query, count_query, ts_column, mapper in sources.values():
        total += (await db.execute(count_query)).scalar() or 0
        rows = (await db.execute(query.order_by(ts_column.desc()).limit(window))).all()
        for row in rows:
            value = row[0] if len(row) == 1 else row
            item = mapper(value)
            if item.timestamp is not None:
                merged.append(item)

    merged.sort(key=lambda item: item.timestamp, reverse=True)

    return UserActivityResponse(
        items=merged[offset : offset + limit],
        total=total,
        offset=offset,
        limit=limit,
    )
