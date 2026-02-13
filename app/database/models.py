from datetime import datetime, time, timedelta
from enum import Enum

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func


Base = declarative_base()


server_squad_promo_groups = Table(
    'server_squad_promo_groups',
    Base.metadata,
    Column(
        'server_squad_id',
        Integer,
        ForeignKey('server_squads.id', ondelete='CASCADE'),
        primary_key=True,
    ),
    Column(
        'promo_group_id',
        Integer,
        ForeignKey('promo_groups.id', ondelete='CASCADE'),
        primary_key=True,
    ),
)


# M2M таблица для связи тарифов с промогруппами (доступ к тарифу)
tariff_promo_groups = Table(
    'tariff_promo_groups',
    Base.metadata,
    Column(
        'tariff_id',
        Integer,
        ForeignKey('tariffs.id', ondelete='CASCADE'),
        primary_key=True,
    ),
    Column(
        'promo_group_id',
        Integer,
        ForeignKey('promo_groups.id', ondelete='CASCADE'),
        primary_key=True,
    ),
)


# M2M таблица для связи платёжных методов с промогруппами (условия показа)
payment_method_promo_groups = Table(
    'payment_method_promo_groups',
    Base.metadata,
    Column(
        'payment_method_config_id',
        Integer,
        ForeignKey('payment_method_configs.id', ondelete='CASCADE'),
        primary_key=True,
    ),
    Column(
        'promo_group_id',
        Integer,
        ForeignKey('promo_groups.id', ondelete='CASCADE'),
        primary_key=True,
    ),
)


class UserStatus(Enum):
    ACTIVE = 'active'
    BLOCKED = 'blocked'
    DELETED = 'deleted'


class SubscriptionStatus(Enum):
    TRIAL = 'trial'
    ACTIVE = 'active'
    EXPIRED = 'expired'
    DISABLED = 'disabled'
    PENDING = 'pending'


class TransactionType(Enum):
    DEPOSIT = 'deposit'
    WITHDRAWAL = 'withdrawal'
    SUBSCRIPTION_PAYMENT = 'subscription_payment'
    REFUND = 'refund'
    REFERRAL_REWARD = 'referral_reward'
    POLL_REWARD = 'poll_reward'


class PromoCodeType(Enum):
    BALANCE = 'balance'
    SUBSCRIPTION_DAYS = 'subscription_days'
    TRIAL_SUBSCRIPTION = 'trial_subscription'
    PROMO_GROUP = 'promo_group'
    DISCOUNT = 'discount'  # Одноразовая процентная скидка (balance_bonus_kopeks = процент, subscription_days = часы)


class PaymentMethod(Enum):
    TELEGRAM_STARS = 'telegram_stars'
    TRIBUTE = 'tribute'
    YOOKASSA = 'yookassa'
    CRYPTOBOT = 'cryptobot'
    HELEKET = 'heleket'
    MULENPAY = 'mulenpay'
    PAL24 = 'pal24'
    WATA = 'wata'
    PLATEGA = 'platega'
    CLOUDPAYMENTS = 'cloudpayments'
    FREEKASSA = 'freekassa'
    KASSA_AI = 'kassa_ai'
    MANUAL = 'manual'
    BALANCE = 'balance'


class MainMenuButtonActionType(Enum):
    URL = 'url'
    MINI_APP = 'mini_app'


class MainMenuButtonVisibility(Enum):
    ALL = 'all'
    ADMINS = 'admins'
    SUBSCRIBERS = 'subscribers'


class WheelPrizeType(Enum):
    """Типы призов на колесе удачи."""

    SUBSCRIPTION_DAYS = 'subscription_days'
    BALANCE_BONUS = 'balance_bonus'
    TRAFFIC_GB = 'traffic_gb'
    PROMOCODE = 'promocode'
    NOTHING = 'nothing'


class WheelSpinPaymentType(Enum):
    """Способы оплаты спина колеса."""

    TELEGRAM_STARS = 'telegram_stars'
    SUBSCRIPTION_DAYS = 'subscription_days'


class YooKassaPayment(Base):
    __tablename__ = 'yookassa_payments'

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    yookassa_payment_id = Column(String(255), unique=True, nullable=False, index=True)
    amount_kopeks = Column(Integer, nullable=False)
    currency = Column(String(3), default='RUB', nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String(50), nullable=False)
    is_paid = Column(Boolean, default=False)
    is_captured = Column(Boolean, default=False)
    confirmation_url = Column(Text, nullable=True)
    metadata_json = Column(JSON, nullable=True)
    transaction_id = Column(Integer, ForeignKey('transactions.id'), nullable=True)
    payment_method_type = Column(String(50), nullable=True)
    refundable = Column(Boolean, default=False)
    test_mode = Column(Boolean, default=False)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    yookassa_created_at = Column(DateTime, nullable=True)
    captured_at = Column(DateTime, nullable=True)
    user = relationship('User', backref='yookassa_payments')
    transaction = relationship('Transaction', backref='yookassa_payment')

    @property
    def amount_rubles(self) -> float:
        return self.amount_kopeks / 100

    @property
    def is_pending(self) -> bool:
        return self.status == 'pending'

    @property
    def is_succeeded(self) -> bool:
        return self.status == 'succeeded' and self.is_paid

    @property
    def is_failed(self) -> bool:
        return self.status in ['canceled', 'failed']

    @property
    def can_be_captured(self) -> bool:
        return self.status == 'waiting_for_capture'

    def __repr__(self):
        return f'<YooKassaPayment(id={self.id}, yookassa_id={self.yookassa_payment_id}, amount={self.amount_rubles}₽, status={self.status})>'


class CryptoBotPayment(Base):
    __tablename__ = 'cryptobot_payments'

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)

    invoice_id = Column(String(255), unique=True, nullable=False, index=True)
    amount = Column(String(50), nullable=False)
    asset = Column(String(10), nullable=False)

    status = Column(String(50), nullable=False)
    description = Column(Text, nullable=True)
    payload = Column(Text, nullable=True)

    bot_invoice_url = Column(Text, nullable=True)
    mini_app_invoice_url = Column(Text, nullable=True)
    web_app_invoice_url = Column(Text, nullable=True)

    paid_at = Column(DateTime, nullable=True)
    transaction_id = Column(Integer, ForeignKey('transactions.id'), nullable=True)

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    user = relationship('User', backref='cryptobot_payments')
    transaction = relationship('Transaction', backref='cryptobot_payment')

    @property
    def amount_float(self) -> float:
        try:
            return float(self.amount)
        except (ValueError, TypeError):
            return 0.0

    @property
    def is_paid(self) -> bool:
        return self.status == 'paid'

    @property
    def is_pending(self) -> bool:
        return self.status == 'active'

    @property
    def is_expired(self) -> bool:
        return self.status == 'expired'

    def __repr__(self):
        return f'<CryptoBotPayment(id={self.id}, invoice_id={self.invoice_id}, amount={self.amount} {self.asset}, status={self.status})>'


class HeleketPayment(Base):
    __tablename__ = 'heleket_payments'

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)

    uuid = Column(String(255), unique=True, nullable=False, index=True)
    order_id = Column(String(128), unique=True, nullable=False, index=True)

    amount = Column(String(50), nullable=False)
    currency = Column(String(10), nullable=False)
    payer_amount = Column(String(50), nullable=True)
    payer_currency = Column(String(10), nullable=True)
    exchange_rate = Column(Float, nullable=True)
    discount_percent = Column(Integer, nullable=True)

    status = Column(String(50), nullable=False)
    payment_url = Column(Text, nullable=True)
    metadata_json = Column(JSON, nullable=True)

    paid_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    transaction_id = Column(Integer, ForeignKey('transactions.id'), nullable=True)

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    user = relationship('User', backref='heleket_payments')
    transaction = relationship('Transaction', backref='heleket_payment')

    @property
    def amount_float(self) -> float:
        try:
            return float(self.amount)
        except (TypeError, ValueError):
            return 0.0

    @property
    def amount_kopeks(self) -> int:
        return int(round(self.amount_float * 100))

    @property
    def payer_amount_float(self) -> float:
        try:
            return float(self.payer_amount) if self.payer_amount is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    @property
    def is_paid(self) -> bool:
        return self.status in {'paid', 'paid_over'}

    def __repr__(self):
        return (
            f'<HeleketPayment(id={self.id}, uuid={self.uuid}, order_id={self.order_id}, amount={self.amount}'
            f' {self.currency}, status={self.status})>'
        )


class MulenPayPayment(Base):
    __tablename__ = 'mulenpay_payments'

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)

    mulen_payment_id = Column(Integer, nullable=True, index=True)
    uuid = Column(String(255), unique=True, nullable=False, index=True)
    amount_kopeks = Column(Integer, nullable=False)
    currency = Column(String(10), nullable=False, default='RUB')
    description = Column(Text, nullable=True)

    status = Column(String(50), nullable=False, default='created')
    is_paid = Column(Boolean, default=False)
    paid_at = Column(DateTime, nullable=True)

    payment_url = Column(Text, nullable=True)
    metadata_json = Column(JSON, nullable=True)
    callback_payload = Column(JSON, nullable=True)

    transaction_id = Column(Integer, ForeignKey('transactions.id'), nullable=True)

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    user = relationship('User', backref='mulenpay_payments')
    transaction = relationship('Transaction', backref='mulenpay_payment')

    @property
    def amount_rubles(self) -> float:
        return self.amount_kopeks / 100

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f'<MulenPayPayment(id={self.id}, mulen_id={self.mulen_payment_id}, amount={self.amount_rubles}₽, status={self.status})>'


class Pal24Payment(Base):
    __tablename__ = 'pal24_payments'

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)

    bill_id = Column(String(255), unique=True, nullable=False, index=True)
    order_id = Column(String(255), nullable=True, index=True)
    amount_kopeks = Column(Integer, nullable=False)
    currency = Column(String(10), nullable=False, default='RUB')
    description = Column(Text, nullable=True)
    type = Column(String(20), nullable=False, default='normal')

    status = Column(String(50), nullable=False, default='NEW')
    is_active = Column(Boolean, default=True)
    is_paid = Column(Boolean, default=False)
    paid_at = Column(DateTime, nullable=True)
    last_status = Column(String(50), nullable=True)
    last_status_checked_at = Column(DateTime, nullable=True)

    link_url = Column(Text, nullable=True)
    link_page_url = Column(Text, nullable=True)
    metadata_json = Column(JSON, nullable=True)
    callback_payload = Column(JSON, nullable=True)

    payment_id = Column(String(255), nullable=True, index=True)
    payment_status = Column(String(50), nullable=True)
    payment_method = Column(String(50), nullable=True)
    balance_amount = Column(String(50), nullable=True)
    balance_currency = Column(String(10), nullable=True)
    payer_account = Column(String(255), nullable=True)

    ttl = Column(Integer, nullable=True)
    expires_at = Column(DateTime, nullable=True)

    transaction_id = Column(Integer, ForeignKey('transactions.id'), nullable=True)

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    user = relationship('User', backref='pal24_payments')
    transaction = relationship('Transaction', backref='pal24_payment')

    @property
    def amount_rubles(self) -> float:
        return self.amount_kopeks / 100

    @property
    def is_pending(self) -> bool:
        return self.status in {'NEW', 'PROCESS'}

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f'<Pal24Payment(id={self.id}, bill_id={self.bill_id}, amount={self.amount_rubles}₽, status={self.status})>'
        )


class WataPayment(Base):
    __tablename__ = 'wata_payments'

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)

    payment_link_id = Column(String(64), unique=True, nullable=False, index=True)
    order_id = Column(String(255), nullable=True, index=True)
    amount_kopeks = Column(Integer, nullable=False)
    currency = Column(String(10), nullable=False, default='RUB')
    description = Column(Text, nullable=True)
    type = Column(String(50), nullable=True)

    status = Column(String(50), nullable=False, default='Opened')
    is_paid = Column(Boolean, default=False)
    paid_at = Column(DateTime, nullable=True)
    last_status = Column(String(50), nullable=True)
    terminal_public_id = Column(String(64), nullable=True)

    url = Column(Text, nullable=True)
    success_redirect_url = Column(Text, nullable=True)
    fail_redirect_url = Column(Text, nullable=True)
    metadata_json = Column(JSON, nullable=True)
    callback_payload = Column(JSON, nullable=True)

    expires_at = Column(DateTime, nullable=True)

    transaction_id = Column(Integer, ForeignKey('transactions.id'), nullable=True)

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    user = relationship('User', backref='wata_payments')
    transaction = relationship('Transaction', backref='wata_payment')

    @property
    def amount_rubles(self) -> float:
        return self.amount_kopeks / 100

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f'<WataPayment(id={self.id}, link_id={self.payment_link_id}, amount={self.amount_rubles}₽, status={self.status})>'


class PlategaPayment(Base):
    __tablename__ = 'platega_payments'

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)

    platega_transaction_id = Column(String(255), unique=True, nullable=True, index=True)
    correlation_id = Column(String(64), unique=True, nullable=False, index=True)
    amount_kopeks = Column(Integer, nullable=False)
    currency = Column(String(10), nullable=False, default='RUB')
    description = Column(Text, nullable=True)

    payment_method_code = Column(Integer, nullable=False)
    status = Column(String(50), nullable=False, default='PENDING')
    is_paid = Column(Boolean, default=False)
    paid_at = Column(DateTime, nullable=True)

    redirect_url = Column(Text, nullable=True)
    return_url = Column(Text, nullable=True)
    failed_url = Column(Text, nullable=True)
    payload = Column(String(255), nullable=True)
    metadata_json = Column(JSON, nullable=True)
    callback_payload = Column(JSON, nullable=True)

    expires_at = Column(DateTime, nullable=True)

    transaction_id = Column(Integer, ForeignKey('transactions.id'), nullable=True)

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    user = relationship('User', backref='platega_payments')
    transaction = relationship('Transaction', backref='platega_payment')

    @property
    def amount_rubles(self) -> float:
        return self.amount_kopeks / 100

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f'<PlategaPayment(id={self.id}, transaction_id={self.platega_transaction_id}, amount={self.amount_rubles}₽, status={self.status}, method={self.payment_method_code})>'


class CloudPaymentsPayment(Base):
    __tablename__ = 'cloudpayments_payments'

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)

    # CloudPayments идентификаторы
    transaction_id_cp = Column(BigInteger, unique=True, nullable=True, index=True)  # TransactionId от CloudPayments
    invoice_id = Column(String(255), unique=True, nullable=False, index=True)  # Наш InvoiceId

    amount_kopeks = Column(Integer, nullable=False)
    currency = Column(String(10), nullable=False, default='RUB')
    description = Column(Text, nullable=True)

    status = Column(String(50), nullable=False, default='pending')  # pending, completed, failed, authorized
    is_paid = Column(Boolean, default=False)
    paid_at = Column(DateTime, nullable=True)

    # Данные карты (маскированные)
    card_first_six = Column(String(6), nullable=True)
    card_last_four = Column(String(4), nullable=True)
    card_type = Column(String(50), nullable=True)  # Visa, MasterCard, etc.
    card_exp_date = Column(String(10), nullable=True)  # MM/YY

    # Токен для рекуррентных платежей
    token = Column(String(255), nullable=True)

    # URL для оплаты (виджет)
    payment_url = Column(Text, nullable=True)

    # Email плательщика
    email = Column(String(255), nullable=True)

    # Тестовый режим
    test_mode = Column(Boolean, default=False)

    # Дополнительные данные
    metadata_json = Column(JSON, nullable=True)
    callback_payload = Column(JSON, nullable=True)

    # Связь с транзакцией в нашей системе
    transaction_id = Column(Integer, ForeignKey('transactions.id'), nullable=True)

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    user = relationship('User', backref='cloudpayments_payments')
    transaction = relationship('Transaction', backref='cloudpayments_payment')

    @property
    def amount_rubles(self) -> float:
        return self.amount_kopeks / 100

    @property
    def is_pending(self) -> bool:
        return self.status == 'pending'

    @property
    def is_completed(self) -> bool:
        return self.status == 'completed' and self.is_paid

    @property
    def is_failed(self) -> bool:
        return self.status == 'failed'

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f'<CloudPaymentsPayment(id={self.id}, invoice={self.invoice_id}, amount={self.amount_rubles}₽, status={self.status})>'


class FreekassaPayment(Base):
    __tablename__ = 'freekassa_payments'

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)

    # Идентификаторы
    order_id = Column(String(64), unique=True, nullable=False, index=True)  # Наш ID заказа
    freekassa_order_id = Column(String(64), unique=True, nullable=True, index=True)  # intid от Freekassa

    # Суммы
    amount_kopeks = Column(Integer, nullable=False)
    currency = Column(String(10), nullable=False, default='RUB')
    description = Column(Text, nullable=True)

    # Статусы
    status = Column(String(32), nullable=False, default='pending')  # pending, success, failed, expired
    is_paid = Column(Boolean, default=False)

    # Данные платежа
    payment_url = Column(Text, nullable=True)
    payment_system_id = Column(Integer, nullable=True)  # ID платежной системы FK

    # Метаданные
    metadata_json = Column(JSON, nullable=True)
    callback_payload = Column(JSON, nullable=True)

    # Временные метки
    paid_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    # Связь с транзакцией
    transaction_id = Column(Integer, ForeignKey('transactions.id'), nullable=True)

    # Relationships
    user = relationship('User', backref='freekassa_payments')
    transaction = relationship('Transaction', backref='freekassa_payment')

    @property
    def amount_rubles(self) -> float:
        return self.amount_kopeks / 100

    @property
    def is_pending(self) -> bool:
        return self.status == 'pending'

    @property
    def is_success(self) -> bool:
        return self.status == 'success' and self.is_paid

    @property
    def is_failed(self) -> bool:
        return self.status in ['failed', 'expired']

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f'<FreekassaPayment(id={self.id}, order_id={self.order_id}, amount={self.amount_rubles}₽, status={self.status})>'


class KassaAiPayment(Base):
    """Платежи через KassaAI (api.fk.life)."""

    __tablename__ = 'kassa_ai_payments'

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)

    # Идентификаторы
    order_id = Column(String(64), unique=True, nullable=False, index=True)  # Наш ID заказа
    kassa_ai_order_id = Column(String(64), unique=True, nullable=True, index=True)  # orderId от KassaAI

    # Суммы
    amount_kopeks = Column(Integer, nullable=False)
    currency = Column(String(10), nullable=False, default='RUB')
    description = Column(Text, nullable=True)

    # Статусы
    status = Column(String(32), nullable=False, default='pending')  # pending, success, failed, expired
    is_paid = Column(Boolean, default=False)

    # Данные платежа
    payment_url = Column(Text, nullable=True)
    payment_system_id = Column(Integer, nullable=True)  # ID платежной системы (44=СБП, 36=Карты, 43=SberPay)

    # Метаданные
    metadata_json = Column(JSON, nullable=True)
    callback_payload = Column(JSON, nullable=True)

    # Временные метки
    paid_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    # Связь с транзакцией
    transaction_id = Column(Integer, ForeignKey('transactions.id'), nullable=True)

    # Relationships
    user = relationship('User', backref='kassa_ai_payments')
    transaction = relationship('Transaction', backref='kassa_ai_payment')

    @property
    def amount_rubles(self) -> float:
        return self.amount_kopeks / 100

    @property
    def is_pending(self) -> bool:
        return self.status == 'pending'

    @property
    def is_success(self) -> bool:
        return self.status == 'success' and self.is_paid

    @property
    def is_failed(self) -> bool:
        return self.status in ['failed', 'expired']

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f'<KassaAiPayment(id={self.id}, order_id={self.order_id}, amount={self.amount_rubles}₽, status={self.status})>'


class PromoGroup(Base):
    __tablename__ = 'promo_groups'

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, nullable=False)
    priority = Column(Integer, nullable=False, default=0, index=True)
    server_discount_percent = Column(Integer, nullable=False, default=0)
    traffic_discount_percent = Column(Integer, nullable=False, default=0)
    device_discount_percent = Column(Integer, nullable=False, default=0)
    period_discounts = Column(JSON, nullable=True, default=dict)
    auto_assign_total_spent_kopeks = Column(Integer, nullable=True, default=None)
    apply_discounts_to_addons = Column(Boolean, nullable=False, default=True)
    is_default = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    users = relationship('User', back_populates='promo_group')
    user_promo_groups = relationship('UserPromoGroup', back_populates='promo_group', cascade='all, delete-orphan')
    server_squads = relationship(
        'ServerSquad',
        secondary=server_squad_promo_groups,
        back_populates='allowed_promo_groups',
        lazy='selectin',
    )

    def _get_period_discounts_map(self) -> dict[int, int]:
        raw_discounts = self.period_discounts or {}

        if isinstance(raw_discounts, dict):
            items = raw_discounts.items()
        else:
            items = []

        normalized: dict[int, int] = {}

        for key, value in items:
            try:
                period = int(key)
                percent = int(value)
            except (TypeError, ValueError):
                continue

            normalized[period] = max(0, min(100, percent))

        return normalized

    def _get_period_discount(self, period_days: int | None) -> int:
        if not period_days:
            return 0

        discounts = self._get_period_discounts_map()

        if period_days in discounts:
            return discounts[period_days]

        if self.is_default:
            try:
                from app.config import settings

                if settings.is_base_promo_group_period_discount_enabled():
                    config_discounts = settings.get_base_promo_group_period_discounts()
                    return config_discounts.get(period_days, 0)
            except Exception:
                return 0

        return 0

    def get_discount_percent(self, category: str, period_days: int | None = None) -> int:
        if category == 'period':
            return max(0, min(100, self._get_period_discount(period_days)))

        mapping = {
            'servers': self.server_discount_percent,
            'traffic': self.traffic_discount_percent,
            'devices': self.device_discount_percent,
        }
        percent = mapping.get(category) or 0

        if percent == 0 and self.is_default:
            base_period_discount = self._get_period_discount(period_days)
            percent = max(percent, base_period_discount)

        return max(0, min(100, percent))


class UserPromoGroup(Base):
    """Таблица связи Many-to-Many между пользователями и промогруппами."""

    __tablename__ = 'user_promo_groups'

    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    promo_group_id = Column(Integer, ForeignKey('promo_groups.id', ondelete='CASCADE'), primary_key=True)
    assigned_at = Column(DateTime, default=func.now())
    assigned_by = Column(String(50), default='system')

    user = relationship('User', back_populates='user_promo_groups')
    promo_group = relationship('PromoGroup', back_populates='user_promo_groups')

    def __repr__(self):
        return f"<UserPromoGroup(user_id={self.user_id}, promo_group_id={self.promo_group_id}, assigned_by='{self.assigned_by}')>"


class Tariff(Base):
    """Тарифный план для режима продаж 'Тарифы'."""

    __tablename__ = 'tariffs'

    id = Column(Integer, primary_key=True, index=True)

    # Основная информация
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    display_order = Column(Integer, default=0, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

    # Параметры тарифа
    traffic_limit_gb = Column(Integer, nullable=False, default=100)  # 0 = безлимит
    device_limit = Column(Integer, nullable=False, default=1)
    device_price_kopeks = Column(
        Integer, nullable=True, default=None
    )  # Цена за доп. устройство (None = нельзя докупить)
    max_device_limit = Column(Integer, nullable=True, default=None)  # Макс. устройств (None = без ограничений)

    # Сквады (серверы) доступные в тарифе
    allowed_squads = Column(JSON, default=list)  # список UUID сквадов

    # Лимиты трафика по серверам (JSON: {"uuid": {"traffic_limit_gb": 100}, ...})
    # Если сервер не указан - используется общий traffic_limit_gb
    server_traffic_limits = Column(JSON, default=dict)

    # Цены на периоды в копейках (JSON: {"14": 30000, "30": 50000, "90": 120000, ...})
    period_prices = Column(JSON, nullable=False, default=dict)

    # Уровень тарифа (для визуального отображения, 1 = базовый)
    tier_level = Column(Integer, default=1, nullable=False)

    # Дополнительные настройки
    is_trial_available = Column(Boolean, default=False, nullable=False)  # Можно ли взять триал на этом тарифе
    allow_traffic_topup = Column(Boolean, default=True, nullable=False)  # Разрешена ли докупка трафика для этого тарифа

    # Докупка трафика
    traffic_topup_enabled = Column(Boolean, default=False, nullable=False)  # Разрешена ли докупка трафика
    # Пакеты трафика: JSON {"5": 5000, "10": 9000, "20": 15000} (ГБ: цена в копейках)
    traffic_topup_packages = Column(JSON, default=dict)
    # Максимальный лимит трафика после докупки (0 = без ограничений)
    max_topup_traffic_gb = Column(Integer, default=0, nullable=False)

    # Суточный тариф - ежедневное списание
    is_daily = Column(Boolean, default=False, nullable=False)  # Является ли тариф суточным
    daily_price_kopeks = Column(Integer, default=0, nullable=False)  # Цена за день в копейках

    # Произвольное количество дней
    custom_days_enabled = Column(Boolean, default=False, nullable=False)  # Разрешить произвольное кол-во дней
    price_per_day_kopeks = Column(Integer, default=0, nullable=False)  # Цена за 1 день в копейках
    min_days = Column(Integer, default=1, nullable=False)  # Минимальное количество дней
    max_days = Column(Integer, default=365, nullable=False)  # Максимальное количество дней

    # Произвольный трафик при покупке
    custom_traffic_enabled = Column(Boolean, default=False, nullable=False)  # Разрешить произвольный трафик
    traffic_price_per_gb_kopeks = Column(Integer, default=0, nullable=False)  # Цена за 1 ГБ в копейках
    min_traffic_gb = Column(Integer, default=1, nullable=False)  # Минимальный трафик в ГБ
    max_traffic_gb = Column(Integer, default=1000, nullable=False)  # Максимальный трафик в ГБ

    # Режим сброса трафика: DAY, WEEK, MONTH, NO_RESET (по умолчанию берётся из конфига)
    traffic_reset_mode = Column(String(20), nullable=True, default=None)  # None = использовать глобальную настройку

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    # M2M связь с промогруппами (какие промогруппы имеют доступ к тарифу)
    allowed_promo_groups = relationship(
        'PromoGroup',
        secondary=tariff_promo_groups,
        lazy='selectin',
    )

    # Подписки на этом тарифе
    subscriptions = relationship('Subscription', back_populates='tariff')

    @property
    def is_unlimited_traffic(self) -> bool:
        """Проверяет, безлимитный ли трафик."""
        return self.traffic_limit_gb == 0

    def get_price_for_period(self, period_days: int) -> int | None:
        """Возвращает цену в копейках для указанного периода."""
        prices = self.period_prices or {}
        return prices.get(str(period_days))

    def get_available_periods(self) -> list[int]:
        """Возвращает список доступных периодов в днях."""
        prices = self.period_prices or {}
        return sorted([int(p) for p in prices.keys()])

    def get_price_rubles(self, period_days: int) -> float | None:
        """Возвращает цену в рублях для указанного периода."""
        price_kopeks = self.get_price_for_period(period_days)
        if price_kopeks is not None:
            return price_kopeks / 100
        return None

    def get_traffic_limit_for_server(self, squad_uuid: str) -> int:
        """Возвращает лимит трафика для конкретного сервера.

        Если для сервера настроен отдельный лимит - возвращает его,
        иначе возвращает общий traffic_limit_gb тарифа.
        """
        limits = self.server_traffic_limits or {}
        if squad_uuid in limits:
            server_limit = limits[squad_uuid]
            if isinstance(server_limit, dict) and 'traffic_limit_gb' in server_limit:
                return server_limit['traffic_limit_gb']
            if isinstance(server_limit, int):
                return server_limit
        return self.traffic_limit_gb

    def is_available_for_promo_group(self, promo_group_id: int | None) -> bool:
        """Проверяет, доступен ли тариф для указанной промогруппы."""
        if not self.allowed_promo_groups:
            return True  # Если нет ограничений - доступен всем
        if promo_group_id is None:
            return True  # Если у пользователя нет группы - доступен
        return any(pg.id == promo_group_id for pg in self.allowed_promo_groups)

    def get_traffic_topup_packages(self) -> dict[int, int]:
        """Возвращает пакеты трафика для докупки: {ГБ: цена в копейках}."""
        packages = self.traffic_topup_packages or {}
        return {int(gb): int(price) for gb, price in packages.items()}

    def get_traffic_topup_price(self, gb: int) -> int | None:
        """Возвращает цену в копейках для указанного пакета трафика."""
        packages = self.get_traffic_topup_packages()
        return packages.get(gb)

    def get_available_traffic_packages(self) -> list[int]:
        """Возвращает список доступных пакетов трафика в ГБ."""
        packages = self.get_traffic_topup_packages()
        return sorted(packages.keys())

    def can_topup_traffic(self) -> bool:
        """Проверяет, можно ли докупить трафик на этом тарифе."""
        return self.traffic_topup_enabled and bool(self.traffic_topup_packages) and not self.is_unlimited_traffic

    def get_daily_price_rubles(self) -> float:
        """Возвращает суточную цену в рублях."""
        return self.daily_price_kopeks / 100 if self.daily_price_kopeks else 0

    def get_price_for_custom_days(self, days: int) -> int | None:
        """Возвращает цену для произвольного количества дней."""
        if not self.custom_days_enabled or not self.price_per_day_kopeks:
            return None
        if days < self.min_days or days > self.max_days:
            return None
        return self.price_per_day_kopeks * days

    def get_price_for_custom_traffic(self, gb: int) -> int | None:
        """Возвращает цену для произвольного количества трафика."""
        if not self.custom_traffic_enabled or not self.traffic_price_per_gb_kopeks:
            return None
        if gb < self.min_traffic_gb or gb > self.max_traffic_gb:
            return None
        return self.traffic_price_per_gb_kopeks * gb

    def can_purchase_custom_days(self) -> bool:
        """Проверяет, можно ли купить произвольное количество дней."""
        return self.custom_days_enabled and self.price_per_day_kopeks > 0

    def can_purchase_custom_traffic(self) -> bool:
        """Проверяет, можно ли купить произвольный трафик."""
        return self.custom_traffic_enabled and self.traffic_price_per_gb_kopeks > 0

    def __repr__(self):
        return f"<Tariff(id={self.id}, name='{self.name}', tier={self.tier_level}, active={self.is_active})>"


class User(Base):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, unique=True, index=True, nullable=True)  # Nullable для email-only пользователей
    auth_type = Column(String(20), default='telegram', nullable=False)  # "telegram" или "email"
    username = Column(String(255), nullable=True)
    first_name = Column(String(255), nullable=True)
    last_name = Column(String(255), nullable=True)
    status = Column(String(20), default=UserStatus.ACTIVE.value)
    language = Column(String(5), default='ru')
    balance_kopeks = Column(Integer, default=0)
    used_promocodes = Column(Integer, default=0)
    has_had_paid_subscription = Column(Boolean, default=False, nullable=False)
    referred_by_id = Column(Integer, ForeignKey('users.id'), nullable=True)
    referral_code = Column(String(20), unique=True, nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    last_activity = Column(DateTime, default=func.now())
    remnawave_uuid = Column(String(255), nullable=True, unique=True)

    # Cabinet authentication fields
    email = Column(String(255), unique=True, nullable=True, index=True)
    email_verified = Column(Boolean, default=False, nullable=False)
    email_verified_at = Column(DateTime, nullable=True)
    password_hash = Column(String(255), nullable=True)
    email_verification_token = Column(String(255), nullable=True)
    email_verification_expires = Column(DateTime, nullable=True)
    password_reset_token = Column(String(255), nullable=True)
    password_reset_expires = Column(DateTime, nullable=True)
    cabinet_last_login = Column(DateTime, nullable=True)
    # Email change fields
    email_change_new = Column(String(255), nullable=True)  # New email pending verification
    email_change_code = Column(String(6), nullable=True)  # 6-digit verification code
    email_change_expires = Column(DateTime, nullable=True)  # Code expiration
    # OAuth provider IDs
    google_id = Column(String(255), unique=True, nullable=True, index=True)
    yandex_id = Column(String(255), unique=True, nullable=True, index=True)
    discord_id = Column(String(255), unique=True, nullable=True, index=True)
    vk_id = Column(BigInteger, unique=True, nullable=True, index=True)
    broadcasts = relationship('BroadcastHistory', back_populates='admin')
    referrals = relationship('User', backref='referrer', remote_side=[id], foreign_keys='User.referred_by_id')
    subscription = relationship('Subscription', back_populates='user', uselist=False)
    transactions = relationship('Transaction', back_populates='user')
    referral_earnings = relationship('ReferralEarning', foreign_keys='ReferralEarning.user_id', back_populates='user')
    discount_offers = relationship('DiscountOffer', back_populates='user')
    promo_offer_logs = relationship('PromoOfferLog', back_populates='user')
    lifetime_used_traffic_bytes = Column(BigInteger, default=0)
    auto_promo_group_assigned = Column(Boolean, nullable=False, default=False)
    auto_promo_group_threshold_kopeks = Column(BigInteger, nullable=False, default=0)
    referral_commission_percent = Column(Integer, nullable=True)
    promo_offer_discount_percent = Column(Integer, nullable=False, default=0)
    promo_offer_discount_source = Column(String(100), nullable=True)
    promo_offer_discount_expires_at = Column(DateTime, nullable=True)
    last_remnawave_sync = Column(DateTime, nullable=True)
    trojan_password = Column(String(255), nullable=True)
    vless_uuid = Column(String(255), nullable=True)
    ss_password = Column(String(255), nullable=True)
    has_made_first_topup: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    promo_group_id = Column(Integer, ForeignKey('promo_groups.id', ondelete='RESTRICT'), nullable=True, index=True)
    promo_group = relationship('PromoGroup', back_populates='users')
    user_promo_groups = relationship('UserPromoGroup', back_populates='user', cascade='all, delete-orphan')
    poll_responses = relationship('PollResponse', back_populates='user')
    notification_settings = Column(JSON, nullable=True, default=dict)
    last_pinned_message_id = Column(Integer, nullable=True)

    # Ограничения пользователя
    restriction_topup = Column(Boolean, default=False, nullable=False)  # Запрет пополнения
    restriction_subscription = Column(Boolean, default=False, nullable=False)  # Запрет продления/покупки
    restriction_reason = Column(String(500), nullable=True)  # Причина ограничения

    @property
    def has_restrictions(self) -> bool:
        """Проверить, есть ли у пользователя активные ограничения."""
        return self.restriction_topup or self.restriction_subscription

    @property
    def balance_rubles(self) -> float:
        return self.balance_kopeks / 100

    @property
    def full_name(self) -> str:
        """Полное имя пользователя с поддержкой email-only юзеров."""
        parts = [self.first_name, self.last_name]
        name = ' '.join(filter(None, parts))
        if name:
            return name
        if self.username:
            return self.username
        if self.telegram_id:
            return f'ID{self.telegram_id}'
        if self.email:
            return self.email.split('@')[0]
        return f'User{self.id}'

    @property
    def is_email_user(self) -> bool:
        """Пользователь зарегистрирован через email (без Telegram)."""
        return self.auth_type == 'email' and self.telegram_id is None

    @property
    def is_web_user(self) -> bool:
        """Пользователь без Telegram (email, OAuth и т.д.)."""
        return self.telegram_id is None

    def get_primary_promo_group(self):
        """Возвращает промогруппу с максимальным приоритетом."""
        if not self.user_promo_groups:
            return getattr(self, 'promo_group', None)

        try:
            # Сортируем по приоритету группы (убывание), затем по ID группы
            # Используем getattr для защиты от ленивой загрузки
            sorted_groups = sorted(
                self.user_promo_groups,
                key=lambda upg: (getattr(upg.promo_group, 'priority', 0) if upg.promo_group else 0, upg.promo_group_id),
                reverse=True,
            )

            if sorted_groups and sorted_groups[0].promo_group:
                return sorted_groups[0].promo_group
        except Exception:
            # Если возникла ошибка (например, ленивая загрузка), fallback на старую связь
            pass

        # Fallback на старую связь если новая пустая или возникла ошибка
        return getattr(self, 'promo_group', None)

    def get_promo_discount(self, category: str, period_days: int | None = None) -> int:
        primary_group = self.get_primary_promo_group()
        if not primary_group:
            return 0
        return primary_group.get_discount_percent(category, period_days)

    def add_balance(self, kopeks: int) -> None:
        self.balance_kopeks += kopeks

    def subtract_balance(self, kopeks: int) -> bool:
        if self.balance_kopeks >= kopeks:
            self.balance_kopeks -= kopeks
            return True
        return False


class Subscription(Base):
    __tablename__ = 'subscriptions'

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, unique=True)

    status = Column(String(20), default=SubscriptionStatus.TRIAL.value)
    is_trial = Column(Boolean, default=True)

    start_date = Column(DateTime, default=func.now())
    end_date = Column(DateTime, nullable=False)

    traffic_limit_gb = Column(Integer, default=0)
    traffic_used_gb = Column(Float, default=0.0)
    purchased_traffic_gb = Column(Integer, default=0)  # Докупленный трафик
    traffic_reset_at = Column(
        DateTime, nullable=True
    )  # Дата сброса докупленного трафика (30 дней после первой докупки)

    subscription_url = Column(String, nullable=True)
    subscription_crypto_link = Column(String, nullable=True)

    device_limit = Column(Integer, default=1)
    modem_enabled = Column(Boolean, default=False)

    connected_squads = Column(JSON, default=list)

    autopay_enabled = Column(Boolean, default=False)
    autopay_days_before = Column(Integer, default=3)

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    remnawave_short_uuid = Column(String(255), nullable=True)

    # Тариф (для режима продаж "Тарифы")
    tariff_id = Column(Integer, ForeignKey('tariffs.id', ondelete='SET NULL'), nullable=True, index=True)

    # Суточная подписка
    is_daily_paused = Column(
        Boolean, default=False, nullable=False
    )  # Приостановлена ли суточная подписка пользователем
    last_daily_charge_at = Column(DateTime, nullable=True)  # Время последнего суточного списания

    user = relationship('User', back_populates='subscription')
    tariff = relationship('Tariff', back_populates='subscriptions')
    discount_offers = relationship('DiscountOffer', back_populates='subscription')
    temporary_accesses = relationship('SubscriptionTemporaryAccess', back_populates='subscription')
    traffic_purchases = relationship('TrafficPurchase', back_populates='subscription', cascade='all, delete-orphan')

    @property
    def is_active(self) -> bool:
        current_time = datetime.utcnow()
        return self.status == SubscriptionStatus.ACTIVE.value and self.end_date > current_time

    @property
    def is_expired(self) -> bool:
        """Проверяет, истёк ли срок подписки"""
        return self.end_date <= datetime.utcnow()

    @property
    def should_be_expired(self) -> bool:
        current_time = datetime.utcnow()
        return self.status == SubscriptionStatus.ACTIVE.value and self.end_date <= current_time

    @property
    def actual_status(self) -> str:
        current_time = datetime.utcnow()

        if self.status == SubscriptionStatus.EXPIRED.value:
            return 'expired'

        if self.status == SubscriptionStatus.DISABLED.value:
            return 'disabled'

        if self.status == SubscriptionStatus.ACTIVE.value:
            if self.end_date <= current_time:
                return 'expired'
            return 'active'

        if self.status == SubscriptionStatus.TRIAL.value:
            if self.end_date <= current_time:
                return 'expired'
            return 'trial'

        return self.status

    @property
    def status_display(self) -> str:
        actual_status = self.actual_status
        datetime.utcnow()

        if actual_status == 'expired':
            return '🔴 Истекла'
        if actual_status == 'active':
            if self.is_trial:
                return '🎯 Тестовая'
            return '🟢 Активна'
        if actual_status == 'disabled':
            return '⚫ Отключена'
        if actual_status == 'trial':
            return '🎯 Тестовая'

        return '❓ Неизвестно'

    @property
    def status_emoji(self) -> str:
        actual_status = self.actual_status

        if actual_status == 'expired':
            return '🔴'
        if actual_status == 'active':
            if self.is_trial:
                return '🎁'
            return '💎'
        if actual_status == 'disabled':
            return '⚫'
        if actual_status == 'trial':
            return '🎁'

        return '❓'

    @property
    def days_left(self) -> int:
        current_time = datetime.utcnow()
        if self.end_date <= current_time:
            return 0
        delta = self.end_date - current_time
        return max(0, delta.days)

    @property
    def time_left_display(self) -> str:
        current_time = datetime.utcnow()
        if self.end_date <= current_time:
            return 'истёк'

        delta = self.end_date - current_time
        days = delta.days
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60

        if days > 0:
            return f'{days} дн.'
        if hours > 0:
            return f'{hours} ч.'
        return f'{minutes} мин.'

    @property
    def traffic_used_percent(self) -> float:
        if self.traffic_limit_gb == 0:
            return 0.0
        if self.traffic_limit_gb > 0:
            return min((self.traffic_used_gb / self.traffic_limit_gb) * 100, 100.0)
        return 0.0

    def extend_subscription(self, days: int):
        if self.end_date > datetime.utcnow():
            self.end_date = self.end_date + timedelta(days=days)
        else:
            self.end_date = datetime.utcnow() + timedelta(days=days)

        if self.status == SubscriptionStatus.EXPIRED.value:
            self.status = SubscriptionStatus.ACTIVE.value

    def add_traffic(self, gb: int):
        if self.traffic_limit_gb == 0:
            return
        self.traffic_limit_gb += gb

    @property
    def is_daily_tariff(self) -> bool:
        """Проверяет, является ли тариф подписки суточным."""
        if self.tariff:
            return getattr(self.tariff, 'is_daily', False)
        return False

    @property
    def daily_price_kopeks(self) -> int:
        """Возвращает суточную цену тарифа в копейках."""
        if self.tariff:
            return getattr(self.tariff, 'daily_price_kopeks', 0)
        return 0

    @property
    def can_charge_daily(self) -> bool:
        """Проверяет, можно ли списать суточную оплату."""
        if not self.is_daily_tariff:
            return False
        if self.is_daily_paused:
            return False
        if self.status != SubscriptionStatus.ACTIVE.value:
            return False
        return True


class TrafficPurchase(Base):
    """Докупка трафика с индивидуальной датой истечения."""

    __tablename__ = 'traffic_purchases'

    id = Column(Integer, primary_key=True, index=True)
    subscription_id = Column(Integer, ForeignKey('subscriptions.id', ondelete='CASCADE'), nullable=False, index=True)

    traffic_gb = Column(Integer, nullable=False)  # Количество ГБ в покупке
    expires_at = Column(DateTime, nullable=False, index=True)  # Дата истечения (покупка + 30 дней)

    created_at = Column(DateTime, default=func.now())

    subscription = relationship('Subscription', back_populates='traffic_purchases')

    @property
    def is_expired(self) -> bool:
        """Проверяет, истекла ли докупка."""
        return datetime.utcnow() >= self.expires_at


class Transaction(Base):
    __tablename__ = 'transactions'

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)

    type = Column(String(50), nullable=False)
    amount_kopeks = Column(Integer, nullable=False)
    description = Column(Text, nullable=True)

    payment_method = Column(String(50), nullable=True)
    external_id = Column(String(255), nullable=True)

    is_completed = Column(Boolean, default=True)

    # NaloGO чек
    receipt_uuid = Column(String(255), nullable=True, index=True)
    receipt_created_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=func.now())
    completed_at = Column(DateTime, nullable=True)

    user = relationship('User', back_populates='transactions')

    @property
    def amount_rubles(self) -> float:
        return self.amount_kopeks / 100


class SubscriptionConversion(Base):
    __tablename__ = 'subscription_conversions'

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)

    converted_at = Column(DateTime, default=func.now())

    trial_duration_days = Column(Integer, nullable=True)

    payment_method = Column(String(50), nullable=True)

    first_payment_amount_kopeks = Column(Integer, nullable=True)

    first_paid_period_days = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=func.now())

    user = relationship('User', backref='subscription_conversions')

    @property
    def first_payment_amount_rubles(self) -> float:
        return (self.first_payment_amount_kopeks or 0) / 100

    def __repr__(self):
        return f'<SubscriptionConversion(user_id={self.user_id}, converted_at={self.converted_at})>'


class PromoCode(Base):
    __tablename__ = 'promocodes'

    id = Column(Integer, primary_key=True, index=True)

    code = Column(String(50), unique=True, nullable=False, index=True)
    type = Column(String(50), nullable=False)

    balance_bonus_kopeks = Column(Integer, default=0)
    subscription_days = Column(Integer, default=0)

    max_uses = Column(Integer, default=1)
    current_uses = Column(Integer, default=0)

    valid_from = Column(DateTime, default=func.now())
    valid_until = Column(DateTime, nullable=True)

    is_active = Column(Boolean, default=True)
    first_purchase_only = Column(Boolean, default=False)  # Только для первой покупки

    created_by = Column(Integer, ForeignKey('users.id'), nullable=True)
    promo_group_id = Column(Integer, ForeignKey('promo_groups.id', ondelete='SET NULL'), nullable=True, index=True)

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    uses = relationship('PromoCodeUse', back_populates='promocode')
    promo_group = relationship('PromoGroup')

    @property
    def is_valid(self) -> bool:
        now = datetime.utcnow()
        return (
            self.is_active
            and self.current_uses < self.max_uses
            and self.valid_from <= now
            and (self.valid_until is None or self.valid_until >= now)
        )

    @property
    def uses_left(self) -> int:
        return max(0, self.max_uses - self.current_uses)


class PromoCodeUse(Base):
    __tablename__ = 'promocode_uses'

    id = Column(Integer, primary_key=True, index=True)
    promocode_id = Column(Integer, ForeignKey('promocodes.id'), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)

    used_at = Column(DateTime, default=func.now())

    promocode = relationship('PromoCode', back_populates='uses')
    user = relationship('User')


class ReferralEarning(Base):
    __tablename__ = 'referral_earnings'

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    referral_id = Column(Integer, ForeignKey('users.id'), nullable=False)

    amount_kopeks = Column(Integer, nullable=False)
    reason = Column(String(100), nullable=False)

    referral_transaction_id = Column(Integer, ForeignKey('transactions.id'), nullable=True)

    created_at = Column(DateTime, default=func.now())

    user = relationship('User', foreign_keys=[user_id], back_populates='referral_earnings')
    referral = relationship('User', foreign_keys=[referral_id])
    referral_transaction = relationship('Transaction')

    @property
    def amount_rubles(self) -> float:
        return self.amount_kopeks / 100


class WithdrawalRequestStatus(Enum):
    """Статусы заявки на вывод реферального баланса."""

    PENDING = 'pending'  # Ожидает рассмотрения
    APPROVED = 'approved'  # Одобрена
    REJECTED = 'rejected'  # Отклонена
    COMPLETED = 'completed'  # Выполнена (деньги переведены)
    CANCELLED = 'cancelled'  # Отменена пользователем


class WithdrawalRequest(Base):
    """Заявка на вывод реферального баланса."""

    __tablename__ = 'withdrawal_requests'

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)

    amount_kopeks = Column(Integer, nullable=False)  # Сумма к выводу
    status = Column(String(50), default=WithdrawalRequestStatus.PENDING.value, nullable=False)

    # Данные для вывода (заполняет пользователь)
    payment_details = Column(Text, nullable=True)  # Реквизиты для перевода

    # Анализ на отмывание
    risk_score = Column(Integer, default=0)  # 0-100, чем выше — тем подозрительнее
    risk_analysis = Column(Text, nullable=True)  # JSON с деталями анализа

    # Обработка админом
    processed_by = Column(Integer, ForeignKey('users.id'), nullable=True)
    processed_at = Column(DateTime, nullable=True)
    admin_comment = Column(Text, nullable=True)

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    user = relationship('User', foreign_keys=[user_id], backref='withdrawal_requests')
    admin = relationship('User', foreign_keys=[processed_by])

    @property
    def amount_rubles(self) -> float:
        return self.amount_kopeks / 100


class ReferralContest(Base):
    __tablename__ = 'referral_contests'

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    prize_text = Column(Text, nullable=True)
    contest_type = Column(String(50), nullable=False, default='referral_paid')
    start_at = Column(DateTime, nullable=False)
    end_at = Column(DateTime, nullable=False)
    daily_summary_time = Column(Time, nullable=False, default=time(hour=12, minute=0))
    daily_summary_times = Column(String(255), nullable=True)  # CSV HH:MM
    timezone = Column(String(64), nullable=False, default='UTC')
    is_active = Column(Boolean, nullable=False, default=True)
    last_daily_summary_date = Column(Date, nullable=True)
    last_daily_summary_at = Column(DateTime, nullable=True)
    final_summary_sent = Column(Boolean, nullable=False, default=False)
    created_by = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    creator = relationship('User', backref='created_referral_contests')
    events = relationship(
        'ReferralContestEvent',
        back_populates='contest',
        cascade='all, delete-orphan',
    )

    def __repr__(self):
        return f"<ReferralContest id={self.id} title='{self.title}'>"


class ReferralContestEvent(Base):
    __tablename__ = 'referral_contest_events'
    __table_args__ = (
        UniqueConstraint(
            'contest_id',
            'referral_id',
            name='uq_referral_contest_referral',
        ),
        Index('idx_referral_contest_referrer', 'contest_id', 'referrer_id'),
    )

    id = Column(Integer, primary_key=True, index=True)
    contest_id = Column(Integer, ForeignKey('referral_contests.id', ondelete='CASCADE'), nullable=False)
    referrer_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    referral_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    event_type = Column(String(50), nullable=False)
    amount_kopeks = Column(Integer, nullable=False, default=0)
    occurred_at = Column(DateTime, nullable=False, default=func.now())

    contest = relationship('ReferralContest', back_populates='events')
    referrer = relationship('User', foreign_keys=[referrer_id])
    referral = relationship('User', foreign_keys=[referral_id])

    def __repr__(self):
        return (
            f'<ReferralContestEvent contest={self.contest_id} referrer={self.referrer_id} referral={self.referral_id}>'
        )


class ReferralContestVirtualParticipant(Base):
    __tablename__ = 'referral_contest_virtual_participants'

    id = Column(Integer, primary_key=True, index=True)
    contest_id = Column(Integer, ForeignKey('referral_contests.id', ondelete='CASCADE'), nullable=False)
    display_name = Column(String(255), nullable=False)
    referral_count = Column(Integer, nullable=False, default=0)
    total_amount_kopeks = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=func.now())

    contest = relationship('ReferralContest')

    def __repr__(self):
        return (
            f"<ReferralContestVirtualParticipant id={self.id} name='{self.display_name}' count={self.referral_count}>"
        )


class ContestTemplate(Base):
    __tablename__ = 'contest_templates'

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    slug = Column(String(50), nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    prize_type = Column(String(20), nullable=False, default='days')
    prize_value = Column(String(50), nullable=False, default='1')
    max_winners = Column(Integer, nullable=False, default=1)
    attempts_per_user = Column(Integer, nullable=False, default=1)
    times_per_day = Column(Integer, nullable=False, default=1)
    schedule_times = Column(String(255), nullable=True)  # CSV of HH:MM in local TZ
    cooldown_hours = Column(Integer, nullable=False, default=24)
    payload = Column(JSON, nullable=True)
    is_enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    rounds = relationship('ContestRound', back_populates='template')


class ContestRound(Base):
    __tablename__ = 'contest_rounds'
    __table_args__ = (
        Index('idx_contest_round_status', 'status'),
        Index('idx_contest_round_template', 'template_id'),
    )

    id = Column(Integer, primary_key=True, index=True)
    template_id = Column(Integer, ForeignKey('contest_templates.id', ondelete='CASCADE'), nullable=False)
    starts_at = Column(DateTime, nullable=False)
    ends_at = Column(DateTime, nullable=False)
    status = Column(String(20), nullable=False, default='active')  # active, finished
    payload = Column(JSON, nullable=True)
    winners_count = Column(Integer, nullable=False, default=0)
    max_winners = Column(Integer, nullable=False, default=1)
    attempts_per_user = Column(Integer, nullable=False, default=1)
    message_id = Column(BigInteger, nullable=True)
    chat_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    template = relationship('ContestTemplate', back_populates='rounds')
    attempts = relationship('ContestAttempt', back_populates='round', cascade='all, delete-orphan')


class ContestAttempt(Base):
    __tablename__ = 'contest_attempts'
    __table_args__ = (
        UniqueConstraint('round_id', 'user_id', name='uq_round_user_attempt'),
        Index('idx_contest_attempt_round', 'round_id'),
    )

    id = Column(Integer, primary_key=True, index=True)
    round_id = Column(Integer, ForeignKey('contest_rounds.id', ondelete='CASCADE'), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    answer = Column(Text, nullable=True)
    is_winner = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=func.now())

    round = relationship('ContestRound', back_populates='attempts')
    user = relationship('User')


class Squad(Base):
    __tablename__ = 'squads'

    id = Column(Integer, primary_key=True, index=True)

    uuid = Column(String(255), unique=True, nullable=False)
    name = Column(String(255), nullable=False)
    country_code = Column(String(5), nullable=True)

    is_available = Column(Boolean, default=True)
    price_kopeks = Column(Integer, default=0)

    description = Column(Text, nullable=True)

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    @property
    def price_rubles(self) -> float:
        return self.price_kopeks / 100


class ServiceRule(Base):
    __tablename__ = 'service_rules'

    id = Column(Integer, primary_key=True, index=True)

    order = Column(Integer, default=0)
    title = Column(String(255), nullable=False)

    content = Column(Text, nullable=False)

    is_active = Column(Boolean, default=True)

    language = Column(String(5), default='ru')

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class PrivacyPolicy(Base):
    __tablename__ = 'privacy_policies'

    id = Column(Integer, primary_key=True, index=True)
    language = Column(String(10), nullable=False, unique=True)
    content = Column(Text, nullable=False)
    is_enabled = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class PublicOffer(Base):
    __tablename__ = 'public_offers'

    id = Column(Integer, primary_key=True, index=True)
    language = Column(String(10), nullable=False, unique=True)
    content = Column(Text, nullable=False)
    is_enabled = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class FaqSetting(Base):
    __tablename__ = 'faq_settings'

    id = Column(Integer, primary_key=True, index=True)
    language = Column(String(10), nullable=False, unique=True)
    is_enabled = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class FaqPage(Base):
    __tablename__ = 'faq_pages'

    id = Column(Integer, primary_key=True, index=True)
    language = Column(String(10), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    content = Column(Text, nullable=False)
    display_order = Column(Integer, default=0, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class SystemSetting(Base):
    __tablename__ = 'system_settings'

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(255), unique=True, nullable=False)
    value = Column(Text, nullable=True)
    description = Column(Text, nullable=True)

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class MonitoringLog(Base):
    __tablename__ = 'monitoring_logs'

    id = Column(Integer, primary_key=True, index=True)

    event_type = Column(String(100), nullable=False)

    message = Column(Text, nullable=False)
    data = Column(JSON, nullable=True)

    is_success = Column(Boolean, default=True)

    created_at = Column(DateTime, default=func.now())


class SentNotification(Base):
    __tablename__ = 'sent_notifications'

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    subscription_id = Column(Integer, ForeignKey('subscriptions.id', ondelete='CASCADE'), nullable=False)
    notification_type = Column(String(50), nullable=False)
    days_before = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=func.now())

    user = relationship('User', backref='sent_notifications')
    subscription = relationship('Subscription', backref='sent_notifications')


class SubscriptionEvent(Base):
    __tablename__ = 'subscription_events'

    id = Column(Integer, primary_key=True, index=True)
    event_type = Column(String(50), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    subscription_id = Column(Integer, ForeignKey('subscriptions.id', ondelete='SET NULL'), nullable=True)
    transaction_id = Column(Integer, ForeignKey('transactions.id', ondelete='SET NULL'), nullable=True)
    amount_kopeks = Column(Integer, nullable=True)
    currency = Column(String(16), nullable=True)
    message = Column(Text, nullable=True)
    occurred_at = Column(DateTime, nullable=False, default=func.now())
    extra = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=func.now())

    user = relationship('User', backref='subscription_events')
    subscription = relationship('Subscription', backref='subscription_events')
    transaction = relationship('Transaction', backref='subscription_events')


class DiscountOffer(Base):
    __tablename__ = 'discount_offers'
    __table_args__ = (Index('ix_discount_offers_user_type', 'user_id', 'notification_type'),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    subscription_id = Column(Integer, ForeignKey('subscriptions.id', ondelete='SET NULL'), nullable=True)
    notification_type = Column(String(50), nullable=False)
    discount_percent = Column(Integer, nullable=False, default=0)
    bonus_amount_kopeks = Column(Integer, nullable=False, default=0)
    expires_at = Column(DateTime, nullable=False)
    claimed_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    effect_type = Column(String(50), nullable=False, default='percent_discount')
    extra_data = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    user = relationship('User', back_populates='discount_offers')
    subscription = relationship('Subscription', back_populates='discount_offers')
    logs = relationship('PromoOfferLog', back_populates='offer')


class PromoOfferTemplate(Base):
    __tablename__ = 'promo_offer_templates'
    __table_args__ = (Index('ix_promo_offer_templates_type', 'offer_type'),)

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    offer_type = Column(String(50), nullable=False)
    message_text = Column(Text, nullable=False)
    button_text = Column(String(255), nullable=False)
    valid_hours = Column(Integer, nullable=False, default=24)
    discount_percent = Column(Integer, nullable=False, default=0)
    bonus_amount_kopeks = Column(Integer, nullable=False, default=0)
    active_discount_hours = Column(Integer, nullable=True)
    test_duration_hours = Column(Integer, nullable=True)
    test_squad_uuids = Column(JSON, default=list)
    is_active = Column(Boolean, default=True, nullable=False)
    created_by = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    creator = relationship('User')


class SubscriptionTemporaryAccess(Base):
    __tablename__ = 'subscription_temporary_access'

    id = Column(Integer, primary_key=True, index=True)
    subscription_id = Column(Integer, ForeignKey('subscriptions.id', ondelete='CASCADE'), nullable=False)
    offer_id = Column(Integer, ForeignKey('discount_offers.id', ondelete='CASCADE'), nullable=False)
    squad_uuid = Column(String(255), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=func.now())
    deactivated_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    was_already_connected = Column(Boolean, default=False, nullable=False)

    subscription = relationship('Subscription', back_populates='temporary_accesses')
    offer = relationship('DiscountOffer')


class PromoOfferLog(Base):
    __tablename__ = 'promo_offer_logs'

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    offer_id = Column(Integer, ForeignKey('discount_offers.id', ondelete='SET NULL'), nullable=True, index=True)
    action = Column(String(50), nullable=False)
    source = Column(String(100), nullable=True)
    percent = Column(Integer, nullable=True)
    effect_type = Column(String(50), nullable=True)
    details = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=func.now())

    user = relationship('User', back_populates='promo_offer_logs')
    offer = relationship('DiscountOffer', back_populates='logs')


class BroadcastHistory(Base):
    __tablename__ = 'broadcast_history'

    id = Column(Integer, primary_key=True, index=True)
    target_type = Column(String(100), nullable=False)
    message_text = Column(Text, nullable=True)  # Nullable for email-only broadcasts
    has_media = Column(Boolean, default=False)
    media_type = Column(String(20), nullable=True)
    media_file_id = Column(String(255), nullable=True)
    media_caption = Column(Text, nullable=True)
    total_count = Column(Integer, default=0)
    sent_count = Column(Integer, default=0)
    failed_count = Column(Integer, default=0)
    status = Column(String(50), default='in_progress')
    admin_id = Column(Integer, ForeignKey('users.id'))
    admin_name = Column(String(255))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # Email broadcast fields
    channel = Column(String(20), default='telegram', nullable=False)  # telegram|email|both
    email_subject = Column(String(255), nullable=True)
    email_html_content = Column(Text, nullable=True)

    admin = relationship('User', back_populates='broadcasts')


class Poll(Base):
    __tablename__ = 'polls'

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    reward_enabled = Column(Boolean, nullable=False, default=False)
    reward_amount_kopeks = Column(Integer, nullable=False, default=0)
    created_by = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)

    creator = relationship('User', backref='created_polls', foreign_keys=[created_by])
    questions = relationship(
        'PollQuestion',
        back_populates='poll',
        cascade='all, delete-orphan',
        order_by='PollQuestion.order',
    )
    responses = relationship(
        'PollResponse',
        back_populates='poll',
        cascade='all, delete-orphan',
    )


class PollQuestion(Base):
    __tablename__ = 'poll_questions'

    id = Column(Integer, primary_key=True, index=True)
    poll_id = Column(Integer, ForeignKey('polls.id', ondelete='CASCADE'), nullable=False, index=True)
    text = Column(Text, nullable=False)
    order = Column(Integer, nullable=False, default=0)

    poll = relationship('Poll', back_populates='questions')
    options = relationship(
        'PollOption',
        back_populates='question',
        cascade='all, delete-orphan',
        order_by='PollOption.order',
    )
    answers = relationship('PollAnswer', back_populates='question')


class PollOption(Base):
    __tablename__ = 'poll_options'

    id = Column(Integer, primary_key=True, index=True)
    question_id = Column(Integer, ForeignKey('poll_questions.id', ondelete='CASCADE'), nullable=False, index=True)
    text = Column(Text, nullable=False)
    order = Column(Integer, nullable=False, default=0)

    question = relationship('PollQuestion', back_populates='options')
    answers = relationship('PollAnswer', back_populates='option')


class PollResponse(Base):
    __tablename__ = 'poll_responses'

    id = Column(Integer, primary_key=True, index=True)
    poll_id = Column(Integer, ForeignKey('polls.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    sent_at = Column(DateTime, default=func.now(), nullable=False)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    reward_given = Column(Boolean, nullable=False, default=False)
    reward_amount_kopeks = Column(Integer, nullable=False, default=0)

    poll = relationship('Poll', back_populates='responses')
    user = relationship('User', back_populates='poll_responses')
    answers = relationship(
        'PollAnswer',
        back_populates='response',
        cascade='all, delete-orphan',
    )

    __table_args__ = (UniqueConstraint('poll_id', 'user_id', name='uq_poll_user'),)


class PollAnswer(Base):
    __tablename__ = 'poll_answers'

    id = Column(Integer, primary_key=True, index=True)
    response_id = Column(Integer, ForeignKey('poll_responses.id', ondelete='CASCADE'), nullable=False, index=True)
    question_id = Column(Integer, ForeignKey('poll_questions.id', ondelete='CASCADE'), nullable=False, index=True)
    option_id = Column(Integer, ForeignKey('poll_options.id', ondelete='CASCADE'), nullable=False, index=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)

    response = relationship('PollResponse', back_populates='answers')
    question = relationship('PollQuestion', back_populates='answers')
    option = relationship('PollOption', back_populates='answers')

    __table_args__ = (UniqueConstraint('response_id', 'question_id', name='uq_poll_answer_unique'),)


class ServerSquad(Base):
    __tablename__ = 'server_squads'

    id = Column(Integer, primary_key=True, index=True)

    squad_uuid = Column(String(255), unique=True, nullable=False, index=True)

    display_name = Column(String(255), nullable=False)

    original_name = Column(String(255), nullable=True)

    country_code = Column(String(5), nullable=True)

    is_available = Column(Boolean, default=True)
    is_trial_eligible = Column(Boolean, default=False, nullable=False)

    price_kopeks = Column(Integer, default=0)

    description = Column(Text, nullable=True)

    sort_order = Column(Integer, default=0)

    max_users = Column(Integer, nullable=True)
    current_users = Column(Integer, default=0)

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    allowed_promo_groups = relationship(
        'PromoGroup',
        secondary=server_squad_promo_groups,
        back_populates='server_squads',
        lazy='selectin',
    )

    @property
    def price_rubles(self) -> float:
        return self.price_kopeks / 100

    @property
    def is_full(self) -> bool:
        if self.max_users is None:
            return False
        return self.current_users >= self.max_users

    @property
    def availability_status(self) -> str:
        if not self.is_available:
            return 'Недоступен'
        if self.is_full:
            return 'Переполнен'
        return 'Доступен'


class SubscriptionServer(Base):
    __tablename__ = 'subscription_servers'

    id = Column(Integer, primary_key=True, index=True)
    subscription_id = Column(Integer, ForeignKey('subscriptions.id'), nullable=False)
    server_squad_id = Column(Integer, ForeignKey('server_squads.id'), nullable=False)

    connected_at = Column(DateTime, default=func.now())

    paid_price_kopeks = Column(Integer, default=0)

    subscription = relationship('Subscription', backref='subscription_servers')
    server_squad = relationship('ServerSquad', backref='subscription_servers')


class SupportAuditLog(Base):
    __tablename__ = 'support_audit_logs'

    id = Column(Integer, primary_key=True, index=True)
    actor_user_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    actor_telegram_id = Column(BigInteger, nullable=True)  # Can be None for email-only users
    is_moderator = Column(Boolean, default=False)
    action = Column(String(50), nullable=False)  # close_ticket, block_user_timed, block_user_perm, unblock_user
    ticket_id = Column(Integer, ForeignKey('tickets.id', ondelete='SET NULL'), nullable=True)
    target_user_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    details = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=func.now())

    actor = relationship('User', foreign_keys=[actor_user_id])
    ticket = relationship('Ticket', foreign_keys=[ticket_id])


class UserMessage(Base):
    __tablename__ = 'user_messages'
    id = Column(Integer, primary_key=True, index=True)
    message_text = Column(Text, nullable=False)
    is_active = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)
    created_by = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    creator = relationship('User', backref='created_messages')

    def __repr__(self):
        return f"<UserMessage(id={self.id}, active={self.is_active}, text='{self.message_text[:50]}...')>"


class WelcomeText(Base):
    __tablename__ = 'welcome_texts'

    id = Column(Integer, primary_key=True, index=True)
    text_content = Column(Text, nullable=False)
    is_active = Column(Boolean, default=True)
    is_enabled = Column(Boolean, default=True)
    created_by = Column(Integer, ForeignKey('users.id'), nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    creator = relationship('User', backref='created_welcome_texts')


class PinnedMessage(Base):
    __tablename__ = 'pinned_messages'

    id = Column(Integer, primary_key=True, index=True)
    content = Column(Text, nullable=False, default='')
    media_type = Column(String(32), nullable=True)
    media_file_id = Column(String(255), nullable=True)
    send_before_menu = Column(Boolean, nullable=False, server_default='1', default=True)
    send_on_every_start = Column(Boolean, nullable=False, server_default='1', default=True)
    is_active = Column(Boolean, default=True)
    created_by = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    creator = relationship('User', backref='pinned_messages')


class AdvertisingCampaign(Base):
    __tablename__ = 'advertising_campaigns'

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    start_parameter = Column(String(64), nullable=False, unique=True, index=True)
    bonus_type = Column(String(20), nullable=False)

    balance_bonus_kopeks = Column(Integer, default=0)

    subscription_duration_days = Column(Integer, nullable=True)
    subscription_traffic_gb = Column(Integer, nullable=True)
    subscription_device_limit = Column(Integer, nullable=True)
    subscription_squads = Column(JSON, default=list)

    # Поля для типа "tariff" - выдача тарифа
    tariff_id = Column(Integer, ForeignKey('tariffs.id', ondelete='SET NULL'), nullable=True)
    tariff_duration_days = Column(Integer, nullable=True)

    is_active = Column(Boolean, default=True)

    created_by = Column(Integer, ForeignKey('users.id'), nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    registrations = relationship('AdvertisingCampaignRegistration', back_populates='campaign')
    tariff = relationship('Tariff', foreign_keys=[tariff_id])

    @property
    def is_balance_bonus(self) -> bool:
        return self.bonus_type == 'balance'

    @property
    def is_subscription_bonus(self) -> bool:
        return self.bonus_type == 'subscription'

    @property
    def is_none_bonus(self) -> bool:
        """Ссылка без награды - только для отслеживания."""
        return self.bonus_type == 'none'

    @property
    def is_tariff_bonus(self) -> bool:
        """Выдача тарифа на определённое время."""
        return self.bonus_type == 'tariff'


class AdvertisingCampaignRegistration(Base):
    __tablename__ = 'advertising_campaign_registrations'
    __table_args__ = (UniqueConstraint('campaign_id', 'user_id', name='uq_campaign_user'),)

    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey('advertising_campaigns.id', ondelete='CASCADE'), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)

    bonus_type = Column(String(20), nullable=False)
    balance_bonus_kopeks = Column(Integer, default=0)
    subscription_duration_days = Column(Integer, nullable=True)

    # Поля для типа "tariff"
    tariff_id = Column(Integer, ForeignKey('tariffs.id', ondelete='SET NULL'), nullable=True)
    tariff_duration_days = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=func.now())

    campaign = relationship('AdvertisingCampaign', back_populates='registrations')
    user = relationship('User')
    tariff = relationship('Tariff')

    @property
    def balance_bonus_rubles(self) -> float:
        return (self.balance_bonus_kopeks or 0) / 100


class TicketStatus(Enum):
    OPEN = 'open'
    ANSWERED = 'answered'
    CLOSED = 'closed'
    PENDING = 'pending'


class Ticket(Base):
    __tablename__ = 'tickets'

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)

    title = Column(String(255), nullable=False)
    status = Column(String(20), default=TicketStatus.OPEN.value, nullable=False)
    priority = Column(String(20), default='normal', nullable=False)  # low, normal, high, urgent
    # Блокировка ответов пользователя в этом тикете
    user_reply_block_permanent = Column(Boolean, default=False, nullable=False)
    user_reply_block_until = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    closed_at = Column(DateTime, nullable=True)
    # SLA reminders
    last_sla_reminder_at = Column(DateTime, nullable=True)

    # Связи
    user = relationship('User', backref='tickets')
    messages = relationship('TicketMessage', back_populates='ticket', cascade='all, delete-orphan')

    @property
    def is_open(self) -> bool:
        return self.status == TicketStatus.OPEN.value

    @property
    def is_answered(self) -> bool:
        return self.status == TicketStatus.ANSWERED.value

    @property
    def is_closed(self) -> bool:
        return self.status == TicketStatus.CLOSED.value

    @property
    def is_pending(self) -> bool:
        return self.status == TicketStatus.PENDING.value

    @property
    def is_user_reply_blocked(self) -> bool:
        if self.user_reply_block_permanent:
            return True
        if self.user_reply_block_until:
            try:
                from datetime import datetime

                return self.user_reply_block_until > datetime.utcnow()
            except Exception:
                return True
        return False

    @property
    def status_emoji(self) -> str:
        status_emojis = {
            TicketStatus.OPEN.value: '🔴',
            TicketStatus.ANSWERED.value: '🟡',
            TicketStatus.CLOSED.value: '🟢',
            TicketStatus.PENDING.value: '⏳',
        }
        return status_emojis.get(self.status, '❓')

    @property
    def priority_emoji(self) -> str:
        priority_emojis = {'low': '🟢', 'normal': '🟡', 'high': '🟠', 'urgent': '🔴'}
        return priority_emojis.get(self.priority, '🟡')

    def __repr__(self):
        return f"<Ticket(id={self.id}, user_id={self.user_id}, status={self.status}, title='{self.title[:30]}...')>"


class TicketMessage(Base):
    __tablename__ = 'ticket_messages'

    id = Column(Integer, primary_key=True, index=True)
    ticket_id = Column(Integer, ForeignKey('tickets.id', ondelete='CASCADE'), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)

    message_text = Column(Text, nullable=False)
    is_from_admin = Column(Boolean, default=False, nullable=False)

    # Для медиа файлов
    has_media = Column(Boolean, default=False)
    media_type = Column(String(20), nullable=True)  # photo, video, document, voice, etc.
    media_file_id = Column(String(255), nullable=True)
    media_caption = Column(Text, nullable=True)

    created_at = Column(DateTime, default=func.now())

    # Связи
    ticket = relationship('Ticket', back_populates='messages')
    user = relationship('User')

    @property
    def is_user_message(self) -> bool:
        return not self.is_from_admin

    @property
    def is_admin_message(self) -> bool:
        return self.is_from_admin

    def __repr__(self):
        return f"<TicketMessage(id={self.id}, ticket_id={self.ticket_id}, is_admin={self.is_from_admin}, text='{self.message_text[:30]}...')>"


class WebApiToken(Base):
    __tablename__ = 'web_api_tokens'

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    token_hash = Column(String(128), nullable=False, unique=True, index=True)
    token_prefix = Column(String(32), nullable=False, index=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    expires_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    last_used_ip = Column(String(64), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_by = Column(String(255), nullable=True)

    def __repr__(self) -> str:
        status = 'active' if self.is_active else 'revoked'
        return f"<WebApiToken id={self.id} name='{self.name}' status={status}>"


class MainMenuButton(Base):
    __tablename__ = 'main_menu_buttons'

    id = Column(Integer, primary_key=True, index=True)
    text = Column(String(64), nullable=False)
    action_type = Column(String(20), nullable=False)
    action_value = Column(Text, nullable=False)
    visibility = Column(String(20), nullable=False, default=MainMenuButtonVisibility.ALL.value)
    is_active = Column(Boolean, nullable=False, default=True)
    display_order = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    __table_args__ = (Index('ix_main_menu_buttons_order', 'display_order', 'id'),)

    @property
    def action_type_enum(self) -> MainMenuButtonActionType:
        try:
            return MainMenuButtonActionType(self.action_type)
        except ValueError:
            return MainMenuButtonActionType.URL

    @property
    def visibility_enum(self) -> MainMenuButtonVisibility:
        try:
            return MainMenuButtonVisibility(self.visibility)
        except ValueError:
            return MainMenuButtonVisibility.ALL

    def __repr__(self) -> str:
        return (
            f"<MainMenuButton id={self.id} text='{self.text}' "
            f'action={self.action_type} visibility={self.visibility} active={self.is_active}>'
        )


class MenuLayoutHistory(Base):
    """История изменений конфигурации меню."""

    __tablename__ = 'menu_layout_history'

    id = Column(Integer, primary_key=True, index=True)
    config_json = Column(Text, nullable=False)  # Полная конфигурация в JSON
    action = Column(String(50), nullable=False)  # update, reset, import
    changes_summary = Column(Text, nullable=True)  # Краткое описание изменений
    user_info = Column(String(255), nullable=True)  # Информация о пользователе/токене
    created_at = Column(DateTime, default=func.now(), index=True)

    __table_args__ = (Index('ix_menu_layout_history_created', 'created_at'),)

    def __repr__(self) -> str:
        return f"<MenuLayoutHistory id={self.id} action='{self.action}' created_at={self.created_at}>"


class ButtonClickLog(Base):
    """Логи кликов по кнопкам меню."""

    __tablename__ = 'button_click_logs'

    id = Column(Integer, primary_key=True, index=True)
    button_id = Column(String(100), nullable=False, index=True)  # ID кнопки
    user_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    callback_data = Column(String(255), nullable=True)  # callback_data кнопки
    clicked_at = Column(DateTime, default=func.now(), index=True)

    # Дополнительная информация
    button_type = Column(String(20), nullable=True)  # builtin, callback, url, mini_app
    button_text = Column(String(255), nullable=True)  # Текст кнопки на момент клика

    __table_args__ = (
        Index('ix_button_click_logs_button_date', 'button_id', 'clicked_at'),
        Index('ix_button_click_logs_user_date', 'user_id', 'clicked_at'),
    )

    # Связи
    user = relationship('User', foreign_keys=[user_id])

    def __repr__(self) -> str:
        return f"<ButtonClickLog id={self.id} button='{self.button_id}' user={self.user_id} at={self.clicked_at}>"


class Webhook(Base):
    """Webhook конфигурация для подписки на события."""

    __tablename__ = 'webhooks'
    __table_args__ = (
        Index('ix_webhooks_event_type', 'event_type'),
        Index('ix_webhooks_is_active', 'is_active'),
    )

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    url = Column(Text, nullable=False)
    secret = Column(String(128), nullable=True)  # Секрет для подписи payload
    event_type = Column(String(50), nullable=False)  # user.created, payment.completed, ticket.created, etc.
    is_active = Column(Boolean, default=True, nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    last_triggered_at = Column(DateTime, nullable=True)
    failure_count = Column(Integer, default=0, nullable=False)
    success_count = Column(Integer, default=0, nullable=False)

    deliveries = relationship('WebhookDelivery', back_populates='webhook', cascade='all, delete-orphan')

    def __repr__(self) -> str:
        status = 'active' if self.is_active else 'inactive'
        return f"<Webhook id={self.id} name='{self.name}' event='{self.event_type}' status={status}>"


class WebhookDelivery(Base):
    """История доставки webhooks."""

    __tablename__ = 'webhook_deliveries'
    __table_args__ = (
        Index('ix_webhook_deliveries_webhook_created', 'webhook_id', 'created_at'),
        Index('ix_webhook_deliveries_status', 'status'),
    )

    id = Column(Integer, primary_key=True, index=True)
    webhook_id = Column(Integer, ForeignKey('webhooks.id', ondelete='CASCADE'), nullable=False)
    event_type = Column(String(50), nullable=False)
    payload = Column(JSON, nullable=False)  # Отправленный payload
    response_status = Column(Integer, nullable=True)  # HTTP статус ответа
    response_body = Column(Text, nullable=True)  # Тело ответа (может быть обрезано)
    status = Column(String(20), nullable=False)  # pending, success, failed
    error_message = Column(Text, nullable=True)
    attempt_number = Column(Integer, default=1, nullable=False)
    created_at = Column(DateTime, default=func.now())
    delivered_at = Column(DateTime, nullable=True)
    next_retry_at = Column(DateTime, nullable=True)

    webhook = relationship('Webhook', back_populates='deliveries')

    def __repr__(self) -> str:
        return f"<WebhookDelivery id={self.id} webhook_id={self.webhook_id} status='{self.status}' event='{self.event_type}'>"


class CabinetRefreshToken(Base):
    """Refresh tokens for cabinet JWT authentication."""

    __tablename__ = 'cabinet_refresh_tokens'
    __table_args__ = (Index('ix_cabinet_refresh_tokens_user', 'user_id'),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    token_hash = Column(String(255), unique=True, nullable=False, index=True)
    device_info = Column(String(500), nullable=True)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=func.now())
    revoked_at = Column(DateTime, nullable=True)

    user = relationship('User', backref='cabinet_tokens')

    @property
    def is_expired(self) -> bool:
        return datetime.utcnow() > self.expires_at

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_valid(self) -> bool:
        return not self.is_expired and not self.is_revoked

    def __repr__(self) -> str:
        status = 'valid' if self.is_valid else ('revoked' if self.is_revoked else 'expired')
        return f'<CabinetRefreshToken id={self.id} user_id={self.user_id} status={status}>'


# ==================== FORTUNE WHEEL ====================


class WheelConfig(Base):
    """Глобальная конфигурация колеса удачи."""

    __tablename__ = 'wheel_configs'

    id = Column(Integer, primary_key=True, index=True)

    # Основные настройки
    is_enabled = Column(Boolean, default=False, nullable=False)
    name = Column(String(255), default='Колесо удачи', nullable=False)

    # Стоимость спина
    spin_cost_stars = Column(Integer, default=10, nullable=False)  # Стоимость в Stars
    spin_cost_days = Column(Integer, default=1, nullable=False)  # Стоимость в днях подписки
    spin_cost_stars_enabled = Column(Boolean, default=True, nullable=False)
    spin_cost_days_enabled = Column(Boolean, default=True, nullable=False)

    # RTP настройки (Return to Player) - процент возврата 0-100
    rtp_percent = Column(Integer, default=80, nullable=False)

    # Лимиты
    daily_spin_limit = Column(Integer, default=5, nullable=False)  # 0 = без лимита
    min_subscription_days_for_day_payment = Column(Integer, default=3, nullable=False)

    # Генерация промокодов
    promo_prefix = Column(String(20), default='WHEEL', nullable=False)
    promo_validity_days = Column(Integer, default=7, nullable=False)

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    prizes = relationship('WheelPrize', back_populates='config', cascade='all, delete-orphan')

    def __repr__(self) -> str:
        return f'<WheelConfig id={self.id} enabled={self.is_enabled} rtp={self.rtp_percent}%>'


class WheelPrize(Base):
    """Приз на колесе удачи."""

    __tablename__ = 'wheel_prizes'

    id = Column(Integer, primary_key=True, index=True)
    config_id = Column(Integer, ForeignKey('wheel_configs.id', ondelete='CASCADE'), nullable=False)

    # Тип и значение приза
    prize_type = Column(String(50), nullable=False)  # WheelPrizeType
    prize_value = Column(Integer, default=0, nullable=False)  # Дни/копейки/GB в зависимости от типа

    # Отображение
    display_name = Column(String(100), nullable=False)
    emoji = Column(String(10), default='🎁', nullable=False)
    color = Column(String(20), default='#3B82F6', nullable=False)  # HEX цвет сектора

    # Стоимость приза для расчета RTP (в копейках)
    prize_value_kopeks = Column(Integer, default=0, nullable=False)

    # Порядок и вероятность
    sort_order = Column(Integer, default=0, nullable=False)
    manual_probability = Column(Float, nullable=True)  # Если задано - игнорирует RTP расчет (0.0-1.0)
    is_active = Column(Boolean, default=True, nullable=False)

    # Настройки генерируемого промокода (только для prize_type=promocode)
    promo_balance_bonus_kopeks = Column(Integer, default=0)
    promo_subscription_days = Column(Integer, default=0)
    promo_traffic_gb = Column(Integer, default=0)

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    config = relationship('WheelConfig', back_populates='prizes')
    spins = relationship('WheelSpin', back_populates='prize')

    def __repr__(self) -> str:
        return f"<WheelPrize id={self.id} type={self.prize_type} name='{self.display_name}'>"


class WheelSpin(Base):
    """История спинов колеса удачи."""

    __tablename__ = 'wheel_spins'
    __table_args__ = (Index('ix_wheel_spins_user_created', 'user_id', 'created_at'),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    prize_id = Column(Integer, ForeignKey('wheel_prizes.id', ondelete='SET NULL'), nullable=True)

    # Способ оплаты
    payment_type = Column(String(50), nullable=False)  # WheelSpinPaymentType
    payment_amount = Column(Integer, nullable=False)  # Stars или дни
    payment_value_kopeks = Column(Integer, nullable=False)  # Эквивалент в копейках для статистики

    # Результат
    prize_type = Column(String(50), nullable=False)  # Копируем из WheelPrize на момент спина
    prize_value = Column(Integer, nullable=False)
    prize_display_name = Column(String(100), nullable=False)
    prize_value_kopeks = Column(Integer, nullable=False)  # Стоимость приза в копейках

    # Сгенерированный промокод (если приз - промокод)
    generated_promocode_id = Column(Integer, ForeignKey('promocodes.id'), nullable=True)

    # Флаг успешного начисления
    is_applied = Column(Boolean, default=False, nullable=False)
    applied_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=func.now())

    user = relationship('User', backref='wheel_spins')
    prize = relationship('WheelPrize', back_populates='spins')
    generated_promocode = relationship('PromoCode')

    @property
    def prize_value_rubles(self) -> float:
        """Стоимость приза в рублях."""
        return self.prize_value_kopeks / 100

    @property
    def payment_value_rubles(self) -> float:
        """Стоимость оплаты в рублях."""
        return self.payment_value_kopeks / 100

    def __repr__(self) -> str:
        return f"<WheelSpin id={self.id} user_id={self.user_id} prize='{self.prize_display_name}'>"


class TicketNotification(Base):
    """Уведомления о тикетах для кабинета (веб-интерфейс)."""

    __tablename__ = 'ticket_notifications'
    __table_args__ = (
        Index('ix_ticket_notifications_user_read', 'user_id', 'is_read'),
        Index('ix_ticket_notifications_admin_read', 'is_for_admin', 'is_read'),
    )

    id = Column(Integer, primary_key=True, index=True)
    ticket_id = Column(Integer, ForeignKey('tickets.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)

    # Тип уведомления: new_ticket, admin_reply, user_reply
    notification_type = Column(String(50), nullable=False)

    # Текст уведомления
    message = Column(Text, nullable=True)

    # Для админа или для пользователя
    is_for_admin = Column(Boolean, default=False, nullable=False)

    # Прочитано ли уведомление
    is_read = Column(Boolean, default=False, nullable=False)

    created_at = Column(DateTime, default=func.now())
    read_at = Column(DateTime, nullable=True)

    ticket = relationship('Ticket', backref='notifications')
    user = relationship('User', backref='ticket_notifications')

    def __repr__(self) -> str:
        return f'<TicketNotification id={self.id} type={self.notification_type} for_admin={self.is_for_admin}>'


# ==================== PAYMENT METHOD CONFIG ====================


class PaymentMethodConfig(Base):
    """Конфигурация отображения платёжных методов в кабинете."""

    __tablename__ = 'payment_method_configs'

    id = Column(Integer, primary_key=True, index=True)

    # Уникальный идентификатор метода (совпадает с PaymentMethod enum: 'yookassa', 'cryptobot' и т.д.)
    method_id = Column(String(50), unique=True, nullable=False, index=True)

    # Порядок отображения (меньше = выше)
    sort_order = Column(Integer, nullable=False, default=0, index=True)

    # Включён/выключен (дополнительно к env-переменным)
    is_enabled = Column(Boolean, nullable=False, default=True)

    # Переопределение отображаемого имени (null = использовать из env)
    display_name = Column(String(255), nullable=True)

    # Под-опции включения/выключения (JSON): {"card": true, "sbp": false}
    # Для методов с вариантами: yookassa, pal24, platega
    sub_options = Column(JSON, nullable=True, default=None)

    # Переопределение мин/макс сумм (null = из env)
    min_amount_kopeks = Column(Integer, nullable=True)
    max_amount_kopeks = Column(Integer, nullable=True)

    # --- Условия отображения ---

    # Фильтр по типу пользователя: 'all', 'telegram', 'email'
    user_type_filter = Column(String(20), nullable=False, default='all')

    # Фильтр по первому пополнению: 'any', 'yes' (делал), 'no' (не делал)
    first_topup_filter = Column(String(10), nullable=False, default='any')

    # Режим фильтра промо-групп: 'all' (все видят), 'selected' (только выбранные)
    promo_group_filter_mode = Column(String(20), nullable=False, default='all')

    # M2M связь с промогруппами
    allowed_promo_groups = relationship(
        'PromoGroup',
        secondary=payment_method_promo_groups,
        lazy='selectin',
    )

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<PaymentMethodConfig method_id='{self.method_id}' order={self.sort_order} enabled={self.is_enabled}>"
