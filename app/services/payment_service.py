"""Агрегирующий сервис, собирающий все платёжные модули."""

from __future__ import annotations

import json
from importlib import import_module
from typing import Any

import structlog
from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.external.cryptobot import CryptoBotService
from app.external.heleket import HeleketService
from app.external.telegram_stars import TelegramStarsService
from app.services.cloudpayments_service import CloudPaymentsService
from app.services.mulenpay_service import MulenPayService
from app.services.nalogo_service import NaloGoService
from app.services.pal24_service import Pal24Service
from app.services.payment import (
    AppleIAPPaymentMixin,
    CryptoBotPaymentMixin,
    HeleketPaymentMixin,
    MulenPayPaymentMixin,
    Pal24PaymentMixin,
    PaymentCommonMixin,
    PlategaPaymentMixin,
    TelegramStarsMixin,
    TributePaymentMixin,
    WataPaymentMixin,
    YooKassaPaymentMixin,
)
from app.services.payment.cloudpayments import CloudPaymentsPaymentMixin
from app.services.payment.freekassa import FreekassaPaymentMixin
from app.services.payment.kassa_ai import KassaAiPaymentMixin
from app.services.payment.riopay import RioPayPaymentMixin
from app.services.payment.severpay import SeverPayPaymentMixin
from app.services.platega_service import PlategaService
from app.services.wata_service import WataService
from app.services.yookassa_service import YooKassaService
from app.utils.currency_converter import currency_converter


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


async def create_severpay_payment(*args, **kwargs):
    severpay_crud = import_module('app.database.crud.severpay')
    return await severpay_crud.create_severpay_payment(*args, **kwargs)


async def get_severpay_payment_by_order_id(*args, **kwargs):
    severpay_crud = import_module('app.database.crud.severpay')
    return await severpay_crud.get_severpay_payment_by_order_id(*args, **kwargs)


async def get_severpay_payment_by_severpay_id(*args, **kwargs):
    severpay_crud = import_module('app.database.crud.severpay')
    return await severpay_crud.get_severpay_payment_by_severpay_id(*args, **kwargs)


async def get_severpay_payment_by_id(*args, **kwargs):
    severpay_crud = import_module('app.database.crud.severpay')
    return await severpay_crud.get_severpay_payment_by_id(*args, **kwargs)


async def get_severpay_payment_by_id_for_update(*args, **kwargs):
    severpay_crud = import_module('app.database.crud.severpay')
    return await severpay_crud.get_severpay_payment_by_id_for_update(*args, **kwargs)


async def update_severpay_payment_status(*args, **kwargs):
    severpay_crud = import_module('app.database.crud.severpay')
    return await severpay_crud.update_severpay_payment_status(*args, **kwargs)


async def link_severpay_payment_to_transaction(*args, **kwargs):
    severpay_crud = import_module('app.database.crud.severpay')
    return await severpay_crud.link_severpay_payment_to_transaction(*args, **kwargs)


# Mapping from model_name to getter function name for providers
# where it differs from the standard get_{model_name}_payment_by_id pattern.
_GETTER_OVERRIDES: dict[str, str] = {
    'mulenpay': 'get_mulenpay_payment_by_local_id',
}


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
    RioPayPaymentMixin,
    SeverPayPaymentMixin,
    AppleIAPPaymentMixin,
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
        self.nalogo_service = NaloGoService() if settings.is_nalogo_enabled() else None
        self.apple_iap_service = None
        if settings.is_apple_iap_enabled():
            from app.external.apple_iap import AppleIAPService

            self.apple_iap_service = AppleIAPService()

        mulenpay_name = settings.get_mulenpay_display_name()
        logger.debug(
            'PaymentService инициализирован (YooKassa Stars CryptoBot Heleket Pal24 Platega Wata CloudPayments=)',
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
        )

    # ------------------------------------------------------------------
    # Guest (landing page) payments
    # ------------------------------------------------------------------
    # Supported providers for guest payments (to be extended per-provider):
    #   - yookassa (card, sbp)
    #   - cryptobot
    #   - heleket
    #   - mulenpay
    #   - pal24
    #   - platega
    #   - wata
    #   - cloudpayments
    #   - freekassa
    #   - kassa_ai
    #   - telegram_stars
    #   - tribute
    # Each provider returns a different result dict; the caller must
    # extract the payment URL from the provider-specific key.
    # ------------------------------------------------------------------

    async def create_guest_payment(
        self,
        db: AsyncSession,
        *,
        amount_kopeks: int,
        payment_method: str,
        description: str,
        purchase_token: str,
        return_url: str,
    ) -> dict[str, Any] | None:
        """Create a payment for a guest (unauthenticated) landing-page purchase.

        Stores ``purchase_token`` in payment metadata so that webhook handlers
        can match the completed payment back to the corresponding
        :class:`GuestPurchase` record.

        Returns a provider-specific dict with at least ``payment_url`` on
        success, or ``None`` when the requested provider is unavailable or
        the creation call fails.
        """
        guest_metadata: dict[str, Any] = {
            'purpose': 'guest_purchase',
            'purchase_token': purchase_token,
            'source': 'landing',
        }

        async def _patch_guest_metadata(local_payment_id: int, model_name: str) -> None:
            """Merge guest_metadata into the local payment record's metadata_json."""
            try:
                crud_module = import_module(f'app.database.crud.{model_name}')
                getter_name = _GETTER_OVERRIDES.get(model_name, f'get_{model_name}_payment_by_id')
                getter = getattr(crud_module, getter_name, None)
                if getter is None:
                    logger.warning(
                        'No getter found for patching guest metadata', model_name=model_name, getter_name=getter_name
                    )
                    return
                payment_record = await getter(db, local_payment_id)
                if payment_record is None:
                    return
                existing_meta = dict(getattr(payment_record, 'metadata_json', None) or {})
                existing_meta.update(guest_metadata)
                payment_record.metadata_json = existing_meta
                await db.commit()
            except Exception as patch_error:
                logger.warning(
                    'Failed to patch guest metadata into payment record',
                    model_name=model_name,
                    local_payment_id=local_payment_id,
                    error=patch_error,
                )

        # --- YooKassa (card / sbp) -------------------------------------------
        if payment_method in ('yookassa', 'yookassa_card', 'yookassa_sbp'):
            if self.yookassa_service is None:
                logger.warning('YooKassa is not enabled, cannot create guest payment')
                return None

            option = 'sbp' if payment_method == 'yookassa_sbp' else 'card'

            if option == 'sbp':
                result = await self.create_yookassa_sbp_payment(
                    db=db,
                    user_id=None,
                    amount_kopeks=amount_kopeks,
                    description=description,
                    metadata=guest_metadata,
                    return_url=return_url,
                )
            else:
                result = await self.create_yookassa_payment(
                    db=db,
                    user_id=None,
                    amount_kopeks=amount_kopeks,
                    description=description,
                    metadata=guest_metadata,
                    return_url=return_url,
                )

            if result:
                return {
                    'payment_url': result.get('confirmation_url'),
                    'payment_id': result.get('yookassa_payment_id'),
                    'provider': 'yookassa',
                }
            return None

        # --- CryptoBot --------------------------------------------------------
        if payment_method == 'cryptobot':
            if self.cryptobot_service is None:
                logger.warning('CryptoBot is not enabled, cannot create guest payment')
                return None

            amount_rubles = amount_kopeks / 100
            try:
                amount_usd = await currency_converter.rub_to_usd(amount_rubles)
            except Exception as conv_error:
                logger.error('Currency conversion failed for CryptoBot guest payment', error=conv_error)
                return None

            # Encode guest metadata into the payload string (CryptoBot uses payload, not metadata dict)
            payload_str = json.dumps(guest_metadata, ensure_ascii=False)

            result = await self.create_cryptobot_payment(
                db=db,
                user_id=None,
                amount_usd=amount_usd,
                asset='USDT',
                description=description,
                payload=payload_str,
            )
            if result:
                # CryptoBot stores guest_metadata in the payload field (no metadata_json column)
                payment_url = result.get('bot_invoice_url') or result.get('mini_app_invoice_url')
                return {
                    'payment_url': payment_url,
                    'payment_id': result.get('invoice_id'),
                    'provider': 'cryptobot',
                }
            return None

        # --- Heleket ----------------------------------------------------------
        if payment_method == 'heleket':
            if self.heleket_service is None:
                logger.warning('Heleket is not enabled, cannot create guest payment')
                return None

            result = await self.create_heleket_payment(
                db=db,
                user_id=None,
                amount_kopeks=amount_kopeks,
                description=description,
                return_url=return_url,
            )
            if result:
                await _patch_guest_metadata(result['local_payment_id'], 'heleket')
                return {
                    'payment_url': result.get('payment_url'),
                    'payment_id': result.get('uuid'),
                    'provider': 'heleket',
                }
            return None

        # --- MulenPay ---------------------------------------------------------
        if payment_method == 'mulenpay':
            if self.mulenpay_service is None:
                logger.warning('MulenPay is not enabled, cannot create guest payment')
                return None

            result = await self.create_mulenpay_payment(
                db=db,
                user_id=None,
                amount_kopeks=amount_kopeks,
                description=description,
            )
            if result:
                await _patch_guest_metadata(result['local_payment_id'], 'mulenpay')
                return {
                    'payment_url': result.get('payment_url'),
                    'payment_id': result.get('uuid'),
                    'provider': 'mulenpay',
                }
            return None

        # --- Pal24 (PayPalych) ------------------------------------------------
        if payment_method in ('pal24', 'pal24_sbp', 'pal24_card'):
            if self.pal24_service is None:
                logger.warning('Pal24 is not enabled, cannot create guest payment')
                return None

            pal24_method = (
                'sbp' if payment_method == 'pal24_sbp' else ('card' if payment_method == 'pal24_card' else None)
            )

            result = await self.create_pal24_payment(
                db=db,
                user_id=None,
                amount_kopeks=amount_kopeks,
                description=description,
                language=settings.DEFAULT_LANGUAGE,
                payment_method=pal24_method,
            )
            if result:
                await _patch_guest_metadata(result['local_payment_id'], 'pal24')
                return {
                    'payment_url': result.get('payment_url') or result.get('primary_url'),
                    'payment_id': result.get('bill_id'),
                    'provider': 'pal24',
                }
            return None

        # --- Platega ----------------------------------------------------------
        if payment_method.startswith('platega'):
            if self.platega_service is None:
                logger.warning('Platega is not enabled, cannot create guest payment')
                return None

            # Extract method code: "platega_2" -> 2, "platega" -> first active method
            method_code: int | None = None
            if '_' in payment_method:
                suffix = payment_method.split('_', 1)[1]
                try:
                    method_code = int(suffix)
                except ValueError:
                    pass

            if method_code is None:
                active_methods = settings.get_platega_active_methods()
                method_code = active_methods[0] if active_methods else 2

            result = await self.create_platega_payment(
                db=db,
                user_id=None,
                amount_kopeks=amount_kopeks,
                description=description,
                language=settings.DEFAULT_LANGUAGE,
                payment_method_code=method_code,
                return_url=return_url,
            )
            if result:
                await _patch_guest_metadata(result['local_payment_id'], 'platega')
                return {
                    'payment_url': result.get('redirect_url'),
                    'payment_id': result.get('correlation_id'),
                    'provider': 'platega',
                }
            return None

        # --- WATA -------------------------------------------------------------
        if payment_method == 'wata':
            if self.wata_service is None:
                logger.warning('WATA is not enabled, cannot create guest payment')
                return None

            result = await self.create_wata_payment(
                db=db,
                user_id=None,
                amount_kopeks=amount_kopeks,
                description=description,
                return_url=return_url,
            )
            if result:
                await _patch_guest_metadata(result['local_payment_id'], 'wata')
                return {
                    'payment_url': result.get('payment_url'),
                    'payment_id': result.get('payment_link_id'),
                    'provider': 'wata',
                }
            return None

        # --- CloudPayments ----------------------------------------------------
        if payment_method == 'cloudpayments':
            if self.cloudpayments_service is None:
                logger.warning('CloudPayments is not enabled, cannot create guest payment')
                return None

            result = await self.create_cloudpayments_payment(
                db=db,
                user_id=None,
                amount_kopeks=amount_kopeks,
                description=description,
                return_url=return_url,
            )
            if result:
                await _patch_guest_metadata(result['payment_id'], 'cloudpayments')
                return {
                    'payment_url': result.get('payment_url'),
                    'payment_id': result.get('invoice_id'),
                    'provider': 'cloudpayments',
                }
            return None

        # --- Freekassa --------------------------------------------------------
        if payment_method in ('freekassa', 'freekassa_sbp', 'freekassa_card'):
            if not settings.is_freekassa_enabled():
                logger.warning('Freekassa is not enabled, cannot create guest payment')
                return None

            result = await self.create_freekassa_payment(
                db=db,
                user_id=None,
                amount_kopeks=amount_kopeks,
                description=description,
                payment_method=payment_method,
            )
            if result:
                await _patch_guest_metadata(result['local_payment_id'], 'freekassa')
                return {
                    'payment_url': result.get('payment_url'),
                    'payment_id': result.get('order_id'),
                    'provider': 'freekassa',
                }
            return None

        # --- KassaAI ----------------------------------------------------------
        if payment_method in ('kassa_ai', 'kassa_ai_sbp', 'kassa_ai_card', 'kassa_ai_sberpay'):
            if not settings.is_kassa_ai_enabled():
                logger.warning('KassaAI is not enabled, cannot create guest payment')
                return None

            from app.services.kassa_ai_service import KASSA_AI_SUB_METHODS

            sub = KASSA_AI_SUB_METHODS.get(payment_method)
            ps_id = sub['payment_system_id'] if sub else None

            result = await self.create_kassa_ai_payment(
                db=db,
                user_id=None,
                amount_kopeks=amount_kopeks,
                description=description,
                payment_system_id=ps_id,
            )
            if result:
                await _patch_guest_metadata(result['local_payment_id'], 'kassa_ai')
                return {
                    'payment_url': result.get('payment_url'),
                    'payment_id': result.get('order_id'),
                    'provider': payment_method,
                }
            return None

        # --- RioPay -----------------------------------------------------------
        if payment_method == 'riopay':
            if not settings.is_riopay_enabled():
                logger.warning('RioPay is not enabled, cannot create guest payment')
                return None

            result = await self.create_riopay_payment(
                db=db,
                user_id=None,
                amount_kopeks=amount_kopeks,
                description=description,
                success_url=return_url,
                fail_url=return_url,
            )
            if result:
                await _patch_guest_metadata(result['local_payment_id'], 'riopay')
                return {
                    'payment_url': result.get('payment_url'),
                    'payment_id': result.get('riopay_order_id') or result.get('order_id'),
                    'provider': 'riopay',
                }
            return None

        # --- SeverPay ---------------------------------------------------------
        if payment_method == 'severpay':
            if not settings.is_severpay_enabled():
                logger.warning('SeverPay is not enabled, cannot create guest payment')
                return None

            result = await self.create_severpay_payment(
                db=db,
                user_id=None,
                amount_kopeks=amount_kopeks,
                description=description,
                return_url=return_url,
            )
            if result:
                await _patch_guest_metadata(result['local_payment_id'], 'severpay')
                return {
                    'payment_url': result.get('payment_url'),
                    'payment_id': result.get('severpay_id') or result.get('order_id'),
                    'provider': 'severpay',
                }
            return None

        # --- Telegram Stars ---------------------------------------------------
        if payment_method == 'telegram_stars':
            if not settings.TELEGRAM_STARS_ENABLED:
                logger.warning('Telegram Stars is not enabled, cannot create guest payment')
                return None

            if self.bot is None:
                logger.warning('Bot instance required for Stars guest payment')
                return None

            from aiogram.types import LabeledPrice

            rate = settings.get_stars_rate()
            if rate <= 0:
                logger.error('TELEGRAM_STARS_RATE_RUB is not positive, cannot create Stars invoice')
                return None

            amount_rubles = amount_kopeks / 100
            stars_amount = max(1, round(amount_rubles / rate))

            payload = f'guest_purchase_{purchase_token}'

            try:
                invoice_url = await self.bot.create_invoice_link(
                    title='Подарочная подписка VPN',
                    description=f'{description} ({stars_amount} ⭐)',
                    payload=payload,
                    provider_token='',
                    currency='XTR',
                    prices=[LabeledPrice(label='Подарочная подписка', amount=stars_amount)],
                )

                logger.info(
                    'Created Stars invoice for guest purchase',
                    stars_amount=stars_amount,
                    purchase_token_prefix=purchase_token[:5],
                )
                return {
                    'payment_url': invoice_url,
                    'payment_id': f'stars_{purchase_token[:12]}',
                    'provider': 'telegram_stars',
                }

            except Exception as stars_error:
                logger.error('Error creating Stars invoice for guest payment', error=stars_error)
                return None

        # --- Unsupported provider ---------------------------------------------
        logger.warning(
            'Guest payment requested for unsupported provider',
            payment_method=payment_method,
            purchase_token_prefix=purchase_token[:5],
        )
        return None
