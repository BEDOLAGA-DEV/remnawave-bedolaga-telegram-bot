"""Агрегирующий сервис, собирающий все платёжные модули."""

from __future__ import annotations

from importlib import import_module

import structlog
from aiogram import Bot

from app.config import settings
from app.external.cryptobot import CryptoBotService
from app.external.heleket import HeleketService
from app.external.telegram_stars import TelegramStarsService
from app.services.cloudpayments_service import CloudPaymentsService
from app.services.mulenpay_service import MulenPayService
from app.services.nalogo_service import NaloGoService
from app.services.pal24_service import Pal24Service
from app.services.payment import (
    CryptoBotPaymentMixin,
    HeleketPaymentMixin,
    MulenPayPaymentMixin,
    Pal24PaymentMixin,
    PaymentCommonMixin,
    PlategaPaymentMixin,
    ShkeeperPaymentMixin,
    TelegramStarsMixin,
    TributePaymentMixin,
    WataPaymentMixin,
    YooKassaPaymentMixin,
)
from app.services.payment.cloudpayments import CloudPaymentsPaymentMixin
from app.services.payment.freekassa import FreekassaPaymentMixin
from app.services.payment.kassa_ai import KassaAiPaymentMixin
from app.services.platega_service import PlategaService
from app.services.shkeeper_service import ShkeeperService
from app.services.wata_service import WataService
from app.services.yookassa_service import YooKassaService
from app.utils.currency_converter import currency_converter  # noqa: F401


logger = structlog.get_logger(__name__)


# --- Совместимость: экспортируем функции, которые активно мокаются в тестах ---


async def create_yookassa_payment(*args, **kwargs):
    yk_crud = import_module('app.database.crud.yookassa')
    return await yk_crud.create_yookassa_payment(*args, **kwargs)


async def update_yookassa_payment_status(*args, **kwargs):
    yk_crud = import_module('app.database.crud.yookassa')
    return await yk_crud.update_yookassa_payment_status(*args, **kwargs)


async def link_yookassa_payment_to_transaction(*args, **kwargs):
    yk_crud = import_module('app.database.crud.yookassa')
    return await yk_crud.link_yookassa_payment_to_transaction(*args, **kwargs)


async def get_yookassa_payment_by_id(*args, **kwargs):
    yk_crud = import_module('app.database.crud.yookassa')
    return await yk_crud.get_yookassa_payment_by_id(*args, **kwargs)


async def get_yookassa_payment_by_local_id(*args, **kwargs):
    yk_crud = import_module('app.database.crud.yookassa')
    return await yk_crud.get_yookassa_payment_by_local_id(*args, **kwargs)


async def create_transaction(*args, **kwargs):
    transaction_crud = import_module('app.database.crud.transaction')
    return await transaction_crud.create_transaction(*args, **kwargs)


async def get_transaction_by_external_id(*args, **kwargs):
    transaction_crud = import_module('app.database.crud.transaction')
    return await transaction_crud.get_transaction_by_external_id(*args, **kwargs)


async def add_user_balance(*args, **kwargs):
    user_crud = import_module('app.database.crud.user')
    return await user_crud.add_user_balance(*args, **kwargs)


async def get_user_by_id(*args, **kwargs):
    user_crud = import_module('app.database.crud.user')
    return await user_crud.get_user_by_id(*args, **kwargs)


async def get_user_by_telegram_id(*args, **kwargs):
    user_crud = import_module('app.database.crud.user')
    return await user_crud.get_user_by_telegram_id(*args, **kwargs)


async def create_mulenpay_payment(*args, **kwargs):
    mulenpay_crud = import_module('app.database.crud.mulenpay')
    return await mulenpay_crud.create_mulenpay_payment(*args, **kwargs)


async def get_mulenpay_payment_by_uuid(*args, **kwargs):
    mulenpay_crud = import_module('app.database.crud.mulenpay')
    return await mulenpay_crud.get_mulenpay_payment_by_uuid(*args, **kwargs)


async def get_mulenpay_payment_by_mulen_id(*args, **kwargs):
    mulenpay_crud = import_module('app.database.crud.mulenpay')
    return await mulenpay_crud.get_mulenpay_payment_by_mulen_id(*args, **kwargs)


async def get_mulenpay_payment_by_local_id(*args, **kwargs):
    mulenpay_crud = import_module('app.database.crud.mulenpay')
    return await mulenpay_crud.get_mulenpay_payment_by_local_id(*args, **kwargs)


async def update_mulenpay_payment_status(*args, **kwargs):
    mulenpay_crud = import_module('app.database.crud.mulenpay')
    return await mulenpay_crud.update_mulenpay_payment_status(*args, **kwargs)


async def update_mulenpay_payment_metadata(*args, **kwargs):
    mulenpay_crud = import_module('app.database.crud.mulenpay')
    return await mulenpay_crud.update_mulenpay_payment_metadata(*args, **kwargs)


async def link_mulenpay_payment_to_transaction(*args, **kwargs):
    mulenpay_crud = import_module('app.database.crud.mulenpay')
    return await mulenpay_crud.link_mulenpay_payment_to_transaction(*args, **kwargs)


async def create_pal24_payment(*args, **kwargs):
    pal_crud = import_module('app.database.crud.pal24')
    return await pal_crud.create_pal24_payment(*args, **kwargs)


async def get_pal24_payment_by_bill_id(*args, **kwargs):
    pal_crud = import_module('app.database.crud.pal24')
    return await pal_crud.get_pal24_payment_by_bill_id(*args, **kwargs)


async def get_pal24_payment_by_order_id(*args, **kwargs):
    pal_crud = import_module('app.database.crud.pal24')
    return await pal_crud.get_pal24_payment_by_order_id(*args, **kwargs)


async def get_pal24_payment_by_id(*args, **kwargs):
    pal_crud = import_module('app.database.crud.pal24')
    return await pal_crud.get_pal24_payment_by_id(*args, **kwargs)


async def update_pal24_payment_status(*args, **kwargs):
    pal_crud = import_module('app.database.crud.pal24')
    return await pal_crud.update_pal24_payment_status(*args, **kwargs)


async def link_pal24_payment_to_transaction(*args, **kwargs):
    pal_crud = import_module('app.database.crud.pal24')
    return await pal_crud.link_pal24_payment_to_transaction(*args, **kwargs)


async def create_wata_payment(*args, **kwargs):
    wata_crud = import_module('app.database.crud.wata')
    return await wata_crud.create_wata_payment(*args, **kwargs)


async def get_wata_payment_by_link_id(*args, **kwargs):
    wata_crud = import_module('app.database.crud.wata')
    return await wata_crud.get_wata_payment_by_link_id(*args, **kwargs)


async def get_wata_payment_by_id(*args, **kwargs):
    wata_crud = import_module('app.database.crud.wata')
    return await wata_crud.get_wata_payment_by_id(*args, **kwargs)


# Алиас для совместимости с хендлерами
async def get_wata_payment_by_local_id(*args, **kwargs):
    return await get_wata_payment_by_id(*args, **kwargs)


async def get_wata_payment_by_order_id(*args, **kwargs):
    wata_crud = import_module('app.database.crud.wata')
    return await wata_crud.get_wata_payment_by_order_id(*args, **kwargs)


async def update_wata_payment_status(*args, **kwargs):
    wata_crud = import_module('app.database.crud.wata')
    return await wata_crud.update_wata_payment_status(*args, **kwargs)


async def link_wata_payment_to_transaction(*args, **kwargs):
    wata_crud = import_module('app.database.crud.wata')
    return await wata_crud.link_wata_payment_to_transaction(*args, **kwargs)


async def create_platega_payment(*args, **kwargs):
    platega_crud = import_module('app.database.crud.platega')
    return await platega_crud.create_platega_payment(*args, **kwargs)


async def get_platega_payment_by_id(*args, **kwargs):
    platega_crud = import_module('app.database.crud.platega')
    return await platega_crud.get_platega_payment_by_id(*args, **kwargs)


async def get_platega_payment_by_id_for_update(*args, **kwargs):
    platega_crud = import_module('app.database.crud.platega')
    return await platega_crud.get_platega_payment_by_id_for_update(*args, **kwargs)


async def get_platega_payment_by_transaction_id(*args, **kwargs):
    platega_crud = import_module('app.database.crud.platega')
    return await platega_crud.get_platega_payment_by_transaction_id(*args, **kwargs)


async def get_platega_payment_by_correlation_id(*args, **kwargs):
    platega_crud = import_module('app.database.crud.platega')
    return await platega_crud.get_platega_payment_by_correlation_id(*args, **kwargs)


async def update_platega_payment(*args, **kwargs):
    platega_crud = import_module('app.database.crud.platega')
    return await platega_crud.update_platega_payment(*args, **kwargs)


async def link_platega_payment_to_transaction(*args, **kwargs):
    platega_crud = import_module('app.database.crud.platega')
    return await platega_crud.link_platega_payment_to_transaction(*args, **kwargs)


async def create_cryptobot_payment(*args, **kwargs):
    crypto_crud = import_module('app.database.crud.cryptobot')
    return await crypto_crud.create_cryptobot_payment(*args, **kwargs)


async def get_cryptobot_payment_by_invoice_id(*args, **kwargs):
    crypto_crud = import_module('app.database.crud.cryptobot')
    return await crypto_crud.get_cryptobot_payment_by_invoice_id(*args, **kwargs)


async def update_cryptobot_payment_status(*args, **kwargs):
    crypto_crud = import_module('app.database.crud.cryptobot')
    return await crypto_crud.update_cryptobot_payment_status(*args, **kwargs)


async def link_cryptobot_payment_to_transaction(*args, **kwargs):
    crypto_crud = import_module('app.database.crud.cryptobot')
    return await crypto_crud.link_cryptobot_payment_to_transaction(*args, **kwargs)


async def create_heleket_payment(*args, **kwargs):
    heleket_crud = import_module('app.database.crud.heleket')
    return await heleket_crud.create_heleket_payment(*args, **kwargs)


async def get_heleket_payment_by_uuid(*args, **kwargs):
    heleket_crud = import_module('app.database.crud.heleket')
    return await heleket_crud.get_heleket_payment_by_uuid(*args, **kwargs)


async def get_heleket_payment_by_id(*args, **kwargs):
    heleket_crud = import_module('app.database.crud.heleket')
    return await heleket_crud.get_heleket_payment_by_id(*args, **kwargs)


async def update_heleket_payment(*args, **kwargs):
    heleket_crud = import_module('app.database.crud.heleket')
    return await heleket_crud.update_heleket_payment(*args, **kwargs)


async def link_heleket_payment_to_transaction(*args, **kwargs):
    heleket_crud = import_module('app.database.crud.heleket')
    return await heleket_crud.link_heleket_payment_to_transaction(*args, **kwargs)


async def create_cloudpayments_payment(*args, **kwargs):
    cloudpayments_crud = import_module('app.database.crud.cloudpayments')
    return await cloudpayments_crud.create_cloudpayments_payment(*args, **kwargs)


async def get_cloudpayments_payment_by_invoice_id(*args, **kwargs):
    cloudpayments_crud = import_module('app.database.crud.cloudpayments')
    return await cloudpayments_crud.get_cloudpayments_payment_by_invoice_id(*args, **kwargs)


async def get_cloudpayments_payment_by_id(*args, **kwargs):
    cloudpayments_crud = import_module('app.database.crud.cloudpayments')
    return await cloudpayments_crud.get_cloudpayments_payment_by_id(*args, **kwargs)


async def update_cloudpayments_payment(*args, **kwargs):
    cloudpayments_crud = import_module('app.database.crud.cloudpayments')
    return await cloudpayments_crud.update_cloudpayments_payment(*args, **kwargs)


async def create_shkeeper_payment(*args, **kwargs):
    shkeeper_crud = import_module('app.database.crud.shkeeper')
    return await shkeeper_crud.create_shkeeper_payment(*args, **kwargs)


async def get_shkeeper_payment_by_id(*args, **kwargs):
    shkeeper_crud = import_module('app.database.crud.shkeeper')
    return await shkeeper_crud.get_shkeeper_payment_by_id(*args, **kwargs)


async def get_shkeeper_payment_by_order_id(*args, **kwargs):
    shkeeper_crud = import_module('app.database.crud.shkeeper')
    return await shkeeper_crud.get_shkeeper_payment_by_order_id(*args, **kwargs)


async def get_shkeeper_payment_by_external_id(*args, **kwargs):
    shkeeper_crud = import_module('app.database.crud.shkeeper')
    return await shkeeper_crud.get_shkeeper_payment_by_external_id(*args, **kwargs)


async def get_shkeeper_payment_by_invoice_id(*args, **kwargs):
    shkeeper_crud = import_module('app.database.crud.shkeeper')
    return await shkeeper_crud.get_shkeeper_payment_by_invoice_id(*args, **kwargs)


async def update_shkeeper_payment_status(*args, **kwargs):
    shkeeper_crud = import_module('app.database.crud.shkeeper')
    return await shkeeper_crud.update_shkeeper_payment_status(*args, **kwargs)


class PaymentService(
    PaymentCommonMixin,
    TelegramStarsMixin,
    YooKassaPaymentMixin,
    TributePaymentMixin,
    CryptoBotPaymentMixin,
    HeleketPaymentMixin,
    MulenPayPaymentMixin,
    Pal24PaymentMixin,
    PlategaPaymentMixin,
    WataPaymentMixin,
    CloudPaymentsPaymentMixin,
    FreekassaPaymentMixin,
    KassaAiPaymentMixin,
    ShkeeperPaymentMixin,
):
    """Основной интерфейс платежей, делегирующий работу специализированным mixin-ам."""

    def __init__(self, bot: Bot | None = None) -> None:
        # Бот нужен для отправки уведомлений и создания звёздных инвойсов.
        self.bot = bot
        # Ниже инициализируем службы-обёртки только если соответствующий провайдер включён.
        self.yookassa_service = YooKassaService() if settings.is_yookassa_enabled() else None
        self.stars_service = TelegramStarsService(bot) if bot else None
        self.cryptobot_service = CryptoBotService() if settings.is_cryptobot_enabled() else None
        self.heleket_service = HeleketService() if settings.is_heleket_enabled() else None
        self.mulenpay_service = MulenPayService() if settings.is_mulenpay_enabled() else None
        self.pal24_service = Pal24Service() if settings.is_pal24_enabled() else None
        self.platega_service = PlategaService() if settings.is_platega_enabled() else None
        self.wata_service = WataService() if settings.is_wata_enabled() else None
        self.cloudpayments_service = CloudPaymentsService() if settings.is_cloudpayments_enabled() else None
        self.shkeeper_service = ShkeeperService() if settings.is_shkeeper_enabled() else None
        self.nalogo_service = NaloGoService() if settings.is_nalogo_enabled() else None

        mulenpay_name = settings.get_mulenpay_display_name()
        logger.debug(
            'PaymentService инициализирован (YooKassa Stars CryptoBot Heleket Pal24 Platega Wata CloudPayments SHKeeper=)',
            yookassa_service=bool(self.yookassa_service),
            stars_service=bool(self.stars_service),
            cryptobot_service=bool(self.cryptobot_service),
            heleket_service=bool(self.heleket_service),
            mulenpay_name=mulenpay_name,
            mulenpay_service=bool(self.mulenpay_service),
            pal24_service=bool(self.pal24_service),
            platega_service=bool(self.platega_service),
            wata_service=bool(self.wata_service),
            cloudpayments_service=bool(self.cloudpayments_service),
            shkeeper_service=bool(self.shkeeper_service),
        )
