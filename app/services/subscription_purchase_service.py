from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PERIOD_PRICES, settings
from app.database.crud.server_squad import (
    add_user_to_servers,
    get_available_server_squads,
    get_server_squad_by_uuid,
)
from app.database.crud.subscription import (
    add_subscription_servers,
    create_paid_subscription,
)
from app.database.crud.subscription_conversion import (
    create_subscription_conversion,
)
from app.database.crud.transaction import create_transaction
from app.database.crud.user import subtract_user_balance
from app.database.models import PaymentMethod, ServerSquad, Subscription, SubscriptionStatus, TransactionType, User
from app.localization.texts import get_texts
from app.services.subscription_service import SubscriptionService
from app.utils.pricing_utils import (
    apply_percentage_discount,
    calculate_months_from_days,
    format_period_description,
    validate_pricing_calculation,
)


logger = structlog.get_logger(__name__)


@dataclass
class PurchaseTrafficOption:
    value: int
    label: str
    price_per_month: int
    price_label: str
    original_price_per_month: int | None = None
    original_price_label: str | None = None
    discount_percent: int = 0
    is_available: bool = True
    is_default: bool = False

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'value': self.value,
            'label': self.label,
            'price_kopeks': self.price_per_month,
            'price_label': self.price_label,
            'is_available': self.is_available,
        }
        if self.original_price_per_month is not None and (
            self.original_price_label and self.original_price_per_month != self.price_per_month
        ):
            payload['original_price_kopeks'] = self.original_price_per_month
            payload['original_price_label'] = self.original_price_label
        if self.discount_percent:
            payload['discount_percent'] = self.discount_percent
        if self.is_default:
            payload['is_default'] = True
        return payload


@dataclass
class PurchaseTrafficConfig:
    selectable: bool
    mode: str
    options: list[PurchaseTrafficOption] = field(default_factory=list)
    default_value: int | None = None
    current_value: int | None = None
    hint: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'selectable': self.selectable,
            'mode': self.mode,
        }
        if self.options:
            payload['options'] = [option.to_payload() for option in self.options]
        if self.default_value is not None:
            payload['default'] = self.default_value
        if self.current_value is not None:
            payload['current'] = self.current_value
        if self.hint:
            payload['hint'] = self.hint
        return payload


@dataclass
class PurchaseServerOption:
    uuid: str
    name: str
    price_per_month: int
    price_label: str
    original_price_per_month: int | None = None
    original_price_label: str | None = None
    discount_percent: int = 0
    is_available: bool = True

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'uuid': self.uuid,
            'name': self.name,
            'price_kopeks': self.price_per_month,
            'price_label': self.price_label,
            'is_available': self.is_available,
        }
        if self.original_price_per_month is not None and (
            self.original_price_label and self.original_price_per_month != self.price_per_month
        ):
            payload['original_price_kopeks'] = self.original_price_per_month
            payload['original_price_label'] = self.original_price_label
        if self.discount_percent:
            payload['discount_percent'] = self.discount_percent
        return payload


@dataclass
class PurchaseServersConfig:
    options: list[PurchaseServerOption]
    min_selectable: int
    max_selectable: int
    default_selection: list[str]
    hint: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'options': [option.to_payload() for option in self.options],
            'min': self.min_selectable,
            'max': self.max_selectable,
            'default': list(self.default_selection),
            'selected': list(self.default_selection),
        }
        if self.hint:
            payload['hint'] = self.hint
        return payload


@dataclass
class PurchaseDevicesConfig:
    minimum: int
    maximum: int
    default: int
    current: int
    price_per_device: int
    discounted_price_per_device: int
    price_label: str
    original_price_label: str | None = None
    discount_percent: int = 0
    hint: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'min': self.minimum,
            'max': self.maximum,
            'default': self.default,
            'current': self.current,
            'price_per_device_kopeks': self.discounted_price_per_device,
            'price_per_device_label': self.price_label,
        }
        if self.price_per_device and self.price_per_device != self.discounted_price_per_device:
            payload['price_per_device_original_kopeks'] = self.price_per_device
            if self.original_price_label:
                payload['price_per_device_original_label'] = self.original_price_label
        if self.discount_percent:
            payload['discount_percent'] = self.discount_percent
        if self.hint:
            payload['hint'] = self.hint
        return payload


@dataclass
class PurchasePeriodConfig:
    id: str
    days: int
    months: int
    label: str
    base_price: int
    base_price_label: str
    base_price_original: int
    base_price_original_label: str | None
    discount_percent: int
    per_month_price: int
    per_month_price_label: str
    traffic: PurchaseTrafficConfig
    servers: PurchaseServersConfig
    devices: PurchaseDevicesConfig

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'id': self.id,
            'code': self.id,
            'period_id': self.id,
            'period_days': self.days,
            'period': self.days,
            'months': self.months,
            'label': self.label,
            'price_kopeks': self.base_price,
            'price_label': self.base_price_label,
            'per_month_price_kopeks': self.per_month_price,
            'per_month_price_label': self.per_month_price_label,
            'is_available': True,
            'traffic': self.traffic.to_payload(),
            'servers': self.servers.to_payload(),
            'devices': self.devices.to_payload(),
        }
        if self.discount_percent:
            payload['discount_percent'] = self.discount_percent
        if self.base_price_original and self.base_price_original_label and self.base_price_original != self.base_price:
            payload['original_price_kopeks'] = self.base_price_original
            payload['original_price_label'] = self.base_price_original_label
        return payload


@dataclass
class PurchaseSelection:
    period: PurchasePeriodConfig
    traffic_value: int
    servers: list[str]
    devices: int


@dataclass
class PurchasePricingResult:
    selection: PurchaseSelection
    server_ids: list[int]
    server_prices_for_period: list[int]
    base_original_total: int
    discounted_total: int
    promo_discount_value: int
    promo_discount_percent: int
    final_total: int
    months: int
    details: dict[str, Any]


@dataclass
class PurchaseOptionsContext:
    user: User
    subscription: Subscription | None
    currency: str
    balance_kopeks: int
    periods: list[PurchasePeriodConfig]
    default_period: PurchasePeriodConfig
    period_map: dict[str, PurchasePeriodConfig]
    server_uuid_to_id: dict[str, int]
    payload: dict[str, Any]


class PurchaseValidationError(Exception):
    def __init__(self, message: str, code: str = 'invalid_selection') -> None:
        super().__init__(message)
        self.code = code


class PurchaseBalanceError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)


def _apply_discount_to_monthly_component(amount_per_month: int, percent: int, months: int) -> dict[str, int]:
    discounted_per_month, discount_per_month = apply_percentage_discount(amount_per_month, percent)
    return {
        'original_per_month': amount_per_month,
        'discounted_per_month': discounted_per_month,
        'discount_percent': max(0, min(100, percent)),
        'discount_per_month': discount_per_month,
        'total': discounted_per_month * months,
        'discount_total': discount_per_month * months,
    }


def _build_server_option(
    server: ServerSquad,
    discount_percent: int,
    texts,
) -> PurchaseServerOption:
    base_per_month = int(getattr(server, 'price_kopeks', 0) or 0)
    discounted_per_month, _ = apply_percentage_discount(base_per_month, discount_percent)
    return PurchaseServerOption(
        uuid=server.squad_uuid,
        name=getattr(server, 'display_name', server.squad_uuid) or server.squad_uuid,
        price_per_month=discounted_per_month,
        price_label=texts.format_price(discounted_per_month),
        original_price_per_month=base_per_month,
        original_price_label=texts.format_price(base_per_month) if base_per_month != discounted_per_month else None,
        discount_percent=max(0, discount_percent),
        is_available=bool(getattr(server, 'is_available', True) and not getattr(server, 'is_full', False)),
    )


class MiniAppSubscriptionPurchaseService:
    """Builds configuration and pricing for subscription purchases in the mini app."""

    async def build_options(
        self, db: AsyncSession, user: User, subscription_id: int | None = None
    ) -> PurchaseOptionsContext:
        from app.database.crud.subscription import get_subscription_by_user_id

        if settings.is_multi_tariff_enabled():
            if subscription_id:
                from app.database.crud.subscription import get_subscription_by_id_for_user

                subscription = await get_subscription_by_id_for_user(db, subscription_id, user.id)
            else:
                from app.database.crud.subscription import get_active_subscriptions_by_user_id

                active_subs = await get_active_subscriptions_by_user_id(db, user.id)
                if active_subs:
                    _non_daily = [s for s in active_subs if not getattr(s, 'is_daily_tariff', False)]
                    _pool = _non_daily or active_subs
                    subscription = max(_pool, key=lambda s: s.days_left)
                else:
                    subscription = None
        else:
            subscription = await get_subscription_by_user_id(db, user.id)
        balance_kopeks = int(getattr(user, 'balance_kopeks', 0) or 0)
        currency = (getattr(user, 'balance_currency', None) or 'RUB').upper()
        texts = get_texts(getattr(user, 'language', None))

        available_servers = await get_available_server_squads(
            db,
            promo_group_id=getattr(user, 'promo_group_id', None),
        )
        server_catalog: dict[str, ServerSquad] = {server.squad_uuid: server for server in available_servers}

        if subscription and subscription.connected_squads:
            for uuid in subscription.connected_squads:
                if uuid in server_catalog:
                    continue
                try:
                    existing = await get_server_squad_by_uuid(db, uuid)
                except Exception as error:  # pragma: no cover - defensive logging
                    logger.warning('Failed to load server squad', uuid=uuid, error=error)
                    existing = None
                if existing:
                    server_catalog[uuid] = existing

        server_uuid_to_id: dict[str, int] = {}
        for server in server_catalog.values():
            try:
                server_uuid_to_id[server.squad_uuid] = int(getattr(server, 'id', 0) or 0)
            except (TypeError, ValueError):
                continue

        default_connected = list(getattr(subscription, 'connected_squads', []) or [])
        if not default_connected:
            for server in available_servers:
                if getattr(server, 'is_available', True) and not getattr(server, 'is_full', False):
                    default_connected = [server.squad_uuid]
                    break

        available_periods: Sequence[int] = settings.get_available_subscription_periods()
        periods: list[PurchasePeriodConfig] = []
        period_map: dict[str, PurchasePeriodConfig] = {}

        default_devices = settings.DEFAULT_DEVICE_LIMIT
        # Для триала НЕ используем его ограничения как дефолтные,
        # чтобы при продлении клиент получил стандартные значения платной подписки
        is_trial_subscription = subscription and getattr(subscription, 'is_trial', False)
        if subscription and getattr(subscription, 'device_limit', None) and not is_trial_subscription:
            default_devices = max(default_devices, int(subscription.device_limit))

        fixed_traffic_value = None
        if settings.is_traffic_fixed():
            fixed_traffic_value = settings.get_fixed_traffic_limit()
        elif subscription and subscription.traffic_limit_gb is not None and not is_trial_subscription:
            fixed_traffic_value = subscription.traffic_limit_gb

        default_period_days = available_periods[0] if available_periods else 30

        for period_days in available_periods:
            months = calculate_months_from_days(period_days)
            period_id = f'days:{period_days}'
            label = format_period_description(period_days, getattr(user, 'language', 'ru'))

            base_price_original = PERIOD_PRICES.get(period_days, 0)
            period_discount_percent = user.get_promo_discount('period', period_days)
            base_price, base_discount_total = apply_percentage_discount(base_price_original, period_discount_percent)
            base_price_label = texts.format_price(base_price)
            base_price_original_label = (
                texts.format_price(base_price_original)
                if base_discount_total and base_price_original != base_price
                else None
            )

            per_month_price = base_price // months if months else base_price
            per_month_price_label = texts.format_price(per_month_price)

            traffic_config = self._build_traffic_config(
                user,
                texts,
                period_days,
                months,
                fixed_traffic_value,
            )
            servers_config = self._build_servers_config(
                user,
                texts,
                period_days,
                server_catalog,
                default_connected,
            )
            devices_config = self._build_devices_config(
                user,
                texts,
                period_days,
                default_devices,
            )

            period_config = PurchasePeriodConfig(
                id=period_id,
                days=period_days,
                months=months,
                label=label,
                base_price=base_price,
                base_price_label=base_price_label,
                base_price_original=base_price_original,
                base_price_original_label=base_price_original_label,
                discount_percent=max(0, period_discount_percent),
                per_month_price=per_month_price,
                per_month_price_label=per_month_price_label,
                traffic=traffic_config,
                servers=servers_config,
                devices=devices_config,
            )

            periods.append(period_config)
            period_map[period_id] = period_config

        if not periods:
            raise PurchaseValidationError('No subscription periods configured', code='configuration')

        default_period = period_map.get(f'days:{default_period_days}') or periods[0]

        default_selection = {
            'period_id': default_period.id,
            'periodId': default_period.id,
            'period_days': default_period.days,
            'periodDays': default_period.days,
            'traffic_value': default_period.traffic.current_value
            if default_period.traffic.current_value is not None
            else default_period.traffic.default_value,
            'trafficValue': default_period.traffic.current_value
            if default_period.traffic.current_value is not None
            else default_period.traffic.default_value,
            'servers': list(default_period.servers.default_selection),
            'countries': list(default_period.servers.default_selection),
            'server_uuids': list(default_period.servers.default_selection),
            'serverUuids': list(default_period.servers.default_selection),
            'devices': default_period.devices.current,
            'device_limit': default_period.devices.current,
            'deviceLimit': default_period.devices.current,
        }

        payload = {
            'currency': currency,
            'balance_kopeks': balance_kopeks,
            'balanceKopeks': balance_kopeks,
            'balance_label': texts.format_price(balance_kopeks),
            'balanceLabel': texts.format_price(balance_kopeks),
            'subscription_id': getattr(subscription, 'id', None),
            'subscriptionId': getattr(subscription, 'id', None),
            'periods': [period.to_payload() for period in periods],
            'traffic': default_period.traffic.to_payload(),
            'servers': default_period.servers.to_payload(),
            'devices': default_period.devices.to_payload(),
            'selection': default_selection,
            'summary': None,
        }

        return PurchaseOptionsContext(
            user=user,
            subscription=subscription,
            currency=currency,
            balance_kopeks=balance_kopeks,
            periods=periods,
            default_period=default_period,
            period_map=period_map,
            server_uuid_to_id=server_uuid_to_id,
            payload=payload,
        )

    def _build_traffic_config(
        self,
        user: User,
        texts,
        period_days: int,
        months: int,
        fixed_traffic_value: int | None,
    ) -> PurchaseTrafficConfig:
        if settings.is_traffic_fixed():
            value = fixed_traffic_value if fixed_traffic_value is not None else settings.get_fixed_traffic_limit()
            # Передаём актуальный режим (fixed или fixed_with_topup)
            actual_mode = settings.TRAFFIC_SELECTION_MODE.lower()
            return PurchaseTrafficConfig(
                selectable=False,
                mode=actual_mode,
                options=[],
                default_value=value,
                current_value=value,
                hint=None,
            )

        packages = [package for package in settings.get_traffic_packages() if package.get('enabled', True)]
        discount_percent = user.get_promo_discount('traffic', period_days)
        options: list[PurchaseTrafficOption] = []

        for package in packages:
            value = int(package.get('gb') or 0)
            price_per_month = int(package.get('price') or 0)
            discounted_per_month, discount_value = apply_percentage_discount(price_per_month, discount_percent)
            label = texts.format_traffic(value or 0)
            options.append(
                PurchaseTrafficOption(
                    value=value,
                    label=label,
                    price_per_month=discounted_per_month,
                    price_label=texts.format_price(discounted_per_month),
                    original_price_per_month=price_per_month,
                    original_price_label=texts.format_price(price_per_month)
                    if discount_value and price_per_month != discounted_per_month
                    else None,
                    discount_percent=max(0, discount_percent),
                    is_available=True,
                )
            )

        default_option = None
        if fixed_traffic_value is not None:
            for option in options:
                if option.value == fixed_traffic_value:
                    default_option = option
                    option.is_default = True
                    break
        if default_option is None and options:
            options[0].is_default = True
            default_option = options[0]

        default_value = default_option.value if default_option else (fixed_traffic_value or 0)

        return PurchaseTrafficConfig(
            selectable=True,
            mode='selectable',
            options=options,
            default_value=default_value,
            current_value=default_value,
            hint=None,
        )

    def _build_servers_config(
        self,
        user: User,
        texts,
        period_days: int,
        server_catalog: dict[str, ServerSquad],
        default_selection: list[str],
    ) -> PurchaseServersConfig:
        discount_percent = user.get_promo_discount('servers', period_days)
        options: list[PurchaseServerOption] = []

        for server in server_catalog.values():
            option = _build_server_option(server, discount_percent, texts)
            options.append(option)

        if not options:
            default_selection = []

        return PurchaseServersConfig(
            options=options,
            min_selectable=1 if options else 0,
            max_selectable=len(options),
            default_selection=default_selection or [opt.uuid for opt in options[:1]],
            hint=None,
        )

    def _build_devices_config(
        self,
        user: User,
        texts,
        period_days: int,
        default_devices: int,
    ) -> PurchaseDevicesConfig:
        discount_percent = user.get_promo_discount('devices', period_days)
        unit_price = settings.PRICE_PER_DEVICE
        discounted_unit_price, unit_discount_value = apply_percentage_discount(unit_price, discount_percent)
        price_label = texts.format_price(discounted_unit_price)
        original_label = (
            texts.format_price(unit_price) if unit_discount_value and unit_price != discounted_unit_price else None
        )

        max_devices_setting = settings.MAX_DEVICES_LIMIT if settings.MAX_DEVICES_LIMIT > 0 else None
        if max_devices_setting is not None:
            maximum = max(max_devices_setting, default_devices)
        else:
            maximum = max(default_devices, settings.DEFAULT_DEVICE_LIMIT) + 10

        return PurchaseDevicesConfig(
            minimum=settings.DEFAULT_DEVICE_LIMIT,
            maximum=maximum,
            default=default_devices,
            current=default_devices,
            price_per_device=unit_price,
            discounted_price_per_device=discounted_unit_price,
            price_label=price_label,
            original_price_label=original_label,
            discount_percent=max(0, discount_percent),
            hint=None,
        )

    def parse_selection(
        self,
        context: PurchaseOptionsContext,
        selection_payload: dict[str, Any],
    ) -> PurchaseSelection:
        period_id = (
            selection_payload.get('period_id')
            or selection_payload.get('periodId')
            or selection_payload.get('period')
            or selection_payload.get('code')
        )
        if not period_id:
            period_days = selection_payload.get('period_days') or selection_payload.get('periodDays')
            if period_days is not None:
                period_id = f'days:{int(period_days)}'

        if not period_id or period_id not in context.period_map:
            raise PurchaseValidationError('Invalid or missing subscription period', code='invalid_period')

        period = context.period_map[period_id]

        # Don't use `or` chaining - 0 is valid for unlimited traffic
        traffic_value = None
        for key in ('traffic_value', 'trafficValue', 'traffic', 'traffic_gb', 'trafficGb'):
            value = selection_payload.get(key)
            if value is not None:
                traffic_value = value
                break

        if period.traffic.selectable:
            available_values = {option.value for option in period.traffic.options}
            if traffic_value is None:
                traffic_value = period.traffic.current_value or period.traffic.default_value
            else:
                traffic_value = int(traffic_value)
                if available_values and traffic_value not in available_values:
                    raise PurchaseValidationError('Selected traffic option is not available', code='invalid_traffic')
        else:
            traffic_value = period.traffic.current_value or period.traffic.default_value or 0

        raw_servers: list[str] = []
        for key in ('servers', 'countries', 'server_uuids', 'serverUuids'):
            value = selection_payload.get(key)
            if isinstance(value, list):
                raw_servers.extend(value)

        servers: list[str] = []
        seen = set()
        for raw in raw_servers:
            if not raw:
                continue
            uuid = str(raw).strip()
            if not uuid or uuid in seen:
                continue
            seen.add(uuid)
            servers.append(uuid)

        if not servers:
            servers = list(period.servers.default_selection)

        if period.servers.min_selectable and len(servers) < period.servers.min_selectable:
            raise PurchaseValidationError('Select at least one server', code='invalid_servers')

        if period.servers.max_selectable and len(servers) > period.servers.max_selectable:
            servers = servers[: period.servers.max_selectable]

        devices = (
            selection_payload.get('devices')
            or selection_payload.get('device_limit')
            or selection_payload.get('deviceLimit')
            or period.devices.current
            or period.devices.default
        )
        try:
            devices = int(devices)
        except (TypeError, ValueError):
            raise PurchaseValidationError('Invalid devices selection', code='invalid_devices')

        devices = max(devices, period.devices.minimum)
        if period.devices.maximum and devices > period.devices.maximum:
            devices = period.devices.maximum

        return PurchaseSelection(
            period=period,
            traffic_value=int(traffic_value or 0),
            servers=servers,
            devices=devices,
        )

    async def calculate_pricing(
        self,
        db: AsyncSession,
        context: PurchaseOptionsContext,
        selection: PurchaseSelection,
    ) -> PurchasePricingResult:
        get_texts(getattr(context.user, 'language', None))
        months = selection.period.months

        # PricingEngine — single source of truth (includes promo-offer internally).
        # Server validation is done via breakdown (avoids a duplicate DB query).
        from app.services.pricing_engine import PricingEngine, pricing_engine

        pricing = await pricing_engine.calculate_classic_new_subscription_price(
            db,
            selection.period.days,
            list(selection.servers),
            selection.traffic_value,
            selection.devices,
            user=context.user,
        )

        # Validate all requested servers were found
        server_ids = pricing.breakdown.get('server_ids', [])
        if len(server_ids) != len(selection.servers):
            raise PurchaseValidationError('Some selected servers are not available', code='invalid_servers')

        details = PricingEngine.classic_pricing_to_purchase_details(pricing)

        base_original_total = pricing.original_total
        discounted_total = pricing.final_total + pricing.promo_offer_discount  # subtotal before offer
        promo_discount_value = pricing.promo_offer_discount
        promo_percent = pricing.breakdown.get('offer_discount_pct', 0)

        is_valid = validate_pricing_calculation(
            details.get('base_price', 0),
            (details.get('traffic_price_per_month', 0) - details.get('traffic_discount_total', 0) // max(1, months))
            + (details.get('servers_price_per_month', 0) - details.get('servers_discount_total', 0) // max(1, months))
            + (details.get('devices_price_per_month', 0) - details.get('devices_discount_total', 0) // max(1, months)),
            months,
            discounted_total,
        )

        if not is_valid:
            raise PurchaseValidationError('Failed to validate pricing', code='calculation_error')

        return PurchasePricingResult(
            selection=selection,
            server_ids=server_ids,
            server_prices_for_period=list(details.get('servers_individual_prices', [])),
            base_original_total=base_original_total,
            discounted_total=discounted_total,
            promo_discount_value=promo_discount_value,
            promo_discount_percent=promo_percent,
            final_total=pricing.final_total,
            months=months,
            details=details,
        )

    def build_preview_payload(
        self,
        context: PurchaseOptionsContext,
        pricing: PurchasePricingResult,
    ) -> dict[str, Any]:
        texts = get_texts(getattr(context.user, 'language', None))
        details = pricing.details

        total_discount = pricing.base_original_total - pricing.final_total
        overall_discount_percent = 0
        if pricing.base_original_total > 0 and total_discount > 0:
            overall_discount_percent = int(round(total_discount * 100 / pricing.base_original_total))

        discount_lines: list[str] = []

        def build_discount_line(key: str, default: str, amount: int, percent: int) -> str | None:
            if not amount:
                return None
            return texts.t(key, default).format(
                amount=texts.format_price(amount),
                percent=percent,
            )

        def build_discount_note(amount: int, percent: int) -> str | None:
            if not amount:
                return None
            return texts.t(
                'MINIAPP_PURCHASE_BREAKDOWN_DISCOUNT_NOTE',
                'Discount: -{amount} ({percent}%)',
            ).format(
                amount=texts.format_price(amount),
                percent=percent,
            )

        base_discount_line = build_discount_line(
            'MINIAPP_PURCHASE_DISCOUNT_PERIOD',
            'Period discount: -{amount} ({percent}%)',
            details.get('base_discount_total', 0),
            details.get('base_discount_percent', 0),
        )
        if base_discount_line:
            discount_lines.append(base_discount_line)

        traffic_discount_line = build_discount_line(
            'MINIAPP_PURCHASE_DISCOUNT_TRAFFIC',
            'Traffic discount: -{amount} ({percent}%)',
            details.get('traffic_discount_total', 0),
            details.get('traffic_discount_percent', 0),
        )
        if traffic_discount_line:
            discount_lines.append(traffic_discount_line)

        servers_discount_line = build_discount_line(
            'MINIAPP_PURCHASE_DISCOUNT_SERVERS',
            'Servers discount: -{amount} ({percent}%)',
            details.get('servers_discount_total', 0),
            details.get('servers_discount_percent', 0),
        )
        if servers_discount_line:
            discount_lines.append(servers_discount_line)

        devices_discount_line = build_discount_line(
            'MINIAPP_PURCHASE_DISCOUNT_DEVICES',
            'Devices discount: -{amount} ({percent}%)',
            details.get('devices_discount_total', 0),
            details.get('devices_discount_percent', 0),
        )
        if devices_discount_line:
            discount_lines.append(devices_discount_line)

        promo_discount_line = None
        if pricing.promo_discount_value:
            promo_discount_line = texts.t(
                'MINIAPP_PURCHASE_DISCOUNT_PROMO',
                'Promo offer: -{amount} ({percent}%)',
            ).format(
                amount=texts.format_price(pricing.promo_discount_value),
                percent=pricing.promo_discount_percent,
            )
            discount_lines.append(promo_discount_line)

        breakdown = [
            {
                'label': texts.t(
                    'MINIAPP_PURCHASE_BREAKDOWN_BASE',
                    'Base plan',
                ),
                'value': texts.format_price(details.get('base_price', 0)),
            }
        ]

        base_discount_note = build_discount_note(
            details.get('base_discount_total', 0),
            details.get('base_discount_percent', 0),
        )
        if base_discount_note:
            breakdown[0]['discount_label'] = base_discount_note
            breakdown[0]['discountLabel'] = base_discount_note

        if details.get('total_traffic_price'):
            traffic_item = {
                'label': texts.t(
                    'MINIAPP_PURCHASE_BREAKDOWN_TRAFFIC',
                    'Traffic',
                ),
                'value': texts.format_price(details['total_traffic_price']),
            }
            traffic_discount_note = build_discount_note(
                details.get('traffic_discount_total', 0),
                details.get('traffic_discount_percent', 0),
            )
            if traffic_discount_note:
                traffic_item['discount_label'] = traffic_discount_note
                traffic_item['discountLabel'] = traffic_discount_note
            breakdown.append(traffic_item)

        if details.get('total_servers_price'):
            servers_item = {
                'label': texts.t(
                    'MINIAPP_PURCHASE_BREAKDOWN_SERVERS',
                    'Servers',
                ),
                'value': texts.format_price(details['total_servers_price']),
            }
            servers_discount_note = build_discount_note(
                details.get('servers_discount_total', 0),
                details.get('servers_discount_percent', 0),
            )
            if servers_discount_note:
                servers_item['discount_label'] = servers_discount_note
                servers_item['discountLabel'] = servers_discount_note
            breakdown.append(servers_item)

        if details.get('total_devices_price'):
            devices_item = {
                'label': texts.t(
                    'MINIAPP_PURCHASE_BREAKDOWN_DEVICES',
                    'Devices',
                ),
                'value': texts.format_price(details['total_devices_price']),
            }
            devices_discount_note = build_discount_note(
                details.get('devices_discount_total', 0),
                details.get('devices_discount_percent', 0),
            )
            if devices_discount_note:
                devices_item['discount_label'] = devices_discount_note
                devices_item['discountLabel'] = devices_discount_note
            breakdown.append(devices_item)

        if pricing.promo_discount_value:
            promo_item = {
                'label': texts.t(
                    'MINIAPP_PURCHASE_BREAKDOWN_PROMO',
                    'Promo discount',
                ),
                'value': f'- {texts.format_price(pricing.promo_discount_value)}',
            }
            if promo_discount_line:
                promo_item['discount_label'] = promo_discount_line
                promo_item['discountLabel'] = promo_discount_line
            breakdown.append(promo_item)

        missing = max(0, pricing.final_total - context.balance_kopeks)
        status_message = ''
        if missing > 0:
            status_message = texts.t(
                'MINIAPP_PURCHASE_STATUS_INSUFFICIENT',
                'Not enough funds on balance',
            )

        per_month_price = pricing.final_total // pricing.months if pricing.months else pricing.final_total

        return {
            'total_price_kopeks': pricing.final_total,
            'totalPriceKopeks': pricing.final_total,
            'total_price_label': texts.format_price(pricing.final_total),
            'totalPriceLabel': texts.format_price(pricing.final_total),
            'original_price_kopeks': pricing.base_original_total if total_discount else None,
            'originalPriceKopeks': pricing.base_original_total if total_discount else None,
            'original_price_label': texts.format_price(pricing.base_original_total) if total_discount else None,
            'originalPriceLabel': texts.format_price(pricing.base_original_total) if total_discount else None,
            'discount_percent': overall_discount_percent,
            'discountPercent': overall_discount_percent,
            'discount_label': texts.t(
                'MINIAPP_PURCHASE_SUMMARY_DISCOUNT',
                'You save {amount}',
            ).format(amount=texts.format_price(total_discount))
            if total_discount
            else None,
            'discountLabel': texts.t(
                'MINIAPP_PURCHASE_SUMMARY_DISCOUNT',
                'You save {amount}',
            ).format(amount=texts.format_price(total_discount))
            if total_discount
            else None,
            'discount_lines': discount_lines,
            'discountLines': discount_lines,
            'per_month_price_kopeks': per_month_price,
            'perMonthPriceKopeks': per_month_price,
            'per_month_price_label': texts.format_price(per_month_price),
            'perMonthPriceLabel': texts.format_price(per_month_price),
            'breakdown': [{'label': item['label'], 'value': item['value']} for item in breakdown],
            'balance_kopeks': context.balance_kopeks,
            'balanceKopeks': context.balance_kopeks,
            'balance_label': texts.format_price(context.balance_kopeks),
            'balanceLabel': texts.format_price(context.balance_kopeks),
            'missing_amount_kopeks': missing,
            'missingAmountKopeks': missing,
            'missing_amount_label': texts.format_price(missing) if missing else None,
            'missingAmountLabel': texts.format_price(missing) if missing else None,
            'can_purchase': missing == 0,
            'canPurchase': missing == 0,
            'status_message': status_message,
            'statusMessage': status_message,
        }

    async def submit_purchase(
        self,
        db: AsyncSession,
        context: PurchaseOptionsContext,
        pricing: PurchasePricingResult,
    ) -> dict[str, Any]:
        user = context.user
        texts = get_texts(getattr(user, 'language', None))

        # Block only if pricing is genuinely invalid (no base price configured).
        # final_total == 0 with base_original_total > 0 means a valid 100% discount.
        if pricing.final_total <= 0 and pricing.base_original_total <= 0:
            raise PurchaseValidationError('Invalid total amount', code='calculation_error')

        if pricing.final_total > 0 and user.balance_kopeks < pricing.final_total:
            raise PurchaseBalanceError(
                texts.t(
                    'MINIAPP_PURCHASE_STATUS_INSUFFICIENT',
                    'Not enough funds on balance',
                )
            )

        description = f'Покупка подписки на {pricing.selection.period.days} дней'
        success = await subtract_user_balance(
            db,
            user,
            pricing.final_total,
            description,
            consume_promo_offer=pricing.promo_discount_value > 0,
            mark_as_paid_subscription=True,
        )
        if not success:
            raise PurchaseBalanceError(
                texts.t(
                    'MINIAPP_PURCHASE_STATUS_INSUFFICIENT',
                    'Not enough funds on balance',
                )
            )

        await db.refresh(user)

        subscription = context.subscription
        if subscription is not None and getattr(subscription, 'id', None):
            # Lock subscription row to prevent concurrent extension race
            result = await db.execute(
                select(Subscription)
                .where(Subscription.id == subscription.id, Subscription.user_id == user.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            locked_sub = result.scalar_one_or_none()
            if locked_sub is not None:
                subscription = locked_sub
                context.subscription = locked_sub
            else:
                logger.warning(
                    'Subscription from context not found after FOR UPDATE',
                    subscription_id=getattr(subscription, 'id', None),
                    user_id=user.id,
                )
                subscription = None
                context.subscription = None
        else:
            context_subscription_id: int | None = context.payload.get('subscription_id')
            if settings.is_multi_tariff_enabled() and context_subscription_id is not None:
                result = await db.execute(
                    select(Subscription)
                    .where(
                        Subscription.user_id == user.id,
                        Subscription.id == context_subscription_id,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            else:
                result = await db.execute(
                    select(Subscription)
                    .where(Subscription.user_id == user.id)
                    .order_by(Subscription.created_at.desc())
                    .limit(1)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            subscription = result.scalar_one_or_none()
            if subscription is not None:
                context.subscription = subscription

        was_trial_conversion = False
        now = datetime.now(UTC)

        if subscription:
            bonus_period = timedelta()
            if subscription.is_trial:
                was_trial_conversion = True
                trial_duration = (now - subscription.start_date).days
                if settings.TRIAL_ADD_REMAINING_DAYS_TO_PAID and subscription.end_date:
                    remaining = subscription.end_date - now
                    if remaining.total_seconds() > 0:
                        bonus_period = remaining
                try:
                    await create_subscription_conversion(
                        db=db,
                        user_id=user.id,
                        trial_duration_days=trial_duration,
                        payment_method='balance',
                        first_payment_amount_kopeks=pricing.final_total,
                        first_paid_period_days=pricing.selection.period.days,
                    )
                except Exception as conversion_error:  # pragma: no cover - defensive logging
                    logger.error('Failed to create subscription conversion record', conversion_error=conversion_error)

            subscription.is_trial = False
            subscription.status = SubscriptionStatus.ACTIVE.value
            subscription.traffic_limit_gb = pricing.selection.traffic_value
            subscription.device_limit = pricing.selection.devices
            subscription.connected_squads = pricing.selection.servers

            extension_base_date = now
            if subscription.end_date and subscription.end_date > now:
                extension_base_date = subscription.end_date
            else:
                subscription.start_date = now

            subscription.end_date = extension_base_date + timedelta(days=pricing.selection.period.days) + bonus_period
            subscription.updated_at = now
            subscription.traffic_used_gb = 0.0

            await db.commit()
            await db.refresh(subscription)
        else:
            subscription = await create_paid_subscription(
                db=db,
                user_id=user.id,
                duration_days=pricing.selection.period.days,
                traffic_limit_gb=pricing.selection.traffic_value,
                device_limit=pricing.selection.devices,
                connected_squads=pricing.selection.servers,
                update_server_counters=False,
            )

        if pricing.server_ids:
            try:
                await add_subscription_servers(
                    db,
                    subscription,
                    pricing.server_ids,
                    pricing.server_prices_for_period,
                )
                await add_user_to_servers(db, pricing.server_ids)
            except Exception as error:  # pragma: no cover - defensive logging
                logger.error('Failed to register subscription servers', error=error)

        # Kill remaining trial subscriptions (trial = probe, dies on any paid purchase)
        from app.database.crud.subscription import (
            deactivate_user_trial_subscriptions,
            decrement_subscription_server_counts,
        )

        killed_trials = await deactivate_user_trial_subscriptions(db, user.id, exclude_subscription_id=subscription.id)

        # Add remaining trial time from OTHER killed trials (current trial already handled above)
        if settings.TRIAL_ADD_REMAINING_DAYS_TO_PAID and killed_trials:
            extra_seconds = 0
            for _kt in killed_trials:
                if _kt.end_date and _kt.end_date > now:
                    extra_seconds += max(0, (_kt.end_date - now).total_seconds())
            if extra_seconds > 0:
                subscription.end_date = subscription.end_date + timedelta(seconds=extra_seconds)
                await db.commit()
                await db.refresh(subscription)

        subscription_service = SubscriptionService()

        # Disable killed trials on RemnaWave panel
        for trial_sub in killed_trials:
            try:
                _trial_uuid = trial_sub.remnawave_uuid or (
                    getattr(user, 'remnawave_uuid', None) if not settings.is_multi_tariff_enabled() else None
                )
                if _trial_uuid:
                    await subscription_service.disable_remnawave_user(_trial_uuid)
                await decrement_subscription_server_counts(db, trial_sub)
            except Exception as trial_err:
                logger.warning('Failed to disable trial on RemnaWave', error=trial_err, trial_id=trial_sub.id)

        try:
            _purch_uuid = (
                subscription.remnawave_uuid
                if settings.is_multi_tariff_enabled() and subscription.remnawave_uuid
                else getattr(user, 'remnawave_uuid', None)
            )
            if _purch_uuid:
                await subscription_service.update_remnawave_user(
                    db,
                    subscription,
                    reset_traffic=True,
                    reset_reason='miniapp purchase',
                    sync_squads=True,
                )
            else:
                await subscription_service.create_remnawave_user(
                    db,
                    subscription,
                    reset_traffic=True,
                    reset_reason='miniapp purchase',
                )
        except Exception as remnawave_error:  # pragma: no cover - defensive logging
            logger.error('Failed to sync subscription with RemnaWave', remnawave_error=remnawave_error)

        transaction = await create_transaction(
            db=db,
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=pricing.final_total,
            description=f'Подписка на {pricing.selection.period.days} дней ({pricing.months} мес)',
            payment_method=PaymentMethod.BALANCE,
        )

        await db.refresh(user)
        await db.refresh(subscription)

        message = texts.t(
            'SUBSCRIPTION_PURCHASED',
            '🎉 Subscription purchased successfully!',
        )

        if pricing.promo_discount_value:
            note = texts.t(
                'SUBSCRIPTION_PROMO_DISCOUNT_NOTE',
                '⚡ Extra discount {percent}%: -{amount}',
            ).format(
                percent=pricing.promo_discount_percent,
                amount=texts.format_price(pricing.promo_discount_value),
            )
            message = f'{message}\n\n{note}'

        return {
            'subscription': subscription,
            'transaction': transaction,
            'was_trial_conversion': was_trial_conversion,
            'message': message,
        }


class SubscriptionPurchaseService:
    """Service for handling simple subscription purchases with predefined parameters."""

    async def create_subscription_order(
        self,
        db: AsyncSession,
        user_id: int,
        period_days: int,
        device_limit: int,
        traffic_limit_gb: int,
        squad_uuid: str,
        payment_method: str,
        total_price_kopeks: int,
    ):
        """Creates a subscription order with predefined parameters."""
        from app.database.crud.subscription import create_pending_subscription

        # Create a pending subscription
        subscription = await create_pending_subscription(
            db=db,
            user_id=user_id,
            duration_days=period_days,
            traffic_limit_gb=traffic_limit_gb,
            device_limit=device_limit,
            connected_squads=[squad_uuid] if squad_uuid else [],
            payment_method=payment_method,
            total_price_kopeks=total_price_kopeks,
        )

        return subscription


purchase_service = MiniAppSubscriptionPurchaseService()
