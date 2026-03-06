"""Пакет с mixin-классами, делающими платёжный сервис модульным.

Здесь собираем все вспомогательные части, чтобы основной `PaymentService`
оставался компактным и импортировал только нужные компоненты.
"""

from .cloudpayments import CloudPaymentsPaymentMixin
from .common import PaymentCommonMixin
from .cryptobot import CryptoBotPaymentMixin
from .freekassa import FreekassaPaymentMixin
from .heleket import HeleketPaymentMixin
from .kassa_ai import KassaAiPaymentMixin
from .mulenpay import MulenPayPaymentMixin
from .pal24 import Pal24PaymentMixin
from .platega import PlategaPaymentMixin
from .shkeeper import ShkeeperPaymentMixin
from .stars import TelegramStarsMixin
from .tribute import TributePaymentMixin
from .wata import WataPaymentMixin
from .yookassa import YooKassaPaymentMixin


__all__ = [
    'CloudPaymentsPaymentMixin',
    'CryptoBotPaymentMixin',
    'FreekassaPaymentMixin',
    'HeleketPaymentMixin',
    'KassaAiPaymentMixin',
    'MulenPayPaymentMixin',
    'Pal24PaymentMixin',
    'PaymentCommonMixin',
    'PlategaPaymentMixin',
    'ShkeeperPaymentMixin',
    'TelegramStarsMixin',
    'TributePaymentMixin',
    'WataPaymentMixin',
    'YooKassaPaymentMixin',
]
